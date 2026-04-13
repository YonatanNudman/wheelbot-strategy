"""Abstract base class for WheelBot trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from utils.logger import get_logger

if TYPE_CHECKING:
    from data.models import Signal

log = get_logger(__name__)


class BaseStrategy(ABC):
    """Contract that every strategy (PMCC, Wheel, etc.) must implement.

    Each method receives a broker adapter and returns a list of Signal
    objects that the engine can route through the approval pipeline.
    """

    @abstractmethod
    def scan_for_entries(self, broker: object, universe: list[str]) -> list[Signal]:
        """Scan the universe for new entry opportunities.

        Args:
            broker: Broker adapter with market-data and account methods.
            universe: List of ticker symbols to evaluate.

        Returns:
            Signals for positions the strategy wants to open.
        """
        ...

    @abstractmethod
    def check_exits(self, broker: object, positions: list[object]) -> list[Signal]:
        """Evaluate open positions for exit or roll conditions.

        Args:
            broker: Broker adapter for live quotes and greeks.
            positions: Currently open Position objects for this strategy.

        Returns:
            Signals for positions that should be closed, rolled, or adjusted.
        """
        ...

    @abstractmethod
    def handle_assignment(self, position: object) -> list[Signal]:
        """React to an option assignment (shares delivered or called away).

        Args:
            position: The Position that was assigned.

        Returns:
            Follow-up signals (e.g. sell covered call after CSP assignment).
        """
        ...
