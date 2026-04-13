"""Trading calendar, earnings dates, and market data utilities."""

from datetime import datetime, timedelta
from typing import Optional

import requests

from utils.logger import get_logger
from utils.timing import ET, now_et

log = get_logger(__name__)


def get_next_earnings_date(symbol: str) -> Optional[datetime]:
    """Fetch the next earnings date for a symbol using Yahoo Finance.

    Returns None if no upcoming earnings found or on API failure.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        headers = {"User-Agent": "Mozilla/5.0"}
        params = {"range": "1d", "interval": "1d", "events": "earnings"}
        resp = requests.get(url, headers=headers, params=params, timeout=10)

        if resp.status_code != 200:
            log.warning("Failed to fetch earnings for %s: HTTP %d", symbol, resp.status_code)
            return None

        data = resp.json()
        events = data.get("chart", {}).get("result", [{}])[0].get("events", {})
        earnings = events.get("earnings", {})

        if not earnings:
            return None

        # Find the next earnings date after today
        today = now_et().date()
        future_dates = []
        for entry in earnings.values():
            dt = datetime.fromtimestamp(entry["date"], tz=ET)
            if dt.date() >= today:
                future_dates.append(dt)

        return min(future_dates) if future_dates else None

    except Exception as e:
        log.warning("Error fetching earnings for %s: %s", symbol, e)
        return None


def days_until_earnings(symbol: str) -> Optional[int]:
    """Days until next earnings. Returns None if unknown."""
    earnings_date = get_next_earnings_date(symbol)
    if earnings_date is None:
        return None
    return (earnings_date.date() - now_et().date()).days


def has_earnings_within(symbol: str, days: int, allow_on_failure: bool = False) -> bool:
    """Check if a symbol has earnings within N days.

    Args:
        symbol: Stock ticker.
        days: Number of days to look ahead for earnings.
        allow_on_failure: If True, returns False (allow trade) when earnings
            data is unavailable.  If False (default), returns True (block
            trade) as the conservative choice.  Use True only in the initial
            scan where other safety rails (stop loss, circuit breaker) exist.
            Use False for exit-engine earnings checks where you're protecting
            an existing position from overnight gap risk.

    Returns:
        True if earnings are within *days* (or data unavailable and
        conservative mode is active), False otherwise.
    """
    d = days_until_earnings(symbol)
    if d is None:
        if allow_on_failure:
            log.warning("Earnings data unavailable for %s — allowing trade per config", symbol)
            return False
        else:
            log.warning("Earnings data unavailable for %s — blocking trade (conservative)", symbol)
            return True  # Conservative default
    return d <= days


def dte(expiration_date: str) -> int:
    """Calculate days to expiration from 'YYYY-MM-DD' string."""
    exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
    return (exp - now_et().date()).days


def next_monthly_expiration(from_date: Optional[datetime] = None) -> str:
    """Find the 3rd Friday of the next month (standard monthly options expiration)."""
    dt = from_date or now_et()
    # Move to next month
    if dt.month == 12:
        target_year, target_month = dt.year + 1, 1
    else:
        target_year, target_month = dt.year, dt.month + 1

    return _third_friday(target_year, target_month)


def next_weekly_expiration(from_date: Optional[datetime] = None) -> str:
    """Find the next Friday from the given date."""
    dt = from_date or now_et()
    days_until_friday = (4 - dt.weekday()) % 7
    if days_until_friday == 0:
        days_until_friday = 7  # Next Friday, not today
    friday = dt + timedelta(days=days_until_friday)
    return friday.strftime("%Y-%m-%d")


def _third_friday(year: int, month: int) -> str:
    """Find the third Friday of a given month."""
    # First day of the month
    first = datetime(year, month, 1)
    # Days until first Friday
    days_to_friday = (4 - first.weekday()) % 7
    first_friday = first + timedelta(days=days_to_friday)
    # Third Friday
    third_friday = first_friday + timedelta(weeks=2)
    return third_friday.strftime("%Y-%m-%d")
