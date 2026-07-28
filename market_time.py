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
    cache-key dates) where the value is only ever strftime'd, never converted back to an
    epoch. NOT safe to hand to nsemine.get_stock_historical_data (see now_host_local)."""
    return now_ist().replace(tzinfo=None)


def now_host_local() -> datetime:
    """The real current instant, expressed as a naive datetime in the HOST's own local
    timezone -- equivalent to plain datetime.now(), spelled out explicitly.

    Use this (not now_ist_naive()) for anything that gets passed to nsemine's
    get_stock_historical_data: it converts datetimes to an epoch via .timestamp(),
    which interprets a naive datetime as host-local time. Feeding it now_ist_naive()'s
    IST-wall-clock numbers on a non-IST host makes it misinterpret them as host-local,
    producing a wrong epoch -- confirmed via nsemine's source (historical.py calls
    `int(start_datetime.timestamp())`). now_ist().astimezone() converts the correct
    absolute instant to whatever the host's local timezone actually is, which is exactly
    what .timestamp() needs to round-trip correctly regardless of host timezone.
    """
    return now_ist().astimezone().replace(tzinfo=None)


def is_market_hours(dt: datetime = None) -> bool:
    """NSE regular trading session: 9:15-15:30 IST, Monday-Friday.

    Doesn't account for exchange holidays -- see nsemine.nse.get_holiday_lists if that's
    ever needed.
    """
    dt = dt if dt is not None else now_ist()
    if dt.weekday() >= 5:
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE
