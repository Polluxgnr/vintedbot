from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RawListing:
    """Annonce brute normalisée — entrée de la partie 2 (vf)."""

    platform: str
    platform_item_id: str
    title: str
    price: Optional[float] = None
    total_price: Optional[float] = None
    currency: str = "EUR"
    url: str = ""
    photo_url: Optional[str] = None
    seller_name: str = ""
    seller_feedback_count: int = 0
    seller_rating: float = 0.0
    brand: str = ""
    size: str = ""
    condition: str = ""
    shipping_available: Optional[bool] = None
    hand_delivery_only: bool = False
    delivery_mode: str = ""
    description: str = ""
    listed_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    search_terms: str = ""
    scrape_variant: str = ""

    def to_catalog_item(self) -> Any:
        from vf.market import CatalogItem

        return CatalogItem(
            id=int(self.platform_item_id) if str(self.platform_item_id).isdigit() else hash(self.platform_item_id) % 10_000_000,
            title=self.title[:240],
            price=self.price,
            total_price=self.total_price or self.price,
            currency=self.currency,
            url=self.url,
            photo_url=self.photo_url,
            platform=self.platform,
            platform_item_id=str(self.platform_item_id),
            seller_feedback_count=int(self.seller_feedback_count or 0),
            seller_rating=float(self.seller_rating or 0.0),
            description=self.description[:4000] if self.description else None,
        )


@dataclass
class PlatformScrapeStats:
    platform: str
    raw: int = 0
    unique: int = 0
    variants: int = 0
    errors: int = 0
    skipped: bool = False
    last_error: str = ""
    duration_ms: int = 0
    log_lines: List[str] = field(default_factory=list)


@dataclass
class ScrapeBatchResult:
    listings: List[RawListing]
    stats: Dict[str, PlatformScrapeStats]
    duplicates_removed: int = 0
