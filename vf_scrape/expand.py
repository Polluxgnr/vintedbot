from __future__ import annotations

import os
from typing import List

_EXTRA_BY_CAT: dict[str, list[str]] = {
    "console_ds": [
        "New Nintendo 3DS XL",
        "New 3DS XL",
        "nintendo 3ds xl new",
        "3ds xl new",
    ],
    "console_ps4": [
        "PlayStation 4",
        "Playstation 4",
        "PS4 Slim",
        "PS4 Pro",
        "Sony PS4",
    ],
    "console_ps5": ["PlayStation 5", "PS5 Digital", "PS5 Slim"],
    "console_switch": ["Nintendo Switch OLED", "Switch OLED"],
    "projector": [
        "videoprojecteur",
        "vidéoprojecteur",
        "projecteur video",
        "home cinema",
        "projecteur epson",
        "projecteur benq",
        "mini projecteur",
    ],
    "phone": [],
}

# Alias complémentaires quand l'API Wallapop ne matche pas le terme exact
_WALLAPOP_FALLBACK: dict[str, list[str]] = {
    "projector": ["home cinema", "epson", "benq", "optoma", "cinema"],
}


def expand_queries(
    query: str,
    *,
    platform: str,
    category_hint: str = "",
    limit: int = 0,
) -> List[str]:
    """Alias catalogue + modèles VF pour une plateforme."""
    q = (query or "").strip()
    cat = (category_hint or "").lower()
    seen: set[str] = set()
    out: list[str] = []

    def add(t: str) -> None:
        tl = t.strip().lower()
        if tl and tl not in seen:
            seen.add(tl)
            out.append(t.strip())

    add(q)
    if platform == "wallapop":
        for t in _WALLAPOP_FALLBACK.get(cat, []):
            add(t)
    try:
        from vf.models import find_model_by_text, search_aliases

        m = find_model_by_text(q, category_hint)
        if m:
            for t in search_aliases(m.key, platform):
                add(t)
    except Exception:
        pass
    for t in _EXTRA_BY_CAT.get(cat, []):
        add(t)

    from .config import profile

    prof = profile(platform)
    default_alias = {"wallapop": "6", "rakuten": "4", "cashexpress": "4"}.get(platform, "3")
    cap = limit or int(os.environ.get(prof.alias_max_env, default_alias) or default_alias)
    return out[:cap]


def title_matches_category(title: str, category_hint: str) -> bool:
    """Filtre léger quand la requête est volontairement large (ex. home cinema → projecteur)."""
    cat = (category_hint or "").lower()
    t = (title or "").lower()
    if not cat or cat == "generic" or len(t) < 4:
        return True
    if cat == "projector":
        keys = (
            "projecteur",
            "projector",
            "vidéo",
            "video",
            "epson",
            "benq",
            "optoma",
            "viewsonic",
            "acer",
            "nec ",
            "cinema",
            "home cin",
            "beamer",
            "dlp",
            "lcd",
        )
        return any(k in t for k in keys)
    return True
