"""Moteur auxiliaire : filtres dynamiques, taxonomy, criteria plan, browser et infra partagée."""

from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import statistics
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .market import (
    CatalogItem,
    CircuitOpenError,
    get_circuit,
    RETRYABLE_HTTP_STATUS,
    AUTH_REFRESH_STATUS,
    backoff_seconds,
    retry_call,
    guarded_platform_call,
)

logger = logging.getLogger("vf.engine")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOP_DEEP_K = int(os.environ.get("VF_TOP_DEEP_K", "25"))
TOP_IMAGE_K = int(os.environ.get("VF_TOP_IMAGE_K", "60"))
TOP_VISION_K = int(os.environ.get("VF_TOP_VISION_K", "8"))
DEAL_SCORE_THRESHOLD = float(os.environ.get("VF_DEAL_THRESHOLD", "65"))
TRIAGE_DOUBTFUL_MAX = int(os.environ.get("VF_TRIAGE_DOUBTFUL_MAX", "80"))
DEEP_FETCH_ALL_DOUBTFUL = os.environ.get("VF_DEEP_ALL_DOUBTFUL", "0").strip() not in ("0", "false")
QUALITY_TIER_MAX = int(os.environ.get("VF_QUALITY_TIER_MAX", "60"))
AI_TITLE_CHUNK_SIZE = int(os.environ.get("VF_AI_CHUNK", "24"))
AI_SPECS_CHUNK = int(os.environ.get("VF_AI_SPECS_CHUNK", "10"))
AI_VERIFY_CHUNK = int(os.environ.get("VF_AI_VERIFY_CHUNK", "18"))
AI_SKIP_FILTERS_SUGGEST = os.environ.get("VF_SKIP_AI_FILTERS", "0").strip() in ("1", "true", "yes")
DEEP_FETCH_MAX_CONSEC_FAIL = int(os.environ.get("VF_DEEP_MAX_FAIL", "3"))
PROMOTE_DOUBTFUL_MIN_SCORE = float(os.environ.get("VF_PROMOTE_MIN_SCORE", "62"))
PROMOTE_DOUBTFUL_MIN_REL = float(os.environ.get("VF_PROMOTE_MIN_REL", "72"))
CATALOG_MAX_ITEMS = int(os.environ.get("VF_CATALOG_MAX", "0"))

from .intent import (
    SearchIntent,
    VINTED_CATALOG_HINTS,
    catalog_display_label,
    catalog_ids_from_params,
    families_from_catalog_params,
    intent_display_label,
    parse_search_intent,
    primary_product_family,
    resolve_item_category,
)
from .intent import _FASHION_NOISE  # noqa: F401 — used in market rules
from .triage import (
    TitleDecision,
    TitleTriage,
    TriageBatch,
    TRIAGE_REASON_LABELS,
    _ac_off_category_title,
    _ac_title_matches_intent,
    can_promote_doubtful_row,
    classify_rejected_listing,
    demote_strict_intent_mismatches,
    explain_triage_reasons,
    filter_ids_heuristic,
    row_matches_strict_intent,
    run_title_triage,
    strict_intent_active,
    title_has_main_console_unit,
    title_matches_search_intent,
    triage_full_text,
    triage_title,
)



# ---------------------------------------------------------------------------
# Pack signals (manette, HDMI…)
# ---------------------------------------------------------------------------

_CONTROLLER = re.compile(
    r"(?i)\b(manette?s?|controller?s?|mando?s?|comando|dual\s*shock|dualshock|joy-?pad|joypad|"
    r"ds4|avec\s+manette|with\s+controller|incl\.?\s*manette)\b"
)
_CONTROLLER_NEG = re.compile(r"(?i)\b(sans\s*manette|console\s*seule|controller\s*only)\b")
_HDMI = re.compile(r"(?i)\b(hdmi)\b")
_POWER = re.compile(
    r"(?i)\b("
    r"alimentation|chargeur|charger|adaptateur\s*secteur|bloc\s*d.?alim|"
    r"power\s*(supply|adapter|cord|cable)|c[âa]ble\s*d.?alimentation|"
    r"vendu\s+avec\s+(son\s+)?chargeur|avec\s+(son\s+)?chargeur|chargeur\s+inclus|"
    r"official\s+charger|wall\s+charger|psu"
    r")\b"
)
_POWER_NEG = re.compile(r"(?i)\b(sans\s*chargeur|sans\s*alim|no\s*charger|no\s*power|chargeur\s+non\s+inclus)\b")
_STYLUS = re.compile(
    r"(?i)\b(stylet|stylus|vendu\s+avec\s+(son\s+)?stylet|avec\s+(son\s+)?stylet|stylet\s+inclus)\b"
)
_STYLUS_NEG = re.compile(r"(?i)\b(sans\s*stylet|no\s*stylus)\b")
_BOX = re.compile(r"(?i)\b(bo[iî]te|box|scatola)\b")
_VISION_CHARGER = re.compile(
    r"(?i)\b(chargeur|adaptateur|alimentation|bloc\s*d.?alim|power\s*adapter|wall\s*charger)\b"
)
_VISION_STYLUS = re.compile(r"(?i)\b(stylet|stylus)\b")
_AI_CHARGER_OK = re.compile(
    r"(?i)\b(accompagn[ée]e?\s+de\s+(son\s+)?chargeur|avec\s+(son\s+)?chargeur|chargeur\s+(inclus|visible|présent)|"
    r"vendu\s+avec\s+le\s+chargeur)\b"
)
_AI_STYLUS_OK = re.compile(
    r"(?i)\b(accompagn[ée]e?\s+de\s+(son\s+)?stylet|avec\s+(son\s+)?stylet|stylet\s+(inclus|visible|bleu))\b"
)
_AI_NEG_CHARGER = re.compile(
    r"(?i)\b("
    r"aucun\s+accessoire|pas\s+de\s+chargeur|sans\s+chargeur|chargeur\s+non\s+inclus|"
    r"no\s+charger|charger\s+not\s+included|pas\s+d['']alimentation"
    r")\b"
)
_AI_NEG_STYLUS = re.compile(r"(?i)\b(pas\s+de\s+stylet|sans\s+stylet|no\s+stylus)\b")
_ACCESSORY_ONLY = re.compile(
    r"(?i)\b(chargeur\s+seul|coque\s+seule|stylet\s+seul|bo[iî]te\s+vide|"
    r"console\s+seule\s+sans|vendu\s+seul)\b"
)
_HARDWARE_DEFECT = re.compile(
    r"(?i)\b("
    r"lecteur\s+de\s+cartouches?\s+non|cartouche\s+ne\s+fonctionne|"
    r"slot\s+de\s+cartouche|reader\s+not\s+working|"
    r"[ée]cran\s+(cass[ée]|hs|mort|ab[iî]m[ée]|ray[ée])|touch\s*screen\s+broken|"
    r"point\s*noir|pixel\s*mort|dead\s*pixel|tache\s+sur\s+l['\u2019]?[ée]cran|"
    r"ne\s+fonctionne\s+pas|hors\s+service|pour\s+pi[eè]ces"
    r")\b"
)
_SCREEN_POSITIVE = re.compile(
    r"(?i)\b("
    r"[ée]cran[s]?\s*(nickel|parfait|impeccable|sans\s*d[ée]faut)|"
    r"tactile\s*nickel|aucune?\s*rayure|sans\s*(d[ée]faut|rayure)|"
    r"nickels?\s*(les\s+)?[ée]crans?|image\s*propre"
    r")\b"
)
_SCREEN_ISSUE_STRONG = re.compile(
    r"(?i)\b("
    r"point\s*noir|pixel\s*mort|dead\s*pixel|fissure|"
    r"[ée]cran\s+(cass[ée]|hs|mort|d[ée]fectueux)|"
    r"top\s*screen.*(cass|hs|d[ée]f)|écran\s+sup[ée]rieur\s+(cass|hs|d[ée]f)"
    r")\b"
)
_SCREEN_ISSUE = re.compile(
    r"(?i)\b("
    r"point\s*noir|pixel\s*mort|dead\s*pixel|tache|rayure|fissure|"
    r"[ée]cran\s+(ab[iî]m[ée]|cass[ée]|hs|d[ée]fectueux)|"
    r"top\s*screen|écran\s+sup[ée]rieur\s+(cass|hs|d[ée]f)"
    r")\b"
)
_VISION_DEFECT = re.compile(
    r"(?i)\b(point\s*noir|pixel\s*mort|dead\s*pixel|tache|rayure|fissure|"
    r"[ée]cran\s+(ab[iî]m|cass|hs|d[ée]fect)|d[ée]faut\s+(écran|ecran|affichage))\b"
)


def row_source_text(row: Dict[str, Any]) -> str:
    """Texte factuel (sans notes IA acheteur — évite les faux positifs « confirmer chargeur »)."""
    return " ".join(
        str(row.get(k) or "")
        for k in (
            "title",
            "description",
            "description_excerpt",
            "ai_specs_summary",
            "ai_vision_note",
            "ai_bundle",
            "ai_verdict",
        )
    ).strip()


def row_text(row: Dict[str, Any]) -> str:
    extra = str(row.get("buyer_fit_notes") or "")
    base = row_source_text(row)
    return f"{base} {extra}".strip() if extra else base


def detect_controller_in_text(text: str) -> bool:
    if not text or _CONTROLLER_NEG.search(text):
        return False
    return bool(_CONTROLLER.search(text))


def detect_hdmi_in_text(text: str) -> bool:
    return bool(text and _HDMI.search(text))


def detect_power_in_text(text: str) -> bool:
    if not text or _POWER_NEG.search(text):
        return False
    return bool(_POWER.search(text))


def detect_stylus_in_text(text: str) -> bool:
    if not text or _STYLUS_NEG.search(text):
        return False
    return bool(_STYLUS.search(text))


def detect_box_in_text(text: str) -> bool:
    return bool(text and _BOX.search(text))


def _stylus_relevant(row: Dict[str, Any], text: str) -> bool:
    cat = str(row.get("item_type") or row.get("market_category") or "").lower()
    if cat in ("console_ds",):
        return True
    return bool(re.search(r"(?i)\b(3ds|2ds|nintendo\s*ds|new\s*3ds)\b", text))


def reconcile_pack_signals(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fusionne titre, description, synthèse IA et vision pour le pack (chargeur, stylet…)."""
    t = row_source_text(row)
    stylus_ok = _stylus_relevant(row, t)
    phone_main = bool(re.search(r"(?i)\b(iphone|galaxy\s*s\d+|pixel\s*\d+)\b", t))
    if _ACCESSORY_ONLY.search(t) and not re.search(
        r"(?i)\b(console|3ds|2ds|switch|ps4|ps5|xbox)\b", t
    ) and not phone_main:
        row["is_accessory"] = True
        row["accessory_kind"] = row.get("accessory_kind") or "accessory_only"

    if detect_controller_in_text(t):
        row["has_controller"] = True
    if detect_hdmi_in_text(t):
        row["has_hdmi"] = True
    if detect_power_in_text(t):
        row["has_power_cable"] = True
    if stylus_ok and detect_stylus_in_text(t):
        row["has_stylus"] = True
    elif not stylus_ok:
        row.pop("has_stylus", None)
        row.pop("missing_stylus", None)
    if detect_box_in_text(t):
        row["has_box"] = True
    if _POWER_NEG.search(t) and not row.get("has_power_cable"):
        row["missing_power"] = True
        row["missing_charger"] = True
    if _STYLUS_NEG.search(t):
        row["has_stylus"] = False
        row["missing_stylus"] = True

    vision = str(row.get("ai_vision_note") or "")
    if vision:
        if _VISION_CHARGER.search(vision) and not _POWER_NEG.search(vision) and not _AI_NEG_CHARGER.search(vision):
            row["has_power_cable"] = True
        if stylus_ok and _VISION_STYLUS.search(vision) and not _AI_NEG_STYLUS.search(vision):
            row["has_stylus"] = True

    ai_blob = " ".join(
        str(row.get(k) or "") for k in ("ai_specs_summary", "ai_bundle", "ai_verdict")
    )
    if _AI_CHARGER_OK.search(ai_blob) and not _AI_NEG_CHARGER.search(ai_blob):
        row["has_power_cable"] = True
    if stylus_ok and _AI_STYLUS_OK.search(ai_blob) and not _AI_NEG_STYLUS.search(ai_blob):
        row["has_stylus"] = True
    if _AI_NEG_CHARGER.search(ai_blob):
        row["missing_charger"] = True
        if not row.get("has_power_cable"):
            row["missing_power"] = True

    screen_ok = bool(_SCREEN_POSITIVE.search(t))
    if _HARDWARE_DEFECT.search(t) or (_SCREEN_ISSUE_STRONG.search(t) and not screen_ok):
        row["defect_hint"] = True
        row["screen_defect"] = True
    elif _SCREEN_ISSUE.search(t) and not screen_ok and not re.search(
        r"(?i)\b(quelques\s+rayures?|l[eé]g[eè]res?\s+rayures?)\b", t
    ):
        row["defect_hint"] = True
        rf = str(row.get("risk_flags") or "")
        for flag in ("hardware_defect", "screen_defect"):
            if flag not in rf:
                rf = f"{rf},{flag}".strip(",")
        row["risk_flags"] = rf

    if vision and _VISION_DEFECT.search(vision):
        row["defect_hint"] = True
        row["screen_defect"] = True
        rf = str(row.get("risk_flags") or "")
        if "screen_defect" not in rf:
            row["risk_flags"] = f"{rf},screen_defect".strip(",")

    if row.get("has_power_cable"):
        row.pop("missing_power", None)
        row.pop("missing_charger", None)
    if row.get("has_stylus"):
        row.pop("missing_stylus", None)

    notes = str(row.get("buyer_fit_notes") or "")
    if notes:

        def _strip_verify_clause(text: str, kw: str) -> str:
            pat = (
                rf"[^.!?]*(?:{kw})[^.!?]*(?:vérifier|confirmer|à\s*vérifier)[^.!?]*[.!?]?\s*|"
                rf"[^.!?]*(?:vérifier|confirmer)[^.!?]*(?:{kw})[^.!?]*[.!?]?\s*"
            )
            return re.sub(pat, "", text, flags=re.IGNORECASE).strip()

        if row_has_power(row):
            notes = _strip_verify_clause(notes, "chargeur|alim")
        if row.get("has_stylus"):
            notes = _strip_verify_clause(notes, "stylet|stylus")
        row["buyer_fit_notes"] = notes[:240]

    row["_pack_reconciled"] = True
    try:
        enrich_listing_taxonomy(row)
    except Exception:
        pass
    return row


def apply_accessory_result(row: Dict[str, Any], acc: Any) -> Dict[str, Any]:
    """Applique le résultat du triage accessoire (bool + pack_hints) sur une ligne."""
    row["is_accessory"] = bool(getattr(acc, "is_accessory", False))
    row["accessory_kind"] = str(getattr(acc, "kind", None) or "")
    hints = list(getattr(acc, "pack_hints", None) or [])
    if hints:
        row["pack_hints"] = hints
        row["complete_hint"] = True
        if "chargeur" in hints:
            row["charger_status"] = "with"
            row["has_power_cable"] = True
        if "coque" in hints or "housse" in hints:
            row["has_case"] = True
    return row


def enrich_pack_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    return reconcile_pack_signals(row)


def row_has_controller(row: Dict[str, Any]) -> bool:
    return bool(row.get("has_controller")) or detect_controller_in_text(row_text(row))


def row_has_hdmi(row: Dict[str, Any]) -> bool:
    return bool(row.get("has_hdmi")) or detect_hdmi_in_text(row_text(row))


def row_has_power(row: Dict[str, Any]) -> bool:
    return bool(row.get("has_power_cable")) or detect_power_in_text(row_text(row))


def row_has_box(row: Dict[str, Any]) -> bool:
    return bool(row.get("has_box")) or detect_box_in_text(row_text(row))


def row_has_stylus(row: Dict[str, Any]) -> bool:
    if not _stylus_relevant(row, row_text(row)):
        return False
    return bool(row.get("has_stylus")) or detect_stylus_in_text(row_text(row))


_ENGINE_BOOL_FIELD_CHECK: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "has_controller": row_has_controller,
    "has_hdmi": row_has_hdmi,
    "has_power_cable": row_has_power,
    "has_box": row_has_box,
}


# ---------------------------------------------------------------------------
# Filtres dynamiques (catégorie = intent prioritaire)
# ---------------------------------------------------------------------------

FilterSpec = Dict[str, Any]

_FIELDS_BY_CATEGORY: Dict[str, Set[str]] = {
    "projector": {"lumens", "resolution", "has_remote", "has_hdmi", "has_power_cable", "has_box"},
    "console_ps4": {"variant", "storage_gb", "has_controller", "has_hdmi", "has_power_cable", "has_box", "complete_hint"},
    "console_ds": {
        "variant",
        "ds_model_family",
        "charger_status",
        "has_controller",
        "has_stylus",
        "includes_games",
        "has_power_cable",
        "has_box",
        "complete_hint",
        "is_modded",
    },
    "console_switch": {"variant", "has_controller", "has_power_cable", "has_box", "complete_hint"},
    "ac": {"btu", "has_hose", "has_remote"},
    "apparel": {"material", "size_alpha"},
    "coffee": {"variant", "complete_hint"},
    "fan": {"variant", "complete_hint"},
    "pc": {"ram_gb", "gpu"},
    "phone": {"storage_gb", "product_model_key", "variant"},
    "wearable": {"product_model_key", "watch_size_mm"},
}

_KNOWN_FILTER_FIELDS: Set[str] = set()
for _s in _FIELDS_BY_CATEGORY.values():
    _KNOWN_FILTER_FIELDS |= _s
_KNOWN_FILTER_FIELDS |= {
    "final_score",
    "rank_score",
    "quality_tier",
    "has_remote",
    "has_hdmi",
    "has_controller",
    "has_power_cable",
    "has_box",
    "has_stylus",
    "includes_games",
    "complete_hint",
    "variant",
    "lumens",
    "resolution",
    "btu",
    "material",
    "size_alpha",
    "storage_gb",
    "item_type",
    "ds_model_family",
    "charger_status",
    "projector_brand",
    "projector_usage",
    "native_aspect",
    "product_model_key",
    "product_model_label",
    "storage_gb",
    "battery_health_pct",
    "screen_defect",
    "phone_unlocked",
    "icloud_locked",
    "has_maps",
    "has_gps",
    "watch_size_mm",
}


def dominant_category(rows: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for r in rows:
        if r.get("is_accessory"):
            continue
        c = str(r.get("item_type") or "unknown")
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return "unknown"
    return max(counts.items(), key=lambda kv: kv[1])[0]


_PROJECTOR_FILTER_FIELDS = frozenset(
    {
        "lumens",
        "resolution",
        "_lumens_band",
        "native_aspect",
        "has_hdmi",
        "missing_hdmi",
        "projector_brand",
        "office_projector",
    }
)


def resolve_effective_category(
    rows: List[Dict[str, Any]],
    intent: str = "",
    *,
    si: Optional[SearchIntent] = None,
    params: Optional[Dict[str, Any]] = None,
    buyer_priorities: str = "",
    run_name: str = "",
) -> str:
    """search_text Vinted prime ; priorités seulement si la recherche est ambiguë."""
    intent_l = (intent or "").lower()
    params = _params_for_intent(intent or "", params)
    st = str(params.get("search_text") or "").strip().lower()
    if st:
        for fam, pat in _FAMILY_RULES:
            if pat.search(st):
                return fam
    prio = (buyer_priorities or "").lower()
    name = (run_name or "").lower()
    # Ne pas laisser d’anciennes priorités projecteur écraser une recherche URL claire.
    prio_part = "" if st else prio
    blob = f"{intent_l} {prio_part} {name}".strip()
    if blob:
        for fam, pat in _FAMILY_RULES:
            if pat.search(blob):
                return fam
    if si:
        pf = primary_product_family(si)
        if pf:
            return pf
    for cid in catalog_ids_from_params(params):
        hint = VINTED_CATALOG_HINTS.get(cid)
        if hint and hint.get("expected"):
            return str(hint["expected"])
    dom = dominant_category(rows) if rows else "unknown"
    return dom


def build_ui_filters_base() -> List[FilterSpec]:
    """Filtres universels (sécurité, score, qualité marché) — toujours présents."""
    return [
        {
            "id": "risk_mode",
            "label": "Sécurité",
            "type": "select",
            "field": "_risk_mode",
            "segment": "general",
            "options": ["all", "sans_risque", "strict"],
        },
        {
            "id": "score_band",
            "label": "Score min",
            "type": "select",
            "field": "_score_band",
            "segment": "general",
            "options": ["all", "55+", "65+", "75+"],
        },
        {
            "id": "hide_accessories",
            "label": "Accessoires",
            "type": "select",
            "field": "_hide_acc_mode",
            "segment": "general",
            "options": ["masquer", "tout"],
        },
        {
            "id": "intent_exact_only",
            "label": "Modèle exact uniquement",
            "type": "bool",
            "field": "intent_exact_only",
            "segment": "general",
            "default": "off",
        },
        {
            "id": "quality_tier",
            "label": "Qualité marché",
            "type": "select",
            "field": "quality_tier",
            "segment": "market",
            "options": ["all", "pepites", "ok", "sans_dropship", "sans_dinosaure"],
            "option_labels": {
                "all": "Tous",
                "pepites": "Pépites",
                "ok": "OK + pépites",
                "sans_dropship": "Sans dropship",
                "sans_dinosaure": "Sans dinosaure",
            },
        },
        {
            "id": "price_max",
            "label": "Prix max",
            "type": "select",
            "field": "_price_max",
            "segment": "general",
            "options": ["all", "40", "60", "80", "100", "130", "160", "200", "250", "300", "400", "500"],
            "option_labels": {
                "all": "Tous",
                "40": "≤ 40 €",
                "60": "≤ 60 €",
                "80": "≤ 80 €",
                "100": "≤ 100 €",
                "130": "≤ 130 €",
                "160": "≤ 160 €",
                "200": "≤ 200 €",
                "250": "≤ 250 €",
                "300": "≤ 300 €",
                "400": "≤ 400 €",
                "500": "≤ 500 €",
            },
        },
        {
            "id": "price_min",
            "label": "Prix min",
            "type": "select",
            "field": "_price_min",
            "segment": "general",
            "options": ["all", "15", "30", "50", "80", "100", "150", "200"],
            "option_labels": {
                "all": "Tous",
                "15": "≥ 15 €",
                "30": "≥ 30 €",
                "50": "≥ 50 €",
                "80": "≥ 80 €",
                "100": "≥ 100 €",
                "150": "≥ 150 €",
                "200": "≥ 200 €",
            },
        },
    ]


def sanitize_dynamic_filters(
    filters: List[FilterSpec],
    category: str,
) -> List[FilterSpec]:
    """Retire les filtres hors-sujet (ex. lumens/HDMI sur une recherche 3DS)."""
    cat = category or "generic"
    out: List[FilterSpec] = []
    seen: set[str] = set()
    seen_fields: set[str] = set()
    for spec in filters:
        if not isinstance(spec, dict):
            continue
        sid = str(spec.get("id") or "")
        fld = str(spec.get("field") or sid)
        if not sid or sid in seen or fld in seen_fields:
            continue
        low_id = sid.lower()
        low_fld = fld.lower()
        if cat != "projector":
            if fld in _PROJECTOR_FILTER_FIELDS or low_fld in _PROJECTOR_FILTER_FIELDS:
                continue
            if any(x in low_id for x in ("lumen", "hdmi", "resolution", "aspect", "1080", "4k", "wxga")):
                continue
        if cat == "console_ds":
            if any(x in low_id for x in ("hdmi", "lumen", "resolution", "projecteur", "1080", "wxga", "format")):
                continue
            if fld in ("has_hdmi", "missing_hdmi", "lumens", "resolution"):
                continue
        if not spec.get("segment"):
            spec = dict(spec)
            spec["segment"] = "market" if fld and not fld.startswith("_") else "general"
        out.append(normalize_filter_spec(spec))
        seen.add(sid)
        seen_fields.add(fld)
    return out


def normalize_filter_spec(spec: FilterSpec) -> FilterSpec:
    """Délègue au moteur universel (operator + options normalisées)."""
    from .engine import normalize_filter_spec as _norm

    return _norm(spec)


def category_filter_templates(category: str) -> List[FilterSpec]:
    """Filtres par défaut (sans IA) — spécifiques au marché, jamais mélangés."""
    cat = category or "generic"
    base = build_ui_filters_base()
    if cat == "projector":
        # Filtres techniques projecteur : construits dynamiquement via interactive_filters
        return base
    if cat == "console_ds":
        return base + [
            {
                "id": "ds_model_family",
                "label": "Modèle",
                "type": "select",
                "field": "ds_model_family",
                "segment": "market",
                "options": [
                    "all",
                    "new3ds_xl",
                    "new3ds",
                    "3ds_xl",
                    "3ds",
                    "2ds_xl",
                    "2ds",
                    "ds_lite",
                    "ds",
                ],
                "option_labels": {
                    "all": "Tous",
                    "new3ds_xl": "New 3DS XL",
                    "new3ds": "New 3DS",
                    "3ds_xl": "3DS XL",
                    "3ds": "3DS",
                    "2ds_xl": "2DS XL",
                    "2ds": "2DS",
                    "ds_lite": "DS Lite",
                    "ds": "DS / NDS",
                },
            },
            {
                "id": "charger_pack",
                "label": "Chargeur",
                "type": "select",
                "field": "_charger_mode",
                "segment": "market",
                "options": ["all", "with", "without", "unknown"],
                "option_labels": {
                    "all": "Tous",
                    "with": "Avec chargeur",
                    "without": "Sans chargeur",
                    "unknown": "Non précisé",
                },
            },
            {
                "id": "has_stylus",
                "label": "Stylet",
                "type": "bool",
                "field": "has_stylus",
                "segment": "market",
            },
            {
                "id": "includes_games",
                "label": "Jeux inclus",
                "type": "select",
                "field": "includes_games",
                "operator": "custom",
                "segment": "market",
                "options": ["all", "yes", "no"],
                "option_labels": {"all": "Tous", "yes": "Avec jeux", "no": "Sans jeux"},
            },
            {
                "id": "condition_class",
                "label": "État",
                "type": "select",
                "field": "_condition_class",
                "segment": "market",
                "options": ["all", "ok", "defects", "for_parts"],
                "option_labels": {
                    "all": "Tous",
                    "ok": "OK (pas HS)",
                    "defects": "Défauts signalés",
                    "for_parts": "Pour pièces",
                },
            },
            {
                "id": "modded_only",
                "label": "Mod / CFW",
                "type": "select",
                "field": "_modded_mode",
                "segment": "market",
                "options": ["all", "stock", "modded"],
                "option_labels": {"all": "Tous", "stock": "Origine", "modded": "Modifiée / CFW"},
            },
        ]
    if cat.startswith("console_"):
        return base + [
            {
                "id": "controller_count",
                "label": "Manettes",
                "type": "select",
                "field": "_controller_count",
                "operator": "custom",
                "segment": "market",
                "options": ["all", "0", "1", "2plus"],
                "option_labels": {
                    "all": "Toutes",
                    "0": "Sans",
                    "1": "1 manette",
                    "2plus": "2+ manettes",
                },
            },
            {
                "id": "has_controller",
                "label": "Manette incluse",
                "type": "bool",
                "field": "has_controller",
                "segment": "market",
            },
            {
                "id": "pack_console",
                "label": "Pack",
                "type": "select",
                "field": "_pack_level",
                "segment": "market",
                "options": ["all", "manette", "complet"],
            },
            {
                "id": "variant",
                "label": "Modèle",
                "type": "select",
                "field": "variant",
                "segment": "market",
                "options": ["all", "slim", "pro", "fat"],
            },
        ]
    if cat == "ac":
        return base + [
            {"id": "btu_min", "label": "BTU min", "type": "number", "field": "btu", "op": "gte", "segment": "market"},
            {"id": "has_hose", "label": "Tuyau", "type": "bool", "field": "has_hose", "segment": "market"},
        ]
    if cat in ("apparel", "shoes", "jeans"):
        return base + [
            {
                "id": "size_band",
                "label": "Taille",
                "type": "select",
                "field": "_text_contains",
                "segment": "market",
                "options": ["all", "s", "m", "l", "xl"],
                "option_labels": {"all": "Toutes", "s": "S", "m": "M", "l": "L", "xl": "XL+"},
            },
            {
                "id": "brand_hint",
                "label": "Marque",
                "type": "select",
                "field": "_brand_match",
                "segment": "market",
                "options": ["all", "nike", "adidas", "zara", "other"],
                "option_labels": {
                    "all": "Toutes",
                    "nike": "Nike",
                    "adidas": "Adidas",
                    "zara": "Zara",
                    "other": "Autre",
                },
            },
            {
                "id": "condition_class",
                "label": "État",
                "type": "select",
                "field": "_condition_class",
                "segment": "market",
                "options": ["all", "ok", "defects"],
                "option_labels": {"all": "Tous", "ok": "OK", "defects": "Défauts"},
            },
        ]
    if cat == "coffee":
        return base + [
            {
                "id": "coffee_type",
                "label": "Type",
                "type": "select",
                "field": "_text_contains",
                "segment": "market",
                "options": ["all", "nespresso", "dolce", "expresso", "filtre"],
                "option_labels": {
                    "all": "Tous",
                    "nespresso": "Nespresso",
                    "dolce": "Dolce Gusto",
                    "expresso": "Expresso",
                    "filtre": "Filtre",
                },
            },
        ]
    if cat == "phone":
        return base + [
            {
                "id": "phone_storage",
                "label": "Stockage",
                "type": "select",
                "field": "storage_gb",
                "segment": "market",
                "options": ["all", "64", "128", "256", "512", "1024"],
                "option_labels": {
                    "all": "Tous",
                    "64": "64 Go",
                    "128": "128 Go",
                    "256": "256 Go",
                    "512": "512 Go",
                    "1024": "1 To",
                },
            },
            {
                "id": "phone_unlocked",
                "label": "Débloqué",
                "type": "select",
                "field": "_phone_unlocked",
                "segment": "market",
                "options": ["all", "yes", "no"],
                "option_labels": {"all": "Tous", "yes": "Débloqué", "no": "Verrouillé / iCloud"},
            },
            {
                "id": "condition_class",
                "label": "État",
                "type": "select",
                "field": "_condition_class",
                "segment": "market",
                "options": ["all", "ok", "defects", "for_parts"],
                "option_labels": {
                    "all": "Tous",
                    "ok": "OK",
                    "defects": "Défauts signalés",
                    "for_parts": "Pour pièces",
                },
            },
        ]
    if cat == "wearable":
        return base + [
            {
                "id": "wearable_brand",
                "label": "Marque",
                "type": "select",
                "field": "_wearable_brand",
                "segment": "market",
                "options": ["all", "garmin", "apple", "suunto", "polar", "coros", "amazfit", "other"],
                "option_labels": {
                    "all": "Toutes",
                    "garmin": "Garmin",
                    "apple": "Apple Watch",
                    "suunto": "Suunto",
                    "polar": "Polar",
                    "coros": "Coros",
                    "amazfit": "Amazfit",
                    "other": "Autre",
                },
            },
            {
                "id": "has_gps",
                "label": "GPS",
                "type": "bool",
                "field": "has_gps",
                "segment": "market",
            },
            {
                "id": "has_maps",
                "label": "Cartes / topo",
                "type": "bool",
                "field": "has_maps",
                "segment": "market",
            },
            {
                "id": "condition_class",
                "label": "État",
                "type": "select",
                "field": "_condition_class",
                "segment": "market",
                "options": ["all", "ok", "defects"],
                "option_labels": {"all": "Tous", "ok": "OK", "defects": "Défauts"},
            },
        ]
    if cat == "motorcycle":
        return base + [
            {
                "id": "moto_cc",
                "label": "Cylindrée",
                "type": "select",
                "field": "_text_contains",
                "segment": "market",
                "options": ["all", "50", "125", "250", "500"],
                "option_labels": {
                    "all": "Toutes",
                    "50": "50 cc",
                    "125": "125 cc",
                    "250": "250+ cc",
                    "500": "500+ cc",
                },
            },
            {
                "id": "condition_class",
                "label": "État",
                "type": "select",
                "field": "_condition_class",
                "segment": "market",
                "options": ["all", "ok", "defects", "for_parts"],
                "option_labels": {
                    "all": "Tous",
                    "ok": "OK",
                    "defects": "Défauts",
                    "for_parts": "Pour pièces",
                },
            },
        ]
    if cat == "skates":
        return base + [
            {
                "id": "skate_type",
                "label": "Type",
                "type": "select",
                "field": "_text_contains",
                "segment": "market",
                "options": ["all", "skateboard", "roller", "longboard"],
                "option_labels": {
                    "all": "Tous",
                    "skateboard": "Skateboard",
                    "roller": "Rollers",
                    "longboard": "Longboard",
                },
            },
        ]
    if cat == "fan":
        return base + [
            {
                "id": "fan_diameter",
                "label": "Taille",
                "type": "select",
                "field": "_text_contains",
                "segment": "market",
                "options": ["all", "40", "50", "60"],
                "option_labels": {"all": "Toutes", "40": "≤40 cm", "50": "~50 cm", "60": "60+ cm"},
            },
        ]
    return base


def _model_market_key(row: Dict[str, Any]) -> str:
    """Clé de regroupement pour médiane par modèle (pas médiane globale)."""
    key = str(row.get("product_model_key") or row.get("ds_model_family") or "").strip()
    if key and key != "unknown":
        return key
    cat = str(row.get("item_type") or row.get("market_category") or "").lower()
    if cat in ("console_ps4", "console_ps5"):
        variant = str(row.get("variant") or "standard").lower()
        if variant not in ("slim", "pro", "standard"):
            variant = "standard"
        base = "ps4_pro" if variant == "pro" else ("ps4_slim" if variant in ("slim", "standard") else f"ps4_{variant}")
        try:
            storage = int(row.get("storage_gb") or 0)
        except (TypeError, ValueError):
            storage = 0
        if storage >= 1000:
            return f"{base}_1tb"
        if storage >= 500:
            return f"{base}_500gb"
        return base
    if cat in ("ac", "clim", "climatisation"):
        try:
            from .scoring import infer_ac_model_key

            specs = row.get("extracted_specs") if isinstance(row.get("extracted_specs"), dict) else {}
            mk = infer_ac_model_key({**specs, **{k: row.get(k) for k in ("brand", "ac_type", "capacity_btu")}})
            if mk:
                return mk
        except Exception:
            pass
        try:
            btu = int(row.get("capacity_btu") or 0)
        except (TypeError, ValueError):
            btu = 0
        if btu >= 4000:
            return f"ac_{int(btu // 1000) * 1000}btu"
    return ""


def apply_model_market_stats(rows: List[Dict[str, Any]], *, min_samples: int = 3) -> None:
    """Prix médian par modèle précis — décote vs le marché du même modèle."""
    by_key: Dict[str, List[float]] = {}
    for r in rows:
        if r.get("is_accessory"):
            continue
        key = _model_market_key(r)
        if not key:
            continue
        try:
            p = float(r.get("total_price") or r.get("price") or 0)
        except (TypeError, ValueError):
            p = 0.0
        if p > 0:
            by_key.setdefault(key, []).append(p)
    medians = {
        k: float(statistics.median(v))
        for k, v in by_key.items()
        if len(v) >= min_samples
    }
    for r in rows:
        key = _model_market_key(r)
        med = medians.get(key)
        if not med or med <= 0:
            continue
        try:
            p = float(r.get("total_price") or r.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        r["model_market_median_eur"] = round(med, 2)
        r["model_market_n"] = len(by_key.get(key, []))
        if len(by_key.get(key, [])) < min_samples:
            continue
        disc = clamp_discount_pct((1.0 - p / med) * 100.0)
        r["model_vs_market_pct"] = disc
        oracle_disc = r.get("market_discount_pct")
        try:
            o_abs = abs(float(oracle_disc or 0))
        except (TypeError, ValueError):
            o_abs = 999.0
        if oracle_disc is None or o_abs > 80 or abs(disc) >= o_abs:
            r["market_discount_pct"] = disc


def merge_results_filters(
    category: str,
    ai_filters: Optional[List[FilterSpec]] = None,
    *,
    catalog_rows: Optional[List[Dict[str, Any]]] = None,
    max_market: int = 10,
    category_schema: Optional[Dict[str, Any]] = None,
) -> List[FilterSpec]:
    """Base + filtres marché (templates) + modèles du catalogue + compléments IA."""
    cat = category or "generic"
    templates = list(category_filter_templates(cat))
    try:
        from .engine import load_category_schema, resolve_category_tier, schema_filter_specs

        if resolve_category_tier(cat) == 2:
            schema = category_schema or load_category_schema(cat)
            if schema:
                templates = templates + schema_filter_specs(schema)
    except Exception:
        pass
    seen: set[str] = set()
    seen_fields: set[str] = set()
    out: List[FilterSpec] = []
    for s in templates:
        sid = str(s.get("id") or "")
        fld = str(s.get("field") or sid)
        if not sid or sid in seen or fld in seen_fields:
            continue
        out.append(normalize_filter_spec(s))
        seen.add(sid)
        seen_fields.add(fld)
    if catalog_rows:
        try:
            from .engine import (
                build_interactive_filters,
                controls_to_filter_specs,
                enrich_listings_specs,
                register_custom_evaluators,
            )

            register_custom_evaluators()
            enrich_listings_specs(catalog_rows, cat)
            controls = build_interactive_filters(cat, catalog_rows)
            for s in controls_to_filter_specs(controls):
                sid = str(s.get("id") or "")
                fld = str(s.get("field") or sid)
                if not sid or sid in seen or fld in seen_fields:
                    continue
                out.append(s)
                seen.add(sid)
                seen_fields.add(fld)
        except Exception:
            pass
        try:
            from .engine import catalog_model_filter_spec

            mspec = catalog_model_filter_spec(catalog_rows, cat)
            if mspec:
                mid = str(mspec.get("id") or "product_model")
                out = [s for s in out if str(s.get("id") or "") not in (mid, "ds_model_family")]
                out.insert(min(3, len(out)), normalize_filter_spec(mspec))
                seen.add(mid)
        except Exception:
            pass
    n_ai = 0
    for s in ai_filters or []:
        sid = str(s.get("id") or "")
        fld = str(s.get("field") or sid)
        if not sid or sid in seen or fld in seen_fields:
            continue
        if fld and not fld.startswith("_") and fld not in _KNOWN_FILTER_FIELDS:
            continue
        out.append(normalize_filter_spec(s))
        seen.add(sid)
        seen_fields.add(fld)
        n_ai += 1
        if n_ai >= max_market:
            break
    return sanitize_dynamic_filters(out, cat)


def build_rule_filters(*, category: str, intent: str = "", rows: Optional[List[Dict[str, Any]]] = None) -> List[FilterSpec]:
    cat = category or resolve_effective_category(rows or [], intent)
    return category_filter_templates(cat)


def merge_ai_filters(
    rule: List[FilterSpec],
    ai: Optional[List[FilterSpec]],
    *,
    category: str = "",
    ai_primary: bool = False,
) -> List[FilterSpec]:
    """Fusionne filtres IA. Si ai_primary, seuls filtres généraux + IA (pas HDMI/lumens figés)."""
    if not ai:
        return rule or build_ui_filters_base()
    if ai_primary:
        base = build_ui_filters_base()
        seen = {s.get("id") for s in base}
        out: List[FilterSpec] = list(base)
        for s in ai:
            sid = str(s.get("id") or "")
            fld = str(s.get("field") or "")
            if not sid or sid in seen:
                continue
            if fld.startswith("_") or fld in _KNOWN_FILTER_FIELDS or not fld:
                out.append(normalize_filter_spec(s))
                seen.add(sid)
        return sanitize_dynamic_filters(out, category)
    allowed = _FIELDS_BY_CATEGORY.get(category, set())
    seen = {s.get("id") for s in rule}
    out = list(rule)
    for s in ai:
        sid = s.get("id")
        fld = str(s.get("field") or "")
        if not sid or sid in seen:
            continue
        if allowed and fld and fld not in allowed and fld not in _KNOWN_FILTER_FIELDS:
            continue
        if fld in ("material", "size_alpha") and category == "projector":
            continue
        out.append(s)
        seen.add(sid)
    return out


def filter_presets(*, category: str = "") -> List[Dict[str, Any]]:
    cat = category or ""
    presets: List[Dict[str, Any]] = [
        {
            "id": "preset_best_deals",
            "label": "Meilleures affaires",
            "values": {
                "risk_mode": "sans_risque",
                "hide_accessories": "masquer",
                "score_band": "65+",
            },
        },
        {
            "id": "preset_safe",
            "label": "Sélection prudente",
            "values": {"risk_mode": "sans_risque", "hide_accessories": "masquer", "score_band": "55+"},
        },
        {
            "id": "preset_top",
            "label": "Top deals (70+)",
            "values": {"risk_mode": "sans_risque", "hide_accessories": "masquer", "score_band": "75+"},
        },
        {
            "id": "preset_reset",
            "label": "Tout afficher",
            "values": {
                "risk_mode": "all",
                "hide_accessories": "tout",
                "score_band": "all",
                "quality_tier": "all",
                "resolution": "all",
                "lumens_band": "all",
                "pack_level": "all",
                "variant": "all",
                "livraison_uniquement": "off",
            },
        },
    ]
    if cat == "console_ds":
        presets.insert(
            1,
            {
                "id": "preset_ds_pack",
                "label": "Pack complet DS",
                "values": {
                    "risk_mode": "sans_risque",
                    "charger_pack": "with",
                    "has_stylus": "on",
                    "condition_class": "ok",
                },
            },
        )
        presets.insert(
            2,
            {
                "id": "preset_new3ds",
                "label": "New 3DS / XL",
                "values": {"ds_model_family": "new3ds_xl", "condition_class": "ok"},
            },
        )
    elif cat.startswith("console_"):
        presets.insert(
            1,
            {
                "id": "preset_console_pack",
                "label": "Pack console complet",
                "values": {"risk_mode": "sans_risque", "has_controller": "on", "score_band": "55+"},
            },
        )
    elif cat in ("phone", "wearable"):
        presets.insert(
            1,
            {
                "id": "preset_same_model",
                "label": "Comparer un modèle",
                "values": {"risk_mode": "all", "score_band": "all", "hide_accessories": "tout"},
            },
        )
    elif cat in ("coffee", "apparel", "motorcycle", "skates", "fan", "jeans", "shoes"):
        presets.insert(
            1,
            {
                "id": "preset_market_ok",
                "label": "Bon état",
                "values": {"risk_mode": "sans_risque", "condition_class": "ok", "score_band": "55+"},
            },
        )
    if cat == "projector":
        presets.insert(
            0,
            {
                "id": "preset_gros_lumens",
                "label": "Gros lumens (3000+)",
                "values": {
                    "risk_mode": "sans_risque",
                    "hide_accessories": "masquer",
                    "lumens_band": "3000+",
                    "exclude_mini_weak": "on",
                },
            },
        )
        presets.insert(
            1,
            {
                "id": "preset_proj_hd",
                "label": "Full HD 1080p",
                "values": {
                    "risk_mode": "sans_risque",
                    "hide_accessories": "masquer",
                    "resolution": "1080p",
                    "quality_tier": "sans_dinosaure",
                },
            },
        )
        presets.insert(
            2,
            {
                "id": "preset_home_cinema",
                "label": "Cinéma maison",
                "values": {
                    "risk_mode": "sans_risque",
                    "projector_usage": "cinema",
                    "lumens_band": "2000+",
                    "has_hdmi": "yes",
                },
            },
        )
        presets.insert(
            3,
            {
                "id": "preset_value",
                "label": "Meilleur rapport qualité/prix",
                "values": {
                    "risk_mode": "sans_risque",
                    "hide_accessories": "masquer",
                    "value_score_min": "70",
                    "exclude_mini_weak": "on",
                },
            },
        )
        presets.insert(
            4,
            {
                "id": "preset_no_junk",
                "label": "Sans dropship",
                "view_mode": "no_junk",
                "values": {
                    "risk_mode": "sans_risque",
                    "hide_accessories": "masquer",
                    "quality_tier": "sans_dinosaure",
                    "exclude_screen_only": "on",
                },
            },
        )
    return presets


def sort_rows_for_view(rows: List[Dict[str, Any]], view_mode: str) -> List[Dict[str, Any]]:
    """Tri intelligent (vues) — n’exclut pas tout sauf mode no_junk."""
    mode = (view_mode or "all").strip()

    def _tier_rank(t: str) -> int:
        return {
            "gem": 0,
            "ok": 1,
            "suspicious": 2,
            "dropship": 3,
            "dinosaur": 4,
        }.get(t, 2)

    if mode == "gems_first":
        return sorted(
            rows,
            key=lambda r: (
                _tier_rank(str(r.get("quality_tier") or "ok")),
                -float(r.get("display_rank_score") or r.get("rank_score") or r.get("final_score") or 0),
            ),
        )
    if mode == "quality_first":
        return sorted(
            rows,
            key=lambda r: (
                0 if str(r.get("quality_tier") or "") in ("gem", "ok") else 1,
                -float(r.get("display_rank_score") or r.get("rank_score") or r.get("final_score") or 0),
            ),
        )
    if mode == "no_junk":
        filtered = [
            r
            for r in rows
            if str(r.get("quality_tier") or "ok") not in ("dropship", "dinosaur")
        ]
        return sorted(
            filtered,
            key=lambda r: -float(r.get("display_rank_score") or r.get("rank_score") or r.get("final_score") or 0),
        )
    return sorted(
        rows,
        key=lambda r: (
            _tier_rank(str(r.get("quality_tier") or "ok")),
            -float(
                r.get("best_deal_score")
                or r.get("quality_value_score")
                or r.get("display_rank_score")
                or r.get("rank_score")
                or r.get("final_score")
                or 0
            ),
        ),
    )


def sort_rows_by_sort_by(rows: List[Dict[str, Any]], sort_by: str) -> List[Dict[str, Any]]:
    """
    Tri UI explicite (ne touche pas au filtrage).

    sort_by:
      - ai (rank_final_score / final_rank_score)
      - score (best_deal_score)
      - value (value_score_v2)
      - price_asc / price_desc (total_price/price)
      - date_newest (listed_at puis scraped_at)
      - platform (A→Z)
    """
    mode = (sort_by or "ai").strip().lower()

    def _price(r: Dict[str, Any]) -> float:
        try:
            return float(r.get("total_price") or r.get("price") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _date_key(r: Dict[str, Any]) -> str:
        # ISO-like strings sort lexicographically (YYYY-MM-DD...).
        # Prefer listed_at when available, else scraped_at, else empty.
        d = str(r.get("listed_at") or "").strip()
        if d:
            return d
        return str(r.get("scraped_at") or "").strip()

    if mode in ("ai", "rank_final", "final"):
        return sorted(
            rows,
            key=lambda r: float(
                r.get("rank_final_score")
                or r.get("final_rank_score")
                or r.get("ai_rank_score")
                or r.get("value_score_v2")
                or r.get("best_deal_score")
                or r.get("rank_score")
                or r.get("final_score")
                or 0.0
            ),
            reverse=True,
        )
    if mode == "score":
        return sorted(
            rows,
            key=lambda r: float(
                r.get("best_deal_score")
                or r.get("rank_score")
                or r.get("final_score")
                or 0.0
            ),
            reverse=True,
        )
    if mode == "value":
        return sorted(
            rows,
            key=lambda r: float(
                r.get("value_score_v2")
                or r.get("quality_value_score")
                or r.get("best_deal_score")
                or 0.0
            ),
            reverse=True,
        )
    if mode == "price_asc":
        return sorted(rows, key=_price)
    if mode == "price_desc":
        return sorted(rows, key=_price, reverse=True)
    if mode == "date_newest":
        return sorted(rows, key=_date_key, reverse=True)
    if mode == "platform":
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("platform") or "").lower().replace("lbc", "leboncoin"),
                -float(
                    r.get("rank_final_score")
                    or r.get("value_score_v2")
                    or r.get("best_deal_score")
                    or r.get("rank_score")
                    or 0.0
                ),
            ),
        )
    return rows


def _apply_builtin_filters(rows: List[Dict[str, Any]], values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Filtres universels (sécurité, score, prix) — indépendants de la catégorie."""
    out: List[Dict[str, Any]] = []

    def _on(fid: str) -> bool:
        v = values.get(fid)
        return v in (True, "on", "1", 1, "true")

    risk_mode = str(values.get("risk_mode") or "all")
    hide_acc = str(values.get("hide_accessories") or "masquer")
    score_band = str(values.get("score_band") or "all")

    for r in rows:
        if hide_acc == "masquer" and bool(r.get("is_accessory")):
            continue
        livraison = str(values.get("livraison_uniquement") or "on")
        if livraison in ("on", "1", "true", "yes", "oui"):
            plat = str(r.get("platform") or "").lower()
            if plat in ("leboncoin", "lbc"):
                try:
                    from .engine import enrich_row_delivery, is_hand_delivery_only_row

                    enrich_row_delivery(r)
                    if is_hand_delivery_only_row(r):
                        continue
                except Exception:
                    pass
        if risk_mode == "sans_risque" and (str(r.get("risk_flags") or "").strip() or r.get("deal_blocked")):
            continue
        if risk_mode == "strict" and (
            str(r.get("risk_flags") or "").strip()
            or r.get("deal_blocked")
            or str(r.get("quality_grade") or "").upper() != "A"
        ):
            continue
        if _on("exclude_accessories") and bool(r.get("is_accessory")):
            continue
        if _on("exclude_risky") and (str(r.get("risk_flags") or "").strip() or r.get("deal_blocked")):
            continue
        if _on("main_only") and bool(r.get("is_accessory")):
            continue
        if _on("grade_a") and str(r.get("quality_grade") or "").upper() != "A":
            continue
        if _on("intent_exact_only") and not row_matches_strict_intent(r):
            continue

        rank_sc = float(
            r.get("best_deal_score") or r.get("rank_score") or r.get("final_score") or 0
        )
        if score_band == "55+" and rank_sc < 55:
            continue
        if score_band == "65+" and rank_sc < 65:
            continue
        if score_band == "75+" and rank_sc < 75:
            continue

        try:
            price_eur = float(r.get("total_price") or r.get("price") or 0)
        except (TypeError, ValueError):
            price_eur = 0.0
        pmax = str(values.get("price_max") or "all")
        if pmax != "all":
            try:
                if price_eur <= 0 or price_eur > float(pmax):
                    continue
            except ValueError:
                pass
        pmin = str(values.get("price_min") or "all")
        if pmin != "all":
            try:
                if price_eur < float(pmin):
                    continue
            except ValueError:
                pass

        out.append(r)
    return out


def apply_dynamic_filters(rows: List[Dict[str, Any]], values: Dict[str, Any], specs: List[FilterSpec]) -> List[Dict[str, Any]]:
    """Moteur data-driven : filtres universels + évaluation par operator."""
    # Post-consolidation: ensure filter_engine evaluators are registered before GUI/CLI use.
    _BOOL_FIELD_CHECK.update(_ENGINE_BOOL_FIELD_CHECK)
    register_custom_evaluators()

    normed = [normalize_filter_spec(s) for s in specs if isinstance(s, dict)]
    return apply_universal_filters(rows, values, normed, pre_filter=_apply_builtin_filters)


# ---------------------------------------------------------------------------
# MSRP
# ---------------------------------------------------------------------------


def estimate_msrp_eur(row: Dict[str, Any]) -> Optional[float]:
    try:
        ref_new = float(row.get("ref_price_new_eur") or 0)
        if ref_new > 0:
            return ref_new
    except (TypeError, ValueError):
        pass
    try:
        from .models import estimate_msrp_eur_for_row

        msrp_kb = estimate_msrp_eur_for_row(row)
        if msrp_kb and msrp_kb > 0:
            return msrp_kb
    except Exception:
        pass
    cat = str(row.get("item_type") or "")
    text = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    if cat == "projector" or re.search(r"projecteur|projector", text):
        try:
            lv = int(row.get("lumens") or 0)
        except (TypeError, ValueError):
            lv = 0
        if lv >= 3500:
            return 550.0
        if lv >= 2000:
            return 320.0
        return 200.0
    if cat == "console_ps4" or re.search(r"\bps4\b", text):
        if re.search(r"\bpro\b", text):
            return 400.0
        if re.search(r"\bslim\b", text):
            return 300.0
        return 320.0
    if cat == "console_ps5":
        return 550.0
    if cat == "ac":
        return 280.0
    return None


def discount_vs_new_pct(row: Dict[str, Any]) -> Optional[float]:
    from .scoring import effective_price_eur, is_discontinued_product, sane_reference_eur

    if is_discontinued_product(row):
        row["is_discontinued"] = True
        return None
    price = effective_price_eur(row)
    if price <= 0:
        return None
    raw_msrp = row.get("msrp_eur") or estimate_msrp_eur(row)
    try:
        msrp = sane_reference_eur(float(raw_msrp or 0), price, discontinued=False)
    except (TypeError, ValueError):
        msrp = None
    if not msrp or msrp <= 0:
        return None
    return round((float(msrp) - price) / float(msrp) * 100.0, 1)


# ---------------------------------------------------------------------------
# Score weights + rescore
# ---------------------------------------------------------------------------

_CONFIG_WEIGHTS = Path("brouillon") / "data" / "score_weights.json"


@dataclass
class ScoreWeights:
    discount: float = 1.2
    engagement: float = 1.0
    seller: float = 1.0
    completeness: float = 1.0
    category_bonus: float = 1.0
    risk: float = 1.0
    ai_blend: float = 0.45
    buyer_fit: float = 1.0
    projector_hdmi: float = 2.0
    projector_aspect: float = 1.5
    projector_lumens: float = 1.8
    dinosaur_penalty: float = 2.5
    ac_btu: float = 1.2
    apparel_brand: float = 1.0
    buyer_priorities: str = ""

    def clamp(self) -> "ScoreWeights":
        for attr in (
            "discount",
            "engagement",
            "seller",
            "completeness",
            "category_bonus",
            "buyer_fit",
            "projector_hdmi",
            "projector_aspect",
            "projector_lumens",
            "dinosaur_penalty",
            "ac_btu",
            "apparel_brand",
        ):
            setattr(self, attr, max(0.0, min(3.0, float(getattr(self, attr)))))
        self.risk = max(0.5, min(3.0, float(self.risk)))
        self.ai_blend = max(0.0, min(1.0, float(self.ai_blend)))
        self.buyer_priorities = str(self.buyer_priorities or "")[:600]
        return self


def weight_field_defs(category: str = "") -> List[Tuple[str, str, str, float, float]]:
    """(key, label, hint, slider_min, slider_max) pour l’onglet Poids."""
    cat = category or ""
    if cat == "console_ds":
        return [
            ("discount", "Prix vs marché", "Décote vs prix neuf / Leboncoin", 0.0, 2.5),
            ("engagement", "Engagement", "Vues / favoris", 0.0, 2.5),
            ("seller", "Vendeur", "Notes et ancienneté", 0.0, 2.5),
            ("completeness", "Stylet & pack", "Stylet, jeux, chargeur, boîte", 0.0, 3.0),
            ("category_bonus", "Modèle 3DS", "XL, New 3DS, 2DS, état écran", 0.0, 3.0),
            ("risk", "Pénalités risques", "HS, ban, pour pièces", 0.5, 3.0),
            ("buyer_fit", "Adéquation IA", "Authenticité, état, prix", 0.0, 2.5),
            ("ai_blend", "Part IA (audit)", "0 = heuristique, 1 = IA domine", 0.0, 1.0),
        ]
    if cat.startswith("console_"):
        return [
            ("discount", "Prix vs marché", "Décote réelle vs référence", 0.0, 2.5),
            ("engagement", "Engagement", "Vues / favoris", 0.0, 2.5),
            ("seller", "Vendeur", "Notes et ancienneté", 0.0, 2.5),
            ("completeness", "Pack console", "Manette, câbles, boîte", 0.0, 3.0),
            ("category_bonus", "Modèle exact", "Slim, Pro, stockage, état", 0.0, 3.0),
            ("risk", "Pénalités risques", "HS, ban, défauts", 0.5, 3.0),
            ("buyer_fit", "Adéquation IA", "Score IA vs vos priorités", 0.0, 2.5),
            ("ai_blend", "Part IA (audit)", "0 = heuristique, 1 = IA domine", 0.0, 1.0),
        ]
    fields: List[Tuple[str, str, str, float, float]] = [
        ("discount", "Prix vs marché", "Décote réelle vs référence", 0.0, 2.5),
        ("engagement", "Engagement", "Vues / favoris", 0.0, 2.5),
        ("seller", "Vendeur", "Notes et ancienneté", 0.0, 2.5),
        ("completeness", "Pack complet", "Câbles, télécommande, boîte", 0.0, 2.5),
        ("category_bonus", "Critères techniques", "Specs catégorie (lumens, BTU…)", 0.0, 2.5),
        ("risk", "Pénalités risques", "HS, ban, défauts", 0.5, 3.0),
        ("buyer_fit", "Adéquation IA", "Score IA vs vos priorités", 0.0, 2.5),
        ("ai_blend", "Part IA (audit)", "0 = heuristique, 1 = IA domine", 0.0, 1.0),
    ]
    if cat == "projector":
        fields.extend(
            [
                ("projector_hdmi", "HDMI obligatoire", "Pénalise VGA/DVI/S-Vidéo seuls", 0.0, 3.0),
                ("projector_aspect", "Format 16:9", "Favorise 16:9 vs 4:3 XGA", 0.0, 3.0),
                ("projector_lumens", "Lumens réels", "ANSI crédibles, pas marketing", 0.0, 3.0),
                ("dinosaur_penalty", "Anti-dinosaure", "Vieux bureautique invendable", 0.0, 3.0),
            ]
        )
    elif cat == "ac":
        fields.append(("ac_btu", "Puissance BTU", "Clim — BTU et volume", 0.0, 3.0))
    elif cat == "coffee":
        fields.append(("coffee_brand", "Marque / type", "Expresso, capsule, broyeur", 0.0, 3.0))
    elif cat == "fan":
        fields.append(("fan_specs", "Débit / bruit", "Diamètre, dB, oscillation", 0.0, 3.0))
    elif cat in ("apparel", "shoes", "jeans"):
        fields.append(("apparel_brand", "Marque / état", "Vêtements — marque et état", 0.0, 3.0))
    return fields


def market_priority_key(raw_url: str, ctx: Dict[str, Any]) -> str:
    """Identifiant stable d’une recherche (URL + marché) — pas de réutilisation entre URLs différentes."""
    cat = str(ctx.get("category") or "generic").strip()
    params = _params_for_intent(raw_url or "", None)
    st = str(ctx.get("search_text") or params.get("search_text") or "").strip().lower()
    cats = ",".join(sorted(catalog_ids_from_params(params)))
    pf = str(params.get("price_from") or params.get("price_from_min") or "")
    pt = str(params.get("price_to") or params.get("price_to_max") or "")
    return f"{cat}|{st}|{cats}|{pf}|{pt}"


def market_priority_hint(category: str, search_text: str = "") -> str:
    """Texte d’aide sous le champ priorités (pas du contenu sauvegardé)."""
    st = (search_text or "").strip()
    if category == "console_ds":
        base = "Décrivez modèle (XL, New 3DS), stylet, jeux, état écran/touch, budget."
    elif category == "projector":
        base = "Décrivez usage (cinéma/salon), lumens ANSI, HDMI, format 16:9, budget max."
    elif category.startswith("console_"):
        base = "Décrivez modèle exact, manette/câbles, stockage, état, budget."
    elif category == "ac":
        base = "Décrivez BTU, pièce m², tuyau/ télécommande, bruit, budget."
    elif category == "phone":
        base = "Décrivez modèle exact, stockage, état écran/batterie, débloqué, budget."
    elif category == "wearable":
        base = "Décrivez modèle Garmin/montre, GPS, cartes, état, accessoires."
    else:
        base = "Décrivez marque, état, accessoires, ce que vous refusez, budget."
    if st:
        return f"Recherche « {st[:60]} » — {base}"
    return base + " Le marché vient de l’URL ; ce champ sert au scoring uniquement."


def market_priority_suggestions(category: str, search_text: str = "") -> List[str]:
    """Courtes phrases activables pour affiner les priorités (langage naturel)."""
    st = (search_text or "").lower()
    if re.search(r"(?i)\b(cafeti|espresso|senseo|nespresso|dolce)\b", st):
        return [
            "Marque et modèle exacts (bar, pression, capsules)",
            "Accessoires : réservoir, porte-capsule, bac",
            "Peu d’usage, nettoyage récent",
            "Éviter fuites / odeur brûlé / HS",
            "Prix sous neuf / retail",
            "Photos du groupe et du bac",
        ][:6]
    if re.search(r"(?i)\b(moto|scooter|125|yamaha|honda|kawasaki)\b", st):
        return [
            "Carte grise et kilométrage cohérents",
            "Contrôle technique valide",
            "Carnet d’entretien / factures",
            "Éviter accidentée / non roulante",
            "Équipement (casque, top case) bonus",
            "Budget max livraison comprise",
        ][:6]
    if re.search(r"(?i)\b(skate|skateboard|longboard|deck|trucks)\b", st):
        return [
            "Deck peu fissuré / pas cassé",
            "Trucks + roulements OK",
            "Marque deck / trucks si connue",
            "Taille et shape adaptées",
            "Éviter rouille / jeu excessif",
            "Prix sous neuf boutique",
        ][:6]
    if category == "console_ds":
        out: List[str] = []
        try:
            from .engine import infer_ds_model_family

            fam = infer_ds_model_family({"title": search_text})
        except Exception:
            fam = ""

        if fam and fam != "unknown":
            label = {
                "new3ds_xl": "New 3DS XL",
                "new3ds": "New 3DS",
                "3ds_xl": "3DS XL",
                "3ds": "3DS",
                "2ds_xl": "2DS XL",
                "2ds": "2DS",
                "ds_lite": "DS Lite",
                "ds": "DS / NDS",
            }.get(fam, fam)
            out.append(f"Priorité modèle détecté : {label}")
        elif "xl" in st:
            out.append("Priorité modèle XL / grande taille (si présent)")
        else:
            out.append("Priorité console : modèle précis (pas vague / pas “DS” seul)")

        wants_stylus = bool(re.search(r"(?i)\b(stylet|stylus)\b", st))
        wants_charger = bool(
            re.search(r"(?i)\b(chargeur|charger|ladekabel|cargador|ladekabel)\b", st)
        )
        if wants_charger:
            out.append("Chargeur d’origine demandé (obligatoire si vous visez la console complète)")
        else:
            out.append("Chargeur d’origine = gros plus (éviter “sans chargeur” si possible)")
        if wants_stylus:
            out.append("Stylet d’origine demandé (éviter “sans stylet”)")
        else:
            out.append("Stylet d’origine = gros plus")

        if re.search(r"(?i)\b(jeux|games|giochi)\b", st):
            out.append("Jeux inclus : priorité (au moins 1 cartouche / pack clair)")
        else:
            out.append("Jeux inclus : bonus fort (et vérifié sur les photos/texte)")

        out.append("Éviter ban / pour pièces / écran HS / “console seule” sans pack")
        out.append("Photos : écran (face), dos + connectiques + chargeur/stylet si inclus")
        return out[:6]
    if category == "projector":
        out: List[str] = []
        try:
            from .engine import infer_projector_brand, infer_projector_usage

            brand = infer_projector_brand({"title": search_text})
            usage = infer_projector_usage({"title": search_text})
        except Exception:
            brand, usage = "", ""

        if brand and brand not in ("other", ""):
            out.append(f"Marque cible : {brand.title()} (lisible sur l’annonce)")
        else:
            out.append("Marque cible lisible (Epson/BenQ/Optoma/XGIMI…) plutôt que “no brand”")

        if re.search(r"(?i)\b(hdmi|hdmi2|hdmi1)\b", st):
            out.append("HDMI vérifié (au moins 1 entrée HDMI, pas VGA seule)")
        else:
            out.append("HDMI moderne obligatoire (pas VGA/DVI “bazar”)")

        if re.search(r"(?i)\b(4k|2160p)\b", st):
            out.append("Résolution visée : 4K (attendu réel, pas upscale flou)")
        elif re.search(r"(?i)\b(1080|full\\s*hd|fhd|720p|xga|wxga|wvga)\\b", st):
            # Générique mais orienté “éviter l’invendable”.
            out.append("Résolution attendue : annonce claire (éviter XGA/WXGA si objectif cinéma)")
        else:
            out.append("Résolution attendue : 1080p/4K si usage films")

        m_lm = re.search(r"(?i)\\b(\\d{3,5})\\s*(lm|lumens|ansi)\\b", st)
        if m_lm:
            out.append(f"Lumens ANSI annoncés cohérents (~{m_lm.group(1)}+)")
        else:
            out.append("Lumens ANSI crédibles (pas LED marketing, vérifier retours/âge)")

        if usage in ("cinema", "office", "portable", "home"):
            out.append(f"Usage cible : {usage} (format/keystone à vérifier)")
        else:
            out.append("Usage cible : salon/cinéma (éviter “bureautique basique”)")

        out.append("Éviter XGA / bureautique invendable (si la recherche vise du cinéma)")
        out.append("Photos : connectiques + lentille + état lampe/compteur si indiqué")
        return out[:6]
    if category.startswith("console_"):
        return [
            "Modèle exact (Slim, Pro, OLED…)",
            "Manette + câbles + boîte",
            "Stockage indiqué (Go)",
            "Éviter HS / ban / pour pièces",
            "Bon état général, peu d’usure",
            "Prix sous référence marché",
        ][:6]
    if category == "ac":
        return [
            "BTU adaptés à la pièce",
            "Tuyau + télécommande inclus",
            "Peu de bruit, bon état",
            "Éviter fuites / odeur moisi",
            "Budget max installation comprise",
        ][:6]
    if category == "phone":
        out: List[str] = []
        try:
            from .engine import infer_phone_model, infer_storage_gb

            stub = {"title": search_text, "ai_model_exact": search_text}
            key, label = infer_phone_model(stub)
            stg = infer_storage_gb(stub)
        except Exception:
            key, label, stg = "", "", 0

        if key and label:
            out.append(f"Modèle exact détecté : {label}")
        else:
            out.append("Modèle exact (iPhone précis, pas “iPhone” générique)")

        if stg and stg > 0:
            out.append(f"Stockage confirmé : {stg} Go")

        wants_unlocked = bool(
            re.search(r"(?i)\b(unlocked|débloqu|sim\\s*free|pas\\s*i.?cloud|find\\s*my)\\b", st)
        )
        if wants_unlocked:
            out.append("Débloqué / SIM free (pas verrou iCloud / Find My)")
        else:
            out.append("Débloqué idéalement (désactiver iCloud/Find My si possible)")

        m_batt = re.search(r"(?i)\\b(batterie|battery)\\b.*?(\\d{2,3})\\s*%", st)
        if m_batt:
            out.append(f"Objectif : batterie ≈ {m_batt.group(2)} % (au-dessus si possible)")
        else:
            out.append("Batterie élevée si possible (viser > 85%)")

        if re.search(r"(?i)\\b(écran\\s*fiss|fêlure|crack|rayure)\\b", st):
            out.append("Écran sans fissure (rayures légères OK, fissure non)")
        else:
            out.append("Écran sain (pas de fissures, Face ID OK si mentionné)")

        out.append("Comparer le même modèle (même stockage/couleur) vs médiane")
        out.append("Photos indispensables : face/écran + dos + bords + Face ID")
        return out[:6]
    if category == "wearable":
        return [
            "Modèle Garmin / montre exact",
            "GPS + cartes si trail",
            "Batterie et écran OK",
            "Bracelet + chargeur inclus",
            "Pas compte verrouillé",
            "Comparer vs médiane du modèle",
        ][:6]
    if category in ("apparel", "shoes"):
        return [
            "Marque et taille exactes",
            "Très bon état, pas de taches",
            "Authenticité / pas contrefaçon",
            "Photos détaillées demandées",
            "Prix sous retail / neuf",
        ][:6]
    return [
        "Marque et modèle identifiables",
        "État décrit clairement",
        "Pack complet valorisé",
        "Éviter dropship / titres vagues",
        "Bon rapport qualité-prix",
        "Budget maximum",
    ][:6]


_priorities_by_market: Dict[str, str] = {}


def get_market_priority(key: str) -> str:
    return str(_priorities_by_market.get(key) or "")[:600]


def set_market_priority(key: str, text: str) -> None:
    k = (key or "").strip()
    if not k:
        return
    t = (text or "").strip()[:600]
    if t:
        _priorities_by_market[k] = t
    elif k in _priorities_by_market:
        del _priorities_by_market[k]


def category_buyer_criteria(category: str) -> str:
    """Critères à évaluer par l'IA selon le marché."""
    try:
        from .models import get_category_config

        cfg = get_category_config(category)
        if cfg:
            return cfg.prompt_block()
    except Exception:
        pass
    guides = {
        "projector": (
            "Projecteur : vrais lumens ANSI (pas LED marketing), format natif 16:9 vs 4:3 XGA/WVGA, "
            "HDMI moderne obligatoire (VGA/DVI/M1-DA seul = dinosaure invendable), modèle identifiable."
        ),
        "ac": "Climatisation : BTU réels, volume pièce possible, tuyau + télécommande.",
        "console_ps4": "Console : modèle exact, stockage, manette, HDMI, alimentation, état.",
        "console_ds": "Nintendo DS/3DS/2DS : modèle (XL, New 3DS), stylet, jeux inclus, chargeur, état écran.",
        "apparel": "Vêtements : marque, taille, matière, état, défauts visibles.",
        "shoes": "Chaussures : modèle, taille EU, état, authenticité.",
        "skate": "Skate : type (deck, complete, trucks), marque, état, taille si indiquée.",
        "skates": "Rollers : marque, taille, état, freins / protections inclus.",
        "coffee": "Machine à café : marque, type (expresso, capsule, broyeur), état, accessoires, pression/bar.",
        "fan": "Ventilateur : diamètre, débit, bruit dB, oscillation, état moteur.",
        "small_appliance": "Petit électroménager : marque, modèle, état, complet, défauts.",
        "phone": (
            "Téléphone : modèle EXACT (iPhone 13 Pro, Galaxy S24…), stockage Go, état écran/batterie, "
            "débloqué / pas iCloud, comparer le même modèle entre annonces."
        ),
        "wearable": (
            "Montre GPS / sport : modèle exact (Garmin Fenix, Forerunner, Apple Watch…), GPS, cartes topo, "
            "taille mm, état écran/batterie."
        ),
        "bicycle": "Vélo : type (VTT/VTC/électrique), taille cadre, roues 26/27.5/29, marque, état transmission.",
        "camera": "Photo : boîtier vs kit, capteur, monture objectif, déclenchements, état capteur.",
        "audio": "Audio : type (casque/enceinte/ampli/DAC), marque, impédance casque, état.",
        "laptop": "Portable : modèle exact, RAM, SSD, GPU gaming si pertinent, état batterie/écran.",
        "vinyl": "Vinyle : état disque/pochette (VG+/NM), original vs réédition.",
        "sports": "Sport : discipline, taille, marque, état, complet.",
    }
    if category.startswith("console_"):
        return guides["console_ps4"]
    return guides.get(
        category,
        "Produit : marque identifiable, état, complétude, pas dropshipping générique, bon rapport qualité/prix.",
    )


def audit_category_row(row: Dict[str, Any]) -> None:
    """Audit technique par catégorie (heuristique, 0 API)."""
    cat = str(row.get("item_type") or "")
    issues: List[str] = []
    grade = "B"

    if cat == "projector":
        lum = int(row.get("lumens") or 0)
        if row.get("legacy_ports_only") or (row.get("missing_hdmi") and not row.get("has_hdmi")):
            issues.append("no_hdmi")
        if LEGACY_VIDEO_PORTS.search(row_text(row)) and not row.get("has_hdmi"):
            issues.append("no_hdmi")
        if row.get("native_aspect") == "4:3" or str(row.get("resolution") or "").lower() in (
            "xga",
            "wvga",
            "svga",
        ):
            issues.append("aspect_43")
        if row.get("underpowered") or (0 < lum < 500):
            issues.append("low_lumens")
        if row.get("inflated_lumens_claim"):
            issues.append("inflated_lumens")
        if issues == ["inflated_lumens"]:
            grade = "C"
        if "no_hdmi" in issues or (lum > 0 and lum < 200):
            grade = "F"
        elif "low_lumens" in issues or "aspect_43" in issues:
            grade = "D" if "no_hdmi" not in issues else "F"
        elif row.get("has_hdmi") and row.get("native_aspect") == "16:9" and lum >= 1500:
            grade = "A"
        elif row.get("has_hdmi") and lum >= 800:
            grade = "B"
    elif cat == "ac":
        btu = int(row.get("btu") or 0)
        if btu and btu < 7000:
            issues.append("low_btu")
            grade = "D"
        if not row.get("has_hose"):
            issues.append("no_hose")
        if btu >= 9000 and row.get("has_hose"):
            grade = "A"

    row["viability_grade"] = grade
    if issues:
        row["viability_issues"] = "|".join(issues)


_active_weights = ScoreWeights()


def get_weights() -> ScoreWeights:
    return _active_weights


def set_weights(w: ScoreWeights) -> None:
    global _active_weights
    _active_weights = w.clamp()


def load_weights() -> ScoreWeights:
    global _active_weights, _priorities_by_market
    try:
        if _CONFIG_WEIGHTS.is_file():
            data = json.loads(_CONFIG_WEIGHTS.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = asdict(ScoreWeights())
                merged = {**base, **{k: data[k] for k in base if k in data}}
                _active_weights = ScoreWeights(**merged).clamp()
                raw_map = data.get("priorities_by_market")
                if isinstance(raw_map, dict):
                    _priorities_by_market = {
                        str(k): str(v)[:600] for k, v in raw_map.items() if str(k).strip()
                    }
                else:
                    _priorities_by_market = {}
                # Ancien champ global : ne plus réinjecter sur une autre URL (migration douce).
                if _active_weights.buyer_priorities and not _priorities_by_market:
                    _priorities_by_market["__legacy_projector__"] = _active_weights.buyer_priorities
                _active_weights.buyer_priorities = ""
    except Exception:
        pass
    return _active_weights


def save_weights(w: Optional[ScoreWeights] = None) -> None:
    global _priorities_by_market
    w = (w or _active_weights).clamp()
    _CONFIG_WEIGHTS.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(w)
    payload["priorities_by_market"] = dict(_priorities_by_market)
    _CONFIG_WEIGHTS.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    set_weights(w)


def rescore_catalog(
    items: List[CatalogItem],
    *,
    intent: str,
    weights: Optional[ScoreWeights] = None,
    prior_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    w = weights or get_weights()
    mctx = detect_market_context(intent)
    market_cat = str(mctx.get("category") or "")
    non_acc: List[float] = []
    acc_cache: Dict[int, Any] = {}
    for it in items:
        acc = detect_accessory(it, search_hint=intent, market_category=market_cat)
        acc_cache[it.id] = acc
        if not acc.is_accessory:
            p = it.total_or_price
            if p and p > 0:
                non_acc.append(float(p))
    stats = compute_market_stats(non_acc)
    prior = prior_rows or {}
    rows: List[Dict[str, Any]] = []
    for it in items:
        acc = acc_cache.get(it.id) or detect_accessory(
            it, search_hint=intent, market_category=market_cat
        )
        row = it.to_row()
        apply_accessory_result(row, acc)
        prev = prior.get(str(it.id)) or {}
        for pk in (
            "buyer_fit_score",
            "buyer_fit_grade",
            "buyer_fit_notes",
            "ai_model_exact",
            "ai_completeness",
            "ai_verdict",
            "ai_relevance",
            "ai_specs_summary",
            "ai_vision_note",
            "ai_bundle",
            "description",
            "description_excerpt",
            "has_power_cable",
            "has_stylus",
            "has_controller",
            "has_hdmi",
            "has_box",
            "includes_games",
            "variant",
            "score_base",
            "score_overlay",
            "_pack_reconciled",
        ):
            if prev.get(pk) is not None:
                row[pk] = prev[pk]
        row.update(score_item_buy_mode(it, stats=stats, search_hint=intent, weights=w))
        row["score_base"] = float(row.get("final_score") or 0.0)
        row["score_overlay"] = 0.0
        row["item_type"] = infer_category(it).category
        row.update(extract_specs(it, row["item_type"]))
        reconcile_pack_signals(row)
        audit_category_row(row)
        apply_category_spec_scoring(row, w)
        tag_market_quality(row)
        apply_deal_caps(row)
        row["rank_score"] = rank_score_for_sort(row)
        row["display_rank_score"] = row["rank_score"]
        rows.append(row)
    normalize_run_rank_scores(rows, main_only=True)
    rows.sort(
        key=lambda r: float(r.get("rank_score") or r.get("display_rank_score") or r.get("final_score") or 0),
        reverse=True,
    )
    return rows


def rescore_rows_inplace(
    rows: List[Dict[str, Any]],
    *,
    weights: Optional[ScoreWeights] = None,
    renormalize: bool = True,
) -> None:
    """Recalcule scores catégorie + tri relatif sans re-scraper le catalogue."""
    w = weights or get_weights()
    apply_listing_intelligence(rows)
    for row in rows:
        audit_category_row(row)
        apply_category_spec_scoring(row, w)
        tag_market_quality(row)
        apply_deal_caps(row)
    if renormalize:
        normalize_run_rank_scores(rows, main_only=True)
        rows.sort(
            key=lambda r: float(
                r.get("rank_score") or r.get("display_rank_score") or r.get("final_score") or 0
            ),
            reverse=True,
        )


load_weights()


LEGACY_VIDEO_PORTS = re.compile(
    r"(?i)\b(vga|rgb|d-?sub|s-?video|svideo|composite|dvi(?!\s*[-to])|m1-?da|rca|composante)\b"
)
_OBSOLETE_PROJECTOR_FORMAT = re.compile(
    r"(?i)\b(xga|svga|wvga|1024\s*[x×]\s*768|800\s*[x×]\s*600|4:3|format\s*4:3)\b"
)
_DROPSHIP_PROJECTOR = re.compile(
    r"(?i)\b(wimius|vamvo|yaber|ysametp|hy300|hy\s*300|topvision|goodee|toumei|magcubic|fangor|cibest|"
    r"vankyo|tepun|tontu|luns|wanbo|hision|nommo|thor|lcos|voplls|uptech|byintek|cheerlux|dbpower|"
    r"apeman|victure|crossbuds|nonete|xnano|wewatch|ty260|ty\s*260|led\s*projector|amazon\s*basics)\b"
)
_WHITELABEL_PROJECTOR_TITLE = re.compile(
    r"(?i)\b(android\s*\d+\s*full\s*hd|full\s*hd\s*1080p\s*projecteur|projecteur\s*android\s*\d+|"
    r"smart\s*projecteur\s*wifi|mini\s*projecteur\s*led|projecteur\s*portable\s*led)\b"
)
_GENERIC_MINI_PROJECTOR = re.compile(
    r"(?i)\b(mini\s*vid[ée]o\s*projecteur|vid[ée]oprojecteur\s*portable|projecteur\s*led\s*portable|"
    r"projecteur\s*android|smart\s*projecteur|projecteur\s*wifi|projecteur\s*bluetooth|"
    r"cylindrique|magcubic|home\s*cin[ée]ma\s*portable)\b"
)
_PREMIUM_PROJECTOR = re.compile(
    r"(?i)\b(xgimi|mogo\s*pro|halo\s*plus|nebula|anker\s*nebula|samsung\s*freestyle|formovie|"
    r"dangbei|wanbo\s*monkey|jmgo|benq\s*gv|viewsonic\s*m1)\b"
)
_BUDGET_LCD_PROJECTOR = re.compile(
    r"(?i)\b(neopix|philips\s*neopix|picopix|philips\s*picopix)\b"
)
_WXGA_OFFICE_PROJECTOR = re.compile(
    r"(?i)\b(wxga|1280\s*[x×]\s*800|wuxga\s*1280|16:10|pt-?lw\d|projecteur\s*professionnel|"
    r"salle\s*de\s*classe|bureautique)\b"
)


def projector_office_wxga(row: Dict[str, Any]) -> bool:
    text = row_text(row)
    if _WXGA_OFFICE_PROJECTOR.search(text):
        return True
    res = str(row.get("resolution") or "").lower()
    return res == "wxga" or str(row.get("native_aspect") or "") == "16:10"
_WRONG_CATEGORY_NOT_VIDEO = re.compile(
    r"(?i)\b(voigtl[aä]nder|diapositive|diapositives|slide\s*projector|projecteur\s*[àa]\s*diapos|"
    r"r[ée]troprojecteur|épiscope|episcope|dia\s*projector|film\s*projector|8\s*mm|super\s*8|"
    r"vp\s*\d+\s*a|carousel\s*slide)\b"
)
_PROJECTOR_HYPE = re.compile(
    r"(?i)\b(mini\s*projecteur|projecteur\s*led\s*portable|home\s*cin[ée]ma\s*portable|"
    r"supporte\s*4k|4k\s*support|20000\s*lum|15000\s*lum|10000\s*lum|9000\s*lum|8000\s*lum|7000\s*lum|"
    r"4000\s*lumens?\s*led|android\s*tv.*projecteur|projecteur.*android\s*tv)\b"
)
_TRUSTED_PROJECTOR = re.compile(
    r"(?i)\b(epson|benq|optoma|viewsonic|sony|jvc|panasonic|nec|christie|acer|lg|infocus|philips)\b"
)
_ANSI_LUMENS_MENTION = re.compile(r"(?i)\bansi\s*lum")


def detect_market_context(
    raw: str,
    *,
    buyer_priorities: str = "",
) -> Dict[str, Any]:
    """
    Analyse intelligente URL Vinted : search_text, catalog[], priorités utilisateur.
    La recherche textuelle prime sur un catalogue projecteur résiduel.
    """
    params: Dict[str, Any] = {}
    if raw and _is_vinted_catalog_url(raw):
        try:
            from .market import params_from_url

            params = params_from_url(raw) or {}
        except Exception:
            params = {}
    st = str(params.get("search_text") or "").strip()
    cats = catalog_ids_from_params(params)
    cat = resolve_effective_category(
        [],
        raw,
        params=params,
        buyer_priorities=buyer_priorities,
    )
    sources: List[str] = []
    if st:
        matched = [fam for fam, pat in _FAMILY_RULES if pat.search(st.lower())]
        if matched:
            sources.append(f"recherche « {st} » → {matched[0]}")
        else:
            sources.append(f"recherche « {st} »")
    for cid in cats:
        hint = VINTED_CATALOG_HINTS.get(cid)
        if hint:
            sources.append(f"catalogue {hint.get('label', cid)}")
    if (buyer_priorities or "").strip():
        sources.append("priorités acheteur")
    si = parse_search_intent(raw, params)
    url_locked = bool(st) and cat not in ("", "generic", "unknown")
    return {
        "category": cat,
        "source": " · ".join(sources[:4]) if sources else "URL (détection automatique)",
        "search_text": st,
        "catalog_ids": cats,
        "product_families": list(si.product_families or []),
        "display_label": intent_display_label(si, params=params, fallback=st or "catalogue Vinted"),
        "url_locked": url_locked,
    }


def guess_market_category_from_url(raw: str, buyer_priorities: str = "") -> str:
    """Devine le marché depuis l’URL (search_text > catalogue > priorités)."""
    return str(detect_market_context(raw, buyer_priorities=buyer_priorities).get("category") or "generic")


def projector_obsolete_signals(row: Dict[str, Any]) -> List[str]:
    """Signaux dinosaure / invendable grand public (projecteurs)."""
    text = row_text(row)
    flags: List[str] = []
    if row.get("legacy_ports_only") or (
        LEGACY_VIDEO_PORTS.search(text) and not row_has_hdmi(row)
    ):
        flags.append("no_hdmi")
    if _OBSOLETE_PROJECTOR_FORMAT.search(text) or str(row.get("native_aspect") or "") == "4:3":
        flags.append("obsolete_aspect")
    res = str(row.get("resolution") or "").lower()
    if res in ("xga", "wvga", "svga"):
        flags.append("obsolete_aspect")
    try:
        lum = int(row.get("lumens") or 0)
    except (TypeError, ValueError):
        lum = 0
    if row.get("underpowered") or (0 < lum < 500):
        flags.append("low_lumens")
    return flags


def tag_market_quality(row: Dict[str, Any]) -> None:
    """Classe qualité marché — la marque seule ne suffit pas (dinosaures VGA/XGA exclus)."""
    if not row.get("viability_grade"):
        audit_category_row(row)

    cat = str(row.get("item_type") or "")
    text = row_text(row)
    tier = "ok"
    flags: List[str] = list(filter(None, str(row.get("viability_issues") or "").split("|")))
    vg = str(row.get("viability_grade") or "B")

    if cat == "projector":
        obs = projector_obsolete_signals(row)
        try:
            lum = int(row.get("lumens") or 0)
        except (TypeError, ValueError):
            lum = 0
        premium = bool(_PREMIUM_PROJECTOR.search(text))
        budget_lcd = bool(_BUDGET_LCD_PROJECTOR.search(text))
        office_wxga = projector_office_wxga(row)
        trusted = bool(_TRUSTED_PROJECTOR.search(text)) and not budget_lcd
        inflated = bool(
            row.get("inflated_lumens_claim")
            or (
                lum >= 2500
                and not _ANSI_LUMENS_MENTION.search(text)
                and not trusted
                and not office_wxga
            )
            or _PROJECTOR_HYPE.search(text)
        )
        aspect = str(row.get("native_aspect") or "")
        if not aspect and re.search(r"(?i)\b(16:9|1080p|full\s*hd)\b", text):
            aspect = "16:9"
        vague_title = bool(
            re.match(
                r"(?i)^(vid[ée]o\s*projecteur|mini\s*projecteur|projecteur|videoprojecteur)\s*$",
                text[:80].strip(),
            )
            or (len(text.split()) <= 4 and not trusted and not premium and not budget_lcd)
        )
        ai_tier = str(row.get("ai_quality_tier") or "").strip().lower()
        if ai_tier in ("gem", "ok", "suspicious", "dropship", "dinosaur"):
            tier = ai_tier
            flags.append("ai_quality")
        elif budget_lcd:
            tier = "suspicious"
            flags.append("budget_lcd_entry")
        elif office_wxga and row_has_hdmi(row):
            tier = "ok"
            flags.append("office_wxga")
        elif (
            _DROPSHIP_PROJECTOR.search(text)
            or _WHITELABEL_PROJECTOR_TITLE.search(text)
            or _GENERIC_MINI_PROJECTOR.search(text)
            or _PROJECTOR_HYPE.search(text)
            or inflated
            or (vague_title and not premium and not trusted)
        ):
            tier = "dropship"
            flags.append("dropship_or_hype")
            if vague_title or _WHITELABEL_PROJECTOR_TITLE.search(text):
                flags.append("generic_white_label")
            if inflated:
                flags.append("inflated_lumens")
        elif obs or vg in ("F", "D") or "no_hdmi" in flags:
            tier = "dinosaur"
            flags.extend(obs)
        elif premium and row_has_hdmi(row) and aspect in ("16:9", "") and not inflated and not office_wxga:
            tier = "gem"
            flags.append("premium_portable")
        elif (
            vg in ("A", "B")
            and row_has_hdmi(row)
            and aspect == "16:9"
            and not inflated
            and not office_wxga
            and not _DROPSHIP_PROJECTOR.search(text)
        ):
            if 800 <= lum <= 3200 or (lum == 0 and trusted and not _PROJECTOR_HYPE.search(text)):
                tier = "gem"
                flags.append("modern_usable")
        elif inflated:
            tier = "suspicious"
            flags.append("inflated_lumens")
        if tier == "ok" and trusted and not row_has_hdmi(row) and not premium:
            tier = "dinosaur"
            flags.append("trusted_but_no_hdmi")
        if re.search(r"(?i)\bsupporte\s*4k\b", text) and tier in ("ok", "suspicious"):
            tier = "suspicious"
            flags.append("marketing_4k")
        if tier == "gem" and (inflated or lum >= 3500) and not premium:
            tier = "dropship"
            flags.append("gem_blocked_inflated")
    elif cat == "ac":
        ai_tier = str(row.get("ai_quality_tier") or "").strip().lower()
        if ai_tier in ("gem", "ok", "suspicious", "dropship", "dinosaur"):
            tier = ai_tier
            flags.append("ai_quality")
        elif int(row.get("btu") or 0) and int(row.get("btu") or 0) < 7000:
            tier = "suspicious"
            flags.append("low_btu")
    elif cat == "console_ds":
        try:
            from .scoring import detect_dropship_risk

            ds_risk, ds_flags = detect_dropship_risk(row)
            if ds_risk >= 55:
                tier = "dropship"
                flags.extend(ds_flags[:4])
            elif ds_risk >= 40:
                tier = "suspicious"
                flags.append("dropship_signals")
        except Exception:
            pass
        ai_tier = str(row.get("ai_quality_tier") or "").strip().lower()
        if ai_tier in ("gem", "ok", "suspicious", "dropship", "dinosaur") and tier == "ok":
            tier = ai_tier
            flags.append("ai_quality")
    else:
        ai_tier = str(row.get("ai_quality_tier") or "").strip().lower()
        if ai_tier in ("gem", "ok", "suspicious", "dropship", "dinosaur"):
            if ai_tier == "dinosaur" and cat != "projector":
                tier = "suspicious"
                flags.append("ai_quality_remapped")
            else:
                tier = ai_tier
            flags.append("ai_quality")

    row["quality_tier"] = tier
    if flags:
        row["quality_flags"] = "|".join(dict.fromkeys(flags))[:120]
    try:
        from .engine import apply_brand_quality_audit

        apply_brand_quality_audit(row)
    except Exception:
        pass


def listing_price_eur(row: Dict[str, Any]) -> Optional[float]:
    for key in ("landed_cost_eur", "total_price", "price", "total_or_price"):
        try:
            v = float(row.get(key) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    raw = row.get("raw")
    if isinstance(raw, dict):
        for key in ("total_item_price", "price", "total_price"):
            try:
                amt = raw.get(key)
                if isinstance(amt, dict):
                    amt = amt.get("amount") or amt.get("value")
                v = float(amt or 0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
    m = re.search(r"(?i)(\d{1,4}(?:[.,]\d{1,2})?)\s*€", row_source_text(row))
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
            if v > 0:
                return v
        except ValueError:
            pass
    return None


def compute_pack_score(row: Dict[str, Any]) -> float:
    """
    Score 0–100 de complétude specs/pack (heuristique + IA).
    Sert au tri : une annonce sans chargeur explicite passe sous un pack complet.
    """
    if row.get("is_accessory"):
        return 12.0
    cat = str(row.get("item_type") or "")
    score = 44.0

    if cat == "console_ds":
        if row_has_power(row):
            score += 30.0
        elif row.get("missing_charger") or row.get("missing_power"):
            score -= 35.0
        elif str(row.get("charger_status") or "") == "without":
            score -= 32.0
        else:
            score -= 8.0
        if row_has_stylus(row):
            score += 14.0
        elif row.get("missing_stylus"):
            score -= 14.0
        if row.get("includes_games"):
            score += 10.0
        if row.get("has_box"):
            score += 6.0
        if row.get("has_controller"):
            score += 5.0
    elif cat.startswith("console_"):
        if row_has_power(row):
            score += 22.0
        elif row.get("missing_charger") or row.get("missing_power"):
            score -= 28.0
        if row.get("has_controller"):
            score += 18.0
        elif re.search(r"(?i)\b(sans\s*manette|no\s*controller)\b", row_source_text(row)):
            score -= 20.0
        if row.get("has_hdmi"):
            score += 6.0
        if row.get("has_box"):
            score += 5.0
    elif cat == "projector":
        if row.get("has_hdmi"):
            score += 16.0
        elif row.get("missing_hdmi"):
            score -= 22.0
        if row_has_power(row):
            score += 10.0
        if row.get("has_remote"):
            score += 6.0
        try:
            lum = int(row.get("lumens") or 0)
            if 1800 <= lum <= 4500:
                score += 8.0
        except (TypeError, ValueError):
            pass
    elif cat == "phone":
        if row.get("icloud_locked"):
            score -= 40.0
        elif row.get("phone_unlocked"):
            score += 12.0
        try:
            bat = int(row.get("battery_health_pct") or 0)
            if bat >= 88:
                score += 14.0
            elif bat >= 80:
                score += 6.0
            elif 0 < bat < 75:
                score -= 18.0
        except (TypeError, ValueError):
            pass
        if row.get("storage_gb"):
            score += 4.0
        if not row.get("screen_defect"):
            score += 6.0
    elif cat == "wearable":
        if row.get("has_gps"):
            score += 8.0
        if row_has_power(row):
            score += 12.0
        if row.get("has_box"):
            score += 5.0
    elif cat == "ac":
        if row.get("has_hose"):
            score += 14.0
        if row.get("has_remote"):
            score += 8.0
        try:
            btu = int(row.get("btu") or 0)
            if btu >= 9000:
                score += 10.0
        except (TypeError, ValueError):
            pass
    else:
        if row.get("complete_hint"):
            score += 10.0
        if row_has_power(row):
            score += 8.0

    comp = int(row.get("ai_completeness") or 0)
    if comp > 0:
        score = score * 0.48 + comp * 0.52
    if row.get("deal_blocked"):
        score -= 25.0
    return round(max(0.0, min(100.0, score)), 1)


def spec_tier_from_pack_score(pack: float, row: Dict[str, Any]) -> str:
    if row.get("deal_blocked"):
        return "D"
    if pack >= 78.0:
        return "A"
    if pack >= 58.0:
        return "B"
    if pack >= 38.0:
        return "C"
    return "D"


def apply_listing_intelligence(rows: List[Dict[str, Any]], *, category: str = "") -> None:
    """Tableau specs/pack par annonce — enrichit pack_score, spec_tier, charger_status."""
    for row in rows:
        reconcile_pack_signals(row)
        try:
            from .engine import enrich_listing_taxonomy

            enrich_listing_taxonomy(row)
        except Exception:
            pass
        ps = compute_pack_score(row)
        row["pack_score"] = ps
        row["spec_tier"] = spec_tier_from_pack_score(ps, row)
        if not row.get("quality_grade") or str(row.get("quality_grade")) in ("—", ""):
            row["quality_grade"] = row["spec_tier"]
        if row.get("ai_specs_summary"):
            row["spec_summary"] = str(row["ai_specs_summary"])[:200]
        else:
            bits: List[str] = []
            if str(row.get("product_model_label") or row.get("ai_model_exact") or ""):
                bits.append(str(row.get("product_model_label") or row.get("ai_model_exact")))
            if row_has_power(row):
                bits.append("chargeur ✓")
            elif row.get("missing_charger") or row.get("missing_power"):
                bits.append("sans chargeur")
            if _stylus_relevant(row, row_text(row)):
                if row_has_stylus(row):
                    bits.append("stylet ✓")
                elif row.get("missing_stylus"):
                    bits.append("sans stylet")
            if row.get("includes_games"):
                bits.append("jeux")
            row["spec_summary"] = " · ".join(bits)[:200]


def raw_rank_signal(row: Dict[str, Any]) -> float:
    """Signal composite non plafonné — sert au tri relatif sur tout le run."""
    try:
        bds = row.get("best_deal_score")
        if bds is not None and str(bds) != "":
            base = float(bds)
            eng = math.log1p(float(row.get("engagement") or 0)) * 0.8
            tier = str(row.get("deal_tier") or "")
            if tier == "dropship_suspect":
                base = min(base, 28.0)
            elif tier == "pepite":
                base += 2.0
            return base + eng
    except (TypeError, ValueError):
        pass
    try:
        cs = row.get("score_composite") or row.get("composite_score")
        if cs is not None and str(cs) != "":
            base_cs = float(cs)
            eng = math.log1p(float(row.get("engagement") or 0)) * 1.2
            tier = str(row.get("deal_tier") or "")
            if tier == "dropship_suspect":
                base_cs = min(base_cs, 28.0)
            elif tier == "pepite":
                base_cs += 4.0
            return base_cs + eng
    except (TypeError, ValueError):
        pass
    try:
        sc = float(row.get("final_score") or 0.0)
    except (TypeError, ValueError):
        sc = 40.0
    rel = float(row.get("ai_title_relevance") or 0)
    try:
        disc = float(row.get("discount_vs_new_pct") or row.get("market_discount_pct") or 0)
    except (TypeError, ValueError):
        disc = 0.0
    disc_pts = min(20.0, max(-12.0, disc * 0.2)) if disc else 0.0
    cat = str(row.get("item_type") or row.get("market_category") or "")
    qvs = float(row.get("quality_value_score") or 0)
    if cat == "projector" and qvs > 0:
        pack_pts = (qvs - 50.0) * 0.42
    else:
        pack = float(row.get("pack_score") or compute_pack_score(row))
        pack_pts = (pack - 50.0) * 0.38
    eng = math.log1p(float(row.get("engagement") or 0)) * 2.2
    seller_pts = 0.0
    try:
        from .scoring import seller_trust_adjustment

        seller_pts, _, _ = seller_trust_adjustment(row)
    except Exception:
        if int(row.get("seller_feedback_count") or 0) >= 5:
            seller_pts = min(10.0, float(row.get("seller_rating") or 0) * 10.0)
        else:
            seller_pts = -4.0
    risk_pen = -45.0 if row.get("deal_blocked") else 0.0
    acc_pen = -55.0 if row.get("is_accessory") else 0.0
    try:
        manual = float(row.get("manual_score_delta") or 0)
    except (TypeError, ValueError):
        manual = 0.0
    grade = str(row.get("quality_grade") or "").upper()
    grade_pts = 4.0 if grade == "A" else (-2.0 if grade == "C" else 0.0)
    signal = (
        sc * 0.58
        + rel * 0.08
        + disc_pts
        + pack_pts
        + eng
        + seller_pts
        + risk_pen
        + acc_pen
        + manual
        + grade_pts
    )
    try:
        from .engine import brand_sort_bonus

        signal += brand_sort_bonus(row)
    except Exception:
        pass
    return signal


def promote_doubtful_listings(
    rows: List[Dict[str, Any]],
    *,
    category: str = "",
    intent: Optional[SearchIntent] = None,
) -> int:
    """Promeut les douteux avec specs solides ou pertinence titre élevée."""
    cat = category or ""
    promoted = 0
    for r in rows:
        if str(r.get("triage_decision") or "") != "doubtful":
            continue
        if not can_promote_doubtful_row(r, intent=intent, category=cat):
            continue
        reasons = str(r.get("triage_reasons") or "")
        try:
            sc = float(r.get("score_composite") or r.get("final_score") or 0)
            rel = float(r.get("ai_title_relevance") or r.get("triage_confidence") or 0)
            if rel <= 1.0:
                rel *= 100.0
        except (TypeError, ValueError):
            continue
        specs = r.get("extracted_specs") if isinstance(r.get("extracted_specs"), dict) else {}
        if cat == "projector" or str(r.get("item_type") or "") == "projector":
            lum = int(specs.get("lumens") or r.get("lumens") or 0)
            brand = str(specs.get("projector_brand") or r.get("projector_brand") or "")
            try:
                from .scoring import effective_price_eur

                price = effective_price_eur(r)
            except Exception:
                price = float(r.get("total_price") or r.get("price") or 0)
            if brand and 80 <= price <= 900:
                r["triage_decision"] = "keep"
                r["triage_promoted"] = "projector_brand_price"
                promoted += 1
                continue
            if brand and lum >= 1500 and rel >= 55:
                r["triage_decision"] = "keep"
                r["triage_promoted"] = "projector_specs"
                promoted += 1
                continue
            if "projector_bundle" in reasons or "projector_in_title" in reasons:
                r["triage_decision"] = "keep"
                r["triage_promoted"] = "projector_bundle"
                promoted += 1
                continue
            if sc > 55 and rel > 65:
                r["triage_decision"] = "keep"
                r["triage_promoted"] = "projector_score"
                promoted += 1
                continue
        elif sc > PROMOTE_DOUBTFUL_MIN_SCORE and rel > PROMOTE_DOUBTFUL_MIN_REL:
            if cat in ("ac", "clim", "climatisation") or str(r.get("item_type") or "") in (
                "ac",
                "clim",
                "climatisation",
            ):
                title = str(r.get("title") or "")
                if _ac_off_category_title(title):
                    continue
                specs = r.get("extracted_specs") if isinstance(r.get("extracted_specs"), dict) else {}
                if not (
                    specs.get("ac_is_unit")
                    or specs.get("capacity_btu")
                    or _ac_title_matches_intent(title, SearchIntent(product_families=["ac"]))
                ):
                    continue
            r["triage_decision"] = "keep"
            r["triage_promoted"] = "score_composite"
            promoted += 1
        elif cat in ("console_ps4", "console_ps5") or str(r.get("item_type") or "") in (
            "console_ps4",
            "console_ps5",
        ):
            if _console_ps4_title_matches_intent(str(r.get("title") or ""), SearchIntent(product_families=[cat or "console_ps4"])):
                r["triage_decision"] = "keep"
                r["triage_promoted"] = "ps4_title_match"
                promoted += 1
    return promoted


def normalize_run_rank_scores(
    rows: List[Dict[str, Any]],
    *,
    main_only: bool = True,
) -> None:
    """
    Classement relatif + scores absolus cohérents (best_deal_score = affichage).
    """
    pool = [r for r in rows if not r.get("is_accessory")] if main_only else list(rows)
    if not pool:
        pool = list(rows)
    if not pool:
        return

    for r in pool:
        if r.get("best_deal_score") is None:
            try:
                from .scoring import compute_best_deal_score

                r["best_deal_score"] = compute_best_deal_score(r)
            except Exception:
                r["best_deal_score"] = round(raw_rank_signal(r), 1)

    def _tier_sort(t: str) -> int:
        return {"gem": 0, "ok": 1, "suspicious": 2, "dropship": 3, "dinosaur": 4}.get(t, 2)

    def _sort_key(r: Dict[str, Any]) -> Tuple[float, int, float]:
        try:
            bds = float(r.get("best_deal_score") or 0)
        except (TypeError, ValueError):
            bds = 0.0
        tier_pri = _tier_sort(str(r.get("quality_tier") or "ok"))
        try:
            price = float(r.get("total_price") or r.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        return (-bds, tier_pri, price)

    pool.sort(key=_sort_key)
    n = len(pool)

    for i, r in enumerate(pool):
        rank_pos = i + 1
        bds = float(r.get("best_deal_score") or 0)
        r["rank_position"] = rank_pos
        r["raw_rank_signal"] = round(bds, 2)
        r["display_rank_score"] = round(bds, 1)
        r["rank_score"] = round(bds, 1)
        r["rank_percentile"] = round(100.0 * (1.0 - (i + 0.5) / n), 1)

    acc_rank = n
    for r in rows:
        if r.get("is_accessory"):
            acc_rank += 1
            r["rank_position"] = acc_rank
            r["display_rank_score"] = 12.0
            r["rank_score"] = 12.0
            r["best_deal_score"] = 12.0
            r["rank_percentile"] = 5.0


def clamp_discount_pct(value: Any, *, lo: float = -95.0, hi: float = 95.0) -> float:
    """Borne les % décote (évite -8000% si référence oracle aberrante)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v) or abs(v) > 500:
        return 0.0
    return round(max(lo, min(hi, v)), 1)


def price_value_adjustment(row: Dict[str, Any]) -> float:
    """Bonus/malus prix vs médiane modèle — même article, prix compte."""
    try:
        from .scoring import effective_price_eur

        price = effective_price_eur(row)
    except Exception:
        try:
            price = float(row.get("total_price") or row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
    if price <= 0:
        return 0.0
    try:
        med = float(row.get("model_market_median_eur") or 0)
    except (TypeError, ValueError):
        med = 0.0
    if med > 0:
        ratio = price / med
        if ratio <= 0.88:
            return min(22.0, (0.88 - ratio) * 55.0)
        if ratio >= 1.06:
            return -min(35.0, (ratio - 1.06) * 70.0)
    disc = row.get("model_vs_market_pct") or row.get("market_discount_pct")
    try:
        d = float(disc or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d > 5:
        return min(15.0, d * 0.18)
    if d < -5:
        return max(-28.0, d * 0.28)
    return 0.0


def confirmed_screen_defect(row: Dict[str, Any]) -> bool:
    """Doute écran seulement si défaut confirmé (pas « écrans nickels »)."""
    if not row.get("screen_defect") and not row.get("defect_hint"):
        return False
    t = row_source_text(row)
    if _SCREEN_POSITIVE.search(t):
        return False
    if _SCREEN_ISSUE_STRONG.search(t):
        return True
    if re.search(r"(?i)\b(d[ée]faut|cass[ée]|hs|pixel\s*mort|point\s*noir)\b", t):
        return True
    return False


def rank_score_for_sort(row: Dict[str, Any]) -> float:
    """Score de tri UI — utilise le score relatif normalisé si présent."""
    if row.get("rank_score") is not None:
        try:
            base = float(row["rank_score"])
            return round(max(0.0, min(99.0, base + price_value_adjustment(row))), 2)
        except (TypeError, ValueError):
            pass
    try:
        sc = float(row.get("composite_score") or row.get("final_score") or 0.0)
    except (TypeError, ValueError):
        sc = 0.0
    pack = float(row.get("pack_score") or 0)
    if pack > 0:
        sc = sc * 0.82 + pack * 0.08
    sc += price_value_adjustment(row)
    try:
        sc += float(row.get("manual_score_delta") or 0)
    except (TypeError, ValueError):
        pass
    return round(max(0.0, min(99.0, sc)), 2)


def display_rank_score(row: Dict[str, Any]) -> float:
    """Score affiché (tri + UI) — best_deal_score absolu si présent."""
    if row.get("best_deal_score") is not None:
        try:
            return float(row["best_deal_score"])
        except (TypeError, ValueError):
            pass
    if row.get("display_rank_score") is not None:
        try:
            return float(row["display_rank_score"])
        except (TypeError, ValueError):
            pass
    return rank_score_for_sort(row)


def apply_category_spec_scoring(row: Dict[str, Any], weights: Optional[ScoreWeights] = None) -> None:
    """Ajuste le score composite selon specs extraites (lumens, résolution, câbles…)."""
    w = weights or get_weights()
    cat = str(row.get("item_type") or "")
    if row.get("score_base") is None:
        try:
            row["score_base"] = float(row.get("final_score") or 0.0)
        except (TypeError, ValueError):
            row["score_base"] = 0.0
    try:
        base = float(row.get("score_base") or 0.0)
    except (TypeError, ValueError):
        base = 0.0
    try:
        base += float(row.get("score_overlay") or 0.0)
    except (TypeError, ValueError):
        pass
    bd: Dict[str, float] = {}

    if cat == "projector":
        tier = str(row.get("quality_tier") or "ok")
        if tier == "dinosaur":
            p = -28.0 * w.dinosaur_penalty
            bd["dinosaur_penalty"] = p
            base += p
        elif tier == "dropship":
            p = -22.0 * w.category_bonus
            bd["dropship_penalty"] = p
            base += p
        elif tier == "suspicious":
            p = -10.0 * w.category_bonus
            bd["suspicious_penalty"] = p
            base += p
        elif tier == "gem":
            b = 8.0 * w.category_bonus
            bd["gem_bonus"] = b
            base += b
        try:
            lum = int(row.get("lumens") or 0)
        except (TypeError, ValueError):
            lum = 0
        if row.get("legacy_ports_only") or (row.get("missing_hdmi") and not row.get("has_hdmi")):
            p = -20.0 * w.projector_hdmi
            bd["no_hdmi_legacy"] = p
            base += p
        elif row.get("has_hdmi"):
            b = 6.0 * w.projector_hdmi
            bd["hdmi_modern"] = b
            base += b
        if str(row.get("native_aspect") or "") == "16:9":
            b = 5.0 * w.projector_aspect
            bd["aspect_169"] = b
            base += b
        elif row.get("native_aspect") == "4:3":
            p = -12.0 * w.projector_aspect
            bd["aspect_43"] = p
            base += p
        if lum >= 6000:
            p = -15.0 * w.projector_lumens
            bd["spec_inflated_lumens"] = p
            base += p
        elif 2000 <= lum <= 4500 and not projector_office_wxga(row):
            b = 6.0 * w.projector_lumens
            bd["spec_lumens_realistic"] = b
            base += b
        elif projector_office_wxga(row):
            p = -16.0 * w.projector_aspect
            bd["office_wxga_penalty"] = p
            base += p
        elif 0 < lum < 500:
            p = -18.0 * w.projector_lumens
            bd["spec_lumens_toy"] = p
            base += p
        res = str(row.get("resolution") or "").lower()
        if res in ("wvga", "xga"):
            p = -10.0 * w.projector_aspect
            bd["resolution_obsolete"] = p
            base += p
        elif res == "wxga":
            p = -12.0 * w.projector_aspect
            bd["resolution_wxga"] = p
            base += p
        elif res == "1080p" and tier not in ("dropship", "dinosaur"):
            b = 4.0 * w.projector_aspect
            bd["spec_resolution_1080p"] = b
            base += b
        if row.get("has_power_cable"):
            b = 3.0 * w.completeness
            bd["spec_power"] = b
            base += b
        elif row.get("missing_power"):
            p = -8.0 * w.risk
            bd["spec_missing_power"] = p
            base += p
        if row.get("has_remote"):
            b = 2.0 * w.completeness
            bd["spec_remote"] = b
            base += b
        elif row.get("missing_remote"):
            p = -4.0 * w.risk
            bd["spec_missing_remote"] = p
            base += p
        if row.get("has_box"):
            b = 2.0 * w.completeness
            bd["spec_box"] = b
            base += b
    elif cat == "console_ds":
        req_ds = str(row.get("required_ds_family") or "")
        fam = str(row.get("ds_model_family") or "")
        if req_ds and fam != "unknown":
            if _ds_family_matches(req_ds, fam):
                b = 14.0 * w.category_bonus
                bd["model_intent_match"] = b
                base += b
            else:
                p = -32.0 * w.category_bonus
                bd["model_intent_mismatch"] = p
                base += p
        if row.get("has_stylus"):
            b = 5.0 * w.completeness
            bd["spec_stylus"] = b
            base += b
        elif row.get("missing_stylus"):
            p = -7.0 * w.completeness
            bd["spec_missing_stylus"] = p
            base += p
        if row_has_power(row):
            b = 5.0 * w.completeness
            bd["spec_charger"] = b
            base += b
        elif row.get("missing_charger") or row.get("missing_power"):
            p = -22.0 * w.completeness
            bd["spec_missing_charger"] = p
            base += p
        elif str(row.get("charger_status") or "") == "unknown" and "console" in str(row.get("title") or "").lower():
            p = -5.0 * w.completeness
            bd["spec_charger_unknown"] = p
            base += p
        if row.get("includes_games"):
            b = 3.0 * w.completeness
            bd["spec_games"] = b
            base += b
        if row.get("has_box"):
            b = 2.0 * w.completeness
            bd["spec_box"] = b
            base += b
        if row.get("has_controller"):
            b = 2.0 * w.completeness
            bd["spec_controller"] = b
            base += b
    elif cat in ("console_ps4", "console_ps5", "console_switch", "console_xbox"):
        if row_has_power(row):
            b = 3.0 * w.completeness
            bd["spec_charger"] = b
            base += b
        if row.get("has_controller"):
            b = 4.0 * w.completeness
            bd["spec_controller"] = b
            base += b
    elif cat == "ac":
        try:
            btu = int(row.get("btu") or 0)
        except (TypeError, ValueError):
            btu = 0
        if btu >= 12000:
            b = 8.0 * w.ac_btu
            bd["spec_btu_high"] = b
            base += b
        elif 0 < btu < 7000:
            p = -10.0 * w.ac_btu
            bd["spec_btu_low"] = p
            base += p
        if row.get("has_hose"):
            b = 4.0 * w.completeness
            bd["spec_hose"] = b
            base += b
    elif cat in ("apparel", "shoes", "jeans"):
        if row.get("brand") and str(row.get("brand")).strip():
            b = 3.0 * w.apparel_brand
            bd["brand_known"] = b
            base += b
        req_br = str(row.get("required_brand") or "").lower()
        title_l = str(row.get("title") or "").lower()
        if req_br:
            if req_br in title_l:
                b = 10.0 * w.apparel_brand
                bd["brand_intent_match"] = b
                base += b
            else:
                p = -18.0 * w.apparel_brand
                bd["brand_intent_mismatch"] = p
                base += p
        req_sz = str(row.get("required_apparel_size") or "").upper()
        if req_sz:
            if re.search(rf"(?i)\b{re.escape(req_sz)}\b", title_l):
                b = 6.0 * w.completeness
                bd["size_match"] = b
                base += b
            else:
                p = -12.0 * w.completeness
                bd["size_missing"] = p
                base += p
        if re.search(r"(?i)\b(trou[ée]|tache|d[ée]chir|us[ée]\s*abim|worn\s*out|stained)\b", title_l):
            p = -14.0 * w.risk
            bd["condition_worn"] = p
            base += p

    if cat != "projector":
        tier = str(row.get("quality_tier") or "ok")
        tier_map = {
            "gem": 8.0,
            "suspicious": -10.0,
            "dropship": -22.0,
            "dinosaur": -28.0,
        }
        if tier in tier_map:
            adj_t = tier_map[tier] * w.category_bonus
            bd[f"tier_{tier}"] = adj_t
            base += adj_t

    try:
        bf = float(row.get("buyer_fit_score") or 0)
    except (TypeError, ValueError):
        bf = 0.0
    if bf > 0:
        adj = (bf - 50.0) * 0.35 * w.buyer_fit
        bd["buyer_fit"] = adj
        base += adj

    try:
        from .models import discount_vs_model_range, model_rank_score_bonus

        mrank = model_rank_score_bonus(row, weight=w.category_bonus)
        if mrank:
            bd["model_rank_value"] = mrank
            base += mrank
        mdisc = discount_vs_model_range(row)
        if mdisc is not None and row.get("market_discount_pct") is None:
            row["model_vs_expected_pct"] = mdisc
    except Exception:
        pass

    row["final_score"] = round(min(100.0, max(0.0, base)), 2)
    if bd:
        row["score_breakdown"] = bd


# ---------------------------------------------------------------------------
# Conseils acheteur
# ---------------------------------------------------------------------------


_BREAKDOWN_LABELS: Dict[str, str] = {
    "spec_charger": "Chargeur / alim.",
    "spec_controller": "Manette",
    "spec_stylus": "Stylet",
    "spec_games": "Jeux inclus",
    "spec_hdmi": "HDMI",
    "spec_box": "Boîte",
    "spec_hose": "Tuyau clim",
    "spec_btu_high": "BTU élevés",
    "spec_btu_low": "BTU faibles",
    "brand_known": "Marque reconnue",
    "buyer_fit": "Adéquation acheteur",
    "accessory_penalty": "Accessoire",
    "risk_penalty": "Risques",
    "market_discount": "Décote marché",
    "completeness": "Complétude pack",
}


def format_score_breakdown(row: Dict[str, Any]) -> List[str]:
    """Lignes lisibles pour le détail annonce (composition du score)."""
    analysis = str(row.get("analysis_detail") or "").strip()
    if analysis:
        lines = [analysis, ""]
    else:
        lines = []
    bd = row.get("score_breakdown")
    if isinstance(bd, str):
        try:
            bd = json.loads(bd.replace("'", '"'))
        except Exception:
            bd = {}
    if isinstance(bd, dict) and bd and all(isinstance(v, dict) for v in bd.values()):
        lines.append("Dimensions hyper-pro (0-100) :")
        for key, info in bd.items():
            if not isinstance(info, dict):
                continue
            sc = info.get("score", 0)
            expl = str(info.get("explanation") or "")[:100]
            lines.append(f"  · {key}: {sc:.0f} — {expl}")
        comp = row.get("score_composite") or row.get("composite_score")
        if comp is not None:
            lines.append(f"  → composite {float(comp):.1f} · tier {row.get('deal_tier') or '—'}")
        return lines
    if not isinstance(bd, dict) or not bd:
        base = row.get("score_base")
        overlay = row.get("score_overlay")
        lines = ["Composition : (détail non enregistré sur cette annonce)"]
        if base is not None:
            lines.append(f"  · base heuristique ≈ {float(base):.1f}")
        if overlay is not None and float(overlay or 0) != 0:
            lines.append(f"  · ajustements ≈ {float(overlay):+.1f}")
        lines.append(f"  · score affiché {float(row.get('final_score') or 0):.1f}")
        return lines
    items = sorted(bd.items(), key=lambda kv: abs(float(kv[1] or 0)), reverse=True)
    lines = ["Composition du score :"]
    total_adj = 0.0
    for key, val in items:
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        total_adj += v
        sign = "+" if v >= 0 else ""
        lbl = _BREAKDOWN_LABELS.get(key, key.replace("_", " "))
        lines.append(f"  · {lbl}: {sign}{v:.1f}")
    lines.append(f"  → contributions listées ≈ {total_adj:+.1f} pts")
    return lines


def deal_advice_lines(row: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    sc = float(
        row.get("score_composite")
        or row.get("composite_score")
        or row.get("display_rank_score")
        or row.get("rank_score")
        or row.get("final_score")
        or 0
    )
    tier = str(row.get("deal_tier") or "")
    if tier == "pepite" or sc >= 75:
        lines.append("✓ Bonne opportunité — score élevé vs le marché.")
    elif tier in ("bonne_affaire", "correct") or sc >= 55:
        lines.append("○ Deal correct — vérifier état et pack.")
    elif tier in ("dropship_suspect", "douteux", "rejet"):
        lines.append("△ Prudence — risque ou écart vs la recherche.")
    else:
        lines.append("△ Score modeste — comparer d'autres annonces.")
    disc = row.get("discount_vs_new_pct") or row.get("market_discount_pct")
    if disc is not None:
        try:
            lines.append(f"Décote ~{float(disc):.0f}% vs référence.")
        except (TypeError, ValueError):
            pass
    if row.get("risk_flags") or row.get("deal_blocked"):
        lines.append(f"⚠ Risques : {row.get('risk_flags') or 'bloqué'}.")
    cat = str(row.get("item_type") or "")
    if cat == "console_ds":
        pack_ok = []
        pack_miss = []
        if row.get("has_stylus"):
            pack_ok.append("stylet")
        elif row.get("missing_stylus"):
            pack_miss.append("stylet")
        if row_has_power(row):
            pack_ok.append("chargeur")
        elif row.get("missing_charger") or row.get("missing_power"):
            pack_miss.append("chargeur")
        if row.get("includes_games"):
            pack_ok.append("jeux")
        if pack_ok:
            lines.append(f"✓ Pack confirmé : {', '.join(pack_ok)}.")
        for m in pack_miss:
            lines.append(f"○ {m.capitalize()} non confirmé — vérifier annonce.")
    elif cat.startswith("console_") and not row_has_controller(row):
        lines.append("○ Manette non confirmée.")
    tier = str(row.get("quality_tier") or "")
    if tier == "dinosaur" and cat == "projector":
        lines.insert(
            0,
            "✕ Dinosaure bureautique — connectique VGA/DVI sans HDMI, format 4:3/WVGA ou lumens trop faibles : invendable grand public.",
        )
    elif tier == "dinosaur":
        lines.insert(0, "✕ Qualité douteuse — vérifier état et conformité au modèle recherché.")
    vg = str(row.get("viability_grade") or "")
    if vg and vg in ("D", "F"):
        lines.append(f"○ Viabilité technique : {vg} ({row.get('viability_issues') or 'voir specs'}).")
    notes = str(row.get("buyer_fit_notes") or "").strip()
    if notes and not (
        cat == "console_ds"
        and row_has_power(row)
        and row.get("has_stylus")
        and re.search(r"(?i)(vérifier|confirmer).*(chargeur|stylet)", notes)
    ):
        lines.append(f"○ IA acheteur : {notes[:220]}")
    if cat == "projector":
        lum = row.get("lumens")
        if lum:
            lines.append(f"○ {lum} lumens — vérifier ANSI vs LED marketing.")
        asp = row.get("native_aspect")
        if asp:
            lines.append(f"○ Format natif : {asp}.")
        if row.get("legacy_ports_only") or (row.get("missing_hdmi") and not row.get("has_hdmi")):
            lines.append("○ Pas de HDMI moderne détecté — console / Chromecast impossibles sans adaptateur.")
        if row.get("resolution"):
            lines.append(f"○ Résolution déclarée : {row.get('resolution')}.")
        pack = []
        if row.get("has_hdmi"):
            pack.append("HDMI")
        if row.get("has_power_cable"):
            pack.append("alim")
        if row.get("has_remote"):
            pack.append("télécommande")
        if pack:
            lines.append(f"○ Pack détecté : {', '.join(pack)}.")
        elif not row.get("description"):
            lines.append("○ Vérifier câbles / télécommande (description absente).")
    if row.get("ai_verdict"):
        lines.append(str(row["ai_verdict"])[:260])
    return lines[:8]


# ---------------------------------------------------------------------------
# Timing / ETA
# ---------------------------------------------------------------------------

_PROFILE_PATH = Path("brouillon") / "data" / "timing_profile.json"


@dataclass
class TimingProfile:
    session_sec: float = 12.0
    catalog_base_sec: float = 3.0
    catalog_per_item_sec: float = 0.011
    ai_title_per_chunk_sec: float = 2.6
    scoring_per_item_sec: float = 0.004
    deep_fetch_per_item_sec: float = 0.32
    deep_top_k: int = 50
    ai_specs_sec: float = 7.0
    ai_verify_per_item_sec: float = 0.18
    ai_vision_count: int = 8
    ai_vision_per_item_sec: float = 1.35
    export_sec: float = 6.0
    filters_sec: float = 2.5


def load_profile() -> TimingProfile:
    try:
        if _PROFILE_PATH.is_file():
            data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = asdict(TimingProfile())
                return TimingProfile(**{**base, **{k: data[k] for k in base if k in data}})
    except Exception:
        pass
    return TimingProfile()


def save_profile(p: TimingProfile) -> None:
    _PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PROFILE_PATH.write_text(json.dumps(asdict(p), indent=2), encoding="utf-8")


def _ema(old: float, new: float, alpha: float = 0.35) -> float:
    return old * (1 - alpha) + new * alpha


@dataclass
class RunEstimator:
    catalog_n: int = 0
    work_n: int = 0
    use_ai: bool = True
    profile: TimingProfile = field(default_factory=load_profile)
    t0: float = field(default_factory=time.monotonic)
    step_actuals: Dict[str, float] = field(default_factory=dict)
    extra_pause_sec: float = 0.0
    ai_title_chunks: int = 0
    scrape_variants: int = 0
    n_platforms: int = 1
    scrape_platforms: List[str] = field(default_factory=list)
    catalog_eta_cap_sec: float = 0.0
    _forecast_ema: Optional[float] = None

    _step_t0: float = 0.0

    def start_step(self, step_id: str) -> None:
        self._step_t0 = time.monotonic()

    def end_step(self, step_id: str) -> float:
        dur = max(0.1, time.monotonic() - self._step_t0)
        self.step_actuals[step_id] = self.step_actuals.get(step_id, 0.0) + dur
        return dur

    def set_catalog_count(self, n: int) -> None:
        self.catalog_n = max(0, int(n))
        self.work_n = self.catalog_n

    def set_work_count(self, n: int) -> None:
        self.work_n = max(0, int(n))

    def set_ai_title_chunks(self, chunks: int) -> None:
        self.ai_title_chunks = max(0, int(chunks))

    def set_scrape_plan(
        self,
        catalog_est: int,
        variants: int,
        *,
        n_platforms: int = 1,
        scrape_platforms: Optional[List[str]] = None,
    ) -> None:
        self.catalog_n = max(0, int(catalog_est))
        self.scrape_variants = max(1, int(variants))
        self.n_platforms = max(1, int(n_platforms))
        self.scrape_platforms = list(scrape_platforms or [])
        try:
            from vf_scrape.config import scrape_mode_profile

            self.catalog_eta_cap_sec = float(scrape_mode_profile().catalog_eta_cap_sec)
        except Exception:
            self.catalog_eta_cap_sec = 900.0

    def _platform_catalog_budget(self) -> float:
        """Budget catalogue basé sur les budgets par plateforme (parallèle)."""
        plats = self.scrape_platforms or []
        if not plats:
            return 0.0
        try:
            from vf_scrape.config import platform_time_budget_sec

            budgets = [platform_time_budget_sec(p) for p in plats]
            budgets = [b for b in budgets if b > 0]
            if budgets:
                return float(max(budgets) + min(25.0, 0.15 * sum(budgets)))
        except Exception:
            pass
        per_var = 11.0 if self.n_platforms >= 5 else 16.0
        return self.profile.session_sec * 0.55 + self.scrape_variants * per_var

    def elapsed_sec(self) -> float:
        return time.monotonic() - self.t0

    def _catalog_n_for_budget(self) -> int:
        """Taille catalogue pour l'estimation (défaut ~900 annonces si pas encore connu)."""
        if self.catalog_n > 0:
            return self.catalog_n
        return 450

    def planned_seconds(self) -> float:
        """Durée totale prévue pour tout le run (budget phases)."""
        p = self.profile
        n = self._catalog_n_for_budget()
        w = max(1, self.work_n if self.work_n > 0 else n)
        k = min(TOP_DEEP_K, w)
        if self.scrape_variants > 0:
            catalog_budget = self._platform_catalog_budget()
        else:
            catalog_budget = p.catalog_base_sec + n * p.catalog_per_item_sec
        total = (
            catalog_budget
            + 2.5
            + w * p.scoring_per_item_sec
            + k * p.deep_fetch_per_item_sec
            + p.export_sec
            + p.filters_sec
        )
        if self.use_ai:
            chunks = self.ai_title_chunks or max(1, (min(n, 400) + 23) // 24)
            total += (
                chunks * p.ai_title_per_chunk_sec
                + p.ai_specs_sec
                + k * p.ai_verify_per_item_sec
                + TOP_VISION_K * p.ai_vision_per_item_sec
            )
        return max(45.0, min(total, self.catalog_eta_cap_sec or total + 600.0))

    def _phase_budget(self, *, remaining_only: bool = False) -> tuple[float, float]:
        """(budget phases terminées, budget phases restantes) en secondes."""
        p = self.profile
        n = self._catalog_n_for_budget()
        w = max(1, self.work_n if self.work_n > 0 else n)
        k = min(TOP_DEEP_K, w)

        phases: List[Tuple[str, float, str]] = [
            ("session", p.session_sec, "session"),
            ("catalog", p.catalog_base_sec + n * p.catalog_per_item_sec, "catalog"),
            ("triage", 2.5, "triage"),
        ]
        if self.use_ai:
            chunks = self.ai_title_chunks or max(1, (min(n, 400) + 23) // 24)
            phases.append(("ai_title", chunks * p.ai_title_per_chunk_sec, "ai_title"))
        phases.extend(
            [
                ("scoring", w * p.scoring_per_item_sec, "scoring"),
                ("deep_fetch", k * p.deep_fetch_per_item_sec, "deep_fetch"),
            ]
        )
        if self.use_ai:
            phases.extend(
                [
                    ("ai_specs", p.ai_specs_sec, "ai_specs"),
                    ("ai_verify", k * p.ai_verify_per_item_sec, "ai_verify"),
                    ("vision", TOP_VISION_K * p.ai_vision_per_item_sec, "vision"),
                ]
            )
        phases.append(("export", p.export_sec + p.filters_sec, "export"))

        done = 0.0
        left = 0.0
        for _name, budget, key in phases:
            if key in self.step_actuals:
                done += self.step_actuals[key]
            else:
                left += budget
        left += self.extra_pause_sec

        if remaining_only:
            return done, left
        return done, done + left

    def _catalog_phase_left(self, **extra: Any) -> float:
        """Budget restant pour la phase catalogue (pagination / plateformes en cours)."""
        p = self.profile
        plat_done = set(extra.get("platform_done") or [])
        plat_elapsed = extra.get("platform_elapsed") or {}
        plats = self.scrape_platforms or []
        if plats:
            try:
                from vf_scrape.config import platform_time_budget_sec

                left = 0.0
                for pk in plats:
                    if pk in plat_done:
                        continue
                    bud = float(platform_time_budget_sec(pk) or 90.0)
                    if pk in plat_elapsed:
                        left += max(8.0, bud - float(plat_elapsed[pk]))
                    else:
                        left += bud
                if left > 0:
                    return left
            except Exception:
                pass
        n = max(int(extra.get("catalog_count") or 0), self.catalog_n, self._catalog_n_for_budget())
        full = p.catalog_base_sec + n * p.catalog_per_item_sec
        cp = int(extra.get("catalog_page") or 0)
        tp = int(extra.get("catalog_pages") or 0)
        if cp > 0 and tp > 0:
            return full * (1.0 - min(1.0, cp / tp))
        return full

    def forecast_total_sec(self, *, step: int = 1, **extra: Any) -> float:
        """Durée totale estimée = écoulé + restant (rythme réel si possible)."""
        elapsed = self.elapsed_sec()
        done_b, left_b = self._phase_budget(remaining_only=True)

        if step == 2 and "catalog" not in self.step_actuals:
            full_cat = self.profile.catalog_base_sec + self._catalog_n_for_budget() * self.profile.catalog_per_item_sec
            left_b = left_b - full_cat + self._catalog_phase_left(**extra)

        dt = int(extra.get("deep_total") or 0)
        dd = int(extra.get("deep_done") or 0)
        if step >= 5 and dt > 0:
            p = self.profile
            k = min(TOP_DEEP_K, max(1, self.work_n or self._catalog_n_for_budget()))
            deep_budget = k * p.deep_fetch_per_item_sec
            if "deep_fetch" not in self.step_actuals:
                frac = min(1.0, dd / max(1, dt))
                left_b = left_b - deep_budget + deep_budget * (1.0 - frac)
            elif dd >= dt and "ai_specs" not in self.step_actuals:
                left_b = max(left_b, p.ai_specs_sec * 0.6)

        if done_b > 2 and elapsed > 2:
            pace = min(1.08, max(0.72, elapsed / max(done_b, 1.0)))
            remaining = left_b * pace
        else:
            remaining = left_b * 0.95

        raw_total = max(elapsed + 5.0, elapsed + remaining)
        cap = self.catalog_eta_cap_sec
        if cap > 0:
            raw_total = min(raw_total, cap)
        if self._forecast_ema is None:
            self._forecast_ema = raw_total
        else:
            self._forecast_ema = _ema(self._forecast_ema, raw_total, alpha=0.18)
        return max(elapsed + 3.0, self._forecast_ema)

    def eta_sec(self, *, step: int = 1, **extra: Any) -> float:
        elapsed = self.elapsed_sec()
        total = self.forecast_total_sec(step=step, **extra)
        raw = max(0.0, total - elapsed)
        pct = self.progress_fraction(step=step, **extra)
        if pct < 0.97:
            floor = 1.5 if pct < 0.85 else 0.5
            return max(raw * 0.96, floor)
        return raw

    def progress_fraction(self, *, step: int, **extra: Any) -> float:
        """Avancement par étape — pas elapsed/forecast (évite 99 % dès la connexion)."""
        step_start = {1: 0.0, 2: 0.08, 3: 0.40, 4: 0.52, 5: 0.65, 6: 0.96}
        step_end = {1: 0.08, 2: 0.40, 3: 0.52, 4: 0.65, 5: 0.96, 6: 1.0}
        lo = step_start.get(step, 0.0)
        hi = step_end.get(step, 1.0)
        if step == 2:
            cp = int(extra.get("catalog_page") or 0)
            tp = int(extra.get("catalog_pages") or 0)
            plat_prog = extra.get("platform_progress") or {}
            plats = self.scrape_platforms or list(plat_prog.keys())
            if plats:
                done_n = sum(1 for pk in plats if pk in (extra.get("platform_done") or []) or int(plat_prog.get(pk) or 0) > 0)
                span = hi - lo
                return min(hi - 0.01, lo + span * min(1.0, done_n / max(1, len(plats))))
            if cp > 0 and tp > 0:
                span = hi - lo
                return min(hi - 0.01, lo + span * min(1.0, cp / tp))
        if step == 3:
            done = int(extra.get("ai_doubtful_done") or 0)
            total = int(extra.get("ai_doubtful_total") or 0)
            if total > 0:
                span = hi - lo
                frac = min(1.0, done / total)
                return min(hi - 0.01, lo + span * frac)
            return lo + (hi - lo) * 0.08
        if step == 4:
            done = int(extra.get("score_done") or 0)
            total = int(extra.get("score_total") or 0)
            if done > 0 and total > 0:
                span = hi - lo
                return min(hi - 0.01, lo + span * min(1.0, done / total))
        if step == 5:
            sub = str(extra.get("subphase") or "")
            span = hi - lo
            if sub == "buyer_fit":
                return lo + span * 0.72
            if sub == "vision":
                vt = int(extra.get("vision_total") or 0)
                vd = int(extra.get("vision_done") or 0)
                if vt > 0:
                    return min(hi - 0.01, lo + span * min(1.0, vd / vt))
                return lo + span * 0.12
            if sub == "audit":
                ad = int(extra.get("audit_done") or 0)
                at = int(extra.get("audit_total") or 0)
                if at > 0:
                    return min(hi - 0.01, lo + span * min(1.0, ad / at))
                return lo + span * 0.75
            if sub == "specs":
                return lo + span * 0.55
            dt = int(extra.get("deep_total") or 0)
            dd = int(extra.get("deep_done") or 0)
            if dt > 0:
                return min(hi - 0.01, lo + span * min(1.0, dd / max(1, dt)) * 0.45)
            return lo + span * 0.2
        return lo + (hi - lo) * 0.06

    def progress_payload(self, *, step: int, step_name: str, human: str = "", **extra: Any) -> Dict[str, Any]:
        budget = round(self.planned_seconds(), 0)
        forecast = round(self.forecast_total_sec(step=step, **extra), 0)
        eta = max(0.0, self.eta_sec(step=step, **extra))
        return {
            "step": step,
            "step_name": step_name,
            "human": human,
            "elapsed_sec": round(self.elapsed_sec(), 1),
            "eta_sec": round(eta, 1),
            "planned_sec": max(budget, forecast),
            "planned_static_sec": budget,
            "pct": self.progress_fraction(step=step, **extra),
            "catalog_n": self.catalog_n,
            **extra,
        }

    def learn_from_run(self) -> None:
        """Met à jour le profil timing avec les durées réelles (EMA)."""
        p = self.profile
        n = max(1, self.catalog_n or self.work_n or 1)
        w = max(1, self.work_n or n)
        k = min(TOP_DEEP_K, w)
        mapping: List[Tuple[str, float, str]] = [
            ("session", p.session_sec, "session"),
            ("catalog", p.catalog_base_sec + n * p.catalog_per_item_sec, "catalog"),
            ("triage", 2.5, "triage"),
        ]
        if self.use_ai:
            chunks = self.ai_title_chunks or max(1, (min(n, 400) + 23) // 24)
            mapping.append(("ai_title", chunks * p.ai_title_per_chunk_sec, "ai_title"))
        mapping.extend(
            [
                ("scoring", w * p.scoring_per_item_sec, "scoring"),
                ("deep_fetch", k * p.deep_fetch_per_item_sec, "deep_fetch"),
            ]
        )
        if self.use_ai:
            mapping.extend(
                [
                    ("ai_specs", p.ai_specs_sec, "ai_specs"),
                    ("ai_verify", k * p.ai_verify_per_item_sec, "ai_verify"),
                    ("vision", TOP_VISION_K * p.ai_vision_per_item_sec, "vision"),
                ]
            )
        mapping.append(("export", p.export_sec + p.filters_sec, "export"))

        for _attr, _planned, step_id in mapping:
            actual = self.step_actuals.get(step_id)
            if actual is None:
                continue
            if step_id == "session":
                p.session_sec = _ema(p.session_sec, actual)
            elif step_id == "catalog" and n > 0:
                base_part = p.catalog_base_sec
                per = max(0.0, (actual - base_part) / n)
                p.catalog_per_item_sec = _ema(p.catalog_per_item_sec, per)
                p.catalog_base_sec = _ema(p.catalog_base_sec, min(actual, base_part * 1.5))
            elif step_id == "ai_title" and self.ai_title_chunks > 0:
                p.ai_title_per_chunk_sec = _ema(p.ai_title_per_chunk_sec, actual / self.ai_title_chunks)
            elif step_id == "vision" and TOP_VISION_K > 0:
                p.ai_vision_per_item_sec = _ema(p.ai_vision_per_item_sec, actual / TOP_VISION_K)
            elif step_id == "scoring" and w > 0:
                p.scoring_per_item_sec = _ema(p.scoring_per_item_sec, actual / w)
            elif step_id == "deep_fetch" and k > 0:
                p.deep_fetch_per_item_sec = _ema(p.deep_fetch_per_item_sec, actual / k)
            elif step_id == "ai_specs":
                p.ai_specs_sec = _ema(p.ai_specs_sec, actual)
            elif step_id == "ai_verify" and k > 0:
                p.ai_verify_per_item_sec = _ema(p.ai_verify_per_item_sec, actual / k)
            elif step_id == "export":
                p.export_sec = _ema(p.export_sec, actual * 0.7)
                p.filters_sec = _ema(p.filters_sec, actual * 0.3)
        save_profile(p)
        self.profile = p


def format_clock(seconds: float) -> str:
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}" if m < 60 else f"{m // 60}:{m % 60:02d}:{sec:02d}"


from .scoring import (  # noqa: E402
    apply_deal_caps,
    compute_market_stats,
    detect_accessory,
    infer_category,
    risk_flags,
    score_item_buy_mode,
    extract_specs,
)


# === merged ===

# --- criteria_plan.py ---

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .ai import _extract_json, _chat, api_key, cache_get, cache_put
from .engine import sanitize_dynamic_filters

logger = logging.getLogger("vf.criteria_plan")


_DEFAULT_SPEC_FIELDS = [
    # Common numeric specs we want cross-category
    "capacity_btu",
    "btu",
    "cooling_kw",
    "noise_db",
    "room_area_m2",
    "room_volume_m3",
    "power_w",
]


def _safe_text(s: Any, *, limit: int = 320) -> str:
    return str(s or "").replace("\n", " ").strip()[:limit]


def _row_blob(r: Dict[str, Any]) -> str:
    return f"{r.get('title') or ''}\n{r.get('description_excerpt') or r.get('description') or ''}".strip()


def _cache_key(*, category: str, query: str, buyer_priorities: str) -> str:
    cat = (category or "generic").strip().lower()
    q = re.sub(r"\s+", " ", (query or "").strip().lower())[:180]
    pr = re.sub(r"\s+", " ", (buyer_priorities or "").strip().lower())[:240]
    return f"criteria_plan:v1:{cat}:{hash((q, pr)) & 0xFFFFFFFF:08x}"


_CATEGORY_FALLBACK_EXCLUDES: Dict[str, List[str]] = {
    "ac": [
        r"compresseur(?:\s+de)?\s+clim",
        r"compresseur\s+clim\s+auto",
        r"rafra[iî]chisseur",
        r"location\s+(?:vacances|saisonni[eè]re)",
        r"pompe.*clim.*auto",
        r"recharge.*clim",
        r"composteur",
        r"t[eé]l[eé]commande",
        r"tuyau(?:\s+flexible)?\s+(?:de\s+)?clim",
        r"pi[eè]ce\s+d[eé]tach[eé]e",
        r"filtre\s+(?:de\s+)?clim",
        r"kit\s+fen[eê]tre",
        r"location\s+saisonni[eè]re",
        r"villa\s+(?:avec\s+)?clim|appartement\s+(?:avec\s+)?clim",
        r"entretien\s+clim|s[eé]ance\s+clim|"
        r"abrigo|chaqueta|veste|rossignol|lafuma\s+access|pegasus|north\s*face|"
        r"chaussure|sneaker|zapatillas?|smartwatch|reloj|montre|"
        r"clima[\s-]?fit|climawarm|climacool|dri[\s-]?fit|"
        r"tank\s+clim|detecteur\s+de\s+gaz|d[eé]tecteur\s+de\s+gaz|access\s+clim|"
        r"coche\s|jouet\b|m[eé]gane|clio|renault|peugeot",
    ],
    "console_ps4": [
        r"xbox(\s*(360|one|series\s*[xs]))?",
        r"nintendo\s*switch",
        r"thrustmaster|skylanders",
        r"volant\s*seul|manette\s*seule|dualshock\s*seul",
        r"lot\s+de\s+\d+\s+jeux",
    ],
    "phone": [
        r"coque\s*seule|housse\s*seule|chargeur\s*seul|[ée]cran\s*seul",
        r"verre\s*trempe|film\s*protection",
    ],
}

_CATEGORY_FALLBACK_UI_FILTERS: Dict[str, List[Dict[str, Any]]] = {
    "ac": [
        {
            "id": "ac_unit_only",
            "label": "Unité clim seulement",
            "type": "select",
            "field": "ac_is_unit",
            "operator": "custom",
            "segment": "market",
            "options": ["all", "unit_only"],
            "option_labels": {"all": "Tous", "unit_only": "Vraies unités"},
        },
        {
            "id": "ac_btu_min",
            "label": "BTU min",
            "type": "number",
            "field": "capacity_btu",
            "operator": "gte",
            "segment": "market",
        },
    ],
    "console_ps4": [
        {
            "id": "listing_type",
            "label": "Type annonce",
            "type": "select",
            "field": "_listing_type",
            "operator": "custom",
            "segment": "market",
            "options": ["all", "console", "pack", "games_only"],
            "option_labels": {
                "all": "Tous",
                "console": "Console",
                "pack": "Pack + jeux",
                "games_only": "Jeux seuls",
            },
        },
        {
            "id": "has_controller",
            "label": "Manette incluse",
            "type": "select",
            "field": "has_controller",
            "operator": "bool_on",
            "segment": "market",
            "options": ["all", "on", "off"],
            "option_labels": {"all": "Tous", "on": "Avec manette", "off": "Sans manette"},
        },
        {
            "id": "storage_gb",
            "label": "Stockage min (Go)",
            "type": "number",
            "field": "storage_gb",
            "operator": "gte",
            "segment": "market",
        },
    ],
}


def _apply_category_fallback_excludes(plan: Dict[str, Any], category: str) -> None:
    cat = (category or "generic").strip().lower()
    existing = {str(x).strip().lower() for x in (plan.get("early_excludes") or [])}
    for pat in _CATEGORY_FALLBACK_EXCLUDES.get(cat, []):
        if str(pat).strip().lower() not in existing:
            plan["early_excludes"].append(pat)
            existing.add(str(pat).strip().lower())


def _apply_category_fallback_ui_filters(plan: Dict[str, Any], category: str) -> None:
    cat = (category or "generic").strip().lower()
    existing_ids = {str(x.get("id") or "") for x in (plan.get("ui_filters") or []) if isinstance(x, dict)}
    for filt in _CATEGORY_FALLBACK_UI_FILTERS.get(cat, []):
        fid = str(filt.get("id") or "")
        if fid and fid not in existing_ids:
            plan["ui_filters"].append(dict(filt))
            existing_ids.add(fid)


def _sanitize_plan(obj: Dict[str, Any], *, category: str) -> Dict[str, Any]:
    """
    Keep only whitelisted keys and sane shapes.
    """
    plan: Dict[str, Any] = {
        "version": 1,
        "category": (category or "generic").strip().lower() or "generic",
        "label": _safe_text(obj.get("label") or obj.get("category") or category, limit=60),
        "agent_note": _safe_text(obj.get("agent_note") or "", limit=400),
        "spec_patterns": [],
        "early_excludes": [],
        "scoring_hints": obj.get("scoring_hints") if isinstance(obj.get("scoring_hints"), dict) else {},
        "ui_filters": [],
    }

    # --- spec_patterns ---
    sp = obj.get("spec_patterns")
    if isinstance(sp, list):
        for it in sp[:24]:
            if not isinstance(it, dict):
                continue
            field = str(it.get("field") or "").strip()
            rx = str(it.get("regex") or "").strip()
            vtype = str(it.get("value_type") or it.get("type") or "number").strip().lower()
            unit = str(it.get("unit") or "").strip()
            if not field or not rx:
                continue
            if len(rx) > 180:
                continue
            # avoid catastrophic patterns
            if ".*.*" in rx or "(?s)" in rx:
                continue
            if vtype not in ("number", "int", "float", "bool", "text"):
                vtype = "number"
            plan["spec_patterns"].append(
                {"field": field, "regex": rx, "value_type": vtype, "unit": unit[:16]}
            )

    # Always ensure these can be expressed even if LLM forgets
    must = {str(x.get("field") or "") for x in plan["spec_patterns"]}
    for f in _DEFAULT_SPEC_FIELDS:
        if f not in must:
            # Allow extraction via shared resolver even without explicit regex.
            plan["spec_patterns"].append({"field": f, "regex": "", "value_type": "number", "unit": ""})

    # --- early_excludes ---
    ex = obj.get("early_excludes")
    if isinstance(ex, list):
        for s in ex[:40]:
            if not s:
                continue
            pat = str(s).strip()
            if len(pat) > 120:
                continue
            plan["early_excludes"].append(pat)

    # --- ui_filters ---
    ui = obj.get("ui_filters")
    if isinstance(ui, list):
        ui2 = [x for x in ui if isinstance(x, dict) and x.get("id")]
        plan["ui_filters"] = sanitize_dynamic_filters(ui2, category)

    _apply_category_fallback_excludes(plan, category)
    _apply_category_fallback_ui_filters(plan, category)
    return plan


@dataclass(slots=True)
class CriteriaPlanResult:
    plan: Dict[str, Any]
    cache_hit: bool = False


def discover_criteria_plan(
    *,
    query: str,
    category: str,
    sample_rows: List[Dict[str, Any]],
    buyer_priorities: str = "",
) -> CriteriaPlanResult:
    """
    Single discovery call (cached) producing a criteria plan usable by:
    - early exclusions
    - spec extraction hints (regex)
    - UI filters additions
    - scoring hints
    """
    cat = (category or "generic").strip().lower() or "generic"
    key = _cache_key(category=cat, query=query, buyer_priorities=buyer_priorities)
    cached = cache_get(key)
    if isinstance(cached, dict) and cached.get("version") == 1:
        return CriteriaPlanResult(plan=cached, cache_hit=True)

    try:
        from .models import get_category_config

        cat_cfg = get_category_config(cat)
    except Exception:
        cat_cfg = None

    # IA hors ligne → minimal, but still writes a plan
    if not api_key():
        plan = _sanitize_plan({}, category=cat)
        if cat_cfg:
            plan["category_config"] = {
                "specs": list(cat_cfg.specs_a_extraire),
                "red_flags": list(cat_cfg.red_flags),
            }
            plan["agent_note"] = cat_cfg.prompt_block()[:240]
        cache_put(key, plan, title=f"{cat}:{_safe_text(query, limit=80)}")
        return CriteriaPlanResult(plan=plan, cache_hit=False)

    on_topic = [r for r in sample_rows if str(r.get("item_type") or "") == cat and not r.get("is_accessory")]
    if len(on_topic) < 4:
        on_topic = [r for r in sample_rows if not r.get("is_accessory")][:24]

    lines: List[str] = []
    for r in on_topic[:20]:
        blob = _row_blob(r)
        lines.append(
            json.dumps(
                {
                    "id": str(r.get("id") or ""),
                    "title": _safe_text(r.get("title") or "", limit=120),
                    "desc": _safe_text(blob, limit=260),
                    "platform": _safe_text(r.get("platform") or "", limit=16),
                },
                ensure_ascii=False,
            )
        )

    # Prompt: generic but asks for concrete regex patterns and excludes.
    hs_hint = ""
    if cat_cfg:
        hs_hint = f"\nConfig catégorie:\n{cat_cfg.prompt_block()}\n"
    if cat in ("ac", "clim", "climatisation"):
        hs_hint = (
            "\nHors-sujets typiques à exclure pour 'clim' (si présents dans l'échantillon):\n"
            "- compresseur/pompe de clim AUTO, pièces auto (Renault/Peugeot/Citroën/Ford/VW…), radiateur, pulseur\n"
            "- recharge / montage clim, prestation seule\n"
            "- annonces location (maison/villa/appartement) avec 'clim'\n"
        )
    prompt = (
        f"Tu conçois un plan de critères pour filtrer + scorer des annonces d'occasion.\n"
        f"Marché (catégorie): {cat}\n"
        f"Requête: {_safe_text(query, limit=220)!r}\n"
        f"Priorités utilisateur: {_safe_text(buyer_priorities, limit=360) or 'livraison, bon rapport qualité/prix'}\n\n"
        "Objectif: découvrir (1) des specs mesurables, (2) des filtres UI utiles, (3) des exclusions hors-sujet.\n"
        "IMPORTANT:\n"
        "- Ne pas proposer de critères d'une autre catégorie.\n"
        "- Ne pas inventer de specs non observables.\n"
        "- Les regex doivent être simples (pas de DOTALL, pas de lookbehind, pas de .*.*).\n\n"
        + hs_hint
        + "Retourne JSON strict avec les champs:\n"
        "{\n"
        '  "label": "nom français court du marché",\n'
        '  "agent_note": "1-2 phrases: ce que tu privilégies",\n'
        '  "spec_patterns": [\n'
        '    {"field":"capacity_btu|cooling_kw|noise_db|room_area_m2|room_volume_m3|power_w|type|has_hose|has_remote",\n'
        '     "regex":"(?i) ...", "value_type":"int|float|bool|text", "unit":"BTU|kW|dB|m2|m3|W"}\n'
        "  ],\n"
        '  "early_excludes": ["regex_ou_motclef ..."],\n'
        '  "ui_filters": [\n'
        '     {"id":"...","label":"...","type":"select|number|bool","field":"...","operator":"eq|gte|contains|bool_on|custom","segment":"market","options":["all","..."]}\n'
        "  ],\n"
        '  "scoring_hints": {"boost_if":["..."],"penalize_if":["..."]}\n'
        "}\n\n"
        "Échantillon d'annonces (JSONL, peut contenir du bruit hors-sujet):\n"
        + "\n".join(lines)
    )

    raw = _chat(
        prompt,
        system=(
            "Agent critères marketplace FR. JSON strict uniquement. "
            "Tu dois proposer des exclusions pour les hors-sujets typiques du marché. "
            "Tu dois proposer des regex pour BTU/kW/dB/m² quand pertinent."
        ),
        max_tokens=1400,
    )
    try:
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            obj = {}
    except Exception:
        obj = {}

    plan = _sanitize_plan(obj, category=cat)
    cache_put(key, plan, title=f"{cat}:{_safe_text(query, limit=80)}")
    logger.info("criteria_plan %s: %d filters, %d excludes, %d patterns", cat, len(plan["ui_filters"]), len(plan["early_excludes"]), len(plan["spec_patterns"]))
    return CriteriaPlanResult(plan=plan, cache_hit=False)


def plan_early_exclude_match(text: str, plan: Dict[str, Any]) -> Optional[str]:
    """Return first matching exclude pattern (for logging) or None."""
    blob = (text or "").lower()
    pats = plan.get("early_excludes") if isinstance(plan, dict) else None
    if not isinstance(pats, list) or not blob:
        return None
    for p in pats[:60]:
        try:
            if not p:
                continue
            s = str(p)
            # allow either plain keyword or regex
            if s.startswith("(?") or any(ch in s for ch in ("\\b", "|", "(", ")", "[")):
                if re.search(s, blob, flags=re.I):
                    return s[:80]
            else:
                if s.lower() in blob:
                    return s[:80]
        except re.error:
            continue
    return None


# --- filter_engine.py ---

import operator
import re
from typing import Any, Callable, Dict, List, Optional, Set

FilterSpec = Dict[str, Any]
Row = Dict[str, Any]
Evaluator = Callable[[Row, FilterSpec, str], bool]

# Filtres gérés en amont (sécurité / score / prix globaux)
_BUILTIN_SKIP_IDS: Set[str] = {
    "exclude_risky",
    "exclude_accessories",
    "main_only",
    "min_score",
    "grade_a",
    "risk_mode",
    "score_band",
    "hide_accessories",
    "price_max",
    "price_min",
    "value_score_min",
    "exclude_screen_only",
    "exclude_mini_weak",
}

# ---------------------------------------------------------------------------
# Registre catégorie → filtres marché (hors base universelle)
# ---------------------------------------------------------------------------

def _phone_filters() -> List[FilterSpec]:
    return [
        {
            "id": "phone_storage",
            "label": "Stockage",
            "type": "select",
            "field": "storage_gb",
            "operator": "eq",
            "segment": "market",
            "options": ["all", "64", "128", "256", "512", "1024"],
            "option_labels": {
                "all": "Tous",
                "64": "64 Go",
                "128": "128 Go",
                "256": "256 Go",
                "512": "512 Go",
                "1024": "1 To",
            },
        },
        {
            "id": "phone_unlocked",
            "label": "Débloqué",
            "type": "select",
            "field": "_phone_unlocked",
            "operator": "custom",
            "segment": "market",
            "options": ["all", "yes", "no"],
            "option_labels": {"all": "Tous", "yes": "Débloqué", "no": "Verrouillé / iCloud"},
        },
        {
            "id": "condition_class",
            "label": "État",
            "type": "select",
            "field": "_condition_class",
            "operator": "eq",
            "segment": "market",
            "options": ["all", "ok", "defects", "for_parts"],
            "option_labels": {
                "all": "Tous",
                "ok": "OK",
                "defects": "Défauts signalés",
                "for_parts": "Pour pièces",
            },
        },
    ]


FILTER_REGISTRY: Dict[str, List[FilterSpec]] = {
    "phone": _phone_filters(),
    # Les autres catégories restent dans engine.category_filter_templates
    # jusqu'à migration complète ; le routeur les charge via get_category_filters().
}


def get_category_filters(category: str) -> List[FilterSpec]:
    """Filtres spécifiques depuis le registre (extensible JSON / IA Tier-2)."""
    cat = (category or "generic").lower()
    return [normalize_filter_spec(dict(s)) for s in FILTER_REGISTRY.get(cat, [])]


def infer_operator(spec: FilterSpec) -> str:
    op = str(spec.get("operator") or spec.get("op") or "").strip().lower()
    if op:
        return op
    ftype = str(spec.get("type") or "select")
    field = str(spec.get("field") or "")
    if ftype == "bool":
        return "bool_on"
    if ftype == "number":
        return "gte"
    if field in ("_text_contains", "_title_keyword", "_brand_match"):
        return "contains"
    if field == "_lumens_band":
        return "custom"
    if field.startswith("_"):
        return "custom"
    return "eq"


def normalize_filter_spec(spec: FilterSpec) -> FilterSpec:
    """Options « all » en premier + operator par défaut."""
    s = dict(spec)
    ftype = str(s.get("type") or "select")
    if ftype == "bool":
        s["type"] = "select"
        s["options"] = ["all", "on"]
        labels = s.get("option_labels") if isinstance(s.get("option_labels"), dict) else {}
        s["option_labels"] = {**{"all": "Tous", "on": "Oui"}, **labels}
    elif ftype == "select":
        opts = [str(o) for o in (s.get("options") or [])]
        if "all" not in opts:
            opts = ["all", *opts]
        s["options"] = opts
        if set(opts) >= {"all", "on"} and len(opts) <= 3:
            s["operator"] = "bool_on"
    if "operator" not in s or not s.get("operator"):
        s["operator"] = infer_operator(s)
    return s


# ---------------------------------------------------------------------------
# Opérateurs standards
# ---------------------------------------------------------------------------

def _row_text(row: Row) -> str:
    return f"{row.get('title') or ''} {row.get('description') or ''} {row.get('brand') or ''}".lower()


def _row_specs(row: Row, category: str = "") -> Dict[str, Any]:
    specs = row.get("extracted_specs") or row.get("enriched_specs")
    if isinstance(specs, dict) and specs:
        return specs
    cat = (category or row.get("item_type") or "generic").strip()
    try:
        from .scoring import extract_listing_specs, normalize_category

        return extract_listing_specs(row, normalize_category(cat))
    except Exception:
        return {}


def _normalize_filter_opt(opt: str) -> str:
    """Codes UI français → codes moteur."""
    o = str(opt or "all").strip().lower()
    return {
        "tous": "all",
        "toutes": "all",
        "oui": "on",
        "non": "no",
        "yes": "on",
        "avec": "on",
        "sans": "no",
        "avec jeux": "on",
        "sans jeux": "no",
    }.get(o, o)


def _row_includes_games(row: Row) -> bool:
    specs = _row_specs(row)
    if specs.get("includes_games") is True or row.get("includes_games"):
        return True
    try:
        if int(specs.get("games_count") or row.get("games_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    blob = _row_text(row)
    if re.search(r"(?i)\b(jeu[x]?|games?|gta|fifa|cod)\b", blob):
        return True
    return False


def _row_controller_count(row: Row) -> int:
    specs = _row_specs(row)
    cnt = specs.get("controller_count")
    if cnt is None:
        cnt = specs.get("controllers_count")
    if cnt is None:
        cnt = row.get("controller_count")
    if cnt is None:
        cnt = row.get("controllers_count")
    if cnt is None:
        if specs.get("has_controller") is True or row.get("has_controller"):
            return 1
        if specs.get("has_controller") is False:
            return 0
        try:
            from .engine import row_has_controller

            return 1 if row_has_controller(row) else -1
        except Exception:
            return -1
    try:
        return int(cnt)
    except (TypeError, ValueError):
        return -1


def _op_eq(row: Row, spec: FilterSpec, opt: str) -> bool:
    field = str(spec.get("field") or "")
    if field == "product_model_key":
        rk = str(row.get("product_model_key") or row.get("ds_model_family") or "")
        return rk == opt
    if field == "ds_model_family":
        fam = str(row.get("ds_model_family") or "")
        if not fam:
            try:
                from .engine import infer_ds_model_family

                fam = infer_ds_model_family(row)
            except Exception:
                fam = ""
        return fam == opt
    if field == "storage_gb":
        try:
            return int(row.get("storage_gb") or 0) == int(opt)
        except (TypeError, ValueError):
            return False
    val = row.get(field)
    if val is None or val == "":
        return True  # champ absent → ne pas exclure
    return str(val).lower() == opt.lower()


def _op_gte(row: Row, spec: FilterSpec, opt: str) -> bool:
    field = str(spec.get("field") or "")
    try:
        need = float(opt)
        got = float(row.get(field) or 0)
    except (TypeError, ValueError):
        return True
    return got >= need


def _op_contains(row: Row, spec: FilterSpec, opt: str) -> bool:
    blob = _row_text(row)
    kw = opt.lower().replace("+", " ")
    if kw == "xl":
        return bool(re.search(r"(?i)\b(xl|xxl|2xl|3xl)\b", blob))
    if kw in ("s", "m", "l"):
        return bool(re.search(rf"(?i)\b(taille\s*{kw}|{kw}\s*[/\-]|size\s*{kw})\b", blob))
    return kw in blob


def _op_bool_on(row: Row, spec: FilterSpec, opt: str) -> bool:
    field = str(spec.get("field") or "")
    if field == "includes_games":
        return _row_includes_games(row)
    checker = _ENGINE_BOOL_FIELD_CHECK.get(field) or _BOOL_FIELD_CHECK.get(field)
    if checker is not None:
        return checker(row)
    if field == "has_remote":
        return bool(row.get("has_remote"))
    val = row.get(field)
    if val is None or val == "":
        specs = _row_specs(row)
        val = specs.get(field)
    return bool(val)


STANDARD_OPERATORS: Dict[str, Evaluator] = {
    "eq": _op_eq,
    "gte": _op_gte,
    "lte": lambda r, s, o: _op_gte(r, {**s, "field": s.get("field")}, o),  # placeholder
    "contains": _op_contains,
    "bool_on": _op_bool_on,
}


# Champs bool hérités engine
_BOOL_FIELD_CHECK: Dict[str, Callable[[Row], bool]] = {}


def register_bool_checker(field: str, fn: Callable[[Row], bool]) -> None:
    _BOOL_FIELD_CHECK[field] = fn


# ---------------------------------------------------------------------------
# Évaluateurs custom (champs virtuels _*)
# ---------------------------------------------------------------------------

def _custom_lumens_band(row: Row, _spec: FilterSpec, opt: str) -> bool:
    specs = _row_specs(row, "projector")
    try:
        lum = int(specs.get("lumens") or row.get("lumens") or 0)
    except (TypeError, ValueError):
        lum = 0
    opt = _normalize_filter_opt(opt)
    if opt in ("all", ""):
        return True
    if opt == "under1000":
        return lum > 0 and lum < 1000
    if opt == "1000-3000":
        return lum >= 1000 and lum < 3000
    thresholds = {"1000+": 1000, "2000+": 2000, "3000+": 3000, "4000+": 4000}
    if opt == "3000+":
        return lum >= 3000
    need = thresholds.get(opt, 0)
    return not need or (lum > 0 and lum >= need)


def _custom_charger_mode(row: Row, _spec: FilterSpec, opt: str) -> bool:
    from .engine import row_has_power

    st = str(row.get("charger_status") or "")
    if not st:
        if row_has_power(row):
            st = "with"
        elif row.get("missing_charger") or row.get("missing_power"):
            st = "without"
        else:
            st = "unknown"
    return st == opt


def _custom_phone_unlocked(row: Row, _spec: FilterSpec, opt: str) -> bool:
    unlocked = bool(row.get("phone_unlocked"))
    locked = bool(row.get("icloud_locked"))
    blob = _row_text(row)
    if opt == "yes":
        if locked:
            return False
        if not unlocked and not re.search(r"(?i)\b(d[ée]bloqu|unlocked|sim\s*free)\b", blob):
            return False
    if opt == "no" and not locked and unlocked:
        return False
    return True


def _custom_brand_match(row: Row, _spec: FilterSpec, opt: str) -> bool:
    blob = f"{row.get('title') or ''} {row.get('brand') or ''}".lower()
    if opt == "apple":
        return "iphone" in blob or "apple" in blob
    if opt == "other":
        return True
    return opt in blob


def _custom_wearable_brand(row: Row, _spec: FilterSpec, opt: str) -> bool:
    blob = _row_text(row)
    if opt == "garmin":
        return "garmin" in blob
    if opt == "apple":
        return "apple watch" in blob
    return opt in blob


def _custom_modded_mode(row: Row, _spec: FilterSpec, opt: str) -> bool:
    is_mod = bool(row.get("is_modded"))
    if opt == "modded":
        return is_mod
    if opt == "stock":
        return not is_mod
    return True


def _custom_pack_level(row: Row, _spec: FilterSpec, opt: str) -> bool:
    if opt == "hdmi":
        return bool(row.get("has_hdmi"))
    if opt == "complet":
        return bool(row.get("complete_hint")) or (
            bool(row.get("has_hdmi")) and bool(row.get("has_power_cable"))
        )
    if opt == "manette":
        return bool(row.get("has_controller"))
    return True


def _custom_quality_tier(row: Row, _spec: FilterSpec, opt: str) -> bool:
    tier = str(row.get("quality_tier") or "ok")
    deal_tier = str(row.get("deal_tier") or "")
    if opt == "pepites":
        return tier == "gem" or deal_tier == "pepite"
    if opt == "ok":
        return tier in ("gem", "ok")
    if opt == "sans_dropship":
        return tier != "dropship"
    if opt == "sans_dinosaure":
        return tier not in ("dropship", "dinosaur")
    return True


def _custom_catalog_price_max(row: Row, _spec: FilterSpec, opt: str) -> bool:
    try:
        cap = float(opt)
        price = float(row.get("total_price") or row.get("price") or 0)
        return price <= cap if price > 0 else True
    except (TypeError, ValueError):
        return True


def _custom_title_color(row: Row, _spec: FilterSpec, opt: str) -> bool:
    blob = f"{row.get('title') or ''} {row.get('color') or ''}".lower()
    return opt.lower() in blob


def _custom_shipping_included(row: Row, _spec: FilterSpec, opt: str) -> bool:
    inc = bool(row.get("shipping_included") or row.get("delivery_included"))
    if opt == "yes":
        return inc
    if opt == "no":
        return not inc
    return True


def _custom_livraison_uniquement(row: Row, _spec: FilterSpec, opt: str) -> bool:
    if opt in ("off", "all", "tous", ""):
        return True
    plat = str(row.get("platform") or "").lower()
    try:
        # LeBonCoin: keep existing delivery parser; other platforms: infer from text/flags.
        if plat in ("leboncoin", "lbc"):
            from .engine import enrich_row_delivery as _lbc_enrich

            _lbc_enrich(row)
        else:
            from .engine import infer_row_delivery

            infer_row_delivery(row)
        from .engine import is_hand_delivery_only_row

        return not is_hand_delivery_only_row(row)
    except Exception:
        return True


def _custom_includes_games(row: Row, _spec: FilterSpec, opt: str) -> bool:
    opt = _normalize_filter_opt(opt)
    if opt in ("all", ""):
        return True
    has = _row_includes_games(row)
    if opt in ("on", "yes", "oui"):
        return has
    if opt in ("off", "no", "non", "none"):
        return not has
    return True


def _custom_controller_count(row: Row, _spec: FilterSpec, opt: str) -> bool:
    opt = _normalize_filter_opt(opt)
    if opt in ("all", ""):
        return True
    n = _row_controller_count(row)
    if opt == "0":
        return n == 0
    if opt == "1":
        return n == 1
    if opt in ("2plus", "2+"):
        return n >= 2
    return True


def _custom_games_included(row: Row, _spec: FilterSpec, opt: str) -> bool:
    opt = _normalize_filter_opt(opt)
    if opt in ("all", ""):
        return True
    specs = _row_specs(row)
    try:
        n = int(specs.get("games_count") or row.get("games_count") or 0)
    except (TypeError, ValueError):
        n = 0
    has = _row_includes_games(row)
    if opt == "none":
        return not has
    if opt == "1_3":
        return has and (n <= 0 or 1 <= n <= 3)
    if opt == "4plus":
        return n >= 4
    return True


def _custom_listing_type(row: Row, _spec: FilterSpec, opt: str) -> bool:
    opt = _normalize_filter_opt(opt)
    if opt in ("all", ""):
        return True
    specs = _row_specs(row)
    lt = str(specs.get("listing_type") or row.get("listing_type") or "")
    is_games = bool(specs.get("is_games_only") or row.get("is_games_only"))
    if opt == "console":
        return lt != "games_only" and not is_games
    if opt == "games_only":
        return lt == "games_only" or is_games
    if opt == "pack":
        return _row_includes_games(row)
    return True


def _custom_projector_brand(row: Row, _spec: FilterSpec, opt: str) -> bool:
    pb = str(row.get("projector_brand") or "").lower()
    if opt == "other":
        return pb not in ("epson", "benq", "optoma", "viewsonic", "sony", "xiaomi")
    return not pb or pb == opt.lower()


_AC_ACCESSORY_RE = re.compile(
    r"(?i)\b("
    r"t[eé]l[eé]commande|composteur|"
    r"tuyau(?:\s+flexible)?\s+(?:de\s+)?clim|"
    r"filtre\s+(?:de\s+)?clim(?:atiseur)?|filtre\s+air\s+clim|"
    r"pi[eè]ce\s+d[eé]tach[eé]e\s+(?:de\s+)?clim|"
    r"kit\s+(?:installation|conduit)\s+clim|"
    r"recharge\s+(?:gaz\s+)?clim"
    r")\b"
)


def _custom_ac_is_unit(row: Row, _spec: FilterSpec, opt: str) -> bool:
    """True if the listing looks like an actual AC unit (not accessory/service)."""
    opt = _normalize_filter_opt(opt)
    if opt in ("all", ""):
        return True

    # Check extracted specs first
    specs = _row_specs(row, "ac")
    ac_is_unit = specs.get("ac_is_unit")
    is_acc = specs.get("is_accessory_only") or row.get("is_accessory")

    if opt == "unit_only":
        if is_acc:
            return False
        title = str(row.get("title") or "")
        if _AC_ACCESSORY_RE.search(title):
            return False
        if ac_is_unit is not None:
            return bool(ac_is_unit)
        # Ambiguous: allow through (don't over-reject)
        return True

    return True


CUSTOM_EVALUATORS: Dict[str, Evaluator] = {
    "_catalog_price_max": _custom_catalog_price_max,
    "_title_color": _custom_title_color,
    "_shipping_included": _custom_shipping_included,
    "_livraison_uniquement": _custom_livraison_uniquement,
    "_lumens_band": _custom_lumens_band,
    "_charger_mode": _custom_charger_mode,
    "_phone_unlocked": _custom_phone_unlocked,
    "_brand_match": _custom_brand_match,
    "_wearable_brand": _custom_wearable_brand,
    "_modded_mode": _custom_modded_mode,
    "_pack_level": _custom_pack_level,
    "_controller_count": _custom_controller_count,
    "_games_included": _custom_games_included,
    "_listing_type": _custom_listing_type,
    "includes_games": _custom_includes_games,
    "controller_count": _custom_controller_count,
    "games_included": _custom_games_included,
    "_condition_class": lambda r, _s, o: str(r.get("condition_class") or "ok") == _normalize_filter_opt(o),
    "_projector_usage": lambda r, _s, o: not (r.get("projector_usage")) or str(r.get("projector_usage")) == _normalize_filter_opt(o),
    "quality_tier": _custom_quality_tier,
    "projector_brand": _custom_projector_brand,
    "_ac_is_unit": _custom_ac_is_unit,
    "ac_is_unit": _custom_ac_is_unit,
    "native_aspect": lambda r, _s, o: not (r.get("native_aspect")) or str(r.get("native_aspect")) == _normalize_filter_opt(o),
    "resolution": lambda r, _s, o: not (r.get("resolution")) or str(r.get("resolution")).lower() == _normalize_filter_opt(o).lower(),
    "variant": lambda r, _s, o: not (r.get("variant")) or str(r.get("variant")).lower() == _normalize_filter_opt(o).lower(),
}


def evaluate_spec(row: Row, spec: FilterSpec, value: str) -> bool:
    """Évalue un filtre sur une ligne. True = la ligne passe."""
    opt = _normalize_filter_opt(str(value or "all"))
    if opt in ("all", "tous", "toutes", "off", ""):
        return True

    field = str(spec.get("field") or spec.get("id") or "")
    ftype = str(spec.get("type") or "select")
    if ftype == "number":
        try:
            need = float(opt)
            if need <= 0:
                return True
            got = float(row.get(field) or 0)
            return got >= need
        except (TypeError, ValueError):
            return True

    op = str(spec.get("operator") or infer_operator(spec))
    if opt == "on" and op not in ("bool_on", "custom"):
        op = "bool_on"

    if op == "custom" or field in CUSTOM_EVALUATORS:
        fn = CUSTOM_EVALUATORS.get(field)
        if fn:
            return fn(row, spec, opt)

    std = STANDARD_OPERATORS.get(op)
    if std:
        return std(row, spec, opt)

    # fallback eq sur champ brut
    rv = row.get(field)
    if rv is None or rv == "":
        return True
    return str(rv).lower() == opt.lower()


def apply_universal_filters(
    rows: List[Row],
    values: Dict[str, Any],
    specs: List[FilterSpec],
    *,
    pre_filter: Optional[Callable[[List[Row], Dict[str, Any]], List[Row]]] = None,
) -> List[Row]:
    """
    Évaluateur universel : pré-filtres globaux (engine) puis boucle operator-driven.
    """
    working = list(rows)
    if pre_filter:
        working = pre_filter(working, values)

    spec_by_id = {s["id"]: normalize_filter_spec(s) for s in specs if s.get("id")}
    out: List[Row] = []

    for r in working:
        ok = True
        for fid, spec in spec_by_id.items():
            if fid in _BUILTIN_SKIP_IDS:
                continue
            if not evaluate_spec(r, spec, str(values.get(fid) or "all")):
                ok = False
                break
        if ok:
            # min_score legacy
            try:
                min_sc = float(values.get("min_score") or 0)
            except (TypeError, ValueError):
                min_sc = 0
            if min_sc > 0:
                rank_sc = float(r.get("rank_score") or r.get("final_score") or 0)
                if rank_sc < min_sc:
                    ok = False
        if ok:
            out.append(r)
    return out


# --- filter_builder.py ---



import math

import re

from typing import Any, Dict, List, Optional, Set



from .engine import FilterSpec, merge_results_filters, normalize_filter_spec



_COLOR_RE = re.compile(

    r"(?i)\b(noir|blanc|rouge|bleu|vert|rose|violet|argent|or|gold|silver|black|white|red|blue|green|pink|purple|turquoise|corail|coral)\b"

)

_COLOR_MAP = {

    "noir": "noir", "noire": "noir", "black": "noir",

    "blanc": "blanc", "blanche": "blanc", "white": "blanc",

    "rouge": "rouge", "red": "rouge", "bleu": "bleu", "blue": "bleu",

    "vert": "vert", "green": "vert", "rose": "rose", "pink": "rose",

    "violet": "violet", "purple": "violet", "argent": "argent", "silver": "argent",

    "or": "or", "gold": "or", "turquoise": "turquoise", "corail": "corail", "coral": "corail",

}





def _price_values(rows: List[Dict[str, Any]]) -> List[float]:

    out: List[float] = []

    for r in rows:

        try:

            p = float(r.get("total_price") or r.get("price") or 0)

        except (TypeError, ValueError):

            p = 0.0

        if p > 0:

            out.append(p)

    return out





def _price_band_spec(rows: List[Dict[str, Any]]) -> Optional[FilterSpec]:

    prices = _price_values(rows)

    if len(prices) < 4:

        return None

    lo, hi = min(prices), max(prices)

    if hi - lo < 15:

        return None

    step = max(20, int((hi - lo) / 5))

    anchors = [lo]

    cur = math.floor(lo / step) * step

    while cur < hi:

        cur += step

        anchors.append(min(cur, hi))

    if anchors[-1] < hi:

        anchors.append(math.ceil(hi / 10) * 10)

    opts = ["all"] + [str(int(a)) for a in sorted(set(anchors))[1:]]

    labels: Dict[str, str] = {"all": "Tous"}

    for o in opts[1:]:

        labels[o] = f"<= {o} EUR"

    return normalize_filter_spec({

        "id": "catalog_price_band", "label": "Prix (catalogue)", "type": "select",

        "field": "_catalog_price_max", "segment": "market", "options": opts[:10], "option_labels": labels,

    })





def _platform_spec(rows: List[Dict[str, Any]]) -> Optional[FilterSpec]:

    counts: Dict[str, int] = {}

    for r in rows:

        p = str(r.get("platform") or r.get("source") or "vinted").lower()

        p = p.replace("cash-express", "cashexpress").replace("lbc", "leboncoin")

        counts[p] = counts.get(p, 0) + 1

    if len(counts) < 2:

        return None

    opts = ["all", *sorted(counts.keys(), key=lambda k: (-counts[k], k))]

    labels = {"all": "Toutes"}

    from .engine import platform_style

    for k in opts[1:]:

        labels[k] = f"{platform_style(k).label} ({counts[k]})"

    return normalize_filter_spec({

        "id": "catalog_platform", "label": "Plateforme", "type": "select",

        "field": "platform", "segment": "market", "options": opts, "option_labels": labels,

    })





def _color_spec(rows: List[Dict[str, Any]], category: str) -> Optional[FilterSpec]:

    if category not in ("console_ds", "console_switch", "apparel", "vetements", "mode", "phone"):

        return None

    found: Set[str] = set()

    for r in rows:

        blob = f"{r.get('title') or ''} {r.get('color') or ''}".lower()

        for m in _COLOR_RE.finditer(blob):

            norm = _COLOR_MAP.get(m.group(1).lower())

            if norm:

                found.add(norm)

    if len(found) < 2:

        return None

    opts = ["all", *sorted(found)]

    labels = {"all": "Toutes"} | {c: c.capitalize() for c in opts[1:]}

    return normalize_filter_spec({

        "id": "catalog_color", "label": "Couleur", "type": "select",

        "field": "_title_color", "operator": "custom", "segment": "market",

        "options": opts, "option_labels": labels,

    })





def _livraison_uniquement_spec(rows: List[Dict[str, Any]]) -> Optional[FilterSpec]:
    lbc = [r for r in rows if str(r.get("platform") or "").lower() in ("leboncoin", "lbc")]
    if not lbc:
        return None
    return normalize_filter_spec({
        "id": "livraison_uniquement",
        "label": "Livraison uniquement",
        "type": "select",
        "field": "_livraison_uniquement",
        "operator": "custom",
        "segment": "market",
        "options": ["on", "off"],
        "option_labels": {"on": "Oui (excl. main propre)", "off": "Tout afficher"},
        "default": "on",
    })


def _shipping_spec(rows: List[Dict[str, Any]]) -> Optional[FilterSpec]:

    has_ship = sum(1 for r in rows if r.get("shipping_included") or r.get("delivery_included"))

    if has_ship < 2:

        return None

    return normalize_filter_spec({

        "id": "shipping_included", "label": "Livraison incluse", "type": "select",

        "field": "_shipping_included", "operator": "custom", "segment": "market",

        "options": ["all", "yes", "no"],

        "option_labels": {"all": "Tous", "yes": "Oui", "no": "Non / a part"},

    })





def _box_complete_spec(category: str) -> Optional[FilterSpec]:

    if not category.startswith("console_") and category not in ("games", "electronics"):

        return None

    return normalize_filter_spec({

        "id": "with_box", "label": "Boite / complet", "type": "select",

        "field": "_pack_level", "segment": "market",

        "options": ["all", "complet", "boite", "console_only"],

        "option_labels": {"all": "Tous", "complet": "Pack complet", "boite": "Avec boite", "console_only": "Console seule"},

    })





def build_catalog_derived_filters(category: str, catalog_rows: Optional[List[Dict[str, Any]]] = None) -> List[FilterSpec]:

    rows = list(catalog_rows or [])

    if not rows:

        return []

    cat = (category or "generic").lower()

    specs: List[FilterSpec] = []

    for sp in (

        _platform_spec(rows),

        _price_band_spec(rows),

        _color_spec(rows, cat),

        _livraison_uniquement_spec(rows),

        _shipping_spec(rows),

        _box_complete_spec(cat),

    ):

        if sp:

            specs.append(sp)

    return specs





def build_filter_schema(

    category: str,

    *,

    catalog_rows: Optional[List[Dict[str, Any]]] = None,

    ai_filters: Optional[List[FilterSpec]] = None,

    category_schema: Optional[Dict[str, Any]] = None,

    max_market: int = 12,

) -> List[FilterSpec]:

    base = merge_results_filters(category or "generic", ai_filters, catalog_rows=None, max_market=max_market, category_schema=category_schema)

    derived = build_catalog_derived_filters(category, catalog_rows)

    seen = {str(s.get("id") or "") for s in base}

    seen_fields = {str(s.get("field") or "") for s in base}

    out = list(base)

    for sp in derived:

        sid = str(sp.get("id") or "")

        fld = str(sp.get("field") or sid)

        if sid in seen or fld in seen_fields:

            continue

        out.append(sp)

        seen.add(sid)

        seen_fields.add(fld)

    return out


# --- interactive_filters.py ---

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from .engine import FilterSpec, normalize_filter_spec
from .scoring import extract_listing_specs, normalize_category

Row = Dict[str, Any]
MatchFn = Callable[[Row, Any], bool]

_PROJECTOR_BRANDS = (
    "epson", "optoma", "benq", "viewsonic", "sony", "acer", "lg", "xiaomi", "philips", "nec", "panasonic",
)


@dataclass
class FilterControl:
    id: str
    label: str
    type: str
    options: List[Any] = field(default_factory=list)
    default: Any = "all"
    extract: Optional[MatchFn] = None
    option_labels: Dict[str, str] = field(default_factory=dict)
    field: str = ""
    segment: str = "market"


def _row_specs(row: Row, category: str = "") -> Dict[str, Any]:
    specs = row.get("extracted_specs")
    if isinstance(specs, dict) and specs:
        return specs
    return extract_listing_specs(row, category or str(row.get("market_category") or row.get("item_type") or "generic"))


def _lumens_match(row: Row, opt: str) -> bool:
    return _custom_lumens_band(row, {}, opt)


def _recognized_brand_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    if opt != "on":
        return True
    specs = _row_specs(row, "projector")
    pb = str(specs.get("projector_brand") or row.get("projector_brand") or "").lower()
    if pb in _PROJECTOR_BRANDS:
        return True
    blob = f"{row.get('title') or ''} {row.get('brand') or ''}".lower()
    return any(b in blob for b in _PROJECTOR_BRANDS)


def _exclude_projector_accessories_match(row: Row, opt: str) -> bool:
    if opt in ("all", "off", ""):
        return True
    if opt != "on":
        return True
    if row.get("is_accessory"):
        return False
    specs = _row_specs(row, "projector")
    if specs.get("is_accessory_only"):
        return False
    try:
        from .scoring import is_projector_accessory_text, has_projector_signal

        if is_projector_accessory_text(_row_text(row)):
            return False
        return has_projector_signal(row)
    except Exception:
        return True


def _resolution_match(row: Row, opt: str) -> bool:
    specs = _row_specs(row, "projector")
    res = str(specs.get("resolution") or row.get("resolution") or "").lower()
    opt_l = str(opt).lower()
    if opt_l == "all" or not res:
        return True
    if opt_l == "1080p":
        return "1080" in res or "full" in res
    if opt_l == "4k":
        return "4k" in res or "uhd" in res
    if opt_l == "720p":
        return "720" in res
    if opt_l == "xga":
        return "xga" in res or "wxga" in res or "svga" in res
    return opt_l in res


def _brand_match(row: Row, opt: str) -> bool:
    specs = _row_specs(row, "projector")
    pb = str(specs.get("projector_brand") or row.get("projector_brand") or "").lower()
    blob = f"{row.get('title') or ''} {row.get('brand') or ''}".lower()
    if opt == "other":
        return pb not in _PROJECTOR_BRANDS and not any(b in blob for b in _PROJECTOR_BRANDS[:8])
    if pb:
        return pb == opt.lower()
    return opt.lower() in blob


def _value_score_match(row: Row, opt: str) -> bool:
    try:
        need = float(opt)
    except (TypeError, ValueError):
        return True
    if need <= 0:
        return True
    try:
        vs = float(row.get("quality_value_score") or row.get("value_score") or 0)
    except (TypeError, ValueError):
        vs = 0.0
    if vs <= 0:
        from .scoring import projector_quality_value_score
        vs = projector_quality_value_score(row)
        row["quality_value_score"] = vs
    return vs >= need


def _exclude_tags(row: Row, tag: str) -> bool:
    specs = _row_specs(row, "projector")
    if tag == "screen_only":
        return not specs.get("is_screen_only") and not row.get("is_screen_only")
    if tag == "mini_weak":
        return not specs.get("is_mini_projector") and not row.get("is_mini_projector")
    return True


def _exact_model_match(row: Row, opt: str) -> bool:
    if not opt or opt in ("all", "off"):
        return True
    key = str(row.get("product_model_key") or row.get("model_key") or "")
    return key == opt or str(row.get("product_model_label") or "").lower() == opt.lower()


def _condition_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    cc = str(row.get("condition_class") or row.get("_condition_class") or "ok")
    if opt == "ok":
        return cc in ("ok", "tres bon", "neuf")
    return cc == opt


def _hdmi_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "projector")
    has = bool(specs.get("has_hdmi") or row.get("has_hdmi"))
    if opt == "yes":
        return has
    if opt == "no":
        return not has
    return True


def _usage_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    usage = str(row.get("projector_usage") or _row_specs(row, "projector").get("projector_usage") or "")
    if not usage:
        return True
    return usage == opt


def _collect_brands(listings: List[Row]) -> List[str]:
    counts: Dict[str, int] = {}
    for r in listings:
        specs = extract_listing_specs(r, "projector")
        b = str(specs.get("projector_brand") or "").lower()
        if b:
            counts[b] = counts.get(b, 0) + 1
    return sorted(counts.keys(), key=lambda k: (-counts[k], k))[:10]


def _collect_models(listings: List[Row]) -> List[tuple[str, str]]:
    seen: Set[str] = set()
    out: List[tuple[str, str]] = []
    for r in listings:
        key = str(r.get("product_model_key") or "")
        label = str(r.get("product_model_label") or r.get("ai_model_exact") or "")
        if key and key != "unknown" and key not in seen:
            seen.add(key)
            out.append((key, label or key))
    return out[:12]


def build_projector_filters(listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    brands = _collect_brands(listings)
    models = _collect_models(listings)
    brand_opts = ["all"] + brands + (["other"] if brands else [])
    brand_labels: Dict[str, str] = {"all": "Toutes", "other": "Autre"}
    for b in brands:
        brand_labels[b] = b.capitalize()

    controls: List[FilterControl] = [
        FilterControl("lumens_band", "Lumens (ANSI)", "select",
                      ["all", "under1000", "1000-3000", "3000+", "2000+", "4000+"],
                      option_labels={
                          "all": "Tous",
                          "under1000": "< 1000",
                          "1000-3000": "1000 – 3000",
                          "3000+": "3000+",
                          "2000+": "2000+ ANSI",
                          "4000+": "4000+ ANSI",
                      },
                      field="_lumens_band", extract=_lumens_match),
        FilterControl("resolution", "Résolution", "select", ["all", "1080p", "4k", "720p", "xga"],
                      option_labels={"all": "Toutes", "1080p": "Full HD 1080p", "4k": "4K / UHD", "720p": "720p HD", "xga": "XGA/WXGA"},
                      field="resolution", extract=_resolution_match),
        FilterControl("has_hdmi", "HDMI", "select", ["all", "yes", "no"],
                      option_labels={"all": "Tous", "yes": "HDMI obligatoire", "no": "Sans HDMI"}, field="has_hdmi", extract=_hdmi_match),
        FilterControl("projector_usage", "Usage", "select", ["all", "home", "cinema", "office", "portable"],
                      option_labels={"all": "Tous", "home": "Salon", "cinema": "Cinéma maison", "office": "Bureau", "portable": "Portable"},
                      field="_projector_usage", extract=_usage_match),
        FilterControl("recognized_brand", "Marque reconnue", "select", ["all", "on"],
                      option_labels={"all": "Toutes", "on": "Marques pro uniquement"},
                      field="_recognized_brand", extract=_recognized_brand_match),
        FilterControl("projector_brand", "Marque", "select", brand_opts, option_labels=brand_labels,
                      field="projector_brand", extract=_brand_match),
        FilterControl("exclude_accessories", "Exclure accessoires", "select", ["all", "on"],
                      option_labels={"all": "Non", "on": "Splitters, trépieds, écrans seuls…"},
                      field="_exclude_projector_accessories", extract=_exclude_projector_accessories_match),
        FilterControl("condition_class", "État min", "select", ["all", "ok", "defects", "for_parts"],
                      option_labels={"all": "Tous", "ok": "OK", "defects": "Défauts", "for_parts": "Pour pieces"},
                      field="_condition_class", extract=_condition_match),
        FilterControl("value_score_min", "Qualité min", "select", ["all", "50", "60", "70", "80"],
                      option_labels={"all": "Tous", "50": ">= 50", "60": ">= 60", "70": "Bon (>=70)", "80": "Excellent (>=80)"},
                      field="_value_score_min", extract=_value_score_match),
        FilterControl("exclude_screen_only", "Exclure écran seul", "select", ["all", "on"],
                      option_labels={"all": "Non", "on": "Oui"}, field="_exclude_screen_only",
                      extract=lambda r, o: o != "on" or _exclude_tags(r, "screen_only")),
        FilterControl("exclude_mini_weak", "Exclure mini faible lum.", "select", ["all", "on"],
                      option_labels={"all": "Non", "on": "Oui"}, field="_exclude_mini_weak",
                      extract=lambda r, o: o != "on" or _exclude_tags(r, "mini_weak")),
    ]
    if models:
        model_opts = ["all"] + [k for k, _ in models]
        model_labels: Dict[str, str] = {"all": "Tous modèles"}
        for k, lbl in models:
            model_labels[k] = lbl[:40]
        controls.insert(2, FilterControl("exact_model", "Modèle exact", "select", model_opts,
                                         option_labels=model_labels, field="product_model_key", extract=_exact_model_match))
    return controls


def build_phone_filters(listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    storages: Set[int] = set()
    for r in listings:
        st = extract_listing_specs(r, "phone").get("storage_gb")
        if st:
            storages.add(int(st))
    opts = ["all"] + [str(s) for s in sorted(storages)]
    return [
        FilterControl("phone_storage", "Stockage", "select", opts,
                      option_labels={"all": "Tous", **{str(s): f"{s} Go" for s in sorted(storages)}}, field="storage_gb"),
        FilterControl("phone_unlocked", "Débloqué", "select", ["all", "yes", "no"],
                      option_labels={"all": "Tous", "yes": "Débloqué", "no": "iCloud"}, field="_phone_unlocked"),
        FilterControl("condition_class", "Etat", "select", ["all", "ok", "defects", "for_parts"],
                      option_labels={"all": "Tous", "ok": "OK", "defects": "Défauts", "for_parts": "Pour pieces"},
                      field="_condition_class", extract=_condition_match),
    ]


def _ps4_listing_type_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "console_ps4")
    lt = str(specs.get("listing_type") or row.get("listing_type") or "")
    is_games = bool(specs.get("is_games_only") or row.get("is_games_only"))
    if opt == "console":
        return lt != "games_only" and not is_games
    if opt == "games_only":
        return lt == "games_only" or is_games
    if opt == "pack":
        return bool(specs.get("includes_games") or row.get("includes_games"))
    return True


def _ps4_controllers_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "console_ps4")
    cnt = specs.get("controller_count")
    if cnt is None:
        if specs.get("has_controller") is True or row.get("has_controller"):
            cnt = 1
        elif specs.get("has_controller") is False:
            cnt = 0
    try:
        n = int(cnt) if cnt is not None else -1
    except (TypeError, ValueError):
        n = -1
    if opt == "0":
        return n == 0
    if opt == "1":
        return n == 1
    if opt == "2plus":
        return n >= 2
    return True


def _ps4_games_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "console_ps4")
    try:
        n = int(specs.get("games_count") or 0)
    except (TypeError, ValueError):
        n = 0
    has = bool(specs.get("includes_games") or row.get("includes_games") or n > 0)
    if opt == "none":
        return not has
    if opt == "1_3":
        return has and (n <= 0 or 1 <= n <= 3)
    if opt == "4plus":
        return n >= 4
    return True


def _ps4_variant_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "console_ps4")
    var = str(specs.get("variant") or row.get("variant") or "").lower()
    blob = f"{row.get('title') or ''}".lower()
    if opt == "slim":
        return var == "slim" or "slim" in blob
    if opt == "pro":
        return var == "pro" or "pro" in blob
    if opt == "standard":
        return var in ("", "standard") and "slim" not in blob and "pro" not in blob
    return True


def _ps4_storage_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "console_ps4")
    try:
        gb = int(specs.get("storage_gb") or row.get("storage_gb") or 0)
    except (TypeError, ValueError):
        gb = 0
    blob = f"{row.get('title') or ''}".lower()
    if opt == "500":
        return gb == 500 or "500" in blob
    if opt == "1000":
        return gb >= 1000 or "1to" in blob or "1 to" in blob or "1tb" in blob
    return True


def _has_controller_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "console_ps4")
    has = bool(specs.get("has_controller") or row.get("has_controller"))
    if opt == "yes":
        return has
    if opt == "no":
        return not has
    return True


def build_console_ps4_filters(listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    return [
        FilterControl(
            "listing_type",
            "Type",
            "select",
            ["console", "all", "pack", "games_only"],
            default="console",
            option_labels={
                "console": "Console seule",
                "all": "Tous",
                "pack": "Pack + jeux",
                "games_only": "Jeux seuls",
            },
            field="_listing_type",
            extract=_ps4_listing_type_match,
        ),
        FilterControl(
            "controller_count",
            "Manettes",
            "select",
            ["all", "0", "1", "2plus"],
            option_labels={"all": "Toutes", "0": "Sans", "1": "1 manette", "2plus": "2+ manettes"},
            field="_controller_count",
            extract=_ps4_controllers_match,
        ),
        FilterControl(
            "games_included",
            "Jeux inclus",
            "select",
            ["all", "none", "1_3", "4plus"],
            option_labels={"all": "Tous", "none": "Aucun", "1_3": "1–3 jeux", "4plus": "4+ jeux"},
            field="_games_included",
            extract=_ps4_games_match,
        ),
        FilterControl(
            "ps4_variant",
            "Modèle",
            "select",
            ["all", "standard", "slim", "pro"],
            option_labels={"all": "Tous", "standard": "PS4", "slim": "Slim", "pro": "Pro"},
            field="_ps4_variant",
            extract=_ps4_variant_match,
        ),
        FilterControl(
            "ps4_storage",
            "Stockage",
            "select",
            ["all", "500", "1000"],
            option_labels={"all": "Tous", "500": "500 Go", "1000": "1 To"},
            field="_ps4_storage",
            extract=_ps4_storage_match,
        ),
        FilterControl(
            "condition_class",
            "État min",
            "select",
            ["all", "ok", "defects", "for_parts"],
            option_labels={"all": "Tous", "ok": "OK", "defects": "Défauts", "for_parts": "Pour pièces"},
            field="_condition_class",
            extract=_condition_match,
        ),
        FilterControl(
            "has_controller",
            "Manette incluse",
            "select",
            ["all", "yes", "no"],
            option_labels={"all": "Tous", "yes": "Avec", "no": "Sans"},
            field="has_controller",
            extract=_has_controller_match,
        ),
    ]


def build_console_ds_filters(listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    return [
        FilterControl("charger_pack", "Chargeur", "select", ["all", "with", "without", "unknown"],
                      option_labels={"all": "Tous", "with": "Avec", "without": "Sans", "unknown": "Non précisé"},
                      field="_charger_mode"),
        FilterControl("has_stylus", "Stylet", "select", ["all", "on"], option_labels={"all": "Tous", "on": "Oui"}, field="has_stylus"),
        FilterControl("includes_games", "Jeux inclus", "select", ["all", "yes", "no"],
                      option_labels={"all": "Tous", "yes": "Avec jeux", "no": "Sans jeux"}, field="includes_games"),
    ]


def _bike_wheel_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "bicycle")
    ws = str(specs.get("wheel_size") or "").replace(",", ".")
    blob = f"{row.get('title') or ''}".lower()
    if opt == "26":
        return ws == "26" or "26" in blob
    if opt == "27.5":
        return ws in ("27.5", "27,5") or "27.5" in blob or "27,5" in blob
    if opt == "29":
        return ws == "29" or "29" in blob
    return True


def _bike_usage_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "bicycle")
    usage = str(specs.get("bike_usage") or "").lower()
    if not usage:
        return True
    return usage == opt.lower()


def _bike_electric_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "bicycle")
    is_e = bool(specs.get("is_ebike") or row.get("is_ebike"))
    if opt == "yes":
        return is_e
    if opt == "no":
        return not is_e
    return True


def _bike_brand_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "bicycle")
    brand = str(specs.get("bike_brand") or row.get("brand") or "").lower()
    return opt.lower() in brand or brand in opt.lower()


def build_bicycle_filters(listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    sizes: Set[str] = set()
    brands: Set[str] = set()
    for r in listings:
        specs = extract_listing_specs(r, "bicycle")
        fs = specs.get("frame_size")
        if fs:
            sizes.add(str(fs).upper())
        b = specs.get("bike_brand")
        if b:
            brands.add(str(b).capitalize())
    frame_opts = ["all"] + sorted(sizes)
    brand_opts = ["all"] + sorted(brands)[:10]
    return [
        FilterControl(
            "wheel_size",
            "Taille roues",
            "select",
            ["all", "26", "27.5", "29"],
            option_labels={"all": "Toutes", "26": "26\"", "27.5": "27.5\"", "29": "29\""},
            field="_wheel_size",
            extract=_bike_wheel_match,
        ),
        FilterControl(
            "frame_size",
            "Taille cadre",
            "select",
            frame_opts,
            option_labels={"all": "Toutes", **{s: s for s in sorted(sizes)}},
            field="frame_size",
        ),
        FilterControl(
            "bike_brand",
            "Marque",
            "select",
            brand_opts,
            option_labels={"all": "Toutes", **{b: b for b in sorted(brands)}},
            field="bike_brand",
            extract=_bike_brand_match,
        ),
        FilterControl(
            "is_ebike",
            "Électrique",
            "select",
            ["all", "yes", "no"],
            option_labels={"all": "Tous", "yes": "VAE", "no": "Musculaire"},
            field="_is_ebike",
            extract=_bike_electric_match,
        ),
        FilterControl(
            "bike_usage",
            "Usage",
            "select",
            ["all", "route", "vtt", "ville"],
            option_labels={"all": "Tous", "route": "Route", "vtt": "VTT", "ville": "Ville"},
            field="_bike_usage",
            extract=_bike_usage_match,
        ),
        FilterControl(
            "condition_class",
            "État min",
            "select",
            ["all", "ok", "defects", "for_parts"],
            option_labels={"all": "Tous", "ok": "OK", "defects": "Défauts", "for_parts": "Pour pièces"},
            field="_condition_class",
            extract=_condition_match,
        ),
    ]


def _ac_btu_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    specs = _row_specs(row, "ac")
    btu = specs.get("capacity_btu") or row.get("capacity_btu")
    try:
        btu_i = int(btu or 0)
    except (TypeError, ValueError):
        return opt == "all"
    bands = {
        "7k": (6000, 7999),
        "9k": (8000, 9999),
        "12k": (10000, 13999),
        "18k": (14000, 19999),
    }
    lo, hi = bands.get(opt, (0, 999999))
    return lo <= btu_i <= hi


def _ac_unit_match(row: Row, opt: str) -> bool:
    if opt in ("all", ""):
        return True
    if opt != "unit_only":
        return True
    specs = _row_specs(row, "ac")
    if specs.get("is_accessory_only") or row.get("is_accessory"):
        return False
    return bool(specs.get("ac_is_unit"))


def build_ac_filters(listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    brands = sorted(
        {
            str(extract_listing_specs(r, "ac").get("brand") or "").strip().lower()
            for r in listings
            if extract_listing_specs(r, "ac").get("brand")
        }
    )
    brand_opts = ["all"] + brands[:12]
    brand_labels = {"all": "Toutes"} | {b: b.capitalize() for b in brands[:12]}
    return [
        FilterControl(
            "ac_unit_only",
            "Type",
            "select",
            ["all", "unit_only"],
            option_labels={"all": "Tous", "unit_only": "Vraies unités"},
            field="_ac_is_unit",
            extract=_ac_unit_match,
        ),
        FilterControl(
            "ac_btu_band",
            "Puissance BTU",
            "select",
            ["all", "7k", "9k", "12k", "18k"],
            option_labels={
                "all": "Toutes",
                "7k": "≈ 7000 BTU",
                "9k": "≈ 9000 BTU",
                "12k": "≈ 12000 BTU",
                "18k": "≈ 18000 BTU",
            },
            field="_ac_btu_band",
            extract=_ac_btu_match,
        ),
        FilterControl(
            "ac_brand",
            "Marque",
            "select",
            brand_opts,
            option_labels=brand_labels,
            field="brand",
            extract=_brand_match,
        ),
        FilterControl(
            "condition_class",
            "État min",
            "select",
            ["all", "ok", "defects", "for_parts"],
            option_labels={"all": "Tous", "ok": "OK", "defects": "Défauts", "for_parts": "Pour pièces"},
            field="_condition_class",
            extract=_condition_match,
        ),
        FilterControl(
            "value_score_min",
            "Score min",
            "select",
            ["all", "55", "65", "75"],
            option_labels={"all": "Tous", "55": "≥ 55", "65": "≥ 65", "75": "≥ 75"},
            field="_value_score_min",
            extract=_value_score_match,
        ),
    ]


_BUILDERS = {
    "projector": build_projector_filters,
    "phone": build_phone_filters,
    "console_ps4": build_console_ps4_filters,
    "console_ps5": build_console_ps4_filters,
    "console_ds": build_console_ds_filters,
    "bicycle": build_bicycle_filters,
    "ac": build_ac_filters,
}


def build_interactive_filters(category: str, listings: List[Row], intent: Optional[Dict[str, Any]] = None) -> List[FilterControl]:
    cat = normalize_category(category)
    builder = _BUILDERS.get(cat)
    return builder(listings, intent) if builder else []


def control_to_filter_spec(ctrl: FilterControl) -> FilterSpec:
    fld = ctrl.field or ctrl.id
    spec: FilterSpec = {
        "id": ctrl.id, "label": ctrl.label,
        "type": ctrl.type if ctrl.type in ("number", "bool") else "select",
        "field": fld, "segment": ctrl.segment, "options": list(ctrl.options),
    }
    if ctrl.option_labels:
        spec["option_labels"] = dict(ctrl.option_labels)
    if fld.startswith("_") or ctrl.extract:
        spec["operator"] = "custom"
    return normalize_filter_spec(spec)


def controls_to_filter_specs(controls: List[FilterControl]) -> List[FilterSpec]:
    return [control_to_filter_spec(c) for c in controls]


def register_custom_evaluators() -> None:
    CUSTOM_EVALUATORS["_value_score_min"] = lambda r, _s, o: _value_score_match(r, o)
    CUSTOM_EVALUATORS["_exclude_screen_only"] = lambda r, _s, o: o != "on" or _exclude_tags(r, "screen_only")
    CUSTOM_EVALUATORS["_exclude_mini_weak"] = lambda r, _s, o: o != "on" or _exclude_tags(r, "mini_weak")
    CUSTOM_EVALUATORS["_recognized_brand"] = lambda r, _s, o: _recognized_brand_match(r, o)
    CUSTOM_EVALUATORS["_exclude_projector_accessories"] = lambda r, _s, o: _exclude_projector_accessories_match(r, o)
    CUSTOM_EVALUATORS["_controller_count"] = lambda r, s, o: _custom_controller_count(r, s, o)
    CUSTOM_EVALUATORS["_games_included"] = lambda r, s, o: _custom_games_included(r, s, o)
    CUSTOM_EVALUATORS["_listing_type"] = lambda r, s, o: _custom_listing_type(r, s, o)
    CUSTOM_EVALUATORS["includes_games"] = lambda r, s, o: _custom_includes_games(r, s, o)


def enrich_listings_specs(listings: List[Row], category: str) -> None:
    cat = normalize_category(category)
    for r in listings:
        specs = extract_listing_specs(r, cat)
        r["extracted_specs"] = specs
        for k, v in specs.items():
            if v not in (None, "", False) and r.get(k) in (None, "", 0, False):
                r[k] = v


# --- taxonomy.py ---

import re
import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .engine import row_source_text

_DS_NEW3DS_XL = re.compile(r"(?i)\b(new\s*nintendo\s*3ds\s*xl|new\s*3ds\s*xl|nn3ds\s*xl)\b")
_DS_NEW3DS = re.compile(r"(?i)\b(new\s*nintendo\s*3ds|new\s*3ds|nn3ds)\b")
_DS_2DS_XL = re.compile(r"(?i)\b(2ds\s*xl)\b")
_DS_2DS = re.compile(r"(?i)\b(2ds)\b")
_DS_3DS_XL = re.compile(r"(?i)\b(3ds\s*xl|3dsxl)\b")
_DS_3DS = re.compile(r"(?i)\b(3ds)\b")
_DS_LITE = re.compile(r"(?i)\b(ds\s*lite)\b")
_DS_NDS = re.compile(r"(?i)\b(nds|nintendo\s*ds)\b")

_PROJECTOR_BRANDS = (
    "epson",
    "benq",
    "optoma",
    "viewsonic",
    "sony",
    "jvc",
    "panasonic",
    "nec",
    "acer",
    "lg",
    "philips",
    "infocus",
    "christie",
    "xiaomi",
    "wanbo",
    "xgimi",
)

_MODDED = re.compile(r"(?i)\b(cfw|luma|hshop|h-shop|homebrew|mod(d[ée]e?)?|customis[ée]e?)\b")
_FOR_PARTS = re.compile(r"(?i)\b(pour\s+pi[eè]ces|for\s+parts|hs|hors\s+service)\b")

_STORAGE_GB = re.compile(r"(?i)\b(\d{2,4})\s*(go|gb|to|tb)\b")

# iPhone — du plus spécifique au plus générique
_IPHONE_MODELS: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?i)\biphone\s*16\s*pro\s*max\b"), "iphone_16_pro_max", "iPhone 16 Pro Max"),
    (re.compile(r"(?i)\biphone\s*16\s*pro\b"), "iphone_16_pro", "iPhone 16 Pro"),
    (re.compile(r"(?i)\biphone\s*16\s*plus\b"), "iphone_16_plus", "iPhone 16 Plus"),
    (re.compile(r"(?i)\biphone\s*16\b"), "iphone_16", "iPhone 16"),
    (re.compile(r"(?i)\biphone\s*15\s*pro\s*max\b"), "iphone_15_pro_max", "iPhone 15 Pro Max"),
    (re.compile(r"(?i)\biphone\s*15\s*pro\b"), "iphone_15_pro", "iPhone 15 Pro"),
    (re.compile(r"(?i)\biphone\s*15\s*plus\b"), "iphone_15_plus", "iPhone 15 Plus"),
    (re.compile(r"(?i)\biphone\s*15\b"), "iphone_15", "iPhone 15"),
    (re.compile(r"(?i)\biphone\s*14\s*pro\s*max\b"), "iphone_14_pro_max", "iPhone 14 Pro Max"),
    (re.compile(r"(?i)\biphone\s*14\s*pro\b"), "iphone_14_pro", "iPhone 14 Pro"),
    (re.compile(r"(?i)\biphone\s*14\s*plus\b"), "iphone_14_plus", "iPhone 14 Plus"),
    (re.compile(r"(?i)\biphone\s*14\b"), "iphone_14", "iPhone 14"),
    (re.compile(r"(?i)\biphone\s*13\s*pro\s*max\b"), "iphone_13_pro_max", "iPhone 13 Pro Max"),
    (re.compile(r"(?i)\biphone\s*13\s*pro\b"), "iphone_13_pro", "iPhone 13 Pro"),
    (re.compile(r"(?i)\biphone\s*13\s*mini\b"), "iphone_13_mini", "iPhone 13 mini"),
    (re.compile(r"(?i)\biphone\s*13\b"), "iphone_13", "iPhone 13"),
    (re.compile(r"(?i)\biphone\s*12\s*pro\s*max\b"), "iphone_12_pro_max", "iPhone 12 Pro Max"),
    (re.compile(r"(?i)\biphone\s*12\s*pro\b"), "iphone_12_pro", "iPhone 12 Pro"),
    (re.compile(r"(?i)\biphone\s*12\s*mini\b"), "iphone_12_mini", "iPhone 12 mini"),
    (re.compile(r"(?i)\biphone\s*12\b"), "iphone_12", "iPhone 12"),
    (re.compile(r"(?i)\biphone\s*11\s*pro\s*max\b"), "iphone_11_pro_max", "iPhone 11 Pro Max"),
    (re.compile(r"(?i)\biphone\s*11\s*pro\b"), "iphone_11_pro", "iPhone 11 Pro"),
    (re.compile(r"(?i)\biphone\s*11\b"), "iphone_11", "iPhone 11"),
    (re.compile(r"(?i)\biphone\s*xs\s*max\b"), "iphone_xs_max", "iPhone XS Max"),
    (re.compile(r"(?i)\biphone\s*xs\b"), "iphone_xs", "iPhone XS"),
    (re.compile(r"(?i)\biphone\s*xr\b"), "iphone_xr", "iPhone XR"),
    (re.compile(r"(?i)\biphone\s*x\b"), "iphone_x", "iPhone X"),
    (re.compile(r"(?i)\biphone\s*se\s*\(?\s*2022\s*\)?\b"), "iphone_se_2022", "iPhone SE (2022)"),
    (re.compile(r"(?i)\biphone\s*se\s*\(?\s*2020\s*\)?\b"), "iphone_se_2020", "iPhone SE (2020)"),
    (re.compile(r"(?i)\biphone\s*se\b"), "iphone_se", "iPhone SE"),
    (re.compile(r"(?i)\biphone\s*8\s*plus\b"), "iphone_8_plus", "iPhone 8 Plus"),
    (re.compile(r"(?i)\biphone\s*8\b"), "iphone_8", "iPhone 8"),
]

_SAMSUNG_MODELS: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?i)\bgalaxy\s*s25\s*ultra\b"), "galaxy_s25_ultra", "Galaxy S25 Ultra"),
    (re.compile(r"(?i)\bgalaxy\s*s25\s*plus\b"), "galaxy_s25_plus", "Galaxy S25+"),
    (re.compile(r"(?i)\bgalaxy\s*s25\b"), "galaxy_s25", "Galaxy S25"),
    (re.compile(r"(?i)\bgalaxy\s*s24\s*ultra\b"), "galaxy_s24_ultra", "Galaxy S24 Ultra"),
    (re.compile(r"(?i)\bgalaxy\s*s24\s*plus\b"), "galaxy_s24_plus", "Galaxy S24+"),
    (re.compile(r"(?i)\bgalaxy\s*s24\b"), "galaxy_s24", "Galaxy S24"),
    (re.compile(r"(?i)\bgalaxy\s*s23\s*ultra\b"), "galaxy_s23_ultra", "Galaxy S23 Ultra"),
    (re.compile(r"(?i)\bgalaxy\s*s23\b"), "galaxy_s23", "Galaxy S23"),
    (re.compile(r"(?i)\bgalaxy\s*a55\b"), "galaxy_a55", "Galaxy A55"),
    (re.compile(r"(?i)\bgalaxy\s*a54\b"), "galaxy_a54", "Galaxy A54"),
    (re.compile(r"(?i)\bgalaxy\s*a35\b"), "galaxy_a35", "Galaxy A35"),
]

_PIXEL_MODELS: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?i)\bpixel\s*9\s*pro\b"), "pixel_9_pro", "Pixel 9 Pro"),
    (re.compile(r"(?i)\bpixel\s*9\b"), "pixel_9", "Pixel 9"),
    (re.compile(r"(?i)\bpixel\s*8\s*pro\b"), "pixel_8_pro", "Pixel 8 Pro"),
    (re.compile(r"(?i)\bpixel\s*8a\b"), "pixel_8a", "Pixel 8a"),
    (re.compile(r"(?i)\bpixel\s*8\b"), "pixel_8", "Pixel 8"),
    (re.compile(r"(?i)\bpixel\s*7\s*pro\b"), "pixel_7_pro", "Pixel 7 Pro"),
    (re.compile(r"(?i)\bpixel\s*7a\b"), "pixel_7a", "Pixel 7a"),
    (re.compile(r"(?i)\bpixel\s*7\b"), "pixel_7", "Pixel 7"),
]

_GARMIN_MODELS: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?i)\bfenix\s*8\b"), "garmin_fenix_8", "Garmin Fenix 8"),
    (re.compile(r"(?i)\bfenix\s*7\b"), "garmin_fenix_7", "Garmin Fenix 7"),
    (re.compile(r"(?i)\bfenix\s*6\b"), "garmin_fenix_6", "Garmin Fenix 6"),
    (re.compile(r"(?i)\bforerunner\s*965\b"), "garmin_fr_965", "Forerunner 965"),
    (re.compile(r"(?i)\bforerunner\s*955\b"), "garmin_fr_955", "Forerunner 955"),
    (re.compile(r"(?i)\bforerunner\s*265\b"), "garmin_fr_265", "Forerunner 265"),
    (re.compile(r"(?i)\bforerunner\s*255\b"), "garmin_fr_255", "Forerunner 255"),
    (re.compile(r"(?i)\bforerunner\s*245\b"), "garmin_fr_245", "Forerunner 245"),
    (re.compile(r"(?i)\bvenu\s*3\b"), "garmin_venu_3", "Garmin Venu 3"),
    (re.compile(r"(?i)\bvenu\s*2\b"), "garmin_venu_2", "Garmin Venu 2"),
    (re.compile(r"(?i)\binstinct\s*2\b"), "garmin_instinct_2", "Garmin Instinct 2"),
    (re.compile(r"(?i)\binstinct\b"), "garmin_instinct", "Garmin Instinct"),
    (re.compile(r"(?i)\bepix\s*(pro|2)?\b"), "garmin_epix", "Garmin Epix"),
    (re.compile(r"(?i)\bvivomove\b"), "garmin_vivomove", "Garmin Vivomove"),
    (re.compile(r"(?i)\bvivoactive\s*5\b"), "garmin_va_5", "Garmin Vivoactive 5"),
    (re.compile(r"(?i)\bvivoactive\s*4\b"), "garmin_va_4", "Garmin Vivoactive 4"),
]

_OTHER_WEARABLE: List[Tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(?i)\bapple\s*watch\s*ultra\s*2\b"), "apple_watch_ultra_2", "Apple Watch Ultra 2"),
    (re.compile(r"(?i)\bapple\s*watch\s*ultra\b"), "apple_watch_ultra", "Apple Watch Ultra"),
    (re.compile(r"(?i)\bapple\s*watch\s*series\s*10\b"), "apple_watch_s10", "Apple Watch Series 10"),
    (re.compile(r"(?i)\bapple\s*watch\s*series\s*9\b"), "apple_watch_s9", "Apple Watch Series 9"),
    (re.compile(r"(?i)\bapple\s*watch\s*se\b"), "apple_watch_se", "Apple Watch SE"),
    (re.compile(r"(?i)\bsuunto\s*9\b"), "suunto_9", "Suunto 9"),
    (re.compile(r"(?i)\bsuunto\s*7\b"), "suunto_7", "Suunto 7"),
    (re.compile(r"(?i)\bpolar\s*vantage\b"), "polar_vantage", "Polar Vantage"),
    (re.compile(r"(?i)\bcoros\s*pace\b"), "coros_pace", "Coros Pace"),
    (re.compile(r"(?i)\bamazfit\s*gtr\b"), "amazfit_gtr", "Amazfit GTR"),
]


def _slug_model(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:64] if s else "unknown"


def infer_storage_gb(row: Dict[str, Any]) -> int:
    if row.get("storage_gb"):
        try:
            return int(row["storage_gb"])
        except (TypeError, ValueError):
            pass
    text = row_source_text(row)
    m = _STORAGE_GB.search(text)
    if not m:
        return 0
    try:
        n = int(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "gb").lower()
    if unit == "tb" or unit == "to":
        return n * 1024
    return n


def _match_model_patterns(
    text: str,
    patterns: List[Tuple[re.Pattern[str], str, str]],
) -> Tuple[str, str]:
    for pat, key, label in patterns:
        if pat.search(text):
            return key, label
    return "", ""


def infer_phone_model(row: Dict[str, Any]) -> Tuple[str, str]:
    """Clé stable + libellé lisible pour comparer des annonces du même modèle."""
    ai = str(row.get("ai_model_exact") or "").strip()
    text = row_source_text(row)
    if ai and len(ai) >= 4:
        for pat, key, label in _IPHONE_MODELS + _SAMSUNG_MODELS + _PIXEL_MODELS:
            if pat.search(ai) or pat.search(text):
                return key, label
        return _slug_model(ai), ai[:72]

    for group in (_IPHONE_MODELS, _SAMSUNG_MODELS, _PIXEL_MODELS):
        key, label = _match_model_patterns(text, group)
        if key:
            return key, label

    if re.search(r"(?i)\biphone\b", text):
        return "iphone_other", "iPhone (autre)"
    if re.search(r"(?i)\bgalaxy\b", text):
        return "samsung_other", "Samsung Galaxy (autre)"
    if re.search(r"(?i)\bpixel\b", text):
        return "pixel_other", "Google Pixel (autre)"
    return "unknown", ""


def infer_wearable_model(row: Dict[str, Any]) -> Tuple[str, str]:
    ai = str(row.get("ai_model_exact") or "").strip()
    text = row_source_text(row)
    if ai and len(ai) >= 4:
        for pat, key, label in _GARMIN_MODELS + _OTHER_WEARABLE:
            if pat.search(ai) or pat.search(text):
                return key, label
        if re.search(r"(?i)\bgarmin\b", ai + " " + text):
            return _slug_model(ai), f"Garmin {ai[:48]}"
        return _slug_model(ai), ai[:72]

    for group in (_GARMIN_MODELS, _OTHER_WEARABLE):
        key, label = _match_model_patterns(text, group)
        if key:
            return key, label
    if re.search(r"(?i)\bgarmin\b", text):
        return "garmin_other", "Garmin (autre)"
    if re.search(r"(?i)\b(apple\s*watch|montre\s*connect[ée]e)\b", text):
        return "smartwatch_other", "Montre connectée"
    return "unknown", ""


def infer_product_model(row: Dict[str, Any]) -> Tuple[str, str]:
    cat = str(row.get("item_type") or "")
    if cat == "phone":
        return infer_phone_model(row)
    if cat == "wearable":
        return infer_wearable_model(row)
    if cat == "console_ds":
        fam = infer_ds_model_family(row)
        if fam != "unknown":
            labels = {
                "new3ds_xl": "New 3DS XL",
                "new3ds": "New 3DS",
                "3ds_xl": "3DS XL",
                "3ds": "3DS",
                "2ds_xl": "2DS XL",
                "2ds": "2DS",
                "ds_lite": "DS Lite",
                "ds": "DS / NDS",
            }
            return fam, labels.get(fam, fam)
    ai = str(row.get("ai_model_exact") or "").strip()
    if ai:
        return _slug_model(ai), ai[:72]
    return "unknown", ""


def infer_ds_model_family(row: Dict[str, Any]) -> str:
    text = row_source_text(row)
    if not text:
        return "unknown"
    if _DS_NEW3DS_XL.search(text):
        return "new3ds_xl"
    if _DS_NEW3DS.search(text):
        return "new3ds"
    if _DS_2DS_XL.search(text):
        return "2ds_xl"
    if _DS_2DS.search(text):
        return "2ds"
    if _DS_3DS_XL.search(text):
        return "3ds_xl"
    if _DS_3DS.search(text):
        return "3ds"
    if _DS_LITE.search(text):
        return "ds_lite"
    if _DS_NDS.search(text):
        return "ds"
    v = str(row.get("variant") or "").lower()
    if v in ("xl", "new"):
        if "new" in text.lower():
            return "new3ds_xl" if "xl" in text.lower() else "new3ds"
        return "3ds_xl" if v == "xl" else "3ds"
    if v == "2ds":
        return "2ds"
    return "unknown"


def infer_projector_brand(row: Dict[str, Any]) -> str:
    pb = str(row.get("projector_brand") or "").lower().strip()
    if pb in _PROJECTOR_BRANDS:
        return pb
    text = row_source_text(row).lower()
    for brand in _PROJECTOR_BRANDS:
        if re.search(rf"(?i)\b{re.escape(brand)}\b", text):
            return brand
    return "other" if re.search(r"(?i)\b(projecteur|projector)\b", text) else ""


def infer_projector_usage(row: Dict[str, Any]) -> str:
    text = row_source_text(row).lower()
    if row.get("office_projector") or re.search(r"(?i)\b(wxga|bureautique|office|présentation)\b", text):
        return "office"
    if re.search(r"(?i)\b(cin[ée]ma|home\s*theater|home\s*cinema|home\s*ciné)\b", text):
        return "cinema"
    if re.search(r"(?i)\b(portable|mini|pico|pocket)\b", text):
        return "portable"
    if re.search(r"(?i)\b(salon|home|maison|gaming|jeu)\b", text):
        return "home"
    return "home"


def enrich_listing_taxonomy(row: Dict[str, Any]) -> Dict[str, Any]:
    """Champs dérivés pour filtres et comparaison par modèle (idempotent)."""
    cat = str(row.get("item_type") or "")
    if cat == "console_ds":
        fam = infer_ds_model_family(row)
        row["ds_model_family"] = fam
        if fam != "unknown":
            row["product_model_key"] = fam
            labels = {
                "new3ds_xl": "New 3DS XL",
                "new3ds": "New 3DS",
                "3ds_xl": "3DS XL",
                "3ds": "3DS",
                "2ds_xl": "2DS XL",
                "2ds": "2DS",
                "ds_lite": "DS Lite",
                "ds": "DS / NDS",
            }
            row["product_model_label"] = labels.get(fam, fam)
        if fam != "unknown" and not row.get("variant"):
            row["variant"] = fam.replace("new3ds", "new").replace("_xl", "")
        if row.get("has_power_cable"):
            row["charger_status"] = "with"
        elif row.get("missing_charger") or row.get("missing_power"):
            row["charger_status"] = "without"
        elif re.search(
            r"(?i)\b(chargeur|alimentation|adaptateur|charger|wall\s*charger)\b",
            row_source_text(row),
        ) and not re.search(r"(?i)\b(sans\s*chargeur|no\s*charger|chargeur\s+non)\b", row_source_text(row)):
            row["charger_status"] = "with"
            row["has_power_cable"] = True
        else:
            row["charger_status"] = row.get("charger_status") or "unknown"
        row["is_modded"] = bool(row.get("is_modded")) or bool(_MODDED.search(row_source_text(row)))
    elif cat == "projector":
        brand = infer_projector_brand(row)
        if brand:
            row["projector_brand"] = brand
        row["projector_usage"] = infer_projector_usage(row)
        try:
            lum = int(row.get("lumens") or 0)
        except (TypeError, ValueError):
            lum = 0
        if lum >= 4000:
            row["lumens_band"] = "4000+"
        elif lum >= 3000:
            row["lumens_band"] = "3000+"
        elif lum >= 2000:
            row["lumens_band"] = "2000+"
        elif lum >= 1000:
            row["lumens_band"] = "1000+"
        elif lum > 0:
            row["lumens_band"] = "<1000"
    elif cat in ("phone", "wearable"):
        key, label = infer_product_model(row)
        if key != "unknown":
            row["product_model_key"] = key
            row["product_model_label"] = label
            if not row.get("ai_model_exact"):
                row["ai_model_exact"] = label
        st = infer_storage_gb(row)
        if st:
            row["storage_gb"] = st
        text = row_source_text(row).lower()
        if cat == "phone":
            if re.search(r"(?i)\b(batterie|battery)\s*(\d{2,3})\s*%", text):
                m = re.search(r"(\d{2,3})\s*%", text)
                if m:
                    row["battery_health_pct"] = int(m.group(1))
            if re.search(r"(?i)\b([ée]cran|screen|display)\s*(cass[ée]|fissur|rayure|crack)", text):
                row["screen_defect"] = True
            if re.search(r"(?i)\b(d[ée]bloqu[ée]|unlocked|sim\s*free)\b", text):
                row["phone_unlocked"] = True
            if re.search(r"(?i)\b(icloud|find\s*my|verrouill[ée])\b", text):
                row["icloud_locked"] = True
        if cat == "wearable":
            if re.search(r"(?i)\b(carte|maps|topo)\b", text):
                row["has_maps"] = True
            if re.search(r"(?i)\b(gps|gnss)\b", text):
                row["has_gps"] = True
            mm = re.search(r"(?i)\b(\d{2})\s*mm\b", text)
            if mm:
                row["watch_size_mm"] = int(mm.group(1))

    if not row.get("product_model_key") or row.get("product_model_key") == "unknown":
        key, label = infer_product_model(row)
        if key != "unknown":
            row["product_model_key"] = key
            row["product_model_label"] = label

    if _FOR_PARTS.search(row_source_text(row)):
        row["condition_class"] = "for_parts"
    elif row.get("defect_hint") or row.get("deal_blocked"):
        row["condition_class"] = "defects"
    else:
        row["condition_class"] = row.get("condition_class") or "ok"
    return row


def catalog_model_filter_spec(
    rows: List[Dict[str, Any]],
    category: str,
    *,
    max_models: int = 28,
    min_count: int = 1,
) -> Optional[Dict[str, Any]]:
    """Filtre « Modèle » alimenté par les annonces du run (comparaison directe)."""
    cat = category or "generic"
    if cat not in ("phone", "wearable", "console_ds", "projector", "console_ps4", "console_switch"):
        return None
    counts: Counter[str] = Counter()
    labels: Dict[str, str] = {}
    for r in rows:
        if r.get("is_accessory"):
            continue
        key = str(r.get("product_model_key") or r.get("ds_model_family") or "").strip()
        if not key or key == "unknown":
            continue
        counts[key] += 1
        labels[key] = str(r.get("product_model_label") or r.get("ai_model_exact") or key)[:56]
    if len(counts) < 1:
        return None
    ranked = [(k, c) for k, c in counts.most_common(max_models) if c >= min_count]
    if not ranked:
        return None
    opts = ["all"] + [k for k, _ in ranked]
    opt_labels = {"all": "Tous les modèles"}
    for k in opts:
        if k != "all":
            opt_labels[k] = labels.get(k, k)
    return {
        "id": "product_model",
        "label": "Modèle précis",
        "type": "select",
        "field": "product_model_key",
        "segment": "market",
        "options": opts,
        "option_labels": opt_labels,
    }


# --- category_brain.py ---

import os
import re
import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .scoring import effective_price_eur
from .scoring import extract_listing_specs, normalize_category, parse_intent_targets

_SAMPLE_LIMIT = 50
_SUSPICIOUS_MIN_COUNT = 8


def _listing_text(row: Dict[str, Any]) -> str:
    return f"{row.get('title') or ''} {row.get('description') or ''}".strip()


def compute_spec_confidence(row: Dict[str, Any], category: str) -> float:
    """Confiance 0-1 sur l'extraction des specs depuis titre + description."""
    cat = normalize_category(category)
    specs = row.get("extracted_specs")
    if not isinstance(specs, dict) or not specs:
        specs = extract_listing_specs(row, cat)
    text = _listing_text(row).lower()
    has_desc = len(str(row.get("description") or "")) > 40
    score = 0.35 if text else 0.15
    if has_desc:
        score += 0.18

    if cat in ("console_ps4", "console_ps5"):
        if specs.get("variant"):
            score += 0.12
        if specs.get("storage_gb"):
            score += 0.12
        if specs.get("has_controller") is not None:
            score += 0.10
        if specs.get("listing_type") == "console":
            score += 0.08
    elif cat == "projector":
        lum = int(specs.get("lumens") or 0)
        if lum >= 500 and not specs.get("marketing_lumens"):
            score += 0.22
        elif lum > 0:
            score += 0.08
        if specs.get("resolution"):
            score += 0.10
        if specs.get("projector_brand"):
            score += 0.12
    elif cat == "bicycle":
        for k in ("wheel_size", "frame_size", "bike_brand", "groupset"):
            if specs.get(k):
                score += 0.10
    elif cat == "phone":
        if specs.get("product_model_key") and specs.get("product_model_key") != "unknown":
            score += 0.20
        if specs.get("storage_gb"):
            score += 0.12
        if specs.get("battery_health_pct"):
            score += 0.10

    if row.get("ai_model_exact") or row.get("product_model_label"):
        score += 0.08
    return round(max(0.0, min(1.0, score)), 3)


def detect_suspicious_price_clusters(
    rows: List[Dict[str, Any]],
    *,
    min_count: int = _SUSPICIOUS_MIN_COUNT,
) -> Dict[str, Dict[str, Any]]:
    """Repère les prix répétés (cluster dropship / faux marché)."""
    buckets: Dict[float, List[str]] = {}
    for r in rows:
        p = effective_price_eur(r)
        if p <= 0:
            continue
        key = round(p * 2) / 2.0
        buckets.setdefault(key, []).append(str(r.get("id") or ""))

    suspicious: Dict[str, Dict[str, Any]] = {}
    for price, ids in buckets.items():
        if len(ids) < min_count:
            continue
        share = len(ids) / max(1, len(rows))
        if share >= 0.04 or len(ids) >= min_count + 4:
            for lid in ids:
                if lid:
                    suspicious[lid] = {
                        "cluster_price_eur": price,
                        "cluster_count": len(ids),
                        "cluster_share_pct": round(share * 100, 1),
                    }
    return suspicious


def _discover_brands(rows: List[Dict[str, Any]], category: str) -> List[str]:
    cat = normalize_category(category)
    counts: Counter[str] = Counter()
    for r in rows:
        specs = extract_listing_specs(r, cat)
        for key in ("projector_brand", "bike_brand", "brand"):
            b = str(specs.get(key) or r.get(key) or "").strip().lower()
            if b and len(b) > 2:
                counts[b] += 1
    return [b for b, _ in counts.most_common(12)]


def _price_stats(prices: List[float]) -> Dict[str, float]:
    if not prices:
        return {"p25": 0.0, "p50": 0.0, "p75": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    sp = sorted(prices)
    n = len(sp)
    p25_i = max(0, n // 4)
    p75_i = min(n - 1, (3 * n) // 4)
    return {
        "p25": float(sp[p25_i]),
        "p50": float(statistics.median(sp)),
        "p75": float(sp[p75_i]),
        "min": float(sp[0]),
        "max": float(sp[-1]),
        "n": float(n),
    }


def analyze_catalog_sample(
    listings: List[Dict[str, Any]],
    category: str,
    query: str = "",
    limit: int = _SAMPLE_LIMIT,
) -> Dict[str, Any]:
    """Marques, prix, specs fréquentes sur un échantillon catalogue."""
    cat = normalize_category(category)
    sample = list(listings[:limit]) if listings else []
    prices = [effective_price_eur(r) for r in sample if effective_price_eur(r) > 0]
    stats = _price_stats(prices)

    spec_counts: Dict[str, Counter] = {}
    for r in sample:
        specs = extract_listing_specs(r, cat)
        for k, v in specs.items():
            if v in (None, "", False, 0):
                continue
            spec_counts.setdefault(k, Counter())[str(v)] += 1

    common_specs: Dict[str, List[str]] = {}
    for k, ctr in spec_counts.items():
        common_specs[k] = [val for val, _ in ctr.most_common(5)]

    suspicious = detect_suspicious_price_clusters(sample)
    brands = _discover_brands(sample, cat)

    return {
        "category": cat,
        "query": query,
        "sample_size": len(sample),
        "brands": brands,
        "price_stats": stats,
        "common_specs": common_specs,
        "suspicious_price_ids": suspicious,
        "market_reference": {
            "median": stats["p50"],
            "p25": stats["p25"],
            "p75": stats["p75"],
            "fair_low": stats["p25"],
            "fair_high": stats["p75"],
        },
    }


def enrich_intent_from_query(query: str, category: str) -> Dict[str, Any]:
    """Préfiltres depuis la requête (PS4 slim 1to, vélo adulte, lumens…)."""
    cat = normalize_category(category)
    blob = (query or "").lower()
    out: Dict[str, Any] = {"category": cat, "query": query}

    if cat in ("console_ps4", "console_ps5") or re.search(r"(?i)\bps\s*4|playstation\s*4\b", blob):
        if re.search(r"(?i)\bslim\b", blob):
            out["ps4_variant"] = "slim"
        elif re.search(r"(?i)\bpro\b", blob):
            out["ps4_variant"] = "pro"
        if re.search(r"(?i)\b(1\s*to|1\s*tb|1000\s*go)\b", blob):
            out["ps4_storage"] = "1000"
        elif re.search(r"(?i)\b500\s*(go|gb)\b", blob):
            out["ps4_storage"] = "500"
        out.setdefault("listing_type", "console")
        out.setdefault("livraison_uniquement", "on")

    if cat == "bicycle" or re.search(r"(?i)\b(vélo|velo|vtt|vtc|gravel)\b", blob):
        if re.search(r"(?i)\b(enfant|enfants|junior|kid)\b", blob):
            out["exclude_kids"] = False
        elif re.search(r"(?i)\b(adulte|homme|femme|cadre\s*\d{2})\b", blob) or not re.search(
            r"(?i)\benfant\b", blob
        ):
            out["exclude_kids"] = True
        fm = re.search(r"(?i)\b(?:taille\s*)?(\d{2})\s*cm\b", blob)
        if fm:
            out["target_frame_size"] = fm.group(1)

    if cat == "projector" or re.search(r"(?i)projecteur", blob):
        out.setdefault("exclude_mini_weak", "on")
        out.setdefault("exclude_screen_only", "on")
        lm = re.search(r"(?i)\b(\d{3,5})\s*(?:ansi\s*)?lum", blob)
        if lm:
            out["lumens_min"] = int(lm.group(1))

    targets = parse_intent_targets(category=cat, query=query, intent=out)
    out.update({k: v for k, v in targets.items() if v not in (None, "", 0, [])})
    return out


def smart_filter_defaults(
    category: str,
    query: str = "",
    brain: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Valeurs par défaut des filtres UI selon catégorie et requête."""
    cat = normalize_category(category)
    intent = enrich_intent_from_query(query, cat)
    defaults: Dict[str, str] = {
        "quality_tier": "all",
        "risk_mode": "all",
        "hide_accessories": "tout",
        "score_band": "all",
    }

    if cat in ("console_ps4", "console_ps5"):
        defaults["listing_type"] = str(intent.get("listing_type") or "console")
        defaults["livraison_uniquement"] = str(intent.get("livraison_uniquement") or "on")
        if intent.get("ps4_variant"):
            defaults["ps4_variant"] = str(intent["ps4_variant"])
        if intent.get("ps4_storage"):
            defaults["ps4_storage"] = str(intent["ps4_storage"])
    elif cat == "bicycle":
        if intent.get("exclude_kids"):
            defaults["exclude_kids"] = "on"
    elif cat == "projector":
        defaults["exclude_screen_only"] = "on"
        defaults["exclude_mini_weak"] = "on"
        if intent.get("lumens_min"):
            lum = int(intent["lumens_min"])
            if lum >= 3000:
                defaults["lumens_band"] = "3000+"
            elif lum >= 2000:
                defaults["lumens_band"] = "2000+"
            else:
                defaults["lumens_band"] = "1000+"

    brain = brain or {}
    stats = brain.get("price_stats") or {}
    p75 = float(stats.get("p75") or 0)
    if p75 > 80:
        defaults["price_max"] = str(int(round(p75)))

    if cat == "console_ds":
        defaults["exclude_screen_only"] = "on"
        blob = (query or "").lower()
        if re.search(r"(?i)new\s*nintendo\s*3ds|new\s*3ds", blob):
            defaults["ds_model_family"] = "new3ds_xl" if re.search(r"(?i)\bxl\b", blob) else "new3ds"
            defaults["intent_exact_only"] = "on"
    elif cat in ("console_ps4", "console_ps5") and intent.get("ps4_variant"):
        defaults["intent_exact_only"] = "on"
        defaults["ps4_variant"] = str(intent["ps4_variant"])
    elif cat == "phone" and re.search(r"(?i)iphone\s*\d{2}|galaxy\s*s\d+", (query or "").lower()):
        defaults["intent_exact_only"] = "on"

    return defaults


def discover_filter_options(
    listings: List[Dict[str, Any]],
    category: str,
    brain: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Options de filtres découvertes dynamiquement sur le catalogue."""
    cat = normalize_category(category)
    brain = brain or analyze_catalog_sample(listings, cat, limit=_SAMPLE_LIMIT)
    opts: Dict[str, List[str]] = {}

    if cat == "projector":
        lumens_vals: Set[int] = set()
        res_vals: Set[str] = set()
        for r in listings[:200]:
            specs = extract_listing_specs(r, cat)
            try:
                lum = int(specs.get("lumens") or 0)
            except (TypeError, ValueError):
                lum = 0
            if lum >= 800:
                lumens_vals.add(lum)
            res = str(specs.get("resolution") or "")
            if res:
                res_vals.add(res)
        lum_opts = ["all"]
        for thr in (1000, 2000, 3000, 4000):
            if any(l >= thr for l in lumens_vals):
                lum_opts.append(f"{thr}+")
        opts["lumens_band"] = lum_opts
        opts["projector_brand"] = ["all"] + list(brain.get("brands") or [])[:10]
        if res_vals:
            opts["resolution"] = ["all"] + sorted(res_vals)[:6]

    elif cat in ("console_ps4", "console_ps5"):
        variants: Set[str] = set()
        storages: Set[str] = set()
        for r in listings[:200]:
            specs = extract_listing_specs(r, cat)
            v = str(specs.get("variant") or "")
            if v:
                variants.add(v)
            try:
                gb = int(specs.get("storage_gb") or 0)
            except (TypeError, ValueError):
                gb = 0
            if gb == 500:
                storages.add("500")
            elif gb >= 1000:
                storages.add("1000")
        if variants:
            opts["ps4_variant"] = ["all"] + sorted(variants)
        if storages:
            opts["ps4_storage"] = ["all"] + sorted(storages)

    elif cat == "bicycle":
        wheels: Set[str] = set()
        brands: Set[str] = set()
        for r in listings[:200]:
            specs = extract_listing_specs(r, cat)
            if specs.get("wheel_size"):
                wheels.add(str(specs["wheel_size"]))
            if specs.get("bike_brand"):
                brands.add(str(specs["bike_brand"]).lower())
        if wheels:
            opts["wheel_size"] = ["all"] + sorted(wheels)
        if brands:
            opts["bike_brand"] = ["all"] + sorted(brands)[:10]

    return opts


def merge_brain_into_eval_context(
    eval_ctx: Dict[str, Any],
    brain: Dict[str, Any],
) -> Dict[str, Any]:
    """Enrichit le contexte Top 5 avec le marché découvert."""
    ref = dict(eval_ctx.get("market_reference") or {})
    bref = brain.get("market_reference") or {}
    for k in ("p25", "p75", "median", "fair_low", "fair_high"):
        if bref.get(k):
            ref[k] = bref[k]
    eval_ctx["market_reference"] = ref
    eval_ctx["brain"] = {
        "brands": brain.get("brands") or [],
        "common_specs": brain.get("common_specs") or {},
        "price_stats": brain.get("price_stats") or {},
    }
    return eval_ctx


def cross_platform_arbitrage_hint(
    row: Dict[str, Any],
    peers: List[Dict[str, Any]],
    *,
    model_key: str = "",
) -> str:
    """Indice si le même modèle est moins cher sur une autre plateforme."""
    if not peers:
        return ""
    plat = str(row.get("platform") or "vinted").lower().replace("lbc", "leboncoin")
    price = effective_price_eur(row)
    if price <= 0:
        return ""

    mk = model_key or str(row.get("product_model_key") or row.get("ds_model_family") or "")
    title_key = re.sub(r"\s+", " ", str(row.get("title") or "").lower())[:60]

    best_other: Optional[Tuple[str, float]] = None
    for p in peers:
        if str(p.get("id") or "") == str(row.get("id") or ""):
            continue
        p_plat = str(p.get("platform") or "").lower().replace("lbc", "leboncoin")
        if p_plat == plat:
            continue
        p_price = effective_price_eur(p)
        if p_price <= 0:
            continue
        if mk:
            if str(p.get("product_model_key") or "") != mk:
                continue
        else:
            pt = re.sub(r"\s+", " ", str(p.get("title") or "").lower())[:60]
            if title_key[:30] not in pt and pt[:30] not in title_key:
                continue
        if p_price < price - 5:
            if best_other is None or p_price < best_other[1]:
                best_other = (p_plat, p_price)

    if not best_other:
        return ""
    other_plat, other_price = best_other
    delta = round(price - other_price, 1)
    labels = {"vinted": "Vinted", "leboncoin": "LeBonCoin", "ebay": "eBay"}
    return f"{delta:.0f}€ moins cher sur {labels.get(other_plat, other_plat)}"


def find_near_misses(
    candidates: List[Dict[str, Any]],
    selected_ids: Union[Set[str], List[str]],
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Annonces proches du Top 5 exclues, avec raisons."""
    selected = {str(x) for x in selected_ids}
    misses: List[Dict[str, Any]] = []
    for r in candidates:
        rid = str(r.get("id") or "")
        if not rid or rid in selected:
            continue
        bds = float(r.get("best_deal_score") or r.get("value_score_v2") or 0)
        if bds < 62:
            continue
        reasons: List[str] = []
        meta = r.get("_top5_meta") or {}
        flags = list(meta.get("flags") or [])
        conf = float(r.get("spec_confidence") or 0)
        if conf < 0.55:
            reasons.append(f"specs peu fiables ({conf:.0%})")
        if r.get("suspicious_price_cluster"):
            reasons.append("prix suspect (cluster)")
        if flags:
            reasons.extend(flags[:2])
        dropship = float(r.get("dropship_risk_score") or 0)
        if dropship >= 35:
            reasons.append(f"risque dropship {dropship:.0f}%")
        qvs = float(r.get("quality_value_score") or 0)
        if qvs and qvs < 50:
            reasons.append(f"qualité faible ({qvs:.0f})")
        if not reasons:
            reasons.append("score inférieur au top 5")
        misses.append(
            {
                "listing_id": rid,
                "title": str(r.get("title") or "")[:100],
                "price": effective_price_eur(r),
                "score": int(bds),
                "reason": " — ".join(reasons[:3]),
                "platform": r.get("platform"),
                "url": r.get("url"),
            }
        )
    misses.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return misses[:limit]


def early_spec_filter_enabled() -> bool:
    raw = os.environ.get("VF_EARLY_SPEC_FILTER", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def enrich_rows_with_brain(
    rows: List[Dict[str, Any]],
    category: str,
    brain: Dict[str, Any],
    spec_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Ajoute confiance specs, clusters suspects et indices arbitrage."""
    cat = normalize_category(category)
    suspicious = brain.get("suspicious_price_ids") or {}
    cache = spec_cache if spec_cache is not None else {}

    for r in rows:
        lid = str(r.get("id") or "")
        if lid and lid not in cache:
            cache[lid] = extract_listing_specs(r, cat)
        if lid and lid in cache:
            r.setdefault("extracted_specs", cache[lid])
        conf = compute_spec_confidence(r, cat)
        r["spec_confidence"] = conf
        r["spec_confidence_label"] = (
            "élevée" if conf >= 0.75 else ("moyenne" if conf >= 0.5 else "faible")
        )
        if lid in suspicious:
            r["suspicious_price_cluster"] = True
            r["suspicious_price_detail"] = suspicious[lid]
        hint = cross_platform_arbitrage_hint(r, rows)
        if hint:
            r["arbitrage_hint"] = hint


# --- learnings.py ---

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LEARN_PATH = Path("brouillon") / "data" / "user_learnings.json"
_OVERRIDES_PATH = Path("brouillon") / "data" / "system_prompt_overrides.txt"
_MAX_RULES = 120
_MAX_OVERRIDE_LINES = 80
_TOKEN_RE = re.compile(r"[\w]{3,}", re.UNICODE)


def _load() -> Dict[str, Any]:
    try:
        if _LEARN_PATH.is_file():
            data = json.loads(_LEARN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"rules": [], "updated": 0}


def _save(data: Dict[str, Any]) -> None:
    _LEARN_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = int(time.time())
    _LEARN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _tokens(text: str, *, limit: int = 8) -> List[str]:
    raw = (text or "").lower()
    stop = {
        "avec",
        "pour",
        "the",
        "and",
        "les",
        "des",
        "une",
        "nintendo",
        "console",
        "vendu",
        "bon",
        "etat",
        "état",
    }
    out: List[str] = []
    for m in _TOKEN_RE.finditer(raw):
        t = m.group(0)
        if t in stop or len(t) < 3:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def load_prompt_overrides() -> str:
    try:
        if _OVERRIDES_PATH.is_file():
            return _OVERRIDES_PATH.read_text(encoding="utf-8").strip()[:4000]
    except OSError:
        pass
    return ""


def record_doubt_training(
    title: str,
    *,
    action: str,
    category: str = "",
    reason: str = "",
) -> None:
    """Feedback mode entraînement — alimente system_prompt_overrides.txt pour le LLM."""
    title = (title or "").strip()
    if not title or action not in ("validate", "reject"):
        return
    note = (reason or "").strip()[:200]
    cat = (category or "generic").strip()
    line = (
        f"- [{action}] ({cat}) «{title[:90]}»"
        + (f" — {note}" if note else "")
    )
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    if _OVERRIDES_PATH.is_file():
        try:
            lines = [
                ln for ln in _OVERRIDES_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        except OSError:
            lines = []
    if line not in lines:
        lines.append(line)
    _OVERRIDES_PATH.write_text(
        "\n".join(lines[-_MAX_OVERRIDE_LINES:]) + "\n",
        encoding="utf-8",
    )
    if action == "reject":
        record_feedback(title, action="dismiss", category=cat, reason=note or "reject")


def record_feedback(
    title: str,
    *,
    action: str = "dismiss",
    category: str = "",
    reason: str = "",
) -> None:
    """Enregistre un motif rejeté par l'utilisateur (écarter une annonce)."""
    title = (title or "").strip()
    if not title or action not in ("dismiss", "reject"):
        return
    data = _load()
    rules: List[Dict[str, Any]] = list(data.get("rules") or [])
    toks = _tokens(title)
    if not toks:
        return
    sig = " ".join(sorted(toks[:5]))
    for r in rules:
        if r.get("sig") == sig and r.get("category") == category:
            r["hits"] = int(r.get("hits") or 0) + 1
            r["last"] = int(time.time())
            _save({**data, "rules": rules})
            return
    rules.insert(
        0,
        {
            "sig": sig,
            "tokens": toks,
            "category": category or "",
            "reason": reason or action,
            "hits": 1,
            "last": int(time.time()),
        },
    )
    _save({**data, "rules": rules[:_MAX_RULES]})


def triage_learning_hit(title: str, category: str = "") -> Optional[Tuple[str, float]]:
    """
    Retourne (reason, confidence) si le titre matche une règle apprise.
    Les règles « faux_doute » et tokens trop génériques ne s'appliquent pas aux titres PS4 explicites.
    """
    title_l = (title or "").strip().lower()
    toks = set(_tokens(title, limit=12))
    if not toks:
        return None
    cat = (category or "").lower()
    ps4_intent = bool(
        cat in ("console_ps4", "console_ps5")
        or re.search(r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4)\b", title_l)
    )
    best: Optional[Tuple[str, float]] = None
    for r in _load().get("rules") or []:
        if cat and r.get("category") and r.get("category") != cat:
            continue
        rt = set(r.get("tokens") or [])
        if not rt:
            continue
        reason = str(r.get("reason") or "user_dismiss")
        if ps4_intent and reason in ("faux_doute", "unknown_category"):
            continue
        if len(rt) < 2 and not reason.startswith("accessory"):
            continue
        overlap = len(rt & toks) / max(1, len(rt))
        if overlap >= 0.72:
            hits = int(r.get("hits") or 1)
            conf = min(0.94, 0.78 + 0.04 * min(hits, 4))
            learned_reason = f"learned:{reason}"
            if best is None or conf > best[1]:
                best = (learned_reason, conf)
    return best


# --- delivery_rules.py ---

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


_PICKUP_ONLY_RE = re.compile(
    r"(?i)\b("
    r"(?:a|à)\s*prendre\s*sur\s*place|"
    r"(?:a|à)\s*r[eé]cup[eé]rer\s*sur\s*place|"
    r"(?:a|à)\s*venir\s*chercher|"
    r"remise\s+en\s+main\s+propre\s+(?:uniquement|seulement)|"
    r"main\s+propre\s+(?:uniquement|seulement)|"
    r"pas\s+d['’]?(?:envoi|envoi?s?)|"
    r"pas\s+d['’]?(?:exp[eé]dition|expedition)|"
    r"envoi\s+impossible|"
    r"uniquement\s+sur\s*place|"
    r"retrait\s+sur\s*place|"
    r"pickup\s+only|"
    r"collection\s+only|"
    r"no\s+shipping"
    r")\b"
)

_SHIPPING_POSITIVE_RE = re.compile(
    r"(?i)\b("
    r"livraison\s+possible|"
    r"envoi\s+possible|"
    r"envoi\s+ok|"
    r"je\s+peux\s+envoyer|"
    r"exp[eé]dition\s+possible|"
    r"shipping\s+available"
    r")\b"
)


@dataclass(slots=True)
class DeliveryFlags:
    shipping_available: Optional[bool]
    hand_delivery_only: bool
    delivery_mode: str  # "shipping" | "hand_only" | "both" | "unknown"


def infer_delivery_flags_from_text(title: str = "", description: str = "") -> DeliveryFlags:
    blob = f"{title or ''}\n{description or ''}".strip()
    if not blob:
        return DeliveryFlags(shipping_available=None, hand_delivery_only=False, delivery_mode="unknown")
    pickup = bool(_PICKUP_ONLY_RE.search(blob))
    ship = bool(_SHIPPING_POSITIVE_RE.search(blob))
    if pickup and ship:
        return DeliveryFlags(shipping_available=True, hand_delivery_only=False, delivery_mode="both")
    if pickup:
        return DeliveryFlags(shipping_available=False, hand_delivery_only=True, delivery_mode="hand_only")
    if ship:
        return DeliveryFlags(shipping_available=True, hand_delivery_only=False, delivery_mode="shipping")
    return DeliveryFlags(shipping_available=None, hand_delivery_only=False, delivery_mode="unknown")


def infer_row_delivery(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mutates row with:
    - shipping_available
    - hand_delivery_only
    - delivery_mode
    """
    plat = str(row.get("platform") or "").lower()
    title = str(row.get("title") or "")
    desc = str(row.get("description") or row.get("description_excerpt") or "")

    # If platform already provided strong signals, keep them but still allow
    # text override to detect explicit pickup-only clauses.
    shipping_available = row.get("shipping_available")
    if shipping_available is not None:
        shipping_available = bool(shipping_available)

    flags_txt = infer_delivery_flags_from_text(title, desc)
    if flags_txt.hand_delivery_only:
        row["hand_delivery_only"] = True
        row["delivery_mode"] = "hand_only"
        if shipping_available is None:
            row["shipping_available"] = False
        return row

    # For Vinted, if shipping flag is explicitly False, treat as not shippable.
    if plat == "vinted" and shipping_available is False:
        row["hand_delivery_only"] = True
        row["delivery_mode"] = "hand_only"
        return row

    # If we have explicit shipping, set mode
    if shipping_available is True:
        row.setdefault("delivery_mode", "shipping")
        row.setdefault("hand_delivery_only", False)
        return row

    # No strong platform signal: use text-inferred positives if any
    if flags_txt.shipping_available is True:
        row["shipping_available"] = True
        row["hand_delivery_only"] = False
        row["delivery_mode"] = flags_txt.delivery_mode or "shipping"
        return row

    row.setdefault("hand_delivery_only", False)
    row.setdefault("delivery_mode", row.get("delivery_mode") or "unknown")
    if row.get("shipping_available") is None:
        row["shipping_available"] = None
    return row


def is_hand_delivery_only_row(row: Dict[str, Any]) -> bool:
    if row.get("hand_delivery_only"):
        return True
    if str(row.get("delivery_mode") or "") == "hand_only":
        return True
    if row.get("shipping_available") is False and str(row.get("delivery_mode") or "") not in ("both", "shipping"):
        return True
    # fallback: text patterns
    flags_txt = infer_delivery_flags_from_text(str(row.get("title") or ""), str(row.get("description") or row.get("description_excerpt") or ""))
    return bool(flags_txt.hand_delivery_only)


# --- lbc_delivery.py ---
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

_HAND_ONLY_RE = re.compile(r"(?i)\b(remise\s+en\s+main\s+propre\s+uniquement|main\s+propre\s+uniquement|uniquement\s+en\s+main\s+propre|pas\s+de\s+livraison|pas\s+d['']?\s*envoi|sans\s+livraison|retrait\s+uniquement)\b")
_SHIPPING_RE = re.compile(r"(?i)\b(envoi\s+possible|livraison\s+possible|en\s+livraison|mondial\s*relay|colissimo|frais\s+de\s+port|envoi\s+suivi)\b")
_BOTH_RE = re.compile(r"(?i)(ou\s+remise\s+en\s+main\s+propre|egalement\s+disponible\s+en\s+main\s+propre|en\s+livraison.{0,60}main\s+propre)")

@dataclass(frozen=True)
class LbcDeliveryInfo:
    shipping_available: Optional[bool]
    hand_delivery_only: bool
    delivery_mode: str
    def as_dict(self):
        return {"shipping_available": self.shipping_available, "hand_delivery_only": self.hand_delivery_only, "delivery_mode": self.delivery_mode}

def _truthy(v):
    if v is None: return None
    if isinstance(v, bool): return v
    s = str(v).strip().lower()
    if s in ("true","1","yes","oui","on"): return True
    if s in ("false","0","no","non","off"): return False
    return None

def _api_flags(ad):
    ship = ad.get("shipping") if isinstance(ad.get("shipping"), dict) else {}
    delivery = ad.get("delivery") if isinstance(ad.get("delivery"), dict) else {}
    shippable = None
    for key in ("shippable","is_shippable","shipping_enabled"):
        if key in ship: shippable = _truthy(ship.get(key)); break
        if key in ad: shippable = _truthy(ad.get(key)); break
    hand = None
    for key in ("hand_delivery","hand_deliverable","face_to_face","pickup"):
        if key in ship: hand = _truthy(ship.get(key)); break
        if key in delivery: hand = _truthy(delivery.get(key)); break
    mode = str(delivery.get("mode") or ship.get("mode") or "").lower()
    if mode in ("face_to_face","pickup","hand","hand_delivery","remise"):
        hand = True if hand is None else hand
        shippable = False if shippable is None else shippable
    elif mode in ("shipping","delivery","colissimo","mondial_relay","mandat"):
        shippable = True if shippable is None else shippable
    return shippable, hand

def _text_flags(title, description):
    blob = f"{title or ''}\n{description or ''}".strip()
    if not blob: return False, False, False
    hand_only = bool(_HAND_ONLY_RE.search(blob))
    has_ship = bool(_SHIPPING_RE.search(blob))
    both = bool(_BOTH_RE.search(blob))
    if hand_only and has_ship:
        both = True; hand_only = False
    return hand_only, has_ship, both

def parse_lbc_delivery(ad, *, title="", description=""):
    t = title or str(ad.get("subject") or ad.get("title") or "")
    d = description or str(ad.get("body") or ad.get("description") or "")
    api_ship, api_hand = _api_flags(ad)
    txt_hand_only, txt_ship, txt_both = _text_flags(t, d)
    if txt_both or (api_ship is True and api_hand is True):
        return LbcDeliveryInfo(True, False, "both")
    if txt_hand_only or (api_ship is False and api_hand is not False):
        return LbcDeliveryInfo(False, True, "hand_only")
    if api_ship is True or txt_ship:
        return LbcDeliveryInfo(True, False, "shipping")
    if api_hand is True and api_ship is not True:
        return LbcDeliveryInfo(False, True, "hand_only")
    return LbcDeliveryInfo(None, False, "unknown")

def enrich_row_delivery(row):
    plat = str(row.get("platform") or "").lower()
    if plat not in ("leboncoin","lbc"): return row
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    row.update(parse_lbc_delivery(raw, title=str(row.get("title") or ""), description=str(row.get("description") or "")).as_dict())
    return row

def enrich_catalog_item_delivery(item):
    raw = getattr(item, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    info = parse_lbc_delivery(raw, title=str(getattr(item, "title", "") or ""), description=str(getattr(item, "description", "") or ""))
    item.shipping_available = info.shipping_available
    item.hand_delivery_only = info.hand_delivery_only
    item.delivery_mode = info.delivery_mode
    return info.as_dict()

def is_hand_delivery_only_row(row):
    plat = str(row.get("platform") or "").lower()
    if plat not in ("leboncoin","lbc"): return False
    if row.get("hand_delivery_only"): return True
    if row.get("delivery_mode") == "hand_only": return True
    if row.get("shipping_available") is False and row.get("delivery_mode") != "both": return True
    return False

def filter_hand_delivery_rows(rows):
    out, dropped = [], 0
    for row in rows:
        r = enrich_row_delivery(dict(row))
        if is_hand_delivery_only_row(r): dropped += 1; continue
        out.append(r)
    return out, dropped

def filter_hand_delivery_catalog_items(items):
    kept, dropped = [], 0
    for it in items:
        plat = str(getattr(it, "platform", "") or "").lower()
        if plat in ("leboncoin","lbc"):
            enrich_catalog_item_delivery(it)
        elif not getattr(it, "hand_delivery_only", False):
            try:
                from .engine import infer_delivery_flags_from_text

                fl = infer_delivery_flags_from_text(
                    str(getattr(it, "title", "") or ""),
                    str(getattr(it, "description", "") or ""),
                )
                if fl.hand_delivery_only:
                    it.hand_delivery_only = True
                    it.delivery_mode = "hand_only"
                    if getattr(it, "shipping_available", None) is None:
                        it.shipping_available = False
            except Exception:
                pass
        if getattr(it, "hand_delivery_only", False):
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


# --- platform_select.py ---

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ALL_PLATFORMS: Tuple[str, ...] = (
    "vinted",
    "leboncoin",
    "wallapop",
    "ebay",
    "rakuten",
    "cashexpress",
)

# GUI Pro : plateformes actives par défaut (CLI/bench conserve tout ALL_PLATFORMS).
GUI_DEFAULT_PLATFORMS: Tuple[str, ...] = ("vinted", "leboncoin")
GUI_OPTIONAL_PLATFORMS: Tuple[str, ...] = ("ebay",)
GUI_ABANDONED_PLATFORMS: Tuple[str, ...] = ("wallapop", "rakuten", "cashexpress")

_ABANDONED = set(GUI_ABANDONED_PLATFORMS)

_CATEGORY_RULES: Dict[str, Dict[str, Any]] = {
    "apparel": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Mode : Vinted + LeBonCoin (sources fiables FR).",
    },
    "vetements": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Vetements : Vinted principal, LeBonCoin en complement.",
    },
    "mode": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Mode : Vinted-first + LeBonCoin.",
    },
    "meubles": {
        "on": ["leboncoin", "vinted"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Meubles / gros : LeBonCoin local + Vinted.",
    },
    "electronics": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Electronique : Vinted-first + LBC, eBay BIN optionnel.",
    },
    "games": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Jeux / consoles : Vinted + LeBonCoin, eBay optionnel.",
    },
    "console_ds": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Consoles DS/3DS : Vinted-first + LeBonCoin.",
    },
    "console_ps4": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "PlayStation : Vinted + LeBonCoin.",
    },
    "console_switch": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Switch : Vinted-first + LeBonCoin.",
    },
    "phone": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Telephones : Vinted + LBC, eBay optionnel.",
    },
    "projector": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Projecteurs : Vinted-first + LeBonCoin.",
    },
    "generic": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": list(_ABANDONED),
        "reason": "Defaut Pro : Vinted + LeBonCoin.",
    },
}

_CONSOLE_RE = re.compile(r"(?i)\b(console|3ds|2ds|switch|playstation|ps[45]|xbox|nintendo)\b")
_MEUBLE_RE = re.compile(r"(?i)\b(meuble|canape|table|armoire|lit\b|buffet|commode)\b")
_VETEMENT_RE = re.compile(r"(?i)\b(vetement|vêtement|mode|robe|pantalon|sweat|hoodie|jean)\b")
_PROJECTOR_RE = re.compile(r"(?i)\b(projecteur|videoprojecteur|vidéoprojecteur|epson|benq)\b")


@dataclass(slots=True)
class PlatformSelection:
    platforms: List[str] = field(default_factory=list)
    category: str = "generic"
    reason: str = ""
    skipped: List[str] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)
    auto: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": list(self.platforms),
            "category": self.category,
            "reason": self.reason,
            "skipped": list(self.skipped),
            "optional": list(self.optional),
            "auto": self.auto,
        }


def _normalize_category(category: str, query: str = "") -> str:
    c = (category or "generic").lower().strip()
    if c in ("unknown", ""):
        c = "generic"
    if c in _CATEGORY_RULES or c.startswith("console_"):
        return c
    q = (query or "").lower()
    if _MEUBLE_RE.search(q):
        return "meubles"
    if _VETEMENT_RE.search(q):
        return "vetements"
    if _PROJECTOR_RE.search(q):
        return "projector"
    if _CONSOLE_RE.search(q):
        return "games"
    return "generic"


def _sort_platforms(plats: List[str]) -> List[str]:
    order = {p: i for i, p in enumerate(ALL_PLATFORMS)}
    seen: set[str] = set()
    out: List[str] = []
    for p in sorted(plats, key=lambda x: order.get(x, 99)):
        k = p.lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress")
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def select_platforms(
    query: str = "",
    category: str = "",
    *,
    manual: Optional[List[str]] = None,
    auto: bool = True,
    include_optional: bool = False,
) -> PlatformSelection:
    cat = _normalize_category(category, query)
    if manual and not auto:
        plats = _sort_platforms(manual)
        return PlatformSelection(
            platforms=plats,
            category=cat,
            reason="Selection manuelle",
            skipped=[p for p in ALL_PLATFORMS if p not in plats],
            auto=False,
        )

    rule = dict(_CATEGORY_RULES.get(cat) or _CATEGORY_RULES["generic"])
    on = list(rule.get("on") or ["vinted", "leboncoin"])
    optional = list(rule.get("optional") or [])
    off = list(rule.get("off") or [])
    reason = str(rule.get("reason") or "")

    try:
        from vf_scrape.config import is_gui_mode, resolve_scrape_mode, scrape_mode_profile

        mode = resolve_scrape_mode()
        if mode == "fast":
            if "ebay" in on:
                on = [p for p in on if p != "ebay"]
            if "ebay" not in optional and "ebay" not in off:
                optional.append("ebay")
            if is_gui_mode() and not include_optional:
                optional = [p for p in optional if p != "ebay"]
                reason = (reason + " - Rapide : eBay desactive (toggle Inclure eBay)").strip(" -")
            elif scrape_mode_profile().skip_ebay_projector and cat == "projector":
                optional = [p for p in optional if p != "ebay"]
                if "ebay" not in off:
                    off.append("ebay")
                reason = (reason + " - Rapide : eBay ignore sur projecteurs").strip(" -")
    except Exception:
        pass

    if "vinted.fr" in (query or "").lower() or (
        (query or "").strip().startswith("http") and "vinted" in (query or "").lower()
    ):
        if "vinted" not in on:
            on.insert(0, "vinted")

    selected = list(dict.fromkeys(on))
    if include_optional:
        selected = list(dict.fromkeys([*selected, *[p for p in optional if p not in off]]))
    selected = [p for p in _sort_platforms(selected) if p not in off]
    skipped = [p for p in ALL_PLATFORMS if p not in selected and p not in optional]

    return PlatformSelection(
        platforms=selected,
        category=cat,
        reason=reason,
        skipped=skipped,
        optional=[p for p in optional if p not in selected],
        auto=True,
    )


def category_platform_profile(category: str, query: str = "") -> Dict[str, Any]:
    sel = select_platforms(query, category, auto=True)
    return {
        "category": sel.category,
        "platforms_on": sel.platforms,
        "platforms_optional": sel.optional,
        "platforms_off": sel.skipped,
        "hint": sel.reason,
        "all_keys": list(ALL_PLATFORMS),
        "selection": sel.to_dict(),
    }


def format_selection_log(sel: PlatformSelection) -> str:
    plats_txt = ", ".join(sel.platforms) or "-"
    parts = [f"Auto plateformes ({sel.category}) : {plats_txt}"]
    if sel.reason:
        parts.append(f"- {sel.reason}")
    if sel.skipped:
        parts.append(f"(ignore : {', '.join(sel.skipped)})")
    return " ".join(parts)


# --- category_tier.py ---

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vf.category_tier")

_SCHEMA_DIR = Path("brouillon") / "data" / "category_schemas"

TIER1_CATEGORIES = frozenset(
    {
        "console_ds",
        "console_ps4",
        "console_ps5",
        "console_switch",
        "console_xbox",
        "projector",
        "phone",
        "wearable",
        "ac",
        "coffee",
        "fan",
        "small_appliance",
        "apparel",
        "shoes",
        "jeans",
        "pc",
    }
)

TIER2_FAMILIES = frozenset(
    {
        "bicycle",
        "camera",
        "audio",
        "laptop",
        "vinyl",
        "sports",
        "tent",
        "skate",
        "skates",
        "surf",
    }
)


def resolve_category_tier(category: str) -> int:
    cat = (category or "unknown").lower()
    if cat in TIER1_CATEGORIES:
        return 1
    if cat in TIER2_FAMILIES:
        return 2
    if cat.startswith("console_"):
        return 1
    if cat in ("unknown", "generic", ""):
        return 3
    return 3


def _schema_path(category: str) -> Path:
    safe = re.sub(r"[^\w\-]+", "_", (category or "generic").lower()).strip("_") or "generic"
    return _SCHEMA_DIR / f"{safe}.json"


def load_category_schema(category: str) -> Optional[Dict[str, Any]]:
    path = _schema_path(category)
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_category_schema(category: str, schema: Dict[str, Any]) -> Path:
    path = _schema_path(category)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def bootstrap_category_schema(
    category: str,
    *,
    search_intent: str = "",
    use_ai: bool = True,
) -> Dict[str, Any]:
    """Tier 2 — one LLM call per category, cached on disk."""
    cached = load_category_schema(category)
    if cached:
        return cached

    default: Dict[str, Any] = {
        "category": category,
        "tier": 2,
        "spec_fields": [],
        "filter_fields": [],
        "pack_hints": [],
        "negative_keywords": [],
    }

    if not use_ai:
        save_category_schema(category, default)
        return default

    try:
        from .ai import _chat, _extract_json_lenient, api_key

        if not api_key():
            save_category_schema(category, default)
            return default
        prompt = (
            f"Marché produit: {category}\nRecherche type: {search_intent[:200] or category}\n\n"
            "Quels champs specs comptent pour un acheteur sur Vinted/LeBonCoin ?\n"
            'JSON: {"spec_fields":["…"],"filter_fields":[{"id":"…","label":"…","field":"…","type":"select"}],'
            '"pack_hints":["chargeur inclus",…],"negative_keywords":["accessoire seul",…]}'
        )
        raw = _chat(prompt, system="Expert shopping occasion. JSON strict.", max_tokens=700)
        obj = _extract_json_lenient(raw)
        if isinstance(obj, dict):
            default.update({k: obj[k] for k in obj if k in default or k == "filter_fields"})
            default["tier"] = 2
            default["category"] = category
    except Exception as exc:  # noqa: BLE001
        logger.warning("Schema bootstrap %s: %s", category, exc)

    save_category_schema(category, default)
    return default


def ensure_category_schema(
    category: str,
    *,
    search_intent: str = "",
    use_ai: bool = True,
) -> Dict[str, Any]:
    tier = resolve_category_tier(category)
    if tier == 1:
        return {"category": category, "tier": 1}
    if tier == 2:
        return bootstrap_category_schema(category, search_intent=search_intent, use_ai=use_ai)
    return {"category": category, "tier": 3}


def extract_specs_tier2(row: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """Extraction heuristique légère depuis champs schema (complète l'IA deep-fetch)."""
    if not schema or schema.get("tier") != 2:
        return {}
    text = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    out: Dict[str, Any] = {}
    for field in schema.get("spec_fields") or []:
        f = str(field).lower()
        if f in ("frame_size", "taille_cadre") and re.search(r"(?i)\b(\d{2})\s*(cm|''|″)\b", text):
            m = re.search(r"(?i)\b(\d{2})\s*(cm|''|″)\b", text)
            if m:
                out["frame_size_cm"] = int(m.group(1))
        elif f in ("wheel_size", "roues") and re.search(r"(?i)\b(26|27\.5|29)\s*[\"″]?\b", text):
            m = re.search(r"(?i)\b(26|27\.5|29)\b", text)
            if m:
                out["wheel_size_in"] = m.group(1)
        elif f in ("storage_gb", "ram_gb", "ssd_gb"):
            m = re.search(r"(?i)\b(\d{2,4})\s*(go|gb|to|tb)\b", text)
            if m:
                out[f] = int(m.group(1))
        elif f in ("brand", "marque"):
            for brand in ("trek", "specialized", "cube", "decathlon", "sony", "canon", "nikon", "fujifilm"):
                if brand in text:
                    out["brand"] = brand.capitalize()
                    break
    neg = schema.get("negative_keywords") or []
    for kw in neg:
        if str(kw).lower() in text:
            out["tier2_negative_hit"] = True
            break
    return out


def schema_filter_specs(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Filtres UI dérivés du schema Tier 2."""
    fl = schema.get("filter_fields") if isinstance(schema.get("filter_fields"), list) else []
    out: List[Dict[str, Any]] = []
    for f in fl:
        if isinstance(f, dict) and f.get("id") and f.get("field"):
            spec = dict(f)
            spec.setdefault("segment", "market")
            spec.setdefault("type", "select")
            spec.setdefault("options", ["all"])
            out.append(spec)
    return out


# --- market_rules.py ---

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Tuple

_RULES_PATH = Path(__file__).resolve().parent.parent / "brouillon" / "data" / "market_rules.json"

_UNKNOWN_BRAND = re.compile(
    r"(?i)\b(marque\s*non\s*sp[ée]cifi[ée]e|sans\s*marque|inconnue?|generic|unbranded|"
    r"no\s*brand|white\s*label|non\s*sp[ée]cifi[ée]|oem\s*g[ée]n[ée]rique)\b"
)
_HYPE_DROPSHIP = re.compile(
    r"(?i)\b(aliexpress|temu|dropship|replica|copie\s*1:1|super\s*copie|contrefa[çc]on|"
    r"fausse?\s*marque|imitation|aaa\s*quality)\b"
)
_NO_BRAND_VALUES = frozenset({"", "unknown", "inconnu", "n/a", "none", "—", "null", "generic"})

BRAND_MULTIPLIERS: Dict[str, float] = {
    "premium": 1.20,
    "good": 1.10,
    "neutral": 1.00,
    "budget_bad": 0.50,
    "unknown": 0.65,
    "scam_hype": 0.25,
}


@dataclass
class ScamRule:
    rule_id: str
    pattern: Pattern[str]
    max_price: float
    categories: Tuple[str, ...] = ()


@dataclass
class BrandProfile:
    premium: Pattern[str]
    good: Optional[Pattern[str]] = None
    budget_bad: Optional[Pattern[str]] = None
    vague_title: Optional[Pattern[str]] = None
    scam_rules: List[ScamRule] = field(default_factory=list)


def _rx(parts: str) -> Pattern[str]:
    return re.compile(parts, re.I)


_SCAM_GLOBAL = [
    ScamRule("hype_keyword", _HYPE_DROPSHIP, 99999.0),
]

# Prix plancher — si le titre correspond et le prix est sous le seuil → arnaque/contrefaçon.
_PRICE_FLOOR_SCAM_IDS = frozenset(
    {
        "iphone_flagship",
        "galaxy_flagship",
        "ps5_cheap",
        "xbox_series",
        "switch_oled",
        "luxury_bag",
        "airpods_pro",
        "sony_xm5",
        "apple_watch_ultra",
        "garmin_fenix",
        "rtx_gaming",
        "macbook_pro",
        "full_frame",
        "carbon_road",
        "ac_mobile_hype",
        "jordan_limited",
    }
)

_PROFILES: Dict[str, BrandProfile] = {
    "projector": BrandProfile(
        premium=_rx(
            r"\b(epson|sony|jvc|optoma|benq|viewsonic|acer|lg|panasonic|nec|christie|infocus)\b"
        ),
        good=_rx(r"\b(xgimi|nebula|anker\s*nebula|formovie|wanbo|jmgo|dangbei|mogo\s*pro|samsung\s*freestyle)\b"),
        budget_bad=_rx(r"\b(neopix|picopix|philips\s*neopix|philips\s*picopix)\b"),
        vague_title=_rx(r"^(vid[ée]o\s*projecteur|mini\s*projecteur|projecteur|videoprojecteur)\s*$"),
        scam_rules=[
            ScamRule(
                "fake_4k",
                _rx(
                    r"\b(4k\s*uhd|uhd\s*4k|native\s*4k|vraie?\s*4k|4k\s*hdr|"
                    r"vid[ée]oprojecteur\s*4k|projecteur\s*4k|4k\s*full\s*hd)\b|\b4k\b"
                ),
                300.0,
                ("projector",),
            ),
        ],
    ),
    "phone": BrandProfile(
        premium=_rx(r"\b(apple|iphone|samsung|google\s*pixel|pixel|oneplus|xiaomi|huawei|oppo|nothing)\b"),
        budget_bad=_rx(r"\b(goophone|hiphone|i\s*phone\s*clone|fake\s*iphone|replica\s*iphone)\b"),
        vague_title=_rx(r"^(t[ée]l[ée]phone|smartphone|mobile)\s*$"),
        scam_rules=[
            ScamRule("iphone_flagship", _rx(r"\b(iphone\s*1[3-9]|iphone\s*1[0-6]\s*pro)\b"), 130.0, ("phone",)),
            ScamRule("galaxy_flagship", _rx(r"\b(galaxy\s*s2[4-9]|galaxy\s*z\s*fold|galaxy\s*z\s*flip\s*5)\b"), 160.0, ("phone",)),
        ],
    ),
    "console": BrandProfile(
        premium=_rx(r"\b(nintendo|playstation|ps[345]|xbox|microsoft|valve\s*steam\s*deck|steam\s*deck)\b"),
        budget_bad=_rx(r"\b(r4\s*card|carte\s*r4|clone\s*console|imitation)\b"),
        vague_title=_rx(r"^(console|manette|jeu\s*vid[ée]o)\s*$"),
        scam_rules=[
            ScamRule("ps5_cheap", _rx(r"\b(ps5|playstation\s*5)\b"), 90.0, ("console",)),
            ScamRule("xbox_series", _rx(r"\b(xbox\s*series\s*x|series\s*x)\b"), 90.0, ("console",)),
            ScamRule("switch_oled", _rx(r"\b(switch\s*oled)\b"), 110.0, ("console",)),
        ],
    ),
    "wearable": BrandProfile(
        premium=_rx(r"\b(apple\s*watch|garmin|suunto|polar|coros|fitbit|samsung\s*galaxy\s*watch)\b"),
        budget_bad=_rx(r"\b(smart\s*watch\s*generic|montre\s*connect[ée]e\s*bluetooth)\b"),
        vague_title=_rx(r"^(montre|smartwatch|montre\s*connect[ée]e)\s*$"),
        scam_rules=[
            ScamRule("apple_watch_ultra", _rx(r"\b(apple\s*watch\s*ultra|watch\s*ultra\s*2)\b"), 90.0, ("wearable",)),
            ScamRule("garmin_fenix", _rx(r"\b(fenix\s*[78]|forerunner\s*9[56])\b"), 120.0, ("wearable",)),
        ],
    ),
    "pc": BrandProfile(
        premium=_rx(r"\b(apple|macbook|dell|lenovo|thinkpad|hp|asus|msi|razer|framework)\b"),
        budget_bad=_rx(r"\b(chromebook\s*reconditionn[ée]|pc\s*bureau\s*complet\s*gamer)\b"),
        vague_title=_rx(r"^(pc|ordinateur|laptop|portable)\s*$"),
        scam_rules=[
            ScamRule("rtx_gaming", _rx(r"\b(rtx\s*40[56789]0|rtx\s*3080|rtx\s*4090|gaming\s*laptop)\b"), 220.0, ("pc",)),
            ScamRule("macbook_pro", _rx(r"\b(macbook\s*pro\s*m[123])\b"), 350.0, ("pc",)),
        ],
    ),
    "laptop": BrandProfile(
        premium=_rx(r"\b(apple|macbook|dell|lenovo|thinkpad|hp|asus|msi|razer|framework)\b"),
        budget_bad=_rx(r"\b(pc\s*portable\s*gamer\s*neuf)\b"),
        vague_title=_rx(r"^(pc\s*portable|ordinateur\s*portable|laptop)\s*$"),
        scam_rules=[
            ScamRule("rtx_gaming", _rx(r"\b(rtx\s*40[56789]0|rtx\s*3080|gaming\s*laptop)\b"), 220.0, ("laptop",)),
        ],
    ),
    "audio": BrandProfile(
        premium=_rx(r"\b(sony|bose|sennheiser|audio-technica|beyerdynamic|focal|devialet|bang\s*&\s*olufsen)\b"),
        good=_rx(r"\b(jbl|marshall|anker|soundcore|nothing\s*ear)\b"),
        budget_bad=_rx(r"\b(earbuds\s*bluetooth\s*5|casque\s*sans\s*fil\s*pro)\b"),
        vague_title=_rx(r"^(casque|écouteurs|enceinte|airpods)\s*$"),
        scam_rules=[
            ScamRule("airpods_pro", _rx(r"\b(airpods\s*pro\s*[23]?)\b"), 45.0, ("audio",)),
            ScamRule("sony_xm5", _rx(r"\b(wh-1000xm[45]|wf-1000xm[45])\b"), 80.0, ("audio",)),
        ],
    ),
    "apparel": BrandProfile(
        premium=_rx(
            r"\b(nike|adidas|patagonia|arc'teryx|north\s*face|carhartt|levi's|levis|"
            r"louis\s*vuitton|gucci|chanel|hermes|dior|moncler|stone\s*island)\b"
        ),
        good=_rx(r"\b(zara|h&m|uniqlo|cos|mango|decathlon)\b"),
        budget_bad=_rx(r"\b(lot\s*de\s*v[êe]tements|v[êe]tement\s*neuf\s*avec\s*[ée]tiquette)\b"),
        vague_title=_rx(r"^(robe|pantalon|veste|sac|chaussure|v[êe]tement)\s*$"),
        scam_rules=[
            ScamRule("luxury_bag", _rx(r"\b(louis\s*vuitton|gucci|chanel|hermes|dior)\b"), 45.0, ("apparel",)),
        ],
    ),
    "shoes": BrandProfile(
        premium=_rx(r"\b(nike|adidas|new\s*balance|asics|salomon|jordan|yeezy|hoka)\b"),
        vague_title=_rx(r"^(chaussure|basket|sneaker)\s*$"),
        scam_rules=[
            ScamRule("jordan_limited", _rx(r"\b(air\s*jordan\s*1|jordan\s*4|yeezy)\b"), 50.0, ("shoes",)),
        ],
    ),
    "jeans": BrandProfile(
        premium=_rx(r"\b(levi's|levis|lee|wrangler|nudie|acne\s*studios)\b"),
        vague_title=_rx(r"^(jean|jeans|denim)\s*$"),
    ),
    "ac": BrandProfile(
        premium=_rx(r"\b(daikin|mitsubishi|atlantic|fujitsu|daikin|toshiba)\b"),
        good=_rx(r"\b(delonghi|rowenta|dyson)\b"),
        budget_bad=_rx(r"\b(climatiseur\s*mobile\s*12000|clim\s*mobile\s*9000)\b"),
        vague_title=_rx(r"^(climatiseur|clim|ventilateur)\s*$"),
        scam_rules=[
            ScamRule("ac_mobile_hype", _rx(r"\b(12000\s*btu|9000\s*btu|climatisation\s*mobile)\b"), 85.0, ("ac",)),
        ],
    ),
    "coffee": BrandProfile(
        premium=_rx(r"\b(delonghi|sage|breville|jura|miele|smeg|philips\s*barista)\b"),
        budget_bad=_rx(r"\b(machine\s*[àa]\s*caf[ée]\s*capsule\s*compatible)\b"),
        vague_title=_rx(r"^(machine\s*[àa]\s*caf[ée]|cafeti[èe]re)\s*$"),
    ),
    "fan": BrandProfile(
        premium=_rx(r"\b(dyson|rowenta|ventilateur\s*dyson)\b"),
        vague_title=_rx(r"^(ventilateur|brumisateur)\s*$"),
    ),
    "camera": BrandProfile(
        premium=_rx(r"\b(canon|nikon|sony|fujifilm|panasonic|leica|gopro)\b"),
        vague_title=_rx(r"^(appareil\s*photo|cam[ée]ra|reflex)\s*$"),
        scam_rules=[
            ScamRule("full_frame", _rx(r"\b(r[56]|a7\s*(iv|v)|z\s*6\s*iii|eos\s*r[56])\b"), 350.0, ("camera",)),
        ],
    ),
    "bicycle": BrandProfile(
        premium=_rx(r"\b(cannondale|trek|specialized|giant|canyon|scott|bianchi)\b"),
        vague_title=_rx(r"^(v[ée]lo|bicyclette|vt[te])\s*$"),
        scam_rules=[
            ScamRule("carbon_road", _rx(r"\b(v[ée]lo\s*route\s*carbone|cadre\s*carbone\s*shimano)\b"), 120.0, ("bicycle",)),
        ],
    ),
    "_default": BrandProfile(
        premium=_rx(
            r"\b(apple|samsung|sony|nintendo|dyson|bosch|miele|ikea|philips|"
            r"bosch|siemens|kitchenaid|lego|canon|nikon)\b"
        ),
        vague_title=_rx(r"^[a-zàâçéèêëîïôùûü\s\-]{3,28}$"),
        scam_rules=[],
    ),
}


def _keywords_to_rx(keywords: list) -> Pattern[str]:
    parts = [re.escape(str(k).strip()) for k in keywords if str(k).strip()]
    if not parts:
        return re.compile(r"(?!)")
    return re.compile(r"(?i)\b(" + "|".join(parts) + r")\b")


def _profile_from_json(data: dict) -> BrandProfile:
    scam_rules: List[ScamRule] = []
    for sr in data.get("scam_rules") or []:
        if not isinstance(sr, dict):
            continue
        pat = sr.get("pattern")
        if pat:
            rx = _rx(str(pat))
        else:
            rx = _keywords_to_rx(sr.get("keywords") or [])
        scam_rules.append(
            ScamRule(
                str(sr.get("id") or "custom"),
                rx,
                float(sr.get("max_price") or 99999),
                tuple(sr.get("categories") or ()),
            )
        )
    prem = data.get("premium") or []
    good = data.get("good") or []
    bad = data.get("budget_bad") or []
    vague = data.get("vague_title")
    return BrandProfile(
        premium=_keywords_to_rx(prem) if isinstance(prem, list) else _rx(str(prem or "(?!)")),
        good=_keywords_to_rx(good) if good else None,
        budget_bad=_keywords_to_rx(bad) if bad else None,
        vague_title=_rx(str(vague)) if vague else None,
        scam_rules=scam_rules,
    )


def reload_market_rules() -> None:
    """Recharge profils depuis JSON (écrase/complète les builtins)."""
    global _PROFILES
    if not _RULES_PATH.exists():
        return
    try:
        raw = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    mult = raw.get("multipliers")
    if isinstance(mult, dict):
        for k, v in mult.items():
            try:
                BRAND_MULTIPLIERS[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        return
    for key, pdata in profiles.items():
        if isinstance(pdata, dict):
            _PROFILES[str(key)] = _profile_from_json(pdata)


reload_market_rules()


def _row_text(row: Dict[str, Any]) -> str:
    return f"{row.get('title') or ''} {row.get('description') or ''}"


def _listing_price(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("total_price") or row.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _brand_field(row: Dict[str, Any]) -> str:
    return str(row.get("brand") or row.get("product_brand") or "").strip().lower()


def _is_unknown_brand(row: Dict[str, Any], text: str) -> bool:
    brand = _brand_field(row)
    if brand and brand not in _NO_BRAND_VALUES:
        return False
    if _UNKNOWN_BRAND.search(text):
        return True
    title = str(row.get("title") or "").strip()
    if len(title.split()) <= 5 and not _PROFILES["_default"].premium.search(text):
        return True
    return brand in _NO_BRAND_VALUES


def category_profile_key(row: Dict[str, Any]) -> str:
    cat = str(row.get("item_type") or row.get("market_category") or "generic").lower()
    if cat.startswith("console_"):
        return "console"
    if cat in ("shoes", "jeans"):
        return cat
    if cat in ("apparel", "motorcycle", "skate"):
        return "apparel"
    if cat == "skates":
        return "apparel"
    if cat in _PROFILES:
        return cat
    if cat in ("laptop", "pc"):
        return cat
    return "_default"


def resolve_brand_profile(row: Dict[str, Any]) -> BrandProfile:
    return _PROFILES.get(category_profile_key(row), _PROFILES["_default"])


def detect_scam_rules(row: Dict[str, Any]) -> Optional[str]:
    """Retourne l'id de la première règle scam déclenchée."""
    text = _row_text(row)
    price = _listing_price(row)
    if price <= 0:
        return None
    cat_key = category_profile_key(row)
    profile = resolve_brand_profile(row)
    unknown = _is_unknown_brand(row, text)
    premium_hit = bool(profile.premium.search(text)) or bool(
        profile.good and profile.good.search(text)
    )

    for rule in _SCAM_GLOBAL + profile.scam_rules:
        if rule.categories and cat_key not in rule.categories:
            continue
        if not rule.pattern.search(text):
            continue
        if price >= rule.max_price:
            continue
        if rule.rule_id == "hype_keyword":
            return rule.rule_id
        if rule.rule_id in _PRICE_FLOOR_SCAM_IDS:
            return rule.rule_id
        if rule.rule_id == "fake_4k" and premium_hit:
            continue
        if unknown or len(text.split()) <= 8:
            return rule.rule_id
    return None


def brand_class(row: Dict[str, Any]) -> str:
    scam = detect_scam_rules(row)
    if scam:
        return "scam_hype"
    profile = resolve_brand_profile(row)
    text = _row_text(row)
    if profile.budget_bad and profile.budget_bad.search(text):
        return "budget_bad"
    if profile.premium.search(text):
        return "premium"
    if profile.good and profile.good.search(text):
        return "good"
    if _is_unknown_brand(row, text):
        if profile.vague_title and profile.vague_title.match(str(row.get("title") or "").strip()[:90]):
            return "unknown"
        if _HYPE_DROPSHIP.search(text):
            return "unknown"
        if len(str(row.get("title") or "").split()) <= 4:
            return "unknown"
    return "neutral"


def brand_quality_multiplier(row: Dict[str, Any]) -> float:
    cls = str(row.get("brand_quality_class") or brand_class(row))
    return float(BRAND_MULTIPLIERS.get(cls, 1.0))


def brand_score_adjustment(row: Dict[str, Any]) -> float:
    mult = brand_quality_multiplier(row)
    if abs(mult - 1.0) < 0.02:
        return 0.0
    return round((mult - 1.0) * 40.0, 2)


def brand_sort_bonus(row: Dict[str, Any]) -> float:
    """Boost tri relatif — pépites marques reconnues avant clones à score artificiel."""
    if row.get("is_accessory"):
        return -30.0
    bonus = 0.0
    tier = str(row.get("quality_tier") or "")
    cls = str(row.get("brand_quality_class") or brand_class(row))
    flags = str(row.get("quality_flags") or "")
    scam_flag = str(row.get("scam_rule_id") or "")

    if scam_flag or "impossible_price_scam" in flags or cls == "scam_hype":
        return -50.0
    if cls == "budget_bad":
        bonus -= 26.0
    elif cls == "unknown":
        bonus -= 12.0
    if tier == "gem" and cls == "premium":
        bonus += 22.0
    elif tier == "gem":
        bonus += 12.0
    elif cls == "premium" and tier == "ok":
        bonus += 8.0
    elif cls == "good" and tier in ("gem", "ok"):
        bonus += 4.0
    if tier in ("dropship", "dinosaur"):
        bonus -= 16.0
    return bonus


def apply_brand_quality_audit(row: Dict[str, Any]) -> None:
    """Post-traitement universel : tier, flags, overlay score."""
    if row.get("is_accessory"):
        return

    text = _row_text(row)
    cls = brand_class(row)
    scam_id = detect_scam_rules(row)
    row["brand_quality_class"] = cls
    row["brand_quality_mult"] = brand_quality_multiplier(row)
    if scam_id:
        row["scam_rule_id"] = scam_id

    flags = [f for f in str(row.get("quality_flags") or "").split("|") if f]
    overlay = float(row.get("score_overlay") or 0.0)
    tier = str(row.get("quality_tier") or "ok")

    if scam_id:
        row["quality_tier"] = "dropship"
        flag_name = "fake_4k_scam" if scam_id == "fake_4k" else "impossible_price_scam"
        if flag_name not in flags:
            flags.append(flag_name)
        if scam_id not in flags:
            flags.append(scam_id)
        overlay -= 36.0
        rf = str(row.get("risk_flags") or "")
        risk_tag = "scam_4k" if scam_id == "fake_4k" else "scam_hype"
        if risk_tag not in rf:
            row["risk_flags"] = f"{rf} {risk_tag}".strip()
    elif cls == "budget_bad" and tier in ("gem", "ok"):
        row["quality_tier"] = "suspicious"
        if "budget_bad_brand" not in flags:
            flags.append("budget_bad_brand")
        overlay -= 20.0
    elif cls == "unknown":
        if tier == "gem":
            row["quality_tier"] = "suspicious"
        if "unknown_brand" not in flags:
            flags.append("unknown_brand")
        overlay -= 10.0
        if _HYPE_DROPSHIP.search(text) and tier not in ("dropship", "dinosaur"):
            row["quality_tier"] = "dropship"
            flags.append("hype_dropship")
            overlay -= 14.0

    try:
        disc = float(row.get("market_discount_pct") or row.get("model_vs_market_pct") or 0)
    except (TypeError, ValueError):
        disc = 0.0
    if disc > 78.0 and cls in ("unknown", "budget_bad") and tier not in ("dropship", "dinosaur"):
        row["quality_tier"] = "suspicious"
        if "inflated_discount" not in flags:
            flags.append("inflated_discount")
        overlay -= 8.0

    if cls not in ("neutral", "premium", "good"):
        flags.append(f"brand_{cls}")

    if flags:
        row["quality_flags"] = "|".join(dict.fromkeys(flags))[:180]
    if abs(overlay - float(row.get("score_overlay") or 0.0)) >= 0.5:
        row["score_overlay"] = round(overlay, 2)


def cap_brand_deal_score(row: Dict[str, Any]) -> Optional[float]:
    """Plafond score si arnaque ou marque douteuse."""
    if row.get("is_accessory"):
        return None
    flags = str(row.get("quality_flags") or "")
    cls = str(row.get("brand_quality_class") or "")
    scam = str(row.get("scam_rule_id") or "")
    if scam or "impossible_price_scam" in flags or cls == "scam_hype":
        return 32.0 if scam in ("fake_4k", "hype_keyword") else 35.0
    if cls == "budget_bad" or "budget_bad_brand" in flags:
        return 52.0
    if cls == "unknown" and str(row.get("quality_tier") or "") in ("dropship", "suspicious"):
        return 46.0
    return None


# --- spec_resolver.py ---

import re
from typing import Any, Dict, Optional, Tuple

_STORAGE_RE = re.compile(
    r"(?i)\b(\d{1,4})\s*"
    r"(go|gb|g\b|giga|gigas|gigaoctet|gigaoctets|"
    r"to|tb|téra|teraoctet|teraoctets)\b"
)
_RAM_RE = re.compile(
    r"(?i)\b(\d{1,3})\s*(go|gb|g\b|giga|ram)\b"
)
_POWER_KW_RE = re.compile(r"(?i)\b(\d(?:[.,]\d{1,2})?)\s*k(?:w|wh)\b")
_BTU_RE = re.compile(r"(?i)\b(\d{3,6})\s*btu\b")
_LUMENS_RE = re.compile(r"(?i)\b(\d{3,6})\s*(?:lumens?|lm|ansi\s*lum)\b")


def _to_int(val: str) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_float(val: str) -> Optional[float]:
    try:
        return float(str(val).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_storage_gb(text: str) -> Optional[int]:
    """500Gb / 500 Go / 500G / 1 To → entier GB."""
    for m in _STORAGE_RE.finditer(text or ""):
        v = _to_int(m.group(1))
        if v is None:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("to", "tb", "téra", "teraoctet", "teraoctets"):
            return v * 1024
        return v
    return None


def parse_ram_gb(text: str) -> Optional[int]:
    m = _RAM_RE.search(text or "")
    if not m:
        return None
    return _to_int(m.group(1))


def parse_power_kw(text: str) -> Optional[float]:
    m = _POWER_KW_RE.search(text or "")
    if not m:
        return None
    return _to_float(m.group(1))


def parse_btu(text: str) -> Optional[int]:
    m = _BTU_RE.search(text or "")
    if not m:
        return None
    return _to_int(m.group(1))


def parse_lumens(text: str) -> Optional[int]:
    m = _LUMENS_RE.search(text or "")
    if not m:
        return None
    return _to_int(m.group(1))


def normalize_specs_from_text(text: str, category: str = "") -> Dict[str, Any]:
    """Extrait specs normalisées depuis un texte libre."""
    out: Dict[str, Any] = {}
    storage = parse_storage_gb(text)
    if storage is not None:
        out["storage_gb"] = storage
        out["storage_unit"] = "GB"
    ram = parse_ram_gb(text)
    if ram is not None:
        out["ram_gb"] = ram
        out["ram_unit"] = "GB"
    kw = parse_power_kw(text)
    if kw is not None:
        out["cooling_kw"] = kw
        out["power_unit"] = "kW"
    btu = parse_btu(text)
    if btu is not None:
        out["btu"] = btu
        out["power_unit"] = out.get("power_unit") or "BTU"
    lum = parse_lumens(text)
    if lum is not None:
        out["lumens"] = lum
        out["light_unit"] = "lm"
    return out


def merge_normalized_specs(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in source.items():
        if v is not None and v != "" and target.get(k) in (None, "", 0):
            target[k] = v
    return target


# --- browser.py ---



import logging

import os

import subprocess

import sys

import webbrowser

from typing import Iterable, List, Optional



logger = logging.getLogger("vf.browser")



_CHROME_PATHS: List[str] = [

    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),

    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),

    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),

]



_START_MENU_DIRS: List[str] = [

    os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),

    os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),

    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",

]



_REG_APP_PATHS = (

    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",

    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",

)



_chrome_path: Optional[str] = None





def _reg_chrome_paths() -> List[str]:

    if sys.platform != "win32":

        return []

    try:

        import winreg

    except ImportError:

        return []

    found: List[str] = []

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):

        for sub in _REG_APP_PATHS:

            try:

                with winreg.OpenKey(root, sub) as key:

                    val, _ = winreg.QueryValueEx(key, "")

                    if val and os.path.isfile(val):

                        found.append(val)

            except OSError:

                pass

    return found





def _resolve_lnk(lnk_path: str) -> Optional[str]:

    if sys.platform != "win32" or not lnk_path.lower().endswith(".lnk"):

        return None

    try:

        proc = subprocess.run(

            [

                "powershell",

                "-NoProfile",

                "-Command",

                f"(New-Object -ComObject WScript.Shell).CreateShortcut({lnk_path!r}).TargetPath",

            ],

            capture_output=True,

            text=True,

            timeout=8,

            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),

        )

        target = (proc.stdout or "").strip()

        if target and os.path.isfile(target):

            return target

    except Exception as exc:  # noqa: BLE001

        logger.debug("Résolution .lnk échouée (%s): %s", lnk_path, exc)

    return None





def _start_menu_chrome_paths() -> List[str]:

    out: List[str] = []

    for base in _START_MENU_DIRS:

        if not base or not os.path.isdir(base):

            continue

        try:

            for root, _dirs, files in os.walk(base):

                for name in files:

                    low = name.lower()

                    if not low.endswith(".lnk"):

                        continue

                    if "chrome" not in low or "remote" in low:

                        continue

                    lnk = os.path.join(root, name)

                    target = _resolve_lnk(lnk)

                    if target:

                        out.append(target)

        except OSError:

            pass

    return out





def _iter_candidates() -> Iterable[str]:

    seen: set[str] = set()

    for group in (_reg_chrome_paths(), _CHROME_PATHS, _start_menu_chrome_paths()):

        for path in group:

            p = os.path.normpath(path)

            if p not in seen:

                seen.add(p)

                yield p





def _chrome_exe() -> Optional[str]:

    global _chrome_path

    if _chrome_path is not None:

        return _chrome_path or None

    for path in _iter_candidates():

        if path and os.path.isfile(path):

            _chrome_path = path

            logger.info("Chrome pour liens annonces : %s", path)

            return path

    _chrome_path = ""

    logger.warning(

        "Chrome introuvable (chemins standards + registre + menu Démarrer). "

        "Repli navigateur par défaut (souvent Edge)."

    )

    return None





def reset_chrome_cache() -> None:

    """Force une nouvelle détection (utile après installation de Chrome)."""

    global _chrome_path

    _chrome_path = None





def listing_platform_label(row: dict) -> str:

    plat = str(row.get("platform") or "vinted").lower()

    if plat == "leboncoin":

        return "LeBonCoin"

    if plat == "wallapop":

        return "Wallapop"

    if plat == "rakuten":

        return "Rakuten"

    if plat == "cashexpress":

        return "Cash Express"

    if plat == "ebay":

        return "eBay"

    return "Vinted"





def listing_url_for_row(row: dict) -> str:

    """URL annonce â€” Vinted, LeBonCoin, Wallapop."""

    u = str(row.get("url") or "").strip()

    if u.startswith(("http://", "https://")):

        return u

    plat = str(row.get("platform") or "vinted").lower()

    sid = str(row.get("platform_item_id") or row.get("id") or "").strip()

    if plat == "leboncoin" and sid:

        return f"https://www.leboncoin.fr/ad/{sid}"

    if plat == "wallapop":

        if sid:

            return f"https://fr.wallapop.com/item/i-{sid}"

        return u

    if plat in ("rakuten", "cashexpress") and u:

        return u

    if sid.isdigit():

        return f"https://www.vinted.fr/items/{sid}"

    path = str(row.get("path") or "").strip()

    if path.startswith("/"):

        return f"https://www.vinted.fr{path}"

    return u


def enrich_row_listing_fields(row: dict) -> dict:
    """Garantit url + photo_url pour l'affichage GUI (toutes plateformes)."""
    u = listing_url_for_row(row)
    if u:
        row["url"] = u
    if not row.get("photo_url"):
        urls = row.get("photo_urls") or []
        if urls:
            row["photo_url"] = str(urls[0])
    return row


def listing_urls_for_row(row: dict) -> List[str]:
    """URL principale de l'annonce."""
    u = listing_url_for_row(row)
    return [u] if u else []


def open_listing_url(url: str) -> bool:

    """

    Ouvre une URL dâ€™annonce dans Google Chrome (subprocess direct).

    Repli webbrowser uniquement si Chrome est absent.

    """

    u = (url or "").strip()

    if not u:

        return False

    exe = _chrome_exe()

    if exe:

        try:

            kwargs: dict = {

                "stdout": subprocess.DEVNULL,

                "stderr": subprocess.DEVNULL,

            }

            if sys.platform == "win32":

                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            else:

                kwargs["close_fds"] = True

            subprocess.Popen([exe, u], **kwargs)

            return True

        except Exception as exc:  # noqa: BLE001

            logger.warning("Chrome subprocess échoué (%s): %s — repli système", exe, exc)

    try:

        webbrowser.open(u, new=2)

        return True

    except Exception as exc:  # noqa: BLE001

        logger.warning("Ouverture navigateur échouée: %s", exc)

        return False



# chrome_driver

import logging
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("vf.chrome_driver")

_LAUNCH_LOCK = threading.RLock()

_CHROME_VER_CACHE: Optional[int] = None


def bootstrap_timeout_sec(platform: str = "") -> float:
    """Délai max bootstrap navigateur (défaut 75s, plafond 120s)."""
    key = {
        "vinted": "VF_VINTED_BOOTSTRAP_TIMEOUT",
        "leboncoin": "VF_LBC_BOOTSTRAP_TIMEOUT",
        "lbc": "VF_LBC_BOOTSTRAP_TIMEOUT",
        "rakuten": "VF_RAKUTEN_BOOTSTRAP_TIMEOUT",
        "cashexpress": "VF_CEX_BOOTSTRAP_TIMEOUT",
        "wallapop": "VF_WALLAPOP_BOOTSTRAP_TIMEOUT",
        "ebay": "VF_EBAY_BOOTSTRAP_TIMEOUT",
    }.get((platform or "").lower().replace("cash-express", "cashexpress"), "")
    raw = os.environ.get(key, os.environ.get("VF_BOOTSTRAP_TIMEOUT_SEC", "75")) if key else os.environ.get(
        "VF_BOOTSTRAP_TIMEOUT_SEC", "75"
    )
    try:
        return min(120.0, max(15.0, float(raw or "75")))
    except (TypeError, ValueError):
        return 75.0


def run_with_timeout(fn: Any, timeout_s: float, *, label: str = "bootstrap") -> Any:
    """Exécute fn dans un thread démon avec timeout — évite bench bloqué indéfiniment.

    Un thread Python ne peut pas être tué ; on le laisse détaché (daemon) pour que le
    bench continue à la plateforme suivante et que le process puisse quitter proprement
    même si un fetch navigateur reste bloqué.
    """
    import threading

    deadline = min(150.0, max(10.0, float(timeout_s)))
    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=_runner, name=f"vf-{label}", daemon=True)
    th.start()
    th.join(deadline)
    if th.is_alive():
        raise TimeoutError(f"{label} timeout après {deadline:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def detect_chrome_version_main() -> Optional[int]:
    """Version majeure Chrome installée (ex. 148) — évite mismatch ChromeDriver."""
    global _CHROME_VER_CACHE
    if _CHROME_VER_CACHE is not None:
        return _CHROME_VER_CACHE
    ver: Optional[int] = None
    if shutil.which("reg"):
        try:
            out = subprocess.run(
                [
                    "reg",
                    "query",
                    r"HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon",
                    "/v",
                    "version",
                ],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            m = re.search(r"(\d+)\.\d+\.\d+", out.stdout or "")
            if m:
                ver = int(m.group(1))
        except Exception as exc:
            logger.debug("detect_chrome reg: %s", exc)
    if ver is None:
        for cmd in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ):
            try:
                out = subprocess.run(
                    [cmd, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=6,
                    check=False,
                )
                m = re.search(r"(\d+)\.", (out.stdout or out.stderr or ""))
                if m:
                    ver = int(m.group(1))
                    break
            except Exception:
                continue
    if ver is not None:
        _CHROME_VER_CACHE = ver
        logger.info("Chrome détecté : v%s (ChromeDriver aligné)", ver)
    return ver

_UC_DEL_PATCHED = False


def patch_uc_chrome_del() -> None:
    """Patch global Chrome.__del__ pour ignorer OSError au shutdown."""
    global _UC_DEL_PATCHED
    if _UC_DEL_PATCHED:
        return
    try:
        import undetected_chromedriver as uc  # type: ignore

        _orig = uc.Chrome.__del__

        def _safe_del(self: Any) -> None:
            try:
                _orig(self)
            except OSError:
                pass
            except Exception:
                pass

        uc.Chrome.__del__ = _safe_del  # type: ignore[method-assign]
        _UC_DEL_PATCHED = True
    except Exception as exc:
        logger.debug("patch_uc_chrome_del: %s", exc)


def safe_quit_driver(driver: Any) -> None:
    """Ferme Chrome sans laisser de handle zombie pour le GC."""
    if driver is None:
        return
    try:
        driver.quit()
    except OSError:
        pass
    except Exception as exc:
        logger.debug("safe_quit_driver: %s", exc)
    for attr in ("service", "reactor", "browser", "tab"):
        try:
            setattr(driver, attr, None)
        except Exception:
            pass
    try:
        driver.__del__ = lambda: None  # type: ignore[method-assign, assignment]
    except Exception:
        pass


def dismiss_cookie_banners(driver: Any, *, pause_s: float = 0.8) -> bool:
    """Tente d'accepter les bannières cookies courantes (FR/EN)."""
    import time

    try:
        from selenium.webdriver.common.by import By  # type: ignore
    except Exception:
        return False
    clicked = False
    for xpath in (
        "//button[@id='onetrust-accept-btn-handler']",
        "//button[contains(@id,'accept') and contains(@class,'accept')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'accepter et fermer')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'accepter')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'accept all')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'tout accepter')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'agree')]",
        "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'continuer sans accepter')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'accepter & fermer')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'i accept')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'i refuse')]",
        "//button[contains(@id,'sd-cmp') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'accept')]",
    ):
        try:
            for btn in driver.find_elements(By.XPATH, xpath):
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    time.sleep(pause_s)
                    break
            if clicked:
                break
        except Exception:
            continue
    return clicked


def dismiss_cashexpress_modals(driver: Any, *, pause_s: float = 0.8) -> bool:
    """Cookies Sirdata + modale pays/magasin Cash Express."""
    import time

    try:
        from selenium.webdriver.common.by import By  # type: ignore
    except Exception:
        return False
    acted = dismiss_cookie_banners(driver, pause_s=pause_s)
    for xpath in (
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'accept all')]",
        "//button[contains(@id,'sd-cmp')]",
    ):
        try:
            for btn in driver.find_elements(By.XPATH, xpath):
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    acted = True
                    time.sleep(pause_s)
                    break
            if acted:
                break
        except Exception:
            continue
    for xpath in (
        "//a[contains(translate(., 'AFFICHER','afficher'),'produits autour')]",
        "//img[contains(@src,'map-france') or contains(@src,'flag-france')]/ancestor::a[1]",
        "//img[contains(@src,'map-france') or contains(@src,'flag-france')]",
    ):
        try:
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    acted = True
                    time.sleep(pause_s)
                    break
            if acted:
                break
        except Exception:
            continue
    return acted


def wait_until_html_ready(
    driver: Any,
    *,
    is_blocked: Any,
    timeout_s: float = 120.0,
    poll_s: float = 2.0,
    on_wait: Optional[Any] = None,
) -> bool:
    """Attend que la page ne soit plus bloquée (captcha / challenge)."""
    import time

    deadline = time.time() + max(5.0, timeout_s)
    notified = False
    while time.time() < deadline:
        try:
            html = driver.page_source or ""
            status_ok = not bool(is_blocked(html))
            if status_ok:
                return True
        except Exception:
            pass
        if on_wait and not notified:
            try:
                on_wait()
            except Exception:
                pass
            notified = True
        time.sleep(poll_s)
    return False


def wait_for_window_handles(driver: Any, timeout_s: float = 14.0) -> list[str]:
    import time

    deadline = time.time() + max(2.0, timeout_s)
    last_exc: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            handles = list(driver.window_handles)
            if handles:
                return handles
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(0.25)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Aucune fenêtre Chrome après {timeout_s}s")


def ensure_active_window(driver: Any) -> None:
    from selenium.common.exceptions import NoSuchWindowException  # type: ignore

    handles = wait_for_window_handles(driver)
    try:
        driver.switch_to.window(handles[0])
    except NoSuchWindowException:
        driver.switch_to.window(wait_for_window_handles(driver, timeout_s=3.0)[0])


def driver_get_safe(driver: Any, url: str, *, retries: int = 2) -> None:
    from selenium.common.exceptions import NoSuchWindowException  # type: ignore

    last_exc: Optional[BaseException] = None
    for attempt in range(max(1, retries)):
        try:
            ensure_active_window(driver)
            driver.get(url)
            return
        except NoSuchWindowException as exc:
            last_exc = exc
            if attempt + 1 >= retries:
                raise
    if last_exc:
        raise last_exc


def launch_uc_chrome(
    *,
    headless: bool = True,
    version_main: Optional[int] = None,
    extra_args: Optional[list[str]] = None,
    user_data_dir: Optional[str] = None,
    keep_alive: bool = False,
    **kwargs: Any,
) -> Any:
    """
    Lance uc.Chrome avec patch __del__ et options communes VF.
    """
    patch_uc_chrome_del()
    import undetected_chromedriver as uc  # type: ignore

    opts = uc.ChromeOptions()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    if headless:
        opts.add_argument("--headless=new")
    if user_data_dir:
        opts.add_argument(f"--user-data-dir={user_data_dir}")
    for arg in extra_args or []:
        opts.add_argument(arg)
    kw: Dict[str, Any] = {
        "options": opts,
        "headless": headless,
        "use_subprocess": True,
        **kwargs,
    }
    if user_data_dir:
        kw["user_data_dir"] = user_data_dir
    if keep_alive:
        kw["keep_alive"] = True
    vm = version_main if version_main is not None else detect_chrome_version_main()
    if vm:
        kw["version_main"] = int(vm)
    with _LAUNCH_LOCK:
        for attempt in range(2):
            try:
                driver = uc.Chrome(**kw)
                try:
                    driver.execute_cdp_cmd(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {
                            "source": (
                                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                                "window.chrome = window.chrome || { runtime: {} };"
                            )
                        },
                    )
                except Exception as exc:
                    logger.debug("CDP stealth patch: %s", exc)
                try:
                    ensure_active_window(driver)
                except Exception as exc:
                    logger.debug("ensure_active_window post-launch: %s", exc)
                return driver
            except Exception as exc:
                err = str(exc).lower()
                if vm and attempt == 0 and (
                    "only supports chrome version" in err or "session not created" in err
                ):
                    logger.warning("Chrome v%s échoué (%s) — repli auto version", vm, exc)
                    kw.pop("version_main", None)
                    vm = None
                    continue
                if attempt == 0 and "no such window" in err:
                    continue
                raise
        raise RuntimeError("launch_uc_chrome: échec après 2 tentatives")


# Patch dès l'import pour couvrir les drivers créés ailleurs.
patch_uc_chrome_del()


# browser_policy


def adapter_headless(platform: str, *, run_headless: bool = True) -> bool:
    """True = headless. Headless forcé sauf opt-in explicite headed."""
    if not run_headless:
        return False
    plat = (platform or "").lower().replace("cash-express", "cashexpress").replace("lbc", "leboncoin")
    cookie_files = {
        "ebay": "ebay.json",
        "leboncoin": "lbc.json",
        "rakuten": "rakuten.json",
        "cashexpress": "cashexpress.json",
    }
    headed_env = {
        "ebay": "VF_EBAY_HEADED",
        "leboncoin": "VF_LBC_HEADED",
        "rakuten": "VF_RAKUTEN_HEADED",
        "cashexpress": "VF_CEX_HEADED",
    }
    auto_env = {
        "ebay": "VF_EBAY_AUTO_HEADED",
        "leboncoin": "VF_LBC_AUTO_HEADED",
        "rakuten": "VF_RAKUTEN_AUTO_HEADED",
        "cashexpress": "VF_CEX_AUTO_HEADED",
    }
    if plat in headed_env and os.environ.get(headed_env[plat], "").strip().lower() in ("1", "true", "yes"):
        return False
    # Bench default: fragile platforms stay headless unless *_AUTO_HEADED is explicitly enabled.
    if plat in cookie_files:
        auto_default = "0" if plat in ("ebay", "leboncoin", "rakuten", "cashexpress") else "1"
        auto = os.environ.get(auto_env.get(plat, ""), auto_default).strip().lower()
        if auto in ("0", "false", "no"):
            return True
        from pathlib import Path

        cp = Path("brouillon") / "data" / "vf_cookies" / cookie_files[plat]
        if not cp.is_file():
            return False
    return True


def hybrid_mode_label() -> str:
    return "headless (aucune fenêtre Chrome)"


# --- core.py ---

import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# Re-exports utilitaires (consolidation LLM-friendly)
from .engine import (  # noqa: F401
    AUTH_REFRESH_STATUS,
    CircuitOpenError,
    RETRYABLE_HTTP_STATUS,
    backoff_seconds,
    get_circuit,
)
from .engine import parse_storage_gb, normalize_specs_from_text  # noqa: F401
from .ai import partition_for_cascade, route_ai_decision  # noqa: F401
from .engine import resolve_category_tier, load_category_schema  # noqa: F401

Listener = Callable[[str, Dict[str, Any]], None]


@dataclass
class PlatformStyle:
    key: str
    label: str
    color: str
    text_color: str = "#ffedd7"


PLATFORM_STYLES: Dict[str, PlatformStyle] = {
    "vinted": PlatformStyle("vinted", "Vinted", "#09B1BA"),
    "ebay": PlatformStyle("ebay", "eBay", "#E53238"),
    "leboncoin": PlatformStyle("leboncoin", "LeBonCoin", "#FF6E14"),
    "lbc": PlatformStyle("leboncoin", "LeBonCoin", "#FF6E14"),
    "wallapop": PlatformStyle("wallapop", "Wallapop", "#13C1AC"),
    "rakuten": PlatformStyle("rakuten", "Rakuten", "#BF0000"),
    "cashexpress": PlatformStyle("cashexpress", "Cash Express", "#F5C518", "#1a120c"),
    "oracle": PlatformStyle("oracle", "Oracle", "#8fbc8f"),
    "backmarket": PlatformStyle("backmarket", "BackMarket", "#00C2A8"),
}


def platform_style(platform: str) -> PlatformStyle:
    key = (platform or "vinted").lower().replace("cash-express", "cashexpress")
    if key == "lbc":
        key = "leboncoin"
    return PLATFORM_STYLES.get(key, PlatformStyle(key, key.title(), "#6c5f51"))


class EventBus:
    """Bus léger thread-safe — pas de DB, callbacks synchrones ou UI via after()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: List[Listener] = []

    def on(self, listener: Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def off(self, listener: Listener) -> None:
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def emit(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        data = dict(payload or {})
        data.setdefault("ts", time.time())
        data.setdefault("event", event)
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event, data)
            except Exception:
                pass


# Singleton process-wide
BUS = EventBus()


def format_bus_log(event: str, payload: Dict[str, Any]) -> Optional[str]:
    """Convertit un événement bus en ligne lisible pour l'UI / journal."""
    plat = str(payload.get("platform") or payload.get("source") or "").lower()
    style = platform_style(plat) if plat else None
    prefix = f"[{style.label}]" if style and style.key != "vinted" else (
        f"[{platform_style(plat).label}]" if plat else ""
    )
    oracle = payload.get("oracle")
    if oracle:
        prefix = f"[Oracle:{oracle}]"

    if event == "SOURCE_START":
        q = payload.get("query") or payload.get("search_terms") or ""
        return f"{prefix} Démarrage — {q!r}".strip()
    if event == "SOURCE_DONE":
        n = int(payload.get("count") or 0)
        ms = int(payload.get("time_ms") or 0)
        extra = f" ({ms} ms)" if ms else ""
        return f"{prefix} {n} annonces{extra}".strip()
    if event == "SOURCE_ERROR":
        err = payload.get("error") or payload.get("message") or "erreur"
        return f"{prefix} {err}".strip()
    if event == "SOURCE_RETRY":
        return f"{prefix} {payload.get('message', 'retry…')}".strip()
    if event == "ORACLE_PRICE":
        price = payload.get("price")
        label = payload.get("label") or payload.get("model") or ""
        if price is not None:
            return f"[Oracle:{payload.get('oracle', 'prix')}] {label} — {price}€".strip()
    if event == "ANALYTICS_READY":
        return payload.get("summary") or "✓ Synthèse multi-plateformes prête"
    return payload.get("message")


def _row_price(row: Dict[str, Any]) -> Optional[float]:
    for k in ("landed_cost_eur", "total_price", "price", "listing_price"):
        try:
            v = float(row.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def compute_cross_platform_analytics(
    rows: List[Dict[str, Any]],
    *,
    main_only: bool = True,
    scrape_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    requested_platforms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Matrice comparative : volume, médiane et minimum par plateforme.
    Compare des lignes déjà filtrées (specs / scoring appliqués).
    """
    pool = rows
    if main_only:
        pool = [r for r in rows if not r.get("is_accessory")]
    # Exclure prix aberrants (accessoires/jeux passés le tri) pour médian fiable
    prices_all = [_row_price(r) for r in pool]
    prices_all = [p for p in prices_all if p is not None and p > 0]
    if len(prices_all) >= 8:
        med_all = float(statistics.median(prices_all))
        floor = max(25.0, med_all * 0.22)
        pool = [r for r in pool if (_row_price(r) or 0) >= floor]
    if not pool:
        return {
            "platforms": {},
            "total": 0,
            "summary": "Aucune offre viable pour comparer les plateformes.",
            "cheapest_platform": "",
            "cheapest_median": 0.0,
            "reference_platform": "",
            "reference_median": 0.0,
            "median_delta_pct": 0.0,
        }

    by_plat: Dict[str, List[float]] = {}
    counts: Dict[str, int] = {}
    for r in pool:
        plat = str(r.get("platform") or "vinted").lower()
        if plat == "lbc":
            plat = "leboncoin"
        p = _row_price(r)
        if p is None:
            continue
        by_plat.setdefault(plat, []).append(p)
        counts[plat] = counts.get(plat, 0) + 1

    total = sum(counts.values())
    stats: Dict[str, Dict[str, Any]] = {}
    for plat, prices in by_plat.items():
        if not prices:
            continue
        med = float(statistics.median(prices))
        stats[plat] = {
            "count": counts.get(plat, len(prices)),
            "pct": round(100.0 * counts.get(plat, 0) / max(1, total), 1),
            "median_eur": round(med, 2),
            "min_eur": round(min(prices), 2),
            "max_eur": round(max(prices), 2),
            "label": platform_style(plat).label,
        }

    scrape_stats = scrape_stats or {}
    req_plats = [p.lower() for p in (requested_platforms or [])]
    for plat in req_plats:
        if plat == "lbc":
            plat = "leboncoin"
        if plat in stats:
            st = scrape_stats.get(plat) or {}
            stats[plat]["scraped_raw"] = int(st.get("raw") or 0)
            continue
        st = scrape_stats.get(plat) or {}
        raw = int(st.get("raw") or 0)
        err = int(st.get("errors") or 0)
        if raw > 0 or err > 0:
            stats[plat] = {
                "count": 0,
                "pct": 0.0,
                "median_eur": 0.0,
                "min_eur": 0.0,
                "max_eur": 0.0,
                "label": platform_style(plat).label,
                "scraped_raw": raw,
                "status": "filtered_out" if raw > 0 else "error",
                "last_error": str(st.get("last_error") or "")[:80],
            }

    if not stats:
        return {
            "platforms": {},
            "total": 0,
            "summary": "Prix indisponibles pour la comparaison.",
            "cheapest_platform": "",
            "cheapest_median": 0.0,
            "reference_platform": "",
            "reference_median": 0.0,
            "median_delta_pct": 0.0,
            "scrape_stats": scrape_stats,
        }

    ranked = [x for x in sorted(stats.items(), key=lambda x: x[1].get("median_eur") or 1e9) if (x[1].get("median_eur") or 0) > 0]
    if not ranked:
        missing = [platform_style(p).label for p in req_plats if (scrape_stats.get(p) or {}).get("errors")]
        summary = "Aucune offre retenue avec prix exploitable."
        if missing:
            summary += f" Échec scrape : {', '.join(missing)}."
        return {
            "platforms": stats,
            "total": total,
            "summary": summary,
            "cheapest_platform": "",
            "cheapest_median": 0.0,
            "reference_platform": "",
            "reference_median": 0.0,
            "median_delta_pct": 0.0,
            "scrape_stats": scrape_stats,
        }

    cheapest_plat, cheapest = ranked[0]
    reference_plat, reference = ranked[-1] if len(ranked) > 1 else ranked[0]
    delta_pct = 0.0
    if reference["median_eur"] > 0 and len(ranked) > 1:
        delta_pct = round(
            100.0 * (reference["median_eur"] - cheapest["median_eur"]) / reference["median_eur"],
            1,
        )

    parts = [
        f"{cheapest['label']} médiane {cheapest['median_eur']:.0f}€ ({cheapest['pct']:.0f}% du volume)"
    ]
    if len(ranked) > 1 and delta_pct > 0.5:
        parts.append(
            f"— {delta_pct:+.0f}% vs {reference['label']} ({reference['median_eur']:.0f}€)"
        )
    summary = " ".join(parts)

    if len(ranked) > 1 and delta_pct >= 3:
        summary = (
            f"{cheapest['label']} est la moins chère pour cette recherche "
            f"(médiane {cheapest['median_eur']:.0f}€, soit −{delta_pct:.0f}% vs {reference['label']})"
        )

    return {
        "platforms": stats,
        "total": total,
        "summary": summary,
        "cheapest_platform": cheapest_plat,
        "cheapest_median": cheapest["median_eur"],
        "reference_platform": reference_plat,
        "reference_median": reference["median_eur"],
        "median_delta_pct": delta_pct,
        "scrape_stats": scrape_stats,
    }


def apply_scam_shield(row: Dict[str, Any]) -> None:
    """
    Scam Shield — décote extrême + vendeur neuf = deal bloqué (in-place).
    Appelé depuis apply_deal_caps pour limiter les tokens IA inutiles.
    """
    try:
        discount = float(
            row.get("market_discount_pct")
            or row.get("model_vs_market_pct")
            or row.get("discount_vs_new_pct")
            or 0
        )
    except (TypeError, ValueError):
        discount = 0.0
    reviews = int(row.get("seller_feedback_count") or row.get("seller_reviews_count") or 0)
    flags = str(row.get("risk_flags") or "")
    if discount > 45.0 and reviews == 0:
        if "scam_suspected" not in flags:
            row["risk_flags"] = (flags + " scam_suspected").strip()
        row["final_score"] = 0.0
        row["deal_blocked"] = True
        row["quality_tier"] = "suspicious"
    elif discount > 30.0 and reviews < 5:
        try:
            sc = float(row.get("final_score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        row["final_score"] = round(max(0.0, sc - 25.0), 2)
        if "low_trust_seller" not in flags:
            row["risk_flags"] = (flags + " low_trust_seller").strip()


def emit_oracle_price(
    oracle: str,
    *,
    price: float,
    label: str = "",
    platform: str = "oracle",
) -> None:
    BUS.emit(
        "ORACLE_PRICE",
        {"oracle": oracle, "platform": platform, "price": price, "label": label},
    )


# --- monitor.py ---

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

_LOG = logging.getLogger("vf.monitor")
_DATA = Path(__file__).resolve().parent.parent / "brouillon" / "data"
_SESSION_LOG = _DATA / "vf_session.log"
_METRICS_PATH = _DATA / "vf_run_metrics.jsonl"

_lock = Lock()
_subscribers: List[Callable[[Dict[str, Any]], None]] = []
_run_id: str = ""
_metrics: Dict[str, Any] = {}
_events: List[Dict[str, Any]] = []
_MAX_EVENTS = 8000


def _ts() -> str:
    return time.strftime("%H:%M:%S")


@dataclass
class RunMonitor:
    run_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    phases: Dict[str, float] = field(default_factory=dict)
    counters: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    platform_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = int(self.counters.get(key) or 0) + n

    def phase_done(self, name: str, sec: float) -> None:
        self.phases[name] = round(float(sec), 2)

    def set_platform(self, plat: str, **stats: Any) -> None:
        self.platform_stats[str(plat).lower()] = dict(stats)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "elapsed_sec": round(time.monotonic() - self.started_at, 1),
            "phases": dict(self.phases),
            "counters": dict(self.counters),
            "errors": list(self.errors[-40:]),
            "platforms": dict(self.platform_stats),
        }


_current = RunMonitor()


def subscribe(cb: Callable[[Dict[str, Any]], None]) -> None:
    if cb not in _subscribers:
        _subscribers.append(cb)


def unsubscribe(cb: Callable[[Dict[str, Any]], None]) -> None:
    try:
        _subscribers.remove(cb)
    except ValueError:
        pass


def _emit(payload: Dict[str, Any]) -> None:
    with _lock:
        _events.append(payload)
        if len(_events) > _MAX_EVENTS:
            del _events[: len(_events) - _MAX_EVENTS]
    for cb in list(_subscribers):
        try:
            cb(payload)
        except Exception as exc:
            _LOG.debug("monitor subscriber: %s", exc)


def setup_logging(*, verbose: bool = False) -> None:
    """Configure logging racine VF (console + fichier session)."""
    _DATA.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("vf")
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(level)
        root.addHandler(sh)
        try:
            fh = logging.FileHandler(_SESSION_LOG, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            root.addHandler(fh)
        except OSError as exc:
            _LOG.debug("session log indisponible: %s", exc)
    root.setLevel(level)


def start_run(run_id: str = "") -> RunMonitor:
    global _current, _run_id
    rid = (run_id or time.strftime("run_%Y%m%d_%H%M%S")).strip()
    _run_id = rid
    _current = RunMonitor(run_id=rid)
    with _lock:
        _metrics.clear()
    event("run_start", f"Run {rid}", level="info")
    return _current


def current_run() -> RunMonitor:
    return _current


def event(
    phase: str,
    message: str,
    *,
    level: str = "info",
    tag: str = "",
    **extra: Any,
) -> None:
    """Log structuré + notification abonnés."""
    payload: Dict[str, Any] = {
        "ts": _ts(),
        "t_mono": round(time.monotonic(), 3),
        "run_id": _run_id,
        "phase": phase,
        "level": level,
        "tag": tag or level,
        "message": message,
        **extra,
    }
    _emit(payload)
    line = f"[{payload['ts']}] {message}"
    if level == "error":
        _current.errors.append(message[:240])
        _LOG.error("%s — %s", phase, message)
    elif level == "warn":
        _LOG.warning("%s — %s", phase, message)
    else:
        _LOG.info("%s — %s", phase, message)
    try:
        _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def metric(name: str, value: Any) -> None:
    with _lock:
        _metrics[name] = value
    event("metric", f"{name}={value}", metric=name, value=value)


def get_metrics() -> Dict[str, Any]:
    with _lock:
        return dict(_metrics)


def recent_events(limit: int = 400) -> List[Dict[str, Any]]:
    with _lock:
        return list(_events[-max(1, limit) :])


def finish_run(**summary: Any) -> Dict[str, Any]:
    snap = _current.to_dict()
    snap.update(summary)
    event("run_end", "Analyse terminée", level="info", **snap)
    try:
        out = _DATA / "vf_last_run.json"
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return snap


# --- http_resilience.py ---

import logging
import os
import random
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Dict, Optional, TypeVar

import requests

logger = logging.getLogger("vf.http_resilience")

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitOpenError(RuntimeError):
    """Plateforme temporairement coupée après trop d'échecs."""

    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"Circuit « {name} » ouvert — réessai dans {retry_after:.0f}s")


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 300.0
    half_open_max: int = 2
    _failures: int = 0
    _opened_at: float = 0.0
    _state: str = "closed"  # closed | open | half_open
    _half_open_trials: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def before_call(self) -> None:
        with self._lock:
            if self._state == "open":
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.recovery_timeout:
                    self._state = "half_open"
                    self._half_open_trials = 0
                    logger.info("Circuit %s → half-open (probe)", self.name)
                else:
                    raise CircuitOpenError(self.name, self.recovery_timeout - elapsed)
            if self._state == "half_open" and self._half_open_trials >= self.half_open_max:
                raise CircuitOpenError(self.name, self.recovery_timeout)

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state != "closed":
                logger.info("Circuit %s → fermé (succès)", self.name)
            self._state = "closed"
            self._half_open_trials = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._half_open_trials += 1
            self._failures += 1
            if self._failures >= self.failure_threshold and self._state != "open":
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit %s OUVERT après %d échecs (pause %.0fs)",
                    self.name,
                    self._failures,
                    self.recovery_timeout,
                )

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._state == "open"


_CIRCUITS: Dict[str, CircuitBreaker] = {}
_CIRCUIT_LOCK = Lock()


def get_circuit(name: str, **kwargs: Any) -> CircuitBreaker:
    key = (name or "default").strip().lower()
    with _CIRCUIT_LOCK:
        cb = _CIRCUITS.get(key)
        if cb is None:
            failure_threshold = int(
                os.environ.get(f"VF_CB_FAIL_{key.upper()}", os.environ.get("VF_CB_FAIL", "5")) or "5"
            )
            recovery_timeout = float(
                os.environ.get(f"VF_CB_COOLDOWN_{key.upper()}", os.environ.get("VF_CB_COOLDOWN", "300")) or "300"
            )
            cb = CircuitBreaker(
                key,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                **kwargs,
            )
            _CIRCUITS[key] = cb
        return cb


def circuit_status() -> Dict[str, Dict[str, Any]]:
    with _CIRCUIT_LOCK:
        return {
            name: {
                "state": cb._state,
                "failures": cb._failures,
                "is_open": cb.is_open,
            }
            for name, cb in _CIRCUITS.items()
        }


# ---------------------------------------------------------------------------
# Backoff / retry helpers (tenacity)
# ---------------------------------------------------------------------------

RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
AUTH_REFRESH_STATUS = frozenset({401, 403})


def is_retryable_http_status(status: Optional[int]) -> bool:
    if status is None:
        return False
    return status in RETRYABLE_HTTP_STATUS or status in AUTH_REFRESH_STATUS


def backoff_seconds(attempt: int, *, base: float = 1.0, maximum: float = 60.0) -> float:
    """Backoff exponentiel avec jitter (tentative 1-indexée)."""
    exp = min(maximum, base * (2 ** max(0, attempt - 1)))
    return exp + random.uniform(0.0, min(2.0, exp * 0.15))


def _import_tenacity():
    try:
        from tenacity import (  # type: ignore
            Retrying,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential,
        )

        return Retrying, retry_if_exception, stop_after_attempt, wait_exponential
    except ImportError:
        return None, None, None, None


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
    retry_predicate: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[[BaseException, int], None]] = None,
) -> T:
    """
    Exécute fn avec retries. Utilise tenacity si installé, sinon boucle manuelle.
    """
    pred = retry_predicate or (lambda _e: True)
    Retrying, retry_if_exception, stop_after_attempt, wait_exponential = _import_tenacity()

    if Retrying is not None and retry_if_exception is not None:
        for attempt in Retrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception(pred),
            reraise=True,
        ):
            with attempt:
                return fn()
        raise RuntimeError("retry_call: unreachable")

    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if attempt >= attempts or not pred(exc):
                raise
            wait = backoff_seconds(attempt, base=min_wait, maximum=max_wait)
            if on_retry:
                on_retry(exc, attempt)
            else:
                logger.warning("Retry %d/%d après %s (pause %.1fs)", attempt, attempts, exc, wait)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("retry_call: empty")


def guarded_platform_call(
    platform: str,
    fn: Callable[[], T],
    *,
    record_failure_on: Optional[Callable[[BaseException], bool]] = None,
) -> T:
    """Circuit breaker + exécution."""
    circuit = get_circuit(platform)
    circuit.before_call()
    try:
        out = fn()
        circuit.record_success()
        return out
    except CircuitOpenError:
        raise
    except Exception as exc:
        should_fail = True
        if record_failure_on is not None:
            try:
                should_fail = bool(record_failure_on(exc))
            except Exception:
                should_fail = True
        if should_fail:
            circuit.record_failure()
        raise


def http_request_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    status = getattr(exc, "status", None)
    if status is not None:
        return is_retryable_http_status(int(status))
    return False


# --- async_fetch.py ---

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("vf.async_fetch")

_DEFAULT_WORKERS = int(os.environ.get("VF_DEEP_FETCH_WORKERS", "3") or "3")
_GLOBAL_SEM = Semaphore(max(1, min(12, _DEFAULT_WORKERS)))


def fetch_details_parallel(
    fetch_fn: Callable[[str], Optional[Dict[str, Any]]],
    item_ids: List[str],
    *,
    max_workers: Optional[int] = None,
    max_consecutive_fail: int = 8,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """
    Récupère les détails en parallèle (threads + sémaphore).

    Retourne (résultats par id, échantillon d'échecs).
    """
    workers = max(1, min(12, max_workers or _DEFAULT_WORKERS))
    ids = [str(i) for i in item_ids if str(i).strip()]
    if not ids:
        return {}, []

    results: Dict[str, Dict[str, Any]] = {}
    fail_samples: List[str] = []
    consecutive_fail = 0
    done = 0
    stop_early = False

    def _one(sid: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        with _GLOBAL_SEM:
            try:
                return sid, fetch_fn(sid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Deep fetch %s: %s", sid, exc)
                return sid, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, sid): sid for sid in ids}
        for fut in as_completed(futures):
            if stop_early:
                break
            sid, obj = fut.result()
            done += 1
            if obj:
                results[sid] = obj
                consecutive_fail = 0
            else:
                consecutive_fail += 1
                if len(fail_samples) < 3:
                    fail_samples.append(f"{sid}:404")
                if consecutive_fail >= max_consecutive_fail:
                    stop_early = True
                    logger.warning(
                        "Deep fetch parallèle interrompu après %d échecs consécutifs",
                        consecutive_fail,
                    )
            if on_progress:
                try:
                    on_progress(done, len(ids))
                except Exception:
                    pass

    return results, fail_samples


# --- oracle.py ---

import json
import logging
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger("vf.oracle")


# --- types.py ---

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PriceReference:
    query: str = ""
    refurb_fair_eur: Optional[float] = None
    refurb_good_eur: Optional[float] = None
    refurb_excellent_eur: Optional[float] = None
    new_low_eur: Optional[float] = None
    new_median_eur: Optional[float] = None
    stockx_lowest_ask_eur: Optional[float] = None
    stockx_title: str = ""
    stockx_url: str = ""
    idealo_low_eur: Optional[float] = None
    idealo_url: str = ""
    sources: List[str] = field(default_factory=list)
    backmarket_url: str = ""
    denicheur_url: str = ""
    degraded: bool = False

    @property
    def market_quote_eur(self) -> Optional[float]:
        """Meilleure référence « cote marché » (StockX prioritaire pour collectibles)."""
        if self.stockx_lowest_ask_eur and self.stockx_lowest_ask_eur > 0:
            return self.stockx_lowest_ask_eur
        return self.refurb_min_eur

    @property
    def retail_low_eur(self) -> Optional[float]:
        """Prix neuf le plus bas (min Dénicheur / Idealo)."""
        vals = [v for v in (self.new_low_eur, self.idealo_low_eur) if v and v > 0]
        return min(vals) if vals else None

    @property
    def refurb_min_eur(self) -> Optional[float]:
        vals = [v for v in (self.refurb_excellent_eur, self.refurb_good_eur, self.refurb_fair_eur) if v]
        return min(vals) if vals else None

# --- cache.py ---

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


_CACHE_PATH = Path("brouillon") / "data" / "oracle_cache.json"
_TTL_SEC = 7 * 24 * 3600


class OracleCache:
    def __init__(self, path: Optional[Path] = None, ttl_sec: float = _TTL_SEC) -> None:
        self.path = path or _CACHE_PATH
        self.ttl_sec = ttl_sec
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception:
            pass

    def get(self, key: str) -> Optional[PriceReference]:
        ent = self._data.get(key)
        if not ent:
            return None
        try:
            if time.time() - float(ent.get("ts") or 0) > self.ttl_sec:
                return None
        except (TypeError, ValueError):
            return None
        ref = PriceReference(query=key)
        ref.refurb_fair_eur = ent.get("refurb_fair_eur")
        ref.refurb_good_eur = ent.get("refurb_good_eur")
        ref.refurb_excellent_eur = ent.get("refurb_excellent_eur")
        ref.new_low_eur = ent.get("new_low_eur")
        ref.new_median_eur = ent.get("new_median_eur")
        ref.stockx_lowest_ask_eur = ent.get("stockx_lowest_ask_eur")
        ref.stockx_title = str(ent.get("stockx_title") or "")
        ref.stockx_url = str(ent.get("stockx_url") or "")
        ref.idealo_low_eur = ent.get("idealo_low_eur")
        ref.idealo_url = str(ent.get("idealo_url") or "")
        ref.sources = list(ent.get("sources") or [])
        ref.denicheur_url = str(ent.get("denicheur_url") or "")
        return ref

    def set(self, key: str, ref: PriceReference) -> None:
        self._data[key] = {
            "ts": time.time(),
            "refurb_fair_eur": ref.refurb_fair_eur,
            "refurb_good_eur": ref.refurb_good_eur,
            "refurb_excellent_eur": ref.refurb_excellent_eur,
            "new_low_eur": ref.new_low_eur,
            "new_median_eur": ref.new_median_eur,
            "stockx_lowest_ask_eur": ref.stockx_lowest_ask_eur,
            "stockx_title": ref.stockx_title,
            "stockx_url": ref.stockx_url,
            "idealo_low_eur": ref.idealo_low_eur,
            "idealo_url": ref.idealo_url,
            "sources": ref.sources,
            "denicheur_url": ref.denicheur_url,
        }
        if len(self._data) > 500:
            # purge oldest
            items = sorted(self._data.items(), key=lambda x: float(x[1].get("ts") or 0))
            self._data = dict(items[-400:])
        self._save()

# --- browser_pool.py ---

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("vf.oracle.browser")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class OracleBrowserPool:
    """Une seule instance UC réutilisée entre Dénicheur / StockX / Idealo."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._driver: Any = None

    @property
    def active(self) -> bool:
        return self._driver is not None

    def get_driver(self) -> Any:
        import os

        if os.environ.get("VF_ORACLE_BROWSER", "1").strip() in ("0", "false", "no"):
            raise RuntimeError("Oracle navigateur désactivé (VF_ORACLE_BROWSER=0)")
        if self._driver is not None:
            return self._driver
        try:
            from .engine import detect_chrome_version_main, launch_uc_chrome
        except Exception as exc:
            raise RuntimeError(
                "Scraper/browser dependency missing: undetected-chromedriver / selenium.\n"
                "Install with:\n"
                '  python -m pip install ".[scrape]"\n'
                "or (full install):\n"
                '  python -m pip install ".[all]"\n'
            ) from exc
        ver = detect_chrome_version_main()
        logger.info("Oracle : démarrage navigateur partagé headless (Chrome %s)…", ver or "?")
        self._driver = launch_uc_chrome(
            headless=self._headless,
            version_main=ver,
            extra_args=[
                "--disable-blink-features=AutomationControlled",
                "--blink-settings=imagesEnabled=false",
                f"--user-agent={DEFAULT_UA}",
            ],
        )
        return self._driver

    def seed_cookies(self, domain_url: str, cookies: Dict[str, str]) -> None:
        if not cookies:
            return
        drv = self.get_driver()
        drv.get(domain_url)
        time.sleep(1.5)
        for name, val in cookies.items():
            try:
                drv.add_cookie({"name": name, "value": val})
            except Exception:
                pass

    def close(self) -> None:
        if self._driver is None:
            return
        from .engine import safe_quit_driver

        safe_quit_driver(self._driver)
        self._driver = None
        logger.info("Oracle : navigateur partagé fermé")

# --- backmarket.py ---

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger("vf.oracle.backmarket")

BM_MULTI_URL = "https://search.backmarket.fr/1/indexes/*/queries"
BM_MODEL_URL = "https://search.backmarket.fr/1/indexes/prod_index_backbox_model_fr-fr/query"
INDEX_MODEL = "prod_index_backbox_model_fr-fr"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _parse_cookie_header(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _load_cookies_from_env() -> Dict[str, str]:
    path = os.environ.get("VF_BM_COOKIE_FILE", "").strip()
    if path and Path(path).is_file():
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    else:
        raw = os.environ.get("VF_BM_COOKIES", "").strip()
    return _parse_cookie_header(raw)


class BackMarketOracle:
    def __init__(self, *, headless: bool = True, timeout: float = 25.0) -> None:
        self._headless = headless
        self._timeout = timeout
        self._bootstrapped = False
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "bm-search-domain": "search",
                "content-type": "application/json",
                "origin": "https://www.backmarket.fr",
                "referer": "https://www.backmarket.fr/",
                "user-agent": DEFAULT_UA,
                "x-algolia-api-key": "dummy_key",
                "x-algolia-application-id": "dummy_app_id",
            }
        )
        cookies = _load_cookies_from_env()
        for k, v in cookies.items():
            self.session.cookies.set(k, v)

    def bootstrap_session(self) -> bool:
        if self._bootstrapped and self.session.cookies:
            return True
        logger.info("Back Market oracle : bootstrap Cloudflare…")
        try:
            import undetected_chromedriver as uc  # type: ignore
        except Exception as exc:
            logger.warning("undetected-chromedriver indisponible : %s", exc)
            return False
        driver = None
        try:
            opts = uc.ChromeOptions()
            if self._headless:
                opts.add_argument("--headless=new")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            driver = uc.Chrome(options=opts, headless=self._headless, use_subprocess=True)
            driver.get("https://www.backmarket.fr/fr-fr")
            time.sleep(4)
            for c in driver.get_cookies():
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            self._bootstrapped = True
            logger.info("Back Market oracle : %d cookies", len(driver.get_cookies()))
            return True
        except Exception as exc:
            logger.error("Back Market bootstrap échoué : %s", exc)
            return False
        finally:
            from .engine import safe_quit_driver

            safe_quit_driver(driver)

    def _post_multi(self, query: str, *, hits: int = 20) -> List[Dict[str, Any]]:
        payload = {
            "requests": [
                {
                    "indexName": INDEX_MODEL,
                    "params": f"query={quote(query)}&distinct=1&hitsPerPage={hits}",
                }
            ]
        }
        res = self.session.post(BM_MULTI_URL, data=json.dumps(payload), timeout=self._timeout)
        if res.status_code in (401, 403):
            return []
        if res.status_code != 200:
            logger.debug("Back Market %s : %s", res.status_code, (res.text or "")[:200])
            return []
        try:
            data = res.json()
            results = data.get("results") or []
            if not results:
                return []
            return results[0].get("hits") or []
        except (ValueError, KeyError, IndexError) as exc:
            logger.debug("Back Market parse : %s", exc)
            return []

    def _price_from_hit(self, hit: Dict[str, Any]) -> Optional[float]:
        for key in ("price", "price_with_shipping", "min_price"):
            val = hit.get(key)
            if val is not None:
                try:
                    p = float(val)
                    if p > 0:
                        return p
                except (TypeError, ValueError):
                    pass
        return None

    def _grade_bucket(self, hit: Dict[str, Any]) -> str:
        g = hit.get("backbox_grade") or hit.get("grade") or hit.get("condition")
        try:
            gi = int(g) if g is not None else 0
        except (TypeError, ValueError):
            gi = 0
        if gi >= 12:
            return "Excellent"
        if gi == 11:
            return "Good"
        if gi == 10:
            return "Fair"
        label = str(hit.get("backbox_grade_label") or hit.get("condition_label") or "").lower()
        if "excellent" in label or "premium" in label:
            return "Excellent"
        if "good" in label or "bon" in label:
            return "Good"
        if "fair" in label or "correct" in label or "stallone" in label:
            return "Fair"
        return "Good"

    def get_baseline_prices(self, model_name: str) -> Dict[str, Optional[float]]:
        q = (model_name or "").strip()
        if not q:
            return {"Fair": None, "Good": None, "Excellent": None}
        if not self._bootstrapped:
            self.bootstrap_session()
        hits = self._post_multi(q, hits=25)
        if not hits and not self._bootstrapped:
            self.bootstrap_session()
            hits = self._post_multi(q, hits=25)
        buckets: Dict[str, List[float]] = {"Fair": [], "Good": [], "Excellent": []}
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            price = self._price_from_hit(hit)
            if not price:
                continue
            bucket = self._grade_bucket(hit)
            buckets[bucket].append(price)
        return {k: (min(v) if v else None) for k, v in buckets.items()}

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

# --- denicheur.py ---

import logging
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vf.oracle.denicheur")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
PRICE_RE = re.compile(r"([\d\s]+,[0-9]{2})\s*€")


def _parse_cookie_header(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _load_dn_cookies() -> Dict[str, str]:
    path = os.environ.get("VF_DN_COOKIE_FILE", "").strip()
    if path and Path(path).is_file():
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    else:
        raw = os.environ.get("VF_DN_COOKIES", "").strip()
    return _parse_cookie_header(raw)


class DenicheurOracle:
    def __init__(
        self,
        *,
        browser: Optional["OracleBrowserPool"] = None,
        headless: bool = True,
    ) -> None:
        self.base_url = "https://ledenicheur.fr/search?query="
        self._browser = browser
        self._headless = headless
        self._owns_browser = browser is None
        self._local_pool: Optional["OracleBrowserPool"] = None
        self._cookies = _load_dn_cookies()
        self._cookies_seeded = False

    def _pool(self) -> "OracleBrowserPool":
        if self._browser is not None:
            return self._browser
        if self._local_pool is None:
            self._local_pool = OracleBrowserPool(headless=self._headless)
        return self._local_pool

    def _ensure_browser(self) -> bool:
        try:
            pool = self._pool()
            if self._cookies and not self._cookies_seeded:
                pool.seed_cookies("https://ledenicheur.fr/", self._cookies)
                self._cookies_seeded = True
            pool.get_driver()
            return True
        except Exception as exc:
            logger.error("Dénicheur navigateur : %s", exc)
            return False

    def _extract_prices(self, html: str) -> List[float]:
        prices: List[float] = []
        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all(attrs={"data-test": "ProductCard"})
            if not cards:
                cards = soup.find_all("a", href=lambda h: h and "/p/" in h)
            for card in cards[:12]:
                text = card.get_text(separator=" ", strip=True)
                for m in PRICE_RE.finditer(text):
                    try:
                        p = float(m.group(1).replace(" ", "").replace(",", "."))
                        if 1 < p < 50000:
                            prices.append(p)
                    except ValueError:
                        pass
            if prices:
                return prices
        except ImportError:
            pass
        for m in PRICE_RE.finditer(html):
            try:
                p = float(m.group(1).replace(" ", "").replace(",", "."))
                if 1 < p < 50000:
                    prices.append(p)
            except ValueError:
                pass
        return prices[:15]

    def search_new_prices(self, query: str) -> Optional[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return None
        if not self._ensure_browser():
            return None
        safe = urllib.parse.quote_plus(q)
        url = f"{self.base_url}{safe}"
        try:
            drv = self._pool().get_driver()
            drv.get(url)
            time.sleep(3.5)
            prices = self._extract_prices(drv.page_source or "")
            if not prices:
                return None
            prices = sorted(set(prices))
            top = prices[:5]
            return {
                "lowest_new_price": prices[0],
                "median_top_5": sum(top) / len(top),
                "market_reference_url": url,
            }
        except Exception as exc:
            logger.error("Dénicheur recherche : %s", exc)
            return None

    def close(self) -> None:
        if self._owns_browser and self._local_pool is not None:
            self._local_pool.close()
            self._local_pool = None

# --- idealo.py ---

import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vf.oracle.idealo")

SEARCH_URL = "https://www.idealo.fr/cat.html?q="
PRICE_RE = re.compile(r"([\d\s]+,[0-9]{2})\s*€")


class IdealoOracle:
    def __init__(self, *, browser: Optional["OracleBrowserPool"] = None, headless: bool = True) -> None:
        self._browser = browser
        self._headless = headless
        self._owns_browser = browser is None
        self._local_pool: Optional["OracleBrowserPool"] = None

    def _pool(self) -> "OracleBrowserPool":
        if self._browser is not None:
            return self._browser
        if self._local_pool is None:
            self._local_pool = OracleBrowserPool(headless=self._headless)
        return self._local_pool

    def search_new_prices(self, query: str) -> Optional[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return None
        safe = urllib.parse.quote_plus(q)
        url = f"{SEARCH_URL}{safe}"
        try:
            drv = self._pool().get_driver()
            drv.get(url)
            time.sleep(3.5)
            prices = _extract_prices(drv.page_source or "")
            if not prices:
                return None
            prices = sorted(set(prices))
            top = prices[:5]
            return {
                "lowest_new_price": prices[0],
                "median_top_5": sum(top) / len(top),
                "market_reference_url": url,
            }
        except Exception as exc:
            logger.error("Idealo oracle : %s", exc)
            return None

    def close(self) -> None:
        if self._owns_browser and self._local_pool is not None:
            self._local_pool.close()
            self._local_pool = None


def _extract_prices(html: str) -> List[float]:
    prices: List[float] = []
    if not html:
        return prices

    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        for item in soup.select(".sr-resultList__item, [data-testid='result-item'], article[data-offer]"):
            text = item.get_text(" ", strip=True)
            for m in PRICE_RE.finditer(text):
                try:
                    p = float(m.group(1).replace(" ", "").replace(",", "."))
                    if 1 < p < 50000:
                        prices.append(p)
                except ValueError:
                    pass
        if prices:
            return prices
    except ImportError:
        pass

    for m in PRICE_RE.finditer(html):
        try:
            p = float(m.group(1).replace(" ", "").replace(",", "."))
            if 1 < p < 50000:
                prices.append(p)
        except ValueError:
            pass
    return prices[:20]

# --- stockx.py ---

import logging
import re
import time
import urllib.parse
from typing import Any, Dict, Optional

logger = logging.getLogger("vf.oracle.stockx")

SEARCH_URL = "https://stockx.com/fr-fr/search?s="
PRICE_EUR_RE = re.compile(r"([\d\s]+(?:[,\.]\d{2})?)\s*€")
PRICE_USD_RE = re.compile(r"\$\s*([\d\s]+(?:[,\.]\d{2})?)")


class StockxOracle:
    def __init__(self, *, browser: Optional["OracleBrowserPool"] = None, headless: bool = True) -> None:
        self._browser = browser
        self._headless = headless
        self._owns_browser = browser is None
        self._local_pool: Optional["OracleBrowserPool"] = None

    def _pool(self) -> "OracleBrowserPool":
        if self._browser is not None:
            return self._browser
        if self._local_pool is None:
            self._local_pool = OracleBrowserPool(headless=self._headless)
        return self._local_pool

    def get_market_value(self, query: str) -> Optional[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return None
        safe = urllib.parse.quote_plus(q)
        url = f"{SEARCH_URL}{safe}"
        try:
            drv = self._pool().get_driver()
            drv.get(url)
            time.sleep(4.0)
            html = drv.page_source or ""
            result = _parse_stockx_html(html, current_url=drv.current_url)
            if result:
                return result
        except Exception as exc:
            logger.error("StockX oracle : %s", exc)
        return None

    def close(self) -> None:
        if self._owns_browser and self._local_pool is not None:
            self._local_pool.close()
            self._local_pool = None


def _parse_price_eur(text: str) -> Optional[float]:
    for m in PRICE_EUR_RE.finditer(text):
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            val = float(raw)
            if 5 < val < 50000:
                return val
        except ValueError:
            pass
    return None


def _parse_stockx_html(html: str, *, current_url: str = "") -> Optional[Dict[str, Any]]:
    if not html or len(html) < 500:
        return None
    title = ""
    price: Optional[float] = None
    ref_url = current_url

    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        tile = soup.find(attrs={"data-testid": "product-tile"})
        if not tile:
            tile = soup.find("a", href=re.compile(r"/fr-fr/[^/]+", re.I))
        if tile:
            text = tile.get_text(" ", strip=True)
            link = tile.find("a", href=True) if tile.name != "a" else tile
            if link and link.get("href"):
                href = str(link["href"])
                ref_url = href if href.startswith("http") else "https://stockx.com" + href
            p = tile.find("p")
            if p:
                title = p.get_text(strip=True)
            price = _parse_price_eur(text)
            if not title:
                title = text[:120]
    except ImportError:
        pass

    if price is None:
        chunk = html[:80000]
        idx = chunk.find("product-tile")
        if idx < 0:
            idx = chunk.find("Lowest Ask")
        if idx < 0:
            idx = chunk.find("Dernière vente")
        if idx >= 0:
            snippet = chunk[max(0, idx - 200) : idx + 1200]
            price = _parse_price_eur(snippet)

    if price is None:
        return None

    return {
        "stockx_title": title or None,
        "lowest_ask_price": price,
        "reference_url": ref_url,
    }

# --- router.py ---

import logging
import re
import statistics
from typing import Any, Dict, List, Optional, Set, Tuple


logger = logging.getLogger("vf.oracle.router")


def _norm_query(q: str) -> str:
    s = re.sub(r"\s+", " ", (q or "").strip().lower())
    return s[:120]


def _oracle_query_for_row(row: Dict[str, Any]) -> str:
    bits = [
        str(row.get("product_model_label") or ""),
        str(row.get("ai_model_exact") or ""),
        str(row.get("ds_model_family") or ""),
        str(row.get("title") or "")[:80],
    ]
    for b in bits:
        if b.strip() and b.strip().lower() not in ("unknown", "none", "—"):
            return b.strip()
    return str(row.get("title") or "")[:80]

# Sources réellement implémentées dans MarketOracle.lookup()
_LIVE_SOURCES = frozenset({"backmarket", "denicheur", "idealo", "stockx"})
_FALLBACK_SOURCES = frozenset({"models", "run_median"})

# Matrice catégorie → oracles autorisés (ordre = priorité d'affichage / logique)
ROUTING_MATRIX: Dict[str, List[str]] = {
    "phone": ["backmarket", "idealo", "denicheur"],
    "console_ps4": ["backmarket", "idealo", "denicheur"],
    "console_ps5": ["backmarket", "idealo", "denicheur"],
    "console_switch": ["backmarket", "idealo", "denicheur"],
    "console_xbox": ["backmarket", "idealo", "denicheur"],
    "console_ds": ["backmarket", "idealo", "denicheur"],
    "handheld": ["backmarket", "idealo", "denicheur"],
    "laptop": ["backmarket", "idealo", "denicheur"],
    "pc": ["backmarket", "idealo", "denicheur"],
    "ac": ["backmarket", "idealo", "denicheur"],
    "projector": ["backmarket", "idealo", "denicheur"],
    "coffee": ["idealo", "denicheur", "backmarket"],
    "fan": ["idealo", "denicheur"],
    "small_appliance": ["idealo", "denicheur", "backmarket"],
    "wearable": ["backmarket", "idealo", "denicheur"],
    "camera": ["backmarket", "idealo", "denicheur"],
    "audio": ["idealo", "denicheur"],
    "shoes": ["stockx", "idealo"],
    "sneakers": ["stockx", "idealo"],
    "apparel": ["stockx", "idealo"],
    "jeans": ["idealo", "denicheur"],
    "vinyl": ["idealo", "denicheur", "models"],
    "bicycle": ["models", "run_median"],
    "sports": ["models", "run_median"],
    "tent": ["idealo", "denicheur", "models"],
    "skate": ["models", "run_median"],
    "skates": ["models", "run_median"],
    "surf": ["models", "run_median"],
    "generic": ["backmarket", "idealo", "denicheur"],
    "unknown": ["idealo", "denicheur", "models"],
}

_DEFAULT_ROUTE = ["backmarket", "idealo", "denicheur", "models"]

_BACKMARKET_FORBIDDEN = frozenset(
    {"bicycle", "sports", "skate", "skates", "surf", "vinyl", "tent"}
)

_STOCKX_CATS = frozenset(
    {"sneakers", "shoes", "apparel", "console", "console_ps4", "console_ps5", "handheld", "generic"}
)


def allowed_oracles_for_category(category: str) -> List[str]:
    """Oracles autorisés pour une catégorie (matrice + overrides env)."""
    cat = (category or "unknown").lower().strip()
    route = list(ROUTING_MATRIX.get(cat) or ROUTING_MATRIX.get("generic") or _DEFAULT_ROUTE)
    if cat in _BACKMARKET_FORBIDDEN and "backmarket" in route:
        route = [s for s in route if s != "backmarket"]
    if "sneaker" in cat or "nike" in cat or "jordan" in cat:
        if "stockx" not in route:
            route.insert(0, "stockx")
    return route


def live_sources_for_category(category: str) -> Set[str]:
    """Sous-ensemble des sources à interroger via réseau/navigateur."""
    allowed = set(allowed_oracles_for_category(category))
    live = allowed & _LIVE_SOURCES
    cat = (category or "").lower()
    if "stockx" in live and cat not in _STOCKX_CATS:
        if not any(x in cat for x in ("sneaker", "nike", "jordan", "shoe", "apparel")):
            live.discard("stockx")
    return live


def eligible_for_oracle(row: Dict[str, Any]) -> bool:
    """Lazy gate — pas d'oracle pour accessoires / dropship / doubtful."""
    if row.get("is_accessory"):
        return False
    tier = str(row.get("quality_tier") or "").lower()
    if tier in ("dropship", "dinosaur"):
        return False
    triage = str(row.get("triage_decision") or "").lower()
    if triage in ("reject", "doubtful"):
        return False
    return True


def resolve_cache_key(row: Dict[str, Any], *, query: str = "") -> str:
    """Clé canonique : product_model_key > ds_model_family > requête normalisée."""
    for field in ("product_model_key", "ds_model_family"):
        key = str(row.get(field) or "").strip()
        if key and key.lower() not in ("unknown", "none", "—", ""):
            return key
    q = (query or "").strip().lower()
    if q:
        return f"q:{_norm_query(q)}"
    return ""


def build_oracle_query(row: Dict[str, Any], *, cache_key: str = "") -> str:
    """Requête oracle : label modèle KB > alias eBay > heuristique titre."""
    key = cache_key or resolve_cache_key(row)
    if key and not key.startswith("q:"):
        try:
            from .models import get_model, search_aliases

            m = get_model(key)
            if m and m.label:
                return m.label
            aliases = search_aliases(key, "ebay")
            if aliases:
                return aliases[0]
        except Exception:
            pass
    return _oracle_query_for_row(row)


def arbitrate_market_reference(ref: PriceReference) -> Tuple[Optional[float], str]:
    """
    Prix de référence marché :
    - StockX prioritaire pour collectibles
    - Sinon MIN(neuf, reconditionné) — Black Friday safe
    """
    if ref.stockx_lowest_ask_eur and ref.stockx_lowest_ask_eur > 0:
        return ref.stockx_lowest_ask_eur, "stockx"

    candidates: List[Tuple[str, float]] = []
    retail = ref.retail_low_eur
    refurb = ref.refurb_min_eur
    if retail and retail > 0:
        candidates.append(("new", float(retail)))
    if refurb and refurb > 0:
        candidates.append(("refurb", float(refurb)))

    if not candidates:
        return None, ""

    kind, price = min(candidates, key=lambda x: x[1])
    return price, kind


class RunModelCache:
    """Cache RAM du run : une résolution oracle par modèle, pas par annonce."""

    def __init__(self) -> None:
        self._refs: Dict[str, PriceReference] = {}

    def get(self, key: str) -> Optional[PriceReference]:
        return self._refs.get(key)

    def set(self, key: str, ref: PriceReference) -> None:
        self._refs[key] = ref

    def __len__(self) -> int:
        return len(self._refs)


def _run_median_for_key(rows: List[Dict[str, Any]], model_key: str) -> Optional[float]:
    if not model_key or model_key.startswith("q:"):
        return None
    prices: List[float] = []
    for r in rows:
        rk = resolve_cache_key(r)
        if rk != model_key:
            continue
        try:
            p = float(r.get("total_price") or r.get("price") or 0)
        except (TypeError, ValueError):
            p = 0.0
        if p > 0:
            prices.append(p)
    if len(prices) < 2:
        return None
    return float(statistics.median(prices))


def graceful_fallback(
    row: Dict[str, Any],
    *,
    all_rows: List[Dict[str, Any]],
    cache_key: str,
    category: str,
    failed_live: bool,
) -> PriceReference:
    """Plan B : MSRP models.json puis médiane du run courant."""
    ref = PriceReference(query=build_oracle_query(row, cache_key=cache_key))
    ref.degraded = True
    allowed = set(allowed_oracles_for_category(category))

    if "models" in allowed or failed_live:
        try:
            from .models import estimate_msrp_eur_for_row

            msrp = estimate_msrp_eur_for_row(row)
            if msrp and msrp > 0:
                ref.new_low_eur = float(msrp)
                ref.sources.append("models")
        except Exception as exc:
            logger.debug("Fallback models : %s", exc)

    if not ref.sources and "run_median" in allowed:
        med = _run_median_for_key(all_rows, cache_key)
        if med and med > 0:
            ref.new_low_eur = med
            ref.sources.append("run_median")

    return ref


def apply_reference_to_row(
    row: Dict[str, Any],
    ref: PriceReference,
    *,
    category: str = "",
) -> bool:
    """Projette un PriceReference (cache ou live) sur une ligne annonce."""
    if not ref.sources and not ref.degraded:
        return False

    try:
        from .scoring import effective_price_eur, is_discontinued_product, sane_reference_eur

        price = effective_price_eur(row)
        discontinued = is_discontinued_product(row)
        if discontinued:
            row["is_discontinued"] = True
    except Exception:
        try:
            price = float(row.get("total_price") or row.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        discontinued = bool(row.get("is_discontinued"))

    if ref.refurb_min_eur and ref.refurb_min_eur > 0:
        row["ref_price_refurb_eur"] = ref.refurb_min_eur
        if price > 0:
            row["discount_vs_refurb_pct"] = round((1.0 - price / ref.refurb_min_eur) * 100.0, 1)

    retail = ref.retail_low_eur
    if retail and retail > 0:
        row["ref_price_new_eur"] = retail
        if price > 0 and not discontinued:
            row["discount_vs_retail_new_pct"] = round((1.0 - price / retail) * 100.0, 1)
        if not discontinued and (
            not row.get("msrp_eur") or float(row.get("msrp_eur") or 0) <= 0
        ):
            row["msrp_eur"] = retail
            if ref.idealo_low_eur and ref.idealo_low_eur <= (ref.new_low_eur or 1e9):
                row["msrp_source"] = "idealo"
            elif "denicheur" in ref.sources:
                row["msrp_source"] = "denicheur"
            elif "models" in ref.sources:
                row["msrp_source"] = "models"
            else:
                row["msrp_source"] = "oracle"

    if ref.stockx_lowest_ask_eur and ref.stockx_lowest_ask_eur > 0:
        row["ref_price_stockx_eur"] = ref.stockx_lowest_ask_eur
        if ref.stockx_title:
            row["stockx_match_title"] = ref.stockx_title
        if ref.stockx_url:
            row["stockx_url"] = ref.stockx_url
        if price > 0:
            row["discount_vs_stockx_pct"] = round(
                (1.0 - price / ref.stockx_lowest_ask_eur) * 100.0, 1
            )

    row["price_reference_sources"] = ",".join(ref.sources)
    row["oracle_allowed_sources"] = ",".join(allowed_oracles_for_category(category or str(row.get("item_type") or "")))
    if ref.degraded:
        row["oracle_degraded"] = True
    if ref.denicheur_url:
        row["denicheur_url"] = ref.denicheur_url
    if ref.idealo_url:
        row["idealo_url"] = ref.idealo_url

    arb_price, arb_kind = arbitrate_market_reference(ref)
    if arb_price and arb_price > 0 and price > 0:
        sane = arb_price >= max(20.0, price * 0.15)
        if sane:
            row["oracle_arbiter_ref_eur"] = round(arb_price, 2)
            row["oracle_arbiter_kind"] = arb_kind
            from .engine import clamp_discount_pct

            disc = clamp_discount_pct((1.0 - price / arb_price) * 100.0)
            row["market_discount_pct"] = disc
            row["model_vs_market_pct"] = disc
        else:
            row["oracle_degraded"] = True
            row["oracle_bad_ref"] = round(arb_price, 2)
    elif row.get("discount_vs_stockx_pct") is not None:
        d = float(row["discount_vs_stockx_pct"])
        row["market_discount_pct"] = d
        row["model_vs_market_pct"] = d
    elif row.get("discount_vs_refurb_pct") is not None:
        dref = float(row["discount_vs_refurb_pct"])
        row["market_discount_pct"] = dref
        row["model_vs_market_pct"] = dref
    elif row.get("discount_vs_retail_new_pct") is not None:
        dnew = float(row["discount_vs_retail_new_pct"])
        row["market_discount_pct"] = dnew
        row["model_vs_market_pct"] = dnew

    if not discontinued and row.get("msrp_eur") and price > 0:
        try:
            msrp = sane_reference_eur(float(row["msrp_eur"]), price, discontinued=False)
            if msrp and msrp > 0:
                row["discount_vs_new_pct"] = round((msrp - price) / msrp * 100.0, 1)
        except (TypeError, ValueError):
            pass

    return bool(ref.sources)


class SmartOracleRouter:
    """Orchestre lookup, cache modèle, routage et dégradation gracieuse."""

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle
        self._cache = RunModelCache()

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def resolve(
        self,
        row: Dict[str, Any],
        *,
        all_rows: List[Dict[str, Any]],
        category_hint: str = "",
    ) -> PriceReference:
        cat = category_hint or str(row.get("item_type") or "unknown")
        query = build_oracle_query(row)
        cache_key = resolve_cache_key(row, query=query)
        if not cache_key:
            cache_key = f"q:{query[:80].lower()}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        live_sources = live_sources_for_category(cat)
        ref = PriceReference(query=query)
        failed_live = False

        if live_sources:
            try:
                ref = self._oracle.lookup(
                    query,
                    category_hint=cat,
                    allowed_sources=live_sources,
                )
            except Exception as exc:
                logger.warning("Oracle live échoué [%s] : %s", cache_key, exc)
                failed_live = True
                ref = PriceReference(query=query)

        if not ref.sources:
            fb_allowed = set(allowed_oracles_for_category(cat)) & _FALLBACK_SOURCES
            if fb_allowed or failed_live:
                ref = graceful_fallback(
                    row,
                    all_rows=all_rows,
                    cache_key=cache_key,
                    category=cat,
                    failed_live=failed_live or not live_sources,
                )

        self._cache.set(cache_key, ref)
        return ref

    def enrich_rows(
        self,
        rows: List[Dict[str, Any]],
        *,
        max_model_lookups: int = 25,
        category_hint: str = "",
        log_fn: Optional[Any] = None,
    ) -> int:
        """
        Lazy enrichissement : filtre → top rank → déduplique par modèle → propage.
        Retourne le nombre de lignes enrichies.
        """
        candidates = [r for r in rows if eligible_for_oracle(r)]
        if not candidates:
            return 0

        ranked = sorted(
            candidates,
            key=lambda r: float(r.get("rank_score") or r.get("final_score") or 0),
            reverse=True,
        )

        # Modèles uniques à résoudre (pas N appels pour N annonces)
        seen_keys: Set[str] = set()
        lookup_queue: List[Dict[str, Any]] = []
        for r in ranked:
            q = build_oracle_query(r)
            ck = resolve_cache_key(r, query=q) or f"q:{q[:80].lower()}"
            if ck in seen_keys:
                continue
            seen_keys.add(ck)
            lookup_queue.append(r)
            if len(lookup_queue) >= max(1, max_model_lookups):
                break

        for r in lookup_queue:
            cat = category_hint or str(r.get("item_type") or "unknown")
            ck = resolve_cache_key(r, query=build_oracle_query(r))
            ref = self.resolve(r, all_rows=rows, category_hint=cat)
            if log_fn:
                try:
                    live = ",".join(sorted(live_sources_for_category(cat)))
                    flag = " [dégradé]" if ref.degraded else ""
                    log_fn(
                        f"  Router [{ck}] → {ref.sources or ['—']} "
                        f"(route: {live}){flag}"
                    )
                except Exception:
                    pass

        # Propagation O(1) : toutes les annonces éligibles du même modèle
        enriched = 0
        for r in ranked:
            q = build_oracle_query(r)
            ck = resolve_cache_key(r, query=q)
            if not ck:
                continue
            ref = self._cache.get(ck)
            if ref is None:
                continue
            cat = category_hint or str(r.get("item_type") or "")
            if apply_reference_to_row(r, ref, category=cat):
                enriched += 1

        return enriched

# --- public API ---

import logging
import os
import re
from typing import Any, Dict, List, Optional, Set


logger = logging.getLogger("vf.oracle")

_ORACLE_ENABLED = os.environ.get("VF_ORACLE", "").strip().lower() in ("1", "true", "yes", "on")
_BOOTSTRAP = os.environ.get("VF_ORACLE_BOOTSTRAP", "auto").strip().lower()

_STOCKX_CATS = frozenset(
    {
        "sneakers",
        "apparel",
        "console",
        "console_ps4",
        "console_ps5",
        "handheld",
        "generic",
    }
)


def oracles_enabled() -> bool:
    return _ORACLE_ENABLED


def _norm_query(q: str) -> str:
    s = re.sub(r"\s+", " ", (q or "").strip().lower())
    return s[:120]


def _use_stockx_for_category(category_hint: str) -> bool:
    if os.environ.get("VF_ORACLE_STOCKX", "1") == "0":
        return False
    cat = (category_hint or "").lower().strip()
    if not cat:
        return True
    return cat in _STOCKX_CATS or "sneaker" in cat or "nike" in cat or "jordan" in cat


class MarketOracle:
    """Façade avec cache disque, bootstrap HTTP et navigateur partagé."""

    def __init__(self, *, headless: bool = True) -> None:


        self._cache = OracleCache()
        self._headless = headless
        self._browser = OracleBrowserPool(headless=headless)
        self._bm = None
        self._bootstrapped_bm = False

    def _backmarket(self):
        if self._bm is None:


            self._bm = BackMarketOracle()
        return self._bm

    def _denicheur(self):


        return DenicheurOracle(browser=self._browser, headless=self._headless)

    def _idealo(self):


        return IdealoOracle(browser=self._browser, headless=self._headless)

    def _stockx(self):


        return StockxOracle(browser=self._browser, headless=self._headless)

    def lookup(
        self,
        query: str,
        *,
        category_hint: str = "",
        allowed_sources: Optional[Set[str]] = None,
        use_backmarket: bool = True,
        use_denicheur: bool = True,
        use_idealo: bool = True,
        use_stockx: bool = True,
    ) -> PriceReference:
        key = _norm_query(query)
        if not key:
            return PriceReference()

        cached = self._cache.get(key)
        if cached:
            return cached

        ref = PriceReference(query=query)

        def _allowed(name: str) -> bool:
            if allowed_sources is not None:
                return name in allowed_sources
            return True

        if (
            use_backmarket
            and _allowed("backmarket")
            and os.environ.get("VF_ORACLE_BACKMARKET", "1") != "0"
        ):
            try:
                bm = self._backmarket()
                if _BOOTSTRAP in ("auto", "1", "true", "yes") and not self._bootstrapped_bm:
                    bm.bootstrap_session()
                    self._bootstrapped_bm = True
                part = bm.get_baseline_prices(query)
                if part:
                    ref.refurb_fair_eur = part.get("Fair")
                    ref.refurb_good_eur = part.get("Good")
                    ref.refurb_excellent_eur = part.get("Excellent")
                    if any(part.values()):
                        ref.sources.append("backmarket")
            except Exception as exc:  # noqa: BLE001
                logger.debug("Back Market oracle: %s", exc)

        new_candidates: List[tuple[str, float, str]] = []

        if (
            use_denicheur
            and _allowed("denicheur")
            and os.environ.get("VF_ORACLE_DENICHEUR", "1") != "0"
        ):
            try:
                part = self._denicheur().search_new_prices(query)
                if part and part.get("lowest_new_price"):
                    ref.new_low_eur = float(part["lowest_new_price"])
                    ref.new_median_eur = part.get("median_top_5")
                    ref.denicheur_url = str(part.get("market_reference_url") or "")
                    new_candidates.append(("denicheur", ref.new_low_eur, ref.denicheur_url))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Dénicheur oracle: %s", exc)

        if use_idealo and _allowed("idealo") and os.environ.get("VF_ORACLE_IDEALO", "1") != "0":
            try:
                part = self._idealo().search_new_prices(query)
                if part and part.get("lowest_new_price"):
                    ref.idealo_low_eur = float(part["lowest_new_price"])
                    ref.idealo_url = str(part.get("market_reference_url") or "")
                    new_candidates.append(("idealo", ref.idealo_low_eur, ref.idealo_url))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Idealo oracle: %s", exc)

        if new_candidates:
            best = min(new_candidates, key=lambda x: x[1])
            ref.new_low_eur = best[1]
            if best[0] not in ref.sources:
                ref.sources.append(best[0])
            for src, _, _ in new_candidates:
                if src not in ref.sources:
                    ref.sources.append(src)

        if (
            use_stockx
            and _allowed("stockx")
            and _use_stockx_for_category(category_hint)
        ):
            try:
                part = self._stockx().get_market_value(query)
                if part and part.get("lowest_ask_price"):
                    ref.stockx_lowest_ask_eur = float(part["lowest_ask_price"])
                    ref.stockx_title = str(part.get("stockx_title") or "")
                    ref.stockx_url = str(part.get("reference_url") or "")
                    ref.sources.append("stockx")
            except Exception as exc:  # noqa: BLE001
                logger.debug("StockX oracle: %s", exc)

        if ref.sources:
            self._cache.set(key, ref)
        return ref

    def close(self) -> None:
        self._browser.close()
        if self._bm is not None:
            try:
                self._bm.close()
            except Exception:
                pass


_singleton: Optional[MarketOracle] = None


def get_oracle() -> MarketOracle:
    global _singleton
    if _singleton is None:
        headless = os.environ.get("VF_ORACLE_HEADED", "").strip().lower() not in ("1", "true", "yes")
        _singleton = MarketOracle(headless=headless)
    return _singleton


def close_oracle() -> None:
    """Ferme navigateur + sessions — appeler en fin de run pipeline."""
    global _singleton
    if _singleton is not None:
        try:
            _singleton.close()
        except Exception:
            pass
        _singleton = None


def lookup_price_reference(query: str, *, category_hint: str = "") -> PriceReference:
    return get_oracle().lookup(query, category_hint=category_hint)


def _oracle_query_for_row(row: Dict[str, Any]) -> str:
    bits = [
        str(row.get("product_model_label") or ""),
        str(row.get("ai_model_exact") or ""),
        str(row.get("ds_model_family") or ""),
        str(row.get("title") or "")[:80],
    ]
    for b in bits:
        if b.strip() and b.strip().lower() not in ("unknown", "none", "—"):
            return b.strip()
    return str(row.get("title") or "")[:80]


def apply_oracle_to_row(row: Dict[str, Any], *, category_hint: str = "") -> bool:
    """Enrichit une ligne via le routeur (compat API legacy)."""
    if not oracles_enabled():
        return False
    if not eligible_for_oracle(row):
        return False
    router = SmartOracleRouter(get_oracle())
    ref = router.resolve(row, all_rows=[row], category_hint=category_hint)
    return apply_reference_to_row(row, ref, category=category_hint)


def enrich_rows_oracle(
    rows: List[Dict[str, Any]],
    *,
    max_lookups: int = 25,
    category_hint: str = "",
    log_fn: Optional[Any] = None,
) -> int:
    """API publique — enrichissement prix oracle sur le top du run."""
    if not oracles_enabled():
        return 0
    router = SmartOracleRouter(get_oracle())
    return router.enrich_rows(
        rows,
        max_model_lookups=max_lookups,
        category_hint=category_hint,
        log_fn=log_fn,
    )

