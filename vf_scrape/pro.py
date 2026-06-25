from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .types import RawListing


def row_price(row: Union[RawListing, Any]) -> Optional[float]:
    for attr in ("total_price", "price"):
        v = getattr(row, attr, None)
        if v is None and isinstance(row, dict):
            v = row.get(attr)
        if v is None:
            continue
        try:
            p = float(v)
            if p > 0:
                return p
        except (TypeError, ValueError):
            continue
    return None


def filter_rows_by_price(
    rows: List[RawListing],
    *,
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
) -> Tuple[List[RawListing], int]:
    """Retire les annonces hors fourchette (post-filtre scrape)."""
    pmin = int(price_min) if price_min and int(price_min) > 0 else None
    pmax = int(price_max) if price_max and int(price_max) > 0 else None
    if not pmin and not pmax:
        return rows, 0
    kept: List[RawListing] = []
    dropped = 0
    for r in rows:
        p = row_price(r)
        if p is None:
            kept.append(r)
            continue
        if pmin and p < pmin:
            dropped += 1
            continue
        if pmax and p > pmax:
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


def audit_listings(
    rows: List[RawListing],
    *,
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Métriques qualité pour bench (prix, photos, conformité)."""
    pmin = int(price_min) if price_min and int(price_min) > 0 else None
    pmax = int(price_max) if price_max and int(price_max) > 0 else None
    prices: List[float] = []
    with_price = 0
    with_photo = 0
    in_range = 0
    out_range = 0
    for r in rows:
        p = row_price(r)
        if p is not None:
            with_price += 1
            prices.append(p)
            if (pmin and p < pmin) or (pmax and p > pmax):
                out_range += 1
            else:
                in_range += 1
        if getattr(r, "photo_url", None):
            with_photo += 1
    return {
        "count": len(rows),
        "with_price": with_price,
        "with_photo": with_photo,
        "missing_price": len(rows) - with_price,
        "missing_photo": len(rows) - with_photo,
        "in_range": in_range,
        "out_range": out_range,
        "price_min_seen": min(prices) if prices else None,
        "price_max_seen": max(prices) if prices else None,
        "price_median": sorted(prices)[len(prices) // 2] if prices else None,
    }


def merge_unique_rows(acc: List[RawListing], seen: Set[str], batch: List[RawListing]) -> int:
    n = 0
    for r in batch:
        pid = str(r.platform_item_id or "")
        key = pid or f"{r.platform}:{r.url}:{r.title[:30]}"
        if key in seen:
            continue
        seen.add(key)
        acc.append(r)
        n += 1
    return n


def bands_for_price_range(
    price_min: Optional[int],
    price_max: Optional[int],
    *,
    preferred: Optional[List[Tuple[int, int]]] = None,
    fast: bool = False,
) -> List[Tuple[int, int]]:
    """
    Retourne des bandes optimisées pour une plage de prix utilisateur.
    - Utilise les bandes préférées de la plateforme/catégorie si fournies.
    - Sinon génère des bandes dynamiques plus larges pour éviter trop d'appels.
    """
    pmin = max(0, int(price_min or 0))
    pmax = max(0, int(price_max or 0))
    cap = pmax or 99999
    if cap < pmin:
        return [(pmin, pmin)]

    base = list(preferred or [])
    if base:
        out = [(max(lo, pmin), min(hi, cap)) for lo, hi in base if min(hi, cap) >= max(lo, pmin)]
        if out:
            return out

    width = max(1, cap - pmin)
    if fast:
        target_bands = 3
    elif width <= 250:
        target_bands = 3
    elif width <= 800:
        target_bands = 4
    elif width <= 2000:
        target_bands = 5
    else:
        target_bands = 6
    step = max(60, int((width + target_bands - 1) / target_bands))

    bands: List[Tuple[int, int]] = []
    lo = pmin
    while lo <= cap:
        hi = min(cap, lo + step)
        bands.append((lo, hi))
        if hi >= cap:
            break
        lo = hi
    return bands or [(pmin, cap)]


def bands_for_category(profile_bands, category_hint, *, price_min=None, price_max=None, fast=False):
    cat = (category_hint or "generic").lower()
    preferred = list(profile_bands.get(cat) or profile_bands.get("generic") or [(0, 99999)])
    pmin, pmax = int(price_min or 0), int(price_max or 0)
    if pmin or pmax:
        return bands_for_price_range(pmin, pmax, preferred=preferred, fast=fast)
    bands = preferred
    if fast and len(bands) > 3:
        lo, mid, hi_end = bands[0][0], bands[len(bands) // 2][0], bands[-1][1]
        mid2 = hi_end // 2 + mid // 2
        bands = [(lo, mid), (mid, mid2), (mid2, hi_end)]
    return bands or [(0, 99999)]
