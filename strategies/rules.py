"""Trading rules engine for WheelBot.

Every rule is a pure validator: it takes relevant data and returns
(passed: bool, message: str).  The aggregate runners compose these
into pre-trade and exit checklists.
"""

from __future__ import annotations

from typing import Optional

from utils.config import get
from utils.logger import get_logger
from utils.market import has_earnings_within
from utils.timing import is_market_open, in_optimal_entry_window, past_entry_cutoff

log = get_logger(__name__)


# ── Result type ───────────────────────────────────────────────────────────

RuleResult = tuple[bool, str]


# ── Universal rules ───────────────────────────────────────────────────────

def check_earnings_buffer(symbol: str, buffer_days: int | None = None) -> RuleResult:
    """True if no earnings within *buffer_days*."""
    if buffer_days is None:
        buffer_days = get("wheel.earnings_buffer_days", 14)

    if has_earnings_within(symbol, buffer_days):
        return False, f"{symbol} has earnings within {buffer_days} days"
    return True, f"{symbol} clear of earnings for {buffer_days}+ days"


def check_liquidity(
    open_interest: int,
    bid_ask_spread: float,
    min_oi: int | None = None,
    max_spread: float | None = None,
) -> RuleResult:
    """True if the option is liquid enough to trade."""
    if min_oi is None:
        min_oi = get("wheel.min_open_interest", 500)
    if max_spread is None:
        max_spread = get("wheel.max_bid_ask_spread", 0.10)

    if open_interest < min_oi:
        return False, f"Open interest {open_interest} < minimum {min_oi}"
    if bid_ask_spread > max_spread:
        return False, f"Bid-ask spread ${bid_ask_spread:.2f} > max ${max_spread:.2f}"
    return True, f"Liquidity OK (OI={open_interest}, spread=${bid_ask_spread:.2f})"


def check_position_limit(
    current_count: int,
    max_total: int | None = None,
) -> RuleResult:
    """True if under the global position cap."""
    if max_total is None:
        max_total = get("positions.max_open_total", 3)

    if current_count >= max_total:
        return False, f"Position limit reached ({current_count}/{max_total})"
    return True, f"Position room available ({current_count}/{max_total})"


def check_capital_available(
    required: float,
    available: float,
    reserve_pct: float | None = None,
) -> RuleResult:
    """True if enough capital remains after keeping the reserve."""
    if reserve_pct is None:
        reserve_pct = get("capital.reserve_pct", 0.10)

    usable = available * (1.0 - reserve_pct)
    if required > usable:
        return (
            False,
            f"Need ${required:.2f} but only ${usable:.2f} usable "
            f"(${available:.2f} - {reserve_pct:.0%} reserve)",
        )
    return True, f"Capital OK — ${usable:.2f} usable, ${required:.2f} required"


def check_pdt(day_trade_count: int, limit: int = 3) -> RuleResult:
    """True if safe from the Pattern Day Trader rule."""
    if day_trade_count >= limit:
        return False, f"PDT risk: {day_trade_count}/{limit} day trades used"
    return True, f"PDT OK ({day_trade_count}/{limit} day trades)"


def check_market_hours() -> RuleResult:
    """True if the market is currently open."""
    if not is_market_open():
        return False, "Market is closed"
    return True, "Market is open"


def check_entry_window() -> RuleResult:
    """True if inside the optimal entry window."""
    if not in_optimal_entry_window():
        return False, "Outside optimal entry window (10:00-11:00 AM ET)"
    return True, "Inside optimal entry window"


def check_not_past_cutoff() -> RuleResult:
    """True if before the 3:45 PM ET new-entry cutoff."""
    if past_entry_cutoff():
        return False, "Past 3:45 PM ET cutoff — no new entries"
    return True, "Before entry cutoff"


# ── PMCC rules ────────────────────────────────────────────────────────────

def check_leaps_delta(
    delta: float,
    min_delta: float | None = None,
    max_delta: float = 0.85,
) -> RuleResult:
    """True if LEAPS delta is in the deep-ITM sweet spot."""
    if min_delta is None:
        min_delta = get("pmcc.leaps_delta", 0.80) - 0.05  # default 0.75
    abs_delta = abs(delta)
    if abs_delta < min_delta:
        return False, f"LEAPS delta {abs_delta:.2f} < min {min_delta:.2f}"
    if abs_delta > max_delta:
        return False, f"LEAPS delta {abs_delta:.2f} > max {max_delta:.2f}"
    return True, f"LEAPS delta {abs_delta:.2f} in range [{min_delta:.2f}, {max_delta:.2f}]"


def check_leaps_dte(dte: int, min_dte: int | None = None) -> RuleResult:
    """True if LEAPS has enough time remaining."""
    if min_dte is None:
        min_dte = get("pmcc.leaps_min_dte", 180)

    if dte < min_dte:
        return False, f"LEAPS DTE {dte} < minimum {min_dte}"
    return True, f"LEAPS DTE {dte} >= {min_dte}"


def check_short_call_above_breakeven(
    short_strike: float,
    leaps_strike: float,
    leaps_premium: float,
) -> RuleResult:
    """True if the short call strike is above the LEAPS breakeven.

    Breakeven = LEAPS_strike + LEAPS_premium_paid.
    """
    breakeven = leaps_strike + leaps_premium
    if short_strike <= breakeven:
        return (
            False,
            f"Short strike ${short_strike:.2f} <= breakeven ${breakeven:.2f} "
            f"(LEAPS strike ${leaps_strike:.2f} + premium ${leaps_premium:.2f})",
        )
    return (
        True,
        f"Short strike ${short_strike:.2f} > breakeven ${breakeven:.2f}",
    )


def check_short_call_delta(
    delta: float,
    min_delta: float = 0.15,
    max_delta: float | None = None,
) -> RuleResult:
    """True if the short call delta is in the target OTM range."""
    if max_delta is None:
        max_delta = get("pmcc.short_call_delta", 0.25) + 0.10  # default 0.35
    abs_delta = abs(delta)
    if abs_delta < min_delta:
        return False, f"Short call delta {abs_delta:.2f} < min {min_delta:.2f}"
    if abs_delta > max_delta:
        return False, f"Short call delta {abs_delta:.2f} > max {max_delta:.2f}"
    return True, f"Short call delta {abs_delta:.2f} in range [{min_delta:.2f}, {max_delta:.2f}]"


def check_roll_needed(
    dte: int,
    extrinsic: float,
    dte_threshold: int | None = None,
    extrinsic_threshold: float | None = None,
) -> RuleResult:
    """True if the short option should be rolled (low DTE or extrinsic)."""
    if dte_threshold is None:
        dte_threshold = get("pmcc.roll_dte_threshold", 7)
    if extrinsic_threshold is None:
        extrinsic_threshold = get("pmcc.roll_extrinsic_threshold", 0.10)

    reasons: list[str] = []
    if dte <= dte_threshold:
        reasons.append(f"DTE {dte} <= {dte_threshold}")
    if extrinsic <= extrinsic_threshold:
        reasons.append(f"extrinsic ${extrinsic:.2f} <= ${extrinsic_threshold:.2f}")

    if reasons:
        return True, f"Roll needed: {', '.join(reasons)}"
    return False, f"No roll needed (DTE={dte}, extrinsic=${extrinsic:.2f})"


def check_assignment_risk(
    extrinsic: float,
    is_itm: bool,
    threshold: float | None = None,
) -> RuleResult:
    """True (DANGER) if early assignment is likely.

    Warning fires when the short call is ITM and extrinsic value is very low.
    """
    if threshold is None:
        threshold = get("pmcc.assignment_extrinsic_floor", 0.05)

    if is_itm and extrinsic <= threshold:
        return (
            True,
            f"ASSIGNMENT RISK — ITM with extrinsic ${extrinsic:.2f} <= ${threshold:.2f}",
        )
    return False, f"Assignment risk low (ITM={is_itm}, extrinsic=${extrinsic:.2f})"


def check_dividend_risk(
    days_to_exdiv: int | None,
    is_itm: bool,
    buffer: int | None = None,
) -> RuleResult:
    """True (DANGER) if early assignment due to dividend is likely.

    Risk exists when ex-dividend is soon, the short call is ITM,
    and it may be rational for the holder to exercise early.
    """
    if buffer is None:
        buffer = get("pmcc.dividend_buffer_days", 7)

    if days_to_exdiv is None:
        return False, "No ex-dividend date known"

    if is_itm and days_to_exdiv <= buffer:
        return (
            True,
            f"DIVIDEND RISK — ITM with ex-div in {days_to_exdiv} days (buffer={buffer})",
        )
    return False, f"Dividend risk low (ex-div in {days_to_exdiv} days, ITM={is_itm})"


def check_leaps_health(dte: int, min_dte: int | None = None) -> RuleResult:
    """True if LEAPS DTE is low enough that it needs to be rolled out."""
    if min_dte is None:
        min_dte = get("pmcc.leaps_min_dte_before_roll", 90)

    if dte <= min_dte:
        return True, f"LEAPS DTE {dte} <= {min_dte} — time to roll the LEAPS out"
    return False, f"LEAPS healthy (DTE {dte} > {min_dte})"


def check_roll_for_credit(new_credit: float, old_cost: float) -> RuleResult:
    """True if the roll produces a net credit (or at worst even)."""
    net = new_credit - old_cost
    if net < 0:
        return False, f"Roll costs net ${abs(net):.2f} debit — not ideal"
    return True, f"Roll produces net ${net:.2f} credit"


# ── Wheel rules ───────────────────────────────────────────────────────────

def check_csp_delta(
    delta: float,
    target: float | None = None,
    tolerance: float = 0.05,
) -> RuleResult:
    """True if the CSP delta is within tolerance of target."""
    if target is None:
        target = get("wheel.target_delta", 0.20)
    abs_delta = abs(delta)
    lo = target - tolerance
    hi = target + tolerance
    if abs_delta < lo or abs_delta > hi:
        return False, f"CSP delta {abs_delta:.2f} outside [{lo:.2f}, {hi:.2f}]"
    return True, f"CSP delta {abs_delta:.2f} within target range [{lo:.2f}, {hi:.2f}]"


def check_ivr(ivr: float, min_ivr: float | None = None) -> RuleResult:
    """True if IV Rank is high enough to sell premium."""
    if min_ivr is None:
        min_ivr = get("wheel.min_ivr", 30)

    if ivr < min_ivr:
        return False, f"IV Rank {ivr:.1f} < minimum {min_ivr}"
    return True, f"IV Rank {ivr:.1f} >= {min_ivr}"


def check_dte_range(
    dte: int,
    min_dte: int | None = None,
    max_dte: int | None = None,
) -> RuleResult:
    """True if DTE is in the target range for Wheel."""
    if min_dte is None:
        min_dte = get("wheel.target_dte_min", 30)
    if max_dte is None:
        max_dte = get("wheel.target_dte_max", 45)

    if dte < min_dte:
        return False, f"DTE {dte} < min {min_dte}"
    if dte > max_dte:
        return False, f"DTE {dte} > max {max_dte}"
    return True, f"DTE {dte} in range [{min_dte}, {max_dte}]"


def check_profit_target(
    current_price: float,
    entry_price: float,
    target_pct: float | None = None,
) -> RuleResult:
    """True if the position has hit 50% (default) profit target.

    For credit trades: profit = entry_credit - current_price_to_close.
    A *target_pct* of 0.50 means close when you've captured half the credit.
    """
    if target_pct is None:
        target_pct = get("wheel.profit_target_pct", 0.50)

    target_price = entry_price * (1.0 - target_pct)
    if current_price <= target_price:
        pnl_pct = (entry_price - current_price) / entry_price
        return (
            True,
            f"Profit target HIT — current ${current_price:.2f} <= "
            f"target ${target_price:.2f} ({pnl_pct:.0%} of max)",
        )
    return (
        False,
        f"Profit target not reached — ${current_price:.2f} vs target ${target_price:.2f}",
    )


def check_stop_loss(
    current_price: float,
    entry_price: float,
    multiplier: float | None = None,
) -> RuleResult:
    """True if the position has hit the stop-loss threshold.

    Stop = entry_price * multiplier (e.g. 2x the credit received).
    """
    if multiplier is None:
        multiplier = get("wheel.stop_loss_multiplier", 2.0)

    stop_price = entry_price * multiplier
    if current_price >= stop_price:
        return (
            True,
            f"STOP LOSS — current ${current_price:.2f} >= "
            f"stop ${stop_price:.2f} ({multiplier}x entry)",
        )
    return (
        False,
        f"Stop loss OK — ${current_price:.2f} vs stop ${stop_price:.2f}",
    )


def check_cc_above_cost_basis(strike: float, cost_basis: float) -> RuleResult:
    """True if the covered call strike is above the share cost basis."""
    if strike <= cost_basis:
        return (
            False,
            f"CC strike ${strike:.2f} <= cost basis ${cost_basis:.2f} — would lock in loss",
        )
    return True, f"CC strike ${strike:.2f} > cost basis ${cost_basis:.2f}"


def score_option(delta: float, dte: int, bid: float, strike: float) -> float:
    """Alpaca-style option scoring formula.

    Score = (1 - |delta|) * (250 / (DTE + 5)) * (bid / strike)

    Higher score = more attractive premium relative to risk and time.
    """
    if strike <= 0 or (dte + 5) <= 0:
        return 0.0
    return (1.0 - abs(delta)) * (250.0 / (dte + 5)) * (bid / strike)


# ── Aggregate runners ─────────────────────────────────────────────────────

def run_entry_checks(
    symbol: str,
    strategy: str,
    current_position_count: int,
    required_capital: float,
    available_capital: float,
    day_trade_count: int,
    open_interest: int,
    bid_ask_spread: float,
    delta: float,
    dte: int,
    ivr: float = 0.0,
    # PMCC-specific
    leaps_strike: Optional[float] = None,
    leaps_premium: Optional[float] = None,
    short_strike: Optional[float] = None,
) -> list[tuple[str, bool, str]]:
    """Run all relevant entry rules for a new position.

    Returns a list of (rule_name, passed, message) tuples.
    """
    results: list[tuple[str, bool, str]] = []

    def _add(name: str, result: RuleResult) -> None:
        results.append((name, result[0], result[1]))

    # --- Universal checks ---
    _add("market_hours", check_market_hours())
    _add("not_past_cutoff", check_not_past_cutoff())
    _add("entry_window", check_entry_window())
    _add("earnings_buffer", check_earnings_buffer(symbol))
    _add("liquidity", check_liquidity(open_interest, bid_ask_spread))
    _add("position_limit", check_position_limit(current_position_count))
    _add("capital_available", check_capital_available(required_capital, available_capital))
    _add("pdt", check_pdt(day_trade_count))

    # --- Strategy-specific checks ---
    if strategy.startswith("pmcc"):
        _add("leaps_delta", check_leaps_delta(delta))
        _add("leaps_dte", check_leaps_dte(dte))

        if leaps_strike is not None and leaps_premium is not None and short_strike is not None:
            _add(
                "short_above_breakeven",
                check_short_call_above_breakeven(short_strike, leaps_strike, leaps_premium),
            )

    elif strategy.startswith("wheel"):
        _add("csp_delta", check_csp_delta(delta))
        _add("ivr", check_ivr(ivr))
        _add("dte_range", check_dte_range(dte))

    passed_count = sum(1 for _, p, _ in results if p)
    total = len(results)
    log.info(
        "Entry checks for %s (%s): %d/%d passed",
        symbol, strategy, passed_count, total,
    )
    for name, passed, msg in results:
        level = log.debug if passed else log.warning
        level("  [%s] %s — %s", "PASS" if passed else "FAIL", name, msg)

    return results


def run_exit_checks(
    position: "Position",  # noqa: F821 — avoids circular import
    current_price: float,
    stock_price: float = 0.0,
) -> list[tuple[str, bool, str]]:
    """Run all exit rules for an open position.

    Returns a list of (rule_name, triggered, message) tuples.
    'triggered' means the rule says to act (take profit, stop loss, roll, etc.).

    Args:
        position: The open position to check.
        current_price: Current option price (mid).
        stock_price: Current underlying stock price (needed for extrinsic calc).
    """
    from data.models import Position  # local import to avoid circular dependency

    results: list[tuple[str, bool, str]] = []

    def _add(name: str, result: RuleResult) -> None:
        results.append((name, result[0], result[1]))

    entry_price = position.entry_price

    # --- Profit / loss ---
    _add("profit_target", check_profit_target(current_price, entry_price))
    _add("stop_loss", check_stop_loss(current_price, entry_price))

    # --- DTE-based roll check ---
    if position.dte_remaining is not None:
        extrinsic = _estimate_extrinsic(position, current_price, stock_price)
        _add(
            "roll_needed",
            check_roll_needed(position.dte_remaining, extrinsic),
        )

    # --- Strategy-specific ---
    strategy = position.strategy
    if strategy in ("pmcc_short_call", "pmcc_leaps"):
        if position.dte_remaining is not None:
            _add("leaps_health", check_leaps_health(position.dte_remaining))

    if strategy in ("wheel_cc",) and position.cost_basis is not None and position.strike is not None:
        _add("cc_above_cost_basis", check_cc_above_cost_basis(position.strike, position.cost_basis))

    triggered = [(n, t, m) for n, t, m in results if t]
    if triggered:
        log.info(
            "Exit signals for %s %s: %s",
            position.symbol,
            position.strategy,
            ", ".join(n for n, _, _ in triggered),
        )

    return results


# ── Private helpers ───────────────────────────────────────────────────────

def _estimate_extrinsic(position: "Position", current_option_price: float, stock_price: float = 0.0) -> float:  # noqa: F821
    """Rough estimate of remaining extrinsic value.

    For a short option position: extrinsic = option_price - intrinsic.
    Intrinsic requires the STOCK price, not the option price.
    """
    if position.strike is None or stock_price <= 0:
        return current_option_price  # all extrinsic if we can't compute intrinsic

    if position.option_type == "call":
        intrinsic = max(0.0, stock_price - position.strike)
    else:
        intrinsic = max(0.0, position.strike - stock_price)

    return max(0.0, current_option_price - intrinsic)
