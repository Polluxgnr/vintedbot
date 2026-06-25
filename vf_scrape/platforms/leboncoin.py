from __future__ import annotations

import time
from typing import Callable, List, Optional

from ..base import PlatformScraper
from ..config import env_flag, env_int, platform_time_budget_sec, profile
from ..pro import bands_for_category
from ..types import PlatformScrapeStats, RawListing


class LeBonCoinScraper(PlatformScraper):
    name = "leboncoin"

    def _fetch_band_with_retry(self, ad: object, url: str, *, max_items: int, on_page=None) -> list:
        retries = max(1, env_int("VF_LBC_BAND_RETRIES", 3))
        base_wait = max(0.4, float(env_int("VF_LBC_RETRY_BASE_MS", 900)) / 1000.0)
        block_wait = max(4.0, float(env_int("VF_LBC_BLOCK_RETRY_WAIT_MS", 9000)) / 1000.0)
        block_retried = False
        for attempt in range(retries):
            try:
                batch = ad.fetch_catalog(url, max_items=max_items, on_page=on_page)  # type: ignore[attr-defined]
                if batch:
                    return batch
                if getattr(ad, "_session_blocked", False) and not block_retried:
                    block_retried = True
                    cooldown = float(getattr(ad, "_bootstrap_cooldown_until", 0) or 0)
                    if cooldown and time.monotonic() < cooldown:
                        self.log.warning("  Datadome 403 — cooldown IP actif, arrêt bandes")
                        return []
                    self.log.warning("  Datadome 403 détecté — pas de rebootstrap agressif")
                    time.sleep(block_wait)
                    if hasattr(ad, "invalidate_session"):
                        ad.invalidate_session(clear_persisted=True)  # type: ignore[attr-defined]
                    return []
                return batch
            except Exception as exc:
                msg = str(exc)
                transient = isinstance(exc, ConnectionResetError) or any(
                    x in msg.lower()
                    for x in (
                        "connectionreset",
                        "connection aborted",
                        "temporarily unavailable",
                        "403",
                        "datadome",
                    )
                )
                if attempt + 1 >= retries or not transient:
                    raise
                wait_s = base_wait * (2**attempt)
                self.log.warning("  bande retry %d/%d après erreur réseau: %s", attempt + 1, retries, exc)
                time.sleep(wait_s)
        return []

    def _session_ready(self, ad: object) -> bool:
        if not getattr(ad, "_bootstrapped", False) or not getattr(ad, "session", None):
            return False
        if not ad.session.cookies:  # type: ignore[attr-defined]
            return False
        if getattr(ad, "_session_blocked", False):
            return False
        if hasattr(ad, "_probe_api_ok"):
            return bool(ad._probe_api_ok(force=True))  # type: ignore[attr-defined]
        return True

    def scrape_query(
        self,
        query: str,
        *,
        price_min: Optional[int] = None,
        price_max: Optional[int] = None,
        max_items: int = 0,
        category_hint: str = "",
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> tuple[List[RawListing], PlatformScrapeStats]:
        from vf.scoring import filter_stale_catalog_items

        st = self._stat()
        q = (query or "").strip()
        if not q:
            return [], st

        prof = profile("leboncoin")
        cap = max_items or env_int("VF_SCRAPE_LBC_MAX", prof.default_max)
        deep = env_flag(prof.deep_env, "1") and not env_flag("VF_LBC_FAST", "0")
        fast = env_flag("VF_LBC_FAST", "0") or not deep
        band_pause = float(env_int("VF_LBC_BAND_PAUSE_MS", 1200 if not fast else 400)) / 1000.0
        healthy_pause = float(env_int("VF_LBC_HEALTHY_BAND_PAUSE_MS", 350 if not fast else 150)) / 1000.0
        t0 = time.monotonic()
        plat_budget = platform_time_budget_sec("leboncoin")
        self.log.info("LeBonCoin PRO — %r cap=%d deep=%s fast=%s", q, cap, deep, fast)

        ad = self._adapter()
        if hasattr(ad, "ensure_session"):
            if not ad.ensure_session(q, headless=self.headless):  # type: ignore[attr-defined]
                st.errors = 1
                st.last_error = "Datadome — session non disponible (IP restreinte)"
                self.log.warning(st.last_error)
                return [], st
        elif hasattr(ad, "_load_persisted_cookies"):
            ad._load_persisted_cookies()  # type: ignore[attr-defined]
        if self._session_ready(ad):
            self.log.info("  session API OK")

        if fast or not deep:
            bands = bands_for_category({}, category_hint, price_min=price_min, price_max=price_max)
        else:
            bands = bands_for_category(
                prof.price_bands,
                category_hint,
                price_min=price_min,
                price_max=price_max,
                fast=fast,
            )

        all_items: list = []
        seen: set[str] = set()
        for bi, (lo, hi) in enumerate(bands):
            if plat_budget and (time.monotonic() - t0) >= plat_budget:
                self.log.info("  budget LBC %.0fs — arrêt partiel (%d annonces)", plat_budget, len(all_items))
                break
            if len(all_items) >= cap:
                break
            pause_s = healthy_pause if self._session_ready(ad) and not getattr(ad, "_session_blocked", False) else band_pause
            if bi > 0 and pause_s > 0:
                time.sleep(pause_s)
            if getattr(ad, "_session_blocked", False):
                self.log.warning("  session bloquée — arrêt bandes (pas de rebootstrap mid-variant)")
                break
            band_cap = max(120, cap // max(1, len(bands)))
            if deep and len(bands) > 1:
                self.log.debug("  bande %d-%d EUR", lo, hi)
            url = ad.build_search_url(q, filters={"price_min": lo, "price_max": hi})
            batch = self._fetch_band_with_retry(ad, url, max_items=band_cap, on_page=on_page)
            if not batch and bi == 0:
                if getattr(ad, "_session_blocked", False):
                    st.errors = 1
                    st.last_error = "IP Datadome bloquée — pause 20-30 min"
                    return [], st
            for it in batch:
                pid = str(getattr(it, "platform_item_id", None) or getattr(it, "id", ""))
                if pid in seen:
                    continue
                seen.add(pid)
                all_items.append(it)

        if not all_items:
            st.errors = 1
            st.last_error = "0 annonce — Datadome / IP restreinte"
            self.log.warning(st.last_error)
            return [], st

        max_age = env_int("VF_LBC_MAX_AGE_DAYS", 180 if fast else 365)
        all_items, dropped = filter_stale_catalog_items(all_items, max_age_days=max_age)
        if dropped:
            self.log.info("  filtre age : -%d (> %dj)", dropped, max_age)

        from vf.engine import filter_hand_delivery_catalog_items

        all_items, hand_dropped = filter_hand_delivery_catalog_items(all_items)
        if hand_dropped:
            self.log.info("  filtre livraison : -%d (main propre uniquement)", hand_dropped)

        rows = self._from_catalog_items(all_items, search_terms=q, variant="pro")
        st.raw = len(rows)
        st.unique = len(rows)
        self.log.info("LeBonCoin OK — %d annonces", len(rows))
        return rows, st
