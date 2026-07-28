from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    """Current time in IST, correct regardless of the host machine's own system timezone."""
    return datetime.now(IST)


def now_ist_naive() -> datetime:
    """now_ist() with tzinfo stripped -- for handing to nsemine, which expects naive
    datetimes representing IST wall-clock time (it has no timezone concept of its own)."""
    return now_ist().replace(tzinfo=None)


def is_market_hours(dt: datetime = None) -> bool:
    """NSE regular trading session: 9:15-15:30 IST, Monday-Friday.

    Doesn't account for exchange holidays -- see nsemine.nse.get_holiday_lists if that's
    ever needed.
    """
    dt = dt if dt is not None else now_ist()
    if dt.weekday() >= 5:
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE
