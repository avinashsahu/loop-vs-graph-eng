from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    """Current time in IST, correct regardless of the host machine's own system timezone."""
    return datetime.now(IST)


def now_ist_naive() -> datetime:
    """now_ist() with tzinfo stripped -- safe for display/formatting (log timestamps,
    cache-key dates) where the value is only ever strftime'd."""
    return now_ist().replace(tzinfo=None)


def is_market_hours(dt: datetime = None) -> bool:
    """NSE regular trading session: 9:15-15:30 IST, Monday-Friday.

    Exchange holidays are not currently accounted for.
    """
    dt = dt if dt is not None else now_ist()
    if dt.weekday() >= 5:
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE
