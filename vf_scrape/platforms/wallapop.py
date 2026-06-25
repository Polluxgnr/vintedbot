from __future__ import annotations

from typing import Callable, List, Optional

from ..base import PlatformScraper
from ..config import env_flag, env_int, profile
from ..pro import bands_for_category
from ..types import PlatformScrapeStats, RawListing


class WallapopScraper(PlatformScraper):
    name = "wallapop"

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
        st = self._stat()
        q = (query or "").strip()
        if not q:
            return [], st

        from ..expand import title_matches_category

        prof = profile("wallapop")
        cap = max_items or env_int("VF_SCRAPE_WALLAPOP_MAX", prof.default_max)
        deep = env_flag(prof.deep_env, "1")
        self.log.info("Wallapop PRO — %r cap=%d deep=%s", q, cap, deep)
        ad = self._adapter()
        if hasattr(ad, "bootstrap"):
            try:
                ad.bootstrap(ad.build_search_url(q), headless=self.headless)
            except Exception as exc:
                self.log.debug("Wallapop bootstrap: %s", exc)

        try:
            all_items: list = []
            seen: set[str] = set()
            bands = (
                bands_for_category(prof.price_bands, category_hint, price_min=price_min, price_max=price_max)
                if deep
                else [(int(price_min or 0), int(price_max or 99999))]
            )
            for lo, hi in bands:
                if len(all_items) >= cap:
                    break
                band_cap = max(30, cap // max(1, len(bands)))
                flt = {}
                if lo:
                    flt["price_min"] = lo
                if hi < 99999:
                    flt["price_max"] = hi
                url = ad.build_search_url(q, filters=flt or None)
                batch = ad.fetch_catalog(url, max_items=band_cap, on_page=on_page)
                for it in batch:
                    title = str(getattr(it, "title", "") or "")
                    if not title_matches_category(title, category_hint):
                        continue
                    pid = str(getattr(it, "platform_item_id", None) or getattr(it, "id", ""))
                    if pid in seen:
                        continue
                    seen.add(pid)
                    all_items.append(it)

            rows = self._from_catalog_items(all_items, search_terms=q)
            st.raw = len(rows)
            st.unique = len(rows)
            self.log.info("Wallapop OK — %d annonces", len(rows))
            return rows, st
        except Exception as exc:
            st.errors = 1
            st.last_error = str(exc)
            self.log.warning("Wallapop échec: %s", exc)
            return [], st
