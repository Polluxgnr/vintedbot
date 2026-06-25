from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .export import CSV_COLUMNS, _csv_row, _safe_float
from .pro import audit_listings
from .types import RawListing


def _quality_sorted(rows: List[RawListing]) -> List[RawListing]:
    def key(r: RawListing) -> tuple:
        d = _csv_row(r)
        return (-int(d.get("quality_score") or 0), str(r.title or "").lower())

    return sorted(rows, key=key)


def _sample_record(r: RawListing, *, price_min: Optional[float], price_max: Optional[float]) -> Dict[str, Any]:
    base = _csv_row(r, price_min=price_min, price_max=price_max)
    raw = r.raw if isinstance(r.raw, dict) else {}
    return {
        **base,
        "relevance_flags": {
            "has_photo": base.get("has_photo"),
            "has_price": base.get("has_price"),
            "in_price_range": base.get("in_price_range"),
            "price_band": base.get("price_band"),
        },
        "scrape_metadata": {
            "search_terms": r.search_terms,
            "scrape_variant": r.scrape_variant,
            "platform_item_id": r.platform_item_id,
            "seller_name": r.seller_name,
            "seller_rating": r.seller_rating,
            "listed_at": r.listed_at,
            "raw_keys": sorted(raw.keys())[:24],
        },
    }


def write_eval_outputs(
    *,
    out_dir: str | Path,
    query: str,
    category: str,
    price_min: Optional[int],
    price_max: Optional[int],
    platform_summaries: List[Dict[str, Any]],
    rows_by_platform: Dict[str, List[RawListing]],
    merged_rows: List[RawListing],
    duration_s: float,
) -> Dict[str, str]:
    """Écrit eval_report.json, eval_samples.jsonl, eval_prompt.md dans bench_out/eval/."""
    eval_dir = Path(out_dir) / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    pmin_f = _safe_float(price_min)
    pmax_f = _safe_float(price_max)

    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": query,
        "category": category,
        "price_min": price_min,
        "price_max": price_max,
        "duration_s": round(duration_s, 1),
        "platforms": {},
        "merged": {
            "unique": len(merged_rows),
            "audit": audit_listings(merged_rows, price_min=price_min, price_max=price_max),
        },
    }

    samples_path = eval_dir / "eval_samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as sf:
        for plat, rows in sorted(rows_by_platform.items()):
            audit = audit_listings(rows, price_min=price_min, price_max=price_max)
            ranked = _quality_sorted(rows)
            top5 = [_sample_record(r, price_min=pmin_f, price_max=pmax_f) for r in ranked[:5]]
            bottom5 = [_sample_record(r, price_min=pmin_f, price_max=pmax_f) for r in ranked[-5:]] if len(ranked) > 5 else []
            entry = next((e for e in platform_summaries if e.get("platform") == plat), {})
            report["platforms"][plat] = {
                "metrics": audit,
                "timing_s": entry.get("duration_s"),
                "variants": entry.get("variants"),
                "raw": entry.get("raw"),
                "errors": entry.get("last_error") or entry.get("error") or "",
                "skipped": bool(entry.get("skipped")),
                "samples_top5": top5,
                "samples_bottom5": bottom5,
            }
            for r in ranked[:12]:
                rec = _sample_record(r, price_min=pmin_f, price_max=pmax_f)
                rec["eval_bucket"] = "representative"
                sf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    report_path = eval_dir / "eval_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    prompt_path = eval_dir / "eval_prompt.md"
    prompt_path.write_text(_build_prompt_md(report, eval_dir), encoding="utf-8")

    return {
        "eval_report": str(report_path.resolve()),
        "eval_samples": str(samples_path.resolve()),
        "eval_prompt": str(prompt_path.resolve()),
    }


def _build_prompt_md(report: Dict[str, Any], eval_dir: Path) -> str:
    plats = report.get("platforms") or {}
    lines = [
        "# VF Finder — AI evaluation prompt",
        "",
        "Copy everything below into your AI assistant to improve triage, scoring, and search quality.",
        "",
        "## Context",
        "",
        f"- **Query:** `{report.get('query', '')}`",
        f"- **Category:** `{report.get('category', '')}`",
        f"- **Price range:** {report.get('price_min') or '—'} – {report.get('price_max') or '—'} EUR",
        f"- **Scrape duration:** {report.get('duration_s')}s",
        "",
        "## Per-platform summary",
        "",
    ]
    for plat, info in sorted(plats.items()):
        m = info.get("metrics") or {}
        lines.append(
            f"- **{plat}**: {m.get('count', 0)} listings, "
            f"photos {m.get('with_photo', 0)}/{m.get('count', 0)}, "
            f"in-range {m.get('in_range', 0)}, "
            f"errors: `{info.get('errors') or 'none'}`"
        )
    lines.extend(
        [
            "",
            "## Attached files",
            "",
            f"- `{eval_dir / 'eval_report.json'}` — metrics + top/bottom samples per platform",
            f"- `{eval_dir / 'eval_samples.jsonl'}` — representative listings with all eval fields",
            f"- `bench_out/bench_merged.csv` — full merged export",
            "",
            "## Task for the AI",
            "",
            "1. Review sample listings (title, price, brand, condition, photo_url, relevance flags).",
            "2. Identify false positives (wrong category, accessories, broken items) and false negatives.",
            "3. Suggest improvements to **quality_score**, **category hints**, **alias expansion**, and **price bands**.",
            "4. Propose scoring/triage rules for the VF assistant pipeline.",
            "",
            "## Key fields",
            "",
            "Each sample includes: `title`, `price`, `platform`, `url`, `photo_url`, `brand`, `condition`,",
            "`quality_score`, `relevance_flags`, `scrape_metadata`.",
            "",
            "---",
            "",
            "Here is scraper output — help me improve triage/scoring/assistant for this search.",
            "",
        ]
    )
    return "\n".join(lines)
