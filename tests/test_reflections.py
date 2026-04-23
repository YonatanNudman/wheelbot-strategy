"""Tests for daily reflection + weekly autopsy generation.

Focus: the pure parts — data aggregation, prompt construction, file writing.
OpenAI API calls are mocked (we trust OpenAI works; we don't need to test it).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from ai.reflections import (
    build_daily_prompt,
    build_weekly_prompt,
    write_reflection,
)

ET = ZoneInfo("America/New_York")


# ── Prompt builders ───────────────────────────────────────────────────────────


class TestDailyPrompt:
    def test_includes_date(self):
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        prompt = build_daily_prompt(date=date, fills=[], open_positions=[], account={})
        assert "2026-04-23" in prompt

    def test_flags_no_trades_day(self):
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        prompt = build_daily_prompt(date=date, fills=[], open_positions=[], account={})
        assert "no trades" in prompt.lower() or "0 fills" in prompt.lower()

    def test_lists_fills(self):
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        fills = [
            {"symbol": "SOFI", "side": "sell", "price": 0.52, "contracts": 1, "strategy": "wheel_csp"},
        ]
        prompt = build_daily_prompt(date=date, fills=fills, open_positions=[], account={})
        assert "SOFI" in prompt
        assert "0.52" in prompt

    def test_asks_for_remove_flag_convention(self):
        # The morning scan reads reflections looking for 'remove' flags on bad stocks.
        # The prompt should instruct the model to use that vocabulary.
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        prompt = build_daily_prompt(date=date, fills=[], open_positions=[], account={})
        assert "remove" in prompt.lower() or "flag" in prompt.lower()


class TestWeeklyPrompt:
    def test_includes_week_range(self):
        end = datetime(2026, 4, 26, tzinfo=ET).date()  # Sunday
        prompt = build_weekly_prompt(week_ending=end, fills=[], closed_trades=[], account={})
        assert "2026-04-26" in prompt

    def test_asks_for_param_tuning(self):
        end = datetime(2026, 4, 26, tzinfo=ET).date()
        prompt = build_weekly_prompt(week_ending=end, fills=[], closed_trades=[], account={})
        # Weekly should prompt for parameter suggestions, not just narrative
        assert any(kw in prompt.lower() for kw in ["parameter", "tune", "adjust", "change"])


# ── File writer ───────────────────────────────────────────────────────────────


class TestWriteReflection:
    def test_writes_file_with_correct_name(self, tmp_path: Path):
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        write_reflection(tmp_path, date, "Some reflection content")
        expected = tmp_path / "2026-04-23.md"
        assert expected.exists()

    def test_writes_file_contents(self, tmp_path: Path):
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        content = "# Reflection\n\nToday went fine."
        write_reflection(tmp_path, date, content)
        assert (tmp_path / "2026-04-23.md").read_text() == content

    def test_overwrites_existing_same_day(self, tmp_path: Path):
        # Rerunning reflection on same day should replace, not append
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        write_reflection(tmp_path, date, "First")
        write_reflection(tmp_path, date, "Second")
        assert (tmp_path / "2026-04-23.md").read_text() == "Second"

    def test_creates_directory_if_missing(self, tmp_path: Path):
        nested = tmp_path / "new_dir"
        date = datetime(2026, 4, 23, tzinfo=ET).date()
        write_reflection(nested, date, "Test")
        assert (nested / "2026-04-23.md").exists()
