"""Tests for the silent-failure alarm — detects N trading days of zero fills.

Context: on 2026-04-23 we discovered the bot had been running on Railway with a
paper/live URL mismatch for 10 days. Every Alpaca call 403'd, but the container
stayed up and logs looked clean. This alarm is the canary that would have fired
within 2 trading days and caught the regression.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine.silent_failure_alarm import should_alarm, trading_days_between

ET = ZoneInfo("America/New_York")


# ── trading_days_between: counts trading days in a date range ─────────────────


class TestTradingDaysBetween:
    def test_same_day_returns_zero(self):
        d = datetime(2026, 4, 22, 10, 0, tzinfo=ET)  # Wed
        assert trading_days_between(d, d) == 0

    def test_one_weekday_gap(self):
        # Wed -> Thu = 1 trading day elapsed
        wed = datetime(2026, 4, 22, 10, 0, tzinfo=ET)
        thu = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
        assert trading_days_between(wed, thu) == 1

    def test_weekend_doesnt_count(self):
        # Fri 10am -> Mon 10am = 1 trading day (just Monday)
        fri = datetime(2026, 4, 17, 10, 0, tzinfo=ET)
        mon = datetime(2026, 4, 20, 10, 0, tzinfo=ET)
        assert trading_days_between(fri, mon) == 1

    def test_holiday_skipped(self):
        # Thu 2026-04-02 -> Mon 2026-04-06: Fri 04-03 is Good Friday (holiday)
        thu = datetime(2026, 4, 2, 10, 0, tzinfo=ET)
        mon = datetime(2026, 4, 6, 10, 0, tzinfo=ET)
        # Trading days elapsed after Thu start: Mon = 1
        assert trading_days_between(thu, mon) == 1


# ── should_alarm: the decision function ───────────────────────────────────────


class TestShouldAlarm:
    def test_fires_when_no_fills_for_2_plus_trading_days(self):
        now = datetime(2026, 4, 23, 10, 0, tzinfo=ET)  # Thu
        last_fill = datetime(2026, 4, 20, 10, 0, tzinfo=ET)  # Mon
        # Mon -> Thu = 3 trading days elapsed, threshold=2 → alarm
        assert should_alarm(now=now, last_fill_at=last_fill, trading_days_threshold=2) is True

    def test_silent_when_fill_today(self):
        now = datetime(2026, 4, 23, 14, 0, tzinfo=ET)
        last_fill = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
        assert should_alarm(now=now, last_fill_at=last_fill, trading_days_threshold=2) is False

    def test_silent_when_fill_yesterday(self):
        now = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
        last_fill = datetime(2026, 4, 22, 10, 0, tzinfo=ET)
        # 1 trading day elapsed < threshold 2 → no alarm
        assert should_alarm(now=now, last_fill_at=last_fill, trading_days_threshold=2) is False

    def test_only_alarms_during_market_hours(self):
        # Saturday at 10am - market closed - should NOT alarm even if 10 days silent
        sat = datetime(2026, 4, 25, 10, 0, tzinfo=ET)
        last_fill = datetime(2026, 4, 10, 10, 0, tzinfo=ET)
        assert should_alarm(now=sat, last_fill_at=last_fill, trading_days_threshold=2) is False

    def test_before_market_open_doesnt_alarm(self):
        # Thu 8am - before 9:30 open - should NOT alarm
        early = datetime(2026, 4, 23, 8, 0, tzinfo=ET)
        last_fill = datetime(2026, 4, 10, 10, 0, tzinfo=ET)
        assert should_alarm(now=early, last_fill_at=last_fill, trading_days_threshold=2) is False

    def test_after_market_close_doesnt_alarm(self):
        # Thu 5pm - after 4pm close - should NOT alarm
        late = datetime(2026, 4, 23, 17, 0, tzinfo=ET)
        last_fill = datetime(2026, 4, 10, 10, 0, tzinfo=ET)
        assert should_alarm(now=late, last_fill_at=last_fill, trading_days_threshold=2) is False

    def test_alarms_when_no_fills_ever_recorded(self):
        # last_fill_at=None means no fills ever. If the bot has been running
        # for 2+ trading days with zero fills, that's alarm-worthy.
        now = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
        bot_started = datetime(2026, 4, 20, 9, 30, tzinfo=ET)  # Mon
        assert should_alarm(
            now=now,
            last_fill_at=None,
            bot_started_at=bot_started,
            trading_days_threshold=2,
        ) is True

    def test_doesnt_alarm_when_bot_just_started(self):
        # Bot started 1 trading day ago, no fills yet → not yet alarmable
        now = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
        bot_started = datetime(2026, 4, 22, 9, 30, tzinfo=ET)  # Wed (1 day prior)
        assert should_alarm(
            now=now,
            last_fill_at=None,
            bot_started_at=bot_started,
            trading_days_threshold=2,
        ) is False
