import io
import os
from datetime import datetime, timedelta

import pandas as pd
from nsemine.historical import get_stock_historical_data
from nsemine.live import get_index_constituents_live_snapshot

import cache

INTRADAY_CACHE_TTL_MINUTES = float(os.environ.get("INTRADAY_CACHE_TTL_MINUTES", "5"))

# interval -> lookback days, tuned for enough bars to warm up RSI14/MACD(12,26,9) at each granularity
# without pulling excessive intraday rows.
_TIMEFRAME_LOOKBACK_DAYS = {
    "D": 150,  # ~100 trading sessions -- matches the original daily-only fetch
    30: 15,
    15: 7,
    5: 3,
}


def get_index_symbols(index_name: str = "NIFTY 50") -> list[str]:
    # nsemine's own docstring example calls this index_name=, but the real keyword is `index`.
    df = get_index_constituents_live_snapshot(index=index_name)
    return df["symbol"].tolist()


def _fetch_history(symbol, interval, lookback_days):
    start = datetime.now() - timedelta(days=lookback_days)
    return get_stock_historical_data(symbol, start_datetime=start, interval=interval)


def _cache_key_and_ttl(symbol, interval):
    if interval == "D":
        # date-in-key so it naturally invalidates at midnight regardless of TTL
        return f"hist_{symbol}_D_{datetime.now():%Y%m%d}", 24 * 3600
    return f"hist_{symbol}_{interval}", INTRADAY_CACHE_TTL_MINUTES * 60


def get_multi_timeframe_history(symbol: str) -> dict:
    result = {}
    for interval, lookback_days in _TIMEFRAME_LOOKBACK_DAYS.items():
        key, ttl = _cache_key_and_ttl(symbol, interval)

        def fetch_fn(interval=interval, lookback_days=lookback_days):
            return _fetch_history(symbol, interval, lookback_days).to_json(orient="split", date_format="iso")

        raw = cache.cached(key, ttl, fetch_fn)
        result[str(interval)] = pd.read_json(io.StringIO(raw), orient="split")
    return result
