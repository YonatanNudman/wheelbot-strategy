"""Capital allocation and position sizing logic."""

from __future__ import annotations

from data import database as db
from utils.config import get
from utils.logger import get_logger

log = get_logger(__name__)


class PositionSizer:
    """Manages capital allocation across positions."""

    def __init__(self):
        self.total_capital = get("capital.total", 5000)
        self.max_per_position_pct = get("capital.max_per_position_pct", 0.50)
        self.reserve_pct = get("capital.reserve_pct", 0.10)
        self.max_open = get("positions.max_open_total", 3)
        self.max_pmcc = get("positions.max_pmcc", 2)
        self.max_wheel = get("positions.max_wheel", 2)

    def can_open_position(self, strategy_prefix: str = "") -> tuple[bool, str]:
        """Check if we can open a new position given current limits.

        Returns (allowed, reason).
        """
        current_count = db.count_open_positions()
        if current_count >= self.max_open:
            return False, f"At position limit ({current_count}/{self.max_open})"

        if strategy_prefix == "pmcc":
            pmcc_count = db.count_open_positions(strategy="pmcc")
            # Count PMCC pairs (LEAPS count, not short calls)
            if pmcc_count >= self.max_pmcc * 2:  # Each pair = 2 positions
                return False, f"At PMCC limit ({pmcc_count // 2}/{self.max_pmcc} pairs)"

        elif strategy_prefix == "wheel":
            wheel_count = db.count_open_positions(strategy="wheel")
            if wheel_count >= self.max_wheel:
                return False, f"At wheel limit ({wheel_count}/{self.max_wheel})"

        return True, "OK"

    def can_afford(self, required_capital: float, available_buying_power: float) -> bool:
        """Check if we can afford a position while maintaining cash reserve."""
        reserve = available_buying_power * self.reserve_pct
        allocatable = available_buying_power - reserve
        max_per_pos = available_buying_power * self.max_per_position_pct
        return required_capital <= min(allocatable, max_per_pos)

    def max_allocatable(self, buying_power: float) -> float:
        """Maximum capital allocatable to a single position."""
        reserve = buying_power * self.reserve_pct
        allocatable = buying_power - reserve
        per_pos_limit = buying_power * self.max_per_position_pct
        return min(allocatable, per_pos_limit)

    def suggest_quantity(self, cost_per_contract: float, buying_power: float) -> int:
        """Suggest how many contracts to trade. Usually 1 at $5K capital."""
        max_alloc = self.max_allocatable(buying_power)
        if cost_per_contract <= 0:
            return 0
        qty = int(max_alloc // cost_per_contract)
        return max(qty, 0)
