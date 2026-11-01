"""Prompt templates. Every prompt asks for strict JSON so the cascade can merge results."""

from __future__ import annotations

import json

from ..models import Deal, Listing, SearchSpec
from ..niches import Niche

SYSTEM_EXPERT = (
    "Tu es un acheteur-revendeur professionnel sur Vinted France, expert en cotes de l'occasion, "
    "en authentification et en détection d'arnaques. Tu es concis, chiffré et sceptique. "
    "Tu réponds UNIQUEMENT avec du JSON compact valide (une ligne, pas de ```), sans texte autour. "
    "Chaque élément de liste fait 12 mots maximum ; seuls summary, negotiation_message et resale_description "
    "peuvent être plus longs."
)


def _niche_context(niche: Niche | None, spec: SearchSpec | None) -> str:
    lines = []
    if niche:
        lines.append(f"Niche: {niche.name}. Risque contrefaçon: {niche.authenticity_risk}.")
        if niche.ai_hints:
            lines.append(f"Conseils niche: {niche.ai_hints}")
        if niche.red_flags:
            lines.append("Signaux d'alerte connus: " + "; ".join(niche.red_flags))
        refs = ", ".join(f"{m.name}≈{m.resale:.0f}€" for m in niche.models[:25])
        if refs:
            lines.append(f"Cotes de revente indicatives (très bon état): {refs}")
    if spec:
        lines.append(f"Recherche de l'utilisateur: {spec.describe()}. Objectif: "
                     + ("revente avec profit" if spec.goal == "resell" else "achat perso au meilleur rapport qualité/prix"))
        if spec.notes:
            lines.append(f"Contraintes: {spec.notes}")
    return "\n".join(lines)


def _listing_brief(l: Listing, desc_chars: int = 1200) -> dict:
    return {
        "id": l.id, "titre": l.title, "prix": l.price, "marque": l.brand, "taille": l.size,
        "etat": l.condition.label, "photos": len(l.photos), "favoris": l.favourites,
        "vendeur": {"note": l.seller.rating, "avis": l.seller.reviews, "pro": l.seller.business},
        "publiee_il_y_a_min": round(l.age_minutes) if l.age_minutes is not None else None,
        "description": (l.description or "")[:desc_chars],
        "modele_detecte": l.model_name,
    }


def triage(listings: list[Listing], niche: Niche | None, spec: SearchSpec | None) -> str:
    items = json.dumps([_listing_brief(l) for l in listings], ensure_ascii=False)
    return f"""{_niche_context(niche, spec)}

Analyse ces {len(listings)} annonces Vinted. LIS LA DESCRIPTION EN ENTIER : c'est là que les vendeurs
avouent ce qui manque ("je garde les manettes", "sans chargeur") ou les défauts. Pour CHAQUE annonce:
- relevant: false si hors-sujet, accessoire seul, boîte vide, recherche d'achat, ou non conforme à la recherche
- model: modèle exact identifié (ex: "AirPods Pro 2 USB-C", "Nike Dunk Low Panda"), null si inconnu
- resale_eur: prix de revente réaliste sur Vinted FR de ce modèle COMPLET en TRÈS BON ÉTAT (null si impossible)
- new_price_eur: prix neuf actuel en magasin (null si plus vendu neuf ou inconnu)
- missing: pièces/accessoires PHYSIQUES normalement fournis mais ABSENTS (ex: ["manettes", "dock"]). Jamais de photos manquantes ni de "non vérifiable" ici (mets-les dans red_flags)
- adjust_pct: décote en % (négatif) due aux pièces manquantes et défauts de CET article, 0 si complet et sans défaut
- authenticity_risk: 0 à 1 (prix trop bas, formulation louche, signaux de fake)
- seller_ok: false si le vendeur semble douteux (nouveau compte + prix trop bas, contact hors Vinted, texte copié...)
- red_flags: liste courte (max 3) en français
- note: 1 phrase max

Annonces:
{items}

Réponds: {{"items": [{{"id": 123, "relevant": true, "model": "...", "resale_eur": 80, "new_price_eur": 150, "missing": [],
"adjust_pct": 0, "authenticity_risk": 0.2, "seller_ok": true, "red_flags": [], "note": "..."}}]}}"""


def vision(l: Listing, niche: Niche | None, spec: SearchSpec | None, valuation_hint: float | None) -> str:
    checklist = "\n".join(f"- {c}" for c in (niche.vision_checklist if niche else [])) or "- état général, défauts, authenticité"
    return f"""{_niche_context(niche, spec)}

Annonce: {json.dumps(_listing_brief(l, 1500), ensure_ascii=False)}
Valeur de revente estimée par le marché: {valuation_hint or 'inconnue'} €.

Examine les photos jointes. Points à vérifier:
{checklist}

Vérifie aussi ce qui est VISIBLE sur les photos : accessoires présents ou absents (manettes, dock, chargeur, boîte...).
resale_eur = prix de revente du modèle COMPLET en très bon état ; adjust_pct = décote (négative) pour CET article.

Réponds en JSON:
{{"model": "modèle exact visible", "matches_listing": true, "condition_score": 0-10, "defects": ["..."],
"missing": ["pièces PHYSIQUES absentes, pas les photos manquantes"], "adjust_pct": -20, "authenticity_risk": 0-1, "authenticity_notes": "...",
"completeness": "ce qui est présent", "resale_eur": nombre, "new_price_eur": nombre|null,
"positives": ["..."], "red_flags": ["..."], "summary": "2 phrases max"}}"""


def deep(deal: Deal, niche: Niche | None, spec: SearchSpec | None, market: dict) -> str:
    l = deal.listing
    v = deal.verdict
    age = f"{l.age_minutes:.0f} min" if l.age_minutes is not None else "durée inconnue"
    return f"""{_niche_context(niche, spec)}

ANNONCE: {json.dumps(_listing_brief(l, 3000), ensure_ascii=False)}
Publiée il y a {age}. Vues: {l.views}.
COÛT RÉEL D'ACHAT (prix + protection acheteur + port): {deal.landed_cost} €
VALORISATION: {deal.valuation.model_dump_json()}
MARCHÉ OBSERVÉ (annonces similaires): {json.dumps(market, ensure_ascii=False)}
ANALYSES PRÉCÉDENTES: {v.model_dump_json(include={'model_name', 'authenticity_risk', 'condition_score', 'red_flags', 'summary'}) if v else 'aucune'}

Rends un verdict d'investissement final. Lis toute la description, vérifie ce qui manque (photos + texte),
évalue la fiabilité du vendeur (avis, ancienneté, cohérence prix/photos/texte). Calcule le profit net après tous les frais.
JSON attendu:
{{"recommendation": "BUY|NEGOTIATE|WATCH|SKIP", "model": "...", "resale_eur": prix du modèle COMPLET en très bon état,
"max_buy_price": prix max à payer pour rester rentable, "time_to_sell_days": nombre,
"new_price_eur": prix neuf actuel ou null, "missing": ["pièces absentes"], "adjust_pct": décote négative pour CET article,
"seller_ok": true|false,
"authenticity_risk": 0-1, "condition_score": 0-10, "red_flags": ["..."], "positives": ["..."],
"summary": "3 phrases: pourquoi acheter ou non, le piège éventuel, la stratégie de revente",
"negotiation_message": "message court et poli au vendeur en français pour négocier (ou null si BUY direct au prix)",
"resale_title": "titre optimisé pour la revente sur Vinted",
"resale_description": "description de revente vendeuse, 4-6 lignes, honnête sur l'état"}}"""


def intent(text: str, niches: list[Niche]) -> str:
    catalogue = ", ".join(f"{n.id} ({n.name})" for n in niches)
    return f"""L'utilisateur d'un bot de chasse aux bonnes affaires Vinted écrit:
"{text}"

Niches disponibles: {catalogue}

Transforme sa demande en recherche structurée.

RÈGLES :
- N'invente RIEN : price_max, conditions, size, min_profit restent null/[] si l'utilisateur ne les a pas dits.
- goal = "personal" seulement s'il dit que c'est pour lui ; sinon "resell".
- questions : 0 à 2 questions MAXIMUM, uniquement celles qui changent vraiment le prix ou la recherche pour CE
  produit (version/modèle exact, capacité de stockage, génération, taille de vêtement/chaussure...).
  JAMAIS de question sur le budget, l'état, revente/perso ou le profit. Si la demande est déjà précise
  (ex : "ps5 slim digital", "airpods pro 2"), renvoie [].
  Chaque option a un "label" court (bouton) et une "value" = mots à ajouter à la recherche Vinted
  ("" si l'option ne change pas la recherche). Mets toujours une dernière option {{"label": "Peu importe", "value": ""}}.
  Exemple pour "ps5" : {{"question": "Quelle version ?", "options": [{{"label": "Standard (lecteur)", "value": "lecteur"}},
  {{"label": "Digital", "value": "digital"}}, {{"label": "Slim", "value": "slim"}}, {{"label": "Pro", "value": "pro"}},
  {{"label": "Peu importe", "value": ""}}]}}

JSON:
{{"query": "mots-clés de recherche Vinted optimaux (courts, nom de produit)",
"niche_id": "id de niche ou null", "price_min": nombre|null, "price_max": nombre|null,
"conditions": [], "size": null, "goal": "resell", "min_profit": null,
"notes": "contrainte qualitative explicite de l'utilisateur, UNE phrase (ou null)",
"watch": true si l'utilisateur veut être alerté en continu,
"questions": [{{"question": "...", "options": [{{"label": "...", "value": "..."}}]}}]}}"""


def negotiation(deal: Deal) -> str:
    l = deal.listing
    target = deal.verdict.max_buy_price if deal.verdict and deal.verdict.max_buy_price else round(l.price * 0.85)
    return f"""Rédige un message court (3 phrases max), poli et naturel en français pour négocier cette annonce Vinted.
Article: {l.title} à {l.price}€, état {l.condition.label}. Prix visé: ~{target:.0f}€.
Utilise un argument crédible (achat rapide, lot, petit défaut visible...). Pas d'emoji excessif.
JSON: {{"message": "...", "offer_eur": nombre}}"""
