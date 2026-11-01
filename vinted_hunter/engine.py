"""The Hunter: runs searches through the cascade and drives continuous watches.

    tier 0  rules            free      title excludes, budget, condition, seller on holiday
    tier 1  local valuation  free      reference price + live comparables from our own DB
    ----    detail fetch     1 req     description + all photos, only for promising items
    tier 2  AI triage        ~$0.00003/item   batched text screening, exact model, resale estimate
    tier 3  AI vision        ~$0.001/item     photo check on the best candidates
    tier 4  AI deep          ~$0.04/item      final verdict, only for exceptional deals (or on demand)
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import defaultdict
from typing import Awaitable, Callable

from .ai.cascade import AICascade
from .ai.llm import BudgetExceeded
from .config import RuntimeSettings, Settings
from .db import Database
from .market import estimate, robust_center
from .models import AIVerdict, Condition, Deal, Listing, MarketCheck, SearchSpec
from .niches import STOPWORDS, Niche, NicheCatalog, normalize, signature_key
from .scoring import build_deal, gate_fails, is_alert_worthy, radar_worthy, reference_of, reject_reason
from .vinted.client import VintedClient, VintedUnavailable

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]
# Below this share of a model's typical resale price, a listing is not that model (game, case, part...).
MODEL_PRICE_FLOOR = 0.25
Pool = tuple[set[str], list[tuple[float, Condition]]]
Notify = Callable[[int, Deal], Awaitable[None]]


async def _noop(_: str) -> None:
    return None


class Hunter:
    def __init__(self, settings: Settings, db: Database, vinted: VintedClient, ai: AICascade, niches: NicheCatalog):
        self.s = settings
        self.db = db
        self.vinted = vinted
        self.ai = ai
        self.niches = niches
        self._query_cursor: dict[int, int] = defaultdict(int)
        self._last_run: dict[int, float] = {}
        self._seeded: set[tuple[int, str]] = set()
        self._near_miss: dict[int, tuple[int, int]] = {}   # listing -> (chat, watch) to re-alert on price drops
        self._last_tracker = 0.0
        self._yield: dict[int, float] = {}                 # watch -> recent good-deal yield (EWMA)
        self._last_cleanup = 0.0
        self._interactive = 0                          # user requests in flight: background loops yield to them
        self.ai_notice: str | None = None   # last budget / API problem, surfaced in /stats

    # ============================================================ cascade
    def _identify(self, listing: Listing, niche: Niche | None) -> Listing:
        # A listing the niche rejects (cover, empty box, stand...) must never feed a model's price memory:
        # 126 PS5 accessories at ~30 € once dragged the "PS5" comparables down to 30 €.
        excluded = niche is not None and niche.excluded_by(normalize(listing.title)) is not None
        ref = niche.identify(listing.title, listing.description) if niche and not excluded else None
        if ref and listing.price < ref.resale * MODEL_PRICE_FLOOR:
            ref = None   # "FIFA 21 ps5" at 3 €: a game or an accessory named after the console, not the console
        if ref:
            return listing.model_copy(update={"niche_id": niche.id, "model_key": f"{niche.id}:{ref.key}",
                                              "model_name": ref.name})
        return listing.model_copy(update={"niche_id": niche.id if niche else None,
                                          "model_key": listing.model_key or signature_key(listing.title, listing.brand)})

    async def _value(self, listing: Listing, niche: Niche | None, rs: RuntimeSettings, spec: SearchSpec | None,
                     verdict: AIVerdict | None, pool: Pool | None = None) -> Deal:
        ref = None
        if niche and listing.model_key and listing.model_key.startswith(f"{niche.id}:"):
            key = listing.model_key.split(":", 1)[1]
            ref = next((m for m in niche.models if m.key == key), None)
        comps = await self.db.comparables(listing.model_key, exclude_id=listing.id) if listing.model_key else []
        if len(comps) < 4 and not ref and pool and pool[0] <= set(normalize(listing.title).split()):
            pool_median = statistics.median(p for p, _ in pool[1])
            # A 40 € "PS5 slim power supply" must not borrow the price of PS5 Slim consoles.
            if listing.price >= pool_median * MODEL_PRICE_FLOOR:
                comps = pool[1]   # unknown product: compare with the other results of the same search
        # The AI is asked for the price of a COMPLETE item in very good condition (baseline);
        # missing parts and defects are applied afterwards in scoring.adjust_valuation.
        ai_estimate = verdict.resale_estimate if verdict and verdict.relevant else None
        ai_weight = {2: 0.35, 3: 0.5, 4: 0.8, 5: 0.9}.get(verdict.tier if verdict else 0, 0.35)
        new_price = (ref.new if ref else None) or (verdict.new_price if verdict else None)
        sales = await self.db.sales(listing.model_key) if listing.model_key and not listing.model_key.startswith("sig:") \
            else None
        valuation = estimate(listing.condition, reference=ref.resale if ref else None, comps=comps,
                             ai_estimate=ai_estimate, ai_weight=ai_weight, new_price=new_price,
                             sold=sales["prices"] if sales else None)
        if sales:
            valuation = valuation.model_copy(update={"sold_count": sales["sold"], "days_to_sell": sales["median_days"],
                                                     "sell_through": sales["sell_through"]})
        return build_deal(listing, valuation, niche, rs, verdict, spec)

    def query_pool(self, listings: list[Listing], query: str, niche: Niche | None,
                   spec: SearchSpec | None) -> Pool | None:
        """Prices of search results that really are the searched product (every query word is a title word).

        Returned with the query words: a listing may only use the pool if it matches them too,
        otherwise a Switch game would borrow the price of Switch consoles.
        """
        words = set(normalize(query).split()) - STOPWORDS
        if not words:
            return None
        prices = [(l.price, l.condition) for l in listings
                  if words <= set(normalize(l.title).split()) and not reject_reason(l, niche, None)]
        return (words, prices) if len(prices) >= 6 else None

    async def evaluate(self, listings: list[Listing], *, niche: Niche | None, spec: SearchSpec | None,
                       rs: RuntimeSettings, mode: str = "watch", progress: Progress = _noop,
                       pool: Pool | None = None) -> list[Deal]:
        """Run listings through the cascade. mode: watch (cheap) | hunt (wider) | full (force every tier)."""
        listings = [self._identify(l, niche) for l in listings]
        await self.db.upsert_listings(listings)                       # market memory, free
        await self.db.track(listings)                                 # sales database

        # ---- tier 0
        survivors = [l for l in listings if not reject_reason(l, niche, spec)]
        if not survivors:
            return []

        # ---- tier 1
        cached = {l.id: await self.db.get_verdict(l.id) for l in survivors}
        deals = [await self._value(l, niche, rs, spec, cached[l.id], pool) for l in survivors]
        min_profit = (spec and spec.min_profit) or (niche and niche.min_profit) or rs.min_profit_eur
        if spec and spec.goal == "personal":
            min_profit = 0  # personal buyers want the best value, not a resale margin

        def promising(d: Deal) -> bool:
            return mode == "full" or d.profit is None or d.profit >= min_profit * 0.4

        candidates = sorted((d for d in deals if promising(d)), key=lambda d: d.score, reverse=True)
        limit = {"watch": self.s.max_details_per_cycle, "hunt": 15, "full": 5}[mode]
        candidates = candidates[:limit]
        await progress(f"🧮 {len(survivors)}/{len(listings)} annonces gardées · {len(candidates)} prometteuses")

        # ---- detail fetch (Vinted requests are a cost too)
        enriched: list[Listing] = []
        for i, d in enumerate(candidates, 1):
            l = d.listing
            await progress(f"📖 Lecture des descriptions {i}/{len(candidates)}")
            if not l.details_loaded:
                try:
                    l = self._identify(await self.vinted.enrich(l), niche)
                except VintedUnavailable:
                    pass
            enriched.append(l)
        # Items revealed as junk by their description are dropped here (still free)
        enriched = [l for l in enriched if not reject_reason(l, niche, spec)]

        verdicts: dict[int, AIVerdict] = {l.id: cached[l.id] for l in enriched if cached.get(l.id)}
        if not self.ai.enabled:
            return await self._finalize(enriched, verdicts, niche, spec, rs, deals, pool)

        try:
            # ---- tier 2: batched text triage
            todo = [l for l in enriched if not verdicts.get(l.id) or verdicts[l.id].tier < 2]
            todo = todo[: self.s.max_triage_per_cycle if mode == "watch" else 60]
            if todo:
                await progress(f"🧠 Analyse IA de {len(todo)} annonces (modèle exact, pièces, vendeur)")
                for lid, v in (await self.ai.triage(todo, niche, spec, rs)).items():
                    verdicts[lid] = v
                    await self.db.save_verdict(lid, v)
                enriched = [await self._ai_rekey(l, verdicts.get(l.id), niche) for l in enriched]

            ranked = sorted([await self._value(l, niche, rs, spec, verdicts.get(l.id), pool) for l in enriched],
                            key=lambda d: d.score, reverse=True)

            # ---- tier 3: vision on the best few
            n_vision = {"watch": self.s.max_vision_per_cycle, "hunt": 8, "full": 5}[mode]
            vision_targets = [d for d in ranked if d.listing.photos and (d.verdict is None or d.verdict.relevant)
                              and (mode == "full" or (d.profit or 0) >= min_profit * 0.7)
                              and (d.verdict is None or d.verdict.tier < 3)][:n_vision]
            if vision_targets:
                await progress(f"📸 Vérification des photos de {len(vision_targets)} annonces")
            results = await asyncio.gather(*[
                self.ai.vision(d.listing, niche, spec, d.verdict, d.valuation.fair_value, rs) for d in vision_targets
            ], return_exceptions=True)
            for d, v in zip(vision_targets, results):
                if isinstance(v, AIVerdict):
                    verdicts[d.listing.id] = v
                    await self.db.save_verdict(d.listing.id, v)
                elif isinstance(v, BudgetExceeded):
                    raise v
                else:
                    log.warning("vision failed for %s: %s", d.listing.id, v)
            enriched = [await self._ai_rekey(l, verdicts.get(l.id), niche) for l in enriched]

            # ---- tier 4: deep verdict, only where the money is
            ranked = sorted([await self._value(l, niche, rs, spec, verdicts.get(l.id), pool) for l in enriched],
                            key=lambda d: d.score, reverse=True)
            deep_targets = [d for d in ranked if mode == "full" or (
                rs.auto_deep and (d.profit or 0) >= rs.deep_min_profit_eur and d.score >= 70
                and d.verdict is not None and d.verdict.tier < 4 and d.verdict.relevant)][:2 if mode != "full" else 1]
            for d in deep_targets:
                await progress(f"🎓 Analyse experte : {d.listing.title[:40]}…")
                v = await self.ai.deep(d, niche, spec, await self.db.market_snapshot(d.listing.model_key or ""), rs)
                verdicts[d.listing.id] = v
                await self.db.save_verdict(d.listing.id, v)
            self.ai_notice = None
        except BudgetExceeded as exc:
            self.ai_notice = str(exc)
            log.warning("%s - continuing with free tiers only", exc)
        except Exception as exc:    # AI must never break the hunt
            self.ai_notice = f"Erreur IA: {exc}"
            log.exception("AI tier failed")

        return await self._finalize(enriched, verdicts, niche, spec, rs, deals, pool)

    async def _finalize(self, enriched: list[Listing], verdicts: dict[int, AIVerdict], niche: Niche | None,
                        spec: SearchSpec | None, rs: RuntimeSettings, tier1: list[Deal],
                        pool: Pool | None = None) -> list[Deal]:
        seen = {l.id for l in enriched}
        final = [await self._value(l, niche, rs, spec, verdicts.get(l.id), pool) for l in enriched]
        final += [d for d in tier1 if d.listing.id not in seen]
        return sorted(final, key=lambda d: d.score, reverse=True)

    async def _ai_rekey(self, listing: Listing, v: AIVerdict | None, niche: Niche | None) -> Listing:
        """Let the AI's exact identification fix the model key.

        "PS5" (version unknown) + AI says "PS5 Slim Digital" -> consoles:ps5_slim_digital, so the right
        reference price and comparables apply. Unknown products get an "ai:" key so they still cluster.
        """
        if not v or not v.model_name or not v.relevant:
            return listing
        current = listing.model_key or "sig:"
        cur_ref = next((m for m in niche.models if f"{niche.id}:{m.key}" == current), None) if niche else None
        ai_ref = niche.identify(v.model_name) if niche else None
        new_key = new_name = None
        if ai_ref and (current.startswith("sig:") or (cur_ref and cur_ref.generic and not ai_ref.generic)):
            new_key, new_name = f"{niche.id}:{ai_ref.key}", ai_ref.name
        elif not ai_ref and current.startswith("sig:"):
            new_key, new_name = f"ai:{normalize(v.model_name)[:60]}", v.model_name
        if not new_key or new_key == current:
            return listing
        await self.db.relabel_model(listing.id, new_key)
        return listing.model_copy(update={"model_key": new_key, "model_name": new_name})

    # ============================================================ live market verification
    async def verify_market(self, deal: Deal, niche: Niche | None, rs: RuntimeSettings,
                            spec: SearchSpec | None) -> Deal:
        """Search Vinted for the exact model right now and rank this listing among identical ones.

        2 requests. This is the evidence behind a pépite: "cheapest of 14 identical listings, 40 % under median".
        """
        ref = reference_of(deal, niche)
        if not ref:
            return self._gate(deal, niche, rs, spec)
        query = ref.name.split("(")[0].split("/")[0].strip()
        found: dict[int, Listing] = {}
        for order in ("relevance", "price_low_to_high"):
            try:
                for l in await self.vinted.search(SearchSpec(query=query), order=order):
                    found.setdefault(l.id, l)
            except VintedUnavailable:
                break
        same = [self._identify(l, niche) for l in found.values()]
        same = [l for l in same if l.model_key == deal.listing.model_key and l.id != deal.listing.id
                and not reject_reason(l, niche)]
        await self.db.upsert_listings(same)                 # fresh evidence also feeds future valuations
        await self.db.track(same)

        mine = deal.listing.condition.value_factor
        # Compare like with like: every price expressed in this listing's condition.
        norm = sorted(l.price / l.condition.value_factor * mine for l in same)
        check = MarketCheck(n=len(norm))
        if norm:
            median, q1, _ = robust_center(norm)
            check.median, check.p25 = round(median, 2), round(q1, 2)
            check.cheaper = sum(1 for p in norm if p < deal.listing.price - 0.01)
            check.cheapest_other = round(norm[0], 2)
            check.percentile = round(1 - check.cheaper / len(norm), 2)
        # Re-value with the fresh comparables, keep the check, then apply the gate.
        fresh = await self._value(deal.listing, niche, rs, spec, deal.verdict)
        fresh = fresh.model_copy(update={"market_check": check, "watch_id": deal.watch_id,
                                         "flags": list(dict.fromkeys(fresh.flags + [f for f in deal.flags if f == "baisse_prix"]))})
        return self._gate(fresh, niche, rs, spec)

    @staticmethod
    def _gate(deal: Deal, niche: Niche | None, rs: RuntimeSettings, spec: SearchSpec | None) -> Deal:
        fails = gate_fails(deal, niche, rs, spec)
        return deal.model_copy(update={"pepite": not fails, "gate_fails": fails})

    # ============================================================ one-shot actions
    async def hunt(self, spec: SearchSpec, rs: RuntimeSettings, progress: Progress = _noop) -> tuple[list[Deal], Niche | None]:
        self._interactive += 1                         # the user is waiting: pause background scanning
        try:
            return await self._hunt(spec, rs, progress)
        finally:
            self._interactive -= 1

    async def _hunt(self, spec: SearchSpec, rs: RuntimeSettings, progress: Progress = _noop) -> tuple[list[Deal], Niche | None]:
        niche = self.niches.get(spec.niche_id) or (self.niches.guess(spec.query) if spec.query else None)
        queries = [spec.query] if spec.query else (niche.queries[:4] if niche else [])
        found: dict[int, Listing] = {}
        await progress("🔎 Recherche sur Vinted")
        pages = [(q, o, p) for q in queries for o, p in (("relevance", 1), ("newest_first", 1), ("relevance", 2))]
        done = 0
        for q in queries:
            sub = spec.model_copy(update={"query": q})
            for order, page in (("relevance", 1), ("newest_first", 1), ("relevance", 2)):
                try:
                    for l in await self.vinted.search(sub, order=order, page=page):
                        found.setdefault(l.id, l)
                    done += 1
                    await progress(f"🔎 Recherche sur Vinted {done}/{len(pages)} pages · {len(found)} annonces")
                except VintedUnavailable as exc:
                    await progress(f"⏳ {exc}")
                    break
        pool = self.query_pool(list(found.values()), spec.query, niche, spec) if spec.query else None
        deals = await self.evaluate(list(found.values()), niche=niche, spec=spec, rs=rs, mode="hunt",
                                    progress=progress, pool=pool)
        deals = [d for d in deals if d.verdict is None or d.verdict.relevant]
        # Verify the market for the most promising few (everything except the market gate already passes,
        # or at least the money side does).
        shortlist = [d for d in deals if not [f for f in gate_fails(d, niche, rs, spec, require_market=False)
                                              if f.startswith(("profit", "remise", "prix <", "modèle"))]][:4]
        if shortlist:
            await progress(f"📊 Comparaison au marché en direct 0/{len(shortlist)}")
        checked = {}
        for i, d in enumerate(shortlist, 1):
            checked[d.listing.id] = await self.verify_market(d, niche, rs, spec)
            await progress(f"📊 Comparaison au marché en direct {i}/{len(shortlist)}")
        deals = [checked.get(d.listing.id, self._gate(d, niche, rs, spec)) for d in deals]
        deals.sort(key=lambda d: (d.pepite, d.score), reverse=True)
        await self.db.save_radar([d for d in deals if radar_worthy(d)])
        return deals, niche

    async def analyze_url(self, url: str, rs: RuntimeSettings, progress: Progress = _noop) -> Deal | None:
        self._interactive += 1                         # the user is waiting: pause background scanning
        try:
            return await self._analyze_url(url, rs, progress)
        finally:
            self._interactive -= 1

    async def _analyze_url(self, url: str, rs: RuntimeSettings, progress: Progress = _noop) -> Deal | None:
        await progress("🔗 Lecture de l'annonce…")
        listing = await self.vinted.listing_from_url(url)
        if not listing:
            return None
        if not listing.details_loaded:
            listing = await self.vinted.enrich(listing)
        niche = self.niches.guess(f"{listing.brand or ''} {listing.title}")
        deals = await self.evaluate([listing], niche=niche, spec=None, rs=rs, mode="full", progress=progress)
        deal = deals[0] if deals else await self._value(self._identify(listing, niche), niche, rs, None, None)
        await progress("📊 Comparaison avec les annonces identiques…")
        return await self.verify_market(deal, niche, rs, None)

    async def deepen(self, deal: Deal, rs: RuntimeSettings, *, expert: bool = False) -> Deal:
        """On-demand tier 4/5 for an alert (Telegram button)."""
        niche = self.niches.get(deal.listing.niche_id)
        market = await self.db.market_snapshot(deal.listing.model_key or "")
        verdict = await self.ai.deep(deal, niche, None, market, rs, expert=expert)
        await self.db.save_verdict(deal.listing.id, verdict)
        new = await self._value(deal.listing, niche, rs, None, verdict)
        return new.model_copy(update={"watch_id": deal.watch_id})

    async def price_check(self, query: str) -> dict:
        self._interactive += 1                         # the user is waiting: pause background scanning
        try:
            return await self._price_check(query)
        finally:
            self._interactive -= 1

    async def _price_check(self, query: str) -> dict:
        """Market snapshot for a free-text product: listed price distribution by condition."""
        found: dict[int, Listing] = {}
        for order, page in (("relevance", 1), ("relevance", 2), ("newest_first", 1)):
            for l in await self.vinted.search(SearchSpec(query=query), order=order, page=page):
                found.setdefault(l.id, l)
        niche = self.niches.guess(query)
        listings = [self._identify(l, niche) for l in found.values()]
        await self.db.upsert_listings(listings)
        listings = [l for l in listings if not reject_reason(l, niche)]
        ref = niche.identify(query) if niche else None
        if ref:
            # Search results drift (relevance search returns related items): keep the exact model only.
            same = [l for l in listings if l.model_key == f"{niche.id}:{ref.key}"]
            listings = same or listings
        by_cond: dict[Condition, list[float]] = defaultdict(list)
        for l in listings:
            by_cond[l.condition].append(l.price)
        rows = {}
        for cond, prices in sorted(by_cond.items(), key=lambda kv: -kv[0].value_factor):
            if len(prices) >= 2:
                med, q1, q3 = robust_center(prices)
                rows[cond.label] = {"n": len(prices), "median": med, "p25": q1, "p75": q3, "min": min(prices)}
        all_prices = [l.price for l in listings]
        sales = await self.db.sales(f"{niche.id}:{ref.key}") if ref else None
        return {
            "sales": sales,
            "query": query, "count": len(listings), "niche": niche.label if niche else None,
            "reference": ref.resale if ref else None, "reference_name": ref.name if ref else None,
            "median": statistics.median(all_prices) if all_prices else None,
            "by_condition": rows,
            "cheapest": sorted(listings, key=lambda l: l.price)[:3],
        }

    # ============================================================ continuous watching
    async def run_watch(self, watch: dict, notify: Notify) -> int:
        spec: SearchSpec = watch["spec"]
        niche = self.niches.get(spec.niche_id)
        chat_id = watch["chat_id"]
        rs = await self.db.runtime_settings(chat_id)

        if watch["kind"] == "niche" and niche:
            idx = self._query_cursor[watch["id"]] % len(niche.queries)
            self._query_cursor[watch["id"]] += 1
            sub = spec.model_copy(update={"query": niche.queries[idx]})
        else:
            sub = spec

        listings = await self.vinted.search(sub, order="newest_first", per_page=96)
        known = await self.db.known_prices([l.id for l in listings])
        fresh = [l for l in listings if l.id not in known or l.price < known[l.id] - 0.5]
        # First time we see a query and most results are unknown: it's the backlog, not new listings.
        seed_key = (watch["id"], sub.query)
        seeding = seed_key not in self._seeded and len(fresh) > 0.5 * max(len(listings), 1)
        self._seeded.add(seed_key)

        if seeding or not fresh:
            # First pass only builds the market memory - avoids a flood of stale alerts.
            await self.db.upsert_listings([self._identify(l, niche) for l in listings])
            await self.db.touch_watch(watch["id"], 0)
            return 0

        pool = self.query_pool(listings, sub.query, niche, spec)
        deals = await self.evaluate(fresh, niche=niche, spec=spec, rs=rs, mode="watch", pool=pool)
        await self.db.save_radar([d for d in deals if radar_worthy(d)])
        hits = verified = 0
        for d in deals:
            if d.listing.id in known:
                d.flags.append("baisse_prix")
            unknown = d.listing.model_key is None or d.listing.model_key.startswith("sig:")
            if watch["kind"] == "niche" and unknown and not (d.verdict and d.verdict.tier >= 2 and d.verdict.relevant):
                continue   # niche feeds drift (cardigans in "outdoor", games in "consoles"): no model, no alert
            if await self.db.was_alerted(d.listing.id, chat_id):
                continue
            if rs.alert_mode == "pepites":
                # Cheap gates first; the live market check (2 requests) only for real contenders.
                if gate_fails(d, niche, rs, spec, require_market=False) or verified >= 3:
                    continue
                verified += 1
                d = await self.verify_market(d, niche, rs, spec)
                if radar_worthy(d):
                    await self.db.save_radar([d])
                if not d.pepite:
                    log.info("near miss %s (%s): %s", d.listing.id, d.listing.title[:40], "; ".join(d.gate_fails))
                    await self.db.track([d.listing], priority=1)
                    self._near_miss[d.listing.id] = (chat_id, watch["id"])
                    while len(self._near_miss) > 5000:            # bounded memory
                        self._near_miss.pop(next(iter(self._near_miss)))
                    continue
            elif not is_alert_worthy(d, rs, niche, spec):
                continue
            d = d.model_copy(update={"watch_id": watch["id"]})
            await self.db.save_deal(d, chat_id)
            await notify(chat_id, d)
            hits += 1
        await self.db.touch_watch(watch["id"], hits)
        return hits

    def _interval(self, watch_id: int) -> float:
        """Adaptive polling: a niche that keeps producing good deals is polled up to 2x more often,
        one that produced nothing for a while up to 2x less (the Vinted request budget goes where the money is)."""
        y = self._yield.get(watch_id, 1.0)
        return self.s.watch_interval_s * (0.5 if y >= 2 else 0.75 if y >= 0.8 else 1.0 if y >= 0.2 else 2.0)

    async def _radar_count(self) -> int:
        row = await self.db._one("SELECT COUNT(*) AS n FROM radar")
        return row["n"] if row else 0

    async def run_tracker(self, notify: Notify, batch: int = 6) -> int:
        """Re-check a few tracked listings: record sales, and re-evaluate near misses whose price dropped."""
        changed = 0
        for row in await self.db.due_for_check(batch):
            try:
                status, price = await self.vinted.item_status(row["listing_id"])
            except VintedUnavailable:
                break
            await self.db.update_tracking(row["listing_id"], status, price)
            changed += status != "active"
            dropped = price is not None and row["last_price"] and price < row["last_price"] - 0.5
            target = self._near_miss.get(row["listing_id"])
            if dropped and status == "active" and target:
                listing = await self.db.get_listing(row["listing_id"])
                niche = self.niches.get(row["niche_id"])
                if listing:
                    chat_id, watch_id = target
                    rs = await self.db.runtime_settings(chat_id)
                    listing = listing.model_copy(update={"price": price})
                    await self.db.upsert_listings([listing])
                    deal = await self._value(listing, niche, rs, None, await self.db.get_verdict(listing.id))
                    deal = await self.verify_market(deal.model_copy(update={"flags": deal.flags + ["baisse_prix"]}),
                                                    niche, rs, None)
                    if deal.pepite and not await self.db.was_alerted(listing.id, chat_id):
                        deal = deal.model_copy(update={"watch_id": watch_id})
                        await self.db.save_deal(deal, chat_id)
                        await notify(chat_id, deal)
                        log.info("price drop turned %s into a pépite", listing.id)
        return changed

    async def run_forever(self, notify: Notify) -> None:
        log.info("Watch loop started")
        while True:
            if self._interactive:                              # a user search is running: give it Vinted
                await asyncio.sleep(1)
                continue
            if time.monotonic() - self._last_cleanup > 86400:    # daily retention
                self._last_cleanup = time.monotonic()
                try:
                    log.info("database cleanup: %s", await self.db.cleanup())
                except Exception:
                    log.exception("cleanup failed")
            if time.monotonic() - self._last_tracker > 600:     # sales database: 6 checks / 10 min
                self._last_tracker = time.monotonic()
                try:
                    if n := await self.run_tracker(notify):
                        log.info("sales tracker: %s listing(s) changed status", n)
                except Exception:
                    log.exception("sales tracker failed")
            try:
                for w in await self.db.watches(active_only=True):
                    if self._interactive:
                        break
                    if time.monotonic() - self._last_run.get(w["id"], 0) < self._interval(w["id"]):
                        continue
                    self._last_run[w["id"]] = time.monotonic()
                    try:
                        before = await self._radar_count()
                        hits = await self.run_watch(w, notify)
                        found = await self._radar_count() - before + 3 * hits
                        self._yield[w["id"]] = 0.7 * self._yield.get(w["id"], 1.0) + 0.3 * found
                        if hits:
                            log.info("watch %s (%s): %s alert(s)", w["id"], w["name"], hits)
                    except VintedUnavailable as exc:
                        log.warning("%s", exc)
                        await asyncio.sleep(self.vinted.cooldown_remaining + 1)
                    except Exception:
                        log.exception("watch %s failed", w["id"])
            except Exception:
                log.exception("watch loop iteration failed")
            await asyncio.sleep(3)
