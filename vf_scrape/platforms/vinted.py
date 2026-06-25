from __future__ import annotations

from typing import Callable, List, Optional

from ..base import PlatformScraper
from ..config import env_flag, env_int, profile
from ..types import PlatformScrapeStats, RawListing


def _vinted_fast_mode() -> bool:
    import os

    fast_env = os.environ.get("VF_FAST_SCRAPE", "auto").strip().lower()
    if fast_env in ("0", "false", "no", "off"):
        return False
    if fast_env in ("1", "true", "yes", "on"):
        return True
    return False


class VintedScraper(PlatformScraper):
    name = "vinted"

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
        from vf.market import DEEP_SCRAPE, VintedAdapter, fetch_vinted_deep

        st = self._stat()
        q = (query or "").strip()
        if not q:
            return [], st

        prof = profile("vinted")
        cap = max_items or env_int("VF_SCRAPE_VINTED_MAX", prof.default_max)
        fast = _vinted_fast_mode()
        self.log.info("Vinted PRO — %r cap=%d deep=%s fast=%s", q, cap, DEEP_SCRAPE, fast)

        ad = self._adapter()
        assert isinstance(ad, VintedAdapter)

        def _page(p: int, tp: int, n: int) -> None:
            self.log.debug("  page %d/%d — %d", p, tp, n)
            if on_page:
                on_page(p, tp, n)

        try:
            if (
                not getattr(ad, "_bootstrapped", False)
                and hasattr(ad, "_session_ready")
                and not ad._session_ready()
                and self._session is None
            ):
                self.log.info("  bootstrap session (fallback)…")
                ad.bootstrap("https://www.vinted.fr", headless=self.headless)
            elif getattr(ad, "_bootstrapped", False) or (hasattr(ad, "_session_ready") and ad._session_ready()):
                self.log.debug("  session Vinted réutilisée")

            items = fetch_vinted_deep(
                ad,
                q,
                category_hint=category_hint or "generic",
                max_items=cap,
                price_from=int(price_min or 0),
                price_to=int(price_max or 0),
                on_page=_page,
                log_fn=lambda m: self.log.info(m),
                fast_mode=fast,
            )
            rows = self._from_catalog_items(items, search_terms=q, variant="pro")
            st.raw = len(rows)
            st.unique = len(rows)
            self.log.info("Vinted OK — %d annonces", len(rows))
            return rows, st
        except Exception as exc:
            st.errors = 1
            st.last_error = str(exc)
            self.log.warning("Vinted échec: %s", exc)
            return [], st
