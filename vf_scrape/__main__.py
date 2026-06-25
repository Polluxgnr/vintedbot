"""CLI bench scrape — py -m vf_scrape -q "New Nintendo 3DS XL" -p leboncoin -v"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from .config import ALL_PLATFORMS, profile
from .export import dedupe_listings, merge_bench_exports, write_jsonl
from .orchestrator import ScrapeJob, run_scrape
from .pro import audit_listings

_PLATFORM_ENV: Dict[str, Dict[str, str]] = {
    "vinted": {
        "VF_FAST_SCRAPE": "0",
        "VF_DEEP_SCRAPE": "1",
        "VF_VINTED_ALIAS_MAX": "2",
        "VF_SCRAPE_VINTED_MAX": "1800",
        "VF_VARIANT_MAX": "1800",
        "VF_VINTED_BOOTSTRAP_TIMEOUT": "45",
        "VF_BOOTSTRAP_TIMEOUT_SEC": "45",
        "VF_VINTED_BROWSER_BOOT": "0",
        "VF_VINTED_HTTP_TIMEOUT": "12",
        "VF_VINTED_MIN_BAND_GAIN": "5",
        "VF_HTTP_POOL_SIZE": "10",
    },
    "leboncoin": {
        "VF_LBC_DEEP": "1",
        "VF_LBC_ALIAS_MAX": "2",
        "VF_LBC_BAND_PAUSE_MS": "1500",
        "VF_LBC_HEALTHY_BAND_PAUSE_MS": "600",
        "VF_LBC_BAND_RETRIES": "2",
        "VF_LBC_RATE_PER_MIN": "10",
        "VF_LBC_MAX_BOOTSTRAP": "1",
        "VF_LBC_BLOCK_COOLDOWN_MIN": "25",
        "VF_LBC_HEADED": "0",
        "VF_LBC_AUTO_HEADED": "0",
        "VF_SCRAPE_LBC_MAX": "1400",
    },
    "wallapop": {
        "VF_WALLAPOP_DEEP": "1",
        "VF_WALLAPOP_MAX_PAGES": "15",
        "VF_WALLAPOP_ALIAS_MAX": "4",
        "VF_WALLAPOP_VARIANTS_MAX": "4",
        "VF_WALLAPOP_SKIP_PRIMARY": "1",
        "VF_WALLAPOP_SHIPPABLE": "0",
        "VF_WALLAPOP_RATE_PER_MIN": "36",
        "VF_WALLAPOP_MIN_INTERVAL_MS": "300",
        "VF_WALLAPOP_MAX_INTERVAL_MS": "900",
        "VF_SCRAPE_WALLAPOP_MAX": "400",
    },
    "ebay": {
        "VF_EBAY_MAX_PAGES": "12",
        "VF_SCRAPE_EBAY_MAX": "220",
        "VF_EBAY_ALIAS_MAX": "2",
        "VF_EBAY_HEADED": "0",
        "VF_EBAY_AUTO_HEADED": "0",
    },
    "rakuten": {
        "VF_SKIP_RAKUTEN": "0",
        "VF_RAKUTEN_ALIAS_MAX": "4",
        "VF_RAKUTEN_MAX_PAGES": "10",
        "VF_SCRAPE_RAKUTEN_MAX": "400",
        "VF_RAKUTEN_BOOTSTRAP_TIMEOUT": "60",
        "VF_RAKUTEN_HEADED": "0",
        "VF_RAKUTEN_AUTO_HEADED": "0",
    },
    "cashexpress": {
        "VF_SKIP_CASHEXPRESS": "0",
        "VF_CEX_ALIAS_MAX": "4",
        "VF_CEX_MAX_PAGES": "15",
        "VF_SCRAPE_CEX_MAX": "260",
        "VF_CEX_URL_PRICE": "1",
        "VF_CEX_HEADED": "0",
        "VF_CEX_AUTO_HEADED": "0",
        "VF_CEX_BOOTSTRAP_TIMEOUT": "60",
        "VF_CEX_FETCH_TIMEOUT": "120",
    },
}

_HEADED_ENV_BY_PLATFORM: Dict[str, str] = {
    "leboncoin": "VF_LBC_HEADED",
    "ebay": "VF_EBAY_HEADED",
    "rakuten": "VF_RAKUTEN_HEADED",
    "cashexpress": "VF_CEX_HEADED",
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vf_scrape",
        description="VF Scrape — extraction catalogue (bench par plateforme)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples rapides:
  py -m vf_scrape -q "New Nintendo 3DS XL" -p leboncoin -v
  py -m vf_scrape -q "New Nintendo 3DS XL" -p vinted -v --max 600
  py -m vf_scrape -q "New Nintendo 3DS XL" -p wallapop -v
  py -m vf_scrape -q "New Nintendo 3DS XL" -p vinted,leboncoin,wallapop -v
  py -m vf_scrape --bench-all -q "video projecteur" --category projector --price-min 50 --price-max 900 -v
  py -m vf_scrape --test-scrapers -q "video projecteur" --category projector --price-min 50 --price-max 900 -v --log-level DEBUG
  py -m vf_scrape --test-scrapers -p vinted -q "video projecteur" --category projector -v --log-level DEBUG
  py -m vf_scrape --bench-all -q "New Nintendo 3DS XL" -v
  py -m vf_scrape -q "New Nintendo 3DS XL" -p leboncoin -v --headed
        """,
    )
    p.add_argument("-q", "--query", required=True, help="Terme de recherche")
    p.add_argument(
        "-p",
        "--platforms",
        default="vinted,leboncoin,wallapop",
        help="Plateformes (virgules) ou 'all'",
    )
    p.add_argument("--category", default="console_ds", help="Hint catégorie")
    p.add_argument("--price-min", type=int, default=0)
    p.add_argument("--price-max", type=int, default=0)
    p.add_argument("--max", type=int, default=0, help="Max annonces par variante")
    p.add_argument("--headed", action="store_true", help="Navigateurs visibles (LBC/eBay)")
    p.add_argument("--no-smart-alias", action="store_true", help="Toujours toutes les variantes")
    p.add_argument("--bench-all", action="store_true", help="Teste chaque plateforme (script unifie)")
    p.add_argument(
        "--test-scrapers",
        action="store_true",
        help="Test scrapers avec logs fichier + export eval (bench_out/logs, bench_out/eval)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Logs DEBUG")
    p.add_argument(
        "--log-level",
        default="",
        choices=["", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="Niveau de log (--test-scrapers, défaut DEBUG si -v)",
    )
    p.add_argument("-o", "--out", default="", help="Export JSONL (run simple)")
    p.add_argument("--out-dir", default="bench_out", help="Dossier export (--bench-all)")
    p.add_argument("--csv", dest="csv", action="store_true", default=True, help="Export CSV fusionné (--bench-all)")
    p.add_argument("--no-csv", dest="csv", action="store_false", help="Désactive CSV fusionné (--bench-all)")
    p.add_argument("--pause", type=int, default=1, help="Pause entre plateformes (--bench-all / --test-scrapers)")
    args = p.parse_args(argv)

    log_level = (args.log_level or "").strip().upper()
    if args.verbose or log_level == "DEBUG":
        os.environ["VF_SCRAPE_VERBOSE"] = "1"
    if args.test_scrapers and not log_level:
        log_level = "DEBUG"
        args.log_level = "DEBUG"

    if args.test_scrapers:
        from .bench_runner import run_scraper_test

        return run_scraper_test(args)

    if args.platforms.strip().lower() == "all":
        plats = list(ALL_PLATFORMS)
    else:
        plats = [x.strip() for x in args.platforms.split(",") if x.strip()]

    if args.bench_all:
        return _bench_all(args)

    job = ScrapeJob(
        query=args.query,
        platforms=plats,
        category_hint=args.category,
        price_min=args.price_min if args.price_min > 0 else None,
        price_max=args.price_max if args.price_max > 0 else None,
        max_per_platform=args.max,
        headless=not args.headed,
        verbose=args.verbose,
        smart_alias=not args.no_smart_alias,
    )
    batch = run_scrape(job)
    _print_summary(batch)
    if args.out:
        write_jsonl(args.out, batch.listings)
        print(f"Export: {args.out}")
    return 0 if batch.listings else 1


def _print_summary(batch) -> None:
    print(f"\nRésumé: {len(batch.listings)} annonces uniques")
    for plat, st in sorted(batch.stats.items()):
        print(
            f"  {plat}: {st.unique} uniques / {st.raw} brut, "
            f"{st.variants} req, err={st.errors}"
            + (" SKIP" if st.skipped else "")
        )


def _apply_platform_env(plat: str, headed: bool) -> None:
    for key, val in _PLATFORM_ENV.get(plat, {}).items():
        os.environ[key] = val
    if plat in ("rakuten", "cashexpress"):
        os.environ["VF_FORCE_FRAGILE"] = "1"
    if headed:
        env_key = _HEADED_ENV_BY_PLATFORM.get(plat)
        if env_key:
            os.environ[env_key] = "1"


def _fmt_audit(audit: dict, pmin: Optional[int], pmax: Optional[int]) -> str:
    parts = [
        f"{audit['count']} uniques",
        f"prix {audit['with_price']}/{audit['count']}",
        f"photos {audit['with_photo']}/{audit['count']}",
    ]
    if pmin or pmax:
        parts.append(f"dans [{pmin or '-'}-{pmax or '-'} EUR]: {audit['in_range']}")
        if audit["out_range"]:
            parts.append(f"hors fourchette: {audit['out_range']}")
    med = audit.get("price_median")
    if med is not None:
        parts.append(f"mediane {med:.0f} EUR")
    return " | ".join(parts)


def _bench_all(args) -> int:
    os.environ.setdefault("VF_FORCE_FRAGILE", "1")
    pmin = args.price_min if args.price_min > 0 else None
    pmax = args.price_max if args.price_max > 0 else None
    out_dir = Path(args.out_dir)
    by_platform_dir = out_dir / "by_platform"
    out_dir.mkdir(parents=True, exist_ok=True)
    by_platform_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("bench_*.jsonl"):
        try:
            old.unlink()
        except OSError:
            pass
    for old in by_platform_dir.glob("*.jsonl"):
        try:
            old.unlink()
        except OSError:
            pass

    print("=" * 60)
    print("VF Scrape — bench unifie (6 plateformes)")
    print(f"  query={args.query!r} category={args.category}")
    print(f"  prix={pmin or '-'} - {pmax or '-'} EUR")
    print("=" * 60)

    summary: list[dict] = []
    merged_rows = []
    t0 = time.monotonic()
    for idx, plat in enumerate(ALL_PLATFORMS):
        if idx > 0 and args.pause > 0:
            time.sleep(args.pause)
        _apply_platform_env(plat, headed=args.headed)
        print(f"\n--- {plat.upper()} ---")
        cap = args.max or profile(plat).default_max
        job = ScrapeJob(
            query=args.query,
            platforms=[plat],
            category_hint=args.category,
            price_min=pmin,
            price_max=pmax,
            max_per_platform=cap,
            headless=not args.headed,
            verbose=args.verbose,
            smart_alias=(plat == "vinted") and (not args.no_smart_alias),
        )
        t_plat = time.monotonic()
        try:
            batch = run_scrape(job)
        except Exception as exc:
            print(f"  ERREUR: {exc}")
            summary.append({"platform": plat, "unique": 0, "error": str(exc)})
            continue
        rows = [r for r in batch.listings if r.platform == plat]
        merged_rows.extend(rows)
        st = batch.stats.get(plat)
        audit = audit_listings(rows, price_min=pmin, price_max=pmax)
        write_jsonl(by_platform_dir / f"{plat}.jsonl", rows)
        entry = {
            "platform": plat,
            "unique": audit["count"],
            "raw": int(st.raw) if st else audit["count"],
            "variants": int(st.variants) if st else 0,
            "audit": audit,
            "duration_s": round(time.monotonic() - t_plat, 1),
            "last_error": (st.last_error if st else "") or "",
        }
        summary.append(entry)
        if st and st.skipped:
            print(f"  SKIP: {st.last_error or 'ignore'}")
        else:
            print(f"  {_fmt_audit(audit, pmin, pmax)}")
            print(f"  brut={entry['raw']} variantes={entry['variants']} {entry['duration_s']}s")

    total = sum(int(x.get("unique") or 0) for x in summary)
    deduped_rows, dupes = dedupe_listings(merged_rows)
    print("\n" + "=" * 60)
    print(f"Total {total} uniques en {time.monotonic() - t0:.0f}s")
    for entry in summary:
        n = int(entry.get("unique") or 0)
        flag = "OK" if n > 0 else "FAIL"
        print(f"  {entry['platform']:14} {flag:5} {n:4}")
    summary_payload = {
        "bench": {
            "query": args.query,
            "category": args.category,
            "price_min": pmin,
            "price_max": pmax,
            "headed": bool(args.headed),
            "duration_s": round(time.monotonic() - t0, 1),
        },
        "platforms": summary,
        "merged": {
            "unique": len(deduped_rows),
            "duplicates_removed": dupes,
            "csv": "",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    csv_path = ""
    (out_dir / "bench_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.csv:
        csv_path = str(merge_bench_exports(out_dir))
        summary_payload["merged"]["csv"] = csv_path
        stats_path = out_dir / "bench_merged_stats.json"
        if stats_path.is_file():
            try:
                summary_payload["merged"]["stats"] = json.loads(stats_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
    (out_dir / "bench_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if csv_path:
        print(f"CSV: {csv_path}")
    try:
        from .eval_export import write_eval_outputs

        rows_by_platform = {}
        for entry in summary:
            plat = entry.get("platform", "")
            fp = by_platform_dir / f"{plat}.jsonl"
            if fp.is_file():
                from .export import _listing_from_dict

                plat_rows = []
                with fp.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict):
                                plat_rows.append(_listing_from_dict(obj))
                        except ValueError:
                            pass
                rows_by_platform[plat] = plat_rows
        eval_paths = write_eval_outputs(
            out_dir=out_dir,
            query=args.query,
            category=args.category,
            price_min=pmin,
            price_max=pmax,
            platform_summaries=summary,
            rows_by_platform=rows_by_platform,
            merged_rows=deduped_rows,
            duration_s=time.monotonic() - t0,
        )
        print("Eval:")
        for label, path in eval_paths.items():
            print(f"  {label}: {path}")
    except Exception as exc:
        print(f"Eval export skipped: {exc}")
    print(f"Export: {out_dir.resolve()}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
