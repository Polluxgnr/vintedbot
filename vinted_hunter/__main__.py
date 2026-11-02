"""Entry point.

    python -m vinted_hunter                 # Telegram bot + continuous watches
    python -m vinted_hunter hunt "airpods pro 2" --max 120
    python -m vinted_hunter price "contax t2"
    python -m vinted_hunter analyze https://www.vinted.fr/items/123
    python -m vinted_hunter niches          # list niches
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import RuntimeSettings, get_settings
from .models import SearchSpec
from .runtime import acquire_single_instance, build_services, setup_logging
from .vinted.client import VintedUnavailable


def _print_deals(deals, limit: int = 15) -> None:
    print(f"\n{'score':>5} {'prix':>6} {'cote':>6} {'profit':>7}  tier  titre")
    for d in deals[:limit]:
        fv = f"{d.valuation.fair_value:.0f}" if d.valuation.fair_value else "?"
        pr = f"{d.profit:+.0f}" if d.profit is not None else "?"
        tier = d.verdict.tier if d.verdict else 1
        print(f"{d.score:>5} {d.listing.price:>6.0f} {fv:>6} {pr:>7}  T{tier}   {'🔥 ' if d.pepite else ''}{d.listing.title[:60]}")
        print(f"{'':>34}{d.listing.url}")
        if d.market_check:
            mc = d.market_check
            print(f"{'':>34}📊 {mc.cheaper} moins chère(s) sur {mc.n} identiques en ligne (+{mc.history} sur 14 j), médiane {mc.median}")
        if d.gate_fails:
            print(f"{'':>34}🚫 {' | '.join(d.gate_fails[:3])}")
        if d.verdict and d.verdict.summary:
            print(f"{'':>34}🧠 {d.verdict.summary[:120]}")


async def _cli(args: argparse.Namespace) -> None:
    sv = await build_services()
    rs = RuntimeSettings()

    async def progress(msg: str) -> None:
        print(" ", msg)

    try:
        if args.cmd == "hunt":
            spec = SearchSpec(query=args.query, price_max=args.max, niche_id=args.niche, min_profit=args.profit)
            deals, niche = await sv.hunter.hunt(spec, rs, progress)
            print(f"Niche: {niche.label if niche else '-'}")
            _print_deals(deals)
        elif args.cmd == "price":
            r = await sv.hunter.price_check(args.query)
            print({k: v for k, v in r.items() if k != "cheapest"})
        elif args.cmd == "analyze":
            deal = await sv.hunter.analyze_url(args.url, rs, progress)
            if deal:
                _print_deals([deal])
                print(deal.verdict.model_dump_json(indent=2) if deal.verdict else "")
            else:
                print("Annonce introuvable")
        elif args.cmd == "harvest":
            # Big data run: crawl every niche and model for N minutes, then clean and recalibrate.
            import time

            end = time.monotonic() + args.minutes * 60
            new = steps = 0
            while time.monotonic() < end:
                try:
                    new += await sv.hunter.harvest_step()
                except VintedUnavailable as exc:
                    print(" ", exc)
                    await asyncio.sleep(sv.vinted.cooldown_remaining + 1)
                    continue
                steps += 1
                if steps % 25 == 0:
                    left = (end - time.monotonic()) / 60
                    print(f"  {steps} pages · {new} nouvelles annonces · {left:.0f} min restantes", flush=True)
            print(f"Récolte terminée : {steps} pages, {new} nouvelles annonces.")
            print("Calibration :", await sv.db.calibrate(), "modèles")
        elif args.cmd == "study":
            import json

            from .study import format_study, niche_study

            study = await niche_study(sv.db, sv.niches, days=args.days)
            if args.json:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump(study, fh, ensure_ascii=False, indent=1)
                print("écrit :", args.json)
            print(format_study(study, top=12))
        elif args.cmd == "niches":
            for n in sv.niches:
                print(f"{n.id:22} {n.label:32} {len(n.queries):>2} requêtes  {len(n.models):>2} modèles  risque={n.authenticity_risk}")
        print(f"\nCoût IA aujourd'hui: {await sv.db.ai_spend_today():.4f}$ · requêtes Vinted: {sv.vinted.requests_made}")
    finally:
        await sv.close()


def main() -> None:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="vinted_hunter")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("bot")
    h = sub.add_parser("hunt")
    h.add_argument("query")
    h.add_argument("--max", type=float)
    h.add_argument("--niche")
    h.add_argument("--profit", type=float)
    sub.add_parser("price").add_argument("query")
    sub.add_parser("analyze").add_argument("url")
    sub.add_parser("niches")
    sub.add_parser("harvest").add_argument("--minutes", type=float, default=60)
    st = sub.add_parser("study")
    st.add_argument("--days", type=int, default=7)
    st.add_argument("--json")
    args = parser.parse_args()

    settings = get_settings()
    bot_mode = args.cmd in (None, "bot")
    # The bot logs to a file next to its database (it usually runs in the background, without a console).
    setup_logging(settings.log_level, settings.db_path.parent / "bot.log" if bot_mode else None)
    if bot_mode:
        if not settings.telegram_token:
            sys.exit("TELEGRAM_TOKEN manquant dans .env (voir .env.example)")
        if not acquire_single_instance():
            logging.getLogger(__name__).warning("Vinted Hunter tourne déjà sur cette machine : arrêt de cette copie.")
            sys.exit(0)
        from .bot.app import run

        run(settings)
    else:
        asyncio.run(_cli(args))


if __name__ == "__main__":
    main()
