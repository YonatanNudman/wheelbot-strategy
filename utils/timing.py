"""ET timezone utilities, market hours, and optimal execution windows."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from utils.config import get

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# US market holidays for 2026 (NYSE/NASDAQ closed)
MARKET_HOLIDAYS_2026 = {
    datetime(2026, 1, 1).date(),    # New Year's Day
    datetime(2026, 1, 19).date(),   # MLK Day
    datetime(2026, 2, 16).date(),   # Presidents' Day
    datetime(2026, 4, 3).date(),    # Good Friday
    datetime(2026, 5, 25).date(),   # Memorial Day
    datetime(2026, 7, 3).date(),    # Independence Day (observed)
    datetime(2026, 9, 7).date(),    # Labor Day
    datetime(2026, 11, 26).date(),  # Thanksgiving
    datetime(2026, 12, 25).date(),  # Christmas
}


def now_et() -> datetime:
    """Current time in US Eastern."""
    return datetime.now(ET)


def is_market_open() -> bool:
    """Check if the US stock market is currently open."""
    current = now_et()

    # Weekend check
    if current.weekday() >= 5:
        return False

    # Holiday check
    if current.date() in MARKET_HOLIDAYS_2026:
        return False

    # Hours check
    current_time = current.time()
    return MARKET_OPEN <= current_time < MARKET_CLOSE


def is_trading_day(dt: datetime | None = None) -> bool:
    """Check if the given date is a trading day (weekday + not holiday)."""
    if dt is None:
        dt = now_et()
    return dt.weekday() < 5 and dt.date() not in MARKET_HOLIDAYS_2026


def in_optimal_entry_window() -> bool:
    """Check if we're in the optimal entry window (10:00-11:00 AM ET)."""
    current = now_et().time()
    start = _parse_time(get("scheduling.optimal_entry_start", "10:00"))
    end = _parse_time(get("scheduling.optimal_entry_end", "11:00"))
    return start <= current < end


def in_roll_window() -> bool:
    """Check if we're in the optimal roll window (10:00 AM - 2:00 PM ET)."""
    current = now_et().time()
    start = _parse_time(get("scheduling.roll_window_start", "10:00"))
    end = _parse_time(get("scheduling.roll_window_end", "14:00"))
    return start <= current < end


def past_entry_cutoff() -> bool:
    """Check if we're past the no-new-entries cutoff (3:45 PM ET)."""
    current = now_et().time()
    cutoff = _parse_time(get("scheduling.no_entry_after", "15:45"))
    return current >= cutoff


def time_until_market_open() -> timedelta | None:
    """Time until next market open. Returns None if market is currently open."""
    if is_market_open():
        return None

    current = now_et()
    # Find the next trading day
    target = current
    while True:
        if target.date() == current.date() and current.time() < MARKET_OPEN:
            # Today, before open
            break
        target += timedelta(days=1)
        if is_trading_day(target):
            break

    open_dt = datetime.combine(target.date(), MARKET_OPEN, tzinfo=ET)
    return open_dt - current


def format_et(dt: datetime) -> str:
    """Format a datetime in ET for display."""
    et_dt = dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)
    return et_dt.strftime("%I:%M %p ET on %b %d, %Y")


def _parse_time(time_str: str) -> time:
    """Parse 'HH:MM' string to time object."""
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))
