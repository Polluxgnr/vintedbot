"""Niche catalogue: curated search queries, reference models and resale prices.

Each niche encodes reseller knowledge (what sells, at what price, what fakes look like)
so the free tiers of the cascade can already do most of the filtering.
"""

from __future__ import annotations

import re
import unicodedata
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

STOPWORDS = _STOPWORDS = set(
    "de du des la le les un une et en pour avec sans tres bon etat neuf taille t homme femme enfant "
    "the a an and for with new size men women vintage original authentique lot".split()
)
_SIZE_RE = re.compile(r"^(?:\d{2}(?:[.,]5)?|x{0,3}[sml]|xxl|\d+(?:go|gb|tb|to|mm|cm|ml))$")

# Hard junk filters applied to every niche (normalized text, word-boundary regex)
GLOBAL_EXCLUDES = [
    r"pour pieces?", r"for parts", r"\bhs\b", r"ne (?:fonctionne|marche) (?:pas|plus)", r"en panne",
    r"boite (?:vide|seule)", r"empty box", r"\bje recherche\b", r"\brecherche\b.*\bachat\b",
    r"\breplica\b", r"\brepro\b", r"\bfake\b", r"\bcopie\b", r"\bdupe\b", r"\b1 1\b", r"\binspired\b",
    r"\bfaux\b", r"\bcontrefacon\b", r"\bfactice\b", r"\bdisplay only\b", r"\bmaquette\b",
    r"\bicloud\b.*\b(?:bloque|lock)", r"\bbloque icloud\b",
    # wanted ads: "je cherche ps5", "cerco ps5", "busco", "suche", "WTB"
    r"\b(?:je cherche|cherche|cerco|busco|suche|looking for|wtb)\b", r"^recherche\b", r"^achete\b",
]
# Listings selling only the box/manual/case of a coveted model ("Contax T2 box"). Rejected unless the
# title says the item comes *with* it ("Contax T2 avec boîte").
ACCESSORY_ONLY = re.compile(
    r"\b(?:boite|box|scatola|caja|karton|notice|manuel|manual|libretto|instructions?|mode d emploi|etui|custodia|"
    r"housse|strap|sangle|dragonne|data back|cache objectif|lens cap|pare soleil|chargeur|charger|cable|"
    r"coque|skin|sticker|autocollant|poster|affiche|figurine|porte cles|keychain)\b"
)
_WITH = re.compile(r"\b(?:avec|with|con|mit|inclus|included|complet|complete|fourni|offert|offerte|neuf|new|scelle|"
                   r"sealed|en boite|dans sa boite|in box|bnib|nib)\b|\+")
# Words that, in a description, deserve an AI look rather than an automatic rejection
# ("100% authentique, pas un fake" is common and legitimate).
SUSPICIOUS_WORDS = [
    r"\bfake\b", r"\breplica\b", r"\bcopie\b", r"\bsans facture\b", r"\bpas de facture\b", r"\bhs\b",
    r"\bfissure\b", r"\braye\b", r"\btache\b", r"\btrou\b", r"\bdefaut\b", r"\bmanque\b",
    r"\bpaiement (?:hors|en dehors)\b", r"\bwhatsapp\b", r"\bvirement\b", r"\bpaypal\b", r"\blydia\b",
]


def normalize(text: str) -> str:
    """lowercase, strip accents, collapse whitespace - used for every match."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+/ ]", " ", text.lower())).strip()


class RefModel(BaseModel):
    key: str
    name: str
    match: list[str]
    resale: float                     # typical resale price, very good condition, on Vinted
    new: float | None = None          # approximate retail price new (prix neuf)
    generic: bool = False             # catch-all model ("Stone Island (autre)"): never enough for a pépite
    exclude: list[str] = Field(default_factory=list)

    @cached_property
    def _patterns(self) -> list[re.Pattern]:
        return [re.compile(p) for p in self.match]

    @cached_property
    def _excludes(self) -> list[re.Pattern]:
        return [re.compile(p) for p in self.exclude]

    def matches(self, norm_text: str) -> bool:
        return any(p.search(norm_text) for p in self._patterns) and not any(
            p.search(norm_text) for p in self._excludes
        )


class Component(BaseModel):
    """A part a complete item ships with. `share` = fraction of the value lost when it's missing."""

    name: str
    match: str
    share: float


class AloneRule(BaseModel):
    """Phrases meaning 'only the main item' ("console seule") -> these components are missing."""

    match: str
    missing: list[str]


class Niche(BaseModel):
    id: str
    name: str
    emoji: str = "🎯"
    description: str = ""
    queries: list[str]
    catalog_ids: list[int] = Field(default_factory=list)
    price_min: float | None = None
    price_max: float | None = None
    shipping_eur: float = 5.0
    resale_fee_pct: float = 0.0       # Vinted sellers pay no fee; set ~13 for eBay-oriented niches
    min_profit: float | None = None
    authenticity_risk: str = "low"    # low | medium | high | very_high
    exclude: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)  # global excludes this niche wants to keep (e.g. "hs" for repairers)
    red_flags: list[str] = Field(default_factory=list)
    vision_checklist: list[str] = Field(default_factory=list)
    ai_hints: str = ""
    models: list[RefModel] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    model_guard: list[str] = Field(default_factory=list)  # title says it's another product type -> no model
    alone: list[AloneRule] = Field(default_factory=list)
    collectible: bool = False         # can resell above retail (sneakers, retro, TCG...) -> no retail cap
    enabled_by_default: bool = False

    @cached_property
    def _exclude_patterns(self) -> list[re.Pattern]:
        globals_ = [p for p in GLOBAL_EXCLUDES if not any(a in p for a in self.allow)]
        return [re.compile(p) for p in globals_ + self.exclude]

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"

    @property
    def risk_level(self) -> float:
        return {"low": 0.1, "medium": 0.3, "high": 0.55, "very_high": 0.8}.get(self.authenticity_risk, 0.3)

    def excluded_by(self, norm_text: str) -> str | None:
        for p in self._exclude_patterns:
            if p.search(norm_text):
                return p.pattern
        if self.models and ACCESSORY_ONLY.search(norm_text) and not _WITH.search(norm_text):
            return "accessoire seul"
        return None

    def identify(self, title: str, description: str | None = None) -> RefModel | None:
        """Most specific reference model matching the title.

        Descriptions are deliberately ignored: sellers stuff them with keywords
        ("... CONTAX T2 YASHICA T4 ...") which would mis-price cheap items.
        """
        text = normalize(title)
        if any(re.search(g, text) for g in self.model_guard):
            return None   # a game, a controller, a card, a kids' item... not the reference product
        hits = [m for m in self.models if m.matches(text)]
        # Longest pattern wins: "airpods pro 2" beats "airpods pro"
        return max(hits, key=lambda m: max(len(p) for p in m.match)) if hits else None

    def keywords(self) -> set[str]:
        words = set()
        for q in self.queries:
            words.update(normalize(q).split())
        return words - _STOPWORDS


def signature_key(title: str, brand: str | None) -> str:
    """Fallback comparable key when no reference model matches: brand + 3 strongest tokens."""
    tokens = [t for t in normalize(title).split() if t not in _STOPWORDS and not _SIZE_RE.match(t) and len(t) > 1]
    b = normalize(brand or "")
    tokens = [t for t in tokens if t not in b.split()]
    return f"sig:{b or 'nobrand'}:{'-'.join(sorted(tokens[:3]))}"


class NicheCatalog:
    def __init__(self, niches: dict[str, Niche], defaults: dict):
        self.niches = niches
        self.defaults = defaults

    @classmethod
    def load(cls, path: Path) -> NicheCatalog:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        defaults = raw.get("defaults", {})
        niches = {}
        for nid, body in (raw.get("niches") or {}).items():
            niches[nid] = Niche(id=nid, **{**defaults, **body})
        return cls(niches, defaults)

    def get(self, niche_id: str | None) -> Niche | None:
        return self.niches.get(niche_id) if niche_id else None

    def __iter__(self):
        return iter(self.niches.values())

    def guess(self, query: str) -> Niche | None:
        """Best niche for a free-text query: reference model hit first, then keyword overlap."""
        norm = normalize(query)
        for niche in self:
            if niche.identify(query):
                return niche
        words = set(norm.split()) - _STOPWORDS
        best, best_score = None, 0
        for niche in self:
            score = len(words & niche.keywords())
            if score > best_score:
                best, best_score = niche, score
        return best
