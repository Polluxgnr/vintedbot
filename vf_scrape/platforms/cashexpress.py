from __future__ import annotations

import time
from typing import Callable, List, Optional

from ..base import PlatformScraper
from ..config import env_int, profile
from ..types import PlatformScrapeStats, RawListing


class CashExpressScraper(PlatformScraper):
    name = "cashexpress"

    def _session_ready(self, ad: object) -> bool:
        if not getattr(ad, "_bootstrapped", False):
            return False
        boot_at = float(getattr(ad, "_bootstrap_at", 0) or 0)
        if not boot_at or time.monotonic() - boot_at >= 300:
            return False
        if hasattr(ad, "_driver_alive") and ad._driver_alive():  # type: ignore[attr-defined]
            return True
        cache = getattr(ad, "_html_cache", None)
        if isinstance(cache, dict) and cache:
            return True
        if hasattr(ad, "_probe_ok"):
            return bool(ad._probe_ok())  # type: ignore[attr-defined]
        return False

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
        import os

        st = self._stat()
        skip = os.environ.get("VF_SKIP_CASHEXPRESS", "auto").strip().lower()
        if skip in ("1", "true", "yes"):
            st.skipped = True
            self.log.info("Cash Express ignoré (VF_SKIP_CASHEXPRESS=1)")
            return [], st

        q = (query or "").strip()
        if not q:
            return [], st

        prof = profile("cashexpress")
        cap = max_items or env_int("VF_SCRAPE_CEX_MAX", prof.default_max)
        self.log.info("Cash Express PRO — %r cap=%d", q, cap)
        ad = self._adapter()

        try:
            if self._session_ready(ad):
                self.log.info("  réutilise session bootstrap")
            elif hasattr(ad, "ensure_session"):
                ad.ensure_session(q, headless=self.headless)  # type: ignore[attr-defined]
            else:
                self.log.warning("Cash Express : session non initialisée")

            if getattr(ad, "_browser_gave_up", False) and skip == "auto":
                st.skipped = True
                st.last_error = "anti-bot Cash Express — auto-skip (VF_SKIP_CASHEXPRESS=1 pour forcer)"
                self.log.warning(st.last_error)
                return [], st

            flt: dict = {}
            if price_min:
                flt["price_min"] = int(price_min)
            if price_max:
                flt["price_max"] = int(price_max)
            items = ad.fetch_catalog(
                ad.build_search_url(q, filters=flt or None),
                max_items=cap,
                on_page=on_page,
                category_hint=category_hint,
            )
            rows = self._from_catalog_items(items, search_terms=q)
            if price_min or price_max:
                from ..pro import filter_rows_by_price

                rows, dropped = filter_rows_by_price(rows, price_min=price_min, price_max=price_max)
                if dropped:
                    self.log.info("  hors fourchette prix : -%d", dropped)
            st.raw = len(rows)
            st.unique = len(rows)
            if not rows:
                st.errors = 1
                st.last_error = "0 annonces — recherche Cash Express sans résultat"
                self.log.warning("Cash Express : 0 annonces")
            else:
                self.log.info("Cash Express OK — %d annonces", len(rows))
            return rows, st
        except Exception as exc:
            st.errors = 1
            st.last_error = str(exc)
            self.log.warning("Cash Express échec: %s", exc)
            return [], st
