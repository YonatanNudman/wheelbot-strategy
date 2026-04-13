"""PerformanceTracker — calculates and stores aggregate trading statistics."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from data import database as db
from data.models import Performance, PortfolioSnapshot, Position, Strategy
from utils.logger import get_logger
from utils.timing import now_et

if TYPE_CHECKING:
    from broker.robinhood import RobinhoodBroker

log = get_logger(__name__)

# Strategy families for per-strategy stats
_STRATEGY_FAMILIES: dict[str, list[str]] = {
    "pmcc": [Strategy.PMCC_LEAPS.value, Strategy.PMCC_SHORT_CALL.value],
    "wheel": [Strategy.WHEEL_CSP.value, Strategy.WHEEL_CC.value, Strategy.WHEEL_SHARES.value],
}

_PERIODS: dict[str, Optional[int]] = {
    "all_time": None,
    "last_30d": 30,
    "last_7d": 7,
}


class PerformanceTracker:
    """Calculates portfolio snapshots and aggregate trading statistics.

    Call :meth:`take_daily_snapshot` once per trading day and
    :meth:`update_stats` whenever closed-trade counts change.
    """

    def __init__(self, broker: RobinhoodBroker) -> None:
        self.broker = broker

    # ── Daily snapshot ────────────────────────────────────────────────────

    def take_daily_snapshot(self) -> PortfolioSnapshot:
        """Capture today's portfolio state and return the saved snapshot."""
        now = now_et()
        today_str = now.strftime("%Y-%m-%d")

        account = self.broker.get_account_info()
        open_count = db.count_open_positions()

        # Day P&L — difference from yesterday
        yesterday = db.get_latest_snapshot()
        day_pnl = 0.0
        if yesterday and yesterday.total_account_value:
            day_pnl = account.portfolio_value - yesterday.total_account_value

        # All-time P&L — difference from first-ever snapshot
        first = db.get_first_snapshot()
        total_pnl = 0.0
        total_pnl_pct = 0.0
        if first and first.total_account_value:
            total_pnl = account.portfolio_value - first.total_account_value
            total_pnl_pct = (total_pnl / first.total_account_value) * 100

        positions_value = account.portfolio_value - account.cash_balance

        snapshot = PortfolioSnapshot(
            date=today_str,
            total_account_value=account.portfolio_value,
            cash_balance=account.cash_balance,
            positions_value=positions_value,
            open_position_count=open_count,
            day_pnl=round(day_pnl, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
        )
        snapshot.id = db.save_snapshot(snapshot)

        log.info(
            "Daily snapshot saved: value=$%.2f, day_pnl=$%.2f, total_pnl=$%.2f (%.1f%%)",
            snapshot.total_account_value, snapshot.day_pnl,
            snapshot.total_pnl, snapshot.total_pnl_pct,
        )
        return snapshot

    # ── Aggregate stats ───────────────────────────────────────────────────

    def update_stats(self) -> None:
        """Recalculate and save Performance records for all strategy/period combos."""
        strategies = ["overall"] + list(_STRATEGY_FAMILIES.keys())
        now = now_et()

        for strategy_key in strategies:
            for period_key, days in _PERIODS.items():
                trades = self._get_trades_for(strategy_key, days, now)
                perf = self._calculate_performance(trades, strategy_key, period_key)

                # Add Sharpe and drawdown for all_time overall only
                if strategy_key == "overall" and period_key == "all_time":
                    perf.sharpe_ratio = self._calculate_sharpe()
                    perf.max_drawdown = self._calculate_max_drawdown()

                db.save_performance(perf)

        log.info("Performance stats updated for all strategy/period combinations")

    def get_summary(self) -> dict:
        """Return a formatted performance summary suitable for a Discord embed."""
        overall = db.get_performance("overall", "all_time")
        last_30d = db.get_performance("overall", "last_30d")
        last_7d = db.get_performance("overall", "last_7d")
        pmcc = db.get_performance("pmcc", "all_time")
        wheel = db.get_performance("wheel", "all_time")
        snapshot = db.get_latest_snapshot()

        summary: dict = {
            "portfolio": {},
            "overall": {},
            "last_30d": {},
            "last_7d": {},
            "pmcc": {},
            "wheel": {},
        }

        if snapshot:
            summary["portfolio"] = {
                "total_value": f"${snapshot.total_account_value:,.2f}",
                "cash": f"${snapshot.cash_balance:,.2f}",
                "positions_value": f"${snapshot.positions_value:,.2f}",
                "open_positions": snapshot.open_position_count,
                "day_pnl": f"${snapshot.day_pnl:+,.2f}",
                "total_pnl": f"${snapshot.total_pnl:+,.2f} ({snapshot.total_pnl_pct:+.1f}%)",
            }

        if overall:
            summary["overall"] = self._format_perf(overall)
        if last_30d:
            summary["last_30d"] = self._format_perf(last_30d)
        if last_7d:
            summary["last_7d"] = self._format_perf(last_7d)
        if pmcc:
            summary["pmcc"] = self._format_perf(pmcc)
        if wheel:
            summary["wheel"] = self._format_perf(wheel)

        return summary

    # ── Internal calculations ─────────────────────────────────────────────

    def _get_trades_for(
        self, strategy_key: str, days: Optional[int], now: datetime,
    ) -> list[Position]:
        """Fetch closed trades filtered by strategy family and time window."""
        if strategy_key == "overall":
            all_trades = db.get_closed_trades()
        else:
            all_trades = []
            for strat_value in _STRATEGY_FAMILIES.get(strategy_key, []):
                all_trades.extend(db.get_closed_trades(strategy=strat_value))

        if days is None:
            return all_trades

        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        return [t for t in all_trades if t.exit_date and t.exit_date >= cutoff]

    def _calculate_performance(
        self,
        trades: list[Position],
        strategy_key: str,
        period_key: str,
    ) -> Performance:
        """Derive aggregate stats from a list of closed trades."""
        total = len(trades)
        if total == 0:
            return Performance(strategy=strategy_key, period=period_key)

        pnls = [t.pnl_dollars or 0.0 for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        win_rate = (len(winners) / total) * 100 if total else 0.0
        avg_profit = sum(pnls) / total
        max_win = max(pnls) if pnls else 0.0
        max_loss = min(pnls) if pnls else 0.0
        total_premium = sum(
            t.entry_credit_total or (t.entry_price * t.quantity * 100)
            for t in trades
        )

        return Performance(
            strategy=strategy_key,
            period=period_key,
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=round(win_rate, 1),
            avg_profit=round(avg_profit, 2),
            max_win=round(max_win, 2),
            max_loss=round(max_loss, 2),
            total_premium_collected=round(total_premium, 2),
        )

    def _calculate_sharpe(self) -> float:
        """Calculate annualized Sharpe ratio from daily portfolio snapshots.

        Sharpe = mean(daily_returns) / std(daily_returns) * sqrt(252)
        """
        snapshots = self._get_all_snapshots()
        if len(snapshots) < 2:
            return 0.0

        daily_returns: list[float] = []
        for i in range(1, len(snapshots)):
            prev_val = snapshots[i - 1].total_account_value
            curr_val = snapshots[i].total_account_value
            if prev_val and prev_val > 0:
                daily_returns.append((curr_val - prev_val) / prev_val)

        if len(daily_returns) < 2:
            return 0.0

        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_return = math.sqrt(variance)

        if std_return == 0:
            return 0.0

        sharpe = (mean_return / std_return) * math.sqrt(252)
        return round(sharpe, 2)

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown from portfolio snapshots.

        Drawdown = (peak - trough) / peak, expressed as a positive percentage.
        """
        snapshots = self._get_all_snapshots()
        if len(snapshots) < 2:
            return 0.0

        peak = snapshots[0].total_account_value
        max_dd = 0.0

        for snap in snapshots:
            value = snap.total_account_value
            if value > peak:
                peak = value
            if peak > 0:
                dd = (peak - value) / peak
                max_dd = max(max_dd, dd)

        return round(max_dd * 100, 2)

    def _get_all_snapshots(self) -> list[PortfolioSnapshot]:
        """Fetch all portfolio snapshots ordered by date ascending.

        Note: database.py does not expose a bulk-fetch — we query directly.
        """
        from data.database import _connect, _row_to_snapshot

        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY date ASC"
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]

    @staticmethod
    def _format_perf(perf: Performance) -> dict:
        """Format a Performance record for display."""
        return {
            "total_trades": perf.total_trades,
            "win_rate": f"{perf.win_rate:.1f}%",
            "avg_profit": f"${perf.avg_profit:+,.2f}",
            "max_win": f"${perf.max_win:+,.2f}",
            "max_loss": f"${perf.max_loss:+,.2f}",
            "sharpe_ratio": f"{perf.sharpe_ratio:.2f}" if perf.sharpe_ratio else "N/A",
            "max_drawdown": f"{perf.max_drawdown:.1f}%" if perf.max_drawdown else "N/A",
            "premium_collected": f"${perf.total_premium_collected:,.2f}",
        }
