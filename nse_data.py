import io
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

import cache
from market_time import MARKET_CLOSE, MARKET_OPEN, is_market_hours, now_ist
from nse_client import (
    get_index_constituents_live_snapshot,
    get_stock_historical_data,
)

INTRADAY_CACHE_TTL_MINUTES = float(os.environ.get("INTRADAY_CACHE_TTL_MINUTES", "5"))
NSE_CALL_DELAY_SECONDS = float(os.environ.get("NSE_CALL_DELAY_SECONDS", "0.3"))
NSE_MARKET_DATA_GRACE_MINUTES = float(os.environ.get("NSE_MARKET_DATA_GRACE_MINUTES", "15"))

# interval -> lookback days, tuned for enough bars to warm up RSI14/MACD(12,26,9) at each granularity
# without pulling excessive intraday rows.
_TIMEFRAME_LOOKBACK_DAYS = {
    "D": 150,  # ~100 trading sessions -- matches the original daily-only fetch
    30: 15,
    15: 7,
    5: 3,
}
MARKET_COMPLETION_POLICY_ID = "nse-completed-bars-v1"


@dataclass(frozen=True)
class MarketSnapshot:
    """Scan-ready completed candles plus enough metadata to audit their freshness."""

    symbol: str
    observed_at: str
    histories: dict[str, pd.DataFrame]
    provenance: dict[str, dict]

    def metadata(self) -> dict:
        return {
            "symbol": self.symbol,
            "observed_at": self.observed_at,
            "completion_policy_id": MARKET_COMPLETION_POLICY_ID,
            "timeframes": self.provenance,
        }


def get_index_symbols(index_name: str = "NIFTY 50") -> list[str]:
    df = get_index_constituents_live_snapshot(index=index_name)
    return df["symbol"].tolist()


def _fetch_history(symbol, interval, lookback_days, observed_at):
    # The project-owned client accepts timezone-aware datetimes and converts the actual
    # instant to epoch seconds, so this remains correct regardless of the host timezone.
    end = observed_at
    start = end - timedelta(days=lookback_days)
    result = get_stock_historical_data(symbol, start_datetime=start, end_datetime=end, interval=interval)
    # Only fires on an actual cache miss (fetch_fn only runs then) -- was previously the
    # only unthrottled NSE call in the whole pipeline, opening every symbol with a burst
    # of up to 4 back-to-back requests (fundamentals staggers, this didn't).
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
        if observed_at < market_close + timedelta(minutes=NSE_MARKET_DATA_GRACE_MINUTES):
            return "closing"
    if observed_at.weekday() < 5 and observed_at.time() < MARKET_OPEN:
        return "preopen"
    return "closed"


def _cache_key_and_ttl(symbol, interval, observed_at):
    phase = _market_phase(observed_at)
    date_key = f"{observed_at:%Y%m%d}"
    if phase == "closed":
        return f"hist_v2_{symbol}_{interval}_{date_key}_closed", 24 * 3600
    return (
        f"hist_v2_{symbol}_{interval}_{date_key}_{phase}",
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


def get_market_snapshot(
    symbol: str,
    timeframes: tuple | None = None,
) -> MarketSnapshot:
    observed_at = now_ist()
    phase = _market_phase(observed_at)
    histories = {}
    provenance = {}
    selected_timeframes = timeframes or tuple(_TIMEFRAME_LOOKBACK_DAYS)
    for interval in selected_timeframes:
        lookback_days = _TIMEFRAME_LOOKBACK_DAYS[interval]
        key, ttl = _cache_key_and_ttl(symbol, interval, observed_at)

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
        interval_key = str(interval)
        histories[interval_key] = completed
        provenance[interval_key] = {
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
        }

    return MarketSnapshot(
        symbol=symbol,
        observed_at=observed_at.isoformat(),
        histories=histories,
        provenance=provenance,
    )


def get_multi_timeframe_history(symbol: str) -> dict:
    """Compatibility wrapper; new callers should retain the snapshot metadata."""
    return get_market_snapshot(symbol).histories
