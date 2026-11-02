"""Precise searches without the AI: "tee carhartt moins de 10 € très bon état logo graphique",
"pack chaussettes boss neuf taille M unicolore pour moi"...

parse_request() turns free French/English text into hard filters (price, size, condition, must / must-not
words, personal vs resell). matches() applies them to a listing. Works offline; the AI intent parser, when
reachable, only adds to it."""

from __future__ import annotations

import re

from .models import Condition, Listing, SearchSpec
from .niches import normalize

# Qualitative wishes -> (must match one of, must match none of). Patterns run on normalized text.
QUALITIES: list[tuple[str, str | None, str | None, str]] = [
    # (trigger in the request, must, must_not, label)
    (r"logo graphique|graphic logo|gros logo|big logo|logo imprime|printed logo|graphique|graphic",
     r"logo|graphi\w*|print\w*|imprim\w*|motif|dessin|illustration", None, "logo graphique"),
    (r"unicolore?s?|uni\b|plain|solid colou?r|une seule couleur|single colou?r|juste le logo|only the logo|"
     r"logo seulement|seulement le logo",
     None, r"\bray(?:e|ee|es|ees|ure|ures)\b|stripe\w*|carreau\w*|motifs?\b|pattern\w*|camo\w*|multicolor\w*|all ?over|"
           r"imprime all|print all|tie ?dye|leopard|fleur\w*|floral", "unicolore"),
    (r"\bpacks?\b|\blots?\b|\bboite\b|multipack|pack de|lot de", r"\bpacks?\b|\blots?\b|\bx ?\d\b|\d ?x\b|\d paires|"
     r"multipack|boite", None, "pack / lot"),
    (r"vintage|90s|annees 90|retro", r"vintage|90s|90 s|annees 90|retro|y2k|old ?school", None, "vintage"),
    (r"avec boite|with box|boite d origine|en boite|complet", r"boite|box|complet\w*|emball\w*", None, "avec boîte"),
    (r"\bsans defaut|no flaws?|parfait etat", None, r"tache\w*|trou\w*|defaut\w*|accroc\w*|usure", "sans défaut"),
]

_SIZES = r"(xxs|xs|s|m|l|xl|xxl|xxxl|2xl|3xl|\d{2}(?:[.,]5)?|w\d{2}(?: ?l\d{2})?)"
_SIZE_RX = re.compile(rf"\b(?:taille|size|en|t\.?)\s*{_SIZES}\b(?! etat)")
_PRICE_MAX_RX = re.compile(r"(?:moins de|sous|under|max(?:imum)?|<|jusqu a|budget|a|pour)\s*(\d{1,5})\s*(?:e|eur|euros?)\b"
                           r"|(\d{1,5})\s*(?:e|eur|euros?)\s*(?:max|maximum)\b")
_PRICE_MIN_RX = re.compile(r"(?:plus de|au moins|over|min(?:imum)?|>)\s*(\d{1,5})\s*(?:e|eur|euros?)\b")
_CONDITIONS = [
    (r"neuf avec etiquette|new with tags|\bnwt\b|etiquette", [Condition.NEW_WITH_TAGS]),
    (r"\bneufs?\b|\bneuves?\b|\bnew\b|jamais porte|never worn", [Condition.NEW_WITH_TAGS, Condition.NEW]),
    (r"(?:super|tres|very) (?:bon|good) etat|very good|excellent etat|comme neuf|like new",
     [Condition.NEW_WITH_TAGS, Condition.NEW, Condition.VERY_GOOD]),
    (r"\bbon etat\b|good condition", [Condition.NEW_WITH_TAGS, Condition.NEW, Condition.VERY_GOOD, Condition.GOOD]),
]
_PERSONAL = re.compile(r"pour moi|perso\w*|for me\b|for myself|me porter|mon usage|ma taille|my size")
_ALERT = re.compile(r"alerte|previens|previens moi|notify|alert me|surveille|des qu|as soon as|en continu")
# Words that describe the wish, not the product: removed from the Vinted search text.
_FILLER = re.compile(r"\b(?:je (?:veux|cherche|voudrais)|i want|looking for|trouve(?:-| )moi|find me|toutes? les?|tous|tout|all|"
                     r"des|les|un|une|de|du|d|en|avec|with|et|and|ou|or|qui|that|sont|are|me|moi|pour|for|myself|"
                     r"perso\w*|state|etat|condition|super|tres|very|bon|good|alerte|previens|notify|alert|surveille|"
                     r"seulement|only|juste|just|le|la|the|a|an)\b")


def parse_request(text: str) -> dict:
    """Free text -> {"query", "price_min", "price_max", "sizes", "conditions", "goal", "must", "must_not",
    "labels", "watch"}. Everything not recognised as a constraint stays in the query."""
    t = normalize(text)
    out: dict = {"must": [], "must_not": [], "labels": [], "sizes": [], "conditions": []}
    rest = t
    if m := _PRICE_MAX_RX.search(t):
        out["price_max"] = float(m.group(1) or m.group(2))
        rest = rest.replace(m.group(0), " ")
    if m := _PRICE_MIN_RX.search(t):
        out["price_min"] = float(m.group(1))
        rest = rest.replace(m.group(0), " ")
    for m in _SIZE_RX.finditer(t):
        out["sizes"].append(m.group(1).upper())
        rest = rest.replace(m.group(0), " ")
    for rx, conds in _CONDITIONS:
        if m := re.search(rx, t):
            out["conditions"] = [c.value for c in conds]
            rest = rest.replace(m.group(0), " ")
            break
    for trigger, must, must_not, label in QUALITIES:
        if m := re.search(trigger, t):
            if label == "pack / lot":            # "pack" also belongs in the Vinted search itself
                pass
            else:
                rest = re.sub(trigger, " ", rest)
            if must:
                out["must"].append(must)
            if must_not:
                out["must_not"].append(must_not)
            out["labels"].append(label)
    out["goal"] = "personal" if _PERSONAL.search(t) else "resell"
    out["watch"] = bool(_ALERT.search(t))
    rest = re.sub(r"\b(?:tee ?shirts?|t ?shirts?|tees?)\b", " t-shirt ", rest)
    query = re.sub(r"\s+", " ", _FILLER.sub(" ", _PERSONAL.sub(" ", rest))).strip()
    out["query"] = query or t
    return out


def apply_to_spec(spec: SearchSpec, parsed: dict) -> SearchSpec:
    spec.price_min = spec.price_min or parsed.get("price_min")
    spec.price_max = spec.price_max or parsed.get("price_max")
    if parsed.get("conditions") and not spec.conditions:
        spec.conditions = [Condition(c) for c in parsed["conditions"]]
    if parsed.get("sizes"):
        spec.sizes = sorted(set(spec.sizes) | set(parsed["sizes"]))
        spec.size_hint = spec.size_hint or "/".join(parsed["sizes"])
    spec.must = list(dict.fromkeys(spec.must + parsed.get("must", [])))
    spec.must_not = list(dict.fromkeys(spec.must_not + parsed.get("must_not", [])))
    spec.labels = list(dict.fromkeys(spec.labels + parsed.get("labels", [])))
    if parsed.get("goal") == "personal":
        spec.goal = "personal"
    return spec


def _size_ok(size: str | None, wanted: list[str]) -> bool:
    if not wanted or not size:
        return True                          # unknown size: let it through, the card shows it
    tokens = set(re.findall(r"[a-z0-9]+", normalize(size)))
    return any(w.lower() in tokens for w in wanted)


def mismatch(listing: Listing, spec: SearchSpec | None) -> str | None:
    """Why a listing breaks the user's precise criteria (None = it fits)."""
    if not spec:
        return None
    if spec.sizes and not _size_ok(listing.size, spec.sizes):
        return f"taille {listing.size} ≠ {'/'.join(spec.sizes)}"
    text = normalize(f"{listing.title} {listing.description or ''} "
                     f"{(listing.translation or {}).get('title', '')} {(listing.translation or {}).get('text', '')}")
    for rx in spec.must_not:
        if re.search(rx, text):
            return "critère exclu dans l'annonce"
    if listing.details_loaded or listing.description:
        for rx in spec.must:
            if not re.search(rx, text):
                return "critère demandé absent de l'annonce"
    return None
