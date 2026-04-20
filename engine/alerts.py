"""Discord alert formatters — Robinhood-style, mobile-first, testable.

The routine prompts (``routine-prompts/*.md``) instruct Claude Remote to
produce Discord messages in this exact format; these helpers are the
single source of truth and are also used by the local Discord bot.
"""

from __future__ import annotations


def cushion_pct(stock_price: float, strike: float, right: str) -> float:
    """Percent buffer between current stock price and the short strike.

    For a short put, cushion is how far the stock can fall before the
    strike is breached (stock > strike → positive cushion).
    For a short call, cushion is how far the stock can rise before the
    strike is breached (stock < strike → positive cushion).
    Negative values mean the option is already in-the-money.
    """
    if right.upper() == "P":
        return (stock_price - strike) / stock_price * 100.0
    if right.upper() == "C":
        return (strike - stock_price) / stock_price * 100.0
    raise ValueError(f"right must be 'P' or 'C', got {right!r}")


def breakeven(strike: float, credit: float, right: str) -> float:
    """Breakeven stock price for a short option.

    Short put:  strike - credit  (if assigned, your cost basis is this)
    Short call: strike + credit  (if assigned on shares you own, this is your exit)
    """
    r = right.upper()
    if r == "P":
        return strike - credit
    if r == "C":
        return strike + credit
    raise ValueError(f"right must be 'P' or 'C', got {right!r}")


def _right_label(right: str) -> str:
    r = right.upper()
    if r == "P":
        return "P"
    if r == "C":
        return "C"
    raise ValueError(f"right must be 'P' or 'C', got {right!r}")


def format_entry_alert(
    *,
    symbol: str,
    right: str,
    strike: float,
    contracts: int,
    credit_per_contract: float,
    expiration: str,
    dte: int,
    stock_price: float,
    profit_target: float,
    portfolio_value: float,
    positions_open: int,
    max_positions: int,
) -> str:
    """Render an entry alert.

    Layout (Robinhood-inspired, mobile-friendly, 5 lines):
        🟢 SOLD {SYMBOL} ${strike}{P|C} × {contracts}
        💰 ${credit_total} credit | {dte}d to {expiration}
        📈 Stock ${stock_price} | BE ${breakeven} | {cushion}% cushion
        🎯 Auto-close at ${profit_target} (50% profit)
        💼 Portfolio: ${portfolio} | {open}/{max} positions
    """
    credit_total = credit_per_contract * contracts * 100
    be = breakeven(strike=strike, credit=credit_per_contract, right=right)
    cush = cushion_pct(stock_price=stock_price, strike=strike, right=right)
    r_label = _right_label(right)
    lines = [
        f"🟢 SOLD {symbol} ${strike:g}{r_label} × {contracts}",
        f"💰 ${credit_total:,.0f} credit | {dte}d to {expiration}",
        f"📈 Stock ${stock_price:,.2f} | BE ${be:,.2f} | {cush:.1f}% cushion",
        f"🎯 Auto-close at ${profit_target:,.2f} (50% profit)",
        f"💼 Portfolio: ${portfolio_value:,.0f} | {positions_open}/{max_positions} positions",
    ]
    return "\n".join(lines)


def _signed_money(value: float) -> str:
    """+$36, -$81, +$1 — always signed, never negative zero."""
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.0f}"


def format_position_line(
    *,
    symbol: str,
    right: str,
    strike: float,
    contracts: int,
    avg_entry_price: float,
    current_price: float,
    stock_price: float,
    dte: int,
) -> str:
    """Render a single-position line for the position snapshot.

    Layout (one line, mobile friendly):
        {emoji} {SYMBOL} -{n}× ${strike}{P|C} ({dte}d) [⚠️ ITM]
        ↳ Stock ${stock} | BE ${be} | {+$pnl} ({+pct%})

    Emoji rules (P/L as % of max profit = credit collected):
        🟢 > +5%    🔴 < -5%    ⚪ otherwise (flat)
    """
    max_profit = avg_entry_price * contracts * 100  # what you can make if it expires worthless
    pnl_dollars = (avg_entry_price - current_price) * contracts * 100
    pnl_pct_of_max = (pnl_dollars / max_profit * 100) if max_profit else 0.0

    if pnl_pct_of_max > 5:
        emoji = "🟢"
    elif pnl_pct_of_max < -5:
        emoji = "🔴"
    else:
        emoji = "⚪"

    # ITM test — different for puts vs calls
    r = right.upper()
    itm = (r == "P" and stock_price < strike) or (r == "C" and stock_price > strike)
    itm_flag = " ⚠️ ITM" if itm else ""

    be = breakeven(strike=strike, credit=avg_entry_price, right=right)
    header = f"{emoji} {symbol} -{contracts}× ${strike:g}{_right_label(right)} ({dte}d){itm_flag}"
    detail = (
        f"   Stock ${stock_price:,.2f} | BE ${be:,.2f} | "
        f"{_signed_money(pnl_dollars)} ({pnl_pct_of_max:+.0f}%)"
    )
    return f"{header}\n{detail}"


def format_snapshot(
    *,
    positions: list[dict],
    portfolio_value: float,
    today_change: float,
    time_label: str,
) -> str:
    """Render the full position snapshot alert.

    Layout:
        📊 POSITIONS ({time_label})
        <position line 1>
        <position line 2>
        ...
        💼 Portfolio: ${value} | Today: {+$change}
        📊 Total: {+$total_pnl} across {n} positions
    """
    header = f"📊 POSITIONS ({time_label})"
    if not positions:
        return (
            f"{header}\n"
            f"No open positions.\n"
            f"💼 Portfolio: ${portfolio_value:,.0f} | "
            f"Today: {_signed_money(today_change)}"
        )

    position_lines = [format_position_line(**p) for p in positions]
    total_pnl = sum(
        (p["avg_entry_price"] - p["current_price"]) * p["contracts"] * 100
        for p in positions
    )
    footer = (
        f"💼 Portfolio: ${portfolio_value:,.0f} | "
        f"Today: {_signed_money(today_change)}\n"
        f"📊 Total: {_signed_money(total_pnl)} across {len(positions)} positions"
    )
    return "\n".join([header, *position_lines, footer])
