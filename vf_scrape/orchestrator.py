from __future__ import annotations



import os

import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from dataclasses import dataclass, field

from typing import Any, Callable, Dict, List, Optional, Set, Tuple



from .config import (
    ALL_PLATFORMS,
    platform_scrape_cap,
    platform_time_budget_sec,
    should_skip_platform,
)

from .export import dedupe_listings

from .expand import expand_queries

from .log import setup_scrape_logging

from .platforms import get_scraper

from .pro import filter_rows_by_price

from .session import ScrapeSession

from .types import PlatformScrapeStats, RawListing, ScrapeBatchResult





@dataclass

class ScrapeJob:

    query: str

    platforms: List[str] = field(default_factory=lambda: ["vinted", "leboncoin"])

    category_hint: str = ""

    price_min: Optional[int] = None

    price_max: Optional[int] = None

    max_per_platform: int = 0

    headless: bool = True

    verbose: bool = False

    adapters: Dict[str, Any] = field(default_factory=dict)

    smart_alias: bool = True





def _queries_for_platform(job: ScrapeJob, plat: str, scraper: Any) -> List[str]:

    queries = scraper.expand_queries(job.query, job.category_hint)

    if plat == "wallapop":

        max_variants = max(1, int(os.environ.get("VF_WALLAPOP_VARIANTS_MAX", "4") or "4"))

        if (job.category_hint or "").strip().lower() == "projector":

            q0 = (job.query or "").strip().lower()

            if q0 and q0 in {"video projecteur", "videoprojecteur", "vidéoprojecteur"}:

                skip_primary = os.environ.get("VF_WALLAPOP_SKIP_PRIMARY", "1").strip().lower() not in ("0", "false", "no")

                reordered = [q for q in queries if q.strip().lower() != q0]

                if not skip_primary and job.query:

                    reordered.append(job.query)

                if reordered:

                    queries = reordered

        queries = queries[:max_variants]

    return queries





def _platform_boot_ok(plat: str, ad: Any) -> bool:

    """True si la session plateforme est utilisable après bootstrap."""

    if plat == "vinted":

        if getattr(ad, "_bootstrapped", False):

            return True

        vs = getattr(ad, "_session", None)

        if vs is not None and getattr(vs, "has_session_cookie", False):

            return True

        return bool(getattr(getattr(vs, "session", None), "cookies", {}) or {})

    if plat == "ebay":

        if getattr(ad, "_challenge_failed", False):

            return False

        return bool(getattr(ad, "_session_warmed", False)) or bool(getattr(ad, "session", None) and ad.session.cookies)

    if bool(getattr(ad, "_bootstrapped", False)):

        if hasattr(ad, "_driver_alive"):

            return bool(ad._driver_alive()) or bool(getattr(ad, "_html_cache", {}))  # type: ignore[attr-defined]

        if hasattr(ad, "_probe_api_ok"):

            return bool(ad._probe_api_ok())  # type: ignore[attr-defined]

        if hasattr(ad, "_probe_ok"):

            return bool(ad._probe_ok())  # type: ignore[attr-defined]

        return True

    if hasattr(ad, "_driver_alive") and ad._driver_alive():  # type: ignore[attr-defined]

        return True

    if hasattr(ad, "_probe_api_ok") and ad._probe_api_ok():  # type: ignore[attr-defined]

        return True

    return False





def _alias_gain_pct(plat: str) -> float:
    try:
        if plat == "ebay":
            return float(os.environ.get("VF_EBAY_ALIAS_GAIN_PCT", os.environ.get("VF_ALIAS_GAIN_PCT", "0.03")) or "0.03")
        return float(os.environ.get("VF_ALIAS_GAIN_PCT", "0.04") or "0.04")
    except (TypeError, ValueError):
        return 0.04


def _vinted_min_target() -> int:
    try:
        return max(0, int(os.environ.get("VF_VINTED_MIN_TARGET", "1000") or "1000"))
    except (TypeError, ValueError):
        return 1000


def _cap_skip_pct() -> float:
    try:
        return float(os.environ.get("VF_CAP_SKIP_PCT", "0.85") or "0.85")
    except (TypeError, ValueError):
        return 0.85


def _platform_budget_exceeded(plat: str, t_start: float) -> bool:
    budget = platform_time_budget_sec(plat)
    if budget <= 0:
        return False
    return (time.monotonic() - t_start) >= budget


def _smart_alias_skip(
    *,
    plat: str,
    new_rows: List[RawListing],
    seen_ids: Set[str],
    prior_unique: int,
    cap: int = 0,
) -> bool:
    """True si l'alias n'apporte presque rien — on peut arrêter les variantes."""
    if not new_rows:
        if prior_unique >= 12 and plat in {"vinted", "wallapop", "leboncoin"}:
            return True
        return False
    if prior_unique < 15 and plat not in {"vinted", "ebay"}:
        return False
    if prior_unique < 25 and plat == "vinted":
        return False
    vinted_min = _vinted_min_target() if plat == "vinted" else 0
    if vinted_min and prior_unique < vinted_min:
        return False
    new_ids = {r.platform_item_id for r in new_rows if r.platform_item_id} - seen_ids
    gain_pct = _alias_gain_pct(plat)
    skip_pct = _cap_skip_pct()
    if cap and prior_unique >= int(cap * skip_pct):
        min_gain = max(3, int(prior_unique * min(0.02, gain_pct)))
        return len(new_ids) < min_gain
    if plat == "ebay":
        min_gain = max(3, int(prior_unique * gain_pct))
    elif plat == "vinted":
        min_gain = max(6, int(prior_unique * max(gain_pct, 0.02)))
    else:
        min_gain = max(5, int(prior_unique * gain_pct))
    return len(new_ids) < min_gain





def _parallel_enabled(n_plats: int) -> bool:

    raw = os.environ.get("VF_SCRAPE_PARALLEL", "auto").strip().lower()

    if raw in ("0", "false", "no", "off"):

        return False

    if raw in ("1", "true", "yes", "on"):

        return n_plats > 1

    return n_plats > 1





def _max_workers(n_plats: int) -> int:

    try:

        cap = int(os.environ.get("VF_SCRAPE_WORKERS", "4") or "4")

    except (TypeError, ValueError):

        cap = 4

    return max(1, min(cap, n_plats, len(ALL_PLATFORMS)))





def _scrape_single_platform(

    job: ScrapeJob,

    plat: str,

    *,

    prefetched: Optional[Any] = None,

    log: Any = None,

    on_platform_progress: Optional[Callable[[str, str, int], None]] = None,

) -> Tuple[str, List[RawListing], PlatformScrapeStats, Optional[Any]]:

    """Scrape une plateforme — session isolée pour parallélisme."""

    plat = plat.lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress")

    n_plats = len(job.platforms)

    cap = job.max_per_platform or platform_scrape_cap(plat)
    if job.max_per_platform and job.max_per_platform > 0:
        cap = min(cap, job.max_per_platform)

    smart_alias = job.smart_alias and os.environ.get("VF_SMART_ALIAS", "1") != "0"
    t_plat_start = time.monotonic()
    plat_budget = platform_time_budget_sec(plat)



    if should_skip_platform(plat, n_plats):

        st = PlatformScrapeStats(platform=plat, skipped=True, last_error="VF_SKIP_FRAGILE / auto")

        if log:

            log.info("[%s] ignoré — fragile (VF_FORCE_FRAGILE=1 pour forcer)", plat)

        return plat, [], st, prefetched



    session = ScrapeSession(headless=job.headless)

    adapter_out: Optional[Any] = prefetched

    try:

        scraper = get_scraper(plat, headless=job.headless, verbose=job.verbose)

        scraper._session = session  # type: ignore[attr-defined]

        if prefetched:

            scraper.prefetched_adapter = prefetched



        boot_ok = bool(prefetched)

        if not prefetched and not session.is_bootstrapped(plat):

            if log:

                log.info("[%s] bootstrap session…", plat)

            t_boot = time.monotonic()

            boot_ok = False

            try:

                session.bootstrap(plat, job.query)

                ad_boot = session.adapter(plat)

                boot_ok = _platform_boot_ok(plat, ad_boot)

                if boot_ok and hasattr(ad_boot, "_bootstrap_at"):

                    ad_boot._bootstrap_at = time.monotonic()  # type: ignore[attr-defined]

                if log:

                    if boot_ok:

                        log.info("[%s] session OK (%.1fs)", plat, time.monotonic() - t_boot)

                    else:

                        log.warning("[%s] bootstrap incomplet (%.1fs)", plat, time.monotonic() - t_boot)

            except TimeoutError as exc:

                if log:

                    log.error("[%s] bootstrap timeout: %s", plat, exc)

                st = PlatformScrapeStats(platform=plat, errors=1, skipped=True, last_error=str(exc))

                return plat, [], st, None

            except Exception as exc:

                if log:

                    log.warning("[%s] bootstrap: %s", plat, exc)

        elif not prefetched and session.is_bootstrapped(plat):

            if log:

                log.debug("[%s] session réutilisée — bootstrap ignoré", plat)

            boot_ok = _platform_boot_ok(plat, session.adapter(plat))



        if not prefetched:

            ad_chk = session.adapter(plat)

            if not boot_ok and plat == "rakuten" and getattr(ad_chk, "_browser_gave_up", False):

                st_skip = PlatformScrapeStats(

                    platform=plat,

                    errors=1,

                    skipped=True,

                    last_error="bootstrap échoué — anti-bot Rakuten",

                )

                if log:

                    log.warning("[%s] ignoré après bootstrap incomplet", plat)

                return plat, [], st_skip, ad_chk

            adapter_out = ad_chk



        queries = _queries_for_platform(job, plat, scraper)

        plat_rows: List[RawListing] = []

        seen_ids: Set[str] = set()

        st = PlatformScrapeStats(platform=plat)



        for qi, q in enumerate(queries):

            if _platform_budget_exceeded(plat, t_plat_start):
                if log:
                    log.info("[%s] budget %.0fs atteint — arrêt partiel (%d uniques)", plat, plat_budget, len(seen_ids))
                break

            vinted_min = _vinted_min_target() if plat == "vinted" else 0
            if cap and len(seen_ids) >= int(cap * _cap_skip_pct()):
                if not vinted_min or len(seen_ids) >= vinted_min:
                    if log:
                        log.info("[%s] cap %.0f%% atteint (%d) — variantes suivantes ignorées", plat, _cap_skip_pct() * 100, len(seen_ids))
                    break

            if on_platform_progress:

                try:

                    on_platform_progress(plat, q, len(plat_rows))

                except Exception:

                    pass

            if log:

                log.info("[%s] variante %d/%d — %r", plat, qi + 1, len(queries), q)

            kwargs: Dict[str, Any] = {

                "price_min": job.price_min,

                "price_max": job.price_max,

                "max_items": cap,

                "category_hint": job.category_hint,

            }

            t1 = time.monotonic()

            try:

                rows, st_part = scraper.scrape_query(q, **kwargs)

            except Exception as exc:

                rows, st_part = [], scraper._stat()

                st_part.errors = 1

                st_part.last_error = str(exc)

                if log:

                    log.warning("[%s] crash variante %r: %s", plat, q, exc)

            ms = int((time.monotonic() - t1) * 1000)

            st_part.duration_ms = ms

            new_only = [r for r in rows if r.platform_item_id and r.platform_item_id not in seen_ids]

            if log:

                log.info(

                    "[%s] variante %d — %d annonces (+%d nouvelles) en %.1fs",

                    plat,

                    qi + 1,

                    len(rows),

                    len(new_only),

                    ms / 1000.0,

                )



            plat_rows.extend(new_only or rows)

            for r in new_only or rows:

                if r.platform_item_id:

                    seen_ids.add(r.platform_item_id)

            st.raw += st_part.raw

            st.errors += st_part.errors

            if st_part.last_error:

                st.last_error = st_part.last_error

            st.variants += 1

            if st_part.skipped:

                st.skipped = True

                break

            if plat in {"cashexpress", "rakuten", "ebay"} and qi >= 1 and not new_only:
                if log:
                    log.info("[%s] variante %d sans gain — stop variantes suivantes", plat, qi + 1)
                break

            if cap and len(seen_ids) >= cap:

                if log:

                    log.info("[%s] cap %d atteint — variantes suivantes ignorées", plat, cap)

                break

            if smart_alias and qi + 1 < len(queries) and _smart_alias_skip(

                plat=plat,

                new_rows=new_only,

                seen_ids=seen_ids,

                prior_unique=len(seen_ids) - len(new_only),

                cap=cap,

            ):

                if log:

                    log.info("[%s] gain alias insuffisant — variantes suivantes ignorées", plat)

                break

            if _platform_budget_exceeded(plat, t_plat_start):

                if log:

                    log.info("[%s] budget %.0fs atteint après variante %d — stop", plat, plat_budget, qi + 1)

                break



        plat_rows, intra_dupes = dedupe_listings(plat_rows)

        if job.price_min or job.price_max:

            plat_rows, price_dropped = filter_rows_by_price(

                plat_rows,

                price_min=job.price_min,

                price_max=job.price_max,

            )

            if price_dropped and log:

                log.info("[%s] hors fourchette prix : -%d", plat, price_dropped)

        st.unique = len(plat_rows)

        if intra_dupes and log:

            log.info("[%s] dédup intra-plateforme : %d retirés", plat, intra_dupes)

        if log:

            log.info(

                "[%s] brut=%d uniques=%d variantes=%d err=%d%s",

                plat,

                st.raw,

                st.unique,

                st.variants,

                st.errors,

                " SKIP" if st.skipped else "",

            )

        if on_platform_progress:

            try:

                on_platform_progress(plat, "done", st.unique)

            except Exception:

                pass

        return plat, plat_rows, st, adapter_out

    finally:

        # Flush cookies eBay une fois en fin de plateforme

        try:

            ad_fin = session.adapter(plat) if plat in session._adapters else None

            if ad_fin is not None and hasattr(ad_fin, "flush_persisted_cookies"):

                ad_fin.flush_persisted_cookies()  # type: ignore[attr-defined]

        except Exception:

            pass

        if not prefetched:

            session.close()





def run_scrape(job: ScrapeJob) -> ScrapeBatchResult:

    log = setup_scrape_logging(verbose=job.verbose)

    log.info(

        "=== VF Scrape -- %r | plateformes %s | cat=%s ===",

        job.query,

        ",".join(job.platforms),

        job.category_hint or "—",

    )

    t0 = time.monotonic()

    all_rows: List[RawListing] = []

    stats: Dict[str, PlatformScrapeStats] = {}

    n_plats = len(job.platforms)

    parallel = _parallel_enabled(n_plats)



    plats_norm = [
        p.lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress") for p in job.platforms
    ]
    _prio = {"vinted": 0, "leboncoin": 1, "ebay": 2, "wallapop": 3, "rakuten": 4, "cashexpress": 5}
    plats_norm = sorted(plats_norm, key=lambda p: (_prio.get(p, 50), p))



    if parallel:

        log.info("Scrape parallèle — %d workers", _max_workers(n_plats))

        with ThreadPoolExecutor(max_workers=_max_workers(n_plats)) as pool:

            futures = {

                pool.submit(

                    _scrape_single_platform,

                    job,

                    plat,

                    prefetched=job.adapters.get(plat),

                    log=log,

                ): plat

                for plat in plats_norm

            }

            for fut in as_completed(futures):

                plat_key = futures[fut]

                try:

                    plat, plat_rows, st, _ad = fut.result()

                    stats[plat] = st

                    all_rows.extend(plat_rows)

                    log.info("[%s] terminé — %d uniques (parallèle)", plat, st.unique)

                except Exception as exc:

                    log.error("[%s] échec thread scrape: %s", plat_key, exc)

                    stats[plat_key] = PlatformScrapeStats(

                        platform=plat_key, errors=1, last_error=str(exc)

                    )

    else:

        session = ScrapeSession(headless=job.headless)

        try:

            for plat in plats_norm:

                pref = job.adapters.get(plat)

                _, plat_rows, st, ad = _scrape_single_platform(job, plat, prefetched=pref, log=log)

                stats[plat] = st

                all_rows.extend(plat_rows)

                if ad is not None and plat not in job.adapters:

                    job.adapters[plat] = ad

        finally:

            session.close()



    merged, dupes = dedupe_listings(all_rows)

    ms = int((time.monotonic() - t0) * 1000)

    log.info(

        "=== Termine -- %d uniques (%d doublons inter-plateformes) en %.1fs ===",

        len(merged),

        dupes,

        ms / 1000.0,

    )

    return ScrapeBatchResult(listings=merged, stats=stats, duplicates_removed=dupes)





def run_scrape_from_plan(

    plan: Any,

    *,

    max_items_per_variant: int = 0,

    headless: bool = True,

    on_page: Optional[Callable[[int, int, int], None]] = None,

    log_fn: Optional[Callable[[str], None]] = None,

    prefetched_adapters: Optional[Dict[str, Any]] = None,

    keep_adapters_open: bool = True,

    verbose: bool = False,

    on_platform_progress: Optional[Callable[[str, str, int], None]] = None,

) -> Tuple[List[Any], int, Dict[str, Dict[str, Any]], Dict[str, Any]]:

    """

    Remplace ``fetch_plan_catalog`` quand ``VF_USE_SCRAPE_LIB=1``.

    Retourne le même tuple que ``vf.search.fetch_plan_catalog``.

    """

    from vf.market import CatalogItem



    del keep_adapters_open, on_page

    plats = sorted({v.platform.lower().replace("lbc", "leboncoin") for v in plan.variants})

    adapters = dict(prefetched_adapters or {})

    job = ScrapeJob(

        query=plan.canonical_query,

        platforms=plats,

        category_hint=getattr(plan, "category_hint", "") or "",

        price_min=getattr(plan.variants[0], "price_min", None) if plan.variants else None,

        price_max=getattr(plan.variants[0], "price_max", None) if plan.variants else None,

        max_per_platform=max_items_per_variant,

        headless=headless,

        verbose=verbose or os.environ.get("VF_SCRAPE_VERBOSE", "") == "1",

        adapters=adapters,

    )



    if _parallel_enabled(len(plats)):

        log = setup_scrape_logging(verbose=job.verbose)

        t0 = time.monotonic()

        all_rows: List[RawListing] = []

        stats: Dict[str, PlatformScrapeStats] = {}

        with ThreadPoolExecutor(max_workers=_max_workers(len(plats))) as pool:

            futures = {

                pool.submit(

                    _scrape_single_platform,

                    job,

                    plat,

                    prefetched=adapters.get(plat),

                    log=log,

                    on_platform_progress=on_platform_progress,

                ): plat

                for plat in plats

            }

            for fut in as_completed(futures):

                plat_key = futures[fut]

                try:

                    plat, plat_rows, st, ad = fut.result()

                    stats[plat] = st

                    all_rows.extend(plat_rows)

                    if ad is not None:

                        adapters[plat] = ad

                    if log_fn:

                        if st.skipped:

                            log_fn(f"⊘ {plat} : ignoré ({st.last_error or 'skip'})")

                        elif st.errors and st.raw == 0:

                            log_fn(f"⚠ {plat}: {st.last_error}")

                        elif st.raw:

                            log_fn(f"✓ {plat} : {st.unique} uniques ({st.variants} variantes)")

                except Exception as exc:

                    if log_fn:

                        log_fn(f"⚠ {plat_key}: {exc}")

                    stats[plat_key] = PlatformScrapeStats(platform=plat_key, errors=1, last_error=str(exc))

        merged, dupes = dedupe_listings(all_rows)

        if log_fn:

            log_fn(f"✓ vf_scrape parallèle : {len(merged)} annonces en {time.monotonic() - t0:.1f}s")

        items: List[CatalogItem] = [r.to_catalog_item() for r in merged]

        scrape_stats: Dict[str, Dict[str, Any]] = {}

        for k, st in stats.items():

            scrape_stats[k] = {

                "raw": st.raw,

                "unique": st.unique,

                "variants": st.variants,

                "errors": st.errors,

                "last_error": st.last_error,

                "skipped": st.skipped,

            }

        return items, dupes, scrape_stats, adapters



    batch = run_scrape(job)

    items = [r.to_catalog_item() for r in batch.listings]

    scrape_stats = {}

    for k, st in batch.stats.items():

        scrape_stats[k] = {

            "raw": st.raw,

            "unique": st.unique,

            "variants": st.variants,

            "errors": st.errors,

            "last_error": st.last_error,

            "skipped": st.skipped,

        }

        if log_fn:

            if st.skipped:

                log_fn(f"⊘ {k} : ignoré ({st.last_error or 'skip'})")

            elif st.errors and st.raw == 0:

                log_fn(f"⚠ {k}: {st.last_error}")

            elif st.raw:

                log_fn(f"  · {k} brut : {st.raw} ({st.variants} requêtes) → {st.unique} uniques")

    return items, batch.duplicates_removed, scrape_stats, adapters


