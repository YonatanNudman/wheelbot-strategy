"""Stock universe management with dynamic capital-based filtering."""

from utils.config import get
from utils.logger import get_logger

logger = get_logger("data.universe")


class StockUniverse:
    """Manages the pool of tickers eligible for Wheel and PMCC strategies.

    Base tickers and PMCC ETFs are loaded from config.yaml on init.
    Runtime additions/removals are tracked separately so a config reload
    never silently drops a ticker the operator added mid-session.
    """

    def __init__(self) -> None:
        self._base_tickers: list[str] = list(get("universe.base_tickers", []))
        self._pmcc_etfs: list[str] = list(get("universe.pmcc_etfs", []))
        self._runtime_additions: set[str] = set()
        self._runtime_removals: set[str] = set()

        logger.info(
            "Universe loaded — %d wheel tickers, %d PMCC ETFs",
            len(self._base_tickers),
            len(self._pmcc_etfs),
        )

    # ------------------------------------------------------------------
    # Candidate filters
    # ------------------------------------------------------------------

    def get_wheel_candidates(self, buying_power: float) -> list[str]:
        """Return base tickers affordable for a cash-secured put.

        A ticker is affordable when *strike * 100 <= buying_power * max_per_position_pct*.
        Because we don't have live strike data here, the caller is expected to
        do the final strike-level check; this method applies the capital ceiling
        so downstream scanners only evaluate tickers within budget.

        For the initial filter we approximate "strike" as the ticker's current
        price (ATM put).  The broker layer resolves the real strike later.
        """
        max_pct: float = get("universe.max_per_position_pct", 0.20)
        max_cost = buying_power * max_pct
        active = self._active_tickers(self._base_tickers)

        candidates = [t for t in active if self._estimated_csp_cost(t) <= max_cost]

        logger.debug(
            "Wheel candidates for $%.0f buying power (%.0f%% cap): %d / %d pass",
            buying_power,
            max_pct * 100,
            len(candidates),
            len(active),
        )
        return candidates

    def get_pmcc_candidates(self) -> list[str]:
        """Return the PMCC ETF list (no capital filter — LEAPS sizing differs)."""
        return list(self._active_tickers(self._pmcc_etfs))

    # ------------------------------------------------------------------
    # Runtime mutations
    # ------------------------------------------------------------------

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the runtime universe."""
        ticker = ticker.upper().strip()
        self._runtime_additions.add(ticker)
        self._runtime_removals.discard(ticker)
        logger.info("Ticker added at runtime: %s", ticker)

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the runtime universe."""
        ticker = ticker.upper().strip()
        self._runtime_removals.add(ticker)
        self._runtime_additions.discard(ticker)
        logger.info("Ticker removed at runtime: %s", ticker)

    def get_all_tickers(self) -> list[str]:
        """Return every unique ticker across both strategies."""
        combined = set(self._base_tickers) | set(self._pmcc_etfs) | self._runtime_additions
        combined -= self._runtime_removals
        return sorted(combined)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _active_tickers(self, base: list[str]) -> list[str]:
        """Apply runtime additions/removals to a base list."""
        pool = set(base) | self._runtime_additions
        pool -= self._runtime_removals
        return sorted(pool)

    @staticmethod
    def _estimated_csp_cost(ticker: str) -> float:
        """Rough cost to secure a CSP (strike * 100).

        Uses the config's price hints when available, otherwise returns 0
        so the ticker is never excluded by the capital filter alone.
        """
        price_hints: dict = get("universe.price_hints", {})
        price = price_hints.get(ticker, 0.0)
        return float(price) * 100
