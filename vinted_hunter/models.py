"""Domain models shared by every layer."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


class Condition(IntEnum):
    """Vinted status ids (also used as search filter `status_ids[]`)."""

    NEW_WITH_TAGS = 6
    NEW = 1
    VERY_GOOD = 2
    GOOD = 3
    SATISFACTORY = 4
    UNKNOWN = 0

    @property
    def label(self) -> str:
        return _CONDITION_LABELS[self]

    @property
    def value_factor(self) -> float:
        """Resale value relative to a 'very good' item."""
        return _CONDITION_FACTORS[self]

    @classmethod
    def parse(cls, raw: str | int | None) -> Condition:
        if raw is None:
            return cls.UNKNOWN
        if isinstance(raw, int):
            try:
                return cls(raw)
            except ValueError:
                return cls.UNKNOWN
        text = raw.lower()
        for needle, cond in _CONDITION_ALIASES:
            if needle in text:
                return cond
        return cls.UNKNOWN


_CONDITION_LABELS = {
    Condition.NEW_WITH_TAGS: "Neuf avec étiquette",
    Condition.NEW: "Neuf sans étiquette",
    Condition.VERY_GOOD: "Très bon état",
    Condition.GOOD: "Bon état",
    Condition.SATISFACTORY: "Satisfaisant",
    Condition.UNKNOWN: "État inconnu",
}
_CONDITION_FACTORS = {
    Condition.NEW_WITH_TAGS: 1.18,
    Condition.NEW: 1.08,
    Condition.VERY_GOOD: 1.0,
    Condition.GOOD: 0.86,
    Condition.SATISFACTORY: 0.68,
    Condition.UNKNOWN: 0.9,
}
# Order matters: most specific first.
_CONDITION_ALIASES = [
    ("avec étiquette", Condition.NEW_WITH_TAGS), ("with tags", Condition.NEW_WITH_TAGS),
    ("mit etikett", Condition.NEW_WITH_TAGS),
    ("sans étiquette", Condition.NEW), ("without tags", Condition.NEW), ("ohne etikett", Condition.NEW),
    ("très bon", Condition.VERY_GOOD), ("very good", Condition.VERY_GOOD), ("sehr gut", Condition.VERY_GOOD),
    ("satisfaisant", Condition.SATISFACTORY), ("satisfactory", Condition.SATISFACTORY),
    ("zufriedenstellend", Condition.SATISFACTORY),
    ("bon", Condition.GOOD), ("good", Condition.GOOD), ("gut", Condition.GOOD),
    ("neuf", Condition.NEW), ("new", Condition.NEW), ("neu", Condition.NEW),
]


class Seller(BaseModel):
    id: int
    login: str = ""
    business: bool = False
    rating: float | None = None        # 0-5
    reviews: int | None = None
    items_listed: int | None = None
    on_holiday: bool = False
    country: str | None = None
    last_seen_at: datetime | None = None

    @property
    def trust(self) -> float:
        """0..1 trust heuristic. Unknown sellers are neutral-ish, not zero."""
        if self.rating is None or self.reviews is None:
            return 0.55
        volume = min(1.0, self.reviews / 50)
        return max(0.0, min(1.0, (self.rating / 5) * (0.5 + 0.5 * volume)))


class Listing(BaseModel):
    id: int
    title: str
    price: float
    currency: str = "EUR"
    total_price: float | None = None   # price incl. buyer protection, when the API gives it
    brand: str | None = None
    size: str | None = None
    condition: Condition = Condition.UNKNOWN
    url: str
    photos: list[str] = Field(default_factory=list)
    favourites: int = 0
    views: int = 0
    promoted: bool = False
    seller: Seller
    description: str | None = None
    posted_at: datetime | None = None
    category_id: int | None = None
    # Filled by the engine
    niche_id: str | None = None
    model_key: str | None = None
    model_name: str | None = None
    details_loaded: bool = False

    @property
    def age_minutes(self) -> float | None:
        if not self.posted_at:
            return None
        return (datetime.now(timezone.utc) - self.posted_at).total_seconds() / 60

    def text_blob(self) -> str:
        return " ".join(filter(None, [self.title, self.brand, self.size, self.description]))


class Finding(BaseModel):
    kind: str                  # missing | defect | positive | scam
    label: str
    impact: float = 0.0        # fraction of value lost


class Inspection(BaseModel):
    findings: list[Finding] = Field(default_factory=list)

    @property
    def missing(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "missing"]

    @property
    def defects(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "defect"]

    @property
    def positives(self) -> list[str]:
        return [f.label for f in self.findings if f.kind == "positive"]

    @property
    def scams(self) -> list[str]:
        return [f.label for f in self.findings if f.kind == "scam"]

    @property
    def value_factor(self) -> float:
        factor = 1.0
        for f in self.findings:
            if f.kind in ("missing", "defect"):
                factor *= 1 - f.impact
        return max(0.2, round(factor, 3))


class SellerCheck(BaseModel):
    level: str                 # ok | moyen | risque
    score: float
    reasons: list[str] = Field(default_factory=list)

    @property
    def icon(self) -> str:
        return {"ok": "🟢", "moyen": "🟡", "risque": "🔴"}[self.level]



class MarketCheck(BaseModel):
    """Live comparison against the identical listings currently on sale."""

    n: int = 0                         # identical active listings found (same model)
    median: float | None = None
    p25: float | None = None
    cheaper: int = 0                   # identical listings cheaper than this one
    cheapest_other: float | None = None
    percentile: float | None = None    # share of identical listings that are more expensive (1.0 = cheapest)


class Recommendation(StrEnum):
    BUY = "BUY"
    NEGOTIATE = "NEGOTIATE"
    WATCH = "WATCH"
    SKIP = "SKIP"


class AIVerdict(BaseModel):
    """Merged output of the AI tiers. Each tier refines the previous one."""

    tier: int = 0                      # highest tier reached (2=triage, 3=vision, 4=deep)
    relevant: bool = True
    model_name: str | None = None
    resale_estimate: float | None = None
    authenticity_risk: float = 0.0     # 0..1
    condition_score: float | None = None  # 0..10, from photos
    red_flags: list[str] = Field(default_factory=list)
    positives: list[str] = Field(default_factory=list)
    summary: str = ""
    recommendation: Recommendation | None = None
    max_buy_price: float | None = None
    time_to_sell_days: float | None = None
    negotiation_message: str | None = None
    resale_title: str | None = None
    resale_description: str | None = None
    missing_parts: list[str] = Field(default_factory=list)
    adjust_pct: float | None = None    # AI's value adjustment for missing parts / defects (negative)
    new_price: float | None = None
    seller_ok: bool | None = None
    cost_usd: float = 0.0


class Valuation(BaseModel):
    fair_value: float | None = None    # expected resale price for THIS item's condition
    low: float | None = None
    high: float | None = None
    confidence: float = 0.0            # 0..1
    comps_count: int = 0
    sources: dict[str, float] = Field(default_factory=dict)
    new_price: float | None = None     # retail price new (prix neuf), when known
    base_value: float | None = None    # complete item in this condition, before missing parts/defects
    sold_count: int = 0                # confirmed sales of this model in the sales database
    days_to_sell: float | None = None  # median days a listing of this model stays on sale before selling
    sell_through: float | None = None  # share of tracked listings that sold


class Deal(BaseModel):
    listing: Listing
    valuation: Valuation
    landed_cost: float
    profit: float | None
    margin_pct: float | None
    score: int
    flags: list[str] = Field(default_factory=list)
    verdict: AIVerdict | None = None
    watch_id: int | None = None
    inspection: Inspection | None = None
    seller_check: SellerCheck | None = None
    score_parts: dict[str, float] = Field(default_factory=dict)
    market_check: MarketCheck | None = None
    pepite: bool = False               # passed every gate of the "insane deals only" filter
    gate_fails: list[str] = Field(default_factory=list)

    @property
    def is_scam_suspect(self) -> bool:
        return "prix_suspect" in self.flags or (self.verdict is not None and self.verdict.authenticity_risk >= 0.7)


class SearchSpec(BaseModel):
    """A search, either one-shot (/hunt) or persisted as a watch."""

    query: str = ""
    niche_id: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    conditions: list[Condition] = Field(default_factory=list)
    brand_ids: list[int] = Field(default_factory=list)
    catalog_ids: list[int] = Field(default_factory=list)
    size_hint: str | None = None
    goal: str = "resell"               # "resell" | "personal"
    min_profit: float | None = None
    notes: str | None = None           # free text constraints for the AI ("pas de rayures", "boîte d'origine"...)

    def describe(self) -> str:
        parts = [f"« {self.query} »" if self.query else "niche"]
        if self.price_max:
            parts.append(f"≤ {self.price_max:.0f} €")
        if self.conditions:
            parts.append("/".join(c.label for c in self.conditions))
        if self.size_hint:
            parts.append(f"taille {self.size_hint}")
        if self.goal == "resell" and self.min_profit:
            parts.append(f"profit ≥ {self.min_profit:.0f} €")
        return " · ".join(parts)
