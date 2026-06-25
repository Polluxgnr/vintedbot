from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .types import RawListing

CSV_COLUMNS = [
    "platform",
    "platform_item_id",
    "title",
    "price",
    "total_price",
    "currency",
    "url",
    "photo_url",
    "description",
    "description_snippet",
    "seller_name",
    "seller_rating",
    "seller_feedback_count",
    "condition",
    "brand",
    "location",
    "shipping_available",
    "listed_at",
    "search_terms",
    "scrape_variant",
    "scraped_at",
    "price_band",
    "quality_score",
    "has_photo",
    "has_price",
    "in_price_range",
]


def dedupe_listings(rows: List[RawListing]) -> Tuple[List[RawListing], int]:
    seen: set[str] = set()
    out: List[RawListing] = []
    dupes = 0
    for r in rows:
        key = f"{r.platform}:{r.platform_item_id or r.url or r.title[:40]}"
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dupes


def _to_json_dict(r: RawListing) -> Dict[str, Any]:
    return {
        "platform": r.platform,
        "platform_item_id": r.platform_item_id,
        "title": r.title,
        "price": r.price,
        "total_price": r.total_price,
        "currency": r.currency,
        "url": r.url,
        "photo_url": r.photo_url,
        "description": r.description,
        "seller_name": r.seller_name,
        "seller_rating": r.seller_rating,
        "seller_feedback_count": r.seller_feedback_count,
        "listed_at": r.listed_at,
        "search_terms": r.search_terms,
        "scrape_variant": r.scrape_variant,
        "raw": r.raw,
    }


def write_jsonl(path: str | Path, rows: List[RawListing]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(_to_json_dict(r), ensure_ascii=False) + "\n")


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _price_band(ref: float | None, *, price_min: float | None, price_max: float | None) -> str:
    if ref is None:
        return "unknown"
    if price_min is not None and ref < price_min:
        return "below_range"
    if price_max is not None and ref > price_max:
        return "above_range"
    return "in_range"


def _csv_row(
    r: RawListing,
    *,
    price_min: float | None = None,
    price_max: float | None = None,
    scraped_at: str = "",
) -> Dict[str, Any]:
    price = _safe_float(r.price)
    total = _safe_float(r.total_price)
    ref = total if total is not None else price
    in_range = True
    if ref is not None:
        if price_min is not None and ref < price_min:
            in_range = False
        if price_max is not None and ref > price_max:
            in_range = False
    price_band = _price_band(ref, price_min=price_min, price_max=price_max)
    raw = r.raw if isinstance(r.raw, dict) else {}
    quality_score = (
        int(bool(r.photo_url))
        + int(ref is not None)
        + int(in_range)
        + int(bool(r.title))
        + int(bool(r.url))
        + int(bool(raw.get("condition") or raw.get("brand") or raw.get("location")))
    )
    desc = r.description or ""
    ship = raw.get("shipping_available")
    if ship is None and isinstance(raw.get("shipping"), dict):
        ship = raw["shipping"].get("shippable")
    return {
        "platform": r.platform,
        "platform_item_id": r.platform_item_id,
        "title": r.title,
        "price": r.price,
        "total_price": r.total_price,
        "currency": r.currency or "EUR",
        "url": r.url,
        "photo_url": r.photo_url or "",
        "description": desc,
        "description_snippet": desc[:240],
        "seller_name": r.seller_name or "",
        "seller_rating": r.seller_rating or 0.0,
        "seller_feedback_count": r.seller_feedback_count or 0,
        "condition": str(r.condition or raw.get("condition") or ""),
        "brand": str(r.brand or raw.get("brand") or ""),
        "location": str(raw.get("location") or ""),
        "shipping_available": "" if (r.shipping_available if r.shipping_available is not None else ship) is None else bool(
            r.shipping_available if r.shipping_available is not None else ship
        ),
        "hand_delivery_only": bool(getattr(r, "hand_delivery_only", False) or raw.get("hand_delivery_only")),
        "delivery_mode": str(getattr(r, "delivery_mode", None) or raw.get("delivery_mode") or ""),
        "listed_at": r.listed_at or "",
        "search_terms": r.search_terms or "",
        "scrape_variant": r.scrape_variant or "",
        "scraped_at": scraped_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "price_band": price_band,
        "quality_score": quality_score,
        "has_photo": bool(r.photo_url),
        "has_price": ref is not None,
        "in_price_range": in_range,
    }


def write_csv(
    path: str | Path,
    rows: List[RawListing],
    *,
    price_min: float | None = None,
    price_max: float | None = None,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            str(r.platform or "").lower(),
            (_safe_float(r.total_price) if _safe_float(r.total_price) is not None else _safe_float(r.price))
            if (_safe_float(r.total_price) is not None or _safe_float(r.price) is not None)
            else float("inf"),
            str(r.title or "").lower(),
        ),
    )
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in sorted_rows:
            w.writerow(_csv_row(r, price_min=price_min, price_max=price_max, scraped_at=scraped_at))


def _listing_from_dict(d: Dict[str, Any]) -> RawListing:
    return RawListing(
        platform=str(d.get("platform") or ""),
        platform_item_id=str(d.get("platform_item_id") or ""),
        title=str(d.get("title") or ""),
        price=_safe_float(d.get("price")),
        total_price=_safe_float(d.get("total_price")),
        currency=str(d.get("currency") or "EUR"),
        url=str(d.get("url") or ""),
        photo_url=str(d.get("photo_url") or "") or None,
        description=str(d.get("description") or ""),
        seller_name=str(d.get("seller_name") or ""),
        seller_rating=float(_safe_float(d.get("seller_rating")) or 0.0),
        seller_feedback_count=int(_safe_float(d.get("seller_feedback_count")) or 0),
        listed_at=str(d.get("listed_at") or ""),
        search_terms=str(d.get("search_terms") or ""),
        scrape_variant=str(d.get("scrape_variant") or ""),
        raw=d.get("raw") if isinstance(d.get("raw"), dict) else {},
    )


def merge_bench_exports(out_dir: str | Path) -> Path:
    root = Path(out_dir)
    summary_data: Dict[str, Any] = {}
    summary_path = root / "bench_summary.json"
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                summary_data = loaded
        except ValueError:
            summary_data = {}
    by_platform = root / "by_platform"
    files = sorted(by_platform.glob("*.jsonl")) if by_platform.is_dir() else []
    if not files:
        files = sorted(root.glob("bench_*.jsonl"))
    rows: List[RawListing] = []
    for fp in files:
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(_listing_from_dict(obj))
    deduped, _ = dedupe_listings(rows)
    bench_info = summary_data.get("bench") if isinstance(summary_data.get("bench"), dict) else {}
    pmin = _safe_float(bench_info.get("price_min"))
    pmax = _safe_float(bench_info.get("price_max"))
    csv_path = root / "bench_merged.csv"
    write_csv(csv_path, deduped, price_min=pmin, price_max=pmax)
    by_platform: Dict[str, int] = {}
    for row in deduped:
        key = str(row.platform or "").lower() or "unknown"
        by_platform[key] = by_platform.get(key, 0) + 1
    stats_payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows_total": len(deduped),
        "rows_by_platform": dict(sorted(by_platform.items(), key=lambda kv: kv[0])),
    }
    (root / "bench_merged_stats.json").write_text(
        json.dumps(stats_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return csv_path
