"""Parsing de l'intention de recherche : familles produit, contraintes strictes, hints plateforme."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SearchIntent:
    raw: str = ""
    search_text: str = ""
    product_families: List[str] = field(default_factory=list)
    must_match_any: List[re.Pattern[str]] = field(default_factory=list)
    platform_hints: List[str] = field(default_factory=list)
    # Affinage strict (ex. New 3DS, marque vêtement, taille)
    required_ds_family: str = ""
    require_all_patterns: List[re.Pattern[str]] = field(default_factory=list)
    exclude_patterns: List[re.Pattern[str]] = field(default_factory=list)
    required_brand: str = ""
    apparel_size: str = ""
    preflight_flags: Dict[str, bool] = field(default_factory=dict)
    required_phone_model_key: str = ""
    required_storage_gb: int = 0
    required_exclude_pro: bool = False


# Catalogues Vinted FR → famille produit (source: site map / brouillon legacy)
VINTED_CATALOG_HINTS: Dict[str, Dict[str, str]] = {
    "3739": {"label": "Vidéoprojecteurs", "expected": "projector", "search_text": "videoprojecteur"},
    "2993": {"label": "Consoles", "expected": "console_ps4", "search_text": "playstation"},
    "3025": {"label": "Climatiseurs", "expected": "ac", "search_text": "climatiseur"},
}

_FASHION_NOISE = re.compile(
    r"(?i)\b(jean|robe|veste|abrigo|chaqueta|basket|sneaker|chaussure|zapatillas?|"
    r"lunette|ceinture|belt|short|hoodie|tee|t-?shirt|sweat|adidas\s*spezial|nike|"
    r"pegasus|ralph\s*lauren|north\s*face|rossignol|lafuma|smartwatch|reloj|montre|"
    r"clima[\s-]?fit|climawarm|climacool|climacool|thermal\s+fit|polar\s+edge|"
    r"dri[\s-]?fit|under\s*armour|puma\s+clim)\b"
)

_FAMILY_RULES: List[Tuple[str, re.Pattern[str]]] = [
    ("console_ps4", re.compile(r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4)\b")),
    ("console_ps5", re.compile(r"(?i)\b(ps5|playstation\s*5)\b")),
    ("console_xbox", re.compile(r"(?i)\b(xbox|series\s*[sx])\b")),
    ("console_switch", re.compile(r"(?i)\b(switch|nintendo\s*switch)\b")),
    ("console_ds", re.compile(r"(?i)\b(3ds|2ds|nintendo\s*3ds|new\s*nintendo\s*3ds|nds)\b")),
    ("ac", re.compile(r"(?i)\b(clim|climatiseur|air\s*conditioner|pac)\b")),
    ("projector", re.compile(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|vid[eé]oprojecteur)\b")),
    ("apparel", re.compile(r"(?i)\b(hoodie|sweat|tee|t-?shirt|jean|veste|robe|chaussure|basket|pull|manteau|parka)\b")),
    ("shoes", re.compile(r"(?i)\b(basket|sneaker|chaussure|boots?|jordan|nike\s*air)\b")),
    ("jeans", re.compile(r"(?i)\b(jean|denim|levi'?s?|wrangler)\b")),
    ("coffee", re.compile(r"(?i)\b(cafetière|cafetiere|nespresso|expresso|dolce\s*gusto|machine\s*à\s*café|coffee\s*maker)\b")),
    (
        "wearable",
        re.compile(
            r"(?i)\b(garmin|fenix|forerunner|venu|instinct|epix|vivomove|vivoactive|"
            r"suunto|polar\s*vantage|coros|amazfit|apple\s*watch|galaxy\s*watch|montre\s*gps)\b"
        ),
    ),
    (
        "phone",
        re.compile(
            r"(?i)\b(i\s*-?\s*phone(?!\s*watch)|iphone(?!\s*watch)|samsung\s*galaxy|smartphone|"
            r"téléphone|telephone|pixel\s*\d|xiaomi\s*\d{2}|redmi|oneplus|oppo|huawei\s*p)\b"
        ),
    ),
    ("motorcycle", re.compile(r"(?i)\b(moto|scooter|yamaha|honda|kawasaki|casque\s*moto|cross)\b")),
    ("skates", re.compile(r"(?i)\b(skate|skateboard|rollers?|patins?\s*à\s*roulettes)\b")),
    ("fan", re.compile(r"(?i)\b(ventilateur|fan|dyson)\b")),
    ("pc", re.compile(r"(?i)\b(pc\s*gamer|laptop|rtx|gtx|macbook)\b")),
    ("bicycle", re.compile(r"(?i)\b(vélo|velo|bike|vtt|vtc|gravel|bmx|cadre\s*\d{2})\b")),
]


def _is_vinted_catalog_url(text: str) -> bool:
    t = (text or "").lower()
    return "vinted." in t and ("catalog" in t or "http" in t or "search_id=" in t)


def catalog_ids_from_params(params: Optional[Dict[str, Any]]) -> List[str]:
    params = params or {}
    raw = params.get("catalog[]") or params.get("catalog") or params.get("catalog_ids[]")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


def families_from_catalog_params(params: Optional[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for cid in catalog_ids_from_params(params):
        hint = VINTED_CATALOG_HINTS.get(cid)
        if hint:
            fam = str(hint.get("expected") or "").strip()
            if fam and fam not in out:
                out.append(fam)
    return out


def catalog_display_label(params: Optional[Dict[str, Any]]) -> str:
    for cid in catalog_ids_from_params(params):
        hint = VINTED_CATALOG_HINTS.get(cid)
        if hint and hint.get("label"):
            return str(hint["label"])
    return ""


def _params_for_intent(intent: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Récupère search_text / catalog depuis l’URL si params non fournis."""
    if params:
        return params
    if intent and _is_vinted_catalog_url(intent):
        try:
            from .market import params_from_url

            return params_from_url(intent) or {}
        except Exception:
            return {}
    return {}


def intent_display_label(
    intent: SearchIntent,
    *,
    params: Optional[Dict[str, Any]] = None,
    fallback: str = "catalogue Vinted",
) -> str:
    """Libellé lisible pour l’IA / scoring (jamais l’URL brute)."""
    params = _params_for_intent(intent.raw or "", params)
    st = (intent.search_text or "").strip()
    if not st:
        st = str(params.get("search_text") or "").strip()
    if st and not _is_vinted_catalog_url(st):
        return st[:200]
    cat_lbl = catalog_display_label(params)
    if cat_lbl and not st:
        return cat_lbl
    if intent.product_families:
        labels = {
            "projector": "Vidéoprojecteurs",
            "console_ps4": "PlayStation / consoles",
            "ac": "Climatisation",
            "apparel": "Vêtements",
            "shoes": "Chaussures",
        }
        return ", ".join(labels.get(f, f) for f in intent.product_families)[:200]
    return fallback


def primary_product_family(si: SearchIntent) -> Optional[str]:
    """search_text Vinted prime sur le catalogue (évite DS + catalog projecteur → projecteur)."""
    st = (si.search_text or "").strip().lower()
    if st:
        for fam, pat in _FAMILY_RULES:
            if pat.search(st):
                return fam
    priority = (
        "console_ds",
        "console_switch",
        "console_ps5",
        "console_ps4",
        "console_xbox",
        "projector",
        "wearable",
        "phone",
        "ac",
        "pc",
        "coffee",
        "fan",
    )
    fam_set = set(si.product_families)
    for fam in priority:
        if fam in fam_set:
            return fam
    return si.product_families[0] if si.product_families else None


def resolve_item_category(
    inferred: Any,
    si: SearchIntent,
) -> str:
    """Intent catalogue/recherche prime sur l’inférence titre (évite « unknown » bruité)."""
    forced = primary_product_family(si)
    if forced:
        return forced
    conf = float(getattr(inferred, "confidence", 0) or 0)
    cat = str(getattr(inferred, "category", "") or "unknown")
    if conf >= 0.5 and cat != "unknown":
        return cat
    return cat


def parse_search_intent(raw: str, params: Optional[Dict[str, Any]] = None) -> SearchIntent:
    text = (raw or "").strip()
    params = params or {}
    is_url = _is_vinted_catalog_url(text)
    st = str(params.get("search_text") or "").strip()
    if is_url and not st:
        st = ""
    elif not st and not is_url:
        st = text

    cat_raw = params.get("catalog[]") or params.get("catalog") or params.get("catalog_ids[]")
    if isinstance(cat_raw, list):
        cat_blob = " ".join(str(x) for x in cat_raw)
    else:
        cat_blob = str(cat_raw or "")

    blob = f"{st} {cat_blob}".lower() if is_url else f"{text} {st} {cat_blob}".lower()
    families: List[str] = []
    must: List[re.Pattern[str]] = []
    platforms: List[str] = []

    st_families: List[str] = []
    if st:
        for fam, pat in _FAMILY_RULES:
            if pat.search(st.lower()):
                if fam not in st_families:
                    st_families.append(fam)

    if st_families:
        families = list(st_families)
        for fam in families:
            for fk, pat in _FAMILY_RULES:
                if fk == fam:
                    must.append(pat)
                    break
    else:
        for fam, pat in _FAMILY_RULES:
            if pat.search(blob):
                families.append(fam)
                must.append(pat)
        for fam in families_from_catalog_params(params):
            if fam not in families:
                families.append(fam)
                for fk, pat in _FAMILY_RULES:
                    if fk == fam:
                        must.append(pat)
                        break

    if not families and st and not _is_vinted_catalog_url(st):
        words = [w for w in re.findall(r"[\w]{3,}", st.lower()) if w not in ("pour", "avec", "the", "and", "les")]
        if words:
            must.append(re.compile(r"(?i)\b(" + "|".join(re.escape(w) for w in words[:6]) + r")\b"))
    if re.search(r"(?i)\b(ps4|playstation\s*4)\b", blob):
        platforms.append("ps4")
    if re.search(r"(?i)\b(ps5|playstation\s*5)\b", blob):
        platforms.append("ps5")

    display_raw = catalog_display_label(params) or (st if st and not is_url else "")
    if not display_raw and is_url:
        display_raw = "catalogue Vinted"
    inj_search = ""
    for cid in catalog_ids_from_params(params):
        hint = VINTED_CATALOG_HINTS.get(cid)
        if hint and hint.get("search_text"):
            inj_search = str(hint["search_text"])
            break

    return SearchIntent(
        raw=display_raw or text,
        search_text=st or inj_search,
        product_families=families,
        must_match_any=must,
        platform_hints=platforms,
    )
