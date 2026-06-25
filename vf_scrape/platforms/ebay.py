from __future__ import annotations

import re
from typing import Callable, List, Optional

from ..base import PlatformScraper
from ..config import env_int, profile
from ..types import PlatformScrapeStats, RawListing


class EbayScraper(PlatformScraper):
    name = "ebay"

    @staticmethod
    def _normalize_item_id(row: RawListing) -> None:
        m = re.search(r"/itm/(?:[^/]+/)?(\d+)", row.url or "")
        if m:
            row.platform_item_id = m.group(1)
            return
        m2 = re.search(r"(/itm/[^/?#]+)", row.url or "")
        if m2:
            row.platform_item_id = m2.group(1).rstrip("/")

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
        del category_hint
        from vf.engine import adapter_headless

        st = self._stat()
        q = (query or "").strip()
        if not q:
            return [], st

        prof = profile("ebay")
        cap = max_items or env_int("VF_SCRAPE_EBAY_MAX", prof.default_max)
        headless = adapter_headless("ebay", run_headless=self.headless)
        self.log.info("eBay PRO — %r cap=%d headed=%s", q, cap, not headless)
        ad = self._adapter()
        flt: dict = {}
        if price_min:
            flt["price_min"] = price_min
        if price_max:
            flt["price_max"] = price_max
        url = ad.build_search_url(q, filters=flt or None)

        try:
            if getattr(ad, "_challenge_failed", False):
                st.skipped = True
                st.last_error = "captcha non résolu"
                self.log.warning("eBay skip — validez captcha (--headed)")
                return [], st
            warmed = bool(getattr(ad, "_session_warmed", False)) and not getattr(ad, "_challenge_failed", False)
            if not warmed:
                ad.bootstrap(url, headless=headless)
            if getattr(ad, "_challenge_failed", False):
                st.skipped = True
                st.last_error = "captcha bootstrap"
                return [], st
            items = ad.fetch_catalog(url, max_items=cap, on_page=on_page)
            rows = self._from_catalog_items(items, search_terms=q)
            for row in rows:
                self._normalize_item_id(row)
            if price_min or price_max:
                from ..pro import filter_rows_by_price

                rows, dropped = filter_rows_by_price(rows, price_min=price_min, price_max=price_max)
                if dropped:
                    self.log.info("  hors fourchette prix : -%d", dropped)
            st.raw = len(rows)
            st.unique = len(rows)
            if not rows:
                st.errors = 1
                st.last_error = "0 annonce — résolvez captcha eBay dans Chrome"
                self.log.warning(st.last_error)
            else:
                self.log.info("eBay OK — %d annonces", len(rows))
            return rows, st
        except Exception as exc:
            st.errors = 1
            st.last_error = str(exc)
            if hasattr(ad, "_challenge_failed"):
                ad._challenge_failed = True  # type: ignore[attr-defined]
            self.log.warning("eBay échec: %s", exc)
            return [], st
