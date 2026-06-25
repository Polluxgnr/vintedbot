"""Dedicated scraper test runner with full DEBUG logs and AI eval export."""

from __future__ import annotations

import json
import os
import time
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, List

from .config import ALL_PLATFORMS, profile
from .eval_export import write_eval_outputs
from .export import dedupe_listings, merge_bench_exports, write_jsonl
from .log import attach_platform_file_logger, detach_platform_file_loggers, setup_scrape_logging
from .orchestrator import ScrapeJob, run_scrape
from .pro import audit_listings
from . import __main__ as cli_main


def _platforms_for_test(args: Namespace) -> List[str]:
    raw = (args.platforms or "all").strip().lower()
    if raw == "all":
        return list(ALL_PLATFORMS)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _write_platform_summary(log_dir: Path, plat: str, entry: Dict[str, Any], *, log_file: str) -> Path:
    payload = {**entry, "log_file": log_file}
    path = log_dir / f"{plat}_summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_scraper_test(args: Namespace) -> int:
    log_level = (getattr(args, "log_level", "") or "DEBUG").strip().upper()
    if args.verbose or log_level == "DEBUG":
        os.environ["VF_SCRAPE_VERBOSE"] = "1"

    setup_scrape_logging(verbose=True, log_level=log_level)
    detach_platform_file_loggers()

    pmin = args.price_min if args.price_min > 0 else None
    pmax = args.price_max if args.price_max > 0 else None
    out_dir = Path(args.out_dir)
    log_dir = out_dir / "logs"
    by_platform_dir = out_dir / "by_platform"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    by_platform_dir.mkdir(parents=True, exist_ok=True)

    plats = _platforms_for_test(args)
    os.environ.setdefault("VF_FORCE_FRAGILE", "1")

    print("=" * 60)
    print("VF Scrape -- test scrapers (full logs)")
    print(f"  query={args.query!r} category={args.category}")
    print(f"  prix={pmin or '-'} - {pmax or '-'} EUR")
    plat_list = ",".join(plats)
    print(f"  platforms={plat_list} log_level={log_level}")
    print(f"  logs -> {log_dir.resolve()}")
    print("=" * 60)

    summary: List[Dict[str, Any]] = []
    merged_rows: List = []
    rows_by_platform: Dict[str, List] = {}
    t0 = time.monotonic()
    pause = max(0, int(getattr(args, "pause", 1) or 0))

    for idx, plat in enumerate(plats):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        cli_main._apply_platform_env(plat, headed=bool(args.headed))
        log_path = attach_platform_file_logger(plat, log_dir, log_level=log_level)
        log = setup_scrape_logging(verbose=True, platform=plat, log_level=log_level)
        log.info("=== TEST START %s ===", plat.upper())

        print(f"\n--- {plat.upper()} ---")
        print(f"  log: {log_path}")
        cap = args.max or profile(plat).default_max
        job = ScrapeJob(
            query=args.query,
            platforms=[plat],
            category_hint=args.category,
            price_min=pmin,
            price_max=pmax,
            max_per_platform=cap,
            headless=not args.headed,
            verbose=True,
            smart_alias=(plat == "vinted") and (not args.no_smart_alias),
        )
        t_plat = time.monotonic()
        try:
            batch = run_scrape(job)
        except Exception as exc:
            log.exception("platform crash: %s", exc)
            print(f"  ERREUR: {exc}")
            entry = {
                "platform": plat,
                "unique": 0,
                "error": str(exc),
                "duration_s": round(time.monotonic() - t_plat, 1),
                "log_file": str(log_path),
            }
            summary.append(entry)
            _write_platform_summary(log_dir, plat, entry, log_file=str(log_path))
            log.info("=== TEST END %s (error) ===", plat.upper())
            continue

        rows = [r for r in batch.listings if r.platform == plat]
        merged_rows.extend(rows)
        rows_by_platform[plat] = rows
        st = batch.stats.get(plat)
        audit = audit_listings(rows, price_min=pmin, price_max=pmax)
        write_jsonl(by_platform_dir / f"{plat}.jsonl", rows)
        entry = {
            "platform": plat,
            "unique": audit["count"],
            "raw": int(st.raw) if st else audit["count"],
            "variants": int(st.variants) if st else 0,
            "errors": int(st.errors) if st else 0,
            "skipped": bool(st.skipped) if st else False,
            "audit": audit,
            "duration_s": round(time.monotonic() - t_plat, 1),
            "last_error": (st.last_error if st else "") or "",
            "log_file": str(log_path),
        }
        summary.append(entry)
        _write_platform_summary(log_dir, plat, entry, log_file=str(log_path))
        log.info(
            "=== TEST END %s -- %d uniques, %.1fs ===",
            plat.upper(),
            entry["unique"],
            entry["duration_s"],
        )
        if st and st.skipped:
            print(f"  SKIP: {st.last_error or 'ignore'}")
        else:
            print(f"  {cli_main._fmt_audit(audit, pmin, pmax)}")
            print(f"  brut={entry['raw']} variantes={entry['variants']} {entry['duration_s']}s")

    total = sum(int(x.get("unique") or 0) for x in summary)
    deduped_rows, dupes = dedupe_listings(merged_rows)
    duration_s = time.monotonic() - t0

    summary_payload = {
        "test_run": {
            "query": args.query,
            "category": args.category,
            "price_min": pmin,
            "price_max": pmax,
            "platforms": plats,
            "log_level": log_level,
            "duration_s": round(duration_s, 1),
            "log_dir": str(log_dir.resolve()),
        },
        "platforms": summary,
        "merged": {
            "unique": len(deduped_rows),
            "duplicates_removed": dupes,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    merged_summary_path = log_dir / "merged_summary.json"
    merged_summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "bench_summary.json").write_text(
        json.dumps(
            {
                "bench": summary_payload["test_run"],
                "platforms": summary,
                "merged": summary_payload["merged"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    csv_path = ""
    if getattr(args, "csv", True):
        csv_path = str(merge_bench_exports(out_dir))
        summary_payload["merged"]["csv"] = csv_path

    eval_paths = write_eval_outputs(
        out_dir=out_dir,
        query=args.query,
        category=args.category,
        price_min=pmin,
        price_max=pmax,
        platform_summaries=summary,
        rows_by_platform=rows_by_platform,
        merged_rows=deduped_rows,
        duration_s=duration_s,
    )
    summary_payload["eval"] = eval_paths
    merged_summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 60)
    print(f"Total {total} uniques en {duration_s:.0f}s")
    for entry in summary:
        n = int(entry.get("unique") or 0)
        flag = "OK" if n > 0 else "FAIL"
        print(f"  {entry['platform']:14} {flag:5} {n:4}")
    print(f"\nMerged summary: {merged_summary_path.resolve()}")
    if csv_path:
        print(f"CSV: {csv_path}")
    for label, path in eval_paths.items():
        print(f"{label}: {path}")
    print(f"Logs: {log_dir.resolve()}")

    detach_platform_file_loggers()
    return 0 if total > 0 else 1
