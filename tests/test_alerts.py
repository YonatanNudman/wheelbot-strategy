"""Tests for Discord alert formatters (Robinhood-style)."""

import pytest

from engine.alerts import (
    breakeven,
    cushion_pct,
    format_entry_alert,
    format_position_line,
    format_snapshot,
)


class TestCushion:
    def test_put_otm_stock_above_strike(self):
        # Short put: stock $29.50, strike $27 → 8.47% cushion (stock can drop 8.47% before assignment)
        assert cushion_pct(stock_price=29.50, strike=27.0, right="P") == pytest.approx(8.47, abs=0.01)

    def test_put_itm_stock_below_strike_returns_negative(self):
        # Stock $21, strike $23 put → ITM, cushion is negative
        assert cushion_pct(stock_price=21.0, strike=23.0, right="P") < 0

    def test_call_otm_stock_below_strike(self):
        # Short call: stock $100, strike $110 → 10% cushion (stock can rise 10% before assignment)
        assert cushion_pct(stock_price=100.0, strike=110.0, right="C") == pytest.approx(10.0, abs=0.01)


class TestBreakeven:
    def test_short_put(self):
        # Short put breakeven = strike - credit (stock can fall to this before we lose)
        assert breakeven(strike=27.0, credit=0.75, right="P") == pytest.approx(26.25)

    def test_short_call(self):
        # Short call breakeven = strike + credit
        assert breakeven(strike=110.0, credit=4.57, right="C") == pytest.approx(114.57)


class TestEntryAlert:
    def test_csp_contains_all_key_fields(self):
        msg = format_entry_alert(
            symbol="CCL", right="P", strike=27.0, contracts=1,
            credit_per_contract=0.75, expiration="2026-05-22", dte=35,
            stock_price=29.50, profit_target=0.38,
            portfolio_value=99787.40, positions_open=8, max_positions=8,
        )
        # Action + symbol + strike
        assert "SOLD" in msg
        assert "CCL" in msg
        assert "$27" in msg
        # Credit total = 0.75 * 1 * 100 = $75
        assert "$75" in msg
        # Days to expiration + actual date
        assert "35d" in msg
        assert "2026-05-22" in msg
        # Stock price, breakeven, cushion
        assert "$29.50" in msg
        assert "$26.25" in msg  # BE = 27 - 0.75
        assert "8.5%" in msg
        # Auto-close limit
        assert "$0.38" in msg
        # Portfolio footer
        assert "$99,787" in msg
        assert "8/8" in msg

    def test_multi_contract_credit_scales(self):
        msg = format_entry_alert(
            symbol="SOFI", right="P", strike=15.0, contracts=5,
            credit_per_contract=0.75, expiration="2026-05-22", dte=30,
            stock_price=16.40, profit_target=0.38,
            portfolio_value=100000.0, positions_open=1, max_positions=8,
        )
        # Total credit = 0.75 * 5 * 100 = $375
        assert "$375" in msg
        assert "× 5" in msg or "x 5" in msg or "x5" in msg


class TestPositionLine:
    def _base(self, **overrides):
        d = dict(
            symbol="SOFI", right="P", strike=15.0, contracts=1,
            avg_entry_price=0.75, current_price=0.39, stock_price=16.40,
            dte=28,
        )
        d.update(overrides)
        return d

    def test_winning_otm_put_is_green_no_itm(self):
        line = format_position_line(**self._base())
        # P/L = (0.75 - 0.39) * 100 = +$36, pct of max = 36/75 = 48%
        assert "🟢" in line
        assert "SOFI" in line
        assert "+$36" in line
        assert "+48%" in line
        assert "ITM" not in line
        assert "⚠️" not in line

    def test_losing_itm_put_shows_warning(self):
        line = format_position_line(**self._base(
            symbol="DKNG", strike=21.5, stock_price=21.10,
            avg_entry_price=0.77, current_price=1.58, dte=31,
        ))
        # P/L = (0.77 - 1.58) * 100 = -$81, pct of max = -81/77 = -105%
        assert "🔴" in line
        assert "DKNG" in line
        assert "-$81" in line
        assert "-105%" in line
        assert "⚠️" in line or "ITM" in line

    def test_flat_position_is_white(self):
        line = format_position_line(**self._base(
            symbol="F", strike=12.0, stock_price=12.30,
            avg_entry_price=0.29, current_price=0.28, dte=28,
        ))
        # P/L = +$1, pct = 3% (within ±5% band → white)
        assert "⚪" in line
        assert "+$1" in line

    def test_covered_call_itm_when_stock_above_strike(self):
        # Short call is ITM when stock > strike (reversed from put)
        line = format_position_line(**self._base(
            symbol="AVGO", right="C", strike=400.0, stock_price=410.0,
            avg_entry_price=4.57, current_price=12.00, contracts=10, dte=45,
        ))
        assert "⚠️" in line or "ITM" in line
        # P/L = (4.57 - 12.00) * 10 * 100 = -$7,430
        assert "-$7,430" in line


class TestSnapshot:
    def test_full_snapshot_aggregates_positions_and_footer(self):
        positions = [
            dict(symbol="SOFI", right="P", strike=15.0, contracts=1,
                 avg_entry_price=0.75, current_price=0.39, stock_price=16.40, dte=28),
            dict(symbol="DKNG", right="P", strike=21.5, contracts=1,
                 avg_entry_price=0.77, current_price=1.58, stock_price=21.10, dte=31),
            dict(symbol="F", right="P", strike=12.0, contracts=1,
                 avg_entry_price=0.29, current_price=0.28, stock_price=12.30, dte=28),
        ]
        msg = format_snapshot(
            positions=positions,
            portfolio_value=99787.40,
            today_change=-197.02,
            time_label="10:15 AM ET",
        )
        # Header
        assert "📊" in msg
        assert "10:15 AM ET" in msg
        # All three symbols present
        for sym in ("SOFI", "DKNG", "F"):
            assert sym in msg
        # ITM warning only on DKNG
        assert "⚠️" in msg
        # Total P/L = +$36 - $81 + $1 = -$44
        assert "-$44" in msg
        # Portfolio footer
        assert "$99,787" in msg
        assert "-$197" in msg
        # Count of positions
        assert "3 positions" in msg or "3 open" in msg or "3 pos" in msg

    def test_empty_snapshot(self):
        msg = format_snapshot(
            positions=[], portfolio_value=100000.0, today_change=0.0, time_label="10:00 AM ET",
        )
        assert "No open positions" in msg or "0 positions" in msg
        assert "$100,000" in msg
