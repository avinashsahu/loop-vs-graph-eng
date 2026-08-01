import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import cache
from market_time import MARKET_CLOSE, MARKET_OPEN, is_market_hours, now_ist
from nse_client import (
    get_index_constituents_live_snapshot,
    get_stock_historical_data,
)

log = logging.getLogger("nse")

INTRADAY_CACHE_TTL_MINUTES = float(os.environ.get("INTRADAY_CACHE_TTL_MINUTES", "5"))
NSE_CALL_DELAY_SECONDS = float(os.environ.get("NSE_CALL_DELAY_SECONDS", "0.3"))
NSE_MARKET_DATA_GRACE_MINUTES = float(
    os.environ.get("NSE_MARKET_DATA_GRACE_MINUTES", "15")
)

# Only these intervals are fetched from NSE. 15m/30m are derived from 5m.
_FETCH_LOOKBACK_DAYS = {
    "D": 150,  # ~100 trading sessions
    5: 15,  # long enough to warm SMA50/MACD on derived 30m bars
}
_DERIVED_INTRADAY = (15, 30)
_DEFAULT_TIMEFRAMES = ("D", "30", "15", "5")
_IST = ZoneInfo("Asia/Kolkata")
_SESSION_ORIGIN = pd.Timestamp("1970-01-01 09:15:00")
MARKET_COMPLETION_POLICY_ID = "nse-completed-bars-v1"
# Bumped when fetch set / lookback / derivation semantics change.
_CACHE_NAMESPACE = "hist_v3"


@dataclass(frozen=True)
class MarketSnapshot:
    """Scan-ready completed candles plus enough metadata to audit their freshness."""

    symbol: str
    observed_at: str
    histories: dict[str, pd.DataFrame]
    provenance: dict[str, dict]

    def metadata(self) -> dict:
        timing_ms = {
            key: value.get("elapsed_ms")
            for key, value in self.provenance.items()
            if value.get("elapsed_ms") is not None
        }
        return {
            "symbol": self.symbol,
            "observed_at": self.observed_at,
            "completion_policy_id": MARKET_COMPLETION_POLICY_ID,
            "timeframes": self.provenance,
            "timing_ms": timing_ms,
            "timing_total_ms": round(sum(timing_ms.values()), 3) if timing_ms else 0.0,
        }


def get_index_symbols(index_name: str = "NIFTY 50") -> list[str]:
    df = get_index_constituents_live_snapshot(index=index_name)
    return df["symbol"].tolist()


def _fetch_history(symbol, interval, lookback_days, observed_at):
    # The project-owned client accepts timezone-aware datetimes and converts the actual
    # instant to epoch seconds, so this remains correct regardless of the host timezone.
    end = observed_at
    start = end - timedelta(days=lookback_days)
    result = get_stock_historical_data(
        symbol,
        start_datetime=start,
        end_datetime=end,
        interval=interval,
    )
    # Only fires on an actual cache miss (fetch_fn only runs then).
    time.sleep(NSE_CALL_DELAY_SECONDS)
    return result


def _market_phase(observed_at):
    if is_market_hours(observed_at):
        return "live"
    if observed_at.weekday() < 5 and observed_at.time() > MARKET_CLOSE:
        market_close = observed_at.replace(
            hour=MARKET_CLOSE.hour,
            minute=MARKET_CLOSE.minute,
            second=0,
            microsecond=0,
        )
        if observed_at < market_close + timedelta(
            minutes=NSE_MARKET_DATA_GRACE_MINUTES
        ):
            return "closing"
    if observed_at.weekday() < 5 and observed_at.time() < MARKET_OPEN:
        return "preopen"
    return "closed"


def _cache_key_and_ttl(symbol, interval, observed_at):
    phase = _market_phase(observed_at)
    date_key = f"{observed_at:%Y%m%d}"
    if phase == "closed":
        return (
            f"{_CACHE_NAMESPACE}_{symbol}_{interval}_{date_key}_closed",
            24 * 3600,
        )
    return (
        f"{_CACHE_NAMESPACE}_{symbol}_{interval}_{date_key}_{phase}",
        INTRADAY_CACHE_TTL_MINUTES * 60,
    )


def _completed_history(history, interval, observed_at, phase):
    result = history.copy()
    original_count = len(result)
    if "datetime" not in result.columns:
        return result.iloc[0:0], original_count

    result["datetime"] = pd.to_datetime(result["datetime"], errors="coerce")
    result = result[result["datetime"].notna()].copy()

    if phase in {"live", "closing"}:
        today = observed_at.date()
        row_dates = result["datetime"].dt.date
        if interval == "D":
            result = result[row_dates < today].copy()
        else:
            today_rows = result.index[row_dates == today]
            if len(today_rows):
                result = result.drop(today_rows[-1]).copy()

    return result.reset_index(drop=True), original_count - len(result)


def _normalize_ohlcv(history: pd.DataFrame) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
    result = history.copy()
    result["datetime"] = pd.to_datetime(result["datetime"], errors="coerce")
    result = result[result["datetime"].notna()].copy()
    for column in ("open", "high", "low", "close", "volume"):
        if column not in result.columns:
            result[column] = pd.NA
    return (
        result[["datetime", "open", "high", "low", "close", "volume"]]
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def resample_intraday(history: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate completed 5m bars into higher intraday frames on the NSE session grid."""
    if minutes not in _DERIVED_INTRADAY:
        raise ValueError(f"unsupported derived interval: {minutes}")
    source = _normalize_ohlcv(history)
    if source.empty:
        return source.copy()

    indexed = source.set_index("datetime")
    # Align buckets to 09:15 IST session open (left-labeled, left-closed).
    aggregated = (
        indexed.resample(
            f"{minutes}min",
            label="left",
            closed="left",
            origin=_SESSION_ORIGIN,
        )
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
    )
    return aggregated.reset_index()


def _load_fetched_interval(
    symbol: str,
    interval,
    observed_at,
    phase: str,
) -> tuple[pd.DataFrame, dict, float]:
    lookback_days = _FETCH_LOOKBACK_DAYS[interval]
    key, ttl = _cache_key_and_ttl(symbol, interval, observed_at)
    started = time.perf_counter()

    def fetch_fn(interval=interval, lookback_days=lookback_days):
        return _fetch_history(
            symbol,
            interval,
            lookback_days,
            observed_at,
        ).to_json(orient="split", date_format="iso")

    cached_history = cache.cached_entry(key, ttl, fetch_fn)
    history = pd.read_json(io.StringIO(cached_history.data), orient="split")
    completed, dropped = _completed_history(history, interval, observed_at, phase)
    elapsed_ms = (time.perf_counter() - started) * 1000
    provenance = {
        "source": "NSE chart API",
        "market_phase": phase,
        "cache_key": key,
        "cache_ttl_seconds": ttl,
        "cache_hit": cached_history.cache_hit,
        "fetched_at": datetime.fromtimestamp(
            cached_history.fetched_at, tz=observed_at.tzinfo
        ).isoformat(),
        "bars": len(completed),
        "dropped_incomplete_bars": dropped,
        "latest_complete_bar": (
            completed["datetime"].iloc[-1].isoformat() if len(completed) else None
        ),
        "elapsed_ms": round(elapsed_ms, 3),
        "lookback_days": lookback_days,
    }
    return completed, provenance, elapsed_ms


def _derived_provenance(
    *,
    minutes: int,
    phase: str,
    parent: dict,
    bars: int,
    resample_ms: float,
) -> dict:
    return {
        "source": "derived_from_5m",
        "derived_from": "5",
        "market_phase": phase,
        "cache_key": parent.get("cache_key"),
        "cache_ttl_seconds": parent.get("cache_ttl_seconds"),
        "cache_hit": parent.get("cache_hit"),
        "fetched_at": parent.get("fetched_at"),
        "bars": bars,
        "dropped_incomplete_bars": 0,
        "latest_complete_bar": None,
        "elapsed_ms": round(resample_ms, 3),
        "parent_bars": parent.get("bars"),
        "interval_minutes": minutes,
    }


def get_market_snapshot(
    symbol: str,
    timeframes: tuple | None = None,
) -> MarketSnapshot:
    observed_at = now_ist()
    phase = _market_phase(observed_at)
    histories: dict[str, pd.DataFrame] = {}
    provenance: dict[str, dict] = {}
    selected = tuple(timeframes) if timeframes is not None else _DEFAULT_TIMEFRAMES
    selected_keys = {str(item) for item in selected}

    need_derived = any(str(item) in selected_keys for item in ("15", "30"))
    fetch_intervals = []
    if "D" in selected_keys or "D" in {str(item) for item in selected}:
        fetch_intervals.append("D")
    # Integer 5 or string "5"
    if need_derived or 5 in selected or "5" in selected_keys:
        fetch_intervals.append(5)

    def _store_fetched(interval, completed, meta):
        histories[str(interval)] = completed
        provenance[str(interval)] = meta
        if str(interval) == "5" and len(completed):
            provenance["5"]["latest_complete_bar"] = (
                completed["datetime"].iloc[-1].isoformat()
            )

    # Overlap D and 5m cache-miss pacing within one symbol; batch stays serial.
    if len(fetch_intervals) <= 1:
        for interval in fetch_intervals:
            completed, meta, _elapsed = _load_fetched_interval(
                symbol, interval, observed_at, phase
            )
            _store_fetched(interval, completed, meta)
    else:
        try:
            with ThreadPoolExecutor(max_workers=len(fetch_intervals)) as pool:
                futures = {
                    pool.submit(
                        _load_fetched_interval,
                        symbol,
                        interval,
                        observed_at,
                        phase,
                    ): interval
                    for interval in fetch_intervals
                }
                for future in as_completed(futures):
                    interval = futures[future]
                    completed, meta, _elapsed = future.result()
                    _store_fetched(interval, completed, meta)
        except Exception:
            # Clear any partial parallel results, then retry strictly sequentially.
            histories.clear()
            provenance.clear()
            log.warning(
                "parallel D/5 fetch failed for %s; falling back to sequential",
                symbol,
                exc_info=True,
            )
            for interval in fetch_intervals:
                completed, meta, _elapsed = _load_fetched_interval(
                    symbol, interval, observed_at, phase
                )
                _store_fetched(interval, completed, meta)

    if need_derived:
        five_minute = histories.get("5")
        if five_minute is None:
            five_minute = pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )
            histories["5"] = five_minute
            provenance["5"] = {
                "source": "NSE chart API",
                "market_phase": phase,
                "cache_hit": False,
                "bars": 0,
                "dropped_incomplete_bars": 0,
                "elapsed_ms": 0.0,
            }
        parent = provenance["5"]
        for minutes in _DERIVED_INTRADAY:
            if str(minutes) not in selected_keys and minutes not in selected:
                continue
            started = time.perf_counter()
            derived = resample_intraday(five_minute, minutes)
            resample_ms = (time.perf_counter() - started) * 1000
            histories[str(minutes)] = derived
            meta = _derived_provenance(
                minutes=minutes,
                phase=phase,
                parent=parent,
                bars=len(derived),
                resample_ms=resample_ms,
            )
            if len(derived):
                meta["latest_complete_bar"] = (
                    derived["datetime"].iloc[-1].isoformat()
                )
            provenance[str(minutes)] = meta

    # Only return requested timeframes (benchmark callers pass ("D",)).
    histories = {
        key: value
        for key, value in histories.items()
        if key in selected_keys or key in {str(item) for item in selected}
    }
    provenance = {key: provenance[key] for key in histories}

    return MarketSnapshot(
        symbol=symbol,
        observed_at=observed_at.isoformat(),
        histories=histories,
        provenance=provenance,
    )


def get_multi_timeframe_history(symbol: str) -> dict:
    """Compatibility wrapper; new callers should retain the snapshot metadata."""
    return get_market_snapshot(symbol).histories
