"""Telegram rendering: HTML messages and inline keyboards. No I/O here."""

from __future__ import annotations

import re
from html import escape

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb

from ..config import RuntimeSettings
from ..models import Deal, Recommendation
from ..niches import Niche

FLAG_LABELS = {
    "prix_suspect": "🚨 Prix anormalement bas (fake/arnaque ?)",
    "auth_non_verifiee": "🛡 Authenticité non vérifiée (niche à contrefaçons)",
    "baisse_prix": "📉 Baisse de prix",
}
REC_LABELS = {
    Recommendation.BUY: "🟢 ACHETER",
    Recommendation.NEGOTIATE: "🟡 NÉGOCIER",
    Recommendation.WATCH: "🔵 SURVEILLER",
    Recommendation.SKIP: "🔴 PASSER",
}


# AI "missing" items that are about evidence, not physical parts
_META_MISSING = re.compile(r"photo|visible|verifi|vérifi|montr|éventuel|eventuel|preuve|ticket|facture|confirmer")


def e(text: object) -> str:
    return escape(str(text)) if text is not None else ""


def _age(minutes: float | None) -> str:
    if minutes is None:
        return ""
    if minutes < 60:
        return f"il y a {minutes:.0f} min"
    if minutes < 48 * 60:
        return f"il y a {minutes / 60:.0f} h"
    return f"il y a {minutes / 1440:.0f} j"


# ------------------------------------------------------------------ menus
def main_menu() -> Kb:
    return Kb([
        [Btn("🔎 Nouvelle chasse", callback_data="m:hunt"), Btn("🔗 Analyser une annonce", callback_data="m:analyze")],
        [Btn("🎯 Niches", callback_data="m:niches"), Btn("👁 Mes alertes", callback_data="m:watches")],
        [Btn("💶 Prix du marché", callback_data="m:price"), Btn("🏆 Radar / Top", callback_data="m:top")],
        [Btn("💼 Portefeuille", callback_data="m:pf"), Btn("📊 Stats", callback_data="m:stats")],
        [Btn("⚙️ Réglages", callback_data="m:settings"), Btn("ℹ️ Comment ça marche", callback_data="m:help")],
    ])


WELCOME = (
    "👋 <b>Vinted Hunter</b> — ton chasseur de bonnes affaires.\n\n"
    "• Écris simplement ce que tu cherches : <i>« AirPods Pro 2 à moins de 90€ pour revendre »</i>\n"
    "• Colle un lien Vinted pour une analyse complète (photos + cote + verdict)\n"
    "• Active des <b>niches</b> pour recevoir des alertes en continu\n\n"
    "Chaque annonce est lue en entier : pièces manquantes, défauts, fiabilité du vendeur, prix neuf, "
    "cote du marché. Touche <b>ℹ️ Comment ça marche</b> pour comprendre chaque chiffre."
)

HELP_PAGES = {
    "cote": (
        "📈 <b>Comment est calculée la cote</b>\n\n"
        "La cote = le prix auquel l'article <b>se revend réellement</b> sur Vinted. Elle mélange jusqu'à 3 sources :\n\n"
        "1️⃣ <b>Référence</b> : prix de revente typique du modèle (catalogue de ~150 modèles), poids 0,5.\n"
        "2️⃣ <b>Marché</b> : les prix de toutes les annonces similaires vues par le bot (45 derniers jours). "
        "Chaque prix est ramené à l'état « très bon », les extrêmes sont retirés, on prend la médiane puis on retire 10 % "
        "(un prix demandé n'est pas un prix vendu). Poids 0,3 à 0,9 selon le nombre d'annonces.\n"
        "3️⃣ <b>IA</b> : estimation du modèle complet (poids 0,35 au tri, 0,5 après photos, 0,8 en analyse experte).\n\n"
        "Puis :\n"
        "• <b>État</b> : neuf étiquette ×1,18 · neuf ×1,08 · très bon ×1 · bon ×0,86 · satisfaisant ×0,68\n"
        "• <b>Plafond prix neuf</b> (high-tech, maison…) : jamais plus de 90 % du prix neuf\n"
        "• <b>Pièces manquantes / défauts</b> lus dans le titre et la description : ex. manettes absentes −22 %, "
        "dock −12 %, écran fissuré −35 %, batterie usée −15 %\n\n"
        "Produit absent du catalogue ? Le bot compare avec les autres résultats de la même recherche."
    ),
    "confiance": (
        "🎯 <b>La confiance de la cote</b> (0–97 %)\n\n"
        "Elle dit à quel point la cote est fiable :\n"
        "• <b>Quantité de preuves</b> : plus il y a de sources (référence + marché + IA) et d'annonces comparables, "
        "plus elle monte.\n"
        "• <b>Accord entre les sources</b> : si la référence dit 200 € et le marché 210 €, confiance haute. "
        "Si l'une dit 100 € et l'autre 300 €, elle chute.\n\n"
        "≥ 70 % : solide · 40–70 % : correct · &lt; 40 % : à vérifier soi-même."
    ),
    "profit": (
        "💰 <b>Le profit net</b>\n\n"
        "<b>Coût total</b> = prix + protection acheteur Vinted (5 % + 0,70 €) + frais de port estimés de la niche.\n"
        "<b>Profit net</b> = cote (ajustée) − coût total. Vendre sur Vinted est gratuit pour le vendeur.\n"
        "<b>Marge</b> = profit ÷ coût total.\n\n"
        "Pour un achat perso, le même chiffre s'affiche comme « Économie vs cote »."
    ),
    "score": (
        "⭐ <b>Le score /100</b>\n\n"
        "Somme de points :\n"
        "• Profit : jusqu'à 38 (≈24 au profit minimum visé)\n"
        "• Marge : jusqu'à 22 (100 % de marge = 22)\n"
        "• Confiance de la cote : jusqu'à 15\n"
        "• Fiabilité vendeur : jusqu'à 10\n"
        "• Fraîcheur : jusqu'à 8 (annonce de moins d'1 h = moins de concurrence)\n"
        "• Demande (favoris/vues) : jusqu'à 7\n\n"
        "Malus : prix suspect −18, arnaque probable −30, pièces manquantes −3 chacune, risque fake IA jusqu'à −35, "
        "verdict IA « passer » −25 (« acheter » +8).\n"
        "Plafonds : profit ≤ 0 → max 25. Niches à contrefaçons sans vérification photo : max 55 (luxe, montres, Apple) "
        "ou 75.\n\n"
        "Une alerte part si score ≥ score min, profit ≥ profit min et marge ≥ marge min (voir ⚙️ Réglages)."
    ),
    "vendeur": (
        "🛡 <b>La fiabilité du vendeur</b>\n\n"
        "Départ à 60 %, puis :\n"
        "• 0 avis −25 · moins de 5 avis −10 · 5–49 avis +10 · 50+ avis +20\n"
        "• Note &lt; 4,3★ −25 · ≥ 4,8★ (10+ avis) +10\n"
        "• Inactif depuis 30+ jours −15 · actif aujourd'hui +5 · vendeur pro +5\n"
        "• Contact/paiement hors Vinted (WhatsApp, virement, numéro…) −50\n"
        "• Prix &lt; 35 % de la cote −20 · une seule photo −10 · description vide −5\n\n"
        "🟢 ≥ 65 % · 🟡 40–65 % · 🔴 &lt; 40 %"
    ),
    "pepite": (
        "🔥 <b>Le filtre « pépites seulement »</b>\n\n"
        "Une alerte part seulement si TOUT est vrai :\n"
        "• modèle identifié précisément (pas une catégorie fourre-tout, pas un jeu/manette/carte/article enfant)\n"
        "• description lue en entier, pièces manquantes et défauts déduits\n"
        "• prix ≥ 35 % sous la cote, mais pas &lt; 30 % de la cote (= autre produit ou arnaque)\n"
        "• profit ≥ 30 € (ou le min de la niche) et marge ≥ 40 %\n"
        "• confiance de la cote ≥ 60 %, score ≥ 70\n"
        "• vendeur pas 🔴, aucun signal d'arnaque\n"
        "• <b>vérification du marché en direct</b> : le bot recherche le modèle exact sur Vinted à l'instant, "
        "compare avec au moins 5 annonces identiques (même état) : au plus 1 moins chère, et ≥ 25 % sous la médiane\n"
        "• niches à contrefaçons : vérification photo IA obligatoire (luxe, montres, Apple) ou vendeur 🟢 "
        "+ prix plausible (sneakers, streetwear…)\n\n"
        "Tu peux repasser en mode « normal » dans ⚙️ Réglages."
    ),
    "base": (
        "🗄 <b>La base de ventes</b>\n\n"
        "Vinted n'affiche pas les ventes. Le bot suit donc chaque annonce de modèle identifié et revient la "
        "vérifier (6 annonces toutes les 10 min) : vendue, réservée, supprimée, ou baisse de prix.\n\n"
        "Ça donne, par modèle :\n"
        "• les <b>vrais prix de vente</b> (source la plus fiable de la cote, poids jusqu'à 1,2)\n"
        "• le <b>délai de vente</b> médian (⚡ sur les cartes) → bonus « liquidité » au score (+6 si &lt; 3 j, −4 si > 30 j)\n"
        "• le taux d'écoulement\n\n"
        "Les quasi-pépites sont suivies en priorité : si le vendeur baisse son prix et que l'annonce passe tous "
        "les filtres, tu reçois l'alerte. La base grossit d'elle-même : plus le bot tourne, plus il est précis."
    ),
    "ia": (
        "🧠 <b>La cascade IA (pour ne pas payer cher)</b>\n\n"
        "0. Filtres gratuits (budget, état, taille, accessoires seuls, répliques, pièces détachées)\n"
        "1. Cote + lecture de la description, gratuites\n"
        "2. Tri texte par Haiku, ~20 annonces par appel (~0,00003 $/annonce)\n"
        "3. Vérification photos par Haiku (~0,001 $/annonce), seulement les meilleures\n"
        "4. Verdict expert Sonnet (~0,04 $) : seulement les vraies pépites, ou sur demande\n"
        "5. Avis Opus : seulement si tu appuies sur le bouton\n\n"
        "Un budget quotidien (⚙️ Réglages) coupe d'abord les étapes chères."
    ),
}


def help_keyboard(current: str | None = None) -> Kb:
    labels = {"pepite": "🔥 Pépites", "cote": "📈 Cote", "confiance": "🎯 Confiance", "profit": "💰 Profit",
              "score": "⭐ Score", "vendeur": "🛡 Vendeur", "base": "🗄 Base de ventes", "ia": "🧠 IA"}
    btns = [Btn(("• " if k == current else "") + v, callback_data=f"h:{k}") for k, v in labels.items()]
    return Kb([btns[:3], btns[3:6], btns[6:]])


HELP_INTRO = ("ℹ️ <b>Comment le bot décide</b>\n\nChoisis un sujet. Sur chaque alerte, le bouton "
              "<b>🔍 Détail du calcul</b> montre les chiffres exacts de cette annonce.")


# ------------------------------------------------------------------ deals
def deal_caption(d: Deal, *, personal: bool = False, compact: bool = True) -> str:
    l, v, vr = d.listing, d.valuation, d.verdict
    head = "🔥" if d.pepite else "🔎"
    title = vr.model_name if vr and vr.model_name else (l.model_name or l.title)
    insp = d.inspection
    lines = ["🔥🔥 <b>PÉPITE VÉRIFIÉE</b> 🔥🔥"] if d.pepite else []
    if insp and insp.scams:
        lines.insert(0, f"🚨 <b>ARNAQUE PROBABLE</b> : {e(insp.scams[0])}")
    lines.append(f"{head} <b>{e(title)}</b>")
    if title != l.title:
        lines.append(f"<i>{e(l.title[:80])}</i>")
    lines.append(f"💶 <b>{l.price:.0f} €</b>  ·  coût total {d.landed_cost:.0f} €")
    if v.new_price:
        lines.append(f"🏷 Prix neuf ≈ {v.new_price:.0f} €  (−{max(0, 1 - l.price / v.new_price) * 100:.0f}%)")
    if v.fair_value:
        lines.append(f"📈 Cote ≈ <b>{v.fair_value:.0f} €</b> ({v.low:.0f}–{v.high:.0f})  ·  confiance {v.confidence * 100:.0f}%")
    else:
        lines.append("📈 Cote inconnue (pas assez d'annonces comparables)")
    missing = [f"{f.label} (−{f.impact * 100:.0f}%)" for f in (insp.missing if insp else [])]
    if vr and vr.missing_parts and not missing:
        missing = [m for m in vr.missing_parts if not _META_MISSING.search(m.lower())]
    if missing:
        lines.append("🧩 <b>Incomplet</b> : " + e(", ".join(missing[:3])))
    if insp and insp.defects:
        lines.append("🔧 " + e(", ".join(f"{f.label} (−{f.impact * 100:.0f}%)" for f in insp.defects[:3])))
    if d.profit is not None:
        label = "Économie vs cote" if personal else "Profit net"
        lines.append(f"💰 {label} <b>{d.profit:+.0f} €</b> ({d.margin_pct:+.0f}%)")
    if d.market_check:
        lines.append(market_line(d))
    if v.trend_pct is not None and abs(v.trend_pct) >= 5:
        lines.append(f"{'📈' if v.trend_pct > 0 else '📉'} Tendance 7 j : {v.trend_pct:+.0f} % vs le mois")
    if v.sold_count >= 3 and v.days_to_sell is not None:
        lines.append(f"⚡ Ce modèle se vend en ~{v.days_to_sell:.0f} j ({v.sold_count} ventes suivies)")
    lines.append(f"⭐ Score <b>{d.score}</b>/100")
    if not d.pepite and d.gate_fails:
        lines.append(f"🚫 Pas une pépite : {e(d.gate_fails[0])}")
    meta = " · ".join(filter(None, [l.condition.label, l.size and f"T. {l.size}", l.brand]))
    lines.append(f"📦 {e(meta)}")
    sc = d.seller_check
    seller = f"👤 {e(l.seller.login or 'vendeur')}"
    if sc:
        seller += f"  {sc.icon} {e(' · '.join(sc.reasons[:3]))}"
    lines.append(seller)
    if d.wardrobe and d.wardrobe.flags:
        icon = "🕵️⚠️" if d.wardrobe.suspect else "🕵️"
        lines.append(f"{icon} Dressing : {e(' · '.join(d.wardrobe.flags[:2]))}")
    age = _age(l.age_minutes)
    extras = " · ".join(filter(None, [age and f"publiée {age}", f"❤️ {l.favourites}" if l.favourites else ""]))
    if extras:
        lines.append(f"⏱ {extras}")
    positives = [p for p in (insp.positives if insp else []) if not (missing and p == "complet")]
    if positives:
        lines.append("✅ " + e(" · ".join(positives)))
    if vr:
        if vr.recommendation:
            lines.append(f"\n<b>{REC_LABELS[vr.recommendation]}</b>")
        if vr.summary:
            lines.append(f"🧠 {e(vr.summary[:220 if compact else 1000])}")
        if vr.condition_score is not None:
            lines.append(f"🔬 État photo {vr.condition_score:.0f}/10 · risque fake {vr.authenticity_risk * 100:.0f}%")
        for flag in vr.red_flags[:2 if compact else 8]:
            lines.append(f"⚠️ {e(flag)}")
    for flag in d.flags:
        if flag in FLAG_LABELS:
            lines.append(FLAG_LABELS[flag])
    if l.description:
        excerpt = l.description[:160].replace("\n", " ")
        lines.append(f"📝 <i>{e(excerpt)}{'…' if len(l.description) > 160 else ''}</i>")
    text = "\n".join(lines)
    if compact and len(text) > 1024:
        # Telegram captions are capped at 1024 chars: drop the description excerpt first, then truncate.
        text = "\n".join(x for x in lines if not x.startswith("📝"))[:1020]
    return text


def deal_keyboard(d: Deal) -> Kb:
    lid = d.listing.id
    tier = d.verdict.tier if d.verdict else 0
    return Kb([
        [Btn("🛒 Voir sur Vinted", url=d.listing.url), Btn("🔍 Détail du calcul", callback_data=f"d:why:{lid}")],
        [Btn("🎓 Avis expert" if tier >= 4 else "🧠 Analyse complète",
             callback_data=f"d:{'exp' if tier >= 4 else 'deep'}:{lid}"),
         Btn("💬 Message vendeur", callback_data=f"d:neg:{lid}")],
        [Btn("✅ Acheté", callback_data=f"d:buy:{lid}"), Btn("🙈 Ignorer", callback_data=f"d:ign:{lid}")],
    ])


RADAR_SORT_LABELS = {"valeur": "💎 Valeur", "profit": "💰 Profit", "marge": "📈 Marge", "remise": "🏷 Remise",
                     "recent": "🆕 Récent"}
RADAR_PERIODS = {24: "24 h", 72: "3 j", 168: "7 j"}
RADAR_MIN_PROFITS = [0, 20, 50, 100]


def radar_state(data: str | None = None) -> dict:
    """Radar view state, packed in callback data: tp:sort:niche:page:hours:pepites:analysed:minprofit_index."""
    parts = (data or "tp:valeur:all:0:24:0:0:0").split(":")
    parts += ["0"] * (8 - len(parts))
    return {"sort": parts[1] if parts[1] in RADAR_SORT_LABELS else "valeur", "niche": None if parts[2] == "all" else parts[2],
            "page": int(parts[3]), "hours": int(parts[4]), "pep": parts[5] == "1", "ana": parts[6] == "1",
            "mp": int(parts[7]) % len(RADAR_MIN_PROFITS)}


def radar_cb(st: dict, **change) -> str:
    s = {**st, **change}
    return (f"tp:{s['sort']}:{s['niche'] or 'all'}:{s['page']}:{s['hours']}:{int(s['pep'])}:{int(s['ana'])}:{s['mp']}")


def radar_view(deals: list[Deal], total: int, st: dict, niches: list[tuple[str, int, float]],
               labels: dict[str, str], per_page: int = 8) -> tuple[str, Kb]:
    """The radar: every live good deal, best expected profit first, with filters and a per-niche summary."""
    scope = labels.get(st["niche"], "toutes niches") if st["niche"] else "toutes niches"
    filters = [f for f, on in (("🔥 pépites", st["pep"]), ("🧠 analysées IA", st["ana"])) if on]
    if RADAR_MIN_PROFITS[st["mp"]]:
        filters.append(f"profit ≥ {RADAR_MIN_PROFITS[st['mp']]} €")
    lines = [f"🏆 <b>Radar — {RADAR_PERIODS.get(st['hours'], str(st['hours']) + ' h')}</b> · {e(scope)}",
             f"<i>{total} affaires encore en ligne · tri {RADAR_SORT_LABELS[st['sort']]}"
             + (f" · {', '.join(filters)}" if filters else "") + "</i>"]
    if niches and not st["niche"]:
        summary = " · ".join(f"{labels.get(n, n).split(' ')[0]} {c} (max {b:+.0f} €)" for n, c, b in niches[:5])
        lines.append(summary)
    lines.append("")
    if not deals:
        lines.append("Rien avec ces filtres pour l'instant. Le radar se remplit à chaque scan : élargis la période "
                     "ou retire un filtre.")
    for i, d in enumerate(deals, start=st["page"] * per_page + 1):
        l, v = d.listing, d.valuation
        tags = "".join(t for t, on in (("🔥", d.pepite), ("🧠", bool(d.verdict and d.verdict.tier >= 2)),
                                       ("📊", d.market_check is not None),
                                       ("⚡", v.days_to_sell is not None and v.sold_count >= 3 and v.days_to_sell <= 7))
                       if on)
        name = d.verdict.model_name if d.verdict and d.verdict.model_name else (l.model_name or l.title)
        disc = f" −{(1 - l.price / v.fair_value) * 100:.0f}%" if v.fair_value else ""
        seller = d.seller_check.icon if d.seller_check else ""
        age = _age(l.age_minutes)
        lines.append(f"<b>{i}.</b> {tags} <a href=\"{e(l.url)}\">{e(name[:40])}</a>")
        lines.append(f"     {l.price:.0f} € → {v.fair_value or 0:.0f} €{disc} · <b>{d.profit or 0:+.0f} €</b> "
                     f"· conf. {v.confidence * 100:.0f}% {seller}{' · ' + age if age else ''}")
    lines.append("\n<i>🔥 pépite · 🧠 analysée par l'IA · 📊 marché vérifié · ⚡ se vend en moins d'une semaine</i>")
    pages = max(1, -(-total // per_page))
    sort_btns = [Btn(("• " if k == st["sort"] else "") + lab, callback_data=radar_cb(st, sort=k, page=0))
                 for k, lab in RADAR_SORT_LABELS.items()]
    rows = [sort_btns[:3], sort_btns[3:]]
    rows.append([
        Btn(("✅ " if st["pep"] else "") + "🔥 Pépites", callback_data=radar_cb(st, pep=not st["pep"], page=0)),
        Btn(("✅ " if st["ana"] else "") + "🧠 Analysées", callback_data=radar_cb(st, ana=not st["ana"], page=0)),
        Btn(f"💶 ≥ {RADAR_MIN_PROFITS[(st['mp'] + 1) % len(RADAR_MIN_PROFITS)]} €",
            callback_data=radar_cb(st, mp=(st["mp"] + 1) % len(RADAR_MIN_PROFITS), page=0)),
    ])
    rows.append([Btn(("• " if h == st["hours"] else "") + lab, callback_data=radar_cb(st, hours=h, page=0))
                 for h, lab in RADAR_PERIODS.items()])
    niche_btns = [Btn(("• " if st["niche"] is None else "") + "Toutes", callback_data=radar_cb(st, niche=None, page=0))]
    niche_btns += [Btn(("• " if nid == st["niche"] else "") + f"{labels.get(nid, nid)[:13]} ({cnt})",
                       callback_data=radar_cb(st, niche=nid, page=0)) for nid, cnt, _ in niches[:8]]
    rows += [niche_btns[i:i + 3] for i in range(0, len(niche_btns), 3)]
    if deals:
        open_btns = [Btn(f"📄 {i}", callback_data=f"rv:{d.listing.id}")
                     for i, d in enumerate(deals, start=st["page"] * per_page + 1)]
        rows += [open_btns[i:i + 4] for i in range(0, len(open_btns), 4)]
    nav = []
    if st["page"] > 0:
        nav.append(Btn("◀️", callback_data=radar_cb(st, page=st["page"] - 1)))
    nav.append(Btn("🔄 Vérifier dispo", callback_data="rr:" + radar_cb(st)[3:]))
    if st["page"] + 1 < pages:
        nav.append(Btn("▶️", callback_data=radar_cb(st, page=st["page"] + 1)))
    rows.append(nav)
    return "\n".join(lines)[:4000], Kb(rows)


def market_line(d: Deal) -> str:
    mc = d.market_check
    if not mc or not mc.n:
        return "📊 Marché : aucune annonce identique trouvée en ce moment"
    rank = "la moins chère" if mc.cheaper == 0 else f"{mc.cheaper} moins chère(s)"
    under = f", −{(1 - d.listing.price / mc.median) * 100:.0f}% vs médiane {mc.median:.0f} €" if mc.median else ""
    return f"📊 Marché en direct : {rank} sur {mc.n + 1} annonces identiques{under}"


def explain_deal(d: Deal) -> str:
    """Every number of an alert, step by step."""
    l, v = d.listing, d.valuation
    out = [f"🔍 <b>Détail du calcul</b> — {e(l.title[:60])}\n", "<b>1. Cote</b>"]
    if v.sources:
        for src, val in v.sources.items():
            out.append(f"  • {e(src)} : {val:.0f} €")
        out.append(f"  → moyenne pondérée, état « {e(l.condition.label)} » (×{l.condition.value_factor:g})")
        if v.base_value and v.base_value != v.fair_value:
            out.append(f"  = {v.base_value:.0f} € pour un article complet")
    else:
        out.append("  • aucune source : modèle inconnu et trop peu d'annonces comparables")
    if v.new_price:
        out.append(f"  • prix neuf ≈ {v.new_price:.0f} €")
    if v.learned_n:
        out.append(f"  • référence apprise sur {v.learned_n} annonces des 30 derniers jours (remplace peu à peu la cote de départ)")
    if v.sold_count:
        st = f", écoulement {v.sell_through * 100:.0f} %" if v.sell_through is not None else ""
        dts = f", vendu en ~{v.days_to_sell:.0f} j" if v.days_to_sell is not None else ""
        out.append(f"  • base de ventes : {v.sold_count} ventes confirmées{dts}{st}")
    insp = d.inspection
    adjustments = (insp.missing + insp.defects) if insp else []
    if adjustments:
        out.append("\n<b>2. Ajustements lus dans l'annonce</b>")
        for f in adjustments:
            out.append(f"  • {e(f.label)} : −{f.impact * 100:.0f}%")
        out.append(f"  → facteur ×{insp.value_factor:.2f}")
    if d.verdict and d.verdict.adjust_pct:
        out.append(f"  • décote estimée par l'IA : {d.verdict.adjust_pct:.0f}% (la plus stricte est retenue)")
    if v.fair_value:
        out.append(f"\n<b>Cote finale : {v.fair_value:.0f} €</b> (fourchette {v.low:.0f}–{v.high:.0f}) · "
                   f"confiance {v.confidence * 100:.0f}% · {v.comps_count} annonces comparables")
    out.append("\n<b>3. Coût & profit</b>")
    out.append(f"  • prix {l.price:.2f} € + protection & port {d.landed_cost - l.price:.2f} € = "
               f"<b>{d.landed_cost:.2f} €</b>")
    if d.profit is not None:
        out.append(f"  • cote {v.fair_value:.0f} € − coût {d.landed_cost:.0f} € = <b>{d.profit:+.0f} €</b> "
                   f"(marge {d.margin_pct:+.0f}%)")
    out.append("\n<b>4. Score</b>")
    for k, val in d.score_parts.items():
        out.append(f"  • {e(k)} : {val:+.1f}")
    out.append(f"  = <b>{d.score}/100</b>")
    if round(sum(d.score_parts.values())) != d.score:
        out.append("  <i>(plafonné : profit ≤ 0 ou authenticité pas encore vérifiée sur photos)</i>")
    if mc := d.market_check:
        out.append("\n<b>5. Vérification du marché en direct</b>")
        out.append("  " + market_line(d))
        if mc.p25:
            out.append(f"  • 25 % des annonces identiques sont sous {mc.p25:.0f} € (prix ramenés à l'état de celle-ci)")
    out.append("\n<b>6. Filtre pépite</b> " + ("✅ toutes les conditions remplies" if d.pepite else "❌"))
    for f in d.gate_fails:
        out.append(f"  • {e(f)}")
    if d.seller_check:
        sc = d.seller_check
        out.append(f"\n<b>7. Vendeur</b> {sc.icon} {sc.score * 100:.0f}%")
        for r in sc.reasons:
            out.append(f"  • {e(r)}")
    if insp and insp.positives:
        out.append("\n<b>Points rassurants</b> : " + e(", ".join(insp.positives)))
    if insp and insp.scams:
        out.append("\n🚨 <b>Signaux d'arnaque</b> : " + e(", ".join(insp.scams)))
    return "\n".join(out)[:4000]


def deep_report(d: Deal) -> str:
    vr = d.verdict
    if not vr:
        return "Aucune analyse disponible."
    out = [f"🎓 <b>Analyse {'experte' if vr.tier >= 5 else 'complète'}</b> — {e(vr.model_name or d.listing.title)}"]
    if vr.recommendation:
        out.append(f"\n<b>{REC_LABELS[vr.recommendation]}</b>")
    if vr.summary:
        out.append(e(vr.summary))
    money = []
    if d.valuation.fair_value:
        money.append(f"Revente estimée <b>{d.valuation.fair_value:.0f} €</b>")
    if d.profit is not None:
        money.append(f"profit net <b>{d.profit:+.0f} €</b>")
    if vr.max_buy_price:
        money.append(f"prix max à payer <b>{vr.max_buy_price:.0f} €</b>")
    if vr.time_to_sell_days:
        money.append(f"revente en ~{vr.time_to_sell_days:.0f} j")
    if money:
        out.append("\n💰 " + " · ".join(money))
    if vr.positives:
        out.append("\n<b>👍 Points forts</b>\n" + "\n".join(f"• {e(p)}" for p in vr.positives))
    if vr.red_flags:
        out.append("\n<b>⚠️ Risques</b>\n" + "\n".join(f"• {e(p)}" for p in vr.red_flags))
    if vr.negotiation_message:
        out.append(f"\n<b>💬 Message de négo</b>\n<code>{e(vr.negotiation_message)}</code>")
    if vr.resale_title:
        out.append(f"\n<b>🏷 Annonce de revente</b>\n<code>{e(vr.resale_title)}</code>")
        if vr.resale_description:
            out.append(f"<code>{e(vr.resale_description)}</code>")
    out.append(f"\n<i>Coût IA cumulé pour cette annonce : {vr.cost_usd * 100:.2f} c$</i>")
    return "\n".join(out)[:4000]


# ------------------------------------------------------------------ wizard
def wizard_budget() -> Kb:
    vals = [30, 60, 100, 200, 400, 800]
    return Kb([[Btn(f"≤ {v} €", callback_data=f"w:b:{v}") for v in vals[:3]],
               [Btn(f"≤ {v} €", callback_data=f"w:b:{v}") for v in vals[3:]],
               [Btn("Pas de limite", callback_data="w:b:0")]])












def wizard_niches(niches: list[Niche]) -> Kb:
    rows, row = [], []
    for n in niches:
        row.append(Btn(n.label, callback_data=f"w:n:{n.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return Kb(rows)




# ------------------------------------------------------------------ niches / watches / settings
def niches_view(niches: list[Niche], active: set[str]) -> tuple[str, Kb]:
    lines = ["🎯 <b>Niches surveillées en continu</b>\nTouchez pour activer/désactiver.\n"]
    rows = []
    for n in niches:
        on = n.id in active
        lines.append(f"{'✅' if on else '⬜'} {n.emoji} <b>{e(n.name)}</b> — {e(n.description)}")
        rows.append([Btn(f"{'✅' if on else '⬜'} {n.label}", callback_data=f"n:t:{n.id}")])
    rows.append([Btn("✅ Tout activer", callback_data="n:all:1"), Btn("⬜ Tout couper", callback_data="n:all:0")])
    return "\n".join(lines), Kb(rows)


def watches_view(watches: list[dict]) -> tuple[str, Kb]:
    if not watches:
        return "👁 Aucune alerte. Lance une chasse ou active une niche.", Kb([[Btn("🔎 Nouvelle chasse", callback_data="m:hunt")]])
    lines, rows = ["👁 <b>Tes alertes</b>\n"], []
    for w in watches:
        state = "▶️" if w["active"] else "⏸"
        lines.append(f"{state} <b>{e(w['name'])}</b> — {e(w['spec'].describe())} · {w['hits']} alertes")
        rows.append([Btn(f"{'⏸ Pause' if w['active'] else '▶️ Reprendre'} {w['name'][:18]}", callback_data=f"wa:p:{w['id']}"),
                     Btn("🗑", callback_data=f"wa:d:{w['id']}")])
    return "\n".join(lines), Kb(rows)


SETTINGS_FIELDS = {
    "min_profit_eur": ("Profit min", "€", 5),
    "min_discount_pct": ("Remise min", "%", 5),
    "min_margin_pct": ("Marge min", "%", 5),
    "min_score": ("Score min", "", 5),
    "ai_daily_budget_usd": ("Budget IA/jour", "$", 0.5),
    "deep_min_profit_eur": ("Analyse experte auto si profit ≥", "€", 10),
}


def settings_view(rs: RuntimeSettings, spend: float) -> tuple[str, Kb]:
    pep = rs.alert_mode == "pepites"
    lines = ["⚙️ <b>Réglages</b>\n",
             f"Mode d'alerte : <b>{'🔥 pépites vérifiées seulement' if pep else 'normal'}</b>"]
    rows = [[Btn("🔥 Mode pépites ✅" if pep else "🔥 Passer en mode pépites", callback_data="s:alert_mode:t")]]
    for key, (label, unit, _) in SETTINGS_FIELDS.items():
        val = getattr(rs, key)
        lines.append(f"• {label} : <b>{val:g}{unit}</b>")
        rows.append([Btn("➖", callback_data=f"s:{key}:-"), Btn(f"{label} {val:g}{unit}", callback_data="noop"),
                     Btn("➕", callback_data=f"s:{key}:+")])
    lines.append(f"• Analyse experte auto : <b>{'oui' if rs.auto_deep else 'non'}</b>")
    lines.append(f"• Alertes : <b>{'en pause' if rs.alerts_paused else 'actives'}</b>")
    lines.append(f"• Heures calmes : <b>{'22h-7h' if rs.quiet_hours else 'non'}</b>")
    lines.append(f"\n💸 Dépense IA aujourd'hui : <b>{spend:.3f}$</b> / {rs.ai_daily_budget_usd:g}$")
    rows.append([Btn(f"🎓 Auto-expert {'✅' if rs.auto_deep else '⬜'}", callback_data="s:auto_deep:t"),
                 Btn(f"🔕 Pause {'✅' if rs.alerts_paused else '⬜'}", callback_data="s:alerts_paused:t")])
    rows.append([Btn(f"🌙 Heures calmes {'✅' if rs.quiet_hours else '⬜'}", callback_data="s:quiet_hours:t")])
    return "\n".join(lines), Kb(rows)


def price_report(r: dict) -> str:
    if not r["count"]:
        return f"💶 Aucune annonce trouvée pour « {e(r['query'])} »."
    out = [f"💶 <b>Marché : {e(r['query'])}</b>  ({r['count']} annonces actives)"]
    if r.get("niche"):
        out.append(f"Niche : {e(r['niche'])}")
    if r.get("reference"):
        out.append(f"Cote de référence ({e(r['reference_name'])}) : <b>{r['reference']:.0f} €</b>")
    if r.get("median"):
        out.append(f"Prix demandé médian : <b>{r['median']:.0f} €</b>")
    if (s := r.get("sales")) and s["tracked"]:
        prices = sorted(p for p, _ in s["prices"])
        sold_med = f", prix vendu médian <b>{prices[len(prices) // 2]:.0f} €</b>" if prices else ""
        speed = f", en ~{s['median_days']:.0f} j" if s["median_days"] is not None else ""
        rate = f" · écoulement {s['sell_through'] * 100:.0f} %" if s["sell_through"] is not None else ""
        out.append(f"📦 Base de ventes : {s['tracked']} annonces suivies, <b>{s['sold']} vendues</b>"
                   f"{sold_med}{speed}{rate}")
    if r["by_condition"]:
        out.append("\n<b>Par état</b> (médiane · fourchette 25-75%)")
        for label, s in r["by_condition"].items():
            out.append(f"• {e(label)} : <b>{s['median']:.0f} €</b> · {s['p25']:.0f}–{s['p75']:.0f} € (n={s['n']}, min {s['min']:.0f})")
    if r["cheapest"]:
        out.append("\n<b>Les moins chères</b>")
        for l in r["cheapest"]:
            out.append(f"• <a href=\"{e(l.url)}\">{e(l.title[:50])}</a> — {l.price:.0f} € ({e(l.condition.label)})")
    out.append("\n<i>Prix demandés (pas vendus) : la revente réelle se fait ~10% en dessous.</i>")
    return "\n".join(out)
