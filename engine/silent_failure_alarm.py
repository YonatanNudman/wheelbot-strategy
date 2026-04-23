"""Silent-failure alarm — detects N trading days of zero fills.

The bot can run "successfully" (container up, logs rolling, Discord connected)
while quietly failing to place any real orders — e.g., due to a paper/live
URL mismatch, expired credentials, or an egress-proxy block. This module is the
canary: if `trading_days_threshold` trading days have elapsed during market
hours with zero fills, fire an alarm.

Pure logic, no side effects — the scheduler wires this into a Discord webhook.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from utils.timing import MARKET_CLOSE, MARKET_OPEN, is_market_open, is_trading_day

ET = ZoneInfo("America/New_York")


def trading_days_between(start: datetime, end: datetime) -> int:
    """Count trading days strictly between start and end (exclusive of start, inclusive of end).

    Returns 0 if start >= end. Weekends and US holidays are skipped.
    """
    if end <= start:
        return 0

    count = 0
    # Walk forward one day at a time from start.date() + 1
    current = start.date() + timedelta(days=1)
    end_date = end.date()
    while current <= end_date:
        # is_trading_day expects a datetime, not date
        probe = datetime.combine(current, MARKET_OPEN, tzinfo=ET)
        if is_trading_day(probe):
            count += 1
        current += timedelta(days=1)
    return count


def should_alarm(
    *,
    now: datetime,
    last_fill_at: Optional[datetime],
    bot_started_at: Optional[datetime] = None,
    trading_days_threshold: int = 2,
) -> bool:
    """Decide whether to fire the no-fills alarm.

    Args:
        now: Current time (ET, tz-aware).
        last_fill_at: Timestamp of the most recent successful fill, or None if
            no fills have ever been recorded in this deployment.
        bot_started_at: When this container / process started. Used as a
            fallback reference when there are no fills. Without it, we can't
            distinguish "bot just started, no trades yet" (quiet correct) from
            "bot has been running for weeks with no trades" (alarm).
        trading_days_threshold: Number of trading days of silence before
            alarming. Default 2 (catches ~1-1.5 calendar days of silent failure
            fast enough to matter, without false-positives from normal weekends).

    Returns:
        True iff the alarm should fire.
    """
    # Only alarm during market hours — no point in pinging the user at 3 AM
    # when the bot has been legitimately idle overnight.
    if not _is_market_hours(now):
        return False

    reference = last_fill_at if last_fill_at is not None else bot_started_at
    if reference is None:
        # No fills ever AND no known start time — can't evaluate.
        return False

    return trading_days_between(reference, now) >= trading_days_threshold


def _is_market_hours(dt: datetime) -> bool:
    """Check if dt is during US market open hours (9:30 AM - 4:00 PM ET on a trading day)."""
    if not is_trading_day(dt):
        return False
    t = dt.astimezone(ET).time() if dt.tzinfo else dt.time()
    return MARKET_OPEN <= t < MARKET_CLOSE
