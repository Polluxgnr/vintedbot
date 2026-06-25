"""Interface graphique CustomTkinter : configuration, filtres dynamiques et affichage des deals."""


import logging
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor

from .engine import (
    enrich_row_listing_fields,
    listing_platform_label,
    listing_url_for_row,
    open_listing_url,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dataclasses import asdict

from .engine import (
    ScoreWeights,
    apply_dynamic_filters,
    build_rule_filters,
    category_buyer_criteria,
    deal_advice_lines,
    format_score_breakdown,
    display_rank_score,
    enrich_pack_fields,
    listing_price_eur,
    filter_presets,
    normalize_filter_spec,
    normalize_run_rank_scores,
    rescore_rows_inplace,
    format_clock,
    sort_rows_for_view,
    get_weights,
    detect_market_context,
    get_market_priority,
    guess_market_category_from_url,
    market_priority_hint,
    market_priority_key,
    set_market_priority,
    rank_score_for_sort,
    load_weights,
    rescore_catalog,
    resolve_effective_category,
    save_weights,
    set_weights,
    weight_field_defs,
)
from .market import download_image_url, resolve_vinted_catalog_url
from .pipeline import VfOutput, run_url

THUMB_CACHE = Path("brouillon") / "data" / "vf_thumbs"

BG = "#100904"
WARM_CREAM = "#ffedd7"
DARK_CORK = "#382416"
BURNT_SIENNA = "#dc5000"
TEXT = WARM_CREAM
TEXT_MUTED = "#6c5f51"
BORDER = "#40372e"
BORDER_ACTIVE = WARM_CREAM
DANGER = BURNT_SIENNA
WARN = "#c9a227"
OK = "#8fbc8f"
RADIUS_CARD = 12
RADIUS_PILL = 36
RADIUS_GHOST = 22
FONT_DISPLAY = ("Plus Jakarta Sans", "Segoe UI", "Inter", "sans-serif")
FONT_BODY = FONT_DISPLAY
FONT_MONO = ("Consolas", "Cascadia Mono", "monospace")


def _badge_chip_style(badge: str) -> Tuple[str, str]:
    """Couleurs badges universels (Obsolète, Bon plan, Complétude…)."""
    b = (badge or "").lower()
    if any(x in b for x in ("obsolète", "veto", "défaut", "gonflées", "hs")):
        return "#3a1a14", DANGER
    if "bon plan" in b or b.startswith("complétude") and "100" in b:
        return "#1a2a22", OK
    if b.startswith("complétude"):
        return "#2a2218", WARN
    return "#1a2a22", OK


def _seller_detail_line(r: Dict[str, Any], seller: str) -> str:
    reviews = r.get("seller_feedback_count") or r.get("seller_feedback") or "—"
    rating = r.get("seller_rating") or "—"
    bits = [f"Vendeur {seller or '—'}", f"note {rating}", f"({reviews} avis)"]
    label = str(r.get("seller_trust_label") or "").strip()
    if label:
        bits.append(f"· {label}")
    return "  ·  ".join(bits)


def _seller_risk_line(r: Dict[str, Any]) -> str:
    flags = str(r.get("risk_flags") or "").strip()
    try:
        reviews = int(r.get("seller_feedback_count") or r.get("seller_feedback") or 0)
    except (TypeError, ValueError):
        reviews = 0
    if reviews == 0 and not flags:
        return "Risques: compte sans avis — prudence (surtout >60€)"
    if not flags:
        return "Risques: aucun signal particulier"
    labels = {
        "new_seller_high_risk": "vendeur sans avis",
        "low_trust_seller": "peu d'avis",
        "low_seller_rating": "note faible",
    }
    parts = [labels.get(f.strip(), f.strip()) for f in flags.split() if f.strip()]
    return f"Risques: {', '.join(parts)}"

logger = logging.getLogger("vf.app")
_CRASH_LOG = Path("brouillon") / "data" / "vf_crash.log"
_SESSION_LOG = Path("brouillon") / "data" / "vf_session.log"


def _write_crash_log(exc: BaseException, *, where: str = "") -> None:
    try:
        _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CRASH_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {where} ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass

STEPS = [
    (1, "Connexion", "Session marketplace"),
    (2, "Catalogue", "Récupération des annonces"),
    (3, "Triage", "Filtre titres + IA douteux"),
    (4, "Scoring", "Notes & risques"),
    (5, "Enrichissement", "Descriptions · specs · photos"),
    (6, "Export", "Classement final"),
]


@dataclass(slots=True)
class AppState:
    phase: str = "idle"  # idle | setup | running | results
    running: bool = False
    intent: str = ""
    output: Optional[VfOutput] = None
    catalog_items: List[Any] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    filtered: List[Dict[str, Any]] = field(default_factory=list)
    selected: Optional[Dict[str, Any]] = None
    filter_vars: Dict[str, Any] = field(default_factory=dict)
    filter_widgets: Dict[str, Any] = field(default_factory=dict)
    run_summary: Dict[str, Any] = field(default_factory=dict)
    current_step: int = 0
    market_category: str = ""
    last_market_url: str = ""
    market_detect_gen: int = 0
    prio_context_key: str = ""
    suggest_tip_keys: List[str] = field(default_factory=list)
    pre_filter_defaults: Dict[str, str] = field(default_factory=dict)
    view_mode: str = "all"
    active_preset_id: str = "preset_reset"
    cards_rendered: int = 0
    enriched_cache: List[Dict[str, Any]] = field(default_factory=list)
    rejected_rows: List[Dict[str, Any]] = field(default_factory=list)
    doubtful_rows: List[Dict[str, Any]] = field(default_factory=list)
    rejected_rendered: int = 0
    doubtful_rendered: int = 0
    platform_analytics: Optional[Dict[str, Any]] = None
    top_picks: List[Dict[str, Any]] = field(default_factory=list)
    top_picks_note: str = ""
    top5_analysis: Optional[Dict[str, Any]] = None
    top5_paths: Dict[str, str] = field(default_factory=dict)
    category_brain: Optional[Dict[str, Any]] = None
    analysis_markdown: str = ""
    eval_export_paths: Dict[str, str] = field(default_factory=dict)
    ai_depth: str = "balanced"
    scrape_depth: str = "pro"
    preflight_active: Dict[str, bool] = field(default_factory=dict)
    search_brief: Optional[Dict[str, Any]] = None
    price_min: str = ""
    price_max: str = ""
    idle_refinements: Dict[str, str] = field(default_factory=dict)
    plat_user_override: bool = False
    last_preview_category: str = ""
    deal_hunt_mode: str = "balanced"
    preview_busy: bool = False
    sort_by: str = "score"


LIST_PAGE_SIZE = 60
LIST_RENDER_BATCH = 12
INITIAL_LIST_RENDER = 40
INLINE_DOUBT_MAX = 8
INITIAL_REJECTED_RENDER = 30
REJECTED_RENDER_BATCH = 10
INITIAL_DOUBT_RENDER = 35
DOUBT_RENDER_BATCH = 10
_THUMB_EXEC = ThreadPoolExecutor(max_workers=3, thread_name_prefix="vf-thumb")
_THUMB_DL_GUARD: set = set()


class _LogHandler(logging.Handler):
    def __init__(self, write_fn):
        super().__init__()
        self._write = write_fn

    def emit(self, record: logging.LogRecord) -> None:
        try:
            p = "⚠ " if record.levelname == "WARNING" else "✕ " if record.levelname == "ERROR" else ""
            self._write(f"{p}{self.format(record)}\n")
        except Exception:
            pass


def _patch_tk_iconbitmap_safe() -> None:
    """
    CustomTkinter programme iconbitmap via after(200) sur CTkToplevel.
    Sur Windows ARM64 + Tcl récent, le .ico CustomTkinter lève TclError hors try/except.
    """
    if getattr(_patch_tk_iconbitmap_safe, "_done", False):
        return
    import tkinter

    _orig = tkinter.Wm.wm_iconbitmap

    def _safe_wm_iconbitmap(self, bitmap=None, default=None):
        try:
            return _orig(self, bitmap, default)
        except tkinter.TclError:
            return None

    tkinter.Wm.wm_iconbitmap = _safe_wm_iconbitmap  # type: ignore[method-assign]
    _patch_tk_iconbitmap_safe._done = True  # type: ignore[attr-defined]


def launch(*, start_url: str = "", use_ai: bool = True, headless: bool = True) -> None:
    from .engine import setup_logging

    setup_logging(verbose=os.environ.get("VF_VERBOSE", "").strip() in ("1", "true", "yes"))
    os.environ["VF_GUI_MODE"] = "1"
    try:
        from vf_scrape.config import apply_scrape_profile

        apply_scrape_profile("pro")
    except Exception:
        pass
    _patch_tk_iconbitmap_safe()
    try:
        import customtkinter as ctk  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "GUI dependency missing: customtkinter.\n"
            "Install with:\n"
            '  python -m pip install ".[gui]"\n'
            "or (full install):\n"
            '  python -m pip install ".[all]"\n'
        ) from exc

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            _write_crash_log(args.exc_value, where=f"thread:{args.thread.name}")

    threading.excepthook = _thread_excepthook

    load_weights()
    ctk.set_appearance_mode("Dark")

    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        Image = None  # type: ignore

    state = AppState()
    root = ctk.CTk()
    root.title("VF Finder")
    root.geometry("1720x980")
    root.minsize(1200, 760)
    root.configure(fg_color=BG)

    F_TITLE = ctk.CTkFont(family=FONT_DISPLAY[0], size=34)
    F_H2 = ctk.CTkFont(family=FONT_DISPLAY[0], size=20)
    F_BODY = ctk.CTkFont(family=FONT_BODY[0], size=14)
    F_CAP = ctk.CTkFont(family=FONT_BODY[0], size=12)
    F_MONO = ctk.CTkFont(family=FONT_MONO[0], size=11)
    _img_refs: Dict[str, Any] = {}

    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(1, weight=1)

    # ── Top bar
    top = ctk.CTkFrame(root, fg_color=BG, height=72, corner_radius=0)
    top.grid(row=0, column=0, sticky="ew")
    top.grid_propagate(False)
    top.grid_columnconfigure(1, weight=1)
    brand_col = ctk.CTkFrame(top, fg_color="transparent")
    brand_col.grid(row=0, column=0, sticky="w", padx=24, pady=10)
    title_row = ctk.CTkFrame(brand_col, fg_color="transparent")
    title_row.pack(anchor="w")
    ctk.CTkLabel(title_row, text="VF Finder", font=F_TITLE, text_color=TEXT).pack(side="left")
    ctk.CTkLabel(
        title_row,
        text="Vinted-first",
        font=F_CAP,
        text_color=WARM_CREAM,
        fg_color=BURNT_SIENNA,
        corner_radius=8,
        width=88,
        height=22,
    ).pack(side="left", padx=(12, 0))
    ctk.CTkLabel(
        brand_col,
        text="Vinted · LeBonCoin · eBay (optionnel)",
        font=F_CAP,
        text_color=TEXT_MUTED,
    ).pack(anchor="w", pady=(4, 0))
    header_stats = ctk.StringVar(value="")
    ctk.CTkLabel(
        top,
        textvariable=header_stats,
        font=F_BODY,
        text_color=TEXT_MUTED,
        anchor="e",
        justify="right",
        wraplength=720,
    ).grid(row=0, column=1, sticky="e", padx=16)
    try:
        from .ai import api_key as _ak

        key_ok = bool(_ak())
    except Exception:
        key_ok = False
    ctk.CTkLabel(
        top,
        text="IA connectée" if key_ok else "IA hors ligne",
        font=F_CAP,
        text_color=OK if key_ok else WARN,
    ).grid(row=0, column=2, padx=24)

    body = ctk.CTkFrame(root, fg_color=BG)
    body.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(0, weight=1)

    # ── IDLE (accueil IA)
    idle_frame = ctk.CTkFrame(body, fg_color=BG)
    idle_frame.grid(row=0, column=0, sticky="nsew")
    idle_frame.grid_columnconfigure(0, weight=1)
    idle_frame.grid_rowconfigure(0, weight=1)
    idle_scroll = ctk.CTkScrollableFrame(idle_frame, fg_color=BG, corner_radius=0)
    idle_scroll.grid(row=0, column=0, sticky="nsew")
    idle_inner = ctk.CTkFrame(idle_scroll, fg_color=BG)
    idle_inner.pack(fill="x", anchor="n", pady=32, padx=24)

    ctk.CTkLabel(
        idle_inner,
        text="Arbitrage live · tout le marché",
        font=ctk.CTkFont(family=FONT_DISPLAY[0], size=34, weight="bold"),
        text_color=TEXT,
    ).pack(anchor="w", pady=(0, 6))
    ctk.CTkLabel(
        idle_inner,
        text="Une recherche, toutes les plateformes. L’IA ne brûle des tokens que sur les annonces ambiguës.",
        font=F_BODY,
        text_color=TEXT_MUTED,
        wraplength=880,
        justify="left",
    ).pack(anchor="w", pady=(0, 20))

    search_card = ctk.CTkFrame(
        idle_inner, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER
    )
    search_card.pack(fill="x", pady=(0, 16))
    search_inner = ctk.CTkFrame(search_card, fg_color="transparent")
    search_inner.pack(fill="x", padx=20, pady=18)
    ctk.CTkLabel(search_inner, text="Que cherchez-vous ?", font=F_H2, text_color=WARM_CREAM).pack(anchor="w")
    search_row = ctk.CTkFrame(search_inner, fg_color="transparent")
    search_row.pack(fill="x", pady=(10, 0))
    search_row.grid_columnconfigure(0, weight=1)
    idle_search_var = ctk.StringVar(value=start_url or "")
    search_entry = ctk.CTkEntry(
        search_row,
        textvariable=idle_search_var,
        height=48,
        font=ctk.CTkFont(family=FONT_BODY[0], size=16),
        placeholder_text="URL Vinted/LBC ou « New Nintendo 3DS XL »…",
        fg_color=BG,
        border_color=BORDER_ACTIVE,
        text_color=TEXT,
    )
    search_entry.grid(row=0, column=0, sticky="ew")
    suggest_popup = ctk.CTkFrame(search_inner, fg_color=BG, corner_radius=8, border_width=1, border_color=BORDER)
    status_row = ctk.CTkFrame(search_inner, fg_color="transparent")
    status_row.pack(fill="x", pady=(10, 0))
    status_row.grid_columnconfigure(0, weight=1)
    detect_chip_lbl = ctk.CTkLabel(
        status_row,
        text="Détection…",
        font=F_CAP,
        text_color=TEXT_MUTED,
        anchor="w",
        justify="left",
        wraplength=640,
    )
    detect_chip_lbl.grid(row=0, column=0, sticky="w")
    cost_hint_lbl = ctk.CTkLabel(status_row, text="", font=F_CAP, text_color=BURNT_SIENNA, anchor="e")
    cost_hint_lbl.grid(row=0, column=1, sticky="e", padx=(12, 0))
    _preview_gen: Dict[str, int] = {"n": 0}
    _preview_anim: Dict[str, Any] = {"id": None}

    examples_row = ctk.CTkFrame(search_inner, fg_color="transparent")
    examples_row.pack(fill="x", pady=(12, 0))
    ctk.CTkLabel(examples_row, text="Exemples", font=F_CAP, text_color=TEXT_MUTED).pack(side="left", padx=(0, 8))

    def _pick_example(txt: str) -> None:
        idle_search_var.set(txt)
        _refresh_search_preview()

    from .market import local_autocomplete

    for ex in local_autocomplete("", limit=5):
        ctk.CTkButton(
            examples_row,
            text=ex[:28] + ("…" if len(ex) > 28 else ""),
            height=26,
            corner_radius=RADIUS_GHOST,
            fg_color=BG,
            hover_color="#3a2218",
            text_color=TEXT_MUTED,
            font=F_CAP,
            command=lambda t=ex: _pick_example(t),
        ).pack(side="left", padx=3)

    refine_card = ctk.CTkFrame(
        idle_inner, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER
    )
    refine_card.pack(fill="x", pady=(0, 16))
    refine_inner = ctk.CTkFrame(refine_card, fg_color="transparent")
    refine_inner.pack(fill="x", padx=20, pady=16)
    ctk.CTkLabel(refine_inner, text="Budget et critères", font=F_H2, text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(
        refine_inner,
        text="Fourchette prix, qualité, marque, lumens, résolution, HDMI, usage — un seul endroit avant le scrape.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=860,
        justify="left",
    ).pack(anchor="w", pady=(4, 10))
    refine_host = ctk.CTkFrame(refine_inner, fg_color="transparent")
    refine_host.pack(fill="x")
    refine_host.grid_columnconfigure(1, weight=1)
    refine_host.grid_columnconfigure(3, weight=1)
    refine_summary_lbl = ctk.CTkLabel(
        refine_inner, text="", font=F_CAP, text_color=BURNT_SIENNA, anchor="w", wraplength=860, justify="left"
    )
    refine_summary_lbl.pack(anchor="w", pady=(8, 0))
    refine_chip_groups: Dict[str, Dict[str, Any]] = {}

    plat_card = ctk.CTkFrame(
        idle_inner, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER
    )
    plat_card.pack(fill="x", pady=(0, 16))
    plat_inner = ctk.CTkFrame(plat_card, fg_color="transparent")
    plat_inner.pack(fill="x", padx=20, pady=16)
    plat_hdr = ctk.CTkFrame(plat_inner, fg_color="transparent")
    plat_hdr.pack(fill="x", pady=(0, 6))
    ctk.CTkLabel(plat_hdr, text="Sources à scanner", font=F_H2, text_color=TEXT).pack(side="left")
    plat_auto_var = ctk.BooleanVar(value=True)
    ctk.CTkCheckBox(
        plat_hdr,
        text="Auto plateformes",
        variable=plat_auto_var,
        font=F_CAP,
        text_color=TEXT,
        fg_color=BURNT_SIENNA,
        command=lambda: (_reset_plat_auto() if plat_auto_var.get() else None, _schedule_search_preview(200)),
    ).pack(side="right", padx=(0, 8))
    plat_reset_btn = ctk.CTkButton(
        plat_hdr,
        text="Réappliquer auto",
        height=26,
        width=120,
        corner_radius=RADIUS_GHOST,
        fg_color=BG,
        hover_color="#3a2218",
        text_color=TEXT_MUTED,
        font=F_CAP,
    )
    plat_reset_btn.pack(side="right")
    plat_hint_lbl = ctk.CTkLabel(
        plat_inner,
        text="",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=860,
        justify="left",
    )
    plat_hint_lbl.pack(anchor="w", pady=(0, 8))
    plat_auto_chips = ctk.CTkFrame(plat_inner, fg_color="transparent")
    plat_auto_chips.pack(fill="x", pady=(0, 8))

    def _render_plat_auto_chips(profile: Optional[Dict[str, Any]] = None) -> None:
        for w in plat_auto_chips.winfo_children():
            w.destroy()
        if not plat_auto_var.get():
            return
        prof = profile or {}
        on = prof.get("platforms_on") or prof.get("platforms") or []
        if not on:
            return
        from .engine import platform_style as _ps

        ctk.CTkLabel(plat_auto_chips, text="Sélection auto :", font=F_CAP, text_color=TEXT_MUTED).pack(
            side="left", padx=(0, 6)
        )
        for pk in on:
            st = _ps(str(pk))
            ctk.CTkLabel(
                plat_auto_chips,
                text=st.label,
                font=F_CAP,
                fg_color=st.color,
                text_color=st.text_color,
                corner_radius=8,
                height=24,
            ).pack(side="left", padx=3)

    plat_grid = ctk.CTkFrame(plat_inner, fg_color="transparent")
    plat_grid.pack(fill="x")
    for c in range(3):
        plat_grid.grid_columnconfigure(c, weight=1)

    use_ai_var = ctk.BooleanVar(value=use_ai)
    plat_vinted_var = ctk.BooleanVar(value=True)
    plat_lbc_var = ctk.BooleanVar(value=True)
    plat_ebay_var = ctk.BooleanVar(value=False)
    plat_card_refs: Dict[str, Any] = {}

    def _toggle_plat_card(key: str, var: ctk.BooleanVar) -> None:
        if plat_auto_var.get():
            plat_auto_var.set(False)
        state.plat_user_override = True
        var.set(not var.get())
        _style_plat_card(key)
        _schedule_search_preview(400)

    def _style_plat_card(key: str) -> None:
        ref = plat_card_refs.get(key)
        if not ref:
            return
        fr, var, st = ref["frame"], ref["var"], ref["style"]
        on = bool(var.get())
        fr.configure(
            border_color=st.color if on else BORDER,
            border_width=2 if on else 1,
            fg_color="#2a1810" if on else BG,
        )

    from .engine import platform_style as _plat_style

    _plat_defs = (
        ("vinted", plat_vinted_var),
        ("leboncoin", plat_lbc_var),
        ("ebay", plat_ebay_var),
    )
    for i, (pkey, pvar) in enumerate(_plat_defs):
        st = _plat_style(pkey)
        pfr = ctk.CTkFrame(plat_grid, fg_color=BG, corner_radius=10, border_width=1, border_color=BORDER, cursor="hand2")
        pfr.grid(row=i // 3, column=i % 3, sticky="ew", padx=5, pady=5)
        ctk.CTkLabel(pfr, text=st.label, font=F_BODY, text_color=st.text_color if pkey == "cashexpress" else TEXT).pack(
            anchor="w", padx=12, pady=(10, 0)
        )
        ctk.CTkLabel(
            pfr,
            text={
                "vinted": "Prioritaire",
                "leboncoin": "FR local",
                "ebay": "BIN — lent, optionnel",
            }.get(pkey, ""),
            font=F_CAP,
            text_color=TEXT_MUTED,
        ).pack(anchor="w", padx=12, pady=(0, 10))
        plat_card_refs[pkey] = {"frame": pfr, "var": pvar, "style": st}
        pfr.bind("<Button-1>", lambda _e, k=pkey, v=pvar: _toggle_plat_card(k, v))
        for w in pfr.winfo_children():
            w.bind("<Button-1>", lambda _e, k=pkey, v=pvar: _toggle_plat_card(k, v))
        _style_plat_card(pkey)

    ai_card = ctk.CTkFrame(
        idle_inner, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER
    )
    ai_card.pack(fill="x", pady=(0, 20))
    ai_inner = ctk.CTkFrame(ai_card, fg_color="transparent")
    ai_inner.pack(fill="x", padx=20, pady=16)
    ctk.CTkLabel(ai_inner, text="Analyse intelligente", font=F_H2, text_color=TEXT).pack(anchor="w")
    ctk.CTkCheckBox(
        ai_inner,
        text="Agent IA (triage, enrichissement top deals, vision, Top 5)",
        variable=use_ai_var,
        font=F_BODY,
        text_color=TEXT,
        fg_color=BURNT_SIENNA,
    ).pack(anchor="w", pady=(10, 0))
    ctk.CTkLabel(
        ai_inner,
        text="Profil unique optimisé : filtre specs précoce + cascade IA économe + vision sur le top.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=860,
        justify="left",
    ).pack(anchor="w", pady=(8, 0))

    run_btn_idle = ctk.CTkButton(
        idle_inner,
        text="Configurer l’analyse intelligente →",
        height=52,
        width=320,
        corner_radius=RADIUS_PILL,
        fg_color=BURNT_SIENNA,
        hover_color="#ff6a1a",
        text_color=WARM_CREAM,
        font=ctk.CTkFont(family=FONT_BODY[0], size=17, weight="bold"),
    )
    run_btn_idle.pack(anchor="w", pady=(4, 24))

    url_box = search_entry
    _suggest_after: Dict[str, Any] = {"id": None}
    _preview_after: Dict[str, Any] = {"id": None}

    def _depth_code() -> str:
        return "balanced"

    def _scrape_depth_code() -> str:
        return "pro"

    def _idle_platforms() -> List[str]:
        out: List[str] = []
        if plat_vinted_var.get():
            out.append("vinted")
        if plat_lbc_var.get():
            out.append("leboncoin")
        if plat_ebay_var.get():
            out.append("ebay")
        return out

    _plat_var_map = {
        "vinted": plat_vinted_var,
        "leboncoin": plat_lbc_var,
        "ebay": plat_ebay_var,
    }

    def _apply_platform_profile(prof: Dict[str, Any], *, force: bool = False) -> None:
        if state.plat_user_override and not force:
            return
        if plat_auto_var.get():
            on = set(prof.get("platforms_on") or prof.get("platforms") or [])
            for key, var in _plat_var_map.items():
                var.set(key in on)
                _style_plat_card(key)
            hint = str(prof.get("hint") or prof.get("reason") or "").strip()
            plat_hint_lbl.configure(text=hint)
            _render_plat_auto_chips(prof)
            return
        on = set(prof.get("platforms_on") or [])
        for key, var in _plat_var_map.items():
            var.set(key in on)
            _style_plat_card(key)
        hint = str(prof.get("hint") or "").strip()
        plat_hint_lbl.configure(text=hint)
        _render_plat_auto_chips(prof)

    _refine_layout_key: Dict[str, Any] = {"key": None}

    def _stop_preview_anim() -> None:
        aid = _preview_anim.get("id")
        if aid is not None:
            try:
                root.after_cancel(aid)
            except Exception:
                pass
            _preview_anim["id"] = None

    def _update_refine_summary_only() -> None:
        from .market import refinements_to_buyer_notes

        refine_summary_lbl.configure(
            text=refinements_to_buyer_notes(state.idle_refinements) or "Affinez avec les menus ci-dessus."
        )

    def _rebuild_refine_chips(category: str, query: str, specs: List[Dict[str, Any]]) -> None:
        cat = (category or "").strip()
        spec_key = tuple((str(s.get("id") or ""), str(s.get("type") or "single")) for s in specs)
        layout_key = (cat, spec_key)
        if layout_key == _refine_layout_key.get("key") and refine_host.winfo_children():
            _update_refine_summary_only()
            return
        if cat and cat != state.last_preview_category:
            state.idle_refinements.clear()
            state.last_preview_category = cat
        for w in refine_host.winfo_children():
            w.destroy()
        refine_chip_groups.clear()
        _refine_layout_key["key"] = None
        if not specs:
            refine_card.pack_forget()
            refine_summary_lbl.configure(text="")
            return
        if not refine_card.winfo_ismapped():
            refine_card.pack(fill="x", pady=(0, 16), before=plat_card)
        row_idx = 0
        col_pair = 0

        def _place_row(label_widget, control_widget) -> None:
            nonlocal row_idx, col_pair
            base_col = 0 if col_pair == 0 else 2
            label_widget.grid(row=row_idx, column=base_col, sticky="w", padx=(0, 8), pady=5)
            control_widget.grid(row=row_idx, column=base_col + 1, sticky="ew", padx=(0, 16 if col_pair == 0 else 0), pady=5)
            if col_pair == 1:
                row_idx += 1
            col_pair = 1 - col_pair

        for spec in specs:
            sid = str(spec["id"])
            spec_type = str(spec.get("type") or "single")
            label = ctk.CTkLabel(
                refine_host,
                text=str(spec.get("label") or sid),
                font=F_BODY,
                text_color=TEXT,
                anchor="w",
            )

            if spec_type == "slider":
                min_v = int(spec.get("min", 0))
                max_v = int(spec.get("max", 5000))
                step = max(1, int(spec.get("step", 500)))
                default_val = state.idle_refinements.get(sid)
                if default_val is None:
                    default_val = str(spec.get("default") or min_v)
                try:
                    cur = int(float(default_val or min_v))
                except (TypeError, ValueError):
                    cur = min_v
                cur = max(min_v, min(max_v, min_v + round((cur - min_v) / step) * step))
                state.idle_refinements[sid] = str(cur)
                val_lbl = ctk.CTkLabel(refine_host, text=f"{cur} lm", font=F_CAP, text_color=BURNT_SIENNA, width=64)
                slider_fr = ctk.CTkFrame(refine_host, fg_color="transparent")
                slider_fr.grid_columnconfigure(0, weight=1)

                def _on_slider(v: float, _sid: str = sid, _lbl=val_lbl, _min=min_v, _step=step) -> None:
                    n = int(float(v))
                    n = _min + round((n - _min) / _step) * _step
                    state.idle_refinements[_sid] = str(n)
                    _lbl.configure(text=f"{n} lm")
                    _update_refine_summary_only()

                steps_n = max(1, (max_v - min_v) // step)
                slider = ctk.CTkSlider(
                    slider_fr,
                    from_=min_v,
                    to=max_v,
                    number_of_steps=steps_n,
                    command=_on_slider,
                    fg_color=BORDER,
                    progress_color=BURNT_SIENNA,
                    button_color=BURNT_SIENNA,
                    button_hover_color="#ff6a1a",
                )
                slider.set(cur)
                slider.grid(row=0, column=0, sticky="ew", padx=(0, 8))
                val_lbl.grid(row=0, column=1)
                _place_row(label, slider_fr)
                refine_chip_groups[sid] = {"slider": slider, "label": val_lbl}
                continue

            if spec_type == "number":
                default_val = state.idle_refinements.get(sid)
                if default_val is None:
                    default_val = str(spec.get("default") or "")
                state.idle_refinements[sid] = default_val
                num_var = ctk.StringVar(value=default_val)

                def _on_num(*_a, _sid: str = sid, _v=num_var) -> None:
                    state.idle_refinements[_sid] = _v.get().strip()
                    _update_refine_summary_only()

                entry = ctk.CTkEntry(
                    refine_host,
                    textvariable=num_var,
                    width=100,
                    placeholder_text=str(spec.get("placeholder") or ""),
                    font=F_CAP,
                    fg_color=BG,
                    text_color=TEXT,
                )
                num_var.trace_add("write", _on_num)
                _place_row(label, entry)
                refine_chip_groups[sid] = {"entry": entry, "var": num_var}
                continue

            opts = list(spec.get("options") or [])
            if not opts:
                continue
            labels = [str(o.get("label") or o.get("value") or "") for o in opts]
            val_by_label = {labels[i]: str(opts[i].get("value", "")) for i in range(len(opts))}
            default_val = state.idle_refinements.get(sid)
            if default_val is None:
                default_val = str(spec.get("default") or opts[0].get("value", ""))
            default_lbl = labels[0]
            for lbl in labels:
                if val_by_label.get(lbl, "") == default_val:
                    default_lbl = lbl
                    break
            state.idle_refinements[sid] = default_val
            menu_var = ctk.StringVar(value=default_lbl)

            def _on_refine_choice(
                choice: str,
                _sid: str = sid,
                _map: Dict[str, str] = val_by_label,
            ) -> None:
                state.idle_refinements[_sid] = _map.get(choice, "")
                _update_refine_summary_only()

            menu = ctk.CTkOptionMenu(
                refine_host,
                values=labels,
                variable=menu_var,
                command=_on_refine_choice,
                font=F_CAP,
                fg_color=BG,
                button_color=BURNT_SIENNA,
                button_hover_color="#ff6a1a",
                dropdown_fg_color=DARK_CORK,
                dropdown_text_color=TEXT,
                width=200,
                anchor="w",
            )
            menu.set(default_lbl)
            _place_row(label, menu)
            refine_chip_groups[sid] = {"menu": menu, "map": val_by_label, "var": menu_var}
        if col_pair == 1:
            row_idx += 1
        _refine_layout_key["key"] = layout_key
        _update_refine_summary_only()
        try:
            refine_host.update_idletasks()
        except Exception:
            pass

    def _hide_suggestions() -> None:
        suggest_popup.pack_forget()
        for w in suggest_popup.winfo_children():
            w.destroy()

    def _show_suggestions(items: List[str]) -> None:
        _hide_suggestions()
        if not items:
            return
        suggest_popup.pack(fill="x", pady=(6, 0), after=search_row)
        for s in items[:7]:
            ctk.CTkButton(
                suggest_popup,
                text=s,
                anchor="w",
                height=30,
                fg_color="transparent",
                hover_color="#3a2218",
                text_color=TEXT,
                font=F_CAP,
                command=lambda t=s: (idle_search_var.set(t), _hide_suggestions(), _refresh_search_preview()),
            ).pack(fill="x", padx=4, pady=1)

    def _apply_search_preview(gen: int, prev: Dict[str, Any], est: Dict[str, Any], raw: str) -> None:
        if gen != _preview_gen["n"]:
            return
        state.preview_busy = False
        _stop_preview_anim()
        cat = str(prev.get("category") or "—")
        bits = [f"Marché : {cat}"]
        if prev.get("model_label"):
            bits.append(str(prev["model_label"]))
        if prev.get("price_hint"):
            bits.append(str(prev["price_hint"]))
        refined_q = str(prev.get("refined_query") or raw).strip()
        if refined_q.lower() != raw.lower() and not raw.startswith("http"):
            short = refined_q if len(refined_q) <= 48 else refined_q[:45] + "…"
            bits.append(f"→ « {short} »")
        detect_chip_lbl.configure(text=" · ".join(bits))
        cost_hint_lbl.configure(text=str(est.get("summary") or ""))
        specs = list(prev.get("refinement_specs") or [])
        _rebuild_refine_chips(cat, raw, specs)
        prof = prev.get("platform_profile") or {}
        _apply_platform_profile(prof)

    def _run_search_preview() -> None:
        _preview_after["id"] = None
        raw = idle_search_var.get().strip()
        if not raw:
            _preview_gen["n"] += 1
            state.preview_busy = False
            _stop_preview_anim()
            detect_chip_lbl.configure(text="Tapez une recherche ou collez une URL")
            cost_hint_lbl.configure(text="")
            refine_summary_lbl.configure(text="")
            _refine_layout_key["key"] = None
            state.last_preview_category = ""
            for w in refine_host.winfo_children():
                w.destroy()
            refine_chip_groups.clear()
            refine_card.pack_forget()
            return
        gen = _preview_gen["n"] + 1
        _preview_gen["n"] = gen
        state.preview_busy = True
        detect_chip_lbl.configure(text="Analyse locale…")
        cost_hint_lbl.configure(text="")

        def _anim_tick(g: int = gen, step: int = 0) -> None:
            if g != _preview_gen["n"] or not state.preview_busy:
                return
            dots = "." * ((step % 3) + 1)
            detect_chip_lbl.configure(text=f"Analyse locale{dots}")
            _preview_anim["id"] = root.after(380, lambda: _anim_tick(g, step + 1))

        _stop_preview_anim()
        _anim_tick()
        q_refs = dict(state.idle_refinements)
        q_plats = _idle_platforms()
        q_depth = _depth_code()

        def _work() -> None:
            from .market import estimate_run_cost, interactive_search_preview

            try:
                prev = interactive_search_preview(raw, q_refs)
                est = estimate_run_cost(q_plats, q_depth)
            except Exception:
                prev = {"category": "generic", "refinement_specs": []}
                est = {"summary": ""}
            root.after(0, lambda: _apply_search_preview(gen, prev, est, raw))

        threading.Thread(target=_work, daemon=True, name="vf-preview").start()

    def _schedule_search_preview(delay_ms: int = 300) -> None:
        pid = _preview_after.get("id")
        if pid is not None:
            try:
                root.after_cancel(pid)
            except Exception:
                pass
        _preview_after["id"] = root.after(delay_ms, _run_search_preview)

    def _refresh_search_preview() -> None:
        _schedule_search_preview(300)

    def _on_search_key(_event: Any = None) -> None:
        aid = _suggest_after.get("id")
        if aid is not None:
            try:
                root.after_cancel(aid)
            except Exception:
                pass

        def _later() -> None:
            q = idle_search_var.get().strip()
            if len(q) >= 2:
                _show_suggestions(local_autocomplete(q))
            else:
                _hide_suggestions()
            _schedule_search_preview(280)

        _suggest_after["id"] = root.after(220, _later)

    search_entry.bind("<KeyRelease>", _on_search_key)
    for v in (plat_vinted_var, plat_lbc_var, plat_ebay_var):
        v.trace_add("write", lambda *_: _schedule_search_preview(400))

    def _reset_plat_auto() -> None:
        state.plat_user_override = False
        plat_auto_var.set(True)
        _refresh_search_preview()

    plat_reset_btn.configure(command=_reset_plat_auto)

    if start_url:
        _refresh_search_preview()

    # ── SETUP (poids + filtres avant scrape)
    setup_frame = ctk.CTkFrame(body, fg_color=BG)
    setup_frame.grid_columnconfigure(0, weight=1)
    setup_frame.grid_rowconfigure(0, weight=1)
    setup_scroll = ctk.CTkScrollableFrame(setup_frame, fg_color=BG, corner_radius=0)
    setup_scroll.grid(row=0, column=0, sticky="nsew", padx=32, pady=20)

    ctk.CTkLabel(
        setup_scroll,
        text="Configurer l'analyse",
        font=F_TITLE,
        text_color=TEXT,
    ).pack(anchor="w", pady=(0, 4))
    ctk.CTkLabel(
        setup_scroll,
        text="Marché détecté, aperçu coût/temps, profil d'achat et poids du score. "
        "Prix et specs produit : écran de recherche précédent.",
        font=F_BODY,
        text_color=TEXT_MUTED,
        wraplength=920,
        justify="left",
    ).pack(anchor="w", pady=(0, 14))

    setup_market_card = ctk.CTkFrame(setup_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    setup_market_card.pack(fill="x", pady=(0, 12))
    mkt_inner = ctk.CTkFrame(setup_market_card, fg_color="transparent")
    mkt_inner.pack(fill="x", padx=16, pady=14)
    setup_url_lbl = ctk.CTkLabel(mkt_inner, text="", font=F_CAP, text_color=TEXT_MUTED, wraplength=880, justify="left")
    setup_url_lbl.pack(anchor="w")
    setup_market_lbl = ctk.CTkLabel(mkt_inner, text="Marché : —", font=F_H2, text_color=WARM_CREAM)
    setup_market_lbl.pack(anchor="w", pady=(8, 4))
    setup_crit_lbl = ctk.CTkLabel(
        mkt_inner,
        text="",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=880,
        justify="left",
    )
    setup_crit_lbl.pack(anchor="w")

    setup_brief_card = ctk.CTkFrame(setup_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BURNT_SIENNA)
    setup_brief_card.pack(fill="x", pady=(0, 12))
    brief_inner = ctk.CTkFrame(setup_brief_card, fg_color="transparent")
    brief_inner.pack(fill="x", padx=16, pady=14)
    ctk.CTkLabel(brief_inner, text="Brief marché (IA honnête)", font=F_H2, text_color=WARM_CREAM).pack(anchor="w")
    setup_brief_lbl = ctk.CTkLabel(
        brief_inner,
        text="Le brief s’affiche après détection du marché…",
        font=F_BODY,
        text_color=TEXT,
        wraplength=900,
        justify="left",
        anchor="w",
    )
    setup_brief_lbl.pack(fill="x", anchor="w", pady=(8, 4))
    setup_pitfalls_lbl = ctk.CTkLabel(
        brief_inner,
        text="",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=900,
        justify="left",
        anchor="w",
    )
    setup_pitfalls_lbl.pack(fill="x", anchor="w")
    setup_strategy_lbl = ctk.CTkLabel(
        brief_inner,
        text="",
        font=F_CAP,
        text_color=OK,
        wraplength=900,
        justify="left",
        anchor="w",
    )
    setup_strategy_lbl.pack(fill="x", anchor="w", pady=(6, 0))

    setup_deal_card = ctk.CTkFrame(setup_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    setup_deal_card.pack(fill="x", pady=(0, 12))
    deal_inner = ctk.CTkFrame(setup_deal_card, fg_color="transparent")
    deal_inner.pack(fill="x", padx=16, pady=14)
    ctk.CTkLabel(deal_inner, text="Profil d'achat", font=F_H2, text_color=TEXT).pack(anchor="w")
    setup_deal_hint_lbl = ctk.CTkLabel(
        deal_inner,
        text="Oriente le scoring IA — adapté au marché détecté.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=900,
        justify="left",
    )
    setup_deal_hint_lbl.pack(anchor="w", pady=(4, 10))
    deal_mode_var = ctk.StringVar(value="balanced")
    deal_seg_row = ctk.CTkFrame(deal_inner, fg_color="transparent")
    deal_seg_row.pack(fill="x")

    def _on_deal_mode(_v: str = "") -> None:
        from .market import DEAL_HUNT_MODES, deal_hunt_notes

        mode = deal_mode_var.get() or "balanced"
        state.deal_hunt_mode = mode
        meta = DEAL_HUNT_MODES.get(mode, DEAL_HUNT_MODES["balanced"])
        setup_deal_hint_lbl.configure(text=str(meta.get("hint") or ""))
        note = deal_hunt_notes(mode, state.market_category or "")
        if note and not buyer_extra_box.get("0.0", "end").strip():
            pass
        _update_prio_preview()

    from .market import DEAL_HUNT_MODES as _DEAL_MODES

    for mid, meta in _DEAL_MODES.items():
        ctk.CTkRadioButton(
            deal_seg_row,
            text=str(meta.get("label") or mid),
            variable=deal_mode_var,
            value=mid,
            font=F_BODY,
            text_color=TEXT,
            fg_color=BURNT_SIENNA,
            command=_on_deal_mode,
        ).pack(side="left", padx=(0, 16))

    setup_preflight_card = ctk.CTkFrame(setup_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    setup_preflight_card.pack(fill="x", pady=(0, 12))
    pf_inner = ctk.CTkFrame(setup_preflight_card, fg_color="transparent")
    pf_inner.pack(fill="x", padx=16, pady=14)
    ctk.CTkLabel(pf_inner, text="Récapitulatif", font=F_H2, text_color=TEXT).pack(anchor="w")
    setup_prio_hint_lbl = ctk.CTkLabel(
        pf_inner,
        text="Critères hérités de l'écran de recherche (prix, specs) — modifiables en revenant en arrière.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=900,
        justify="left",
    )
    setup_prio_hint_lbl.pack(anchor="w", pady=(4, 10))
    unified_toolbar = ctk.CTkFrame(pf_inner, fg_color="transparent")
    unified_toolbar.pack(fill="x", pady=(0, 8))
    suggest_count_lbl = ctk.CTkLabel(unified_toolbar, text="0 actif", font=F_CAP, text_color=BURNT_SIENNA)
    suggest_count_lbl.pack(side="right")
    setup_preflight_host = ctk.CTkFrame(pf_inner, fg_color=BG, corner_radius=8)
    setup_preflight_host.pack(fill="x")
    setup_run_preview_lbl = ctk.CTkLabel(
        pf_inner,
        text="",
        font=F_CAP,
        text_color=BURNT_SIENNA,
        wraplength=880,
        justify="left",
        anchor="w",
    )
    setup_run_preview_lbl.pack(fill="x", anchor="w", pady=(8, 0))
    ctk.CTkLabel(pf_inner, text="Notes libres (optionnel)", font=F_BODY, text_color=TEXT).pack(anchor="w", pady=(12, 0))
    buyer_extra_box = ctk.CTkTextbox(pf_inner, height=64, fg_color=BG, text_color=TEXT, font=F_CAP, wrap="word")
    buyer_extra_box.pack(fill="x", pady=(6, 8))
    ctk.CTkLabel(pf_inner, text="Aperçu envoyé au pipeline", font=F_CAP, text_color=TEXT_MUTED).pack(anchor="w")
    setup_prio_preview = ctk.CTkLabel(
        pf_inner,
        text="—",
        font=F_CAP,
        text_color=WARM_CREAM,
        wraplength=880,
        justify="left",
        anchor="w",
    )
    setup_prio_preview.pack(fill="x", anchor="w", pady=(4, 0))

    setup_price_card = ctk.CTkFrame(setup_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    setup_price_min_var = ctk.StringVar(value="")
    setup_price_max_var = ctk.StringVar(value="")
    setup_price_auto_lbl = ctk.CTkLabel(setup_scroll, text="")
    setup_price_hint_lbl = ctk.CTkLabel(setup_scroll, text="")
    # Prix unique : défini sur l'écran de recherche (idle_refinements), pas ici.

    setup_weights_card = ctk.CTkFrame(setup_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    setup_weights_card.pack(fill="x", pady=(0, 8))
    w_inner = ctk.CTkFrame(setup_weights_card, fg_color="transparent")
    w_inner.pack(fill="x", padx=16, pady=14)
    ctk.CTkLabel(w_inner, text="Poids du score (tri des annonces)", font=F_H2, text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(
        w_inner,
        text="Glissez pour favoriser prix, complétude du pack, risques, etc. — sans refaire un scrape.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        wraplength=880,
        justify="left",
    ).pack(anchor="w", pady=(4, 10))
    weights_toolbar = ctk.CTkFrame(w_inner, fg_color="transparent")
    weights_toolbar.pack(fill="x", pady=(0, 8))

    setup_slider_host = ctk.CTkFrame(w_inner, fg_color="transparent")
    setup_slider_host.pack(fill="x")
    setup_slider_host.grid_columnconfigure(0, weight=1)
    setup_slider_host.grid_columnconfigure(1, weight=1)

    setup_btn_row = ctk.CTkFrame(setup_frame, fg_color="transparent")
    setup_btn_row.grid(row=1, column=0, sticky="ew", padx=32, pady=16)

    # ── RUNNING
    run_frame = ctk.CTkFrame(body, fg_color=BG)
    run_frame.grid_columnconfigure(0, weight=0, minsize=460)
    run_frame.grid_columnconfigure(1, weight=1)
    run_frame.grid_rowconfigure(0, weight=1)

    run_left = ctk.CTkFrame(run_frame, fg_color=BG, width=480)
    run_left.grid(row=0, column=0, sticky="nsew", padx=(28, 12), pady=24)

    run_inner = ctk.CTkFrame(run_left, fg_color=BG)
    run_inner.pack(fill="both", expand=True, anchor="nw")

    ctk.CTkLabel(run_inner, text="Analyse en cours", font=F_TITLE, text_color=TEXT).pack(anchor="w", pady=(0, 12))
    human_lbl = ctk.CTkLabel(run_inner, text="Initialisation…", font=F_H2, text_color=WARM_CREAM, wraplength=440, justify="left")
    human_lbl.pack(anchor="w", pady=(0, 8))
    clock_card = ctk.CTkFrame(run_inner, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    clock_card.pack(fill="x", pady=(0, 20))
    clock_inner = ctk.CTkFrame(clock_card, fg_color="transparent")
    clock_inner.pack(fill="x", padx=24, pady=20)
    timer_lbl = ctk.CTkLabel(
        clock_inner,
        text="00:00",
        font=ctk.CTkFont(family=FONT_DISPLAY[0], size=56),
        text_color=BURNT_SIENNA,
    )
    timer_lbl.pack(anchor="w", pady=(0, 4))
    timer_meta = ctk.CTkLabel(clock_inner, text="Initialisation…", font=F_CAP, text_color=TEXT_MUTED)
    timer_meta.pack(anchor="w", pady=(0, 4))
    eta_lbl = ctk.CTkLabel(clock_inner, text="", font=F_BODY, text_color=WARM_CREAM)
    eta_lbl.pack(anchor="w", pady=(0, 2))
    progress = ctk.CTkProgressBar(clock_inner, height=6, progress_color=BURNT_SIENNA, fg_color=BORDER, corner_radius=3)
    progress.pack(fill="x", pady=(16, 0))
    progress.set(0)
    progress_pct_lbl = ctk.CTkLabel(clock_inner, text="0 %", font=F_CAP, text_color=TEXT_MUTED)
    progress_pct_lbl.pack(anchor="e", pady=(6, 0))

    run_clock: Dict[str, Any] = {
        "t0": 0.0,
        "after_id": None,
        "progress": {},
        "extra_pause": 0.0,
        "smooth_pct": 0.0,
        "smooth_remaining": None,
        "smooth_total": None,
    }

    steps_box = ctk.CTkFrame(run_inner, fg_color="transparent")
    steps_box.pack(fill="x")
    step_labels: Dict[int, ctk.CTkLabel] = {}
    for num, short, _desc in STEPS:
        row = ctk.CTkFrame(steps_box, fg_color="transparent")
        row.pack(fill="x", pady=4)
        dot = ctk.CTkLabel(row, text="○", width=28, font=F_BODY, text_color=TEXT_MUTED)
        dot.pack(side="left")
        lbl = ctk.CTkLabel(row, text=f"{num}. {short}", font=F_BODY, text_color=TEXT_MUTED, anchor="w")
        lbl.pack(side="left", fill="x")
        step_labels[num] = lbl

    summary_run = ctk.CTkLabel(run_inner, text="", font=F_CAP, text_color=TEXT_MUTED, wraplength=520, justify="left")
    summary_run.pack(anchor="w", pady=(16, 0))
    plat_prog_box = ctk.CTkFrame(run_inner, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    plat_prog_box.pack(fill="x", pady=(12, 0))
    plat_prog_inner = ctk.CTkFrame(plat_prog_box, fg_color="transparent")
    plat_prog_inner.pack(fill="x", padx=14, pady=10)
    ctk.CTkLabel(plat_prog_inner, text="Catalogue par plateforme", font=F_CAP, text_color=TEXT_MUTED).pack(anchor="w")
    plat_prog_rows = ctk.CTkFrame(plat_prog_inner, fg_color="transparent")
    plat_prog_rows.pack(fill="x", pady=(6, 0))
    plat_prog_labels: Dict[str, ctk.CTkLabel] = {}

    def _init_plat_prog_labels(plats: List[str]) -> None:
        for w in plat_prog_rows.winfo_children():
            w.destroy()
        plat_prog_labels.clear()
        from .engine import platform_style as _ps

        for pk in plats:
            st = _ps(pk)
            row = ctk.CTkFrame(plat_prog_rows, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=st.label, font=F_CAP, text_color=st.text_color, width=88, anchor="w").pack(side="left")
            lbl = ctk.CTkLabel(row, text="en attente…", font=F_CAP, text_color=TEXT_MUTED, anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            plat_prog_labels[pk] = lbl

    def _update_plat_prog(progress: Dict[str, int], active: str = "", elapsed: Optional[Dict[str, float]] = None) -> None:
        for pk, lbl in plat_prog_labels.items():
            n = int(progress.get(pk) or 0)
            el = float((elapsed or {}).get(pk) or 0)
            if n > 0:
                lbl.configure(text=f"✓ {n} annonces · {format_clock(el)}", text_color=OK)
            elif pk == active:
                lbl.configure(text=f"scraping… {format_clock(el)}" if el > 0 else "scraping…", text_color=BURNT_SIENNA)
            else:
                lbl.configure(text="en attente…", text_color=TEXT_MUTED)

    run_log_compact_var = ctk.BooleanVar(value=True)
    run_log_panel = ctk.CTkFrame(run_frame, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    run_log_panel.grid(row=0, column=1, sticky="nsew", padx=(12, 28), pady=24)
    run_log_panel.grid_columnconfigure(0, weight=1)
    run_log_panel.grid_rowconfigure(1, weight=1)
    run_log_head = ctk.CTkFrame(run_log_panel, fg_color="transparent")
    run_log_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
    ctk.CTkLabel(run_log_head, text="Journal en direct", font=F_H2, text_color=WARM_CREAM).pack(side="left")
    ctk.CTkCheckBox(
        run_log_head,
        text="Compact",
        variable=run_log_compact_var,
        font=F_CAP,
        text_color=TEXT_MUTED,
        fg_color=BURNT_SIENNA,
        hover_color="#b84500",
        border_color=BORDER,
        width=18,
        height=18,
    ).pack(side="right", padx=(0, 8))
    run_log_count_lbl = ctk.CTkLabel(run_log_head, text="0 lignes", font=F_CAP, text_color=TEXT_MUTED)
    run_log_count_lbl.pack(side="right")
    run_log_live = ctk.CTkLabel(
        run_log_head,
        text="● LIVE",
        font=F_CAP,
        text_color=OK,
    )
    run_log_live.pack(side="right", padx=(0, 14))
    run_logs_box = ctk.CTkTextbox(
        run_log_panel,
        fg_color="#0c0806",
        text_color=TEXT_MUTED,
        font=F_MONO,
        wrap="word",
        corner_radius=8,
        border_width=0,
        activate_scrollbars=True,
    )
    run_logs_box.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 8))
    run_log_panel.grid_rowconfigure(1, weight=1)
    ctk.CTkLabel(
        run_log_panel,
        text="✓ succès   ⚠ alerte   ▸ étape   · défilement automatique",
        font=F_CAP,
        text_color=TEXT_MUTED,
    ).grid(row=2, column=0, sticky="w", padx=18, pady=(0, 12))

    run_log_state: Dict[str, Any] = {"lines": 0, "last_prog_sig": None, "compact": True}

    def _run_log_is_noise(line: str) -> bool:
        if not run_log_compact_var.get():
            return False
        low = line.lower().strip()
        if low.startswith("→ [") or low.startswith("→ "):
            return True
        if "page " in low and "/" in low and "—" in low:
            return True
        if "variante " in low and "/" in low:
            return True
        if low.startswith("vinted deep · bande"):
            return True
        if "bootstrap" in low and "session" in low:
            return True
        return False

    def _run_logs_text() -> Any:
        return getattr(run_logs_box, "_textbox", run_logs_box)

    def _init_run_log_tags() -> None:
        tb = _run_logs_text()
        for tag, color in (
            ("time", TEXT_MUTED),
            ("muted", TEXT_MUTED),
            ("step", BURNT_SIENNA),
            ("ok", OK),
            ("warn", WARN),
            ("err", DANGER),
            ("body", WARM_CREAM),
        ):
            try:
                tb.tag_configure(tag, foreground=color)
            except Exception:
                pass

    _init_run_log_tags()

    def _run_log_copy_selection() -> None:
        tb = _run_logs_text()
        try:
            chunk = tb.get("sel.first", "sel.last")
        except Exception:
            chunk = ""
        if chunk:
            root.clipboard_clear()
            root.clipboard_append(chunk)
            root.update()

    def _run_logs_key(_event: Any = None) -> Optional[str]:
        if _event and (_event.state & 0x4) and getattr(_event, "keysym", "").lower() == "c":
            _run_log_copy_selection()
            return "break"
        if _event and getattr(_event, "keysym", "") in ("Up", "Down", "Prior", "Next", "Home", "End"):
            return None
        return "break"

    try:
        _rl = _run_logs_text()
        _rl.bind("<Key>", _run_logs_key)
        _rl.configure(exportselection=True, selectbackground="#5c3d2a", selectforeground=WARM_CREAM)
    except Exception:
        pass

    def _run_log_tag(msg: str) -> str:
        s = (msg or "").strip()
        if s.startswith("✓"):
            return "ok"
        if s.startswith("⚠"):
            return "warn"
        if s.startswith("✕") or "erreur" in s.lower() or "error" in s.lower():
            return "err"
        if s.startswith("══") or s.startswith("---"):
            return "step"
        if s.startswith("  ") or s.startswith("\t"):
            return "muted"
        if any(s.startswith(f"{n}/") for n in range(1, 7)):
            return "step"
        return "body"

    def _trim_run_logs(max_lines: int = 900) -> None:
        tb = _run_logs_text()
        try:
            n = int(float(tb.index("end-1c").split(".")[0]))
        except (ValueError, TypeError):
            return
        if n > max_lines:
            tb.delete("1.0", f"{n - max_lines + 1}.0")
            run_log_state["lines"] = max_lines

    def _append_run_log(msg: str, *, tag: str = "") -> None:
        line = (msg or "").rstrip("\n")
        if not line:
            return
        if _run_log_is_noise(line):
            return
        tb = _run_logs_text()
        ttag = tag or _run_log_tag(line)
        ts = time.strftime("%H:%M:%S")
        try:
            tb.insert("end", f"{ts} ", "time")
            tb.insert("end", line + "\n", ttag)
            run_logs_box.see("end")
            run_log_state["lines"] = int(run_log_state.get("lines") or 0) + 1
            run_log_count_lbl.configure(text=f"{run_log_state['lines']} lignes")
            _trim_run_logs()
        except Exception:
            pass

    def _clear_run_logs() -> None:
        try:
            _run_logs_text().delete("1.0", "end")
        except Exception:
            pass
        run_log_state["lines"] = 0
        run_log_state["last_prog_sig"] = None
        run_log_count_lbl.configure(text="0 lignes")

    def _format_progress_detail(p: Dict[str, Any]) -> str:
        step = int(p.get("step") or 0)
        name = str(p.get("step_name") or "")
        human = str(p.get("human") or "").strip()
        pct = int(round(float(p.get("pct") or 0) * 100))
        bits: List[str] = [f"▸ Étape {step}/{len(STEPS)} · {name} · {pct}%"]
        if human:
            bits.append(f"   {human}")
        sub = str(p.get("subphase") or "")
        if sub:
            bits.append(f"   sous-phase : {sub}")
        if p.get("catalog_count") is not None:
            bits.append(f"   catalogue : {p['catalog_count']} annonces")
        pp = p.get("platform_progress")
        if isinstance(pp, dict) and pp:
            bits.append("   plateformes : " + ", ".join(f"{k}={v}" for k, v in sorted(pp.items())))
        cp, tp = p.get("catalog_page"), p.get("catalog_pages")
        if cp and tp:
            bits.append(f"   pagination : page {cp}/{tp}")
        if p.get("ai_before") is not None:
            bits.append(f"   IA titres : {p.get('ai_before')} → {p.get('ai_after', '…')}")
        if p.get("ai_doubtful_done") is not None and p.get("ai_doubtful_total"):
            bits.append(f"   lots IA : {p['ai_doubtful_done']}/{p['ai_doubtful_total']}")
        if p.get("main_items") is not None:
            bits.append(f"   produits : {p['main_items']} · accessoires exclus : {p.get('excluded_accessories', 0)}")
        sd, st = p.get("score_done"), p.get("score_total")
        if sd and st:
            bits.append(f"   scoring : {sd}/{st}")
        dd, dt = p.get("deep_done"), p.get("deep_total")
        if dd is not None and dt:
            bits.append(f"   descriptions : {dd}/{dt}")
        vd, vt = p.get("vision_done"), p.get("vision_total")
        if vd and vt:
            bits.append(f"   vision : photo {vd}/{vt}")
        eta = float(p.get("eta_sec") or 0)
        if eta > 0:
            bits.append(f"   ETA ~{format_clock(eta)} · budget {int(p.get('planned_sec') or 0)}s")
        return "\n".join(bits)

    def _log_progress_event(p: Dict[str, Any]) -> None:
        human = str(p.get("human") or "")
        step = int(p.get("step") or 0)
        vd = p.get("vision_done")
        vt = p.get("vision_total")
        vis_bucket = None
        if vd and vt:
            try:
                vis_bucket = int(vd) // max(1, int(vt) // 6)
            except (TypeError, ValueError):
                vis_bucket = int(vd) // 5
        sig = (
            step,
            str(p.get("step_name") or ""),
            human,
            p.get("catalog_page"),
            p.get("catalog_count"),
            (int(p.get("score_done") or 0) // 40) if p.get("score_done") else None,
            p.get("deep_done"),
            vis_bucket,
            p.get("ai_doubtful_done"),
        )
        prev = run_log_state.get("last_prog_sig")
        force = step != (prev[0] if prev else -1) or human != (prev[2] if prev else "")
        periodic = p.get("score_done") and int(p.get("score_done") or 0) % 80 == 0
        catalog_page = p.get("catalog_page")
        page_log = catalog_page and (catalog_page == 1 or int(catalog_page) % 2 == 0)
        if prev == sig and not force and not periodic and not page_log:
            return
        run_log_state["last_prog_sig"] = sig
        _append_run_log(_format_progress_detail(p), tag="step")

    def _stop_run_clock() -> None:
        aid = run_clock.get("after_id")
        if aid is not None:
            try:
                root.after_cancel(aid)
            except Exception:
                pass
        run_clock["after_id"] = None

    def _smooth_clock(prev: Optional[float], target: float, alpha: float = 0.14) -> float:
        if prev is None:
            return target
        return prev * (1.0 - alpha) + target * alpha

    def _tick_run_clock() -> None:
        if not state.running:
            return
        elapsed = time.monotonic() - float(run_clock.get("t0") or time.monotonic())
        timer_lbl.configure(text=format_clock(elapsed))

        p = run_clock.get("progress") or {}
        pct_raw = float(p.get("pct") or 0.0)
        planned = float(p.get("planned_sec") or 0.0)
        eta_payload = float(p.get("eta_sec") or 0.0)

        pct_show = max(0.0, min(0.995, pct_raw))
        run_clock["smooth_pct"] = pct_show
        progress.set(pct_show)
        progress_pct_lbl.configure(text=f"{int(round(pct_show * 100))} %")

        if pct_show >= 0.02:
            total_target = max(planned, elapsed / pct_show, elapsed + eta_payload)
            remain_target = max(0.0, total_target - elapsed)
        else:
            total_target = max(planned, elapsed + eta_payload, min(planned, 300.0))
            remain_target = max(0.0, total_target - elapsed)

        try:
            from vf_scrape.config import scrape_mode_profile

            cap_sec = float(scrape_mode_profile().catalog_eta_cap_sec)
            if cap_sec > 0:
                total_target = min(total_target, cap_sec + max(0.0, elapsed - cap_sec * 0.4))
                remain_target = min(remain_target, max(0.0, total_target - elapsed))
        except Exception:
            pass

        total_est = _smooth_clock(run_clock.get("smooth_total"), total_target, alpha=0.10)
        remain_show = _smooth_clock(run_clock.get("smooth_remaining"), remain_target, alpha=0.12)
        run_clock["smooth_total"] = total_est
        run_clock["smooth_remaining"] = remain_show

        step_name = str(p.get("step_name") or "")
        sub = str(p.get("subphase") or "")
        cat_n = p.get("catalog_n") or p.get("catalog_count")
        if step_name:
            meta = f"Étape · {step_name}"
            if sub == "specs":
                meta += " · Specs IA"
            elif sub == "audit":
                meta += " · Audit IA"
            elif sub == "buyer_fit":
                meta += " · Adéquation"
            elif sub == "vision":
                vd = p.get("vision_done")
                vt = p.get("vision_total")
                if vd and vt:
                    meta += f" · photo {vd}/{vt}"
            if cat_n:
                meta += f"  ·  {cat_n} annonces"
            cp, tp = p.get("catalog_page"), p.get("catalog_pages")
            if cp and tp:
                meta += f"  ·  p.{cp}/{tp}"
            timer_meta.configure(text=meta)

        eta_lbl.configure(
            text=f"Restant ~{format_clock(remain_show)}  ·  total ~{format_clock(total_est)}"
        )
        cat_n = p.get("catalog_n") or p.get("catalog_count")
        if state.running and cat_n and elapsed > 2:
            try:
                rate = float(cat_n) / elapsed
                header_stats.set(f"{format_clock(elapsed)} · {cat_n} annonces · {rate:.1f}/s")
            except (TypeError, ValueError):
                pass

        run_clock["after_id"] = root.after(120, _tick_run_clock)

    def _start_run_clock() -> None:
        _stop_run_clock()
        run_clock["t0"] = time.monotonic()
        run_clock["progress"] = {}
        run_clock["extra_pause"] = 0.0
        run_clock["smooth_pct"] = 0.0
        run_clock["smooth_remaining"] = None
        run_clock["smooth_total"] = None
        timer_lbl.configure(text="00:00")
        timer_meta.configure(text="Démarrage…")
        eta_lbl.configure(text="Estimation catalogue maximale (Vinted + LBC)")
        progress.set(0)
        progress_pct_lbl.configure(text="0 %")
        _tick_run_clock()

    # ── RESULTS (tabbed)
    results_frame = ctk.CTkFrame(body, fg_color=BG)
    results_frame.grid_columnconfigure(0, weight=1)
    results_frame.grid_rowconfigure(0, weight=1)

    tabs = ctk.CTkTabview(
        results_frame,
        fg_color=BG,
        segmented_button_fg_color=DARK_CORK,
        segmented_button_selected_color=BURNT_SIENNA,
        segmented_button_unselected_color=DARK_CORK,
        text_color=TEXT,
    )
    tabs.grid(row=0, column=0, sticky="nsew", padx=16, pady=8)
    tab_deals = tabs.add("Offres")
    tab_rejected = tabs.add("Rejetés")
    tab_doubtful = tabs.add("Doutes")
    tab_ai_selection = tabs.add("Sélection IA")
    tab_analysis = tabs.add("Analyse")
    tab_logs = tabs.add("Journal")

    tab_ai_selection.grid_columnconfigure(0, weight=1)
    tab_ai_selection.grid_rowconfigure(0, weight=1)
    selection_ia_scroll = ctk.CTkScrollableFrame(tab_ai_selection, fg_color=BG, corner_radius=0)
    selection_ia_scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    tab_analysis.grid_columnconfigure(0, weight=1)
    tab_analysis.grid_rowconfigure(0, weight=1)
    analysis_scroll = ctk.CTkScrollableFrame(tab_analysis, fg_color=BG, corner_radius=0)
    analysis_scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
    analysis_title_lbl = ctk.CTkLabel(
        analysis_scroll,
        text="Analyse de marché",
        font=F_H2,
        text_color=BURNT_SIENNA,
        anchor="w",
    )
    analysis_title_lbl.pack(fill="x", padx=12, pady=(12, 4))
    analysis_body = ctk.CTkTextbox(
        analysis_scroll,
        height=520,
        font=F_BODY,
        fg_color=DARK_CORK,
        text_color=TEXT,
        border_width=1,
        border_color=BORDER,
        wrap="word",
        activate_scrollbars=True,
    )
    analysis_body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    analysis_body.insert("1.0", "Lancez une analyse pour voir le marché, les pièges et le déroulé du tri.")
    analysis_body.configure(state="disabled")

    tab_deals.grid_columnconfigure(0, weight=1)
    tab_deals.grid_rowconfigure(0, weight=0)
    tab_deals.grid_rowconfigure(1, weight=1)

    toolbar = ctk.CTkFrame(tab_deals, fg_color="transparent")
    toolbar.grid(row=0, column=0, sticky="new", padx=8, pady=(8, 4))

    offers_main_scroll = ctk.CTkScrollableFrame(tab_deals, fg_color=BG, corner_radius=0)
    offers_main_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
    top_picks_fr = ctk.CTkFrame(
        offers_main_scroll,
        fg_color="#2a1810",
        corner_radius=RADIUS_CARD,
        border_width=2,
        border_color=BURNT_SIENNA,
    )
    top_picks_title = ctk.CTkLabel(
        top_picks_fr,
        text="Top 10 affaires (qualité / prix)",
        font=F_H2,
        text_color=BURNT_SIENNA,
        anchor="w",
    )
    top_picks_title.pack(fill="x", padx=14, pady=(12, 2))
    top_picks_note_box = ctk.CTkTextbox(
        top_picks_fr,
        height=108,
        font=F_CAP,
        fg_color=BG,
        text_color=TEXT_MUTED,
        border_width=1,
        border_color=BORDER,
        wrap="word",
        activate_scrollbars=True,
    )
    top_picks_note_box.pack(fill="x", padx=14, pady=(0, 6))
    top_picks_note_box.insert("1.0", "Synthèse IA après enrichissement du top 100.")
    top_picks_note_box.configure(state="disabled")
    top_picks_chips = ctk.CTkFrame(top_picks_fr, fg_color="transparent")
    top_picks_chips.pack(fill="x", padx=10, pady=(0, 12))
    top_picks_fr.pack(fill="x", pady=(0, 8), padx=2)
    top_picks_fr.pack_forget()
    top5_ia_fr = ctk.CTkFrame(
        selection_ia_scroll,
        fg_color="#1f140c",
        corner_radius=14,
        border_width=1,
        border_color="#e85d04",
    )
    top5_ia_title = ctk.CTkLabel(
        top5_ia_fr,
        text="Top 5 IA",
        font=F_H2,
        text_color="#ff8c42",
        anchor="w",
    )
    top5_ia_title.pack(fill="x", padx=14, pady=(12, 4))
    top5_ia_summary = ctk.CTkLabel(
        top5_ia_fr,
        text="Analyse IA des 5 meilleures annonces selon les critères de la catégorie.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        anchor="w",
        wraplength=720,
        justify="left",
    )
    top5_ia_summary.pack(fill="x", padx=14, pady=(0, 8))
    top5_ia_cards = ctk.CTkFrame(top5_ia_fr, fg_color="transparent")
    top5_ia_cards.pack(fill="x", padx=8, pady=(0, 12))
    top5_avoid_fr = ctk.CTkFrame(selection_ia_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
    top5_avoid_title = ctk.CTkLabel(top5_avoid_fr, text="À éviter", font=F_BODY, text_color=WARN, anchor="w")
    top5_avoid_title.pack(fill="x", padx=14, pady=(10, 4))
    top5_avoid_lbl = ctk.CTkLabel(
        top5_avoid_fr,
        text="Patterns à éviter selon la catégorie (jeux seuls, accessoires…).",
        font=F_CAP,
        text_color=TEXT_MUTED,
        anchor="w",
        wraplength=720,
        justify="left",
    )
    top5_avoid_lbl.pack(fill="x", padx=14, pady=(0, 12))
    top5_avoid_fr.pack_forget()
    top5_eval_lbl = ctk.CTkLabel(
        selection_ia_scroll,
        text="",
        font=F_CAP,
        text_color=TEXT_MUTED,
        anchor="w",
        wraplength=900,
        justify="left",
    )
    top5_eval_lbl.pack_forget()
    top5_ia_fr.pack_forget()
    platform_analytics_fr = ctk.CTkFrame(
        offers_main_scroll,
        fg_color=DARK_CORK,
        corner_radius=RADIUS_CARD,
        border_width=1,
        border_color=BORDER,
    )
    platform_analytics_title = ctk.CTkLabel(
        platform_analytics_fr,
        text="Comparaison des sources",
        font=F_BODY,
        text_color=TEXT,
        anchor="w",
    )
    platform_analytics_title.pack(fill="x", padx=12, pady=(10, 4))
    platform_analytics_summary = ctk.CTkLabel(
        platform_analytics_fr,
        text="Lancez une analyse multi-plateformes pour voir volumes et médianes par site.",
        font=F_CAP,
        text_color=TEXT_MUTED,
        anchor="w",
        wraplength=900,
        justify="left",
    )
    platform_analytics_summary.pack(fill="x", padx=12, pady=(0, 6))
    platform_analytics_chips = ctk.CTkFrame(platform_analytics_fr, fg_color="transparent")
    platform_analytics_chips.pack(fill="x", padx=10, pady=(0, 10))
    platform_analytics_fr.pack(fill="x", pady=(0, 8), padx=2)
    platform_analytics_fr.pack_forget()
    stats_var = ctk.StringVar(value="")
    ctk.CTkLabel(toolbar, textvariable=stats_var, font=F_BODY, text_color=TEXT_MUTED).pack(side="left", padx=8)
    search_var = ctk.StringVar(value="")
    ctk.CTkEntry(
        toolbar,
        textvariable=search_var,
        width=280,
        placeholder_text="Filtrer par mot-clé…",
        fg_color="transparent",
        border_color=BORDER_ACTIVE,
    ).pack(side="right", padx=8)
    rerun_btn = ctk.CTkButton(
        toolbar,
        text="Nouvelle analyse",
        corner_radius=RADIUS_GHOST,
        fg_color="transparent",
        border_width=1,
        border_color=WARM_CREAM,
        text_color=TEXT,
        width=140,
    )
    rerun_btn.pack(side="right", padx=8)
    csv_btn = ctk.CTkButton(
        toolbar,
        text="Excel",
        corner_radius=RADIUS_GHOST,
        fg_color=DARK_CORK,
        text_color=TEXT,
        width=72,
    )
    csv_btn.pack(side="right", padx=(0, 4))
    eval_export_btn = ctk.CTkButton(
        toolbar,
        text="Éval IA",
        corner_radius=RADIUS_GHOST,
        fg_color=DARK_CORK,
        text_color=WARM_CREAM,
        width=88,
    )
    eval_export_btn.pack(side="right", padx=(0, 4))
    export_btn = ctk.CTkButton(
        toolbar,
        text="Dossier",
        corner_radius=RADIUS_GHOST,
        fg_color=DARK_CORK,
        text_color=TEXT,
        width=90,
    )
    export_btn.pack(side="right")
    run_summary_card = ctk.CTkFrame(
        offers_main_scroll,
        fg_color=DARK_CORK,
        corner_radius=RADIUS_CARD,
        border_width=1,
        border_color=BURNT_SIENNA,
    )
    run_summary_card_lbl = ctk.CTkLabel(
        run_summary_card,
        text="",
        font=F_BODY,
        text_color=TEXT,
        wraplength=900,
        justify="left",
        anchor="w",
    )
    run_summary_card_lbl.pack(fill="x", padx=16, pady=14)
    run_summary_card.pack_forget()

    _QUALITY_CODES = ["all", "pepites", "ok", "sans_dropship", "sans_dinosaure"]
    _QUALITY_LABELS = {
        "all": "Tous",
        "pepites": "Pépites",
        "ok": "OK + pépites",
        "sans_dropship": "Sans dropship",
        "sans_dinosaure": "Sans junk",
    }
    _QUALITY_QUICK = ["Tous", "Pépites", "Sans dropship", "Sans junk"]
    _quality_label_to_code = {v: k for k, v in _QUALITY_LABELS.items()}

    results_ctrl = ctk.CTkFrame(
        offers_main_scroll,
        fg_color=DARK_CORK,
        corner_radius=RADIUS_CARD,
        border_width=1,
        border_color=BORDER,
    )
    results_ctrl_hdr = ctk.CTkFrame(results_ctrl, fg_color="transparent")
    results_ctrl_hdr.pack(fill="x", padx=12, pady=(10, 4))
    ctk.CTkLabel(
        results_ctrl_hdr,
        text="Filtres précis",
        font=F_BODY,
        text_color=TEXT,
    ).pack(side="left")
    results_ctrl_collapsed = ctk.BooleanVar(value=False)
    results_ctrl_body = ctk.CTkFrame(results_ctrl, fg_color="transparent")

    def _toggle_results_ctrl() -> None:
        if results_ctrl_collapsed.get():
            results_ctrl_body.pack_forget()
        else:
            results_ctrl_body.pack(fill="x", padx=0, pady=(0, 8))

    ctk.CTkButton(
        results_ctrl_hdr,
        text="▼",
        width=28,
        height=24,
        fg_color="transparent",
        hover_color="#3a2218",
        text_color=TEXT_MUTED,
        command=lambda: (
            results_ctrl_collapsed.set(not results_ctrl_collapsed.get()),
            _toggle_results_ctrl(),
        ),
    ).pack(side="right")
    results_ctrl_body.pack(fill="x", padx=0, pady=(0, 8))

    results_quality_var = ctk.StringVar(value=_QUALITY_LABELS["all"])
    results_platform_var = ctk.StringVar(value="all")
    sort_by_var = ctk.StringVar(value="Score affaire")
    _filter_guard: Dict[str, bool] = {"build": False}
    platform_filt_row = ctk.CTkFrame(results_ctrl_body, fg_color="transparent")
    platform_filt_row.pack(fill="x", padx=12, pady=(0, 6))
    ctk.CTkLabel(platform_filt_row, text="Plateforme", font=F_CAP, text_color=TEXT_MUTED, width=72).pack(
        side="left"
    )
    platform_chip_host = ctk.CTkFrame(platform_filt_row, fg_color="transparent")
    platform_chip_host.pack(side="left", fill="x", expand=True, padx=(4, 0))
    platform_chip_buttons: Dict[str, ctk.CTkButton] = {}

    quick_filt_row = ctk.CTkFrame(results_ctrl_body, fg_color="transparent")
    quick_filt_row.pack(fill="x", padx=12, pady=(0, 6))
    ctk.CTkLabel(quick_filt_row, text="Qualité", font=F_CAP, text_color=TEXT_MUTED, width=72).pack(side="left")
    ctk.CTkOptionMenu(
        quick_filt_row,
        variable=results_quality_var,
        values=_QUALITY_QUICK,
        width=148,
        height=30,
        fg_color=DARK_CORK,
        button_color=BURNT_SIENNA,
        button_hover_color="#b84500",
        dropdown_fg_color=DARK_CORK,
        dropdown_hover_color="#4a2a18",
        text_color=TEXT,
    ).pack(side="left", padx=(4, 0))

    sort_row = ctk.CTkFrame(results_ctrl_body, fg_color="transparent")
    sort_row.pack(fill="x", padx=12, pady=(0, 6))
    ctk.CTkLabel(sort_row, text="Trier", font=F_CAP, text_color=TEXT_MUTED, width=72).pack(side="left")
    _SORT_LABELS = [
        "IA (recommandé)",
        "Score affaire",
        "Valeur (v2)",
        "Prix croissant",
        "Prix décroissant",
        "Date (récent)",
        "Plateforme",
    ]
    _sort_label_to_code = {
        "IA (recommandé)": "ai",
        "Score affaire": "score",
        "Valeur (v2)": "value",
        "Prix croissant": "price_asc",
        "Prix décroissant": "price_desc",
        "Date (récent)": "date_newest",
        "Plateforme": "platform",
    }

    def _on_sort_change(_choice: str = "") -> None:
        state.sort_by = _sort_label_to_code.get(sort_by_var.get(), "ai")
        _schedule_apply_filters()

    ctk.CTkOptionMenu(
        sort_row,
        variable=sort_by_var,
        values=_SORT_LABELS,
        width=188,
        height=30,
        command=_on_sort_change,
        fg_color=DARK_CORK,
        button_color=BURNT_SIENNA,
        button_hover_color="#b84500",
        dropdown_fg_color=DARK_CORK,
        dropdown_hover_color="#4a2a18",
        text_color=TEXT,
    ).pack(side="left", padx=(4, 0))

    filt_bar = ctk.CTkFrame(results_ctrl_body, fg_color="transparent")
    preset_slot = ctk.CTkFrame(filt_bar, fg_color="transparent")
    preset_buttons: Dict[str, Any] = {}
    filt_bar.pack_forget()

    filt_stats_row = ctk.CTkFrame(results_ctrl_body, fg_color="transparent")
    filt_stats_row.pack(fill="x", padx=12, pady=(0, 6))
    filt_count_lbl = ctk.CTkLabel(filt_stats_row, text="", font=F_CAP, text_color=TEXT_MUTED, anchor="w")
    filt_count_lbl.pack(side="left", anchor="w")
    price_chip_lbl = ctk.CTkLabel(filt_stats_row, text="", font=F_CAP, text_color=WARM_CREAM, anchor="e")
    price_chip_lbl.pack(side="right", anchor="e", padx=(8, 0))
    market_badge_lbl = ctk.CTkLabel(filt_stats_row, text="", font=F_CAP, text_color=BURNT_SIENNA, anchor="e")
    market_badge_lbl.pack(side="right", anchor="e")

    inline_doubt_fr = ctk.CTkFrame(
        offers_main_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=WARN
    )
    inline_doubt_hdr = ctk.CTkFrame(inline_doubt_fr, fg_color="transparent")
    inline_doubt_hdr.pack(fill="x", padx=12, pady=(10, 4))
    inline_doubt_title = ctk.CTkLabel(
        inline_doubt_hdr,
        text="Annonces à vérifier",
        font=F_H2,
        text_color=WARM_CREAM,
        anchor="w",
    )
    inline_doubt_title.pack(side="left")
    inline_doubt_stats = ctk.StringVar(value="")
    ctk.CTkLabel(inline_doubt_hdr, textvariable=inline_doubt_stats, font=F_CAP, text_color=TEXT_MUTED).pack(
        side="left", padx=12
    )

    def _dismiss_all_doubts() -> None:
        state.doubtful_rows = []
        _render_inline_doubts()
        _render_doubtful_list()
        try:
            tabs.tab("Doutes").configure(text="Doutes")
            inline_doubt_fr.pack_forget()
        except Exception:
            pass

    ctk.CTkButton(
        inline_doubt_hdr,
        text="Écarter tout",
        height=28,
        fg_color=BG,
        text_color=TEXT,
        command=_dismiss_all_doubts,
    ).pack(side="right")
    inline_doubt_scroll = ctk.CTkScrollableFrame(inline_doubt_fr, fg_color=BG, corner_radius=0, height=320)
    inline_doubt_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 10))
    inline_doubt_fr.pack(fill="x", pady=(0, 6))
    inline_doubt_fr.pack_forget()

    filt_collapsed = {"on": True}
    filt_toggle_row = ctk.CTkFrame(results_ctrl, fg_color="transparent")
    filt_toggle_row.pack(fill="x", padx=12, pady=(0, 4))

    filt_body = ctk.CTkFrame(results_ctrl, fg_color=BG, corner_radius=8, border_width=1, border_color=BORDER)
    ctk.CTkLabel(
        filt_body,
        text="Filtres catégorie — lumens, résolution, HDMI, usage, marque…",
        font=F_CAP,
        text_color=TEXT_MUTED,
    ).pack(anchor="w", padx=10, pady=(8, 0))
    filt_active_lbl = ctk.CTkLabel(
        filt_body,
        text="",
        font=F_CAP,
        text_color=BURNT_SIENNA,
        anchor="w",
    )
    filt_active_lbl.pack(anchor="w", padx=10, pady=(0, 4))
    filt_grid = ctk.CTkFrame(filt_body, fg_color=BG)
    filt_grid.pack(fill="x", padx=6, pady=(0, 4))

    filter_agent_lbl = ctk.CTkLabel(
        filt_body,
        text="",
        font=F_CAP,
        text_color=WARM_CREAM,
        wraplength=860,
        justify="left",
        anchor="w",
    )
    filter_agent_lbl.pack(fill="x", padx=10, pady=(0, 10))

    results_ctrl.pack(fill="x", pady=(0, 6))

    offers_list_section = ctk.CTkFrame(offers_main_scroll, fg_color=BG)
    offers_list_section.pack(fill="x")

    offers_list_hdr = ctk.CTkFrame(offers_list_section, fg_color="transparent")
    offers_list_hdr.pack(fill="x", pady=(0, 6))
    offers_count_lbl = ctk.CTkLabel(
        offers_list_hdr,
        text="Liste des offres",
        font=F_BODY,
        text_color=WARM_CREAM,
        anchor="w",
    )
    offers_count_lbl.pack(side="left", padx=4)
    offers_hint_lbl = ctk.CTkLabel(
        offers_list_hdr,
        text="Défilez la page pour parcourir filtres et offres",
        font=F_CAP,
        text_color=TEXT_MUTED,
        anchor="e",
    )
    offers_hint_lbl.pack(side="right", padx=4)

    deals_list = ctk.CTkFrame(offers_list_section, fg_color=BG)
    deals_list.pack(fill="x")

    list_busy_lbl = ctk.CTkLabel(
        offers_list_section,
        text="Filtrage en cours…",
        font=F_BODY,
        text_color=TEXT,
        fg_color=DARK_CORK,
        corner_radius=RADIUS_CARD,
    )

    load_more_fr = ctk.CTkFrame(offers_list_section, fg_color="transparent")
    load_more_fr.pack(fill="x", pady=(4, 0))

    open_filters_btn = ctk.CTkButton(
        offers_list_hdr,
        text="Filtres",
        height=28,
        corner_radius=RADIUS_GHOST,
        fg_color=BG,
        hover_color="#3a2218",
        text_color=WARM_CREAM,
        border_width=1,
        border_color=BORDER_ACTIVE,
        command=lambda: _set_filters_collapsed(False),
    )
    open_filters_btn.pack(side="right", padx=4)

    def _set_filters_collapsed(collapsed: bool) -> None:
        filt_collapsed["on"] = collapsed
        if collapsed:
            try:
                filt_body.pack_forget()
            except Exception:
                pass
            try:
                filt_toggle_row.pack_forget()
            except Exception:
                pass
            filt_toggle_btn.configure(text="Filtres produit ▼")
            offers_hint_lbl.configure(text="Plateforme et qualité ci-dessus · défilez pour les offres")
            try:
                open_filters_btn.pack(side="right", padx=4)
            except Exception:
                pass
        else:
            filt_body.pack(fill="x", padx=12, pady=(0, 10))
            filt_toggle_btn.configure(text="Masquer filtres produit ▲")
            offers_hint_lbl.configure(text="")
            try:
                filt_toggle_row.pack(fill="x", padx=12, pady=(0, 4))
            except Exception:
                pass
            try:
                open_filters_btn.pack_forget()
            except Exception:
                pass

    def _toggle_filt_body() -> None:
        _set_filters_collapsed(not filt_collapsed["on"])

    filt_toggle_btn = ctk.CTkButton(
        filt_toggle_row,
        text="Filtres produit ▼",
        height=30,
        corner_radius=RADIUS_GHOST,
        fg_color=BG,
        hover_color="#3a2218",
        text_color=WARM_CREAM,
        border_width=1,
        border_color=BORDER_ACTIVE,
        command=_toggle_filt_body,
    )
    filt_toggle_btn.pack(side="left")
    _set_filters_collapsed(True)
    load_more_btn = ctk.CTkButton(
        load_more_fr,
        text="Charger 30 annonces suivantes",
        height=36,
        corner_radius=RADIUS_GHOST,
        fg_color=DARK_CORK,
        text_color=TEXT,
    )

    detail_win = ctk.CTkToplevel(root)
    detail_win.withdraw()
    detail_win.title("Détail annonce")
    detail_win.geometry("1120x860")
    detail_win.minsize(900, 640)
    detail_win.configure(fg_color=BG)
    detail_win.grid_rowconfigure(1, weight=1)
    detail_win.grid_columnconfigure(0, weight=1)

    def _close_detail_win() -> None:
        detail_win.withdraw()

    detail_win.protocol("WM_DELETE_WINDOW", _close_detail_win)

    d_header = ctk.CTkFrame(detail_win, fg_color=DARK_CORK, corner_radius=0)
    d_header.grid(row=0, column=0, sticky="ew")
    ctk.CTkLabel(d_header, text="Détail annonce", font=F_H2, text_color=TEXT).pack(side="left", padx=20, pady=14)
    d_btns = ctk.CTkFrame(d_header, fg_color="transparent")
    d_btns.pack(side="right", padx=8, pady=10)
    ctk.CTkButton(
        d_header,
        text="Fermer",
        width=88,
        height=32,
        corner_radius=RADIUS_GHOST,
        fg_color=BG,
        text_color=TEXT,
        command=_close_detail_win,
    ).pack(side="right", padx=(0, 16), pady=10)

    detail_scroll = ctk.CTkScrollableFrame(detail_win, fg_color=BG, corner_radius=0)
    detail_scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
    d_head = ctk.CTkFrame(detail_scroll, fg_color="transparent")
    d_head.pack(fill="x", padx=8, pady=(4, 8))
    d_title = ctk.CTkLabel(d_head, text="—", font=F_H2, text_color=TEXT, wraplength=900, justify="left")
    d_title.pack(anchor="w")
    d_price_row = ctk.CTkFrame(d_head, fg_color="transparent")
    d_price_row.pack(anchor="w", pady=(6, 0))
    d_price = ctk.CTkLabel(
        d_price_row,
        text="",
        font=ctk.CTkFont(family=FONT_DISPLAY[0], size=40, weight="bold"),
        text_color=WARM_CREAM,
    )
    d_price.pack(side="left")
    d_rank_meta = ctk.CTkLabel(d_price_row, text="", font=F_BODY, text_color=TEXT_MUTED)
    d_rank_meta.pack(side="left", padx=(16, 0))
    d_score = ctk.CTkLabel(
        d_head, text="", font=ctk.CTkFont(family=FONT_DISPLAY[0], size=32), text_color=BURNT_SIENNA
    )
    d_score.pack(anchor="w", pady=(6, 0))

    d_content = ctk.CTkFrame(detail_scroll, fg_color="transparent")
    d_content.pack(fill="both", expand=True, padx=4, pady=(0, 8))
    d_content.grid_columnconfigure(0, weight=0)
    d_content.grid_columnconfigure(1, weight=1)
    d_content.grid_rowconfigure(0, weight=1)
    d_img = ctk.CTkLabel(d_content, text="", width=420)
    d_img.grid(row=0, column=0, sticky="nw", padx=(8, 16), pady=8)
    d_right = ctk.CTkFrame(d_content, fg_color="transparent")
    d_right.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)
    d_right.grid_rowconfigure(1, weight=1)
    d_right.grid_columnconfigure(0, weight=1)
    d_assistant = ctk.CTkLabel(
        d_right,
        text="",
        font=F_BODY,
        text_color=WARM_CREAM,
        wraplength=560,
        justify="left",
        anchor="nw",
    )
    d_assistant.pack(anchor="nw", fill="x")
    d_body = ctk.CTkTextbox(d_right, fg_color="#1a120c", text_color=TEXT, font=F_CAP, height=480, wrap="word")
    d_body.pack(fill="both", expand=True, pady=(10, 0))

    tab_rejected.grid_columnconfigure(0, weight=1)
    tab_rejected.grid_rowconfigure(1, weight=1)
    rej_toolbar = ctk.CTkFrame(tab_rejected, fg_color="transparent")
    rej_toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
    rej_stats_var = ctk.StringVar(value="Aucun rejet enregistré")
    ctk.CTkLabel(rej_toolbar, textvariable=rej_stats_var, font=F_BODY, text_color=TEXT_MUTED).pack(
        side="left", padx=8
    )
    rej_search_var = ctk.StringVar(value="")
    ctk.CTkEntry(
        rej_toolbar,
        textvariable=rej_search_var,
        width=260,
        placeholder_text="Chercher dans les rejetés…",
        fg_color=BG,
        border_color=BORDER_ACTIVE,
    ).pack(side="right", padx=8)

    rej_scroll = ctk.CTkScrollableFrame(tab_rejected, fg_color=BG, corner_radius=0)
    rej_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
    rej_load_btn = ctk.CTkButton(
        tab_rejected,
        text="Charger plus de rejetés",
        height=32,
        fg_color=DARK_CORK,
        text_color=TEXT,
        corner_radius=RADIUS_GHOST,
    )

    tab_doubtful.grid_columnconfigure(0, weight=1)
    tab_doubtful.grid_rowconfigure(1, weight=1)
    doubt_toolbar = ctk.CTkFrame(tab_doubtful, fg_color="transparent")
    doubt_toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
    doubt_stats_var = ctk.StringVar(value="Aucun cas ambigu")
    doubt_subtitle_var = ctk.StringVar(value="Annonces à vérifier")
    ctk.CTkLabel(
        doubt_toolbar,
        textvariable=doubt_subtitle_var,
        font=F_CAP,
        text_color=TEXT_MUTED,
    ).pack(side="left", padx=4)
    ctk.CTkLabel(doubt_toolbar, textvariable=doubt_stats_var, font=F_BODY, text_color=WARM_CREAM).pack(
        side="left", padx=12
    )
    doubt_search_var = ctk.StringVar(value="")
    ctk.CTkEntry(
        doubt_toolbar,
        textvariable=doubt_search_var,
        width=260,
        placeholder_text="Chercher dans les doutes…",
        fg_color=BG,
        border_color=BORDER_ACTIVE,
    ).pack(side="right", padx=8)

    doubt_scroll = ctk.CTkScrollableFrame(tab_doubtful, fg_color=BG, corner_radius=0)
    doubt_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
    doubt_load_btn = ctk.CTkButton(
        tab_doubtful,
        text="Charger plus de doutes",
        height=32,
        fg_color=DARK_CORK,
        text_color=TEXT,
        corner_radius=RADIUS_GHOST,
    )

    weight_vars: Dict[str, ctk.DoubleVar] = {}
    weight_slider_frames: List[Any] = []

    def _clear_weight_sliders(*, host: Any = None) -> None:
        for fr in weight_slider_frames:
            fr.destroy()
        weight_slider_frames.clear()
        weight_vars.clear()
        if host is setup_slider_host:
            for w in setup_slider_host.winfo_children():
                w.destroy()

    def _match_saved_to_specs(saved: str, specs: List[Dict[str, Any]]) -> Tuple[Dict[str, bool], str]:
        raw = (saved or "").strip()
        if not raw:
            return {}, ""
        active: Dict[str, bool] = {}
        extra_parts: List[str] = []
        for part in re.split(r"[;\n]+", raw):
            p = part.strip()
            if not p:
                continue
            pl = p.lower()
            matched = False
            for sp in specs:
                sid = str(sp["id"])
                for field in ("priority_text", "label"):
                    txt = str(sp.get(field) or "").strip().lower()
                    if not txt:
                        continue
                    if txt == pl or (len(p) >= 12 and (pl in txt or txt in pl)):
                        active[sid] = True
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                extra_parts.append(p)
        return active, "; ".join(extra_parts)

    def _current_unified_specs() -> List[Dict[str, Any]]:
        from .market import unified_market_specs

        return unified_market_specs(
            state.market_category or "",
            state.intent or "",
            state.idle_refinements,
        )

    def _build_combined_priorities() -> str:
        from .market import deal_hunt_notes, specs_to_scoring_notes

        specs = _current_unified_specs()
        parts: List[str] = []
        dh = deal_hunt_notes(state.deal_hunt_mode, state.market_category or "")
        if dh:
            parts.append(dh)
        parts.extend(specs_to_scoring_notes(state.preflight_active, specs))
        extra = buyer_extra_box.get("0.0", "end").strip()
        if extra:
            parts.append(extra)
        return "; ".join(parts)[:600]

    def _update_prio_preview() -> None:
        from .market import specs_to_preflight_map, specs_to_scoring_notes

        specs = _current_unified_specs()
        n = sum(1 for sp in specs if state.preflight_active.get(sp["id"], sp.get("default", False)))
        extra = buyer_extra_box.get("0.0", "end").strip()
        suggest_count_lbl.configure(
            text=f"{n} actif{'s' if n != 1 else ''}" + (" · + notes" if extra else "")
        )
        pf = specs_to_preflight_map(state.preflight_active, specs)
        scoring = specs_to_scoring_notes(state.preflight_active, specs)
        lines: List[str] = []
        if pf:
            lines.append("Filtres : " + ", ".join(k for k, v in pf.items() if v)[:240])
        if scoring:
            lines.append("IA : " + " · ".join(scoring[:5]))
        if extra:
            lines.append("+ " + extra)
        preview = "\n".join(lines) if lines else "(affinage par défaut du marché)"
        setup_prio_preview.configure(text=preview[:900])

    buyer_extra_box.bind("<KeyRelease>", lambda _e: _update_prio_preview())

    def _sync_preflight_from_refinements(
        category: str,
        query: str,
        *,
        saved_active: Optional[Dict[str, bool]] = None,
    ) -> List[Dict[str, Any]]:
        from .market import refinements_to_preflight_overrides, unified_market_specs

        specs = unified_market_specs(category, query, state.idle_refinements)
        active = {str(sp["id"]): bool(sp.get("default", False)) for sp in specs}
        for pid, val in refinements_to_preflight_overrides(state.idle_refinements).items():
            active[pid] = bool(val)
        if saved_active:
            active.update({k: bool(v) for k, v in saved_active.items()})
        state.preflight_active = active
        return specs

    def _toggle_all_unified(on: bool) -> None:
        return

    def _rebuild_unified_specs(
        category: str,
        query: str,
        *,
        saved_active: Optional[Dict[str, bool]] = None,
    ) -> None:
        from .market import refinements_to_buyer_notes, specs_to_preflight_map, specs_to_scoring_notes

        for w in setup_preflight_host.winfo_children():
            w.destroy()
        specs = _sync_preflight_from_refinements(category, query, saved_active=saved_active)
        state.suggest_tip_keys = [str(sp["id"]) for sp in specs]
        if not specs:
            ctk.CTkLabel(
                setup_preflight_host,
                text="Marché non reconnu — précisez dans les notes libres.",
                font=F_CAP,
                text_color=TEXT_MUTED,
                wraplength=820,
                justify="left",
            ).pack(anchor="w", padx=10, pady=10)
            _update_prio_preview()
            return
        ref_note = refinements_to_buyer_notes(state.idle_refinements)
        if ref_note:
            ctk.CTkLabel(
                setup_preflight_host,
                text=f"Affinage : {ref_note}",
                font=F_BODY,
                text_color=BURNT_SIENNA,
                wraplength=820,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=10, pady=(8, 4))
        pf = specs_to_preflight_map(state.preflight_active, specs)
        scoring = specs_to_scoring_notes(state.preflight_active, specs)
        if pf:
            filt_bits = [
                str(sp.get("label") or sp["id"])
                for sp in specs
                if sp.get("scope") in ("filter", "both") and pf.get(sp["id"])
            ]
            ctk.CTkLabel(
                setup_preflight_host,
                text="Filtres scrape : " + ", ".join(filt_bits[:8])[:320],
                font=F_CAP,
                text_color=TEXT,
                wraplength=820,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=10, pady=2)
        if scoring:
            ctk.CTkLabel(
                setup_preflight_host,
                text="IA scoring : " + " · ".join(scoring[:5])[:320],
                font=F_CAP,
                text_color=TEXT_MUTED,
                wraplength=820,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=10, pady=(2, 8))
        _update_prio_preview()

    def _rebuild_preflight_chips(category: str, query: str) -> None:
        _rebuild_unified_specs(category, query)

    def _load_search_brief(gen: int) -> None:
        from .market import brief_market_analysis

        plats = _idle_platforms()
        cat = state.market_category or ""
        raw = state.intent

        def _work() -> None:
            brief = brief_market_analysis(
                raw,
                platforms=plats,
                category=cat,
                use_ai=bool(use_ai_var.get()),
            )

            def _done() -> None:
                if gen != state.market_detect_gen:
                    return
                state.search_brief = brief
                setup_brief_lbl.configure(text=str(brief.get("honest_take") or "—"))
                pits = brief.get("pitfalls") or []
                setup_pitfalls_lbl.configure(
                    text=("⚠ " + " · ".join(pits)) if pits else ""
                )
                strat = brief.get("strategies") or []
                if brief.get("compare_note"):
                    strat = [str(brief["compare_note"])] + list(strat)
                setup_strategy_lbl.configure(text="\n".join(f"→ {s}" for s in strat[:6]))
                criteria = str(brief.get("buyer_criteria") or "").strip()
                if criteria and use_ai_var.get() and not buyer_extra_box.get("0.0", "end").strip():
                    buyer_extra_box.insert("0.0", criteria)
                    _update_prio_preview()
                pf_ids = brief.get("preflight_ids") or []
                if isinstance(pf_ids, list) and pf_ids:
                    for pid in pf_ids:
                        state.preflight_active[pid] = True
                    _rebuild_unified_specs(state.market_category or cat, state.intent or raw)

            root.after(0, _done)

        threading.Thread(target=_work, daemon=True, name="vf-brief").start()

    def _reset_setup_ui() -> None:
        """Vide l’écran Configurer (évite flash projecteur en cache)."""
        state.market_category = ""
        state.prio_context_key = ""
        state.search_brief = None
        setup_market_lbl.configure(text="Marché : —")
        setup_crit_lbl.configure(text="")
        setup_brief_lbl.configure(text="Le brief s’affiche après détection du marché…")
        setup_pitfalls_lbl.configure(text="")
        setup_strategy_lbl.configure(text="")
        for w in setup_preflight_host.winfo_children():
            w.destroy()
        state.preflight_active.clear()
        setup_prio_hint_lbl.configure(
            text="Chaque URL a ses propres critères — changez d’URL pour repartir à zéro."
        )
        buyer_extra_box.delete("0.0", "end")
        setup_prio_preview.configure(text="—")
        suggest_count_lbl.configure(text="0 actif")
        _clear_weight_sliders(host=setup_slider_host)

    def _rebuild_weight_sliders(category: str = "", *, host: Any = None) -> None:
        _clear_weight_sliders(host=host)
        cat = (category or "generic").strip() or "generic"
        parent = host or setup_slider_host
        use_grid = parent is setup_slider_host
        if use_grid:
            for c in range(2):
                parent.grid_columnconfigure(c, weight=1)
        w = get_weights()
        for i, (key, label, hint, mn, mx) in enumerate(weight_field_defs(cat)):
            fr = ctk.CTkFrame(parent, fg_color=DARK_CORK, corner_radius=RADIUS_CARD)
            if use_grid:
                r, c = divmod(i, 2)
                fr.grid(row=r, column=c, sticky="ew", padx=4, pady=6)
            else:
                fr.pack(fill="x", pady=6)
            weight_slider_frames.append(fr)
            ctk.CTkLabel(fr, text=label, font=F_BODY, text_color=TEXT).pack(anchor="w", padx=16, pady=(12, 0))
            if hint:
                ctk.CTkLabel(fr, text=hint, font=F_CAP, text_color=TEXT_MUTED).pack(anchor="w", padx=16)
            var = ctk.DoubleVar(value=float(getattr(w, key, mn)))
            weight_vars[key] = var
            val_lbl = ctk.CTkLabel(fr, text=f"{var.get():.2f}", font=F_CAP, text_color=BURNT_SIENNA)
            val_lbl.pack(anchor="e", padx=16)

            def _upd(v, lbl=val_lbl) -> None:
                lbl.configure(text=f"{float(v):.2f}")

            ctk.CTkSlider(
                fr,
                from_=mn,
                to=mx,
                variable=var,
                progress_color=BURNT_SIENNA,
                button_color=WARM_CREAM,
                command=_upd,
            ).pack(fill="x", padx=16, pady=(4, 16))

    def _collect_weights() -> ScoreWeights:
        base = asdict(get_weights())
        for k, var in weight_vars.items():
            base[k] = float(var.get())
        base["buyer_priorities"] = _build_combined_priorities()
        allowed = set(asdict(ScoreWeights()).keys())
        return ScoreWeights(**{k: base[k] for k in allowed if k in base}).clamp()

    def _ai_adjust_weights() -> None:
        from .ai import api_key, suggest_score_weights

        if not api_key():
            _log_ui("⚠ OPENROUTER_API_KEY manquante — impossible d’ajuster les poids par IA.")
            return
        cat = state.market_category or "generic"
        prio = _build_combined_priorities()
        _log_ui("IA : suggestion des poids…")
        suggested = suggest_score_weights(
            category=cat,
            buyer_priorities=prio or state.intent,
            current=asdict(_collect_weights()),
        )
        if not suggested:
            _log_ui("⚠ L’IA n’a pas renvoyé de poids — réessayez ou ajustez à la main.")
            return
        for k, v in suggested.items():
            if k in weight_vars:
                weight_vars[k].set(float(v))
        w = _collect_weights()
        save_weights(w)
        set_weights(w)
        _log_ui(f"✓ Poids mis à jour par l’IA ({len(suggested)} curseurs)")

    def _reset_weights_defaults() -> None:
        cat = state.market_category or "generic"
        fresh = ScoreWeights()
        fresh.buyer_priorities = _build_combined_priorities()
        save_weights(fresh)
        set_weights(fresh)
        _rebuild_weight_sliders(cat, host=setup_slider_host)
        _log_ui("✓ Poids réinitialisés aux valeurs par défaut")

    ctk.CTkButton(
        weights_toolbar,
        text="Réinit. défaut",
        height=28,
        width=110,
        corner_radius=RADIUS_GHOST,
        fg_color=BG,
        hover_color="#3a2218",
        text_color=TEXT,
        font=F_CAP,
        command=_reset_weights_defaults,
    ).pack(side="left")

    logs_toolbar = ctk.CTkFrame(tab_logs, fg_color="transparent")
    logs_toolbar.pack(fill="x", padx=8, pady=(8, 4))
    ctk.CTkLabel(
        logs_toolbar,
        text="Journal complet de la session (analyse, filtres, erreurs)",
        font=F_CAP,
        text_color=TEXT_MUTED,
    ).pack(side="left", padx=4)

    def _open_session_log() -> None:
        try:
            _SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
            if not _SESSION_LOG.is_file():
                _SESSION_LOG.write_text("(aucun log pour l’instant)\n", encoding="utf-8")
            os.startfile(str(_SESSION_LOG.resolve()))
        except Exception as exc:  # noqa: BLE001
            _log_ui(f"⚠ Impossible d’ouvrir le journal : {exc}")

    def _copy_session_log() -> None:
        try:
            tb = _journal_text()
            txt = tb.get("1.0", "end-1c")
            if not (txt or "").strip():
                _log_ui("⚠ Journal vide — rien à copier")
                return

            def _do_clipboard() -> None:
                try:
                    root.clipboard_clear()
                    root.clipboard_append(txt)
                    root.update_idletasks()
                    _log_ui("✓ Journal copié dans le presse-papiers")
                except Exception as exc2:
                    _log_ui(f"⚠ Copie impossible : {exc2}")

            root.after(0, _do_clipboard)
        except Exception as exc:
            _log_ui(f"⚠ Copie impossible : {exc}")

    ctk.CTkButton(
        logs_toolbar,
        text="Copier tout",
        width=100,
        height=30,
        corner_radius=RADIUS_GHOST,
        fg_color=BURNT_SIENNA,
        text_color=WARM_CREAM,
        command=_copy_session_log,
    ).pack(side="right", padx=4)
    ctk.CTkButton(
        logs_toolbar,
        text="Ouvrir fichier",
        width=120,
        height=30,
        corner_radius=RADIUS_GHOST,
        fg_color=DARK_CORK,
        text_color=TEXT,
        command=_open_session_log,
    ).pack(side="right", padx=4)
    monitor_metrics_lbl = ctk.CTkLabel(
        logs_toolbar,
        text="métriques — en attente",
        font=F_CAP,
        text_color=TEXT_MUTED,
    )
    monitor_metrics_lbl.pack(side="right", padx=(8, 16))

    logs_box = ctk.CTkTextbox(
        tab_logs,
        fg_color="#0c0806",
        text_color=TEXT_MUTED,
        font=F_MONO,
        wrap="word",
        corner_radius=8,
        activate_scrollbars=True,
    )
    logs_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    try:
        logs_box.configure(state="normal")
    except Exception:
        pass

    def _journal_text() -> Any:
        return getattr(logs_box, "_textbox", logs_box)

    def _journal_log_tag(msg: str) -> str:
        s = (msg or "").strip()
        if s.startswith("✓"):
            return "ok"
        if s.startswith("⚠"):
            return "warn"
        if "erreur" in s.lower() or "error" in s.lower():
            return "err"
        if s.startswith("▸") or "étape" in s.lower():
            return "step"
        return "body"

    def _init_journal_tags() -> None:
        tb = _journal_text()
        for tag, color in (
            ("time", TEXT_MUTED),
            ("ok", OK),
            ("warn", WARN),
            ("err", DANGER),
            ("step", BURNT_SIENNA),
            ("body", WARM_CREAM),
        ):
            try:
                tb.tag_configure(tag, foreground=color)
            except Exception:
                pass
        try:
            tb.configure(
                selectbackground="#5c3d2a",
                selectforeground=WARM_CREAM,
                insertbackground=WARM_CREAM,
                exportselection=True,
            )
        except Exception:
            pass

    _init_journal_tags()

    def _journal_copy_selection() -> None:
        tb = _journal_text()
        try:
            chunk = tb.get("sel.first", "sel.last")
        except Exception:
            chunk = ""
        if chunk:
            root.clipboard_clear()
            root.clipboard_append(chunk)
            root.update()

    def _journal_context_menu(event: Any) -> None:
        try:
            import tkinter as tk

            menu = tk.Menu(root, tearoff=0, bg=DARK_CORK, fg=TEXT, activebackground=BURNT_SIENNA)
            menu.add_command(label="Copier la sélection", command=_journal_copy_selection)
            menu.add_command(label="Copier tout", command=_copy_session_log)
            menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass

    try:
        _jt = _journal_text()
        def _on_journal_copy(_event: Any = None) -> str:
            _journal_copy_selection()
            return "break"

        for widget in (logs_box, _jt):
            try:
                widget.configure(state="normal")
            except Exception:
                pass
        for seq in ("<Control-c>", "<Control-C>", "<Control-Insert>"):
            _jt.bind(seq, _on_journal_copy)
            try:
                logs_box.bind(seq, _on_journal_copy)
            except Exception:
                pass
        _jt.bind("<Button-3>", _journal_context_menu)
        _jt.bind("<Button-1>", lambda _e: None)
    except Exception:
        pass

    from .engine import BUS, format_bus_log, platform_style

    _CATEGORY_UI_LABELS = {
        "phone": "téléphone",
        "bicycle": "vélo",
        "projector": "vidéoprojecteur",
        "console_ds": "console DS/3DS",
        "apparel": "vêtements",
    }

    def _doubt_subtitle_for_category(cat: str) -> str:
        label = _CATEGORY_UI_LABELS.get((cat or "").strip(), cat or "marché")
        return f"Annonces à vérifier ({label}, filtre incertain)"

    def _truncate_at_word(text: str, max_chars: int = 480) -> str:
        t = (text or "").strip()
        if len(t) <= max_chars:
            return t
        cut = t[:max_chars].rsplit(" ", 1)[0]
        return (cut or t[:max_chars]).rstrip(".,;:") + "…"

    def _set_top_picks_note(text: str) -> None:
        body = (text or "Meilleures affaires après analyse prix + pack.").strip()
        top_picks_note_box.configure(state="normal")
        top_picks_note_box.delete("1.0", "end")
        top_picks_note_box.insert("1.0", body)
        top_picks_note_box.configure(state="disabled")

    _tooltip_win: Dict[str, Any] = {"w": None}

    def _bind_tooltip(widget: Any, tip: str) -> None:
        def _show(_e: Any = None) -> None:
            if _tooltip_win["w"] is not None:
                return
            tw = ctk.CTkToplevel(root)
            tw.wm_overrideredirect(True)
            tw.configure(fg_color=DARK_CORK)
            tw.attributes("-topmost", True)
            lbl = ctk.CTkLabel(
                tw,
                text=tip,
                font=F_CAP,
                text_color=TEXT,
                fg_color=DARK_CORK,
                corner_radius=6,
                wraplength=340,
                justify="left",
            )
            lbl.pack(padx=10, pady=8)
            tw.update_idletasks()
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tw.geometry(f"+{x}+{y}")
            _tooltip_win["w"] = tw

        def _hide(_e: Any = None) -> None:
            tw = _tooltip_win.get("w")
            if tw is not None:
                try:
                    tw.destroy()
                except Exception:
                    pass
                _tooltip_win["w"] = None

        widget.bind("<Enter>", _show)
        widget.bind("<Leave>", _hide)

    def _count_pepites(rows: List[Dict[str, Any]]) -> int:
        return sum(
            1
            for r in rows
            if str(r.get("deal_tier") or "") == "pepite" or str(r.get("quality_tier") or "") == "gem"
        )

    def _open_listing_url(url: str) -> None:
        if not url:
            return
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    def _render_top5_ia(data: Optional[Dict[str, Any]]) -> None:
        for w in top5_ia_cards.winfo_children():
            w.destroy()
        items = (data or {}).get("top5") or []
        if not items:
            top5_ia_fr.pack_forget()
            top5_avoid_fr.pack_forget()
            top5_eval_lbl.pack_forget()
            return
        try:
            tabs.tab("Sélection IA").configure(text=f"Sélection IA ({len(items)})")
        except Exception:
            pass
        top5_ia_fr.pack(fill="x", pady=(0, 8), padx=2)
        summary = str((data or {}).get("market_summary") or "").strip()
        cat = str((data or {}).get("category") or "").strip()
        cat_labels = {
            "phone": "téléphone",
            "bicycle": "vélo",
            "projector": "projecteur",
            "console_ps4": "PS4",
            "console_ps5": "PS5",
            "console_ds": "console DS/3DS",
            "apparel": "vêtements",
        }
        if summary:
            top5_ia_summary.configure(text=summary)
        elif cat:
            top5_ia_summary.configure(
                text=f"Top 5 {cat_labels.get(cat, cat)} — critères specs adaptés à la catégorie."
            )
        avoid = list((data or {}).get("avoid") or [])
        if avoid:
            top5_avoid_lbl.configure(text=" · ".join(avoid[:8]))
            top5_avoid_fr.pack(fill="x", pady=(0, 8), padx=2, after=top5_ia_fr)
        else:
            top5_avoid_fr.pack_forget()
        eval_paths = getattr(state, "eval_export_paths", None) or {}
        if eval_paths:
            bits = [f"{k}: {v}" for k, v in list(eval_paths.items())[:2]]
            top5_eval_lbl.configure(text="Export éval IA : " + " · ".join(bits))
            top5_eval_lbl.pack(fill="x", pady=(0, 8), padx=4, after=top5_avoid_fr if avoid else top5_ia_fr)
        else:
            top5_eval_lbl.pack_forget()
        for it in items[:5]:
            card = ctk.CTkFrame(top5_ia_cards, fg_color="#2a1810", corner_radius=10, border_width=1, border_color="#5c3d2e")
            card.pack(fill="x", pady=4, padx=4)
            head = ctk.CTkFrame(card, fg_color="transparent")
            head.pack(fill="x", padx=10, pady=(8, 2))
            ctk.CTkLabel(
                head,
                text=f"#{it.get('rank')} · {it.get('price')} € · score {it.get('score')}",
                font=F_BODY,
                text_color="#ff8c42",
                anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                head,
                text=str(it.get("verdict") or "")[:48],
                font=F_CAP,
                text_color=TEXT_MUTED,
                anchor="e",
            ).pack(side="right")
            ctk.CTkLabel(
                card,
                text=str(it.get("title") or "")[:88],
                font=F_BODY,
                text_color=TEXT,
                anchor="w",
                wraplength=680,
                justify="left",
            ).pack(fill="x", padx=10, pady=(0, 4))
            ctk.CTkLabel(
                card,
                text=str(it.get("justification") or "")[:320],
                font=F_CAP,
                text_color=TEXT_MUTED,
                anchor="w",
                wraplength=680,
                justify="left",
            ).pack(fill="x", padx=10, pady=(0, 4))
            chips = ctk.CTkFrame(card, fg_color="transparent")
            chips.pack(fill="x", padx=8, pady=(0, 8))
            badge_list = list(it.get("highlights") or [])
            if not badge_list and it.get("extracted_specs"):
                try:
                    from .scoring import spec_badges

                    badge_list = spec_badges(it.get("extracted_specs") or {}, cat or "generic")
                except Exception:
                    badge_list = []
            for hl in badge_list[:4]:
                ctk.CTkLabel(
                    chips,
                    text=str(hl),
                    font=F_CAP,
                    fg_color="#1a3d2a",
                    text_color="#7dffb3",
                    corner_radius=6,
                    height=22,
                ).pack(side="left", padx=2)
            for wn in (it.get("warnings") or [])[:3]:
                ctk.CTkLabel(
                    chips,
                    text=str(wn),
                    font=F_CAP,
                    fg_color="#3d1a1a",
                    text_color="#ffb07d",
                    corner_radius=6,
                    height=22,
                ).pack(side="left", padx=2)
            arb = str(it.get("arbitrage_hint") or "")
            if arb:
                ctk.CTkLabel(
                    chips,
                    text=arb[:48],
                    font=F_CAP,
                    fg_color="#1a2a3d",
                    text_color="#7db8ff",
                    corner_radius=6,
                    height=22,
                ).pack(side="left", padx=2)
            url = str(it.get("url") or "")
            if url:
                ctk.CTkButton(
                    card,
                    text="Voir l'annonce",
                    width=120,
                    height=26,
                    font=F_CAP,
                    fg_color=BURNT_SIENNA,
                    hover_color="#c44f03",
                    command=lambda u=url: _open_listing_url(u),
                ).pack(anchor="e", padx=10, pady=(0, 8))
        near = list((data or {}).get("near_misses") or [])
        if near:
            nm_fr = ctk.CTkFrame(top5_ia_cards, fg_color=DARK_CORK, corner_radius=10, border_width=1, border_color=BORDER)
            nm_fr.pack(fill="x", pady=8, padx=4)
            ctk.CTkLabel(
                nm_fr,
                text="Pourquoi pas les autres",
                font=F_BODY,
                text_color=WARN,
                anchor="w",
            ).pack(fill="x", padx=10, pady=(8, 4))
            for nm in near[:3]:
                line = f"· {nm.get('score')} — {nm.get('title', '')[:50]} — {nm.get('reason', '')}"
                ctk.CTkLabel(
                    nm_fr,
                    text=line[:200],
                    font=F_CAP,
                    text_color=TEXT_MUTED,
                    anchor="w",
                    wraplength=660,
                    justify="left",
                ).pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(nm_fr, text="", height=6).pack()

    def _render_top_picks(picks: List[Dict[str, Any]], note: str = "") -> None:
        for w in top_picks_chips.winfo_children():
            w.destroy()
        ranked_main = any(r.get("rank_final") for r in (state.filtered or state.rows)[:30])
        if ranked_main or (state.top5_analysis and (state.top5_analysis.get("top5") or [])):
            top_picks_fr.pack_forget()
            return
        if not picks:
            top_picks_fr.pack_forget()
            return
        top_picks_fr.pack(fill="x", pady=(0, 8), padx=2, before=results_ctrl)
        _set_top_picks_note(note)
        for p in picks[:10]:
            plat = str(p.get("platform") or "vinted").lower()
            st = platform_style(plat)
            pr = p.get("total_price") or p.get("price") or "—"
            rk = p.get("deal_pick_rank") or "?"
            title = str(p.get("title") or "")[:42]
            med = p.get("model_market_median_eur")
            med_bit = f" · med {med}€" if med else ""
            ctk.CTkLabel(
                top_picks_chips,
                text=f"#{rk} {pr}€{med_bit} · {title}",
                font=F_CAP,
                fg_color=st.color,
                text_color=st.text_color,
                corner_radius=8,
                height=28,
            ).pack(side="left", padx=3, pady=3)

    def _render_platform_analytics(analytics: Optional[Dict[str, Any]]) -> None:
        for w in platform_analytics_chips.winfo_children():
            w.destroy()
        if not analytics or not analytics.get("platforms"):
            platform_analytics_fr.pack_forget()
            return
        platform_analytics_fr.pack(fill="x", pady=(0, 8), padx=2, before=results_ctrl)
        platform_analytics_summary.configure(text=str(analytics.get("summary") or "—"))
        for plat, ps in sorted(
            analytics["platforms"].items(),
            key=lambda x: -int(x[1].get("count") or x[1].get("scraped_raw") or 0),
        ):
            st = platform_style(plat)
            kept = int(ps.get("count") or 0)
            raw = int(ps.get("scraped_raw") or 0)
            status = str(ps.get("status") or "")
            if kept > 0 and ps.get("median_eur"):
                txt = (
                    f"{st.label}  {kept} ({ps.get('pct', 0):.0f}%)"
                    f"  · méd. {ps.get('median_eur', 0):.0f}€"
                )
                if raw > kept:
                    txt += f"  · {raw} scrapées"
            elif raw > 0:
                txt = f"{st.label}  {raw} scrapées → 0 retenues"
            elif status == "error":
                txt = f"{st.label}  échec scrape"
            else:
                txt = f"{st.label}  0 annonce"
            chip_col = st.color
            if status == "error" or (raw == 0 and kept == 0):
                chip_col = "#4a3028"
            ctk.CTkLabel(
                platform_analytics_chips,
                text=txt,
                font=F_CAP,
                fg_color=chip_col,
                text_color=st.text_color,
                corner_radius=8,
                height=26,
            ).pack(side="left", padx=4, pady=2)

    def _render_analysis_tab() -> None:
        from .export import build_analysis_markdown

        md = state.analysis_markdown
        if not md:
            md = build_analysis_markdown(
                category=state.market_category,
                query=state.intent,
                brain=getattr(state.output, "category_brain", None) if state.output else None,
                rows=state.rows,
                platform_analytics=state.platform_analytics,
                triage_stats={
                    "scraped": state.run_summary.get("scraped"),
                    "after_triage": state.run_summary.get("after_triage"),
                    "rejected": state.run_summary.get("rejected"),
                    "doubts": state.run_summary.get("doubts"),
                },
            )
        # Append discovered criteria plan (if available) for inspection/debugging.
        try:
            plan = getattr(state.output, "criteria_plan", None) if state.output else None
            if not plan and state.output and state.output.folder:
                cand = Path(state.output.folder) / "criteria_plan.json"
                if cand.is_file():
                    plan = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(plan, dict) and plan:
                md += "\n\n---\n\n## Critères marché (IA)\n\n"
                md += "```json\n" + json.dumps(plan, ensure_ascii=False, indent=2)[:6000] + "\n```\n"
        except Exception:
            pass
        analysis_body.configure(state="normal")
        analysis_body.delete("1.0", "end")
        analysis_body.insert("1.0", md)
        analysis_body.configure(state="disabled")

    def _on_bus_event(event: str, payload: Dict[str, Any]) -> None:
        if event not in (
            "SOURCE_START",
            "SOURCE_DONE",
            "SOURCE_ERROR",
            "SOURCE_RETRY",
            "ORACLE_PRICE",
            "ANALYTICS_READY",
        ):
            return
        line = format_bus_log(event, payload)
        if line:
            _log_ui(line)

    BUS.on(_on_bus_event)

    def _on_run_end_metrics(evt: Dict[str, Any]) -> None:
        if evt.get("phase") != "run_end":
            return
        try:
            monitor_metrics_lbl.configure(
                text=(
                    f"catalog {evt.get('catalog', '—')} · offres {evt.get('offers', '—')} · "
                    f"rej {evt.get('rejected', '—')} · doutes {evt.get('doubtful', '—')}"
                )
            )
        except Exception:
            pass

    try:
        from .engine import subscribe

        subscribe(_on_run_end_metrics)
    except Exception:
        pass

    def _log_ui(msg: str) -> None:
        def _do() -> None:
            raw = (msg or "").rstrip("\n")
            raw = re.sub(r"^\d{2}:\d{2}:\d{2}\s*[·\-]\s*", "", raw)
            if not raw:
                return
            tb = _journal_text()
            ttag = _journal_log_tag(raw)
            ts = time.strftime("%H:%M:%S")
            try:
                tb.insert("end", f"{ts} ", "time")
                tb.insert("end", raw + "\n", ttag)
                logs_box.see("end")
            except Exception:
                logs_box.insert("end", f"{ts} {raw}\n")
                logs_box.see("end")
            if state.running or state.phase == "running":
                for part in raw.split("\n"):
                    if part.strip():
                        _append_run_log(part)
            try:
                _SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
                with _SESSION_LOG.open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {raw}\n")
            except Exception:
                pass
            if state.running:
                low = msg.lower()
                if "429" in msg or "rate limit" in low:
                    run_clock["extra_pause"] = float(run_clock.get("extra_pause") or 0) + 6.0
                m = __import__("re").search(r"pause\s+([\d.]+)s", low)
                if m:
                    try:
                        run_clock["extra_pause"] = float(run_clock.get("extra_pause") or 0) + float(m.group(1))
                    except ValueError:
                        pass

        root.after(0, _do)

    if not getattr(logging.getLogger(), "_vsb_log", False):
        h = _LogHandler(_log_ui)
        h.setFormatter(logging.Formatter("%(message)s"))
        root_log = logging.getLogger()
        root_log.addHandler(h)
        root_log.setLevel(logging.INFO)
        for name in ("vf", "vf.pipeline", "vf.market", "vf.ai"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
        setattr(logging.getLogger(), "_vsb_log", True)

    def _count_active_filters() -> int:
        n = 0
        for fid, var in state.filter_vars.items():
            try:
                v = var.get()
            except Exception:
                continue
            meta = state.filter_widgets.get(fid) or {}
            spec = meta.get("spec") or {}
            if _filter_is_active(fid, spec, v):
                n += 1
        if search_var.get().strip():
            n += 1
        qcode = _quality_label_to_code.get(results_quality_var.get(), "all")
        if qcode not in ("all", ""):
            n += 1
        plat_sel = results_platform_var.get() or "all"
        if plat_sel not in ("all", ""):
            n += 1
        return n

    def _format_header_stats(summary: Dict[str, Any]) -> str:
        scraped = summary.get("scraped", summary.get("catalog_count", "—"))
        retained = summary.get("after_triage", summary.get("analyzed", "—"))
        offers = summary.get("offers_visible", summary.get("shown", "—"))
        active = int(summary.get("filters_active") or 0)
        filt = f" ({active} filtre{'s' if active != 1 else ''} actif{'s' if active != 1 else ''})" if active else ""
        top = summary.get("top_score", "—")
        return f"{scraped} scrapées → {retained} retenues → {offers} offres{filt} · top {top}"

    def _set_phase(phase: str) -> None:
        state.phase = phase
        idle_frame.grid_remove()
        setup_frame.grid_remove()
        run_frame.grid_remove()
        results_frame.grid_remove()
        if phase == "idle":
            idle_frame.grid(row=0, column=0, sticky="nsew")
            header_stats.set("")
            _reset_setup_ui()
        elif phase == "setup":
            setup_frame.grid(row=0, column=0, sticky="nsew")
            header_stats.set("Configuration…")
        elif phase == "running":
            run_frame.grid(row=0, column=0, sticky="nsew")
            header_stats.set("Analyse…")
            try:
                run_log_live.configure(text="● LIVE", text_color=OK)
            except Exception:
                pass
        else:
            results_frame.grid(row=0, column=0, sticky="nsew")
            header_stats.set(_format_header_stats(state.run_summary))

    def _update_steps(current: int, done: bool = False) -> None:
        for num, _, _ in STEPS:
            lbl = step_labels[num]
            if num < current or (done and num <= current):
                lbl.configure(text=f"✓ {num}. {STEPS[num-1][1]}", text_color=OK)
            elif num == current:
                lbl.configure(text=f"▸ {num}. {STEPS[num-1][1]}", text_color=WARM_CREAM)
            else:
                lbl.configure(text=f"{num}. {STEPS[num-1][1]}", text_color=TEXT_MUTED)

    def _highlight_preset(preset_id: str) -> None:
        for pid, btn in preset_buttons.items():
            if pid and pid == preset_id:
                btn.configure(
                    fg_color=BURNT_SIENNA,
                    hover_color=BURNT_SIENNA,
                    text_color=WARM_CREAM,
                    border_color=BURNT_SIENNA,
                    border_width=2,
                )
            else:
                btn.configure(
                    fg_color=BG,
                    hover_color="#3a2218",
                    text_color=TEXT,
                    border_color=BORDER_ACTIVE,
                    border_width=1,
                )

    def _reset_filters_to_show_all() -> None:
        """Tous les filtres sur « afficher tout » (évite 0 résultat par filtre IA actif)."""
        _filter_guard["build"] = True
        try:
            for fid, meta in list(state.filter_widgets.items()):
                spec = meta.get("spec") or {}
                ftype = str(spec.get("type") or "select")
                var = meta.get("var")
                if var is None:
                    continue
                cmap = meta.get("code_by_label") if isinstance(meta.get("code_by_label"), dict) else {}
                opts = [str(o) for o in (spec.get("options") or [])]
                if ftype == "bool" or "on" in opts:
                    code = "all"
                elif "tout" in opts:
                    code = "tout"
                else:
                    code = "all" if "all" in opts else (opts[0] if opts else "all")
                opt_labels = spec.get("option_labels") if isinstance(spec.get("option_labels"), dict) else {}
                label = str(opt_labels.get(code, code))
                try:
                    var.set(label)
                except Exception:
                    pass
            results_quality_var.set(_QUALITY_LABELS["all"])
            results_platform_var.set("all")
            _rebuild_platform_chips()
        finally:
            _filter_guard["build"] = False
        _refresh_filter_styles()

    def _apply_preset_values(values: Dict[str, str]) -> None:
        """Applique les filtres avancés sans écraser la vue de tri."""
        skip = {"quality_tier"}
        _filter_guard["build"] = True
        try:
            for fid, val in values.items():
                if fid in skip:
                    continue
                v = state.filter_vars.get(fid)
                if v is None:
                    continue
                meta = state.filter_widgets.get(fid) or {}
                spec = meta.get("spec") or {}
                opt_labels = spec.get("option_labels") if isinstance(spec.get("option_labels"), dict) else {}
                display = str(opt_labels.get(val, val)) if opt_labels else val
                try:
                    v.set(display)
                except Exception:
                    pass
            qv = values.get("quality_tier")
            if qv and qv in _QUALITY_LABELS:
                results_quality_var.set(_QUALITY_LABELS[qv])
        finally:
            _filter_guard["build"] = False
        _refresh_filter_styles()

    def _apply_view_preset(preset: Dict[str, Any]) -> None:
        state.active_preset_id = str(preset.get("id") or "")
        state.view_mode = str(preset.get("view_mode") or "all")
        vals = dict(preset.get("values") or {})
        vm = state.view_mode
        if vm == "gems_first":
            vals["quality_tier"] = "pepites"
            vals["lumens_band"] = "all"
            vals["pack_level"] = "all"
            vals["score_band"] = "all"
        elif vm == "no_junk":
            vals["quality_tier"] = "sans_dinosaure"
        elif vm == "quality_first":
            vals["quality_tier"] = "sans_dinosaure"
        _apply_preset_values(vals)
        _highlight_preset(state.active_preset_id)
        _apply_filters()

    def _rebuild_presets() -> None:
        return

    def _primary_weight_key(category: str = "") -> str:
        cat = (category or state.market_category or "generic").strip()
        if cat == "console_ds":
            return "completeness"
        if cat == "projector":
            return "completeness"
        if cat.startswith("console_"):
            return "completeness"
        return "buyer_fit"

    def _open_detail(r: Dict[str, Any]) -> None:
        _show_detail(r)
        try:
            detail_win.deiconify()
            detail_win.lift()
            detail_win.focus_force()
        except Exception:
            pass

    def _clear_filters_ui() -> None:
        for w in filt_grid.winfo_children():
            w.destroy()
        state.filter_vars.clear()
        state.filter_widgets.clear()

    def _filter_code(fid: str, val: Any) -> str:
        meta = state.filter_widgets.get(fid) or {}
        cmap = meta.get("code_by_label") if isinstance(meta.get("code_by_label"), dict) else {}
        raw = str(val or "")
        return str(cmap.get(raw, raw))

    def _filter_is_active(fid: str, spec: Dict[str, Any], val: Any) -> bool:
        code = _filter_code(fid, val)
        default = state.pre_filter_defaults.get(fid)
        if default is not None and str(code) == str(default):
            return False
        ftype = str(spec.get("type") or "")
        if fid == "hide_accessories":
            return code == "masquer"
        if ftype == "select":
            return code not in ("", "all", "tout", "off")
        if ftype == "number":
            return bool(code.strip())
        return code in ("on", "1", "true", "yes", "oui")

    def _refresh_filter_styles() -> None:
        vals = _filter_values()
        active_n = 0
        for fid, meta in state.filter_widgets.items():
            fr = meta.get("frame")
            spec = meta.get("spec") or {}
            if fr is None:
                continue
            active = _filter_is_active(fid, spec, vals.get(fid))
            if active:
                active_n += 1
            if active:
                fr.configure(border_color=BURNT_SIENNA, fg_color="#3a2218")
            else:
                fr.configure(border_color=BORDER, fg_color=DARK_CORK)
        try:
            if active_n:
                filt_active_lbl.configure(text=f"{active_n} filtre(s) actif(s)")
            else:
                filt_active_lbl.configure(text="")
        except Exception:
            pass

    _FILTER_PRIORITY = (
        "product_model_key",
        "exact_model",
        "ds_model_family",
        "price_max",
        "price_min",
        "lumens_band",
        "resolution",
        "projector_brand",
        "value_score_min",
        "projector_usage",
        "has_hdmi",
        "exclude_screen_only",
        "exclude_mini_weak",
        "listing_type",
        "controller_count",
        "games_included",
        "ps4_variant",
        "ps4_storage",
        "wheel_size",
        "bike_brand",
        "is_ebike",
        "bike_usage",
        "frame_size",
        "intent_exact_only",
        "score_band",
        "quality_tier",
        "has_stylus",
        "includes_games",
        "condition_class",
        "phone_storage",
        "phone_unlocked",
        "risk_mode",
        "hide_accessories",
        "variant",
    )

    def _order_filter_specs(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_id: Dict[str, Dict[str, Any]] = {}
        seen_fields: set[str] = set()
        for raw in specs:
            s = normalize_filter_spec(raw)
            sid = str(s.get("id") or "")
            fld = str(s.get("field") or sid)
            if sid and sid not in by_id and fld not in seen_fields:
                by_id[sid] = s
                seen_fields.add(fld)
        ordered: List[Dict[str, Any]] = []
        for pid in _FILTER_PRIORITY:
            if pid in by_id:
                ordered.append(by_id.pop(pid))
        rest = sorted(by_id.values(), key=lambda x: str(x.get("label") or ""))
        merged = [s for s in (ordered + rest) if str(s.get("id") or "") != "quality_tier"]
        return merged[:10]

    def _row_platform_key(row: Dict[str, Any]) -> str:
        pk = str(row.get("platform") or row.get("source") or "vinted").lower().replace("cash-express", "cashexpress")
        if pk == "lbc":
            return "leboncoin"
        return pk

    def _rebuild_platform_chips() -> None:
        for w in platform_chip_host.winfo_children():
            w.destroy()
        platform_chip_buttons.clear()
        counts: Dict[str, int] = {}
        for r in state.rows:
            pk = _row_platform_key(r)
            counts[pk] = counts.get(pk, 0) + 1
        sel = results_platform_var.get() or "all"

        def _style_chip(btn: ctk.CTkButton, active: bool, color: str = "") -> None:
            if active:
                btn.configure(
                    fg_color=BURNT_SIENNA,
                    hover_color="#b84500",
                    text_color=WARM_CREAM,
                    border_color=BURNT_SIENNA,
                )
            else:
                btn.configure(
                    fg_color=BG,
                    hover_color="#3a2218",
                    text_color=TEXT,
                    border_color=color or BORDER_ACTIVE,
                )

        def _pick_platform(key: str) -> None:
            if _filter_guard["build"]:
                return
            results_platform_var.set(key)
            state.active_preset_id = ""
            _highlight_preset("")
            _rebuild_platform_chips()
            _schedule_apply_filters()

        all_btn = ctk.CTkButton(
            platform_chip_host,
            text=f"Toutes ({len(state.rows)})",
            height=30,
            corner_radius=RADIUS_GHOST,
            fg_color=BG,
            hover_color="#3a2218",
            text_color=TEXT,
            border_width=1,
            border_color=BORDER_ACTIVE,
            font=F_CAP,
            command=lambda: _pick_platform("all"),
        )
        all_btn.pack(side="left", padx=(0, 4), pady=2)
        platform_chip_buttons["all"] = all_btn
        _style_chip(all_btn, sel == "all")
        top5_btn = ctk.CTkButton(
            platform_chip_host,
            text="Top 5 / plateforme",
            height=30,
            corner_radius=RADIUS_GHOST,
            fg_color=BG,
            hover_color="#3a2218",
            text_color=TEXT,
            border_width=1,
            border_color=BORDER_ACTIVE,
            font=F_CAP,
            command=lambda: _pick_platform("top5_each"),
        )
        top5_btn.pack(side="left", padx=(0, 8), pady=2)
        platform_chip_buttons["top5_each"] = top5_btn
        _style_chip(top5_btn, sel == "top5_each")
        for pk in ("vinted", "leboncoin", "ebay"):
            if pk not in counts:
                continue
            st = platform_style(pk)
            btn = ctk.CTkButton(
                platform_chip_host,
                text=f"{st.label} ({counts[pk]})",
                height=30,
                corner_radius=RADIUS_GHOST,
                fg_color=BG,
                hover_color="#3a2218",
                text_color=TEXT,
                border_width=1,
                border_color=st.color,
                font=F_CAP,
                command=lambda k=pk: _pick_platform(k),
            )
            btn.pack(side="left", padx=(0, 4), pady=2)
            platform_chip_buttons[pk] = btn
            _style_chip(btn, sel == pk, st.color)

    def _build_filters_ui(specs: List[Dict[str, Any]]) -> None:
        _clear_filters_ui()
        _filter_guard["build"] = True
        defaults = {
            "risk_mode": "all",
            "hide_accessories": "tout",
            "score_band": "all",
            "quality_tier": "all",
            "resolution": "all",
            "lumens_band": "all",
            "variant": "all",
            "price_max": "all",
            "price_min": "all",
            "value_score_min": "all",
            "exclude_screen_only": "all",
            "exclude_mini_weak": "all",
        }
        row_fr: Optional[ctk.CTkFrame] = None
        try:
            for chip_idx, spec in enumerate(_order_filter_specs(specs)):
                fid = str(spec.get("id") or "")
                if not fid or fid == "quality_tier":
                    continue
                opts = list(spec.get("options") or ["all"])
                if len(opts) <= 1:
                    continue
                label = str(spec.get("label") or fid)
                if chip_idx % 2 == 0:
                    row_fr = ctk.CTkFrame(filt_grid, fg_color="transparent")
                    row_fr.pack(fill="x", pady=3)
                fr = ctk.CTkFrame(
                    row_fr,
                    fg_color=DARK_CORK,
                    corner_radius=RADIUS_GHOST,
                    border_width=1,
                    border_color=BORDER,
                )
                fr.pack(side="left", fill="x", expand=True, padx=3, pady=2)
                opts = list(spec.get("options") or ["all"])
                opt_labels = spec.get("option_labels") if isinstance(spec.get("option_labels"), dict) else {}
                display_opts = [str(opt_labels.get(o, o)) for o in opts]
                code_by_label = {str(opt_labels.get(o, o)): o for o in opts}
                default_code = defaults.get(fid, opts[0] if opts else "all")
                pre = (state.pre_filter_defaults or {}).get(fid)
                if pre and pre in opts:
                    default_code = pre
                if fid == "quality_tier":
                    init_label = results_quality_var.get()
                else:
                    init_label = str(opt_labels.get(default_code, default_code))
                var = ctk.StringVar(value=init_label)
                state.filter_vars[fid] = var
                state.filter_widgets[fid] = {
                    "frame": fr,
                    "spec": spec,
                    "var": var,
                    "code_by_label": code_by_label,
                }

                def _adv_filter_change(choice: str, filter_id: str = fid) -> None:
                    if _filter_guard["build"]:
                        return
                    if filter_id == "quality_tier":
                        results_quality_var.set(choice)
                        state.active_preset_id = ""
                        _highlight_preset("")
                    _schedule_apply_filters()

                ctk.CTkLabel(fr, text=label, font=F_CAP, text_color=TEXT_MUTED).pack(
                    anchor="w", padx=8, pady=(6, 0)
                )
                menu_w = max(120, min(200, 11 * max([len(s) for s in display_opts] + [8])))
                ctk.CTkOptionMenu(
                    fr,
                    variable=var,
                    values=display_opts if display_opts else opts,
                    width=menu_w,
                    height=30,
                    command=_adv_filter_change,
                    fg_color=DARK_CORK,
                    button_color=BURNT_SIENNA,
                    button_hover_color="#b84500",
                    dropdown_fg_color=DARK_CORK,
                    dropdown_hover_color="#4a2a18",
                    text_color=TEXT,
                ).pack(anchor="w", padx=8, pady=(2, 8))
        finally:
            _filter_guard["build"] = False
        _refresh_filter_styles()

    def _filter_values() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in state.filter_vars.items():
            try:
                raw = v.get()
            except Exception:
                raw = v
            meta = state.filter_widgets.get(k) or {}
            cmap = meta.get("code_by_label") if isinstance(meta.get("code_by_label"), dict) else {}
            out[k] = cmap.get(str(raw), raw) if cmap else raw
        return out

    _rej_render_gen = {"n": 0}
    _rej_view_rows: List[Dict[str, Any]] = []

    def _render_one_rejected_card(i: int, r: Dict[str, Any]) -> None:
        enrich_row_listing_fields(r)
        fr = ctk.CTkFrame(rej_scroll, fg_color=DARK_CORK, corner_radius=RADIUS_CARD, border_width=1, border_color=BORDER)
        fr.pack(fill="x", pady=4, padx=2)
        row = ctk.CTkFrame(fr, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=10)
        thumb = ctk.CTkLabel(row, text="…", width=72, height=72, fg_color="#1a120c")
        thumb.pack(side="left", padx=(0, 10))
        _bind_thumb(thumb, r, f"rej{i}")
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True)
        top = ctk.CTkFrame(mid, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(
            top,
            text=str(r.get("reject_stage") or r.get("triage_reasons") or r.get("triage_decision") or "rejet")[:40],
            font=F_CAP,
            text_color=DANGER,
            width=140,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(top, text=listing_platform_label(r), font=F_CAP, text_color=TEXT_MUTED).pack(
            side="left", padx=6
        )
        pe = listing_price_eur(r)
        ctk.CTkLabel(
            top,
            text=f"{pe:.0f} €" if pe is not None else "—",
            font=F_CAP,
            text_color=WARM_CREAM,
        ).pack(side="right")
        ctk.CTkLabel(
            mid,
            text=str(r.get("title") or "—")[:120],
            font=F_BODY,
            text_color=TEXT,
            anchor="w",
            wraplength=680,
            justify="left",
        ).pack(fill="x", anchor="w", pady=(4, 0))
        sub = ctk.CTkLabel(
            fr,
            text=str(r.get("reject_reason") or r.get("triage_reasons") or "—")[:200],
            font=F_CAP,
            text_color=TEXT_MUTED,
            wraplength=900,
            justify="left",
            anchor="w",
        )
        sub.pack(anchor="w", padx=12, pady=(0, 6))
        btns = ctk.CTkFrame(fr, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 10))
        u = listing_url_for_row(r)

        def _open_rej(uu: str = u) -> None:
            if uu:
                open_listing_url(uu)

        def _restore_rej(rr: Dict[str, Any] = r) -> None:
            state.rows.append(dict(rr))
            state.rows.sort(
                key=lambda x: float(x.get("rank_score") or x.get("final_score") or 0),
                reverse=True,
            )
            normalize_run_rank_scores(state.rows, main_only=True)
            state.rejected_rows = [x for x in state.rejected_rows if x.get("id") != rr.get("id")]
            _render_rejected_list()
            _apply_filters()

        ctk.CTkButton(
            btns,
            text=f"{listing_platform_label(r)} →",
            width=100,
            height=28,
            fg_color=BURNT_SIENNA,
            text_color=WARM_CREAM,
            command=_open_rej,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns,
            text="Réintégrer dans Offres",
            height=28,
            fg_color=BG,
            text_color=TEXT,
            command=_restore_rej,
        ).pack(side="left")

    def _render_rejected_list(*, reset: bool = True) -> None:
        nonlocal _rej_view_rows
        if reset:
            _rej_render_gen["n"] += 1
            state.rejected_rendered = 0
            for w in rej_scroll.winfo_children():
                w.destroy()
            rej_load_btn.grid_forget()
        rows = list(state.rejected_rows or [])
        q = rej_search_var.get().strip().lower()
        if q:
            rows = [
                r
                for r in rows
                if q in f"{r.get('title') or ''} {r.get('reject_reason') or ''} {r.get('triage_reasons') or ''}".lower()
            ]
        _rej_view_rows = rows[:500]
        shown = min(state.rejected_rendered, len(_rej_view_rows)) if not reset else 0
        if reset:
            state.rejected_rendered = 0
        rej_stats_var.set(
            f"{shown} affichées sur {len(_rej_view_rows)} rejetées "
            f"(tri : hors sujet, accessoire, mauvaise plateforme…)"
        )
        if not _rej_view_rows:
            ctk.CTkLabel(
                rej_scroll,
                text="Aucune annonce rejetée pour ce run (ou filtre vide).",
                text_color=TEXT_MUTED,
                font=F_BODY,
            ).pack(pady=40)
            return
        gen = _rej_render_gen["n"]
        if reset:
            target_end = min(len(_rej_view_rows), INITIAL_REJECTED_RENDER)
        else:
            target_end = min(state.rejected_rendered + LIST_PAGE_SIZE, len(_rej_view_rows))
        start_at = state.rejected_rendered if not reset else 0

        def _batch(start: int) -> None:
            if gen != _rej_render_gen["n"]:
                return
            end = min(start + REJECTED_RENDER_BATCH, target_end)
            for j in range(start, end):
                try:
                    _render_one_rejected_card(j + 1, _rej_view_rows[j])
                except Exception as exc:
                    logger.warning("Carte rejetée #%s: %s", j + 1, exc)
            if end < target_end:
                root.after(14, lambda s=end: _batch(s))
            else:
                state.rejected_rendered = target_end
                rej_stats_var.set(
                    f"{state.rejected_rendered} affichées sur {len(_rej_view_rows)} rejetées "
                    f"(tri : hors sujet, accessoire, mauvaise plateforme…)"
                )
                if state.rejected_rendered < len(_rej_view_rows):
                    rej_load_btn.configure(
                        text=f"Charger plus ({state.rejected_rendered}/{len(_rej_view_rows)})"
                    )
                    rej_load_btn.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
                else:
                    rej_load_btn.grid_forget()

        _batch(start_at)

    def _load_more_rejected() -> None:
        _render_rejected_list(reset=False)

    rej_load_btn.configure(command=_load_more_rejected)

    def _remove_doubt_row(rr: Dict[str, Any]) -> None:
        rid = str(rr.get("id") or "")
        state.doubtful_rows = [x for x in state.doubtful_rows if str(x.get("id") or "") != rid]
        _render_inline_doubts()
        _render_doubtful_list()
        try:
            n = len(state.doubtful_rows)
            tabs.tab("Doutes").configure(text=f"Doutes ({n})" if n else "Doutes")
        except Exception:
            pass

    def _build_doubt_card(parent: Any, r: Dict[str, Any], *, key_prefix: str) -> None:
        fr = ctk.CTkFrame(
            parent,
            fg_color=DARK_CORK,
            corner_radius=RADIUS_CARD,
            border_width=1,
            border_color=WARN,
        )
        fr.pack(fill="x", pady=4, padx=2)
        row = ctk.CTkFrame(fr, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=10)
        thumb = ctk.CTkLabel(row, text="…", width=72, height=72, fg_color="#1a120c")
        thumb.pack(side="left", padx=(0, 10))
        _bind_thumb(thumb, r, f"d{key_prefix}")
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True)
        top = ctk.CTkFrame(mid, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(
            top,
            text=str(r.get("reject_stage") or "doute"),
            font=F_CAP,
            text_color=WARN,
            width=90,
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            top,
            text=listing_platform_label(r),
            font=F_CAP,
            text_color=TEXT_MUTED,
        ).pack(side="left", padx=6)
        pe = listing_price_eur(r)
        ctk.CTkLabel(
            top,
            text=f"{pe:.2f} €" if pe is not None else "—",
            font=F_CAP,
            text_color=WARM_CREAM,
        ).pack(side="right")
        ctk.CTkLabel(
            mid,
            text=str(r.get("title") or "—")[:120],
            font=F_BODY,
            text_color=TEXT,
            anchor="w",
            wraplength=680,
            justify="left",
        ).pack(fill="x", anchor="w", pady=(4, 2))
        from .engine import explain_triage_reasons

        doubt_txt = explain_triage_reasons(
            str(r.get("reject_reason") or r.get("triage_reasons") or "")
        )
        ctk.CTkLabel(
            mid,
            text=doubt_txt[:220],
            font=F_CAP,
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=680,
            justify="left",
        ).pack(fill="x", anchor="w")
        btns = ctk.CTkFrame(fr, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 10))
        u = listing_url_for_row(r)

        def _open_d(uu: str = u) -> None:
            if uu:
                open_listing_url(uu)

        def _restore_d(rr: Dict[str, Any] = r) -> None:
            try:
                dlg = ctk.CTkInputDialog(
                    text="Pourquoi réintégrer ? (ex: bon modèle, faux doute)",
                    title="Doute infirmé",
                )
                note = (dlg.get_input() or "").strip()
                from .engine import record_doubt_training

                record_doubt_training(
                    str(rr.get("title") or ""),
                    action="reject",
                    category=str(state.market_category or rr.get("item_type") or ""),
                    reason=note or "faux_doute",
                )
            except Exception:
                pass
            state.rows.append(dict(rr))
            state.rows.sort(
                key=lambda x: float(x.get("rank_score") or x.get("final_score") or 0),
                reverse=True,
            )
            normalize_run_rank_scores(state.rows, main_only=True)
            _remove_doubt_row(rr)
            _apply_filters()

        def _confirm_doubt(rr: Dict[str, Any] = r) -> None:
            try:
                dlg = ctk.CTkInputDialog(
                    text="Pourquoi ce doute est juste ? (3 mots max conseillés)",
                    title="Valider le doute",
                )
                note = (dlg.get_input() or "").strip()
                from .engine import record_doubt_training

                record_doubt_training(
                    str(rr.get("title") or ""),
                    action="validate",
                    category=str(state.market_category or rr.get("item_type") or ""),
                    reason=note or str(rr.get("triage_reasons") or "doute"),
                )
            except Exception:
                pass
            _dismiss_d(rr)

        def _dismiss_d(rr: Dict[str, Any] = r) -> None:
            try:
                from .engine import record_feedback

                record_feedback(
                    str(rr.get("title") or ""),
                    action="dismiss",
                    category=str(state.market_category or rr.get("item_type") or ""),
                    reason=str(rr.get("triage_reasons") or rr.get("reject_reason") or "dismiss"),
                )
            except Exception:
                pass
            _remove_doubt_row(rr)

        ctk.CTkButton(
            btns,
            text=f"{listing_platform_label(r)} →",
            width=120,
            height=28,
            fg_color=BURNT_SIENNA,
            text_color=WARM_CREAM,
            command=_open_d,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns,
            text="Valider doute",
            height=28,
            fg_color=WARN,
            text_color="#1a120c",
            command=_confirm_doubt,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns,
            text="Réintégrer",
            height=28,
            fg_color=BG,
            text_color=TEXT,
            command=_restore_d,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns,
            text="Écarter",
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            command=_dismiss_d,
        ).pack(side="left")

    def _render_inline_doubts() -> None:
        """Panneau doute retiré de l'onglet Offres — tout est dans Doutes."""
        try:
            inline_doubt_fr.pack_forget()
        except Exception:
            pass

    _doubt_render_gen = {"n": 0}
    _doubt_view_rows: List[Dict[str, Any]] = []

    def _render_doubtful_list(*, reset: bool = True) -> None:
        nonlocal _doubt_view_rows
        if reset:
            _doubt_render_gen["n"] += 1
            state.doubtful_rendered = 0
            for w in doubt_scroll.winfo_children():
                w.destroy()
            doubt_load_btn.grid_forget()
        rows = list(state.doubtful_rows or [])
        q = doubt_search_var.get().strip().lower()
        if q:
            rows = [
                r
                for r in rows
                if q
                in f"{r.get('title') or ''} {r.get('reject_reason') or ''} {r.get('triage_reasons') or ''}".lower()
            ]
        _doubt_view_rows = rows[:500]
        if reset:
            state.doubtful_rendered = 0
        doubt_stats_var.set(
            f"{state.doubtful_rendered} affichées sur {len(_doubt_view_rows)} doutes"
        )
        if not _doubt_view_rows:
            ctk.CTkLabel(
                doubt_scroll,
                text="Aucun cas ambigu (ou filtre vide). Les annonces douteuses pour cette recherche apparaîtront ici.",
                text_color=TEXT_MUTED,
                font=F_BODY,
                wraplength=720,
            ).pack(pady=40, padx=12)
            return
        gen = _doubt_render_gen["n"]
        if reset:
            target_end = min(len(_doubt_view_rows), INITIAL_DOUBT_RENDER)
        else:
            target_end = min(state.doubtful_rendered + LIST_PAGE_SIZE, len(_doubt_view_rows))
        start_at = state.doubtful_rendered if not reset else 0

        def _batch(start: int) -> None:
            if gen != _doubt_render_gen["n"]:
                return
            end = min(start + DOUBT_RENDER_BATCH, target_end)
            for j in range(start, end):
                try:
                    _build_doubt_card(doubt_scroll, _doubt_view_rows[j], key_prefix=f"tab{j}")
                except Exception as exc:
                    logger.warning("Carte doute #%s: %s", j + 1, exc)
            if end < target_end:
                root.after(14, lambda s=end: _batch(s))
            else:
                state.doubtful_rendered = target_end
                doubt_stats_var.set(
                    f"{state.doubtful_rendered} affichées sur {len(_doubt_view_rows)} doutes"
                )
                if state.doubtful_rendered < len(_doubt_view_rows):
                    doubt_load_btn.configure(
                        text=f"Charger plus ({state.doubtful_rendered}/{len(_doubt_view_rows)})"
                    )
                    doubt_load_btn.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
                else:
                    doubt_load_btn.grid_forget()

        _batch(start_at)

    def _load_more_doubts() -> None:
        _render_doubtful_list(reset=False)

    doubt_load_btn.configure(command=_load_more_doubts)

    def _show_detail(r: Dict[str, Any]) -> None:
        if not r.get("_pack_reconciled"):
            enrich_pack_fields(r)
        state.selected = r
        d_title.configure(text=str(r.get("title") or "—"))
        tp = listing_price_eur(r)
        lp = r.get("list_price_eur")
        if tp is not None and lp and abs(float(lp) - float(tp)) > 0.5:
            d_price.configure(text=f"{float(tp):.0f} € réel · affiché {float(lp):.0f} €")
        else:
            d_price.configure(text=f"{tp:.0f} €" if tp is not None else "—")
        rank_sc = display_rank_score(r)
        comp_sc = r.get("score_composite")
        tier = str(r.get("quality_tier") or "")
        rp = r.get("rank_position")
        pct = r.get("rank_percentile")
        meta_bits = []
        if rp:
            meta_bits.append(f"#{rp} du classement")
        if pct is not None:
            meta_bits.append(f"top {pct:.0f}%")
        if str(r.get("triage_decision") or "") == "doubtful":
            try:
                tconf = float(r.get("triage_confidence") or 0)
                meta_bits.insert(0, f"triage douteux ({tconf:.0%})")
            except (TypeError, ValueError):
                meta_bits.insert(0, "triage douteux")
        d_rank_meta.configure(text="  ·  ".join(meta_bits) if meta_bits else "")
        d_score.configure(text=f"Affaire {rank_sc:.0f}/100" + (f"  ·  comp {float(comp_sc):.0f}" if comp_sc else "") + (f"  ·  {tier}" if tier else ""))
        d_score.configure(text_color=DANGER if bool(r.get("deal_blocked")) else BURNT_SIENNA)
        tips = deal_advice_lines(r)
        d_assistant.configure(text="\n".join(tips) if tips else "Sélectionnez une annonce pour les conseils acheteur.")
        d_body.configure(state="normal")
        d_body.delete("1.0", "end")
        spec_line = ""
        cat = str(r.get("item_type") or "")
        if cat == "projector":
            bits = []
            if r.get("lumens"):
                bits.append(f"{r.get('lumens')} lm")
            if r.get("native_aspect"):
                bits.append(str(r.get("native_aspect")))
            if r.get("resolution"):
                bits.append(str(r.get("resolution")))
            if r.get("viability_grade"):
                bits.append(f"viab. {r.get('viability_grade')}")
            if r.get("projector_brand"):
                bits.append(str(r.get("projector_brand")))
            for k, lbl in (
                ("has_hdmi", "HDMI"),
                ("has_power_cable", "Alim"),
                ("has_remote", "Télécommande"),
                ("has_box", "Boîte"),
            ):
                if r.get(k):
                    bits.append(lbl)
            spec_line = "Specs : " + (" · ".join(bits) if bits else "— (titre uniquement)")
        elif cat.startswith("console"):
            bits = []
            mdl_lbl = str(r.get("product_model_label") or r.get("ai_model_exact") or "").strip()
            variant = str(r.get("variant") or "").strip()
            if variant and variant.lower() not in (mdl_lbl.lower(), ""):
                bits.append(variant)
            if r.get("has_stylus") and not r.get("missing_stylus"):
                bits.append("Stylet")
            elif r.get("missing_stylus"):
                bits.append("sans stylet")
            for k, lbl in (
                ("includes_games", "Jeux"),
                ("has_controller", "Manette"),
                ("has_power_cable", "Chargeur"),
                ("has_box", "Boîte"),
            ):
                if r.get(k):
                    bits.append(lbl)
            spec_line = "Pack : " + (" · ".join(bits) if bits else "—")
        url = listing_url_for_row(r)
        if url:
            r["url"] = url
        seller = str(r.get("seller_username") or "")
        oracle_bits = ""
        if r.get("ref_price_refurb_eur") or r.get("ref_price_new_eur") or r.get("ref_price_stockx_eur"):
            deg = " ⚠ cote dégradée" if r.get("oracle_degraded") else ""
            arb = r.get("oracle_arbiter_ref_eur")
            arb_bit = f" · ref arbitrée ~{arb} €" if arb else ""
            oracle_bits = (
                f"  ·  Oracle refurb ~{r.get('ref_price_refurb_eur', '—')} €"
                f" · neuf ~{r.get('ref_price_new_eur', '—')} €"
                f" · StockX ~{r.get('ref_price_stockx_eur', '—')} €"
                f"{arb_bit}"
                f" ({r.get('price_reference_sources') or '—'}){deg}"
            )
        from .engine import clamp_discount_pct

        def _pct(v: Any) -> str:
            if v is None or v == "":
                return "—"
            d = clamp_discount_pct(v)
            if abs(d) < 0.05:
                return "—"
            return f"{d:+.1f}%"

        mdisc = _pct(r.get("model_vs_market_pct") or r.get("market_discount_pct"))
        dnew = _pct(r.get("discount_vs_new_pct"))
        drefurb = _pct(r.get("discount_vs_refurb_pct"))
        price = listing_price_eur(r)
        list_p = r.get("list_price_eur")
        med = r.get("model_market_median_eur")
        refurb = r.get("ref_price_refurb_eur") or r.get("oracle_arbiter_ref_eur")
        neuf = r.get("ref_price_new_eur") or r.get("msrp_eur")
        if list_p and price and abs(float(list_p) - float(price)) > 0.5:
            ladder = f"Affiché {list_p}€ → coût réel ~{price:.0f}€"
            if r.get("landed_cost_note"):
                ladder += f" ({r.get('landed_cost_note')})"
        else:
            ladder = f"Annonce {price}€"
        if r.get("is_discontinued"):
            ladder += "  ·  rétro/discontinué (pas de ref. neuf MSRP)"
        if med:
            ladder += f"  →  médiane run {med}€ ({mdisc})"
        if refurb:
            ladder += f"  →  refurb ~{refurb}€ ({drefurb})"
        if neuf:
            ladder += f"  →  neuf ~{neuf}€ ({dnew})"
        lines = [
            ladder,
            f"Échelle prix : annonce · marché · reconditionné · neuf{oracle_bits}",
            f"Qualité {r.get('quality_grade', '—')}  ·  tier {r.get('quality_tier') or '—'}  ·  {r.get('item_type', '—')}",
            spec_line,
            f"Modèle {r.get('product_model_label') or r.get('ai_model_exact') or r.get('variant') or '—'}",
            (
                f"Médiane modèle ~{r.get('model_market_median_eur')} € ({r.get('model_market_n')} annonces)"
                if r.get("model_market_median_eur")
                else ""
            ),
            f"Stockage {r.get('storage_gb') or '—'} Go  ·  pack {r.get('ai_completeness', '—')}%",
            _seller_detail_line(r, seller),
            _seller_risk_line(r),
            f"Accessoire: {'oui' if r.get('is_accessory') else 'non'}"
            + (f" ({r.get('accessory_kind')})" if r.get('accessory_kind') else ""),
            "",
            "— Lien annonce —",
            url or "(URL absente)",
            "",
            "— Composition du score —",
            *format_score_breakdown(r),
            "",
            "— Synthèse IA —",
            str(r.get("ai_specs_summary") or "—")[:400],
            str(r.get("ai_vision_note") or "")[:400],
            "",
            "— Adéquation acheteur —",
            f"{r.get('buyer_fit_grade') or '—'} · score {r.get('buyer_fit_score') or '—'}",
            str(r.get("buyer_fit_notes") or "—")[:400],
            "",
            "— Audit —",
            str(r.get("ai_verdict") or "—")[:500],
            "",
            "— Description —",
            str(r.get("description") or r.get("description_excerpt") or "—")[:4000],
            "",
        ]
        d_body.insert("end", "\n".join(lines))
        d_body.configure(state="disabled")
        ip = str(r.get("image_path") or "")
        if ip and Image:
            try:
                im = Image.open(ip)
                im.thumbnail((520, 380))
                ci = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                _img_refs["d"] = ci
                d_img.configure(image=ci, text="")
            except Exception:
                d_img.configure(image=None, text="")

    def _row_thumb_key(r: Dict[str, Any]) -> str:
        plat = str(r.get("platform") or "vinted").lower()
        sid = str(r.get("platform_item_id") or r.get("id") or "")
        return f"{plat}_{sid}" if sid else ""

    def _thumb_disk_paths(r: Dict[str, Any]) -> List[Path]:
        cache_key = _row_thumb_key(r)
        sid = cache_key or str(r.get("id") or "")
        out: List[Path] = []
        for p in (
            r.get("image_path"),
            THUMB_CACHE / f"{cache_key}.jpg" if cache_key else None,
            THUMB_CACHE / f"{sid}.jpg" if sid else None,
        ):
            if p:
                pp = Path(str(p))
                if pp.is_file():
                    out.append(pp)
        return out

    def _thumb_from_disk(r: Dict[str, Any], key: str) -> Optional[Any]:
        if not Image:
            return None
        for p in _thumb_disk_paths(r):
            try:
                im = Image.open(p)
                im.thumbnail((96, 96))
                ci = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                _img_refs[key] = ci
                return ci
            except Exception:
                pass
        return None

    def _photo_url_for_row(r: Dict[str, Any]) -> str:
        url = str(r.get("photo_url") or "")
        if not url and r.get("photo_urls"):
            urls = r.get("photo_urls") or []
            if urls:
                url = str(urls[0])
        return url

    def _bind_thumb(label: Any, r: Dict[str, Any], key: str) -> None:
        """Miniature : disque immédiat, téléchargement réseau en arrière-plan (évite freeze UI)."""
        stable_key = _row_thumb_key(r) or key
        ci = _thumb_from_disk(r, stable_key)
        if ci:
            label.configure(image=ci, text="")
            return
        cache_key = stable_key or str(r.get("id") or "")
        url = _photo_url_for_row(r)
        if not url or not cache_key:
            return
        guard_key = f"dl:{cache_key}"
        if guard_key in _THUMB_DL_GUARD:
            return
        _THUMB_DL_GUARD.add(guard_key)
        dest = THUMB_CACHE / f"{cache_key}.jpg"

        def _work() -> Optional[str]:
            try:
                THUMB_CACHE.mkdir(parents=True, exist_ok=True)
                if download_image_url(url, dest, timeout=10.0):
                    return str(dest)
            except Exception:
                pass
            return None

        def _apply(path: Optional[str]) -> None:
            _THUMB_DL_GUARD.discard(guard_key)
            if not path:
                return
            try:
                if not label.winfo_exists():
                    return
            except Exception:
                return
            try:
                r["image_path"] = path
                im = Image.open(path)
                im.thumbnail((96, 96))
                ci2 = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                _img_refs[stable_key] = ci2
                label.configure(image=ci2, text="")
            except Exception:
                pass

        fut = _THUMB_EXEC.submit(_work)

        def _done(f: Any) -> None:
            try:
                path = f.result()
            except Exception:
                path = None
            root.after(0, lambda p=path: _apply(p))

        fut.add_done_callback(_done)

    def _thumb_for_row(r: Dict[str, Any], key: str) -> Optional[Any]:
        return _thumb_from_disk(r, key)

    _render_gen = {"n": 0}
    _render_busy = {"on": False}
    _filter_after: Dict[str, Any] = {"id": None}
    _filter_busy = {"on": False}

    def _show_list_busy(on: bool) -> None:
        if on:
            list_busy_lbl.place(relx=0.5, rely=0.45, anchor="center")
            list_busy_lbl.lift()
        else:
            list_busy_lbl.place_forget()

    def _refresh_deals_scroll() -> None:
        try:
            offers_main_scroll.update_idletasks()
            canvas = getattr(offers_main_scroll, "_parent_canvas", None)
            if canvas is not None:
                canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    def _schedule_apply_filters() -> None:
        aid = _filter_after.get("id")
        if aid is not None:
            try:
                root.after_cancel(aid)
            except Exception:
                pass
        _filter_after["id"] = root.after(450, _apply_filters)

    def _compute_filtered_rows() -> List[Dict[str, Any]]:
        specs = state.output.dynamic_filters if state.output else []
        if state.enriched_cache and len(state.enriched_cache) == len(state.rows):
            enriched_rows = [dict(r) for r in state.enriched_cache]
        else:
            enriched_rows = []
            for r in state.rows:
                row = dict(r)
                if not row.get("_pack_reconciled"):
                    enrich_pack_fields(row)
                enriched_rows.append(row)
        vals = _filter_values()
        qcode = _quality_label_to_code.get(results_quality_var.get(), "all")
        vals["quality_tier"] = qcode
        if state.view_mode == "no_junk":
            vals["quality_tier"] = "sans_dinosaure"
        elif state.view_mode == "gems_first":
            vals["quality_tier"] = "pepites"
        rows = apply_dynamic_filters(enriched_rows, vals, specs)
        rows = [
            r
            for r in rows
            if str(r.get("triage_decision") or "") != "doubtful" and not r.get("intent_mismatch")
        ]
        if state.view_mode == "gems_first":
            rows = [
                r
                for r in rows
                if str(r.get("deal_tier") or "") == "pepite" or str(r.get("quality_tier") or "") == "gem"
            ]
        plat_sel = results_platform_var.get() or "all"
        if plat_sel == "top5_each":
            by_plat: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                pk = _row_platform_key(r)
                by_plat.setdefault(pk, []).append(r)

            def _rank_key(rr: Dict[str, Any]) -> float:
                try:
                    return float(
                        rr.get("rank_final_score")
                        or rr.get("final_rank_score")
                        or rr.get("display_rank_score")
                        or rr.get("best_deal_score")
                        or rr.get("rank_score")
                        or 0.0
                    )
                except (TypeError, ValueError):
                    return 0.0

            merged: List[Dict[str, Any]] = []
            seen_ids: set[str] = set()
            for pk in ("vinted", "leboncoin", "ebay"):
                pool = by_plat.get(pk) or []
                if not pool:
                    continue
                for r in sorted(pool, key=_rank_key, reverse=True)[:5]:
                    rid = str(r.get("id") or "")
                    if rid and rid in seen_ids:
                        continue
                    if rid:
                        seen_ids.add(rid)
                    merged.append(r)
            rows = merged
        elif plat_sel != "all":
            rows = [r for r in rows if _row_platform_key(r) == plat_sel]
        q = search_var.get().strip().lower()
        if q:
            rows = [r for r in rows if q in f"{r.get('title') or ''}".lower()]
        rows = sort_rows_for_view(rows, state.view_mode)
        try:
            from .engine import sort_rows_by_sort_by

            rows = sort_rows_by_sort_by(rows, state.sort_by or "ai")
        except Exception:
            pass
        return rows

    def _render_one_card(i: int, r: Dict[str, Any]) -> None:
        row_data = dict(r)
        thumb_key = _row_thumb_key(row_data) or f"card_{i}"
        blocked = bool(row_data.get("deal_blocked"))
        tier = str(row_data.get("quality_tier") or "")
        border = BURNT_SIENNA if tier == "gem" else BORDER
        if blocked:
            border = DANGER
        elif tier == "dropship":
            border = "#5a4030"
        elif tier == "dinosaur":
            border = DANGER
        card = ctk.CTkFrame(
            deals_list,
            fg_color="#2a1810" if blocked else DARK_CORK,
            corner_radius=RADIUS_CARD,
            border_width=2 if tier == "gem" else 1,
            border_color=border,
            cursor="hand2",
        )
        card.pack(fill="x", pady=6, padx=4)

        try:
            enrich_row_listing_fields(row_data)
            u = listing_url_for_row(row_data)
            title_txt = str(row_data.get("title") or "—")[:110]
        except Exception:
            u = ""
            title_txt = str(row_data.get("title") or "Annonce")[:110]

        def pick_card(_e: Any = None, rr: Dict[str, Any] = row_data) -> None:
            _open_detail(rr)

        def open_card_url(_e: Any = None, uu: str = u, rr: Dict[str, Any] = row_data) -> None:
            link = uu or listing_url_for_row(rr)
            if link:
                open_listing_url(link)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=10)
        row.grid_columnconfigure(2, weight=1)

        thumb = ctk.CTkLabel(row, text="…", width=84, height=84, fg_color="#1a120c", cursor="hand2")
        thumb.grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky="nw")
        _bind_thumb(thumb, row_data, thumb_key)

        try:
            rank_sc = float(
                row_data.get("rank_final_score")
                or row_data.get("display_rank_score")
                or display_rank_score(row_data)
            )
        except (TypeError, ValueError):
            rank_sc = display_rank_score(row_data)
        comp_sc = row_data.get("score_composite")
        if tier == "gem":
            sc_col = OK
        elif tier == "dinosaur":
            sc_col = DANGER
        elif tier == "dropship":
            sc_col = TEXT_MUTED
        else:
            sc_col = WARM_CREAM
        left = ctk.CTkFrame(row, fg_color="transparent", width=72)
        left.grid(row=0, column=1, padx=(0, 8), sticky="nw")
        rank_pos = row_data.get("rank_final")
        if rank_pos:
            ctk.CTkLabel(
                left,
                text=f"IA #{rank_pos}",
                font=F_CAP,
                text_color=BURNT_SIENNA,
            ).pack(anchor="w")
        ctk.CTkLabel(left, text=f"#{i}", font=ctk.CTkFont(family=FONT_DISPLAY[0], size=18), text_color=sc_col).pack(anchor="w")
        ctk.CTkLabel(
            left, text=f"{rank_sc:.0f}", font=ctk.CTkFont(family=FONT_DISPLAY[0], size=24), text_color=sc_col
        ).pack(anchor="w")
        if comp_sc is not None:
            try:
                ctk.CTkLabel(
                    left,
                    text=f"comp {float(comp_sc):.0f}",
                    font=F_CAP,
                    text_color=TEXT_MUTED,
                ).pack(anchor="w")
            except (TypeError, ValueError):
                pass
        if tier == "gem":
            ctk.CTkLabel(left, text="Pépite", font=F_CAP, text_color=OK).pack(anchor="w")
        try:
            mvp = float(row_data.get("model_vs_market_pct") or 0)
            if mvp >= 15.0 and not row_data.get("intent_mismatch"):
                ctk.CTkLabel(left, text="Bon plan", font=F_CAP, text_color=OK).pack(anchor="w")
        except (TypeError, ValueError):
            pass
        pick_rank = row_data.get("deal_pick_rank")
        if pick_rank:
            ctk.CTkLabel(left, text=f"Top {pick_rank}", font=F_CAP, text_color=BURNT_SIENNA).pack(anchor="w")
        if str(row_data.get("triage_decision") or "") == "doubtful":
            ctk.CTkLabel(left, text="Doute triage", font=F_CAP, text_color="#c9a227").pack(anchor="w")

        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.grid(row=0, column=2, sticky="ew")

        price_val = listing_price_eur(row_data)
        price_row = ctk.CTkFrame(mid, fg_color="transparent")
        price_row.pack(fill="x", anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            price_row,
            text=f"{price_val:.0f} €" if price_val is not None else "Prix —",
            font=ctk.CTkFont(family=FONT_DISPLAY[0], size=28, weight="bold"),
            text_color=BURNT_SIENNA if price_val else TEXT_MUTED,
            anchor="w",
        ).pack(side="left")
        list_p = row_data.get("list_price_eur")
        if (
            price_val is not None
            and list_p
            and abs(float(list_p) - float(price_val)) > 0.5
        ):
            ctk.CTkLabel(
                price_row,
                text=f"(affiche {float(list_p):.0f}€)",
                font=F_CAP,
                text_color=TEXT_MUTED,
            ).pack(side="left", padx=(6, 0))
        age_lbl = str(row_data.get("age_text") or row_data.get("listing_age_label") or "").strip()
        if age_lbl:
            try:
                age_h = float(row_data.get("age_hours") or 0)
                age_d = int(row_data.get("listing_age_days") or 999)
            except (TypeError, ValueError):
                age_h, age_d = 0.0, 999
            fresh_col = OK if (age_h and age_h <= 48) or age_d <= 2 else (OK if age_d <= 14 else TEXT_MUTED)
            ctk.CTkLabel(price_row, text=age_lbl, font=F_CAP, text_color=fresh_col).pack(
                side="left", padx=(8, 0)
            )
        plat_key = str(row_data.get("platform") or "vinted").lower()
        pst = platform_style(plat_key)
        plat_txt = pst.label
        ctk.CTkLabel(
            price_row,
            text=plat_txt,
            font=F_CAP,
            fg_color=pst.color,
            text_color=pst.text_color,
            corner_radius=6,
            height=22,
        ).pack(side="left", padx=(8, 0))
        cat_mkt = str(state.market_category or row_data.get("item_type") or "")
        spec_chip = ""
        spec_badges_list: List[str] = []
        try:
            from .scoring import compact_specs_line, extract_listing_specs, normalize_category, row_spec_badges

            mcat = normalize_category(cat_mkt)
            specs = row_data.get("extracted_specs") or extract_listing_specs(row_data, mcat)
            spec_chip = compact_specs_line(specs, mcat)
            spec_badges_list = row_spec_badges(row_data, mcat)
            if mcat in ("projector", "console_ps4", "console_ps5", "bicycle", "phone", "ac", "console_ds"):
                if spec_chip and spec_chip != "-":
                    ctk.CTkLabel(
                        price_row,
                        text=spec_chip[:72],
                        font=F_CAP,
                        text_color=OK,
                    ).pack(side="left", padx=(10, 0))
                conf_lbl = str(row_data.get("spec_confidence_label") or "")
                if conf_lbl:
                    conf_col = OK if conf_lbl == "élevée" else (WARM_CREAM if conf_lbl == "moyenne" else TEXT_MUTED)
                    ctk.CTkLabel(
                        price_row,
                        text=f"Specs {conf_lbl}",
                        font=F_CAP,
                        fg_color="#1a2a22" if conf_lbl == "élevée" else "#2a2218",
                        text_color=conf_col,
                        corner_radius=6,
                        height=22,
                    ).pack(side="left", padx=(6, 0))
        except Exception:
            pass
        disc = row_data.get("model_vs_market_pct") or row_data.get("discount_vs_new_pct") or row_data.get("market_discount_pct")
        if disc is not None:
            ctk.CTkLabel(
                price_row,
                text=f"−{disc}%",
                font=F_BODY,
                text_color=OK if float(disc or 0) >= 8 else TEXT_MUTED,
            ).pack(side="left", padx=(8, 0))

        mdl_lbl = str(row_data.get("product_model_label") or row_data.get("ai_model_exact") or "").strip()
        if mdl_lbl and mdl_lbl.lower() not in ("none", "inconnu", "n/a", "—"):
            ctk.CTkLabel(
                mid,
                text=mdl_lbl[:80],
                font=F_CAP,
                text_color=OK,
                anchor="w",
            ).pack(fill="x", anchor="w")
        title_lbl = ctk.CTkLabel(
            mid,
            text=title_txt,
            font=F_BODY,
            text_color=TEXT,
            anchor="w",
            wraplength=520,
            justify="left",
            cursor="hand2",
        )
        title_lbl.pack(fill="x", anchor="w")
        title_lbl.bind("<Button-1>", lambda _e, rr=row_data: pick_card(rr=rr))

        if spec_badges_list:
            badge_row = ctk.CTkFrame(mid, fg_color="transparent")
            badge_row.pack(fill="x", anchor="w", pady=(0, 4))
            for badge in spec_badges_list[:5]:
                bg_c, fg_c = _badge_chip_style(badge)
                ctk.CTkLabel(
                    badge_row,
                    text=badge,
                    font=F_CAP,
                    fg_color=bg_c,
                    text_color=fg_c,
                    corner_radius=6,
                    height=22,
                ).pack(side="left", padx=(0, 6))

        tips = deal_advice_lines(row_data)
        rank_reason = str(row_data.get("rank_reason") or "").strip()
        syn = (tips[0] if tips else "") or (rank_reason[:160] if rank_reason else "") or str(
            row_data.get("ai_specs_summary") or row_data.get("buyer_fit_notes") or ""
        )[:140]
        if syn:
            ctk.CTkLabel(
                mid,
                text=syn,
                font=F_CAP,
                text_color=WARM_CREAM,
                anchor="w",
                wraplength=560,
                justify="left",
            ).pack(fill="x", anchor="w", pady=(2, 4))

        bits: List[str] = []
        mdl = str(row_data.get("product_model_label") or row_data.get("ai_model_exact") or "").strip()
        if mdl and mdl != "None":
            bits.append(mdl)
        summ = str(row_data.get("spec_summary") or row_data.get("ai_bundle") or "").strip()
        if summ:
            bits.append(summ[:100])
        elif row_data.get("charger_status") == "with":
            bits.append("chargeur ✓")
        elif row_data.get("charger_status") == "without" or row_data.get("missing_charger"):
            bits.append("sans chargeur")
        try:
            from .engine import _stylus_relevant, row_has_stylus, row_text

            if _stylus_relevant(row_data, row_text(row_data)):
                if row_has_stylus(row_data) and not row_data.get("missing_stylus"):
                    bits.append("stylet ✓")
                elif row_data.get("missing_stylus"):
                    bits.append("sans stylet")
        except Exception:
            if row_data.get("has_stylus") and not row_data.get("missing_stylus"):
                bits.append("stylet ✓")
            elif row_data.get("missing_stylus"):
                bits.append("sans stylet")
        if row_data.get("includes_games"):
            bits.append("jeux")
        if row_data.get("storage_gb"):
            bits.append(f"{row_data.get('storage_gb')} Go")
        if row_data.get("battery_health_pct"):
            bits.append(f"🔋 {row_data.get('battery_health_pct')}%")
        elif row_data.get("_condition_hint"):
            bits.append(str(row_data.get("_condition_hint")))
        if str(row_data.get("deal_tier") or "") == "pepite" or tier == "gem":
            bits.append("💎 pépite")
        meta_txt = " · ".join(b for b in bits if b and b != "—")
        if meta_txt.strip():
            ctk.CTkLabel(mid, text=meta_txt, font=F_CAP, text_color=TEXT_MUTED, anchor="w", wraplength=560).pack(
                fill="x", anchor="w"
            )

        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=3, padx=(8, 0), sticky="ne")
        ctk.CTkButton(
            btns,
            text="Détail",
            width=72,
            height=28,
            corner_radius=RADIUS_GHOST,
            fg_color="transparent",
            border_width=1,
            border_color=BORDER_ACTIVE,
            text_color=TEXT,
            command=lambda rr=row_data: pick_card(rr=rr),
        ).pack(pady=(0, 6))
        ctk.CTkButton(
            btns,
            text=f"{listing_platform_label(row_data)} →",
            width=96,
            height=32,
            corner_radius=RADIUS_GHOST,
            fg_color=BURNT_SIENNA,
            text_color=WARM_CREAM,
            command=lambda uu=u, rr=row_data: open_card_url(uu=uu, rr=rr),
        ).pack(pady=(0, 4))

        def _dismiss_offer(rr: Dict[str, Any] = row_data) -> None:
            try:
                from .engine import record_feedback

                record_feedback(
                    str(rr.get("title") or ""),
                    action="dismiss",
                    category=str(state.market_category or rr.get("item_type") or ""),
                    reason=str(rr.get("triage_reasons") or "user_dismiss_offer"),
                )
            except Exception:
                pass
            rid = str(rr.get("id") or "")
            state.rows = [x for x in state.rows if str(x.get("id") or "") != rid]
            state.filtered = [x for x in state.filtered if str(x.get("id") or "") != rid]
            if state.enriched_cache:
                state.enriched_cache = [x for x in state.enriched_cache if str(x.get("id") or "") != rid]
            _apply_filters()

        ctk.CTkButton(
            btns,
            text="Écarter",
            width=96,
            height=26,
            corner_radius=RADIUS_GHOST,
            fg_color="transparent",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            command=_dismiss_offer,
        ).pack(pady=(0, 4))

        for w in (card, row, thumb, left, mid):
            w.bind("<Button-1>", lambda _e, rr=row_data: pick_card(rr=rr))
        card.bind("<Double-Button-1>", lambda _e, uu=u, rr=row_data: open_card_url(uu=uu, rr=rr))

    def _update_stats_and_count(rows: List[Dict[str, Any]]) -> None:
        if not rows:
            stats_var.set("0 annonce")
            filt_count_lbl.configure(text=f"0 / {len(state.rows)}")
            offers_count_lbl.configure(text="Liste des offres — 0 résultat")
            return
        deal_avg = sum(float(r.get("final_score") or 0) for r in rows) / len(rows)
        rank_avg = sum(display_rank_score(r) for r in rows) / len(rows)
        gems = _count_pepites(rows)
        stats_var.set(
            f"{len(rows)} offres · score moy. {deal_avg:.0f} · tri {rank_avg:.0f} · {gems} pépites"
        )
        state.run_summary["offers_visible"] = len(rows)
        state.run_summary["shown"] = len(rows)
        state.run_summary["filters_active"] = _count_active_filters()
        shown = min(state.cards_rendered, len(rows))
        header_stats.set(_format_header_stats(state.run_summary))
        plat_sel = results_platform_var.get() or "all"
        plat_note = ""
        if plat_sel != "all" and plat_sel != "top5_each":
            plat_note = f"{platform_style(plat_sel).label} · "
        elif plat_sel == "top5_each":
            plat_note = "Top 5 / plateforme · "
        try:
            pmin_s = str(state.price_min or "").strip()
            pmax_s = str(state.price_max or "").strip()
            if pmin_s or pmax_s:
                price_chip_lbl.configure(text=f"Scrape: {pmin_s or '—'}–{pmax_s or '—'} €")
            else:
                price_chip_lbl.configure(text="")
        except Exception:
            pass
        if state.view_mode == "gems_first" and gems == 0:
            filt_count_lbl.configure(
                text=f"{plat_note}{len(rows)} / {len(state.rows)} · liste {shown}/{len(rows)} · 0 pépite"
            )
        else:
            filt_count_lbl.configure(
                text=f"{plat_note}{len(rows)} / {len(state.rows)} · liste {shown}/{len(rows)} · {gems} pépites"
            )
        offers_count_lbl.configure(
            text=f"Offres — {shown} affichées sur {len(rows)} (filtres actifs)"
        )

    def _render_deals(rows: List[Dict[str, Any]], *, reset: bool = True) -> None:
        view_rows = list(rows)
        if reset:
            _render_gen["n"] += 1
            state.cards_rendered = 0
            for w in deals_list.winfo_children():
                w.destroy()
            load_more_btn.pack_forget()
        gen = _render_gen["n"]
        if not view_rows:
            ctk.CTkLabel(deals_list, text="Aucun résultat.", text_color=TEXT_MUTED).pack(pady=40)
            _update_stats_and_count(view_rows)
            _render_busy["on"] = False
            return
        _update_stats_and_count(view_rows)
        start_at = 0 if reset else state.cards_rendered
        if reset:
            target_end = min(len(view_rows), INITIAL_LIST_RENDER)
        else:
            target_end = min(start_at + LIST_PAGE_SIZE, len(view_rows))
        _render_busy["on"] = True

        def _batch(start: int) -> None:
            if gen != _render_gen["n"]:
                _render_busy["on"] = False
                return
            end = min(start + LIST_RENDER_BATCH, target_end)
            for j in range(start, end):
                try:
                    _render_one_card(j + 1, view_rows[j])
                except Exception as exc:
                    logger.exception("Carte offre #%s: %s", j + 1, exc)
                    _write_crash_log(exc, where=f"render_card_{j+1}")
            if end < target_end:
                root.after(12, lambda s=end: _batch(s))
            else:
                state.cards_rendered = target_end
                _render_busy["on"] = False
                _update_stats_and_count(view_rows)
                if state.cards_rendered < len(view_rows):
                    load_more_btn.configure(
                        text=f"Charger {min(LIST_PAGE_SIZE, len(view_rows) - state.cards_rendered)} suivantes "
                        f"({state.cards_rendered}/{len(view_rows)})"
                    )
                    load_more_btn.pack(fill="x", padx=8, pady=6)
                else:
                    load_more_btn.pack_forget()
                if view_rows and state.selected is None:
                    state.selected = view_rows[0]
                _refresh_deals_scroll()
                try:
                    deals_list.update_idletasks()
                except Exception:
                    pass

        _batch(start_at)

    def _load_more_deals() -> None:
        if _render_busy["on"] or not state.filtered:
            return
        _render_deals(state.filtered, reset=False)

    load_more_btn.configure(command=_load_more_deals)

    def _on_filters_done(rows: List[Dict[str, Any]]) -> None:
        state.filtered = rows
        _refresh_filter_styles()
        _render_deals(rows, reset=True)
        if rows and (not state.selected or state.selected not in rows):
            state.selected = rows[0]

    def _apply_filters() -> None:
        if _filter_busy["on"] or _filter_guard["build"]:
            return
        _filter_busy["on"] = True
        _show_list_busy(True)

        def _work() -> None:
            try:
                rows = _compute_filtered_rows()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Filtres UI: %s", exc)
                rows = []

            def _done() -> None:
                _filter_busy["on"] = False
                _show_list_busy(False)
                _on_filters_done(rows)
                _log_ui(f"Filtres appliqués — {len(rows)} annonces visibles")

            root.after(0, _done)

        threading.Thread(target=_work, daemon=True, name="vf-filter").start()

    def _finish_run(out: VfOutput) -> None:
        # Robustness: if some run artifacts are written to disk but missing from
        # the in-memory `VfOutput` (or lost due to partial failures), reload the
        # essentials so tabs don't appear empty.
        def _read_csv_rows(path: Path) -> List[Dict[str, Any]]:
            try:
                import csv

                with path.open("r", encoding="utf-8-sig", newline="") as fh:
                    return [dict(r) for r in csv.DictReader(fh)]
            except Exception:
                return []

        def _maybe_int(x: Any) -> Any:
            s = str(x or "").strip()
            if not s:
                return x
            if s.isdigit():
                try:
                    return int(s)
                except Exception:
                    return x
            return x

        state.output = out
        state.platform_analytics = getattr(out, "platform_analytics", None)
        state.catalog_items = list(out.items or [])
        state.rejected_rows = list(getattr(out, "rejected_rows", None) or [])
        state.doubtful_rows = list(getattr(out, "doubtful_rows", None) or [])
        state.top_picks = list(getattr(out, "top_picks", None) or [])
        state.top_picks_note = str(getattr(out, "top_picks_note", "") or "").strip()
        state.top5_analysis = getattr(out, "top5_analysis", None)
        state.top5_paths = dict(getattr(out, "top5_paths", None) or {})
        state.eval_export_paths = dict(getattr(out, "eval_export_paths", None) or {})
        state.rows = list(out.rows or [])

        # Disk fallbacks (run folder contract in `vf/pipeline.py`).
        folder = Path(str(getattr(out, "folder", "") or "").strip()) if getattr(out, "folder", "") else None
        if folder and folder.is_dir():
            if not state.rows:
                full_json = folder / "results_full.json"
                results_csv = folder / "results.csv"
                if full_json.is_file():
                    try:
                        import json

                        payload = json.loads(full_json.read_text(encoding="utf-8"))
                        rows_disk = payload.get("rows") if isinstance(payload, dict) else None
                        if isinstance(rows_disk, list) and rows_disk:
                            state.rows = rows_disk
                    except Exception:
                        pass
                if not state.rows and results_csv.is_file():
                    state.rows = _read_csv_rows(results_csv)
            if not state.top_picks:
                deals_csv = folder / "deals_top.csv"
                if deals_csv.is_file():
                    state.top_picks = _read_csv_rows(deals_csv)[:10]
            if not state.rejected_rows:
                rej_csv = folder / "rejected.csv"
                if rej_csv.is_file():
                    state.rejected_rows = _read_csv_rows(rej_csv)
                    for r in state.rejected_rows:
                        r["id"] = _maybe_int(r.get("id"))
            if not state.top5_analysis:
                t5 = folder / "top5_analysis.json"
                if t5.is_file():
                    try:
                        import json

                        state.top5_analysis = json.loads(t5.read_text(encoding="utf-8"))
                        state.top5_paths = dict(state.top5_paths or {})
                        state.top5_paths.setdefault("top5_json", str(t5.resolve()))
                        md = folder / "top5_analysis.md"
                        if md.is_file():
                            state.top5_paths.setdefault("top5_md", str(md.resolve()))
                    except Exception:
                        pass

        # If doubtful rows weren't returned, rebuild them from `rows`.
        if not state.doubtful_rows and state.rows:
            rebuilt: List[Dict[str, Any]] = []
            for r in state.rows:
                if str(r.get("triage_decision") or "") == "doubtful":
                    dr = dict(r)
                    if not dr.get("reject_stage"):
                        dr["reject_stage"] = "doute"
                    if not dr.get("reject_reason"):
                        dr["reject_reason"] = str(dr.get("triage_reasons") or "doute")
                    rebuilt.append(dr)
            state.doubtful_rows = rebuilt

        for row in state.rows:
            enrich_row_listing_fields(row)
            if not row.get("_pack_reconciled"):
                enrich_pack_fields(row)
        for row in state.rejected_rows:
            enrich_row_listing_fields(row)
        for row in state.doubtful_rows:
            enrich_row_listing_fields(row)
        if state.rows and not any(r.get("display_rank_score") is not None for r in state.rows[:3]):
            normalize_run_rank_scores(state.rows, main_only=True)
        state.rows.sort(
            key=lambda r: float(
                r.get("best_deal_score")
                or r.get("rank_score")
                or r.get("display_rank_score")
                or r.get("final_score")
                or 0
            ),
            reverse=True,
        )
        n_acc = sum(1 for r in state.rows if r.get("is_accessory"))
        top = state.rows[0] if state.rows else {}
        main_rows = [
            r
            for r in state.rows
            if not r.get("is_accessory") and str(r.get("triage_decision") or "") != "doubtful"
        ]
        n_main = len(main_rows) or len(state.rows)
        n_cat = int(getattr(out, "scraped_count", None) or len(out.items))
        n_rej = len(state.rejected_rows)
        n_dou = len(state.doubtful_rows)
        after_triage = int(getattr(out, "after_triage_count", None) or len(out.rows) or 0)
        state.run_summary = {
            "scraped": n_cat,
            "catalog_count": n_cat,
            "after_triage": after_triage,
            "analyzed": n_main,
            "rejected": n_rej,
            "doubts": n_dou,
            "accessories": n_acc,
            "top_score": f"{display_rank_score(top):.1f}" if top else "—",
            "shown": 0,
            "offers_visible": 0,
            "filters_active": 0,
        }
        try:
            from .export import build_analysis_markdown

            state.analysis_markdown = build_analysis_markdown(
                category=str(out.market_category or state.market_category or ""),
                query=state.intent,
                brain=getattr(out, "category_brain", None),
                rows=state.rows,
                platform_analytics=state.platform_analytics,
                triage_stats={
                    "scraped": n_cat,
                    "after_triage": after_triage,
                    "rejected": n_rej,
                    "doubts": n_dou,
                },
            )
        except Exception:
            state.analysis_markdown = ""
        display_rows = list(main_rows if main_rows else state.rows)
        dynamic_filters = list(out.dynamic_filters or [])
        filter_note = str(getattr(out, "filter_agent_note", "") or "").strip()
        rows_for_cache = [dict(r) for r in state.rows]

        # Afficher l’onglet Offres tout de suite (évite freeze « Analyse… » en haut).
        _set_phase("results")
        try:
            tabs.set("Offres")
            n_rej = len(state.rejected_rows)
            n_dou = len(state.doubtful_rows)
            tabs.tab("Rejetés").configure(text=f"Rejetés ({n_rej})" if n_rej else "Rejetés")
            tabs.tab("Doutes").configure(text=f"Doutes ({n_dou})" if n_dou else "Doutes")
            _set_filters_collapsed(True)
        except Exception:
            pass
        state.view_mode = "all"
        state.active_preset_id = "preset_reset"
        mkt = str(out.market_category or state.market_category or "")
        state.pre_filter_defaults = dict(getattr(out, "pre_filter_defaults", None) or {})
        try:
            from .market import refinements_to_pre_filter_defaults

            state.pre_filter_defaults.update(
                refinements_to_pre_filter_defaults(state.idle_refinements, mkt)
            )
        except Exception:
            pass
        if not state.pre_filter_defaults:
            state.pre_filter_defaults = {
                "quality_tier": "all",
                "risk_mode": "all",
                "hide_accessories": "tout",
                "score_band": "all",
                "livraison_uniquement": "on",
            }
            if mkt.startswith("console_ps"):
                state.pre_filter_defaults["listing_type"] = "console"
                state.pre_filter_defaults["has_controller"] = "on"
        results_quality_var.set(_QUALITY_LABELS["all"])
        results_platform_var.set("all")
        state.filtered = list(display_rows)
        state.enriched_cache = []
        state.cards_rendered = 0
        for w in deals_list.winfo_children():
            w.destroy()
        list_busy_lbl.configure(text="Préparation des résultats…")
        _show_list_busy(True)

        def _bg_prepare() -> Tuple[List[Dict[str, Any]], str]:
            from .engine import enrich_listing_taxonomy

            cache: List[Dict[str, Any]] = []
            for r in rows_for_cache:
                row = dict(r)
                if not row.get("_pack_reconciled"):
                    enrich_pack_fields(row)
                try:
                    enrich_listing_taxonomy(row)
                except Exception:
                    pass
                cache.append(row)
            mkt = str(out.market_category or "generic")
            try:
                run_url_resolved, _ = resolve_vinted_catalog_url(state.intent)
                mctx = detect_market_context(run_url_resolved or state.intent, buyer_priorities="")
                mkt = (
                    str(out.market_category or "")
                    or str(mctx.get("category") or "")
                    or resolve_effective_category(main_rows, run_url_resolved or state.intent, buyer_priorities="")
                    or mkt
                )
            except Exception:
                pass
            return cache, mkt

        def _apply_prepared(cache: List[Dict[str, Any]], mkt: str) -> None:
            _filter_guard["build"] = True
            state.enriched_cache = cache
            state.market_category = mkt
            try:
                doubt_subtitle_var.set(_doubt_subtitle_for_category(mkt))
            except Exception:
                pass
            try:
                filter_agent_lbl.configure(
                    text=f"🎯 Marché {mkt}" + (f" — {filter_note}" if filter_note else "")
                )
                market_badge_lbl.configure(text=f"Marché · {mkt}")
            except Exception:
                pass
            try:
                _build_filters_ui(dynamic_filters)
                _reset_filters_to_show_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Filtres UI: %s", exc)
                _log_ui(f"⚠ Filtres UI simplifiés : {exc}")
            try:
                _rebuild_platform_chips()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Plateformes filtres: %s", exc)
            try:
                state.filtered = _compute_filtered_rows()
                if not state.filtered:
                    _reset_filters_to_show_all()
                    state.filtered = _compute_filtered_rows()
            except Exception:
                state.filtered = list(display_rows)
            if not state.filtered:
                state.filtered = list(display_rows)
            _filter_guard["build"] = False
            state.run_summary["offers_visible"] = len(state.filtered)
            state.run_summary["shown"] = len(state.filtered)
            state.run_summary["filters_active"] = _count_active_filters()
            _show_list_busy(False)
            header_stats.set(_format_header_stats(state.run_summary))
            try:
                _render_deals(state.filtered, reset=True)
                if state.filtered:
                    state.selected = state.filtered[0]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Re-render deals: %s", exc)
            _render_inline_doubts()
            _render_analysis_tab()
            _render_top5_ia(state.top5_analysis)
            _render_top_picks(state.top_picks, state.top_picks_note)
            _render_platform_analytics(state.platform_analytics)
            root.after(50, _render_rejected_list)
            root.after(120, _render_doubtful_list)
            if state.top_picks_note:
                summary_run.configure(text=_truncate_at_word(state.top_picks_note, 520))
            elif state.top_picks:
                tp = ", ".join(
                    f"#{p.get('deal_pick_rank')} {p.get('total_price') or p.get('price')}€"
                    for p in state.top_picks[:5]
                )
                summary_run.configure(text=f"Top affaires : {tp}")
            dur = float(getattr(out, "run_duration_s", 0) or 0)
            plat_bits = []
            if out.scrape_stats:
                for pk, st in sorted(out.scrape_stats.items()):
                    plat_bits.append(f"{pk} {int(st.get('unique') or st.get('raw') or 0)}")
            best = state.top_picks[0] if state.top_picks else {}
            best_pr = best.get("total_price") or best.get("price")
            summary_lines = [
                f"Résumé — {n_cat} scrapées → {after_triage} retenues → {len(state.filtered)} offres · {dur:.0f}s",
                f"Archives : {n_rej} rejetées · {n_dou} doutes",
                f"Plateformes : {', '.join(plat_bits) or '—'}",
            ]
            if best_pr:
                summary_lines.append(
                    f"Meilleure affaire : {best_pr}€ — {str(best.get('title') or '')[:60]}"
                )
            if out.eval_export_paths.get("eval_summary"):
                summary_lines.append(f"Éval IA : {out.eval_export_paths['eval_summary']}")
            run_summary_card_lbl.configure(text="\n".join(summary_lines))
            if not run_summary_card.winfo_ismapped():
                run_summary_card.pack(fill="x", pady=(0, 10), padx=2, before=results_ctrl)
            _log_ui(
                f"✓ Terminé — {n_cat} scrapées → {after_triage} retenues → "
                f"{len(state.filtered)} offres ({n_rej} rejetées, {n_dou} doutes)"
            )

        def _bg_thread() -> None:
            try:
                result = _bg_prepare()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Préparation résultats: %s", exc)
                result = (rows_for_cache, str(out.market_category or "generic"))

            def _ui() -> None:
                cache, mkt = result
                _apply_prepared(cache, mkt)

            root.after(0, _ui)

        threading.Thread(target=_bg_thread, daemon=True, name="vf-finish-prep").start()

    def _infer_market_from_prio() -> None:
        from .ai import api_key, infer_market_from_priorities

        if not api_key():
            _log_ui("⚠ IA hors ligne — critères par défaut selon l’URL.")
            return
        prio = _build_combined_priorities()
        state.market_detect_gen += 1
        gen = state.market_detect_gen
        _log_ui("IA : analyse de vos priorités et du marché…")

        def _work() -> None:
            info = infer_market_from_priorities(state.intent, prio)

            def _done() -> None:
                if gen != state.market_detect_gen:
                    return
                prio = _build_combined_priorities()
                ctx = detect_market_context(state.intent, buyer_priorities=prio)
                url_locked = bool(ctx.get("url_locked"))
                cat = str(ctx.get("category") or "generic")
                if not url_locked:
                    ia_cat = str(info.get("category") or "")
                    if ia_cat and ia_cat != "generic":
                        cat = ia_cat
                        ctx["category"] = cat
                lbl = str(info.get("label") or cat)
                note_ia = "conseils IA (marché = URL)" if url_locked else "affiné par IA"
                ctx["source"] = (ctx.get("source") or "") + f" · {note_ia}"
                if url_locked and prio and re.search(
                    r"(?i)\b(projecteur|projector|lumens|hdmi|1080)\b", prio
                ):
                    ctx["source"] += " · priorités projecteur ignorées"
                crit = str(info.get("criteria") or "").strip()
                qs = info.get("questions") if isinstance(info.get("questions"), list) else []
                ia_lines: List[str] = []
                if crit:
                    ia_lines.append(crit)
                if qs:
                    ia_lines.append("❓ " + "\n❓ ".join(str(q) for q in qs[:2]))
                _apply_market_context(ctx, gen=gen, ia_extra="\n\n".join(ia_lines))
                _log_ui(f"✓ Marché : {cat}" + (" (verrouillé par URL)" if url_locked else ""))

            root.after(0, _done)

        threading.Thread(target=_work, daemon=True, name="vf-infer-market").start()

    def _open_criteria_plan() -> None:
        """Open the discovered criteria plan for the last run (if any)."""
        if not (state.output and state.output.folder):
            _infer_market_from_prio()
            return
        folder = Path(state.output.folder)
        target = folder / "criteria_plan.json"
        if target.is_file():
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
            except Exception:
                try:
                    webbrowser.open(target.as_uri())
                except Exception:
                    pass
            _log_ui(f"✓ Critères marché : {target.name}")
            return
        # Fallback (older run / not generated)
        _infer_market_from_prio()

    def _refresh_buyer_priorities(ctx: Dict[str, Any], url: str) -> None:
        """Charge les priorités de CETTE recherche uniquement ; vide si URL/marché différent."""
        from .market import unified_market_specs

        key = market_priority_key(url, ctx)
        if key == state.prio_context_key:
            return
        state.prio_context_key = key
        cat = str(ctx.get("category") or "generic")
        st = str(ctx.get("search_text") or "")
        q = url or st
        saved = get_market_priority(key)
        buyer_extra_box.delete("0.0", "end")
        hint = market_priority_hint(cat, st)
        if saved:
            setup_prio_hint_lbl.configure(text=hint + " — affinage mémorisé pour cette recherche")
        else:
            setup_prio_hint_lbl.configure(text=hint)
        specs = unified_market_specs(cat, q, state.idle_refinements)
        saved_active, extra = _match_saved_to_specs(saved, specs) if saved else ({}, "")
        if extra:
            buyer_extra_box.insert("0.0", extra)
        _rebuild_unified_specs(cat, q, saved_active=saved_active or None)

    def _apply_market_context(
        ctx: Dict[str, Any],
        *,
        gen: int = 0,
        ia_extra: str = "",
    ) -> None:
        if gen and gen != state.market_detect_gen:
            return
        cat = str(ctx.get("category") or "generic")
        state.market_category = cat
        src = str(ctx.get("source") or "")
        st = str(ctx.get("search_text") or "")
        url = state.intent.strip() or state.last_market_url
        _refresh_buyer_priorities(ctx, url)
        lines = [category_buyer_criteria(cat)]
        extra = (ia_extra or "").strip()
        if extra and not (
            ctx.get("url_locked")
            and re.search(r"(?i)\b(projecteur|projector|lumens|hdmi|1080)\b", extra)
            and cat != "projector"
        ):
            lines.append(extra)
        if src or st:
            lines.append("\n📍 " + src)
            if st:
                lines.append(f"search_text API : « {st} »")
        setup_market_lbl.configure(text=f"Marché : {cat}")
        setup_crit_lbl.configure(text="\n".join(lines))
        try:
            from .market import estimate_run_cost, refinements_to_buyer_notes, suggest_price_bounds

            user_pmin = (state.idle_refinements.get("price_min") or "").strip()
            user_pmax = (state.idle_refinements.get("price_max") or "").strip()
            if not user_pmin and not user_pmax:
                pmin, pmax = suggest_price_bounds(cat, st or url)
                if pmin:
                    state.idle_refinements["price_min"] = str(pmin)
                    state.price_min = str(pmin)
                if pmax:
                    state.idle_refinements["price_max"] = str(pmax)
                    state.price_max = str(pmax)
            else:
                pmin = int(user_pmin) if user_pmin.isdigit() else None
                pmax = int(user_pmax) if user_pmax.isdigit() else None
            plats = _selected_platforms() or _idle_platforms()
            est = estimate_run_cost(plats, _depth_code())
            price_note = refinements_to_buyer_notes(state.idle_refinements) if state.idle_refinements else ""
            setup_run_preview_lbl.configure(
                text=f"{est.get('summary', '')}"
                + (f" · {price_note}" if price_note else "")
            )
        except Exception:
            pass
        try:
            market_badge_lbl.configure(text=f"Marché {cat}")
        except Exception:
            pass
        root.after(40, lambda c=cat: _rebuild_weight_sliders(c, host=setup_slider_host))

    def _normalize_run_url(raw: str) -> str:
        resolved, note = resolve_vinted_catalog_url(raw)
        if note and resolved != raw:
            _log_ui(f"✓ {note}")
        if resolved:
            return resolved
        return raw

    def _apply_idle_refinements_to_preflight(category: str) -> None:
        _sync_preflight_from_refinements(category, state.intent or "")
        _rebuild_unified_specs(category, state.intent or "")

    def _go_setup() -> None:
        from .market import build_refined_query, interactive_search_preview, refinements_to_buyer_notes

        raw = idle_search_var.get().strip()
        if not raw:
            return
        if not _idle_platforms():
            _log_ui("⚠ Sélectionnez au moins une plateforme.")
            return
        preview = interactive_search_preview(raw, state.idle_refinements)
        cat = str(preview.get("category") or "")
        if not raw.startswith(("http://", "https://")):
            raw = build_refined_query(raw, state.idle_refinements, category=cat) or raw
        raw = _normalize_run_url(raw)
        state.market_detect_gen += 1
        gen = state.market_detect_gen
        state.intent = raw
        state.last_market_url = raw
        state.market_category = cat
        ref_note = refinements_to_buyer_notes(state.idle_refinements)
        if ref_note:
            state.pre_filter_defaults["idle_refine"] = ref_note
        pmin = (state.idle_refinements.get("price_min") or "").strip()
        pmax = (state.idle_refinements.get("price_max") or "").strip()
        if pmin:
            state.price_min = pmin
        if pmax:
            state.price_max = pmax
        state.ai_depth = _depth_code()
        idle_search_var.set(raw)
        _reset_setup_ui()
        setup_market_lbl.configure(text="Marché : analyse…")
        setup_url_lbl.configure(text=f"Recherche : {raw[:120]}{'…' if len(raw) > 120 else ''}")
        setup_brief_lbl.configure(text="Brief IA en cours…")
        deal_mode_var.set(state.deal_hunt_mode or "balanced")
        _set_phase("setup")
        try:
            root.update_idletasks()
        except Exception:
            pass

        def _setup_work() -> None:
            try:
                ctx = detect_market_context(raw, buyer_priorities="")
            except Exception:
                ctx = {"category": cat or "generic", "search_text": raw}

            def _setup_done() -> None:
                if gen != state.market_detect_gen:
                    return
                _apply_market_context(ctx, gen=gen)
                _apply_idle_refinements_to_preflight(state.market_category or cat)
                _on_deal_mode()
                _load_search_brief(gen)

            root.after(0, _setup_done)

        threading.Thread(target=_setup_work, daemon=True, name="vf-setup").start()

    def _selected_platforms() -> List[str]:
        if plat_auto_var.get():
            from .market import resolve_run_platforms

            raw = state.intent.strip() or idle_search_var.get().strip()
            cat = state.market_category or state.last_preview_category or ""
            plats, _ = resolve_run_platforms(raw, cat, auto=True)
            return plats
        return _idle_platforms()

    def _launch_from_setup() -> None:
        from .market import apply_depth_profile, specs_to_preflight_map, unified_market_specs

        state.ai_depth = _depth_code()
        apply_depth_profile(state.ai_depth)
        state.scrape_depth = "pro"
        try:
            import os as _os_scrape
            from vf_scrape.config import apply_scrape_profile

            _os_scrape.environ["VF_GUI_MODE"] = "1"
            apply_scrape_profile("pro")
        except Exception:
            pass
        ctx = detect_market_context(state.intent, buyer_priorities="")
        key = market_priority_key(state.intent, ctx)
        prio_txt = _build_combined_priorities()
        specs = unified_market_specs(
            state.market_category or "",
            state.intent or "",
            state.idle_refinements,
        )
        pf_map = specs_to_preflight_map(state.preflight_active, specs)
        set_market_priority(key, prio_txt)
        w = _collect_weights()
        w.buyer_priorities = prio_txt
        save_weights(w)
        set_weights(w)
        try:
            import json as _json
            import os as _os

            _os.environ["VF_PREFLIGHT"] = _json.dumps(pf_map)
            pmin_s = (state.idle_refinements.get("price_min") or state.price_min or "").strip()
            pmax_s = (state.idle_refinements.get("price_max") or state.price_max or "").strip()
            state.price_min, state.price_max = pmin_s, pmax_s
            if pmin_s:
                _os.environ["VF_PRICE_MIN"] = pmin_s
            else:
                _os.environ.pop("VF_PRICE_MIN", None)
            if pmax_s:
                _os.environ["VF_PRICE_MAX"] = pmax_s
            else:
                _os.environ.pop("VF_PRICE_MAX", None)
        except Exception:
            pass
        mkt = str(state.market_category or ctx.get("category") or "")
        from .market import refinements_to_pre_filter_defaults

        state.pre_filter_defaults = {
            "quality_tier": "all",
            "risk_mode": "all",
            "hide_accessories": "tout",
            "score_band": "all",
            "livraison_uniquement": "on",
        }
        state.pre_filter_defaults.update(refinements_to_pre_filter_defaults(state.idle_refinements, mkt))
        if mkt.startswith("console_ps"):
            state.pre_filter_defaults["listing_type"] = "console"
        state.view_mode = "all"
        state.active_preset_id = "preset_reset"
        _run_analysis()

    def _run_analysis() -> None:
        if state.running:
            return
        raw = state.intent.strip() or idle_search_var.get().strip()
        if not raw:
            return
        if not _selected_platforms():
            _log_ui("⚠ Cochez au moins une plateforme (Vinted, LeBonCoin ou Wallapop).")
            return
        raw = _normalize_run_url(raw)
        state.intent = raw
        ctx_pre = detect_market_context(raw, buyer_priorities="")
        if plat_vinted_var.get() and raw.startswith(("http://", "https://")) and "vinted" in raw.lower():
            if not str(ctx_pre.get("search_text") or "").strip():
                _log_ui(
                    "⚠ URL Vinted sans search_text — collez le lien complet "
                    "(catalog?search_text=…), pas session-refresh."
                )
        search_var.set("")
        rej_search_var.set("")
        doubt_search_var.set("")
        results_platform_var.set("all")
        results_quality_var.set(_QUALITY_LABELS["all"])
        _render_gen["n"] += 1
        state.cards_rendered = 0
        aid = _filter_after.get("id")
        if aid is not None:
            try:
                root.after_cancel(aid)
            except Exception:
                pass
        _filter_after["id"] = None
        state.running = True
        _set_phase("running")
        progress.set(0)
        _update_steps(1)
        plats_run = _selected_platforms()
        _init_plat_prog_labels(plats_run)
        human_lbl.configure(text="Connexion aux plateformes…")
        eta_lbl.configure(text="")
        summary_run.configure(text="")
        _clear_run_logs()
        _log_ui("═══ Nouvelle analyse ═══")
        _log_ui(f"URL : {raw[:120]}{'…' if len(raw) > 120 else ''}")
        from .engine import hybrid_mode_label

        nav_lbl = hybrid_mode_label() if headless else "visible (debug --headed)"
        _log_ui(f"IA : {'oui' if use_ai_var.get() else 'non'} · navigateur : {nav_lbl}")
        _log_ui(f"Catalogue : profil complet · analyse IA optimisée")
        _log_ui(f"Plateformes : {', '.join(plats_run) or 'aucune'}")
        if str(ctx_pre.get("search_text") or "").strip():
            _log_ui(f"Recherche : « {str(ctx_pre.get('search_text'))[:80]} »")
        if state.market_category:
            _log_ui(f"Marché configuré : {state.market_category}")
        if state.intent and state.intent != raw:
            _log_ui(f"Remplacement recherche : {state.intent[:50]}… → {raw[:50]}…")
        run_clock["progress_ts"] = time.monotonic()
        _start_run_clock()

        def on_progress(p: Dict[str, Any]) -> None:
            def _do() -> None:
                prev = run_clock.get("progress") or {}
                if int(p.get("step") or 0) > int(prev.get("step") or 0):
                    run_clock["smooth_pct"] = 0.0
                run_clock["progress"] = dict(p)
                run_clock["progress_ts"] = time.monotonic()
                step = int(p.get("step") or 1)
                state.current_step = step
                human = str(p.get("human") or p.get("step_name") or "")
                human_lbl.configure(text=human)
                _update_steps(step)
                bits = []
                if "catalog_count" in p:
                    bits.append(f"{p['catalog_count']} scrapées")
                if "ai_before" in p:
                    bits.append(f"IA {p['ai_before']}→{p.get('ai_after')}")
                if "main_items" in p:
                    bits.append(f"{p['main_items']} produits")
                if "excluded_accessories" in p:
                    bits.append(f"{p['excluded_accessories']} accessoires")
                if "deep_done" in p:
                    bits.append(f"descriptions {p['deep_done']}/{p.get('deep_total', '?')}")
                if bits:
                    summary_run.configure(text=" · ".join(bits))
                pp = p.get("platform_progress")
                if isinstance(pp, dict):
                    _update_plat_prog(
                        pp,
                        str(p.get("platform_active") or ""),
                        p.get("platform_elapsed") if isinstance(p.get("platform_elapsed"), dict) else None,
                    )
                _log_progress_event(p)

            root.after(0, _do)

        def worker() -> None:
            out: Optional[VfOutput] = None
            try:
                def _parse_price(s: str) -> Optional[int]:
                    try:
                        v = int(float((s or "").strip().replace(",", ".")))
                        return v if v > 0 else None
                    except (TypeError, ValueError):
                        return None

                out = run_url(
                    raw,
                    name=raw[:60],
                    headless=headless,
                    use_ai=bool(use_ai_var.get()),
                    search_intent=str(ctx_pre.get("search_text") or raw),
                    market_category_hint=state.market_category or str(ctx_pre.get("category") or ""),
                    log_fn=_log_ui,
                    progress_fn=on_progress,
                    platforms=plats_run if not plat_auto_var.get() else None,
                    price_min=_parse_price(state.price_min or setup_price_min_var.get()),
                    price_max=_parse_price(state.price_max or setup_price_max_var.get()),
                    auto_platforms=bool(plat_auto_var.get()),
                )
            except BaseException as exc:  # noqa: BLE001
                out = VfOutput(errors=[str(exc)])
                logger.exception("run failed")
                _write_crash_log(exc, where="worker")
                _log_ui(f"✕ Erreur analyse : {exc}\n  → journal : {_CRASH_LOG.resolve()}")

            def done() -> None:
                state.running = False
                _stop_run_clock()
                try:
                    run_log_live.configure(text="● Terminé", text_color=TEXT_MUTED)
                except Exception:
                    pass
                result = out or VfOutput(errors=["Analyse interrompue (aucun résultat)."])
                has_rows = bool(result.rows)
                try:
                    if result.errors and not has_rows:
                        err = result.errors[0]
                        human_lbl.configure(text=f"Erreur : {err}")
                        timer_meta.configure(text="Interrompu")
                        eta_lbl.configure(text="")
                        progress.set(0)
                        _log_ui(f"✕ {err}")
                        try:
                            tabs.set("Journal")
                        except Exception:
                            pass
                        _set_phase("idle")
                        return
                    total = time.monotonic() - float(run_clock.get("t0") or time.monotonic())
                    progress.set(1.0)
                    progress_pct_lbl.configure(text="100 %")
                    _update_steps(6, done=True)
                    human_lbl.configure(text="Analyse terminée — ouverture des offres…")
                    timer_lbl.configure(text=format_clock(total))
                    timer_meta.configure(text="Analyse terminée")
                    eta_lbl.configure(text=f"Durée totale · {format_clock(total)}")
                    for w in (result.warnings or []):
                        _log_ui(f"⚠ {w}")
                    if result.errors and has_rows:
                        _log_ui(f"⚠ {result.errors[0]} (résultats affichés quand même)")
                    _finish_run(result)
                    try:
                        root.update_idletasks()
                    except Exception:
                        pass
                except Exception as exc:  # noqa: BLE001
                    _write_crash_log(exc, where="done")
                    logger.exception("finish run failed")
                    if has_rows:
                        _log_ui(f"⚠ Erreur interface partielle : {exc} — tentative affichage minimal")
                        try:
                            state.rows = list(result.rows or [])
                            state.filtered = list(state.rows or [])
                            _set_phase("results")
                            tabs.set("Offres")
                            _render_deals(state.filtered, reset=True)
                        except Exception:
                            _set_phase("idle")
                    else:
                        human_lbl.configure(text=f"Erreur affichage : {exc}")
                        _log_ui(f"✕ Crash interface : {exc}\n  → {_CRASH_LOG.resolve()}")
                        _set_phase("idle")

            root.after(0, done)

        threading.Thread(target=worker, daemon=True, name="vf-run").start()

    def _rescore_fast() -> None:
        if not state.rows:
            return
        w = _collect_weights()
        save_weights(w)
        set_weights(w)
        _log_ui("Recalcul des scores…")
        rescore_rows_inplace(state.rows, weights=w, renormalize=True)
        _apply_filters()
        _log_ui("✓ Classement mis à jour")

    def _rescore() -> None:
        if not state.catalog_items:
            _rescore_fast()
            return
        w = _collect_weights()
        save_weights(w)
        set_weights(w)
        _log_ui("Recalcul complet (catalogue)…")
        prior = {str(r.get("id") or ""): r for r in state.rows if r.get("id") is not None}
        for k in ("score_base", "score_overlay", "_pack_reconciled", "ai_vision_note", "description"):
            for row in state.rows:
                rid = str(row.get("id") or "")
                if rid and prior.get(rid, {}).get(k) is not None:
                    prior[rid][k] = row.get(k)
        state.rows = rescore_catalog(
            state.catalog_items,
            intent=state.intent,
            weights=w,
            prior_rows=prior,
        )
        _apply_filters()
        _log_ui("✓ Classement mis à jour")

    def _open_export() -> None:
        if state.output and state.output.folder:
            folder = Path(state.output.folder)
            try:
                webbrowser.open(folder.as_uri())
            except Exception:
                pass
            for name in ("results_unified.csv", "catalog_unified.csv", "results.csv"):
                p = folder / name
                if p.is_file():
                    _log_ui(f"✓ Export : {p}")
                    break

    def _open_eval_export() -> None:
        paths = (state.output.eval_export_paths if state.output else {}) or {}
        p = paths.get("eval_summary") or paths.get("eval_summary_bench")
        if not p and state.output and state.output.folder:
            cand = Path(state.output.folder) / "eval" / "eval_summary.json"
            if cand.is_file():
                p = str(cand)
        if not p:
            _log_ui("⚠ Aucun export éval — lancez une analyse d'abord")
            return
        fp = Path(p)
        if fp.is_file():
            try:
                webbrowser.open(fp.as_uri())
            except Exception:
                pass
            _log_ui(f"✓ Éval IA : {fp}")
        else:
            _log_ui(f"⚠ Fichier introuvable : {fp}")

    def _export_csv_dialog() -> None:
        if not (state.output and state.output.folder):
            _log_ui("⚠ Aucun export — lancez une analyse d'abord")
            return
        folder = Path(state.output.folder)
        target = folder / "results_enriched.xlsx"
        if not target.is_file():
            target = folder / "results_unified.csv"
        if not target.is_file():
            target = folder / "results.csv"
        if target.is_file():
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
            except Exception:
                webbrowser.open(target.as_uri())
            _log_ui(f"✓ Export ouvert : {target.name}")
        else:
            _open_export()

    def _new_run() -> None:
        """Retour accueil — conserve la dernière URL pour relancer."""
        state.market_detect_gen += 1
        _reset_setup_ui()
        prev = state.intent.strip() or idle_search_var.get().strip()
        state.running = False
        _stop_run_clock()
        _set_phase("idle")
        if prev:
            idle_search_var.set(prev)
        try:
            search_entry.focus_set()
        except Exception:
            pass

    ctk.CTkButton(
        setup_btn_row,
        text="← Retour",
        height=44,
        corner_radius=RADIUS_GHOST,
        fg_color="transparent",
        border_width=1,
        border_color=BORDER_ACTIVE,
        text_color=TEXT,
        command=lambda: _set_phase("idle"),
    ).pack(side="left", padx=(0, 8))

    ctk.CTkButton(
        setup_btn_row,
        text="IA → critères marché",
        height=44,
        corner_radius=RADIUS_PILL,
        fg_color=DARK_CORK,
        text_color=TEXT,
        command=_open_criteria_plan,
    ).pack(side="left", padx=8)
    ctk.CTkButton(
        setup_btn_row,
        text="IA → ajuster les poids",
        height=44,
        corner_radius=RADIUS_PILL,
        fg_color=DARK_CORK,
        text_color=TEXT,
        command=_ai_adjust_weights,
    ).pack(side="left", padx=8)

    ctk.CTkButton(
        setup_btn_row,
        text="Lancer l’analyse",
        height=48,
        corner_radius=RADIUS_PILL,
        fg_color=BURNT_SIENNA,
        text_color=WARM_CREAM,
        font=ctk.CTkFont(family=FONT_BODY[0], size=16),
        command=_launch_from_setup,
    ).pack(side="right")

    run_btn_idle.configure(command=_go_setup)
    rerun_btn.configure(command=_new_run)
    export_btn.configure(command=_open_export)
    eval_export_btn.configure(command=_open_eval_export)
    csv_btn.configure(command=_export_csv_dialog)
    search_var.trace_add("write", lambda *_: _schedule_apply_filters())

    def _on_results_quality_change(*_args: Any) -> None:
        if _filter_guard["build"]:
            return
        state.active_preset_id = ""
        _highlight_preset("")
        _schedule_apply_filters()

    results_quality_var.trace_add("write", _on_results_quality_change)
    rej_search_var.trace_add("write", lambda *_: _render_rejected_list())
    doubt_search_var.trace_add("write", lambda *_: _render_doubtful_list())

    def _open_selected_listing() -> None:
        if state.selected:
            open_listing_url(listing_url_for_row(state.selected))

    open_listing_btn = ctk.CTkButton(
        d_btns,
        text="Ouvrir l’annonce",
        corner_radius=RADIUS_GHOST,
        border_width=1,
        border_color=BURNT_SIENNA,
        fg_color="transparent",
        text_color=BURNT_SIENNA,
        command=_open_selected_listing,
    )
    open_listing_btn.pack(side="left")

    _set_phase("idle")
    root.mainloop()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(prog="python -m vf.app", description="Launch VF Finder GUI")
    p.add_argument("--url", default="", help="Optional start URL (e.g. Vinted catalog URL)")
    p.add_argument(
        "--no-ai",
        action="store_true",
        help="Disable AI features (overrides default behavior for GUI mode)",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Run browsers headed where applicable (default: headless/hybrid)",
    )
    args = p.parse_args()

    launch(
        start_url=str(args.url or ""),
        use_ai=not bool(args.no_ai),
        headless=not bool(args.headed),
    )
