from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PlatformProfile:
    name: str
    label: str
    default_max: int
    alias_max_env: str
    deep_env: str
    skip_env: str
    needs_headed: bool
    fragile: bool
    price_bands: Dict[str, List[Tuple[int, int]]]


PROFILES: Dict[str, PlatformProfile] = {
    "vinted": PlatformProfile("vinted", "Vinted", 1800, "VF_VINTED_ALIAS_MAX", "VF_DEEP_SCRAPE", "", False, False, {}),
    "leboncoin": PlatformProfile(
        "leboncoin", "LeBonCoin", 1400, "VF_LBC_ALIAS_MAX", "VF_LBC_DEEP", "", False, False,
        {
            "console_ds": [(0, 80), (80, 150), (150, 250), (250, 400), (400, 700)],
            "console_ps4": [(0, 80), (80, 150), (150, 250), (250, 400)],
            "projector": [(0, 80), (80, 150), (150, 300), (300, 600), (600, 1200)],
            "generic": [(0, 100), (100, 250), (250, 500)],
        },
    ),
    "wallapop": PlatformProfile(
        "wallapop", "Wallapop", 400, "VF_WALLAPOP_ALIAS_MAX", "VF_WALLAPOP_DEEP", "", False, False,
        {
            "console_ds": [(0, 100), (100, 200), (200, 400)],
            "projector": [(0, 100), (100, 250), (250, 600)],
            "generic": [(0, 150), (150, 400)],
        },
    ),
    "ebay": PlatformProfile("ebay", "eBay", 220, "VF_EBAY_ALIAS_MAX", "", "", True, True, {}),
    "rakuten": PlatformProfile("rakuten", "Rakuten", 400, "VF_RAKUTEN_ALIAS_MAX", "", "VF_SKIP_RAKUTEN", True, True, {}),
    "cashexpress": PlatformProfile("cashexpress", "Cash Express", 260, "VF_CEX_ALIAS_MAX", "", "VF_SKIP_CASHEXPRESS", True, True, {}),
}
ALL_PLATFORMS = ["vinted", "leboncoin", "wallapop", "ebay", "rakuten", "cashexpress"]
GUI_ENABLED_PLATFORMS = ["vinted", "leboncoin"]
GUI_ABANDONED_PLATFORMS = ["wallapop", "rakuten", "cashexpress"]


def profile(name: str) -> PlatformProfile:
    key = name.lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress")
    return PROFILES.get(key, PROFILES["vinted"])


def env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def should_skip_platform(name: str, n_platforms: int) -> bool:
    if os.environ.get("VF_FORCE_FRAGILE", "0") == "1":
        return False
    p = profile(name)
    if p.skip_env and os.environ.get(p.skip_env, "auto").strip().lower() in ("1", "true", "yes"):
        return True
    if n_platforms >= 5 and p.fragile:
        return os.environ.get("VF_SKIP_FRAGILE", "auto").strip().lower() in ("1", "true", "yes", "auto")
    return False


# ---------------------------------------------------------------------------
# Single scrape profile (max volume, still time-bounded)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScrapeModeProfile:
    key: str
    label: str
    variant_max: int
    platform_caps: Dict[str, int]
    time_budgets_sec: Dict[str, int]
    lbc_deep: bool
    lbc_fast: bool
    wallapop_deep: bool
    vinted_deep: bool
    vinted_max_bands: int
    alias_gain_pct: float
    cap_skip_pct: float
    catalog_eta_cap_sec: int
    skip_ebay_projector: bool = False
    env_overrides: Dict[str, str] = field(default_factory=dict)


SCRAPE_MODES: Dict[str, ScrapeModeProfile] = {
    "pro": ScrapeModeProfile(
        key="pro",
        label="Complet",
        variant_max=4,
        platform_caps={
            "vinted": 1500,
            "leboncoin": 800,
            "wallapop": 450,
            "ebay": 180,
            "rakuten": 350,
            "cashexpress": 260,
        },
        time_budgets_sec={
            "vinted": 180,
            "leboncoin": 150,
            "wallapop": 150,
            "ebay": 90,
            "rakuten": 120,
            "cashexpress": 120,
        },
        lbc_deep=True,
        lbc_fast=False,
        wallapop_deep=True,
        vinted_deep=True,
        vinted_max_bands=0,
        alias_gain_pct=0.03,
        cap_skip_pct=0.88,
        catalog_eta_cap_sec=900,
        skip_ebay_projector=False,
        env_overrides={
            "VF_LBC_MAX_OFFSET": "600",
            "VF_LBC_MAX_AGE_DAYS": "300",
            "VF_VINTED_MAX_PAGES": "18",
            "VF_VINTED_FAST_PAGES": "12",
            "VF_VINTED_MIN_TARGET": "1000",
            "VF_VINTED_PER_BAND_MAX": "500",
            "VF_VINTED_ALIAS_MAX": "6",
            "VF_WALLAPOP_MAX_PAGES": "14",
            "VF_WALLAPOP_BOOTSTRAP_TIMEOUT": "55",
            "VF_EBAY_MAX_PAGES": "10",
            "VF_FAST_SCRAPE": "0",
            "VF_WALLAPOP_VARIANTS_MAX": "4",
            "VF_VINTED_ALIAS_MAX": "4",
            "VF_LBC_ALIAS_MAX": "3",
            "VF_EBAY_ALIAS_MAX": "2",
            "VF_WALLAPOP_ALIAS_MAX": "3",
            "VF_SMART_ALIAS": "1",
            "VF_EBAY_ALIAS_GAIN_PCT": "0.03",
            "VF_RULE_BYPASS": "0.82",
            "VF_CASCADE_MAX_CHUNKS": "10",
            "VF_CASCADE_TITLE_CHUNK": "80",
            "VF_CASCADE_SKIP_KEEP_RATE": "0.90",
            "VF_EARLY_SPEC_FILTER": "1",
            "VF_VINTED_PAGE_GAIN_PCT": "0.02",
            "VF_LBC_BOOTSTRAP_TIMEOUT": "45",
            "VF_VINTED_MIN_YIELD_FALLBACK": "40",
            "VF_TOP_VISION_K": "15",
        },
    ),
}

# Legacy aliases → pro
for _legacy in ("fast", "complet", "deep"):
    SCRAPE_MODES[_legacy] = SCRAPE_MODES["pro"]


def is_gui_mode() -> bool:
    return os.environ.get("VF_GUI_MODE", "0").strip().lower() in ("1", "true", "yes", "on")


def resolve_scrape_mode(explicit: Optional[str] = None) -> str:
    raw = (explicit or os.environ.get("VF_SCRAPE_MODE") or "").strip().lower()
    if raw in SCRAPE_MODES:
        return "pro"
    return "pro"


def scrape_mode_profile(mode: Optional[str] = None) -> ScrapeModeProfile:
    return SCRAPE_MODES["pro"]


def apply_scrape_profile(mode: Optional[str] = None) -> str:
    """Applique le profil scrape unique (env vars session). Retourne 'pro'."""
    prof = SCRAPE_MODES["pro"]
    os.environ["VF_SCRAPE_MODE"] = "pro"
    os.environ["VF_SCRAPE_PROFILE"] = "pro"
    os.environ["VF_VARIANT_MAX"] = str(prof.variant_max)
    os.environ["VF_LBC_DEEP"] = "1" if prof.lbc_deep else "0"
    os.environ["VF_LBC_FAST"] = "1" if prof.lbc_fast else "0"
    os.environ["VF_WALLAPOP_DEEP"] = "1" if prof.wallapop_deep else "0"
    os.environ["VF_DEEP_SCRAPE"] = "1" if prof.vinted_deep else "0"
    if prof.vinted_max_bands > 0:
        os.environ["VF_VINTED_MAX_BANDS"] = str(prof.vinted_max_bands)
    else:
        os.environ.pop("VF_VINTED_MAX_BANDS", None)
    os.environ["VF_ALIAS_GAIN_PCT"] = str(prof.alias_gain_pct)
    os.environ["VF_CAP_SKIP_PCT"] = str(prof.cap_skip_pct)
    os.environ["VF_CATALOG_ETA_CAP_SEC"] = str(prof.catalog_eta_cap_sec)
    for plat, cap in prof.platform_caps.items():
        env_key = {
            "vinted": "VF_SCRAPE_VINTED_MAX",
            "leboncoin": "VF_SCRAPE_LBC_MAX",
            "wallapop": "VF_SCRAPE_WALLAPOP_MAX",
            "ebay": "VF_SCRAPE_EBAY_MAX",
            "rakuten": "VF_SCRAPE_RAKUTEN_MAX",
            "cashexpress": "VF_SCRAPE_CEX_MAX",
        }.get(plat)
        if env_key:
            os.environ[env_key] = str(cap)
    for ek, ev in prof.env_overrides.items():
        os.environ[ek] = ev
    return "pro"


def platform_scrape_cap(name: str, mode: Optional[str] = None) -> int:
    key = name.lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress")
    prof = scrape_mode_profile(mode)
    if key in prof.platform_caps:
        return prof.platform_caps[key]
    return profile(key).default_max


def platform_time_budget_sec(name: str, mode: Optional[str] = None) -> int:
    key = name.lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress")
    prof = scrape_mode_profile(mode)
    return int(prof.time_budgets_sec.get(key) or 0)


def variant_limits_for_mode(n_platforms: int, mode: Optional[str] = None) -> Dict[str, int]:
    """Variantes alias par plateforme — profil unique pro."""
    prof = scrape_mode_profile(mode)
    vm = prof.variant_max
    if n_platforms >= 6:
        return {
            "vinted": min(4, vm),
            "leboncoin": min(3, vm),
            "ebay": 1,
            "wallapop": 1,
            "rakuten": 0,
            "cashexpress": 0,
            "default": 1,
        }
    if n_platforms >= 4:
        return {
            "vinted": vm,
            "leboncoin": min(vm, 3),
            "ebay": min(2, vm),
            "wallapop": min(2, vm),
            "rakuten": 1,
            "cashexpress": 0,
            "default": 1,
        }
    return {
        "vinted": vm,
        "leboncoin": min(vm, 3),
        "ebay": min(2, vm),
        "wallapop": min(2, vm),
        "default": min(2, vm),
    }
