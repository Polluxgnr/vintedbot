"""Fair-value estimation: blend reference prices, live comparables and AI estimates.

All prices are normalized to a 'very good' condition baseline before aggregation,
then re-scaled to the condition of the item being valued.
"""

from __future__ import annotations

import statistics

from .models import Condition, Valuation

# Asking prices on Vinted are above what items actually sell for (offers, negotiation).
ASK_TO_SALE = 0.9
MIN_COMPS = 4


def robust_center(values: list[float]) -> tuple[float, float, float]:
    """Median and interquartile bounds after trimming obvious outliers (junk 1€ listings, 10x typos)."""
    values = sorted(values)
    med = statistics.median(values)
    trimmed = [v for v in values if med / 3 <= v <= med * 3] or values
    if len(trimmed) >= 4:
        q1, _, q3 = statistics.quantiles(trimmed, n=4)
    else:
        q1, q3 = trimmed[0], trimmed[-1]
    return statistics.median(trimmed), q1, q3


def estimate(
    condition: Condition,
    *,
    reference: float | None = None,
    comps: list[tuple[float, Condition]] | None = None,
    ai_estimate: float | None = None,
    ai_weight: float = 0.4,
    new_price: float | None = None,
    sold: list[tuple[float, Condition]] | None = None,
) -> Valuation:
    """Return the expected resale price of an item in `condition`.

    `reference` and `ai_estimate` are 'very good' baseline prices; comps carry their own condition.
    """
    estimates: list[tuple[float, float, str]] = []   # (baseline value, weight, source)
    low = high = None
    comps = comps or []

    if reference:
        estimates.append((reference, 0.5, "référence"))

    if len(comps) >= MIN_COMPS:
        baseline = [p / c.value_factor for p, c in comps if p > 0]
        center, q1, q3 = robust_center(baseline)
        center *= ASK_TO_SALE
        weight = 0.3 + 0.6 * min(1.0, len(comps) / 25)
        estimates.append((center, weight, f"marché ({len(comps)} annonces)"))
        low, high = q1 * ASK_TO_SALE, q3 * ASK_TO_SALE

    if sold and len(sold) >= 3:
        # Confirmed sales are what buyers actually paid (no ask-to-sale discount): the strongest evidence.
        center, q1, q3 = robust_center([p / c.value_factor for p, c in sold if p > 0])
        estimates.append((center, 0.6 + 0.6 * min(1.0, len(sold) / 15), f"ventes réelles ({len(sold)})"))
        low = q1 if low is None else min(low, q1)
        high = q3 if high is None else max(high, q3)

    if ai_estimate and ai_estimate > 0:
        estimates.append((ai_estimate, ai_weight, "IA"))

    if not estimates:
        return Valuation(comps_count=len(comps), new_price=new_price)

    total_w = sum(w for _, w, _ in estimates)
    baseline_value = sum(v * w for v, w, _ in estimates) / total_w

    # Confidence: more evidence and agreement between sources -> higher.
    values = [v for v, _, _ in estimates]
    spread = (max(values) - min(values)) / baseline_value if len(values) > 1 else 0.35
    agreement = max(0.0, 1 - spread)
    evidence = min(1.0, total_w / 1.6)
    confidence = round(0.15 + 0.85 * evidence * (0.4 + 0.6 * agreement), 2)

    factor = condition.value_factor
    fair = round(baseline_value * factor, 2)
    return Valuation(
        fair_value=fair,
        low=round((low or fair / factor * 0.8) * factor, 2),
        high=round((high or fair / factor * 1.2) * factor, 2),
        confidence=min(confidence, 0.97),
        comps_count=len(comps),
        sources={src: round(v * factor, 2) for v, _, src in estimates},
        new_price=new_price,
    )
