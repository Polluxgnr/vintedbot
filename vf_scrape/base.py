from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional  # noqa: F401 — Any used by prefetched_adapter

from .log import setup_scrape_logging
from .types import PlatformScrapeStats, RawListing


class PlatformScraper(ABC):
    name: str = "unknown"

    def __init__(self, *, headless: bool = True, verbose: bool = False) -> None:
        self.headless = headless
        self.verbose = verbose
        self.log = setup_scrape_logging(verbose=verbose, platform=self.name)
        self.prefetched_adapter: Any = None
        self._session: Any = None

    def expand_queries(self, query: str, category_hint: str = "") -> List[str]:
        from .expand import expand_queries

        return expand_queries(query, platform=self.name, category_hint=category_hint)

    def _adapter(self) -> Any:
        if self.prefetched_adapter is not None:
            return self.prefetched_adapter
        if self._session is not None:
            return self._session.adapter(self.name)
        from vf.engine import adapter_headless
        from vf.market import get_adapter

        h = adapter_headless(self.name, run_headless=self.headless)
        return get_adapter(self.name, headless=h)

    def _stat(self) -> PlatformScrapeStats:
        return PlatformScrapeStats(platform=self.name)

    @abstractmethod
    def scrape_query(
        self,
        query: str,
        *,
        price_min: Optional[int] = None,
        price_max: Optional[int] = None,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> tuple[List[RawListing], PlatformScrapeStats]:
        ...

    def _from_catalog_items(
        self,
        items: List[Any],
        *,
        search_terms: str = "",
        variant: str = "",
    ) -> List[RawListing]:
        out: List[RawListing] = []
        for it in items:
            pid = str(getattr(it, "platform_item_id", None) or getattr(it, "id", ""))
            raw_obj = getattr(it, "raw", None)
            raw = raw_obj if isinstance(raw_obj, dict) else {}
            seller_name = str(
                getattr(it, "seller_username", None)
                or getattr(it, "seller_name", None)
                or raw.get("sellerName")
                or raw.get("seller_name")
                or ""
            )
            listed_at = str(getattr(it, "listed_at", None) or raw.get("listed_at") or raw.get("date") or "")
            description = str(getattr(it, "description", None) or raw.get("description") or raw.get("body") or "")
            loc_obj = raw.get("location") if isinstance(raw.get("location"), dict) else {}
            location = str(
                getattr(it, "location", None)
                or raw.get("location_label")
                or loc_obj.get("city_label")
                or loc_obj.get("city")
                or loc_obj.get("region_name")
                or ""
            )
            condition = str(getattr(it, "condition", None) or raw.get("condition") or "")
            brand = str(getattr(it, "brand", None) or raw.get("brand") or raw.get("brand_name") or "")
            size = str(getattr(it, "size", None) or raw.get("size") or raw.get("size_title") or "")
            ship_val = getattr(it, "shipping_available", None)
            if ship_val is None:
                ship_val = raw.get("shipping_available")
            hand_only = bool(getattr(it, "hand_delivery_only", False) or raw.get("hand_delivery_only"))
            delivery_mode = str(getattr(it, "delivery_mode", None) or raw.get("delivery_mode") or "")
            if brand or location or condition or size:
                raw = {**raw, "brand": brand, "location": location, "condition": condition, "size": size}
            if ship_val is not None:
                raw = {**raw, "shipping_available": ship_val}
            if hand_only or delivery_mode:
                raw = {**raw, "hand_delivery_only": hand_only, "delivery_mode": delivery_mode}
            out.append(
                RawListing(
                    platform=str(getattr(it, "platform", None) or self.name),
                    platform_item_id=pid,
                    title=str(getattr(it, "title", "") or ""),
                    price=getattr(it, "price", None),
                    total_price=getattr(it, "total_price", None) or getattr(it, "price", None),
                    currency=str(getattr(it, "currency", None) or "EUR"),
                    url=str(getattr(it, "url", "") or ""),
                    photo_url=getattr(it, "photo_url", None),
                    description=description,
                    seller_name=seller_name,
                    seller_feedback_count=int(getattr(it, "seller_feedback_count", 0) or 0),
                    seller_rating=float(getattr(it, "seller_rating", 0) or 0.0),
                    brand=brand,
                    size=size,
                    condition=condition,
                    shipping_available=ship_val if isinstance(ship_val, bool) else None,
                    hand_delivery_only=hand_only,
                    delivery_mode=delivery_mode,
                    listed_at=listed_at,
                    search_terms=search_terms,
                    scrape_variant=variant,
                    raw=raw,
                )
            )
        return out

    def timed(
        self,
        fn: Callable[[], tuple[List[RawListing], PlatformScrapeStats]],
    ) -> tuple[List[RawListing], PlatformScrapeStats]:
        t0 = time.monotonic()
        rows, st = fn()
        st.duration_ms = int((time.monotonic() - t0) * 1000)
        return rows, st
