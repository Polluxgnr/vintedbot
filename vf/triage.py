"""Triage des annonces par titre et description : keep, reject ou doubtful avant scoring."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

from .intent import SearchIntent, parse_search_intent, _FASHION_NOISE
from .market import CatalogItem
from .scoring import detect_accessory, infer_category, risk_flags

import os

logger = logging.getLogger("vf.triage")

PROMOTE_DOUBTFUL_MIN_SCORE = float(os.environ.get("VF_PROMOTE_MIN_SCORE", "62"))
PROMOTE_DOUBTFUL_MIN_REL = float(os.environ.get("VF_PROMOTE_MIN_REL", "72"))


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
_WRONG_CATEGORY_NOT_VIDEO = re.compile(
    r"(?i)\b(voigtl[aä]nder|diapositive|diapositives|slide\s*projector|projecteur\s*[àa]\s*diapos|"
    r"r[ée]troprojecteur|épiscope|episcope|dia\s*projector|film\s*projector|8\s*mm|super\s*8|"
    r"vp\s*\d+\s*a|carousel\s*slide)\b"
)

class TitleDecision(str, Enum):
    KEEP = "keep"
    REJECT = "reject"
    DOUBTFUL = "doubtful"


@dataclass(slots=True)
class TitleTriage:
    decision: TitleDecision
    reasons: List[str] = field(default_factory=list)
    confidence: float = 1.0


TRIAGE_REASON_LABELS: Dict[str, str] = {
    "brand_mismatch": "Marque différente de la recherche",
    "intent_mismatch": "Produit probablement hors-sujet",
    "screen_defect_mentioned": "Défaut écran mentionné dans le titre",
    "short_title": "Titre trop vague (peu d'infos)",
    "bundle_lot": "Lot / bundle flou",
    "unknown_category": "Catégorie incertaine",
    "accessory_only": "Accessoire seul (pas le produit)",
    "game_only": "Jeu seul (pas la console)",
    "game_only_title": "Lot de jeux sans console",
    "wrong_category": "Hors catégorie (TV, accessoire…)",
    "ps4_title_match": "Titre PS4 reconnu",
    "ps4_console_generic": "Console (intention PS4)",
    "wrong_platform": "Mauvaise plateforme (hors PS4)",
    "empty_packaging": "Boîte vide / emballage seul",
    "wrong_model": "Modèle différent de la recherche",
    "confirmed_after_desc": "Confirmé OK après lecture description",
}


def explain_triage_reasons(raw: str) -> str:
    """Libellés français lisibles pour les doutes/rejets."""
    if not (raw or "").strip():
        return "Annonce ambiguë — vérification manuelle conseillée"
    parts = re.split(r"[|;,]+", str(raw))
    out: List[str] = []
    for p in parts:
        code = p.strip()
        if not code:
            continue
        if code.startswith("weak_cat:"):
            out.append(f"Catégorie faible : {code.split(':', 1)[-1]}")
            continue
        if code.startswith("risk:"):
            out.append(f"Risque détecté : {code[5:].replace('_', ' ')}")
            continue
        out.append(TRIAGE_REASON_LABELS.get(code, code.replace("_", " ")))
    return " · ".join(dict.fromkeys(out))[:280]


@dataclass(slots=True)
class TriageBatch:
    keep_ids: Set[str] = field(default_factory=set)
    reject_ids: Set[str] = field(default_factory=set)
    doubtful_ids: Set[str] = field(default_factory=set)
    by_id: Dict[str, TitleTriage] = field(default_factory=dict)

    @property
    def working_ids(self) -> Set[str]:
        return self.keep_ids | self.doubtful_ids


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return re.sub(r"\s+", " ", nfkd.strip().lower())


def _fold_accents(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in folded if not unicodedata.combining(c)).lower()


def _title_text(item: CatalogItem) -> str:
    return _normalize(item.title or "")


_TITLE_RISK: List[Tuple[str, re.Pattern[str]]] = [
    ("for_parts", re.compile(r"(?i)\b(pour\s*pi[eè]ces|for\s*parts|ricambi|para\s*piezas)\b")),
    ("not_working", re.compile(r"(?i)\b(ne\s*fonctionne\s*pas|doesn'?t\s*work|non\s*funziona|funktioniert\s*nicht)\b")),
    ("broken", re.compile(r"(?i)(?<!prix )\b(cass[ée]e?|broken|hors\s*service|kaputt|defekt)\b")),
    ("broken_hs", re.compile(r"(?i)(^|\s)hs(\s|$)")),
    ("ban", re.compile(r"(?i)\b(bann[iéée]?|psn\s*ban|compte\s+banni|gesperrt)\b")),
    ("repair_only", re.compile(r"(?i)\b([àa]\s*r[ée]parer|for\s*repair|da\s*riparare)\b")),
    (
        "off_platform_payment",
        re.compile(
            r"(?i)\b(paypal|pay\s*pal|revolut|lydia|western\s*union|crypto|bitcoin|"
            r"virement\s*direct|hors\s*vinted|en\s+dehors|wire\s*transfer|"
            r"paiement\s+externe|payment\s+outside)\b"
        ),
    ),
]

_PARTS_ONLY = re.compile(
    r"(?i)\b(pi[eè]ce\s*d[ée]tach|spare\s*part|coque\s*seule|manette\s*seule|controller\s*only|"
    r"lampe\s*seule|ampoule\s*seule|écran\s*seul|screen\s*only|télécommande\s*seule|remote\s*only|"
    r"charni[eè]re|bouton\s*l|ruban\s*nfc|nfc\s*ribbon|connecteur\s*d[ée]fectueux|"
    r"cartouche\s*seule|module\s*écran|dalle\s*seule|faceplate|cover\s*plate)\b"
)
_CONSOLE_DS_NOISE = re.compile(
    r"(?i)\b(amiibo|figurine|poster|affiche|guide\s*officiel|manuel\s*seul|"
    r"notice\s*seule|sticker|autocollant|protection\s*écran|verre\s*trempé)\b"
)
_DOUBTFUL_HINT = re.compile(
    r"(?i)(\blot\b|\bbundle\b|\bpack\b|\bset\b|\d+\s*\+\s*\d+|\b\w+\s*\+\s*\w+|"
    r"\bavec\s+\d|\bincl\.|\bconsole\s*\+)"
)
_CONSOLE_MAIN_TITLE = re.compile(
    r"(?i)\b(new\s*nintendo\s*3ds|new\s*3ds(\s*xl)?|nintendo\s*3ds|3ds\s*xl|2ds|nintendo\s*ds|"
    r"ps5|playstation\s*5|ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4|"
    r"xbox\s*(series\s*[xs]|one)?|nintendo\s*switch)\b"
)
_PS4_FUZZY = re.compile(r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4)\b")
_PS4_INTENT_HINT = re.compile(
    r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4|playstation|sony)\b"
)
_CONSOLE_PS4_WRONG_PLATFORM = re.compile(
    r"(?i)\b("
    r"xbox(\s*(360|one|series\s*[xs]))?|\brgh\b|"
    r"nintendo\s*switch|switch\s*(oled|lite)?|"
    r"super\s*famicom|snes|famicom|\bnes\b|n64|gamecube|wii\s*u?|"
    r"game\s*boy|gameboy|\bgba\b|master\s*system|mega\s*drive|genesis|"
    r"sega\s*saturn|dreamcast|"
    r"ps[235]\b|playstation\s*[235]|psp|ps\s*vita|"
    r"steam\s*deck|nintendo\s*64"
    r")\b"
)
_AC_WRONG_CATEGORY = re.compile(
    r"(?i)\b("
    r"compresseur(?:\s+de)?\s+clim|compresseur\s+clim\s+auto|pompe\s+(?:de\s+)?clim\s+auto|"
    r"pulseur\s+(?:d['\u2019]?air|habitacle)|radiateur\s+(?:auto|moteur)|"
    r"rafra[iî]chisseur(?:\s+d['\u2019]?air)?|"
    r"recharge\s+(?:de\s+)?(?:gaz\s+)?clim|montage\s+clim|prestation\s+clim|"
    r"location\s+(?:vacances|saisonni[eè]re|été)|"
    r"villa\s+(?:avec\s+)?clim|appartement\s+(?:avec\s+)?clim|maison\s+(?:avec\s+)?clim|"
    r"composteur(?:\s+de\s+clim)?|"
    r"t[eé]l[eé]commande(?:\s+clim(?:atiseur)?)?|t[eé]l[eé]commande\s+universelle|"
    r"tuyau(?:\s+flexible)?\s+(?:de\s+)?clim|kit\s+(?:de\s+)?conduit(?:e)?\s+clim|"
    r"kit\s+fen[eê]tre|fen[eê]tre.*clim|"
    r"pi[eè]ce\s+(?:d[eé]tach[eé]e\s+)?(?:de\s+)?clim(?:atiseur)?|"
    r"filtre\s+(?:de\s+)?clim(?:atiseur)?|filtre\s+air\s+clim|"
    r"support\s+(?:de\s+)?clim(?:atiseur)?|condenseur\s+clim\s+auto|"
    r"s[eé]ance\s+(?:de\s+)?clim|entretien\s+clim|"
    r"d[eé]tecteur\s+de\s+gaz|detecteur\s+de\s+gaz|"
    r"m[eé]gane|clio|scenic|captur|dacia|renault|peugeot|citro[eë]n|ford|volkswagen|vw\b"
    r")\b"
)
_AC_OFF_CATEGORY = re.compile(
    r"(?i)\b("
    r"abrigo|chaqueta|pullover|manteau|parka|"
    r"rossignol|lafuma\s+access|pegasus|north\s*face|"
    r"chaussure|chaussures|sneaker|basket|zapatillas?|sandales?|boots?|"
    r"reloj|smartwatch|montre|apple\s*watch|galaxy\s*watch|"
    r"tank\s+clim|coche\s|voiture\s+(mini|jouet)|jouet\b|"
    r"access\s+clim|d[eé]tecteur\s+de\s+gaz|detecteur\s+de\s+gaz|"
    r"clima[\s-]?fit|climawarm|climacool|dri[\s-]?fit|thermal\s+fit"
    r")\b"
)
_AC_CLIM_SUBSTRING_NOISE = re.compile(
    r"(?i)(?:"
    r"\b(?:veste|abrigo|chaqueta|rossignol|lafuma|access|nike|adidas|puma|under\s*armour)\b.*\bclim\b|"
    r"\bclim\b.*\b(?:rossignol|t-\d{2,4}|nike|adidas|climawarm|climacool|clima[\s-]?fit)\b|"
    r"\b(?:tank|coche|detecteur|gaz|montre|reloj)\b.*\bclim\b|"
    r"\bde\s+clim\b"
    r")"
)
_CONSOLE_PS4_REJECT = re.compile(
    r"(?i)\b("
    r"tele(?:vision|viseur)|t[ée]l[ée]viseur|\btv\b|smart\s*tv|"
    r"thrustmaster|skylanders?|"
    r"pneu|pneus|tire|tyre|"
    r"volant\s*seul|manette\s*seule|dualshock\s*seul|"
    r"support\s*tv|bras\s*tv"
    r")\b"
)
_PS4_GAME_LOT = re.compile(r"(?i)\blot\s+de\s+\d+\s+jeux\b")
_ACCESSORY_ONLY_TITLE = re.compile(
    r"(?i)\b(coque\s*seule|housse\s*seule|[ée]tui\s*seul|case\s*only|shell\s*only|"
    r"coque\s+pour|housse\s+pour|[ée]tui\s+pour|case\s+for|shell\s+for|"
    r"jeu\s*seul|game\s+only|chargeur\s*seul|manette\s*seule)\b"
)
_GAME_ONLY_TITLE = re.compile(r"(?i)^(jeu(x)?|game(s)?|juego(s)?|giochi|spiel)\b")


def title_has_main_console_unit(title: str) -> bool:
    return bool(_CONSOLE_MAIN_TITLE.search(title or ""))


def title_matches_search_intent(title: str, search_intent: str = "") -> bool:
    """True si le titre correspond clairement à la requête utilisateur."""
    raw = (search_intent or "").strip().lower()
    if not raw:
        return False
    if raw.startswith("http"):
        try:
            from .market import params_from_url

            p = params_from_url(raw)
            raw = str(p.get("search_text") or "").strip().lower()
        except Exception:
            pass
        if not raw:
            m = re.search(r"vinted\.[^/]+/(.+)$", search_intent.strip(), re.I)
            if m:
                from urllib.parse import unquote

                raw = unquote(m.group(1).replace("+", " ")).strip().lower()
    if not raw:
        return False
    title_l = (title or "").lower()
    tokens = [t for t in re.split(r"\s+", raw) if len(t) > 1]
    if not tokens:
        return raw in title_l
    return all(t in title_l for t in tokens[:5])


def classify_rejected_listing(
    row: Dict[str, Any],
    *,
    title_rel: Optional[int] = None,
    market_category: str = "",
    search_intent: str = "",
) -> str:
    """Classe une annonce archivée : « doubtful » (ambigu) ou « rejected » (clair)."""
    title = str(row.get("title") or "")
    reasons = str(row.get("triage_reasons") or row.get("reject_reason") or "")
    stage = str(row.get("reject_stage") or "")

    # Rejets explicites au triage → toujours Rejetés
    if stage == "triage_titre" or "risk:" in reasons:
        return "rejected"
    if reasons.startswith("accessory:") or "accessory:" in reasons:
        return "rejected"
    if any(x in reasons for x in ("parts_only", "game_only", "wrong_category", "fashion_noise", "not_projector")):
        return "rejected"

    if market_category == "projector" or "projector" in str(row.get("item_type") or ""):
        title_l = title.lower()
        if re.search(r"(?i)\b(epson|benq|optoma|viewsonic|projecteur|projector)\b", title_l):
            if any(x in reasons for x in ("bundle_lot", "unknown_category", "intent_mismatch")):
                return "rejected"
            if title_matches_search_intent(title, search_intent):
                return "rejected"

    # clear_title : triage OK mais écarté par l’IA → Doutes (surtout consoles / produit principal)
    if "clear_title" in reasons:
        if title_has_main_console_unit(title) and not _GAME_ONLY_TITLE.search(title):
            return "doubtful"
        if stage == "filtre_ia_ou_pipeline":
            if title_matches_search_intent(title, search_intent):
                return "doubtful"
            if title_rel is not None and title_rel >= 18:
                return "doubtful"
            return "doubtful"

    ambiguous_markers = (
        "accessory_maybe",
        "bundle_lot",
        "unknown_category",
        "intent_mismatch",
        "short_title",
        "weak_cat:",
    )
    if any(m in reasons for m in ambiguous_markers):
        return "doubtful"

    if stage == "filtre_ia_ou_pipeline" and title_rel is not None and title_rel >= 28:
        return "doubtful"

    if title_has_main_console_unit(title) and "console" in reasons:
        return "doubtful"

    return "rejected"
_GUIDE_MEDIA = re.compile(r"(?i)\b(gu[ií]a|guide|manuale|libro|book|magazine|poster)\b")
_GUIDE_EXCEPT = re.compile(r"(?i)\b(console|ps[345]|playstation|bundle|lot|projecteur)\b")
_SHORT_TITLE = 7
_EMPTY_PACKAGING_ONLY = re.compile(
    r"(?i)\b("
    r"bo[iî]te\s*vide|empty\s*box|sans\s*console|console\s*non\s*incluse|"
    r"notice\s*seule|manuel\s*seul|bo[iî]te\s*et\s*notice|emballage\s*seul|"
    r"packaging\s*only|scatola\s*vuota"
    r")\b"
)
_DSI_TITLE = re.compile(r"(?i)\bdsi(\s*xl)?\b")
_DS_OLD_GEN = frozenset({"3ds", "3ds_xl", "2ds", "2ds_xl", "ds", "ds_lite"})


@lru_cache(maxsize=2048)
def _cached_infer_category(item_id: str, title: str, brand: str):
    stub = CatalogItem(id=int(item_id) if item_id.isdigit() else 0, title=title, brand=brand)
    return infer_category(stub)


def _infer(item: CatalogItem):
    return _cached_infer_category(str(item.id), item.title or "", item.brand or "")


def risk_flags_title(title: str) -> List[str]:
    normalized = _normalize(title)
    return [name for name, pat in _TITLE_RISK if pat.search(normalized)]


def _ds_family_matches(required: str, actual: str) -> bool:
    if not required or actual == "unknown":
        return True
    if required == actual:
        return True
    if required == "new3ds" and actual == "new3ds_xl":
        return True
    return False


def strict_intent_active(
    intent: Optional[SearchIntent] = None,
    row: Optional[Dict[str, Any]] = None,
) -> bool:
    """Intent avec contrainte modèle/variante explicite."""
    if intent is not None:
        if (
            intent.required_ds_family
            or intent.required_phone_model_key
            or intent.require_all_patterns
            or intent.required_brand
            or intent.required_exclude_pro
            or intent.required_storage_gb
        ):
            return True
    if row:
        if row.get("required_ds_family") or row.get("required_phone_model_key"):
            return True
        if row.get("intent_exact_only") in (True, "on", "1", 1, "true"):
            return True
    return False


def row_matches_strict_intent(
    row: Dict[str, Any],
    intent: Optional[SearchIntent] = None,
) -> bool:
    """True si la ligne respecte l'intent strict (ou pas d'intent strict actif)."""
    if not strict_intent_active(intent, row):
        return True
    reasons = str(row.get("triage_reasons") or "")
    if "wrong_ds_generation" in reasons or "intent_mismatch" in reasons:
        return False

    req_ds = ""
    if intent and intent.required_ds_family:
        req_ds = str(intent.required_ds_family)
    elif row.get("required_ds_family"):
        req_ds = str(row["required_ds_family"])
    if req_ds:
        fam = str(row.get("ds_model_family") or "")
        if not fam or fam == "unknown":
            fam = infer_ds_model_family(row)
        if fam != "unknown" and not _ds_family_matches(req_ds, fam):
            return False

    req_phone = ""
    if intent and intent.required_phone_model_key:
        req_phone = str(intent.required_phone_model_key)
    elif row.get("required_phone_model_key"):
        req_phone = str(row["required_phone_model_key"])
    if req_phone:
        key = str(row.get("product_model_key") or "")
        if key and key not in ("", "unknown") and key != req_phone:
            return False
        if intent and not _phone_title_matches_intent(str(row.get("title") or ""), intent):
            return False

    if intent and intent.required_brand:
        if intent.required_brand.lower() not in str(row.get("title") or "").lower():
            return False

    if intent and intent.exclude_patterns:
        title = str(row.get("title") or "")
        if any(p.search(title) for p in intent.exclude_patterns):
            return False

    ps4_var = str(row.get("required_ps4_variant") or row.get("search_ps4_variant") or "")
    if not ps4_var and intent:
        blob = f"{intent.search_text or ''} {intent.raw or ''}".lower()
        if re.search(r"(?i)\bslim\b", blob):
            ps4_var = "slim"
        elif re.search(r"(?i)\bpro\b", blob):
            ps4_var = "pro"
    if ps4_var:
        variant = str(row.get("variant") or row.get("ps4_variant") or "").lower()
        if not variant:
            try:
                from .scoring import extract_listing_specs

                variant = str(extract_listing_specs(row, "console_ps4").get("variant") or "").lower()
            except Exception:
                variant = ""
        if variant and variant not in ("", "unknown", "standard") and variant != ps4_var:
            return False

    return True


def can_promote_doubtful_row(
    row: Dict[str, Any],
    *,
    intent: Optional[SearchIntent] = None,
    category: str = "",
) -> bool:
    """Promotion post-triage : intent strict + score composite minimum."""
    if not row_matches_strict_intent(row, intent):
        return False
    reasons = str(row.get("triage_reasons") or "")
    if any(
        x in reasons
        for x in (
            "wrong_ds_generation",
            "wrong_platform",
            "intent_mismatch",
            "intent_exclude",
            "wrong_category",
        )
    ):
        return False
    try:
        sc = float(row.get("score_composite") or row.get("final_score") or 0)
        rel = float(
            row.get("ai_title_relevance")
            or row.get("triage_confidence")
            or row.get("item_type_conf")
            or 0
        )
        if rel <= 1.0:
            rel *= 100.0
    except (TypeError, ValueError):
        return False
    cat = category or str(row.get("item_type") or "")
    thresh_sc, thresh_rel = PROMOTE_DOUBTFUL_MIN_SCORE, PROMOTE_DOUBTFUL_MIN_REL
    if cat == "projector":
        thresh_sc, thresh_rel = 55.0, 65.0
    elif cat in ("ac", "clim", "climatisation"):
        thresh_sc, thresh_rel = 78.0, 82.0
    return sc > thresh_sc and rel > thresh_rel


def demote_strict_intent_mismatches(
    rows: List[Dict[str, Any]],
    intent: Optional[SearchIntent] = None,
) -> int:
    """Retire des offres les lignes hors modèle quand l'intent est strict."""
    if not strict_intent_active(intent):
        return 0
    demoted = 0
    for r in rows:
        if row_matches_strict_intent(r, intent):
            continue
        prev = str(r.get("triage_decision") or "")
        if prev in ("keep", "doubtful", ""):
            r["triage_decision"] = "doubtful"
            tr = str(r.get("triage_reasons") or "")
            if "intent_mismatch_demote" not in tr:
                r["triage_reasons"] = f"{tr}|intent_mismatch_demote".strip("|")[:160]
            r["intent_mismatch"] = True
            r["deal_blocked"] = True
            try:
                r["best_deal_score"] = min(float(r.get("best_deal_score") or 99), 12.0)
            except (TypeError, ValueError):
                r["best_deal_score"] = 12.0
            demoted += 1
    return demoted


def _normalize_phone_title(title: str) -> str:
    return re.sub(r"(?i)i\s*-?\s*phone", "iphone", title or "")


def _phone_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    """Assouplit le triage telephone : modele + stockage suffisent."""
    if "phone" not in (intent.product_families or []):
        return False
    norm = _normalize_phone_title(title)
    blob = f"{intent.search_text or ''} {intent.raw or ''}".lower()
    if not re.search(r"(?i)\biphone\b", norm) and not re.search(r"(?i)i\s*-?\s*phone", title or ""):
        if not re.search(r"(?i)\b(galaxy|pixel|smartphone|samsung)\b", norm):
            return False
    qm = re.search(r"(?i)iphone\s*(\d{2})", blob)
    tm = re.search(r"(?i)iphone\s*(\d{2})", norm)
    if qm and tm and qm.group(1) != tm.group(1):
        return False
    if intent.required_exclude_pro or (
        re.search(r"(?i)iphone\s*\d{2}\b", blob) and not re.search(r"(?i)\b(pro|plus|max)\b", blob)
    ):
        if re.search(r"(?i)\b(pro\s*max|pro|plus|max)\b", norm):
            return False
    qs = re.search(r"(?i)\b(64|128|256|512|1024)\s*(go|gb)\b", blob)
    ts = re.search(r"(?i)\b(64|128|256|512|1024)\s*(go|gb)\b", norm)
    if qs and ts and qs.group(1) != ts.group(1):
        return False
    if intent.required_phone_model_key:
        try:
            from .engine import infer_phone_model

            key, _ = infer_phone_model({"title": norm})
            if key and key != "unknown" and key != intent.required_phone_model_key:
                return False
        except Exception:
            pass
    if qm or tm or re.search(r"(?i)\biphone\b", norm):
        return True
    return any(p.search(norm) for p in intent.must_match_any)


def _bicycle_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    if "bicycle" not in (intent.product_families or []):
        return False
    blob = f"{intent.search_text or ''} {intent.raw or ''}".lower()
    if re.search(r"(?i)\b(vélo|velo|bike|vtt|vtc|gravel|bmx|cadre)\b", title):
        return True
    if re.search(r"(?i)\b(vélo|velo|bike|vtt|vtc)\b", blob) and re.search(
        r"(?i)\b(taille|cm|carbone|shimano|sram|vae|électrique)\b", title
    ):
        return True
    return any(p.search(title) for p in intent.must_match_any)


def _projector_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    if "projector" not in (intent.product_families or []):
        return False
    try:
        from .scoring import has_projector_unit_signal, is_projector_accessory_text

        if is_projector_accessory_text(title):
            return False
        if has_projector_unit_signal(title):
            return True
    except Exception:
        pass
    if re.search(
        r"(?i)\b(projecteur|projector|epson|benq|optoma|lumens?|hdmi|vid[eé]o\s*proj|uhd\s*\d{2,3})\b",
        title,
    ):
        return True
    return any(p.search(title) for p in intent.must_match_any)


def _ac_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    if "ac" not in (intent.product_families or []):
        return False
    try:
        from .scoring import ac_has_unit_signal

        if ac_has_unit_signal(title):
            return True
    except Exception:
        pass
    if re.search(
        r"(?i)\b(climatiseur|climatisation|clim\s+mobile|clim\s+fixe|"
        r"monosplit|bi[\s-]?split|multi[\s-]?split|pompe\s+[àa]\s+chaleur|pac\b)\b",
        title,
    ):
        return True
    return any(p.search(title) for p in intent.must_match_any)


def _ac_off_category_title(title: str) -> bool:
    folded = _fold_accents(title or "")
    try:
        from .scoring import ac_has_unit_signal

        if ac_has_unit_signal(folded):
            return False
    except Exception:
        pass
    if _AC_OFF_CATEGORY.search(folded):
        return True
    if _AC_CLIM_SUBSTRING_NOISE.search(folded):
        return True
    if _FASHION_NOISE.search(folded):
        return True
    if re.search(r"(?i)\bclim\b", folded) and not re.search(
        r"(?i)\bclim(?:atiseur|atisation|mobile|fixe|r[eé]versible)\b", folded
    ):
        return True
    return False


def _apparel_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    fams = set(intent.product_families or [])
    if not fams & {"apparel", "shoes", "jeans"}:
        return False
    if re.search(r"(?i)\b(taille|teeshirt|t-?shirt|hoodie|jean|basket|nike|adidas)\b", title):
        return True
    if intent.required_brand and intent.required_brand.lower() in title.lower():
        return True
    return any(p.search(title) for p in intent.must_match_any)


def _console_ps4_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    if "console_ps4" not in (intent.product_families or []):
        return False
    if _PS4_FUZZY.search(title):
        return True
    if re.search(r"(?i)\b(sony|playstation)\b", title) and re.search(
        r"(?i)\b(console|slim|pro|playstation)\b", title
    ):
        return True
    st = (intent.search_text or intent.raw or "").strip().lower()
    if st and len(st) >= 2 and st.replace(" ", "") in title.lower().replace(" ", ""):
        return True
    return any(p.search(title) for p in intent.must_match_any)


def _category_title_matches_intent(title: str, intent: SearchIntent) -> bool:
    """Assouplit le triage pour eviter les faux intent_mismatch hors telephone."""
    if "phone" in (intent.product_families or []):
        return _phone_title_matches_intent(title, intent)
    if "console_ds" in (intent.product_families or []):
        if intent.required_ds_family:
            try:
                fam = infer_ds_model_family({"title": title})
                if fam != "unknown":
                    return _ds_family_matches(intent.required_ds_family, fam)
            except Exception:
                pass
            return False
        if re.search(r"(?i)\b(2ds|3ds|dsi|ds\s*lite|nintendo\s*ds)\b", title):
            return True
        try:
            fam = infer_ds_model_family({"title": title})
            if fam != "unknown":
                return True
        except Exception:
            pass
    if _console_ps4_title_matches_intent(title, intent):
        return True
    if _bicycle_title_matches_intent(title, intent):
        return True
    if _projector_title_matches_intent(title, intent):
        return True
    if _ac_title_matches_intent(title, intent):
        return True
    if _apparel_title_matches_intent(title, intent):
        return True
    return False


def _intent_mismatch(title: str, intent: SearchIntent) -> bool:
    norm_title = _normalize_phone_title(title)
    if intent.product_families and _category_title_matches_intent(title, intent):
        return False
    if "phone" in (intent.product_families or []):
        title = norm_title
    if intent.exclude_patterns and any(p.search(title) for p in intent.exclude_patterns):
        return True
    if intent.require_all_patterns and not all(p.search(title) for p in intent.require_all_patterns):
        return True
    if intent.required_brand:
        if intent.required_brand.lower() not in title.lower():
            return True
    if intent.required_ds_family and "console_ds" in intent.product_families:
        try:
            from .engine import infer_ds_model_family

            fam = infer_ds_model_family({"title": title})
            if fam != "unknown" and not _ds_family_matches(intent.required_ds_family, fam):
                return True
        except Exception:
            pass
        if _DSI_TITLE.search(title):
            return True
    if not intent.must_match_any:
        return False
    if any(p.search(title) for p in intent.must_match_any):
        return False
    if intent.product_families and re.search(
        r"(?i)\b(playstation|xbox|nintendo|console|clim|projecteur|projector|gaming)\b", title
    ):
        return False
    if len(title.split()) <= 3:
        return False
    return bool(intent.product_families)


def _title_is_effectively_empty(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if len(t) >= 4:
        return False
    if len(t) >= 3 and _PS4_INTENT_HINT.search(t):
        return False
    return len(t) < 3


def _console_ps4_wrong_platform_title(title: str) -> bool:
    if not _CONSOLE_PS4_WRONG_PLATFORM.search(title):
        return False
    if _PS4_FUZZY.search(title):
        return False
    return True


def triage_title(item: CatalogItem, intent: SearchIntent) -> TitleTriage:
    title = _title_text(item)
    if _title_is_effectively_empty(title):
        return TitleTriage(TitleDecision.REJECT, ["empty_title"], 0.95)

    plat = str(getattr(item, "platform", "") or "").lower()
    if plat in ("leboncoin", "lbc"):
        try:
            from .engine import enrich_catalog_item_delivery

            enrich_catalog_item_delivery(item)
        except Exception:
            pass
    elif not getattr(item, "hand_delivery_only", False):
        try:
            from .engine import infer_delivery_flags_from_text

            fl = infer_delivery_flags_from_text(
                getattr(item, "title", "") or "",
                getattr(item, "description", "") or "",
            )
            if fl.hand_delivery_only:
                item.hand_delivery_only = True
                item.delivery_mode = "hand_only"
                if getattr(item, "shipping_available", None) is None:
                    item.shipping_available = False
        except Exception:
            pass
    if getattr(item, "hand_delivery_only", False):
        return TitleTriage(TitleDecision.REJECT, ["hand_delivery_only"], 0.94)

    risks = risk_flags_title(title)
    critical = {
        "ban",
        "broken",
        "broken_hs",
        "for_parts",
        "not_working",
        "repair_only",
        "off_platform_payment",
    }
    hit = critical & set(risks)
    if hit:
        return TitleTriage(TitleDecision.REJECT, [f"risk:{','.join(sorted(hit))}"], 0.92)

    try:
        from .engine import triage_learning_hit

        _cat0 = intent.product_families[0] if intent.product_families else ""
        learned = triage_learning_hit(title, _cat0)
        if learned:
            return TitleTriage(TitleDecision.REJECT, [learned[0]], learned[1])
    except Exception:
        pass

    if "ac" in intent.product_families and _AC_WRONG_CATEGORY.search(
        _fold_accents(getattr(item, "title", "") or title)
    ):
        return TitleTriage(TitleDecision.REJECT, ["wrong_category_ac"], 0.96)

    if "ac" in intent.product_families:
        title_folded = _fold_accents(title)
        try:
            from .scoring import ac_has_unit_signal

            ac_unit = ac_has_unit_signal(title_folded)
        except Exception:
            ac_unit = bool(
                re.search(
                    r"(?i)\b(\d{4,6}\s*btu|climatiseur|clim\s+mobile|monosplit|split)\b",
                    title_folded,
                )
            )
        if _AC_OFF_CATEGORY.search(title_folded) and not ac_unit:
            return TitleTriage(TitleDecision.REJECT, ["wrong_category_ac"], 0.95)
        if _AC_CLIM_SUBSTRING_NOISE.search(title_folded) and not ac_unit:
            return TitleTriage(TitleDecision.REJECT, ["clim_substring_noise"], 0.94)
        if _FASHION_NOISE.search(title_folded) and not ac_unit:
            return TitleTriage(TitleDecision.REJECT, ["fashion_noise"], 0.93)
        if not ac_unit and not _ac_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.REJECT, ["not_ac"], 0.90)
        if ac_unit or _ac_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["ac_unit_signal"], 0.88)

    if intent.preflight_flags.get("require_console_unit", True) and _EMPTY_PACKAGING_ONLY.search(title):
        if not re.search(r"(?i)\b(console\s+[\w]|avec\s+console|inclu[ae]\s+console)\b", title):
            return TitleTriage(TitleDecision.REJECT, ["empty_packaging"], 0.93)

    if re.search(r"(?i)^\s*bo[iî]te\b", title) and not re.search(
        r"(?i)\b(avec\s+console|console\s+inclu|inclu[ae]\s+console)\b", title
    ):
        return TitleTriage(TitleDecision.REJECT, ["empty_packaging"], 0.93)

    if re.search(
        r"(?i)^(cover\s*plate|plaque|stylet|stylus|coque|housse|chargeur|écran|ecran|screen)\b",
        title,
    ):
        acc_k = "plate" if re.search(r"(?i)plate|plaque", title) else "head_noun"
        return TitleTriage(TitleDecision.REJECT, [f"accessory:{acc_k}"], 0.91)

    if "console_ds" in intent.product_families and _CONSOLE_DS_NOISE.search(title):
        if not title_has_main_console_unit(title):
            return TitleTriage(TitleDecision.REJECT, ["console_ds_noise"], 0.91)

    if "console_ds" in intent.product_families and re.search(
        r"(?i)\b(jeu(x)?\s+seul|game\s+only|cartouche\s+seule|lot\s+de\s+\d+\s+jeux)\b", title
    ):
        if not title_has_main_console_unit(title):
            return TitleTriage(TitleDecision.REJECT, ["game_only_title"], 0.92)

    if "console_ps4" in intent.product_families:
        if _console_ps4_wrong_platform_title(title):
            return TitleTriage(TitleDecision.REJECT, ["wrong_platform"], 0.94)
        if _CONSOLE_PS4_REJECT.search(title):
            return TitleTriage(TitleDecision.REJECT, ["wrong_category"], 0.94)
        if re.search(r"(?i)^\s*console\s*$", title) or (
            re.search(r"(?i)\bconsole\b", title)
            and not _console_ps4_wrong_platform_title(title)
            and not _ACCESSORY_ONLY_TITLE.search(title)
        ):
            if _console_ps4_title_matches_intent(title, intent) or re.search(
                r"(?i)^\s*console\s*$", title
            ):
                return TitleTriage(TitleDecision.KEEP, ["ps4_console_generic"], 0.78)
        if _PS4_GAME_LOT.search(title) and not title_has_main_console_unit(title):
            return TitleTriage(TitleDecision.REJECT, ["game_only_title"], 0.93)
        if re.search(r"(?i)\bjeu(x)?\s+seul\b", title) and not title_has_main_console_unit(title):
            return TitleTriage(TitleDecision.REJECT, ["game_only_title"], 0.92)
        if _console_ps4_title_matches_intent(title, intent) and not _ACCESSORY_ONLY_TITLE.search(title):
            return TitleTriage(TitleDecision.KEEP, ["ps4_title_match"], 0.86)

    if re.search(r"(?i)\b(game|jeu|cartouche|cartridge)\b", title) and not re.search(
        r"(?i)\b(console|bundle|pack|lot)\b", title
    ):
        if _GAME_ONLY_TITLE.search(title) or not title_has_main_console_unit(title):
            return TitleTriage(TitleDecision.REJECT, ["game_only_title"], 0.90)

    try:
        _price = float(getattr(item, "total_or_price", None) or getattr(item, "price", None) or 0)
    except (TypeError, ValueError):
        _price = 0.0
    if (
        _price > 0
        and _price < 35
        and "console_ds" in intent.product_families
        and title_has_main_console_unit(title)
        and not re.search(r"(?i)\bconsole\b", title)
    ):
        return TitleTriage(TitleDecision.REJECT, ["accessory:low_price_unit_name"], 0.89)

    if intent.exclude_patterns and any(p.search(title) for p in intent.exclude_patterns):
        return TitleTriage(TitleDecision.REJECT, ["intent_exclude"], 0.91)

    if intent.required_ds_family and "console_ds" in intent.product_families:
        try:
            from .engine import infer_ds_model_family

            fam = infer_ds_model_family({"title": title})
            if fam != "unknown" and not _ds_family_matches(intent.required_ds_family, fam):
                return TitleTriage(TitleDecision.REJECT, [f"wrong_ds_generation:{fam}"], 0.93)
        except Exception:
            pass
        if _DSI_TITLE.search(title):
            return TitleTriage(TitleDecision.REJECT, ["wrong_platform_dsi"], 0.92)

    if intent.require_all_patterns and not all(p.search(title) for p in intent.require_all_patterns):
        return TitleTriage(TitleDecision.REJECT, ["intent_require_missing"], 0.90)

    if intent.required_brand and intent.required_brand.lower() not in title.lower():
        if "apparel" in intent.product_families or "shoes" in intent.product_families:
            return TitleTriage(TitleDecision.DOUBTFUL, ["brand_mismatch"], 0.55)

    if _PARTS_ONLY.search(title):
        return TitleTriage(TitleDecision.REJECT, ["parts_only"], 0.90)

    if "projector" in intent.product_families and re.search(
        r"(?i)\b(housse\s*seule|sac\s*seul|étui\s*seul|support\s*seul|tripod\s*only)\b", title
    ):
        return TitleTriage(TitleDecision.REJECT, ["projector_accessory"], 0.88)

    if "projector" in intent.product_families:
        try:
            from .scoring import is_projector_accessory_text

            if is_projector_accessory_text(title):
                return TitleTriage(TitleDecision.REJECT, ["projector_accessory"], 0.92)
        except Exception:
            pass

    if _GAME_ONLY_TITLE.search(title) and not re.search(
        r"(?i)\b(console|bundle|lot|pack)\b", title
    ):
        return TitleTriage(TitleDecision.REJECT, ["game_only_title"], 0.90)

    cat_hint = (intent.product_families[0] if intent.product_families else "")
    acc = detect_accessory(
        item, search_hint=intent.raw, title_only=True, market_category=cat_hint
    )
    if acc.is_accessory:
        ac_unit_override = False
        if "ac" in intent.product_families:
            try:
                from .scoring import ac_has_unit_signal

                ac_unit_override = ac_has_unit_signal(title)
            except Exception:
                ac_unit_override = False
        if not ac_unit_override:
            if re.match(
                r"(?i)^(mando|manette|controller|volant|jeu|game|juego|chargeur|c[aâ]ble|coque|housse)\b",
                title.strip(),
            ) and not re.search(r"(?i)\b(console|bundle|lot|pack)\b", title):
                return TitleTriage(TitleDecision.REJECT, [f"accessory:{acc.kind or acc.reason}"], 0.90)
            phone_ctx = cat_hint == "phone" or re.search(
                r"(?i)\b(iphone|galaxy|pixel|smartphone)\b", title
            )
            if phone_ctx:
                price = getattr(item, "total_or_price", None) or getattr(item, "price", None)
                if acc.pack_hints or (
                    re.search(r"(?i)^\s*(apple\s+)?iphone", title)
                    and price is not None
                    and float(price) >= 80
                ):
                    return TitleTriage(TitleDecision.KEEP, ["phone_pack"], 0.84)
            if title_has_main_console_unit(title) and not _ACCESSORY_ONLY_TITLE.search(title):
                return TitleTriage(
                    TitleDecision.KEEP,
                    [f"console_pack:{acc.kind or acc.reason}"],
                    0.84,
                )
            return TitleTriage(TitleDecision.REJECT, [f"accessory:{acc.kind or acc.reason}"], 0.88)

    if "projector" in intent.product_families:
        if _WRONG_CATEGORY_NOT_VIDEO.search(title):
            return TitleTriage(TitleDecision.REJECT, ["wrong_category"], 0.96)
        if _FASHION_NOISE.search(title):
            return TitleTriage(TitleDecision.REJECT, ["fashion_noise"], 0.9)
        try:
            from .scoring import has_projector_signal

            if not has_projector_signal({"title": title}):
                return TitleTriage(TitleDecision.REJECT, ["not_projector"], 0.82)
        except Exception:
            if not re.search(
                r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|lumens?|lumen|"
                r"epson|benq|optoma|viewsonic|xga|full\s*hd|4k|uhd\s*\d{2,3}|hdmi|thrown|throw)\b",
                title,
            ):
                return TitleTriage(TitleDecision.REJECT, ["not_projector"], 0.82)
    elif "ac" in intent.product_families:
        if _ac_off_category_title(title):
            return TitleTriage(TitleDecision.REJECT, ["wrong_category_ac"], 0.94)
        if _ac_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["ac_intent_match"], 0.86)
        return TitleTriage(TitleDecision.REJECT, ["not_ac"], 0.88)
    elif _intent_mismatch(title, intent):
        if intent.required_ds_family or intent.exclude_patterns or intent.require_all_patterns:
            return TitleTriage(TitleDecision.REJECT, ["intent_mismatch"], 0.88)
        st = (intent.search_text or intent.raw or "").strip().lower()
        if st and len(st) >= 6 and st in title.lower():
            return TitleTriage(TitleDecision.KEEP, ["strong_title_match"], 0.86)
        return TitleTriage(TitleDecision.DOUBTFUL, ["intent_mismatch"], 0.52)

    if "console_ps4" in intent.product_families and _console_ps4_wrong_platform_title(title):
        return TitleTriage(TitleDecision.REJECT, ["wrong_platform"], 0.90)
    if "ps4" in intent.platform_hints:
        if re.search(r"(?i)\b(ps[23]|playstation\s*[23]|psp)\b", title) and not re.search(r"(?i)\bps4\b", title):
            return TitleTriage(TitleDecision.REJECT, ["wrong_platform"], 0.90)

    if _GUIDE_MEDIA.search(title) and not _GUIDE_EXCEPT.search(title):
        return TitleTriage(TitleDecision.REJECT, ["guide_media"], 0.90)

    if _SCREEN_ISSUE_STRONG.search(title) and not _SCREEN_POSITIVE.search(title):
        return TitleTriage(TitleDecision.DOUBTFUL, ["screen_defect_mentioned"], 0.68)

    inf = _infer(item)
    if len(title) < _SHORT_TITLE and inf.category == "unknown":
        return TitleTriage(TitleDecision.DOUBTFUL, ["short_title"], 0.45)
    if _DOUBTFUL_HINT.search(title):
        if re.search(r"(?i)^\s*(apple\s+)?iphone\b", title):
            return TitleTriage(TitleDecision.KEEP, ["phone_bundle"], 0.82)
        if title_has_main_console_unit(title):
            return TitleTriage(TitleDecision.KEEP, ["console_bundle"], 0.82)
        if "projector" in intent.product_families:
            if _projector_title_matches_intent(title, intent):
                return TitleTriage(TitleDecision.KEEP, ["projector_bundle"], 0.84)
            if not re.search(
                r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|epson|benq|optoma|viewsonic|lumens?)\b",
                title,
            ):
                return TitleTriage(TitleDecision.DOUBTFUL, ["bundle_lot"], 0.50)
            return TitleTriage(TitleDecision.KEEP, ["projector_bundle"], 0.82)
        if _projector_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["projector_bundle"], 0.82)
        return TitleTriage(TitleDecision.DOUBTFUL, ["bundle_lot"], 0.50)
    if inf.category == "unknown" and intent.product_families:
        if "console_ps4" in intent.product_families and _console_ps4_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["ps4_title_match"], 0.82)
        if "projector" in intent.product_families and _projector_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["projector_in_title"], 0.82)
        if title_has_main_console_unit(title) and not _ACCESSORY_ONLY_TITLE.search(title):
            return TitleTriage(TitleDecision.KEEP, ["console_in_title"], 0.78)
        if "phone" in intent.product_families and _phone_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["phone_intent_match"], 0.84)
        if "ac" in intent.product_families and _ac_title_matches_intent(title, intent):
            return TitleTriage(TitleDecision.KEEP, ["ac_in_title"], 0.84)
        return TitleTriage(TitleDecision.DOUBTFUL, ["unknown_category"], 0.55)
    if (
        inf.category
        and intent.product_families
        and inf.category not in intent.product_families
        and inf.confidence < 0.55
    ):
        return TitleTriage(TitleDecision.DOUBTFUL, [f"weak_cat:{inf.category}"], 0.50)

    return TitleTriage(TitleDecision.KEEP, ["clear_title"], 0.85)


def triage_full_text(
    item: CatalogItem,
    intent: SearchIntent,
    title_result: Optional[TitleTriage] = None,
) -> TitleTriage:
    base = title_result if title_result is not None else triage_title(item, intent)
    if base.decision == TitleDecision.REJECT:
        return base

    flags = risk_flags(item)
    critical = {"ban", "broken", "for_parts", "not_working", "repair_only", "no_power", "off_platform_payment"}
    names = {str(f.get("flag") or "") for f in flags}
    if critical & names:
        return TitleTriage(TitleDecision.REJECT, [f"risk_full:{','.join(sorted(critical & names))}"], 0.93)

    cat_hint = (intent.product_families[0] if intent.product_families else "")
    acc = detect_accessory(
        item, search_hint=intent.raw, title_only=False, market_category=cat_hint
    )
    if acc.is_accessory:
        return TitleTriage(TitleDecision.REJECT, [f"accessory_full:{acc.kind}"], 0.90)

    if base.decision == TitleDecision.DOUBTFUL:
        return TitleTriage(TitleDecision.KEEP, ["confirmed_after_desc"], 0.80)
    return base


def run_title_triage(items: List[CatalogItem], intent: SearchIntent) -> TriageBatch:
    batch = TriageBatch()
    for it in items:
        if not it.id:
            continue
        sid = str(it.id)
        tr = triage_title(it, intent)
        batch.by_id[sid] = tr
        if tr.decision == TitleDecision.REJECT:
            batch.reject_ids.add(sid)
        elif tr.decision == TitleDecision.DOUBTFUL:
            batch.doubtful_ids.add(sid)
        else:
            batch.keep_ids.add(sid)
    logger.info(
        "Triage : %d → keep %d, doubtful %d, reject %d",
        len(items),
        len(batch.keep_ids),
        len(batch.doubtful_ids),
        len(batch.reject_ids),
    )
    return batch


def filter_ids_heuristic(items: List[CatalogItem], intent: str = "") -> Set[str]:
    si = parse_search_intent(intent)
    batch = run_title_triage(items, si)
    return batch.working_ids

