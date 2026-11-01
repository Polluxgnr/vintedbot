"""Read the whole listing like a careful buyer would (free, no AI).

- missing parts  "j'ai décidé de ne pas vendre les manettes" -> manettes absentes, value -22 %
- defects        "écran fissuré", "batterie à changer", "drift" (negation-aware: "aucune rayure" is not a defect)
- positives      facture, testé, complet, scellé, négociable...
- scam signals   WhatsApp, payment outside Vinted, phone number
- seller trust   reviews, rating, activity, plus the signals above

Descriptions are split into clauses so a negation never leaks across a comma
("sans rayure, manettes incluses" keeps the controllers).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import Finding, Inspection, Listing, SellerCheck, Valuation
from .niches import Niche, normalize

_CLAUSE_SPLIT = re.compile(r"[.!?;:\n,()\[\]/]|\s[-–•]\s|\s\+\s")
_NEG = r"(?:sans|pas d[e]?|pas les?|pas la|pas l|aucune?|manque(?:nt)?|manquante?s?|ni|ne(?: \w+){0,3}? pas|n(?: \w+){0,2}? pas|non)"
_STOP = r"(?!avec\b|et\b|mais\b|plus\b|inclus\w*\b|fourni\w*\b)"
_POST = (r"(?:(?:non|pas|jamais) (?:inclus\w*|fourni\w*|vendu\w*|compris\w*|livre\w*|present\w*|dispo\w*)"
         r"|absente?s?|manquante?s?|perdue?s?|a part|en option|vendue?s? separement|pas avec)")
_NEG_BEFORE = re.compile(r"\b(?:sans|pas|aucune?|ni|jamais|zero|0|non|nul(?:le)?)\b(?: \w+){0,2}$")

# (group, regex, label, value impact)
DEFECTS = [
    ("broken", r"ne(?: \w+)? (?:s allume|charge|fonctionne|marche|lit|demarre)\w* (?:plus|pas)|en panne|hors service", "ne fonctionne pas", 0.6),
    ("lock", r"bloque\w* icloud|icloud (?:\w+ )?(?:actif|bloque|verrouille)|compte google (?:actif|bloque)", "verrouillage compte", 0.8),
    ("screen", r"ecran (?:\w+ )?(?:fissure|casse|brise|felure|fele)\w*|(?:vitre|dalle) (?:fissuree|cassee)", "écran fissuré", 0.35),
    ("crack", r"(?:fissur|felur|cassure)\w*|\bcasse(?:e|es|s)?\b", "fissure/casse", 0.2),
    ("screen2", r"pixels? morts?|ecran (?:jaune|marque|brule)|burn in", "écran marqué", 0.12),
    ("battery", r"batterie(?: \w+){0,2} (?:faible|usee|fatiguee|a changer|hs|gonflee|tient (?:pas|plus))", "batterie usée", 0.15),
    ("drift", r"\bdrift\w*", "drift manettes", 0.08),
    ("fungus", r"champignons?|moisissures?", "champignons (optique)", 0.3),
    ("oxid", r"oxyd\w*|rouille", "oxydation", 0.15),
    ("untested", r"non teste\w*|pas teste\w*|pas pu (?:le |la )?tester|sans garantie de fonctionnement", "non testé", 0.15),
    ("scratch", r"(?:grosses?|nombreuses?|profondes?|beaucoup de) rayures?", "rayures marquées", 0.08),
    ("scratch", r"\brayures?\b|\braye(?:e|es|s)?\b", "rayures", 0.03),
    ("stain", r"\btaches?\b|\btache(?:e|es)\b", "taches", 0.08),
    ("hole", r"\btrous?\b|\btroue\w*|\baccroc\w*", "trou/accroc", 0.12),
    ("tear", r"decousu\w*|dechir\w*", "déchirure", 0.1),
    ("pilling", r"bouloch\w*", "boulochage", 0.04),
    ("wear", r"traces? d usure|\busure\b|\busee?s?\b", "usure", 0.04),
    ("yellow", r"jaunis?\w*", "jaunissement", 0.05),
]
POSITIVES = [
    (r"facture|ticket de caisse|preuve d achat", "facture"),
    (r"garantie\w*", "garantie"),
    (r"\bteste\w*|\bfonctionne (?:tres bien|parfaitement|nickel|impec\w*)|\bfonctionnel\w*|parfait etat de (?:fonctionnement|marche)", "testé/fonctionnel"),
    (r"boite d origine|\bcomplet\w*|en boite|avec (?:sa |la |boite)|tous les accessoires", "complet"),
    (r"jamais (?:porte|utilise|servi|ouvert)\w*|porte (?:une|1|deux|2) fois|tres peu (?:porte|utilise|servi)", "quasi neuf"),
    (r"sous blister|\bscelle\w*|\bsealed\b", "scellé"),
    (r"batterie (?:a )?(?:9\d|100) ?%|batterie (?:neuve|changee|remplacee)", "batterie OK"),
    (r"nettoye\w*|revise\w*|entretenu\w*|pate thermique", "entretenu"),
    (r"negociable|prix a debattre|faire (?:une )?offre|ouvert aux offres|urgent|doit partir", "négociable"),
]
SCAMS = [
    (r"whats ?app|telegram|snap(?:chat)?\b|insta(?:gram)?\b", "contact hors Vinted demandé"),
    (r"paiement(?: \w+){0,2} (?:hors|en dehors|direct)|virement|paypal|lydia|wero|paylib|western union", "paiement hors Vinted"),
    (r"\b0[67](?: ?\d{2}){4}\b|mon (?:numero|num|tel)\b", "numéro de téléphone"),
]

_DEFECTS = [(g, re.compile(p), label, impact) for g, p, label, impact in DEFECTS]
_POSITIVES = [(re.compile(p), label) for p, label in POSITIVES]
_SCAMS = [(re.compile(p), label) for p, label in SCAMS]


SPEC_PATTERNS = {
    "storage": r"\b(\d{2,4}) ?(?:go|gb|gigas?)\b(?! ?(?:de )?ram)|\b([124]) ?(?:to|tb)\b",
    "ram": r"\b(\d{1,3}) ?(?:go|gb) (?:de )?ram\b|\bram (\d{1,3}) ?(?:go|gb)\b",
    "cpu": r"\b(i[3579][ -]?\d{4,5}[a-z]{0,2}|ryzen [3579] \d{4}[a-z]{0,2}|m[1-4](?: pro| max| ultra)?|n100|n95|celeron \w+)\b",
    "battery": r"batterie (?:a |à )?(\d{2,3}) ?%|(\d{2,3}) ?% (?:de )?(?:batterie|capacite)",
}
SPEC_LABELS = {"storage": "stockage", "ram": "RAM", "cpu": "processeur", "battery": "santé batterie"}


def extract_specs(text: str) -> dict[str, str]:
    norm = normalize(text).replace("%", " %")
    raw = (text or "").lower()
    out: dict[str, str] = {}
    for key, pattern in SPEC_PATTERNS.items():
        m = re.search(pattern, raw if key == "battery" else norm)
        if not m:
            continue
        val = next(g for g in m.groups() if g)
        if key == "storage":
            out[key] = f"{val} To" if m.group(2) else f"{val} Go"
        elif key == "ram":
            out[key] = f"{val} Go"
        elif key == "battery":
            out[key] = f"{val} %"
        else:
            out[key] = val.upper()
    return out


def clauses(text: str) -> list[str]:
    return [c for c in (normalize(part) for part in _CLAUSE_SPLIT.split((text or "").lower())) if c]


def _negated(clause: str, start: int) -> bool:
    return bool(_NEG_BEFORE.search(clause[:start].rstrip()))


def find_missing(text: str, niche: Niche | None) -> list[Finding]:
    if not niche or not niche.components:
        return []
    found: dict[str, Finding] = {}
    parts = clauses(text)
    for comp in niche.components:
        before = re.compile(rf"\b{_NEG}\b(?: {_STOP}\w+){{0,4}}? (?:{comp.match})\b")
        after = re.compile(rf"\b(?:{comp.match})\b(?: \w+){{0,3}}? {_POST}\b")
        if any(before.search(c) or after.search(c) for c in parts):
            found[comp.name] = Finding(kind="missing", label=f"{comp.name} absent(e)s", impact=comp.share)
    norm = normalize(text)
    by_name = {c.name: c for c in niche.components}
    for rule in niche.alone:
        if re.search(rf"\b(?:{rule.match})", norm):
            for name in rule.missing:
                if name in by_name and name not in found:
                    found[name] = Finding(kind="missing", label=f"{name} absent(e)s", impact=by_name[name].share)
    return list(found.values())


def inspect_listing(listing: Listing, niche: Niche | None) -> Inspection:
    text = f"{listing.title}. {listing.description or ''}"
    findings = find_missing(text, niche)

    worst: dict[str, Finding] = {}
    seen_pos: set[str] = set()
    scams: set[str] = set()
    for clause in clauses(text):
        for group, rx, label, impact in _DEFECTS:
            m = rx.search(clause)
            if m and not _negated(clause, m.start()) and (group not in worst or worst[group].impact < impact):
                worst[group] = Finding(kind="defect", label=label, impact=impact)
        for rx, label in _POSITIVES:
            m = rx.search(clause)
            if m and not _negated(clause, m.start()):
                seen_pos.add(label)
        for rx, label in _SCAMS:
            if rx.search(clause):
                scams.add(label)
    # "complet" claims contradict detected missing parts
    if findings:
        seen_pos.discard("complet")
    findings += list(worst.values())
    findings += [Finding(kind="positive", label=p) for p in sorted(seen_pos)]
    findings += [Finding(kind="scam", label=s) for s in sorted(scams)]
    specs = extract_specs(text)
    needed = list(niche.required_specs) if niche else []
    if niche and niche.id in ("apple_tech", "smartphones"):
        needed = ["storage"] if re.search(r"iphone|ipad|galaxy|pixel|xiaomi|oneplus|nothing|macbook",
                                          normalize(listing.title)) else []
        if re.search(r"iphone|galaxy|pixel", normalize(listing.title)):
            needed.append("battery")
    verification = None
    if niche and niche.vinted_verification and listing.price >= niche.verification_min_price:
        verification = niche.vinted_verification
    return Inspection(findings=findings, specs=specs, missing_specs=[k for k in needed if k not in specs],
                      verification=verification)


def apply_inspection(valuation: Valuation, insp: Inspection) -> Valuation:
    if not valuation.fair_value or insp.value_factor >= 0.999:
        return valuation
    f = insp.value_factor
    return valuation.model_copy(update={
        "fair_value": round(valuation.fair_value * f, 2),
        "low": round((valuation.low or valuation.fair_value) * f, 2),
        "high": round((valuation.high or valuation.fair_value) * f, 2),
    })


def _ago(dt: datetime | None) -> float | None:
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400 if dt else None


def assess_seller(listing: Listing, insp: Inspection, valuation: Valuation) -> SellerCheck:
    s = listing.seller
    score, reasons = 0.6, []
    if s.reviews is None:
        reasons.append("avis inconnus")
    elif s.reviews == 0:
        score -= 0.25
        reasons.append("nouveau compte (0 avis)")
    elif s.reviews < 5:
        score -= 0.1
        reasons.append(f"peu d'avis ({s.reviews})")
    else:
        score += 0.2 if s.reviews >= 50 else 0.1
        reasons.append(f"{s.rating:.1f}★ · {s.reviews} avis" if s.rating else f"{s.reviews} avis")
    if s.rating is not None and (s.reviews or 0) >= 5:
        if s.rating < 4.3:
            score -= 0.25
            reasons.append("note faible")
        elif s.rating >= 4.8 and (s.reviews or 0) >= 10:
            score += 0.1
    idle = _ago(s.last_seen_at)
    if idle is not None:
        if idle > 30:
            score -= 0.15
            reasons.append(f"inactif depuis {idle:.0f} j")
        elif idle < 1:
            score += 0.05
            reasons.append("actif aujourd'hui")
    if s.business:
        score += 0.05
        reasons.append("vendeur pro")
    if insp.scams:
        score -= 0.5
        reasons += insp.scams
    if valuation.fair_value and listing.price < valuation.fair_value * 0.35:
        score -= 0.2
        reasons.append("prix trop beau pour être vrai")
    if len(listing.photos) <= 1:
        score -= 0.1
        reasons.append("une seule photo")
    if listing.description is not None and len(listing.description) < 25:
        score -= 0.05
        reasons.append("description vide")
    score = max(0.0, min(1.0, score))
    level = "ok" if score >= 0.65 else "moyen" if score >= 0.4 else "risque"
    return SellerCheck(level=level, score=round(score, 2), reasons=reasons)
