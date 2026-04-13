"""GPT-4o AI researcher — ranks filtered candidates with market analysis."""

from __future__ import annotations

import os
from typing import Optional

from openai import OpenAI

from utils.config import get
from utils.logger import get_logger

log = get_logger(__name__)


class AIResearcher:
    """Uses OpenAI GPT-4o to rank and analyze trading candidates."""

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.warning("OPENAI_API_KEY not set — AI research disabled")
            self.client = None
            return

        self.client = OpenAI(api_key=api_key)
        self.model = get("ai.model", "gpt-4o")
        self.max_candidates = get("ai.max_candidates", 5)

    @property
    def enabled(self) -> bool:
        return self.client is not None and get("ai.enabled", True)

    def rank_pmcc_candidates(self, candidates: list[dict]) -> list[dict]:
        """Rank PMCC ETF candidates by AI analysis.

        Input: list of dicts with symbol, price, iv, delta, etc.
        Output: same list reordered with ai_reasoning added.
        """
        if not self.enabled or not candidates:
            return candidates

        return self._rank(candidates, strategy="PMCC")

    def rank_wheel_candidates(self, candidates: list[dict]) -> list[dict]:
        """Rank wheel stock candidates by AI analysis."""
        if not self.enabled or not candidates:
            return candidates

        return self._rank(candidates, strategy="wheel (cash-secured puts)")

    def _rank(self, candidates: list[dict], strategy: str) -> list[dict]:
        """Send candidates to GPT-4o for ranking with reasoning."""
        candidate_text = "\n".join(
            f"- {c['symbol']}: ${c.get('strike', 'N/A')} strike, "
            f"{c.get('dte', 'N/A')} DTE, delta {c.get('delta', 'N/A')}, "
            f"credit ${c.get('credit', c.get('cost', 'N/A'))}, "
            f"score {c.get('score', 'N/A')}"
            for c in candidates
        )

        prompt = f"""You are a quantitative options analyst. Rank these {len(candidates)} candidates for a {strategy} strategy.

For each candidate, consider:
1. Current market conditions and sector sentiment
2. Whether the IV/premium is rich relative to recent history
3. Upcoming catalysts or risks (earnings, Fed meetings, etc.)
4. Technical setup (is the stock trending, range-bound, or breaking down?)

Candidates:
{candidate_text}

Return a JSON array with this structure:
[
  {{"symbol": "XYZ", "rank": 1, "reasoning": "One sentence explaining why this is the top pick"}},
  ...
]

Only return the JSON array, nothing else. Rank from best (1) to worst ({len(candidates)})."""

        try:
            log.info("Sending %d candidates to %s for ranking", len(candidates), self.model)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )

            content = response.choices[0].message.content.strip()

            # Parse JSON response
            import json

            # Handle markdown code blocks
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            rankings = json.loads(content)

            # Merge AI reasoning into candidates
            reasoning_map = {r["symbol"]: r["reasoning"] for r in rankings}
            rank_map = {r["symbol"]: r["rank"] for r in rankings}

            for c in candidates:
                c["ai_reasoning"] = reasoning_map.get(c["symbol"], "")
                c["ai_rank"] = rank_map.get(c["symbol"], 99)

            candidates.sort(key=lambda c: c.get("ai_rank", 99))

            log.info("AI ranking complete. Top pick: %s", candidates[0]["symbol"] if candidates else "none")
            return candidates[: self.max_candidates]

        except Exception as e:
            log.error("AI research failed: %s — returning unranked candidates", e)
            return candidates

    def analyze_exit_opportunity(self, position_data: dict) -> Optional[str]:
        """Get AI opinion on whether to close a position early.

        Used for borderline cases (e.g., 40% profit — close or hold for 50%?).
        """
        if not self.enabled:
            return None

        prompt = f"""You are an options trading analyst. Should this position be closed now or held?

Position: {position_data.get('symbol')} {position_data.get('strike')} {position_data.get('option_type')}
Expiration: {position_data.get('expiration_date')} ({position_data.get('dte_remaining')} DTE)
Entry: ${position_data.get('entry_price', 0):.2f}
Current: ${position_data.get('current_price', 0):.2f}
P&L: {position_data.get('pnl_percent', 0):.1f}%
Delta: {position_data.get('current_delta', 'N/A')}

Respond in one sentence: "HOLD because..." or "CLOSE because..." """

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=100,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            log.warning("AI exit analysis failed: %s", e)
            return None
