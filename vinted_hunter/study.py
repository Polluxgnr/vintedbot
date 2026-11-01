"""Niche study: where are the mispricings? Computed from the market database, refreshed as it grows.

For every catalog model with enough observations:
    volume       listings seen in the window
    deal rate    share of listings priced >= 35 % under the model's learned 'very good' price (condition-adjusted)
    deal profit  median net profit of those deals (resale - price - buyer fees - shipping)
    dispersion   interquartile range / median of condition-adjusted prices (scattered prices = mispricings)
    speed        median days to sell, when the sales database knows it
Opportunity = deals per day x typical profit, discounted for fake risk.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .db import Database
from .market import ASK_TO_SALE, robust_center
from .models import Condition
from .niches import NicheCatalog

FAKE_DISCOUNT = {"low": 1.0, "medium": 0.85, "high": 0.65, "very_high": 0.45}
DEAL_THRESHOLD = 0.65            # price <= 65 % of the learned value
MIN_OBS = 12


async def niche_study(db: Database, catalog: NicheCatalog, days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db._all(
        "SELECT model_key, price, condition, observed_at FROM price_obs WHERE observed_at>=? AND model_key IS NOT NULL "
        "AND model_key NOT LIKE 'sig:%' AND model_key NOT LIKE 'ai:%'", (since,))
    span_days = max(1 / 24, min(days, await _span_days(db, since)))
    by_model: dict[str, list[tuple[float, Condition]]] = defaultdict(list)
    for r in rows:
        by_model[r["model_key"]].append((r["price"], Condition.parse(r["condition"])))

    models = []
    for key, obs in by_model.items():
        if len(obs) < MIN_OBS or ":" not in key:
            continue
        nid, mkey = key.split(":", 1)
        niche = catalog.get(nid)
        ref = next((m for m in niche.models if m.key == mkey), None) if niche else None
        if not niche or not ref or ref.generic:          # catch-all models mix different products: not rankable
            continue
        base = [p / c.value_factor for p, c in obs if p > 0]
        center, q1, q3 = robust_center(base)
        # Measured against the model's OWN market (what it really trades at), not the seed price.
        value = center * ASK_TO_SALE
        dispersion = (q3 - q1) / center if center else 0
        homogeneity = 1.0 if dispersion < 0.6 else 0.6 if dispersion < 1.0 else 0.3   # scattered = mixed products
        fees = lambda price: price * 0.05 + 0.7 + niche.shipping_eur  # noqa: E731
        deals = []
        for p, c in obs:
            v = value * c.value_factor
            if v * 0.35 <= p <= v * DEAL_THRESHOLD:       # under 35 % = other product, part or scam, not a deal
                deals.append(v - p - fees(p))
        deal_rate = len(deals) / len(obs)
        deal_profit = statistics.median(deals) if deals else 0.0
        sales = await db.sales(key, days=days)
        per_day = len(obs) / span_days
        # Profit pool: deals currently on sale x typical profit, discounted for fakes and mixed categories.
        opportunity = (len(deals) * max(deal_profit, 0) * FAKE_DISCOUNT.get(niche.authenticity_risk, 0.8)
                       * homogeneity)
        models.append({
            "key": key, "niche": nid, "name": ref.name, "obs": len(obs), "per_day": round(per_day, 1),
            "value": round(value), "dispersion": round(dispersion, 2), "mixed": dispersion >= 1.0,
            "deal_rate": round(deal_rate, 3), "deal_profit": round(deal_profit), "deals": len(deals),
            "days_to_sell": sales["median_days"], "sold": sales["sold"],
            "fake_risk": niche.authenticity_risk, "opportunity": round(opportunity, 1),
        })

    niches = []
    grouped: dict[str, list[dict]] = defaultdict(list)
    for m in models:
        grouped[m["niche"]].append(m)
    for nid, ms in grouped.items():
        niche = catalog.get(nid)
        obs = sum(m["obs"] for m in ms)
        deals = sum(m["deals"] for m in ms)
        profits = [m["deal_profit"] for m in ms if m["deals"]]
        niches.append({
            "niche": nid, "label": niche.label, "models": len(ms), "obs": obs,
            "per_day": round(sum(m["per_day"] for m in ms), 1), "deal_rate": round(deals / obs, 3) if obs else 0,
            "deal_profit": round(statistics.median(profits)) if profits else 0,
            "opportunity": round(sum(m["opportunity"] for m in ms), 1), "fake_risk": niche.authenticity_risk,
            "best_models": [m["name"] for m in sorted(ms, key=lambda m: -m["opportunity"])[:3]],
        })
    niches.sort(key=lambda n: -n["opportunity"])
    models.sort(key=lambda m: -m["opportunity"])
    return {"days": days, "span_days": round(span_days, 2), "observations": len(rows), "niches": niches,
            "models": models, "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}


async def _span_days(db: Database, since: str) -> float:
    row = await db._one("SELECT MIN(observed_at) AS a, MAX(observed_at) AS b FROM price_obs WHERE observed_at>=?", (since,))
    if not row or not row["a"]:
        return 1.0
    a, b = datetime.fromisoformat(row["a"]), datetime.fromisoformat(row["b"])
    return max((b - a).total_seconds() / 86400, 1 / 24)


def format_study(study: dict, top: int = 8) -> str:
    """Telegram rendering (HTML)."""
    from html import escape as e

    risk = {"low": "🟢", "medium": "🟡", "high": "🟠", "very_high": "🔴"}
    out = [f"📊 <b>Étude des niches</b> — {study['observations']} observations ({study['span_days']} j de données)\n",
           "<b>Classement des niches</b> (réserve de profit = affaires en vente × profit typique, pondérée par le "
           "risque de faux)"]
    for i, n in enumerate(study["niches"][:top], 1):
        out.append(f"{i}. {e(n['label'])} {risk[n['fake_risk']]} — <b>{n['opportunity']:.0f} €</b> · "
                   f"{n['obs']} annonces vues · {n['deal_rate'] * 100:.1f} % d'affaires · ~{n['deal_profit']} € de profit")
        out.append(f"    ↳ {e(', '.join(n['best_models']))}")
    out.append("\n<b>Top modèles</b>")
    for m in study["models"][:top]:
        speed = f" · vendu en ~{m['days_to_sell']:.0f} j" if m["days_to_sell"] else ""
        out.append(f"• {e(m['name'])} — {m['deal_rate'] * 100:.1f} % d'affaires, ~{m['deal_profit']} € "
                   f"(cote {m['value']} €){speed}")
    out.append("\n<i>Plus le bot tourne, plus cette étude est fiable. 🟢🟡🟠🔴 = risque de contrefaçon.</i>")
    return "\n".join(out)[:4000]


def opportunity_bar(value: float, best: float) -> str:
    n = 0 if best <= 0 else max(1, round(10 * math.sqrt(value / best)))
    return "▓" * n + "░" * (10 - n)
