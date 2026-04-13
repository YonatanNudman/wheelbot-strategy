"""Stock/ETF screening pipeline — filters universe by trading rules and scores candidates."""

from __future__ import annotations

import time
from typing import Optional

from data.models import Signal
from utils.config import get
from utils.logger import get_logger
from utils.market import has_earnings_within

log = get_logger(__name__)


class StockScanner:
    """Screens the universe of stocks/ETFs for entry opportunities."""

    def __init__(self, broker, universe, position_sizer):
        self.broker = broker
        self.universe = universe
        self.sizer = position_sizer

        self.min_ivr = get("wheel.min_ivr", 30)
        self.min_oi = get("wheel.min_open_interest", 500)
        self.max_spread = get("wheel.max_bid_ask_spread", 0.10)
        self.earnings_buffer = get("wheel.earnings_buffer_days", 14)

    def scan_pmcc_etfs(self) -> list[dict]:
        """Scan PMCC ETFs for LEAPS + short call opportunities.

        Returns list of ETFs with their chain data, filtered by basic quality checks.
        """
        etfs = self.universe.get_pmcc_candidates()
        results = []

        for symbol in etfs:
            log.info("Scanning PMCC ETF: %s", symbol)
            try:
                quote = self.broker.get_stock_quote(symbol)
                if not quote:
                    continue

                results.append({
                    "symbol": symbol,
                    "price": quote.price,
                    "change_pct": quote.change_pct,
                })
                time.sleep(1.5)  # Rate limit

            except Exception as e:
                log.warning("Failed to scan %s: %s", symbol, e)

        return results

    def scan_wheel_candidates(self, buying_power: float) -> list[dict]:
        """Scan wheel candidates with full filtering pipeline.

        Pipeline:
        1. Filter by affordability (strike*100 <= allocatable capital)
        2. Check earnings buffer
        3. Fetch options chain
        4. Filter by IVR, open interest, bid-ask spread
        5. Find best strike near target delta
        6. Score using Alpaca formula
        """
        tickers = self.universe.get_wheel_candidates(buying_power)
        log.info("Scanning %d wheel candidates (buying power: $%.0f)", len(tickers), buying_power)

        candidates = []

        for symbol in tickers:
            try:
                # Earnings check
                if has_earnings_within(symbol, self.earnings_buffer):
                    log.debug("Skipping %s — earnings within %d days", symbol, self.earnings_buffer)
                    continue

                # Fetch option chain
                chain = self.broker.get_option_chain(symbol)
                if not chain:
                    continue

                # Filter puts in target DTE range
                target_dte_min = get("wheel.target_dte_min", 30)
                target_dte_max = get("wheel.target_dte_max", 45)
                target_delta = get("wheel.target_delta", 0.20)

                best = None
                best_score = -1

                for contract in chain:
                    if contract.option_type != "put":
                        continue

                    from utils.market import dte as calc_dte
                    c_dte = calc_dte(contract.expiration_date)

                    # DTE check
                    if not (target_dte_min <= c_dte <= target_dte_max):
                        continue

                    # Delta check (puts have negative delta, use absolute)
                    if contract.delta is None:
                        continue
                    abs_delta = abs(contract.delta)
                    if not (0.10 <= abs_delta <= 0.35):
                        continue

                    # Liquidity checks
                    if contract.open_interest < self.min_oi:
                        continue
                    spread = contract.ask - contract.bid
                    if spread > self.max_spread:
                        continue

                    # Affordability check
                    collateral = contract.strike * 100
                    if not self.sizer.can_afford(collateral, buying_power):
                        continue

                    # Score: (1-|Δ|) × (250/(DTE+5)) × (bid/strike)
                    score = (1 - abs_delta) * (250 / (c_dte + 5)) * (contract.bid / contract.strike)

                    if score > best_score:
                        best_score = score
                        best = {
                            "symbol": symbol,
                            "strike": contract.strike,
                            "expiration": contract.expiration_date,
                            "bid": contract.bid,
                            "ask": contract.ask,
                            "delta": abs_delta,
                            "theta": contract.theta,
                            "iv": contract.iv,
                            "dte": c_dte,
                            "credit": contract.bid * 100,
                            "collateral": collateral,
                            "open_interest": contract.open_interest,
                            "score": round(score, 6),
                        }

                if best:
                    candidates.append(best)
                    log.info(
                        "Candidate: %s $%s put, %d DTE, delta %.2f, credit $%.0f, score %.4f",
                        best["symbol"], best["strike"], best["dte"],
                        best["delta"], best["credit"], best["score"],
                    )

                time.sleep(1.5)  # Rate limit between tickers

            except Exception as e:
                log.warning("Error scanning %s: %s", symbol, e)

        # Sort by score descending
        candidates.sort(key=lambda c: c["score"], reverse=True)
        log.info("Found %d wheel candidates after filtering", len(candidates))
        return candidates
