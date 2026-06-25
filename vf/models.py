"""Source de vérité catégorie : CategoryConfig, MSRP, brand tiers et catalogue modèles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DATA_PATH = Path("brouillon") / "data" / "product_models.json"


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    category: str
    msrp_eur: float = 0.0
    release_year: int = 0
    rank_in_family: int = 0
    spec_fields: Tuple[str, ...] = ()
    pack_required: Tuple[str, ...] = ()
    aliases: Dict[str, List[str]] = field(default_factory=dict)
    used_price_eur: Tuple[float, float] = (0.0, 0.0)

    @property
    def used_median_eur(self) -> float:
        lo, hi = self.used_price_eur
        if lo > 0 and hi > 0:
            return (lo + hi) / 2.0
        return 0.0


def _ds_models() -> Dict[str, ModelSpec]:
    common_pack = ("has_power_cable",)
    return {
      "ds": ModelSpec("ds", "DS / NDS", "console_ds", 150, 2004, 10, ("variant",), common_pack,
                      {"vinted": ["nintendo ds", "nds"]}, (25, 55)),
      "ds_lite": ModelSpec("ds_lite", "DS Lite", "console_ds", 130, 2006, 20, ("variant",), common_pack,
                           {"vinted": ["ds lite", "nintendo ds lite"]}, (30, 65)),
      "2ds": ModelSpec("2ds", "2DS", "console_ds", 110, 2013, 30, ("variant",), common_pack,
                       {"vinted": ["nintendo 2ds", "2ds"]}, (45, 90)),
      "2ds_xl": ModelSpec("2ds_xl", "2DS XL", "console_ds", 150, 2016, 35, ("variant",), common_pack,
                          {"vinted": ["2ds xl"]}, (70, 130)),
      "3ds": ModelSpec("3ds", "3DS", "console_ds", 170, 2011, 40, ("variant", "has_stylus"), common_pack,
                       {"vinted": ["nintendo 3ds", "3ds"]}, (60, 120)),
      "3ds_xl": ModelSpec("3ds_xl", "3DS XL", "console_ds", 200, 2012, 50, ("variant", "has_stylus"), common_pack,
                          {"vinted": ["3ds xl", "3dsxl"]}, (80, 150)),
      "new3ds": ModelSpec("new3ds", "New 3DS", "console_ds", 200, 2014, 60, ("variant", "has_stylus"), common_pack,
                          {"vinted": ["new 3ds", "new nintendo 3ds", "nn3ds"]}, (90, 170)),
      "new3ds_xl": ModelSpec(
          "new3ds_xl",
          "New 3DS XL",
          "console_ds",
          220,
          2015,
          70,
          ("variant", "has_stylus"),
          common_pack,
          {
              "vinted": [
                  "new 3ds xl",
                  "new nintendo 3ds xl",
                  "3ds new xl",
                  "n3ds xl",
                  "nouvelle 3ds xl",
                  "nn3ds xl",
              ],
              "leboncoin": ["new 3ds xl", "nintendo 3ds xl new", "new nintendo 3ds"],
              "ebay": ["New Nintendo 3DS XL", "NN3DS XL"],
          },
          (100, 200),
      ),
  }


def _iphone_models() -> Dict[str, ModelSpec]:
    out: Dict[str, ModelSpec] = {}
    specs = [
        ("iphone_16_pro_max", "iPhone 16 Pro Max", 2024, 100, 1499, (900, 1300)),
        ("iphone_16_pro", "iPhone 16 Pro", 2024, 95, 1229, (750, 1100)),
        ("iphone_16_plus", "iPhone 16 Plus", 2024, 85, 969, (600, 900)),
        ("iphone_16", "iPhone 16", 2024, 80, 969, (550, 850)),
        ("iphone_15_pro_max", "iPhone 15 Pro Max", 2023, 90, 1479, (700, 1050)),
        ("iphone_15_pro", "iPhone 15 Pro", 2023, 85, 1229, (600, 950)),
        ("iphone_15", "iPhone 15", 2023, 75, 969, (450, 750)),
        ("iphone_14_pro_max", "iPhone 14 Pro Max", 2022, 80, 1479, (500, 850)),
        ("iphone_14_pro", "iPhone 14 Pro", 2022, 75, 1329, (450, 750)),
        ("iphone_14", "iPhone 14", 2022, 70, 1019, (350, 600)),
        ("iphone_13_pro", "iPhone 13 Pro", 2021, 65, 1159, (320, 550)),
        ("iphone_13", "iPhone 13", 2021, 60, 909, (250, 450)),
        ("iphone_12", "iPhone 12", 2020, 50, 909, (180, 350)),
        ("iphone_11", "iPhone 11", 2019, 40, 709, (120, 250)),
        ("iphone_se_2022", "iPhone SE (2022)", 2022, 35, 529, (100, 220)),
    ]
    for key, label, year, rank, msrp, used in specs:
        slug = label.lower().replace(" ", " ")
        out[key] = ModelSpec(
            key,
            label,
            "phone",
            msrp,
            year,
            rank,
            ("storage_gb", "battery_health_pct", "phone_unlocked"),
            ("phone_unlocked",),
            {"vinted": [slug, slug.replace("iphone", "iphone ")], "ebay": [label]},
            used,
        )
    return out


def _console_models() -> Dict[str, ModelSpec]:
    return {
        "ps4_slim": ModelSpec("ps4_slim", "PS4 Slim", "console_ps4", 300, 2016, 50, ("storage_gb", "has_controller"),
                              ("has_controller", "has_power_cable"), {"vinted": ["ps4 slim"]}, (120, 220)),
        "ps4_pro": ModelSpec("ps4_pro", "PS4 Pro", "console_ps4", 400, 2016, 70, ("storage_gb", "has_controller"),
                             ("has_controller", "has_power_cable"), {"vinted": ["ps4 pro"]}, (150, 280)),
        "ps5": ModelSpec("ps5", "PS5", "console_ps5", 550, 2020, 80, ("storage_gb", "has_controller"),
                         ("has_controller",), {"vinted": ["ps5", "playstation 5"]}, (280, 450)),
        "switch_oled": ModelSpec("switch_oled", "Switch OLED", "console_switch", 350, 2021, 75, ("has_controller",),
                                 ("has_controller",), {"vinted": ["switch oled"]}, (200, 320)),
        "switch": ModelSpec("switch", "Nintendo Switch", "console_switch", 300, 2017, 60, ("has_controller",),
                            ("has_controller",), {"vinted": ["nintendo switch", "switch"]}, (150, 260)),
    }


_BUILTIN: Dict[str, ModelSpec] = {}
_BUILTIN.update(_ds_models())
_BUILTIN.update(_iphone_models())
_BUILTIN.update(_console_models())

_BY_CATEGORY: Dict[str, List[str]] = {}
for _m in _BUILTIN.values():
    _BY_CATEGORY.setdefault(_m.category, []).append(_m.key)


def _load_overlay() -> Dict[str, ModelSpec]:
    if not _DATA_PATH.exists():
        return {}
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: Dict[str, ModelSpec] = {}
    models = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(models, dict):
        return {}
    for key, obj in models.items():
        if not isinstance(obj, dict):
            continue
        try:
            out[str(key)] = ModelSpec(
                key=str(key),
                label=str(obj.get("label") or key),
                category=str(obj.get("category") or "generic"),
                msrp_eur=float(obj.get("msrp_eur") or 0),
                release_year=int(obj.get("release_year") or 0),
                rank_in_family=int(obj.get("rank_in_family") or 0),
                spec_fields=tuple(obj.get("spec_fields") or ()),
                pack_required=tuple(obj.get("pack_required") or ()),
                aliases=dict(obj.get("aliases") or {}),
                used_price_eur=tuple(obj.get("used_price_eur") or (0, 0)),
            )
        except (TypeError, ValueError):
            continue
    return out


_REGISTRY: Dict[str, ModelSpec] = dict(_BUILTIN)


def reload_models() -> None:
    """Recharge le registre (builtin + overlay JSON)."""
    global _REGISTRY, _BY_CATEGORY
    _REGISTRY = dict(_BUILTIN)
    _REGISTRY.update(_load_overlay())
    _BY_CATEGORY = {}
    for m in _REGISTRY.values():
        _BY_CATEGORY.setdefault(m.category, []).append(m.key)


reload_models()


def get_model(key: str) -> Optional[ModelSpec]:
    return _REGISTRY.get(str(key or "").strip())


def models_for_category(category: str) -> List[ModelSpec]:
    keys = _BY_CATEGORY.get(category or "", [])
    return [m for k in keys if (m := get_model(k))]


def get_model_rank(key: str) -> int:
    m = get_model(key)
    return int(m.rank_in_family) if m else 0


def search_aliases(model_key: str, platform: str = "vinted") -> List[str]:
    m = get_model(model_key)
    if not m:
        return []
    plat = (platform or "vinted").lower()
    aliases = list(m.aliases.get(plat) or m.aliases.get("vinted") or [])
    if m.label and m.label not in aliases:
        aliases.insert(0, m.label)
    return aliases


def find_model_by_text(text: str, category: str = "") -> Optional[ModelSpec]:
    t = (text or "").lower()
    if not t:
        return None
    cat = (category or "").lower()
    candidates = models_for_category(cat) if cat else list(_REGISTRY.values())
    best: Optional[ModelSpec] = None
    best_len = 0
    for m in candidates:
        for terms in m.aliases.values():
            for term in terms:
                if term.lower() in t and len(term) > best_len:
                    best, best_len = m, len(term)
        if m.label.lower() in t and len(m.label) > best_len:
            best, best_len = m, len(m.label)
        key_sp = m.key.replace("_", " ")
        if key_sp in t and len(key_sp) > best_len:
            best, best_len = m, len(key_sp)
    return best


def estimate_msrp_eur_for_row(row: Dict[str, Any]) -> Optional[float]:
    key = str(row.get("product_model_key") or row.get("ds_model_family") or "").strip()
    if key and key != "unknown":
        m = get_model(key)
        if m and m.msrp_eur > 0:
            return float(m.msrp_eur)
    cat = str(row.get("item_type") or "")
    m2 = find_model_by_text(f"{row.get('title') or ''} {row.get('ai_model_exact') or ''}", cat)
    if m2 and m2.msrp_eur > 0:
        return float(m2.msrp_eur)
    return None


def expected_used_range(model_key: str) -> Optional[Tuple[float, float]]:
    m = get_model(model_key)
    if not m:
        return None
    lo, hi = m.used_price_eur
    if lo > 0 and hi > 0:
        return (float(lo), float(hi))
    return None


def discount_vs_model_range(row: Dict[str, Any]) -> Optional[float]:
    """Décote vs fourchette occasion typique du modèle (indépendant du run)."""
    key = str(row.get("product_model_key") or row.get("ds_model_family") or "").strip()
    if not key or key == "unknown":
        return None
    rng = expected_used_range(key)
    if not rng:
        return None
    lo, hi = rng
    med = (lo + hi) / 2.0
    if med <= 0:
        return None
    try:
        price = float(row.get("total_price") or row.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return round((1.0 - price / med) * 100.0, 1)


def model_rank_score_bonus(row: Dict[str, Any], *, weight: float = 1.0) -> float:
    """
    Bonus/malus selon le rang du modèle dans sa famille.
    New 3DS XL > 3DS à prix comparable.
    """
    key = str(row.get("product_model_key") or row.get("ds_model_family") or "").strip()
    if not key or key == "unknown":
        return 0.0
    rank = get_model_rank(key)
    if rank <= 0:
        return 0.0
    try:
        price = float(row.get("total_price") or row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return 0.0
    value_ratio = rank / max(1.0, price / 50.0)
    return round(min(12.0, max(-4.0, (value_ratio - 1.2) * 3.5 * weight)), 2)


def compare_models_value(model_a: str, model_b: str, price_a: float, price_b: float) -> int:
    """Retourne >0 si A est meilleur rapport modèle/prix que B."""
    ra, rb = get_model_rank(model_a), get_model_rank(model_b)
    if ra <= 0 or rb <= 0 or price_a <= 0 or price_b <= 0:
        return 0
    va = ra / price_a
    vb = rb / price_b
    if va > vb * 1.05:
        return 1
    if vb > va * 1.05:
        return -1
    return 0


@dataclass(frozen=True)
class CategoryConfig:
    """Schema-driven extraction / scoring for a product category."""

    categorie: str
    label: str = ""
    brand_tiers: Dict[int, Tuple[str, ...]] = field(default_factory=dict)
    specs_a_extraire: Tuple[str, ...] = ()
    red_flags_veto: Tuple[str, ...] = ()
    red_flags: Tuple[str, ...] = ()
    inflated_spec_rules: Tuple[Tuple[str, str, float], ...] = ()
    pack_complet: Tuple[str, ...] = ()
    obsolete_if: Tuple[Tuple[str, str], ...] = ()
    deal_min_discount_tier: Dict[int, float] = field(default_factory=dict)
    extraction_hint: str = ""
    expert_veto_note: str = ""

    def prompt_block(self) -> str:
        specs = ", ".join(self.specs_a_extraire) or "modèle, état, prix"
        veto = ", ".join(self.red_flags_veto) or "HS, arnaque"
        flags = ", ".join(self.red_flags) or veto
        pack = ", ".join(self.pack_complet) or "accessoires inclus"
        hint = f"\n{self.extraction_hint}" if self.extraction_hint else ""
        veto_note = (
            f"\nRÈGLE ABSOLUE: {self.expert_veto_note}"
            if self.expert_veto_note
            else "\nSi is_obsolete ou critical_defect → Rejeté même à 1€."
        )
        return (
            f"Tu es un expert {self.label or self.categorie}. "
            f"Extrais: {specs}. Veto immédiat: {veto}. Autres red flags: {flags}. "
            f"Pack complet idéal: {pack}.{hint}{veto_note}"
        )


_TIER1_PROJECTOR = (
    "epson", "benq", "optoma", "sony", "jvc", "nec", "panasonic", "christie", "infocus",
)
_TIER2_PROJECTOR = ("viewsonic", "acer", "lg", "philips", "xiaomi")
_TIER3_PROJECTOR = (
    "vankyo", "tepun", "tontu", "wanbo", "dbpower", "cheerlux", "byintek", "neopix",
    "picopix", "crosstour", "apeman", "voplls", "luns", "hision", "nommo", "uptech",
)

_CATEGORY_CONFIGS: Dict[str, CategoryConfig] = {
    "projector": CategoryConfig(
        "projector",
        label="vidéoprojecteur home cinéma",
        brand_tiers={1: _TIER1_PROJECTOR, 2: _TIER2_PROJECTOR, 3: _TIER3_PROJECTOR},
        specs_a_extraire=("lumens", "resolution", "marque", "hdmi", "usage", "native_aspect"),
        red_flags_veto=("lampe_hs", "vga_only", "svga_obsolete", "xga_obsolete", "critical_defect", "no_hdmi_obsolete"),
        red_flags=("accessoire_hdmi", "dinosaur_projector", "no_hdmi", "low_lumens", "marketing_lumens"),
        inflated_spec_rules=(("lumens", "tier3_high_lumens_no_ansi", 10.0),),
        pack_complet=("projector", "has_remote", "has_hdmi", "has_power_cable"),
        obsolete_if=(
            (r"\b(svga|800\s*[x×]\s*600)\b", "Résolution SVGA obsolète"),
            (r"\b(xga|1024\s*[x×]\s*768)\b", "Résolution XGA 4:3 obsolète"),
            (r"\b(wvga|854\s*[x×]\s*480)\b", "Résolution WVGA obsolète"),
            (r"\b(vga|d-?sub)\b(?![^|]{0,40}\bhdmi\b)", "Connectique VGA legacy sans HDMI"),
        ),
        deal_min_discount_tier={1: 12.0, 2: 28.0, 3: 42.0},
        extraction_hint="Lumens ANSI réalistes — marques tier-3 : diviser lumens marketing par 10 si pas « ANSI ».",
        expert_veto_note="SVGA/XGA/VGA sans HDMI = obsolète invendable. Rejeté même à 1€.",
    ),
    "console_ds": CategoryConfig(
        "console_ds",
        label="console Nintendo DS / 3DS",
        brand_tiers={1: ("nintendo",), 2: ("nintendo",), 3: ()},
        specs_a_extraire=("variant", "has_stylus", "has_charger", "has_games", "screen_defect"),
        red_flags_veto=("ban", "hs", "icloud", "critical_defect", "screen_defect"),
        red_flags=("accessory_only", "wrong_model", "missing_charger"),
        pack_complet=("console", "has_power_cable", "has_stylus"),
        obsolete_if=(),
        deal_min_discount_tier={1: 10.0, 2: 22.0, 3: 35.0},
    ),
    "console_ps4": CategoryConfig(
        "console_ps4",
        label="PlayStation 4",
        brand_tiers={1: ("sony",), 2: ("sony",), 3: ()},
        specs_a_extraire=("variant", "storage_gb", "controllers_count", "games_count", "has_hdmi", "has_power_cable"),
        red_flags_veto=("ban", "hs", "broken", "critical_defect"),
        red_flags=("accessory_only", "wrong_model", "missing_controller"),
        pack_complet=("console", "has_controller", "has_hdmi", "has_power_cable"),
        obsolete_if=(),
        deal_min_discount_tier={1: 12.0, 2: 25.0, 3: 38.0},
    ),
    "bicycle": CategoryConfig(
        "bicycle",
        label="vélo occasion",
        brand_tiers={
            1: ("trek", "specialized", "cannondale", "bianchi", "cervelo"),
            2: ("cube", "giant", "scott", "canyon", "decathlon"),
            3: (),
        },
        specs_a_extraire=("marque", "taille_cadre", "roues", "type_velo", "repairs_needed"),
        red_flags_veto=("cadre_fissure", "critical_defect", "vendu_pieces"),
        red_flags=("roue_voilee", "missing_parts"),
        pack_complet=("frame", "wheels", "brakes", "saddle"),
        obsolete_if=(),
        deal_min_discount_tier={1: 10.0, 2: 22.0, 3: 35.0},
    ),
    "ac": CategoryConfig(
        "ac",
        label="climatiseur / clim mobile",
        brand_tiers={
            1: ("daikin", "mitsubishi", "atlantic", "fujitsu"),
            2: ("delonghi", "rowenta", "tcl", "hisense", "klarstein"),
            3: (),
        },
        specs_a_extraire=("capacity_btu", "cooling_kw", "has_hose", "has_remote", "room_area_m2"),
        red_flags_veto=("hs", "critical_defect", "piece_auto"),
        red_flags=("recharge_seule", "location", "missing_hose"),
        pack_complet=("unit", "has_remote", "has_hose"),
        obsolete_if=(),
        deal_min_discount_tier={1: 12.0, 2: 25.0, 3: 38.0},
    ),
    "phone": CategoryConfig(
        "phone",
        label="smartphone",
        brand_tiers={
            1: ("apple", "iphone", "samsung", "google", "pixel"),
            2: ("xiaomi", "oneplus", "oppo", "huawei", "honor"),
            3: (),
        },
        specs_a_extraire=("marque", "modele", "storage_gb", "battery_health_pct", "phone_unlocked"),
        red_flags_veto=("icloud", "find_my", "hs", "critical_defect"),
        red_flags=("wrong_model", "battery_low", "screen_crack"),
        pack_complet=("phone", "has_charger", "phone_unlocked"),
        obsolete_if=(),
        deal_min_discount_tier={1: 15.0, 2: 28.0, 3: 40.0},
    ),
}


def get_category_config(category: str) -> Optional[CategoryConfig]:
    cat = (category or "").strip().lower()
    if not cat or cat in ("unknown", "generic"):
        return None
    if cat in _CATEGORY_CONFIGS:
        return _CATEGORY_CONFIGS[cat]
    if cat.startswith("console_") and cat != "console_ds":
        base = _CATEGORY_CONFIGS.get("console_ps4")
        if base:
            return CategoryConfig(
                cat,
                label=f"console {cat.replace('console_', '').replace('_', ' ')}",
                brand_tiers=base.brand_tiers,
                specs_a_extraire=base.specs_a_extraire,
                red_flags_veto=base.red_flags_veto,
                red_flags=base.red_flags,
                pack_complet=base.pack_complet,
                deal_min_discount_tier=base.deal_min_discount_tier,
            )
    if cat in ("clim", "climatisation"):
        return _CATEGORY_CONFIGS.get("ac")
    if cat in ("bike", "velo", "bicycle"):
        return _CATEGORY_CONFIGS.get("bicycle")
    return None


def detect_brand_tier(text: str, cfg: CategoryConfig) -> int:
    t = (text or "").lower()
    if not t or not cfg.brand_tiers:
        return 2
    # Tier 3 first — budget sub-lines (NeoPix, Vankyo…) override parent brand (Philips, etc.)
    for tier in (3, 2, 1):
        for brand in cfg.brand_tiers.get(tier, ()):
            if re.search(rf"(?i)\b{re.escape(brand)}\b", t):
                return tier
    return 3


def category_config_dict(category: str) -> Dict[str, Any]:
    cfg = get_category_config(category)
    if not cfg:
        return {}
    return {
        "categorie": cfg.categorie,
        "label": cfg.label,
        "brand_tiers": {k: list(v) for k, v in cfg.brand_tiers.items()},
        "specs_a_extraire": list(cfg.specs_a_extraire),
        "red_flags_veto": list(cfg.red_flags_veto),
        "red_flags": list(cfg.red_flags),
        "inflated_spec_rules": list(cfg.inflated_spec_rules),
        "pack_complet": list(cfg.pack_complet),
        "obsolete_if": list(cfg.obsolete_if),
        "deal_min_discount_tier": dict(cfg.deal_min_discount_tier),
    }


def export_builtin_seed(path: Optional[Path] = None) -> Path:
    """Écrit le registre builtin (utile pour édition manuelle)."""
    dest = path or _DATA_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "models": {
            k: {
                "label": m.label,
                "category": m.category,
                "msrp_eur": m.msrp_eur,
                "release_year": m.release_year,
                "rank_in_family": m.rank_in_family,
                "spec_fields": list(m.spec_fields),
                "pack_required": list(m.pack_required),
                "aliases": m.aliases,
                "used_price_eur": list(m.used_price_eur),
            }
            for k, m in _BUILTIN.items()
        }
    }
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
