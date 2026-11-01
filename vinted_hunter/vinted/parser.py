"""Turn Vinted JSON / HTML payloads into domain models.

Parsing is deliberately defensive: Vinted changes its payloads regularly, so every
field is optional and unknown shapes degrade to `None` instead of crashing.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Condition, Listing, Seller

_ITEM_ID_RE = re.compile(r"/items/(\d+)")
_LD_JSON_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_META_RE = re.compile(r'<meta[^>]+(?:property|name)="([^"]+)"[^>]+content="([^"]*)"', re.S)


_UNITS = {"seconde": 1 / 60, "minute": 1, "min": 1, "heure": 60, "h": 60, "jour": 1440, "semaine": 10080,
          "mois": 43200, "an": 525600, "second": 1 / 60, "hour": 60, "day": 1440, "week": 10080, "month": 43200,
          "year": 525600}
_REL_RE = re.compile(r"(une?|a|an|\d+)\s+(seconde|minute|min|heure|jour|semaine|mois|an|second|hour|day|week|month|year)s?",
                     re.I)
_UPLOAD_RE = re.compile(r'\\"code\\":\\"upload_date\\",\\"data\\":\{[^}]*?\\"value\\":\\"([^\\"]+)\\"')
_LAST_SEEN_RE = re.compile(r'\\"key\\":\\"last-logged-in\\",\\"text\\":\\"([^\\"]+)\\"')
_STATUS_ATTR_RE = re.compile(r'\\"code\\":\\"status\\",\\"data\\":\{[^}]*?\\"value\\":\\"([^\\"]+)\\"')


# Item pages without JSON-LD: read the page's embedded (doubly escaped) data stream instead.
_RSC_STR = r'((?:[^\\]|\\.)*?)'
_RSC_DESC_RE = re.compile(r'\\"data\\":\{\\"description\\":\\"' + _RSC_STR + r'\\",\\"is_expanded')
_RSC_PRICE_RE = re.compile(r'\\"price\\":\{\\"amount\\":\\"([\d.]+)\\",\\"currency_code\\":\\"(\w+)\\"\},'
                           r'\\"title\\":\\"' + _RSC_STR + r'\\"')
_RSC_BRAND_RE = re.compile(r'\\"brand_dto\\":\{[^}]*?\\"title\\":\\"([^\\"]+)\\"')


def _rsc_text(raw: str) -> str:
    """Undo the double JSON escaping of strings embedded in the page's data stream."""
    try:
        return json.loads('"' + json.loads('"' + raw + '"') + '"')
    except (json.JSONDecodeError, TypeError):
        return raw


_AVAIL_RE = re.compile(r'"availability"\s*:\s*"(?:https?://schema\.org/)?(\w+)"')
_FLAG_RE = r'\\"{}\\":(true|false)'


def item_status(page: str) -> tuple[str, float | None]:
    """'active' | 'reserved' | 'sold' from a public item page, plus its current price."""
    price = None
    for block in _LD_JSON_RE.findall(page):
        m = re.search(r'"price"\s*:\s*"?([\d.]+)', block)
        if m:
            price = float(m.group(1))
    if price is None and (m := _RSC_PRICE_RE.search(page)):
        price = float(m.group(1))

    def flag(name: str) -> bool | None:
        m = re.search(_FLAG_RE.format(name), page)
        return None if not m else m.group(1) == "true"

    avail = _AVAIL_RE.search(page)
    if (avail and avail.group(1).lower() in ("soldout", "outofstock", "discontinued")) or flag("is_closed"):
        return "sold", price
    if flag("is_reserved"):
        return "reserved", price
    return "active", price


def parse_relative(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Il y a une heure' / 'il y a 3 jours' / 'à l'instant' -> absolute UTC datetime."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    low = text.lower()
    if "instant" in low or "en ligne" in low or "just now" in low:
        return now
    m = _REL_RE.search(low)
    if not m:
        return None
    qty = 1 if m.group(1) in ("un", "une", "a", "an") else int(m.group(1))
    return now - timedelta(minutes=qty * _UNITS[m.group(2)])


def item_id_from_url(url: str) -> int | None:
    m = _ITEM_ID_RE.search(url)
    return int(m.group(1)) if m else None


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _photo_url(photo: dict | None) -> str | None:
    if not photo:
        return None
    # Prefer a mid-size thumbnail (good enough for vision, cheap in tokens)
    for thumb in photo.get("thumbnails") or []:
        if thumb.get("type") in ("thumb800", "thumb_800", "f800") and thumb.get("url"):
            return thumb["url"]
    return photo.get("url") or photo.get("full_size_url")


def _ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def parse_seller(user: dict | None) -> Seller:
    user = user or {}
    rep = user.get("feedback_reputation")
    # Vinted reports reputation 0 for accounts without any review: that's "unknown", not 0 stars.
    rating = round(float(rep) * 5, 2) if isinstance(rep, (int, float)) and user.get("feedback_count") else None
    return Seller(
        id=int(user.get("id") or 0),
        login=user.get("login") or "",
        business=bool(user.get("business")),
        rating=rating,
        reviews=user.get("feedback_count"),
        items_listed=user.get("item_count"),
        on_holiday=bool(user.get("is_on_holiday")),
        country=user.get("country_title") or user.get("country_code"),
        last_seen_at=_ts(user.get("last_loged_on_ts") or user.get("last_logged_on_ts")),
    )


def parse_catalog_item(raw: dict, domain: str) -> Listing | None:
    """Parse one entry of `/api/v2/catalog/items`."""
    try:
        item_id = int(raw["id"])
        price = _money(raw.get("price"))
        if price is None:
            return None
    except (KeyError, TypeError, ValueError):
        return None

    photos = [u for u in (_photo_url(p) for p in raw.get("photos") or []) if u]
    main = _photo_url(raw.get("photo"))
    if main and main not in photos:
        photos.insert(0, main)

    # The main photo's high-res timestamp is the most reliable "posted at" signal in catalog payloads.
    posted = None
    hr = (raw.get("photo") or {}).get("high_resolution") or {}
    posted = _ts(hr.get("timestamp"))

    url = raw.get("url") or f"https://{domain}/items/{item_id}"
    if url.startswith("/"):
        url = f"https://{domain}{url}"

    return Listing(
        id=item_id,
        title=(raw.get("title") or "").strip(),
        price=price,
        currency=(raw.get("price") or {}).get("currency_code", "EUR") if isinstance(raw.get("price"), dict) else "EUR",
        total_price=_money(raw.get("total_item_price")),
        brand=raw.get("brand_title") or None,
        size=raw.get("size_title") or None,
        condition=Condition.parse(raw.get("status_id") or raw.get("status")),
        url=url,
        photos=photos,
        favourites=int(raw.get("favourite_count") or 0),
        views=int(raw.get("view_count") or 0),
        promoted=bool(raw.get("promoted")),
        seller=parse_seller(raw.get("user")),
        posted_at=posted,
        category_id=raw.get("catalog_id"),
    )


def merge_item_details(listing: Listing, payload: dict) -> Listing:
    """Merge `/api/v2/items/{id}/details`-like payloads (shape varies) into a listing."""
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    update: dict[str, Any] = {"details_loaded": True}
    if desc := item.get("description"):
        update["description"] = str(desc).strip()
    if item.get("photos"):
        photos = [u for u in (_photo_url(p) for p in item["photos"]) if u]
        if photos:
            update["photos"] = photos
    if (cond := item.get("status_id") or item.get("status")) and listing.condition is Condition.UNKNOWN:
        update["condition"] = Condition.parse(cond)
    if posted := _ts(item.get("created_at_ts") or item.get("created_at")):
        update["posted_at"] = posted
    if isinstance(item.get("user"), dict) and item["user"].get("feedback_reputation") is not None:
        update["seller"] = parse_seller(item["user"])
    for key, field in (("favourite_count", "favourites"), ("view_count", "views")):
        if isinstance(item.get(key), int):
            update[field] = item[key]
    return listing.model_copy(update=update)


# ---------------------------------------------------------------- HTML pages
# The catalog grid is server-rendered. Each item's overlay link carries a summary:
#   title="Contax T2, Marque: contax, Taille: M, État: Bon état, 700.00 €, 735.70 €"
_GRID_ITEM_RE = re.compile(r'data-testid="product-item-id-(\d+)"')
_GRID_LINK_RE = re.compile(r'<a href="(/items/\d+[^"]*)"[^>]*--overlay-link" title="([^"]*)"')
_GRID_IMG_RE = re.compile(r'<img src="([^"]+)"')
_GRID_FAV_RE = re.compile(r'favourite-count-text">(\d+)<')
_PRICE_RE = re.compile(r"(\d[\d\s \xa0]*(?:[.,]\d{1,2})?)\s*€")
_FIELD_RE = re.compile(r", (Marque|Taille|État|Brand|Size|Condition): ([^,]+)")
_PHOTO_RE = re.compile(r"https://images\d*\.vinted\.net/t/[^\"'\\\s]+/f800/[^\"'\\\s]+")
_SELLER_RE = re.compile(
    r'\\"business\\":(true|false),\\"feedback_count\\":(\d+),\\"feedback_reputation\\":([\d.]+)'
    r'.{0,400}?\\"name\\":\\"([^\\"]+)\\"', re.S)


def _price_text(raw: str) -> float | None:
    raw = re.sub(r"[\s \xa0]", "", raw).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_grid_summary(summary: str) -> dict[str, Any]:
    """'Title, Marque: X, État: Y, 45.00 €, 47.95 €' -> fields. The title itself may contain commas."""
    summary = html.unescape(summary)
    prices = [p for p in (_price_text(m) for m in _PRICE_RE.findall(summary)) if p is not None]
    fields = {k: v.strip() for k, v in _FIELD_RE.findall(summary)}
    cut = len(summary)
    for m in (_FIELD_RE.search(summary), _PRICE_RE.search(summary)):
        if m:
            cut = min(cut, m.start())
    title = summary[:cut].rstrip(" ,")
    return {
        "title": title,
        "brand": fields.get("Marque") or fields.get("Brand"),
        "size": fields.get("Taille") or fields.get("Size"),
        "condition": fields.get("État") or fields.get("Condition"),
        "price": prices[0] if prices else None,
        "total": prices[1] if len(prices) > 1 else None,
    }


def parse_catalog_html(page: str, domain: str) -> list[Listing]:
    """Parse the server-rendered /catalog search page."""
    starts = [(m.start(), int(m.group(1))) for m in _GRID_ITEM_RE.finditer(page)]
    out: list[Listing] = []
    seen: set[int] = set()
    for idx, (pos, item_id) in enumerate(starts):
        if item_id in seen:
            continue
        end = starts[idx + 1][0] if idx + 1 < len(starts) else pos + 20000
        block = page[pos:end]
        link = _GRID_LINK_RE.search(block)
        if not link:
            continue
        info = parse_grid_summary(link.group(2))
        if info["price"] is None or not info["title"]:
            continue
        seen.add(item_id)
        img = _GRID_IMG_RE.search(block)
        fav = _GRID_FAV_RE.search(block)
        out.append(Listing(
            id=item_id,
            title=info["title"],
            price=info["price"],
            total_price=info["total"],
            brand=info["brand"],
            size=info["size"],
            condition=Condition.parse(info["condition"]),
            url=f"https://{domain}{link.group(1).split('?')[0]}",
            photos=[html.unescape(img.group(1))] if img else [],
            favourites=int(fav.group(1)) if fav else 0,
            seller=Seller(id=0),
        ))
    return out


def parse_item_page(page: str, listing: Listing | None = None, domain: str = "www.vinted.fr",
                    item_id: int | None = None) -> Listing | None:
    """Extract description, photos, price and seller from a public item page (JSON-LD + embedded data)."""
    update: dict[str, Any] = {"details_loaded": True}
    product: dict = {}
    for block in _LD_JSON_RE.findall(page):
        try:
            data = json.loads(html.unescape(block))
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("@type") == "Product":
                product = node
    if product.get("description"):
        update["description"] = str(product["description"]).strip()
    elif m := _RSC_DESC_RE.search(page):
        update["description"] = _rsc_text(m.group(1)).strip()
    else:
        metas = {k: html.unescape(v) for k, v in _META_RE.findall(page)}
        if desc := metas.get("og:description") or metas.get("description"):
            update["description"] = desc.strip()

    photos = list(dict.fromkeys(_PHOTO_RE.findall(page)))
    image = product.get("image")
    if isinstance(image, str):
        photos = [image] + [p for p in photos if p != image]
    elif isinstance(image, list):
        photos = [str(i) for i in image] + [p for p in photos if p not in image]
    if photos:
        update["photos"] = photos[:10]

    if m := _SELLER_RE.search(page):
        reviews = int(m.group(2))
        seen = _LAST_SEEN_RE.search(page)
        update["seller"] = Seller(id=listing.seller.id if listing else 0, login=m.group(4),
                                  business=m.group(1) == "true", reviews=reviews,
                                  rating=round(float(m.group(3)) * 5, 2) if reviews else None,
                                  last_seen_at=parse_relative(seen.group(1)) if seen else None)
    if (up := _UPLOAD_RE.search(page)) and (posted := parse_relative(up.group(1))):
        update["posted_at"] = posted

    if listing is not None:
        return listing.model_copy(update=update)

    # Building from scratch (/analyze <url>)
    offer = product.get("offers") or {}
    price, name = _money(offer.get("price")), product.get("name")
    if (price is None or not name) and (m := _RSC_PRICE_RE.search(page)):
        price, name = float(m.group(1)), _rsc_text(m.group(3))
    if not name or price is None:
        return None
    brand = product.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    if not brand and (b := _RSC_BRAND_RE.search(page)):
        brand = b.group(1)
    return Listing(
        id=item_id or 0,
        title=name,
        price=price,
        brand=brand,
        url=offer.get("url") or f"https://{domain}/items/{item_id}",
        condition=Condition.parse(_condition_from_page(page)),
        **{"seller": Seller(id=0), **update},
    )


def _condition_from_page(page: str) -> str | None:
    m = _STATUS_ATTR_RE.search(page)
    return m.group(1) if m else None


def parse_user(payload: dict) -> Seller | None:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    if not user or not user.get("id"):
        return None
    return parse_seller(user)
