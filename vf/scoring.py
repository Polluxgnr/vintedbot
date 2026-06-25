"""Scoring acheteur : extraction specs, composite, veto technique, top 5 et badges deal."""

# --- pricing.py ---

import re
from typing import Any, Dict, Optional, Tuple

_HAND_DELIVERY = re.compile(
    r"(?i)\b(remise\s*en\s*main\s*propre|main\s*propre|retrait\s*chez|pickup|en\s*personne)\b"
)
_SHIPPING_MENTION = re.compile(
    r"(?i)\b(livraison|mondial\s*relay|colissimo|relais|envoi|port\s*inclus)\b"
)

# Catégories où le MSRP « neuf » n'est plus une référence fiable.
DISCONTINUED_CATEGORIES = frozenset(
    {
        "console_ds",
        "console_ps3",
        "console_ps2",
        "console_ps1",
        "console_wii",
        "console_wiiu",
        "console_gamecube",
        "console_3ds",
        "vinyl",
    }
)

DISCONTINUED_PREFIXES = ("console_ds",)


def is_discontinued_product(row: Dict[str, Any]) -> bool:
    cat = str(row.get("item_type") or row.get("market_category") or "").lower()
    if cat in DISCONTINUED_CATEGORIES:
        return True
    if any(cat.startswith(p) for p in DISCONTINUED_PREFIXES):
        return True
    try:
        yr = int(row.get("release_year") or row.get("model_release_year") or 0)
        if yr and yr < 2014 and cat.startswith("console_"):
            return True
    except (TypeError, ValueError):
        pass
    return False


def sane_reference_eur(msrp: Optional[float], price: float, *, discontinued: bool = False) -> Optional[float]:
    """Rejette les références oracle/MSRP aberrantes (ex. 2,9 € pour une 3DS)."""
    if not msrp or msrp <= 0 or price <= 0:
        return None
    if msrp < max(12.0, price * 0.22):
        return None
    if discontinued and msrp > price * 6.0:
        return None
    if msrp > price * 25.0:
        return None
    return float(msrp)


def vinted_buyer_protection_fee(price: float) -> float:
    """Protection acheteur Vinted : 5 % + 0,70 € (barème public)."""
    if price <= 0:
        return 0.0
    return round(price * 0.05 + 0.70, 2)


def estimate_shipping_eur(platform: str, row: Optional[Dict[str, Any]] = None) -> float:
    plat = (platform or "vinted").lower()
    text = ""
    if row:
        text = f"{row.get('title') or ''} {row.get('description') or ''}"
        try:
            ship = float(row.get("shipping_eur") or row.get("shipping_price") or 0)
            if ship > 0:
                return round(ship, 2)
        except (TypeError, ValueError):
            pass
    if _HAND_DELIVERY.search(text) and not _SHIPPING_MENTION.search(text):
        return 0.0
    if plat == "vinted":
        return 3.99
    if plat in ("leboncoin", "lbc"):
        return 0.0 if _HAND_DELIVERY.search(text) else 4.50
    if plat == "ebay":
        return 5.50
    if plat == "wallapop":
        return 3.50
    return 0.0


def calculate_landed_cost(
    price: float,
    platform: str,
    *,
    row: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """
    Coût total estimé pour l'acheteur (prix affiché + frais).
    """
    list_price = max(0.0, float(price or 0))
    plat = (platform or "vinted").lower()
    buyer_fee = 0.0
    shipping = 0.0
    if plat == "vinted":
        buyer_fee = vinted_buyer_protection_fee(list_price)
        shipping = estimate_shipping_eur(plat, row)
    elif plat in ("leboncoin", "lbc"):
        shipping = estimate_shipping_eur(plat, row)
    elif plat == "ebay":
        shipping = estimate_shipping_eur(plat, row)
    elif plat == "wallapop":
        shipping = estimate_shipping_eur(plat, row)
    landed = round(list_price + buyer_fee + shipping, 2)
    return {
        "list_price_eur": round(list_price, 2),
        "buyer_fees_eur": round(buyer_fee, 2),
        "shipping_eur": round(shipping, 2),
        "landed_cost_eur": landed,
    }


def enrich_row_landed_cost(row: Dict[str, Any]) -> None:
    try:
        price = float(row.get("list_price_eur") or row.get("price_eur") or row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        try:
            price = float(row.get("total_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
    if price <= 0:
        return
    plat = str(row.get("platform") or "vinted")
    costs = calculate_landed_cost(price, plat, row=row)
    row.update(costs)
    row["total_price"] = costs["landed_cost_eur"]
    row["total_eur"] = costs["landed_cost_eur"]
    if costs["buyer_fees_eur"] > 0 or costs["shipping_eur"] > 0:
        row["landed_cost_note"] = (
            f"{costs['list_price_eur']:.0f}€ + "
            f"{costs['buyer_fees_eur']:.2f}€ prot. + "
            f"{costs['shipping_eur']:.2f}€ port"
        )[:120]


def effective_price_eur(row: Dict[str, Any]) -> float:
    """Prix à utiliser pour scoring / comparaisons."""
    for k in ("landed_cost_eur", "total_price", "total_eur", "price_eur", "price"):
        try:
            v = float(row.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def seller_trust_adjustment(row: Dict[str, Any]) -> Tuple[float, str, str]:
    """
    Malus/bonus selon profil vendeur.
    Retourne (score_adj, label_ui, risk_flag).
    """
    try:
        reviews = int(row.get("seller_feedback_count") or row.get("seller_feedback") or 0)
    except (TypeError, ValueError):
        reviews = 0
    try:
        rating = float(row.get("seller_rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    price = effective_price_eur(row)
    high_value = price >= 80.0

    if reviews == 0:
        adj = -18.0 if high_value else -10.0
        return adj, "Compte sans avis — prudence", "new_seller_high_risk"
    if reviews < 5:
        adj = -8.0 if high_value else -4.0
        return adj, f"Peu d'avis ({reviews}) — mode prudence", "low_trust_seller"
    if reviews >= 50 and rating >= 0.85:
        return 8.0, f"Vendeur fiable ({reviews} avis)", ""
    if reviews >= 15 and rating >= 0.75:
        return 3.0, "", ""
    if rating > 0 and rating < 0.55:
        return -10.0, "Note vendeur faible", "low_seller_rating"
    return 0.0, "", ""


def apply_seller_trust_to_row(row: Dict[str, Any]) -> None:
    adj, label, flag = seller_trust_adjustment(row)
    if adj:
        row["seller_trust_adj"] = round(adj, 2)
    if label:
        row["seller_trust_label"] = label
    if flag:
        rf = str(row.get("risk_flags") or "")
        if flag not in rf:
            row["risk_flags"] = f"{rf} {flag}".strip()
    if int(row.get("seller_feedback_count") or row.get("seller_feedback") or 0) == 0:
        try:
            price = effective_price_eur(row)
            if price >= 60:
                row["deal_capped"] = True
                sc = float(row.get("final_score") or 0)
                row["final_score"] = round(min(sc, 58.0), 2)
        except (TypeError, ValueError):
            pass


# --- analyze.py ---
import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .market import CatalogItem


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def item_title_text(item: CatalogItem) -> str:
    return _norm_text(item.title or "")


def item_text(item: CatalogItem) -> str:
    return _norm_text(" ".join([item.title or "", item.description or "", str(item.raw.get("description") or "")]))


# ---------------------------------------------------------------------------
# Category inference + lightweight spec extraction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InferredCategory:
    category: str
    confidence: float
    signals: str = ""


_CAT_RULES: List[Tuple[str, re.Pattern[str], float]] = [
    ("console_ds", re.compile(r"(?i)\b(new\s*nintendo\s*3ds|nintendo\s*3ds|3ds\s*xl|2ds)\b"), 3.5),
    ("console_ps4", re.compile(r"(?i)\b(ps4|playstation\s*4)\b"), 3.5),
    ("console_ps5", re.compile(r"(?i)\b(ps5|playstation\s*5)\b"), 3.5),
    ("console_xbox", re.compile(r"(?i)\b(xbox(\s*one|\s*series)?|series\s*[sx])\b"), 3.2),
    ("console_switch", re.compile(r"(?i)\b(nintendo\s*switch|switch\s*(oled|lite)?)\b"), 3.2),
    ("ac", re.compile(r"(?i)\b(clim(atisation)?|climatiseur|air\s*conditioner|portable\s*ac|pac)\b"), 3.2),
    ("projector", re.compile(r"(?i)\b(projecteur|projector|vid[ée]o\s*projecteur|home\s*cinema)\b"), 3.2),
    ("coffee", re.compile(r"(?i)\b(machine\s*[àa]\s*caf[ée]|cafeti[èe]re|nespresso|expresso|espresso|broyeur\s*caf[ée])\b"), 3.0),
    ("fan", re.compile(r"(?i)\b(ventilateur|fan\b|brasseur\s*d.?air|colonne\s*ventilateur|dyson\s*hot)\b"), 2.8),
    ("small_appliance", re.compile(r"(?i)\b(aspirateur|robot\s*aspirateur|purificateur|humidificateur|fer\s*[àa]\s*repasser)\b"), 2.4),
    ("apparel", re.compile(r"(?i)\b(t-?shirt|tee|sweat|hoodie|pull|veste|jean|pantalon|robe|jupe|chemise|gilet|maillot|pyjama|short)\b"), 1.6),
    ("shoes", re.compile(r"(?i)\b(basket(s)?|sneaker(s)?|chaussure(s)?|boots?|nike|adidas|puma|asics|salomon)\b"), 2.0),
    ("jeans", re.compile(r"(?i)\b(jean(s)?|denim|levi'?s|levis)\b"), 2.0),
    ("pc", re.compile(r"(?i)\b(pc\s*gamer|gaming\s*pc|ordinateur|ordinateur\s*portable|laptop|notebook|ryzen|intel\s*i[3579]|rtx\s*\d{3,4}|gtx\s*\d{3,4})\b"), 2.8),
    ("tent", re.compile(r"(?i)\b(tente|tent|camping|quechua|forclaz)\b"), 2.4),
    ("skates", re.compile(r"(?i)\b(roller(s)?|rollers|inline\s*skate(s)?|patin(s)?\s*[àa]\s*roulettes)\b"), 2.4),
    ("skate", re.compile(r"(?i)\b(skate(board)?|longboard|deck|truck(s)?|roue(s)?|wheels?|bearings?)\b"), 2.4),
    ("wearable", re.compile(r"(?i)\b(garmin|fenix|forerunner|venu|instinct|epix|suunto|polar|coros|amazfit|apple\s*watch)\b"), 3.4),
    ("phone", re.compile(r"(?i)\b(iphone(?!\s*watch)|samsung\s*galaxy|pixel\s*\d|smartphone|téléphone|telephone)\b"), 3.2),
    ("bicycle", re.compile(r"(?i)\b(v[ée]lo|vélo|bike|vtt|vtc|bicycle|gravel|route|cadre|roues?\s*(26|27\.5|29))\b"), 2.8),
    ("camera", re.compile(r"(?i)\b(appareil\s*photo|reflex|hybride|mirrorless|canon\s*eos|sony\s*alpha|nikon\s*z|fujifilm|objectif|bo[iî]tier)\b"), 2.8),
    ("audio", re.compile(r"(?i)\b(enceinte|amplificateur|ampli|casque|headphone|dac|platine\s*vinyle|turntable|enceinte\s*monitor)\b"), 2.6),
    ("laptop", re.compile(r"(?i)\b(macbook|thinkpad|laptop|ordinateur\s*portable|chromebook|asus\s*rog|dell\s*xps)\b"), 2.8),
    ("vinyl", re.compile(r"(?i)\b(vinyle|vinyl|lp\b|33\s*tours|disque\s*vinyle|record)\b"), 2.2),
    ("sports", re.compile(r"(?i)\b(running|trail|escalade|climbing|ski|snowboard|halt[èe]re|musculation)\b"), 2.0),
    # Avoid false positive on French word "fin" (e.g. "gilet fin").
    ("surf", re.compile(r"(?i)\b(surf(board)?|planche\s*de\s*surf|shortboard|longboard\s*surf|fins\b|ailerons?)\b"), 2.4),
]


def infer_category(item: CatalogItem) -> InferredCategory:
    text = item_text(item)
    if not text:
        return InferredCategory(category="unknown", confidence=0.0)
    scores: Dict[str, float] = {}
    hits: Dict[str, List[str]] = {}
    for cat, pat, w in _CAT_RULES:
        if pat.search(text):
            scores[cat] = scores.get(cat, 0.0) + w
            hits.setdefault(cat, []).append(pat.pattern[:42])
    if not scores:
        return InferredCategory(category="unknown", confidence=0.0)
    best = max(scores.items(), key=lambda kv: kv[1])
    total = sum(scores.values()) or 1.0
    conf = float(best[1] / total)
    return InferredCategory(category=best[0], confidence=round(conf, 3), signals="|".join(hits.get(best[0], []))[:160])


_LEGACY_VIDEO_PORTS = re.compile(
    r"(?i)\b(vga|rgb|d-?sub|s-?video|svideo|composite|dvi(?!\s*[-to])|m1-?da|rca|composante)\b"
)
_PROJECTOR_HDMI = re.compile(r"(?i)\bhdmi\b")
_NATIVE_43 = re.compile(r"(?i)\b(xga|1024\s*[x×]\s*768|4:3|format\s*4:3)\b")
_NATIVE_WVGA = re.compile(r"(?i)\b(wvga|854\s*[x×]\s*480)\b")
_NATIVE_169 = re.compile(r"(?i)\b(16:9|16/9|wuxga|1920\s*[x×]\s*1080|full\s*hd|1080p)\b")


def _enrich_projector_specs(text: str, out: Dict[str, Any]) -> None:
    """Specs techniques projecteur — connectique moderne, format natif, lumens crédibles."""
    m = re.search(r"(?i)\b(\d{1,5})\s*(ansi\s*)?lum(en)?s\b", text)
    if m:
        try:
            out["lumens"] = int(m.group(1))
        except ValueError:
            pass
    if _PROJECTOR_HDMI.search(text):
        out["has_hdmi"] = True
    if re.search(r"(?i)\b(sans\s*hdmi|no\s*hdmi|pas\s*d['\u2019]?hdmi)\b", text):
        out["missing_hdmi"] = True
    legacy_hits = _LEGACY_VIDEO_PORTS.findall(text)
    if legacy_hits and not out.get("has_hdmi"):
        out["legacy_ports_only"] = True
        out["missing_hdmi"] = True
    if re.search(r"(?i)\b(svga|800\s*[x×]\s*600)\b", text):
        out["native_aspect"] = "4:3"
        if not out.get("resolution"):
            out["resolution"] = "svga"
    if re.search(r"(?i)\b(4k|uhd)\b", text):
        out["claims_4k"] = True
    if re.search(r"(?i)\b(1080p|full\s*hd)\b", text):
        out["claims_1080p"] = True
    if re.search(r"(?i)\b(720p|hd\s*ready)\b", text):
        out["claims_720p"] = True
    if _NATIVE_WVGA.search(text):
        out["native_aspect"] = "4:3"
        out["resolution"] = "wvga"
    elif _NATIVE_43.search(text):
        out["native_aspect"] = "4:3"
        if not out.get("resolution"):
            out["resolution"] = "xga"
    if re.search(r"(?i)\b(wxga|1280\s*[x×]\s*800)\b", text):
        out["native_aspect"] = "16:10"
        out["resolution"] = "wxga"
        out["office_projector"] = True
    elif _NATIVE_169.search(text):
        out["native_aspect"] = "16:9"
    if out.get("claims_4k") and not out.get("resolution"):
        out["resolution"] = "4k"
    elif out.get("claims_1080p") and not out.get("resolution"):
        out["resolution"] = "1080p"
    elif out.get("claims_720p") and not out.get("resolution"):
        out["resolution"] = "720p"
    if re.search(r"(?i)\b(t[ée]l[ée]commande|remote)\b", text):
        out["has_remote"] = True
    if re.search(r"(?i)\b(sans\s*t[ée]l[ée]commande|no\s*remote)\b", text):
        out["missing_remote"] = True
    if re.search(
        r"(?i)\b(alimentation|chargeur|c[âa]ble\s*d.?alimentation|power\s*(cord|supply|cable)|adaptateur)\b",
        text,
    ):
        out["has_power_cable"] = True
    if re.search(r"(?i)\b(sans\s*alim|sans\s*chargeur|no\s*power)\b", text):
        out["missing_power"] = True
    if re.search(r"(?i)\b(bo[iî]te|box)\b", text):
        out["has_box"] = True
    for brand in (
        "epson",
        "benq",
        "optoma",
        "viewsonic",
        "sony",
        "nec",
        "infocus",
        "panasonic",
        "christie",
        "acer",
        "lg",
        "jvc",
    ):
        if re.search(rf"(?i)\b{brand}\b", text):
            out["projector_brand"] = brand
            break
    lum = int(out.get("lumens") or 0)
    if 0 < lum < 300:
        out["underpowered"] = True
    if lum >= 2500 and not re.search(r"(?i)\bansi\s*lum", text):
        out["inflated_lumens_claim"] = True
    elif lum >= 6000:
        out["inflated_lumens_claim"] = True


def extract_specs(item: CatalogItem, category: str) -> Dict[str, Any]:
    text = item_text(item)
    out: Dict[str, Any] = {}

    try:
        from .engine import merge_normalized_specs, normalize_specs_from_text

        merge_normalized_specs(out, normalize_specs_from_text(text, category))
    except Exception:
        pass

    # Common signals (work for any category)
    if re.search(r"(?i)\b(neuf|new\s*with(out)?\s*tags|nwt|jamais\s*port[ée]?)\b", text):
        out["condition_hint"] = "new"
    elif re.search(r"(?i)\b(tr[èe]s\s*bon|very\s*good)\b", text):
        out["condition_hint"] = "very_good"
    elif re.search(r"(?i)\b(bon\s*[ée]tat|good\s*condition)\b", text):
        out["condition_hint"] = "good"

    if re.search(r"(?i)\b(tache(s)?|stain(s)?|trou(x)?|hole(s)?|accroc|d[ée]chir[ée]e?|rip|pilling|bouloche(s)?)\b", text):
        out["defect_hint"] = True
    if re.search(r"(?i)\b(complet(e)?|compl[eè]te|full\s*set)\b", text):
        out["complete_hint"] = True

    if category in ("console_ds", "console_ps4", "console_ps5", "console_xbox", "console_switch"):
        # storage (PS4), model variants — spec_resolver déjà appliqué
        if not out.get("storage_gb"):
            m = re.search(r"(?i)\b(\d{3,4})\s*gb\b", text)
            if m:
                out["storage_gb"] = int(m.group(1))
        # PS4 CUH model codes
        cuh = re.search(r"(?i)\bCUH[-\s]?(\d{4})\b", text)
        if cuh:
            code = cuh.group(1)
            out["cuh"] = f"CUH-{code}"
            # Rough mapping by series
            if code.startswith(("10", "11", "12")):
                out["ps4_family"] = "fat"
                out["ps4_year_hint"] = "2013-2015"
            elif code.startswith("20"):
                out["ps4_family"] = "slim"
                out["ps4_year_hint"] = "2016+"
            elif code.startswith("70"):
                out["ps4_family"] = "pro"
                out["ps4_year_hint"] = "2016+"
        if re.search(r"(?i)\b(pro|ps4\s*pro)\b", text):
            out["variant"] = "pro"
        elif re.search(r"(?i)\b(slim)\b", text):
            out["variant"] = "slim"
        elif category == "console_ps4":
            # Some listings mention FAT explicitly
            if re.search(r"(?i)\b(fat)\b", text):
                out["variant"] = "fat"
            elif out.get("ps4_family") in ("fat", "slim", "pro"):
                out["variant"] = str(out.get("ps4_family"))
        elif category == "console_ds":
            if re.search(r"(?i)\b(xl)\b", text):
                out["variant"] = "xl"
            elif re.search(r"(?i)\b(new\s*nintendo\s*3ds)\b", text):
                out["variant"] = "new"
            if re.search(r"(?i)\b(stylet|stylus)\b", text):
                out["has_stylus"] = True
            elif re.search(r"(?i)\b(sans\s*stylet|no\s*stylus)\b", text):
                out["missing_stylus"] = True
            if re.search(r"(?i)\b(\d+\s*jeu(x)?|jeu(x)?\s*inclus|avec\s*jeu(x)?|cartouche)\b", text):
                out["includes_games"] = True
            if re.search(
                r"(?i)\b(point\s*noir|pixel\s*mort|dead\s*pixel|tache|rayure|fissure|"
                r"[ée]cran\s+(ab[iî]m|cass|hs|d[ée]fect)|top\s*screen)\b",
                text,
            ):
                out["screen_defect"] = True
                out["defect_hint"] = True
        if re.search(r"(?i)\b(complet(e)?|bo[iî]te|chargeur|notice)\b", text):
            out["complete_hint"] = True
        if re.search(r"(?i)\b(sans\s*chargeur|no\s*charger)\b", text):
            out["missing_charger"] = True
        if re.search(r"(?i)\b(manette|controller|mando|comando|dual\s*shock|joypad)\b", text):
            out["has_controller"] = True
        if re.search(r"(?i)\b(hdmi)\b", text):
            out["has_hdmi"] = True
        elif re.search(r"(?i)\b(sans\s*hdmi|no\s*hdmi)\b", text):
            out["missing_hdmi"] = True
        if re.search(r"(?i)\b(alimentation|chargeur|power\s*supply|psu)\b", text):
            out["has_power_cable"] = True
        elif re.search(r"(?i)\b(sans\s*chargeur|sans\s*alim|no\s*power|no\s*psu)\b", text):
            out["missing_power"] = True
        if re.search(r"(?i)\b(bo[iî]te|box|scatola)\b", text):
            out["has_box"] = True

    if category == "ac":
        # BTU / cooling power
        btu = re.search(r"(?i)\b(\d{4,6})\s*btu\b", text)
        if btu:
            out["btu"] = int(btu.group(1))
        # kW (cooling)
        kw = re.search(r"(?i)\b(\d(?:[.,]\d)?)\s*kw\b", text)
        if kw:
            try:
                out["cooling_kw"] = float(kw.group(1).replace(",", "."))
            except Exception:
                pass
        # hose / window kit / remote
        if re.search(r"(?i)\b(tuyau|gaine|hose|exhaust\s*hose)\b", text):
            out["has_hose"] = True
        if re.search(r"(?i)\b(kit\s*fen[eê]tre|window\s*kit)\b", text):
            out["has_window_kit"] = True
        if re.search(r"(?i)\b(t[ée]l[ée]commande|remote)\b", text):
            out["has_remote"] = True
        if re.search(r"(?i)\b(sans\s*tuyau|sans\s*gaine|no\s*hose)\b", text):
            out["missing_hose"] = True
        if re.search(r"(?i)\b(sans\s*t[ée]l[ée]commande|no\s*remote)\b", text):
            out["missing_remote"] = True
        # noise
        db = re.search(r"(?i)\b(\d{2})\s*dba?\b", text)
        if db:
            out["noise_db"] = int(db.group(1))
        # energy class
        ec = re.search(r"(?i)\b(a\+\+|a\+|a|b|c)\b", text)
        if ec:
            out["energy_class"] = ec.group(1).upper()

    if category == "projector":
        _enrich_projector_specs(text, out)

    if category == "bicycle":
        if re.search(r"(?i)\b(vtt|mountain|enduro|downhill)\b", text):
            out["bike_type"] = "vtt"
        elif re.search(r"(?i)\b(vtc|gravel|route|road)\b", text):
            out["bike_type"] = "vtc" if "vtc" in text.lower() else "route"
        elif re.search(r"(?i)\b([ée]lectrique|e-?bike|ebike|vae)\b", text):
            out["bike_type"] = "electric"
        m = re.search(r"(?i)\b(26|27\.5|29)\s*[\"″]?\b", text)
        if m:
            out["wheel_size_in"] = m.group(1)
        fs = re.search(r"(?i)\b(?:taille\s*)?(\d{2})\s*(cm|''|″)\b", text)
        if fs:
            out["frame_size_cm"] = int(fs.group(1))
        for brand in ("trek", "specialized", "cube", "decathlon", "giant", "cannondale", "scott"):
            if re.search(rf"(?i)\b{brand}\b", text):
                out["brand"] = brand.capitalize()
                break
        if re.search(r"(?i)\b(bosch|yamaha|shimano\s*steps|bafang)\b", text):
            out["motor_brand"] = True

    if category == "camera":
        if re.search(r"(?i)\b(full\s*frame|plein\s*format)\b", text):
            out["sensor"] = "full_frame"
        elif re.search(r"(?i)\b(aps-?c|aps\s*c)\b", text):
            out["sensor"] = "aps_c"
        elif re.search(r"(?i)\b(micro\s*4/3|m43|m4/3)\b", text):
            out["sensor"] = "m43"
        if re.search(r"(?i)\b(bo[iî]tier\s*seul|body\s*only)\b", text):
            out["body_only"] = True
        elif re.search(r"(?i)\b(kit|objectif|lens|18-55|24-70)\b", text):
            out["with_lens"] = True
        sc = re.search(r"(?i)\b(\d{1,3})\s*(k\s*)?(clics|shutter|actuations)\b", text)
        if sc:
            out["shutter_count"] = int(sc.group(1)) * (1000 if sc.group(2) else 1)
        for brand in ("sony", "canon", "nikon", "fujifilm", "panasonic", "olympus"):
            if re.search(rf"(?i)\b{brand}\b", text):
                out["brand"] = brand.capitalize()
                break

    if category == "audio":
        if re.search(r"(?i)\b(casque|headphone|headset)\b", text):
            out["audio_type"] = "headphones"
        elif re.search(r"(?i)\b(enceinte|speaker|monitor)\b", text):
            out["audio_type"] = "speaker"
        elif re.search(r"(?i)\b(ampli|amplificateur|amp)\b", text):
            out["audio_type"] = "amplifier"
        elif re.search(r"(?i)\b(dac)\b", text):
            out["audio_type"] = "dac"
        elif re.search(r"(?i)\b(platine|turntable|vinyle)\b", text):
            out["audio_type"] = "turntable"
        imp = re.search(r"(?i)\b(\d{2,3})\s*(ohm|Ω)\b", text)
        if imp:
            out["impedance_ohm"] = int(imp.group(1))

    if category == "laptop":
        if re.search(r"(?i)\bmacbook\b", text):
            out["brand"] = "Apple"
            m = re.search(r"(?i)\b(m[123](?:\s*pro|\s*air)?)\b", text)
            if m:
                out["variant"] = m.group(1).upper().replace(" ", "")
        elif re.search(r"(?i)\bthinkpad\b", text):
            out["brand"] = "Lenovo"
        ram = re.search(r"(?i)\b(\d{1,2})\s*go\s*ram\b", text)
        if ram:
            out["ram_gb"] = int(ram.group(1))
        stor = re.search(r"(?i)\b(\d{3,4})\s*go\b", text)
        if stor:
            out["ssd_gb"] = int(stor.group(1))

    if category == "vinyl":
        if re.search(r"(?i)\b(neuf\s*sous\s*blister|ss|sealed|mint\s*\(m\))\b", text):
            out["vinyl_grade"] = "M"
        elif re.search(r"(?i)\b(near\s*mint|nm|quasi\s*neuf)\b", text):
            out["vinyl_grade"] = "NM"
        elif re.search(r"(?i)\b(very\s*good|vg\+?)\b", text):
            out["vinyl_grade"] = "VG+"
        if re.search(r"(?i)\b(r[ée]edition|r[ée]press|repress)\b", text):
            out["pressing_type"] = "repress"

    if category in ("bicycle", "camera", "audio", "laptop", "vinyl", "sports"):
        try:
            from .engine import extract_specs_tier2, load_category_schema

            schema = load_category_schema(category)
            if schema:
                row_stub = {"title": item.title, "description": item.description, **out}
                out.update(extract_specs_tier2(row_stub, schema))
        except Exception:
            pass

    if category == "pc":
        # RAM
        ram = re.search(r"(?i)\b(\d{1,2})\s*gb\s*(ram)?\b", text)
        if ram:
            try:
                out["ram_gb"] = int(ram.group(1))
            except Exception:
                pass
        # Storage (SSD/HDD)
        ssd = re.search(r"(?i)\b(\d{3,4})\s*gb\s*ssd\b", text) or re.search(r"(?i)\b(\d)\s*tb\s*ssd\b", text)
        if ssd:
            try:
                out["ssd_gb"] = int(ssd.group(1)) if len(ssd.group(1)) >= 2 else int(ssd.group(1)) * 1024
            except Exception:
                pass
        hdd = re.search(r"(?i)\b(\d)\s*tb\s*hdd\b", text) or re.search(r"(?i)\b(\d{3,4})\s*gb\s*hdd\b", text)
        if hdd:
            try:
                out["hdd_gb"] = int(hdd.group(1)) if len(hdd.group(1)) >= 2 else int(hdd.group(1)) * 1024
            except Exception:
                pass
        # CPU / GPU (keep as strings)
        cpu = re.search(r"(?i)\b(i[3579]-?\d{3,5}[a-z]{0,2}|ryzen\s*[3579]\s*\d{3,4}[a-z]{0,2})\b", text)
        if cpu:
            out["cpu"] = cpu.group(1).lower()
        gpu = re.search(r"(?i)\b(rtx\s*\d{3,4}|gtx\s*\d{3,4}|rx\s*\d{3,4})\b", text)
        if gpu:
            out["gpu"] = gpu.group(1).lower().replace(" ", "")
        # Screen size for laptops
        inch = re.search(r"(?i)\b(1[0-9](?:[.,]\d)?)\s*(?:\"|inch)\b", text)
        if inch:
            try:
                out["screen_in"] = float(inch.group(1).replace(",", "."))
            except Exception:
                pass

    if category == "tent":
        # Capacity (2p/3p/4p)
        cap = re.search(r"(?i)\b([2-8])\s*(p|places?|person(ne)?s?)\b", text)
        if cap:
            try:
                out["capacity_p"] = int(cap.group(1))
            except Exception:
                pass
        if re.search(r"(?i)\b(2\s*saisons?|3\s*saisons?|4\s*saisons?|four\s*season)\b", text):
            m = re.search(r"(?i)\b([234])\s*saisons?\b", text)
            if m:
                out["season"] = f"{m.group(1)}-season"
            elif re.search(r"(?i)\bfour\s*season\b", text):
                out["season"] = "4-season"
        if re.search(r"(?i)\b(imperm[ée]able|waterproof|mm\s*h2o)\b", text):
            out["waterproof_hint"] = True

    if category == "skates":
        # Shoe size + wheel size
        if re.search(r"(?i)\b(taille|size|eu)\b", text):
            m = re.search(r"(?i)\b(?:eu\s*)?(\d{2})(?:\.?\d)?\b", text)
            if m:
                out["size_eu"] = int(m.group(1))
        wh = re.search(r"(?i)\b(\d{2,3})\s*mm\b", text)
        if wh:
            try:
                out["wheel_mm"] = int(wh.group(1))
            except Exception:
                pass

    if category in ("shoes", "jeans"):
        # sizes
        m = re.search(r"(?i)\b(?:eu\s*)?(\d{2})(?:\.?\d)?\b", text)
        if category == "shoes":
            # avoid catching years/prices: require "taille" or shoe words nearby
            if re.search(r"(?i)\b(taille|size|eu)\b", text) and m:
                out["size_eu"] = int(m.group(1))
        if category == "jeans":
            wl = re.search(r"(?i)\bW\s*(\d{2})\b.*\bL\s*(\d{2})\b", text)
            if wl:
                out["waist_w"] = int(wl.group(1))
                out["length_l"] = int(wl.group(2))

    if category == "phone":
        from .engine import infer_phone_model, infer_storage_gb

        key, label = infer_phone_model({"title": item.title, "description": item.description or ""})
        if key != "unknown":
            out["product_model_key"] = key
            out["product_model_label"] = label
            out["ai_model_exact"] = label
        st = infer_storage_gb({"title": item.title, "description": item.description or ""})
        if st:
            out["storage_gb"] = st
        if re.search(r"(?i)\b(d[ée]bloqu[ée]|unlocked|sim\s*free)\b", text):
            out["phone_unlocked"] = True
        if re.search(r"(?i)\b(icloud|find\s*my|verrouill[ée]e?)\b", text):
            out["icloud_locked"] = True
        if re.search(r"(?i)\b([ée]cran|display).{0,20}(cass|fissur|rayure|crack)\b", text):
            out["screen_defect"] = True
        bm = re.search(r"(?i)\b(batterie|battery)\s*(\d{2,3})\s*%", text)
        if bm:
            try:
                out["battery_health_pct"] = int(bm.group(2))
            except ValueError:
                pass

    if category == "wearable":
        from .engine import infer_wearable_model

        key, label = infer_wearable_model({"title": item.title, "description": item.description or ""})
        if key != "unknown":
            out["product_model_key"] = key
            out["product_model_label"] = label
            out["ai_model_exact"] = label
        if re.search(r"(?i)\b(gps|gnss)\b", text):
            out["has_gps"] = True
        if re.search(r"(?i)\b(carte|maps|topo)\b", text):
            out["has_maps"] = True
        mm = re.search(r"(?i)\b(\d{2})\s*mm\b", text)
        if mm:
            try:
                out["watch_size_mm"] = int(mm.group(1))
            except ValueError:
                pass

    if category in ("skate", "surf"):
        if re.search(r"(?i)\b(lot|bundle|pack)\b", text):
            out["bundle_hint"] = True
        dim = re.search(r"(?i)\b(\d{2,3}(?:[.,]\d)?)\s*(cm|\")\b", text)
        if dim:
            out["dimension"] = dim.group(0)

    if category == "apparel":
        # Materials
        if re.search(r"(?i)\b(coton|cotton)\b", text):
            out["material"] = "cotton"
        if re.search(r"(?i)\b(laine|wool|merino)\b", text):
            out["material"] = "wool"
        if re.search(r"(?i)\b(lin|linen)\b", text):
            out["material"] = "linen"
        if re.search(r"(?i)\b(cuir|leather)\b", text):
            out["material"] = "leather"
        # Size signals
        m = re.search(r"(?i)\b(xxs|xs|s|m|l|xl|xxl)\b", text)
        if m:
            out["size_alpha"] = m.group(1).upper()
        m2 = re.search(r"(?i)\b(3[0-9]|4[0-6])\b", text)
        if m2:
            out["size_eu"] = int(m2.group(1))

    return out


# ---------------------------------------------------------------------------
# Accessory detection (main-unit gating)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AccessoryResult:
    is_accessory: bool
    kind: str = ""
    reason: str = ""
    pack_hints: List[str] = field(default_factory=list)


_ACCESSORY_PATTERNS: List[Tuple[str, str]] = [
    ("coque", r"(?i)\b(coque|coques|scocca|scocche|shell|housing|case|carcasa|housse|étui|etui|cover)\b"),
    # "volant" (steering wheel) is commonly used without the final "e".
    # Accept common misspellings like "Mannette".
    ("manette", r"(?i)\b(man+ette|controller|dual\s*shock|dualshock|volant|volante|steering\s*wheel|joypad|pad|mandos|gamepad)\b"),
    ("casque", r"(?i)\b(casque|headset|micro\s*casque|pulse\s*3d|p(u|ú)lse)\b"),
    ("keychain", r"(?i)\b(porte\s*-?\s*cl[eé]s|porte\s*-?\s*cles|key\s*chain|keychain)\b"),
    ("box_pack", r"(?i)\b(scatola|scatole|box|packaging|bo[iî]te|manual|notice)\b"),
    ("jeu_seul", r"(?i)\b(jeu(x)?\s*(ps4|3ds)?|game(s)?\s*(ps4)?)\b(?![\s\S]{0,40}\b(console|ps4\s*pro|ps4\s*slim)\b)"),
    ("plate", r"(?i)\b(cover\s*plate(s)?|face\s*plate(s)?|plate(s)?)\b"),
    ("film", r"(?i)\b(protege[\s-]?ecran|screen\s*protector|film\s*(verre|tremp[eé])|tempered\s*glass)\b"),
    ("stylet", r"(?i)\b(stylet|stylus)\b"),
    ("chargeur", r"(?i)\b(chargeur|charger|charging\s*(dock|station)|dock)\b"),
    ("grip", r"(?i)\b(grip|poign[ée]e|ergonomic\s*grip)\b"),
    ("vr", r"(?i)\b(casq?ue\s*vr|vr\s*headset|virtual\s*reality)\b"),
    ("camera", r"(?i)\b(cam[eé]ra|webcam|web\s*cam|camera)\b"),
    ("cooler", r"(?i)\b(refroidisseur|cooler|fan|ventilateur|radiateur)\b"),
    ("power_supply", r"(?i)\b(allimentation|al+limentation|alimentation|alimentatore|power\s*supply|psu|transformer)\b"),
    ("stand", r"(?i)\b(support\s*consoles?|support\s*mural|wall\s*stand|stand|docking\s*station|dock)\b"),
    ("facade", r"(?i)\b(facade|façade|faade)\b"),
    ("backbone", r"(?i)\b(backbone)\b"),
    ("certificat", r"(?i)\b(certificat(o)?|attestato|attestation|certificate|certified)\b"),
    # Games/accessoires "logiciels" (surtout en recherche console) -> souvent à exclure des top deals.
    ("jeu", r"(?i)\b(jeu(x)?|juego(s)?|giochi|juegos|games?|game(s)?|spiel|cart(ouche)?|cartridge|dark\s*souls|ghost\s*of\s*tsushima|fifa\s*\d{2,4}|call\s*of\s*duty|nba\s*2k|trackmania|fallout\s*\d*|spider\s*-?man|spiderman|miles\s*morales|morales)\b"),
    ("boite", r"(?i)\b(bo[iî]te|box|packaging|notice|manuel|manual|scatola|scatole)\b"),
    ("cable", r"(?i)\b(c[aâ]ble|cable|adaptateur|adapter)\b"),
    ("piece", r"(?i)\b(pour\s*pi[eè]ces|pezzi\s*di\s*ricambio|pi[eè]ces\s*d[ée]tach[ée]es|spares?|parts?)\b"),
]

_MAIN_UNIT_HINTS = re.compile(
    r"(?i)\b(console|ps5|playstation\s*5|ps4|playstation\s*4|xbox|"
    r"new\s*nintendo\s*3ds(\s*xl)?|nintendo\s*3ds(\s*xl)?|3ds(\s*xl)?|nintendo\s*switch)\b"
)

_MAIN_UNIT_NEGATORS = re.compile(
    r"(?i)\b(coque|cover\s*plate|face\s*plate|plate(s)?|certificat|attestation|chargeur|stylus|stylet)\b"
)

_STRONG_CONSOLE_UNIT = re.compile(
    r"(?i)\b(console)\b.*\b(ps5|playstation\s*5|ps4|playstation\s*4|ps4\s*(slim|pro)|"
    r"CUH[-\s]?\d{4}|CFI[-\s]?\d{4}|\d{3,4}\s*gb|500\s*go|1\s*to|bundle|lot|pack)\b|"
    r"\b(ps5|playstation\s*5|ps4|playstation\s*4)\b"
)


_HEAD_ACCESSORY = re.compile(
    r"(?i)^(?:"
    r"coque|coques|housse|housses|[ée]tui|[ée]tuis|case|cover|shell|"
    r"chargeur|chargeurs|charger|c[aâ]ble|cables|cable|"
    r"vitre|verre|film|protecteur|protection|"
    r"support|dock|station|"
    r"manette|controller|volant|"
    r"coque\s+pour|housse\s+pour|[ée]tui\s+pour|case\s+for"
    r")\b"
)
_BUNDLE_LINK = re.compile(r"(?i)\b(avec|inclus|inclue|fourni|offert|\+|et|plus|lot|pack|bundle)\b")
_PHONE_MAIN = re.compile(
    r"(?i)\b(iphone\s*\d{1,2}(?:\s*(?:pro|max|plus|mini))?|"
    r"samsung\s+galaxy|galaxy\s+[asmz]\d|pixel\s*\d|xiaomi\s+\w+|huawei\s+\w+)\b"
)
_ACCESSORY_KIND_MAP = {
    "coque": r"(?i)\b(coque|coques|housse|[ée]tui|case|cover|shell|magsafe|uag)\b",
    "chargeur": r"(?i)\b(chargeur|charger|c[aâ]ble|adaptateur|magsafe)\b",
    "film": r"(?i)\b(film|verre|vitre|protecteur|screen\s*protector|tremp[eé])\b",
    "manette": r"(?i)\b(manette|controller|volant|joypad)\b",
    "boite": r"(?i)\b(bo[iî]te\s*(vide|empty)?|box\s*(only|empty)?)\b",
}


def _listing_price(item: CatalogItem) -> float:
    try:
        return float(item.total_or_price or item.price or 0)
    except (TypeError, ValueError):
        return 0.0


def _price_floor_accessory(title: str, price: float, category: str) -> bool:
    """True si le prix est trop bas pour être l'objet principal."""
    if price <= 0:
        return False
    cat = (category or "").lower()
    title_l = (title or "").lower()
    try:
        from .models import find_model_by_text, get_model

        m = find_model_by_text(title, cat)
        if m and m.msrp_eur > 0 and price < m.msrp_eur * 0.10:
            if re.search(r"(?i)^(lot|pack|bundle)\b", title_l):
                return True
            if re.match(r"(?i)^\s*(apple\s+)?iphone\b", title_l.strip()):
                return True
            if re.search(r"(?i)\b(coques?|housses?|chargeurs?)\b", title_l):
                return True
            return True
    except Exception:
        pass
    if cat == "phone" or _PHONE_MAIN.search(title_l):
        if _PHONE_MAIN.search(title_l):
            if price < 70:
                return True
            if price >= 120:
                return False
        if price < 25:
            return True
    if cat.startswith("console_") or cat == "console_ds":
        if price < 35 and not re.search(r"(?i)\b(jeu|game)\b", title_l):
            return True
    return False


def _extract_pack_hints(text: str) -> List[str]:
    hints: List[str] = []
    for kind, pat in _ACCESSORY_KIND_MAP.items():
        if re.search(pat, text):
            hints.append(kind)
    return hints


def _accessory_after_bundle_link(text: str, pat: str) -> bool:
    """True si le motif accessoire est précédé d'un mot de liaison (pack)."""
    for m in re.finditer(pat, text):
        start = m.start()
        window = text[max(0, start - 40) : start]
        if _BUNDLE_LINK.search(window):
            return False
    return True


def detect_accessory(
    item: CatalogItem,
    *,
    search_hint: str = "",
    title_only: bool = False,
    market_category: str = "",
) -> AccessoryResult:
    text = item_title_text(item) if title_only else item_text(item)
    hint = _norm_text(search_hint)
    if not text:
        return AccessoryResult(is_accessory=False)

    cat = (market_category or "").lower()
    if not cat:
        if re.search(r"(?i)\b(iphone|samsung|galaxy|pixel)\b", hint + " " + text):
            cat = "phone"
        elif re.search(r"(?i)\b(clim|climatiseur|climatisation)\b", hint + " " + text):
            cat = "ac"
        elif re.search(r"(?i)\b(ps5|playstation\s*5)\b", hint + " " + text):
            cat = "console_ps5"
        elif re.search(r"(?i)\b(ps4|playstation|3ds|switch|xbox)\b", hint + " " + text):
            cat = "console_ps4"

    title_raw = str(item.title or "")
    price = _listing_price(item)

    if cat == "ac" or re.search(r"(?i)\b(clim|climatiseur|climatisation)\b", hint):
        try:
            from .scoring import ac_has_unit_signal

            if ac_has_unit_signal(text):
                return AccessoryResult(False, reason="ac_main_unit")
        except Exception:
            if re.search(r"(?i)\b(\d{4,6}\s*btu|climatiseur|clim\s+mobile)\b", text):
                return AccessoryResult(False, reason="ac_main_unit")

    if cat == "projector" or re.search(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur)\b", hint):
        if is_projector_accessory_text(text):
            kind = "hdmi_accessory" if re.search(r"(?i)\b(splitter|extender|goobay|hdmi)\b", text) else "projector_accessory"
            return AccessoryResult(True, kind=kind, reason="projector_accessory")
        if has_projector_unit_signal(text):
            return AccessoryResult(False, reason="projector_main")

    if (cat == "console_ps5" or re.search(r"(?i)\b(ps5|playstation\s*5)\b", hint + " " + text)) and re.search(
        r"(?i)\b(ps5|playstation\s*5)\b", text
    ):
        peripheral_only = bool(
            re.match(r"(?i)^(mando|manette|controller|volant|jeu|game|juego)\b", title_raw.strip())
            or re.search(
                r"(?i)\b(manette|controller|volant)\s*(ps5|seule?|only)\b|"
                r"\b(jeu|game)\s*(ps5|seul)\b",
                text,
            )
            and not re.search(r"(?i)\b(console|bundle|lot|pack)\b", text)
        )
        if not peripheral_only and not re.match(r"(?i)^(jeu|game|juego|giochi|spiel)\b", title_raw.strip()):
            return AccessoryResult(False, pack_hints=_extract_pack_hints(text), reason="ps5_main")
    head = " ".join(title_raw.split()[:3]).lower()

    # 1) Sujet principal en tête → accessoire pur
    if _HEAD_ACCESSORY.match(head) and not _PHONE_MAIN.search(text[:60]):
        if not (cat == "phone" and price >= 150 and _PHONE_MAIN.search(text)):
            kind = "coque" if "coque" in head or "housse" in head or "étui" in head else "chargeur"
            return AccessoryResult(True, kind=kind, reason="head_noun")

    # 2) Lot / pack d'accessoires (ex. « Lot de 20 coques iPhone 13 »)
    if re.search(r"(?i)^(lot|pack|bundle)\b", title_raw.strip()) and re.search(
        r"(?i)\b(coques?|housses?|[ée]tuis?|chargeurs?|cables?|films?|protecteurs?)\b", text
    ):
        kind = "coque" if re.search(r"(?i)\b(coques?|housses?|[ée]tui)\b", text) else "chargeur"
        return AccessoryResult(True, kind=kind, reason="accessory_lot")

    # 3) Accessoire explicite « seul / pour »
    if re.search(
        r"(?i)\b(coque\s*seule|housse\s*seule|[ée]tui\s*seul|coque\s+pour|housse\s+pour|[ée]tui\s+pour|"
        r"case\s+for|shell\s+for|jeu\s*seul|game\s+only|chargeur\s*seul|manette\s*seule)\b",
        text,
    ) and not re.search(r"(?i)\b(console|lot|bundle|pack)\b", text):
        return AccessoryResult(True, kind="coque", reason="accessory_only")

    # 4) Téléphone : pack bonus vs accessoire
    if cat == "phone" or _PHONE_MAIN.search(text):
        hints = _extract_pack_hints(text)
        if _price_floor_accessory(title_raw, price, "phone"):
            return AccessoryResult(True, kind="low_price", reason="price_floor")
        if _PHONE_MAIN.match(title_raw.strip()) or re.match(
            r"(?i)^(apple\s+)?iphone", title_raw.strip()
        ):
            return AccessoryResult(False, pack_hints=hints, reason="phone_main_title")
        if _PHONE_MAIN.search(text) and price >= 100:
            standalone = any(
                re.search(_ACCESSORY_KIND_MAP[k], text)
                and _accessory_after_bundle_link(text, _ACCESSORY_KIND_MAP[k])
                for k in _ACCESSORY_KIND_MAP
            )
            if not standalone:
                return AccessoryResult(False, pack_hints=hints, reason="phone_pack")
        for kind, pat in _ACCESSORY_KIND_MAP.items():
            if re.search(pat, text) and _accessory_after_bundle_link(text, pat):
                if not _PHONE_MAIN.search(text[:50]):
                    return AccessoryResult(True, kind=kind, reason=f"phone_ctx:{kind}")

    # Console search context: be stricter (PS4 coques/jeux/manettes seules).
    console_search = bool(
        re.search(r"(?i)\b(ps5|ps4|playstation|3ds|nintendo\s*ds|switch|xbox)\b", hint)
        or re.search(r"(?i)\b(ps5|playstation\s*5|ps4|playstation\s*4|3ds|2ds)\b", text)
    )
    if re.search(r"(?i)\b(3ds|2ds|new\s*nintendo\s*3ds|nintendo\s*ds)\b", text) and (
        re.search(
            r"(?i)\b(jeu(x)?|games?|giochi|juegos|spiel|chargeur|charger|cargador|ladekabel|"
            r"console|pack|lot|bundle|housse|coque|bo[iî]te)\b",
            text,
        )
    ):
        return AccessoryResult(False)

    if console_search:
        peripheral_only = bool(
            re.search(r"(?i)^(mando|manette|controller|volant)\b", text)
            and not re.search(r"(?i)\bconsole\b", text)
        )
        _console_unit = (
            None
            if peripheral_only
            else re.search(
                r"(?i)\b(console|nintendo\s*3ds|new\s*nintendo\s*3ds|3ds\s*xl|2ds|"
                r"ps5\s*(slim|pro|digital)?|playstation\s*5|ps4\s*(pro|slim|fat)?|playstation\s*4|"
                r"switch\s*oled|nintendo\s*switch|lot|bundle|pack|avec\s+manette)\b",
                text,
            )
        )
        if not _console_unit:
            if re.search(
                r"(?i)\b(gu[ií]a|guide|manuale|manuel|libro|book|magazine|revista|poster|affiche)\b",
                text,
            ) and not re.search(r"(?i)\b(console|ps4\s*(slim|pro)?|playstation\s*4|bundle|lot)\b", text):
                return AccessoryResult(True, kind="guide", reason="guide_media")
            if re.search(
                r"(?i)\b(jeu(x)?|game(s)?|giochi|disque|blu-?ray|cartouche|mortal\s*kombat|fifa|"
                r"call\s*of\s*duty|god\s*of\s*war|spider\s*man|nba\s*2k|trackmania|fallout|"
                r"ghost\s*of|red\s*dead|horizon|assassin|zelda|mario)\b",
                text,
            ) and not re.search(
                r"(?i)\b(new\s*3ds|nintendo\s*3ds|3ds\s*xl|2ds|ps5|playstation\s*5|ps4|playstation\s*4|"
                r"console|avec\s+jeux|\+\s*\d+\s*jeu)\b",
                text,
            ):
                return AccessoryResult(True, kind="jeu", reason="game_only")
            if re.search(
                r"^(mando|manette|controller|volant)\b|"
                r"\b(manette|controller|volant|mandos)\s*(ps4|playstation)?\s*$",
                text,
                re.I,
            ):
                return AccessoryResult(True, kind="manette", reason="controller_only")
        if re.search(
            r"(?i)\b(scocca|scocche|coque|cover|volant|volante|casque|headset|pulse\s*3d|"
            r"manette\s*seule|controller\s*only|"
            r"jeu\s+ps4|game\s+ps4|ghost\s+of|fifa\s+\d|call\s+of\s+duty)\b",
            text,
        ):
            if not _console_unit:
                return AccessoryResult(True, kind="console_accessory", reason="console_context")

    if re.search(
        r"(?i)\b(bo[iî]te\s*vide|empty\s*box|sans\s*console|notice\s*seule|manuel\s*seul|"
        r"bo[iî]te\s*et\s*notice|emballage\s*seul)\b",
        text,
    ):
        if not re.search(r"(?i)\b(console\s+[\w]|avec\s+console|inclu[ae]\s+console)\b", text):
            return AccessoryResult(True, kind="boite_vide", reason="empty_box_only")

    # If it screams accessory, treat as accessory even if "console" is mentioned.
    for kind, pat in _ACCESSORY_PATTERNS:
        if re.search(pat, text):
            if kind == "box_pack" and re.search(
                r"(?i)\b(climatiseur|clim\s+mobile|\d{4,6}\s*btu|monosplit|split)\b", text
            ):
                continue
            # Special case: "lot console + accessoires" => main unit, not accessory.
            if kind in (
                "jeu",
                "boite",
                "chargeur",
                "stylet",
                "plate",
                "cable",
                "manette",
                "casque",
                "coque",
                "box_pack",
            ) and _MAIN_UNIT_HINTS.search(text):
                if kind == "boite" and re.search(
                    r"(?i)\b(vide|empty|sans\s*console|notice\s*seule|manuel\s*seul)\b", text
                ):
                    return AccessoryResult(True, kind=kind, reason=f"match:{kind}")
                if re.search(
                    r"(?i)\b(coque\s*seule|housse\s*seule|[ée]tui\s*seul|coque\s+pour|housse\s+pour|"
                    r"[ée]tui\s+pour|case\s+for|shell\s*for|jeu\s*seul|game\s*only)\b",
                    text,
                ):
                    return AccessoryResult(True, kind=kind, reason=f"match:{kind}")
                if not re.search(r"(?i)\bconsole\b", text) and kind in (
                    "stylet",
                    "plate",
                    "chargeur",
                    "boite",
                    "box_pack",
                    "jeu",
                ):
                    if _price_floor_accessory(title_raw, price, cat) or price < 45:
                        return AccessoryResult(True, kind=kind, reason=f"unit_name_only:{kind}")
                if not re.search(
                    r"(?i)\b(volant|casque|headset|manette\s*seule|controller\s*only)\b", text
                ):
                    return AccessoryResult(False)
            # Jeu seul sans console dans le titre
            if kind == "jeu_seul" and not re.search(r"(?i)\b(console|lot|bundle)\b", text):
                return AccessoryResult(True, kind=kind, reason=f"match:{kind}")
            if kind != "jeu_seul":
                if (cat == "phone" or _PHONE_MAIN.search(text)) and not _accessory_after_bundle_link(
                    text, pat
                ):
                    return AccessoryResult(
                        False,
                        pack_hints=_extract_pack_hints(text),
                        reason="bundle_pack",
                    )
                return AccessoryResult(True, kind=kind, reason=f"match:{kind}")

    return AccessoryResult(False)


_GAME_ONLY_LOT = re.compile(r"(?i)\blot\s+de\s+\d+\s+jeux\b")
_PS4_IN_TITLE = re.compile(r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|console)\b")


def is_game_only_listing(row: Dict[str, Any], *, category: str = "") -> bool:
    """True si l'annonce est un jeu/lot de jeux sans console."""
    cat = (category or row.get("item_type") or row.get("market_category") or "").lower()
    if cat not in ("console_ps4", "console_ps5", "console_switch", "console_ds", "console"):
        if not cat.startswith("console_"):
            return False
    if row.get("is_games_only") or str(row.get("listing_type") or "") == "games_only":
        return True
    kind = str(row.get("accessory_kind") or "")
    if kind in ("jeu", "jeu_seul") and not _PS4_IN_TITLE.search(str(row.get("title") or "")):
        return True
    text = f"{row.get('title') or ''} {row.get('description') or ''}"
    url = str(row.get("url") or "").lower()
    if _GAME_ONLY_LOT.search(text) and not re.search(r"(?i)\bconsole\b", text):
        return True
    if "jeux_video" in url and not re.search(r"(?i)\bconsole\b", text):
        return True
    if re.search(r"(?i)^(jeu(x)?|game(s)?)\b", text.strip()) and not re.search(r"(?i)\bconsole\b", text):
        return True
    if re.search(r"(?i)\bjeu(x)?\s+seul\b", text) and not re.search(r"(?i)\bconsole\b", text):
        return True
    return False


def is_console_listing(row: Dict[str, Any], *, category: str = "") -> bool:
    """True si l'annonce décrit une console (pas jeu/accessoire seul)."""
    if is_game_only_listing(row, category=category):
        return False
    if row.get("is_accessory") and not row.get("pack_hints"):
        return False
    try:
        from .engine import title_has_main_console_unit

        if title_has_main_console_unit(str(row.get("title") or "")):
            return True
    except Exception:
        pass
    return bool(_PS4_IN_TITLE.search(str(row.get("title") or "")))


# ---------------------------------------------------------------------------
# Risk flags (buy mode)
# ---------------------------------------------------------------------------


_RISK_FLAGS: List[Tuple[str, re.Pattern[str], float]] = [
    ("blocked", re.compile(r"(?i)\b(bloqu[ée]e?|blocked|br(i|î)qu[ée]e?)\b"), -45.0),
    ("no_power", re.compile(r"(?i)\b(ne\s*s'?allume\s*pas|doesn'?t\s*turn\s*on|won'?t\s*turn\s*on)\b"), -90.0),
    # Avoid false positives like "à prix cassé"
    ("broken", re.compile(r"(?i)(?<!prix\s)(?<!d[ée]cote\s)\b(cass[ée]e?|broken|hs|hors\s*service)\b"), -85.0),
    ("not_working", re.compile(r"(?i)\b(ne\s*fonctionne\s*pas|doesn'?t\s*work|not\s*working|unusable|inutilisable|d[ée]fectueux|defective|a\s*problem)\b"), -78.0),
    ("for_parts", re.compile(r"(?i)\b(pour\s*pi[eè]ces|for\s*parts)\b"), -85.0),
    # common console failure descriptions
    ("shuts_off", re.compile(r"(?i)\b(s['’]?éteint|s['’]?eteint|spegne|si\s*spenge|turns?\s*off|shut\s*down|shuts\s*down)\b"), -80.0),
    ("no_signal", re.compile(r"(?i)\b(pas\s*de\s*signal|no\s*signal|aucun\s*signal)\b"), -70.0),
    ("repair_only", re.compile(r"(?i)\b([àa]\s*r[ée]parer|a\s*riparare|per\s*ricambi|riparazione|for\s*repair)\b"), -75.0),
    (
        "ban",
        re.compile(
            r"(?i)"
            r"(\bbann[iéée]?\b|\bban(ned)?\b|psn\s*ban|playstore\s*ban|ban\s*playstore|"
            r"ban\w*\s+(de\s+)?play\s*store|play\s*store\s+(ban|banni)|"
            r"compte\s+banni|account\s+banned)"
        ),
        -92.0,
    ),
    ("scammy", re.compile(r"(?i)\b(virement|western\s*union|whatsapp|telegram)\b"), -60.0),
    (
        "screen_defect",
        re.compile(
            r"(?i)\b("
            r"point\s*noir|pixel\s*mort|dead\s*pixel|tache\s+sur\s+l['\u2019]?[ée]cran|"
            r"[ée]cran\s+(ab[iî]m[ée]|cass[ée]|hs|d[ée]fectueux)|top\s*screen\s+(cass|hs|d[ée]f)"
            r")\b"
        ),
        -42.0,
    ),
]

_MODDED = re.compile(r"(?i)\b(cfw|custom\s*firmware|luma|godmode9|hshop|h-shop|homebrew|mod(d[ée]e?)?)\b")


def risk_flags(item: CatalogItem) -> List[Dict[str, Any]]:
    text = item_text(item)
    flags: List[Dict[str, Any]] = []
    if not text:
        return flags
    for name, pat, penalty in _RISK_FLAGS:
        if pat.search(text):
            flags.append({"flag": name, "penalty": penalty})
    if _MODDED.search(text):
        flags.append({"flag": "modded", "penalty": 0.0})
    return flags


# ---------------------------------------------------------------------------
# Market stats + scoring
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MarketStats:
    median: float
    p25: float
    p75: float
    iqr: float


def _quantile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    q = min(1.0, max(0.0, float(q)))
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    w = pos - lo
    return float(sorted_vals[lo] * (1.0 - w) + sorted_vals[hi] * w)


def compute_market_stats(prices: Iterable[float]) -> Optional[MarketStats]:
    vals = sorted([float(p) for p in prices if p is not None and p > 0])
    if len(vals) < 6:
        return None
    p25 = _quantile(vals, 0.25)
    p75 = _quantile(vals, 0.75)
    med = statistics.median(vals)
    iqr = max(1e-6, p75 - p25)
    return MarketStats(median=float(med), p25=float(p25), p75=float(p75), iqr=float(iqr))


def _sigmoid(x: float) -> float:
    # stable sigmoid
    x = max(-12.0, min(12.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def score_item_buy_mode(
    item: CatalogItem,
    *,
    stats: Optional[MarketStats],
    search_hint: str = "",
    weights: Optional[Any] = None,
    effective_price: Optional[float] = None,
) -> Dict[str, Any]:
    from .engine import ScoreWeights, get_weights

    w: ScoreWeights = weights if weights is not None else get_weights()
    inferred = infer_category(item)
    price = item.total_or_price
    if effective_price is not None and effective_price > 0:
        price = float(effective_price)
    base = 40.0
    reasons: List[str] = []
    breakdown: Dict[str, float] = {"base": 40.0}

    if price is None or price <= 0:
        return {"final_score": 0.0, "reason": "no_price", "market_discount_pct": 0.0}

    # Market discount vs median
    disc_pct = 0.0
    if stats is not None and stats.median > 0:
        disc_pct = (stats.median - float(price)) / stats.median * 100.0
        # translate discount into [0..55] with diminishing returns
        disc_points = 55.0 * _sigmoid((disc_pct - 10.0) / 10.0) * float(w.discount)
        breakdown["discount"] = float(disc_points)
        base += disc_points
        reasons.append(f"discount_vs_median={disc_pct:.0f}%")
    else:
        # fallback: weak price-only signal
        bonus = 8.0 * float(w.discount)
        breakdown["no_market_stats_bonus"] = bonus
        base += bonus
        reasons.append("no_market_stats")

    # Engagement (cheap proxy for desirability / exposure)
    eng = float(item.engagement)
    eng_points = min(10.0, math.log1p(eng) * 2.4) * float(w.engagement)
    breakdown["engagement"] = float(eng_points)
    base += eng_points

    # Seller reputation
    if item.seller_feedback_count >= 10:
        seller_points = min(6.0, max(0.0, (item.seller_rating - 0.6) * 10.0)) * float(w.seller)
        breakdown["seller"] = float(seller_points)
        base += seller_points
    elif item.seller_feedback_count == 0:
        pen = -6.0 * float(w.risk)
        breakdown["new_seller_penalty"] = pen
        base += pen
        reasons.append("new_seller")

    # Condition text heuristics (Vinted statuses are not always informative)
    t = item_text(item)
    if re.search(r"(?i)\b(complet(e)?|compl[eè]te|bo[iî]te|chargeur|notice|câble|cable)\b", t):
        inc = 6.0 * float(w.completeness)
        breakdown["completeness_hint"] = breakdown.get("completeness_hint", 0.0) + inc
        base += inc
        reasons.append("completeness_hint")
    if re.search(r"(?i)\b(griff|rayure|scratch|jauni|yellow|usure|fatigue)\b", t):
        pen = -7.0 * float(w.risk)
        breakdown["cosmetic_wear_penalty"] = breakdown.get("cosmetic_wear_penalty", 0.0) + pen
        base += pen
        reasons.append("cosmetic_wear")

    # Category-specific tweaks
    cat = inferred.category
    if cat == "projector":
        # Penalize obvious fake claims: "4k" with very low price is suspicious.
        if re.search(r"(?i)\b(4k|uhd)\b", t) and (price is not None and float(price) < 180):
            breakdown["trap_4k_low_price"] = -18.0
            base -= 18.0
            reasons.append("trap_4k_low_price")
        if re.search(r"(?i)\b(1080p|full\s*hd)\b", t) and (price is not None and float(price) < 60):
            breakdown["trap_1080p_low_price"] = -12.0
            base -= 12.0
            reasons.append("trap_1080p_low_price")
        # positive signals
        if re.search(r"(?i)\b(1080p|full\s*hd)\b", t):
            breakdown["projector_1080p_bonus"] = 6.0
            base += 6.0
        lum = re.search(r"(?i)\b(\d{3,5})\s*(ansi\s*)?lum(en)?s\b", t)
        if lum:
            try:
                lum_v = int(lum.group(1))
                if lum_v >= 2500:
                    breakdown["projector_lumens_bonus"] = 6.0
                    base += 6.0
                elif lum_v < 300:
                    breakdown["projector_low_lumens_penalty"] = -10.0
                    base -= 10.0
                    reasons.append("low_lumens")
            except Exception:
                pass
        if re.search(r"(?i)\b(sans\s*t[ée]l[ée]commande|no\s*remote)\b", t):
            breakdown["missing_remote_penalty"] = -6.0
            base -= 6.0
            reasons.append("missing_remote")

    if cat == "ac":
        # Cheap rules: hose/remote presence is critical; BTU is value driver.
        if re.search(r"(?i)\b(sans\s*tuyau|sans\s*gaine|no\s*hose)\b", t):
            breakdown["missing_hose_penalty"] = -22.0
            base -= 22.0
            reasons.append("missing_hose")
        if re.search(r"(?i)\b(sans\s*t[ée]l[ée]commande|no\s*remote)\b", t):
            breakdown["missing_remote_penalty"] = -8.0
            base -= 8.0
            reasons.append("missing_remote")
        btu = re.search(r"(?i)\b(\d{4,6})\s*btu\b", t)
        if btu:
            try:
                btu_v = int(btu.group(1))
                if btu_v >= 12000:
                    breakdown["btu_bonus"] = 10.0
                    base += 10.0
                elif btu_v >= 9000:
                    breakdown["btu_bonus"] = 6.0
                    base += 6.0
                elif btu_v < 7000:
                    breakdown["btu_low_penalty"] = -8.0
                    base -= 8.0
                    reasons.append("low_btu")
            except Exception:
                pass
        if re.search(r"(?i)\b(kit\s*fen[eê]tre|window\s*kit)\b", t):
            breakdown["window_kit_bonus"] = 4.0
            base += 4.0

    if cat == "apparel":
        # Favor premium materials a bit for buyers/resale.
        if re.search(r"(?i)\b(laine|wool|merino|lin|linen|cuir|leather)\b", t):
            breakdown["premium_material_bonus"] = 6.0
            base += 6.0
            reasons.append("premium_material")
        # Defects are strong negatives
        if re.search(r"(?i)\b(tache(s)?|trou(x)?|d[ée]chir[ée]e?|accroc|pilling|bouloche(s)?)\b", t):
            breakdown["defect_penalty"] = -18.0
            base -= 18.0
            reasons.append("defects")
    if cat.startswith("console_"):
        # For consoles, completeness matters a bit more.
        if re.search(r"(?i)\b(sans\s*chargeur|no\s*charger)\b", t):
            breakdown["missing_charger_penalty"] = -9.0
            base -= 9.0
            reasons.append("missing_charger")
        # Missing key cables is a strong negative for buyers.
        if re.search(r"(?i)\b(sans\s*hdmi|no\s*hdmi)\b", t):
            breakdown["missing_hdmi_penalty"] = -7.0
            base -= 7.0
            reasons.append("missing_hdmi")
        if re.search(r"(?i)\b(sans\s*alim|sans\s*alimentation|no\s*power|no\s*psu)\b", t):
            breakdown["missing_power_penalty"] = -10.0
            base -= 10.0
            reasons.append("missing_power")
        # Slight bonus when controller is included (most searches care).
        if re.search(r"(?i)\b(manette|controller|dual\s*shock|joypad|mando)\b", t):
            breakdown["controller_included_bonus"] = 5.0
            base += 5.0
            reasons.append("controller_included")
        if re.search(
            r"\b(bann[iéée]?|ban|psn\s*ban|playstore|play\s*store)\b.*\b(ban|banni)\b|"
            r"ban\w*\s+(de\s+)?play\s*store",
            t,
            re.I,
        ):
            breakdown["ban_risk_penalty"] = -45.0
            base -= 45.0
            reasons.append("ban_risk")
        hint = search_hint or ""
        if re.search(r"(?i)\b(ps4|playstation\s*4)\b", hint):
            if re.search(r"(?i)\b(ps2|playstation\s*2|ps3|playstation\s*3|psp|ps\s*vita)\b", t) and not re.search(
                r"(?i)\bps4\b", t
            ):
                breakdown["wrong_platform_penalty"] = -70.0 * float(w.risk)
                base += breakdown["wrong_platform_penalty"]
                reasons.append("wrong_platform")
                base = min(base, 25.0)
    if cat in ("shoes", "jeans"):
        if re.search(r"(?i)\b(neuf|jamais\s*port[ée]e?|deadstock|ds)\b", t):
            breakdown["like_new_bonus"] = 6.0
            base += 6.0
            reasons.append("like_new")
        if re.search(r"(?i)\b(fausse?|fake|replica|replique)\b", t):
            breakdown["fake_risk_penalty"] = -35.0
            base -= 35.0
            reasons.append("fake_risk")
    if cat == "pc":
        # light value signals
        if re.search(r"(?i)\b(rtx\s*\d{3,4}|gtx\s*\d{3,4}|rx\s*\d{3,4})\b", t):
            breakdown["dedicated_gpu_bonus"] = 8.0
            base += 8.0
            reasons.append("dedicated_gpu")
        ram = re.search(r"(?i)\b(\d{1,2})\s*gb\s*(ram)?\b", t)
        if ram:
            try:
                rv = int(ram.group(1))
                if rv >= 16:
                    breakdown["ram_bonus"] = 6.0
                    base += 6.0
                elif rv < 8:
                    breakdown["low_ram_penalty"] = -8.0
                    base -= 8.0
                    reasons.append("low_ram")
            except Exception:
                pass

    if cat == "tent":
        cap = re.search(r"(?i)\b([2-8])\s*(p|places?|person(ne)?s?)\b", t)
        if cap:
            try:
                cp = int(cap.group(1))
                if cp >= 3:
                    breakdown["capacity_bonus"] = 4.0
                    base += 4.0
            except Exception:
                pass
        if re.search(r"(?i)\b(4\s*saisons?|four\s*season)\b", t):
            breakdown["four_season_bonus"] = 6.0
            base += 6.0
            reasons.append("4-season")

    if cat == "skates":
        if re.search(r"(?i)\b(neuf|jamais\s*servi)\b", t):
            breakdown["like_new_bonus"] = 5.0
            base += 5.0
            reasons.append("like_new")

    # Risk flags
    flags = risk_flags(item)
    if flags:
        pen = sum(float(f.get("penalty") or 0.0) for f in flags) * float(w.risk)
        breakdown["risk_flags_penalty_sum"] = float(pen)
        base += pen
        for f in flags:
            reasons.append(f"risk:{f['flag']}")

    # Hard caps for deal-breakers (price discount must not outweigh critical defects)
    critical = {"ban", "broken", "for_parts", "not_working", "no_power", "shuts_off", "blocked", "no_signal"}
    flag_names = {str(f.get("flag") or "") for f in flags}
    if flag_names & critical:
        base = min(base, 15.0)
        reasons.append("critical_risk_cap")

    # clamp
    final = max(0.0, min(100.0, float(base)))
    return {
        "final_score": round(final, 2),
        "market_discount_pct": round(disc_pct, 2),
        "reason": ";".join(reasons)[:280],
        "risk_flags": ",".join([str(f["flag"]) for f in flags]),
        "item_type": inferred.category,
        "item_type_conf": inferred.confidence,
        "score_breakdown_json": str({k: round(v, 2) for k, v in breakdown.items()}),
    }


# ---------------------------------------------------------------------------
# Quality audit (Top deals verification)
# ---------------------------------------------------------------------------

def quality_audit_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cheap, deterministic verification pass (no LLM):
    - checks for missing critical parts
    - checks for risk flags / defects
    Returns fields suitable for GUI + export.
    """
    cat = str(row.get("item_type") or "unknown")
    notes: List[str] = []
    penalties = 0

    # Global blockers / risks
    rf = str(row.get("risk_flags") or "").strip().lower()
    if rf:
        notes.append(f"risques: {rf}")
        penalties += 2

    if bool(row.get("defect_hint")):
        notes.append("défauts mentionnés")
        penalties += 2

    # Category checks
    if cat.startswith("console_"):
        if not bool(row.get("has_power_cable")) and bool(row.get("missing_power")):
            notes.append("alim manquante")
            penalties += 2
        if not bool(row.get("has_hdmi")) and bool(row.get("missing_hdmi")):
            notes.append("câble HDMI manquant")
            penalties += 1
        if not bool(row.get("has_controller")):
            # not always required, but most buyers want it
            notes.append("manette non confirmée")
            penalties += 1
        if not bool(row.get("complete_hint")):
            notes.append("pack complet non confirmé")
            penalties += 1

    if cat == "ac":
        if bool(row.get("missing_hose")):
            notes.append("tuyau manquant")
            penalties += 2
        if bool(row.get("missing_remote")):
            notes.append("télécommande manquante")
            penalties += 1

    if cat == "projector":
        if bool(row.get("missing_remote")):
            notes.append("télécommande manquante")
            penalties += 1
        if row.get("resolution") in ("720p", "") or row.get("resolution") is None:
            notes.append("résolution faible/unknown")
            penalties += 1

    if cat in ("apparel", "shoes", "jeans"):
        if bool(row.get("defect_hint")):
            notes.append("à inspecter: défauts")
            penalties += 1
        if str(row.get("condition_hint") or "") == "new":
            notes.append("état: neuf (signal)")

    # Grade
    if penalties <= 1:
        grade = "A"
    elif penalties <= 3:
        grade = "B"
    else:
        grade = "C"

    return {
        "quality_grade": grade,
        "quality_notes": " · ".join(notes)[:220],
    }


def apply_deal_caps(row: Dict[str, Any]) -> None:
    """In-place score caps so junk cannot rank above real deals."""
    try:
        sc = float(row.get("final_score") or 0.0)
    except (TypeError, ValueError):
        sc = 0.0
    rf = str(row.get("risk_flags") or "").lower()
    critical = (
        "ban",
        "broken",
        "for_parts",
        "not_working",
        "no_power",
        "shuts_off",
        "blocked",
        "hardware_defect",
    )
    if bool(row.get("screen_defect")) or "screen_defect" in rf:
        row["final_score"] = round(min(sc, 55.0), 2)
        row["deal_capped"] = True
        return
    if any(c in rf for c in critical):
        row["final_score"] = round(min(sc, 12.0), 2)
        row["deal_blocked"] = True
    elif bool(row.get("is_accessory")):
        row["final_score"] = round(min(sc, 38.0), 2)
    elif str(row.get("quality_tier") or "") == "dinosaur":
        row["final_score"] = round(min(sc, 42.0), 2)
    elif str(row.get("quality_tier") or "") == "dropship":
        cap = 58.0
        qf = str(row.get("quality_flags") or "")
        if "fake_4k_scam" in qf or "impossible_price_scam" in qf:
            cap = 30.0
        row["final_score"] = round(min(sc, cap), 2)
    elif str(row.get("quality_tier") or "") == "suspicious":
        qf = str(row.get("quality_flags") or "")
        cap = 48.0 if ("budget_lcd_entry" in qf or "budget_bad_brand" in qf) else 68.0
        row["final_score"] = round(min(sc, cap), 2)
    elif str(row.get("triage_decision") or "") == "doubtful":
        row["final_score"] = round(min(sc, 62.0), 2)
    elif "wrong_ds_generation" in str(row.get("triage_reasons") or "") or row.get("intent_mismatch"):
        row["final_score"] = round(min(sc, 40.0), 2)
    elif str(row.get("quality_tier") or "") == "ok":
        try:
            from .engine import projector_office_wxga

            if projector_office_wxga(row):
                row["final_score"] = round(min(sc, 72.0), 2)
        except Exception:
            pass
    try:
        from .engine import cap_brand_deal_score

        proj_cap = cap_brand_deal_score(row)
        if proj_cap is not None:
            try:
                sc2 = float(row.get("final_score") or 0.0)
            except (TypeError, ValueError):
                sc2 = 0.0
            row["final_score"] = round(min(sc2, proj_cap), 2)
            row["deal_capped"] = True
    except Exception:
        pass
    try:
        from .engine import apply_scam_shield

        apply_scam_shield(row)
    except Exception:
        pass


# --- spec_profile.py ---

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


_BATTERY_RE = re.compile(
    r"(?i)(?:batterie|battery|sant[eé])\s*(?:à|a|at)?\s*(\d{2,3})\s*%|(\d{2,3})\s*%\s*(?:batterie|battery)"
)


def __extract_battery_pct(text: str) -> Optional[int]:
    if not text:
        return None
    m = _BATTERY_RE.search(text)
    if not m:
        m = re.search(r"(?i)\b(\d{2,3})\s*%\b", text)
        if not m or not re.search(r"(?i)battery|batterie|sant", text):
            return None
    try:
        pct = int(m.group(1) or m.group(2))
        return pct if 50 <= pct <= 100 else None
    except (TypeError, ValueError, IndexError):
        return None

# ---------------------------------------------------------------------------
# Category normalization (UI / market aliases → internal keys)
# ---------------------------------------------------------------------------

_CATEGORY_ALIASES = {
    "velo": "bicycle",
    "vélo": "bicycle",
    "vetements": "apparel",
    "vêtements": "apparel",
    "console": "console_ds",
    "default": "generic",
    "laptop": "laptop_pc",
    "pc": "laptop_pc",
    "ordinateur": "laptop_pc",
    "clim": "ac",
    "climatisation": "ac",
    "climatiseur": "ac",
}


def normalize_category(category: str) -> str:
    c = (category or "generic").strip().lower()
    return _CATEGORY_ALIASES.get(c, c or "generic")


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_STORAGE_RE = re.compile(r"(?i)\b(64|128|256|512|1024)\s*(go|gb|to|tb)\b")
_IPHONE_MODEL_RE = re.compile(r"(?i)i\s*-?\s*phone\s*(\d{2})|iphone\s*(\d{2})")
_PRO_PLUS_RE = re.compile(r"(?i)\b(pro\s*max|pro|plus|max)\b")
_CONDITION_GOOD = re.compile(r"(?i)\b(neuf|tres\s+bon|très\s+bon|excellent|nickel)\b")
_CONDITION_WEAR = re.compile(r"(?i)\b(rayure|usure|griffure|fissur|tache)\b")
_FRAME_SIZE_RE = re.compile(
    r"(?i)\b(?:taille\s*|cadre\s*)?(\d{2})\s*cm\b|"
    r"\b(?:taille\s*)?(XXS|XS|S|M|L|XL|XXL)\b|"
    r"\b(\d{2})\s*/\s*(XXS|XS|S|M|L|XL|XXL)\b"
)
_LUMENS_RE = re.compile(
    r"(?i)\b(\d{3,5})\s*(ansi\s*)?lum(?:ens?)?\b|\b(\d{3,5})\s*lm\b|"
    r"\b(\d{3,5})\s*(?:ansi\s*)?(?:lumens?|lumen)\b"
)
_SCREEN_ONLY_RE = re.compile(
    r"(?i)\b(écran\s*seul|ecran\s*seul|écran\s*de\s*projection\s*seul|ecran\s*de\s*projection\s*seul|"
    r"screen\s*only|fixation\s*seule|télécommande\s*seule|telecommande\s*seule|lampe\s*seule|ampoule\s*seule)\b"
)
_PROJECTOR_ACCESSORY_RE = re.compile(
    r"(?i)\b("
    r"splitter|répartiteur|repartiteur|extendeur|extender|hdmi\s*extender|hdmi\s*splitter|"
    r"switch\s*hdmi|matrix\s*switch|multiprise\s*hdmi|goobay|"
    r"câble\s*hdmi|cable\s*hdmi|adaptateur\s*hdmi|hdmi\s*cable|hdmi\s*adapter|"
    r"trépied|trepeid|tripod|support\s*(?:mural\s*)?seul|fixation\s*seule|"
    r"lampe\s*seule|ampoule\s*seule|bulb\s*only|lamp\s*only|"
    r"télécommande\s*seule|telecommande\s*seule|remote\s*only"
    r")\b"
)
_WIRELESS_DISPLAY_ACCESSORY_RE = re.compile(
    r"(?i)\b("
    r"adaptateur\s*affichage\s*sans\s*fil|wireless\s*display|miracast|chromecast|"
    r"fire\s*tv\s*stick|firestick|dongle\s*hdmi|dongle\s*wifi|dongle\s*wi-?fi|"
    r"miroir\s*d['\u2019]?\s*[eé]cran|screen\s*mirror(?:ing)?|"
    r"hdmi\s*wireless|wireless\s*hdmi|"
    r"adaptateur\s*bluetooth\s*hdmi|adaptateur\s*hdmi\s*bluetooth"
    r")\b"
)
_HDMI_RECEIVER_ACCESSORY_RE = re.compile(
    r"(?i)(?:\b(récepteur|recepteur|receiver)\b.*\b(hdmi|wireless|sans\s*fil|miracast|affichage)\b|"
    r"\b(hdmi|wireless|sans\s*fil|miracast|affichage)\b.*\b(récepteur|recepteur|receiver)\b)"
)
_PROJECTOR_UNIT_SIGNAL_RE = re.compile(
    r"(?i)\b("
    r"projecteur|projector|vid[eé]o\s*projecteur|vid[eé]oprojecteur|"
    r"epson|benq|optoma|viewsonic|sony|acer|philips|nec|panasonic|jvc|lg|xiaomi|"
    r"lumens?|lumen|ansi\s*lum|"
    r"uhd\s*\d{2,3}[a-z0-9]*|hd\s*\d{3,4}[a-z0-9]*|eh[-\s]?tw|eb[-\s]?\d|tw\d{3,4}|w\d{4}|"
    r"xga|wxga|svga|full\s*hd|1080p|4k|throw\s*ratio"
    r")\b"
)
_RECOGNIZED_PROJECTOR_BRANDS = frozenset(
    {"epson", "benq", "optoma", "viewsonic", "sony", "acer", "philips", "nec", "panasonic", "jvc", "lg", "xiaomi"}
)
_MINI_PROJECTOR_RE = re.compile(
    r"(?i)\b(pico\s*proj|picoprojecteur|mini\s*projecteur|projecteur\s*de\s*poche|"
    r"pocket\s*projector|led\s*projector|wanbo|xgimi\s*mogo)\b"
)


def _is_hdmi_adapter_without_projector(text: str) -> bool:
    if not re.search(r"(?i)\badaptateur\b", text):
        return False
    if re.search(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|vid[eé]oprojecteur)\b", text):
        return False
    return bool(
        re.search(
            r"(?i)\b(hdmi|affichage\s*sans\s*fil|wireless|miracast|chromecast|dongle|bluetooth)\b",
            text,
        )
    )


def is_projector_accessory_text(text: str) -> bool:
    if not text:
        return False
    if _WIRELESS_DISPLAY_ACCESSORY_RE.search(text):
        return True
    if _HDMI_RECEIVER_ACCESSORY_RE.search(text):
        return True
    if _is_hdmi_adapter_without_projector(text):
        return True
    if _PROJECTOR_ACCESSORY_RE.search(text):
        if _PROJECTOR_UNIT_SIGNAL_RE.search(text) and re.search(
            r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|vid[eé]oprojecteur|optoma|epson|benq)\b",
            text,
        ):
            return False
        return True
    if _SCREEN_ONLY_RE.search(text) and not re.search(
        r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|vid[eé]oprojecteur)\b", text
    ):
        return True
    return False


def has_projector_unit_signal(text: str) -> bool:
    if not text:
        return False
    if is_projector_accessory_text(text):
        return False
    return bool(_PROJECTOR_UNIT_SIGNAL_RE.search(text))


def has_projector_signal(row: Dict[str, Any]) -> bool:
    if row.get("is_accessory"):
        return False
    specs = row.get("extracted_specs") if isinstance(row.get("extracted_specs"), dict) else {}
    if specs.get("is_accessory_only"):
        return False
    text = _row_text(row)
    if is_projector_accessory_text(text):
        return False
    try:
        lum = int(specs.get("lumens") or row.get("lumens") or 0)
    except (TypeError, ValueError):
        lum = 0
    brand = str(specs.get("projector_brand") or row.get("projector_brand") or "").lower()
    if lum > 0 or brand in _RECOGNIZED_PROJECTOR_BRANDS:
        return True
    if has_projector_unit_signal(text):
        return True
    if re.search(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur|vid[eé]oprojecteur)\b", text):
        return not is_projector_accessory_text(text)
    return False


_MARKETING_LUMENS_RE = re.compile(r"(?i)\b(\d{4,5})\s*(?:lumens?\s*)?(?:marketing|led|ansi\s*équivalent)\b")
_GROUPSSET_RE = re.compile(
    r"(?i)\b(shimano|sram|campagnolo|tiagra|105|ultegra|dura[- ]?ace|"
    r"deore|xt|xtr|claris|sora)\b"
)
_MATERIAL_RE = re.compile(r"(?i)\b(carbone|carbon|alu|aluminium|acier|chromoly|titane)\b")
_EBIKE_RE = re.compile(r"(?i)\b(e[- ]?bike|vae|vélo\s*électrique|electrique|ebike|bosch|shimano\s*steps)\b")
_WHEEL_SIZE_RE = re.compile(r"(?i)\b(26|27\.5|27,5|29)\s*(?:\"|pouces|inch|')?\b")
_BIKE_USAGE_RE = re.compile(r"(?i)\b(vtt|vtc|route|gravel|ville|urbain|bmx|cyclo)\b")
_BIKE_BRANDS_RE = re.compile(
    r"(?i)\b(trek|specialized|cannondale|giant|decathlon|btwin|scott|cube|"
    r"lapierre|canyon|orbea|bianchi|peugeot|gitane|rockrider|riverside)\b"
)
_PS4_ACCESSORY_RE = re.compile(
    r"(?i)\b(volant|thrustmaster|skylanders|manette\s*seule|casque\s*ps4|"
    r"support\s*ps4|chargeur\s*seul|cable\s*hdmi\s*seul)\b"
)
_DS_MODEL_RE = re.compile(r"(?i)\b(new\s*)?(2ds|3ds|dsi|ds\s*lite|ds\s*xl|3ds\s*xl)\b")
_PS4_MODEL_RE = re.compile(r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4)\s*(slim|pro)?\b")
_CONSOLE_PS4_WRONG_PLATFORM = re.compile(
    r"(?i)\b("
    r"xbox(\s*(360|one|series\s*[xs]))?|\brgh\b|"
    r"nintendo\s*switch|switch\s*(oled|lite)?|"
    r"super\s*famicom|snes|famicom|\bnes\b|n64|gamecube|wii\s*u?|"
    r"ps[235]\b|playstation\s*[235]|psp|ps\s*vita|steam\s*deck"
    r")\b"
)
_PS4_STORAGE_RE = re.compile(r"(?i)\b(500\s*go|500\s*gb|1\s*to|1\s*tb|1000\s*go)\b")
_PS4_CONTROLLER_RE = re.compile(
    r"(?i)\b(\d\s*manettes?|2\s*manettes?|avec\s*manette|manette\s*incluse|dual\s*shock|dualshock|sans\s*manette|console\s*seule)\b"
)
_PS4_GAMES_RE = re.compile(r"(?i)\b(lot\s+de\s+(\d+)\s+jeux|(\d+)\s+jeux\s+inclus|jeux\s+inclus)\b")
_ICLOUD_RE = re.compile(r"(?i)\b(icloud\s*lock|verrouillage\s*icloud|bloqué\s*icloud)\b")
_SIZE_ALPHA_RE = re.compile(r"(?i)\b(taille\s*)?([xsml]{1,2}|3[468]|[0-9]{2})\b")


def _listing_text(listing: Dict[str, Any]) -> str:
    return f"{listing.get('title') or ''} {listing.get('description') or ''}".strip()


def _norm_phone_text(text: str) -> str:
    return re.sub(r"(?i)i\s*-?\s*phone", "iphone", text or "")


# ---------------------------------------------------------------------------
# Per-dimension extractors (rule-based + cached AI fields on listing)
# ---------------------------------------------------------------------------


def _merge_row_fields(specs: Dict[str, Any], listing: Dict[str, Any], keys: Tuple[str, ...]) -> None:
    for k in keys:
        v = listing.get(k)
        if v is not None and v != "" and specs.get(k) in (None, "", 0):
            specs[k] = v


def extract_phone_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _norm_phone_text(_listing_text(listing))
    specs: Dict[str, Any] = {}
    _merge_row_fields(
        specs,
        listing,
        ("storage_gb", "battery_health_pct", "product_model_key", "product_model_label", "icloud_locked"),
    )
    if not specs.get("storage_gb"):
        sm = _STORAGE_RE.search(text)
        if sm:
            specs["storage_gb"] = int(sm.group(1))
    bat = specs.get("battery_health_pct") or __extract_battery_pct(text)
    if bat:
        specs["battery_health_pct"] = int(bat)
    if _ICLOUD_RE.search(text):
        specs["icloud_locked"] = True
    if not specs.get("product_model_key"):
        try:
            from .engine import infer_phone_model

            key, label = infer_phone_model({"title": text, **listing})
            if key != "unknown":
                specs["product_model_key"] = key
                specs["product_model_label"] = label
        except Exception:
            pass
    if _CONDITION_GOOD.search(text):
        specs["condition_hint"] = "tres bon"
    elif _CONDITION_WEAR.search(text):
        specs["condition_hint"] = "usure"
    return specs


def extract_bicycle_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    _merge_row_fields(specs, listing, ("frame_size", "frame_material", "groupset", "is_ebike", "wheel_size", "bike_brand", "bike_usage"))
    wm = _WHEEL_SIZE_RE.search(text)
    if wm and not specs.get("wheel_size"):
        specs["wheel_size"] = wm.group(1).replace(",", ".")
    bm = _BIKE_BRANDS_RE.search(text)
    if bm and not specs.get("bike_brand"):
        specs["bike_brand"] = bm.group(1).capitalize()
    um = _BIKE_USAGE_RE.search(text)
    if um and not specs.get("bike_usage"):
        tok = um.group(1).lower()
        specs["bike_usage"] = {"vtt": "vtt", "vtc": "ville", "route": "route", "gravel": "route", "ville": "ville", "urbain": "ville", "bmx": "ville", "cyclo": "route"}.get(tok, tok)
    m = _FRAME_SIZE_RE.search(text)
    if m and not specs.get("frame_size"):
        if m.group(1):
            specs["frame_size"] = m.group(1)
        elif m.group(2):
            specs["frame_size"] = m.group(2).upper()
        elif m.group(3) and m.group(4):
            specs["frame_size"] = f"{m.group(3)}/{m.group(4).upper()}"
    mat = _MATERIAL_RE.search(text)
    if mat and not specs.get("frame_material"):
        specs["frame_material"] = mat.group(1).capitalize()
    gs = _GROUPSSET_RE.search(text)
    if gs and not specs.get("groupset"):
        specs["groupset"] = gs.group(1)
    if _EBIKE_RE.search(text):
        specs["is_ebike"] = True
        bat = __extract_battery_pct(text)
        if bat:
            specs["battery_health_pct"] = int(bat)
    if _CONDITION_GOOD.search(text):
        specs["condition_hint"] = "tres bon"
    elif _CONDITION_WEAR.search(text):
        specs["condition_hint"] = "usure"
    return specs


def extract_projector_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    _merge_row_fields(
        specs,
        listing,
        ("lumens", "projector_brand", "resolution", "has_hdmi", "lamp_hours", "projector_usage", "product_model_key"),
    )
    lm = _LUMENS_RE.search(text)
    if lm and not specs.get("lumens"):
        specs["lumens"] = int(lm.group(1) or lm.group(3) or lm.group(4) or 0)
    mkt = _MARKETING_LUMENS_RE.search(text)
    if mkt and _MINI_PROJECTOR_RE.search(text):
        specs["marketing_lumens"] = int(mkt.group(1))
        if not specs.get("lumens") or int(specs.get("lumens") or 0) > 1500:
            specs["lumens"] = min(int(specs.get("lumens") or int(mkt.group(1))), 450)
    if not specs.get("projector_brand"):
        for brand in ("epson", "benq", "optoma", "viewsonic", "sony", "acer", "lg", "xiaomi", "philips", "nec"):
            if re.search(rf"(?i)\b{brand}\b", text):
                specs["projector_brand"] = brand
                break
    if not specs.get("projector_brand") and re.search(r"(?i)\buhd\s*\d{2,3}[a-z0-9]*\b", text):
        specs["projector_brand"] = "optoma"
    if is_projector_accessory_text(text):
        specs["is_accessory_only"] = True
        specs["listing_type"] = "accessory"
    if not specs.get("resolution"):
        if re.search(r"(?i)\b4k\b|\buhd\b", text):
            specs["resolution"] = "4k"
        elif re.search(r"(?i)\b1080p\b|full\s*hd", text):
            specs["resolution"] = "1080p"
        elif re.search(r"(?i)\b720p\b", text):
            specs["resolution"] = "720p"
        elif re.search(r"(?i)\bwxga\b", text):
            specs["resolution"] = "wxga"
        elif re.search(r"(?i)\bxga\b|\bsvga\b", text):
            specs["resolution"] = "xga"
    if re.search(r"(?i)\bhdmi\b", text):
        specs["has_hdmi"] = True
    if _SCREEN_ONLY_RE.search(text) and not re.search(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur)\b", text):
        specs["is_screen_only"] = True
        specs["is_accessory_only"] = True
    if _MINI_PROJECTOR_RE.search(text):
        specs["is_mini_projector"] = True
        try:
            if int(specs.get("lumens") or 0) >= 2000:
                specs["lumens"] = min(int(specs["lumens"]), 500)
        except (TypeError, ValueError):
            pass
    if re.search(r"(?i)\b(bureau|office|présentation|presentation|wxga)\b", text):
        specs["projector_usage"] = "office"
    elif re.search(r"(?i)\b(cin[eé]ma\s*maison|home\s*cinema|home\s*theater)\b", text):
        specs["projector_usage"] = "cinema"
    elif re.search(r"(?i)\b(portable|poche|nomade|voyage)\b", text):
        specs["projector_usage"] = "portable"
    elif re.search(r"(?i)\b(salon|maison|home)\b", text):
        specs["projector_usage"] = "home"
    if re.search(r"(?i)\b(écran|ecran|screen)\b", text) and re.search(
        r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur)\b", text
    ):
        specs["has_screen_bundle"] = True
    lh = re.search(r"(?i)\b(\d{1,5})\s*h\b", text)
    if lh and not specs.get("lamp_hours"):
        specs["lamp_hours"] = int(lh.group(1))
    _enrich_projector_model_defaults(specs, listing, text)
    return specs


_PROJECTOR_MODEL_DEFAULTS = {
    "optoma_hd146x": {"lumens": 3600, "resolution": "1080p", "projector_brand": "optoma"},
    "optoma_uhd38": {"lumens": 3800, "resolution": "4k", "projector_brand": "optoma", "has_hdmi": True},
    "optoma_uhd38x": {"lumens": 3800, "resolution": "4k", "projector_brand": "optoma", "has_hdmi": True},
    "epson_eb_955wh": {"lumens": 3600, "resolution": "wxga", "projector_brand": "epson"},
    "epson_eh_tw650": {"lumens": 3500, "resolution": "1080p", "projector_brand": "epson"},
    "benq_w1700": {"lumens": 2200, "resolution": "1080p", "projector_brand": "benq"},
}


def _enrich_projector_model_defaults(specs: Dict[str, Any], listing: Dict[str, Any], text: str) -> None:
    key = str(listing.get("product_model_key") or specs.get("product_model_key") or "")
    if not key or key == "unknown":
        blob = text.lower()
        for mk, defaults in _PROJECTOR_MODEL_DEFAULTS.items():
            token = mk.replace("_", " ")
            if token in blob or mk.replace("_", "-") in blob:
                key = mk
                break
    if key and key in _PROJECTOR_MODEL_DEFAULTS:
        specs.setdefault("product_model_key", key)
        for k, v in _PROJECTOR_MODEL_DEFAULTS[key].items():
            if not specs.get(k):
                specs[k] = v
    if not key or key == "unknown":
        m_uhd = re.search(r"(?i)\buhd\s*(\d{2,3})([a-z]?)\b", text)
        if m_uhd:
            suffix = (m_uhd.group(2) or "").lower()
            mk = f"optoma_uhd{m_uhd.group(1)}{suffix}"
            if mk in _PROJECTOR_MODEL_DEFAULTS:
                specs.setdefault("product_model_key", mk)
                for k, v in _PROJECTOR_MODEL_DEFAULTS[mk].items():
                    if not specs.get(k):
                        specs[k] = v


def extract_console_ps4_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    _merge_row_fields(
        specs,
        listing,
        ("variant", "storage_gb", "has_controller", "has_hdmi", "has_power_cable", "has_box", "includes_games"),
    )
    m = _PS4_MODEL_RE.search(text)
    if m and not specs.get("variant"):
        mod = (m.group(2) or "standard").lower()
        specs["variant"] = mod if mod in ("slim", "pro") else "standard"
    if not specs.get("storage_gb"):
        sm = _PS4_STORAGE_RE.search(text)
        if sm:
            tok = sm.group(1).lower().replace(" ", "")
            specs["storage_gb"] = 1000 if "1to" in tok or "1tb" in tok or "1000" in tok else 500
    cm = _PS4_CONTROLLER_RE.search(text)
    if cm:
        blob = cm.group(0).lower()
        if "sans manette" in blob or "console seule" in blob:
            specs["has_controller"] = False
            specs["controller_count"] = 0
        elif re.search(r"(?i)\b2\s*manettes?\b", blob):
            specs["has_controller"] = True
            specs["controller_count"] = 2
        else:
            specs["has_controller"] = True
            specs["controller_count"] = 1
    gm = _PS4_GAMES_RE.search(text)
    if gm:
        try:
            n = int(gm.group(2) or gm.group(3) or 1)
            specs["games_count"] = n
            specs["includes_games"] = n > 0
        except (TypeError, ValueError):
            specs["includes_games"] = True
    if re.search(r"(?i)\bjeu(x)?\s+seul|lot\s+de\s+\d+\s+jeux\b", text) and not re.search(
        r"(?i)\bconsole\b", text
    ):
        specs["listing_type"] = "games_only"
        specs["is_games_only"] = True
    elif re.search(r"(?i)\bconsole\b", text) or _PS4_MODEL_RE.search(text):
        specs["listing_type"] = "console"
    elif _PS4_ACCESSORY_RE.search(text) and not _PS4_MODEL_RE.search(text):
        specs["listing_type"] = "accessory"
        specs["is_accessory_only"] = True
    if re.search(r"(?i)\b(hdmi)\b", text):
        specs["has_hdmi"] = True
    if re.search(r"(?i)\b(alimentation|chargeur|câble|hdmi)\b", text):
        specs["has_power_cable"] = True
    if re.search(r"(?i)\b(bo[iî]te|complet)\b", text):
        specs["has_box"] = True
        specs["complete_hint"] = True
    if re.search(r"(?i)\b(neuf|sous\s*blister)\b", text):
        specs["condition_hint"] = "neuf"
    elif _CONDITION_GOOD.search(text):
        specs["condition_hint"] = "tres bon"
    elif _CONDITION_WEAR.search(text):
        specs["condition_hint"] = "defauts"
    variant = str(specs.get("variant") or "").lower()
    if variant in ("slim", "pro", "standard"):
        key = f"ps4_{variant}" if variant != "standard" else "ps4_slim"
        try:
            storage = int(specs.get("storage_gb") or 0)
        except (TypeError, ValueError):
            storage = 0
        if storage >= 1000:
            key = f"{key}_1tb"
        elif storage >= 500:
            key = f"{key}_500gb"
        specs["product_model_key"] = key
    return specs


def extract_console_ds_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    _merge_row_fields(specs, listing, ("ds_model_family", "has_charger", "has_stylus", "has_box"))
    dm = _DS_MODEL_RE.search(text)
    if dm and not specs.get("ds_model_family"):
        specs["ds_model_family"] = dm.group(0).strip().lower().replace(" ", "_")
    if re.search(r"(?i)\b(chargeur|alimentation)\b", text):
        specs["has_charger"] = True
    if re.search(r"(?i)\b(stylet|stylus)\b", text):
        specs["has_stylus"] = True
    if re.search(r"(?i)\b(bo[iî]te|complet)\b", text):
        specs["has_box"] = True
    if _CONDITION_GOOD.search(text):
        specs["condition_hint"] = "tres bon"
    return specs


def extract_apparel_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    _merge_row_fields(specs, listing, ("size", "brand", "condition_hint"))
    if listing.get("brand"):
        specs["brand"] = listing["brand"]
    if listing.get("size"):
        specs["size"] = listing["size"]
    elif not specs.get("size"):
        sm = _SIZE_ALPHA_RE.search(text)
        if sm:
            specs["size"] = sm.group(2)
    if re.search(r"(?i)\b(neuf|avec\s*étiquette|tags)\b", text):
        specs["condition_hint"] = "neuf"
    elif _CONDITION_WEAR.search(text):
        specs["condition_hint"] = "defauts"
    auth = extract_apparel_authenticity(text)
    if auth:
        specs["authenticity"] = auth
    return specs


def extract_generic_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    if _CONDITION_GOOD.search(text):
        specs["condition_hint"] = "tres bon"
    elif _CONDITION_WEAR.search(text):
        specs["condition_hint"] = "usure"
    if listing.get("brand"):
        specs["brand"] = listing["brand"]
    return specs


_CPU_RE = re.compile(
    r"(?i)\b(i[3579][\s-]?\d{4,5}[a-z]{0,3}|ryzen\s*[3579]\s*\d{4}|m[12]\s*(?:pro|max)?|"
    r"core\s*i[3579]|apple\s*m[12])\b"
)
_RAM_RE = re.compile(r"(?i)\b(\d{1,2})\s*(?:go|gb)\s*(?:de\s*)?ram\b|\bram\s*(\d{1,2})\s*(?:go|gb)\b")
_GPU_RE = re.compile(
    r"(?i)\b(rtx\s*\d{3,4}(?:\s*ti)?|gtx\s*\d{3,4}|rx\s*\d{4}|iris\s*xe|uhd\s*graphics|"
    r"radeon\s*\w+|geforce\s*\w+)\b"
)
_SCREEN_INCH_RE = re.compile(r"(?i)\b(\d{2}(?:[.,]\d)?)\s*(?:\"|pouces|inch|')\b")
_STORAGE_LAPTOP_RE = re.compile(r"(?i)\b(ssd\s*)?(\d{3,4})\s*(go|gb|to|tb)\b")
_AUTHENTICITY_RE = re.compile(
    r"(?i)\b(réplique|replique|fake|contrefa[cç]on|copie|inspired|dupe|aaa)\b"
)


def extract_laptop_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}
    _merge_row_fields(specs, listing, ("cpu", "ram_gb", "storage_gb", "gpu", "screen_inches", "brand"))
    cm = _CPU_RE.search(text)
    if cm and not specs.get("cpu"):
        specs["cpu"] = cm.group(0).strip()
    rm = _RAM_RE.search(text)
    if rm and not specs.get("ram_gb"):
        specs["ram_gb"] = int(rm.group(1) or rm.group(2))
    gm = _GPU_RE.search(text)
    if gm and not specs.get("gpu"):
        specs["gpu"] = gm.group(0).strip()
    sm = _SCREEN_INCH_RE.search(text)
    if sm and not specs.get("screen_inches"):
        try:
            specs["screen_inches"] = float(sm.group(1).replace(",", "."))
        except (TypeError, ValueError):
            pass
    if not specs.get("storage_gb"):
        sm = _STORAGE_RE.search(text)
        if sm:
            specs["storage_gb"] = int(sm.group(1))
        else:
            lm = _STORAGE_LAPTOP_RE.search(text)
            if lm:
                specs["storage_gb"] = int(lm.group(2))
    if _CONDITION_GOOD.search(text):
        specs["condition_hint"] = "tres bon"
    elif _CONDITION_WEAR.search(text):
        specs["condition_hint"] = "usure"
    return specs


def extract_apparel_authenticity(text: str) -> Optional[str]:
    if _AUTHENTICITY_RE.search(text):
        return "doute"
    if re.search(r"(?i)\b(authentic|authentique|certificat|facture)\b", text):
        return "ok"
    return None


# ---------------------------------------------------------------------------
# AC / Climatisation spec extraction
# ---------------------------------------------------------------------------

_AC_BTU_RE = re.compile(
    r"(?i)\b(\d{4,6})\s*(?:btu(?:/h(?:eure)?)?|british\s+thermal)\b"
)
_AC_KW_RE = re.compile(
    r"(?i)\b(\d+(?:[.,]\d+)?)\s*k[Ww](?:\s*/\s*(?:froid|cooling|refroidissement))?\b"
)
_AC_ROOM_M2_RE = re.compile(
    r"(?i)\b(\d{1,3})\s*m[²2](?:\s+(?:maxi(?:mum)?|recommand[eé]|pi[eè]ce))?\b"
)
_AC_TYPE_RE = re.compile(
    r"(?i)\b(monosplit|bi[\s-]?split|multi[\s-]?split|r[eé]versible|mono[\s-]?bloc|"
    r"clim(?:atiseur)?\s+mobile|climatiseur\s+fixe|split\s+system|cassette|gainable)\b"
)
_AC_BRAND_RE = re.compile(
    r"(?i)\b(daikin|mitsubishi|fujitsu|toshiba|panasonic|lg|samsung|midea|gree|haier|"
    r"atlantic|hitachi|carrier|airton|olimpia|klarstein|delonghi|whirlpool|beko|"
    r"electrolux|bosch|trotec|mondial|cecotec|comfee|inventor|cooper\s*&\s*hunter)\b"
)
_AC_ACCESSORY_TITLE_RE = re.compile(
    r"(?i)\b("
    r"t[eé]l[eé]commande|composteur|"
    r"tuyau(?:\s+flexible)?\s+(?:de\s+)?clim|"
    r"conduit(?:e)?\s+(?:de\s+)?clim|kit\s+(?:installation|conduit|fen[eê]tre)\s+clim|"
    r"kit\s+fen[eê]tre|fen[eê]tre.*clim|"
    r"filtre\s+(?:de\s+)?clim(?:atiseur)?|filtre\s+air\s+clim|"
    r"support\s+(?:de\s+)?clim(?:atiseur)?|"
    r"pi[eè]ce\s+d[eé]tach[eé]e\s+(?:de\s+)?clim|"
    r"recharge\s+(?:gaz\s+)?clim|entretien\s+clim|s[eé]ance\s+clim"
    r")\b"
)
_AC_UNIT_SIGNAL_RE = re.compile(
    r"(?i)\b(\d{4,6}\s*btu|split|monosplit|clim(?:atiseur)?\s+(?:mobile|fixe|r[eé]versible)|"
    r"clim\s+mobile|climatisation\s+(?:mobile|r[eé]versible|fixe)|"
    r"pompe\s+[àa]\s+chaleur|pac\b|"
    r"unit[eé]\s+(?:int[eé]rieure|ext[eé]rieure))\b"
)


def ac_has_unit_signal(text: str) -> bool:
    """True when the listing text signals a real AC unit (not just the substring 'clim')."""
    if _AC_ACCESSORY_TITLE_RE.search(text or ""):
        return False
    return bool(_AC_UNIT_SIGNAL_RE.search(text or ""))


def infer_ac_model_key(specs: Dict[str, Any]) -> str:
    """Grouping key for median-by-model AC scoring."""
    brand = str(specs.get("brand") or "").strip().lower()
    ac_type = str(specs.get("ac_type") or "").strip().lower().replace(" ", "_")
    btu = specs.get("capacity_btu")
    parts: List[str] = ["ac"]
    if brand:
        parts.append(brand)
    if ac_type:
        parts.append(ac_type[:24])
    if btu:
        try:
            parts.append(str(int(int(btu) // 1000) * 1000))
        except (TypeError, ValueError):
            pass
    return "_".join(parts) if len(parts) > 1 else ""


def extract_ac_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    """Extract BTU, power, type, brand and accessory flag for AC listings."""
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}

    # Capacity in BTU
    bm = _AC_BTU_RE.search(text)
    if bm:
        try:
            specs["capacity_btu"] = int(bm.group(1))
        except ValueError:
            pass

    # Power in kW (cooling / heating)
    km = _AC_KW_RE.search(text)
    if km:
        try:
            specs["cooling_kw"] = float(km.group(1).replace(",", "."))
        except ValueError:
            pass

    # If we have kW but not BTU, derive BTU approximation (1 kW ≈ 3412 BTU)
    if specs.get("cooling_kw") and not specs.get("capacity_btu"):
        specs["capacity_btu"] = int(specs["cooling_kw"] * 3412)

    # Room area in m²
    rm = _AC_ROOM_M2_RE.search(text)
    if rm:
        try:
            specs["room_area_m2"] = int(rm.group(1))
        except ValueError:
            pass

    # AC type
    tm = _AC_TYPE_RE.search(text)
    if tm:
        specs["ac_type"] = tm.group(0).strip().lower()

    # Brand
    brm = _AC_BRAND_RE.search(text)
    if brm:
        specs["brand"] = brm.group(0).strip().lower()
    elif listing.get("brand"):
        specs["brand"] = str(listing["brand"]).lower()

    model_key = infer_ac_model_key(specs)
    if model_key:
        specs["product_model_key"] = model_key
        label_bits = []
        if specs.get("brand"):
            label_bits.append(str(specs["brand"]).capitalize())
        if specs.get("capacity_btu"):
            label_bits.append(f"{specs['capacity_btu']} BTU")
        elif specs.get("cooling_kw"):
            label_bits.append(f"{specs['cooling_kw']} kW")
        if specs.get("ac_type"):
            label_bits.append(str(specs["ac_type"]).capitalize())
        if label_bits:
            specs["product_model_label"] = " ".join(label_bits)

    # Detect accessory-only listing (telecommande, filtre, tuyau…)
    title = str(listing.get("title") or "")
    if _AC_ACCESSORY_TITLE_RE.search(title):
        specs["is_accessory_only"] = True
        specs["ac_is_unit"] = False
    elif _AC_UNIT_SIGNAL_RE.search(text):
        specs["ac_is_unit"] = True
    else:
        # Ambiguous: no BTU/kW and no clear unit signal → mark as uncertain
        has_power = bool(specs.get("capacity_btu") or specs.get("cooling_kw"))
        specs["ac_is_unit"] = has_power

    return specs


_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "phone": extract_phone_specs,
    "bicycle": extract_bicycle_specs,
    "projector": extract_projector_specs,
    "console_ps4": extract_console_ps4_specs,
    "console_ps5": extract_console_ps4_specs,
    "console_ds": extract_console_ds_specs,
    "apparel": extract_apparel_specs,
    "laptop_pc": extract_laptop_specs,
    "ac": extract_ac_specs,
    "generic": extract_generic_specs,
}


def extract_listing_specs(listing: Dict[str, Any], category: str) -> Dict[str, Any]:
    """Unified spec extraction: rules + fields already on the listing row."""
    cat = normalize_category(category)
    fn = _EXTRACTORS.get(cat, extract_generic_specs)
    specs = fn(listing)
    try:
        from .engine import merge_normalized_specs, normalize_specs_from_text

        merge_normalized_specs(specs, normalize_specs_from_text(_listing_text(listing), cat))
    except Exception:
        pass
    return {k: v for k, v in specs.items() if v not in (None, "", False) or k in ("icloud_locked", "has_hdmi")}


# ---------------------------------------------------------------------------
# Category profiles
# ---------------------------------------------------------------------------

@dataclass
class SpecDimension:
    id: str
    label: str
    weight: float = 1.0


@dataclass
class CategoryProfile:
    dimensions: List[SpecDimension] = field(default_factory=list)
    title_patterns: List[str] = field(default_factory=list)
    llm_criteria_hint: str = ""
    avoid_patterns: List[str] = field(default_factory=list)
    scoring_dimensions: List[str] = field(default_factory=list)
    enrichment_fields: List[str] = field(default_factory=list)
    completeness_weights: Dict[str, float] = field(default_factory=dict)
    excel_spec_columns: List[Tuple[str, str]] = field(default_factory=list)


CATEGORY_PROFILES: Dict[str, CategoryProfile] = {
    "phone": CategoryProfile(
        dimensions=[
            SpecDimension("model_exact", "Modèle exact", 1.2),
            SpecDimension("storage_gb", "Stockage", 0.9),
            SpecDimension("battery_health_pct", "Santé batterie %", 1.0),
            SpecDimension("condition", "État / défauts", 1.0),
            SpecDimension("icloud_locked", "Verrou iCloud", 0.8),
            SpecDimension("price_vs_market", "Prix vs marché", 1.1),
        ],
        title_patterns=[r"iphone", r"galaxy", r"pixel", r"smartphone"],
        llm_criteria_hint=(
            "Modèle exact, stockage Go, % batterie si indiqué, état/écran, verrou iCloud, "
            "prix vs médiane. Ne pas inventer de specs absentes."
        ),
        avoid_patterns=["pro", "plus", "pro max", "max"],
        scoring_dimensions=["price_value", "relevance", "condition", "battery_health", "seller_trust", "completeness"],
        enrichment_fields=["product_model_key", "product_model_label", "storage_gb", "battery_health_pct", "condition", "icloud_locked"],
        completeness_weights={"model": 1.2, "storage_gb": 0.9, "battery_health_pct": 1.0, "condition": 1.0, "icloud_locked": 0.8},
        excel_spec_columns=[
            ("product_model_label", "Modèle"),
            ("storage_gb", "Stockage (Go)"),
            ("battery_health_pct", "Batterie %"),
            ("icloud_locked", "iCloud verrouillé"),
            ("condition", "État"),
        ],
    ),
    "bicycle": CategoryProfile(
        dimensions=[
            SpecDimension("wheel_size", "Taille roues", 1.1),
            SpecDimension("frame_size", "Taille cadre", 1.2),
            SpecDimension("bike_brand", "Marque", 0.9),
            SpecDimension("is_ebike", "Électrique (VAE)", 0.8),
            SpecDimension("bike_usage", "Usage", 0.8),
            SpecDimension("frame_material", "Matériau", 0.8),
            SpecDimension("groupset", "Groupe", 0.9),
            SpecDimension("condition", "État / usure", 1.0),
            SpecDimension("price_vs_market", "Prix vs marché", 1.0),
        ],
        title_patterns=[r"vélo", r"velo", r"bike", r"vtt", r"vtc", r"gravel", r"bmx"],
        llm_criteria_hint=(
            "Taille roues (26/27.5/29), taille cadre (cm ou S/M/L), marque, VAE ou musculaire, "
            "usage route/VTT/ville, matériau, groupe transmission, état."
        ),
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
        enrichment_fields=["wheel_size", "frame_size", "bike_brand", "is_ebike", "groupset", "bike_usage", "condition"],
        completeness_weights={"wheel_size": 1.1, "frame_size": 1.2, "bike_brand": 0.9, "is_ebike": 0.8, "groupset": 0.9, "bike_usage": 0.8, "condition": 1.0},
        excel_spec_columns=[
            ("wheel_size", "Taille roues"),
            ("frame_size", "Taille cadre"),
            ("bike_brand", "Marque"),
            ("is_ebike", "Électrique"),
            ("groupset", "Groupe"),
            ("bike_usage", "Usage"),
            ("condition", "État"),
        ],
    ),
    "projector": CategoryProfile(
        dimensions=[
            SpecDimension("lumens", "Lumens ANSI", 1.2),
            SpecDimension("resolution", "Résolution", 1.0),
            SpecDimension("projector_brand", "Marque", 0.8),
            SpecDimension("lamp_hours", "Heures lampe", 0.7),
            SpecDimension("has_hdmi", "HDMI", 0.9),
            SpecDimension("price_vs_market", "Prix vs marché", 1.0),
        ],
        title_patterns=[r"projecteur", r"projector", r"epson", r"benq", r"optoma"],
        llm_criteria_hint=(
            "Lumens réels (ANSI), résolution native, marque/modèle, heures lampe, HDMI. "
            "Pénaliser dinosaures VGA seul ou lumens marketing gonflés."
        ),
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
        enrichment_fields=["lumens", "resolution", "projector_brand", "lamp_hours", "has_hdmi", "condition"],
        completeness_weights={"lumens": 1.2, "resolution": 1.0, "projector_brand": 0.8, "lamp_hours": 0.7, "has_hdmi": 0.9},
        excel_spec_columns=[
            ("lumens", "Lumens ANSI"),
            ("resolution", "Résolution"),
            ("projector_brand", "Marque"),
            ("lamp_hours", "Heures lampe"),
            ("has_hdmi", "HDMI"),
            ("condition", "État"),
        ],
    ),
    "console_ds": CategoryProfile(
        dimensions=[
            SpecDimension("ds_model_family", "Modèle (2DS/3DS/XL)", 1.2),
            SpecDimension("completeness", "Complétude pack", 1.0),
            SpecDimension("condition", "État", 1.0),
            SpecDimension("price_vs_market", "Prix vs marché", 1.0),
        ],
        title_patterns=[r"3ds", r"2ds", r"nintendo\s*ds"],
        llm_criteria_hint="Variante exacte 2DS vs 3DS XL, chargeur/stylet/jeux inclus, état écran/charnière.",
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
    ),
    "console_ps4": CategoryProfile(
        dimensions=[
            SpecDimension("variant", "Modèle (Slim/Pro)", 1.1),
            SpecDimension("storage_gb", "Stockage", 0.9),
            SpecDimension("has_controller", "Manette", 1.2),
            SpecDimension("includes_games", "Jeux inclus", 0.7),
            SpecDimension("condition", "État", 1.0),
            SpecDimension("completeness", "Pack complet", 1.0),
            SpecDimension("price_vs_market", "Prix vs marché", 1.0),
        ],
        title_patterns=[r"ps4", r"playstation\s*4", r"play\s*4"],
        llm_criteria_hint=(
            "Console PS4 (pas jeux seuls) : modèle Slim/Pro, stockage 500Go/1To, manette(s), "
            "câbles HDMI/alim, jeux inclus si lot. Pénaliser jeux seuls et accessoires."
        ),
        avoid_patterns=["jeu seul", "lot de jeux", "manette seule", "thrustmaster", "skylanders"],
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
        enrichment_fields=["variant", "model", "storage_gb", "controllers_count", "games_count", "games_list", "premium_games", "condition", "has_box", "has_hdmi", "has_power_cable"],
        completeness_weights={"variant": 1.1, "storage_gb": 0.9, "controllers_count": 1.2, "games_count": 0.7, "premium_games": 0.8, "condition": 1.0, "has_box": 0.6, "has_hdmi": 0.5, "has_power_cable": 0.5},
        excel_spec_columns=[
            ("variant", "Modèle"),
            ("storage_gb", "Stockage (Go)"),
            ("controllers_count", "Manettes"),
            ("games_count", "Nb jeux"),
            ("games_list", "Liste jeux"),
            ("premium_games", "Jeux premium"),
            ("condition", "État"),
            ("has_box", "Boîte"),
            ("has_hdmi", "HDMI"),
            ("has_power_cable", "Alimentation"),
        ],
    ),
    "apparel": CategoryProfile(
        dimensions=[
            SpecDimension("size", "Taille", 1.1),
            SpecDimension("brand", "Marque", 0.9),
            SpecDimension("condition", "État", 1.0),
            SpecDimension("authenticity", "Signaux authenticité", 0.8),
            SpecDimension("price_vs_market", "Prix vs marché", 1.0),
        ],
        title_patterns=[r"taille", r"hoodie", r"sweat", r"jean", r"basket"],
        llm_criteria_hint="Taille exacte, marque, état (taches/trous), signaux contrefaçon si mentionnés.",
        scoring_dimensions=["price_value", "relevance", "condition", "seller_trust"],
        enrichment_fields=["brand", "size", "condition", "authenticity"],
        completeness_weights={"brand": 0.9, "size": 1.1, "condition": 1.0, "authenticity": 0.8},
        excel_spec_columns=[
            ("brand", "Marque"),
            ("size", "Taille"),
            ("condition", "État"),
            ("authenticity", "Authenticité"),
        ],
    ),
    "laptop_pc": CategoryProfile(
        dimensions=[
            SpecDimension("cpu", "Processeur", 1.2),
            SpecDimension("ram_gb", "RAM (Go)", 1.0),
            SpecDimension("storage_gb", "Stockage", 0.9),
            SpecDimension("gpu", "Carte graphique", 1.0),
            SpecDimension("screen_inches", "Écran (pouces)", 0.8),
            SpecDimension("condition", "État", 1.0),
            SpecDimension("price_vs_market", "Prix vs marché", 1.0),
        ],
        title_patterns=[r"macbook", r"laptop", r"portable", r"ordinateur", r"pc\s+portable"],
        llm_criteria_hint="CPU, RAM Go, stockage SSD/Go, GPU dédiée si gaming, taille écran, état batterie/clavier.",
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
        enrichment_fields=["cpu", "ram_gb", "storage_gb", "gpu", "screen_inches", "condition"],
        completeness_weights={"cpu": 1.2, "ram_gb": 1.0, "storage_gb": 0.9, "gpu": 1.0, "screen_inches": 0.8, "condition": 1.0},
        excel_spec_columns=[
            ("cpu", "Processeur"),
            ("ram_gb", "RAM (Go)"),
            ("storage_gb", "Stockage (Go)"),
            ("gpu", "GPU"),
            ("screen_inches", "Écran (pouces)"),
            ("condition", "État"),
        ],
    ),
    "ac": CategoryProfile(
        dimensions=[
            SpecDimension("capacity_btu", "Puissance BTU", 1.2),
            SpecDimension("cooling_kw", "Puissance kW", 1.0),
            SpecDimension("brand", "Marque", 0.9),
            SpecDimension("ac_type", "Type (mobile/split)", 1.0),
            SpecDimension("room_area_m2", "Surface m²", 0.7),
            SpecDimension("condition", "État", 1.0),
            SpecDimension("price_vs_market", "Prix vs marché", 1.1),
        ],
        title_patterns=[r"clim", r"climatiseur", r"btu", r"split", r"pompe\s*[àa]\s*chaleur"],
        llm_criteria_hint=(
            "Unité clim réelle uniquement : BTU ou kW, marque, type mobile/fixe/split, surface m². "
            "Rejeter accessoires (télécommande, filtre, tuyau), vêtements mode, pièces auto, locations."
        ),
        avoid_patterns=[
            "compresseur auto", "télécommande", "filtre", "tuyau", "location",
            "nike", "adidas", "rossignol", "montre", "jouet",
        ],
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
        enrichment_fields=["capacity_btu", "cooling_kw", "brand", "ac_type", "room_area_m2", "condition", "ac_is_unit"],
        completeness_weights={"capacity_btu": 1.2, "brand": 0.9, "ac_type": 1.0, "cooling_kw": 0.8, "condition": 1.0},
        excel_spec_columns=[
            ("capacity_btu", "BTU"),
            ("cooling_kw", "kW froid"),
            ("brand", "Marque"),
            ("ac_type", "Type"),
            ("room_area_m2", "Surface (m²)"),
            ("condition", "État"),
        ],
    ),
    "generic": CategoryProfile(
        dimensions=[
            SpecDimension("condition", "État", 1.0),
            SpecDimension("completeness", "Complétude", 0.8),
            SpecDimension("price_vs_market", "Prix vs marché", 1.1),
            SpecDimension("seller_trust", "Vendeur", 0.7),
            SpecDimension("relevance", "Pertinence recherche", 1.0),
        ],
        title_patterns=[],
        llm_criteria_hint="État, complétude, prix vs marché, pertinence vs requête. Utiliser uniquement specs visibles.",
        scoring_dimensions=["price_value", "relevance", "condition", "completeness", "seller_trust"],
        enrichment_fields=["brand", "condition", "complete_hint"],
        completeness_weights={"brand": 0.8, "condition": 1.0, "complete_hint": 0.8},
        excel_spec_columns=[
            ("brand", "Marque"),
            ("condition", "État"),
        ],
    ),
}


def field_schema_for_category(category: str) -> str:
    """JSON field hints for LLM enrichment prompts."""
    try:
        from .models import get_category_config

        cfg = get_category_config(category)
        if cfg and cfg.specs_a_extraire:
            return "\n".join(f"- {f}: {f.replace('_', ' ')}" for f in cfg.specs_a_extraire)
    except Exception:
        pass
    prof = profile_for_category(category)
    fields = prof.enrichment_fields or [d.id for d in prof.dimensions[:8]]
    schema_parts = []
    for f in fields:
        label = next((d.label for d in prof.dimensions if d.id == f or d.id == f.replace("_gb", "")), f)
        schema_parts.append(f"- {f}: {label}")
    return "\n".join(schema_parts) or "- condition: État\n- brand: Marque"


def excel_columns_for_category(category: str) -> List[Tuple[str, str]]:
    """Category-specific Excel columns (French labels)."""
    prof = profile_for_category(category)
    return list(prof.excel_spec_columns)


def completeness_weights_for_category(category: str) -> Dict[str, float]:
    prof = profile_for_category(category)
    return dict(prof.completeness_weights) if prof.completeness_weights else {"condition": 1.0}


def profile_for_category(category: str) -> CategoryProfile:
    cat = normalize_category(category)
    return CATEGORY_PROFILES.get(cat, CATEGORY_PROFILES["generic"])


def category_weights(category: str) -> Dict[str, float]:
    """Weights for hyper-pro scoring dimensions per category."""
    prof = profile_for_category(category)
    base = {
        "price_value": 0.28,
        "relevance": 0.18,
        "condition": 0.14,
        "seller_trust": 0.12,
        "completeness": 0.11,
        "dropship_risk": 0.16,
        "platform_bonus": 0.05,
        "battery_health": 0.0,
    }
    dims = prof.scoring_dimensions or list(base.keys())
    raw = {d: base.get(d, 0.1) for d in dims if d in base or d == "battery_health"}
    if "battery_health" in dims:
        raw["battery_health"] = 0.10 if normalize_category(category) == "phone" else 0.0
    total = sum(raw.values()) or 1.0
    return {k: round(v / total, 4) for k, v in raw.items()}


def for_category(category: str) -> CategoryProfile:
    return profile_for_category(category)


# ---------------------------------------------------------------------------
# Eval context + UI helpers
# ---------------------------------------------------------------------------


def parse_intent_targets(*, category: str, query: str, intent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    intent = intent or {}
    cat = normalize_category(category)
    blob = _norm_phone_text(query).lower()
    cfg: Dict[str, Any] = {
        "category": cat,
        "query": query,
        "target_model_key": str(intent.get("target_model_key") or intent.get("required_phone_model_key") or ""),
        "target_model_label": str(intent.get("target_model_label") or ""),
        "target_storage_gb": int(intent.get("target_storage_gb") or intent.get("required_storage_gb") or 0),
        "target_frame_size": str(intent.get("target_frame_size") or ""),
        "exclude_variants": list(intent.get("exclude_variants") or []),
        "market_median_eur": float(intent.get("market_median_eur") or 0),
    }
    if cat == "phone" or re.search(r"(?i)\biphone|galaxy|pixel\b", blob):
        try:
            from .engine import infer_phone_model, infer_storage_gb

            key, label = infer_phone_model({"title": query, "ai_model_exact": query})
            if key != "unknown":
                cfg["target_model_key"], cfg["target_model_label"] = key, label
            st = infer_storage_gb({"title": query})
            if st:
                cfg["target_storage_gb"] = int(st)
        except Exception:
            pass
        m = _IPHONE_MODEL_RE.search(blob)
        if m and not cfg["target_model_label"]:
            num = m.group(1) or m.group(2)
            cfg["target_model_label"], cfg["target_model_key"] = f"iPhone {num}", f"iphone_{num}"
        sm = _STORAGE_RE.search(blob)
        if sm and not cfg["target_storage_gb"]:
            cfg["target_storage_gb"] = int(sm.group(1))
        if re.search(r"(?i)iphone\s*\d{2}\b", blob) and not _PRO_PLUS_RE.search(blob):
            cfg["exclude_variants"] = cfg["exclude_variants"] or ["pro", "plus", "pro max", "max"]
    if cat == "bicycle":
        fm = _FRAME_SIZE_RE.search(blob)
        if fm:
            cfg["target_frame_size"] = (fm.group(1) or fm.group(2) or "").upper() or cfg["target_frame_size"]
    if cat in ("console_ps4", "console_ps5") or re.search(r"(?i)\bps\s*4|playstation\s*4\b", blob):
        if re.search(r"(?i)\bslim\b", blob):
            cfg["ps4_variant"] = "slim"
        elif re.search(r"(?i)\bpro\b", blob):
            cfg["ps4_variant"] = "pro"
        sm = _PS4_STORAGE_RE.search(blob)
        if sm:
            txt = sm.group(0).lower()
            if "1" in txt and ("to" in txt or "tb" in txt or "1000" in txt):
                cfg["target_storage_gb"] = 1000
            elif "500" in txt:
                cfg["target_storage_gb"] = 500
    prof = profile_for_category(cat)
    if not cfg["exclude_variants"]:
        cfg["exclude_variants"] = list(prof.avoid_patterns)
    return cfg


def build_eval_context(
    category: str,
    intent: Optional[Dict[str, Any]],
    listings: List[Dict[str, Any]],
    *,
    query: str = "",
) -> Dict[str, Any]:
    cat = normalize_category(category)
    prof = profile_for_category(cat)
    prices = [effective_price_eur(r) for r in listings if effective_price_eur(r) > 0]
    med = float(statistics.median(prices)) if prices else 0.0
    if med and intent is not None:
        intent = {**intent, "market_median_eur": med}
    targets = parse_intent_targets(category=cat, query=query, intent=intent)
    targets["market_median_eur"] = med
    specs_found: Dict[str, Dict[str, Any]] = {}
    for r in listings:
        lid = str(r.get("id") or "")
        if lid:
            specs_found[lid] = extract_listing_specs(r, cat)
    p25 = p75 = med
    if len(prices) >= 4:
        sp = sorted(prices)
        p25 = float(statistics.median(sp[: len(sp) // 2]))
        p75 = float(statistics.median(sp[len(sp) // 2 :]))
    return {
        "category": cat,
        "dimensions": [{"id": d.id, "label": d.label, "weight": d.weight} for d in prof.dimensions],
        "llm_criteria_hint": prof.llm_criteria_hint,
        "specs_found": specs_found,
        "market_reference": {"median": med, "p25": p25, "p75": p75, "fair_low": p25, "fair_high": p75},
        "targets": targets,
        "avoid_patterns": prof.avoid_patterns,
    }


def compact_specs_line(specs: Dict[str, Any], category: str) -> str:
    cat = normalize_category(category)
    parts: List[str] = []
    if cat == "phone":
        if specs.get("product_model_label"):
            parts.append(str(specs["product_model_label"]))
        if specs.get("storage_gb"):
            parts.append(f"{specs['storage_gb']} Go")
        if specs.get("battery_health_pct"):
            parts.append(f"bat {specs['battery_health_pct']}%")
        if specs.get("icloud_locked"):
            parts.append("iCloud!")
    elif cat == "bicycle":
        if specs.get("wheel_size"):
            parts.append(f"roues {specs['wheel_size']}")
        if specs.get("frame_size"):
            parts.append(f"taille {specs['frame_size']}")
        if specs.get("bike_brand"):
            parts.append(str(specs["bike_brand"]))
        if specs.get("frame_material"):
            parts.append(str(specs["frame_material"]))
        if specs.get("groupset"):
            parts.append(str(specs["groupset"]))
        if specs.get("is_ebike"):
            parts.append("VAE")
        if specs.get("bike_usage"):
            parts.append(str(specs["bike_usage"]))
    elif cat == "projector":
        if specs.get("lumens"):
            parts.append(f"{specs['lumens']} lm")
        if specs.get("resolution"):
            parts.append(str(specs["resolution"]))
        if specs.get("projector_brand"):
            parts.append(str(specs["projector_brand"]))
        if specs.get("has_hdmi") is True:
            parts.append("HDMI")
        elif specs.get("has_hdmi") is False or specs.get("missing_hdmi") or specs.get("legacy_ports_only"):
            parts.append("sans HDMI")
        if specs.get("projector_usage"):
            parts.append(str(specs["projector_usage"]))
        if specs.get("product_model_label"):
            parts.append(str(specs["product_model_label"]))
    elif cat == "console_ds":
        if specs.get("ds_model_family"):
            parts.append(str(specs["ds_model_family"]))
    elif cat in ("console_ps4", "console_ps5"):
        if specs.get("variant"):
            parts.append(str(specs["variant"]))
        if specs.get("storage_gb"):
            parts.append(f"{specs['storage_gb']} Go")
        if specs.get("controllers_count") is not None:
            parts.append(f"{specs['controllers_count']} man.")
        if specs.get("games_count"):
            parts.append(f"{specs['games_count']} jeux")
    elif cat == "apparel":
        if specs.get("size"):
            parts.append(f"taille {specs['size']}")
        if specs.get("brand"):
            parts.append(str(specs["brand"]))
    elif cat == "ac":
        if specs.get("capacity_btu"):
            parts.append(f"{specs['capacity_btu']} BTU")
        elif specs.get("cooling_kw"):
            parts.append(f"{specs['cooling_kw']} kW")
        if specs.get("brand"):
            parts.append(str(specs["brand"]).capitalize())
        if specs.get("ac_type"):
            parts.append(str(specs["ac_type"]))
        if specs.get("product_model_label"):
            parts.append(str(specs["product_model_label"]))
    if specs.get("condition_hint"):
        parts.append(str(specs["condition_hint"]))
    return ", ".join(parts) if parts else "-"


def spec_badges(specs: Dict[str, Any], category: str) -> List[str]:
    """Short badges for UI chips (universels : Obsolète, Bon plan, Complétude…)."""
    cat = normalize_category(category)
    badges: List[str] = []
    if specs.get("technical_veto"):
        badges.append("Veto technique")
    if specs.get("is_obsolete"):
        badges.append("Obsolète")
    elif specs.get("obsolete_label"):
        badges.append(str(specs["obsolete_label"])[:28])
    if specs.get("critical_defect"):
        badges.append("Défaut critique")
    if specs.get("inflated_specs") or specs.get("inflated_lumens_claim") or specs.get("marketing_lumens"):
        badges.append("Specs gonflées")
    deal = str(specs.get("deal_tier") or specs.get("quality_tier") or "")
    if deal in ("pepite", "gem", "bonne_affaire") and not specs.get("technical_veto"):
        badges.append("Bon plan")
    comp = specs.get("completeness_score")
    if comp is not None:
        try:
            badges.append(f"Complétude {int(comp)}%")
        except (TypeError, ValueError):
            pass
    if cat == "phone":
        if specs.get("battery_health_pct"):
            badges.append(f"{specs['battery_health_pct']}% batterie")
        if specs.get("storage_gb"):
            badges.append(f"{specs['storage_gb']} Go")
    elif cat == "bicycle":
        if specs.get("wheel_size"):
            badges.append(f"Roues {specs['wheel_size']}")
        if specs.get("frame_size"):
            badges.append(f"Taille {specs['frame_size']}")
        if specs.get("bike_brand"):
            badges.append(str(specs["bike_brand"]))
        if specs.get("frame_material"):
            badges.append(str(specs["frame_material"]))
        if specs.get("is_ebike"):
            badges.append("VAE")
    elif cat == "console_ps4":
        if specs.get("variant"):
            badges.append(str(specs["variant"]).capitalize())
        if specs.get("controller_count") is not None:
            badges.append(f"{specs['controller_count']} manette(s)")
        if specs.get("games_count"):
            badges.append(f"{specs['games_count']} jeux")
        if specs.get("storage_gb"):
            badges.append(f"{specs['storage_gb']} Go")
    elif cat == "projector":
        if specs.get("lumens"):
            badges.append(f"{specs['lumens']} lm")
        if specs.get("resolution"):
            badges.append(str(specs["resolution"]))
        if specs.get("projector_brand"):
            badges.append(str(specs["projector_brand"]).capitalize())
        if specs.get("has_hdmi") is True:
            badges.append("HDMI")
        elif specs.get("has_hdmi") is False or specs.get("missing_hdmi") or specs.get("legacy_ports_only"):
            badges.append("sans HDMI")
        if specs.get("projector_usage"):
            badges.append(str(specs["projector_usage"]))
    elif cat == "apparel" and specs.get("size"):
        badges.append(f"Taille {specs['size']}")
    elif cat == "ac":
        if specs.get("capacity_btu"):
            badges.append(f"{specs['capacity_btu']} BTU")
        elif specs.get("cooling_kw"):
            badges.append(f"{specs['cooling_kw']} kW")
        if specs.get("brand"):
            badges.append(str(specs["brand"]).capitalize())
        if specs.get("ac_type"):
            badges.append(str(specs["ac_type"]).capitalize())
    if specs.get("condition_hint"):
        badges.append(str(specs["condition_hint"]))
    return badges[:6]


def row_spec_badges(row: Dict[str, Any], category: str) -> List[str]:
    """Merge row-level expert tags with extracted specs for UI chips."""
    cat = normalize_category(category)
    specs = dict(row.get("extracted_specs") or extract_listing_specs(row, cat))
    for k in (
        "is_obsolete",
        "obsolete_label",
        "obsolete_reason",
        "critical_defect",
        "inflated_specs",
        "inflated_lumens_claim",
        "marketing_lumens",
        "completeness_score",
        "technical_veto",
        "deal_tier",
        "quality_tier",
    ):
        if row.get(k) is not None:
            specs[k] = row[k]
    return spec_badges(specs, cat)


def apply_early_spec_filters(
    items: List[Any],
    *,
    category: str,
    intent: Optional[Any] = None,
) -> Tuple[List[Any], int, Dict[str, str]]:
    """Filtre specs rule-based avant cascade IA — économise tokens."""
    cat = normalize_category(category)
    if cat not in ("console_ps4", "console_ps5", "bicycle", "projector", "phone", "ac"):
        return items, 0, {}

    kept: List[Any] = []
    reasons: Dict[str, str] = {}
    dropped = 0

    for it in items:
        sid = str(getattr(it, "id", None) or "")
        title = str(getattr(it, "title", None) or "")
        desc = str(getattr(it, "description", None) or "")
        row = {"title": title, "description": desc, "id": sid}
        if hasattr(it, "to_row"):
            try:
                row = {**it.to_row(), **row}
            except Exception:
                pass
        specs = extract_listing_specs(row, cat)
        text = f"{title} {desc}".strip()
        reason = ""

        if cat in ("console_ps4", "console_ps5"):
            if _CONSOLE_PS4_WRONG_PLATFORM.search(text) and not _PS4_MODEL_RE.search(text):
                reason = "mauvaise_plateforme"
            elif specs.get("is_games_only") or specs.get("listing_type") == "games_only":
                if not re.search(r"(?i)\bconsole\b", text):
                    reason = "jeux_seuls"
            elif specs.get("is_accessory_only") or specs.get("listing_type") == "accessory":
                reason = "accessoire"
            elif _PS4_ACCESSORY_RE.search(text) and not _PS4_MODEL_RE.search(text):
                if not re.search(r"(?i)\bconsole\b", text):
                    reason = "accessoire"
        elif cat == "projector":
            if specs.get("is_screen_only") or specs.get("is_accessory_only"):
                reason = "accessoire"
            elif is_projector_accessory_text(text):
                reason = "accessoire_hdmi"
        elif cat == "bicycle":
            if re.search(r"(?i)\b(poussette|trottinette|scooter|casque\s*seul|sonnette)\b", text):
                if not re.search(r"(?i)\b(vélo|velo|bike|vtt|vtc|bicycle)\b", text):
                    reason = "hors_velo"
        elif cat == "ac":
            if specs.get("is_accessory_only"):
                reason = "accessoire"
            elif _AC_ACCESSORY_TITLE_RE.search(title) and not ac_has_unit_signal(text):
                reason = "accessoire"
            elif not specs.get("ac_is_unit") and not ac_has_unit_signal(text):
                if re.search(r"(?i)\bclim\b", text) and not re.search(
                    r"(?i)\b(\d{4,6}\s*btu|climatiseur|split|monosplit)\b", text
                ):
                    reason = "pas_unite_clim"

        if reason:
            dropped += 1
            if sid:
                reasons[sid] = reason
        else:
            kept.append(it)

    return kept, dropped, reasons


def prescore_listing(listing: Dict[str, Any], eval_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based pre-score before LLM Top 5 selection."""
    from .scoring import hyper_pro_value_score  # lazy: avoid import cycle at module load

    cat = eval_ctx.get("category") or "generic"
    targets = eval_ctx.get("targets") or {}
    specs = extract_listing_specs(listing, cat)
    lid = str(listing.get("id") or "")
    if lid:
        eval_ctx.setdefault("specs_found", {})[lid] = specs

    text = _listing_text(listing)
    price = effective_price_eur(listing)
    med = float((eval_ctx.get("market_reference") or {}).get("median") or targets.get("market_median_eur") or 0)
    score = float(listing.get("score_composite") or 0) or hyper_pro_value_score(listing) or float(
        listing.get("rank_score") or listing.get("final_score") or 50.0
    )
    flags: List[str] = []
    penalties = 0.0

    if cat == "phone":
        tk = str(targets.get("target_model_key") or "")
        if tk:
            key = specs.get("product_model_key") or ""
            if key and key != "unknown" and key != tk:
                penalties += 35.0
                flags.append(f"modèle {specs.get('product_model_label') or key}")
        for ex in targets.get("exclude_variants") or []:
            if re.search(rf"(?i)\b{re.escape(str(ex))}\b", text):
                penalties += 40.0
                flags.append(f"variante {ex}")
                break
        ts = int(targets.get("target_storage_gb") or 0)
        st = int(specs.get("storage_gb") or 0)
        if ts and st and st != ts:
            penalties += 25.0
            flags.append(f"stockage {st}!={ts} Go")
        bat = specs.get("battery_health_pct")
        if bat:
            b = int(bat)
            if b >= 88:
                score += 8.0
            elif b >= 80:
                score += 4.0
            elif b < 75:
                penalties += 8.0
                flags.append(f"batterie {b}%")
        if specs.get("icloud_locked"):
            penalties += 50.0
            flags.append("iCloud verrouillé")

    elif cat == "bicycle":
        tf = str(targets.get("target_frame_size") or "").upper()
        fs = str(specs.get("frame_size") or "").upper()
        if tf and fs and tf not in fs and fs not in tf:
            penalties += 20.0
            flags.append(f"taille {fs} vs {tf}")

    elif cat == "projector":
        lum = int(specs.get("lumens") or 0)
        if 0 < lum < 500:
            penalties += 25.0
            flags.append(f"lumens faibles ({lum})")
        if specs.get("missing_hdmi") or (lum > 0 and not specs.get("has_hdmi") and "hdmi" not in text.lower()):
            if re.search(r"(?i)\b(vga|d-sub|composite)\b", text):
                penalties += 15.0
                flags.append("pas HDMI")

    elif cat in ("console_ps4", "console_ps5"):
        try:
            ctrl = int(specs.get("controllers_count") or specs.get("controller_count") or -1)
        except (TypeError, ValueError):
            ctrl = -1
        if ctrl >= 2:
            score += 12.0
        elif ctrl == 1:
            score += 4.0
        elif ctrl == 0:
            penalties += 10.0
            flags.append("sans manette")
        try:
            st = int(specs.get("storage_gb") or 0)
            if st >= 1000:
                score += 6.0
        except (TypeError, ValueError):
            pass
        premium = specs.get("premium_games") or []
        if premium:
            score += min(10.0, len(premium) * 3.0)

    if med > 0 and price > 0:
        disc = (1.0 - price / med) * 100.0
        listing["_vs_market_pct"] = round(disc, 1)
        if disc >= 25:
            score += min(12.0, disc * 0.15)
        elif disc < -15:
            penalties += 10.0

    if float(listing.get("dropship_risk_score") or 0) >= 55:
        penalties += 20.0
        flags.append("dropship")

    pre = max(0.0, min(100.0, score - penalties))
    return {
        "pre_score": round(pre, 1),
        "flags": flags,
        "specs": specs,
        "condition_hint": specs.get("condition_hint", ""),
    }


# --- score.py ---

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from .engine import ScoreWeights

def _engine_score_helpers():
    from . import engine
    return engine

def clamp_discount_pct(*args, **kwargs):
    return _engine_score_helpers().clamp_discount_pct(*args, **kwargs)

def price_value_adjustment(*args, **kwargs):
    return _engine_score_helpers().price_value_adjustment(*args, **kwargs)


def _f(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key)
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# freshness

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_ts(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        return ts if ts > 0 else None
    s = str(val).strip()
    if not s:
        return None
    if s.isdigit():
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s.replace(" ", "T")[:25])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        m = _ISO_DATE.search(s)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                return None
    return None


def parse_listed_at(platform: str, raw: Any = None, *, row: Optional[Dict[str, Any]] = None) -> str:
    """Retourne une date ISO (UTC) de mise en vente si détectable."""
    plat = (platform or "").lower()
    data = raw if isinstance(raw, dict) else {}
    if row:
        for k in ("posted_at", "listed_at", "created_at", "first_publication_date"):
            if row.get(k):
                ts = _parse_ts(row.get(k))
                if ts:
                    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    keys: tuple[str, ...]
    if plat == "vinted":
        keys = ("created_at_ts", "created_at", "published_at", "created_at_ts")
    elif plat == "leboncoin":
        keys = ("first_publication_date", "index_date", "ad_first_publication", "date")
    elif plat == "ebay":
        keys = ("listingDate", "startTime", "itemCreationDate")
    elif plat == "wallapop":
        keys = ("created_at", "createdAt", "publish_date")
    else:
        keys = ("created_at", "listed_at", "date")

    for k in keys:
        ts = _parse_ts(data.get(k))
        if ts:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return ""


def filter_stale_catalog_items(
    items: List[Any],
    *,
    max_age_days: int = 365,
    platforms: Optional[set[str]] = None,
) -> Tuple[List[Any], int]:
    """Retire les annonces trop anciennes (ex. LeBonCoin sans tri date fiable)."""
    if max_age_days <= 0 or not items:
        return items, 0
    plats = platforms or {"leboncoin", "lbc"}
    kept: List[Any] = []
    dropped = 0
    for it in items:
        plat = str(getattr(it, "platform", None) or "").lower()
        if plat not in plats:
            kept.append(it)
            continue
        raw = getattr(it, "raw", None)
        iso = parse_listed_at(plat, raw=raw if isinstance(raw, dict) else {})
        if not iso:
            kept.append(it)
            continue
        age = listing_age_days(iso)
        if age is not None and age > max_age_days:
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def listing_age_hours(listed_at: str = "", *, row: Optional[Dict[str, Any]] = None) -> Optional[float]:
    ts: Optional[float] = None
    if listed_at:
        ts = _parse_ts(listed_at)
    elif row:
        for k in ("posted_at", "listed_at", "created_at", "first_publication_date"):
            ts = _parse_ts(row.get(k))
            if ts:
                break
        if ts is None and row.get("age_hours") is not None:
            try:
                return max(0.0, float(row["age_hours"]))
            except (TypeError, ValueError):
                pass
    if ts is None:
        return None
    return max(0.0, (time.time() - ts) / 3600.0)


def listing_age_days(listed_at: str = "", *, row: Optional[Dict[str, Any]] = None) -> Optional[int]:
    hours = listing_age_hours(listed_at, row=row)
    if hours is not None:
        return max(0, int(hours // 24))
    if row and row.get("listing_age_days") is not None:
        try:
            return int(row["listing_age_days"])
        except (TypeError, ValueError):
            pass
    return None


def freshness_score_adjustment(age_days: Optional[int]) -> float:
    """Bonus récents, malus annonces très anciennes (>1 an)."""
    if age_days is None:
        return 0.0
    if age_days <= 2:
        return 10.0
    if age_days <= 7:
        return 8.0
    if age_days <= 14:
        return 6.0
    if age_days <= 30:
        return 4.0
    if age_days <= 90:
        return 2.0
    if age_days <= 180:
        return 0.0
    if age_days <= 365:
        return -6.0
    return -14.0


def format_listing_age(
    age_days: Optional[int],
    listed_at: str = "",
    *,
    age_hours: Optional[float] = None,
) -> str:
    if age_hours is None and listed_at:
        age_hours = listing_age_hours(listed_at)
    if age_hours is not None:
        if age_hours < 1:
            return "À l'instant"
        if age_hours < 24:
            h = int(age_hours)
            return f"Il y a {h} h" if h > 1 else "Il y a 1 h"
    if age_days is None:
        return ""
    if age_days == 0:
        return "Aujourd'hui"
    if age_days == 1:
        return "Hier"
    if age_days < 7:
        return f"Il y a {age_days} j"
    if age_days < 30:
        w = age_days // 7
        return f"Il y a {w} sem."
    if age_days < 365:
        m = age_days // 30
        return f"Il y a {m} mois"
    y = age_days // 365
    return f"Il y a {y} an{'s' if y > 1 else ''}"


def enrich_row_freshness(row: Dict[str, Any]) -> None:
    posted = str(row.get("posted_at") or row.get("listed_at") or "").strip()
    if not posted:
        posted = parse_listed_at(
            str(row.get("platform") or ""),
            row.get("raw") if isinstance(row.get("raw"), dict) else None,
            row=row,
        )
    if posted:
        row["posted_at"] = posted
        row["listed_at"] = posted
    hours = listing_age_hours(posted, row=row)
    age = listing_age_days(posted, row=row) if hours is not None else None
    if hours is not None:
        row["age_hours"] = round(hours, 1)
    if age is not None:
        row["listing_age_days"] = age
        label = format_listing_age(age, posted, age_hours=hours)
        row["age_text"] = label
        row["listing_age_label"] = label
        row["freshness_adj"] = freshness_score_adjustment(age)


def compute_composite_score(
    row: Dict[str, Any],
    *,
    weights: Optional["ScoreWeights"] = None,
    market_ctx: Optional[Any] = None,
) -> Dict[str, float]:
    """Score composite multi-dimensionnel (hyper-pro) avec repli legacy."""
    try:
        from .scoring import ProMarketContext, compute_hyper_pro_score

        ctx = market_ctx if isinstance(market_ctx, ProMarketContext) else None
        hp = compute_hyper_pro_score(row, ctx)
        comp = float(hp["score_composite"])
        dims = hp.get("dimension_scores") or {}
        components = {f"dim_{k}": round(float(v), 2) for k, v in dims.items()}
        if hp.get("dropship_risk_score", 0) >= 60:
            components["dropship_cap"] = -max(0.0, comp - 30.0)
        return {"composite_score": comp, **{f"comp_{k}": v for k, v in components.items()}}
    except Exception:
        pass

    w = weights
    components: Dict[str, float] = {}
    base = _f(row, "final_score", _f(row, "score_base", 50.0))
    components["base"] = round(base, 2)
    total = base

    fit = _f(row, "buyer_fit_score")
    if fit > 0:
        delta = round((fit - 50.0) * 0.18, 2)
        components["buyer_fit"] = delta
        total += delta

    disc = clamp_discount_pct(_f(row, "model_vs_market_pct") or _f(row, "market_discount_pct"))
    if disc:
        if disc > 0:
            delta = round(min(20.0, disc * 0.16), 2)
        else:
            delta = round(max(-32.0, disc * 0.26), 2)
        if not row.get("is_accessory"):
            try:
                from .engine import brand_quality_multiplier

                mult = brand_quality_multiplier(row)
                if mult < 1.0 and delta > 0:
                    delta = round(delta * mult, 2)
            except Exception:
                pass
        components["vs_market"] = delta
        total += delta
    pv = price_value_adjustment(row)
    if abs(pv) >= 0.5:
        components["price_value"] = round(pv, 2)
        total += pv

    pack = _f(row, "pack_score")
    if pack:
        delta = round((pack - 50.0) * 0.08, 2)
        components["pack"] = delta
        total += delta

    rel = _f(row, "ai_title_relevance") or _f(row, "title_relevance")
    if rel:
        delta = round((rel - 50.0) * 0.06, 2)
        components["relevance"] = delta
        total += delta

    intent = _f(row, "model_intent_match")
    if intent:
        delta = round(intent * 0.05, 2)
        components["model_match"] = delta
        total += delta

    tier = str(row.get("quality_tier") or "ok")
    tier_adj = {"gem": 8.0, "ok": 2.0, "suspicious": -6.0, "dropship": -14.0, "dinosaur": -20.0}.get(tier, 0.0)
    if tier_adj:
        components["quality_tier"] = tier_adj
        total += tier_adj

    if not row.get("is_accessory"):
        try:
            from .engine import brand_score_adjustment

            bq = brand_score_adjustment(row)
            if abs(bq) >= 0.5:
                components["brand_quality"] = bq
                total += bq
        except Exception:
            pass

    rf = str(row.get("risk_flags") or "").lower()
    risk_pen = 0.0
    for flag, pen in (
        ("ban", -40.0),
        ("broken", -35.0),
        ("screen_defect", -18.0),
        ("hardware_defect", -15.0),
        ("scammy", -25.0),
    ):
        if flag in rf:
            risk_pen += pen
    if risk_pen:
        components["risks"] = risk_pen
        total += risk_pen

    if row.get("is_accessory"):
        components["accessory"] = -28.0
        total -= 28.0

    if row.get("screen_defect") or row.get("defect_hint"):
        components["defect"] = -15.0
        total -= 15.0

    fresh = _f(row, "freshness_adj")
    if not fresh and row.get("listing_age_days") is not None:
        try:
            fresh = freshness_score_adjustment(int(row["listing_age_days"]))
        except (TypeError, ValueError):
            fresh = 0.0
    if abs(fresh) >= 0.5:
        components["freshness"] = round(fresh, 2)
        total += fresh

    if not row.get("is_accessory"):
        try:
            from .scoring import seller_trust_adjustment

            st_adj, _, _ = seller_trust_adjustment(row)
            if abs(st_adj) >= 0.5:
                components["seller_trust"] = round(st_adj, 2)
                total += st_adj
        except Exception:
            pass

    total = max(5.0, min(99.0, total))
    return {"composite_score": round(total, 2), **{f"comp_{k}": v for k, v in components.items()}}


def apply_composite_scores(
    rows: List[Dict[str, Any]],
    *,
    weights: Optional["ScoreWeights"] = None,
    market_ctx: Optional[Any] = None,
    sync_quality_tier: bool = True,
) -> None:
    try:
        from .scoring import ProMarketContext, apply_hyper_pro_scoring

        ctx = market_ctx
        if ctx is None and rows:
            ctx = build_market_context_for_scoring(rows)
        if isinstance(ctx, ProMarketContext):
            apply_hyper_pro_scoring(rows, ctx, sync_quality_tier=sync_quality_tier)
            return
    except Exception:
        pass

    for row in rows:
        comp = compute_composite_score(row, weights=weights, market_ctx=market_ctx)
        row["composite_score"] = comp.pop("composite_score")
        row["score_composite"] = row["composite_score"]
        breakdown = [f"{k.replace('comp_', '')}:{v:+.1f}" for k, v in sorted(comp.items()) if abs(v) >= 0.5]
        row["composite_breakdown"] = " · ".join(breakdown)[:220]
        for k, v in comp.items():
            row[k] = v
        row["_pre_rank_signal"] = row["composite_score"]


def build_market_context_for_scoring(rows: List[Dict[str, Any]]) -> Any:
    from .scoring import build_market_context

    return build_market_context(rows)


def deal_value_score(row: Dict[str, Any]) -> float:
    try:
        bds = float(row.get("best_deal_score") or 0)
        if bds > 0:
            return bds
    except (TypeError, ValueError):
        pass
    try:
        from .scoring import hyper_pro_value_score

        return hyper_pro_value_score(row)
    except Exception:
        pass
    base = float(row.get("composite_score") or row.get("rank_score") or row.get("final_score") or 0.0)
    score = base + price_value_adjustment(row) * 1.15
    tier = str(row.get("quality_tier") or "")
    if tier in ("dropship", "dinosaur"):
        score -= 35.0
    elif tier == "suspicious":
        score -= 12.0
    elif tier == "gem":
        score += 10.0
    if not row.get("is_accessory"):
        try:
            from .engine import brand_sort_bonus

            score += brand_sort_bonus(row) * 0.65
        except Exception:
            pass
    return score


def pick_value_top_deals(rows: List[Dict[str, Any]], *, limit: int = 10) -> List[Dict[str, Any]]:
    mains = [
        r
        for r in rows
        if not r.get("is_accessory")
        and str(r.get("quality_tier") or "") not in ("dropship", "dinosaur")
        and not any(
            f in str(r.get("quality_flags") or "")
            for f in ("fake_4k_scam", "impossible_price_scam", "hype_dropship")
        )
    ]
    ranked = sorted(mains, key=deal_value_score, reverse=True)
    out: List[Dict[str, Any]] = []
    seen_plat_price: set = set()
    for r in ranked:
        if len(out) >= limit:
            break
        try:
            p = round(float(r.get("total_price") or r.get("price") or 0), 0)
        except (TypeError, ValueError):
            p = 0.0
        plat = str(r.get("platform") or "vinted").lower()
        key = f"{plat}:{p}"
        if key in seen_plat_price and p > 0:
            continue
        seen_plat_price.add(key)
        out.append(r)
    return out


def _enrich_candidate_score(row: Dict[str, Any]) -> float:
    if row.get("is_accessory"):
        return -1000.0
    try:
        from .scoring import hyper_pro_value_score

        v = hyper_pro_value_score(row)
        if v > -500:
            return v
    except Exception:
        pass
    base = float(
        row.get("score_composite")
        or row.get("composite_score")
        or row.get("rank_score")
        or row.get("final_score")
        or 0.0
    )
    intent = float(row.get("model_intent_match") or 0.0)
    rel = float(row.get("ai_title_relevance") or row.get("title_relevance") or 0.0)
    disc = float(row.get("market_discount_pct") or row.get("model_vs_market_pct") or 0.0)
    score = base + intent * 0.35 + max(0.0, rel - 50.0) * 0.12 + min(15.0, max(-8.0, disc * 0.06))
    score += price_value_adjustment(row) * 0.85
    td = str(row.get("triage_decision") or "keep")
    if td == "reject":
        return -500.0
    if td == "doubtful":
        score -= 12.0
    rf = str(row.get("risk_flags") or "").lower()
    if "ban" in rf or "broken" in rf:
        score -= 40.0
    if row.get("screen_defect") or row.get("defect_hint"):
        score -= 18.0
    tier = str(row.get("quality_tier") or "")
    if tier in ("dropship", "dinosaur"):
        score -= 25.0
    elif tier == "gem":
        score += 6.0
    try:
        from .engine import brand_sort_bonus

        score += brand_sort_bonus(row) * 0.4
    except Exception:
        pass
    try:
        age = row.get("listing_age_days")
        if age is not None:
            score += freshness_score_adjustment(int(age)) * 0.85
    except (TypeError, ValueError):
        pass
    try:
        p = float(row.get("price_eur") or row.get("price") or 0)
        if 0 < p < 12:
            score -= 20.0
    except (TypeError, ValueError):
        pass
    return score


def select_top_for_enrichment(
    rows: List[Dict[str, Any]],
    *,
    limit: int = 100,
    doubtful_ids: Optional[Set[str]] = None,
    doubtful_cap: int = 12,
) -> List[Dict[str, Any]]:
    limit = max(1, int(limit))
    doubtful_ids = doubtful_ids or set()
    mains = [r for r in rows if not r.get("is_accessory")]
    if not mains:
        return []

    ranked = sorted(mains, key=_enrich_candidate_score, reverse=True)
    plat_cap = max(20, int(limit * 0.55))
    per_plat: Dict[str, int] = {}
    picked: List[Dict[str, Any]] = []
    picked_ids: Set[str] = set()

    def _plat(r: Dict[str, Any]) -> str:
        p = str(r.get("platform") or "vinted").lower()
        return "leboncoin" if p == "lbc" else p

    def _rid(r: Dict[str, Any]) -> str:
        return str(r.get("id") or r.get("listing_key") or "")

    for r in ranked:
        if len(picked) >= limit:
            break
        rid = _rid(r)
        if not rid or rid in picked_ids:
            continue
        plat = _plat(r)
        if per_plat.get(plat, 0) >= plat_cap:
            continue
        picked.append(r)
        picked_ids.add(rid)
        per_plat[plat] = per_plat.get(plat, 0) + 1

    if len(picked) < limit:
        for r in ranked:
            if len(picked) >= limit:
                break
            rid = _rid(r)
            if not rid or rid in picked_ids:
                continue
            picked.append(r)
            picked_ids.add(rid)

    doubt_added = 0
    if doubtful_ids:
        id_to_row = {_rid(r): r for r in rows}
        for sid in sorted(doubtful_ids):
            if doubt_added >= doubtful_cap or len(picked) >= limit + doubtful_cap:
                break
            dr = id_to_row.get(sid)
            if dr and sid not in picked_ids and not dr.get("is_accessory"):
                picked.append(dr)
                picked_ids.add(sid)
                doubt_added += 1

    return picked[: limit + doubtful_cap]


# --- pro_scoring.py ---
import json, re, statistics, time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_DROPSHIP_TITLE = re.compile(
    r"(?i)\b(neuf\s+sous\s+blister|envoi\s+rapide|stock\s+europ\w+|dropship|100\s*%\s*neuf|aliexpress|temu)\b"
)
_DROPSHIP_VAGUE = re.compile(r"(?i)^(console|nintendo|jeu|game)\s*$")
_CONDITION_GOOD = re.compile(r"(?i)\b(tres\s+bon|excellent|nickel|bon\s+etat)\b")
_CONDITION_WEAR = re.compile(r"(?i)\b(usure|rayure|griff)\b")
_CONDITION_BAD = re.compile(r"(?i)\b(hs|pour\s+pieces|casse|broken|ban)\b")
_DAMAGE_EXCUSE = re.compile(r"(?i)\b(pour\s+pieces|hs|casse|broken|rayure)\b")
_WEIGHTS = {
    "price_value": 0.28, "relevance": 0.18, "condition": 0.14,
    "seller_trust": 0.12, "completeness": 0.12, "dropship_risk": 0.16, "platform_bonus": 0.05,
}

_PHONE_WEIGHTS = {
    "price_value": 0.26, "relevance": 0.18, "condition": 0.10, "battery_health": 0.10,
    "seller_trust": 0.10, "completeness": 0.10, "dropship_risk": 0.12, "platform_bonus": 0.04,
}
_BATTERY_RE = re.compile(r"(?i)(?:batterie|battery|sante)\s*(?:[:=]?\s*)?(\d{2,3})\s*%|(\d{2,3})\s*%\s*(?:batterie|battery|sante)")
_IPHONE_VARIANT_RE = re.compile(r"(?i)\b(pro\s*max|pro|plus|max)\b")

# pro_scoring uses spec_profile.category_weights (defined above in this module)
_spec_profile_category_weights = category_weights


def extract_battery_pct(text: str) -> Optional[int]:
    if not text:
        return None
    m = _BATTERY_RE.search(text)
    if not m:
        m = re.search(r"(?i)\b(\d{2,3})\s*%\b", text)
        if not m or not re.search(r"(?i)batter|sant|battery|health", text):
            return None
    try:
        pct = int(m.group(1) or m.group(2))
        return pct if 50 <= pct <= 100 else None
    except (TypeError, ValueError, IndexError):
        return None

def _weights_for_row(row: Dict[str, Any]) -> Dict[str, float]:
    cat = str(row.get("item_type") or row.get("market_category") or row.get("category") or "")
    if cat == "phone" or str(row.get("item_type") or "") == "phone":
        return dict(_PHONE_WEIGHTS)
    w = dict(_spec_profile_category_weights(cat)) if cat else dict(_WEIGHTS)
    return w if w else dict(_WEIGHTS)

@dataclass
class ModelMarketStats:
    median_eur: float = 0.0
    p25_eur: float = 0.0
    p75_eur: float = 0.0
    count: int = 0
    oracle_ref_eur: float = 0.0

@dataclass
class ProMarketContext:
    model_stats: Dict[str, ModelMarketStats] = field(default_factory=dict)
    global_median_eur: float = 0.0
    platform_medians: Dict[str, float] = field(default_factory=dict)
    image_url_index: Dict[str, List[str]] = field(default_factory=dict)
    built_at: float = 0.0

def _row_text(row: Dict[str, Any]) -> str:
    return f"{row.get('title') or ''} {row.get('description') or ''}".lower()

def _model_key(row: Dict[str, Any]) -> str:
    for k in ("product_model_key", "ds_model_family", "model_key", "variant"):
        v = str(row.get(k) or "").strip()
        if v and v != "unknown":
            return v
    try:
        from .scoring import extract_listing_specs, normalize_category

        cat = normalize_category(str(row.get("item_type") or row.get("market_category") or ""))
        if cat == "ac":
            specs = extract_listing_specs(row, "ac")
            mk = str(specs.get("product_model_key") or "").strip()
            if mk:
                return mk
    except Exception:
        pass
    return ""

def build_market_context(rows: List[Dict[str, Any]], *, min_samples: int = 2) -> ProMarketContext:
    ctx = ProMarketContext()
    by_model: Dict[str, List[float]] = {}
    by_plat: Dict[str, List[float]] = {}
    all_prices: List[float] = []
    img_index: Dict[str, List[str]] = {}
    for r in rows:
        if r.get("is_accessory"):
            continue
        p = effective_price_eur(r)
        if p <= 0:
            continue
        all_prices.append(p)
        mk = _model_key(r)
        if mk:
            by_model.setdefault(mk, []).append(p)
        plat = str(r.get("platform") or "vinted").lower()
        if plat == "lbc":
            plat = "leboncoin"
        by_plat.setdefault(plat, []).append(p)
        pu, rid = str(r.get("photo_url") or "").strip(), str(r.get("id") or r.get("listing_key") or "")
        if pu and rid:
            img_index.setdefault(pu, []).append(rid)
    if all_prices:
        ctx.global_median_eur = float(statistics.median(all_prices))
    for mk, prices in by_model.items():
        if len(prices) < min_samples:
            continue
        sp = sorted(prices)
        n = len(sp)
        oracle = 0.0
        for r in rows:
            if _model_key(r) == mk:
                try:
                    ref = float(r.get("oracle_arbiter_ref_eur") or r.get("ref_price_refurb_eur") or 0)
                    if ref > 0:
                        oracle = ref
                        break
                except (TypeError, ValueError):
                    pass
        ctx.model_stats[mk] = ModelMarketStats(
            median_eur=float(statistics.median(sp)),
            p25_eur=float(sp[max(0, n // 4)]),
            p75_eur=float(sp[min(n - 1, (3 * n) // 4)]),
            count=n,
            oracle_ref_eur=oracle,
        )
    for plat, prices in by_plat.items():
        if len(prices) >= min_samples:
            ctx.platform_medians[plat] = float(statistics.median(prices))
    ctx.image_url_index = img_index
    ctx.built_at = time.time()
    return ctx

def _enrich_row_market_fields(row: Dict[str, Any], ctx: ProMarketContext) -> None:
    mk = _model_key(row)
    ms = ctx.model_stats.get(mk) if mk else None
    if ms and ms.median_eur > 0:
        row["model_market_median_eur"] = round(ms.median_eur, 2)
        row["model_market_n"] = ms.count
        p = effective_price_eur(row)
        if p > 0:
            disc = round((1.0 - p / ms.median_eur) * 100.0, 1)
            row["model_vs_market_pct"] = disc
            if row.get("market_discount_pct") is None:
                row["market_discount_pct"] = disc
    if ctx.global_median_eur > 0 and not row.get("run_market_median_eur"):
        row["run_market_median_eur"] = round(ctx.global_median_eur, 2)

def detect_dropship_risk(row: Dict[str, Any], ctx: Optional[ProMarketContext] = None) -> Tuple[float, List[str]]:
    text = _row_text(row)
    title = str(row.get("title") or "")
    flags: List[str] = []
    risk = 0.0
    if _DROPSHIP_TITLE.search(text):
        risk += 42.0
        flags.append("dropship_phrase")
    if _DROPSHIP_VAGUE.match(title.strip()):
        risk += 28.0
        flags.append("vague_title")
    try:
        reviews = int(row.get("seller_feedback_count") or row.get("seller_feedback") or 0)
    except (TypeError, ValueError):
        reviews = 0
    if reviews == 0:
        risk += 18.0
        flags.append("new_seller")
    elif reviews < 3:
        risk += 10.0
        flags.append("low_reviews")
    price = effective_price_eur(row)
    med = 0.0
    mk = _model_key(row)
    if ctx and mk and mk in ctx.model_stats:
        med = ctx.model_stats[mk].median_eur
    elif ctx and ctx.global_median_eur > 0:
        med = ctx.global_median_eur
    else:
        try:
            med = float(row.get("model_market_median_eur") or row.get("run_market_median_eur") or 0)
        except (TypeError, ValueError):
            med = 0.0
    if med > 0 and price > 0:
        disc_pct = (1.0 - price / med) * 100.0
        if disc_pct > 40 and not _DAMAGE_EXCUSE.search(text):
            risk += 35.0
            flags.append("price_anomaly_40pct")
        elif disc_pct > 30 and reviews < 5 and not _DAMAGE_EXCUSE.search(text):
            risk += 22.0
            flags.append("price_anomaly_new_seller")
    if re.search(r"(?i)\b(neuf|new|sealed|blister)\b", text) and price > 0 and med > 0 and price < med * 0.55:
        risk += 25.0
        flags.append("new_too_cheap")
    pu = str(row.get("photo_url") or "").strip()
    if ctx and pu and pu in ctx.image_url_index and len(ctx.image_url_index[pu]) >= 2:
        risk += 30.0
        flags.append("duplicate_image")
    if str(row.get("quality_tier") or "") == "dropship":
        risk = max(risk, 75.0)
        flags.append("tier_dropship")
    for f in ("hype_dropship", "fake_4k_scam", "impossible_price_scam"):
        if f in str(row.get("quality_flags") or ""):
            risk = max(risk, 80.0)
            flags.append(f)
    if row.get("deal_blocked"):
        risk = max(risk, 90.0)
        flags.append("deal_blocked")
    return round(min(100.0, risk), 1), list(dict.fromkeys(flags))


def _score_price_value(row: Dict[str, Any], ctx: Optional[ProMarketContext]) -> Tuple[float, str]:
    price = effective_price_eur(row)
    if price <= 0:
        return 20.0, "Prix indisponible"
    cat = str(row.get("item_type") or row.get("market_category") or "")
    if cat == "projector":
        if not has_projector_signal(row) or is_projector_accessory_text(_row_text(row)):
            return 12.0, "Accessoire ou hors-sujet — prix non compare"
    try:
        from .engine import row_matches_strict_intent

        if not row_matches_strict_intent(row):
            return 22.0, "Hors modele recherche — prix non compare"
    except Exception:
        pass
    med = oracle = 0.0
    mk = _model_key(row)
    if ctx and mk and mk in ctx.model_stats:
        med = ctx.model_stats[mk].median_eur
        oracle = ctx.model_stats[mk].oracle_ref_eur
    if med <= 0:
        try:
            med = float(row.get("model_market_median_eur") or row.get("run_market_median_eur") or 0)
        except (TypeError, ValueError):
            med = 0.0
    if oracle <= 0:
        try:
            oracle = float(row.get("oracle_arbiter_ref_eur") or row.get("ref_price_refurb_eur") or 0)
        except (TypeError, ValueError):
            oracle = 0.0
    disc = 0.0
    ref_label = ""
    if med > 0:
        disc = (1.0 - price / med) * 100.0
        ref_label = f"mediane modele {med:.0f} EUR"
    elif oracle > 0:
        disc = (1.0 - price / oracle) * 100.0
        ref_label = f"ref oracle {oracle:.0f} EUR"
    disc = clamp_discount_pct(disc)
    if disc >= 35:
        score = min(100.0, 72.0 + disc * 0.55)
    elif disc >= 20:
        score = 58.0 + disc * 0.7
    elif disc >= 8:
        score = 48.0 + disc * 0.65
    elif disc >= -5:
        score = 50.0 + disc * 0.3
    elif disc >= -20:
        score = 42.0 + (disc + 5) * 0.35
    else:
        score = max(8.0, 30.0 + disc * 0.4)
    plat = str(row.get("platform") or "vinted").lower()
    if ctx and ctx.platform_medians:
        plat_med = ctx.platform_medians.get("leboncoin" if plat == "lbc" else plat, 0)
        cheapest = min(ctx.platform_medians.values()) if ctx.platform_medians else 0
        if plat_med > 0 and cheapest > 0 and plat_med > cheapest * 1.03:
            score = min(100.0, score + min(8.0, (plat_med - cheapest) / plat_med * 100.0 * 0.25))
    expl = f"{price:.0f} EUR"
    if ref_label and disc != 0:
        expl += f" ? {disc:+.0f}% vs {ref_label}"
    return round(max(0.0, min(100.0, score)), 1), expl



def _score_battery_health(row: Dict[str, Any]) -> Tuple[float, str]:
    text = _row_text(row)
    pct = row.get("battery_health_pct")
    if pct is None:
        pct = extract_battery_pct(text)
        if pct:
            row["battery_health_pct"] = pct
    try:
        b = int(pct) if pct is not None else 0
    except (TypeError, ValueError):
        b = 0
    if b <= 0:
        return 52.0, "batterie non indiquee"
    if b >= 90:
        return 92.0, f"batterie {b}% excellente"
    if b >= 85:
        return 82.0, f"batterie {b}% bonne"
    if b >= 80:
        return 68.0, f"batterie {b}% correcte"
    if b >= 75:
        return 52.0, f"batterie {b}% limite"
    return 28.0, f"batterie {b}% faible"

def _phone_model_relevance_adjust(row: Dict[str, Any], score: float, bits: List[str]) -> float:
    if str(row.get("item_type") or "") != "phone":
        return score
    target = str(row.get("required_phone_model_key") or row.get("search_model_key") or "")
    key = str(row.get("product_model_key") or "")
    if target and key and key != "unknown":
        if key == target:
            score += 18.0
            bits.append("modele exact")
        else:
            score -= 32.0
            bits.append(f"mauvais variant ({key})")
    req_st = int(row.get("required_storage_gb") or 0)
    st = int(row.get("storage_gb") or 0)
    if req_st and st and st == req_st:
        score += 10.0
        bits.append(f"{st} Go confirme")
    elif req_st and st and st != req_st:
        score -= 20.0
        bits.append(f"stockage {st} Go")
    if row.get("required_exclude_pro") and _IPHONE_VARIANT_RE.search(_row_text(row)):
        score -= 35.0
        bits.append("Pro/Plus detecte")
    return score

def _score_relevance(row: Dict[str, Any]) -> Tuple[float, str]:
    score = 42.0
    bits: List[str] = []
    rel = float(row.get("ai_title_relevance") or row.get("title_relevance") or 0)
    if rel > 0:
        score = 25.0 + rel * 0.72
        bits.append(f"pertinence titre {rel:.0f}/100")
    intent = float(row.get("model_intent_match") or 0)
    if intent > 0:
        score += min(18.0, intent * 0.22)
        bits.append("modele cible")
    score = _phone_model_relevance_adjust(row, score, bits)
    req, fam = str(row.get("required_ds_family") or ""), str(row.get("ds_model_family") or "")
    if req and fam not in ("", "unknown"):
        if fam == req or (req == "new3ds" and fam == "new3ds_xl"):
            score += 15.0
            bits.append(f"variante {fam}")
        else:
            score -= 28.0
            bits.append(f"mauvais modele ({fam})")
            row["intent_mismatch"] = True
    td = str(row.get("triage_decision") or "")
    if td == "keep":
        score += 8.0
    elif td == "doubtful":
        score -= 12.0
    elif td == "reject":
        score -= 35.0
    if row.get("is_accessory"):
        score = min(score, 15.0)
        bits.append("accessoire")
    return round(max(0.0, min(100.0, score)), 1), "; ".join(bits) or "correspondance titre standard"


def confirmed_screen_ok(row: Dict[str, Any]) -> bool:
    return bool(re.search(r"(?i)\b(ecran\s+(nickel|parfait|ok)|top\s+ecran)\b", _row_text(row)))


def _score_condition(row: Dict[str, Any]) -> Tuple[float, str]:
    text = _row_text(row)
    if _CONDITION_BAD.search(text) or row.get("deal_blocked"):
        return 12.0, "Etat HS / pour pieces signale"
    score, bits = 58.0, []
    if _CONDITION_GOOD.search(text):
        score += 22.0
        bits.append("bon etat annonce")
    if _CONDITION_WEAR.search(text):
        score -= 12.0
        bits.append("usure signalee")
    if row.get("screen_defect") or row.get("defect_hint"):
        score -= 22.0
        bits.append("defaut ecran")
    elif confirmed_screen_ok(row):
        score += 8.0
        bits.append("ecran OK")
    vis = str(row.get("ai_vision_note") or "").lower()
    if vis:
        if any(w in vis for w in ("cass", "fissur", "rayure", "defect", "hs")):
            score -= 10.0
            bits.append("vision: defaut")
        elif any(w in vis for w in ("bon", "correct", "nickel", "propre", "ok")):
            score += 6.0
            bits.append("vision: OK")
    return round(max(0.0, min(100.0, score)), 1), "; ".join(bits) or "etat non precise"

def _score_seller_trust(row: Dict[str, Any], dropship_flags: List[str]) -> Tuple[float, str]:
    try:
        reviews = int(row.get("seller_feedback_count") or row.get("seller_feedback") or 0)
    except (TypeError, ValueError):
        reviews = 0
    try:
        rating = float(row.get("seller_rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    if reviews >= 50 and rating >= 0.85:
        score, expl = 88.0, f"vendeur fiable ({reviews} avis)"
    elif reviews >= 15 and rating >= 0.75:
        score, expl = 72.0, f"profil etabli ({reviews} avis)"
    elif reviews >= 5:
        score, expl = 62.0 + min(12.0, rating * 12.0), f"{reviews} avis"
    elif reviews > 0:
        score, expl = 48.0, f"peu d'avis ({reviews})"
    else:
        score, expl = 28.0, "compte sans avis"
    if "new_seller" in dropship_flags or "low_reviews" in dropship_flags:
        score = min(score, 38.0)
    if str(row.get("seller_trust_label") or ""):
        expl = str(row["seller_trust_label"])[:80]
    return round(max(0.0, min(100.0, score)), 1), expl


def _spec_value(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Read flattened field or enriched_specs nested value."""
    if row.get(key) not in (None, "", False):
        return row.get(key)
    es = row.get("enriched_specs") or {}
    meta = es.get(key)
    if isinstance(meta, dict) and meta.get("value") not in (None, ""):
        return meta["value"]
    return default


_OBSOLETE_REASON_LABELS = {
    "obsolete_tech": "Technologie obsolète",
    "legacy_ports": "Connectique legacy (VGA/DVI)",
    "no_hdmi": "Sans HDMI",
    "low_lumens": "Lumens insuffisants",
    "dinosaur": "Produit invendable",
    "vga_only": "VGA seul",
}

_CRITICAL_DEFECT_RE = re.compile(
    r"(?i)\b("
    r"lampe\s*hs|lampe\s*morte|ampoule\s*hs|ampoule\s*morte|bulb\s*dead|"
    r"pour\s*pi[eè]ces|hs\b|hors\s*service|ne\s*marche\s*pas|ne\s*fonctionne\s*pas|"
    r"panne\s*d['\u2019]?[ée]cran|[ée]cran\s*cass|[ée]cran\s*hs"
    r")\b"
)
_REPAIRS_NEEDED_RE = re.compile(
    r"(?i)\b(r[eé]paration|r[eé]parer|roue\s*voil|cadre\s*fiss|freins\s*[àa]\s*refaire|"
    r"vendu\s*pi[eè]ces|d[eé]faut\s*majeur)\b"
)

_PACK_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "projector": ("has_remote", "has_hdmi", "has_power_cable"),
    "has_remote": ("has_remote",),
    "has_hdmi": ("has_hdmi",),
    "has_power_cable": ("has_power_cable", "has_power"),
    "has_controller": ("has_controller", "controllers_count"),
    "has_stylus": ("has_stylus",),
    "has_hose": ("has_hose",),
    "has_charger": ("has_power_cable", "has_power", "has_charger"),
    "phone_unlocked": ("phone_unlocked",),
    "console": ("has_controller",),
    "unit": ("capacity_btu", "btu", "cooling_kw"),
    "frame": ("frame_size", "frame_size_cm", "bike_brand"),
    "wheels": ("wheel_size", "wheel_size_in"),
    "brakes": ("has_brakes",),
    "saddle": ("has_saddle",),
}


def _pack_field_present(row: Dict[str, Any], specs: Dict[str, Any], field: str) -> bool:
    keys = _PACK_FIELD_ALIASES.get(field, (field,))
    for k in keys:
        val = row.get(k)
        if val is None:
            val = specs.get(k)
        if k in ("controllers_count", "games_count", "storage_gb", "capacity_btu", "btu", "lumens"):
            try:
                if int(val or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        elif val is True or (isinstance(val, (int, float)) and val > 0):
            return True
        elif isinstance(val, str) and val.strip():
            return True
    if field == "projector":
        return bool(re.search(r"(?i)\b(projecteur|projector|videoproj)", _row_text(row)))
    return False


def _compute_pack_completeness(row: Dict[str, Any], specs: Dict[str, Any], cfg: Any) -> int:
    pack = cfg.pack_complet if cfg else ()
    if not pack:
        return int(row.get("completeness_score") or row.get("ai_completeness") or 0)
    hits = sum(1 for f in pack if _pack_field_present(row, specs, f))
    return max(0, min(100, int(round(100.0 * hits / max(1, len(pack))))))


def apply_category_heuristics(row: Dict[str, Any], category: str = "") -> None:
    """Rule-based pre-LLM: obsolete, veto, inflated specs, brand tier, complétude."""
    from .models import detect_brand_tier, get_category_config

    cat = normalize_category(category or str(row.get("item_type") or row.get("market_category") or ""))
    cfg = get_category_config(cat)
    if not cfg:
        return

    text = _row_text(row)
    specs = extract_listing_specs(row, cat)
    row["brand_tier"] = detect_brand_tier(text, cfg)

    for pat, reason in cfg.obsolete_if:
        if re.search(pat, text, re.I):
            row["is_obsolete"] = True
            row["obsolete_reason"] = reason
            row["technical_veto"] = True
            break

    res = str(specs.get("resolution") or row.get("resolution") or "").lower()
    if cat == "projector" and res in ("svga", "xga", "wvga", "vga"):
        row["is_obsolete"] = True
        row.setdefault("obsolete_reason", f"Résolution {res.upper()} obsolète")
        row["technical_veto"] = True

    if cat == "projector" and (
        specs.get("legacy_ports_only")
        or (specs.get("missing_hdmi") and not specs.get("has_hdmi"))
    ):
        if res in ("svga", "xga", "wvga", "vga", "") or re.search(r"(?i)\b(svga|xga|vga|wvga)\b", text):
            row["is_obsolete"] = True
            row.setdefault("obsolete_reason", "Connectique legacy sans HDMI")
            row["technical_veto"] = True

    if _CRITICAL_DEFECT_RE.search(text):
        row["critical_defect"] = True
        row["defect_reason"] = "Défaut critique signalé (HS/lampe/pièces)"
        row["technical_veto"] = True

    if _REPAIRS_NEEDED_RE.search(text):
        row["repairs_needed"] = True

    lum = int(specs.get("lumens") or row.get("lumens") or 0)
    tier = int(row.get("brand_tier") or 3)
    for field, trigger, divisor in cfg.inflated_spec_rules:
        if trigger == "tier3_high_lumens_no_ansi" and tier >= 3 and lum >= 2000:
            if not re.search(r"(?i)\bansi", text):
                row["inflated_specs"] = True
                row["inflated_lumens_claim"] = True
                adjusted = max(50, int(lum / max(1.0, divisor)))
                row["lumens_marketing"] = lum
                row["lumens"] = adjusted
                specs["lumens"] = adjusted
                specs["marketing_lumens"] = lum
                row["extracted_specs"] = {**(row.get("extracted_specs") or specs), **specs}

    row["completeness_score"] = _compute_pack_completeness(row, specs, cfg)

    if row.get("technical_veto") or row.get("is_obsolete") or row.get("critical_defect"):
        row["excluded_from_top5"] = True


def is_technical_veto_row(row: Dict[str, Any]) -> bool:
    return bool(
        row.get("technical_veto")
        or row.get("is_obsolete")
        or row.get("critical_defect")
        or row.get("excluded_from_top5")
    )


def apply_universal_tags(row: Dict[str, Any]) -> None:
    """Tags universels — remplace les libellés catégorie-spécifiques (ex. « dinosaure »)."""
    cat = str(row.get("item_type") or "")
    tier = str(row.get("quality_tier") or "ok")
    flags = str(row.get("risk_flags") or row.get("viability_issues") or "").lower()

    obsolete = bool(row.get("is_obsolete")) or tier == "dinosaur" or bool(row.get("legacy_ports_only"))
    reasons: List[str] = []
    if row.get("obsolete_reason"):
        reasons.append(str(row["obsolete_reason"]))
    if tier == "dinosaur":
        reasons.append("obsolete_tech")
    if row.get("legacy_ports_only") or "no_hdmi" in flags or "vga" in flags:
        reasons.append("legacy_ports")
    if "no_hdmi" in flags:
        reasons.append("no_hdmi")
    if row.get("underpowered") or "low_lumens" in flags:
        reasons.append("low_lumens")
    row["is_obsolete"] = obsolete or bool(reasons)
    row["obsolete_reason"] = row.get("obsolete_reason") or "|".join(dict.fromkeys(reasons)) if reasons else ""
    if row["is_obsolete"] and not row.get("obsolete_label"):
        labels = [_OBSOLETE_REASON_LABELS.get(r, r.replace("_", " ")) for r in reasons[:3]]
        row["obsolete_label"] = labels[0] if labels else "Obsolète"

    if not row.get("completeness_score"):
        try:
            comp = int(row.get("ai_completeness") or row.get("pack_score") or 0)
        except (TypeError, ValueError):
            comp = 0
        if comp <= 0 and cat.startswith("console"):
            comp = int(console_completeness_score(row)[0])
        row["completeness_score"] = max(0, min(100, comp))

    missing: List[str] = []
    pack_checks = (
        ("has_controller", "Manette"),
        ("has_power_cable", "Chargeur"),
        ("has_hdmi", "HDMI"),
        ("has_stylus", "Stylet"),
        ("phone_unlocked", "Débloqué"),
    )
    for field, label in pack_checks:
        if field in row and row.get(field) is False:
            missing.append(label)
    ai_missing = row.get("completeness_missing")
    if isinstance(ai_missing, list):
        missing.extend(str(x) for x in ai_missing if x)
    row["completeness_missing"] = list(dict.fromkeys(missing))[:8]

    repair = str(row.get("repairs_needed") or row.get("defect_hint") or row.get("ai_condition_note") or "").strip()
    if repair and re.search(r"(?i)\b(hs|pour\s+pieces|r[eé]par|d[eé]faut|cass)", repair):
        row["repairs_needed"] = repair[:200]
    elif row.get("repairs_needed"):
        row.pop("repairs_needed", None)


def console_completeness_score(row: Dict[str, Any]) -> Tuple[float, str]:
    """
    PS4 pack value 0-100: model, storage, controllers, games, completeness, condition.
    Slim > Fat; Pro premium; 2+ controllers big plus; premium games boost.
    """
    score = 38.0
    bits: List[str] = []
    variant = str(_spec_value(row, "variant") or _spec_value(row, "model") or "").lower()
    if variant == "pro":
        score += 14.0
        bits.append("PS4 Pro")
    elif variant == "slim":
        score += 10.0
        bits.append("Slim")
    elif variant == "standard":
        score += 5.0
        bits.append("Fat/standard")

    try:
        storage = int(_spec_value(row, "storage_gb") or 0)
    except (TypeError, ValueError):
        storage = 0
    if storage >= 1000:
        score += 12.0
        bits.append("1 To")
    elif storage >= 500:
        score += 6.0
        bits.append("500 Go")

    try:
        ctrl = int(
            _spec_value(row, "controllers_count")
            or _spec_value(row, "controller_count")
            or (-1 if _spec_value(row, "has_controller") is None else int(bool(_spec_value(row, "has_controller"))))
        )
    except (TypeError, ValueError):
        ctrl = -1
    if ctrl >= 2:
        score += 20.0
        bits.append(f"{ctrl} manettes")
    elif ctrl == 1:
        score += 10.0
        bits.append("1 manette")
    elif ctrl == 0:
        score -= 14.0
        bits.append("sans manette")

    try:
        games_n = int(_spec_value(row, "games_count") or 0)
    except (TypeError, ValueError):
        games_n = 0
    if games_n > 0:
        score += min(14.0, games_n * 2.5)
        bits.append(f"{games_n} jeux")
    premium = _spec_value(row, "premium_games") or []
    if premium:
        try:
            from .scoring import premium_games_boost

            boost = premium_games_boost(premium if isinstance(premium, list) else [premium])
            score += min(18.0, boost * 0.45)
            bits.append(f"premium: {', '.join(premium if isinstance(premium, list) else [str(premium)])[:60]}")
        except Exception:
            score += min(12.0, len(premium) * 4.0) if isinstance(premium, list) else 4.0

    if _spec_value(row, "has_box"):
        score += 6.0
        bits.append("boîte")
    if _spec_value(row, "has_hdmi"):
        score += 4.0
        bits.append("HDMI")
    if _spec_value(row, "has_power_cable") or row.get("has_power"):
        score += 4.0
        bits.append("alim")
    if _spec_value(row, "has_headset"):
        score += 3.0
        bits.append("casque")

    cond = str(_spec_value(row, "condition") or _spec_value(row, "condition_hint") or "").lower()
    if "tres" in cond or "excellent" in cond or cond == "neuf":
        score += 10.0
        bits.append("très bon état")
    elif "bon" in cond:
        score += 5.0
        bits.append("bon état")
    elif "defaut" in cond or "usure" in cond:
        score -= 10.0
        bits.append("défauts signalés")

    return round(max(0.0, min(100.0, score)), 1), ", ".join(bits) or "pack partiel"


def games_lot_score(row: Dict[str, Any]) -> Tuple[float, str]:
    """Score for games-only PS4 lots (when not hard-rejected)."""
    try:
        games_n = int(_spec_value(row, "games_count") or 0)
    except (TypeError, ValueError):
        games_n = 0
    games_list = _spec_value(row, "games_list") or []
    if not games_n and isinstance(games_list, list):
        games_n = len(games_list)
    score = 30.0 + min(25.0, games_n * 3.0)
    premium = _spec_value(row, "premium_games") or []
    if premium:
        try:
            from .scoring import premium_games_boost

            score += min(30.0, premium_games_boost(premium if isinstance(premium, list) else [premium]) * 0.6)
        except Exception:
            pass
    pv, pv_expl = _score_price_value(row, None)
    score = score * 0.55 + pv * 0.45
    bits = [f"{games_n} jeux", pv_expl[:80]]
    return round(max(0.0, min(100.0, score)), 1), "; ".join(bits)


def _score_completeness(row: Dict[str, Any]) -> Tuple[float, str]:
    pack = float(row.get("pack_score") or 0)
    if pack > 0:
        score = pack
    else:
        score = 44.0
        text = _row_text(row)
        if re.search(r"(?i)\b(chargeur|charger|alim|cable)\b", text):
            score += 18.0
        if re.search(r"(?i)\b(stylet|stylus)\b", text):
            score += 12.0
        if re.search(r"(?i)\b(boite|box|complet)\b", text):
            score += 10.0
        if re.search(r"(?i)\b(jeu(x)?|game|cartouche)\b", text):
            score += 8.0
        if re.search(r"(?i)\b(sans\s+chargeur|no\s+charger)\b", text):
            score -= 22.0
    bits = []
    if row.get("has_box") or row.get("complete_hint"):
        bits.append("boite/complet")
    if row.get("has_power") or str(row.get("charger_status") or "") == "with":
        bits.append("chargeur")
    elif row.get("missing_charger") or row.get("missing_power"):
        bits.append("sans chargeur")
    if row.get("includes_games"):
        bits.append("jeux inclus")
    return round(max(0.0, min(100.0, score)), 1), ", ".join(bits) or f"pack {score:.0f}/100"


def _score_platform_bonus(row: Dict[str, Any], ctx: Optional[ProMarketContext]) -> Tuple[float, str]:
    plat = str(row.get("platform") or "vinted").lower()
    if plat == "lbc":
        plat = "leboncoin"
    if plat == "vinted":
        score, bits = 78.0, ["protection acheteur Vinted"]
    elif plat == "wallapop":
        score, bits = 62.0, ["Wallapop"]
    elif plat == "leboncoin":
        score, bits = 58.0, ["LeBonCoin"]
    else:
        score, bits = 55.0, [plat]
    if ctx and ctx.platform_medians and len(ctx.platform_medians) > 1:
        p_med = ctx.platform_medians.get(plat, 0)
        cheap = min(ctx.platform_medians.values())
        if p_med > 0 and cheap > 0:
            delta = (p_med - cheap) / p_med * 100.0
            if delta > 3:
                score -= min(15.0, delta * 0.4)
                bits.append(f"+{delta:.0f}% vs plateforme moins chere")
    return round(max(0.0, min(100.0, score)), 1), " ? ".join(bits)


def _projector_specs_verified(row: Dict[str, Any], specs: Optional[Dict[str, Any]] = None) -> bool:
    specs = specs if specs is not None else extract_listing_specs(row, "projector")
    try:
        lum = int(specs.get("lumens") or row.get("lumens") or 0)
    except (TypeError, ValueError):
        lum = 0
    if lum <= 0 or specs.get("marketing_lumens"):
        return False
    if specs.get("is_mini_projector") or row.get("is_mini_projector"):
        return lum >= 800
    return lum >= 500


def value_score_v2(row: Dict[str, Any], ctx: Optional[ProMarketContext] = None) -> float:
    """Score affaire v2 — formule catégorie + pondération confiance specs."""
    base = compute_best_deal_score(row, ctx)
    cat = str(row.get("item_type") or row.get("market_category") or row.get("category") or "")
    try:
        from .engine import compute_spec_confidence

        conf = float(row.get("spec_confidence") or compute_spec_confidence(row, cat))
    except Exception:
        conf = float(row.get("spec_confidence") or 0.55)
    has_desc = len(str(row.get("description") or "")) > 50
    if has_desc:
        conf = min(1.0, conf + 0.08)
    score = base * (0.52 + 0.48 * conf)
    if row.get("suspicious_price_cluster"):
        score = min(score, base * 0.82)
        row["suspicious_price_penalty"] = True
    row["value_score_v2"] = round(max(0.0, min(100.0, score)), 1)
    return row["value_score_v2"]


def ac_price_value_score(row: Dict[str, Any], specs: Optional[Dict[str, Any]] = None) -> float:
    """Score 0-100 from price per kBTU vs typical market bands."""
    from .scoring import extract_listing_specs

    specs = specs or extract_listing_specs(row, "ac")
    price = effective_price_eur(row)
    btu = specs.get("capacity_btu") or row.get("capacity_btu")
    if not price or not btu:
        return 50.0
    try:
        eur_per_kbtu = price / (float(btu) / 1000.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 50.0
    if eur_per_kbtu <= 22:
        return 88.0
    if eur_per_kbtu <= 30:
        return 76.0
    if eur_per_kbtu <= 38:
        return 62.0
    if eur_per_kbtu <= 48:
        return 48.0
    return 32.0


def compute_best_deal_score(row: Dict[str, Any], ctx: Optional[ProMarketContext] = None) -> float:
    """Score absolu 0-100 — clé de tri principale (qualité/prix, pas prix seul)."""
    if row.get("is_accessory"):
        return 8.0
    cat = str(row.get("item_type") or row.get("market_category") or row.get("category") or "")
    text = _row_text(row)
    if cat == "projector" or "projecteur" in text.lower():
        specs = extract_listing_specs(row, "projector")
        if specs.get("is_accessory_only") or is_projector_accessory_text(text):
            row["is_accessory"] = True
            return 5.0
        if not has_projector_signal(row):
            row["is_accessory"] = True
            return 5.0
    try:
        from .scoring import is_game_only_listing, is_console_listing

        if is_game_only_listing(row, category=cat):
            return 5.0
    except Exception:
        pass
    dropship_risk, ds_flags = detect_dropship_risk(row, ctx)
    score = 50.0

    if cat == "projector" or "projecteur" in _row_text(row):
        specs = extract_listing_specs(row, "projector")
        qvs = projector_quality_value_score(row)
        row["quality_value_score"] = qvs
        pv, _ = _score_price_value(row, ctx)
        cond, _ = _score_condition(row)
        seller, _ = _score_seller_trust(row, ds_flags)
        score = qvs * 0.40 + pv * 0.25 + cond * 0.20 + seller * 0.15
        if not (specs.get("lumens") or row.get("lumens")):
            score -= 14.0
        elif specs.get("marketing_lumens"):
            score -= 18.0
        if not _model_key(row) and not specs.get("projector_brand"):
            score -= 10.0
        if specs.get("is_mini_projector") or row.get("is_mini_projector"):
            score -= 12.0
        price = effective_price_eur(row)
        med = 0.0
        try:
            med = float(row.get("model_market_median_eur") or row.get("run_market_median_eur") or 0)
        except (TypeError, ValueError):
            med = 0.0
        if med > 0 and price > 0 and price < med * 0.45 and dropship_risk >= 25:
            score -= 15.0
    elif cat in ("console_ps4", "console_ps5") or cat.startswith("console_"):
        try:
            from .scoring import is_game_only_listing

            if is_game_only_listing(row, category=cat) and row.get("allow_games_lot"):
                gls, _ = games_lot_score(row)
                return round(max(0.0, min(100.0, gls)), 1)
        except Exception:
            pass
        specs = extract_listing_specs(row, cat or "console_ps4")
        if row.get("enriched_specs"):
            for k, meta in row["enriched_specs"].items():
                if isinstance(meta, dict) and meta.get("value") not in (None, "", False):
                    specs[k] = meta["value"]
        pv, _ = _score_price_value(row, ctx)
        rel, _ = _score_relevance(row)
        cond, _ = _score_condition(row)
        pack_score, pack_expl = console_completeness_score(row)
        row["console_pack_score"] = pack_score
        row["console_pack_detail"] = pack_expl
        seller, _ = _score_seller_trust(row, ds_flags)
        score = pv * 0.24 + rel * 0.18 + pack_score * 0.30 + cond * 0.14 + seller * 0.14
        try:
            from .scoring import is_console_listing

            if is_console_listing(row, category=cat):
                score += 6.0
        except Exception:
            pass
        try:
            conf = float(row.get("spec_confidence") or 0.55)
            score += min(6.0, (conf - 0.5) * 12.0)
        except (TypeError, ValueError):
            pass
    elif cat == "phone" or str(row.get("item_type") or "") == "phone":
        battery, _ = _score_battery_health(row)
        rel, _ = _score_relevance(row)
        pv, _ = _score_price_value(row, ctx)
        cond, _ = _score_condition(row)
        score = battery * 0.30 + rel * 0.25 + pv * 0.35 + cond * 0.10
    else:
        try:
            from .scoring import normalize_category as _norm_cat
            ncat = _norm_cat(cat)
        except Exception:
            ncat = cat
        if ncat == "ac" or row.get("capacity_btu") or row.get("cooling_kw"):
            ac_specs = extract_listing_specs(row, "ac")
            if ac_specs.get("is_accessory_only") or row.get("is_accessory"):
                return 5.0
            pv, _ = _score_price_value(row, ctx)
            rel, _ = _score_relevance(row)
            cond, _ = _score_condition(row)
            seller, _ = _score_seller_trust(row, ds_flags)
            btu_val = ac_price_value_score(row, ac_specs)
            row["ac_price_value_score"] = btu_val
            completeness = 42.0
            if ac_specs.get("ac_is_unit"):
                completeness += 22.0
            if ac_specs.get("brand"):
                completeness += 12.0
            if ac_specs.get("capacity_btu") or ac_specs.get("cooling_kw"):
                completeness += 16.0
            score = pv * 0.28 + rel * 0.18 + btu_val * 0.28 + cond * 0.10 + seller * 0.10 + completeness * 0.06
        else:
            pv, _ = _score_price_value(row, ctx)
            rel, _ = _score_relevance(row)
            cond, _ = _score_condition(row)
            seller, _ = _score_seller_trust(row, ds_flags)
            score = pv * 0.35 + rel * 0.25 + cond * 0.25 + seller * 0.15

    if dropship_risk >= 40:
        score -= min(28.0, dropship_risk * 0.32)
    if row.get("deal_blocked"):
        score = min(score, 18.0)
    try:
        from .engine import row_matches_strict_intent, strict_intent_active

        if strict_intent_active(row=row) and not row_matches_strict_intent(row):
            score = min(score, 14.0)
            row["intent_mismatch"] = True
    except Exception:
        pass
    return round(max(0.0, min(100.0, score)), 1)


def classify_deal_tier(
    composite: float,
    dims: Dict[str, float],
    *,
    dropship_risk: float,
    row: Dict[str, Any],
    best_deal: Optional[float] = None,
) -> str:
    if is_technical_veto_row(row):
        return "rejet"
    if row.get("is_accessory") or row.get("deal_blocked"):
        return "rejet"
    try:
        from .scoring import is_game_only_listing

        cat = str(row.get("item_type") or row.get("market_category") or "")
        if is_game_only_listing(row, category=cat):
            return "rejet"
    except Exception:
        pass
    if dropship_risk >= 60 or str(row.get("quality_tier") or "") == "dropship":
        return "dropship_suspect"
    bds = best_deal if best_deal is not None else float(row.get("best_deal_score") or composite)
    cat = str(row.get("item_type") or row.get("market_category") or "")
    verified = True
    if cat == "projector":
        verified = _projector_specs_verified(row)
    try:
        from .engine import compute_spec_confidence

        spec_conf = float(row.get("spec_confidence") or compute_spec_confidence(row, cat))
    except Exception:
        spec_conf = float(row.get("spec_confidence") or 0.5)
    suspicious = bool(row.get("suspicious_price_cluster"))
    if bds >= 78 and verified and dropship_risk < 35 and spec_conf >= 0.6 and not suspicious:
        return "pepite"
    if bds >= 65 and dims.get("price_value", 0) >= 52 and dropship_risk < 45:
        return "bonne_affaire"
    if bds >= 48 and dropship_risk < 55:
        return "correct"
    if dropship_risk >= 40 or dims.get("relevance", 50) < 35:
        return "douteux"
    return "correct"


def _tier_label_fr(tier: str, best_deal: float) -> str:
    if tier == "pepite":
        return "pépite"
    if tier == "bonne_affaire":
        return "bonne affaire"
    if tier == "correct":
        return "correct" if best_deal >= 50 else "score modeste"
    if tier == "douteux":
        return "douteux"
    if tier in ("dropship_suspect", "rejet"):
        return tier.replace("_", " ")
    return tier.replace("_", " ")


def build_analysis_detail(
    row: Dict[str, Any],
    dims: Dict[str, str],
    tier: str,
    composite: float,
    *,
    best_deal: Optional[float] = None,
) -> str:
    bds = best_deal if best_deal is not None else float(row.get("best_deal_score") or composite)
    tier_lbl = _tier_label_fr(tier, bds)
    parts = [f"Affaire {bds:.0f}/100 — {tier_lbl} (composite {composite:.0f})."]
    if dims.get("price_value"):
        parts.append(dims["price_value"] + ".")
    qvs = row.get("quality_value_score")
    if qvs and float(qvs) > 0 and str(row.get("item_type") or "") == "projector":
        parts.append(f"Valeur qualité {float(qvs):.0f}/100.")
    if dims.get("relevance") and tier in ("pepite", "bonne_affaire"):
        parts.append(dims["relevance"].capitalize() + ".")
    if dims.get("completeness") and any(w in dims["completeness"].lower() for w in ("chargeur", "jeux", "boite")):
        parts.append(f"Inclut : {dims['completeness']}.")
    return " ".join(parts)[:480]


def compute_hyper_pro_score(row: Dict[str, Any], ctx: Optional[ProMarketContext] = None) -> Dict[str, Any]:
    dropship_risk, ds_flags = detect_dropship_risk(row, ctx)
    dropship_dim = round(100.0 - dropship_risk, 1)
    dim_scores: Dict[str, float] = {}
    dim_expl: Dict[str, str] = {}
    weights = _weights_for_row(row)
    fns: List[Tuple[str, Any]] = [
        ("price_value", lambda: _score_price_value(row, ctx)),
        ("relevance", lambda: _score_relevance(row)),
        ("condition", lambda: _score_condition(row)),
        ("seller_trust", lambda: _score_seller_trust(row, ds_flags)),
        ("completeness", lambda: _score_completeness(row)),
        ("platform_bonus", lambda: _score_platform_bonus(row, ctx)),
    ]
    cat = str(row.get("item_type") or row.get("market_category") or row.get("category") or "")
    specs = extract_listing_specs(row, cat) if cat else {}
    use_battery = cat == "phone" or (cat in ("bicycle", "velo") and (row.get("is_ebike") or specs.get("is_ebike")))
    if use_battery and weights.get("battery_health", 0) > 0:
        fns.insert(3, ("battery_health", lambda: _score_battery_health(row)))
    for name, fn in fns:
        s, e = fn()
        dim_scores[name] = s
        dim_expl[name] = e
    dim_scores["dropship_risk"] = dropship_dim
    dim_expl["dropship_risk"] = f"risque dropship {dropship_risk:.0f}/100" + (f" ({', '.join(ds_flags[:3])})" if ds_flags else "")
    composite = round(max(0.0, min(100.0, sum(dim_scores.get(k, 50.0) * w for k, w in weights.items()))), 1)
    best_deal = value_score_v2(row, ctx)
    tier = classify_deal_tier(composite, dim_scores, dropship_risk=dropship_risk, row=row, best_deal=best_deal)
    if is_technical_veto_row(row):
        composite = min(composite, 15.0)
        best_deal = min(best_deal, 15.0)
        tier = "rejet"
        row["excluded_from_top5"] = True
    if tier == "dropship_suspect":
        composite = min(composite, 30.0)
        best_deal = min(best_deal, 28.0)
    elif tier == "rejet":
        composite = min(composite, 15.0)
        best_deal = min(best_deal, 12.0)
    breakdown = {k: {"score": dim_scores.get(k, 0), "explanation": dim_expl.get(k, "")} for k in list(weights) + ["dropship_risk"]}
    return {
        "score_composite": composite,
        "composite_score": composite,
        "best_deal_score": best_deal,
        "score_breakdown": breakdown,
        "deal_tier": tier,
        "dropship_risk_score": dropship_risk,
        "dropship_flags": "|".join(ds_flags)[:120],
        "analysis_detail": build_analysis_detail(row, dim_expl, tier, composite, best_deal=best_deal),
        "comparison_insight": dim_expl.get("price_value", ""),
        "dimension_scores": dim_scores,
    }


def apply_hyper_pro_scoring(rows: List[Dict[str, Any]], ctx: Optional[ProMarketContext] = None, *, sync_quality_tier: bool = True) -> ProMarketContext:
    if ctx is None:
        ctx = build_market_context(rows)
    for row in rows:
        cat = str(row.get("item_type") or row.get("market_category") or "")
        apply_category_heuristics(row, cat)
        _enrich_row_market_fields(row, ctx)
        result = compute_hyper_pro_score(row, ctx)
        row.update({
            "score_composite": result["score_composite"],
            "composite_score": result["composite_score"],
            "best_deal_score": result["best_deal_score"],
            "score_breakdown": result["score_breakdown"],
            "deal_tier": result["deal_tier"],
            "dropship_risk_score": result["dropship_risk_score"],
            "analysis_detail": result["analysis_detail"],
            "comparison_insight": result["comparison_insight"],
            "dimension_scores": result["dimension_scores"],
            "display_rank_score": result["best_deal_score"],
            "rank_score": result["best_deal_score"],
            "_pre_rank_signal": result["best_deal_score"],
        })
        if result.get("dropship_flags"):
            row["dropship_flags"] = result["dropship_flags"]
        row["composite_breakdown"] = " ? ".join(
            f"{k}:{result['dimension_scores'].get(k, 0):.0f}" for k in ("price_value", "relevance", "dropship_risk", "completeness")
        )[:220]
        if sync_quality_tier:
            prev = str(row.get("quality_tier") or "ok")
            if result["deal_tier"] == "dropship_suspect":
                row["quality_tier"] = "dropship"
            elif result["deal_tier"] == "pepite" and prev not in ("dropship", "dinosaur"):
                row["quality_tier"] = "gem"
            elif result["deal_tier"] == "douteux" and prev == "ok":
                row["quality_tier"] = "suspicious"
            elif result["deal_tier"] == "rejet":
                row["quality_tier"] = "suspicious"
    return ctx


def projector_quality_value_score(row: Dict[str, Any]) -> float:
    """Score qualité/prix projecteur : lumens/euro, résolution, modèle connu."""
    cat = str(row.get("item_type") or row.get("market_category") or "")
    text = _row_text(row)
    if cat != "projector" and "projecteur" not in text:
        return float(row.get("quality_value_score") or row.get("score_composite") or 50.0)
    specs = extract_listing_specs(row, "projector")
    if is_technical_veto_row(row) or specs.get("is_accessory_only") or is_projector_accessory_text(text) or not has_projector_signal(row):
        return 5.0
    price = effective_price_eur(row)
    if price <= 0:
        return 20.0
    try:
        lum = int(specs.get("lumens") or row.get("lumens") or 0)
    except (TypeError, ValueError):
        lum = 0
    score = 42.0
    if lum > 0:
        lpe = lum / price
        row["lumens_per_euro"] = round(lpe, 2)
        if lpe >= 25:
            score += 28.0
        elif lpe >= 18:
            score += 20.0
        elif lpe >= 12:
            score += 12.0
        elif lpe >= 8:
            score += 5.0
        else:
            score -= 8.0
        if lum >= 3500:
            score += 14.0
        elif lum >= 2500:
            score += 8.0
        elif lum < 500:
            score -= 22.0
    res = str(specs.get("resolution") or "").lower()
    if res in ("4k", "1080p"):
        score += 10.0
    elif res in ("xga", "wxga", "svga"):
        score -= 6.0
    brand = str(specs.get("projector_brand") or "").lower()
    if brand in ("epson", "optoma", "benq", "viewsonic", "sony"):
        score += 10.0
    elif brand in _RECOGNIZED_PROJECTOR_BRANDS:
        score += 6.0
    if specs.get("has_hdmi") is True:
        score += 6.0
    elif specs.get("missing_hdmi") or specs.get("legacy_ports_only"):
        score -= 22.0
    if re.search(r"(?i)\b(vga|svga|xga)\b", text) and not specs.get("has_hdmi"):
        score -= 18.0
    if specs.get("is_mini_projector") or row.get("is_mini_projector"):
        score -= 18.0
    if specs.get("marketing_lumens"):
        score -= 12.0
    med = 0.0
    try:
        med = float(row.get("model_market_median_eur") or row.get("run_market_median_eur") or 0)
    except (TypeError, ValueError):
        med = 0.0
    if med > 0 and price > 0 and (lum > 0 or brand in _RECOGNIZED_PROJECTOR_BRANDS):
        disc = clamp_discount_pct((1.0 - price / med) * 100.0)
        if 10 <= disc <= 45:
            score += min(15.0, disc * 0.25)
        elif disc < -10:
            score -= 10.0
    mk = str(row.get("product_model_key") or specs.get("product_model_key") or "")
    if mk and mk != "unknown" and mk in (
        "optoma_hd146x", "optoma_uhd38", "optoma_uhd38x",
        "epson_eb_955wh", "epson_eh_tw650", "benq_w1700",
    ):
        score += 8.0
    return round(max(0.0, min(100.0, score)), 1)


def hyper_pro_value_score(row: Dict[str, Any]) -> float:
    if row.get("is_accessory"):
        return -1000.0
    bds = float(row.get("best_deal_score") or 0)
    if bds > 0:
        tier = str(row.get("deal_tier") or "")
        if tier in ("dropship_suspect", "rejet"):
            return bds - 35.0
        return bds
    cat = str(row.get("item_type") or row.get("market_category") or "")
    if cat == "projector":
        qvs = projector_quality_value_score(row)
        row["quality_value_score"] = qvs
        base = float(row.get("score_composite") or row.get("composite_score") or 0)
        pv = float((row.get("dimension_scores") or {}).get("price_value") or 0)
        ds = float(row.get("dropship_risk_score") or 0)
        score = base * 0.35 + qvs * 0.55 + pv * 0.08 - ds * 0.25
        tier = str(row.get("deal_tier") or "")
        if tier == "pepite":
            score += 10.0
        elif tier in ("dropship_suspect", "rejet"):
            score -= 35.0
        return score
    base = float(row.get("score_composite") or row.get("composite_score") or 0)
    pv = float((row.get("dimension_scores") or {}).get("price_value") or 0)
    ds = float(row.get("dropship_risk_score") or 0)
    score = base + pv * 0.12 - ds * 0.35
    tier = str(row.get("deal_tier") or "")
    if tier == "pepite":
        score += 12.0
    elif tier in ("dropship_suspect", "rejet"):
        score -= 40.0
    elif tier == "douteux":
        score -= 15.0
    return score


def score_breakdown_for_export(row: Dict[str, Any]) -> str:
    bd = row.get("score_breakdown")
    return json.dumps(bd, ensure_ascii=False) if isinstance(bd, dict) else str(bd or "")


def top_pepites_summary(rows: List[Dict[str, Any]], *, limit: int = 5) -> List[Dict[str, Any]]:
    pepites = [r for r in rows if str(r.get("deal_tier") or "") == "pepite" and not r.get("is_accessory")]
    pepites.sort(key=lambda r: float(r.get("best_deal_score") or r.get("score_composite") or 0), reverse=True)
    return [
        {
            "rank": i,
            "id": r.get("id"),
            "title": str(r.get("title") or "")[:100],
            "price_eur": r.get("total_price") or r.get("price"),
            "platform": r.get("platform"),
            "score": r.get("score_composite"),
            "why": r.get("analysis_detail") or r.get("comparison_insight"),
            "url": r.get("url"),
        }
        for i, r in enumerate(pepites[:limit], start=1)
    ]


# --- top5_analyst.py ---

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict


logger = logging.getLogger("vf.top5")


class Top5Item(TypedDict, total=False):
    rank: int
    listing_id: str
    title: str
    price: float
    score: int
    verdict: str
    justification: str
    highlights: List[str]
    warnings: List[str]
    vs_market: str
    url: str
    photo_url: str
    platform: str
    extracted_specs: Dict[str, Any]


class Top5Result(TypedDict, total=False):
    top5: List[Top5Item]
    market_summary: str
    avoid: List[str]
    category: str
    eval_context: Dict[str, Any]
    near_misses: List[Dict[str, Any]]


def select_top_candidates(
    rows: List[Dict[str, Any]],
    *,
    category: str,
    query: str,
    intent: Optional[Dict[str, Any]] = None,
    limit: int = 100,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    mains = [
        r
        for r in rows
        if not r.get("is_accessory")
        and not is_technical_veto_row(r)
        and str(r.get("deal_tier") or "") not in ("rejet", "dropship_suspect")
        and (
            str(r.get("quality_tier") or "") not in ("dropship", "dinosaur")
            or float(r.get("best_deal_score") or 0) >= 55.0
        )
    ]
    cat_norm = normalize_category(category)
    if cat_norm in ("console_ps4", "console_ps5"):
        try:
            from .scoring import is_game_only_listing

            mains = [r for r in mains if not is_game_only_listing(r, category=cat_norm)]
        except Exception:
            pass
    if cat_norm == "projector":
        try:
            mains = [
                r
                for r in mains
                if has_projector_signal(r) and not is_projector_accessory_text(_row_text(r))
            ]
        except Exception:
            pass
    if cat_norm == "ac":
        try:
            from .engine import _AC_ACCESSORY_RE as _ac_acc_re

            def _ac_is_credible(r: dict) -> bool:
                if r.get("is_accessory") or r.get("is_accessory_only"):
                    return False
                title = str(r.get("title") or "")
                if _ac_acc_re.search(title):
                    return False
                return True

            mains = [r for r in mains if _ac_is_credible(r)]
        except Exception:
            pass
    eval_ctx = build_eval_context(category, intent, mains, query=query)
    try:
        from .engine import merge_brain_into_eval_context

        brain = (intent or {}).get("category_brain") if isinstance(intent, dict) else None
        if brain:
            eval_ctx = merge_brain_into_eval_context(eval_ctx, brain)
    except Exception:
        pass
    from .scoring import build_market_context, compute_best_deal_score, projector_quality_value_score

    mkt_ctx = build_market_context(mains)
    scored: List[Dict[str, Any]] = []
    cat_norm = normalize_category(category)
    for r in mains:
        apply_category_heuristics(r, cat_norm)
        meta = prescore_listing(r, eval_ctx)
        if r.get("best_deal_score") is None:
            r["best_deal_score"] = compute_best_deal_score(r, mkt_ctx)
        if cat_norm == "projector":
            if r.get("quality_value_score") is None:
                r["quality_value_score"] = projector_quality_value_score(r)
        scored.append({**r, "_top5_meta": meta, "_top5_pre": meta["pre_score"]})
    if cat_norm == "projector":
        scored = [
            r
            for r in scored
            if has_projector_signal(r)
            and not is_projector_accessory_text(_row_text(r))
            and float(r.get("quality_value_score") or 0) >= 35.0
        ]
    scored.sort(
        key=lambda x: (
            float(x.get("best_deal_score") or 0),
            float(x.get("quality_value_score") or 0) if cat_norm == "projector" else 0.0,
            float(x.get("_top5_pre") or 0),
            hyper_pro_value_score(x),
            float(x.get("score_composite") or 0),
        ),
        reverse=True,
    )
    return scored[:limit], eval_ctx


def _fallback_top5(candidates: List[Dict[str, Any]], eval_ctx: Dict[str, Any]) -> Top5Result:
    cat = eval_ctx.get("category") or "generic"
    med = float((eval_ctx.get("market_reference") or {}).get("median") or 0)
    prof = profile_for_category(cat)
    out: List[Top5Item] = []
    for i, r in enumerate(candidates[:5], 1):
        price = effective_price_eur(r)
        meta = r.get("_top5_meta") or {}
        specs = meta.get("specs") or {}
        if med > 0 and price > 0:
            vs = f"{(1 - price / med) * 100:+.0f}% vs mediane {med:.0f} EUR"
        else:
            vs = ""
        hl = spec_badges(specs, cat) or [compact_specs_line(specs, cat)]
        hl = [x for x in hl if x and x != "-"]
        if not hl:
            hl = ["Annonce coherente"]
        out.append(
            {
                "rank": i,
                "listing_id": str(r.get("id") or ""),
                "title": str(r.get("title") or "")[:120],
                "price": price,
                "score": int(r.get("best_deal_score") or meta.get("pre_score") or 70),
                "verdict": "Meilleur rapport qualite-prix" if i == 1 else "Alternative",
                "justification": f"Prix {price:.0f} EUR. {vs}".strip(),
                "highlights": hl,
                "warnings": list(meta.get("flags") or [])[:3],
                "vs_market": vs,
                "url": r.get("url"),
                "photo_url": r.get("photo_url"),
                "platform": r.get("platform"),
                "extracted_specs": specs,
            }
        )
    summary = f"Fourchette ~{med:.0f} EUR" if med else "Analyse rule-based."
    return {
        "top5": out,
        "market_summary": summary,
        "avoid": list(eval_ctx.get("avoid_patterns") or prof.avoid_patterns),
        "category": cat,
        "eval_context": eval_ctx,
    }


def analyze_top5(
    listings: List[Dict[str, Any]],
    *,
    query: str,
    category: str,
    intent: Optional[Dict[str, Any]] = None,
    market_stats: Optional[Dict[str, Any]] = None,
    top_n_candidates: int = 100,
    use_ai: bool = True,
    run_folder: Optional[str | Path] = None,
    log_fn: Optional[Any] = None,
) -> Top5Result:
    intent_merged: Dict[str, Any] = dict(intent or {})
    if market_stats:
        if market_stats.get("median_eur"):
            intent_merged["market_median_eur"] = market_stats["median_eur"]
        if market_stats.get("median"):
            intent_merged["market_median_eur"] = market_stats["median"]

    candidates, eval_ctx = select_top_candidates(
        listings,
        category=category,
        query=query,
        intent=intent_merged,
        limit=top_n_candidates,
    )
    if not candidates:
        return {
            "top5": [],
            "market_summary": "Aucun candidat.",
            "avoid": [],
            "category": normalize_category(category),
            "eval_context": eval_ctx,
        }

    data = _fallback_top5(candidates, eval_ctx)
    if use_ai:
        try:
            from .ai import api_key, analyze_top5_batch

            if api_key():
                data = analyze_top5_batch(
                    query,
                    eval_ctx.get("category") or category,
                    candidates,
                    eval_ctx,
                )
                data.setdefault("category", eval_ctx.get("category"))
                data.setdefault("eval_context", eval_ctx)
        except Exception as exc:
            logger.warning("top5: %s", exc)
            if log_fn:
                log_fn(f"Top 5 IA (regles): {exc}")

    by_id = {str(r.get("id") or ""): r for r in candidates}
    cat_norm = normalize_category(data.get("category") or category)

    def _refine_top5(data_in: Top5Result) -> Top5Result:
        items = list(data_in.get("top5") or [])
        if cat_norm == "projector":
            items = [
                it
                for it in items
                if has_projector_signal(by_id.get(str(it.get("listing_id") or "")) or {})
                and not is_projector_accessory_text(
                    _row_text(by_id.get(str(it.get("listing_id") or "")) or {})
                )
                and float((by_id.get(str(it.get("listing_id") or "")) or {}).get("quality_value_score") or 0) >= 50.0
            ]
        items.sort(
            key=lambda it: float(
                (by_id.get(str(it.get("listing_id") or "")) or {}).get("best_deal_score")
                or it.get("score")
                or 0
            ),
            reverse=True,
        )
        seen = {str(it.get("listing_id") or "") for it in items}
        for r in candidates:
            if len(items) >= 5:
                break
            rid = str(r.get("id") or "")
            if not rid or rid in seen:
                continue
            if cat_norm == "projector":
                if not has_projector_signal(r) or is_projector_accessory_text(_row_text(r)):
                    continue
                if float(r.get("quality_value_score") or 0) < 50.0:
                    continue
            meta = r.get("_top5_meta") or {}
            specs = meta.get("specs") or {}
            price = effective_price_eur(r)
            hl = spec_badges(specs, cat_norm) or [compact_specs_line(specs, cat_norm)]
            hl = [x for x in hl if x and x != "-"] or ["Specs a verifier"]
            items.append(
                {
                    "rank": len(items) + 1,
                    "listing_id": rid,
                    "title": str(r.get("title") or "")[:120],
                    "price": price,
                    "score": int(r.get("best_deal_score") or meta.get("pre_score") or 70),
                    "verdict": "Alternative",
                    "justification": f"Score affaire {float(r.get('best_deal_score') or 0):.0f}/100.",
                    "highlights": hl,
                    "warnings": list(meta.get("flags") or [])[:3],
                    "vs_market": "",
                }
            )
            seen.add(rid)
        for i, it in enumerate(items[:5], 1):
            it["rank"] = i
            src = by_id.get(str(it.get("listing_id") or ""))
            if src:
                it["score"] = int(src.get("best_deal_score") or it.get("score") or 0)
        data_in["top5"] = items[:5]
        return data_in

    data = _refine_top5(data)

    try:
        from .engine import find_near_misses

        selected = {str(it.get("listing_id") or "") for it in data.get("top5") or []}
        data["near_misses"] = find_near_misses(candidates, selected, limit=3)
    except Exception:
        data.setdefault("near_misses", [])

    by_id = {str(r.get("id") or ""): r for r in candidates}
    for item in data.get("top5") or []:
        src = by_id.get(str(item.get("listing_id") or ""))
        if src:
            item.setdefault("url", src.get("url"))
            item.setdefault("photo_url", src.get("photo_url"))
            item.setdefault("platform", src.get("platform"))
            meta = src.get("_top5_meta") or {}
            if not item.get("extracted_specs"):
                item["extracted_specs"] = meta.get("specs") or {}
            if not item.get("highlights"):
                item["highlights"] = spec_badges(
                    item.get("extracted_specs") or {},
                    data.get("category") or category,
                )
            arb = src.get("arbitrage_hint")
            if arb:
                item["arbitrage_hint"] = arb

    if run_folder:
        from .export import _safe_json_write

        folder = Path(run_folder)
        export = {k: v for k, v in data.items() if k != "eval_context"}
        _safe_json_write(folder / "top5_analysis.json", export)
        _safe_json_write(folder / "eval" / "top5_analysis.json", export)
        md = ["# Top 5 IA", "", data.get("market_summary", "")]
        for it in data.get("top5") or []:
            md += [
                "",
                f"## #{it.get('rank')} {it.get('title', '')[:70]}",
                it.get("justification", ""),
                f"Score {it.get('score')} - {it.get('price')} EUR",
            ]
        (folder / "top5_analysis.md").write_text("\n".join(md), encoding="utf-8")

    return data


def run_top5_analysis(
    rows: List[Dict[str, Any]],
    *,
    query: str,
    category: str = "generic",
    use_ai: bool = True,
    run_folder: Optional[str | Path] = None,
    log_fn: Optional[Any] = None,
    intent: Optional[Dict[str, Any]] = None,
) -> Top5Result:
    return analyze_top5(
        rows,
        query=query,
        category=category,
        intent=intent,
        use_ai=use_ai,
        run_folder=run_folder,
        log_fn=log_fn,
    )


# --- ai_ranker.py ---
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


Row = Dict[str, Any]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _as_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _as_conf(v: Any) -> float:
    c = _as_float(v)
    if c > 1.0:
        # some fields are 0..100
        c = c / 100.0
    return max(0.0, min(1.0, c))


def _row_spec_int(row: Row, *keys: str) -> int:
    for k in keys:
        if not k:
            continue
        if row.get(k) not in (None, ""):
            return _as_int(row.get(k))
        es = row.get("enriched_specs")
        if isinstance(es, dict):
            meta = es.get(k)
            if isinstance(meta, dict) and meta.get("value") not in (None, ""):
                return _as_int(meta.get("value"))
    return 0


def _row_spec_bool(row: Row, *keys: str) -> bool:
    for k in keys:
        if not k:
            continue
        if row.get(k) not in (None, ""):
            return bool(row.get(k))
        es = row.get("enriched_specs")
        if isinstance(es, dict):
            meta = es.get(k)
            if isinstance(meta, dict) and meta.get("value") not in (None, ""):
                return bool(meta.get("value"))
    return False


def _risk_penalty(row: Row) -> float:
    flags = str(row.get("risk_flags") or "").strip().lower()
    if not flags and not row.get("deal_blocked"):
        return 0.0
    penalty = 8.0
    if "ban" in flags or "banni" in flags:
        penalty += 16.0
    if "scam" in flags or "fraud" in flags:
        penalty += 14.0
    return penalty


def _quality_penalty(row: Row) -> float:
    tier = str(row.get("ai_quality_tier") or row.get("quality_tier") or "").strip().lower()
    if tier == "gem":
        return -2.0
    if tier == "ok":
        return 0.0
    if tier == "suspicious":
        return 7.0
    if tier == "dropship":
        return 18.0
    if tier == "dinosaur":
        return 22.0
    return 0.0


def _criteria_hits(row: Row, criteria_plan: Optional[Dict[str, Any]]) -> List[str]:
    """
    Détermine quelques "hits" déterministes depuis les champs déjà extraits/enrichis.
    Pas de LLM par annonce.
    """
    hits: List[str] = []
    cat = str(row.get("item_type") or row.get("market_category") or "").strip().lower()
    if row.get("has_controller") or _row_spec_int(row, "controller_count", "controllers_count") >= 1:
        hits.append("manette")
    if _row_spec_int(row, "controller_count", "controllers_count") >= 2:
        hits.append("2+ manettes")
    st = _row_spec_int(row, "storage_gb")
    if st >= 1000:
        hits.append("1 To")
    if row.get("has_hdmi") or _row_spec_bool(row, "has_hdmi"):
        hits.append("HDMI")
    if (
        row.get("has_power_cable")
        or row.get("has_power")
        or _row_spec_bool(row, "has_power_cable", "has_power")
        or not row.get("missing_charger")
    ):
        hits.append("alimentation")
    if row.get("includes_games") or _row_spec_int(row, "games_count") >= 1:
        hits.append("jeux")
    _cat_hits = str(row.get("item_type") or "").strip().lower()
    if _cat_hits in ("ac", "clim", "climatisation"):
        btu = _row_spec_int(row, "btu", "capacity_btu")
        if btu >= 9000:
            hits.append(f"{btu} BTU")
        elif btu >= 7000:
            hits.append(f"{btu} BTU")
        kw = _as_float(row.get("cooling_kw") or _row_spec_int(row, "cooling_kw") or 0)
        if kw >= 2.0 and not btu:
            hits.append(f"{kw:.1f} kW")
        if _row_spec_bool(row, "has_hose"):
            hits.append("tuyau")
    if _cat_hits == "projector":
        lum = _row_spec_int(row, "lumens")
        if lum >= 3000:
            hits.append("3000+ lumens")
        elif lum >= 2000:
            hits.append("2000+ lumens")
        if str(row.get("resolution") or "").lower() in ("1080p", "4k"):
            hits.append(str(row.get("resolution")).upper())
    # Criteria plan: if it exposes scoring_hints / ui_filters, keep a minimal trace.
    try:
        if isinstance(criteria_plan, dict):
            label = str(criteria_plan.get("label") or "").strip()
            if label:
                hits.append(label[:24])
    except Exception:
        pass
    # De-dup stable order
    out: List[str] = []
    seen = set()
    for h in hits:
        hh = str(h).strip()
        if not hh or hh.lower() in seen:
            continue
        seen.add(hh.lower())
        out.append(hh)
    return out[:8]


@dataclass(slots=True)
class RankConfig:
    top_n_candidates: int = 200


def compute_ai_adjustment(
    row: Row,
    *,
    category: str = "",
    criteria_plan: Optional[Dict[str, Any]] = None,
    category_brain: Optional[Dict[str, Any]] = None,
) -> Tuple[float, List[str], str]:
    """
    Calcule un ajustement IA "scalable" sans LLM par annonce.
    Retourne (adjustment, criteria_hits, reason_str).
    """
    del category_brain  # reserved for future deterministic enrichments
    hits = _criteria_hits(row, criteria_plan)

    # Signals already produced by the pipeline (LLM used only on top-K deep pool).
    rel = _as_float(row.get("ai_relevance") or 0.0)  # 0..100
    fit = _as_float(row.get("buyer_fit_score") or 0.0)  # 0..100
    comp = _as_float(row.get("ai_completeness") or row.get("pack_score") or row.get("console_pack_score") or 0.0)  # 0..100
    conf = _as_conf(row.get("spec_confidence") or row.get("heuristic_confidence") or 0.55)  # 0..1

    adj = 0.0
    # Confidence: reward verified specs, penalize unknown/low confidence.
    adj += (conf - 0.65) * 18.0
    # Buyer fit & relevance (when available) act as "AI signals".
    if rel > 0:
        adj += (rel - 70.0) * 0.20
    if fit > 0:
        adj += (fit - 70.0) * 0.14
    if comp > 0:
        adj += (comp - 70.0) * 0.10

    # Hard penalties.
    adj -= _risk_penalty(row)
    adj -= _quality_penalty(row)
    if row.get("missing_charger") or row.get("missing_power") or row.get("missing_power_cable"):
        adj -= 8.0
    if row.get("missing_hdmi"):
        adj -= 10.0
    if str(row.get("triage_decision") or "") == "doubtful":
        adj -= 6.0
    # AC-specific: heavily penalize accessories / non-units in AC market
    _cat = (category or str(row.get("item_type") or "")).strip().lower()
    if _cat in ("ac", "clim", "climatisation"):
        if row.get("is_accessory") or row.get("is_accessory_only"):
            adj -= 40.0
        else:
            es = row.get("enriched_specs") or {}
            ac_is_unit = es.get("ac_is_unit")
            if isinstance(ac_is_unit, dict):
                ac_is_unit = ac_is_unit.get("value")
            if ac_is_unit is False:
                adj -= 40.0
            elif ac_is_unit is None:
                # No BTU and no unit signal → mild penalty for ambiguity
                btu = _row_spec_int(row, "btu", "capacity_btu")
                if not btu:
                    adj -= 8.0

    # Small bonus for "pepite" tier when present.
    if str(row.get("deal_tier") or "") == "pepite":
        adj += 3.5
    if str(row.get("quality_tier") or "") == "gem":
        adj += 2.5

    # Reason (short, UI friendly).
    reasons: List[str] = []
    if rel >= 75:
        reasons.append(f"IA: pertinence {int(rel)}/100")
    if fit >= 75:
        reasons.append(f"besoins: {int(fit)}/100")
    if comp >= 80:
        reasons.append(f"pack: {int(comp)}%")
    if conf >= 0.80:
        reasons.append(f"specs: {int(conf*100)}%")
    if hits:
        reasons.append(", ".join(hits[:3]))
    if not reasons:
        reasons.append("classement IA (règles + signaux)")

    return float(round(adj, 2)), hits, " · ".join(reasons[:3])


def rank_rows(
    rows: List[Row],
    *,
    category: str = "",
    criteria_plan: Optional[Dict[str, Any]] = None,
    category_brain: Optional[Dict[str, Any]] = None,
    config: Optional[RankConfig] = None,
) -> List[Row]:
    """
    Produit un ranking complet sur toutes les annonces.
    Mutates rows (adds rank_final*, ai_adjustment, rank_reason, criteria_hits).
    """
    cfg = config or RankConfig()
    # Candidate pre-selection: to compute stable adjustments we don't need to drop,
    # but we cap heavy derived computations here if needed later.
    _ = cfg

    for r in rows:
        # Ensure base value exists when possible.
        base = _as_float(r.get("value_score_v2") or r.get("quality_value_score") or 0.0)
        if base <= 0:
            base = _as_float(r.get("best_deal_score") or r.get("score_composite") or r.get("rank_score") or 0.0)
        adj, hits, reason = compute_ai_adjustment(
            r,
            category=category,
            criteria_plan=criteria_plan,
            category_brain=category_brain,
        )
        final = _clamp(base + adj, 0.0, 100.0)
        r["ai_adjustment"] = float(round(adj, 2))
        r["rank_final_score"] = float(round(final, 2))
        r["final_rank_score"] = r["rank_final_score"]
        r["rank_reason"] = reason[:220]
        r["criteria_hits"] = "|".join(hits)[:420]

    ranked = sorted(rows, key=lambda r: _as_float(r.get("rank_final_score") or 0.0), reverse=True)
    for i, r in enumerate(ranked, start=1):
        r["rank_final"] = i
    return ranked


# --- spec_enrichment.py ---

import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional


logger = logging.getLogger("vf.spec_enrichment")

_AI_SKIP_CONFIDENCE = 0.8
_BATCH_SIZE = 20
_RULE_WORKERS = 8
_SPEC_CACHE: Dict[str, Dict[str, Any]] = {}


def _listing_id(listing: Dict[str, Any]) -> str:
    return str(listing.get("id") or listing.get("listing_id") or "")


def _cache_key(listing: Dict[str, Any], category: str) -> str:
    lid = _listing_id(listing)
    return f"{normalize_category(category)}:{lid}" if lid else ""


def _wrap_rules(raw: Dict[str, Any], *, base_conf: float = 0.72) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if v in (None, "", False) and k not in ("has_hdmi", "has_controller", "icloud_locked", "is_ebike"):
            continue
        out[k] = {"value": v, "confidence": base_conf, "source": "rules"}
    return out


class SpecEnricher(ABC):
    """Rule-based extraction + optional LLM batch fill for missing fields."""

    category: str = "generic"

    @abstractmethod
    def extract_specs(self, listing: Dict[str, Any]) -> Dict[str, Any]:
        """Return enriched_specs dict: field -> {value, confidence, source}."""

    def enrich_batch(
        self,
        listings: List[Dict[str, Any]],
        *,
        use_ai: bool = True,
    ) -> None:
        for listing in listings:
            self.apply_to_listing(listing, use_ai=False)
        if use_ai:
            self._ai_fill_missing(listings)

    def apply_to_listing(self, listing: Dict[str, Any], *, use_ai: bool = False) -> Dict[str, Any]:
        if _should_skip_reenrich(listing):
            return listing
        ck = _cache_key(listing, self.category)
        if ck and ck in _SPEC_CACHE:
            cached = _SPEC_CACHE[ck]
            listing["enriched_specs"] = cached.get("enriched_specs") or {}
            listing["spec_confidence"] = cached.get("spec_confidence", 0.55)
            listing["enriched_by"] = cached.get("enriched_by", "cache")
            self._flatten_specs(listing, listing["enriched_specs"])
            return listing

        specs = self.extract_specs(listing)
        listing["enriched_specs"] = specs
        listing["enriched_by"] = self._dominant_source(specs)
        self._flatten_specs(listing, specs)
        listing["spec_confidence"] = self._aggregate_confidence(specs)

        if ck:
            _SPEC_CACHE[ck] = {
                "enriched_specs": specs,
                "spec_confidence": listing["spec_confidence"],
                "enriched_by": listing["enriched_by"],
            }
        if use_ai and float(listing.get("spec_confidence") or 0) < _AI_SKIP_CONFIDENCE:
            self._ai_fill_missing([listing])
        return listing

    def _ai_fill_missing(self, listings: List[Dict[str, Any]]) -> None:
        needs = [r for r in listings if float(r.get("spec_confidence") or 0) < _AI_SKIP_CONFIDENCE]
        if not needs:
            return
        try:
            from .ai import batch_enrich_specs

            for off in range(0, len(needs), _BATCH_SIZE):
                chunk = needs[off : off + _BATCH_SIZE]
                ai_map = batch_enrich_specs(chunk, self.category)
                for listing in chunk:
                    lid = _listing_id(listing)
                    patch = ai_map.get(lid) or {}
                    if patch:
                        self._merge_ai(listing, patch)
        except Exception as exc:
            logger.debug("_ai_fill_missing(%s): %s", self.category, exc)

    def _merge_ai(self, listing: Dict[str, Any], patch: Dict[str, Any]) -> None:
        specs = dict(listing.get("enriched_specs") or {})
        for field, val in patch.items():
            if field.startswith("_") or val in (None, ""):
                continue
            try:
                conf = float(patch.get(f"_conf_{field}") or 0.68)
            except (TypeError, ValueError):
                conf = 0.68
            specs[field] = {"value": val, "confidence": conf, "source": "ai"}
        listing["enriched_specs"] = specs
        listing["enriched_by"] = self._dominant_source(specs)
        self._flatten_specs(listing, specs)
        listing["spec_confidence"] = self._aggregate_confidence(specs)
        ck = _cache_key(listing, self.category)
        if ck:
            _SPEC_CACHE[ck] = {
                "enriched_specs": specs,
                "spec_confidence": listing["spec_confidence"],
                "enriched_by": listing["enriched_by"],
            }

    @staticmethod
    def _dominant_source(specs: Dict[str, Any]) -> str:
        sources = {str(v.get("source") or "rules") for v in specs.values() if isinstance(v, dict)}
        if "ai" in sources and "rules" in sources:
            return "rules|ai"
        if "ai" in sources:
            return "ai"
        if "cascade" in sources:
            return "cascade|rules"
        return "rules"

    @staticmethod
    def _aggregate_confidence(specs: Dict[str, Any]) -> float:
        confs = [
            float(v.get("confidence") or 0)
            for v in specs.values()
            if isinstance(v, dict) and v.get("value") not in (None, "", False)
        ]
        if not confs:
            return 0.45
        return round(min(1.0, sum(confs) / len(confs)), 3)

    @staticmethod
    def _flatten_specs(listing: Dict[str, Any], specs: Dict[str, Any]) -> None:
        for field, meta in specs.items():
            if not isinstance(meta, dict):
                continue
            val = meta.get("value")
            if val in (None, ""):
                continue
            if listing.get(field) in (None, "", 0, False):
                listing[field] = val
        ctrl = specs.get("controllers_count") or specs.get("controller_count")
        if isinstance(ctrl, dict) and ctrl.get("value") is not None:
            listing["controllers_count"] = ctrl["value"]
            listing["controller_count"] = ctrl["value"]
            listing["has_controller"] = int(ctrl["value"] or 0) > 0
        games = specs.get("games_list")
        if isinstance(games, dict) and games.get("value"):
            listing["games_list"] = games["value"]
        premium = specs.get("premium_games")
        if isinstance(premium, dict) and premium.get("value"):
            listing["premium_games"] = premium["value"]
        cond = specs.get("condition") or specs.get("condition_hint")
        if isinstance(cond, dict) and cond.get("value"):
            listing.setdefault("condition_hint", cond["value"])


def _should_skip_reenrich(listing: Dict[str, Any]) -> bool:
    """Skip if cascade already populated specs with high confidence."""
    if listing.get("enriched_specs") and float(listing.get("spec_confidence") or 0) >= _AI_SKIP_CONFIDENCE:
        return True
    if float(listing.get("spec_confidence") or 0) >= _AI_SKIP_CONFIDENCE:
        cascade_fields = (
            "variant",
            "storage_gb",
            "controllers_count",
            "battery_health_pct",
            "lumens",
            "frame_size",
            "cpu",
        )
        if any(listing.get(f) not in (None, "", 0, False) for f in cascade_fields):
            return True
    return False


class RuleBasedEnricher(SpecEnricher):
    """Generic enricher wrapping spec_profile extractors."""

    def __init__(self, category: str, extractor) -> None:
        self.category = category
        self._extractor = extractor

    def extract_specs(self, listing: Dict[str, Any]) -> Dict[str, Any]:
        raw = self._extractor(listing)
        merged = _extract_raw_specs(listing, self.category)
        for k, v in merged.items():
            if v not in (None, "", False) and k not in raw:
                raw[k] = v
        return _wrap_rules(raw)


class ConsolePS4EnricherProxy(SpecEnricher):
    """Delegates to console_enrichment.ConsolePS4Enricher (PS4 quality preserved)."""

    def __init__(self, category: str = "console_ps4") -> None:
        from .scoring import ConsolePS4Enricher

        self.category = category
        self._inner = ConsolePS4Enricher(category=category)

    def extract_specs(self, listing: Dict[str, Any]) -> Dict[str, Any]:
        return self._inner.extract_specs(listing)

    def _ai_fill_missing(self, listings: List[Dict[str, Any]]) -> None:
        self._inner._ai_fill_missing(listings)

    def _merge_ai(self, listing: Dict[str, Any], patch: Dict[str, Any]) -> None:
        self._inner._merge_ai(listing, patch)


_ENRICHERS: Dict[str, SpecEnricher] = {}


def _register_defaults() -> None:
    if _ENRICHERS:
        return
    _ENRICHERS["console_ps4"] = ConsolePS4EnricherProxy("console_ps4")
    _ENRICHERS["console_ps5"] = ConsolePS4EnricherProxy("console_ps5")
    _ENRICHERS["phone"] = RuleBasedEnricher("phone", extract_phone_specs)
    _ENRICHERS["bicycle"] = RuleBasedEnricher("bicycle", extract_bicycle_specs)
    _ENRICHERS["projector"] = RuleBasedEnricher("projector", extract_projector_specs)
    _ENRICHERS["apparel"] = RuleBasedEnricher("apparel", extract_apparel_specs)
    _ENRICHERS["laptop_pc"] = RuleBasedEnricher("laptop_pc", extract_laptop_specs)
    _ENRICHERS["ac"] = RuleBasedEnricher("ac", extract_ac_specs)
    _ENRICHERS["generic"] = RuleBasedEnricher("generic", extract_generic_specs)


def get_enricher(category: str) -> Optional[SpecEnricher]:
    _register_defaults()
    cat = normalize_category(category)
    return _ENRICHERS.get(cat) or _ENRICHERS.get("generic")


def enrich_listing_specs(listing: Dict[str, Any], category: str, *, use_ai: bool = False) -> Dict[str, Any]:
    """Enrich a single listing; returns mutated listing."""
    enricher = get_enricher(category)
    if not enricher:
        return listing
    return enricher.apply_to_listing(listing, use_ai=use_ai)


def enrich_batch(
    listings: List[Dict[str, Any]],
    category: str,
    *,
    use_ai: bool = True,
    workers: int = _RULE_WORKERS,
) -> int:
    """
    Enrich listings: parallel rules pass, then LLM batches (20/lot) for gaps.
    Skips listings already enriched from cascade with confidence >= 0.8.
    """
    enricher = get_enricher(category)
    if not enricher or not listings:
        return 0

    to_process = [r for r in listings if not _should_skip_reenrich(r)]

    def _rules_one(row: Dict[str, Any]) -> None:
        enricher.apply_to_listing(row, use_ai=False)

    if workers > 1 and len(to_process) > 4:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_rules_one, r) for r in to_process]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    logger.debug("rules enrich: %s", exc)
    else:
        for row in to_process:
            _rules_one(row)

    if use_ai:
        ai_targets = [r for r in to_process if float(r.get("spec_confidence") or 0) < _AI_SKIP_CONFIDENCE]
        if ai_targets:
            enricher._ai_fill_missing(ai_targets)

    return len(to_process)


def enrich_listings_specs(
    listings: List[Dict[str, Any]],
    category: str,
    *,
    use_ai: bool = True,
    limit: int = 300,
) -> int:
    """Backward-compatible batch entry (pipeline / tests)."""
    batch = listings[:limit] if limit > 0 else listings
    return enrich_batch(batch, category, use_ai=use_ai)


def clear_spec_cache() -> None:
    _SPEC_CACHE.clear()


def field_schema(category: str) -> str:
    return field_schema_for_category(category)


# --- console_enrichment.py ---

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# Premium games — pattern -> score boost when included in lot
PREMIUM_GAMES: Dict[str, Tuple[str, float]] = {
    "fifa": (r"(?i)\bfifa\s*\d{2}\b|\bfifa\b", 8.0),
    "gta": (r"(?i)\bgta\s*v?\b|grand\s*theft\s*auto", 10.0),
    "god_of_war": (r"(?i)\bgod\s*of\s*war\b", 9.0),
    "last_of_us": (r"(?i)\blast\s*of\s*us\b|the\s*last\s*of\s*us", 9.0),
    "spider_man": (r"(?i)\bspider[\s-]*man\b", 8.0),
    "horizon": (r"(?i)\bhorizon\s*(zero\s*dawn|forbidden\s*west)?\b", 8.0),
    "uncharted": (r"(?i)\buncharted\b", 7.0),
    "ghost_of_tsushima": (r"(?i)\bghost\s*of\s*tsushima\b", 8.0),
    "ratchet": (r"(?i)\bratchet\s*(et|&)\s*clank\b", 6.0),
    "bloodborne": (r"(?i)\bbloodborne\b", 7.0),
    "cod": (r"(?i)\bcall\s*of\s*duty\b|\bcod\s*(mw|bo)?\b", 6.0),
    "red_dead": (r"(?i)\bred\s*dead\s*redemption\b", 8.0),
    "fc24": (r"(?i)\bfc\s*2[456]\b|\bea\s*sports\s*fc\b", 8.0),
}

_PS4_MODEL_RE = re.compile(
    r"(?i)\b(ps4|play\s*station\s*4|playstation\s*4|play\s*4|ps\s*4)\s*(slim|pro|fat)?\b"
)
_PS4_STORAGE_RE = re.compile(r"(?i)\b(500\s*go|500\s*gb|1\s*to|1\s*tb|1000\s*go|1\s*000\s*go)\b")
_PS4_CONTROLLER_RE = re.compile(
    r"(?i)\b(\d+)\s*manettes?|2\s*manettes?|avec\s*manette|manette\s*incluse|"
    r"dual\s*shock|dualshock|sans\s*manette|console\s*seule|x\s*manette\b"
)
_PS4_GAMES_COUNT_RE = re.compile(
    r"(?i)\b(lot\s+de\s+(\d+)\s+jeux|(\d+)\s+jeux\s+inclus|(\d+)\s+jeux|jeux\s+inclus)\b"
)
_CONDITION_GOOD = re.compile(r"(?i)\b(tres\s+bon|très\s+bon|excellent|nickel|bon\s+etat|bon\s+état)\b")
_CONDITION_OK = re.compile(r"(?i)\b(bon|correct|propre)\b")
_CONDITION_BAD = re.compile(r"(?i)\b(defaut|défaut|rayure|usure|hs|pour\s+pieces)\b")
_GAME_TITLE_RE = re.compile(
    r"(?i)\b(fifa|gta|god\s*of\s*war|last\s*of\s*us|spider[\s-]*man|horizon|uncharted|"
    r"ghost|ratchet|bloodborne|call\s*of\s*duty|red\s*dead|fc\s*2\d|minecraft|fortnite|"
    r"assassin|far\s*cry|battlefield|nba|pes|tekken|mortal\s*kombat)\b"
)
_BATCH_SIZE = 20


def _field(value: Any, confidence: float = 0.75, source: str = "rules") -> Dict[str, Any]:
    return {"value": value, "confidence": round(confidence, 3), "source": source}


def _listing_text(listing: Dict[str, Any]) -> str:
    return f"{listing.get('title') or ''} {listing.get('description') or ''}".strip()


def detect_premium_games(text: str) -> List[str]:
    found: List[str] = []
    for key, (pat, _) in PREMIUM_GAMES.items():
        if re.search(pat, text):
            found.append(key)
    return found


def premium_games_boost(game_keys: List[str]) -> float:
    total = 0.0
    for k in game_keys:
        entry = PREMIUM_GAMES.get(k)
        if entry:
            total += entry[1]
    return min(35.0, total)


def extract_games_list(text: str) -> List[str]:
    games: List[str] = []
    for m in _GAME_TITLE_RE.finditer(text):
        g = m.group(0).strip()
        if g and g.lower() not in {x.lower() for x in games}:
            games.append(g)
    return games[:20]


def extract_console_ps4_specs(listing: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based PS4 specs from title + description."""
    text = _listing_text(listing)
    specs: Dict[str, Any] = {}

    # Model variant
    variant = str(listing.get("variant") or "").lower()
    m = _PS4_MODEL_RE.search(text)
    if m:
        mod = (m.group(2) or "").lower()
        if mod in ("slim", "pro"):
            variant = mod
        elif re.search(r"(?i)\bfat\b|classique|originale|standard", text):
            variant = "standard"
        elif not variant:
            variant = "standard"
    if variant:
        conf = 0.92 if m else 0.55
        specs["variant"] = _field(variant, conf)
        specs["model"] = _field(variant, conf)

    # Storage
    storage = int(listing.get("storage_gb") or 0)
    sm = _PS4_STORAGE_RE.search(text)
    if sm and not storage:
        tok = sm.group(0).lower().replace(" ", "")
        storage = 1000 if any(x in tok for x in ("1to", "1tb", "1000")) else 500
    if storage:
        specs["storage_gb"] = _field(storage, 0.88 if sm else 0.6)

    # Controllers
    ctrl_count: Optional[int] = None
    if listing.get("controller_count") is not None:
        try:
            ctrl_count = int(listing["controller_count"])
        except (TypeError, ValueError):
            pass
    cm = _PS4_CONTROLLER_RE.search(text)
    if cm:
        blob = cm.group(0).lower()
        if "sans manette" in blob or "console seule" in blob:
            ctrl_count = 0
        elif cm.group(1) and cm.group(1).isdigit():
            ctrl_count = int(cm.group(1))
        elif "2 manette" in blob or blob.startswith("2"):
            ctrl_count = 2
        else:
            ctrl_count = 1 if ctrl_count is None else ctrl_count
    if ctrl_count is not None:
        specs["controllers_count"] = _field(ctrl_count, 0.85 if cm else 0.55)
        specs["has_controller"] = _field(ctrl_count > 0, 0.85 if cm else 0.55)

    # Games
    games_count = int(listing.get("games_count") or 0)
    gm = _PS4_GAMES_COUNT_RE.search(text)
    if gm:
        for g in gm.groups():
            if g and str(g).isdigit():
                games_count = max(games_count, int(g))
                break
    games_list = extract_games_list(text)
    if not games_count and games_list:
        games_count = len(games_list)
    if games_count or games_list:
        if games_count:
            specs["games_count"] = _field(games_count, 0.8 if gm else 0.65)
        if games_list:
            specs["games_list"] = _field(games_list, 0.7)
        specs["includes_games"] = _field(bool(games_count or games_list), 0.75)
    premium = detect_premium_games(text)
    if premium:
        specs["premium_games"] = _field(premium, 0.82)

    # Completeness
    has_hdmi = bool(listing.get("has_hdmi")) or bool(re.search(r"(?i)\bhdmi\b", text))
    has_power = bool(listing.get("has_power_cable")) or bool(
        re.search(r"(?i)\b(alimentation|chargeur|câble\s*alim)\b", text)
    )
    has_box = bool(listing.get("has_box")) or bool(re.search(r"(?i)\b(bo[iî]te|complet)\b", text))
    has_headset = bool(re.search(r"(?i)\b(casque|headset|micro)\b", text))
    if has_hdmi:
        specs["has_hdmi"] = _field(True, 0.8)
    if has_power:
        specs["has_power_cable"] = _field(True, 0.78)
    if has_box:
        specs["has_box"] = _field(True, 0.75)
    if has_headset:
        specs["has_headset"] = _field(True, 0.7)

    # Condition
    cond = str(listing.get("condition_hint") or listing.get("condition") or "")
    if re.search(r"(?i)\bneuf\b", text):
        cond = "neuf"
    elif _CONDITION_GOOD.search(text):
        cond = "tres bon"
    elif _CONDITION_BAD.search(text):
        cond = "defauts"
    elif _CONDITION_OK.search(text) and not cond:
        cond = "bon"
    if cond:
        specs["condition"] = _field(cond, 0.72)

    # Listing type
    if re.search(r"(?i)\bjeu(x)?\s+seul|lot\s+de\s+\d+\s+jeux\b", text) and not re.search(
        r"(?i)\bconsole\b", text
    ):
        specs["listing_type"] = _field("games_only", 0.85)
    elif re.search(r"(?i)\bconsole\b", text) or _PS4_MODEL_RE.search(text):
        specs["listing_type"] = _field("console", 0.8)

    return specs


def enrich_console_specs_batch(
    listings: List[Dict[str, Any]],
    *,
    use_ai: bool = True,
    batch_size: int = _BATCH_SIZE,
) -> None:
    """Enrich listings: rules first, then 1 LLM call per batch for gaps."""
    enricher = ConsolePS4Enricher()
    for listing in listings:
        enricher.apply_to_listing(listing, use_ai=False)
    if not use_ai:
        return
    needs_ai = [r for r in listings if enricher._needs_ai(r)]
    for off in range(0, len(needs_ai), batch_size):
        chunk = needs_ai[off : off + batch_size]
        try:
            from .ai import batch_enrich_console_specs

            ai_map = batch_enrich_console_specs(chunk)
            for listing in chunk:
                lid = str(listing.get("id") or "")
                patch = ai_map.get(lid) or {}
                if patch:
                    enricher._merge_ai(listing, patch)
        except Exception:
            pass


class ConsolePS4Enricher(SpecEnricher):
    category = "console_ps4"

    def __init__(self, category: str = "console_ps4") -> None:
        self.category = category

    def extract_specs(self, listing: Dict[str, Any]) -> Dict[str, Any]:
        return extract_console_ps4_specs(listing)

    def _ai_fill_missing(self, listings: List[Dict[str, Any]]) -> None:
        enrich_console_specs_batch(listings, use_ai=True)

    def _needs_ai(self, listing: Dict[str, Any]) -> bool:
        specs = listing.get("enriched_specs") or {}
        critical = ("variant", "storage_gb", "controllers_count", "games_count")
        missing = sum(
            1
            for f in critical
            if not isinstance(specs.get(f), dict) or specs[f].get("value") in (None, "", 0)
        )
        text = _listing_text(listing)
        return missing >= 2 or (len(text) > 80 and missing >= 1)

    def _merge_ai(self, listing: Dict[str, Any], patch: Dict[str, Any]) -> None:
        specs = dict(listing.get("enriched_specs") or {})
        for field, val in patch.items():
            if field.startswith("_") or val in (None, ""):
                continue
            try:
                conf = float(patch.get(f"_conf_{field}") or 0.68)
            except (TypeError, ValueError):
                conf = 0.68
            specs[field] = {"value": val, "confidence": conf, "source": "ai"}
        listing["enriched_specs"] = specs
        listing["enriched_by"] = self._dominant_source(specs)
        self._flatten_specs(listing, specs)
        listing["spec_confidence"] = self._aggregate_confidence(specs)

