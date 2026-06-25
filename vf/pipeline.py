"""Orchestration du pipeline VF : scrape, triage, scoring, top deals et export."""

import csv
import json
import logging
import os
import shutil
import time
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .scoring import (
    apply_deal_caps,
    compute_market_stats,
    detect_accessory,
    extract_specs,
    infer_category,
    quality_audit_from_row,
    score_item_buy_mode,
)
from .engine import (
    AI_SKIP_FILTERS_SUGGEST,
    apply_category_spec_scoring,
    audit_category_row,
    confirmed_screen_defect,
    display_rank_score,
    normalize_run_rank_scores,
    promote_doubtful_listings,
    demote_strict_intent_mismatches,
    can_promote_doubtful_row,
    rank_score_for_sort,
    tag_market_quality,
    CATALOG_MAX_ITEMS,
    DEAL_SCORE_THRESHOLD,
    DEEP_FETCH_ALL_DOUBTFUL,
    DEEP_FETCH_MAX_CONSEC_FAIL,
    TOP_DEEP_K,
    TOP_IMAGE_K,
    TOP_VISION_K,
    QUALITY_TIER_MAX,
    TRIAGE_DOUBTFUL_MAX,
    TitleDecision,
    RunEstimator,
    discount_vs_new_pct,
    apply_accessory_result,
    enrich_pack_fields,
    estimate_msrp_eur,
    get_weights,
    intent_display_label,
    parse_search_intent,
    SearchIntent,
    resolve_item_category,
    resolve_effective_category,
    run_title_triage,
    triage_full_text,
)
from .market import (
    CatalogItem,
    VintedAdapter,
    build_catalog_url,
    download_image_url,
    get_adapter,
    normalize_search_name,
    normalize_vinted_catalog_params,
    params_from_url,
)
from .export import MarketReport, build_market_report

logger = logging.getLogger("vf.pipeline")


@dataclass(slots=True)
class VfOutput:
    folder: str = ""
    items: List[CatalogItem] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    deals_top: List[Dict[str, Any]] = field(default_factory=list)
    top_picks: List[Dict[str, Any]] = field(default_factory=list)
    top_picks_note: str = ""
    report: Optional[MarketReport] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    search_intent: str = ""
    dynamic_filters: List[Dict[str, Any]] = field(default_factory=list)
    market_category: str = ""
    filter_agent_note: str = ""
    rejected_rows: List[Dict[str, Any]] = field(default_factory=list)
    doubtful_rows: List[Dict[str, Any]] = field(default_factory=list)
    scraped_count: int = 0
    after_triage_count: int = 0
    search_plan: Optional[Dict[str, Any]] = None
    category_tier: int = 0
    category_schema: Optional[Dict[str, Any]] = None
    platform_analytics: Optional[Dict[str, Any]] = None
    scrape_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    platform_selection: Optional[Dict[str, Any]] = None
    eval_export_paths: Dict[str, str] = field(default_factory=dict)
    top5_analysis: Optional[Dict[str, Any]] = None
    top5_paths: Dict[str, str] = field(default_factory=dict)
    run_duration_s: float = 0.0
    category_brain: Optional[Dict[str, Any]] = None
    criteria_plan: Optional[Dict[str, Any]] = None
    perf_summary: Dict[str, Any] = field(default_factory=dict)
    pre_filter_defaults: Dict[str, str] = field(default_factory=dict)

    @property
    def has_results(self) -> bool:
        return bool(self.rows)


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def _base_runs_dir() -> Path:
    # Keep repo root clean: everything goes under brouillon/data/.
    return Path("brouillon") / "data" / "vf_runs"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def _cleanup_keep_latest_run(runs_dir: Path, *, current_folder: Path) -> None:
    try:
        if not runs_dir.exists():
            return
        cur = current_folder.resolve()
        for p in runs_dir.iterdir():
            if not p.is_dir():
                continue
            try:
                if p.resolve() == cur:
                    continue
            except Exception:
                # if resolve fails, still attempt deletion as long as it's not the current path string
                if os.fspath(p) == os.fspath(current_folder):
                    continue
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        # never fail the run because cleanup failed
        return


def _export_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    _ensure_dir(path.parent)
    # stable field order: known-first then extra keys
    fields: List[str] = []
    preferred = [
        "id",
        "title",
        "brand",
        "price",
        "total_price",
        "currency",
        "condition",
        "url",
        "final_score",
        "score_composite",
        "deal_tier",
        "analysis_detail",
        "dropship_risk_score",
        "comparison_insight",
        "market_discount_pct",
        "ref_price_refurb_eur",
        "ref_price_new_eur",
        "ref_price_stockx_eur",
        "oracle_arbiter_ref_eur",
        "oracle_arbiter_kind",
        "oracle_degraded",
        "oracle_allowed_sources",
        "discount_vs_refurb_pct",
        "discount_vs_retail_new_pct",
        "discount_vs_stockx_pct",
        "stockx_url",
        "idealo_url",
        "price_reference_sources",
        "is_accessory",
        "item_type",
        "accessory_kind",
        "reason",
    ]
    for f in preferred:
        fields.append(f)
    for r in rows:
        for k in r.keys():
            if k not in fields and not str(k).startswith("_"):
                fields.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_search_plan(
    plan: Any,
    *,
    headless: bool = True,
    use_ai: bool = False,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> VfOutput:
    """Multi-variant search (aliases Vinted) puis pipeline standard."""
    from .market import SearchPlan

    if not isinstance(plan, SearchPlan):
        out = VfOutput()
        out.errors.append("SearchPlan invalide")
        return out
    primary = next((v for v in plan.variants if v.platform == "vinted" and v.url), None)
    if not primary:
        primary = next((v for v in plan.variants if v.url), None)
    if not primary and plan.variants:
        primary = plan.variants[0]
    if not primary:
        out = VfOutput()
        out.errors.append("Aucune variante exécutable dans le plan")
        return out
    entry_url = primary.url or build_catalog_url(search_text=plan.canonical_query)
    plats = sorted({v.platform.lower() for v in plan.variants})
    return run_url(
        entry_url,
        name=plan.canonical_query,
        search_intent=plan.canonical_query,
        market_category_hint=plan.category_hint,
        headless=headless,
        use_ai=use_ai,
        log_fn=log_fn,
        progress_fn=progress_fn,
        search_plan=plan,
        platforms=plats,
    )


def run_url(
    url: str,
    *,
    name: str = "",
    headless: bool = True,
    use_ai: bool = False,
    search_intent: str = "",
    market_category_hint: str = "",
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
    search_plan: Any = None,
    platforms: Optional[List[str]] = None,
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    auto_platforms: bool = True,
) -> VfOutput:
    """
    VF run (URL mode).

    For now, the VF guarantees:
    - robust session bootstrap (HTTP then browser fallback),
    - catalog fetch with rate limiting,
    - CSV export in its own run folder,
    - placeholders for scoring & smart filtering (next step).
    """
    from .engine import current_run, event, finish_run, setup_logging, start_run

    setup_logging(verbose=os.environ.get("VF_VERBOSE", "").strip() in ("1", "true", "yes"))
    try:
        from vf_scrape.config import apply_scrape_profile, is_gui_mode

        if is_gui_mode() or os.environ.get("VF_SCRAPE_MODE"):
            apply_scrape_profile()
    except Exception:
        pass

    estimator = RunEstimator(use_ai=use_ai)
    from .ai import reset_cost_tracker

    reset_cost_tracker()

    def _emit(step: int, step_name: str, human: str = "", **extra: Any) -> None:
        payload = estimator.progress_payload(step=step, step_name=step_name, human=human, **extra)
        if progress_fn:
            try:
                progress_fn(payload)
            except Exception:
                pass

    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass
        else:
            lvl = "warn" if msg.strip().startswith("⚠") else "error" if msg.strip().startswith("✗") else "info"
            try:
                event("pipeline", msg, level=lvl)
            except Exception:
                logger.info(msg)

    from .market import resolve_vinted_catalog_url
    from .engine import PlatformSelection, format_selection_log
    from .market import SearchPlan, build_search_plan, default_platforms, detect_input_platform, resolve_run_platforms

    out = VfOutput()
    run_t0 = time.monotonic()
    url_raw = (url or "").strip()
    is_http = url_raw.startswith(("http://", "https://"))
    manual_plats = [p.lower() for p in platforms] if platforms else None
    if auto_platforms:
        plats, sel_meta = resolve_run_platforms(
            (search_intent or name or url_raw).strip(),
            (market_category_hint or "").strip(),
            manual=None,
            auto=True,
        )
        out.platform_selection = sel_meta
    else:
        plats = manual_plats or default_platforms()
    plat_url = detect_input_platform(url_raw) if is_http else None
    url_note = ""

    import os as _os_price

    def _env_int(key: str) -> Optional[int]:
        raw = (_os_price.environ.get(key) or "").strip()
        if not raw:
            return None
        try:
            v = int(float(raw))
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    def _param_price(v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        try:
            n = int(float(str(v).replace(",", ".")))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    eff_pmin = price_min if price_min and price_min > 0 else _env_int("VF_PRICE_MIN")
    eff_pmax = price_max if price_max and price_max > 0 else _env_int("VF_PRICE_MAX")
    if eff_pmin is None or eff_pmax is None:
        try:
            from .market import suggest_price_bounds

            sug_min, sug_max = suggest_price_bounds(
                (market_category_hint or "").strip(),
                url_raw if not is_http else (search_intent or url_raw),
            )
            if eff_pmin is None and sug_min:
                eff_pmin = sug_min
            if eff_pmax is None and sug_max:
                eff_pmax = sug_max
        except Exception:
            pass

    if search_plan is None and not is_http and url_raw:
        search_plan = build_search_plan(
            url_raw,
            platforms=plats,
            category_hint=(market_category_hint or "").strip(),
            price_min=eff_pmin,
            price_max=eff_pmax,
        )
        url = build_catalog_url(search_text=url_raw)
        intent = (search_intent or name or url_raw).strip()
    elif plat_url == "vinted" or (is_http and "vinted" in url_raw.lower()):
        url, url_note = resolve_vinted_catalog_url(url_raw)
        intent = (search_intent or name or "").strip()
        if search_plan is None and len(plats) > 1:
            params_pre = params_from_url(url)
            st_pre = str(params_pre.get("search_text") or intent).strip()
            if st_pre:
                search_plan = build_search_plan(
                    st_pre,
                    platforms=plats,
                    category_hint=(market_category_hint or "").strip(),
                    price_min=eff_pmin,
                    price_max=eff_pmax,
                )
    elif plat_url in ("leboncoin", "wallapop", "rakuten", "cashexpress", "ebay"):
        url = url_raw
        intent = (search_intent or name or "").strip()
        if search_plan is None:
            ad0 = get_adapter(plat_url, headless=headless)
            p0 = ad0.parse_params(url_raw)
            q0 = str(p0.get("search_text") or intent).strip()
            if q0:
                search_plan = build_search_plan(
                    q0,
                    platforms=[plat_url] if plat_url not in plats else plats,
                    category_hint=(market_category_hint or "").strip(),
                    price_min=eff_pmin,
                    price_max=eff_pmax,
                )
                intent = q0
    else:
        url, url_note = resolve_vinted_catalog_url(url_raw) if is_http else url_raw
        intent = (search_intent or name or url_raw).strip()

    if os.environ.get("VF_ORACLE", "").strip() == "":
        os.environ["VF_ORACLE"] = "1"
    if headless and os.environ.get("VF_ORACLE_BROWSER", "").strip() == "":
        os.environ["VF_ORACLE_BROWSER"] = "0"
        os.environ.setdefault("VF_ORACLE_BOOTSTRAP", "0")
        os.environ.setdefault("VF_ORACLE_DENICHEUR", "0")
        os.environ.setdefault("VF_ORACLE_IDEALO", "0")
        os.environ.setdefault("VF_ORACLE_BACKMARKET", "0")

    from .engine import adapter_headless, hybrid_mode_label

    if headless:
        _log(f"Navigateur : {hybrid_mode_label()}")

    out.search_intent = intent
    run_name = name.strip() or normalize_search_name(
        intent or params_from_url(url if is_http else build_catalog_url(search_text=url_raw)).get("search_text")
        or "vf_run"
    )
    runs_dir = _base_runs_dir()
    folder = runs_dir / f"vf_{normalize_search_name(run_name)}_{_timestamp()}"
    _ensure_dir(folder)
    _ensure_dir(folder / "images")
    out.folder = str(folder.resolve())
    start_run(folder.name)
    if out.platform_selection:
        try:
            ps = PlatformSelection(
                platforms=list(out.platform_selection.get("platforms") or plats),
                category=str(out.platform_selection.get("category") or ""),
                reason=str(out.platform_selection.get("reason") or ""),
                skipped=list(out.platform_selection.get("skipped") or []),
                optional=list(out.platform_selection.get("optional") or []),
                auto=bool(out.platform_selection.get("auto", True)),
            )
            _log(f"✓ {format_selection_log(ps)}")
        except Exception:
            _log(f"✓ Plateformes : {', '.join(plats)}")

    mp = VintedAdapter(headless=adapter_headless("vinted", run_headless=headless))
    all_catalog: List[CatalogItem] = []
    platform_adapters: Dict[str, Any] = {}
    try:
        needs_vinted = search_plan is None or not isinstance(search_plan, SearchPlan) or any(
            v.platform == "vinted" for v in search_plan.variants
        )
        if not needs_vinted and plat_url != "vinted":
            needs_vinted = "vinted" in plats and is_http and "vinted" in url_raw.lower()

        estimator.start_step("session")
        if needs_vinted:
            _log("1/6 Connexion à Vinted…")
            _emit(1, "Connexion", "Connexion sécurisée à Vinted (cookies / navigateur)…")
            mp.bootstrap(url, headless=adapter_headless("vinted", run_headless=headless))
            estimator.end_step("session")
            _log(f"✓ Session Vinted OK ({estimator.step_actuals.get('session', 0):.1f}s)")
        else:
            _log("1/6 Sessions marketplaces (hors Vinted)…")
            _emit(1, "Connexion", "Initialisation marketplaces (LBC, Wallapop, Rakuten, Cash Express…)…")
            booted: List[str] = []
            for plat in sorted({v.platform for v in search_plan.variants} if isinstance(search_plan, SearchPlan) else plats):
                if plat == "vinted":
                    continue
                try:
                    from .engine import CircuitOpenError, get_circuit

                    cb = get_circuit(plat)
                    cb.before_call()
                    h_plat = adapter_headless(plat, run_headless=headless)
                    ad_b = get_adapter(plat, headless=h_plat)
                    ad_b.bootstrap(url_raw or url, headless=h_plat)
                    cb.record_success()
                    booted.append(plat)
                except CircuitOpenError as exc:
                    _log(f"⚠ {plat} en pause (circuit ouvert) : {exc}")
                except Exception as exc:  # noqa: BLE001
                    try:
                        get_circuit(plat).record_failure()
                    except Exception:
                        pass
                    _log(f"⚠ Bootstrap {plat}: {exc}")
            estimator.end_step("session")
            _log(f"✓ Plateformes prêtes : {', '.join(booted) or '—'} ({estimator.step_actuals.get('session', 0):.1f}s)")
        if url_note and url != url_raw:
            _log(f"✓ {url_note}")
        if needs_vinted:
            params = mp.parse_params(url)
        elif plat_url in ("leboncoin", "wallapop", "rakuten", "cashexpress", "ebay"):
            params = get_adapter(plat_url, headless=headless).parse_params(url_raw or url)
        else:
            params = {"search_text": intent, "url": url_raw or url}
        # Affiche la fourchette effectivement utilisée (UI setup / env / URL).
        # Sur Vinted, l'URL d'entrée peut ne pas contenir price_from/to même si le run a une fourchette.
        _pf = params.get("price_from")
        _pt = params.get("price_to")
        _pf = _pf if _pf not in (None, "", "None") else eff_pmin
        _pt = _pt if _pt not in (None, "", "None") else eff_pmax
        _log(
            f"Params URL: search_text={params.get('search_text')!r} "
            f"catalog={params.get('catalog[]') or params.get('catalog') or params.get('catalog_ids[]')!r} "
            f"price_from={_pf!r} price_to={_pt!r}"
        )
        if eff_pmin is None:
            eff_pmin = _param_price(params.get("price_from") or params.get("price_min"))
        if eff_pmax is None:
            eff_pmax = _param_price(params.get("price_to") or params.get("price_max"))
        if search_plan is not None and (eff_pmin or eff_pmax):
            try:
                from .market import SearchPlan, build_search_plan

                if isinstance(search_plan, SearchPlan):
                    q_plan = str(search_plan.canonical_query or intent or params.get("search_text") or "").strip()
                    if q_plan:
                        search_plan = build_search_plan(
                            q_plan,
                            platforms=plats,
                            category_hint=(market_category_hint or "").strip(),
                            price_min=eff_pmin,
                            price_max=eff_pmax,
                        )
            except Exception:
                pass
        st_api = str(params.get("search_text") or "").strip()
        if not st_api and url_note:
            _log("⚠ URL catalogue incomplète (search_text absent) — collez le lien recherche Vinted complet")
        if not intent or "vinted." in intent.lower() or "session-refresh" in intent.lower():
            intent = st_api or intent
        if not intent:
            intent = str(params.get("search_text") or run_name)
        if needs_vinted:
            params = normalize_vinted_catalog_params(params)
        si = parse_search_intent(st_api or intent, params)
        try:
            import json as _json

            from .market import enrich_search_intent

            pf_raw = os.environ.get("VF_PREFLIGHT", "").strip()
            pf_map: Dict[str, bool] = {}
            if pf_raw:
                try:
                    pf_map = {k: bool(v) for k, v in (_json.loads(pf_raw) or {}).items()}
                except Exception:
                    pf_map = {}
            w_pf = get_weights()
            enrich_search_intent(
                si,
                query=st_api or intent,
                preflight=pf_map,
                buyer_priorities=w_pf.buyer_priorities or "",
            )
            if si.required_ds_family:
                _log(f"✓ Intent strict : famille DS « {si.required_ds_family} »")
        except Exception:
            pass
        intent_label = intent_display_label(si, params=params, fallback=run_name[:80] or "catalogue multi-plateforme")
        search_hint = st_api or intent_label
        out.search_intent = intent_label
        if needs_vinted and params.get("search_text") and not str(mp.parse_params(url).get("search_text") or "").strip():
            _log(f"Intent catalogue → search_text API : {params['search_text']!r}")
        _log(f"Intent : {intent_label!r} · familles {si.product_families or ['—']}")
        w0_early = get_weights()
        prio_early = (w0_early.buyer_priorities or "").strip()
        st_early = str(params.get("search_text") or "").strip()
        market_cat_early = resolve_effective_category(
            [],
            url,
            si=si,
            params=params,
            buyer_priorities="" if st_early else prio_early,
            run_name=run_name,
        )
        if (market_category_hint or "").strip() and market_cat_early in ("unknown", "generic", ""):
            market_cat_early = market_category_hint.strip()
        market_cat = market_cat_early
        if needs_vinted and mp is not None:
            platform_adapters["vinted"] = mp
        estimator.start_step("catalog")
        plat_labels = ", ".join(sorted({v.platform for v in search_plan.variants})) if isinstance(search_plan, SearchPlan) else ", ".join(plats)
        _log(f"2/6 Téléchargement du catalogue ({plat_labels or 'marketplaces'})…")
        _emit(2, "Catalogue", "Récupération des annonces (multi-plateforme)…")

        _plat_progress: Dict[str, int] = {}
        _plat_done: set[str] = set()
        _plat_t0: Dict[str, float] = {}

        def _on_plat_progress(plat: str, phase: str, count: int) -> None:
            pk = plat.lower().replace("lbc", "leboncoin")
            if phase == "done":
                _plat_progress[pk] = count
                _plat_done.add(pk)
            elif pk not in _plat_t0:
                _plat_t0[pk] = time.monotonic()
            plat_elapsed = {k: round(time.monotonic() - _plat_t0[k], 1) for k in _plat_t0}
            _emit(
                2,
                "Catalogue",
                f"{pk} — {count} annonces" if phase == "done" else f"{pk} : {phase[:40]}…",
                catalog_count=sum(_plat_progress.values()),
                platform_progress=dict(_plat_progress),
                platform_active=pk if phase != "done" else "",
                platform_done=sorted(_plat_done),
                platform_elapsed=plat_elapsed,
            )

        def _on_catalog_page(page: int, total_pages: int, collected: int) -> None:
            if page <= 1 or page % 2 == 0 or page >= total_pages:
                _log(f"  Catalogue page {page}/{total_pages} — {collected} annonces")
            _emit(
                2,
                "Catalogue",
                f"Catalogue — page {page}/{max(1, total_pages)} · {collected} annonces…",
                catalog_page=page,
                catalog_pages=max(1, total_pages),
                catalog_count=collected,
                platform_progress=dict(_plat_progress),
            )

        if search_plan is not None:
            from .market import SearchPlan, fetch_plan_catalog

            if isinstance(search_plan, SearchPlan):
                out.search_plan = search_plan.to_dict()
                estimator.set_scrape_plan(
                    search_plan.estimated_after_dedup,
                    len(search_plan.variants),
                    n_platforms=len(plats),
                    scrape_platforms=plats,
                )
                eta0 = int(estimator.planned_seconds())
                _log(
                    f"Plan recherche : {len(search_plan.variants)} requêtes · "
                    f"~{search_plan.estimated_after_dedup} annonces · ETA ~{eta0 // 60}:{eta0 % 60:02d}"
                )
                _emit(
                    2,
                    "Catalogue",
                    f"Plan {len(search_plan.variants)} requêtes · ~{search_plan.estimated_after_dedup} annonces",
                    catalog_count=search_plan.estimated_after_dedup,
                    planned_sec=eta0,
                )
                import os as _os

                _per_var = 0
                try:
                    from vf_scrape.config import platform_scrape_cap

                    _per_var = max(
                        platform_scrape_cap("vinted"),
                        platform_scrape_cap("leboncoin"),
                    )
                except Exception:
                    _per_var = 0
                _prefetch: Dict[str, Any] = {}
                if needs_vinted and mp is not None:
                    _prefetch["vinted"] = mp
                items, dupes, scrape_stats, platform_adapters = fetch_plan_catalog(
                    search_plan,
                    max_items_per_variant=_per_var,
                    headless=headless,
                    on_page=_on_catalog_page,
                    on_platform_progress=_on_plat_progress,
                    log_fn=_log,
                    prefetched_adapters=_prefetch or None,
                    keep_adapters_open=True,
                )
                out.scrape_stats = scrape_stats
                if eff_pmin or eff_pmax:
                    _log(f"✓ Fourchette prix scrape : {eff_pmin or '—'} – {eff_pmax or '—'} €")
                for plat_k, st in sorted(scrape_stats.items()):
                    raw_n = int(st.get("raw") or 0)
                    err_n = int(st.get("errors") or 0)
                    if err_n and raw_n == 0:
                        _log(f"⚠ {plat_k} : 0 annonce ({st.get('last_error', 'erreur')})")
                    elif raw_n:
                        _log(f"  · {plat_k} brut : {raw_n} ({st.get('variants', 0)} requêtes)")
                if dupes:
                    _log(f"✓ Dédup variantes : {dupes} doublons retirés")
            else:
                items = mp.fetch_catalog(url, max_items=CATALOG_MAX_ITEMS, on_page=_on_catalog_page)
        else:
            items = mp.fetch_catalog(url, max_items=CATALOG_MAX_ITEMS, on_page=_on_catalog_page)
        try:
            from .scoring import filter_stale_catalog_items

            lbc_max = int(os.environ.get("VF_LBC_MAX_AGE_DAYS", "365") or "365")
            items, stale_n = filter_stale_catalog_items(items, max_age_days=lbc_max)
            if stale_n:
                _log(f"✓ LeBonCoin : {stale_n} annonces > {lbc_max}j retirées")
        except Exception:
            pass
        try:
            from .engine import filter_hand_delivery_catalog_items

            items, lbc_hand = filter_hand_delivery_catalog_items(items)
            if lbc_hand:
                _log(f"✓ Livraison : {lbc_hand} annonces main propre uniquement retirées")
        except Exception:
            pass
        all_catalog = list(items)
        out.items = all_catalog
        estimator.set_catalog_count(len(items))
        estimator.set_work_count(len(items))
        from .export import export_listings_csv, normalize_catalog_items

        catalog_rows = normalize_catalog_items(
            items,
            search_intent=intent_label,
            market_category=market_cat_early,
        )
        spec_cache: Dict[str, Dict[str, Any]] = {}
        category_brain: Optional[Dict[str, Any]] = None
        try:
            from .engine import (
                analyze_catalog_sample,
                early_spec_filter_enabled,
                enrich_intent_from_query,
                smart_filter_defaults,
            )

            category_brain = analyze_catalog_sample(
                catalog_rows,
                category=market_cat_early or si.category_hint or "generic",
                query=search_hint,
            )
            out.category_brain = category_brain
            intent_enriched = enrich_intent_from_query(search_hint, market_cat_early or "generic")
            if intent_enriched.get("ps4_variant") or intent_enriched.get("ps4_storage"):
                _log(
                    f"✓ Intent enrichi : "
                    f"{intent_enriched.get('ps4_variant', '')} "
                    f"{intent_enriched.get('ps4_storage', '')} Go".strip()
                )
            out.pre_filter_defaults = smart_filter_defaults(
                market_cat_early or "generic",
                search_hint,
                category_brain,
            )
            if si.required_ds_family:
                out.pre_filter_defaults["ds_model_family"] = si.required_ds_family
                out.pre_filter_defaults["intent_exact_only"] = "on"
            if si.required_phone_model_key:
                out.pre_filter_defaults["intent_exact_only"] = "on"
            try:
                (folder / "category_brain.json").write_text(
                    json.dumps(category_brain, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
        except Exception as exc:
            logger.debug("category_brain: %s", exc)
        export_listings_csv(folder / "catalog_unified.csv", catalog_rows)
        _export_csv(folder / "catalog_all.csv", [it.to_row() for it in items])
        estimator.end_step("catalog")
        _emit(
            2,
            "Catalogue",
            f"{len(items)} annonces — estimation mise à jour (~{int(estimator.forecast_total_sec(step=2))}s total)",
            catalog_count=len(items),
        )
        _log(f"✓ Catalogue : {len(items)} annonces ({estimator.step_actuals.get('catalog', 0):.1f}s)")

        # Phase 1 — Triage titre (heuristique, 0 coût API)
        estimator.start_step("triage")
        _emit(3, "Triage titres", "Filtrage strict sur les titres (accessoires, HS, hors-sujet)…")
        before_triage = len(items)
        triage = run_title_triage(items, si)
        out.after_triage_count = len(triage.working_ids)
        items = [it for it in items if str(it.id) in triage.working_ids]
        rejected = before_triage - len(items)
        _log(
            f"✓ Triage titre : {before_triage} → {len(items)} "
            f"(rejetés {rejected}, douteux {len(triage.doubtful_ids)})"
        )

        # Phase 1a — Filtre specs précoce (avant cascade IA, économie tokens)
        try:
            from .engine import early_spec_filter_enabled

            if early_spec_filter_enabled():
                from .scoring import apply_early_spec_filters

                before_spec = len(items)
                items, spec_dropped, _spec_reasons = apply_early_spec_filters(
                    items,
                    category=market_cat_early or si.category_hint or "",
                    intent=si,
                )
                if spec_dropped:
                    _log(
                        f"✓ Filtre specs précoce : {before_spec} → {len(items)} "
                        f"(-{spec_dropped} hors-catégorie / accessoires évidents)"
                    )
        except Exception as exc:
            logger.debug("early spec filter: %s", exc)

        if use_ai and triage.doubtful_ids:
            from .ai import clarify_doubtful_titles
            from .engine import TitleTriage

            doubtful_batch = [it for it in items if str(it.id) in triage.doubtful_ids][:36]
            if doubtful_batch:
                clar = clarify_doubtful_titles(
                    search_hint,
                    doubtful_batch,
                    triage.by_id,
                    market_category=market_cat_early,
                )
                promoted = demoted = 0
                for it in doubtful_batch:
                    sid = str(it.id)
                    c = clar.get(sid)
                    if not c:
                        continue
                    dec = c.get("decision")
                    reason_fr = str(c.get("reason") or "").strip()
                    if dec == "keep":
                        triage.doubtful_ids.discard(sid)
                        triage.keep_ids.add(sid)
                        reasons = [reason_fr or "ia_keep"]
                        triage.by_id[sid] = TitleTriage(TitleDecision.KEEP, reasons, 0.78)
                        promoted += 1
                    elif dec == "reject":
                        triage.doubtful_ids.discard(sid)
                        triage.keep_ids.discard(sid)
                        triage.reject_ids.add(sid)
                        items = [x for x in items if str(x.id) != sid]
                        triage.by_id[sid] = TitleTriage(
                            TitleDecision.REJECT,
                            [reason_fr or "ia_reject"],
                            0.82,
                        )
                        demoted += 1
                    elif reason_fr:
                        tr = triage.by_id.get(sid)
                        base = list(getattr(tr, "reasons", []) or []) if tr else []
                        triage.by_id[sid] = TitleTriage(
                            TitleDecision.DOUBTFUL,
                            base + [reason_fr],
                            float(getattr(tr, "confidence", 0.55) if tr else 0.55),
                        )
                if promoted or demoted:
                    _log(
                        f"✓ IA doutes : {promoted} réintégrées, {demoted} écartées "
                        f"({len(triage.doubtful_ids)} doutes restants)"
                    )
        estimator.end_step("triage")
        _emit(
            3,
            "Triage titres",
            f"{before_triage} → {len(items)} annonces · {len(triage.doubtful_ids)} à clarifier",
            ai_before=before_triage,
            ai_after=len(items),
        )

        # Phase 1b — IA titres : douteux seulement (défaut) ; cascade complète si VF_TITLE_AI=cascade
        title_rel_map: Dict[str, int] = {}
        from .ai import CASCADE_ENABLED, filter_titles_cascade

        title_ai_mode = os.environ.get("VF_TITLE_AI", "smart").strip().lower()
        use_title_cascade = use_ai and CASCADE_ENABLED and title_ai_mode in ("cascade", "smart")

        if use_title_cascade:
            from .ai import partition_for_cascade

            auto_keep, needs_llm, conf_map = partition_for_cascade(
                items,
                triage_by_id=triage.by_id,
                search_intent=search_hint,
                market_category=market_cat_early,
            )
            estimator.start_step("ai_title")
            _emit(
                3,
                "Filtre IA titres",
                f"Analyse IA sur {len(needs_llm)} annonces ({len(auto_keep)} bypass confiance)…",
                ai_before=len(items),
                ai_doubtful_total=len(needs_llm),
                ai_doubtful_done=0,
            )
            keep_ia: set[str] = {str(it.id) for it in auto_keep}
            rel_partial: Dict[str, int] = {}
            keep_rate = len(auto_keep) / max(1, len(items))
            cascade_skip_thr = float(os.environ.get("VF_CASCADE_SKIP_KEEP_RATE", "0.90"))
            if needs_llm and keep_rate >= cascade_skip_thr:
                keep_ia |= {str(it.id) for it in needs_llm}
                _log(
                    f"✓ Cascade skip LLM ({keep_rate:.0%} bypass) — "
                    f"{len(needs_llm)} titres gardes par regles"
                )
            elif needs_llm:
                title_sample = [
                    str(getattr(it, "title", "") or "")[:80]
                    for it in items[:48]
                    if str(getattr(it, "title", "") or "").strip()
                ]
                keep_llm, title_rel_map = filter_titles_cascade(
                    search_hint,
                    needs_llm,
                    market_category=market_cat_early,
                    buyer_priorities=prio_early,
                    max_items=len(needs_llm),
                    market_sample=title_sample,
                )
                keep_ia |= keep_llm
                rel_partial = title_rel_map
            for sid, conf in conf_map.items():
                if sid in keep_ia and sid not in rel_partial:
                    rel_partial[sid] = min(100, int(70 + conf * 30))
            title_rel_map = rel_partial
            before_ia = len(items)
            items = [it for it in items if str(it.id) in keep_ia]
            _log(
                f"✓ Cascade titres IA : {before_ia} → {len(items)} pertinentes "
                f"({len(auto_keep)} bypass confiance, {len(needs_llm)} passés LLM)"
            )
            estimator.set_ai_title_chunks(max(1, (before_ia + 47) // 48))
            estimator.end_step("ai_title")
            _emit(
                3,
                "Filtre IA titres",
                f"{before_ia} → {len(items)} annonces retenues",
                ai_after=len(items),
                ai_doubtful_done=before_ia,
                ai_doubtful_total=before_ia,
            )
        elif use_ai and triage.doubtful_ids and title_ai_mode not in ("off", "smart", "cascade"):
            from .ai import resolve_doubtful_titles

            doubtful_items = [
                it for it in all_catalog if str(it.id) in triage.doubtful_ids
            ][:TRIAGE_DOUBTFUL_MAX]
            estimator.start_step("ai_title")
            _emit(
                3,
                "IA douteux",
                f"Vérification IA de {len(doubtful_items)} titres ambigus…",
                ai_doubtful_total=len(doubtful_items),
                ai_doubtful_done=0,
            )
            keep_doubtful = resolve_doubtful_titles(search_hint, doubtful_items)
            _emit(
                3,
                "IA douteux",
                f"IA terminée — {len(keep_doubtful)} titres confirmés",
                ai_doubtful_total=len(doubtful_items),
                ai_doubtful_done=len(doubtful_items),
            )
            drop = triage.doubtful_ids - keep_doubtful
            if drop:
                items = [it for it in items if str(it.id) not in drop]
                _log(f"✓ IA douteux : {len(drop)} exclus, {len(keep_doubtful)} confirmés")
            estimator.set_ai_title_chunks(max(1, (len(doubtful_items) + 23) // 24))
            estimator.end_step("ai_title")
            _emit(
                3,
                "IA douteux",
                f"Clarification terminée — {len(items)} annonces retenues",
                ai_after=len(items),
            )

        estimator.set_work_count(len(items))

        # Phase 1c — Découverte IA des critères marché (1 appel max, caché) + excludes early.
        criteria_plan: Optional[Dict[str, Any]] = None
        compiled_plan_patterns: List[Tuple[str, Any, str]] = []
        try:
            from .engine import discover_criteria_plan, plan_early_exclude_match

            # Sample rows: normalized catalog rows for current items if possible.
            id_set_now = {str(getattr(it, "id", "") or "") for it in items}
            sample_rows = [r for r in (catalog_rows or []) if str(r.get("id") or "") in id_set_now][:60]
            if not sample_rows:
                sample_rows = list(catalog_rows or [])[:60]

            w0_early = get_weights()
            prio_early = (w0_early.buyer_priorities or "").strip()
            res = discover_criteria_plan(
                query=search_hint,
                category=market_cat_early or market_cat or "generic",
                sample_rows=sample_rows[:50],
                buyer_priorities=prio_early,
            )
            criteria_plan = res.plan if isinstance(res.plan, dict) else None
            if criteria_plan:
                out.criteria_plan = dict(criteria_plan)
                try:
                    (folder / "criteria_plan.json").write_text(
                        json.dumps(criteria_plan, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

                # Early excludes on items (pre-scoring / pre-LLM enrichment).
                before = len(items)
                kept: List[CatalogItem] = []
                dropped = 0
                for it in items:
                    blob = f"{getattr(it, 'title', '') or ''} {getattr(it, 'description', '') or ''}".strip()
                    hit = plan_early_exclude_match(blob, criteria_plan)
                    if hit:
                        dropped += 1
                        continue
                    kept.append(it)
                if dropped:
                    items = kept
                    _log(f"✓ Critères marché : {dropped} hors-sujets exclus (early_excludes)")
                    if before != len(items):
                        estimator.set_work_count(len(items))

                # Compile spec regex patterns once (applied per-row later; no extra LLM).
                try:
                    import re as _re

                    for sp in (criteria_plan.get("spec_patterns") or [])[:40]:
                        if not isinstance(sp, dict):
                            continue
                        field = str(sp.get("field") or "").strip()
                        rx = str(sp.get("regex") or "").strip()
                        vtype = str(sp.get("value_type") or "number").strip().lower()
                        if not field:
                            continue
                        if not rx:
                            continue
                        try:
                            pat = _re.compile(rx, flags=_re.I)
                        except _re.error:
                            pat = _re.compile(_re.escape(rx), flags=_re.I)
                        compiled_plan_patterns.append((field, pat, vtype))
                except Exception:
                    compiled_plan_patterns = []
        except Exception as exc:  # noqa: BLE001
            # criteria_plan.json is part of the run contract; don't silently skip it.
            _log(f"⚠ Criteria plan : {exc}")
            try:
                criteria_plan = {
                    "version": 1,
                    "category": str(market_cat_early or market_cat or "generic"),
                    "label": str(market_cat_early or market_cat or "generic"),
                    "agent_note": "Plan indisponible (fallback).",
                    "spec_patterns": [],
                    "early_excludes": [],
                    "scoring_hints": {},
                    "ui_filters": [],
                }
                out.criteria_plan = dict(criteria_plan)
                try:
                    (folder / "criteria_plan.json").write_text(
                        json.dumps(criteria_plan, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            except Exception:
                pass

        # Phase 1: gating + preliminary scoring (buy mode)
        # Market baseline from full scraped catalog (pre-cascade) — avoids skewed median.
        market_prices: List[float] = []
        for it in all_catalog:
            acc0 = detect_accessory(it, search_hint=search_hint, market_category=market_cat_early)
            if not acc0.is_accessory:
                p0 = it.total_or_price
                if p0 is not None and p0 > 0:
                    try:
                        from .scoring import calculate_landed_cost

                        landed = calculate_landed_cost(
                            float(p0),
                            str(getattr(it, "platform", None) or "vinted"),
                            row={"title": getattr(it, "title", "") or ""},
                        )
                        market_prices.append(float(landed["landed_cost_eur"]))
                    except Exception:
                        market_prices.append(float(p0))
        stats = compute_market_stats(market_prices)

        enriched: List[Dict[str, Any]] = []
        enriched_main: List[Dict[str, Any]] = []
        top5_data: Optional[Dict[str, Any]] = None
        acc_cache: Dict[int, Any] = {}
        excluded_acc = 0
        t_gate = _time.monotonic()
        for it in items:
            acc = detect_accessory(it, search_hint=search_hint, market_category=market_cat)
            acc_cache[it.id] = acc
            if acc.is_accessory:
                excluded_acc += 1
        _log(
            f"3/5 Scoring ({len(items) - excluded_acc} principales, {excluded_acc} accessoires exclus) "
            f"(gate {_time.monotonic() - t_gate:.1f}s)…"
        )
        estimator.start_step("scoring")
        _emit(
            4,
            "Scoring",
            f"Calcul des scores — {len(items) - excluded_acc} produits, {excluded_acc} accessoires repérés",
            main_items=len(items) - excluded_acc,
            excluded_accessories=excluded_acc,
        )
        n_scored = 0
        for it in items:
            acc = acc_cache.get(it.id) or detect_accessory(
                it, search_hint=search_hint, market_category=market_cat
            )
            from .export import normalize_listing_row

            row = normalize_listing_row(
                it.to_row(),
                search_intent=search_hint,
                market_category=market_cat,
            )
            apply_accessory_result(row, acc)
            try:
                from .engine import enrich_row_listing_fields

                enrich_row_listing_fields(row)
            except Exception:
                pass
            try:
                from .ai import compute_heuristic_confidence

                row["heuristic_confidence"] = compute_heuristic_confidence(
                    row, market_category=market_cat, search_intent=search_hint
                )
            except Exception:
                pass
            tr0 = triage.by_id.get(str(it.id))
            if tr0:
                row["triage_decision"] = tr0.decision.value
                row["triage_reasons"] = "|".join(tr0.reasons)[:120]
                row["triage_confidence"] = tr0.confidence
            if si.required_ds_family:
                row["required_ds_family"] = si.required_ds_family
            if si.required_brand:
                row["required_brand"] = si.required_brand
            if si.apparel_size:
                row["required_apparel_size"] = si.apparel_size
            if si.required_phone_model_key:
                row["required_phone_model_key"] = si.required_phone_model_key
                row["search_model_key"] = si.required_phone_model_key
            if si.required_storage_gb:
                row["required_storage_gb"] = si.required_storage_gb
            if si.required_exclude_pro:
                row["required_exclude_pro"] = True
            row["image_path"] = ""
            inferred = infer_category(it)
            item_cat = resolve_item_category(inferred, si)
            row["item_type"] = item_cat
            row["item_type_conf"] = inferred.confidence
            row["category_signals"] = inferred.signals
            row.update(extract_specs(it, item_cat))
            # Criteria plan per-run extraction (regex patterns) — no extra LLM calls.
            if compiled_plan_patterns:
                blob = f"{row.get('title') or ''} {row.get('description') or row.get('description_excerpt') or ''}"
                for field, pat, vtype in compiled_plan_patterns:
                    if row.get(field) not in (None, "", False):
                        continue
                    m = pat.search(blob)
                    if not m:
                        continue
                    val = m.group(1) if m.lastindex else m.group(0)
                    try:
                        if vtype in ("int",):
                            row[field] = int(str(val).replace(",", ".").strip())
                        elif vtype in ("float", "number"):
                            s = str(val).replace(",", ".").strip()
                            row[field] = float(s) if "." in s else int(s)
                        elif vtype == "bool":
                            row[field] = True
                        else:
                            row[field] = str(val).strip()[:80]
                    except Exception:
                        continue
            sid_cache = str(it.id)
            if sid_cache:
                spec_cache[sid_cache] = {
                    k: row[k]
                    for k in row
                    if k in (
                        "storage_gb", "lumens", "variant", "listing_type", "has_controller",
                        "controller_count", "games_count", "projector_brand", "resolution",
                        "wheel_size", "frame_size", "bike_brand", "product_model_key",
                        "battery_health_pct", "is_mini_projector", "is_screen_only",
                    )
                    or str(k).startswith("is_")
                }
            try:
                from .engine import enrich_listing_taxonomy

                enrich_listing_taxonomy(row)
            except Exception:
                pass
            enrich_pack_fields(row)
            audit_category_row(row)
            try:
                from .scoring import effective_price_eur

                eff_price = effective_price_eur(row)
            except Exception:
                eff_price = None
            score = score_item_buy_mode(
                it,
                stats=stats,
                search_hint=search_hint,
                weights=get_weights(),
                effective_price=eff_price,
            )
            row.update(score)
            row["score_base"] = float(row.get("final_score") or 0.0)
            row["score_overlay"] = 0.0
            try:
                from .scoring import apply_seller_trust_to_row

                apply_seller_trust_to_row(row)
            except Exception:
                pass
            sid0 = str(it.id)
            if title_rel_map.get(sid0):
                row["ai_title_relevance"] = title_rel_map[sid0]
                try:
                    boost = min(8.0, float(title_rel_map[sid0]) / 25.0)
                    row["score_overlay"] = float(row.get("score_overlay") or 0.0) + boost
                except (TypeError, ValueError):
                    pass
            row["rank_score"] = rank_score_for_sort(row)
            row["display_rank_score"] = row["rank_score"]
            msrp = estimate_msrp_eur(row)
            if msrp:
                row["msrp_eur"] = msrp
                dnew = discount_vs_new_pct(row)
                if dnew is not None:
                    row["discount_vs_new_pct"] = dnew
            enriched.append(row)
            is_doubt = str(row.get("triage_decision") or "") == "doubtful"
            if not acc.is_accessory and not is_doubt:
                enriched_main.append(row)
            n_scored += 1
            if n_scored % 80 == 0 or n_scored == len(items):
                _emit(
                    4,
                    "Scoring",
                    f"Scores… {n_scored}/{len(items)}",
                    score_done=n_scored,
                    score_total=len(items),
                )
        estimator.end_step("scoring")
        _log(f"✓ Scoring terminé ({len(enriched)} lignes) ({estimator.step_actuals.get('scoring', 0):.1f}s)")

        if category_brain and enriched_main:
            try:
                from .engine import enrich_rows_with_brain

                enrich_rows_with_brain(
                    enriched_main,
                    category=market_cat_early or market_cat or "generic",
                    brain=category_brain,
                    spec_cache=spec_cache,
                )
            except Exception as exc:
                logger.debug("enrich_rows_with_brain: %s", exc)

        enriched.sort(key=lambda r: float(r.get("rank_score") or r.get("final_score") or 0.0), reverse=True)
        main_ranked = sorted(
            [r for r in enriched if not r.get("is_accessory")],
            key=lambda r: float(r.get("rank_score") or r.get("final_score") or 0.0),
            reverse=True,
        )

        w0 = get_weights()
        prio = (w0.buyer_priorities or "").strip()
        st_lock = str(params.get("search_text") or "").strip()
        prio_for_market = "" if st_lock else prio
        market_cat = resolve_effective_category(
            [],
            url,
            si=si,
            params=params,
            buyer_priorities=prio_for_market,
            run_name=run_name,
        )
        hint = (market_category_hint or "").strip()
        if market_cat in ("unknown", "generic", "") and hint and hint not in ("unknown", "generic"):
            market_cat = hint
            _log(f"✓ Marché imposé par configuration : {market_cat}")
        elif market_cat in ("unknown", "generic", ""):
            from .engine import detect_market_context

            mc2 = str(detect_market_context(url, buyer_priorities=prio).get("category") or "")
            if mc2 and mc2 not in ("unknown", "generic"):
                market_cat = mc2
                _log(f"✓ Marché détecté depuis URL/priorités : {market_cat}")
        dom_cat = resolve_effective_category(
            enriched_main,
            url,
            si=si,
            params=params,
            buyer_priorities=prio_for_market,
            run_name=run_name,
        )
        if market_cat != dom_cat:
            _log(f"✓ Marché filtres: {market_cat} · scoring dominant: {dom_cat}")
        else:
            _log(f"✓ Catégorie effective : {dom_cat}")
        if dom_cat in ("unknown", "generic") and market_cat not in ("unknown", "generic", ""):
            dom_cat = market_cat
        out.market_category = market_cat
        try:
            from .engine import ensure_category_schema, resolve_category_tier

            out.category_tier = resolve_category_tier(market_cat)
            if out.category_tier >= 2:
                out.category_schema = ensure_category_schema(
                    market_cat,
                    search_intent=search_hint,
                    use_ai=use_ai,
                )
                _log(f"✓ Catégorie tier {out.category_tier} — schema {market_cat}")
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Schema catégorie: {exc}")
        st = str(params.get("search_text") or "")
        from .engine import build_filter_schema
        ai_fl: List[Dict[str, Any]] = []
        if use_ai and not AI_SKIP_FILTERS_SUGGEST:
            from .ai import agent_design_market_filters

            ai_fl, out.filter_agent_note = agent_design_market_filters(
                intent_label,
                market_category=market_cat,
                catalog_url=url,
                search_text=st,
                sample_rows=enriched_main[:40],
                buyer_priorities=prio,
            )
            _log(
                f"✓ Agent filtres ({market_cat}): {len(ai_fl)} IA — "
                f"{(out.filter_agent_note or '')[:80]}"
            )
        else:
            out.filter_agent_note = f"Filtres {market_cat or 'generic'} (sans agent IA)."

        # Merge criteria plan UI filters into schema (dynamic, category-agnostic).
        try:
            if isinstance(criteria_plan, dict) and criteria_plan.get("ui_filters"):
                ai_fl = list(ai_fl or []) + list(criteria_plan.get("ui_filters") or [])
        except Exception:
            pass
        out.dynamic_filters = build_filter_schema(
            market_cat or "generic",
            ai_filters=ai_fl,
            category_schema=out.category_schema,
        )
        _log(f"✓ Filtres UI : {len(out.dynamic_filters)} contrôles ({market_cat})")

        # Phase 2: sélection intelligente top-K puis enrichissement profond
        top_k = TOP_DEEP_K
        from .engine import apply_model_market_stats
        from .scoring import apply_composite_scores, build_market_context_for_scoring, select_top_for_enrichment

        try:
            apply_model_market_stats(enriched_main)
        except Exception:
            pass
        pro_ctx = build_market_context_for_scoring(enriched_main)
        apply_composite_scores(
            main_ranked,
            weights=get_weights(),
            market_ctx=pro_ctx,
            sync_quality_tier=False,
        )
        deep_targets = select_top_for_enrichment(
            main_ranked,
            limit=top_k,
            doubtful_ids=triage.doubtful_ids if DEEP_FETCH_ALL_DOUBTFUL else None,
            doubtful_cap=min(12, TRIAGE_DOUBTFUL_MAX // 6),
        )
        deep_pool = list(deep_targets)
        deep_ids = [str(r.get("id") or "") for r in deep_targets if r.get("id") is not None]

        estimator.start_step("deep_fetch")
        _log(
            f"5/6 Enrichissement top {len(deep_targets)} sélectionnés "
            f"(sur {len(main_ranked)} offres principales)…"
        )
        _emit(
            5,
            "Enrichissement",
            f"Analyse détaillée des {len(deep_targets)} meilleures offres…",
            top_k=len(deep_targets),
        )

        from .market import enrich_rows_deep

        def _on_deep_progress(done: int, total: int) -> None:
            _emit(
                5,
                "Enrichissement",
                f"Descriptions détaillées… {done}/{total}",
                deep_done=done,
                deep_total=total,
            )

        if platform_adapters.get("vinted") is None and mp is not None:
            platform_adapters["vinted"] = mp

        import os as _os_deep

        deep_workers = int(_os_deep.environ.get("VF_DEEP_FETCH_WORKERS", "3") or "3")
        deep_n, deep_fail_samples = enrich_rows_deep(
            deep_targets,
            platform_adapters,
            max_workers=max(1, min(6, deep_workers)),
            max_consecutive_fail=DEEP_FETCH_MAX_CONSEC_FAIL,
            headless=headless,
            boot_url=url,
            on_progress=_on_deep_progress,
        )

        estimator.end_step("deep_fetch")
        need_desc = sum(1 for r in deep_targets if not str(r.get("description") or "").strip())
        _log(f"✓ Deep fetch: {deep_n}/{need_desc or len(deep_targets)} descriptions")
        if deep_fail_samples:
            _log(f"  ⚠ échantillon échecs: {', '.join(deep_fail_samples)}")
        if deep_n == 0 and need_desc:
            _log("  ⚠ Descriptions manquantes — session API ou anti-bot (réessayez --headed)")
        skip_vision = deep_n == 0 and need_desc > 0

        # Re-triage douteux après description + re-score top-K
        id_to_item = {it.id: it for it in items}
        drop_after_desc: List[str] = []
        for sid in list(triage.doubtful_ids):
            try:
                iid = int(sid)
            except ValueError:
                continue
            it = id_to_item.get(iid)
            if not it:
                continue
            id_row = next((r for r in deep_targets if str(r.get("id") or "") == sid), None)
            desc = str((id_row or {}).get("description") or it.description or "")
            if desc:
                it.description = desc
            tr = triage_full_text(it, si, title_result=triage.by_id.get(sid))
            if tr.decision == TitleDecision.REJECT:
                drop_after_desc.append(sid)
        if drop_after_desc:
            items = [it for it in items if str(it.id) not in drop_after_desc]
            enriched = [r for r in enriched if str(r.get("id") or "") not in drop_after_desc]
            enriched_main = [r for r in enriched_main if str(r.get("id") or "") not in drop_after_desc]
            deep_pool = [r for r in deep_pool if str(r.get("id") or "") not in drop_after_desc]
            _log(f"✓ Post-description : {len(drop_after_desc)} douteux exclus")

        for r in deep_pool:
            sid = r.get("id")
            try:
                iid = int(sid) if sid is not None else 0
            except (TypeError, ValueError):
                iid = 0
            it = id_to_item.get(iid)
            if not it:
                continue
            desc = str(r.get("description") or "").strip()
            if desc:
                it.description = desc
            inf = infer_category(it)
            item_cat = resolve_item_category(inf, si)
            r["item_type"] = item_cat
            r.update(extract_specs(it, item_cat))
            enrich_pack_fields(r)
            audit_category_row(r)
            score = score_item_buy_mode(it, stats=stats, search_hint=search_hint, weights=get_weights())
            r.update(score)
            r["score_base"] = float(r.get("final_score") or 0.0)
            prev_overlay = float(r.get("score_overlay") or 0.0)
            r["score_overlay"] = prev_overlay
            msrp = estimate_msrp_eur(r)
            if msrp:
                r["msrp_eur"] = msrp
                dnew = discount_vs_new_pct(r)
                if dnew is not None:
                    r["discount_vs_new_pct"] = dnew

        if use_ai:
            from .ai import classify_quality_tier_batch, vision_check_item
            from .ai import CASCADE_ENABLED, analyze_top_deals_cascade, apply_cascade_analysis

            w0 = get_weights()
            priorities = (w0.buyer_priorities or "").strip() or search_hint
            ab = float(w0.ai_blend)

            if CASCADE_ENABLED:
                from .ai import partition_rows_for_deep_verify

                bypass_rows, verify_rows = partition_rows_for_deep_verify(
                    deep_pool,
                    market_category=dom_cat,
                    search_intent=search_hint,
                )
                estimator.start_step("ai_specs")
                _emit(
                    5,
                    "Analyse deals",
                    f"Prix · pack — {len(verify_rows)} audits IA "
                    f"({len(bypass_rows)} bypass confiance)…",
                    subphase="audit",
                    deep_done=len(deep_ids),
                    deep_total=len(deep_ids),
                )
                cascade_map = analyze_top_deals_cascade(
                    search_hint,
                    verify_rows,
                    category=dom_cat,
                    buyer_priorities=priorities,
                    max_items=TOP_DEEP_K,
                )
                for r in deep_pool:
                    aid = str(r.get("id") or "")
                    info = cascade_map.get(aid)
                    if info:
                        apply_cascade_analysis(r, info, ai_blend=ab)
                estimator.end_step("ai_specs")
                _log(f"✓ Cascade analyse top: {len(cascade_map)}/{len(deep_pool)}")
            else:
                from .ai import (
                    deep_verify_top_batch,
                    evaluate_buyer_fit_batch,
                    extract_specs_batch,
                )

                estimator.start_step("ai_specs")
                _emit(
                    5,
                    "Specs IA",
                    f"Extraction modèle / pack / état sur top {len(deep_pool)}…",
                    subphase="specs",
                    deep_done=len(deep_ids),
                    deep_total=len(deep_ids),
                )
                specs_map = extract_specs_batch(
                    intent_label,
                    deep_pool,
                    max_items=TOP_DEEP_K,
                    category=dom_cat,
                )
                for r in deep_pool:
                    aid = str(r.get("id") or "")
                    info = specs_map.get(aid)
                    if not info:
                        continue
                    apply_cascade_analysis(r, info, ai_blend=0.0)
                estimator.end_step("ai_specs")
                _log(f"✓ Specs IA: {len(specs_map)}/{len(deep_pool)}")
                fit_map = evaluate_buyer_fit_batch(
                    intent_label,
                    deep_pool,
                    category=dom_cat,
                    buyer_priorities=priorities,
                    max_items=TOP_DEEP_K,
                )
                for r in deep_pool:
                    fit = fit_map.get(str(r.get("id") or ""))
                    if fit:
                        r.update(fit)
                audit = deep_verify_top_batch(
                    intent_label,
                    deep_pool,
                    max_items=TOP_DEEP_K,
                    category=dom_cat,
                )
                for r in deep_pool:
                    info = audit.get(str(r.get("id") or ""))
                    if info:
                        apply_cascade_analysis(r, info, ai_blend=ab)

            estimator.start_step("vision")
            if skip_vision:
                _log("✓ Vision IA ignorée (deep fetch Vinted en échec)")
                estimator.end_step("vision")
            else:
                _emit(
                    5,
                    "Vision IA",
                    f"Photos des {min(TOP_VISION_K, len(deep_pool))} meilleures offres…",
                    subphase="vision",
                )
                vis_targets = [r for r in deep_pool[:TOP_VISION_K] if str(r.get("photo_url") or "")]
                vis_total = len(vis_targets)
                vis_n = 0
                vis_skip_thr = float(os.environ.get("VF_VISION_SKIP_CONF", "0.85"))
                for vi, r in enumerate(vis_targets):
                    try:
                        conf = float(r.get("spec_confidence") or 0)
                    except (TypeError, ValueError):
                        conf = 0.0
                    if conf >= vis_skip_thr:
                        r["ai_vision_note"] = f"Vision ignorée (confiance specs {conf:.0%})"
                        vis_n += 1
                        continue
                    note = vision_check_item(
                        search_intent=search_hint,
                        title=str(r.get("title") or ""),
                        image_url=str(r.get("photo_url") or ""),
                    )
                    if note:
                        r["ai_vision_note"] = note[:400]
                        vis_n += 1
                    _emit(
                        5,
                        "Vision IA",
                        f"Photo {vi + 1}/{vis_total} — {vis_n} analysées",
                        subphase="vision",
                        vision_done=vi + 1,
                        vision_total=max(1, vis_total),
                        catalog_n=len(items),
                    )
                estimator.end_step("vision")
                _log(f"✓ Vision IA: {vis_n}/{min(TOP_VISION_K, len(deep_pool))}")

            _log(f"5c/6 Classification qualité IA (top {QUALITY_TIER_MAX})…")
            quality_pool = list(deep_pool)
            deep_ids_q = {str(r.get("id") or "") for r in deep_pool}
            if main_ranked:
                rule_sorted = sorted(
                    main_ranked,
                    key=lambda r: float(r.get("rank_score") or r.get("final_score") or 0),
                    reverse=True,
                )
                top_n = max(1, int(len(rule_sorted) * 0.30))
                quality_pool.extend(
                    r for r in rule_sorted[:top_n] if str(r.get("id") or "") not in deep_ids_q
                )
            qm = classify_quality_tier_batch(
                dom_cat,
                quality_pool[:QUALITY_TIER_MAX],
                buyer_priorities=priorities,
                max_items=QUALITY_TIER_MAX,
            )
            if qm:
                _log(f"✓ Qualité IA: {len(qm)} annonces reclassées")
            for r in enriched:
                aid = str(r.get("id") or "")
                info = qm.get(aid)
                if info:
                    r.update(info)

        from .engine import apply_listing_intelligence, reconcile_pack_signals  # noqa: F401

        apply_listing_intelligence(enriched, category=dom_cat)

        # Spec enrichment — rules + optional IA on top-ranked rows, then re-score
        try:
            from .scoring import enrich_listings_specs

            spec_limit = int(os.environ.get("VF_SPEC_ENRICH_LIMIT", "25"))
            spec_pool = list(enriched_main or enriched)
            spec_pool.sort(
                key=lambda r: float(
                    r.get("rank_score")
                    or r.get("score_composite")
                    or r.get("final_score")
                    or 0
                ),
                reverse=True,
            )
            top_k_spec = min(spec_limit, max(TOP_DEEP_K, 25))
            n_spec = enrich_listings_specs(
                spec_pool[:top_k_spec],
                dom_cat or market_cat,
                use_ai=use_ai,
                limit=top_k_spec,
            )
            if n_spec:
                _log(f"✓ Enrichissement specs : {n_spec} annonces ({dom_cat or market_cat})")
        except Exception as exc:
            logger.debug("enrich_listings_specs: %s", exc)

        for r in enriched:
            reconcile_pack_signals(r)
            if confirmed_screen_defect(r) and str(r.get("triage_decision") or "") == "keep":
                r["triage_decision"] = "doubtful"
                tr = str(r.get("triage_reasons") or "")
                if "screen_defect" not in tr:
                    r["triage_reasons"] = f"{tr}|screen_defect_confirmed".strip("|")
            elif (r.get("screen_defect") or r.get("defect_hint")) and not confirmed_screen_defect(r):
                r.pop("screen_defect", None)
                if str(r.get("defect_hint") or "") == "True":
                    r.pop("defect_hint", None)
            if r.get("score_base") is None:
                r["score_base"] = float(r.get("final_score") or 0.0)
            tag_market_quality(r)
            try:
                from .scoring import apply_category_heuristics, apply_universal_tags

                apply_category_heuristics(r, dom_cat)
                apply_universal_tags(r)
            except Exception:
                pass
            apply_category_spec_scoring(r, get_weights())
            apply_deal_caps(r)
            r["rank_score"] = rank_score_for_sort(r)
        from .scoring import apply_composite_scores, build_market_context_for_scoring
        from .scoring import top_pepites_summary

        pro_ctx_final = build_market_context_for_scoring(enriched_main or enriched)
        apply_composite_scores(
            enriched,
            weights=get_weights(),
            market_ctx=pro_ctx_final,
            sync_quality_tier=True,
        )
        # Garde-fou: la fourchette prix choisie AVANT run doit toujours être respectée dans les résultats,
        # même si une plateforme ne peut pas appliquer le filtre au scrape.
        if eff_pmin or eff_pmax:
            try:
                from .scoring import effective_price_eur

                kept: List[Dict[str, Any]] = []
                dropped_rows: List[Dict[str, Any]] = []
                for r in enriched:
                    p = effective_price_eur(r)
                    if p and eff_pmin and p < int(eff_pmin):
                        dr = dict(r)
                        dr["reject_stage"] = "prix"
                        dr["reject_reason"] = f"prix {p:.0f}€ < min {int(eff_pmin)}€"
                        dropped_rows.append(dr)
                        continue
                    if p and eff_pmax and p > int(eff_pmax):
                        dr = dict(r)
                        dr["reject_stage"] = "prix"
                        dr["reject_reason"] = f"prix {p:.0f}€ > max {int(eff_pmax)}€"
                        dropped_rows.append(dr)
                        continue
                    kept.append(r)
                if dropped_rows:
                    enriched = kept
                    # keep enriched_main coherent
                    kept_ids_now = {str(x.get("id") or "") for x in kept}
                    enriched_main = [r for r in enriched_main if str(r.get("id") or "") in kept_ids_now]
                    out.rejected_rows = list(out.rejected_rows or []) + dropped_rows
                    _log(f"✓ Filtre prix résultats : -{len(dropped_rows)} hors {eff_pmin or '—'}–{eff_pmax or '—'} €")
            except Exception as exc:
                _log(f"⚠ Filtre prix résultats ignoré : {exc}")
        promoted = 0
        demote_strict_intent_mismatches(enriched, si)
        for r in enriched:
            if str(r.get("triage_decision") or "") != "doubtful":
                continue
            if not can_promote_doubtful_row(r, intent=si, category=dom_cat):
                continue
            try:
                sc = float(r.get("score_composite") or 0)
                rel = float(
                    r.get("ai_title_relevance")
                    or r.get("triage_confidence")
                    or r.get("item_type_conf")
                    or 0
                )
                if rel <= 1.0:
                    rel *= 100.0
            except (TypeError, ValueError):
                continue
            thresh_sc, thresh_rel = (55.0, 65.0) if dom_cat == "projector" else (60.0, 70.0)
            if dom_cat in ("ac", "clim", "climatisation"):
                thresh_sc, thresh_rel = 78.0, 82.0
            if sc > thresh_sc and rel > thresh_rel:
                if dom_cat in ("ac", "clim", "climatisation"):
                    try:
                        from .engine import _ac_off_category_title, _ac_title_matches_intent
                        from .scoring import ac_has_unit_signal

                        title = str(r.get("title") or "")
                        specs = (
                            r.get("extracted_specs")
                            if isinstance(r.get("extracted_specs"), dict)
                            else {}
                        )
                        if _ac_off_category_title(title):
                            continue
                        if not (
                            ac_has_unit_signal(title)
                            or specs.get("ac_is_unit")
                            or specs.get("capacity_btu")
                            or _ac_title_matches_intent(
                                title, SearchIntent(product_families=["ac"])
                            )
                        ):
                            continue
                    except Exception:
                        continue
                r["triage_decision"] = "keep"
                r["triage_promoted"] = "score_composite"
                promoted += 1
        promoted += promote_doubtful_listings(enriched, category=dom_cat, intent=si)
        demote_strict_intent_mismatches(enriched, si)
        if promoted:
            _log(f"✓ Post-triage : {promoted} douteux promus (score>60, pertinence>70)")
        normalize_run_rank_scores(enriched, main_only=True)
        # IA ranking complet (déterministe, sans LLM par annonce) — fusionne score + signaux IA déjà calculés.
        try:
            from .scoring import rank_rows

            ranked_all = rank_rows(
                enriched,
                category=dom_cat or market_cat or "generic",
                criteria_plan=out.criteria_plan,
                category_brain=out.category_brain,
            )
            enriched = ranked_all
            # Default display: use the AI-assisted ranking score for UI sorting.
            for r in enriched:
                if r.get("rank_final_score") is not None:
                    r["display_rank_score"] = float(r.get("rank_final_score") or 0.0)
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Ranking IA complet : {exc}")
        from .scoring import pick_value_top_deals

        picks = pick_value_top_deals(enriched_main or main_ranked, limit=10)
        for i, pr in enumerate(picks, start=1):
            pr["deal_pick_rank"] = i
            pr["is_top_pick"] = True
        out.top_picks = picks
        if use_ai and picks:
            try:
                from .ai import summarize_top_picks

                out.top_picks_note = summarize_top_picks(search_hint, picks, category=dom_cat)
            except Exception as exc:  # noqa: BLE001
                _log(f"⚠ Top 10 IA : {exc}")
        try:
            from .scoring import run_top5_analysis

            top5_intent: Dict[str, Any] = {}
            if category_brain:
                top5_intent["category_brain"] = category_brain
                ref = category_brain.get("market_reference") or {}
                if ref.get("median"):
                    top5_intent["market_median_eur"] = ref["median"]
            top5_data = run_top5_analysis(
                enriched_main or enriched,
                query=intent_label,
                category=dom_cat or market_cat or "generic",
                use_ai=use_ai,
                run_folder=folder,
                log_fn=_log,
                intent=top5_intent or None,
            )
            out.top5_analysis = top5_data
            out.top5_paths = {
                "top5_json": str((folder / "top5_analysis.json").resolve()),
                "top5_md": str((folder / "top5_analysis.md").resolve()),
            }
            t5 = top5_data.get("top5") or []
            if t5:
                _log(
                    "✓ Top 5 IA : "
                    + "; ".join(
                        f"{x.get('title', '')[:40]} {x.get('price')}€"
                        for x in t5[:5]
                    )
                )
                for i, pr in enumerate(t5[:5], start=1):
                    lid = str(pr.get("listing_id") or "")
                    for row in enriched:
                        if str(row.get("id") or "") == lid:
                            row["top5_rank"] = i
                            row["top5_ia_score"] = pr.get("score")
                            break
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Top 5 IA : {exc}")

        if picks:
            prices = [
                float(p.get("total_price") or p.get("price") or 0)
                for p in picks[:3]
                if float(p.get("total_price") or p.get("price") or 0) > 0
            ]
            pick_bits = [
                f"{p.get('deal_pick_rank')}. {p.get('total_price') or p.get('price')}€" for p in picks[:5]
            ]
            _log(
                f"✓ Top 10 affaires : {', '.join(pick_bits)}"
                + (f"… (à partir de {min(prices):.0f}€)" if prices else "")
            )
        _log(
            f"✓ Score composite appliqué — top {display_rank_score(enriched[0]) if enriched else '—'}"
        )
        enriched.sort(
            key=lambda r: float(
                r.get("rank_final_score")
                or r.get("display_rank_score")
                or r.get("rank_score")
                or r.get("final_score")
                or 0.0
            ),
            reverse=True,
        )
        banned = sum(1 for r in enriched if "ban" in str(r.get("risk_flags") or ""))
        acc = sum(1 for r in enriched if r.get("is_accessory"))
        gems = sum(
            1
            for r in enriched
            if r.get("quality_tier") == "gem" or str(r.get("deal_tier") or "") == "pepite"
        )
        drops = sum(
            1
            for r in enriched
            if r.get("quality_tier") == "dropship" or str(r.get("deal_tier") or "") == "dropship_suspect"
        )
        top_comp = enriched[0].get("score_composite") if enriched else "—"
        pepite_bits = top_pepites_summary(enriched_main or enriched, limit=3)
        if pepite_bits:
            _log(
                "✓ Top pepites : "
                + "; ".join(
                    f"{p['rank']}. {p.get('price_eur')}€ score {p.get('score')}"
                    for p in pepite_bits
                )
            )
        _log(
            f"✓ Tri final — {gems} pepites, {drops} dropship ecartes, "
            f"top composite {top_comp}, rank {enriched[0].get('rank_score') if enriched else '—'}"
        )
        try:
            from .engine import apply_model_market_stats
            from .engine import enrich_listing_taxonomy

            for row in enriched:
                enrich_listing_taxonomy(row)
            apply_model_market_stats(enriched_main)
            try:
                from .engine import enrich_rows_oracle, oracles_enabled

                if oracles_enabled():
                    n_or = enrich_rows_oracle(
                        enriched,
                        max_lookups=int(os.environ.get("VF_ORACLE_MAX", "25")),
                        category_hint=dom_cat,
                        log_fn=_log,
                    )
                    if n_or:
                        _log(f"✓ Smart Oracle Router : {n_or} annonces enrichies (cache modèle)")
            except Exception as exc:  # noqa: BLE001
                _log(f"⚠ Oracle prix : {exc}")
            out.dynamic_filters = build_filter_schema(
                market_cat or "generic",
                catalog_rows=enriched_main,
                ai_filters=ai_fl,
                category_schema=out.category_schema,
            )
            _log(f"✓ Filtres UI finaux: {len(out.dynamic_filters)} (catalogue + catégorie)")
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Filtres catalogue: {exc}")

        out.rows = enriched
        out.scraped_count = len(all_catalog)
        kept_ids = {str(r.get("id") or "") for r in enriched}
        from .engine import classify_rejected_listing

        rejected_rows: List[Dict[str, Any]] = []
        doubtful_rows: List[Dict[str, Any]] = []
        for r in enriched:
            if str(r.get("triage_decision") or "") == "doubtful":
                dr = dict(r)
                dr["reject_stage"] = "doute_triage"
                dr["reject_reason"] = str(dr.get("triage_reasons") or "doute_triage")
                doubtful_rows.append(dr)
        for it in all_catalog:
            sid = str(it.id or "")
            if not sid or sid in kept_ids:
                continue
            row = it.to_row()
            tr = triage.by_id.get(sid)
            if tr:
                row["triage_decision"] = tr.decision.value
                row["triage_reasons"] = "|".join(tr.reasons)[:160]
                row["triage_confidence"] = tr.confidence
            if sid in triage.reject_ids:
                row["reject_stage"] = "triage_titre"
            elif tr and tr.decision.value == "reject":
                row["reject_stage"] = "triage_titre"
            else:
                row["reject_stage"] = "filtre_ia_ou_pipeline"
            row["reject_reason"] = str(row.get("triage_reasons") or row["reject_stage"])
            try:
                from .engine import enrich_listing_taxonomy

                enrich_listing_taxonomy(row)
            except Exception:
                pass
            rel = title_rel_map.get(sid) if title_rel_map else None
            bucket = classify_rejected_listing(
                row,
                title_rel=rel,
                market_category=market_cat,
                search_intent=search_hint,
            )
            if bucket == "doubtful":
                row["reject_stage"] = "doute"
                doubtful_rows.append(row)
            else:
                rejected_rows.append(row)
        out.rejected_rows = rejected_rows
        out.doubtful_rows = doubtful_rows
        _log(
            f"✓ Archivage : {len(rejected_rows)} rejetés, {len(doubtful_rows)} doutes "
            f"/ {len(all_catalog)} scrapées"
        )
        from .export import export_listings_csv as _export_catalog

        _export_catalog(folder / "results_unified.csv", out.rows)
        _export_csv(folder / "results.csv", out.rows)
        try:
            # Export explicite du ranking IA complet (CSV) — colonnes incluses dans results.csv aussi.
            _export_csv(folder / "ranking_full.csv", out.rows)
        except Exception:
            pass
        try:
            from .export import export_results_xlsx

            xlsx_path = export_results_xlsx(
                folder / "results_enriched.xlsx",
                rows=out.rows,
                top5=(out.top5_analysis or {}).get("top5") if out.top5_analysis else None,
                rejected=rejected_rows,
                doubtful=doubtful_rows,
                category=dom_cat or market_cat or "",
            )
            _log(f"✓ Export Excel : {xlsx_path.name}")
        except Exception as exc:
            _log(f"⚠ Export Excel ignoré : {exc}")
        if rejected_rows:
            _export_csv(folder / "rejected.csv", rejected_rows)
        # Full export for external LLM / deep analysis
        try:
            payload = {
                "meta": {
                    "intent": intent_label,
                    "source_url": url,
                    "catalog_count": len(out.items),
                    "excluded_accessories": excluded_acc,
                },
                "rows": out.rows,
            }
            slim_rows = out.rows[:250] if len(out.rows) > 250 else out.rows
            payload["rows"] = slim_rows
            payload["meta"]["rows_exported"] = len(slim_rows)
            payload["meta"]["rows_total"] = len(out.rows)
            try:
                from .scoring import score_breakdown_for_export, top_pepites_summary

                payload["top_pepites"] = top_pepites_summary(out.rows, limit=5)
                for sr in payload["rows"]:
                    if isinstance(sr.get("score_breakdown"), dict):
                        sr["score_breakdown_json"] = score_breakdown_for_export(sr)
            except Exception:
                pass
            (folder / "results_full.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            _log(f"⚠ Export JSON complet ignoré : {exc}")

        # Ranked deals (main units) — primary GUI feed
        main_ranked = sorted(
            enriched_main,
            key=lambda r: float(
                r.get("rank_final_score")
                or r.get("value_score_v2")
                or r.get("rank_score")
                or r.get("final_score")
                or 0.0
            ),
            reverse=True,
        )
        top = [
            r
            for r in main_ranked
            if float(
                r.get("rank_final_score")
                or r.get("rank_score")
                or r.get("final_score")
                or 0.0
            )
            >= DEAL_SCORE_THRESHOLD
        ]
        out.deals_top = main_ranked[:120] if main_ranked else top[:120]
        _log(f"✓ Classement: {len(out.deals_top)} annonces principales (seuil deals {DEAL_SCORE_THRESHOLD})")

        # Download images for top ranked (cards in GUI)
        for r in enriched[: min(100, len(enriched))]:
            sid = str(r.get("id") or "")
            pu = str(r.get("photo_url") or "")
            if not sid or not pu or r.get("image_path"):
                continue
            img_path = folder / "images" / f"{sid}.jpg"
            if download_image_url(pu, img_path):
                r["image_path"] = str(img_path)

        for r in out.deals_top[:TOP_IMAGE_K]:
            sid = str(r.get("id") or "")
            pu = str(r.get("photo_url") or "")
            if not sid or not pu:
                continue
            img_path = folder / "images" / f"{sid}.jpg"
            if download_image_url(pu, img_path):
                r["image_path"] = str(img_path.resolve())
        # Quality audit for top deals (verification)
        for r in out.deals_top[:TOP_DEEP_K]:
            try:
                r.update(quality_audit_from_row(r))
            except Exception:
                continue
        _export_csv(folder / "deals_top.csv", out.deals_top)
        try:
            (folder / "dynamic_filters.json").write_text(
                json.dumps(out.dynamic_filters, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (folder / "market_agent.json").write_text(
                json.dumps(
                    {
                        "market_category": out.market_category,
                        "filter_agent_note": out.filter_agent_note,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        estimator.start_step("export")
        _emit(
            6,
            "Export",
            f"Classement terminé — {len(out.deals_top)} offres principales exportées",
            deals_top=len(out.deals_top),
        )

        try:
            from .engine import BUS, compute_cross_platform_analytics

            out.platform_analytics = compute_cross_platform_analytics(
                enriched_main,
                main_only=True,
                scrape_stats=out.scrape_stats,
                requested_platforms=plats,
            )
            BUS.emit("ANALYTICS_READY", {"summary": out.platform_analytics.get("summary", "")})
            _log(f"✓ Multi-plateformes : {out.platform_analytics.get('summary', '—')}")
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Analytics plateformes : {exc}")

        try:
            _log("6/6 Rapport marché…")
            _emit(6, "Rapport", "Génération du rapport marché…")
            rep = build_market_report(
                enriched_main,
                search_label=intent_label,
                source_url=url,
                catalog_count=len(out.items),
                excluded_accessories=excluded_acc,
                platform_analytics=out.platform_analytics,
            )
            if use_ai:
                from .ai import synthesize_market_brief

                try:
                    ai_text = synthesize_market_brief(intent_label, rep.stats, rep.top_deals)
                    if ai_text:
                        rep.summary_text = rep.summary_text + "\n\n---\n\n🤖 Synthèse IA\n\n" + ai_text
                        rep.markdown += "\n\n## Synthèse IA\n\n" + ai_text + "\n"
                except Exception as exc:  # noqa: BLE001
                    out.warnings.append(f"Synthèse IA : {exc}")
                    _log(f"⚠ Synthèse IA ignorée : {exc}")
            out.report = rep
            (folder / "report.md").write_text(rep.markdown, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            out.warnings.append(f"Rapport : {exc}")
            _log(f"⚠ Rapport marché ignoré (offres déjà classées) : {exc}")

        if use_ai:
            from .ai import get_cost_tracker

            tracker = get_cost_tracker()
            _log(f"✓ Coût IA estimé : {tracker.summary()}")
        out.run_duration_s = time.monotonic() - run_t0
        try:
            from .ai import get_cost_tracker

            trk = get_cost_tracker()
            out.perf_summary = {
                "scrape_s": round(estimator.step_actuals.get("catalog", 0), 1),
                "triage_s": round(estimator.step_actuals.get("triage", 0), 1),
                "cascade_s": round(estimator.step_actuals.get("ai_title", 0), 1),
                "scoring_s": round(estimator.step_actuals.get("scoring", 0), 1),
                "enrich_s": round(
                    estimator.step_actuals.get("deep_fetch", 0)
                    + estimator.step_actuals.get("ai_specs", 0)
                    + estimator.step_actuals.get("vision", 0),
                    1,
                ),
                "total_s": round(out.run_duration_s, 1),
                "catalog_count": len(out.items),
                "vinted_count": int((out.scrape_stats or {}).get("vinted", {}).get("unique", 0)
                    or (out.scrape_stats or {}).get("vinted", {}).get("raw", 0)),
                "cascade_chunks": estimator.ai_title_chunks,
                "tokens_est": trk.total_tokens,
                "cache_hits": trk.cache_hits,
            }
            ps = out.perf_summary
            _log(
                f"✓ Perf — total {ps['total_s']}s · scrape {ps['scrape_s']}s · "
                f"triage {ps['triage_s']}s · cascade {ps['cascade_s']}s · enrich {ps['enrich_s']}s · "
                f"tokens ~{ps['tokens_est']}"
            )
        except Exception:
            pass
        try:
            from .export import write_run_eval_summary

            out.eval_export_paths = write_run_eval_summary(
                run_folder=out.folder,
                query=intent_label,
                category=market_cat or dom_cat or "generic",
                platform_selection=out.platform_selection,
                scrape_stats=out.scrape_stats,
                catalog_count=len(out.items),
                main_count=len(enriched_main),
                deals_count=len(out.deals_top),
                duration_s=out.run_duration_s,
                top_deals=out.deals_top[:10],
                dynamic_filters=out.dynamic_filters,
                top5_analysis=top5_data or out.top5_analysis,
            )
            _log(f"✓ Export éval IA : {out.eval_export_paths.get('eval_summary', '—')}")
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Export éval IA ignoré : {exc}")
        _log(f"Terminé — export : {out.folder}")
        estimator.end_step("export")
        try:
            estimator.learn_from_run()
        except Exception:
            pass
        _emit(
            6,
            "Terminé",
            "Analyse terminée — consulte les offres classées",
            pct=1.0,
            export_folder=out.folder,
            catalog_count=len(out.items),
            main_items=len(enriched_main),
            excluded_accessories=excluded_acc,
        )
        try:
            mon = current_run()
            for k, v in estimator.step_actuals.items():
                mon.phase_done(k, v)
            mon.bump("catalog", len(out.items))
            mon.bump("offers", len(enriched_main))
            for pk, st in (out.scrape_stats or {}).items():
                mon.set_platform(pk, **st)
            finish_run(
                folder=out.folder,
                catalog=len(out.items),
                offers=len(enriched_main),
                rejected=len(out.rejected_rows),
                doubtful=len(out.doubtful_rows),
            )
        except Exception:
            pass
        try:
            _cleanup_keep_latest_run(runs_dir, current_folder=folder)
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠ Nettoyage dossiers runs : {exc}")
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb

        if out.has_results:
            out.warnings.append(str(exc))
            logger.exception("VF run partial failure (rows kept)")
            _log(f"⚠ Erreur après classement (résultats conservés) : {exc}")
            _log(_tb.format_exc()[-1200:])
        else:
            out.errors.append(str(exc))
            logger.exception("VF run failed")
            _log(f"✕ Échec pipeline : {exc}")
            _log(_tb.format_exc()[-2500:])
    finally:
        closed: set = set()
        try:
            for _ad in (platform_adapters or {}).values():
                oid = id(_ad)
                if oid in closed:
                    continue
                closed.add(oid)
                try:
                    _ad.close()
                except Exception:
                    pass
        except Exception:
            pass
        if id(mp) not in closed:
            mp.close()
    return out

