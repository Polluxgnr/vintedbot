"""Intégration LLM OpenRouter : triage batch sur cas ambigus, vision top-K, prompts CategoryConfig."""


import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from .engine import (
    CircuitOpenError,
    get_circuit,
    retry_call,
)
# llm_cache merged below (get_cost_tracker)

logger = logging.getLogger("vf.ai")

# Prompts système centralisés (revue LLM / cohérence)
SYS_MARKET_ANALYST = (
    "Analyste arbitrage occasion FR multi-plateformes. JSON strict quand demandé. "
    "Compare prix annonce vs médiane run, neuf et reconditionné. "
    "Pénalise surprix et packs incomplets. Français concis."
)
SYS_TITLE_FILTER = (
    "Filtre titres marketplace. JSON uniquement. "
    "Garde produit principal + bundles cohérents. Exclure accessoires/jeux seuls."
)
SYS_DEAL_AUDIT = (
    "Expert arbitrage multi-plateformes FR. JSON strict. "
    "Le prix ne sauve pas un produit obsolète : technical_veto=true si is_obsolete, "
    "critical_defect, SVGA/VGA sans HDMI, mauvaise génération DS, accessoire seul. "
    "Cap score 15 si veto. Le pack complet compte autant que le prix. Pas de prose hors JSON."
)
SYS_CRITERIA_DISCOVERY = (
    "Expert marketplace occasion FR. Tu proposes des critères de filtrage EXTRACTIBLES "
    "depuis titres/mini-descriptions (regex/mots-clés) et utiles pour CE marché. "
    "Tu dois aussi proposer des hard_excludes pour éliminer les hors-sujets typiques. "
    "Si pertinent pour le marché, inclure des dimensions techniques avec regex (groupe 1): "
    "BTU (\\b(\\d{4,6})\\s*btu\\b), kW (\\b(\\d(?:[.,]\\d)?)\\s*kw\\b), dB (\\b(\\d{2})\\s*dba?\\b), "
    "m² (\\b(\\d{1,3})\\s*m\\s*2\\b ou \\b(\\d{1,3})\\s*m²\\b). "
    "Réponse = JSON strict uniquement, sans markdown."
)


def _load_dotenv() -> None:
    """Load tests/.env into os.environ (does not override existing vars)."""
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
            logger.debug("Variables chargées depuis %s", path)
        except OSError as exc:
            logger.warning("Impossible de lire %s : %s", path, exc)
        return


_load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("VF_LLM_MODEL", "google/gemini-2.5-flash-lite-preview-09-2025")
DEFAULT_VISION_MODEL = os.environ.get("VF_VISION_MODEL", "google/gemini-2.5-flash")
_VISION_FALLBACK_MODELS = (
    "google/gemini-2.5-flash",
    "google/gemini-2.0-flash-001",
    "openai/gpt-4o-mini",
)
_vision_model_disabled: set[str] = set()
_vision_disabled_all = False


def api_key() -> str:
    return (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("VF_OPENROUTER_API_KEY") or "").strip()


def _extract_json(text: str) -> Any:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return json.loads(t[i : j + 1])
    return json.loads(t)


def _parse_llm_items_list(raw: str) -> List[Dict[str, Any]]:
    """Parse une réponse LLM items[] — tolère JSONL ou JSON multiples."""
    if not raw:
        return []
    try:
        obj = _extract_json_lenient(raw)
        if isinstance(obj, dict):
            items = obj.get("items")
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
    except Exception:
        pass
    items: List[Dict[str, Any]] = []
    for line in (raw or "").splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
            if isinstance(o, dict) and o.get("id"):
                items.append(o)
        except json.JSONDecodeError:
            continue
    if items:
        return items
    for m in re.finditer(r'\{\s*"id"\s*:\s*"(\d+)"[^}]*\}', raw, re.S):
        try:
            o = json.loads(m.group(0))
            if isinstance(o, dict):
                items.append(o)
        except json.JSONDecodeError:
            continue
    return items


def _extract_json_lenient(text: str) -> Any:
    try:
        return _extract_json(text)
    except json.JSONDecodeError:
        t = text or ""
        m = re.search(r'"keep_ids"\s*:\s*\[([^\]]*)\]', t, re.S)
        if m:
            ids = re.findall(r'"(\d+)"', m.group(1))
            if ids:
                return {"keep_ids": ids}
        m2 = re.search(r'"items"\s*:\s*\[', t)
        if m2:
            ids = re.findall(r'"id"\s*:\s*"(\d+)"', t)
            if ids:
                return {"items": [{"id": i} for i in ids]}
        m3 = re.search(r'"keep"\s*:\s*\[', t)
        if m3:
            keep: List[Dict[str, Any]] = []
            for m in re.finditer(
                r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"r"\s*:\s*(\d+)',
                t,
            ):
                keep.append({"id": m.group(1), "r": int(m.group(2))})
            if keep:
                return {"keep": keep}
            ids = re.findall(r'"id"\s*:\s*"([^"]+)"', t[m3.start() : m3.start() + 4000])
            if ids:
                return {"keep": [{"id": i, "r": 55} for i in ids]}
        raise

class _RetryableLLMError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _chat(prompt: str, *, system: str, max_tokens: int = 900) -> str:
    key = api_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY manquante (variable d'environnement).")

    tracker = get_cost_tracker()
    if tracker.budget_exceeded():
        raise RuntimeError(
            f"Budget LLM dépassé (~${tracker.estimated_usd:.4f} ≥ ${tracker.budget_usd:.4f}). "
            "Augmentez VF_LLM_BUDGET_USD ou désactivez l'IA."
        )

    circuit = get_circuit("openrouter")
    try:
        circuit.before_call()
    except CircuitOpenError as exc:
        raise RuntimeError(str(exc)) from exc

    try:
        from .engine import load_prompt_overrides

        overrides = load_prompt_overrides()
        if overrides:
            system = f"{system}\n\nConsignes apprises (retours utilisateur):\n{overrides}"[:2400]
    except Exception:
        pass

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }

    def _llm_retryable(exc: BaseException) -> bool:
        if isinstance(exc, _RetryableLLMError):
            return True
        if isinstance(exc, requests.RequestException):
            return True
        return False

    def _do_call() -> str:
        r = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        if r.status_code in (429, 500, 502, 503, 504):
            raise _RetryableLLMError(f"HTTP {r.status_code}", status=r.status_code)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            tracker.add_usage(usage, model=DEFAULT_MODEL)
        return str(data["choices"][0]["message"]["content"])

    try:
        out = retry_call(_do_call, attempts=5, min_wait=2.0, max_wait=20.0, retry_predicate=_llm_retryable)
        circuit.record_success()
        return out
    except Exception:
        circuit.record_failure()
        raise


def summarize_item_brief(
    *,
    intent: str,
    item: Dict[str, Any],
    market_stats: Dict[str, Any],
) -> str:
    """
    One-item analysis on demand (cheap): strengths/weaknesses + what to verify.
    Returns short French text.
    """
    if not api_key():
        return ""
    safe_item = {
        k: item.get(k)
        for k in (
            "title",
            "price",
            "total_price",
            "item_type",
            "condition",
            "reason",
            "risk_flags",
            "quality_grade",
            "quality_notes",
            "variant",
            "storage_gb",
            "cuh",
            "ps4_family",
            "lumens",
            "has_hdmi",
            "has_controller",
            "final_score",
            "discount_pct",
        )
        if item.get(k) is not None
    }
    med = market_stats.get("median_price") or market_stats.get("median")
    prompt = (
        f"Intent: {intent}\n"
        f"Annonce: {json.dumps(safe_item, ensure_ascii=False)}\n"
        f"Marché (médiane ~{med}€): {json.dumps({k: market_stats.get(k) for k in ('median_price', 'median', 'count', 'p25', 'p75') if market_stats.get(k) is not None}, ensure_ascii=False)}\n\n"
        "3-5 puces françaises: points forts, faiblesses, ce qu'il faut vérifier avant achat. Prudent."
    )
    try:
        return _chat(prompt, system="Expert achat occasion, concis, prudent.", max_tokens=320).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Résumé item IA échoué : %s", exc)
        return ""


def agent_design_market_filters(
    search_intent: str,
    *,
    market_category: str,
    catalog_url: str = "",
    search_text: str = "",
    sample_rows: List[Dict[str, Any]],
    buyer_priorities: str = "",
) -> tuple[List[Dict[str, Any]], str]:
    """
    Agent conseiller : conçoit les filtres UI APRÈS analyse de l’URL/recherche (pas le catalogue seul).
    Retourne (filtres sanitizés, note courte pour l’UI).
    """
    from .engine import category_filter_templates, sanitize_dynamic_filters

    cat = market_category or "generic"
    fallback = category_filter_templates(cat)
    if not api_key():
        return fallback, "Filtres par défaut (IA hors ligne)."

    on_topic = [r for r in sample_rows if str(r.get("item_type") or "") == cat and not r.get("is_accessory")]
    if len(on_topic) < 3:
        on_topic = [r for r in sample_rows if not r.get("is_accessory")][:20]
    lines = []
    for r in on_topic[:20]:
        bits = [str(r.get("item_type")), str(r.get("title") or "")[:70]]
        for k in (
            "has_stylus",
            "includes_games",
            "variant",
            "ds_model_family",
            "charger_status",
            "has_controller",
            "has_hdmi",
            "has_power_cable",
            "lumens",
            "resolution",
            "projector_brand",
            "projector_usage",
            "btu",
        ):
            if r.get(k):
                bits.append(f"{k}={r.get(k)}")
        lines.append(" | ".join(bits))

    examples_by_cat = {
        "console_ds": "Nintendo 3DS/DS: stylet, jeux inclus, modèle, chargeur — pas HDMI/lumens.",
        "console_ps4": "PS4: manette, HDMI, alim, stockage, slim/pro.",
        "console_switch": "Switch: manette, dock, modèle OLED/Lite.",
        "projector": "Projecteur: lumens, résolution, HDMI, usage bureau/cinéma.",
        "ac": "Climatisation: BTU, tuyau, marque.",
        "coffee": "Cafetière / machine café: type (expresso, filtre), marque, état.",
        "fan": "Ventilateur: diamètre, bruit, oscillation.",
        "apparel": "Vêtement: taille, matière, marque, état.",
        "phone": "Téléphone: modèle EXACT (iPhone 14 Pro…), stockage Go, débloqué, batterie/écran.",
        "wearable": "Montre GPS: modèle exact (Garmin Fenix, Forerunner…), GPS, cartes, taille mm.",
        "moto": "Moto / scooter: cylindrée, km, année, papiers.",
        "generic": "Produit d'occasion: attributs pertinents pour CE marché uniquement.",
    }
    market_guide = examples_by_cat.get(cat, examples_by_cat["generic"])
    forbidden = "filtres d'une autre catégorie (ex. pas lumens/HDMI pour 3DS, pas BTU pour cafetière)"

    prompt = (
        f"Tu es un agent acheteur Vinted avec accès aux annonces (texte + signaux).\n"
        f"URL: {catalog_url[:200]}\n"
        f"Recherche Vinted (search_text): {search_text!r}\n"
        f"Marché à traiter: {cat}\n"
        f"Guide marché: {market_guide}\n"
        f"Priorités utilisateur: {(buyer_priorities or 'qualité/prix')[:400]}\n"
        f"Intent affiché: {search_intent[:120]}\n\n"
        f"INTERDIT pour ce marché: {forbidden or 'filtres d’une autre catégorie'}\n\n"
        "Échantillon annonces (peut contenir du bruit hors-sujet — ignore le bruit):\n"
        + ("\n".join(lines) if lines else "(peu d’échantillons)")
        + "\n\nRéponds JSON strict:\n"
        '{"filters":[{"id":"…","label":"…","type":"bool|select|number","field":"…",'
        '"operator":"eq|gte|lte|contains|bool_on|custom","segment":"market",'
        '"options":["all","…"]}],"agent_note":"1-2 phrases: ce que tu regardes pour conseiller l\'acheteur"}\n'
        "4-7 filtres utiles. operator obligatoire : eq (égalité), gte (minimum numérique), "
        "contains (mot-clé titre/desc), bool_on (oui/non). select: options avec all en premier."
    )
    try:
        raw = _chat(
            prompt,
            system=(
                "Agent marketplace — tu conçois l’UI de filtrage comme un expert du marché déclaré. "
                "Ne mélange jamais les catégories. JSON uniquement."
            ),
            max_tokens=1100,
        )
        obj = _extract_json(raw)
        note = ""
        fl: List[Dict[str, Any]] = []
        if isinstance(obj, dict):
            note = str(obj.get("agent_note") or "")[:280]
            raw_fl = obj.get("filters")
            if isinstance(raw_fl, list):
                fl = [f for f in raw_fl if isinstance(f, dict) and f.get("id")]
        if fl:
            ai_only = sanitize_dynamic_filters(fl, cat)
            logger.info("Agent filtres %s: %d IA (%s)", cat, len(ai_only), note[:60])
            return ai_only, note or f"Filtres IA — marché {cat}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agent filtres échoué: %s", exc)
    return fallback, f"Filtres adaptés au marché {cat} (repli règles)."


def suggest_dynamic_filters(
    search_intent: str,
    *,
    dominant_category: str,
    sample_rows: List[Dict[str, Any]],
    buyer_priorities: str = "",
) -> List[Dict[str, Any]]:
    """
    One LLM call: propose buyer-relevant dynamic filters for this search.
    Returns list of filter specs compatible with filters_dynamic.apply_dynamic_filters.
    """
    fl, _note = agent_design_market_filters(
        search_intent,
        market_category=dominant_category,
        sample_rows=sample_rows,
        buyer_priorities=buyer_priorities,
    )
    base_ids = {"risk_mode", "score_band", "hide_accessories", "quality_tier"}
    return [f for f in fl if str(f.get("id") or "") not in base_ids]


def build_category_extraction_prompt(category: str, *, search_intent: str = "") -> str:
    """Prompt générique basé sur CategoryConfig (models.py)."""
    from .models import get_category_config

    cfg = get_category_config(category)
    if not cfg:
        return (
            f"Intent: {search_intent}\n"
            "Extrais modèle, état, accessoires inclus, red flags (HS, arnaque, hors-sujet)."
        )
    return f"Intent acheteur: {search_intent}\n{cfg.prompt_block()}"


def extract_specs_batch(
    search_intent: str,
    rows: List[Dict[str, Any]],
    *,
    max_items: int = 50,
    category: str = "",
) -> Dict[str, Dict[str, Any]]:
    """
    Extraction modèle / specs / complétude sur le top N (1–2 appels LLM par chunks).
    """
    if not api_key() or not rows:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    from .engine import AI_SPECS_CHUNK, category_buyer_criteria

    dom_cat = category or str(rows[0].get("item_type") or "") if rows else ""
    criteria = build_category_extraction_prompt(dom_cat, search_intent=search_intent)
    chunk_size = AI_SPECS_CHUNK
    subset = rows[:max_items]
    for off in range(0, len(subset), chunk_size):
        chunk = subset[off : off + chunk_size]
        lines = []
        for r in chunk:
            desc = str(r.get("description") or r.get("description_excerpt") or "")[:350]
            lines.append(
                json.dumps(
                    {
                        "id": str(r.get("id")),
                        "title": str(r.get("title") or "")[:100],
                        "price": r.get("total_price") or r.get("price"),
                        "item_type": r.get("item_type"),
                        "desc": desc,
                    },
                    ensure_ascii=False,
                )
            )
        cat_extra = ""
        chunk_types = {str(r.get("item_type") or "") for r in chunk}
        cfg_block = build_category_extraction_prompt(dom_cat, search_intent=search_intent)
        if dom_cat == "projector" or "projector" in chunk_types:
            cat_extra = (
                "\nProjecteur — champs obligatoires:\n"
                "- ansi_lumens (réaliste, pas LED marketing)\n"
                "- native_aspect: 16:9|4:3|16:10|unknown\n"
                "- has_hdmi (bool), legacy_ports_only (bool si VGA/DVI/M1 seul)\n"
                "- usable_for_home: bool\n"
            )
        elif dom_cat == "ac" or "ac" in chunk_types:
            cat_extra = (
                "\nClim — champs obligatoires:\n"
                "- btu (entier), room_volume_m3 si indiqué, has_hose (bool)\n"
            )
        elif dom_cat in ("skate", "skates") or chunk_types & {"skate", "skates"}:
            cat_extra = (
                "\nSkate — champs obligatoires:\n"
                "- board_type (deck|complete|trucks|wheels), brand, wear_grade (A-F)\n"
            )
        elif dom_cat in ("apparel", "shoes", "jeans") or chunk_types & {"apparel", "shoes", "jeans"}:
            cat_extra = (
                "\nVêtements — champs obligatoires:\n"
                "- brand, size, material, wear_grade (A-F), defect_note\n"
            )
        prompt = (
            f"{cfg_block}\n"
            f"Critères marché: {criteria}\n"
            "Pour chaque annonce JSONL, extrais les specs exactes pour comparer les offres.\n"
            "Champs communs:\n"
            "- model_exact: modèle précis\n"
            "- variant: fat|slim|pro|standard|…\n"
            "- storage_gb, has_controller, has_hdmi, has_power, has_box (bool)\n"
            "- bundle_items: liste courte accessoires inclus\n"
            "- condition_note: état en 1 phrase\n"
            "- completeness_score: 0-100\n"
            "- is_obsolete, obsolete_reason, critical_defect, defect_reason (bool/str)\n"
            "- inflated_specs, brand_tier (1-3), repairs_needed, technical_veto (bool)\n"
            "- red_flags: liste (ban, HS, dinosaur_projector, no_hdmi, low_lumens, wrong_model…)\n"
            "Si is_obsolete ou critical_defect → technical_veto true, relevance 0.\n"
            f"{cat_extra}"
            '- Réponds JSON: {"items":[{"id":"…","model_exact":"…",…}]}\n\n'
            + "\n".join(lines)
        )
        try:
            raw = _chat(prompt, system="Expert specs produits occasion. JSON strict.", max_tokens=1800)
            obj = _extract_json_lenient(raw)
            items = obj.get("items") if isinstance(obj, dict) else []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("id"):
                        iid = str(it["id"])
                        out[iid] = {
                            "ai_model_exact": str(it.get("model_exact") or "")[:120],
                            "ai_variant": str(it.get("variant") or ""),
                            "ai_bundle": (
                                ", ".join(it.get("bundle_items") or [])
                                if isinstance(it.get("bundle_items"), list)
                                else str(it.get("bundle_items") or "")
                            )[:200],
                            "ai_condition_note": str(it.get("condition_note") or "")[:160],
                            "ai_completeness": int(it.get("completeness_score") or 0),
                            "ai_spec_flags": (
                                ",".join(it.get("red_flags") or [])
                                if isinstance(it.get("red_flags"), list)
                                else str(it.get("red_flags") or "")
                            ),
                        }
                        for bk in (
                            "storage_gb",
                            "has_controller",
                            "has_hdmi",
                            "has_power",
                            "has_box",
                            "ansi_lumens",
                            "native_aspect",
                            "usable_for_home",
                            "legacy_ports_only",
                            "btu",
                            "room_volume_m3",
                            "has_hose",
                            "board_type",
                            "wear_grade",
                            "brand",
                            "size",
                            "material",
                        ):
                            if bk in it:
                                out[iid][bk] = it[bk]
        except Exception as exc:  # noqa: BLE001
            logger.warning("IA specs batch échouée (chunk %s): %s", off, exc)
    logger.info("IA specs: %d/%d items", len(out), min(max_items, len(rows)))
    return out


def deep_verify_top_batch(
    search_intent: str,
    rows: List[Dict[str, Any]],
    *,
    max_items: int = 50,
    chunk_size: int = 0,
    category: str = "",
) -> Dict[str, Dict[str, Any]]:
    """
    Deep text audit on top N (title+description+price vs neuf).
    Returns id -> {ai_relevance, ai_verdict, ai_msrp_eur, ai_flags, ai_summary}
    """
    if not api_key() or not rows:
        return {}

    from .engine import AI_VERIFY_CHUNK, category_buyer_criteria

    dom_cat = category or str(rows[0].get("item_type") or "") if rows else ""
    criteria = category_buyer_criteria(dom_cat)
    if chunk_size <= 0:
        chunk_size = AI_VERIFY_CHUNK
    out: Dict[str, Dict[str, Any]] = {}
    subset = rows[:max_items]
    for off in range(0, len(subset), chunk_size):
        chunk = subset[off : off + chunk_size]
        lines = []
        for r in chunk:
            desc = str(r.get("description") or r.get("description_excerpt") or "")[:280]
            lines.append(
                json.dumps(
                    {
                        "id": str(r.get("id")),
                        "title": str(r.get("title") or "")[:90],
                        "price": r.get("total_price") or r.get("price"),
                        "model": r.get("ai_model_exact") or r.get("variant"),
                        "completeness": r.get("ai_completeness"),
                        "desc": desc,
                        "risk_flags": r.get("risk_flags"),
                        "quality_tier": r.get("quality_tier"),
                        "viability_grade": r.get("viability_grade"),
                        "lumens": r.get("lumens"),
                        "native_aspect": r.get("native_aspect"),
                    },
                    ensure_ascii=False,
                )
            )
        prompt = (
            f"Intent: {search_intent}\n"
            f"Critères marché: {criteria}\n"
            "Analyse chaque annonce (JSONL). Pour un acheteur prudent:\n"
            "- relevance 0-100 (0 si dinosaure invendable: pas HDMI, XGA/WVGA, <500 lumens réels)\n"
            "- msrp_eur prix neuf estimé (0 si modèle obsolète sans MSRP actuel)\n"
            "- flags: accessory_only, wrong_model, dinosaur_projector, no_hdmi, low_lumens, banned, broken, scam_risk\n"
            "- verdict: 1 phrase comparative\n"
            'Réponds JSON: {"items":[{"id":"...","relevance":0,"msrp_eur":0,"flags":[],"verdict":"..."}]}\n\n'
            + "\n".join(lines)
        )
        try:
            raw = _chat(prompt, system="Expert achat occasion Vinted. JSON strict.", max_tokens=2400)
            obj = _extract_json(raw)
            items = obj.get("items") if isinstance(obj, dict) else []
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    iid = str(it.get("id") or "")
                    if not iid:
                        continue
                    out[iid] = {
                        "ai_relevance": int(it.get("relevance") or 0),
                        "ai_msrp_eur": it.get("msrp_eur"),
                        "ai_flags": ",".join(it.get("flags") or [])
                        if isinstance(it.get("flags"), list)
                        else str(it.get("flags") or ""),
                        "ai_verdict": str(it.get("verdict") or "")[:200],
                    }
        except Exception as exc:  # noqa: BLE001
            logger.warning("IA deep verify chunk %s échouée: %s", off, exc)
    logger.info("IA deep verify: %d items", len(out))
    return out


def vision_check_item(
    *,
    search_intent: str,
    title: str,
    image_url: str,
) -> str:
    """Optional vision pass (image URL). Returns short French note or ''."""
    global _vision_disabled_all
    if _vision_disabled_all or not api_key() or not image_url:
        return ""
    url = str(image_url).strip()
    if not url.startswith(("http://", "https://")):
        return ""
    if len(url) > 2048:
        logger.warning("Vision IA: URL image trop longue (%d)", len(url))
        return ""
    vision_models = [os.environ.get("VF_VISION_MODEL", DEFAULT_VISION_MODEL)]
    for m in _VISION_FALLBACK_MODELS:
        if m and m not in vision_models:
            vision_models.append(m)
    key = api_key()
    prompt_text = (
        f"Intent acheteur: {search_intent}\n"
        f"Titre: {title}\n"
        "Décris ce que tu vois : accessoires visibles (chargeur, stylet, manette, câbles), "
        "défauts cosmétiques, état général. Si un chargeur ou stylet est visible, dis-le clairement. "
        "2-4 phrases, français."
    )
    for vision_model in vision_models:
        if not vision_model or vision_model in _vision_model_disabled:
            continue
        payload = {
            "model": vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            "max_tokens": 220,
            "temperature": 0.2,
        }
        try:
            r = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
            if r.status_code == 404:
                _vision_model_disabled.add(vision_model)
                logger.warning("Vision IA 404 (%s) — modèle suivant", vision_model)
                continue
            if r.status_code == 400:
                body = (r.text or "")[:240]
                logger.warning("Vision IA 400 (%s): %s — repli texte seul", vision_model, body)
                fallback = _chat(
                    f"Intent: {search_intent}\nTitre: {title}\n"
                    "Sans image. Infère l'état probable et accessoires possibles depuis le titre. 2 phrases FR.",
                    system="Assistant achat occasion. Concis.",
                    max_tokens=120,
                )
                return str(fallback or "").strip()
            if r.status_code in (401, 403):
                body = (r.text or "")[:240]
                logger.warning("Vision IA %s (%s): %s — repli texte seul", r.status_code, vision_model, body)
                _vision_model_disabled.add(vision_model)
                fallback = _chat(
                    f"Intent: {search_intent}\nTitre: {title}\n"
                    "Sans image. Infère l'état probable et accessoires possibles depuis le titre. 2 phrases FR.",
                    system="Assistant achat occasion. Concis.",
                    max_tokens=120,
                )
                return str(fallback or "").strip()
            r.raise_for_status()
            data = r.json()
            return str(data["choices"][0]["message"]["content"]).strip()
        except requests.HTTPError as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code == 404:
                _vision_model_disabled.add(vision_model)
                logger.warning("Vision IA 404 (%s) — modèle suivant", vision_model)
                continue
            logger.warning("Vision IA échouée (%s): %s", vision_model, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vision IA échouée (%s): %s", vision_model, exc)
    if len(_vision_model_disabled) >= len(vision_models):
        _vision_disabled_all = True
        logger.warning("Vision IA désactivée pour cette session (tous modèles en échec)")
    return ""


def evaluate_buyer_fit_batch(
    search_intent: str,
    rows: List[Dict[str, Any]],
    *,
    category: str = "",
    buyer_priorities: str = "",
    max_items: int = 40,
) -> Dict[str, Dict[str, Any]]:
    """
    Évalue chaque annonce selon les critères du marché + priorités utilisateur.
    Retourne id -> {buyer_fit_score, buyer_fit_grade, buyer_fit_notes, buyer_fit_flags, usable_for_home?}
    """
    if not api_key() or not rows:
        return {}

    from .engine import AI_SPECS_CHUNK, category_buyer_criteria

    dom_cat = category or str(rows[0].get("item_type") or "") if rows else ""
    criteria = category_buyer_criteria(dom_cat)
    priorities = (buyer_priorities or search_intent).strip()[:500]
    out: Dict[str, Dict[str, Any]] = {}
    subset = rows[:max_items]
    chunk_size = max(6, AI_SPECS_CHUNK)

    for off in range(0, len(subset), chunk_size):
        chunk = subset[off : off + chunk_size]
        lines = []
        for r in chunk:
            lines.append(
                json.dumps(
                    {
                        "id": str(r.get("id")),
                        "title": str(r.get("title") or "")[:90],
                        "price": r.get("total_price") or r.get("price"),
                        "item_type": r.get("item_type"),
                        "lumens": r.get("lumens"),
                        "native_aspect": r.get("native_aspect"),
                        "has_hdmi": r.get("has_hdmi"),
                        "quality_tier": r.get("quality_tier"),
                        "viability_grade": r.get("viability_grade"),
                        "model": r.get("ai_model_exact"),
                        "desc": str(r.get("description") or r.get("description_excerpt") or "")[:280],
                    },
                    ensure_ascii=False,
                )
            )
        prompt = (
            f"Recherche: {search_intent}\n"
            f"Catégorie: {dom_cat}\n"
            f"Critères techniques: {criteria}\n"
            f"Priorités acheteur (texte libre): {priorities or 'défaut prudent'}\n\n"
            "Pour chaque annonce JSONL, note l'adéquation aux priorités (pas seulement le prix).\n"
            "Projecteur: lumens réels + format 16:9 + HDMI. Clim: BTU + tuyau. Vêtements: marque + état.\n"
            "Pénaliser fortement dinosaures bureautiques (VGA seul, XGA 4:3, 50 lumens).\n"
            "Champs par item:\n"
            "- fit_score: 0-100\n"
            "- fit_grade: A|B|C|D|F\n"
            "- notes: 1-2 phrases françaises\n"
            "- flags: liste (dinosaur, no_hdmi, low_lumens, good_deal, …)\n"
            "- usable_for_home: bool (projecteurs)\n"
            '{"items":[{"id":"…","fit_score":0,"fit_grade":"C","notes":"…","flags":[],"usable_for_home":true}]}\n\n'
            + "\n".join(lines)
        )
        try:
            raw = _chat(
                prompt,
                system="Expert acheteur occasion — compare specs réelles, pas la marque seule. JSON strict.",
                max_tokens=2000,
            )
            items = _parse_llm_items_list(raw)
            if items:
                for it in items:
                    if not isinstance(it, dict) or not it.get("id"):
                        continue
                    iid = str(it["id"])
                    try:
                        sc = float(it.get("fit_score") or 0)
                    except (TypeError, ValueError):
                        sc = 0.0
                    out[iid] = {
                        "buyer_fit_score": max(0.0, min(100.0, sc)),
                        "buyer_fit_grade": str(it.get("fit_grade") or "")[:2],
                        "buyer_fit_notes": str(it.get("notes") or "")[:240],
                        "buyer_fit_flags": (
                            ",".join(it.get("flags") or [])
                            if isinstance(it.get("flags"), list)
                            else str(it.get("flags") or "")
                        )[:120],
                    }
                    if "usable_for_home" in it:
                        out[iid]["usable_for_home"] = bool(it.get("usable_for_home"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("IA buyer fit chunk %s: %s", off, exc)
    logger.info("IA buyer fit: %d/%d", len(out), min(max_items, len(rows)))
    return out


def suggest_score_weights(
    *,
    category: str,
    buyer_priorities: str,
    current: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """
    Propose des poids de score (sliders) selon priorités texte + catégorie.
    """
    if not api_key():
        return {}

    from .engine import asdict, category_buyer_criteria, weight_field_defs
    from .engine import ScoreWeights

    cat = category or "generic"
    criteria = category_buyer_criteria(cat)
    fields = weight_field_defs(cat)
    keys = [f[0] for f in fields]
    cur = current or asdict(ScoreWeights())
    cur_snip = {k: cur.get(k) for k in keys if k in cur}

    prompt = (
        f"Catégorie: {cat}\n"
        f"Critères: {criteria}\n"
        f"Priorités utilisateur: {buyer_priorities.strip()[:500] or 'bon rapport qualité/prix, éviter arnaques'}\n"
        f"Poids actuels: {json.dumps(cur_snip, ensure_ascii=False)}\n\n"
        "Ajuste les poids (0-3 sauf ai_blend 0-1, risk 0.5-3) pour refléter ces priorités.\n"
        "Ex: projecteur home cinema → monter projector_hdmi, projector_lumens, dinosaur_penalty; baisser discount si peu pertinent.\n"
        f'JSON: {{"weights":{{"{keys[0] if keys else "discount"}":1.0}}}} — uniquement les clés: {", ".join(keys)}\n'
    )
    try:
        raw = _chat(prompt, system="Réglage scoring marketplace. JSON uniquement.", max_tokens=600)
        obj = _extract_json(raw)
        wobj = obj.get("weights") if isinstance(obj, dict) else obj
        if not isinstance(wobj, dict):
            return {}
        result: Dict[str, float] = {}
        for k in keys:
            if k in wobj:
                try:
                    result[k] = float(wobj[k])
                except (TypeError, ValueError):
                    pass
        logger.info("IA poids suggérés: %d champs", len(result))
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("IA suggest weights: %s", exc)
        return {}


def classify_quality_tier_batch(
    category: str,
    rows: List[Dict[str, Any]],
    *,
    buyer_priorities: str = "",
    max_items: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """
    Reclasse quality_tier (gem/ok/suspicious/dropship/dinosaur) via IA.
    Projecteurs : XGIMI/Mogo=gem, clones Crossbuds/Android générique=dropship, NeoPix=ok.
    """
    if not api_key() or not rows:
        return {}

    from .engine import AI_SPECS_CHUNK, category_buyer_criteria

    dom_cat = category or "generic"
    criteria = category_buyer_criteria(dom_cat)
    priorities = (buyer_priorities or "").strip()[:500]
    out: Dict[str, Dict[str, Any]] = {}
    subset = [r for r in rows if not r.get("is_accessory")][:max_items]
    chunk_size = max(5, AI_SPECS_CHUNK)

    if dom_cat == "projector":
        tier_guide = (
            "Tiers: gem=vraie bonne affaire technique, ok=correct au prix, suspicious=doute marketing, "
            "dropship=marque blanche/clones Amazon, dinosaur=obsolète VGA/XGA/lumens faibles. "
            "XGIMI, Epson, BenQ, Optoma, Acer portable récent = gem si Full HD. "
            "Philips NeoPix/PicoPix = suspicious (LCD bas de gamme, pas gem). "
            "4K + marque inconnue + prix <300€ = dropship arnaque. "
        )
    else:
        tier_guide = (
            f"Tiers pour {dom_cat}: gem=excellent deal marque reconnue, ok=correct, "
            "suspicious=doute/état/marque faible, dropship=clone/no-name/prix impossible. "
            "iPhone/PS5/luxe à prix dérisoire sans contexte = dropship. "
            "N'utilise JAMAIS dinosaur (réservé aux vieux projecteurs VGA). "
        )

    for off in range(0, len(subset), chunk_size):
        chunk = subset[off : off + chunk_size]
        lines = []
        for r in chunk:
            lines.append(
                json.dumps(
                    {
                        "id": str(r.get("id")),
                        "title": str(r.get("title") or "")[:100],
                        "price": r.get("total_price") or r.get("price"),
                        "tier_heuristic": r.get("quality_tier"),
                        "lumens": r.get("lumens"),
                        "has_hdmi": r.get("has_hdmi"),
                        "native_aspect": r.get("native_aspect"),
                    },
                    ensure_ascii=False,
                )
            )
        prompt = (
            f"Marché: {dom_cat}\nCritères: {criteria}\nPriorités: {priorities or 'qualité/prix'}\n"
            f"{tier_guide}\n"
            "Pour chaque annonce, corrige le tier si l'heuristique se trompe.\n"
            '{"items":[{"id":"…","quality_tier":"gem|ok|suspicious|dropship|dinosaur","note":"1 phrase","flags":[]}]}\n\n'
            + "\n".join(lines)
        )
        try:
            raw = _chat(
                prompt,
                system="Expert qualité occasion — JSON strict, pas de markdown.",
                max_tokens=1800,
            )
            items = _parse_llm_items_list(raw)
            for it in items:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                iid = str(it["id"])
                qt = str(it.get("quality_tier") or "").strip().lower()
                if qt not in ("gem", "ok", "suspicious", "dropship", "dinosaur"):
                    continue
                if qt == "dinosaur" and dom_cat != "projector":
                    qt = "suspicious"
                out[iid] = {
                    "ai_quality_tier": qt,
                    "ai_quality_note": str(it.get("note") or "")[:200],
                    "ai_quality_flags": (
                        ",".join(it.get("flags") or [])
                        if isinstance(it.get("flags"), list)
                        else str(it.get("flags") or "")
                    )[:120],
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("IA quality tier chunk %s: %s", off, exc)
    logger.info("IA quality tier: %d/%d", len(out), len(subset))
    return out


def infer_market_from_priorities(
    url: str,
    priorities: str,
) -> Dict[str, Any]:
    """Devine marché + critères + questions pour l'utilisateur (multi-catégories)."""
    if not api_key():
        return {}
    from .engine import detect_market_context

    url_ctx = detect_market_context(url, buyer_priorities="")
    url_cat = str(url_ctx.get("category") or "generic")
    url_locked = bool(url_ctx.get("url_locked"))
    st = str(url_ctx.get("search_text") or "")
    prompt = (
        f"URL catalogue: {url[:200]}\n"
        f"search_text Vinted: {st!r}\n"
        f"Marché imposé par l'URL (ne pas changer): {url_cat if url_locked else 'ambigu'}\n"
        f"Priorités utilisateur (conseils scoring, pas pour changer le marché si URL claire): {priorities[:500]}\n\n"
        "Réponds JSON:\n"
        '{"category":"projector|console_ds|console_ps4|console_switch|ac|coffee|fan|apparel|generic",'
        '"label":"nom français court",'
        '"criteria":"critères pour CE marché (1 paragraphe, adapté à l\'URL)",'
        '"questions":["question optionnelle"]}\n'
        f"Si search_text décrit déjà le produit ({url_cat}), garde category={url_cat!r}. "
        "N'utilise pas d'anciennes priorités projecteur si l'URL dit 3DS/DS."
    )
    try:
        raw = _chat(prompt, system="Expert marketplace occasion. JSON uniquement.", max_tokens=500)
        obj = _extract_json(raw)
        if isinstance(obj, dict):
            if url_locked:
                obj["category"] = url_cat
            return obj
    except Exception as exc:  # noqa: BLE001
        logger.warning("infer_market: %s", exc)
    return {
        "category": url_cat,
        "label": url_cat,
        "criteria": "",
        "questions": [],
        "url_locked": url_locked,
    }


def _filter_title_chunk(search_intent: str, lines: List[str]) -> Set[str]:
    prompt = (
        f"Intent acheteur : {search_intent.strip()}\n\n"
        "Liste (id|titre). Garde UNIQUEMENT le produit principal recherché "
        "(console complète, clim, etc. — pas accessoire/jeu/manette/guide/livre seul).\n"
        "Exclure HS, pour pièces, banni PSN/playstore.\n"
        '{"keep_ids":["id1","id2"]}\n\n'
        + "\n".join(lines)
    )
    raw = _chat(
        prompt,
        system="Filtre Vinted. Réponds UNIQUEMENT un objet JSON valide, sans markdown.",
        max_tokens=800,
    )
    obj = _extract_json_lenient(raw)
    keep = obj.get("keep_ids") if isinstance(obj, dict) else []
    if not isinstance(keep, list):
        return set()
    return {str(x).strip() for x in keep if str(x).strip()}



def resolve_doubtful_titles(
    search_intent: str,
    catalog_items: List[Any],
    *,
    max_items: int = 80,
) -> Set[str]:
    """
    IA uniquement sur annonces « douteuses » (titres ambigus) — coût minimal.
    Retourne les ids à conserver parmi les douteux.
    """
    if not api_key() or not catalog_items:
        return {str(getattr(it, "id", "") or "") for it in catalog_items if getattr(it, "id", None)}

    lines: List[str] = []
    id_list: List[str] = []
    for it in catalog_items[:max_items]:
        iid = str(getattr(it, "id", "") or "")
        title = str(getattr(it, "title", "") or "").replace("\n", " ")[:80]
        if iid and title:
            lines.append(f"{iid}|{title}")
            id_list.append(iid)
    if not lines:
        return set()

    prompt = (
        f"Intent acheteur : {search_intent.strip()}\n\n"
        "Ces annonces ont un titre AMBIGU (lot, bundle, court). "
        "Garde seulement celles qui correspondent au produit principal recherché.\n"
        "Exclure accessoire seul, jeu seul, pièce, HS, ban.\n"
        '{"keep_ids":["id1"]}\n\n'
        + "\n".join(lines)
    )
    try:
        raw = _chat(
            prompt,
            system="Filtre Vinted prudent. JSON uniquement.",
            max_tokens=min(900, 80 + 25 * len(lines)),
        )
        obj = _extract_json_lenient(raw)
        keep = obj.get("keep_ids") if isinstance(obj, dict) else []
        if isinstance(keep, list):
            out = {str(x).strip() for x in keep if str(x).strip()}
            logger.info("IA douteux : %d → %d gardés", len(lines), len(out))
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("IA douteux échouée : %s — on garde les douteux", exc)
    return set(id_list)


def filter_relevant_ids(
    search_intent: str,
    items: List[Dict[str, Any]],
    *,
    max_items: int = 400,
    catalog_items: Optional[List[Any]] = None,
) -> Set[str]:
    """
    Filtre titres par IA (chunks) — legacy / repli si triage insuffisant.
    Préférer triage.py + resolve_doubtful_titles() pour limiter les coûts.
    """
    if not api_key():
        logger.info("IA titre : pas de clé → heuristique")
        if catalog_items:
            from .engine import filter_ids_heuristic

            return filter_ids_heuristic(catalog_items, search_intent)  # type: ignore[arg-type]
        return {str(it.get("id") or "") for it in items if it.get("id")}

    subset = items[:max_items]
    lines: List[str] = []
    for it in subset:
        iid = str(it.get("id") or "")
        title = str(it.get("title") or "").replace("\n", " ").replace("|", " ")[:72]
        if iid and title:
            lines.append(f"{iid}|{title}")
    if not lines:
        return set()

    out: Set[str] = set()
    from .engine import AI_TITLE_CHUNK_SIZE

    chunk_size = AI_TITLE_CHUNK_SIZE
    failed_chunks = 0
    for off in range(0, len(lines), chunk_size):
        chunk = lines[off : off + chunk_size]
        try:
            out.update(_filter_title_chunk(search_intent, chunk))
        except Exception as exc:  # noqa: BLE001
            failed_chunks += 1
            logger.warning("IA titre chunk %s échouée : %s", off, exc)

    if out:
        logger.info("IA titre : %d → %d annonces (%d chunks)", len(lines), len(out), (len(lines) + chunk_size - 1) // chunk_size)
        return out

    logger.warning("IA titre : échec total → filtre heuristique")
    if catalog_items:
        from .engine import filter_ids_heuristic

        h = filter_ids_heuristic(catalog_items, search_intent)  # type: ignore[arg-type]
        logger.info("Heuristique : %d → %d annonces", len(lines), len(h))
        return h
    return set()


def summarize_top_picks(
    search_intent: str,
    picks: List[Dict[str, Any]],
    *,
    category: str = "",
) -> str:
    """Résumé IA court du top 10 affaires (1 appel, ~10 lignes)."""
    if not api_key() or not picks:
        return ""
    lines = []
    for r in picks[:10]:
        lines.append(
            f"#{r.get('deal_pick_rank')} {r.get('total_price') or r.get('price')}€ "
            f"[{r.get('platform')}] score {r.get('rank_score') or r.get('composite_score')} — "
            f"{str(r.get('title') or '')[:65]}"
        )
    prompt = (
        f"Recherche: {search_intent[:120]}\nCatégorie: {category or 'auto'}\n\n"
        f"Top 10 affaires (qualité/prix):\n" + "\n".join(lines) + "\n\n"
        "En 5-8 lignes FR : lesquelles sont les vraies bonnes affaires, lesquelles éviter "
        "(surprix, pack incomplet), et une fourchette prix raisonnable pour CE modèle."
    )
    try:
        return _chat(
            prompt,
            system="Expert arbitrage occasion FR. Concis, factuel, pas de blabla.",
            max_tokens=320,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("summarize_top_picks: %s", exc)
        return ""


def analyze_top5_batch(
    query: str,
    category: str,
    candidates: List[Dict[str, Any]],
    eval_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Un seul appel LLM pour le Top 5 avec criteres dynamiques par categorie."""
    from .scoring import compact_specs_line, profile_for_category
    from .scoring import _fallback_top5

    if not api_key() or not candidates:
        return _fallback_top5(candidates, eval_ctx)

    cat = str(eval_ctx.get("category") or category or "generic")
    prof = profile_for_category(cat)
    med = float((eval_ctx.get("market_reference") or {}).get("median") or 0)
    p25 = float((eval_ctx.get("market_reference") or {}).get("p25") or 0)
    p75 = float((eval_ctx.get("market_reference") or {}).get("p75") or 0)
    brain = eval_ctx.get("brain") or {}
    price_stats = brain.get("price_stats") or {}
    if not p25 and price_stats.get("p25"):
        p25, p75 = float(price_stats["p25"]), float(price_stats.get("p75") or 0)
    targets = eval_ctx.get("targets") or {}
    criteria = eval_ctx.get("llm_criteria_hint") or prof.llm_criteria_hint
    dim_labels = ", ".join(d["label"] for d in (eval_ctx.get("dimensions") or [])[:8])

    lines = []
    specs_found = eval_ctx.get("specs_found") or {}
    for i, r in enumerate(candidates[:100], start=1):
        meta = r.get("_top5_meta") or {}
        specs = meta.get("specs") or specs_found.get(str(r.get("id") or ""), {})
        spec_line = compact_specs_line(specs, cat)
        lines.append(
            f"{i}. id={r.get('id')} | {r.get('total_price') or r.get('price')}EUR | "
            f"bds={r.get('best_deal_score')} | pre={meta.get('pre_score')} | specs={spec_line} | "
            f"{str(r.get('title') or '')[:60]} | flags={','.join(meta.get('flags') or [])}"
        )

    target_bits = []
    if targets.get("target_model_label"):
        target_bits.append(f"modele={targets['target_model_label']}")
    if targets.get("target_storage_gb"):
        target_bits.append(f"stockage={targets['target_storage_gb']} Go")
    if targets.get("target_frame_size"):
        target_bits.append(f"taille={targets['target_frame_size']}")

    prompt = (
        f"Recherche: {query}\nCategorie: {cat}\n"
        f"Marche decouvert — p25={p25:.0f} p50={med:.0f} p75={p75:.0f} EUR\n"
        f"Cibles: {', '.join(target_bits) or '-'}\n\n"
        f"Evalue selon les criteres pertinents pour {cat}: {criteria}\n"
        f"Dimensions: {dim_labels}.\n"
        "REGLE VETO: si is_obsolete, critical_defect ou technical_veto → NE PAS elire, meme a 1 EUR.\n"
    )
    if cat in ("console_ps4", "console_ps5"):
        prompt += (
            "CONSOLE PS4: PRIORISE FORTEMENT la completude du pack — manette(s), câbles HDMI/alim, "
            "boîte, jeux inclus (FIFA/GTA/GoW bonus). Pro > Slim > Fat. 2+ manettes = gros plus. "
            "1 To > 500 Go. N'elis PAS une console nue sans manette si une offre complete est proche en prix. "
            "Chaque pick doit citer manettes, stockage et jeux si presents.\n"
        )
    elif cat == "projector":
        prompt += (
            "PROJECTEUR: eligis UNIQUEMENT de vrais videoprojecteurs (lumens ANSI, marque/modele reconnu). "
            "N'elis JAMAIS adaptateurs HDMI sans fil, dongles Miracast/Chromecast, splitters, extenders, "
            "recepteurs HDMI wireless, cables, lampes seules, trepieds/supports seuls. "
            "Chaque pick doit citer lumens et resolution native si disponibles.\n"
        )
    else:
        prompt += (
            "PRIORISE le rapport qualite/prix (lumens ANSI, resolution native, etat, batterie si phone). "
            "N'elis PAS le moins cher si les specs sont faibles ou non verifiees. "
            "Chaque pick doit citer au moins 1 spec concrete dans highlights (lumens, resolution, batterie, etc.).\n"
            "N'invente pas de batterie pour un velo musculaire. "
            "Utilise uniquement les specs extraites ou visibles dans le tableau.\n"
        )
    prompt += (
        "\nAnnonces pre-classees par best_deal_score (top 100):\n" + "\n".join(lines[:100]) + "\n\n"
        "JSON strict uniquement:\n"
        '{"top5":[{"rank":1,"listing_id":"...","title":"...","price":0,"score":0,'
        '"verdict":"...","justification":"2-4 phrases FR","highlights":["..."],'
        '"warnings":["..."],"vs_market":"-42% vs mediane 372EUR"}],'
        '"market_summary":"...","avoid":["..."]}\n'
        f"Choisis exactement 5 annonces (listing_id des lignes). Evite: {targets.get('exclude_variants') or prof.avoid_patterns}."
    )
    try:
        raw = _chat(prompt, system=SYS_MARKET_ANALYST, max_tokens=4000)
        data = _extract_json(raw)
        if isinstance(data, dict) and isinstance(data.get("top5"), list) and data["top5"]:
            data["category"] = cat
            return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("analyze_top5_batch: %s", exc)
    return _fallback_top5(candidates, eval_ctx)


def batch_enrich_console_specs(listings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    One LLM call per batch (≤20): extract missing PS4 specs from title+description snippet.
    Returns id -> {field: value, _conf_field: 0-1}.
    """
    if not api_key() or not listings:
        return {}
    lines = []
    for r in listings:
        desc = str(r.get("description") or "")[:280]
        lines.append(
            json.dumps(
                {
                    "id": str(r.get("id") or ""),
                    "title": str(r.get("title") or "")[:100],
                    "desc": desc,
                    "known": {
                        k: v
                        for k, v in {
                            "variant": r.get("variant"),
                            "storage_gb": r.get("storage_gb"),
                            "controllers_count": r.get("controllers_count"),
                            "games_count": r.get("games_count"),
                        }.items()
                        if v not in (None, "", 0)
                    },
                },
                ensure_ascii=False,
            )
        )
    prompt = (
        "Extraction specs PS4 depuis annonces occasion FR. Ne pas inventer.\n"
        "Pour chaque id, compléter UNIQUEMENT les champs manquants ou ambigus:\n"
        "- variant: slim|pro|standard\n"
        "- storage_gb: 500 ou 1000\n"
        "- controllers_count: 0-4\n"
        "- games_count: nombre de jeux inclus avec la console\n"
        "- games_list: liste courte des jeux cités\n"
        "- premium_games: sous-ensemble parmi fifa,gta,god_of_war,last_of_us,spider_man,horizon,...\n"
        "- condition: tres bon|bon|defauts|neuf\n"
        "- has_box, has_hdmi, has_power_cable, has_headset: true/false\n"
        "JSON: {\"items\":[{\"id\":\"…\",\"variant\":\"slim\",\"storage_gb\":500,"
        "\"controllers_count\":2,\"games_count\":3,\"games_list\":[\"FIFA 23\"],"
        "\"premium_games\":[\"fifa\"],\"condition\":\"bon\",\"has_box\":true}]}\n\n"
        + "\n".join(lines)
    )
    out: Dict[str, Dict[str, Any]] = {}
    try:
        raw = _chat(prompt, system=SYS_DEAL_AUDIT, max_tokens=min(2500, 200 + 120 * len(lines)))
        data = _extract_json(raw)
        items = data.get("items") if isinstance(data, dict) else data
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                lid = str(it["id"])
                patch: Dict[str, Any] = {}
                for k in (
                    "variant",
                    "storage_gb",
                    "controllers_count",
                    "games_count",
                    "games_list",
                    "premium_games",
                    "condition",
                    "has_box",
                    "has_hdmi",
                    "has_power_cable",
                    "has_headset",
                ):
                    if k in it and it[k] not in (None, ""):
                        patch[k] = it[k]
                        patch[f"_conf_{k}"] = 0.72
                if patch:
                    out[lid] = patch
    except Exception as exc:
        logger.debug("batch_enrich_console_specs: %s", exc)
    return out


def synthesize_market_brief(
    search_intent: str,
    stats: Dict[str, Any],
    top_deals: List[Dict[str, Any]],
) -> str:
    """Synthèse IA courte (2–3 paragraphes) pour l'onglet Récap."""
    if not api_key():
        return ""

    deals_lines = []
    for r in top_deals[:6]:
        deals_lines.append(
            f"- score {r.get('final_score')} | {r.get('total_price')}€ | {str(r.get('title') or '')[:70]}"
        )
    prompt = (
        f"Recherche : {search_intent}\n"
        f"Stats : {json.dumps(stats, ensure_ascii=False)}\n"
        f"Top deals :\n" + "\n".join(deals_lines) + "\n\n"
        "Rédige une synthèse marché en français (3 courts paragraphes max) : "
        "prix typiques, meilleures opportunités, pièges à éviter. Ton direct, utile acheteur."
    )
    try:
        return _chat(prompt, system="Analyste marché Vinted. Français, concis, factuel.", max_tokens=500).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Synthèse IA échouée : %s", exc)
        return ""


# --- llm cache ---
import hashlib
import threading
import time
import unicodedata
from dataclasses import dataclass, field

_llm_cache_log = logging.getLogger("vf.llm_cache")

_CACHE_PATH = Path(os.environ.get("VF_LLM_CACHE", "brouillon/data/vf_llm_cache.json"))
_LOCK = threading.Lock()


def _normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", (title or "").lower())
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_cache_key(title: str, *, namespace: str = "cascade") -> str:
    norm = _normalize_title(title)
    h = hashlib.sha256(f"{namespace}:{norm}".encode("utf-8")).hexdigest()[:24]
    return h


@dataclass
class CostTracker:
    """Compteur tokens / coût estimé pour un run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_usd: float = 0.0
    calls: int = 0
    cache_hits: int = 0
    budget_usd: float = field(
        default_factory=lambda: float(os.environ.get("VF_LLM_BUDGET_USD", "0.05") or "0.05")
    )

    def add_usage(self, usage: Dict[str, Any], *, model: str = "") -> None:
        try:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            tt = int(usage.get("total_tokens") or pt + ct)
        except (TypeError, ValueError):
            return
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += tt
        self.calls += 1
        # Estimation grossière OpenRouter (Flash ~ $0.10/1M in, $0.40/1M out)
        self.estimated_usd += (pt * 0.10 + ct * 0.40) / 1_000_000.0

    def record_cache_hit(self) -> None:
        self.cache_hits += 1

    def budget_exceeded(self) -> bool:
        if self.budget_usd <= 0:
            return False
        return self.estimated_usd >= self.budget_usd

    def summary(self) -> str:
        return (
            f"{self.calls} appels · {self.total_tokens} tokens · "
            f"~${self.estimated_usd:.4f} · {self.cache_hits} cache"
        )


_RUN_COST = CostTracker()


def get_cost_tracker() -> CostTracker:
    return _RUN_COST


def reset_cost_tracker() -> CostTracker:
    global _RUN_COST
    _RUN_COST = CostTracker()
    return _RUN_COST


def _load_cache() -> Dict[str, Any]:
    try:
        if _CACHE_PATH.is_file():
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        _llm_cache_log.warning("Cache LLM illisible: %s", exc)
    return {"entries": {}, "meta": {"version": 1}}


def _save_cache(data: Dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError as exc:
        _llm_cache_log.warning("Cache LLM non sauvegardé: %s", exc)


def cache_get(key: str) -> Optional[Any]:
    with _LOCK:
        data = _load_cache()
        entry = (data.get("entries") or {}).get(key)
        if not entry:
            return None
        if time.time() - float(entry.get("ts") or 0) > 86400 * 14:
            return None
        return entry.get("value")


def cache_put(key: str, value: Any, *, title: str = "") -> None:
    with _LOCK:
        data = _load_cache()
        entries = data.setdefault("entries", {})
        if len(entries) > 50000:
            # Éviction simple : garder les 40k plus récents
            sorted_keys = sorted(
                entries.keys(),
                key=lambda k: float((entries[k] or {}).get("ts") or 0),
                reverse=True,
            )
            for k in sorted_keys[40000:]:
                entries.pop(k, None)
        entries[key] = {"ts": time.time(), "title": title[:120], "value": value}
        data["meta"] = {"version": 1, "updated": time.time(), "count": len(entries)}
        _save_cache(data)


def ai_discover_criteria(
    *,
    query: str,
    category_guess: str,
    listings_sample: List[Dict[str, Any]],
    market_stats: Dict[str, Any],
    cache_key: str = "",
) -> Dict[str, Any]:
    """
    One small LLM call: propose filter dimensions + extraction hints for ANY product type.
    Returns JSON dict (validated later by criteria_discovery).
    Cached by (category, query_prefix) via cache_key.
    """
    if cache_key:
        cached = cache_get(cache_key)
        if isinstance(cached, dict):
            get_cost_tracker().record_cache_hit()
            return cached

    sample_lines: List[str] = []
    for r in (listings_sample or [])[:30]:
        title = str(r.get("title") or "")[:90]
        price = r.get("total_price") or r.get("price")
        try:
            p = int(float(price)) if price is not None else 0
        except (TypeError, ValueError):
            p = 0
        if title:
            sample_lines.append(f"- {p}€ | {title}")

    stats_snip = {
        k: market_stats.get(k)
        for k in (
            "price_stats",
            "market_reference",
            "brands",
            "suspicious_clusters",
            "category",
            "label",
        )
        if market_stats.get(k) is not None
    }
    cat_low = (category_guess or "generic").strip().lower()
    hs_hint = ""
    if cat_low in ("ac", "clim", "climatisation"):
        hs_hint = (
            "\nHors-sujets fréquents à EXCLURE pour clim: compresseur de clim auto, pièces voiture "
            "(Renault/Peugeot/Citroën/Ford/VW…), pulseur/ventilateur habitacle, radiateur, "
            "recharge/clim sur place, montage seul.\n"
        )

    prompt = (
        f"Requête utilisateur: {query.strip()[:140]}\n"
        f"Catégorie devinée (peut être approximative): {category_guess or 'generic'}\n"
        f"Stats marché (échantillon): {json.dumps(stats_snip, ensure_ascii=False)[:1200]}\n\n"
        "Échantillon d'annonces (titre + prix) :\n"
        + ("\n".join(sample_lines) if sample_lines else "(vide)")
        + "\n\n"
        + hs_hint
        + "Tâche: découvre des critères de filtrage pertinents pour CE marché.\n"
        "- Propose <= 8 dimensions maximum.\n"
        "- Chaque dimension doit être extractible SANS LLM par annonce, depuis titre + mini description.\n"
        "- Si la description manque souvent, privilégie le titre.\n"
        "- N'invente pas d'options hors échantillon (sauf 'brand' depuis catalog_brands).\n"
        "- Inclure des 'hard_excludes' (patterns) pour éviter les hors-sujets fréquents.\n"
        "- Donne des hints d'extraction: regex (capture groupe 1) et/ou keywords.\n"
        "- IMPORTANT: si le marché est clim/ventilation/électroménager, propose des dimensions "
        "techniques comme BTU/kW/dB/m² avec regex simples.\n"
        "Sortie JSON strict uniquement, sans markdown:\n"
        "{\n"
        '  "category_label": "nom français court",\n'
        '  "dimensions": [\n'
        '    {"id":"...","label":"...","type":"range|select|multiselect|toggle","unit":"",\n'
        '     "options":["..."],\n'
        '     "extract":{"regex":["(...)"],"keywords":{"opt":["kw1","kw2"]}}}\n'
        "  ],\n"
        '  "hard_excludes":[{"reason":"...","patterns":["..."]}],\n'
        '  "default_filters": {},\n'
        '  "scoring_hints": {},\n'
        '  "notes": ["..."]\n'
        "}\n"
    )

    raw = _chat(prompt, system=SYS_CRITERIA_DISCOVERY, max_tokens=900)
    obj = _extract_json(raw)
    if not isinstance(obj, dict):
        raise RuntimeError("criteria_discovery: JSON non objet")
    if cache_key:
        cache_put(cache_key, obj, title=f"{category_guess}::{query[:60]}")
    return obj


# --- confidence router ---
from enum import Enum
from typing import Tuple

from .market import CatalogItem


class AIRoute(str, Enum):
    BYPASS = "bypass"
    DEEP_VERIFY = "deep_verify"
    REJECT = "reject"


def compute_heuristic_confidence(
    row: Dict[str, Any],
    *,
    market_category: str = "",
    search_intent: str = "",
) -> float:
    """Score 0–1 : certitude que l'annonce est le produit principal recherché."""
    score = 0.35
    title = str(row.get("title") or "")
    intent = (search_intent or "").lower()

    tconf = float(row.get("triage_confidence") or row.get("item_type_conf") or 0)
    if tconf >= 0.8:
        score += 0.15
    elif tconf >= 0.55:
        score += 0.08

    if row.get("triage_decision") == "keep" or "clear_title" in str(row.get("triage_reasons") or ""):
        score += 0.14
    if str(row.get("triage_decision") or "") == "keep" and float(row.get("triage_confidence") or 0) >= 0.82:
        score += 0.08
    if row.get("complete_hint") or row.get("has_box"):
        score += 0.06
    if row.get("pack_hints"):
        score += 0.04

    disc = row.get("model_vs_market_pct") or row.get("discount_vs_new_pct") or row.get("market_discount_pct")
    if disc is not None:
        try:
            d = float(disc)
            if 5 <= d <= 55:
                score += 0.12
            elif d > 55:
                score -= 0.08
        except (TypeError, ValueError):
            pass

    if intent and intent in title.lower():
        score += 0.1
    if re.search(r"(?i)\biphone\s*\d|galaxy\s*[asmz]?\d|ps[45]|nintendo\s*switch", title):
        score += 0.08

    if row.get("is_accessory"):
        score -= 0.35
    if row.get("defect_hint") or "risk:" in str(row.get("triage_reasons") or ""):
        score -= 0.12
    if str(row.get("triage_decision") or "") == "doubtful":
        score -= 0.28

    req_ds = str(row.get("required_ds_family") or "")
    fam = str(row.get("ds_model_family") or "")
    if req_ds and fam not in ("", "unknown"):
        if fam == req_ds or (req_ds == "new3ds" and fam == "new3ds_xl"):
            score += 0.18
        elif not (req_ds == "new3ds" and fam == "new3ds_xl"):
            score -= 0.42

    price = row.get("total_price") or row.get("price")
    if market_category == "phone" and price is not None:
        try:
            p = float(price)
            if p < 40:
                score -= 0.2
            elif p >= 120:
                score += 0.05
        except (TypeError, ValueError):
            pass

    if market_category == "projector":
        if re.search(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur)\b", title) and re.search(
            r"(?i)\b(epson|benq|optoma|viewsonic|sony|acer|lg|xiaomi|philips)\b", title
        ):
            score += 0.28
        elif re.search(r"(?i)\b(projecteur|projector)\b", title):
            score += 0.14

    return max(0.0, min(1.0, round(score, 3)))


def cascade_bypass_threshold(market_category: str = "") -> float:
    """Seuil confiance pour bypass cascade titre/analyse (categories a signal fort)."""
    try:
        custom = float(
            os.environ.get("VF_RULE_BYPASS", "0")
            or os.environ.get("VF_CASCADE_BYPASS_MIN", "0")
            or "0"
        )
        if custom > 0:
            return custom
    except (TypeError, ValueError):
        pass
    cat = (market_category or "").lower()
    if cat in ("console_ds", "phone", "projector") or cat.startswith("console_"):
        return 0.82
    return 0.85


def route_ai_decision(confidence: float, *, market_category: str = "") -> AIRoute:
    bypass_thr = cascade_bypass_threshold(market_category)
    if confidence >= bypass_thr:
        return AIRoute.BYPASS
    if confidence >= 0.38:
        return AIRoute.DEEP_VERIFY
    return AIRoute.REJECT


def partition_for_cascade(
    items: List[CatalogItem],
    *,
    triage_by_id: Dict[str, Any],
    search_intent: str = "",
    market_category: str = "",
) -> Tuple[List[CatalogItem], List[CatalogItem], Dict[str, float]]:
    """
    Retourne (auto_keep, needs_llm, confidence_map).
    """
    auto_keep: List[CatalogItem] = []
    needs_llm: List[CatalogItem] = []
    conf_map: Dict[str, float] = {}

    for it in items:
        sid = str(it.id)
        row: Dict[str, Any] = {
            "title": it.title,
            "price": it.price,
            "total_price": it.total_or_price,
            "is_accessory": False,
        }
        tr = triage_by_id.get(sid)
        is_doubtful = False
        if tr:
            dec = getattr(tr.decision, "value", str(tr.decision))
            row["triage_decision"] = dec
            row["triage_reasons"] = "|".join(getattr(tr, "reasons", []) or [])
            row["triage_confidence"] = getattr(tr, "confidence", 0)
            is_doubtful = dec == "doubtful"

        if is_doubtful:
            needs_llm.append(it)
            conf_map[sid] = 0.35
            continue

        if market_category == "projector":
            title = str(it.title or "")
            if re.search(r"(?i)\b(projecteur|projector|vid[eé]o\s*projecteur)\b", title) and re.search(
                r"(?i)\b(epson|benq|optoma|viewsonic|sony|acer|lg|xiaomi|philips)\b", title
            ):
                auto_keep.append(it)
                conf_map[sid] = 0.90
                continue

        conf = compute_heuristic_confidence(
            row, market_category=market_category, search_intent=search_intent
        )
        conf_map[sid] = conf
        tr_dec = str(getattr(tr, "decision", "") or getattr(tr, "value", "") if tr else "")
        if hasattr(tr, "decision"):
            tr_dec = getattr(tr.decision, "value", str(tr.decision))
        keep_thr = cascade_bypass_threshold(market_category) - 0.08
        if tr_dec == "keep" and conf >= keep_thr:
            auto_keep.append(it)
        elif route_ai_decision(conf, market_category=market_category) == AIRoute.BYPASS:
            auto_keep.append(it)
        else:
            needs_llm.append(it)

    return auto_keep, needs_llm, conf_map


def partition_rows_for_deep_verify(
    rows: List[Dict[str, Any]],
    *,
    market_category: str = "",
    search_intent: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Sépare les lignes top-K : bypass vs deep verify IA."""
    bypass: List[Dict[str, Any]] = []
    verify: List[Dict[str, Any]] = []
    for r in rows:
        conf = compute_heuristic_confidence(
            r, market_category=market_category, search_intent=search_intent
        )
        r["heuristic_confidence"] = conf
        r["ai_route"] = route_ai_decision(conf, market_category=market_category).value
        if route_ai_decision(conf, market_category=market_category) == AIRoute.BYPASS:
            bypass.append(r)
        elif route_ai_decision(conf, market_category=market_category) == AIRoute.DEEP_VERIFY:
            verify.append(r)
    return bypass, verify


# --- cascade pipeline ---
_cascade_log = logging.getLogger("vf.cascade")

CASCADE_ENABLED = os.environ.get("VF_CASCADE", "1").strip() not in ("0", "false", "no")
CASCADE_TITLE_CHUNK = int(os.environ.get("VF_CASCADE_TITLE_CHUNK", "64"))
CASCADE_TITLE_MIN_REL = int(os.environ.get("VF_CASCADE_TITLE_MIN", "32"))
CASCADE_DEAL_CHUNK = int(os.environ.get("VF_CASCADE_DEAL_CHUNK", "7"))
CASCADE_MAX_CHUNKS = int(os.environ.get("VF_CASCADE_MAX_CHUNKS", "12"))
CASCADE_SKIP_KEEP_RATE = float(os.environ.get("VF_CASCADE_SKIP_KEEP_RATE", "0.85"))


def _compact_title_line(item: Any) -> str:
    iid = str(getattr(item, "id", "") or "")
    title = str(getattr(item, "title", "") or "").replace("\n", " ").replace("|", " ")[:72]
    try:
        price = float(getattr(item, "total_or_price", None) or getattr(item, "price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    if iid and title:
        return f"{iid}|{int(price)}|{title}"
    return ""


def _cascade_keep_examples(market_category: str) -> str:
    """Exemples GARDER/EXCLURE adaptés au marché (évite le biais console)."""
    cat = (market_category or "generic").lower()
    if cat == "console_ds" or cat.endswith("_ds"):
        return (
            "GARDER: console même avec jeux/coque/housse/chargeur, bundle New 3DS+jeux, éditions limitées.\n"
            "Exclure: coque SEULE, jeu SEUL, chargeur SEUL, stylet SEUL."
        )
    if cat.startswith("console_"):
        return (
            "GARDER: console + manette/jeux/câbles, bundle complet.\n"
            "Exclure: manette SEULE, jeu SEUL, câble/chargeur SEUL."
        )
    if cat == "projector":
        return (
            "GARDER: projecteur seul ou avec télécommande/câble HDMI.\n"
            "Exclure: lampe/ampoule SEULE, support SEUL, câble SEUL, adaptateur HDMI sans fil, "
            "dongle Miracast/Chromecast, splitter/extender HDMI, récepteur wireless."
        )
    if cat == "phone":
        return (
            "GARDER: téléphone complet même avec chargeur/coque offerts si le titre COMMENCE par "
            "iPhone/Samsung/Galaxy/Pixel OU si prix ≥120€ avec modèle téléphone dans le titre.\n"
            "Exclure: titre COMMENÇANT par coque/chargeur/housse/vitre, prix <80€ pour iPhone récent, "
            "pièce détachée SEULE."
        )
    if cat == "coffee":
        return (
            "GARDER: machine à café seule ou avec porte-filtre/cuillère.\n"
            "Exclure: capsules SEULES, moulin SEUL, pichet SEUL."
        )
    if cat == "ac":
        return (
            "GARDER: climatiseur/mobile/split complet (souvent avec BTU ex. 7000/9000/12000), "
            "unité avec tuyau ou télécommande INCLUSE dans le pack.\n"
            "EXCLURE: télécommande SEULE, tuyau/kit fenêtre SEUL, filtre SEUL, "
            "vêtements (Nike Clima-FIT, Adidas Climawarm, veste Rossignol « clim »), "
            "chaussures (Pegasus Shield, Lafuma Access Clim), jouets/tanks « clim », "
            "compresseur auto, pièces voiture, location vacances, détecteur de gaz."
        )
    return (
        "GARDER: produit principal recherché (seul ou pack cohérent).\n"
        "Exclure: accessoire SEUL, pièce détachée SEULE, HS, hors-sujet."
    )


def _compact_row_line(r: Dict[str, Any]) -> str:
    """Ligne JSON compacte pour cascade deals — inclut références prix."""
    desc = str(r.get("description") or r.get("description_excerpt") or "")[:200]
    payload = {
        "id": str(r.get("id")),
        "t": str(r.get("title") or "")[:80],
        "p": r.get("total_price") or r.get("price"),
        "plat": str(r.get("platform") or "")[:12],
        "med": r.get("model_market_median_eur"),
        "refurb": r.get("ref_price_refurb_eur") or r.get("oracle_arbiter_ref_eur"),
        "neuf": r.get("ref_price_new_eur") or r.get("msrp_eur"),
        "vs_med_pct": r.get("model_vs_market_pct"),
        "seller": str(r.get("seller_username") or "")[:20],
        "d": desc,
        "v": r.get("variant") or r.get("ai_model_exact"),
        "pack": r.get("ai_completeness") or r.get("pack_score"),
    }
    return json.dumps(payload, ensure_ascii=False)


def filter_titles_cascade(
    search_intent: str,
    items: List[Any],
    *,
    market_category: str = "",
    buyer_priorities: str = "",
    max_items: int = 1000,
    market_sample: Optional[List[str]] = None,
) -> Tuple[Set[str], Dict[str, int]]:
    """
    Passe 1 IA : pertinence sur tous les titres (chunks compacts).
    Retourne (ids à garder, scores 0-100 par id).
    """
    from .engine import category_buyer_criteria

    if not api_key() or not items:
        return {str(getattr(it, "id", "") or "") for it in items if getattr(it, "id", None)}, {}

    cat = market_category or "generic"
    criteria = category_buyer_criteria(cat)[:280]
    prio = (buyer_priorities or "").strip()[:400]
    lines_all: List[str] = []
    id_set: Set[str] = set()
    for it in items[:max_items]:
        ln = _compact_title_line(it)
        if ln:
            lines_all.append(ln)
            id_set.add(ln.split("|", 1)[0])

    if not lines_all:
        return set(), {}

    keep_all: Set[str] = set()
    rel_map: Dict[str, int] = {}
    chunk = max(12, CASCADE_TITLE_CHUNK)
    cache_hits_batch = 0
    cache_checked = 0

    n_chunks = max(1, (len(lines_all) + chunk - 1) // chunk)
    tracker = get_cost_tracker()

    for off in range(0, len(lines_all), chunk):
        part = lines_all[off : off + chunk]
        chunk_i = off // chunk + 1
        if chunk_i > CASCADE_MAX_CHUNKS:
            for ln in lines_all[off:]:
                keep_all.add(ln.split("|", 1)[0])
            logger.info("Cascade titres: early exit apres %d chunks", CASCADE_MAX_CHUNKS)
            break
        uncached: List[str] = []
        for ln in part:
            pieces = ln.split("|", 2)
            title_part = pieces[2] if len(pieces) > 2 else ln
            iid = pieces[0] if pieces else ""
            ck = title_cache_key(title_part, namespace=f"cascade:{cat}")
            cached = cache_get(ck)
            if isinstance(cached, dict) and iid:
                tracker.record_cache_hit()
                cache_hits_batch += 1
                cache_checked += 1
                try:
                    rel = int(cached.get("r") or cached.get("rel") or 0)
                except (TypeError, ValueError):
                    rel = 55
                rel_map[iid] = rel
                if rel >= CASCADE_TITLE_MIN_REL:
                    keep_all.add(iid)
                continue
            uncached.append(ln)

        cache_checked += len(part)
        if cache_checked >= 24:
            hit_rate = cache_hits_batch / max(1, cache_checked)
            if hit_rate >= 0.72:
                chunk = min(128, int(chunk * 1.5))
            elif hit_rate >= 0.55:
                chunk = min(96, chunk + 16)

        if not uncached:
            logger.info(
                "  Cascade titres · lot %d/%d · %d lignes · cache 100%% · %d retenues",
                chunk_i,
                n_chunks,
                len(part),
                len(keep_all),
            )
            continue

        examples = _cascade_keep_examples(cat)
        ctx_lines = ""
        if market_sample and off == 0:
            sample = [s.strip() for s in market_sample if s and s.strip()][:28]
            if sample:
                ctx_lines = (
                    "Contexte titres déjà vus sur ce scrape (pour calibrer le marché):\n"
                    + "\n".join(f"• {t[:80]}" for t in sample)
                    + "\n\n"
                )
        prompt = (
            ctx_lines
            + f"Marché: {cat}\nRecherche: {search_intent[:200]}\n"
            f"Critères: {criteria}\nPriorités: {prio or 'bon rapport qualité/prix'}\n\n"
            "Lignes id|€|titre — garde UNIQUEMENT annonces = produit principal recherché.\n"
            f"{examples}\n"
            f'JSON: {{"keep":[{{"id":"…","r":0-100}}]}} — ids avec r>={CASCADE_TITLE_MIN_REL} seulement.\n\n'
            + "\n".join(uncached)
        )
        chunk_kept = 0
        try:
            raw = _chat(
                prompt,
                system="Filtre shopping Vinted. JSON strict, pas de prose.",
                max_tokens=min(1200, 40 + 18 * len(uncached)),
            )
            obj = _extract_json_lenient(raw)
            items_out = obj.get("keep") if isinstance(obj, dict) else []
            if isinstance(items_out, list):
                for it in items_out:
                    if not isinstance(it, dict):
                        continue
                    iid = str(it.get("id") or "").strip()
                    if not iid:
                        continue
                    try:
                        rel = int(it.get("r") or it.get("rel") or it.get("relevance") or 0)
                    except (TypeError, ValueError):
                        rel = 55
                    rel_map[iid] = rel
                    if rel >= CASCADE_TITLE_MIN_REL:
                        keep_all.add(iid)
                        chunk_kept += 1
                    for ln in uncached:
                        if ln.startswith(f"{iid}|"):
                            title_part = ln.split("|", 2)[2] if ln.count("|") >= 2 else ln
                            cache_put(
                                title_cache_key(title_part, namespace=f"cascade:{cat}"),
                                {"r": rel, "id": iid},
                                title=title_part,
                            )
                            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cascade titres chunk %s: %s", off, exc)
            for ln in uncached:
                keep_all.add(ln.split("|", 1)[0])
        else:
            if chunk_kept == 0:
                logger.warning(
                    "Cascade titres lot %d/%d: JSON valide mais 0 retenues — garde-fou lot",
                    chunk_i,
                    n_chunks,
                )
                for ln in uncached:
                    keep_all.add(ln.split("|", 1)[0])
            logger.info(
                "  Cascade titres · lot %d/%d · %d lignes (%d cache) · %d retenues cumulées",
                chunk_i,
                n_chunks,
                len(part),
                len(part) - len(uncached),
                len(keep_all),
            )

    if not keep_all:
        logger.warning("Cascade titres vide — repli garde-fou heuristique")
        return id_set, rel_map

    logger.info("Cascade titres: %d → %d (%d chunks)", len(lines_all), len(keep_all), (len(lines_all) + chunk - 1) // chunk)
    return keep_all, rel_map


def clarify_doubtful_titles(
    search_intent: str,
    doubtful_items: List[Any],
    triage_by_id: Dict[str, Any],
    *,
    market_category: str = "",
    max_items: int = 36,
) -> Dict[str, Dict[str, Any]]:
    """
    Passe IA courte sur les doutes heuristiques — raison FR + keep/reject.
    Retourne {id: {decision, reason, r}}.
    """
    from .engine import explain_triage_reasons

    if not doubtful_items or not api_key():
        if not doubtful_items:
            return {}
        # Repli hors-ligne : heuristique stricte sur les doutes (0 token).
        from .engine import explain_triage_reasons

        out_off: Dict[str, Dict[str, Any]] = {}
        for it in doubtful_items[:max_items]:
            sid = str(getattr(it, "id", "") or "")
            title = str(getattr(it, "title", "") or "")
            tr = triage_by_id.get(sid)
            reasons = "|".join(getattr(tr, "reasons", []) or []) if tr else ""
            reason_fr = explain_triage_reasons(reasons) if reasons else ""
            dec = "doubtful"
            if market_category in ("ac", "clim", "climatisation"):
                try:
                    from .engine import _ac_off_category_title
                    from .scoring import ac_has_unit_signal

                    if _ac_off_category_title(title):
                        dec = "reject"
                        reason_fr = reason_fr or "hors-sujet clim (vêtement/accessoire/auto)"
                    elif ac_has_unit_signal(title):
                        dec = "keep"
                        reason_fr = reason_fr or "unité clim détectée (BTU/split/mobile)"
                except Exception:
                    pass
            elif str(market_category or "").startswith("console_"):
                if re.search(r"(?i)\b(manette|jeu)\s+seul\b", title) and not re.search(
                    r"(?i)\bconsole\b", title
                ):
                    dec = "reject"
                    reason_fr = reason_fr or "accessoire ou jeu seul"
                elif re.search(r"(?i)\b(ps4|playstation\s*4|console)\b", title):
                    dec = "keep"
                    reason_fr = reason_fr or "console PS4 probable"
            out_off[sid] = {"decision": dec, "reason": reason_fr[:120], "r": 72 if dec == "keep" else 35}
        return out_off
    batch = doubtful_items[: max(1, int(max_items))]
    lines: List[str] = []
    for it in batch:
        sid = str(getattr(it, "id", "") or "")
        tr = triage_by_id.get(sid)
        reasons = ""
        if tr:
            reasons = explain_triage_reasons("|".join(getattr(tr, "reasons", []) or []))
        price = getattr(it, "total_or_price", None) or getattr(it, "price", None) or ""
        title = str(getattr(it, "title", "") or "")[:90]
        lines.append(f"{sid}|{price}|{title}|{reasons[:100]}")
    if not lines:
        return {}
    cat = market_category or "generic"
    prompt = (
        f"Recherche: {search_intent[:180]}\nMarché: {cat}\n"
        "Annonces marquées DOUTE par heuristique — arbitre avec le CONTEXTE recherche.\n"
        "Décide si c'est le bon produit, un accessoire, ou hors-sujet.\n"
        'JSON: {"items":[{"id":"…","decision":"keep|reject|doubtful","reason":"phrase FR courte","r":0-100}]}\n\n'
        + "\n".join(lines)
    )
    out: Dict[str, Dict[str, Any]] = {}
    try:
        raw = _chat(prompt, system=SYS_TITLE_FILTER, max_tokens=min(900, 30 + 22 * len(lines)))
        obj = _extract_json_lenient(raw)
        items_out = obj.get("items") if isinstance(obj, dict) else []
        if isinstance(items_out, list):
            for it in items_out:
                if not isinstance(it, dict):
                    continue
                sid = str(it.get("id") or "").strip()
                if not sid:
                    continue
                dec = str(it.get("decision") or "doubtful").lower()
                if dec not in ("keep", "reject", "doubtful"):
                    dec = "doubtful"
                try:
                    rel = int(it.get("r") or it.get("rel") or 50)
                except (TypeError, ValueError):
                    rel = 50
                reason = str(it.get("reason") or "").strip()
                out[sid] = {"decision": dec, "reason": reason, "r": rel}
    except Exception as exc:  # noqa: BLE001
        logger.warning("clarify_doubtful_titles: %s", exc)
    return out


def analyze_top_deals_cascade(
    search_intent: str,
    rows: List[Dict[str, Any]],
    *,
    category: str = "",
    buyer_priorities: str = "",
    max_items: int = 30,
) -> Dict[str, Dict[str, Any]]:
    """
    Passe 2 IA fusionnée : specs + adéquation + score deal + flags (1 appel / chunk).
    Remplace extract_specs + buyer_fit + deep_verify.
    """
    from .engine import category_buyer_criteria

    if not api_key() or not rows:
        return {}

    dom_cat = category or str(rows[0].get("item_type") or "") if rows else "generic"
    criteria = category_buyer_criteria(dom_cat)
    priorities = (buyer_priorities or search_intent).strip()[:500]
    out: Dict[str, Dict[str, Any]] = {}
    subset = rows[:max_items]
    tracker = get_cost_tracker()
    n_chunks = 0

    # Lots dynamiques : haute confiance → chunks plus gros (moins d'appels)
    buckets: List[List[Dict[str, Any]]] = [[], []]
    for r in subset:
        try:
            conf = float(r.get("heuristic_confidence") or 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        buckets[0 if conf >= 0.82 else 1].append(r)

    work: List[Tuple[int, List[Dict[str, Any]]]] = []
    if buckets[0]:
        sz = max(8, min(12, CASCADE_DEAL_CHUNK + 4))
        for off in range(0, len(buckets[0]), sz):
            work.append((sz, buckets[0][off : off + sz]))
    if buckets[1]:
        sz = max(4, CASCADE_DEAL_CHUNK)
        for off in range(0, len(buckets[1]), sz):
            work.append((sz, buckets[1][off : off + sz]))
    n_chunks = len(work) or 1

    for chunk_i, (_sz, chunk) in enumerate(work, start=1):
        if tracker.budget_exceeded():
            _cascade_log.warning("Budget IA atteint — cascade deals arrêtée à %d/%d", len(out), len(subset))
            break
        lines = [_compact_row_line(r) for r in chunk if r.get("id")]
        if not lines:
            continue
        prompt = (
            f"Intent: {search_intent[:160]}\nCatégorie: {dom_cat}\n"
            f"Critères acheteur: {criteria[:320]}\nPriorités: {priorities[:280]}\n\n"
            "Pour chaque ligne JSON, évalue l'AFFAIRE (pas juste la description).\n"
            "Prix: compare p vs med (médiane run), refurb (Back Market), neuf si présents.\n"
            "- relevance 0-100 : bon deal = sous médiane + bon état/pack ; surprix = <55\n"
            "- fit_score / fit_grade A-F / notes courtes\n"
            "- ai_completeness 0-100 (chargeur, stylet, jeux, boîte selon marché)\n"
            "- ai_model_exact, ai_bundle, flags[], verdict 1 phrase\n"
        )
        if dom_cat in ("console_ps4", "console_ps5"):
            prompt += (
                "PS4: extraire controllers_count, games_list, variant slim/pro, storage_gb 500/1000, "
                "condition, has_box, has_hdmi, has_power_cable. Completeness = manette(s)+câbles+jeux. "
                "2+ manettes et jeux premium (FIFA/GTA/GoW) = completeness élevée.\n"
            )
        prompt += (
            "Pénaliser: mauvais modèle, accessoire seul, HS, pack incomplet, surprix vs med.\n"
            '{"items":[{"id":"…","relevance":0,"fit_score":0,"fit_grade":"C","notes":"…",'
            '"ai_model_exact":"…","ai_completeness":0,"controllers_count":0,"games_count":0,'
            '"variant":"slim","storage_gb":500,"flags":[],"verdict":"…"}]}\n\n'
            + "\n".join(lines)
        )
        try:
            raw = _chat(
                prompt,
                system=SYS_DEAL_AUDIT,
                max_tokens=min(2200, 400 + 180 * len(lines)),
            )
            items = _parse_llm_items_list(raw)
            for it in items:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                iid = str(it["id"])
                flags = it.get("flags") or []
                flags_s = ",".join(flags) if isinstance(flags, list) else str(flags or "")
                try:
                    rel = float(it.get("relevance") or it.get("fit_score") or 0)
                except (TypeError, ValueError):
                    rel = 0.0
                try:
                    fit = float(it.get("fit_score") or rel)
                except (TypeError, ValueError):
                    fit = rel
                out[iid] = {
                    "ai_relevance": int(max(0, min(100, rel))),
                    "ai_verdict": str(it.get("verdict") or "")[:200],
                    "ai_flags": flags_s[:120],
                    "ai_msrp_eur": it.get("msrp_eur"),
                    "buyer_fit_score": max(0.0, min(100.0, fit)),
                    "buyer_fit_grade": str(it.get("fit_grade") or "")[:2],
                    "buyer_fit_notes": str(it.get("notes") or "")[:240],
                    "buyer_fit_flags": flags_s[:120],
                    "ai_model_exact": str(it.get("ai_model_exact") or it.get("model") or "")[:80],
                    "ai_completeness": int(it.get("ai_completeness") or 0),
                    "ai_bundle": str(it.get("ai_bundle") or "")[:120],
                    "ai_spec_flags": flags_s[:80],
                }
                if it.get("ai_variant"):
                    out[iid]["ai_variant"] = str(it["ai_variant"])
                for bk in (
                    "has_hdmi",
                    "has_controller",
                    "has_stylus",
                    "includes_games",
                    "has_power",
                    "has_box",
                    "ansi_lumens",
                    "native_aspect",
                    "usable_for_home",
                    "legacy_ports_only",
                    "btu",
                    "controllers_count",
                    "games_count",
                    "storage_gb",
                    "variant",
                    "games_list",
                    "premium_games",
                    "condition",
                    "has_headset",
                ):
                    if bk in it and it[bk] is not None:
                        out[iid][bk] = it[bk]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cascade analyse chunk %s: %s", off, exc)
        else:
            logger.info(
                "  Cascade analyse · lot %d/%d · %d offres · %d analysées cumulées",
                chunk_i,
                n_chunks,
                len(chunk),
                len(out),
            )

    logger.info("Cascade analyse top: %d/%d", len(out), len(subset))
    return out


def apply_cascade_analysis(
    row: Dict[str, Any],
    info: Dict[str, Any],
    *,
    ai_blend: float = 0.55,
) -> None:
    """Fusionne le résultat cascade dans une ligne (pack + overlay IA)."""

    if not info:
        return
    for k, v in info.items():
        if k.startswith("ai_") or k.startswith("buyer_") or k in (
            "has_hdmi",
            "has_controller",
            "has_stylus",
            "includes_games",
            "has_power",
            "has_power_cable",
            "has_box",
            "ansi_lumens",
            "native_aspect",
            "usable_for_home",
            "legacy_ports_only",
            "btu",
            "controllers_count",
            "controller_count",
            "games_count",
            "storage_gb",
            "variant",
            "games_list",
            "premium_games",
            "condition",
            "has_headset",
        ):
            row[k] = v
    if info.get("has_power") is True:
        row["has_power_cable"] = True
    if info.get("ai_variant"):
        row["variant"] = str(info["ai_variant"])
    if info.get("ansi_lumens"):
        try:
            row["lumens"] = int(info["ansi_lumens"])
        except (TypeError, ValueError):
            pass
    if info.get("has_hdmi") is False:
        row["missing_hdmi"] = True
    comp = int(info.get("ai_completeness") or 0)
    if comp:
        row["ai_specs_summary"] = (
            f"{info.get('ai_model_exact') or '—'} · complétude {comp}% · {info.get('ai_bundle') or ''}"
        )[:220]
    rel = float(info.get("ai_relevance") or 0)
    row["ai_relevance"] = rel
    if row.get("score_base") is None:
        row["score_base"] = float(row.get("final_score") or 0.0)
    try:
        base_m = float(row.get("score_base") or 0.0)
        overlay = float(row.get("score_overlay") or 0.0)
    except (TypeError, ValueError):
        base_m, overlay = 0.0, 0.0
    old_eff = base_m + overlay
    new_eff = round((1.0 - ai_blend) * old_eff + ai_blend * rel, 2)
    row["score_overlay"] = new_eff - base_m
    sf = str(info.get("ai_spec_flags") or info.get("ai_flags") or "").lower()
    if any(x in sf for x in ("wrong_model", "accessory", "banned", "ban", "accessory_only")):
        row["score_overlay"] = min(float(row.get("score_overlay") or 0), 28.0 - base_m)
    elif comp >= 80:
        row["score_overlay"] = float(row.get("score_overlay") or 0) + 8.0
    elif comp and comp < 35:
        row["score_overlay"] = float(row.get("score_overlay") or 0) - 14.0
    sf_low = str(info.get("ai_bundle") or info.get("ai_verdict") or "").lower()
    if any(x in sf_low for x in ("sans chargeur", "no charger", "chargeur non", "missing charger")):
        row["missing_charger"] = True
        row["score_overlay"] = float(row.get("score_overlay") or 0) - 12.0
    if any(x in sf_low for x in ("chargeur", "charger", "alimentation")) and not any(
        x in sf_low for x in ("sans chargeur", "no charger", "non inclus")
    ):
        row["has_power_cable"] = True
    flags_l = str(info.get("ai_flags") or "").lower()
    if any(x in flags_l for x in ("dinosaur", "no_hdmi", "low_lumens", "dinosaur_projector")):
        row["msrp_eur"] = 0
        row["discount_vs_new_pct"] = None
    elif info.get("ai_msrp_eur"):
        try:
            msrp_v = float(info["ai_msrp_eur"])
            if msrp_v > 0:
                row["msrp_eur"] = msrp_v
                from .engine import discount_vs_new_pct

                dnew = discount_vs_new_pct(row)
                if dnew is not None:
                    row["discount_vs_new_pct"] = dnew
        except (TypeError, ValueError):
            pass
    from .engine import reconcile_pack_signals

    reconcile_pack_signals(row)
