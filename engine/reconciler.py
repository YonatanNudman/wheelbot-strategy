"""PositionReconciler — detects assignments by comparing broker vs DB state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from data import database as db
from data.models import Position, PositionState, Strategy
from utils.logger import get_logger
from utils.timing import now_et

if TYPE_CHECKING:
    from broker.alpaca_broker import AlpacaBroker

log = get_logger(__name__)


class PositionReconciler:
    """Compares live broker positions against the DB to detect assignments,
    expirations, and other out-of-band state changes.

    Run once per day (typically at market open) to catch overnight events
    like option assignments and expirations.
    """

    def __init__(self, broker: AlpacaBroker, db_module: object | None = None) -> None:
        self.broker = broker
        # Accept a db_module for testability; default to the real module.
        self._db = db_module or db

    # ── Public API ────────────────────────────────────────────────────────

    def reconcile(self) -> list[str]:
        """Run a full reconciliation pass.

        Returns:
            Human-readable descriptions of every change detected.
        """
        log.info("Starting position reconciliation")
        changes: list[str] = []

        broker_stocks = self._get_broker_stock_map()
        broker_options = self._get_broker_option_set()
        db_positions = self._db.get_open_positions()

        for pos in db_positions:
            if pos.strategy in (
                Strategy.WHEEL_CSP.value,
                Strategy.PMCC_SHORT_CALL.value,
            ):
                change = self._check_short_option_assignment(pos, broker_options, broker_stocks)
                if change:
                    changes.append(change)

            elif pos.strategy == Strategy.PMCC_LEAPS.value:
                change = self._check_leaps_still_exists(pos, broker_options)
                if change:
                    changes.append(change)

            elif pos.strategy in (Strategy.WHEEL_CC.value,):
                change = self._check_covered_call(pos, broker_options, broker_stocks)
                if change:
                    changes.append(change)

            elif pos.strategy == Strategy.WHEEL_SHARES.value:
                change = self._check_shares_still_held(pos, broker_stocks)
                if change:
                    changes.append(change)

        # VRP spread reconciliation
        vrp_changes = self._reconcile_vrp_spreads(broker_options)
        changes.extend(vrp_changes)

        if changes:
            log.info("Reconciliation found %d change(s):", len(changes))
            for c in changes:
                log.info("  - %s", c)
        else:
            log.info("Reconciliation complete — no discrepancies found")

        return changes

    # ── Detection logic ───────────────────────────────────────────────────

    def _check_short_option_assignment(
        self,
        pos: Position,
        broker_options: set[str],
        broker_stocks: dict[str, float],
    ) -> str | None:
        """Detect CSP / short-call assignment.

        If the option has disappeared from the broker AND shares appeared,
        it was assigned.
        """
        key = self._option_key(pos)
        option_gone = key not in broker_options
        shares_appeared = pos.symbol in broker_stocks and broker_stocks[pos.symbol] >= 100

        if option_gone and shares_appeared:
            log.info("ASSIGNMENT detected: %s %s $%s", pos.symbol, pos.option_type, pos.strike)

            # Close the option position
            self._db.close_position(
                pos.id,
                exit_price=0.0,
                exit_reason="assigned",
            )

            # Create a new SHARES position
            share_price = broker_stocks[pos.symbol] / (broker_stocks[pos.symbol] // 100 * 100)
            # Cost basis = strike - premium collected per share.
            # entry_price is the premium per share collected when selling
            # the CSP.  Subtracting it gives the true effective cost basis,
            # which is the whole point of the wheel strategy.
            cost_basis = (pos.strike or 0.0) - (pos.entry_price or 0.0)
            new_pos = Position(
                symbol=pos.symbol,
                strategy=Strategy.WHEEL_SHARES.value,
                pair_id=pos.pair_id,
                state=PositionState.OPEN.value,
                quantity=int(broker_stocks[pos.symbol]),
                entry_date=now_et().strftime("%Y-%m-%d"),
                entry_price=cost_basis,
                cost_basis=cost_basis,
                total_premium_collected=pos.total_premium_collected,
                notes=f"Assigned from {pos.strategy} position #{pos.id}",
            )
            new_id = self._db.create_position(new_pos)

            msg = (
                f"ASSIGNED: {pos.symbol} {pos.option_type} ${pos.strike} "
                f"(position #{pos.id}) -> {int(broker_stocks[pos.symbol])} shares "
                f"created as position #{new_id} (cost basis ${cost_basis:.2f})"
            )
            return msg

        if option_gone and not shares_appeared:
            # Option gone but no shares — likely expired worthless
            self._db.close_position(pos.id, exit_price=0.0, exit_reason="expired")
            msg = (
                f"EXPIRED: {pos.symbol} {pos.option_type} ${pos.strike} "
                f"(position #{pos.id}) expired worthless — full premium kept"
            )
            log.info(msg)
            return msg

        return None

    def _check_leaps_still_exists(
        self,
        pos: Position,
        broker_options: set[str],
    ) -> str | None:
        """Verify a LEAPS position still exists at the broker."""
        key = self._option_key(pos)
        if key not in broker_options:
            # LEAPS gone — could be assigned, exercised, or expired
            self._db.close_position(
                pos.id,
                exit_price=0.0,
                exit_reason="expired_or_exercised",
            )
            msg = (
                f"LEAPS GONE: {pos.symbol} call ${pos.strike} "
                f"(position #{pos.id}) no longer at broker — "
                f"marked expired/exercised"
            )
            log.warning(msg)
            return msg
        return None

    def _check_covered_call(
        self,
        pos: Position,
        broker_options: set[str],
        broker_stocks: dict[str, float],
    ) -> str | None:
        """Check covered call status — if call gone AND shares gone, assigned."""
        key = self._option_key(pos)
        if key not in broker_options:
            shares_remaining = broker_stocks.get(pos.symbol, 0)
            if shares_remaining < 100:
                # Call assigned — shares were called away
                self._db.close_position(
                    pos.id, exit_price=0.0, exit_reason="assigned",
                )
                msg = (
                    f"CC ASSIGNED: {pos.symbol} call ${pos.strike} "
                    f"(position #{pos.id}) — shares called away"
                )
                log.info(msg)
                return msg
            else:
                # Call expired worthless, shares still held
                self._db.close_position(
                    pos.id, exit_price=0.0, exit_reason="expired",
                )
                msg = (
                    f"CC EXPIRED: {pos.symbol} call ${pos.strike} "
                    f"(position #{pos.id}) expired — shares retained"
                )
                log.info(msg)
                return msg
        return None

    def _check_shares_still_held(
        self,
        pos: Position,
        broker_stocks: dict[str, float],
    ) -> str | None:
        """Verify share positions are still held at the broker."""
        qty = broker_stocks.get(pos.symbol, 0)
        if qty < pos.quantity:
            self._db.close_position(
                pos.id,
                exit_price=0.0,
                exit_reason="shares_sold_externally",
            )
            msg = (
                f"SHARES GONE: {pos.symbol} x{pos.quantity} "
                f"(position #{pos.id}) — only {qty:.0f} remain at broker"
            )
            log.warning(msg)
            return msg
        return None

    # ── VRP spread reconciliation ─────────────────────────────────────────

    def _reconcile_vrp_spreads(self, broker_options: set[str]) -> list[str]:
        """Verify both legs of every open VRP spread still exist at the broker.

        For each spread pair_id, checks that both short and long legs are still
        held. If a leg is missing, closes the DB position and returns an alert.
        """
        changes: list[str] = []

        vrp_positions = self._db.get_open_positions(strategy="vrp_spread")
        if not vrp_positions:
            return changes

        # Group by pair_id
        pairs: dict[str, list[Position]] = {}
        for pos in vrp_positions:
            if pos.pair_id:
                pairs.setdefault(pos.pair_id, []).append(pos)

        for pair_id, legs in pairs.items():
            for leg in legs:
                key = self._option_key(leg)
                if key not in broker_options:
                    # Leg missing at broker — close it
                    self._db.close_position(
                        leg.id,
                        exit_price=0.0,
                        exit_reason="leg_missing_at_broker",
                    )
                    msg = (
                        f"VRP SPREAD LEG MISSING: {leg.symbol} put ${leg.strike} "
                        f"(pair {pair_id}, position #{leg.id}) "
                        f"not found at broker — closed in DB"
                    )
                    log.warning(msg)
                    changes.append(msg)

        if changes:
            log.warning("VRP reconciliation: %d missing leg(s) detected", len(changes))
        else:
            log.info("VRP reconciliation: all spread legs accounted for")

        return changes

    # ── Broker data helpers ───────────────────────────────────────────────

    def _get_broker_stock_map(self) -> dict[str, float]:
        """Build {symbol: total_quantity} map from broker stock positions.

        Note: AlpacaBroker.get_stock_positions() returns list[dict], not dataclass.
        """
        positions = self.broker.get_stock_positions()
        stock_map: dict[str, float] = {}
        for sp in positions:
            sym = sp["symbol"]
            qty = sp["quantity"]
            stock_map[sym] = stock_map.get(sym, 0) + qty
        log.debug("Broker stock positions: %d symbols", len(stock_map))
        return stock_map

    def _get_broker_option_set(self) -> set[str]:
        """Build a set of option keys currently held at the broker.

        Note: AlpacaBroker.get_option_positions() returns list[dict], not dataclass.
        The option dicts have 'symbol' but not 'option_type', 'strike', or
        'expiration_date' as separate fields — they are encoded in the OCC symbol.
        We store the full OCC symbol as the key for comparison.
        """
        positions = self.broker.get_option_positions()
        option_set: set[str] = set()
        for op in positions:
            # Alpaca option dicts contain 'symbol' (the full OCC symbol)
            # We use the symbol directly since it uniquely identifies the contract.
            option_set.add(op["symbol"])
        log.debug("Broker option positions: %d contracts", len(option_set))
        return option_set

    @staticmethod
    def _option_key(pos: Position) -> str:
        """Generate a matching OCC symbol for a DB position to compare with broker data.

        OCC format: SYMBOL(6 chars) + YYMMDD + C/P + strike*1000 (8 digits).
        Must match the format used by AlpacaBroker._build_option_symbol().
        """
        from datetime import datetime as _dt

        symbol = pos.symbol or ""
        strike = pos.strike or 0.0
        expiration = pos.expiration_date or ""
        option_type = pos.option_type or "put"

        try:
            exp_date = _dt.strptime(expiration, "%Y-%m-%d")
            date_str = exp_date.strftime("%y%m%d")
        except (ValueError, TypeError):
            # Fall back to pipe-delimited key if date parsing fails
            return f"{symbol}|{option_type}|{strike}|{expiration}"

        type_char = "C" if option_type.lower() == "call" else "P"
        # Use round() instead of int() to prevent floating-point truncation
        # Must match AlpacaBroker._build_option_symbol()
        strike_int = round(strike * 1000)
        strike_str = f"{strike_int:08d}"
        padded = symbol.ljust(6)

        return f"{padded}{date_str}{type_char}{strike_str}"
