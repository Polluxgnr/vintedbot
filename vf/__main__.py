"""Point d'entrée CLI : `py -m vf` (--gui, --q, --url, --category)."""

import argparse
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass(slots=True)
class VfRunConfig:
    url: str = ""
    q: str = ""
    name: str = ""
    platforms: str = ""
    category: str = ""
    ai: bool = False
    headless: bool = True
    gui: bool = False
    auto_platforms: bool = True
    price_min: int = 0
    price_max: int = 0


def _parse_args(argv: Optional[list[str]] = None) -> VfRunConfig:
    p = argparse.ArgumentParser(prog="vf", description="VF Finder — arbitrage multi-plateformes")
    p.add_argument("--url", default="", help="Vinted catalog URL")
    p.add_argument("--q", default="", help="Search text (multi-platform catalog fetch)")
    p.add_argument(
        "--platforms",
        default="",
        help="Comma-separated: vinted,leboncoin,wallapop,rakuten,cashexpress,ebay (désactive auto si fourni sans --auto-platforms)",
    )
    p.add_argument(
        "--category",
        default="",
        help="Market category hint (e.g. console_ds, projector, apparel)",
    )
    p.add_argument(
        "--auto-platforms",
        action="store_true",
        help="Sélection automatique des plateformes selon la catégorie (défaut si --platforms absent)",
    )
    p.add_argument(
        "--no-auto-platforms",
        action="store_true",
        help="Désactive la sélection auto — utilise --platforms ou le défaut VF_PLATFORMS",
    )
    p.add_argument("--name", default="", help="Run/search name (optional)")
    p.add_argument("--price-min", type=int, default=0, help="Prix minimum (scrape + filtre résultats)")
    p.add_argument("--price-max", type=int, default=0, help="Prix maximum (scrape + filtre résultats)")
    p.add_argument("--ai", action="store_true", help="Enable LLM features (OpenRouter key required)")
    p.add_argument(
        "--headed",
        action="store_true",
        help="All browsers visible (default: hybrid — eBay/LBC visible, rest headless)",
    )
    p.add_argument("--gui", action="store_true", help="Run the VF GUI")
    args = p.parse_args(argv)
    if not bool(args.gui) and not (args.url or args.q):
        p.error("one of --url or --q is required (unless --gui is used)")
    use_ai = bool(args.ai)
    if bool(args.gui) and not use_ai:
        if os.environ.get("VF_NO_AI", "").strip() not in ("1", "true", "yes"):
            use_ai = True
    auto_plat = True
    if bool(args.no_auto_platforms):
        auto_plat = False
    elif bool(args.auto_platforms):
        auto_plat = True
    elif str(args.platforms or "").strip():
        auto_plat = False
    return VfRunConfig(
        url=str(args.url or ""),
        q=str(args.q or ""),
        name=str(args.name or "").strip(),
        platforms=str(args.platforms or "").strip(),
        category=str(args.category or "").strip(),
        ai=use_ai,
        headless=not bool(args.headed),
        gui=bool(args.gui),
        auto_platforms=auto_plat,
        price_min=int(args.price_min or 0),
        price_max=int(args.price_max or 0),
    )


def main(argv: Optional[list[str]] = None) -> int:
    from .engine import setup_logging

    setup_logging(verbose=os.environ.get("VF_VERBOSE", "").strip() in ("1", "true", "yes"))
    cfg = _parse_args(argv)

    if cfg.gui or os.environ.get("VF_GUI_MODE", "").strip() in ("1", "true", "yes"):
        os.environ["VF_GUI_MODE"] = "1"
        try:
            from vf_scrape.config import apply_scrape_profile

            apply_scrape_profile("pro")
        except Exception:
            pass

    plats: Optional[List[str]] = None
    if cfg.platforms:
        plats = [p.strip().lower() for p in cfg.platforms.split(",") if p.strip()]

    if cfg.q and not cfg.url:
        from .market import build_catalog_url

        cfg.url = build_catalog_url(search_text=cfg.q)
        if not cfg.name:
            cfg.name = cfg.q

    if cfg.gui:
        from .app import launch

        launch(start_url=cfg.url, use_ai=cfg.ai, headless=cfg.headless)
        return 0

    from .pipeline import run_url

    out = run_url(
        cfg.url,
        name=cfg.name or "",
        headless=cfg.headless,
        use_ai=cfg.ai,
        platforms=plats,
        market_category_hint=cfg.category,
        auto_platforms=cfg.auto_platforms,
        price_min=cfg.price_min if cfg.price_min > 0 else None,
        price_max=cfg.price_max if cfg.price_max > 0 else None,
    )
    if out.folder:
        print(out.folder)
    if out.eval_export_paths.get("eval_summary"):
        print(f"eval: {out.eval_export_paths['eval_summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
