"""Free tiers of the cascade: hard rules, completeness-adjusted value, landed cost, profit and score.

The score is a sum of named parts (kept in `Deal.score_parts`) so the bot can explain
every number it shows.
"""

from __future__ import annotations

import math

from .config import RuntimeSettings
from .criteria import mismatch
from .inspection import apply_inspection, assess_seller, inspect_listing
from .models import AIVerdict, Condition, Deal, Finding, Inspection, Listing, Recommendation, SearchSpec, SellerCheck, Valuation
from .niches import Niche, normalize

# Without a photo check, scores in fake-prone niches are capped below/near the alert threshold.
UNVERIFIED_CAP = {"very_high": 55, "high": 75}


def reject_reason(listing: Listing, niche: Niche | None, spec: SearchSpec | None = None) -> str | None:
    """Tier 0: free, deterministic rejection. Returns a reason or None if the listing survives."""
    title = normalize(listing.title)
    if niche and (pattern := niche.excluded_by(title)):
        return f"exclu ({pattern})"
    price_min = (spec and spec.price_min) or (niche and niche.price_min)
    price_max = (spec and spec.price_max) or (niche and niche.price_max)
    if price_min and listing.price < price_min:
        return "prix sous le minimum"
    if price_max and listing.price > price_max:
        return "prix au-dessus du budget"
    if spec and spec.conditions and listing.condition not in spec.conditions:
        return "état non souhaité"
    if spec and spec.size_hint and listing.size:
        wanted = {s for s in normalize(spec.size_hint).replace("/", " ").split() if s}
        if wanted and not wanted & set(normalize(listing.size).replace("/", " ").split()):
            return "taille différente"
    if listing.seller.on_holiday:
        return "vendeur en vacances"
    return mismatch(listing, spec)


def landed_cost(listing: Listing, niche: Niche | None, rs: RuntimeSettings) -> float:
    """What you actually pay: price + Vinted buyer protection + shipping."""
    protection = listing.price * rs.protection_pct / 100 + rs.protection_fixed_eur
    shipping = niche.shipping_eur if niche else 5.0
    return round(listing.price + protection + shipping, 2)


def adjust_valuation(listing: Listing, valuation: Valuation, niche: Niche | None, insp: Inspection,
                     verdict: AIVerdict | None) -> Valuation:
    """Complete-item value -> capped at retail (non-collectibles) -> minus missing parts and defects."""
    if not valuation.fair_value:
        return valuation
    v = valuation
    if v.new_price and niche and not niche.collectible:
        cap = v.new_price * 0.9
        if v.fair_value > cap:
            ratio = cap / v.fair_value
            v = v.model_copy(update={"fair_value": round(cap, 2), "low": round((v.low or cap) * ratio, 2),
                                     "high": round((v.high or cap) * ratio, 2)})
    v = v.model_copy(update={"base_value": v.fair_value})
    adjusted = apply_inspection(v, insp)
    if verdict and verdict.adjust_pct is not None and verdict.adjust_pct < 0:
        # The AI read the full text and photos: keep the stricter of both estimates.
        ai_factor = max(0.2, 1 + verdict.adjust_pct / 100)
        if ai_factor < insp.value_factor:
            adjusted = v.model_copy(update={k: round(getattr(v, k) * ai_factor, 2) for k in ("fair_value", "low", "high")})
    return adjusted


def flags_for(listing: Listing, valuation: Valuation, niche: Niche | None, insp: Inspection,
              verdict: AIVerdict | None) -> list[str]:
    flags = []
    if valuation.fair_value and listing.price < valuation.fair_value * 0.3:
        flags.append("prix_suspect")
    if insp.missing or (verdict and verdict.missing_parts):
        flags.append("incomplet")
    if niche and niche.authenticity_risk in UNVERIFIED_CAP and not (verdict and verdict.tier >= 3):
        flags.append("auth_non_verifiee")
    return flags


def score_parts(listing: Listing, valuation: Valuation, profit_eur: float | None, margin: float | None,
                flags: list[str], verdict: AIVerdict | None, seller: SellerCheck, insp: Inspection,
                target_profit: float) -> dict[str, float]:
    if profit_eur is None or margin is None:
        return {"cote inconnue": 20.0}
    p: dict[str, float] = {
        "profit": 38 * (1 - math.exp(-max(profit_eur, 0) / max(target_profit, 1))),
        "marge": 22 * min(1.0, max(margin, 0) / 100),
        "confiance cote": 15 * valuation.confidence,
        "vendeur": 10 * seller.score,
    }
    if listing.age_minutes is not None:
        p["fraîcheur"] = 8 * math.exp(-listing.age_minutes / 90)
    demand = listing.favourites / max(listing.views, 20) if listing.views else min(listing.favourites, 10) / 10
    p["demande"] = 7 * min(1.0, demand * 4)
    if valuation.days_to_sell is not None and valuation.sold_count >= 3:
        # From the sales database: a model that sells in 2 days is worth more to a reseller than one that sits.
        d2s = valuation.days_to_sell
        p["liquidité"] = 6 if d2s <= 3 else 4 if d2s <= 7 else 2 if d2s <= 14 else -4 if d2s > 30 else 0
    if "prix_suspect" in flags:
        p["prix suspect"] = -18
    if insp.scams:
        p["arnaque probable"] = -30
    if insp.missing:
        p["pièces manquantes"] = -3 * len(insp.missing)   # value already reduced; this is for uncertainty
    if insp.verification:
        # Vinted can check the item (authenticity / electronics) before it reaches you: fakes become refundable.
        p["vérification Vinted dispo"] = 5
    if insp.missing_specs and not (verdict and verdict.specs):
        p["specs non précisées"] = -3 * len(insp.missing_specs)
    if verdict and verdict.specs_consistent is False:
        p["specs incohérentes (IA)"] = -12
    if verdict:
        p["risque fake (IA)"] = -35 * verdict.authenticity_risk
        if not verdict.relevant:
            p["hors-sujet (IA)"] = -60
        if verdict.condition_score is not None:
            p["état photos (IA)"] = (verdict.condition_score - 6) * 1.5
        if verdict.red_flags:
            p["alertes IA"] = -2 * len(verdict.red_flags)
        if verdict.recommendation is Recommendation.BUY:
            p["verdict IA"] = 8
        elif verdict.recommendation is Recommendation.SKIP:
            p["verdict IA"] = -25
    return {k: round(v, 1) for k, v in p.items()}


def total_score(parts: dict[str, float], profit_eur: float | None, flags: list[str], niche: Niche | None) -> int:
    s = sum(parts.values())
    if profit_eur is not None and profit_eur <= 0:
        s = min(s, 25)
    if "auth_non_verifiee" in flags and niche:
        s = min(s, UNVERIFIED_CAP[niche.authenticity_risk])
    return max(0, min(100, round(s)))


_SCAM_WORDS = ("virement", "paypal", "whatsapp", "lydia", "hors vinted", "en dehors de vinted", "western union",
               "numéro de téléphone", "arnaque")


def ai_scam_reason(verdict: AIVerdict | None) -> str | None:
    """The AI read the full listing: a payment/contact outside Vinted or a doubtful seller is a scam signal."""
    if not verdict:
        return None
    for flag in verdict.red_flags:
        if any(w in flag.lower() for w in _SCAM_WORDS):
            return f"IA : {flag}"
    if verdict.seller_ok is False:
        return "IA : vendeur jugé douteux"
    return None


def build_deal(listing: Listing, valuation: Valuation, niche: Niche | None, rs: RuntimeSettings,
               verdict: AIVerdict | None = None, spec: SearchSpec | None = None) -> Deal:
    insp = inspect_listing(listing, niche)
    if (reason := ai_scam_reason(verdict)) and reason not in insp.scams:
        insp.findings.append(Finding(kind="scam", label=reason))
    valuation = adjust_valuation(listing, valuation, niche, insp, verdict)
    cost = landed_cost(listing, niche, rs)
    profit_eur = margin = None
    if valuation.fair_value:
        fee = (niche.resale_fee_pct if niche else 0.0) / 100
        profit_eur = round(valuation.fair_value * (1 - fee) - cost, 2)
        margin = round(profit_eur / cost * 100, 1)
    seller = assess_seller(listing, insp, valuation)
    flags = flags_for(listing, valuation, niche, insp, verdict)
    target = (spec and spec.min_profit) or (niche and niche.min_profit) or rs.min_profit_eur
    parts = score_parts(listing, valuation, profit_eur, margin, flags, verdict, seller, insp, target)
    if profit_eur is not None:
        parts.update(quality_parts(listing, niche, insp))
    return Deal(
        listing=listing, valuation=valuation, landed_cost=cost, profit=profit_eur, margin_pct=margin,
        score=total_score(parts, profit_eur, flags, niche), flags=flags, verdict=verdict,
        inspection=insp, seller_check=seller, score_parts=parts,
    )


def reference_of(deal: Deal, niche: Niche | None):
    key = deal.listing.model_key or ""
    if not niche or not key.startswith(f"{niche.id}:"):
        return None
    return next((m for m in niche.models if m.key == key.split(":", 1)[1]), None)


def gate_fails(deal: Deal, niche: Niche | None, rs: RuntimeSettings, spec: SearchSpec | None = None,
               *, require_market: bool = True) -> list[str]:
    """The "insane deals only" filter. Returns every reason a deal is NOT a pépite (empty list = pépite)."""
    fails: list[str] = []
    l, v, vr = deal.listing, deal.valuation, deal.verdict
    ref = reference_of(deal, niche)
    ai_identified = bool(vr and vr.tier >= 3 and vr.relevant and vr.model_name)
    if not (ref and not ref.generic) and not ai_identified:
        fails.append("modèle pas identifié précisément")
    if not l.details_loaded:
        fails.append("description pas encore lue")
    major = [f.label for f in (deal.inspection.defects if deal.inspection else [])
             if f.impact >= 0.3 or f.label.startswith("vendu en l'état")]
    if major and not (spec and spec.goal == "personal"):
        # A broken / locked / "sold as is" item is a repair gamble, never a pépite.
        fails.append(f"défaut majeur : {major[0]}")
    if not v.fair_value:
        return fails + ["cote inconnue"]
    discount = (1 - l.price / v.fair_value) * 100
    if l.price < v.fair_value * 0.3:
        fails.append("prix < 30 % de la cote : autre produit ou arnaque")
    if discount < rs.min_discount_pct:
        fails.append(f"remise {discount:.0f} % < {rs.min_discount_pct:.0f} %")
    min_profit = max((spec and spec.min_profit) or 0, (niche and niche.min_profit) or 0, rs.min_profit_eur)
    if (deal.profit or 0) < min_profit:
        fails.append(f"profit {deal.profit or 0:.0f} € < {min_profit:.0f} €")
    if (deal.margin_pct or 0) < rs.min_margin_pct:
        fails.append(f"marge {deal.margin_pct or 0:.0f} % < {rs.min_margin_pct:.0f} %")
    if v.confidence < rs.min_confidence:
        fails.append(f"confiance {v.confidence * 100:.0f} % < {rs.min_confidence * 100:.0f} %")
    sc, insp = deal.seller_check, deal.inspection
    if (sc and sc.level == "risque") or (insp and insp.scams):
        fails.append("vendeur risqué")
    if vr and (not vr.relevant or vr.recommendation is Recommendation.SKIP or vr.authenticity_risk >= 0.5):
        fails.append("rejeté par l'IA")
    if insp and insp.missing_specs and not (vr and vr.specs):
        fails.append("specs à vérifier : " + ", ".join(insp.missing_specs) + " non précisé(s)")
    if vr and vr.specs_consistent is False:
        fails.append("specs incohérentes entre titre, description et photos")
    if niche and not (vr and vr.tier >= 3):
        if niche.authenticity_risk == "very_high":
            fails.append("niche à contrefaçons : vérification photo IA obligatoire")
        elif (niche.authenticity_risk == "high" and not (insp and insp.verification)
              and (not sc or sc.level != "ok" or l.price < v.fair_value * 0.45)):
            fails.append("niche à contrefaçons : vendeur 🟢 et prix ≥ 45 % de la cote exigés sans IA")
    if require_market:
        mc = deal.market_check
        if mc is None:
            fails.append("marché pas encore vérifié")
        elif mc.n < 5:
            fails.append(f"seulement {mc.n} annonces identiques : impossible de confirmer")
        else:
            if mc.cheaper > 1:
                fails.append(f"{mc.cheaper} annonces identiques moins chères")
            if mc.median and l.price > mc.median * 0.75:
                fails.append(f"seulement {(1 - l.price / mc.median) * 100:.0f} % sous la médiane du marché")
    if deal.wardrobe and deal.wardrobe.suspect:
        fails.append("dressing du vendeur suspect : " + ", ".join(deal.wardrobe.flags[:2]))
    if rs.quality_mode and not (spec and spec.goal == "personal"):
        fails += quality_fails(deal, niche)
    if deal.score < rs.min_score:
        fails.append(f"score {deal.score} < {rs.min_score}")
    return fails


NEW_CONDITIONS = (Condition.NEW_WITH_TAGS, Condition.NEW)


def quality_fails(deal: Deal, niche: Niche | None) -> list[str]:
    """Quality mode: new items of a known brand model, low fake risk, established seller. Fewer alerts, each
    one easy to resell fast (new sells 2-3x faster than used) and hard to get scammed on."""
    l, insp = deal.listing, deal.inspection
    fails = []
    if l.condition not in NEW_CONDITIONS:
        fails.append(f"qualité : état « {l.condition.label} », neuf exigé")
    risk = niche.authenticity_risk if niche else "medium"
    verified = bool(insp and insp.verification)
    if risk == "very_high" and not verified:
        fails.append("qualité : niche à contrefaçons sans vérification Vinted")
    elif risk == "high" and not verified and not (deal.verdict and deal.verdict.tier >= 3):
        fails.append("qualité : risque de contrefaçon, vérification Vinted ou photos IA exigées")
    s = l.seller
    if s.reviews is not None and (s.reviews < 5 or (s.rating is not None and s.rating < 4.5)):
        fails.append(f"qualité : vendeur peu établi ({s.reviews} avis)")
    return fails


def quality_parts(listing: Listing, niche: Niche | None, insp: Inspection) -> dict[str, float]:
    """Score weight for what makes a deal safe and fast to flip, whatever the mode."""
    p: dict[str, float] = {}
    if listing.condition is Condition.NEW_WITH_TAGS:
        p["neuf avec étiquette"] = 7
    elif listing.condition is Condition.NEW:
        p["neuf"] = 5
    elif listing.condition in (Condition.GOOD, Condition.SATISFACTORY):
        p["état moyen"] = -4
    risk = niche.authenticity_risk if niche else "medium"
    if not insp.verification:
        p["risque contrefaçon"] = {"low": 4, "medium": 0, "high": -5, "very_high": -10}.get(risk, 0)
    s = listing.seller
    if s.reviews is not None and s.reviews >= 20 and (s.rating or 0) >= 4.8:
        p["vendeur établi"] = 3
    return {k: v for k, v in p.items() if v}


def radar_worthy(deal: Deal) -> bool:
    """Good enough for the Top/radar list: real money, identified model, nothing alarming."""
    key = deal.listing.model_key or "sig:"
    return (
        deal.valuation.fair_value is not None and (deal.profit or 0) >= 10 and deal.score >= 45
        and "prix_suspect" not in deal.flags and not key.startswith("sig:")
        and not (deal.seller_check and deal.seller_check.level == "risque")
        and not (deal.inspection and deal.inspection.scams)
        and not (deal.verdict and (not deal.verdict.relevant or deal.verdict.recommendation is Recommendation.SKIP))
    )


def is_alert_worthy(deal: Deal, rs: RuntimeSettings, niche: Niche | None, spec: SearchSpec | None = None) -> bool:
    if spec and spec.goal == "personal":
        # Shopping for yourself: every listing that fits the criteria and the budget, from a safe seller.
        if mismatch(deal.listing, spec) or (deal.inspection and deal.inspection.scams):
            return False
        if deal.seller_check and deal.seller_check.level == "risque":
            return False
        if deal.verdict and not deal.verdict.relevant:
            return False
        return deal.valuation.fair_value is None or deal.listing.price <= deal.valuation.fair_value * 1.1
    if rs.alert_mode == "pepites":
        return not gate_fails(deal, niche, rs, spec)
    min_profit = (spec and spec.min_profit) or (niche and niche.min_profit) or rs.min_profit_eur
    if deal.profit is None or deal.profit < min_profit:
        return False
    if "prix_suspect" in deal.flags:
        return False
    if (deal.margin_pct or 0) < rs.min_margin_pct:
        return False
    if deal.verdict and (not deal.verdict.relevant or deal.verdict.recommendation is Recommendation.SKIP):
        return False
    if deal.seller_check and deal.seller_check.level == "risque" and deal.inspection and deal.inspection.scams:
        return False
    return deal.score >= rs.min_score
