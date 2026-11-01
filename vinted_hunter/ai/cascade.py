"""AI tiers of the cascade. Cheap models see many items, expensive models see very few.

    tier 2  triage   cheap text model, batches of ~20 listings in one call
    tier 3  vision   cheap vision model, 4 photos of each surviving candidate
    tier 4  deep     strong model, all photos + market data, final verdict + negotiation/resale copy
    tier 5  expert   strongest model, on demand only (Telegram button)
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import RuntimeSettings, Settings
from ..models import AIVerdict, Deal, Listing, Recommendation, SearchSpec
from ..niches import Niche
from . import prompts
from .llm import LLM, image_part

log = logging.getLogger(__name__)

TRIAGE_BATCH = 20
# Fraction of the daily budget each tier may consume before it stops (cheap tiers keep running longest).
BUDGET_RESERVE = {2: 1.0, 3: 0.9, 4: 0.75, 5: 1.0}


def _num(value: Any) -> float | None:
    try:
        f = float(value)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _risk(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _pct(value: Any) -> float | None:
    try:
        return max(-80.0, min(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def _merge_list(a: list[str], b: Any, cap: int = 6) -> list[str]:
    out = list(a)
    if isinstance(b, str):
        b = [b]
    for x in b or []:
        if isinstance(x, str) and x and x not in out:
            out.append(x)
    return out[:cap]


class AICascade:
    def __init__(self, llm: LLM, settings: Settings):
        self.llm = llm
        self.s = settings

    @property
    def enabled(self) -> bool:
        return self.llm.enabled

    # --------------------------------------------------------------- tier 2
    async def triage(self, listings: list[Listing], niche: Niche | None, spec: SearchSpec | None,
                     rs: RuntimeSettings) -> dict[int, AIVerdict]:
        out: dict[int, AIVerdict] = {}
        for i in range(0, len(listings), TRIAGE_BATCH):
            batch = listings[i:i + TRIAGE_BATCH]
            await self.llm.ensure_budget(rs.ai_daily_budget_usd, BUDGET_RESERVE[2])
            res = await self.llm.complete(self.s.model_triage, prompts.SYSTEM_EXPERT,
                                          prompts.triage(batch, niche, spec), tier=2,
                                          max_tokens=400 * len(batch) + 300)
            items = (res.data or {}).get("items", []) if isinstance(res.data, dict) else (res.data or [])
            share = res.cost / max(len(batch), 1)
            for row in items:
                if not isinstance(row, dict) or "relevant" not in row:
                    continue          # half-written row from a truncated reply: don't trust defaults
                try:
                    lid = int(row.get("id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                out[lid] = AIVerdict(
                    tier=2,
                    relevant=bool(row.get("relevant", True)),
                    model_name=row.get("model") or None,
                    resale_estimate=_num(row.get("resale_eur")),
                    authenticity_risk=_risk(row.get("authenticity_risk")),
                    red_flags=_merge_list([], row.get("red_flags"), 3),
                    summary=row.get("note") or "",
                    missing_parts=_merge_list([], row.get("missing")),
                    adjust_pct=_pct(row.get("adjust_pct")),
                    new_price=_num(row.get("new_price_eur")),
                    seller_ok=row.get("seller_ok") if isinstance(row.get("seller_ok"), bool) else None,
                    specs={str(k): str(v) for k, v in (row.get("specs") or {}).items() if v} if isinstance(row.get("specs"), dict) else {},
                    specs_consistent=row.get("specs_consistent") if isinstance(row.get("specs_consistent"), bool) else None,
                    cost_usd=share,
                )
        return out

    # --------------------------------------------------------------- tier 3
    async def vision(self, listing: Listing, niche: Niche | None, spec: SearchSpec | None,
                     prev: AIVerdict | None, fair_value: float | None, rs: RuntimeSettings) -> AIVerdict:
        await self.llm.ensure_budget(rs.ai_daily_budget_usd, BUDGET_RESERVE[3])
        content = [{"type": "text", "text": prompts.vision(listing, niche, spec, fair_value)}]
        content += [image_part(u) for u in listing.photos[:4]]
        res = await self.llm.complete(self.s.model_vision, prompts.SYSTEM_EXPERT, content, tier=3, max_tokens=3000,
                                      reasoning=1024)
        return self._merge(prev, res.data or {}, tier=3, cost=res.cost)

    # --------------------------------------------------------------- tier 4/5
    async def deep(self, deal: Deal, niche: Niche | None, spec: SearchSpec | None, market: dict,
                   rs: RuntimeSettings, *, expert: bool = False) -> AIVerdict:
        tier = 5 if expert else 4
        await self.llm.ensure_budget(rs.ai_daily_budget_usd * (1.5 if expert else 1), BUDGET_RESERVE[tier])
        model = self.s.model_expert if expert else self.s.model_deep
        content = [{"type": "text", "text": prompts.deep(deal, niche, spec, market)}]
        content += [image_part(u) for u in deal.listing.photos[:8]]
        res = await self.llm.complete(model, prompts.SYSTEM_EXPERT, content, tier=tier,
                                      max_tokens=9000 if expert else 6000, reasoning=6000 if expert else 3000,
                                      temperature=0.3)
        return self._merge(deal.verdict, res.data or {}, tier=tier, cost=res.cost)

    # ---------------------------------------------------------- utilities
    async def parse_intent(self, text: str, niches: list[Niche], rs: RuntimeSettings) -> dict:
        await self.llm.ensure_budget(rs.ai_daily_budget_usd)
        res = await self.llm.complete(self.s.model_triage, prompts.SYSTEM_EXPERT, prompts.intent(text, niches),
                                      tier=1, max_tokens=1000)
        return res.data if isinstance(res.data, dict) else {}

    async def negotiation(self, deal: Deal, rs: RuntimeSettings) -> dict:
        await self.llm.ensure_budget(rs.ai_daily_budget_usd)
        res = await self.llm.complete(self.s.model_triage, prompts.SYSTEM_EXPERT, prompts.negotiation(deal),
                                      tier=1, max_tokens=600, temperature=0.6)
        return res.data if isinstance(res.data, dict) else {}

    @staticmethod
    def _merge(prev: AIVerdict | None, data: dict, *, tier: int, cost: float) -> AIVerdict:
        v = prev.model_copy() if prev else AIVerdict()
        v.cost_usd += cost
        if not isinstance(data, dict) or not data:
            log.warning("tier %s returned no usable JSON - verdict unchanged", tier)
            return v                      # never mark a tier as done when nothing was parsed
        v.tier = max(v.tier, tier)
        if data.get("model"):
            v.model_name = data["model"]
        if (resale := _num(data.get("resale_eur"))) is not None:
            v.resale_estimate = resale
        if "authenticity_risk" in data:
            # Later tiers see more evidence: trust them more, but never fully forget an earlier alarm.
            v.authenticity_risk = round(0.75 * _risk(data["authenticity_risk"]) + 0.25 * v.authenticity_risk, 2)
        if data.get("matches_listing") is False:
            v.relevant = False
            v.red_flags = _merge_list(v.red_flags, ["photos non conformes à l'annonce"])
        if (cs := _num(data.get("condition_score"))) is not None:
            v.condition_score = min(10.0, cs)
        v.red_flags = _merge_list(_merge_list(v.red_flags, data.get("red_flags")), data.get("defects"))
        v.missing_parts = _merge_list(v.missing_parts, data.get("missing"))
        if (adj := _pct(data.get("adjust_pct"))) is not None:
            v.adjust_pct = adj
        if (newp := _num(data.get("new_price_eur"))) is not None:
            v.new_price = newp
        if isinstance(data.get("seller_ok"), bool):
            v.seller_ok = data["seller_ok"]
        if isinstance(data.get("specs"), dict) and data["specs"]:
            v.specs = {**v.specs, **{str(k): str(val) for k, val in data["specs"].items() if val}}
        if isinstance(data.get("specs_consistent"), bool):
            v.specs_consistent = data["specs_consistent"] if v.specs_consistent is not False else False
        v.positives = _merge_list(v.positives, data.get("positives"))
        if data.get("completeness"):
            v.positives = _merge_list(v.positives, [f"Complet: {data['completeness']}"])
        if data.get("summary"):
            v.summary = data["summary"]
        if rec := data.get("recommendation"):
            try:
                v.recommendation = Recommendation(str(rec).upper())
            except ValueError:
                pass
        for key, field in (("max_buy_price", "max_buy_price"), ("time_to_sell_days", "time_to_sell_days")):
            if (n := _num(data.get(key))) is not None:
                setattr(v, field, n)
        for key in ("negotiation_message", "resale_title", "resale_description"):
            if data.get(key):
                setattr(v, key, str(data[key]))
        return v
