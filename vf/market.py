"""Accès marché : adapters multi-plateforme, scrape catalogue, résilience HTTP."""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Dict, Optional, TypeVar

import requests

logger = logging.getLogger("vf.http_resilience")

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitOpenError(RuntimeError):
    """Plateforme temporairement coupée après trop d'échecs."""

    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"Circuit « {name} » ouvert — réessai dans {retry_after:.0f}s")


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 300.0
    half_open_max: int = 2
    _failures: int = 0
    _opened_at: float = 0.0
    _state: str = "closed"  # closed | open | half_open
    _half_open_trials: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def before_call(self) -> None:
        with self._lock:
            if self._state == "open":
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.recovery_timeout:
                    self._state = "half_open"
                    self._half_open_trials = 0
                    logger.info("Circuit %s → half-open (probe)", self.name)
                else:
                    raise CircuitOpenError(self.name, self.recovery_timeout - elapsed)
            if self._state == "half_open" and self._half_open_trials >= self.half_open_max:
                raise CircuitOpenError(self.name, self.recovery_timeout)

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state != "closed":
                logger.info("Circuit %s → fermé (succès)", self.name)
            self._state = "closed"
            self._half_open_trials = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._half_open_trials += 1
            self._failures += 1
            if self._failures >= self.failure_threshold and self._state != "open":
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit %s OUVERT après %d échecs (pause %.0fs)",
                    self.name,
                    self._failures,
                    self.recovery_timeout,
                )

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._state == "open"


_CIRCUITS: Dict[str, CircuitBreaker] = {}
_CIRCUIT_LOCK = Lock()


def get_circuit(name: str, **kwargs: Any) -> CircuitBreaker:
    key = (name or "default").strip().lower()
    with _CIRCUIT_LOCK:
        cb = _CIRCUITS.get(key)
        if cb is None:
            failure_threshold = int(
                os.environ.get(f"VF_CB_FAIL_{key.upper()}", os.environ.get("VF_CB_FAIL", "5")) or "5"
            )
            recovery_timeout = float(
                os.environ.get(f"VF_CB_COOLDOWN_{key.upper()}", os.environ.get("VF_CB_COOLDOWN", "300")) or "300"
            )
            cb = CircuitBreaker(
                key,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                **kwargs,
            )
            _CIRCUITS[key] = cb
        return cb


def circuit_status() -> Dict[str, Dict[str, Any]]:
    with _CIRCUIT_LOCK:
        return {
            name: {
                "state": cb._state,
                "failures": cb._failures,
                "is_open": cb.is_open,
            }
            for name, cb in _CIRCUITS.items()
        }


# ---------------------------------------------------------------------------
# Backoff / retry helpers (tenacity)
# ---------------------------------------------------------------------------

RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
AUTH_REFRESH_STATUS = frozenset({401, 403})


def is_retryable_http_status(status: Optional[int]) -> bool:
    if status is None:
        return False
    return status in RETRYABLE_HTTP_STATUS or status in AUTH_REFRESH_STATUS


def backoff_seconds(attempt: int, *, base: float = 1.0, maximum: float = 60.0) -> float:
    """Backoff exponentiel avec jitter (tentative 1-indexée)."""
    exp = min(maximum, base * (2 ** max(0, attempt - 1)))
    return exp + random.uniform(0.0, min(2.0, exp * 0.15))


def _import_tenacity():
    try:
        from tenacity import (  # type: ignore
            Retrying,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential,
        )

        return Retrying, retry_if_exception, stop_after_attempt, wait_exponential
    except ImportError:
        return None, None, None, None


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
    retry_predicate: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[[BaseException, int], None]] = None,
) -> T:
    """
    Exécute fn avec retries. Utilise tenacity si installé, sinon boucle manuelle.
    """
    pred = retry_predicate or (lambda _e: True)
    Retrying, retry_if_exception, stop_after_attempt, wait_exponential = _import_tenacity()

    if Retrying is not None and retry_if_exception is not None:
        for attempt in Retrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception(pred),
            reraise=True,
        ):
            with attempt:
                return fn()
        raise RuntimeError("retry_call: unreachable")

    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if attempt >= attempts or not pred(exc):
                raise
            wait = backoff_seconds(attempt, base=min_wait, maximum=max_wait)
            if on_retry:
                on_retry(exc, attempt)
            else:
                logger.warning("Retry %d/%d après %s (pause %.1fs)", attempt, attempts, exc, wait)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("retry_call: empty")


def guarded_platform_call(
    platform: str,
    fn: Callable[[], T],
    *,
    record_failure_on: Optional[Callable[[BaseException], bool]] = None,
) -> T:
    """Circuit breaker + exécution."""
    circuit = get_circuit(platform)
    circuit.before_call()
    try:
        out = fn()
        circuit.record_success()
        return out
    except CircuitOpenError:
        raise
    except Exception as exc:
        should_fail = True
        if record_failure_on is not None:
            try:
                should_fail = bool(record_failure_on(exc))
            except Exception:
                should_fail = True
        if should_fail:
            circuit.record_failure()
        raise


def http_request_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    status = getattr(exc, "status", None)
    if status is not None:
        return is_retryable_http_status(int(status))
    return False

# --- market_recovered.py ---
import json
import logging
import math
import os
import zlib
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests


logger = logging.getLogger("vf.market")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token bucket-ish limiter with penalty backoff."""

    def __init__(self, rate_per_minute: float = 28.0, min_interval: float = 1.2, max_interval: float = 3.2):
        self.rate_per_minute = max(1.0, float(rate_per_minute))
        self.min_interval = max(0.0, float(min_interval))
        self.max_interval = max(self.min_interval, float(max_interval))
        self._last = 0.0
        self._slowdown = 1.0

    def acquire(self) -> None:
        now = time.monotonic()
        base = 60.0 / self.rate_per_minute
        jitter = random.uniform(self.min_interval, self.max_interval)
        wait = max(base, jitter) * self._slowdown
        due = self._last + wait
        if due > now:
            time.sleep(due - now)
        self._last = time.monotonic()
        # gentle recovery
        self._slowdown = max(1.0, self._slowdown * 0.985)

    def penalty(self, seconds: float) -> None:
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            sec = 0.0
        if sec > 0:
            time.sleep(min(75.0, sec))
        self._slowdown = min(4.5, max(self._slowdown, 1.25) * 1.18)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _as_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _parse_amount(obj: Any) -> Optional[float]:
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        amt = obj.get("amount")
        try:
            return float(amt) if amt is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return float(str(obj).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_currency(obj: Any) -> str:
    if isinstance(obj, dict) and obj.get("currency_code"):
        return str(obj["currency_code"])
    return "EUR"


def _first_photo_url(photo: Any) -> Optional[str]:
    if not isinstance(photo, dict):
        return None
    # vinted usually provides url + thumbnails
    for key in ("url", "full_size_url"):
        url = photo.get(key)
        if url:
            return str(url)
    thumbs = photo.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        u = thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else None
        return str(u) if u else None
    return None


@dataclass(slots=True)
class CatalogItem:
    id: int
    title: str = ""
    price: Optional[float] = None
    currency: str = "EUR"
    service_fee: Optional[float] = None
    total_price: Optional[float] = None
    description: str = ""
    brand: str = ""
    condition: str = ""
    size: str = ""
    url: str = ""
    path: str = ""
    favourite_count: int = 0
    view_count: int = 0
    promoted: bool = False
    photo_url: Optional[str] = None
    photo_urls: List[str] = field(default_factory=list)
    seller_username: str = ""
    seller_rating: float = 0.0
    seller_feedback_count: int = 0
    platform: str = "vinted"
    platform_item_id: str = ""
    shipping_available: Optional[bool] = None
    hand_delivery_only: bool = False
    delivery_mode: str = ""
    catalog_id: Optional[int] = None
    listed_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: Dict[str, Any], base_url: str) -> "CatalogItem":
        item_id = data.get("id")
        path = data.get("path") or ""
        url = data.get("url") or (base_url.rstrip("/") + path if path else "")
        photo_urls: List[str] = []
        raw_photos = data.get("photos") or []
        if isinstance(raw_photos, list):
            for ph in raw_photos:
                u = _first_photo_url(ph)
                if u:
                    photo_urls.append(u)
        primary = _first_photo_url(data.get("photo"))
        if primary and primary not in photo_urls:
            photo_urls.insert(0, primary)
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        from .scoring import parse_listed_at

        listed = parse_listed_at("vinted", data)
        shipping_available: Optional[bool] = None
        if data.get("is_reserved") is True:
            shipping_available = False
        elif data.get("can_be_sold") is not None:
            shipping_available = bool(data.get("can_be_sold"))
        elif data.get("is_visible") is not None and not data.get("is_reserved"):
            shipping_available = bool(data.get("is_visible"))
        ship_fee = data.get("item_shipping_price") or data.get("shipping_fee")
        if shipping_available is None and ship_fee is not None:
            shipping_available = True
        iid = int(item_id) if item_id is not None else 0
        item = cls(
            id=iid,
            platform_item_id=str(item_id or ""),
            platform="vinted",
            title=data.get("title") or "",
            description=str(data.get("description") or "").strip(),
            price=_parse_amount(data.get("price")),
            currency=_parse_currency(data.get("price")),
            service_fee=_parse_amount(data.get("service_fee")),
            total_price=_parse_amount(data.get("total_item_price")),
            brand=data.get("brand_title") or "",
            condition=data.get("status") or "",
            size=data.get("size_title") or "",
            url=url or "",
            path=path,
            favourite_count=_as_int(data.get("favourite_count")),
            view_count=_as_int(data.get("view_count")),
            promoted=bool(data.get("promoted", False)),
            photo_url=primary,
            photo_urls=photo_urls,
            seller_username=str(user.get("login") or ""),
            seller_rating=float(user.get("feedback_reputation") or 0.0),
            seller_feedback_count=_as_int(user.get("feedback_count")),
            shipping_available=shipping_available,
            listed_at=listed,
            raw=data,
        )
        # Delivery flags: use API when present, else infer from text.
        try:
            from .engine import infer_delivery_flags_from_text

            txt = infer_delivery_flags_from_text(item.title, item.description)
            if txt.hand_delivery_only:
                item.hand_delivery_only = True
                item.delivery_mode = "hand_only"
                if item.shipping_available is None:
                    item.shipping_available = False
            elif item.shipping_available is True:
                item.delivery_mode = item.delivery_mode or "shipping"
        except Exception:
            pass
        return item

    @property
    def total_or_price(self) -> Optional[float]:
        return self.total_price if self.total_price is not None else self.price

    @property
    def engagement(self) -> int:
        return int(self.favourite_count + self.view_count)

    @classmethod
    def stable_id(cls, platform: str, platform_item_id: str) -> int:
        """ID entier stable pour triage / CSV (hors Vinted)."""
        pid = str(platform_item_id or "").strip()
        if pid.isdigit():
            try:
                v = int(pid)
                if v > 0:
                    return v
            except ValueError:
                pass
        key = f"{(platform or 'x').lower()}:{pid}"
        return int(zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF) or 1

    @classmethod
    def from_leboncoin(cls, ad: Dict[str, Any]) -> "CatalogItem":
        list_id = str(ad.get("list_id") or ad.get("id") or "")
        title = str(ad.get("subject") or ad.get("title") or "").strip()
        body = str(ad.get("body") or ad.get("description") or "").strip()
        price_val: Optional[float] = None
        raw_price = ad.get("price")
        if isinstance(raw_price, list) and raw_price:
            try:
                price_val = float(raw_price[0])
            except (TypeError, ValueError):
                price_val = None
        elif raw_price is not None:
            price_val = _parse_amount(raw_price)
        if price_val is None and ad.get("price_cents") is not None:
            try:
                price_val = float(ad["price_cents"]) / 100.0
            except (TypeError, ValueError):
                pass
        photo_urls: List[str] = []
        images = ad.get("images")
        if isinstance(images, dict):
            urls = images.get("urls_large") or images.get("urls") or []
            if isinstance(urls, list):
                photo_urls = [str(u) for u in urls if u and str(u).startswith("http")]
            thumb = images.get("thumb_url") or images.get("small_url")
            if thumb and str(thumb).startswith("http") and str(thumb) not in photo_urls:
                photo_urls.append(str(thumb))
        elif isinstance(images, list):
            for im in images:
                if isinstance(im, dict):
                    u = im.get("url") or im.get("thumb_url")
                    if u:
                        photo_urls.append(str(u))
                elif im:
                    photo_urls.append(str(im))
        url = str(ad.get("url") or "").strip()
        if not url and list_id:
            url = f"https://www.leboncoin.fr/ad/{list_id}"
        owner = ad.get("owner") if isinstance(ad.get("owner"), dict) else {}
        loc = ad.get("location") if isinstance(ad.get("location"), dict) else {}
        from .engine import parse_lbc_delivery
        from .scoring import parse_listed_at

        delivery = parse_lbc_delivery(ad, title=title, description=body)
        listed = parse_listed_at("leboncoin", ad)
        iid = cls.stable_id("leboncoin", list_id)
        return cls(
            id=iid,
            platform="leboncoin",
            platform_item_id=list_id,
            title=title,
            description=body,
            price=price_val,
            currency="EUR",
            total_price=price_val,
            url=url,
            photo_url=photo_urls[0] if photo_urls else None,
            photo_urls=photo_urls,
            seller_username=str(owner.get("name") or owner.get("store_name") or ""),
            shipping_available=delivery.shipping_available,
            hand_delivery_only=delivery.hand_delivery_only,
            delivery_mode=delivery.delivery_mode,
            condition=str(ad.get("condition") or ""),
            listed_at=listed,
            raw=ad,
        )

    @classmethod
    def from_wallapop(cls, node: Dict[str, Any]) -> "CatalogItem":
        obj = node.get("object") if isinstance(node.get("object"), dict) else node
        if not isinstance(obj, dict):
            obj = node
        item_id = str(obj.get("id") or obj.get("item_id") or "")
        title = str(obj.get("title") or "").strip()
        body = str(obj.get("description") or "").strip()
        price_val: Optional[float] = None
        pr = obj.get("price")
        if isinstance(pr, dict):
            price_val = _parse_amount(pr.get("amount"))
        else:
            price_val = _parse_amount(pr)
        photo_urls: List[str] = []
        for key in ("images", "pictures"):
            imgs = obj.get(key)
            if isinstance(imgs, list):
                for im in imgs:
                    if isinstance(im, dict):
                        u = im.get("urls", {}).get("medium") if isinstance(im.get("urls"), dict) else None
                        u = u or im.get("url") or im.get("original")
                        if u:
                            photo_urls.append(str(u))
                    elif im:
                        photo_urls.append(str(im))
        slug = str(obj.get("web_slug") or obj.get("slug") or "").strip()
        url = str(obj.get("share_url") or obj.get("url") or "").strip()
        if not url and slug:
            url = f"https://fr.wallapop.com/item/{slug}"
        elif not url and item_id:
            url = f"https://fr.wallapop.com/item/i-{item_id}"
        ship = obj.get("shipping") if isinstance(obj.get("shipping"), dict) else {}
        shipping_available: Optional[bool] = None
        if isinstance(ship, dict):
            if "item_is_shippable" in ship:
                shipping_available = bool(ship.get("item_is_shippable"))
            elif "is_shippable" in ship:
                shipping_available = bool(ship.get("is_shippable"))
        fav = _as_int(obj.get("favorited") or obj.get("favourite_count"))
        iid = cls.stable_id("wallapop", item_id)
        return cls(
            id=iid,
            platform="wallapop",
            platform_item_id=item_id,
            title=title,
            description=body,
            price=price_val,
            currency=str((pr or {}).get("currency") if isinstance(pr, dict) else "EUR") or "EUR",
            total_price=price_val,
            url=url,
            photo_url=photo_urls[0] if photo_urls else None,
            photo_urls=photo_urls,
            favourite_count=fav,
            shipping_available=shipping_available,
            raw=obj,
        )

    @classmethod
    def from_rakuten(cls, node: Dict[str, Any]) -> "CatalogItem":
        item_id = str(node.get("id") or node.get("productId") or node.get("product_id") or "")
        title = str(node.get("title") or node.get("name") or "").strip()
        price_val = _parse_amount(node.get("price"))
        if price_val is None and isinstance(node.get("price"), dict):
            price_val = _parse_amount(node["price"].get("amount"))
        url = str(node.get("url") or node.get("link") or "").strip()
        if url and not url.startswith("http"):
            url = "https://fr.shopping.rakuten.com" + (url if url.startswith("/") else "/" + url)
        photo_urls: List[str] = []
        for key in ("imageUrl", "image", "picture", "thumbnail", "photo_url", "photo", "img"):
            im = node.get(key)
            if im:
                photo_urls.append(str(im))
        seller = node.get("seller") if isinstance(node.get("seller"), dict) else {}
        seller_name = str(node.get("sellerName") or seller.get("name") or "Rakuten")
        iid = cls.stable_id("rakuten", item_id or url)
        return cls(
            id=iid,
            platform="rakuten",
            platform_item_id=item_id or url,
            title=title,
            price=price_val,
            currency="EUR",
            total_price=price_val,
            url=url,
            photo_url=photo_urls[0] if photo_urls else None,
            photo_urls=photo_urls,
            seller_username=seller_name,
            shipping_available=True,
            raw=node,
        )

    @classmethod
    def from_cashexpress(cls, node: Dict[str, Any]) -> "CatalogItem":
        item_id = str(node.get("id") or node.get("url") or "")
        title = str(node.get("title") or "").strip()
        try:
            price_val = float(node.get("price") or 0)
        except (TypeError, ValueError):
            price_val = None
        if price_val is not None and price_val <= 0:
            price_val = None
        url = str(node.get("url") or "").strip()
        photo_urls: List[str] = []
        for key in ("photo_url", "photo", "image", "imageUrl", "thumbnail"):
            im = node.get(key)
            if im:
                photo_urls.append(str(im))
        iid = cls.stable_id("cashexpress", item_id)
        return cls(
            id=iid,
            platform="cashexpress",
            platform_item_id=item_id,
            title=title,
            price=price_val,
            currency="EUR",
            total_price=price_val,
            url=url,
            photo_url=photo_urls[0] if photo_urls else None,
            photo_urls=photo_urls,
            seller_username="Cash Express",
            shipping_available=False,
            condition="occasion",
            raw=node,
        )

    def to_row(self) -> Dict[str, Any]:
        desc = self.description or str(self.raw.get("description") or "")
        return {
            "id": str(self.id),
            "platform": self.platform,
            "platform_item_id": self.platform_item_id or str(self.id),
            "shipping_available": self.shipping_available,
            "hand_delivery_only": self.hand_delivery_only,
            "delivery_mode": self.delivery_mode or "",
            "title": self.title,
            "brand": self.brand,
            "price": self.price,
            "total_price": self.total_or_price,
            "currency": self.currency,
            "service_fee": self.service_fee,
            "condition": self.condition,
            "size": self.size,
            "url": self.url,
            "favourite_count": self.favourite_count,
            "view_count": self.view_count,
            "engagement": self.engagement,
            "promoted": self.promoted,
            "photo_url": self.photo_url,
            "photo_urls": list(self.photo_urls),
            "seller_username": self.seller_username,
            "seller_rating": self.seller_rating,
            "seller_feedback_count": self.seller_feedback_count,
            "description": desc,
            "description_excerpt": desc[:240] if desc else "",
            "listed_at": self.listed_at or "",
        }


# ---------------------------------------------------------------------------
# Session bootstrap (HTTP-first, browser fallback)
# ---------------------------------------------------------------------------


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

_SESSION_COOKIE_RE = re.compile(r"^_vinted_[a-z]{2}_session$")
_CHROME_BROWSER_VERSION_RE = re.compile(r"Current browser version is (\\d+)", re.IGNORECASE)


def detect_chrome_major_version() -> Optional[int]:
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore

        for hive, subkey in (
            (winreg.HKEY_LOCAL_MACHINE, r"Software\\Google\\Chrome\\BLBeacon"),
            (winreg.HKEY_CURRENT_USER, r"Software\\Google\\Chrome\\BLBeacon"),
        ):
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                if version:
                    return int(str(version).split(".")[0])
            except OSError:
                continue
    except Exception:
        pass
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if not exe.is_file():
            continue
        try:
            proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15, check=False)
            match = re.search(r"(\\d+)\\.", (proc.stdout or "") + (proc.stderr or ""))
            if match:
                return int(match.group(1))
        except Exception:
            continue
    return None


def _chrome_version_from_error(exc: BaseException) -> Optional[int]:
    match = _CHROME_BROWSER_VERSION_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _create_http_session(user_agent: str, proxy: Optional[str]) -> requests.Session:
    from requests.adapters import HTTPAdapter

    pool_size = 8
    try:
        pool_size = max(4, int(os.environ.get("VF_HTTP_POOL_SIZE", "8") or "8"))
    except (TypeError, ValueError):
        pass
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})
    return sess


class VintedSession:
    def __init__(
        self,
        base_url: str = "https://www.vinted.fr",
        *,
        headless: bool = True,
        chrome_version: Optional[int] = None,
        proxy: Optional[str] = None,
        timeout: float = 0.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.chrome_version = chrome_version
        self.proxy = proxy
        try:
            env_t = float(os.environ.get("VF_VINTED_HTTP_TIMEOUT", "12") or "12")
        except (TypeError, ValueError):
            env_t = 12.0
        self.timeout = timeout if timeout > 0 else min(20.0, max(8.0, env_t))
        self.user_agent = random.choice(USER_AGENTS)
        self.session = _create_http_session(self.user_agent, proxy)
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": self.base_url + "/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )
        self.bootstrap_status = "pending"

    @property
    def has_session_cookie(self) -> bool:
        return any(_SESSION_COOKIE_RE.match(name) for name in self.session.cookies.keys())

    def bootstrap_session(self, catalog_url: Optional[str] = None) -> bool:
        url = (catalog_url or (self.base_url + "/")).strip()
        self.bootstrap_status = "pending"
        # 1) HTTP
        try:
            res = self.session.get(url, timeout=self.timeout)
            logger.info("Visite HTTP : status=%s, cookie session=%s", res.status_code, self.has_session_cookie)
        except requests.RequestException as exc:
            logger.warning("Visite HTTP échouée : %s", exc)
        if self.has_session_cookie:
            self.bootstrap_status = "http_ok"
            return True
        # 2) Browser fallback (optionnel si VF_VINTED_BROWSER_BOOT=0)
        if os.environ.get("VF_VINTED_BROWSER_BOOT", "1").strip().lower() in ("0", "false", "no"):
            self.bootstrap_status = "browser_skipped"
            return len(self.session.cookies) > 0
        ok = self._bootstrap_browser(url)
        self.bootstrap_status = "browser_ok" if ok else "browser_failed"
        return ok or len(self.session.cookies) > 0

    def refresh(self) -> bool:
        return self._bootstrap_browser(self.base_url + "/")

    def _bootstrap_browser(self, start_url: str) -> bool:
        logger.info("Repli navigateur (undetected-chromedriver) pour la session…")
        try:
            from selenium.webdriver.common.by import By  # type: ignore

            from .engine import bootstrap_timeout_sec, driver_get_safe, launch_uc_chrome
        except Exception as exc:
            logger.error("Selenium/undetected-chromedriver indisponible : %s", exc)
            return False

        driver = None
        profile_dir: Optional[str] = None
        version_main = self.chrome_version or detect_chrome_major_version()
        page_timeout = int(min(60, bootstrap_timeout_sec("vinted") - 10))
        try:
            profile_dir = tempfile.mkdtemp(prefix="vf_chrome_")
            extra = [
                "--disable-blink-features=AutomationControlled",
                f"--user-agent={self.user_agent}",
            ]
            if not self.headless:
                extra.append("--start-maximized")
            driver = launch_uc_chrome(
                headless=self.headless,
                version_main=version_main,
                user_data_dir=profile_dir,
                keep_alive=True,
                extra_args=extra,
            )
            driver.set_page_load_timeout(max(20, page_timeout))
            driver_get_safe(driver, start_url, retries=2)
            time.sleep(2)
            for xpath in (
                "//button[@id='onetrust-accept-btn-handler']",
                "//button[contains(text(),'Accepter') or contains(text(),'Accept')]",
            ):
                try:
                    btn = driver.find_element(By.XPATH, xpath)
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1)
                        break
                except Exception:
                    continue
            for c in driver.get_cookies():
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            logger.info("Cookies navigateur importés (%d).", len(driver.get_cookies()))
            return self.has_session_cookie or len(self.session.cookies) > 0
        except Exception as exc:
            logger.error("Repli navigateur échoué : %s", exc)
            return False
        finally:
            from .engine import safe_quit_driver

            safe_quit_driver(driver)
            if profile_dir:
                shutil.rmtree(profile_dir, ignore_errors=True)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class VintedApiClient:
    def __init__(
        self,
        session: VintedSession,
        *,
        rate_limiter: Optional[RateLimiter] = None,
        timeout: float = 15.0,
        max_retries: int = 4,
    ):
        self.session = session
        self.base_url = session.base_url
        self.rate = rate_limiter or RateLimiter()
        self.timeout = timeout
        self.max_retries = max_retries

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        circuit = get_circuit("vinted")
        try:
            circuit.before_call()
        except CircuitOpenError as exc:
            raise ApiError(str(exc), status=503) from exc

        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_status: Optional[int] = None
        try:
            for attempt in range(1, self.max_retries + 1):
                self.rate.acquire()
                try:
                    res = self.session.session.get(url, params=params, timeout=self.timeout)
                except requests.RequestException as exc:
                    logger.warning("Erreur réseau (tentative %d) : %s", attempt, exc)
                    time.sleep(backoff_seconds(attempt, base=1.0, maximum=30.0))
                    continue
                last_status = res.status_code
                if res.status_code == 200:
                    try:
                        payload = res.json()
                        circuit.record_success()
                        return payload
                    except ValueError as exc:
                        raise ApiError(f"Réponse JSON invalide : {exc}", status=200) from exc
                if res.status_code in AUTH_REFRESH_STATUS:
                    logger.warning("Blocage auth (%s) → rafraîchissement session.", res.status_code)
                    self.rate.penalty(random.uniform(2.0, 5.0))
                    self.session.refresh()
                    time.sleep(backoff_seconds(attempt, base=2.0, maximum=15.0))
                    continue
                if res.status_code == 429:
                    wait = backoff_seconds(attempt, base=2.0, maximum=60.0) + random.uniform(2.0, 6.0)
                    logger.warning("429 Rate limit → pause %.1fs (tentative %d).", wait, attempt)
                    self.rate.penalty(wait)
                    continue
                if res.status_code in RETRYABLE_HTTP_STATUS:
                    logger.warning("Erreur serveur %s (tentative %d).", res.status_code, attempt)
                    time.sleep(backoff_seconds(attempt, base=1.0, maximum=30.0))
                    continue
                raise ApiError(f"Réponse inattendue {res.status_code} ({url})", status=res.status_code)
            circuit.record_failure()
            raise ApiError(
                f"Échec après {self.max_retries} tentatives (dernier status={last_status}).",
                status=last_status,
            )
        except ApiError as exc:
            if exc.status in RETRYABLE_HTTP_STATUS or exc.status in AUTH_REFRESH_STATUS:
                circuit.record_failure()
            raise

    def get_catalog_page(self, params: Dict[str, Any], page: int = 1) -> Dict[str, Any]:
        query = dict(params)
        query["page"] = str(page)
        query.setdefault("per_page", "96")
        return self._get("/api/v2/catalog/items", params=query)

    def iter_catalog(
        self,
        params: Dict[str, Any],
        *,
        max_items: int = 0,
        check_running: Optional[Callable[[], bool]] = None,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> Iterator[Dict[str, Any]]:
        page = 1
        collected = 0
        total_pages = 1
        while True:
            if check_running is not None and not check_running():
                break
            payload = self.get_catalog_page(params, page=page)
            items = payload.get("items") or []
            pagination = payload.get("pagination") or {}
            try:
                total_pages = int(pagination.get("total_pages") or total_pages)
            except (TypeError, ValueError):
                total_pages = total_pages
            if not items:
                break
            for item in items:
                yield item
                collected += 1
                if max_items and collected >= max_items:
                    break
            if on_page is not None:
                try:
                    on_page(page, total_pages, collected)
                except Exception:
                    pass
            if max_items and collected >= max_items:
                break
            if page >= total_pages:
                break
            page += 1

    def fetch_catalog(self, params: Dict[str, Any], *, max_items: int = 0) -> List[Dict[str, Any]]:
        return list(self.iter_catalog(params, max_items=max_items))

    def get_item(self, item_id: int | str) -> Dict[str, Any]:
        return self._get(f"/api/v2/items/{item_id}")


# ---------------------------------------------------------------------------
# Helpers: URL params parsing (simple, proven safe)
# ---------------------------------------------------------------------------


def download_image_url(
    url: str,
    dest: Path,
    *,
    timeout: float = 18.0,
    headers: Optional[Dict[str, str]] = None,
) -> bool:
    if not url:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        hdr = dict(headers or {})
        low = url.lower()
        if "leboncoin" in low and "referer" not in {k.lower() for k in hdr}:
            hdr.setdefault("Referer", "https://www.leboncoin.fr/")
        hdr.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        )
        with requests.get(url, stream=True, timeout=timeout, headers=hdr) as res:
            if res.status_code != 200:
                return False
            ctype = (res.headers.get("content-type") or "").lower()
            if "image" not in ctype:
                return False
            with dest.open("wb") as f:
                for chunk in res.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def resolve_vinted_catalog_url(url: str) -> tuple[str, str]:
    """
    Normalise une URL Vinted (session-refresh → ref_url, chemins relatifs).
    Retourne (url_catalogue, note) ; note vide si inchangé.
    """
    from urllib.parse import parse_qs, unquote, urljoin, urlparse

    raw = (url or "").strip()
    if not raw:
        return raw, ""
    if not raw.startswith(("http://", "https://")):
        raw = urljoin("https://www.vinted.fr/", raw.lstrip("/"))

    parsed = urlparse(raw)
    path_l = (parsed.path or "").lower()
    note = ""

    if "session-refresh" in path_l or "session_refresh" in path_l:
        qs = parse_qs(parsed.query)
        ref_vals = qs.get("ref_url") or qs.get("ref") or []
        if ref_vals:
            ref = unquote(str(ref_vals[0]).strip())
            if ref.startswith("/"):
                ref = urljoin("https://www.vinted.fr", ref)
            elif not ref.startswith(("http://", "https://")):
                ref = urljoin("https://www.vinted.fr/", ref)
            child, sub = resolve_vinted_catalog_url(ref)
            return child, sub or "URL extraite depuis session-refresh"

    if "/catalog" not in path_l and "search_text" not in (parsed.query or "").lower():
        qs = parse_qs(parsed.query)
        if qs.get("search_text"):
            base = f"{parsed.scheme}://{parsed.netloc}/catalog"
            q = "&".join(f"{k}={unquote(v[0])}" for k, v in qs.items() if v)
            return f"{base}?{q}", "URL catalogue reconstruite"
        # Chemin type /iphone 13 ou /iphone%2013 (sans catalog?search_text=)
        path_search = unquote((parsed.path or "").strip("/"))
        if path_search and path_search.lower() not in (
            "catalog",
            "items",
            "member",
            "session-refresh",
        ):
            from urllib.parse import urlencode

            st = path_search.replace("-", " ").replace("_", " ")
            vinted_order = os.environ.get("VF_VINTED_ORDER", "newest_first").strip() or "newest_first"
            catalog_url = f"{parsed.scheme}://{parsed.netloc}/catalog?" + urlencode(
                {"search_text": st, "page": "1", "per_page": "96", "order": vinted_order}
            )
            return catalog_url, f"Recherche extraite du chemin : {st!r}"

    return raw, note


def params_from_url(url: str) -> Dict[str, Any]:
    from urllib.parse import parse_qs, urlparse

    resolved, _ = resolve_vinted_catalog_url(url)
    q = urlparse(resolved).query
    parsed = parse_qs(q)
    out: Dict[str, Any] = {}
    for k, v in parsed.items():
        if len(v) == 1:
            out[k] = v[0]
        else:
            out[k] = v
    return normalize_vinted_catalog_params(out)


def normalize_vinted_catalog_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Prépare les paramètres API catalogue (catalog[] + search_text produit si catalogue connu)."""
    from .engine import VINTED_CATALOG_HINTS, catalog_ids_from_params

    p = dict(params)
    cats = catalog_ids_from_params(p)
    if cats:
        p["catalog[]"] = cats if len(cats) > 1 else cats[0]
    if not str(p.get("search_text") or "").strip():
        for cid in cats:
            hint = VINTED_CATALOG_HINTS.get(cid)
            if hint and hint.get("search_text"):
                p["search_text"] = str(hint["search_text"])
                break
    return p


def normalize_search_name(text: str) -> str:
    # NOTE: use \w in regex (raw string), not a literal backslash-w
    s = re.sub(r"[^\w\-]+", "_", (text or "").strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:80] or "run"


def default_vinted_order() -> str:
    return os.environ.get("VF_VINTED_ORDER", "newest_first").strip() or "newest_first"


def build_catalog_url(
    *,
    base_url: str = "https://www.vinted.fr",
    search_text: str,
    per_page: int = 96,
    order: Optional[str] = None,
    currency: str = "EUR",
    price_from: Optional[float] = None,
    price_to: Optional[float] = None,
) -> str:
    from urllib.parse import urlencode

    q = {
        "search_text": search_text,
        "page": "1",
        "per_page": str(int(per_page)),
        "order": order or default_vinted_order(),
        "currency": currency,
    }
    if price_from is not None and float(price_from) > 0:
        q["price_from"] = str(int(price_from))
    if price_to is not None and float(price_to) > 0:
        q["price_to"] = str(int(price_to))
    return base_url.rstrip("/") + "/catalog?" + urlencode(q)


# ---------------------------------------------------------------------------
# Marketplace adapter (extensible)
# ---------------------------------------------------------------------------


class MarketplaceAdapter:
    """Protocol commun Vinted / eBay / LeBonCoin / …"""

    name: str = "base"

    @property
    def platform_name(self) -> str:
        return self.name

    def bootstrap(self, url: str, *, headless: bool = True) -> None:
        raise NotImplementedError

    def fetch_catalog(
        self,
        url: str,
        *,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[CatalogItem]:
        raise NotImplementedError

    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def parse_params(self, url: str) -> Dict[str, Any]:
        raise NotImplementedError

    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class VintedAdapter(MarketplaceAdapter):
    name = "vinted"

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._session: Optional[VintedSession] = None
        self._client: Optional[VintedApiClient] = None
        self.base_url = "https://www.vinted.fr"
        self._bootstrapped = False
        self._bootstrap_at = 0.0

    def _session_ready(self) -> bool:
        vs = self._session
        if vs is None:
            return False
        if vs.has_session_cookie:
            return True
        return bool(vs.session.cookies)

    def bootstrap(self, url: str, *, headless: bool = True) -> None:
        h = headless if headless is not None else self._headless
        if self._bootstrapped and self._session_ready() and time.monotonic() - self._bootstrap_at < 600:
            logger.debug("Vinted : session déjà active — skip bootstrap")
            return
        if self._session is None:
            self._session = VintedSession(headless=h)
        else:
            self._session.headless = h
        ok = self._session.bootstrap_session(url)
        self._client = VintedApiClient(self._session)
        self.base_url = self._session.base_url
        self._bootstrapped = ok or self._session_ready()
        self._bootstrap_at = time.monotonic()
        if not self._bootstrapped:
            logger.warning("Vinted bootstrap incomplet (status=%s)", self._session.bootstrap_status)

    @property
    def client(self) -> VintedApiClient:
        if self._client is None:
            raise RuntimeError("Marketplace non initialisé")
        return self._client

    def parse_params(self, url: str) -> Dict[str, Any]:
        return params_from_url(url)

    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:
        return build_catalog_url(base_url=self.base_url, search_text=query)

    def fetch_catalog(
        self,
        url: str,
        *,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[CatalogItem]:
        params = normalize_vinted_catalog_params(self.parse_params(url))
        out: List[CatalogItem] = []
        for raw in self.client.iter_catalog(params, max_items=max_items, on_page=on_page):
            if isinstance(raw, dict):
                out.append(CatalogItem.from_api(raw, base_url=self.base_url))
        return out

    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:
        try:
            payload = self.client.get_item(int(item_id))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        item_obj = payload.get("item")
        if isinstance(item_obj, dict):
            return item_obj
        if payload.get("id") or payload.get("title"):
            return payload
        return None

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass


def _adapter_kwargs(name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    allowed: Dict[str, tuple] = {
        "vinted": ("headless", "timeout"),
        "ebay": ("site", "timeout", "headless"),
        "leboncoin": ("headless", "timeout"),
        "lbc": ("headless", "timeout"),
        "wallapop": ("headless", "timeout"),
        "rakuten": ("headless", "timeout"),
        "cashexpress": ("headless", "timeout"),
    }
    keys = allowed.get(name, ("headless", "timeout"))
    return {k: v for k, v in kwargs.items() if k in keys}


def get_adapter(name: str = "vinted", **kwargs: Any) -> MarketplaceAdapter:
    key = (name or "vinted").lower()
    kw = _adapter_kwargs(key, kwargs)
    if key in ("vinted", "vf", ""):
        return VintedAdapter(**kw)
    if key == "ebay":
        from .market import EbayAdapter

        return EbayAdapter(**kw)
    if key in ("leboncoin", "lbc"):
        from .market import LeBonCoinAdapter

        return LeBonCoinAdapter(**kw)
    if key == "wallapop":
        from .market import WallapopAdapter

        return WallapopAdapter(**kw)
    if key == "rakuten":
        from .market import RakutenAdapter

        return RakutenAdapter(**kw)
    if key in ("cashexpress", "cash-express", "cex"):
        from .market import CashExpressAdapter

        return CashExpressAdapter(**kw)
    raise ValueError(
        f"Marketplace inconnu : {name!r} — vinted, ebay, leboncoin, wallapop, rakuten, cashexpress"
    )


# --- platforms.py ---


# ================================================== ebay.py



import json

import logging

import os

import re

import time

from pathlib import Path

from typing import Any, Callable, Dict, List, Optional

from urllib.parse import quote_plus, urljoin, urlparse, parse_qs, urlencode, urlunparse



import requests

from bs4 import BeautifulSoup



from .market import CatalogItem, MarketplaceAdapter, RateLimiter



log_ebay = logging.getLogger("vf.ebay")



_DEFAULT_UA = (

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "

    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

)

_CHALLENGE_MARKERS = (

    "splashui/challenge",

    "splashui/captcha",

    "perfdrive",

    "please verify yourself",

    "checking your browser",

    "captcha",

    "je suis un humain",

)

_SKIP_TITLE = re.compile(

    r"(?i)^(shop\s+on\s+ebay|nouvelle\s+annonce|new\s+listing|sponsoris)"

)

_EBAY_TITLE_JUNK = re.compile(
    r"\s*La page s['\u2019]?ouvre dans une nouvelle fen[eê]tre ou un nouvel onglet.*$",
    re.I,
)

_PRICE_RE = re.compile(r"[\d\s.,]+")

_EBAY_COOKIE_PATH = Path("brouillon") / "data" / "vf_cookies" / "ebay.json"





def _clean_ebay_title(title: str) -> str:
    t = _EBAY_TITLE_JUNK.sub("", (title or "").strip())
    t = re.sub(r"(?i)^nouvelle annonce\s+", "", t).strip()
    return re.sub(r"\s+", " ", t)


def _ebay_image_url(node: Any) -> str:
    for img in node.select("img[src*='ebayimg'], img[data-src*='ebayimg']"):
        u = str(img.get("src") or img.get("data-src") or "").strip()
        if u.startswith("http") and "ebayimg" in u:
            return u
    img = node.select_one("img.s-item__image-img") or node.select_one("img")
    if img:
        u = str(img.get("src") or img.get("data-src") or "").strip()
        if u.startswith("http") and "ebaystatic.com/rs/v/" not in u:
            return u
    return ""


def _parse_ebay_price(text: str) -> Optional[float]:

    if not text:

        return None

    s = text.replace("\xa0", " ").replace("EUR", "").replace("€", "").replace("$", "").strip()

    m = _PRICE_RE.search(s)

    if not m:

        return None

    raw = m.group(0).replace(" ", "").replace(",", ".")

    try:

        val = float(raw)
        return val if val > 0 else None

    except ValueError:

        return None





def _item_id_from_url(url: str) -> str:
    key = _ebay_item_key_from_url(url)
    if key:
        m = re.search(r"/itm/(\d+)$", key)
        if m:
            return m.group(1)
        return key

    m = re.search(r"/itm/(?:[^/]+/)?(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"item=(\d+)", url or "")
    return m.group(1) if m else ""

def _ebay_item_key_from_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    path = (parsed.path or "").strip()
    m = re.search(r"/itm/(?:[^/]+/)?(\d+)", path)
    if m:
        return f"/itm/{m.group(1)}"
    m2 = re.search(r"(/itm/[^/?#]+)", path)
    if m2:
        return m2.group(1).rstrip("/")
    return ""





class EbayAdapter(MarketplaceAdapter):

    name = "ebay"



    def __init__(self, *, site: str = "ebay.fr", timeout: float = 28.0, headless: bool = True) -> None:

        self.site = site

        self.timeout = timeout

        self._headless = headless

        self._driver = None

        self._challenge_failed = False

        self._session_warmed = False

        self._cookies_dirty = False

        self._rate = RateLimiter(rate_per_minute=12.0, min_interval=2.5, max_interval=5.0)

        self.session = requests.Session()

        self.session.headers.update(

            {

                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

                "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",

                "cache-control": "no-cache",

                "pragma": "no-cache",

                "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',

                "sec-ch-ua-mobile": "?0",

                "sec-ch-ua-platform": '"Windows"',

                "sec-fetch-dest": "document",

                "sec-fetch-mode": "navigate",

                "sec-fetch-site": "same-origin",

                "upgrade-insecure-requests": "1",

                "user-agent": _DEFAULT_UA,

            }

        )

        self._load_persisted_cookies()



    def _load_persisted_cookies(self) -> None:

        try:

            if not _EBAY_COOKIE_PATH.is_file():

                return

            data = json.loads(_EBAY_COOKIE_PATH.read_text(encoding="utf-8"))

            for c in data if isinstance(data, list) else []:

                if isinstance(c, dict) and c.get("name"):

                    self.session.cookies.set(

                        str(c["name"]),

                        str(c.get("value") or ""),

                        domain=c.get("domain") or f".{self.site}",

                        path=c.get("path") or "/",

                    )

        except Exception as exc:

            log_ebay.debug("eBay cookies persistés illisibles: %s", exc)



    def _mark_cookies_dirty(self) -> None:

        self._cookies_dirty = True



    def _save_persisted_cookies(self, *, force: bool = False) -> None:

        if not force and not self._cookies_dirty:

            return

        try:

            _EBAY_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)

            cookies: List[Dict[str, Any]] = []

            if self._driver:

                cookies = list(self._driver.get_cookies() or [])

            if not cookies:

                for c in self.session.cookies:

                    cookies.append(

                        {

                            "name": c.name,

                            "value": c.value,

                            "domain": c.domain or f".{self.site}",

                            "path": c.path or "/",

                        }

                    )

            if cookies:

                _EBAY_COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")

                log_ebay.debug("eBay : %d cookies persistés", len(cookies))

                self._cookies_dirty = False

        except Exception as exc:

            log_ebay.debug("eBay save cookies: %s", exc)



    def flush_persisted_cookies(self) -> None:

        """Écrit les cookies une fois (fin de scrape / fermeture session)."""

        self._save_persisted_cookies(force=True)



    def _captcha_timeout_s(self) -> float:

        try:

            return float(os.environ.get("VF_EBAY_CAPTCHA_TIMEOUT", "180") or "180")

        except (TypeError, ValueError):

            return 180.0



    def bootstrap(self, url: str, *, headless: bool = True) -> None:

        """Warm-up session — HTTP puis cookies navigateur si challenge."""

        self._headless = headless

        if self._session_warmed and not self._challenge_failed and self.session.cookies:

            log_ebay.debug("eBay : session réutilisée — bootstrap ignoré")

            return

        home = f"https://www.{self.site}/"

        target = url if url and url.startswith("http") else home

        try:

            self._rate.acquire()

            r = self.session.get(target, timeout=self.timeout)

            if not self._html_blocked(r.text or "", r.status_code):

                self._session_warmed = True

                self._mark_cookies_dirty()

                log_ebay.info("eBay : session HTTP OK (%s)", target[:80])

                return

            self._ensure_browser()

            if self._driver:

                from .engine import driver_get_safe

                driver_get_safe(self._driver, target, retries=3)

                self._browser_post_load(wait_challenge=not self._headless)

            else:

                self._rate.acquire()

                r2 = self.session.get(home, timeout=self.timeout)

                if self._html_blocked(r2.text or "", r2.status_code):

                    self._fetch_html_browser(target)

        except Exception as exc:

            self._challenge_failed = "challenge" in str(exc).lower()

            log_ebay.warning("eBay bootstrap: %s", exc)



    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:

        flt = dict(filters or {})

        params: Dict[str, str] = {

            "_nkw": (query or "").strip(),

            "_sop": "12",

            "LH_BIN": "1",

            "LH_ItemCondition": str(flt.get("item_condition") or "3000|1500"),

            "_from": "R40",

        }

        pmin = flt.get("price_min")

        pmax = flt.get("price_max")

        if pmin is not None:

            try:

                params["_udlo"] = str(int(pmin))

            except (TypeError, ValueError):

                pass

        if pmax is not None:

            try:

                params["_udhi"] = str(int(pmax))

            except (TypeError, ValueError):

                pass

        params.update({k: str(v) for k, v in flt.items() if k not in ("price_min", "price_max", "item_condition") and v is not None})

        return f"https://www.{self.site}/sch/i.html?{urlencode(params)}"



    def _html_has_listings(self, html: str) -> bool:

        low = (html or "").lower()

        return (

            'class="s-item' in low

            or "srp-river-results" in low

            or "srp-results" in low

            or "ul class=\"srp-results" in low

        )



    def _html_blocked(self, html: str, status: int = 200) -> bool:

        if status in (403, 429, 503):

            return True

        if self._html_has_listings(html):

            return False

        low = (html or "").lower()[:24000]

        if any(m in low for m in _CHALLENGE_MARKERS):

            return True

        if "itm/" not in low and len(low) > 1200:

            return True

        return False



    def _ensure_browser(self) -> None:

        if self._driver is not None:

            return

        try:

            from .engine import detect_chrome_version_main, launch_uc_chrome



            self._driver = launch_uc_chrome(

                headless=self._headless,

                version_main=detect_chrome_version_main(),

                extra_args=["--lang=fr-FR", f"--user-agent={_DEFAULT_UA}"],

            )

            self._driver.set_page_load_timeout(int(self.timeout) + 10)

        except Exception as exc:

            log_ebay.warning("eBay navigateur indisponible: %s", exc)

            self._driver = None



    def _import_browser_cookies(self) -> None:

        if not self._driver:

            return

        try:

            for c in self._driver.get_cookies():

                self.session.cookies.set(

                    c.get("name", ""),

                    c.get("value", ""),

                    domain=c.get("domain") or f".{self.site}",

                    path=c.get("path") or "/",

                )

            self._mark_cookies_dirty()

        except Exception:

            pass



    def _browser_post_load(self, *, wait_challenge: bool = False) -> str:

        from .engine import dismiss_cookie_banners, wait_until_html_ready



        if not self._driver:

            return ""

        dismiss_cookie_banners(self._driver)

        time.sleep(1.5)

        html = self._driver.page_source or ""

        if self._html_blocked(html):

            timeout = self._captcha_timeout_s() if wait_challenge else 18.0

            if wait_challenge:

                log_ebay.info(

                    "eBay : captcha détecté — résolvez-le dans la fenêtre Chrome (jusqu'à %ds)…",

                    int(timeout),

                )



                def _notify() -> None:

                    log_ebay.info("eBay : en attente de validation captcha / cookies…")



                wait_until_html_ready(

                    self._driver,

                    is_blocked=lambda h: self._html_blocked(h),

                    timeout_s=timeout,

                    on_wait=_notify,

                )

            dismiss_cookie_banners(self._driver)

            html = self._driver.page_source or ""

        self._import_browser_cookies()

        if not self._html_blocked(html):

            self._session_warmed = True

            self._mark_cookies_dirty()

        return html



    def _fetch_html_browser(self, url: str, *, headed_retry: bool = False) -> str:

        self._ensure_browser()

        if not self._driver:

            raise RuntimeError("eBay bloqué (challenge) — navigateur indisponible")

        auto_headed = os.environ.get("VF_EBAY_AUTO_HEADED", "0").strip().lower() not in ("0", "false", "no")
        visible = headed_retry or not self._headless

        try:

            from .engine import driver_get_safe

            driver_get_safe(self._driver, url, retries=3)

            html = self._browser_post_load(wait_challenge=visible)

            if self._html_blocked(html):

                if self._headless and not headed_retry and auto_headed:

                    from .engine import safe_quit_driver



                    safe_quit_driver(self._driver)

                    self._driver = None

                    self._headless = False

                    log_ebay.info("eBay : repli navigateur visible (challenge anti-bot)")

                    return self._fetch_html_browser(url, headed_retry=True)

                self._challenge_failed = True

                raise RuntimeError(

                    "eBay challenge non résolu — validez le captcha dans Chrome puis relancez"

                )

            return html

        except Exception as exc:

            if "challenge" in str(exc).lower():

                self._challenge_failed = True

            if self._headless and not headed_retry and auto_headed and "challenge" in str(exc).lower():

                from .engine import safe_quit_driver



                safe_quit_driver(self._driver)

                self._driver = None

                self._headless = False

                return self._fetch_html_browser(url, headed_retry=True)

            raise RuntimeError(f"eBay navigateur: {exc}") from exc



    def _page_url(self, base: str, page: int) -> str:

        if page <= 1:

            return base

        parsed = urlparse(base)

        qs = parse_qs(parsed.query, keep_blank_values=True)

        qs["_pgn"] = [str(page)]

        new_q = urlencode({k: v[0] if isinstance(v, list) else v for k, v in qs.items()})

        return urlunparse(parsed._replace(query=new_q))



    def _fetch_html(self, url: str) -> str:

        # local: get_circuit

        if self._challenge_failed:

            raise RuntimeError("eBay challenge — session invalide (validez cookies / relancez)")

        cb = get_circuit("ebay")

        cb.before_call()

        self._rate.acquire()

        try:

            r = self.session.get(url, timeout=self.timeout, headers={"referer": f"https://www.{self.site}/"})

            if r.status_code == 429:

                cb.record_failure()

                raise RuntimeError("Blocage 429 — ralentir ou changer IP")

            html = r.text or ""

            if self._html_blocked(html, r.status_code):

                html = self._fetch_html_browser(url)

            elif r.status_code >= 400:

                r.raise_for_status()

            if self._html_has_listings(html):

                self._session_warmed = True

                self._mark_cookies_dirty()

            cb.record_success()

            return html

        except Exception:

            cb.record_failure()

            raise



    def _parse_items(self, html: str, *, max_items: int = 0) -> List[CatalogItem]:

        soup = BeautifulSoup(html, "html.parser")

        nodes = soup.select("li.s-item, div.s-item__wrapper, li.s-card")

        out: List[CatalogItem] = []

        seen: set = set()

        for node in nodes:

            if max_items > 0 and len(out) >= max_items:

                break

            link_el = node.select_one("a.s-item__link") or node.select_one("a[href*='/itm/']")

            if not link_el:

                continue

            href = str(link_el.get("href") or "").split("?")[0]

            if not href or "/itm/" not in href:

                continue

            title_el = (
                node.select_one(".s-card__title")
                or node.select_one(".s-item__title")
                or node.select_one("div.s-item__title")
                or node.select_one("[role='heading']")
            )

            raw_title = title_el.get_text(" ", strip=True) if title_el else (link_el.get("title") or link_el.get_text(" ", strip=True) or "")

            title = _clean_ebay_title(raw_title)

            if not title or _SKIP_TITLE.search(title) or title.lower() in ("shop on ebay", "ebay"):

                continue

            price_el = (
                node.select_one(".s-card__price")
                or node.select_one(".s-item__price")
                or node.select_one("span.s-item__price")
                or node.select_one("span[class*='price']")
            )

            price = _parse_ebay_price(price_el.get_text(" ", strip=True) if price_el else "")

            if price is None:
                pm = re.search(r"([\d\s]+[,\.]\d{2})\s*(?:EUR|€)", node.get_text(" ", strip=True), re.I)
                if pm:
                    price = _parse_ebay_price(pm.group(1))

            iid = _item_id_from_url(href)
            if not iid:
                iid = _ebay_item_key_from_url(href)

            if not iid or iid in seen:

                continue

            seen.add(iid)

            photo = _ebay_image_url(node)

            seller_el = node.select_one(".s-item__seller-info-text") or node.select_one(".s-item__seller")

            seller_txt = seller_el.get_text(" ", strip=True) if seller_el else ""

            feedback = 0

            fm = re.search(r"(\d[\d\s.]*)", seller_txt.replace(".", ""))

            if fm and ("évaluation" in seller_txt.lower() or "%" in seller_txt):

                try:

                    feedback = int(re.sub(r"\D", "", fm.group(1)) or 0)

                except ValueError:

                    feedback = 0

            out.append(

                CatalogItem(

                    id=int(iid) if iid.isdigit() else hash(iid) % 10_000_000,

                    title=title[:240],

                    price=price,

                    total_price=price,

                    currency="EUR",

                    url=href if href.startswith("http") else urljoin(f"https://www.{self.site}", href),

                    photo_url=photo or None,

                    platform="ebay",

                    platform_item_id=iid,

                    seller_feedback_count=feedback,

                )

            )

        if not out:
            for link in soup.select("a[href*='/itm/']"):
                href = str(link.get("href") or "").split("?")[0]
                if "/itm/" not in href:
                    continue
                iid = _item_id_from_url(href)
                if not iid:
                    iid = _ebay_item_key_from_url(href)
                if not iid or iid in seen:
                    continue
                title = _clean_ebay_title(link.get("title") or link.get_text(" ", strip=True) or "")
                if not title or _SKIP_TITLE.search(title):
                    continue
                parent = link.find_parent(["li", "div"])
                price = None
                if parent:
                    pe = parent.select_one(".s-card__price, .s-item__price, span[class*='price']")
                    if pe:
                        price = _parse_ebay_price(pe.get_text(" ", strip=True))
                photo = _ebay_image_url(parent) if parent else ""
                seen.add(iid)
                out.append(
                    CatalogItem(
                        id=int(iid) if iid.isdigit() else hash(iid) % 10_000_000,
                        title=title[:240],
                        price=price,
                        total_price=price,
                        currency="EUR",
                        url=href if href.startswith("http") else urljoin(f"https://www.{self.site}", href),
                        photo_url=photo or None,
                        platform="ebay",
                        platform_item_id=iid,
                    )
                )
                if max_items > 0 and len(out) >= max_items:
                    break

        return out



    def fetch_catalog(

        self,

        url: str,

        *,

        max_items: int = 0,

        on_page: Optional[Callable[[int, int, int], None]] = None,

    ) -> List[CatalogItem]:

        from .engine import BUS



        t0 = time.monotonic()

        query = ""

        try:

            qs = parse_qs(urlparse(url).query)

            query = (qs.get("_nkw") or [""])[0]

        except Exception:

            pass

        BUS.emit("SOURCE_START", {"platform": "ebay", "query": query or url})

        if self._challenge_failed:

            log_ebay.warning("eBay fetch ignoré — challenge non résolu (cookies / captcha)")

            BUS.emit("SOURCE_DONE", {"platform": "ebay", "count": 0, "time_ms": 0})

            return []



        collected: List[CatalogItem] = []

        page = 1

        max_pages = int(os.environ.get("VF_EBAY_MAX_PAGES", "12") or "12")

        try:

            while page <= max_pages:

                page_url = self._page_url(url, page)

                html = self._fetch_html(page_url)

                batch = self._parse_items(html, max_items=max(0, max_items - len(collected)) if max_items else 0)

                if not batch:

                    break

                collected.extend(batch)

                if on_page:

                    on_page(page, max_pages, len(collected))

                if max_items > 0 and len(collected) >= max_items:

                    collected = collected[:max_items]

                    break

                if len(batch) < 5:

                    break

                page += 1

        except Exception as exc:

            BUS.emit("SOURCE_ERROR", {"platform": "ebay", "error": str(exc)})

            raise



        ms = int((time.monotonic() - t0) * 1000)

        if not collected:
            log_ebay.warning("eBay : 0 annonce — captcha ou HTML sans résultats (_nkw=%r)", query)

        BUS.emit("SOURCE_DONE", {"platform": "ebay", "count": len(collected), "time_ms": ms})

        return collected



    def fetch_by_terms(

        self,

        terms: str,

        *,

        max_items: int = 0,

        price_min: int = 0,

        price_max: int = 0,

    ) -> List[CatalogItem]:

        flt: Dict[str, Any] = {}

        if price_min > 0:

            flt["price_min"] = price_min

        if price_max > 0:

            flt["price_max"] = price_max

        url = self.build_search_url(terms, filters=flt or None)

        return self.fetch_catalog(url, max_items=max_items)



    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:

        url = f"https://www.{self.site}/itm/{item_id}"

        try:

            html = self._fetch_html(url)

        except Exception:

            return None

        soup = BeautifulSoup(html, "html.parser")

        title = soup.select_one("h1.x-item-title__mainTitle, h1.it-ttl")

        return {"id": item_id, "title": title.get_text(strip=True) if title else ""}



    def parse_params(self, url: str) -> Dict[str, Any]:

        qs = parse_qs(urlparse(url).query)

        st = (qs.get("_nkw") or [""])[0]

        return {"search_text": st, "url": url}



    def close(self) -> None:

        try:

            self.flush_persisted_cookies()

        except Exception:

            pass

        try:

            self.session.close()

        except Exception:

            pass

        if self._driver:

            try:

                self._driver.quit()

            except Exception:

                pass

            self._driver = None



# ================================================== leboncoin.py

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from .market import CatalogItem, MarketplaceAdapter, RateLimiter

log_lbc = logging.getLogger("vf.leboncoin")

LBC_API_URL = "https://api.leboncoin.fr/finder/search"
LBC_API_KEY = "ba0c2dad52b3ec"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
_LBC_COOKIE_PATH = Path("brouillon") / "data" / "vf_cookies" / "lbc.json"


class LeBonCoinAdapter(MarketplaceAdapter):
    name = "leboncoin"

    def __init__(self, *, headless: bool = True, timeout: float = 20.0) -> None:
        self._headless = headless
        self._timeout = timeout
        self._bootstrapped = False
        self._session_blocked = False
        self._headed_recovery_used = False
        self._bootstrap_at = 0.0
        self._bootstrap_cooldown_until = 0.0
        self._bootstrap_attempts = 0
        rpm = float(os.environ.get("VF_LBC_RATE_PER_MIN", "10") or "10")
        self._rate = RateLimiter(rate_per_minute=rpm, min_interval=2.5, max_interval=5.0)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "api_key": LBC_API_KEY,
                "content-type": "application/json",
                "origin": "https://www.leboncoin.fr",
                "referer": "https://www.leboncoin.fr/",
                "user-agent": DEFAULT_UA,
            }
        )
        self._load_persisted_cookies()

    def invalidate_session(self, *, clear_persisted: bool = False) -> None:
        self._bootstrapped = False
        self._session_blocked = False
        self._bootstrap_at = 0.0
        if clear_persisted:
            cooldown_min = float(os.environ.get("VF_LBC_BLOCK_COOLDOWN_MIN", "25") or "25")
            self._bootstrap_cooldown_until = time.monotonic() + max(300.0, cooldown_min * 60.0)
        self.session.cookies.clear()
        if clear_persisted:
            try:
                if _LBC_COOKIE_PATH.is_file():
                    _LBC_COOKIE_PATH.unlink()
            except OSError:
                pass

    def _load_persisted_cookies(self) -> None:
        try:
            if not _LBC_COOKIE_PATH.is_file():
                return
            data = json.loads(_LBC_COOKIE_PATH.read_text(encoding="utf-8"))
            for c in data if isinstance(data, list) else []:
                if isinstance(c, dict) and c.get("name"):
                    self.session.cookies.set(
                        str(c["name"]),
                        str(c.get("value") or ""),
                        domain=c.get("domain") or ".leboncoin.fr",
                        path=c.get("path") or "/",
                    )
        except Exception as exc:
            log_lbc.debug("LBC cookies persistés illisibles: %s", exc)

    def _save_persisted_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        try:
            _LBC_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _LBC_COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
            log_lbc.info("LeBonCoin : %d cookies sauvegardés", len(cookies))
        except Exception as exc:
            log_lbc.debug("LBC save cookies: %s", exc)

    def bootstrap(self, url: str, *, headless: bool = True) -> None:
        self._headless = headless if headless is not None else self._headless
        start = (url or "https://www.leboncoin.fr").strip()
        if not start.startswith("http"):
            start = "https://www.leboncoin.fr/recherche"
        if self._bootstrapped and self.session.cookies and self._probe_api_ok():
            return
        if self.session.cookies and self._probe_api_ok():
            self._bootstrapped = True
            return
        ok = self._bootstrap_browser(start)
        self._bootstrapped = ok or bool(self.session.cookies)
        if not self._bootstrapped:
            log_lbc.warning("LeBonCoin : bootstrap sans cookies Datadome — requêtes API à risque")

    def ensure_session(self, query: str = "", *, headless: bool = True) -> bool:
        """Bootstrap + probe API — régénère cookies si session bloquée."""
        self._headless = headless
        self._load_persisted_cookies()
        q = (query or "console").strip()
        start = self.build_search_url(q) if q else "https://www.leboncoin.fr/recherche"
        if self._bootstrapped and self.session.cookies and self._probe_api_ok(force=True):
            return True
        if self._session_blocked and time.monotonic() < self._bootstrap_cooldown_until:
            wait_min = max(1, int((self._bootstrap_cooldown_until - time.monotonic()) / 60))
            log_lbc.warning(
                "LeBonCoin : IP Datadome chaude — cooldown %d min (cookies persistés conservés)",
                wait_min,
            )
            return False
        max_boot = max(1, int(os.environ.get("VF_LBC_MAX_BOOTSTRAP", "1") or "1"))
        if self._session_blocked or (self._bootstrapped and not self._probe_api_ok(force=True)):
            if self._bootstrap_attempts >= max_boot:
                log_lbc.warning("LeBonCoin : limite bootstrap (%d) — pas de rebootstrap agressif", max_boot)
                return False
            log_lbc.info("LeBonCoin : session expirée — régénération cookies (tentative %d/%d)", self._bootstrap_attempts + 1, max_boot)
            self.invalidate_session(clear_persisted=False)
        self._bootstrap_attempts += 1
        self.bootstrap(start, headless=headless)
        if self._probe_api_ok(force=True):
            self._bootstrap_attempts = 0
            return True
        auto_headed = (
            os.environ.get("VF_LBC_AUTO_HEADED", "0").strip().lower() not in ("0", "false", "no")
            or os.environ.get("VF_LBC_HEADED", "0").strip().lower() in ("1", "true", "yes")
        )
        if headless and auto_headed and self._bootstrap_attempts < max_boot:
            log_lbc.info("LeBonCoin : repli navigateur visible (Datadome)")
            self.bootstrap(start, headless=False)
        ok = self._probe_api_ok(force=True)
        if not ok and self._session_blocked:
            self.invalidate_session(clear_persisted=True)
        return ok

    def _is_block_response(self, res: requests.Response) -> bool:
        if res.status_code in (403, 429, 503):
            return True
        body = (res.text or "").lower()[:2000]
        return any(
            x in body
            for x in (
                "captcha-delivery",
                "geo.captcha",
                "unusual activity",
                "temporarily restricted",
                "access is temporarily",
            )
        )

    def _probe_api_ok(self, *, force: bool = False) -> bool:
        if self._session_blocked and not force:
            return False
        if not self.session.cookies:
            self._load_persisted_cookies()
        try:
            self._rate.acquire()
            res = self.session.post(
                LBC_API_URL,
                json={
                    "limit": 1,
                    "offset": 0,
                    "filters": {"keywords": {"text": "test"}, "ranges": {"price": {"min": 1, "max": 99999}}},
                },
                timeout=min(12.0, self._timeout),
            )
            if self._is_block_response(res):
                self._session_blocked = True
                return False
            self._session_blocked = False
            return res.status_code == 200
        except Exception:
            return False

    def _allow_headed_recovery(self) -> bool:
        if self._headed_recovery_used:
            return False
        return os.environ.get("VF_LBC_HEADED", "0").strip().lower() in ("1", "true", "yes")

    def _bootstrap_browser(self, start_url: str) -> bool:
        if time.monotonic() < self._bootstrap_cooldown_until:
            log_lbc.warning("LeBonCoin : bootstrap ignoré (cooldown Datadome actif)")
            return False
        log_lbc.info("LeBonCoin : bootstrap session (Datadome)…")
        try:
            from .engine import (
                bootstrap_timeout_sec,
                detect_chrome_version_main,
                dismiss_cookie_banners,
                driver_get_safe,
                launch_uc_chrome,
                safe_quit_driver,
            )
        except Exception as exc:
            log_lbc.error("undetected-chromedriver indisponible : %s", exc)
            return False
        wait_headed = float(os.environ.get("VF_LBC_CAPTCHA_WAIT", "12") or "12")
        page_timeout = int(min(55, bootstrap_timeout_sec("leboncoin") - 15))
        for attempt in range(2):
            driver = None
            try:
                driver = launch_uc_chrome(
                    headless=self._headless,
                    version_main=detect_chrome_version_main(),
                    extra_args=[f"--user-agent={DEFAULT_UA}", "--start-maximized"],
                )
                driver.set_page_load_timeout(max(20, page_timeout))
                driver_get_safe(driver, start_url, retries=2)
                time.sleep(random.uniform(2.0, 3.5))
                dismiss_cookie_banners(driver)
                if not self._headless:
                    log_lbc.info("LeBonCoin : attente %.0fs (challenge Datadome si affiché)…", wait_headed)
                    time.sleep(wait_headed)
                else:
                    time.sleep(random.uniform(3.0, 5.0))
                _ = driver.current_url
                cookies = driver.get_cookies()
                for c in cookies:
                    self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
                log_lbc.info("LeBonCoin : %d cookies importés", len(cookies))
                self._save_persisted_cookies(cookies)
                self._bootstrap_at = time.monotonic()
                self._bootstrapped = True
                self._session_blocked = False
                return len(cookies) > 0
            except Exception as exc:
                log_lbc.error("LeBonCoin bootstrap %d/2 : %s", attempt + 1, exc)
                time.sleep(2.0)
            finally:
                safe_quit_driver(driver)
        return False

    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:
        flt = filters or {}
        parts = [f"text={quote_plus(query)}"]
        if flt.get("price_min") is not None:
            parts.append(f"price_min={int(flt['price_min'])}")
        if flt.get("price_max") is not None:
            parts.append(f"price_max={int(flt['price_max'])}")
        return "https://www.leboncoin.fr/recherche?" + "&".join(parts)

    def parse_params(self, url: str) -> Dict[str, Any]:
        parsed = urlparse((url or "").strip())
        qs = parse_qs(parsed.query)
        out: Dict[str, Any] = {"url": url}
        for key in ("text", "keywords", "q"):
            if qs.get(key):
                out["search_text"] = qs[key][0]
                break
        if qs.get("price_min"):
            try:
                out["price_min"] = int(qs["price_min"][0])
            except (TypeError, ValueError):
                pass
        if qs.get("price_max"):
            try:
                out["price_max"] = int(qs["price_max"][0])
            except (TypeError, ValueError):
                pass
        if not out.get("search_text"):
            m = re.search(r"/recherche/([^/?#]+)", parsed.path or "")
            if m:
                out["search_text"] = m.group(1).replace("-", " ")
        return out

    def _search_api(
        self,
        keywords: str,
        *,
        min_price: int = 0,
        max_price: int = 99999,
        shippable: bool = True,
        limit: int = 35,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort_by": "time",
            "sort_order": "desc",
            "filters": {
                "enums": {"ad_type": ["offer"]},
                "keywords": {"text": keywords},
                "ranges": {"price": {"min": min_price, "max": max_price}},
            },
        }
        ship_env = os.environ.get("VF_LBC_SHIPPABLE", "1").strip().lower()
        if shippable and ship_env not in ("0", "false", "no"):
            payload["filters"]["location"] = {"shippable": True}
        self._rate.acquire()
        res = self.session.post(LBC_API_URL, json=payload, timeout=self._timeout)
        if self._is_block_response(res):
            log_lbc.warning("LeBonCoin bloqué (Datadome) — status=%s", res.status_code)
            self._session_blocked = True
            self.invalidate_session()
            if os.environ.get("VF_LBC_RETRY_ON_BLOCK", "1") == "0":
                return []
            retries = max(1, int(os.environ.get("VF_LBC_BLOCK_RETRIES", "2") or "2"))
            base_backoff = max(0.4, float(os.environ.get("VF_LBC_BLOCK_BACKOFF_MS", "1200") or "1200") / 1000.0)
            for attempt in range(retries):
                wait_s = base_backoff * (2**attempt)
                time.sleep(wait_s)
                self.ensure_session(keywords, headless=self._headless)
                if not self._probe_api_ok(force=True):
                    if self._allow_headed_recovery() and self._headless:
                        self._headed_recovery_used = True
                        log_lbc.info("LeBonCoin : tentative recovery navigateur visible")
                        self.ensure_session(keywords, headless=False)
                    elif attempt + 1 >= retries:
                        return []
                self._rate.acquire()
                res = self.session.post(LBC_API_URL, json=payload, timeout=self._timeout)
                if not self._is_block_response(res):
                    self._session_blocked = False
                    break
                if attempt + 1 >= retries:
                    return []
        if res.status_code != 200:
            log_lbc.error("LeBonCoin API %s : %s", res.status_code, (res.text or "")[:200])
            return []
        try:
            data = res.json()
        except ValueError:
            return []
        ads = data.get("ads") or data.get("results") or []
        return ads if isinstance(ads, list) else []

    def search_raw(
        self,
        keywords: str,
        *,
        min_price: int = 0,
        max_price: int = 99999,
        shippable: bool = True,
        max_items: int = 0,
    ) -> List[Dict[str, Any]]:
        if not self._bootstrapped or not self._probe_api_ok():
            self.ensure_session(keywords, headless=self._headless)
        out: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        offset = 0
        page_size = 35
        max_offset = int(os.environ.get("VF_LBC_MAX_OFFSET", "700") or "700")
        while True:
            batch = self._search_api(
                keywords,
                min_price=min_price,
                max_price=max_price,
                shippable=shippable,
                limit=page_size,
                offset=offset,
            )
            if not batch:
                break
            for ad in batch:
                if not isinstance(ad, dict):
                    continue
                aid = str(ad.get("list_id") or ad.get("id") or "")
                if aid and aid in seen_ids:
                    continue
                if aid:
                    seen_ids.add(aid)
                out.append(ad)
            if max_items and len(out) >= max_items:
                out = out[:max_items]
                break
            if len(batch) < page_size:
                break
            offset += page_size
            if offset >= max_offset:
                break
        return out

    def fetch_catalog(
        self,
        url: str,
        *,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[CatalogItem]:
        params = self.parse_params(url)
        keywords = str(params.get("search_text") or "").strip()
        if not keywords and url and not url.startswith("http"):
            keywords = url.strip()
        if not keywords:
            log_lbc.warning("LeBonCoin : aucun mot-clé (text=) dans l’URL")
            return []
        min_p = int(params.get("price_min") or 0)
        max_p = int(params.get("price_max") or 99999)
        cap = max_items or 120
        raw_ads = self.search_raw(keywords, min_price=min_p, max_price=max_p, max_items=cap)
        items = [CatalogItem.from_leboncoin(ad) for ad in raw_ads if isinstance(ad, dict)]
        if on_page:
            try:
                on_page(1, 1, len(items))
            except Exception:
                pass
        return items

    def fetch_by_terms(
        self,
        terms: str,
        *,
        max_items: int = 0,
        price_min: int = 0,
        price_max: int = 99999,
    ) -> List[CatalogItem]:
        url = self.build_search_url(terms, filters={"price_min": price_min, "price_max": price_max})
        return self.fetch_catalog(url, max_items=max_items)

    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:
        """Détail minimal via page publique (si API search n'a pas le body)."""
        lid = str(item_id or "").strip()
        if not lid:
            return None
        url = f"https://www.leboncoin.fr/ad/{lid}" if lid.isdigit() else lid
        if not url.startswith("http"):
            return None
        try:
            self._rate.acquire()
            res = self.session.get(url, timeout=self._timeout, headers={"accept": "text/html"})
            if res.status_code != 200:
                return None
            html = res.text or ""
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            title = soup.select_one("h1") or soup.select_one("[data-qa-id='adview_title']")
            body = soup.select_one("[data-qa-id='adview_description_container']") or soup.select_one(
                "div[class*='Description']"
            )
            desc = body.get_text("\n", strip=True) if body else ""
            if not desc and len(html) > 500:
                return None
            return {
                "id": lid,
                "title": title.get_text(strip=True) if title else "",
                "description": desc[:8000],
            }
        except Exception:
            return None

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

# ================================================== wallapop.py

import logging
import os
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from .market import CatalogItem, MarketplaceAdapter, RateLimiter

logger = logging.getLogger("vf.wallapop")

WALLAPOP_API = "https://api.wallapop.com/api/v3/search/section"
DEFAULT_LAT = "48.8582"
DEFAULT_LON = "2.3387"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class WallapopAdapter(MarketplaceAdapter):
    name = "wallapop"

    def __init__(self, *, headless: bool = True, timeout: float = 20.0, **kwargs: Any) -> None:
        self._headless = headless
        self._timeout = timeout
        self._search_id = str(uuid.uuid4())
        self._device_id = os.environ.get("VF_WALLAPOP_DEVICE_ID", "").strip() or str(uuid.uuid4())
        rpm = float(os.environ.get("VF_WALLAPOP_RATE_PER_MIN", "24") or "24")
        min_ms = float(os.environ.get("VF_WALLAPOP_MIN_INTERVAL_MS", "1000") or "1000")
        max_ms = float(os.environ.get("VF_WALLAPOP_MAX_INTERVAL_MS", "2500") or "2500")
        self._rate = RateLimiter(
            rate_per_minute=max(8.0, rpm),
            min_interval=max(0.05, min_ms / 1000.0),
            max_interval=max(max(0.05, min_ms / 1000.0), max_ms / 1000.0),
        )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en,fr-FR;q=0.9,fr;q=0.8,en-US;q=0.7",
                "x-appversion": "822130",
                "x-deviceid": self._device_id,
                "x-deviceos": "0",
                "deviceos": "0",
                "Origin": "https://fr.wallapop.com",
                "Referer": "https://fr.wallapop.com/",
                "User-Agent": DEFAULT_UA,
            }
        )

    def bootstrap(self, url: str, *, headless: bool = True) -> None:
        try:
            self._rate.acquire()
            self.session.get("https://fr.wallapop.com/", timeout=self._timeout)
            self.session.get(
                "https://fr.wallapop.com/search?keywords=test",
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            log_wallapop.debug("Wallapop bootstrap: %s", exc)

    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:
        flt = filters or {}
        q = quote_plus(query)
        url = f"https://fr.wallapop.com/search?keywords={q}"
        if flt.get("price_max"):
            url += f"&max_sale_price={int(flt['price_max'])}"
        if flt.get("price_min"):
            url += f"&min_sale_price={int(flt['price_min'])}"
        return url

    def parse_params(self, url: str) -> Dict[str, Any]:
        parsed = urlparse((url or "").strip())
        qs = parse_qs(parsed.query)
        out: Dict[str, Any] = {"url": url}
        for key in ("keywords", "keyword", "q", "search"):
            if qs.get(key):
                out["search_text"] = qs[key][0]
                break
        if qs.get("max_sale_price"):
            try:
                out["price_max"] = int(float(qs["max_sale_price"][0]))
            except (TypeError, ValueError):
                pass
        if qs.get("min_sale_price"):
            try:
                out["price_min"] = int(float(qs["min_sale_price"][0]))
            except (TypeError, ValueError):
                pass
        return out

    def _search_page(
        self,
        keywords: str,
        *,
        max_price: Optional[int] = None,
        min_price: Optional[int] = None,
        shippable: Optional[bool] = None,
        latitude: str = DEFAULT_LAT,
        longitude: str = DEFAULT_LON,
        page: int = 1,
        order_by: str = "newest",
        next_page: str = "",
    ) -> tuple[List[Dict[str, Any]], str]:
        if next_page:
            params: Dict[str, str] = {"next_page": next_page}
        else:
            params = {
                "keywords": keywords,
                "source": "search_box",
                "search_id": self._search_id,
                "latitude": latitude,
                "longitude": longitude,
                "order_by": order_by if order_by != "price_low_to_high" else "price_low_to_high",
                "section_type": "organic_search_results",
                "page": str(max(1, int(page))),
            }
            ship = shippable
            if ship is None:
                ship = os.environ.get("VF_WALLAPOP_SHIPPABLE", "0").strip().lower() in ("1", "true", "yes")
            if ship:
                params["is_shippable"] = "true"
            if max_price is not None and max_price > 0:
                params["max_sale_price"] = str(int(max_price))
            if min_price is not None and min_price > 0:
                params["min_sale_price"] = str(int(min_price))
        self._rate.acquire()
        res = self.session.get(WALLAPOP_API, params=params, timeout=self._timeout)
        if res.status_code != 200:
            log_wallapop.error("Wallapop API %s : %s", res.status_code, (res.text or "")[:200])
            return [], ""
        try:
            data = res.json()
        except ValueError:
            return [], ""
        objs = data.get("search_objects") or []
        if not objs:
            section = data.get("data", {}).get("section") if isinstance(data.get("data"), dict) else {}
            if isinstance(section, dict):
                objs = section.get("items") or section.get("components") or []
        if not objs and isinstance(data.get("section"), dict):
            objs = data["section"].get("items") or []
        if not isinstance(objs, list):
            return [], ""
        out_nodes: List[Dict[str, Any]] = []
        for o in objs:
            if not isinstance(o, dict):
                continue
            if o.get("type") == "item" or o.get("object"):
                out_nodes.append(o)
            elif o.get("title") or o.get("id"):
                out_nodes.append({"type": "item", "object": o})
        cursor = str((data.get("meta") or {}).get("next_page") or "")
        return out_nodes, cursor

    def fetch_catalog(
        self,
        url: str,
        *,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[CatalogItem]:
        params = self.parse_params(url)
        keywords = str(params.get("search_text") or "").strip()
        if not keywords and url and not url.startswith("http"):
            keywords = url.strip()
        if not keywords:
            log_wallapop.warning("Wallapop : aucun mot-clé")
            return []
        max_p = params.get("price_max")
        min_p = params.get("price_min")
        cap = max_items or 120
        items: List[CatalogItem] = []
        seen: set[str] = set()
        orders = ["newest"]
        if os.environ.get("VF_WALLAPOP_DUAL_SORT", "1") != "0":
            orders.append("price_low_to_high")
        max_pages = int(os.environ.get("VF_WALLAPOP_MAX_PAGES", "15") or "15")
        for order in orders:
            page = 1
            cursor = ""
            while page <= max_pages:
                nodes, cursor = self._search_page(
                    keywords,
                    max_price=int(max_p) if max_p else None,
                    min_price=int(min_p) if min_p else None,
                    page=page,
                    order_by=order,
                    next_page=cursor,
                )
                if not nodes:
                    break
                added = 0
                for node in nodes:
                    try:
                        it = CatalogItem.from_wallapop(node)
                        pid = str(getattr(it, "platform_item_id", None) or it.id)
                        if pid in seen:
                            continue
                        seen.add(pid)
                        items.append(it)
                        added += 1
                    except Exception as exc:  # noqa: BLE001
                        log_wallapop.debug("Wallapop map skip: %s", exc)
                    if cap and len(items) >= cap:
                        break
                if on_page:
                    try:
                        on_page(page, max_pages, len(items))
                    except Exception:
                        pass
                if cap and len(items) >= cap:
                    break
                if not cursor:
                    break
                page += 1
            if cap and len(items) >= cap:
                break
        return items

    def fetch_by_terms(
        self,
        terms: str,
        *,
        max_items: int = 0,
        price_min: int = 0,
        price_max: int = 0,
    ) -> List[CatalogItem]:
        flt: Dict[str, Any] = {}
        if price_min:
            flt["price_min"] = int(price_min)
        if price_max:
            flt["price_max"] = int(price_max)
        return self.fetch_catalog(self.build_search_url(terms, filters=flt or None), max_items=max_items)

    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:
        return None

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

# ================================================== rakuten.py

import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import requests

from .market import CatalogItem, MarketplaceAdapter, RateLimiter, _parse_amount, detect_chrome_major_version

logger = logging.getLogger("vf.rakuten")
log_rakuten = logger

GRAPHQL_URL = "https://fr.shopping.rakuten.com/restpublic/tech-web/graphql/common"
SEARCH_BASE = "https://fr.shopping.rakuten.com/s/"
_RAKUTEN_COOKIE_PATH = Path("brouillon") / "data" / "vf_cookies" / "rakuten.json"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Requêtes candidates — la bonne opération dépend des déploiements Rakuten.
# Mettre à jour VF_RAKUTEN_GQL_FILE avec la query « listing » capturée dans DevTools.
_GQL_SEARCH_CANDIDATES: List[Dict[str, Any]] = [
    {
        "operationName": "searchProducts",
        "variables": {"query": None, "page": 1, "pageSize": 30},
        "query": (
            "query searchProducts($query: String!, $page: Int, $pageSize: Int) { "
            "searchProducts(query: $query, page: $page, pageSize: $pageSize) { "
            "products { id title price { amount } url imageUrl seller { name } } "
            "} }"
        ),
    },
    {
        "operationName": "productSearch",
        "variables": {"keywords": None, "limit": 30},
        "query": (
            "query productSearch($keywords: String!, $limit: Int) { "
            "productSearch(keywords: $keywords, limit: $limit) { "
            "items { productId name price link picture sellerName } "
            "} }"
        ),
    },
]


def _parse_cookie_header(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _load_env_cookies() -> Dict[str, str]:
    path = os.environ.get("VF_RAKUTEN_COOKIE_FILE", "").strip()
    if path and Path(path).is_file():
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    else:
        raw = os.environ.get("VF_RAKUTEN_COOKIES", "").strip()
    return _parse_cookie_header(raw)


class RakutenAdapter(MarketplaceAdapter):
    name = "rakuten"

    def __init__(self, *, headless: bool = True, timeout: float = 25.0) -> None:
        self._headless = headless
        self._timeout = timeout
        self._bootstrapped = False
        self._bootstrap_at = 0.0
        self._driver: Any = None
        self._html_cache: Dict[str, str] = {}
        self._browser_attempts = 0
        self._browser_gave_up = False
        self._http_bootstrap_once = False
        self._bootstrap_start_url = SEARCH_BASE
        self._rate = RateLimiter(rate_per_minute=12.0, min_interval=2.0, max_interval=4.0)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "content-type": "application/json",
                "origin": "https://fr.shopping.rakuten.com",
                "referer": "https://fr.shopping.rakuten.com/",
                "user-agent": DEFAULT_UA,
            }
        )
        for k, v in _load_env_cookies().items():
            self.session.cookies.set(k, v)
        self._load_persisted_cookies()

    def invalidate_session(self, *, clear_persisted: bool = False) -> None:
        from .engine import safe_quit_driver

        self._bootstrapped = False
        self._bootstrap_at = 0.0
        self._html_cache.clear()
        self._browser_attempts = 0
        self._browser_gave_up = False
        self._http_bootstrap_once = False
        self.session.cookies.clear()
        safe_quit_driver(self._driver)
        self._driver = None
        if clear_persisted:
            try:
                if _RAKUTEN_COOKIE_PATH.is_file():
                    _RAKUTEN_COOKIE_PATH.unlink()
            except OSError:
                pass

    def _load_persisted_cookies(self) -> None:
        try:
            if not _RAKUTEN_COOKIE_PATH.is_file():
                return
            data = json.loads(_RAKUTEN_COOKIE_PATH.read_text(encoding="utf-8"))
            for c in data if isinstance(data, list) else []:
                if isinstance(c, dict) and c.get("name"):
                    self.session.cookies.set(
                        str(c["name"]),
                        str(c.get("value") or ""),
                        domain=c.get("domain") or ".rakuten.com",
                        path=c.get("path") or "/",
                    )
        except Exception as exc:
            log_rakuten.debug("Rakuten cookies: %s", exc)

    def _save_persisted_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        try:
            _RAKUTEN_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _RAKUTEN_COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
            log_rakuten.info("Rakuten : %d cookies sauvegardés", len(cookies))
        except Exception as exc:
            log_rakuten.debug("Rakuten save cookies: %s", exc)

    def _html_get_headers(self) -> Dict[str, str]:
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "referer": "https://fr.shopping.rakuten.com/",
        }

    def _is_block_html(self, html: str) -> bool:
        if not html:
            return True
        low = html.lower()
        head = low[:6000]
        if len(html) < 8000:
            if any(
                x in head
                for x in (
                    "cf-browser-verification",
                    "captcha-delivery",
                    "geo.captcha",
                    "just a moment",
                    "checking your browser",
                    "access denied",
                )
            ):
                return True
            if "data-productid" not in html and "/offer/buy/" not in html and "/offer/shop/" not in html:
                return True
        if "cf-browser-verification" in head or "captcha-delivery" in head or "geo.captcha" in head:
            return True
        if "résultats" in low or "resultats" in low:
            return False
        if "/offer/buy/" in html or "/offer/shop/" in html or "data-productid" in html:
            return False
        if len(html) < 20000 and "noindex, nofollow" in head:
            return True
        return False

    def _driver_alive(self) -> bool:
        drv = self._driver
        if drv is None:
            return False
        try:
            _ = drv.current_url
            return True
        except Exception:
            from .engine import safe_quit_driver

            safe_quit_driver(drv)
            self._driver = None
            return False

    def _probe_ok(self, keywords: str = "test") -> bool:
        url = self.build_search_url(keywords)
        cached = self._html_cache.get(url, "")
        if cached and not self._is_block_html(cached):
            return True
        if self._driver_alive():
            try:
                html = self._driver.page_source or ""
                if html and not self._is_block_html(html):
                    self._html_cache[url] = html
                    return True
            except Exception:
                pass
        try:
            self._rate.acquire()
            res = self.session.get(url, timeout=min(12.0, self._timeout), headers=self._html_get_headers())
            if res.status_code == 200 and not self._is_block_html(res.text or ""):
                self._html_cache[url] = res.text
                return True
        except Exception:
            pass
        return False

    def bootstrap(self, url: str, *, headless: bool = True) -> None:
        self._headless = headless if headless is not None else self._headless
        if self._bootstrapped and self._probe_ok():
            return
        start = (url or SEARCH_BASE).strip()
        if not start.startswith("http"):
            start = SEARCH_BASE
        self._bootstrap_start_url = start
        ok = self._bootstrap_browser(start)
        if ok:
            self._bootstrapped = True
            self._http_bootstrap_once = True
        elif not self._driver_alive() and not self._html_cache:
            self._bootstrapped = False
            log_rakuten.warning("Rakuten : bootstrap échoué — navigateur requis")

    def ensure_session(self, query: str = "", *, headless: bool = True) -> bool:
        self._headless = headless
        q = (query or "console").strip()
        url = self.build_search_url(q)
        if self._probe_ok(q):
            return True
        log_rakuten.info("Rakuten : session invalide — régénération")
        self.invalidate_session()
        self.bootstrap(url, headless=headless)
        if self._probe_ok(q):
            return True
        auto = os.environ.get("VF_RAKUTEN_AUTO_HEADED", "0").strip().lower() not in ("0", "false", "no")
        if headless and auto:
            log_rakuten.info("Rakuten : repli navigateur visible")
            self.invalidate_session()
            self.bootstrap(url, headless=False)
        return self._probe_ok(q)

    def _bootstrap_browser(self, start_url: str) -> bool:
        if self._browser_gave_up:
            return False
        log_rakuten.info("Rakuten : bootstrap Cloudflare / Datadome…")
        try:
            from .engine import (
                dismiss_cookie_banners,
                driver_get_safe,
                launch_uc_chrome,
                safe_quit_driver,
                wait_until_html_ready,
            )
        except Exception as exc:
            log_rakuten.error("undetected-chromedriver indisponible : %s", exc)
            self._browser_gave_up = True
            return False
        wait_s = float(os.environ.get("VF_RAKUTEN_CAPTCHA_WAIT", "8") or "8")
        keep = os.environ.get("VF_RAKUTEN_KEEP_BROWSER", "1").strip().lower() not in ("0", "false", "no")
        try:
            from .engine import bootstrap_timeout_sec

            page_timeout = int(min(50, bootstrap_timeout_sec("rakuten") - 15))
        except Exception:
            page_timeout = 45
        max_attempts = 2 if self._headless else 3
        for attempt in range(max_attempts):
            driver = None
            try:
                if attempt > 0:
                    time.sleep(2.0 * attempt)
                driver = launch_uc_chrome(
                    headless=self._headless,
                    version_main=detect_chrome_major_version(),
                    extra_args=[f"--user-agent={DEFAULT_UA}", "--start-maximized"],
                )
                driver.set_page_load_timeout(max(20, page_timeout))
                driver_get_safe(driver, "https://fr.shopping.rakuten.com/", retries=2)
                time.sleep(random.uniform(1.5, 2.5))
                dismiss_cookie_banners(driver)
                driver_get_safe(driver, start_url, retries=2)
                if not self._headless:
                    log_rakuten.info("Rakuten : attente %.0fs (cookies / challenge)…", wait_s)
                    time.sleep(wait_s)
                dismiss_cookie_banners(driver)
                wait_until_html_ready(
                    driver,
                    is_blocked=self._is_block_html,
                    timeout_s=25.0 if self._headless else 90.0,
                    poll_s=2.0,
                )
                self._prepare_search_page(driver)
                for c in driver.get_cookies():
                    self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
                cookies = driver.get_cookies()
                log_rakuten.info("Rakuten : %d cookies", len(cookies))
                self._save_persisted_cookies(cookies)
                html = driver.page_source or ""
                self._bootstrap_at = time.monotonic()
                if keep:
                    if self._driver is not None:
                        safe_quit_driver(self._driver)
                    self._driver = driver
                    driver = None
                if html and not self._is_block_html(html):
                    self._html_cache[start_url] = html
                    return True
                if cookies:
                    log_rakuten.warning(
                        "Rakuten bootstrap : HTML bloqué (%d octets) — cookies conservés pour fetch navigateur",
                        len(html),
                    )
                    return True
            except Exception as exc:
                err = str(exc).lower()
                log_rakuten.error("Rakuten bootstrap (tentative %d) : %s", attempt + 1, exc)
                if "chromedriver" in err or "winerror" in err:
                    time.sleep(3.0)
            finally:
                safe_quit_driver(driver)
        self._browser_gave_up = True
        log_rakuten.warning("Rakuten : abandon navigateur (trop d'échecs)")
        return False

    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:
        flt = dict(filters or {})
        q = (query or "").strip()
        cat = str(flt.get("category_hint") or flt.get("category") or "").strip().lower()
        pmin = flt.get("price_min")
        pmax = flt.get("price_max")
        use_projector_nav = (
            cat == "projector"
            and any(x in q.lower() for x in ("project", "videoproject", "vidéo"))
            and os.environ.get("VF_RAKUTEN_USE_NAV", "0").strip().lower() in ("1", "true", "yes")
        )
        base = "https://fr.shopping.rakuten.com/nav/Hifi_projecteur" if use_projector_nav else f"{SEARCH_BASE}{quote_plus(q)}"
        extra: Dict[str, str] = {}
        if pmin is not None:
            try:
                extra["price_min"] = str(int(pmin))
            except (TypeError, ValueError):
                pass
        if pmax is not None:
            try:
                extra["price_max"] = str(int(pmax))
            except (TypeError, ValueError):
                pass
        if not extra:
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{urlencode(extra)}"

    def parse_params(self, url: str) -> Dict[str, Any]:
        parsed = urlparse((url or "").strip())
        out: Dict[str, Any] = {"url": url}
        if "/s/" in (parsed.path or ""):
            slug = parsed.path.split("/s/", 1)[-1].strip("/")
            if slug:
                out["search_text"] = slug.replace("+", " ").replace("-", " ")
        from urllib.parse import parse_qs

        qs = parse_qs(parsed.query)
        for key in ("q", "query", "keywords", "kw"):
            if qs.get(key):
                out["search_text"] = qs[key][0]
                break
        if "/nav/Hifi_projecteur" in (parsed.path or ""):
            out["category_hint"] = "projector"
            if not out.get("search_text"):
                out["search_text"] = "projecteur"
        if qs.get("price_min"):
            try:
                out["price_min"] = int(qs["price_min"][0])
            except (TypeError, ValueError):
                pass
        if qs.get("price_max"):
            try:
                out["price_max"] = int(qs["price_max"][0])
            except (TypeError, ValueError):
                pass
        return out

    def _load_custom_gql(self) -> List[Dict[str, Any]]:
        path = os.environ.get("VF_RAKUTEN_GQL_FILE", "").strip()
        if not path or not Path(path).is_file():
            return []
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception as exc:
            log_rakuten.warning("VF_RAKUTEN_GQL_FILE illisible : %s", exc)
        return []

    def _browser_graphql_search(self, keywords: str) -> List[Dict[str, Any]]:
        if not self._driver_alive():
            return []
        try:
            self._driver.set_script_timeout(35)
            for tpl in self._load_custom_gql() + _GQL_SEARCH_CANDIDATES:
                payload = {
                    "operationName": tpl.get("operationName"),
                    "query": tpl.get("query"),
                    "variables": dict(tpl.get("variables") or {}),
                }
                for k, v in payload["variables"].items():
                    if v is None:
                        payload["variables"][k] = keywords
                data = self._driver.execute_async_script(
                    """
                    const body = arguments[0];
                    const done = arguments[arguments.length - 1];
                    fetch('https://fr.shopping.rakuten.com/restpublic/tech-web/graphql/common', {
                      method: 'POST',
                      credentials: 'include',
                      headers: {
                        'content-type': 'application/json',
                        'accept': '*/*',
                      },
                      body: JSON.stringify(body),
                    })
                      .then(r => r.json())
                      .then(j => done(j))
                      .catch(() => done(null));
                    """,
                    payload,
                )
                if isinstance(data, dict):
                    hits = _extract_products_from_gql(data)
                    if hits:
                        log_rakuten.info("Rakuten GQL navigateur %s → %d", payload.get("operationName"), len(hits))
                        return hits
        except Exception as exc:
            log_rakuten.debug("Rakuten browser GQL: %s", exc)
        return []

    def _graphql_search(self, keywords: str) -> List[Dict[str, Any]]:
        candidates = self._load_custom_gql() + _GQL_SEARCH_CANDIDATES
        for tpl in candidates:
            payload = {
                "operationName": tpl.get("operationName"),
                "query": tpl.get("query"),
                "variables": dict(tpl.get("variables") or {}),
            }
            for k, v in payload["variables"].items():
                if v is None:
                    payload["variables"][k] = keywords
            self._rate.acquire()
            try:
                res = self.session.post(GRAPHQL_URL, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                log_rakuten.debug("Rakuten GQL %s : %s", payload.get("operationName"), exc)
                continue
            if res.status_code in (401, 403):
                continue
            if res.status_code != 200:
                continue
            try:
                data = res.json()
            except ValueError:
                continue
            hits = _extract_products_from_gql(data)
            if hits:
                log_rakuten.info("Rakuten GQL %s → %d produits", payload.get("operationName"), len(hits))
                return hits
        return []

    def _prepare_search_page(self, driver: Any) -> None:
        from selenium.webdriver.common.by import By  # type: ignore
        from selenium.webdriver.support.ui import WebDriverWait  # type: ignore

        from .engine import dismiss_cookie_banners

        time.sleep(random.uniform(1.0, 2.0))
        dismiss_cookie_banners(driver)
        time.sleep(0.6)
        try:
            WebDriverWait(driver, 20).until(
                lambda d: "résultats" in (d.page_source or "").lower()
                or "resultats" in (d.page_source or "").lower()
                or len(d.find_elements(By.CSS_SELECTOR, "[data-productid]")) > 2
            )
        except Exception:
            pass
        for step in range(6):
            driver.execute_script(f"window.scrollTo(0, {step * 900});")
            time.sleep(0.4)

    def _browser_fetch_html(self, driver: Any, url: str) -> str:
        try:
            driver.set_script_timeout(35)
            html = driver.execute_async_script(
                """
                const url = arguments[0];
                const done = arguments[arguments.length - 1];
                fetch(url, {credentials: 'include', headers: {accept: 'text/html'}})
                  .then(r => r.text())
                  .then(t => done(t || ''))
                  .catch(() => done(''));
                """,
                url,
            )
            if isinstance(html, str) and html and not self._is_block_html(html):
                return html
        except Exception as exc:
            log_rakuten.debug("Rakuten fetch JS: %s", exc)
        return ""

    def _driver_fetch_html(self, driver: Any, url: str) -> str:
        try:
            from .engine import driver_get_safe

            driver_get_safe(driver, url, retries=2)
            self._prepare_search_page(driver)
            html = driver.page_source or ""
            if self._is_block_html(html):
                html = self._browser_fetch_html(driver, url) or html
            if html and not self._is_block_html(html):
                self._html_cache[url] = html
                log_rakuten.info("Rakuten : HTML navigateur %d octets", len(html))
            if html:
                return html
        except Exception as exc:
            log_rakuten.debug("Rakuten navigateur : %s", exc)
        return ""

    def _fetch_search_html(self, url: str) -> str:
        cached = self._html_cache.get(url)
        if cached and not self._is_block_html(cached):
            return cached
        try:
            self._rate.acquire()
            res = self.session.get(url, timeout=self._timeout, headers=self._html_get_headers())
            if res.status_code == 200 and not self._is_block_html(res.text or ""):
                self._html_cache[url] = res.text
                return res.text
            if res.status_code in (403, 429, 503):
                self.invalidate_session(clear_persisted=True)
        except requests.RequestException as exc:
            log_rakuten.debug("Rakuten HTTP : %s", exc)
        if not self._driver_alive() and not self._browser_gave_up:
            self.ensure_session("", headless=self._headless)
        if self._driver_alive():
            html = self._driver_fetch_html(self._driver, url)
            if html:
                return html
        if not self._browser_gave_up:
            self.invalidate_session()
            self.ensure_session("", headless=self._headless)
            if self._driver_alive():
                html = self._driver_fetch_html(self._driver, url)
                if html:
                    return html
        return ""

    def _page_url(
        self,
        keywords: str,
        page: int,
        *,
        min_price: int = 0,
        max_price: int = 0,
        category_hint: str = "",
    ) -> str:
        flt: Dict[str, Any] = {}
        if min_price > 0:
            flt["price_min"] = int(min_price)
        if max_price > 0:
            flt["price_max"] = int(max_price)
        if category_hint:
            flt["category_hint"] = category_hint
        url = self.build_search_url(keywords, filters=flt or None)
        if page <= 1:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}pa={int(page)}"

    def _html_search(
        self,
        keywords: str,
        *,
        max_pages: int = 0,
        min_price: int = 0,
        max_price: int = 0,
        category_hint: str = "",
    ) -> List[Dict[str, Any]]:
        """Charge les pages /s/… et extrait liens + prix du HTML rendu."""
        pages = max_pages or int(os.environ.get("VF_RAKUTEN_MAX_PAGES", "5") or "5")
        items: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max(1, pages) + 1):
            url = self._page_url(
                keywords,
                page,
                min_price=min_price,
                max_price=max_price,
                category_hint=category_hint,
            )
            html = self._fetch_search_html(url)
            batch: List[Dict[str, Any]] = []
            if html:
                batch = _parse_search_html(html)
            if (
                not batch
                and page == 1
                and str(category_hint or "").strip().lower() == "projector"
                and "/nav/Hifi_projecteur" not in url
            ):
                nav_url = self.build_search_url("projecteur", filters={"category_hint": "projector"})
                nav_html = self._fetch_search_html(nav_url)
                if nav_html:
                    batch = _parse_search_html(nav_html)
            batch = [x for x in batch if _is_rakuten_product_url(str(x.get("url") or ""))]
            if page == 1 and len(batch) < 5 and self._driver_alive():
                sel_items = _parse_search_selenium(self._driver)
                if len(sel_items) > len(batch):
                    batch = sel_items
            new = 0
            for it in batch:
                pid = str(it.get("id") or it.get("url") or "")
                if pid in seen:
                    continue
                seen.add(pid)
                items.append(it)
                new += 1
            if not batch or new < 3:
                break
        if not items:
            log_rakuten.warning("Rakuten HTML vide (Datadome)")
        else:
            log_rakuten.info("Rakuten HTML → %d produits", len(items))
        return items

    def search_raw(
        self,
        keywords: str,
        *,
        max_items: int = 0,
        min_price: int = 0,
        max_price: int = 0,
        category_hint: str = "",
    ) -> List[Dict[str, Any]]:
        if not self._bootstrapped:
            flt: Dict[str, Any] = {}
            if min_price > 0:
                flt["price_min"] = int(min_price)
            if max_price > 0:
                flt["price_max"] = int(max_price)
            if category_hint:
                flt["category_hint"] = category_hint
            self.bootstrap(self.build_search_url(keywords, filters=flt or None), headless=self._headless)
        out = self._graphql_search(keywords)
        if not out and self._driver_alive():
            out = self._browser_graphql_search(keywords)
        if not out:
            out = self._html_search(
                keywords,
                min_price=min_price,
                max_price=max_price,
                category_hint=category_hint,
            )
        if not out and self._driver_alive():
            out = self._browser_graphql_search(keywords)
        if not out and self._headless and self.session.cookies:
            # Rejoue une fois avec cookies persistés si le headless a renvoyé vide.
            self._load_persisted_cookies()
            out = self._html_search(
                keywords,
                min_price=min_price,
                max_price=max_price,
                category_hint=category_hint,
            )
        if max_items and len(out) > max_items:
            out = out[:max_items]
        return out

    def fetch_catalog(
        self,
        url: str,
        *,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[CatalogItem]:
        params = self.parse_params(url)
        keywords = str(params.get("search_text") or "").strip()
        if not keywords and url and not url.startswith("http"):
            keywords = url.strip()
        if not keywords and str(params.get("category_hint") or "").strip().lower() == "projector":
            keywords = "projecteur"
        if not keywords:
            return []
        cap = max_items or 80
        raw = self.search_raw(
            keywords,
            max_items=cap,
            min_price=int(params.get("price_min") or 0),
            max_price=int(params.get("price_max") or 0),
            category_hint=str(params.get("category_hint") or ""),
        )
        items = [CatalogItem.from_rakuten(node) for node in raw if isinstance(node, dict)]
        if on_page:
            try:
                on_page(1, 1, len(items))
            except Exception:
                pass
        return items

    def fetch_by_terms(self, terms: str, *, max_items: int = 0) -> List[CatalogItem]:
        return self.fetch_catalog(self.build_search_url(terms), max_items=max_items)

    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:
        return None

    def close(self) -> None:
        from .engine import safe_quit_driver

        safe_quit_driver(self._driver)
        self._driver = None
        try:
            self.session.close()
        except Exception:
            pass


def _walk_json(node: Any, hits: List[Dict[str, Any]], depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        if {"title", "price"} <= keys or {"name", "price"} <= keys or {"productid", "price"} <= keys:
            if any(node.get(k) for k in ("title", "name", "productId", "id")):
                hits.append(node)
        for v in node.values():
            _walk_json(v, hits, depth + 1)
    elif isinstance(node, list):
        for v in node[:80]:
            _walk_json(v, hits, depth + 1)


def _extract_products_from_gql(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    _walk_json(data, hits)
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for h in hits:
        pid = str(h.get("id") or h.get("productId") or h.get("product_id") or "")
        title = str(h.get("title") or h.get("name") or "").strip()
        if not title or len(title) < 3:
            continue
        key = pid or title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _is_rakuten_product_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u or "/nav/" in u or "/s/" in u or "javascript:" in u:
        return False
    return any(x in u for x in ("/offer/buy/", "/offer/shop/", "/achat/", "/mpitem/", "/p/", "/pv/"))


def _parse_search_selenium(driver: Any) -> List[Dict[str, Any]]:
    """Extraction DOM directe quand le HTML statique ne suffit pas."""
    try:
        from selenium.webdriver.common.by import By  # type: ignore
    except Exception:
        return []
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    base = "https://fr.shopping.rakuten.com"
    price_re = re.compile(r"([\d\s]+[,\.]\d{2})")
    try:
        cards = driver.find_elements(By.CSS_SELECTOR, "[data-productid]")
        for card in cards:
            pid = (card.get_attribute("data-productid") or "").strip()
            if not pid or pid in seen:
                continue
            href = ""
            title = ""
            for a in card.find_elements(
                By.CSS_SELECTOR,
                "a[href*='/offer/buy/'], a[href*='/offer/shop/'], a[title]",
            ):
                href = (a.get_attribute("href") or "").strip()
                if _is_rakuten_product_url(href):
                    title = (a.get_attribute("title") or a.text or "").strip()
                    break
            if not _is_rakuten_product_url(href):
                continue
            if len(title) < 4:
                continue
            price_val: Optional[float] = None
            for qa in ("new_product", "used_product", "new_product_advertised"):
                try:
                    el = card.find_element(By.CSS_SELECTOR, f"[data-qa='{qa}']")
                    pm = price_re.search(el.text.replace("\xa0", " "))
                    if pm:
                        price_val = float(pm.group(1).replace(" ", "").replace(",", "."))
                        break
                except Exception:
                    continue
            if price_val is None or price_val <= 0:
                pm = price_re.search(card.text.replace("\xa0", " "))
                if pm:
                    price_val = float(pm.group(1).replace(" ", "").replace(",", "."))
            if price_val is None or price_val <= 0:
                continue
            image_url = ""
            try:
                for im in card.find_elements(By.CSS_SELECTOR, "img[src], img[data-src]"):
                    iu = (im.get_attribute("src") or im.get_attribute("data-src") or "").strip()
                    if iu and iu.startswith("http"):
                        image_url = iu
                        break
            except Exception:
                pass
            seen.add(pid)
            norm = href.split("?")[0]
            if not norm.startswith("http"):
                norm = base + (norm if norm.startswith("/") else "/" + norm)
            row = {"id": pid, "title": title, "price": price_val, "url": norm}
            if image_url:
                row["imageUrl"] = image_url
            items.append(row)
        if items:
            return items

        # Fallback DOM parsing when Rakuten no longer exposes data-productid.
        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='/offer/buy/'], a[href*='/offer/shop/']")
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            if not _is_rakuten_product_url(href):
                continue
            norm = href.split("?")[0]
            if not norm.startswith("http"):
                norm = base + (norm if norm.startswith("/") else "/" + norm)
            if norm in seen:
                continue
            title = (a.get_attribute("title") or a.text or "").strip()
            if len(title) < 4:
                continue
            price_val: Optional[float] = None
            container = None
            for xp in ("ancestor::*[@data-productid][1]", "ancestor::article[1]", "ancestor::li[1]", "ancestor::div[1]"):
                try:
                    container = a.find_element(By.XPATH, xp)
                    if container:
                        break
                except Exception:
                    continue
            text = (container.text if container else "") or ""
            pm = price_re.search(text.replace("\xa0", " "))
            if pm:
                try:
                    price_val = float(pm.group(1).replace(" ", "").replace(",", "."))
                except ValueError:
                    price_val = None
            if price_val is None or price_val <= 0:
                continue
            pid = norm.rstrip("/").rsplit("/", 1)[-1]
            row = {"id": pid, "title": title, "price": price_val, "url": norm}
            try:
                img_el = (container or a).find_element(By.CSS_SELECTOR, "img[src], img[data-src]")
                iu = (img_el.get_attribute("src") or img_el.get_attribute("data-src") or "").strip()
                if iu:
                    row["imageUrl"] = iu
            except Exception:
                pass
            seen.add(norm)
            items.append(row)
    except Exception as exc:
        log_rakuten.debug("Rakuten Selenium parse: %s", exc)
    return items


def _parse_search_html(html: str) -> List[Dict[str, Any]]:
    """Parse heuristique du listing Rakuten (/s/…)."""
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    base = "https://fr.shopping.rakuten.com"
    price_re = re.compile(r"([\d\s]+[,\.]\d{2})")

    for m in re.finditer(r'data-productid="(\d+)"', html):
        pid = m.group(1)
        if pid in seen:
            continue
        chunk = html[m.start() : m.start() + 12000]
        href_m = re.search(r'href="(/offer/(?:buy|shop)/[^"?]+)', chunk)
        if not href_m:
            continue
        path = href_m.group(1).replace("&amp;", "&").split("?")[0]
        url = path if path.startswith("http") else base + path
        if not _is_rakuten_product_url(url):
            continue
        title_m = re.search(r'title="([^"]{4,220})"', chunk)
        title = title_m.group(1).replace("&amp;", "&").strip() if title_m else ""
        if len(title) < 4:
            continue
        image_url = ""
        im = re.search(r'<img[^>]+(?:data-src|src)="([^"]+)"', chunk, re.I)
        if im:
            image_url = im.group(1).replace("&amp;", "&").strip()
        price_val: Optional[float] = None
        for qa in ("new_product", "used_product", "new_product_advertised"):
            pm = re.search(
                rf'data-qa="{qa}"[^>]*>.*?({price_re.pattern})',
                chunk,
                re.S,
            )
            if pm:
                try:
                    price_val = float(pm.group(1).replace(" ", "").replace(",", "."))
                    break
                except ValueError:
                    continue
        if price_val is None or price_val <= 0:
            prices = price_re.findall(chunk)
            if prices:
                try:
                    price_val = float(prices[0].replace(" ", "").replace(",", "."))
                except ValueError:
                    price_val = None
        if price_val is None or price_val <= 0:
            continue
        seen.add(pid)
        row = {"id": pid, "title": title, "price": price_val, "url": url}
        if image_url:
            row["imageUrl"] = image_url
        items.append(row)

    if items:
        return items

    link_re = re.compile(
        r'href="((?:https://fr\.shopping\.rakuten\.com)?/offer/(?:buy|shop)/[^"#?]+)"',
        re.I,
    )
    for m in link_re.finditer(html):
        path = m.group(1).replace("&amp;", "&")
        norm = path if path.startswith("http") else base + path.split("?")[0]
        if not _is_rakuten_product_url(norm) or norm in seen:
            continue
        chunk = html[m.start() : m.start() + 3000]
        prices = price_re.findall(chunk)
        if not prices:
            continue
        try:
            price_val = float(prices[0].replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        title_m = re.search(r'(?:title|alt|aria-label)="([^"]{4,160})"', chunk, re.I)
        title = title_m.group(1).strip() if title_m else norm.rsplit("/", 1)[-1].replace("-", " ")
        pid = norm.rstrip("/").rsplit("/", 1)[-1]
        seen.add(norm)
        items.append({"id": pid, "title": title, "price": price_val, "url": norm})
    return items

# ================================================== cashexpress.py

import logging
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

import requests

from .market import CatalogItem, MarketplaceAdapter, RateLimiter, detect_chrome_major_version

log_cex = logging.getLogger("vf.cashexpress")

BASE_URL = "https://www.cashexpress.fr/produits-occasions.html"
SITE_ROOT = "https://www.cashexpress.fr"
_CEX_COOKIE_PATH = Path("brouillon") / "data" / "vf_cookies" / "cashexpress.json"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
PRICE_RE = re.compile(r"(?:partir de|à partir de)\s*([\d\s]+[,\.]\d{2})\s*€", re.I)
_CEX_PRODUCT_HREF_RE = re.compile(r"^/[a-z0-9][^/]*-occasion,\d+\.html$", re.I)


def _is_cex_product_href(href: str) -> bool:
    path = (href or "").split("?")[0].strip()
    if not path or "/produits-occasions/" in path:
        return False
    return bool(_CEX_PRODUCT_HREF_RE.match(path))


class CashExpressAdapter(MarketplaceAdapter):
    name = "cashexpress"

    def __init__(self, *, headless: bool = True, timeout: float = 25.0) -> None:
        self._headless = headless
        self._timeout = timeout
        self._bootstrapped = False
        self._bootstrap_at = 0.0
        self._driver: Any = None
        self._html_cache: Dict[str, str] = {}
        self._browser_attempts = 0
        self._browser_gave_up = False
        self._http_bootstrap_once = False
        self._bootstrap_start_url = BASE_URL
        self._rate = RateLimiter(rate_per_minute=20.0, min_interval=1.2, max_interval=2.8)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "user-agent": DEFAULT_UA,
            }
        )
        self._load_persisted_cookies()

    def invalidate_session(self, *, clear_persisted: bool = False) -> None:
        from .engine import safe_quit_driver

        self._bootstrapped = False
        self._bootstrap_at = 0.0
        self._html_cache.clear()
        self._browser_attempts = 0
        self._browser_gave_up = False
        self._http_bootstrap_once = False
        self.session.cookies.clear()
        safe_quit_driver(self._driver)
        self._driver = None
        if clear_persisted:
            try:
                if _CEX_COOKIE_PATH.is_file():
                    _CEX_COOKIE_PATH.unlink()
            except OSError:
                pass

    def _load_persisted_cookies(self) -> None:
        try:
            if not _CEX_COOKIE_PATH.is_file():
                return
            data = json.loads(_CEX_COOKIE_PATH.read_text(encoding="utf-8"))
            for c in data if isinstance(data, list) else []:
                if isinstance(c, dict) and c.get("name"):
                    self.session.cookies.set(
                        str(c["name"]),
                        str(c.get("value") or ""),
                        domain=c.get("domain") or ".cashexpress.fr",
                        path=c.get("path") or "/",
                    )
        except Exception as exc:
            log_cex.debug("CEX cookies: %s", exc)

    def _save_persisted_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        try:
            _CEX_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CEX_COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
            log_cex.info("Cash Express : %d cookies sauvegardés", len(cookies))
        except Exception as exc:
            log_cex.debug("CEX save cookies: %s", exc)

    def _is_block_html(self, html: str) -> bool:
        if not html:
            return True
        low = html.lower()
        if "unable_to_access" in low or "access denied" in low[:2000]:
            return True
        if re.search(r"-occasion,\d+\.html", html):
            return False
        if "produits-occasions" in low and (
            'class="product-item' in low
            or 'class="products-grid' in low
            or 'class="product-item-info' in low
        ):
            return False
        return len(html) < 9000

    def _driver_alive(self) -> bool:
        drv = self._driver
        if drv is None:
            return False
        try:
            _ = drv.current_url
            return True
        except Exception:
            from .engine import safe_quit_driver

            safe_quit_driver(drv)
            self._driver = None
            return False

    def _fetch_timeout_sec(self) -> float:
        try:
            return min(150.0, max(10.0, float(os.environ.get("VF_CEX_FETCH_TIMEOUT", "120") or "120")))
        except (TypeError, ValueError):
            return 120.0

    def _begin_fetch_budget(self) -> None:
        self._fetch_deadline = time.monotonic() + self._fetch_timeout_sec()

    def _fetch_deadline_remaining(self) -> float:
        dl = float(getattr(self, "_fetch_deadline", 0) or 0)
        if not dl:
            return self._fetch_timeout_sec()
        return max(0.0, dl - time.monotonic())

    def _fetch_budget_exhausted(self) -> bool:
        dl = float(getattr(self, "_fetch_deadline", 0) or 0)
        return bool(dl) and time.monotonic() >= dl

    def detach_driver(self) -> None:
        """Lâche le driver sans quit — évite blocage quit() après timeout fetch."""
        self._driver = None

    def _probe_ok(self, keywords: str = "test") -> bool:
        url = self.build_search_url(keywords)
        cached = self._html_cache.get(url, "")
        if cached and not self._is_block_html(cached):
            return True
        if self._driver_alive() and self._bootstrapped and time.monotonic() - self._bootstrap_at < 300:
            return True
        try:
            self._rate.acquire()
            res = self.session.get(BASE_URL, params={"recherche": keywords}, timeout=min(12.0, self._timeout))
            if res.status_code == 200 and not self._is_block_html(res.text or ""):
                self._html_cache[url] = res.text
                return True
        except requests.RequestException:
            pass
        return False

    def _prepare_search_page(self, driver: Any) -> None:
        from .engine import dismiss_cashexpress_modals

        time.sleep(random.uniform(1.0, 2.0))
        dismiss_cashexpress_modals(driver)
        time.sleep(0.8)
        for step in range(10):
            try:
                driver.execute_script(f"window.scrollTo(0, {step * 900});")
            except Exception:
                break
            time.sleep(0.35)

    def _catalog_url_actuelle(self, category_hint: str = "") -> str:
        cat = (category_hint or "").strip().lower()
        if cat in ("console_ds", "console"):
            return f"{SITE_ROOT}/produits-occasions/jeux-video,42.html"
        return f"{SITE_ROOT}/produits-occasions.html"

    def _browser_catalog_fetch(
        self,
        driver: Any,
        keywords: str,
        *,
        page: int = 1,
        min_price: int = 0,
        max_price: int = 0,
        category_hint: str = "",
    ) -> str:
        remaining = self._fetch_deadline_remaining()
        if remaining <= 0:
            return ""
        url_actuelle = self._catalog_url_actuelle(category_hint)
        payload: Dict[str, str] = {
            "url_actuelle": url_actuelle,
            "nom_produit": (keywords or "").strip(),
            "id_magasin": "",
            "hide-price-range-min": str(int(min_price)) if min_price else "",
            "hide-price-range-max": str(int(max_price)) if max_price else "",
            "page": str(int(page)),
        }
        if min_price:
            payload["prix_min"] = str(int(min_price))
        if max_price:
            payload["prix_max"] = str(int(max_price))
        try:
            driver.set_script_timeout(max(8, min(45, int(remaining))))
            html = driver.execute_async_script(
                """
                const payload = arguments[0];
                const done = arguments[arguments.length - 1];
                const body = new URLSearchParams(payload);
                fetch('/extranet/franchises_front/filtres_catalogue', {
                  method: 'POST',
                  credentials: 'include',
                  headers: {
                    'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'x-requested-with': 'XMLHttpRequest',
                  },
                  body,
                })
                  .then(r => r.text())
                  .then(t => {
                    if (!t) { done(''); return; }
                    if (t.trim().startsWith('{')) {
                      try {
                        const j = JSON.parse(t);
                        const u = j && j.url;
                        if (!u) { done(''); return; }
                        const abs = u.startsWith('http') ? u : (location.origin + u);
                        const sep = abs.includes('?') ? '&' : '?';
                        const pageNum = parseInt(payload.page || '1', 10);
                        const finalUrl = pageNum > 1 && !abs.includes('page=')
                          ? abs + sep + 'page=' + pageNum
                          : abs;
                        return fetch(finalUrl, { credentials: 'include' })
                          .then(r => r.text())
                          .then(h => done(h || ''));
                      } catch (e) { done(''); return; }
                    }
                    done(t);
                  })
                  .catch(() => done(''));
                """,
                payload,
            )
            return html if isinstance(html, str) else ""
        except Exception as exc:
            log_cex.debug("CEX catalog fetch JS: %s", exc)
            return ""

    def _submit_browser_search(self, driver: Any, keywords: str) -> None:
        try:
            from selenium.webdriver.common.by import By  # type: ignore
            from selenium.webdriver.common.keys import Keys  # type: ignore
        except Exception:
            return
        q = (keywords or "").strip()
        if not q:
            return
        for sel in ("#recherche", "input[name='recherche']", "input[type='search']"):
            try:
                inp = driver.find_element(By.CSS_SELECTOR, sel)
                if inp and inp.is_displayed():
                    inp.clear()
                    inp.send_keys(q)
                    inp.send_keys(Keys.RETURN)
                    time.sleep(random.uniform(2.0, 3.5))
                    return
            except Exception:
                continue

    def _category_urls(self, category_hint: str) -> List[str]:
        cat = (category_hint or "").strip().lower()
        if cat == "projector":
            return [f"{SITE_ROOT}/produits-occasions/hifi-son,98.html"]
        if cat in ("console_ds", "console"):
            return [f"{SITE_ROOT}/produits-occasions/jeux-video,42.html"]
        return []

    @staticmethod
    def _matches_keywords(
        title: str,
        keywords: str,
        *,
        category_hint: str = "",
        url: str = "",
    ) -> bool:
        blob = f"{title or ''} {url or ''}".lower()
        if not blob.strip():
            return False
        kw_l = (keywords or "").lower()
        parts = [p for p in re.split(r"[\s/+,-]+", kw_l) if len(p) >= 3]
        if not parts:
            return True
        if any(p in blob for p in parts):
            return True
        cat = (category_hint or "").strip().lower()
        if cat == "projector":
            aliases = ("projecteur", "projector", "videoproj", "vidéoproj", "beamer", "epson", "benq", "optoma")
            return any(a in blob for a in aliases)
        compact_kw = re.sub(r"[\s_+-]+", "", kw_l)
        compact_blob = re.sub(r"[\s_+-]+", "", blob)
        return bool(compact_kw) and compact_kw in compact_blob

    def bootstrap(self, url: str, *, headless: bool = True) -> None:
        self._headless = headless if headless is not None else self._headless
        if self._bootstrapped and self._probe_ok():
            return
        start = (url or BASE_URL).strip()
        if not start.startswith("http"):
            start = BASE_URL
        self._bootstrap_start_url = start
        ok = self._bootstrap_browser(start)
        if ok:
            self._bootstrapped = True
            self._http_bootstrap_once = True
        elif not self._driver_alive() and not self._html_cache:
            self._bootstrapped = False
            log_cex.warning("Cash Express : bootstrap échoué — navigateur requis")

    def ensure_session(self, query: str = "", *, headless: bool = True) -> bool:
        self._headless = headless
        q = (query or "console").strip()
        url = self.build_search_url(q)
        if self._probe_ok(q):
            return True
        log_cex.info("Cash Express : session invalide — régénération")
        self.invalidate_session()
        self.bootstrap(url, headless=headless)
        if self._probe_ok(q):
            return True
        auto = os.environ.get("VF_CEX_AUTO_HEADED", "0").strip().lower() not in ("0", "false", "no")
        if headless and auto and not self._browser_gave_up:
            log_cex.info("Cash Express : repli navigateur visible")
            self.invalidate_session()
            self.bootstrap(url, headless=False)
        return self._probe_ok(q)

    def _bootstrap_browser(self, start_url: str) -> bool:
        if self._browser_gave_up:
            return False
        log_cex.info("Cash Express : bootstrap navigateur…")
        try:
            from .engine import detect_chrome_version_main, driver_get_safe, launch_uc_chrome, safe_quit_driver
        except Exception as exc:
            log_cex.warning("uc indisponible : %s", exc)
            self._browser_gave_up = True
            return False
        wait_s = float(os.environ.get("VF_CEX_CAPTCHA_WAIT", "10") or "10")
        keep = os.environ.get("VF_CEX_KEEP_BROWSER", "1").strip().lower() not in ("0", "false", "no")
        for attempt in range(2):
            driver = None
            try:
                driver = launch_uc_chrome(
                    headless=self._headless,
                    version_main=detect_chrome_version_main(),
                    extra_args=[f"--user-agent={DEFAULT_UA}", "--start-maximized"],
                )
                driver.set_page_load_timeout(int(self._timeout) + 40)
                boot_url = start_url if start_url.startswith("http") else BASE_URL
                driver_get_safe(driver, boot_url, retries=2)
                if not self._headless:
                    log_cex.info("Cash Express : attente %.0fs (cookies / modales)…", wait_s)
                    time.sleep(wait_s)
                self._prepare_search_page(driver)
                cookies = driver.get_cookies()
                for c in cookies:
                    self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
                self._save_persisted_cookies(cookies)
                html = driver.page_source or ""
                if html and not self._is_block_html(html):
                    self._html_cache[start_url] = html
                self._bootstrap_at = time.monotonic()
                if keep:
                    if self._driver is not None:
                        safe_quit_driver(self._driver)
                    self._driver = driver
                    driver = None
                return True
            except Exception as exc:
                log_cex.error("Cash Express bootstrap (tentative %d) : %s", attempt + 1, exc)
                time.sleep(1.5)
            finally:
                safe_quit_driver(driver)
        self._browser_gave_up = True
        log_cex.warning("Cash Express : abandon navigateur (trop d'échecs)")
        return False

    def _driver_fetch_html(self, driver: Any, url: str, *, page: int = 1, keywords: str = "") -> str:
        remaining = self._fetch_deadline_remaining()
        if remaining <= 0:
            return ""
        try:
            from selenium.webdriver.common.by import By  # type: ignore

            from .engine import driver_get_safe

            driver.set_page_load_timeout(max(10, min(int(self._timeout) + 40, int(remaining))))

            if page > 1:
                clicked = False
                for sel in (
                    f"a[href*='page={page}']",
                    f"a.page-link[href*='page={page}']",
                    "a.action.next",
                    "li.pages-item-next a",
                    "a[rel='next']",
                ):
                    try:
                        el = driver.find_element(By.CSS_SELECTOR, sel)
                        if el and el.is_displayed():
                            driver.execute_script("arguments[0].click();", el)
                            time.sleep(random.uniform(1.2, 2.0))
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    driver_get_safe(driver, url, retries=2)
            else:
                driver_get_safe(driver, url, retries=2)
            self._prepare_search_page(driver)
            if keywords and "recherche=" in url:
                self._submit_browser_search(driver, keywords)
                self._prepare_search_page(driver)
            html = driver.page_source or ""
            if html and not self._is_block_html(html):
                self._html_cache[url] = html
                log_cex.info("Cash Express : HTML navigateur %d octets (page %d)", len(html), page)
                return html
        except Exception as exc:
            log_cex.debug("Cash Express navigateur : %s", exc)
        return ""

    def _cex_result_count(self, html: str) -> int:
        if not html:
            return 0
        head = html[:120000]
        m = re.search(r"(\d{1,4})\s+r[eé]f[eé]rences?\s+correspondent", head, re.I)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return len({m.group(1) for m in re.finditer(r'href="(/[^"]+-occasion,\d+\.html)"', head) if _is_cex_product_href(m.group(1))})

    def build_search_url(self, query: str, *, filters: Optional[Dict[str, Any]] = None) -> str:
        flt = dict(filters or {})
        parts = [f"recherche={quote_plus((query or '').strip())}"]
        if flt.get("price_min") is not None:
            try:
                parts.append(f"prix_min={int(flt['price_min'])}")
            except (TypeError, ValueError):
                pass
        if flt.get("price_max") is not None:
            try:
                parts.append(f"prix_max={int(flt['price_max'])}")
            except (TypeError, ValueError):
                pass
        return f"{BASE_URL}?{'&'.join(parts)}"

    def parse_params(self, url: str) -> Dict[str, Any]:
        parsed = urlparse((url or "").strip())
        from urllib.parse import parse_qs

        qs = parse_qs(parsed.query)
        out: Dict[str, Any] = {"url": url}
        if qs.get("recherche"):
            out["search_text"] = qs["recherche"][0]
        if qs.get("prix_min"):
            try:
                out["price_min"] = int(qs["prix_min"][0])
            except (TypeError, ValueError):
                pass
        if qs.get("prix_max"):
            try:
                out["price_max"] = int(qs["prix_max"][0])
            except (TypeError, ValueError):
                pass
        return out

    def _fetch_html(
        self,
        keywords: str,
        *,
        page: int = 1,
        min_price: int = 0,
        max_price: int = 0,
        category_hint: str = "",
    ) -> str:
        flt: Dict[str, Any] = {}
        if min_price > 0:
            flt["price_min"] = int(min_price)
        if max_price > 0:
            flt["price_max"] = int(max_price)
        url = self.build_search_url(keywords, filters=flt or None)
        if page > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}page={int(page)}"
        cached = self._html_cache.get(url)
        if cached and not self._is_block_html(cached):
            return cached
        if self._driver_alive():
            remaining = self._fetch_deadline_remaining()
            ajax_html = ""
            if remaining > 1:
                from .engine import run_with_timeout

                try:
                    ajax_html = run_with_timeout(
                        lambda: self._browser_catalog_fetch(
                            self._driver,
                            keywords,
                            page=page,
                            min_price=min_price,
                            max_price=max_price,
                            category_hint=category_hint,
                        ),
                        remaining,
                        label="cex ajax fetch",
                    )
                except TimeoutError:
                    ajax_html = ""
            if ajax_html and not self._is_block_html(ajax_html):
                self._html_cache[url] = ajax_html
                return ajax_html
        if self._fetch_budget_exhausted():
            return ""
        blocked_http = False
        try:
            self._rate.acquire()
            params: Dict[str, Any] = {"recherche": keywords}
            if page > 1:
                params["page"] = str(int(page))
            if min_price > 0:
                params["prix_min"] = str(int(min_price))
            if max_price > 0:
                params["prix_max"] = str(int(max_price))
            http_timeout = min(self._timeout, max(3.0, self._fetch_deadline_remaining()))
            res = self.session.get(BASE_URL, params=params, timeout=http_timeout)
            if res.status_code == 200 and not self._is_block_html(res.text or ""):
                self._html_cache[url] = res.text
                return res.text
            if res.status_code in (403, 429):
                blocked_http = True
        except requests.RequestException as exc:
            log_cex.debug("Cash Express HTTP : %s", exc)
        if blocked_http and not self._driver_alive():
            self.invalidate_session(clear_persisted=True)
        if self._fetch_budget_exhausted():
            return ""
        if self._driver_alive():
            remaining = self._fetch_deadline_remaining()
            html = ""
            if remaining > 1:
                from .engine import run_with_timeout

                try:
                    html = run_with_timeout(
                        lambda: self._driver_fetch_html(self._driver, url, page=page, keywords=keywords),
                        remaining,
                        label="cex driver fetch",
                    )
                except TimeoutError:
                    html = ""
            if html:
                return html
        log_cex.warning("Cash Express HTML vide (anti-bot)")
        return ""

    def _parse_html(self, html: str) -> List[Dict[str, Any]]:
        if not html or self._is_block_html(html):
            return []
        items: List[Dict[str, Any]] = []
        seen: set[str] = set()
        price_re = re.compile(r"([\d\s]+[,\.]\d{2})\s*€?")

        def _append_row(href: str, title: str, price: float, img_url: str = "") -> None:
            norm_href = href.split("?")[0]
            if not _is_cex_product_href(norm_href) or norm_href in seen:
                return
            title = (title or "").strip()
            if len(title) < 4 or title.lower().startswith("produits "):
                slug = norm_href.rsplit("/", 1)[-1].replace("-occasion", "").rsplit(",", 1)[0]
                title = slug.replace("-", " ").strip().title() or title
            seen.add(norm_href)
            pid = re.search(r",(\d+)\.html", norm_href)
            row = {
                "title": title,
                "price": price,
                "url": urljoin(SITE_ROOT, norm_href),
                "id": pid.group(1) if pid else norm_href,
            }
            if img_url:
                row["photo_url"] = urljoin(SITE_ROOT, img_url) if not img_url.startswith("http") else img_url
            items.append(row)

        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            for h3 in soup.select("h3"):
                link = h3.select_one("a[href*='-occasion,']")
                if not link:
                    continue
                href = str(link.get("href") or "").strip()
                if not _is_cex_product_href(href):
                    continue
                card = h3.find_parent(class_=re.compile(r"item_produit|pave-border|product-item"))
                container = card or h3.find_parent("div") or h3.parent
                text = (container.get_text(" ", strip=True) if container else h3.get_text(" ", strip=True)) or ""
                pm = PRICE_RE.search(text) or price_re.search(text)
                if not pm:
                    continue
                try:
                    price = float(pm.group(1).replace(" ", "").replace(",", "."))
                except ValueError:
                    continue
                img_url = ""
                if container:
                    im = container.select_one("img.media_produit, img.contenant-img img, img[src*='images_produits']")
                    if not im and card:
                        im = card.select_one("img.media_produit, img[src*='images_produits']")
                    if im:
                        img_url = str(im.get("src") or im.get("data-src") or "").strip()
                _append_row(href, link.get_text(strip=True), price, img_url)
            if items:
                return items
        except ImportError:
            pass

        link_re = re.compile(r'href="(/[^"]+-occasion,\d+\.html)"', re.I)
        for m in link_re.finditer(html):
            href = m.group(1)
            if not _is_cex_product_href(href):
                continue
            chunk = html[m.start() : m.start() + 3500]
            h3 = re.search(r"<h3[^>]*>\s*<a[^>]*>([^<]{4,220})</a>", chunk, re.I | re.S)
            title = h3.group(1).strip() if h3 else ""
            pm = PRICE_RE.search(chunk) or price_re.search(chunk)
            if not pm:
                continue
            try:
                price_val = float(pm.group(1).replace(" ", "").replace(",", "."))
            except ValueError:
                continue
            image_url = ""
            im = re.search(r'<img[^>]+(?:data-src|src)="([^"]+)"', chunk, re.I)
            if im:
                image_url = im.group(1).strip()
            _append_row(href, title, price_val, image_url)
        return items

    def search_raw(
        self,
        keywords: str,
        *,
        max_items: int = 0,
        min_price: int = 0,
        max_price: int = 0,
        category_hint: str = "",
    ) -> List[Dict[str, Any]]:
        if not self._bootstrapped:
            flt: Dict[str, Any] = {}
            if min_price > 0:
                flt["price_min"] = int(min_price)
            if max_price > 0:
                flt["price_max"] = int(max_price)
            self.bootstrap(self.build_search_url(keywords, filters=flt or None), headless=self._headless)
        merged: List[Dict[str, Any]] = []
        seen: set[str] = set()
        url_price = os.environ.get("VF_CEX_URL_PRICE", "1").strip().lower() not in ("0", "false", "no")
        fetch_min = min_price if url_price else 0
        fetch_max = max_price if url_price else 0
        max_pages = int(os.environ.get("VF_CEX_MAX_PAGES", "12") or "12")
        expected = 0
        zero_streak = 0

        def _collect_from_html(html: str) -> int:
            new = 0
            for it in self._parse_html(html):
                title = str(it.get("title") or "")
                item_url = str(it.get("url") or "")
                if not self._matches_keywords(
                    title, keywords, category_hint=category_hint, url=item_url
                ):
                    continue
                key = str(it.get("id") or item_url or "")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(it)
                new += 1
            return new

        keyword_variants: List[str] = []
        kw_main = (keywords or "").strip()
        if kw_main:
            keyword_variants.append(kw_main)
        compact = re.sub(r"[\s/_+-]+", "", kw_main.lower())
        if compact and compact.lower() not in {x.lower() for x in keyword_variants}:
            keyword_variants.append(compact)
        for kw in keyword_variants:
            if self._fetch_budget_exhausted():
                log_cex.warning("Cash Express : budget fetch épuisé")
                break
            if merged and len(merged) >= max(12, (max_items or 40) // 3):
                break
            pages_for_kw = max_pages
            zero_streak = 0
            for page in range(1, max_pages + 1):
                if self._fetch_budget_exhausted():
                    log_cex.warning("Cash Express : budget fetch épuisé (%r p%d)", kw, page)
                    break
                html = self._fetch_html(
                    kw,
                    page=page,
                    min_price=fetch_min,
                    max_price=fetch_max,
                    category_hint=category_hint,
                )
                if page == 1 and html:
                    expected = self._cex_result_count(html)
                    if expected > 0:
                        per_page = max(12, len(self._parse_html(html)) or 16)
                        pages_for_kw = min(max_pages, max(3, (expected // per_page) + 2))
                        log_cex.info("Cash Express : ~%d refs (%r), max %d pages", expected, kw, pages_for_kw)
                if page > pages_for_kw:
                    break
                batch = self._parse_html(html)
                new = _collect_from_html(html)
                if not batch:
                    break
                if new == 0:
                    zero_streak += 1
                    if zero_streak >= 2:
                        break
                else:
                    zero_streak = 0
                if max_items and len(merged) >= max_items:
                    break
        if len(merged) < max(8, (max_items or 40) // 4) and not self._fetch_budget_exhausted():
            for cat_url in self._category_urls(category_hint):
                if self._fetch_budget_exhausted():
                    break
                html = ""
                if self._driver_alive():
                    html = self._driver_fetch_html(self._driver, cat_url, keywords=keywords)
                added = _collect_from_html(html)
                if added:
                    log_cex.info("Cash Express : catégorie fallback +%d", added)
                if max_items and len(merged) >= max_items:
                    break
        if max_items and len(merged) > max_items:
            merged = merged[:max_items]
        return merged

    def fetch_catalog(
        self,
        url: str,
        *,
        max_items: int = 0,
        on_page: Optional[Callable[[int, int, int], None]] = None,
        category_hint: str = "",
    ) -> List[CatalogItem]:
        params = self.parse_params(url)
        keywords = str(params.get("search_text") or "").strip()
        if not keywords and url and not url.startswith("http"):
            keywords = url.strip()
        if not keywords:
            return []
        self._last_category_hint = str(category_hint or params.get("category_hint") or "")
        self._begin_fetch_budget()
        cap = max_items or 60
        raw = self.search_raw(
            keywords,
            max_items=cap,
            min_price=int(params.get("price_min") or 0),
            max_price=int(params.get("price_max") or 0),
            category_hint=self._last_category_hint,
        )
        items = [CatalogItem.from_cashexpress(node) for node in raw if isinstance(node, dict)]
        if on_page:
            try:
                on_page(1, 1, len(items))
            except Exception:
                pass
        return items

    def fetch_by_terms(self, terms: str, *, max_items: int = 0) -> List[CatalogItem]:
        return self.fetch_catalog(self.build_search_url(terms), max_items=max_items)

    def get_item_detail(self, item_id: int | str) -> Optional[Dict[str, Any]]:
        return None

    def close(self) -> None:
        from .engine import safe_quit_driver

        safe_quit_driver(self._driver)
        self._driver = None
        try:
            self.session.close()
        except Exception:
            pass

__all__ = ["EbayAdapter", "LeBonCoinAdapter", "WallapopAdapter", "RakutenAdapter", "CashExpressAdapter"]


# --- vinted_scrape.py ---

import logging
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger("vf.vinted_scrape")

DEEP_SCRAPE = os.environ.get("VF_DEEP_SCRAPE", "1").strip().lower() not in ("0", "false", "no")


def _per_band_cap() -> int:
    """Max items per band/order pass — NOT VF_VARIANT_MAX (alias count)."""
    try:
        return max(0, int(os.environ.get("VF_VINTED_PER_BAND_MAX", "0") or "0"))
    except (TypeError, ValueError):
        return 0


def _vinted_min_target() -> int:
    try:
        return max(0, int(os.environ.get("VF_VINTED_MIN_TARGET", "1000") or "1000"))
    except (TypeError, ValueError):
        return 1000


# Extra search terms when the primary query is short (PS4, vélo, etc.)
_QUERY_VARIANTS_BY_CAT: Dict[str, List[str]] = {
    "console_ps4": ["PlayStation 4", "PS4 Slim", "PS4 Pro", "Sony PS4"],
    "console_ps5": ["PlayStation 5", "PS5 Slim", "PS5 Digital"],
    "console_switch": ["Nintendo Switch", "Switch OLED"],
    "projector": ["videoprojecteur", "vidéoprojecteur", "projecteur video"],
    "bicycle": ["vélo", "VTT", "VTC"],
}


def _vinted_max_pages() -> int:
    try:
        return int(os.environ.get("VF_VINTED_MAX_PAGES", "0") or "0")
    except (TypeError, ValueError):
        return 0


def _vinted_min_yield_fallback() -> int:
    try:
        return max(10, int(os.environ.get("VF_VINTED_MIN_YIELD_FALLBACK", "20") or "20"))
    except (TypeError, ValueError):
        return 20


def _deep_scrape_enabled() -> bool:
    return os.environ.get("VF_DEEP_SCRAPE", "1").strip().lower() not in ("0", "false", "no")


def _narrow_price_range(pmin: int, pmax: int) -> bool:
    """True si la fourchette utilisateur est trop etroite pour un scrape bandes-only."""
    if pmin <= 0 and pmax <= 0:
        return False
    if pmin > 0 and pmax > 0 and (pmax - pmin) < 120:
        return True
    if pmax > 0 and pmax < 180:
        return True
    return False


# Tranches € pour contourner le plafond de pagination Vinted (~10 pages / ~240-960 items)
PRICE_BANDS_BY_CATEGORY: Dict[str, List[Tuple[int, int]]] = {
    "phone": [(0, 150), (150, 250), (250, 350), (350, 500), (500, 800), (800, 2000)],
    "console_ps4": [(0, 80), (80, 150), (150, 250), (250, 400), (400, 600)],
    "console_ps5": [(0, 150), (150, 300), (300, 500), (500, 800)],
    "console_switch": [(0, 100), (100, 200), (200, 350), (350, 600)],
    "console_ds": [(0, 50), (50, 100), (100, 180), (180, 350), (350, 600)],
    "wearable": [(0, 80), (80, 200), (200, 500), (500, 1200)],
    "laptop": [(0, 200), (200, 400), (400, 700), (700, 1500)],
    "projector": [(50, 120), (120, 200), (200, 350), (350, 600), (600, 1200)],
    "apparel": [(0, 30), (30, 60), (60, 120), (120, 250)],
    "generic": [(0, 50), (50, 120), (120, 250), (250, 500)],
}


def _catalog_id_for(category: str) -> Optional[int]:
    from .market import _VINTED_CATALOG_IDS

    return _VINTED_CATALOG_IDS.get(category)


def build_catalog_url_with_price(
    search_text: str,
    *,
    price_from: int = 0,
    price_to: int = 0,
    base_url: str = "https://www.vinted.fr",
    catalog_id: Optional[int] = None,
) -> str:
    url = build_catalog_url(base_url=base_url, search_text=search_text, per_page=96)
    extra: Dict[str, str] = {}
    if price_from > 0:
        extra["price_from"] = str(price_from)
    if price_to > 0:
        extra["price_to"] = str(price_to)
    if catalog_id:
        extra["catalog[]"] = str(catalog_id)
    if not extra:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode(extra)


def price_band_variants(
    search_text: str,
    *,
    category_hint: str = "",
    base_url: str = "https://www.vinted.fr",
    price_from: int = 0,
    price_to: int = 0,
) -> List[Dict[str, Any]]:
    """Variantes Vinted par tranche de prix (intersectées avec la fourchette utilisateur)."""
    cat = (category_hint or "").lower()
    bands = list(PRICE_BANDS_BY_CATEGORY.get(cat) or [])
    pmin, pmax = int(price_from or 0), int(price_to or 0)
    if pmin or pmax:
        from vf_scrape.pro import bands_for_price_range

        bands = bands_for_price_range(pmin, pmax, preferred=bands, fast=False)
    if not bands:
        return []
    cid = _catalog_id_for(cat)
    out: List[Dict[str, Any]] = []
    for lo, hi in bands:
        url = build_catalog_url_with_price(
            search_text,
            price_from=lo,
            price_to=hi,
            base_url=base_url,
            catalog_id=cid,
        )
        out.append(
            {
                "url": url,
                "search_terms": search_text,
                "price_from": lo,
                "price_to": hi,
                "note": f"bande {lo}-{hi}€",
            }
        )
    return out


def fetch_vinted_band(
    adapter: VintedAdapter,
    url: str,
    *,
    max_items: int = 0,
    max_pages: int = 0,
    on_page: Optional[Callable[[int, int, int], None]] = None,
    scrape_ctx: Optional[Dict[str, Any]] = None,
) -> List[CatalogItem]:
    """Fetch une variante (une bande ou une requête simple)."""
    params = normalize_vinted_catalog_params(adapter.parse_params(url))
    page_cap = max_pages or _vinted_max_pages()
    collected: List[CatalogItem] = []
    page = 1
    total_pages = 1
    empty_streak = 0
    min_global = int((scrape_ctx or {}).get("min_before_stop") or _vinted_min_target())
    gain_stop_pct = float(os.environ.get("VF_VINTED_PAGE_GAIN_PCT", "0.02") or "0.02")
    seen_band: set[str] = set()
    while True:
        if page_cap and page > page_cap:
            break
        try:
            payload = adapter.client.get_catalog_page(params, page=page)
        except ApiError as exc:
            # Vinted occasionally returns 404 (endpoint/WAF) → retry with safer query variants.
            if getattr(exc, "status", None) == 404 and page == 1:
                st = str(params.get("search_text") or "").strip()
                variants = [st]
                if st:
                    variants.extend(
                        [
                            st.replace(" ", "+"),
                            st.replace(" ", ""),
                            st.lower(),
                        ]
                    )
                tried: set[str] = set()
                for alt in [v for v in variants if v and v not in tried]:
                    tried.add(alt)
                    alt_params = dict(params)
                    alt_params["search_text"] = alt
                    try:
                        logger.warning("⚠ Vinted 404 → retry search_text=%r", alt)
                        payload = adapter.client.get_catalog_page(alt_params, page=page)
                        params = alt_params
                        break
                    except ApiError as exc2:
                        if getattr(exc2, "status", None) != 404:
                            raise
                else:
                    raise
            else:
                raise
        items = payload.get("items") or []
        pagination = payload.get("pagination") or {}
        try:
            total_pages = int(pagination.get("total_pages") or total_pages)
        except (TypeError, ValueError):
            pass
        if not items:
            empty_streak += 1
            if empty_streak >= 2:
                break
            page += 1
            continue
        empty_streak = 0
        before_page = len(collected)
        for raw in items:
            if isinstance(raw, dict):
                collected.append(CatalogItem.from_api(raw, base_url=adapter.base_url))
            if max_items and len(collected) >= max_items:
                break
        page_new = 0
        gseen: Optional[set] = None
        if scrape_ctx is not None:
            gseen = scrape_ctx.setdefault("global_seen", set())
        for it in collected[before_page:]:
            pid = str(getattr(it, "platform_item_id", None) or it.id)
            if not pid:
                continue
            seen_band.add(pid)
            if gseen is not None:
                if pid not in gseen:
                    gseen.add(pid)
                    page_new += 1
            else:
                page_new += 1
        if scrape_ctx is not None and gseen is not None:
            scrape_ctx["global_total"] = len(gseen)
        if on_page:
            try:
                on_page(page, total_pages, len(collected))
            except Exception:
                pass
        if max_items and len(collected) >= max_items:
            break
        global_n = int((scrape_ctx or {}).get("global_total") or 0)
        if scrape_ctx and global_n >= min_global and global_n > 0:
            gain = page_new / max(1, global_n)
            if gain < gain_stop_pct:
                break
        if page >= total_pages:
            break
        page += 1
    return collected


def fetch_vinted_deep(
    adapter: VintedAdapter,
    search_text: str,
    *,
    category_hint: str = "",
    max_items: int = 0,
    price_from: int = 0,
    price_to: int = 0,
    on_page: Optional[Callable[[int, int, int], None]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    fast_mode: bool = False,
) -> List[CatalogItem]:
    """
    Récupération exhaustive : alias simple + tranches de prix si DEEP_SCRAPE actif.
    """
    q = (search_text or "").strip()
    if not q:
        return []
    cat = (category_hint or "").lower()
    pmin, pmax = int(price_from or 0), int(price_to or 0)
    has_user_price_range = bool(pmin or pmax)
    api_pmin, api_pmax = pmin, pmax
    bands = price_band_variants(
        q,
        category_hint=cat,
        base_url=adapter.base_url,
        price_from=api_pmin,
        price_to=api_pmax,
    )
    use_only_bands = bool(bands and has_user_price_range and not fast_mode)
    base_variants = 0 if use_only_bands else (1 if fast_mode else 3)
    total_variants = max(1, base_variants + len(bands))
    if max_items:
        per_band = int(math.ceil(max_items / max(1, min(total_variants, 8))))
        per_band = max(240, per_band)
    else:
        per_band = 500
    band_cap = _per_band_cap()
    if band_cap > 0:
        per_band = band_cap
    if per_band <= 0:
        per_band = 400
    if fast_mode:
        per_band = min(per_band, 280)
    fast_pages = int(os.environ.get("VF_VINTED_FAST_PAGES", "12") or "12")
    max_pages_env = int(os.environ.get("VF_VINTED_MAX_PAGES", "18") or "18")
    page_cap = fast_pages if fast_mode else (max_pages_env if max_pages_env > 0 else 18)
    min_target = 0 if fast_mode else _vinted_min_target()
    if max_items and max_items > 0:
        min_target = min(min_target, max_items) if min_target else max_items

    all_items: List[CatalogItem] = []
    seen: set[str] = set()
    scrape_ctx: Dict[str, Any] = {
        "global_total": 0,
        "global_seen": set(),
        "min_before_stop": min_target,
    }

    def _in_price(it: CatalogItem) -> bool:
        try:
            p = float(getattr(it, "total_price", None) or getattr(it, "price", None) or 0)
        except (TypeError, ValueError):
            return True
        if p <= 0:
            return True
        if pmin and p < pmin:
            return False
        if pmax and p > pmax:
            return False
        return True

    def _add(batch: List[CatalogItem]) -> int:
        n = 0
        for it in batch:
            if not _in_price(it):
                continue
            pid = str(getattr(it, "platform_item_id", None) or it.id)
            if pid in seen:
                continue
            seen.add(pid)
            all_items.append(it)
            n += 1
            if max_items and len(all_items) >= max_items:
                break
        return n

    cid = _catalog_id_for(cat)

    def _url_with_price(**extra_order: Any) -> str:
        order = extra_order.get("order")
        url = build_catalog_url_with_price(
            q,
            price_from=api_pmin,
            price_to=api_pmax,
            base_url=adapter.base_url,
            catalog_id=cid,
        )
        if order:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode({"order": order})
        return url

    if not use_only_bands:
        # Requête principale (fourchette utilisateur)
        base_url = _url_with_price()
        if log_fn:
            log_fn("  Vinted deep · requête principale")
        _add(fetch_vinted_band(
            adapter, base_url, max_items=per_band, max_pages=page_cap or 0,
            on_page=on_page, scrape_ctx=scrape_ctx,
        ))

        if not fast_mode:
            rel_url = _url_with_price(order="relevance")
            if log_fn:
                log_fn("  Vinted deep · tri pertinence")
            _add(
                fetch_vinted_band(
                    adapter,
                    rel_url,
                    max_items=per_band,
                    max_pages=page_cap or 0,
                    on_page=on_page,
                    scrape_ctx=scrape_ctx,
                )
            )
            price_url = _url_with_price(order="price_low_to_high")
            if log_fn:
                log_fn("  Vinted deep · tri prix croissant")
            _add(
                fetch_vinted_band(
                    adapter,
                    price_url,
                    max_items=per_band,
                    max_pages=page_cap or 0,
                    on_page=on_page,
                    scrape_ctx=scrape_ctx,
                )
            )

    if not _deep_scrape_enabled():
        return all_items

    min_band_gain = max(3, int(os.environ.get("VF_VINTED_MIN_BAND_GAIN", "5") or "5"))
    max_bands = int(os.environ.get("VF_VINTED_MAX_BANDS", "0") or "0")
    band_list: List[Dict[str, Any]] = []
    if not fast_mode:
        band_list = bands[:max_bands] if max_bands > 0 else bands
    for band in band_list:
        if max_items and len(all_items) >= max_items:
            break
        if log_fn:
            log_fn(f"  Vinted deep · {band['note']}")
        before = len(all_items)
        batch = fetch_vinted_band(
            adapter,
            band["url"],
            max_items=per_band,
            max_pages=page_cap or 0,
            on_page=on_page,
            scrape_ctx=scrape_ctx,
        )
        added = _add(batch)
        if log_fn:
            log_fn(f"    +{added} (total {len(all_items)})")
        if added < min_band_gain and len(all_items) - before < min_band_gain and before >= 40:
            if log_fn:
                log_fn(f"  Vinted deep · gain bande faible ({added}) — bandes suivantes ignorées")
            break

    min_yield = _vinted_min_yield_fallback()
    if len(all_items) < min_yield and not has_user_price_range:
        if log_fn:
            log_fn(
                f"  Vinted · rendement faible ({len(all_items)}<{min_yield}) — retry pagination elargie"
            )
        wide_cap = max(per_band, 480)
        wide_pages = max(page_cap or 0, 12)
        for order in (None, "relevance", "price_low_to_high"):
            if len(all_items) >= min_yield:
                break
            fb_url = build_catalog_url_with_price(
                q,
                price_from=0,
                price_to=0,
                base_url=adapter.base_url,
                catalog_id=None if fast_mode else cid,
            )
            if order:
                sep = "&" if "?" in fb_url else "?"
                fb_url = fb_url + sep + urlencode({"order": order})
            before_fb = len(all_items)
            _add(
                fetch_vinted_band(
                    adapter,
                    fb_url,
                    max_items=wide_cap,
                    max_pages=wide_pages,
                    on_page=on_page,
                    scrape_ctx=scrape_ctx,
                )
            )
            if log_fn and len(all_items) > before_fb:
                log_fn(f"    +{len(all_items) - before_fb} wide ({order or 'default'}) → {len(all_items)}")

  # Extra query variants when volume still below target (short queries like « PS4 »)
    if min_target and len(all_items) < min_target and not fast_mode and not has_user_price_range:
        extra_terms = list(_QUERY_VARIANTS_BY_CAT.get(cat) or [])
        if len(q) <= 12 and q.lower() not in {t.lower() for t in extra_terms}:
            extra_terms = [t for t in extra_terms if t.lower() != q.lower()]
        for alt_q in extra_terms:
            if len(all_items) >= min_target:
                break
            if max_items and len(all_items) >= max_items:
                break
            if log_fn:
                log_fn(f"  Vinted deep · variante requête « {alt_q} » ({len(all_items)}/{min_target})")
            alt_url = build_catalog_url_with_price(
                alt_q,
                price_from=api_pmin,
                price_to=api_pmax,
                base_url=adapter.base_url,
                catalog_id=cid,
            )
            before_alt = len(all_items)
            _add(
                fetch_vinted_band(
                    adapter,
                    alt_url,
                    max_items=max(per_band, min_target - len(all_items)),
                    max_pages=page_cap or 0,
                    on_page=on_page,
                    scrape_ctx=scrape_ctx,
                )
            )
            if log_fn and len(all_items) > before_alt:
                log_fn(f"    +{len(all_items) - before_alt} → total {len(all_items)}")

    if min_target and len(all_items) < min_target:
        msg = (
            f"⚠ Vinted : {len(all_items)} annonces < objectif {min_target} "
            f"(requête {q!r}) — catalogue peut être épuisé"
        )
        logger.warning(msg)
        if log_fn:
            log_fn(msg)
    elif min_target and log_fn:
        log_fn(f"  Vinted · objectif {min_target} atteint ({len(all_items)} uniques)")

    return all_items


# --- search.py ---

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .market import CatalogItem, MarketplaceAdapter, build_catalog_url, get_adapter, normalize_search_name

logger = logging.getLogger("vf.search_builder")


def detect_input_platform(raw: str) -> Optional[str]:
    """Détecte la plateforme depuis une URL utilisateur."""
    low = (raw or "").lower()
    if "leboncoin.fr" in low:
        return "leboncoin"
    if "wallapop.com" in low:
        return "wallapop"
    if "rakuten.com" in low or "fr.shopping.rakuten" in low:
        return "rakuten"
    if "cashexpress.fr" in low:
        return "cashexpress"
    if "ebay." in low:
        return "ebay"
    if "vinted." in low:
        return "vinted"
    return None


@dataclass
class SearchVariant:
    platform: str
    url: str
    search_terms: str
    catalog_id: Optional[int] = None
    expected_volume: int = 0
    negative_keywords: List[str] = field(default_factory=list)
    note: str = ""
    price_min: Optional[int] = None
    price_max: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "url": self.url,
            "search_terms": self.search_terms,
            "catalog_id": self.catalog_id,
            "expected_volume": self.expected_volume,
            "negative_keywords": self.negative_keywords,
            "note": self.note,
            "price_min": self.price_min,
            "price_max": self.price_max,
        }


@dataclass
class SearchPlan:
    canonical_query: str
    variants: List[SearchVariant] = field(default_factory=list)
    estimated_total: int = 0
    estimated_after_dedup: int = 0
    model_key: str = ""
    category_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_query": self.canonical_query,
            "variants": [v.to_dict() for v in self.variants],
            "estimated_total": self.estimated_total,
            "estimated_after_dedup": self.estimated_after_dedup,
            "model_key": self.model_key,
            "category_hint": self.category_hint,
        }


_VINTED_CATALOG_IDS: Dict[str, int] = {
    "console": 2993,
    "console_ps4": 2993,
    "console_ps5": 2993,
    "console_switch": 2993,
    "phone": 3661,
    "projector": 3562,
    "coffee": 3478,
    "bicycle": 1141,
    "camera": 3129,
    "audio": 3561,
    "laptop": 3563,
}


def _alias_variants(query: str, *, platform: str = "vinted", category_hint: str = "") -> Tuple[str, List[str]]:
    from .models import find_model_by_text, get_model, search_aliases

    q = (query or "").strip()
    m = find_model_by_text(q, category_hint)
    if not m:
        m = get_model(normalize_search_name(q).replace("_", ""))
    if m:
        terms = search_aliases(m.key, platform)
        seen: Set[str] = set()
        out: List[str] = []
        for t in [q, *terms]:
            tl = t.strip().lower()
            if tl and tl not in seen:
                seen.add(tl)
                out.append(t.strip())
        return m.key, out
    return "", [q] if q else []


def _llm_expand_queries(query: str, *, platform: str = "vinted", category_hint: str = "") -> List[str]:
    try:
        from .ai import _chat, _extract_json_lenient, api_key

        if not api_key():
            return []
        prompt = (
            f"Produit recherché: {query}\nPlateforme: {platform}\nCatégorie: {category_hint or 'auto'}\n\n"
            "Génère 4-6 termes de recherche utilisés sur les annonces FR (abréviations, fautes courantes).\n"
            'JSON: {"terms":["…"],"negative":["accessoire seul",…]}'
        )
        raw = _chat(prompt, system="Expert annonces occasion FR. JSON strict.", max_tokens=400)
        obj = _extract_json_lenient(raw)
        if isinstance(obj, dict):
            terms = obj.get("terms") or obj.get("queries") or []
            if isinstance(terms, list):
                return [str(t).strip() for t in terms if str(t).strip()][:8]
    except Exception as exc:  # noqa: BLE001
        logger.debug("LLM query expand: %s", exc)
    return []


def _variant_limits(n_platforms: int) -> Dict[str, int]:
    """Variantes par plateforme — vf_scrape étend les alias en amont."""
    try:
        from vf_scrape.config import variant_limits_for_mode

        return variant_limits_for_mode(n_platforms)
    except Exception:
        pass
    if n_platforms >= 6:
        return {"vinted": 3, "leboncoin": 3, "ebay": 1, "wallapop": 1, "rakuten": 0, "cashexpress": 0, "default": 1}
    if n_platforms >= 4:
        return {"vinted": 3, "leboncoin": 2, "ebay": 1, "wallapop": 2, "rakuten": 1, "cashexpress": 0, "default": 1}
    if n_platforms >= 3:
        return {"vinted": 4, "leboncoin": 2, "ebay": 2, "wallapop": 2, "rakuten": 1, "cashexpress": 0, "default": 2}
    return {"vinted": 4, "leboncoin": 3, "ebay": 2, "wallapop": 2, "default": 2}


def build_search_plan(
    query: str,
    *,
    platforms: Optional[List[str]] = None,
    category_hint: str = "",
    use_llm_expand: bool = False,
    vinted_base: str = "https://www.vinted.fr",
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
) -> SearchPlan:
    """
    Layer 1 aliases + optional Layer 2 LLM + Layer 3 catalog-filter URLs.
    """
    q = (query or "").strip()
    plats = _sort_platforms([p.lower() for p in (platforms or ["vinted"])])
    model_key, alias_terms = _alias_variants(q, platform=plats[0], category_hint=category_hint)

    if use_llm_expand and len(alias_terms) <= 2:
        extra = _llm_expand_queries(q, platform=plats[0], category_hint=category_hint)
        seen = {t.lower() for t in alias_terms}
        for t in extra:
            if t.lower() not in seen:
                alias_terms.append(t)
                seen.add(t.lower())

    variants: List[SearchVariant] = []
    cat_id = _VINTED_CATALOG_IDS.get(category_hint) if category_hint else None
    limits = _variant_limits(len(plats))
    pmin = int(price_min) if price_min and int(price_min) > 0 else None
    pmax = int(price_max) if price_max and int(price_max) > 0 else None
    price_flt: Dict[str, Any] = {}
    if pmin:
        price_flt["price_min"] = pmin
    if pmax:
        price_flt["price_max"] = pmax

    for plat in plats:
        cap = limits.get(plat, limits["default"])
        if plat == "vinted":
            term_cap = limits.get("vinted", cap)
            for i, term in enumerate(alias_terms[:term_cap]):
                band_list: List[Dict[str, Any]] = []
                if pmin or pmax:
                    band_list = price_band_variants(
                        term,
                        category_hint=category_hint or "",
                        base_url=vinted_base,
                        price_from=pmin or 0,
                        price_to=pmax or 0,
                    )
                if band_list:
                    max_bands = int(os.environ.get("VF_VINTED_PLAN_MAX_BANDS", "4") or "4")
                    for bi, band in enumerate(band_list[:max_bands]):
                        variants.append(
                            SearchVariant(
                                platform="vinted",
                                url=str(band["url"]),
                                search_terms=term,
                                catalog_id=cat_id if i == 0 and bi == 0 else cat_id,
                                expected_volume=55 if bi == 0 and i == 0 else 35,
                                note=str(band.get("note") or f"bande {bi + 1}"),
                                price_min=int(band.get("price_from") or pmin or 0) or None,
                                price_max=int(band.get("price_to") or pmax or 0) or None,
                            )
                        )
                    continue
                url = build_catalog_url(
                    base_url=vinted_base,
                    search_text=term,
                    price_from=pmin,
                    price_to=pmax,
                )
                if cat_id and i == 0:
                    url = url + f"&catalog[]={cat_id}"
                variants.append(
                    SearchVariant(
                        platform="vinted",
                        url=url,
                        search_terms=term,
                        catalog_id=cat_id,
                        expected_volume=80 if i == 0 else 40,
                        note="alias" if i > 0 else "primary",
                        price_min=pmin,
                        price_max=pmax,
                    )
                )
        elif plat == "ebay":
            from .market import EbayAdapter

            ebay = EbayAdapter()
            for i, term in enumerate(alias_terms[: limits.get("ebay", cap)]):
                variants.append(
                    SearchVariant(
                        platform="ebay",
                        url=ebay.build_search_url(term, filters=price_flt or None),
                        search_terms=term,
                        expected_volume=45 if i == 0 else 22,
                        note="alias" if i > 0 else "primary",
                        price_min=pmin,
                        price_max=pmax,
                    )
                )
        elif plat in ("leboncoin", "lbc"):
            from .market import LeBonCoinAdapter

            lbc = LeBonCoinAdapter()
            for i, term in enumerate(alias_terms[: limits.get("leboncoin", cap)]):
                variants.append(
                    SearchVariant(
                        platform="leboncoin",
                        url=lbc.build_search_url(term, filters=price_flt or None),
                        search_terms=term,
                        expected_volume=50 if i == 0 else 25,
                        note="alias" if i > 0 else "primary",
                        price_min=pmin,
                        price_max=pmax,
                    )
                )
        elif plat == "wallapop":
            from .market import WallapopAdapter

            wap = WallapopAdapter()
            for i, term in enumerate(alias_terms[: limits.get("wallapop", cap)]):
                variants.append(
                    SearchVariant(
                        platform="wallapop",
                        url=wap.build_search_url(term, filters=price_flt or None),
                        search_terms=term,
                        expected_volume=40 if i == 0 else 20,
                        note="alias" if i > 0 else "primary",
                        price_min=pmin,
                        price_max=pmax,
                    )
                )
        elif plat == "rakuten":
            from .market import RakutenAdapter

            rak = RakutenAdapter()
            for i, term in enumerate(alias_terms[:cap]):
                variants.append(
                    SearchVariant(
                        platform="rakuten",
                        url=rak.build_search_url(term, filters=price_flt or None),
                        search_terms=term,
                        expected_volume=35 if i == 0 else 18,
                        note="alias" if i > 0 else "primary",
                        price_min=pmin,
                        price_max=pmax,
                    )
                )
        elif plat == "cashexpress":
            from .market import CashExpressAdapter

            cex = CashExpressAdapter()
            for i, term in enumerate(alias_terms[:cap]):
                variants.append(
                    SearchVariant(
                        platform="cashexpress",
                        url=cex.build_search_url(term, filters=price_flt or None),
                        search_terms=term,
                        expected_volume=30 if i == 0 else 15,
                        note="alias" if i > 0 else "primary",
                        price_min=pmin,
                        price_max=pmax,
                    )
                )
        else:
            variants.append(
                SearchVariant(platform=plat, url="", search_terms=q, note="plateforme non supportée")
            )

    est = sum(v.expected_volume for v in variants)
    dedup_est = max(est // max(1, len(plats) + len(alias_terms) // 3), min(est, 650 if len(plats) <= 2 else 420))

    return SearchPlan(
        canonical_query=q,
        variants=variants,
        estimated_total=est,
        estimated_after_dedup=dedup_est,
        model_key=model_key,
        category_hint=category_hint,
    )


def merge_catalog_items(batches: List[List[CatalogItem]]) -> Tuple[List[CatalogItem], int]:
    """
    Dédup intra-plateforme par (platform, platform_item_id) — variantes de recherche.
    Chaque annonce reste une entrée distincte (pas de fusion cross-plateforme).
    """
    seen_platform: Set[str] = set()
    out: List[CatalogItem] = []
    dupes = 0

    for batch in batches:
        for it in batch:
            plat = str(getattr(it, "platform", None) or "vinted")
            pid = str(getattr(it, "platform_item_id", None) or it.id)
            pk = f"{plat}:{pid}"
            if pk in seen_platform:
                dupes += 1
                continue
            seen_platform.add(pk)
            out.append(it)
    return out, dupes


def fetch_plan_catalog(
    plan: SearchPlan,
    *,
    max_items_per_variant: int = 0,
    headless: bool = True,
    on_page: Optional[Callable[[int, int, int], None]] = None,
    on_platform_progress: Optional[Callable[[str, str, int], None]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    prefetched_adapters: Optional[Dict[str, MarketplaceAdapter]] = None,
    keep_adapters_open: bool = True,
) -> Tuple[List[CatalogItem], int, Dict[str, Dict[str, Any]], Dict[str, MarketplaceAdapter]]:
    """Exécute toutes les variantes du plan (Vinted, LeBonCoin, Wallapop)."""
    if os.environ.get("VF_USE_SCRAPE_LIB", "1").strip().lower() not in ("0", "false", "no"):
        try:
            from vf_scrape.orchestrator import run_scrape_from_plan

            items, dupes, stats, adapters = run_scrape_from_plan(
                plan,
                max_items_per_variant=max_items_per_variant,
                headless=headless,
                on_page=on_page,
                on_platform_progress=on_platform_progress,
                log_fn=log_fn,
                prefetched_adapters=prefetched_adapters,
                keep_adapters_open=keep_adapters_open,
            )
            if log_fn:
                log_fn(f"✓ vf_scrape : {len(items)} annonces ({dupes} doublons retirés)")
            return items, dupes, stats, adapters or dict(prefetched_adapters or {})
        except Exception as exc:
            logger.warning("vf_scrape indisponible, repli legacy: %s", exc)
    batches: List[List[CatalogItem]] = []
    adapters: Dict[str, MarketplaceAdapter] = dict(prefetched_adapters or {})
    seen_variant: Set[str] = set()
    scrape_stats: Dict[str, Dict[str, Any]] = {}
    plat_set = {v.platform.lower().replace("lbc", "leboncoin") for v in plan.variants}
    n_platforms = len(plat_set)
    fast_env = os.environ.get("VF_FAST_SCRAPE", "auto").strip().lower()
    if fast_env in ("0", "false", "no", "off"):
        fast_scrape = False
    elif fast_env in ("1", "true", "yes", "on"):
        fast_scrape = True
    else:
        fast_scrape = n_platforms >= 6 and len(plan.variants) >= 14
    fragile_skip = os.environ.get("VF_SKIP_FRAGILE", "auto").strip().lower()
    plat_failed_403: Set[str] = set()

    for var in plan.variants:
        plat = var.platform.lower()
        if plat == "lbc":
            plat = "leboncoin"
        if not var.search_terms.strip():
            continue
        vkey = f"{plat}:{var.search_terms.strip().lower()}"
        if vkey in seen_variant:
            continue
        seen_variant.add(vkey)

        if (
            plat in ("rakuten", "cashexpress")
            and fragile_skip in ("1", "true", "yes")
            and n_platforms >= 6
        ):
            if log_fn:
                log_fn(f"⊘ {plat} : ignoré (mode rapide — anti-bot fréquent)")
            scrape_stats.setdefault(plat, {"raw": 0, "variants": 0, "errors": 0, "skipped": True})
            continue
        if plat in plat_failed_403 and var.note != "primary":
            if log_fn:
                log_fn(f"⊘ {plat} : variante sautée (403 précédent)")
            continue

        from .engine import BUS, format_bus_log, platform_style

        t_src = time.monotonic()
        try:
            # local: CircuitOpenError, get_circuit

            cb = get_circuit(plat)
            try:
                cb.before_call()
            except CircuitOpenError as exc:
                BUS.emit("SOURCE_ERROR", {"platform": plat, "error": f"en pause ({exc})"})
                st = scrape_stats.setdefault(plat, {"raw": 0, "variants": 0, "errors": 0})
                st["errors"] = int(st.get("errors") or 0) + 1
                st["last_error"] = str(exc)
                if log_fn:
                    log_fn(f"⊘ {plat}: en pause ({exc})")
                continue
            from .engine import adapter_headless

            plat_headless = adapter_headless(plat, run_headless=headless)
            if plat not in adapters:
                adapters[plat] = get_adapter(plat, headless=plat_headless)
                boot_url = var.url or plan.canonical_query
                adapters[plat].bootstrap(boot_url, headless=plat_headless)
            ad = adapters[plat]
            if plat == "ebay" and getattr(ad, "_challenge_failed", False):
                st = scrape_stats.setdefault(plat, {"raw": 0, "variants": 0, "errors": 0, "skipped": True})
                st["last_error"] = "challenge bootstrap"
                if log_fn:
                    log_fn("⊘ eBay : ignoré — captcha non résolu (VF_EBAY_HEADED=1 ou cookies)")
                plat_failed_403.add(plat)
                continue
            cb.record_success()
        except CircuitOpenError:
            continue
        except Exception as exc:  # noqa: BLE001
            try:
                get_circuit(plat).record_failure()
            except Exception:
                pass
            BUS.emit("SOURCE_ERROR", {"platform": plat, "error": str(exc)})
            st = scrape_stats.setdefault(plat, {"raw": 0, "variants": 0, "errors": 0})
            st["errors"] = int(st.get("errors") or 0) + 1
            st["last_error"] = str(exc)
            if log_fn:
                log_fn(f"⊘ {plat}: {exc}")
            continue

        BUS.emit(
            "SOURCE_START",
            {"platform": plat, "query": var.search_terms, "search_terms": var.search_terms},
        )
        if log_fn:
            lbl = platform_style(plat).label
            log_fn(f"→ [{lbl}] {var.search_terms!r}")
        try:
            if plat == "vinted":
                if not var.url:
                    continue
                from .market import DEEP_SCRAPE, fetch_vinted_deep

                if DEEP_SCRAPE and var.note in ("primary", "alias", ""):
                    items = fetch_vinted_deep(
                        ad,  # type: ignore[arg-type]
                        var.search_terms,
                        category_hint=plan.category_hint or "generic",
                        max_items=max_items_per_variant,
                        price_from=int(var.price_min or 0),
                        price_to=int(var.price_max or 0),
                        on_page=on_page,
                        log_fn=log_fn,
                        fast_mode=fast_scrape,
                    )
                else:
                    items = ad.fetch_catalog(var.url, max_items=max_items_per_variant, on_page=on_page)
            elif hasattr(ad, "fetch_by_terms"):
                kw: Dict[str, Any] = {"max_items": max_items_per_variant}
                if var.price_min:
                    kw["price_min"] = int(var.price_min)
                if var.price_max:
                    kw["price_max"] = int(var.price_max)
                try:
                    items = ad.fetch_by_terms(var.search_terms, **kw)  # type: ignore[attr-defined]
                except TypeError:
                    items = ad.fetch_by_terms(var.search_terms, max_items=max_items_per_variant)  # type: ignore[attr-defined]
            else:
                url = var.url or ad.build_search_url(var.search_terms)  # type: ignore[attr-defined]
                items = ad.fetch_catalog(url, max_items=max_items_per_variant, on_page=on_page)
        except NotImplementedError as exc:
            if log_fn:
                log_fn(f"⊘ {plat}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch %s %r: %s", plat, var.search_terms, exc)
            st = scrape_stats.setdefault(plat, {"raw": 0, "variants": 0, "errors": 0})
            st["errors"] = int(st.get("errors") or 0) + 1
            st["last_error"] = str(exc)
            if "403" in str(exc) or "challenge" in str(exc).lower():
                plat_failed_403.add(plat)
            if log_fn:
                log_fn(f"⚠ {plat}: {exc}")
            continue

        st = scrape_stats.setdefault(plat, {"raw": 0, "variants": 0, "errors": 0})
        st["raw"] = int(st.get("raw") or 0) + len(items)
        st["variants"] = int(st.get("variants") or 0) + 1

        for it in items:
            it.platform = plat
            if not getattr(it, "platform_item_id", None):
                it.platform_item_id = str(it.id)
        ms = int((time.monotonic() - t_src) * 1000)
        BUS.emit("SOURCE_DONE", {"platform": plat, "count": len(items), "time_ms": ms})
        if items:
            batches.append(items)
            if log_fn:
                line = format_bus_log("SOURCE_DONE", {"platform": plat, "count": len(items), "time_ms": ms})
                log_fn(f"  ✓ {line or f'{plat}: {len(items)} annonces'}")
        elif log_fn:
            log_fn(f"  ⊘ [{platform_style(plat).label}] 0 annonce")

    if not keep_adapters_open:
        for ad in adapters.values():
            try:
                ad.close()
            except Exception:
                pass

    merged, dupes = merge_catalog_items(batches)
    pmin = pmax = None
    if plan.variants:
        pmin = plan.variants[0].price_min
        pmax = plan.variants[0].price_max
    if pmin or pmax:
        filtered: List[Any] = []
        dropped = 0
        for it in merged:
            try:
                p = float(getattr(it, "total_price", None) or getattr(it, "price", None) or 0)
            except (TypeError, ValueError):
                p = 0.0
            if p > 0:
                if pmin and p < int(pmin):
                    dropped += 1
                    continue
                if pmax and p > int(pmax):
                    dropped += 1
                    continue
            filtered.append(it)
        if dropped and log_fn:
            log_fn(f"✓ Filtre prix scrape : -{dropped} hors {pmin or '—'}–{pmax or '—'} €")
        merged = filtered
    return merged, dupes, scrape_stats, adapters


_PLATFORM_ORDER = (
    "vinted",
    "leboncoin",
    "ebay",
    "cashexpress",
    "wallapop",
    "rakuten",
)


def _sort_platforms(plats: List[str]) -> List[str]:
    order = {p: i for i, p in enumerate(_PLATFORM_ORDER)}

    def _key(p: str) -> int:
        k = p.lower()
        if k == "lbc":
            k = "leboncoin"
        if k in ("cash-express", "cex"):
            k = "cashexpress"
        return order.get(k, 99)

    seen: Set[str] = set()
    out: List[str] = []
    for p in sorted(plats, key=_key):
        k = p.lower()
        if k == "lbc":
            k = "leboncoin"
        if k in ("cash-express", "cex"):
            k = "cashexpress"
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def default_platforms() -> List[str]:
    """Plateformes actives par défaut (surcharge VF_PLATFORMS). Ordre : Vinted → LBC → eBay → CEX → Wallapop."""
    import os

    raw = os.environ.get(
        "VF_PLATFORMS",
        "vinted,leboncoin",
    ).strip()
    if not raw:
        return ["vinted"]
    out: List[str] = []
    for p in raw.split(","):
        key = p.strip().lower()
        if key == "lbc":
            key = "leboncoin"
        if key in ("cash-express", "cex"):
            key = "cashexpress"
        if key and key not in out:
            out.append(key)
    return _sort_platforms(out) or ["vinted"]


# ================================================== search_intel

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vf.search_intel")

_POPULAR = (
    "New Nintendo 3DS XL",
    "iPhone 13 128 Go",
    "PlayStation 5",
    "Nintendo Switch OLED",
    "MacBook Air M1",
    "Dyson V15",
    "Projecteur Full HD",
    "Samsung Galaxy S23",
    "iPad Air",
    "Canon EOS R50",
)

_DEPTH_BUDGET = {
    "fast": {"budget_usd": 0.018, "vision_max": 15, "oracle_max": 10, "label": "Rapide"},
    "balanced": {"budget_usd": 0.055, "vision_max": 40, "oracle_max": 22, "label": "Équilibré"},
    "deep": {"budget_usd": 0.14, "vision_max": 60, "oracle_max": 35, "label": "Exhaustif"},
}

_NEW_3DS_REQ = re.compile(r"(?i)\b(new\s*nintendo\s*3ds|new\s*3ds|nn3ds)\b")
_APPAREL_BRANDS = re.compile(
    r"(?i)\b(carhartt|supreme|stussy|patagonia|nike|adidas|ralph\s*lauren|levi'?s?|"
    r"stone\s*island|north\s*face|arc'?teryx|champion|vans|converse)\b"
)
_APPAREL_SIZE = re.compile(r"(?i)\b(?:taille\s*)?(xxs|xs|s|m|l|xl|xxl|2xl|3xl)\b")
_GRAPHIC_TEE = re.compile(r"(?i)\b(graphic|logo|print|tee|t-?shirt|sweat|hoodie)\b")


def local_autocomplete(prefix: str, *, limit: int = 8) -> List[str]:
    """Suggestions sans API — modèles connus + recherches populaires."""
    q = (prefix or "").strip().lower()
    if len(q) < 2:
        return list(_POPULAR[:5])
    out: List[str] = []
    seen: set = set()

    def _add(s: str) -> None:
        k = s.lower()
        if k not in seen and q in k:
            seen.add(k)
            out.append(s)

    for p in _POPULAR:
        _add(p)
    try:
        from .models import _REGISTRY

        for m in _REGISTRY.values():
            _add(m.label)
            for terms in m.aliases.values():
                for t in terms:
                    _add(t)
    except Exception:
        pass
    return out[:limit]


def preview_search_local(raw: str) -> Dict[str, Any]:
    """Détection instantanée (0 token) — catégorie, modèle, fourchette prix."""
    from .engine import detect_market_context
    from .models import find_model_by_text, expected_used_range

    text = (raw or "").strip()
    ctx = detect_market_context(text, buyer_priorities="")
    cat = str(ctx.get("category") or "generic")
    st = str(ctx.get("search_text") or text)[:80]
    model = find_model_by_text(st, cat)
    price_hint = ""
    if model:
        rng = expected_used_range(model.key)
        if rng:
            price_hint = f"{rng[0]:.0f}–{rng[1]:.0f} € occasion typique"
    plat_hint = "URL détectée" if text.startswith("http") else "Recherche texte"
    return {
        "category": cat,
        "search_text": st,
        "model_label": model.label if model else "",
        "model_key": model.key if model else "",
        "price_hint": price_hint,
        "url_locked": bool(ctx.get("url_locked")),
        "platform_hint": plat_hint,
        "label": str(ctx.get("label") or cat),
    }


def suggest_price_bounds(category: str, query: str) -> tuple[Optional[int], Optional[int]]:
    """
    Fourchette prix raisonnable pour le scrape (évite coques à 5€ et lots à 2000€).
  """
    cat = (category or "generic").lower()
    q = (query or "").lower()
    try:
        from .models import find_model_by_text, expected_used_range

        m = find_model_by_text(query, cat)
        if m:
            rng = expected_used_range(m.key)
            if rng:
                lo = max(15, int(rng[0] * 0.40))
                hi = int(rng[1] * 1.70)
                try:
                    from vf_scrape.config import resolve_scrape_mode

                    if resolve_scrape_mode() == "fast":
                        lo = max(15, int(rng[0] * 0.35))
                        hi = max(hi, int(rng[1] * 1.85))
                except Exception:
                    pass
                if cat.startswith("console_"):
                    hi = max(hi, int(rng[1] * 2.0))
                return lo, hi
    except Exception:
        pass
    if cat == "console_ds" or "3ds" in q or "2ds" in q:
        return 40, 500
    if cat in ("console_ps4", "console_ps5") or re.search(r"(?i)\b(ps4|ps5|playstation)\b", q):
        return 60, 280
    if cat == "console_switch" or "switch" in q:
        return 50, 320
    if cat.startswith("console_"):
        return 50, 300
    if cat == "projector" or "projecteur" in q:
        return 50, 200
    if cat == "phone" or "iphone" in q or "galaxy" in q:
        return 80, 1400
    if cat in ("apparel", "shoes", "jeans") or any(
        b in q for b in ("carhartt", "nike", "adidas", "tee", "jean", "hoodie")
    ):
        return 15, 250
    if cat == "ac" or "clim" in q:
        return 80, 1200
    return 20, None


def _spec(
    sid: str,
    label: str,
    *,
    hint: str = "",
    default: bool = True,
    scope: str = "both",
    priority_text: str = "",
) -> Dict[str, Any]:
    return {
        "id": sid,
        "label": label,
        "hint": hint,
        "default": bool(default),
        "scope": scope,
        "priority_text": (priority_text or label).strip(),
    }


def _safety_specs() -> List[Dict[str, Any]]:
    return [
        _spec(
            "exclude_broken",
            "Exclure HS / pour pièces",
            hint="Titres à risque (broken, ban, iCloud…)",
            scope="filter",
        ),
        _spec(
            "scam_shield",
            "Scam shield (vendeur neuf + -50%)",
            hint="Bloque les arnaques évidentes",
            scope="filter",
        ),
    ]


def _projector_specs(q: str) -> List[Dict[str, Any]]:
    st = q.lower()
    specs = list(_safety_specs())
    try:
        from .engine import infer_projector_brand, infer_projector_usage

        brand = infer_projector_brand({"title": q})
        usage = infer_projector_usage({"title": q})
    except Exception:
        brand, usage = "", ""

    lum_default = True
    if re.search(r"(?i)\b(mini|pocket|led\s*star)\b", st):
        lum_default = False
    m_lm = re.search(r"(?i)\b(\d{3,5})\s*(lm|lumens|ansi)\b", st)
    lum_label = f"≥ {m_lm.group(1)} lumens ANSI" if m_lm else "≥ 2000 lumens ANSI"
    lum_hint = "Écarte mini LED et jouets « 9000 lumens » marketing"
    specs.append(
        _spec(
            "min_lumens_2000",
            lum_label,
            hint=lum_hint,
            scope="both",
            priority_text="Lumens ANSI crédibles (pas LED marketing, vérifier âge lampe)",
        )
    )
    specs.append(
        _spec(
            "require_hdmi",
            "HDMI moderne obligatoire",
            hint="Pas VGA/DVI/M1-DA seul",
            scope="both",
            priority_text="HDMI moderne obligatoire (pas VGA/DVI « bazar »)",
            default=not re.search(r"(?i)\bvga\b", st),
        )
    )
    specs.append(
        _spec(
            "exclude_xga_office",
            "Éviter XGA / bureautique 4:3",
            hint="Invendable pour usage cinéma",
            scope="both",
            priority_text="Éviter XGA / WVGA bureautique si objectif salon/cinéma",
        )
    )
    if brand and brand not in ("other", ""):
        brand_txt = f"Marque cible : {brand.title()}"
    else:
        brand_txt = "Marque lisible (Epson/BenQ/Optoma/XGIMI…)"
    specs.append(
        _spec(
            "proj_brand",
            brand_txt[:52],
            hint="Pas de « no brand » générique",
            scope="scoring",
            priority_text=brand_txt,
        )
    )
    if re.search(r"(?i)\b(4k|2160p)\b", st):
        res_txt = "Résolution visée : 4K native"
    elif re.search(r"(?i)\b(1080|full\s*hd|fhd)\b", st):
        res_txt = "Résolution : Full HD 1080p"
    else:
        res_txt = "Résolution : 1080p/4K si usage films"
    specs.append(
        _spec(
            "proj_resolution",
            res_txt[:52],
            hint="Pas upscale flou ni résolution absente",
            scope="scoring",
            priority_text=res_txt,
        )
    )
    if usage in ("cinema", "office", "portable", "home"):
        usage_txt = f"Usage cible : {usage}"
    elif re.search(r"(?i)\b(cin[eé]ma|salon|home\s*cin[eé]ma)\b", st):
        usage_txt = "Usage : salon / home cinéma"
    else:
        usage_txt = "Usage : salon/cinéma (pas bureautique basique)"
    specs.append(
        _spec(
            "proj_usage",
            usage_txt[:52],
            hint="Format 16:9, keystone si portable",
            scope="scoring",
            priority_text=usage_txt,
        )
    )
    specs.append(
        _spec(
            "exclude_mini_projector",
            "Exclure mini / jouets / rétroprojecteurs HS",
            hint="Camescopes, jouets LED, pièces détachées",
            scope="filter",
        )
    )
    specs.append(
        _spec(
            "proj_photos",
            "Photos connectiques + lentille",
            hint="Scoring IA — vérifie HDMI et état",
            scope="scoring",
            priority_text="Photos : connectiques + lentille + compteur lampe si indiqué",
            default=False,
        )
    )
    return specs


def _console_specs(cat: str, q: str) -> List[Dict[str, Any]]:
    st = q.lower()
    specs = list(_safety_specs())
    specs.append(
        _spec(
            "exclude_accessories",
            "Exclure accessoires / jeux seuls",
            hint="Coques, câbles, manettes seules…",
            scope="filter",
        )
    )
    specs.extend(
        [
            _spec(
                "require_console_unit",
                "Console complète uniquement",
                hint="Pas jeu seul ni boîte vide",
                scope="both",
                priority_text="Console complète — pas boîte vide ni jeu seul",
            ),
            _spec(
                "prefer_charger",
                "Favoriser avec chargeur",
                hint="Bonus scoring si chargeur mentionné",
                default="chargeur" in st or "pack" in st,
                scope="scoring",
                priority_text="Chargeur d'origine = gros plus",
            ),
        ]
    )
    if re.search(r"(?i)\bnew\s*(nintendo\s*)?3ds", st):
        specs.append(
            _spec(
                "require_new_3ds",
                "Exiger New 3DS (pas 3DS / DSI)",
                hint="Rejette 3DS/2DS/DSI et boîtes vides",
                scope="both",
                priority_text="New 3DS uniquement — pas 3DS classique ni DSI",
            )
        )
    if cat == "console_ds":
        specs.append(
            _spec(
                "prefer_stylus",
                "Stylet d'origine valorisé",
                hint="Pénalise « sans stylet » si recherché",
                default=bool(re.search(r"(?i)\bstylet|stylus", st)),
                scope="scoring",
                priority_text="Stylet d'origine demandé si console tactile",
            )
        )
    specs.append(
        _spec(
            "console_condition",
            "Bon état — pas ban / HS",
            hint="Écran, coque, lecteur",
            scope="scoring",
            priority_text="Éviter ban / pour pièces / écran HS",
        )
    )
    return specs


def _apparel_specs(q: str) -> List[Dict[str, Any]]:
    specs = list(_safety_specs())
    specs.extend(
        [
            _spec(
                "require_brand_match",
                "Marque exacte dans le titre",
                hint="Ex. Carhartt si recherché",
                default=bool(_APPAREL_BRANDS.search(q)),
                scope="both",
                priority_text="Marque et taille exactes",
            ),
            _spec(
                "prefer_good_condition",
                "Favoriser bon état",
                hint="Pénalise « usé / trou / tache »",
                scope="both",
                priority_text="Très bon état, pas de taches ni trous",
            ),
            _spec(
                "exclude_counterfeit",
                "Éviter contrefaçons évidentes",
                hint="Titres suspects, prix trop bas",
                scope="scoring",
                priority_text="Authenticité — pas contrefaçon / dropship",
            ),
        ]
    )
    if _APPAREL_SIZE.search(q):
        specs.append(
            _spec(
                "require_size_match",
                "Taille indiquée dans l'annonce",
                hint="Filtre tailles absentes ou différentes",
                scope="both",
                priority_text="Taille exacte obligatoire",
            )
        )
    return specs


def _phone_specs(q: str) -> List[Dict[str, Any]]:
    st = q.lower()
    specs = list(_safety_specs())
    specs.extend(
        [
            _spec(
                "min_storage_64",
                "Stockage ≥ 64 Go",
                hint="Filtre 32 Go et moins",
                default=bool(re.search(r"\b(128|256|512)\b", st)),
                scope="both",
                priority_text="Stockage confirmé dans le titre",
            ),
            _spec(
                "exclude_icloud",
                "Exclure iCloud / blocage",
                hint="Comptes verrouillés",
                scope="both",
                priority_text="Débloqué — pas iCloud / Find My",
            ),
            _spec(
                "phone_model_exact",
                "Modèle exact (pas « iPhone » générique)",
                hint="Scoring IA sur le modèle",
                scope="scoring",
                priority_text="Modèle exact (iPhone 13 Pro, Galaxy S24…)",
            ),
            _spec(
                "phone_screen",
                "Écran sain — pas de fissure",
                hint="Face ID OK si mentionné",
                scope="scoring",
                priority_text="Écran sain (pas fissure, rayures légères OK)",
            ),
        ]
    )
    return specs


def _generic_specs(q: str) -> List[Dict[str, Any]]:
    specs = list(_safety_specs())
    specs.extend(
        [
            _spec(
                "exclude_vague",
                "Exclure titres vagues / dropship",
                hint="« Article mode », « super deal »…",
                scope="filter",
            ),
            _spec(
                "identifiable_product",
                "Marque et modèle identifiables",
                hint="Scoring IA",
                scope="scoring",
                priority_text="Marque et modèle identifiables dans le titre",
            ),
            _spec(
                "good_condition",
                "État décrit clairement",
                hint="Photos et description",
                scope="scoring",
                priority_text="État décrit clairement — pack complet valorisé",
            ),
        ]
    )
    return specs


def unified_market_specs(
    category: str,
    query: str = "",
    refinements: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Affinage unifié : filtres avant scrape + critères scoring IA — 100 % contextuels (0 token).
    """
    cat = _normalize_category(category)
    q = (query or "").strip()
    st = q.lower()
    ref = dict(refinements or {})

    if cat == "projector" or re.search(r"(?i)\b(projecteur|projector|lumens|vid[eé]o\s*proj)\b", st):
        specs = _projector_specs(q)
    elif cat in ("console_ds", "console_switch", "console_ps4", "console_ps5") or cat.startswith("console_"):
        specs = _console_specs(cat, q)
    elif cat in ("apparel", "shoes", "jeans") or _APPAREL_BRANDS.search(st) or _GRAPHIC_TEE.search(st):
        specs = _apparel_specs(q)
    elif cat == "phone" or re.search(r"(?i)\biphone|galaxy|pixel|samsung\b", st):
        specs = _phone_specs(q)
    else:
        specs = _generic_specs(q)

    overrides = refinements_to_preflight_overrides(ref)
    for sp in specs:
        oid = sp["id"]
        if oid in overrides:
            sp["default"] = bool(overrides[oid])
    cond = (ref.get("condition_min") or "").lower()
    if cond in ("good", "very_good", "new", "new_tags"):
        for sp in specs:
            if sp["id"] in ("prefer_good_condition", "good_condition", "console_condition"):
                sp["default"] = True
    lum = ref.get("lumens_min") or ""
    if lum == "3000":
        for sp in specs:
            if sp["id"] == "min_lumens_2000":
                sp["label"] = "≥ 3000 lumens ANSI"
                sp["priority_text"] = "Lumens ANSI ≥ 3000 (salon lumineux)"
    elif lum and str(lum).isdigit() and int(lum) >= 2000:
        for sp in specs:
            if sp["id"] == "min_lumens_2000":
                sp["label"] = f"≥ {lum} lumens ANSI"
                sp["priority_text"] = f"Lumens ANSI ≥ {lum}"
    brand = (ref.get("projector_brand") or "").strip()
    if brand and brand != "other":
        for sp in specs:
            if sp["id"] == "proj_brand":
                sp["default"] = True
                sp["label"] = f"Marque cible : {brand.title()}"
                sp["priority_text"] = f"Marque cible : {brand.title()}"
    elif brand == "other":
        for sp in specs:
            if sp["id"] == "proj_brand":
                sp["default"] = True
    res = (ref.get("resolution") or "").strip()
    if res:
        res_txt = {"1080p": "Full HD 1080p", "4k": "4K native", "720p": "720p HD"}.get(res, res)
        for sp in specs:
            if sp["id"] == "proj_resolution":
                sp["default"] = True
                sp["label"] = f"Résolution : {res_txt}"
                sp["priority_text"] = f"Résolution visée : {res_txt}"
    hdmi = (ref.get("hdmi") or "").strip()
    if hdmi == "required":
        for sp in specs:
            if sp["id"] == "require_hdmi":
                sp["default"] = True
    usage = (ref.get("usage") or "").strip()
    if usage:
        usage_txt = {
            "cinema": "Cinéma maison",
            "home": "Salon",
            "office": "Bureau",
            "portable": "Portable",
        }.get(usage, usage)
        for sp in specs:
            if sp["id"] == "proj_usage":
                sp["default"] = True
                sp["label"] = f"Usage : {usage_txt}"
                sp["priority_text"] = f"Usage cible : {usage_txt}"
    qmin = (ref.get("quality_min") or "").strip()
    if qmin and qmin != "all":
        for sp in specs:
            if sp["id"] in ("proj_photos", "good_condition"):
                sp["default"] = True
    return specs


def preflight_filter_specs(category: str, query: str) -> List[Dict[str, Any]]:
    """Rétrocompat — uniquement les specs à portée filtre."""
    return [s for s in unified_market_specs(category, query) if s.get("scope") in ("filter", "both")]


def specs_to_preflight_map(active: Dict[str, bool], specs: List[Dict[str, Any]]) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for sp in specs:
        if sp.get("scope") not in ("filter", "both"):
            continue
        sid = str(sp["id"])
        out[sid] = bool(active.get(sid, sp.get("default", False)))
    return out


def specs_to_scoring_notes(active: Dict[str, bool], specs: List[Dict[str, Any]]) -> List[str]:
    notes: List[str] = []
    for sp in specs:
        if sp.get("scope") not in ("scoring", "both"):
            continue
        sid = str(sp["id"])
        if not active.get(sid, sp.get("default", False)):
            continue
        txt = str(sp.get("priority_text") or sp.get("label") or "").strip()
        if txt and txt not in notes:
            notes.append(txt)
    return notes


def category_brief_pitfalls(category: str, platforms: List[str]) -> List[str]:
    """Pièges marché — contextuels, pas de « jeux » sur un projecteur."""
    cat = _normalize_category(category)
    pitfalls: List[str] = []
    if cat == "projector":
        pitfalls = [
            "Mini LED « 9000 lumens » = marketing — viser lumens ANSI et modèle identifiable",
            "VGA/DVI seul ou XGA 4:3 = invendable pour home cinéma",
            "Lots flous (lampe seule, télécommande) passent en « Doutes »",
        ]
    elif cat.startswith("console_"):
        pitfalls = [
            "Jeux et accessoires seuls polluent — triage titre avant IA",
            "Boîtes vides et « console seule » sans détail → rejet",
        ]
    elif cat in ("apparel", "shoes", "jeans"):
        pitfalls = [
            "Tailles absentes ou contrefaçons évidentes — vérifier photos",
            "Lots flous passent en « Doutes », pas en offres principales",
        ]
    elif cat == "phone":
        pitfalls = [
            "iCloud / Find My = deal killer — filtrer avant enrichissement",
            "« iPhone » sans modèle = bruit — exiger modèle + stockage",
        ]
    else:
        pitfalls = [
            "Titres vagues et dropship polluent — triage strict avant IA",
            "Annonces ambiguës → « Doutes », pas offres principales",
        ]
    if "ebay" in platforms:
        pitfalls.append("eBay : uniquement Achat Immédiat (pas enchères à 1€)")
    return pitfalls[:4]


ALL_PLATFORMS = ("vinted", "leboncoin", "wallapop", "ebay", "rakuten", "cashexpress")
_ALL_PLATFORMS = ALL_PLATFORMS


def select_platforms(*args: Any, **kwargs: Any) -> Any:
    from .engine import select_platforms as _fn
    return _fn(*args, **kwargs)


def category_platform_profile(*args: Any, **kwargs: Any) -> Any:
    from .engine import category_platform_profile as _fn
    return _fn(*args, **kwargs)


def format_selection_log(*args: Any, **kwargs: Any) -> Any:
    from .engine import format_selection_log as _fn
    return _fn(*args, **kwargs)

# Legacy alias — profils déplacés dans vf/platform_select.py
_CATEGORY_PLATFORM_PROFILES: Dict[str, Dict[str, Any]] = {
    "apparel": {
        "on": ["vinted"],
        "optional": ["leboncoin"],
        "off": ["ebay", "wallapop", "rakuten", "cashexpress"],
        "hint": "Mode FR : Vinted concentre le volume. LeBonCoin en complément local si besoin.",
    },
    "shoes": {
        "on": ["vinted"],
        "optional": ["leboncoin", "ebay"],
        "off": ["wallapop", "rakuten", "cashexpress"],
        "hint": "Sneakers : Vinted + eBay (Buy It Now) pour comparer. LBC optionnel.",
    },
    "jeans": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay"],
        "off": ["wallapop", "rakuten", "cashexpress"],
        "hint": "Denim : Vinted volume + LBC local.",
    },
    "phone": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay", "rakuten"],
        "off": ["wallapop", "cashexpress"],
        "hint": "Téléphones : Vinted + LBC + eBay. Oracle Back Market pour prix reconditionné.",
    },
    "console_ds": {
        "on": ["vinted", "leboncoin", "wallapop", "ebay"],
        "optional": [],
        "off": ["rakuten", "cashexpress"],
        "hint": "Consoles DS : Vinted, LBC, Wallapop, eBay.",
    },
    "console_ps4": {
        "on": ["vinted", "leboncoin", "ebay"],
        "optional": ["wallapop", "rakuten", "cashexpress"],
        "off": [],
        "hint": "PlayStation : Vinted + LBC + eBay pour couvrir le marché FR/EU.",
    },
    "console_ps5": {
        "on": ["vinted", "leboncoin", "ebay"],
        "optional": ["wallapop", "rakuten"],
        "off": ["cashexpress"],
        "hint": "PS5 : volume Vinted/LBC + eBay pour références prix.",
    },
    "console_switch": {
        "on": ["vinted", "leboncoin", "ebay"],
        "optional": ["wallapop", "rakuten"],
        "off": ["cashexpress"],
        "hint": "Switch : même logique consoles — comparez LBC vs Vinted.",
    },
    "laptop": {
        "on": ["vinted", "leboncoin", "ebay"],
        "optional": ["rakuten"],
        "off": ["wallapop", "cashexpress"],
        "hint": "PC portable : eBay + LBC pour le B2B occasion ; Vinted pour particuliers.",
    },
    "projector": {
        "on": ["vinted", "leboncoin", "ebay"],
        "optional": ["wallapop"],
        "off": ["rakuten", "cashexpress"],
        "hint": "Projecteurs : LBC volume local + eBay pour modèles pro.",
    },
    "generic": {
        "on": ["vinted", "leboncoin"],
        "optional": ["ebay", "wallapop"],
        "off": ["rakuten", "cashexpress"],
        "hint": "Recherche générique : Vinted + LeBonCoin par défaut.",
    },
}


def _normalize_category(cat: str) -> str:
    c = (cat or "generic").lower().strip()
    if c in ("unknown", ""):
        return "generic"
    if c.startswith("console_"):
        return c
    if c in _CATEGORY_PLATFORM_PROFILES:
        return c
    if "apparel" in c or c in ("wearable",):
        return "apparel"
    return c if c in _CATEGORY_PLATFORM_PROFILES else "generic"


def resolve_run_platforms(
    query: str,
    category: str = "",
    *,
    manual: Optional[List[str]] = None,
    auto: bool = True,
) -> Tuple[List[str], Dict[str, Any]]:
    """Résout la liste plateformes + métadonnées pour un run."""
    sel = select_platforms(query, category, manual=manual, auto=auto)
    return sel.platforms, sel.to_dict()


def idle_refinement_specs(category: str, query: str) -> List[Dict[str, Any]]:
    """
    Options d'affinage interactif (0 token) — affichées avant le scrape.
    type: single | multi
    """
    cat = _normalize_category(category)
    q = (query or "").lower()
    specs: List[Dict[str, Any]] = []

    if cat in ("apparel", "jeans", "shoes") or _GRAPHIC_TEE.search(q) or _APPAREL_BRANDS.search(q):
        specs.append(
            {
                "id": "apparel_size",
                "label": "Taille",
                "type": "single",
                "options": [
                    {"value": "", "label": "Toutes"},
                    {"value": "XS", "label": "XS"},
                    {"value": "S", "label": "S"},
                    {"value": "M", "label": "M"},
                    {"value": "L", "label": "L"},
                    {"value": "XL", "label": "XL"},
                    {"value": "XXL", "label": "XXL"},
                ],
                "default": (_APPAREL_SIZE.search(q).group(1).upper() if _APPAREL_SIZE.search(q) else ""),
            }
        )
        specs.append(
            {
                "id": "condition_min",
                "label": "État minimum",
                "type": "single",
                "options": [
                    {"value": "any", "label": "Tous états"},
                    {"value": "good", "label": "Bon état"},
                    {"value": "very_good", "label": "Très bon"},
                    {"value": "new_tags", "label": "Neuf / étiquette"},
                ],
                "default": "good",
            }
        )
        if not _APPAREL_BRANDS.search(q):
            specs.append(
                {
                    "id": "apparel_type",
                    "label": "Type",
                    "type": "single",
                    "options": [
                        {"value": "", "label": "Auto"},
                        {"value": "sweat", "label": "Sweat"},
                        {"value": "hoodie", "label": "Hoodie"},
                        {"value": "tee", "label": "T-shirt"},
                        {"value": "jacket", "label": "Veste"},
                    ],
                    "default": "sweat" if "sweat" in q else ("hoodie" if "hood" in q else ""),
                }
            )
    elif cat == "phone" or re.search(r"(?i)\biphone|galaxy|pixel|samsung\b", q):
        specs.append(
            {
                "id": "storage_min",
                "label": "Stockage min.",
                "type": "single",
                "options": [
                    {"value": "", "label": "Tous"},
                    {"value": "64", "label": "64 Go"},
                    {"value": "128", "label": "128 Go"},
                    {"value": "256", "label": "256 Go"},
                ],
                "default": "",
            }
        )
        specs.append(
            {
                "id": "condition_min",
                "label": "État",
                "type": "single",
                "options": [
                    {"value": "any", "label": "Tous"},
                    {"value": "good", "label": "Bon"},
                    {"value": "very_good", "label": "Très bon"},
                    {"value": "new", "label": "Comme neuf"},
                ],
                "default": "good",
            }
        )
    elif cat.startswith("console_"):
        specs.append(
            {
                "id": "pack_level",
                "label": "Pack",
                "type": "single",
                "options": [
                    {"value": "any", "label": "Indifférent"},
                    {"value": "console_only", "label": "Console seule OK"},
                    {"value": "with_charger", "label": "Avec chargeur"},
                    {"value": "with_games", "label": "Avec jeux"},
                    {"value": "full", "label": "Pack complet"},
                ],
                "default": "with_charger" if "pack" in q or "chargeur" in q else "any",
            }
        )
        specs.append(
            {
                "id": "condition_min",
                "label": "État",
                "type": "single",
                "options": [
                    {"value": "any", "label": "Tous"},
                    {"value": "good", "label": "Bon état"},
                    {"value": "very_good", "label": "Très bon"},
                ],
                "default": "good",
            }
        )
    elif cat == "projector":
        try:
            from .engine import infer_projector_brand

            inferred_brand = infer_projector_brand({"title": q}) or ""
        except Exception:
            inferred_brand = ""
        brand_default = inferred_brand if inferred_brand not in ("other", "") else ""
        m_lm = re.search(r"(?i)\b(\d{3,5})\s*(lm|lumens|ansi)\b", q)
        lum_default = m_lm.group(1) if m_lm else "2000"
        res_default = ""
        if re.search(r"(?i)\b(4k|2160p)\b", q):
            res_default = "4k"
        elif re.search(r"(?i)\b(1080|full\s*hd|fhd)\b", q):
            res_default = "1080p"
        usage_default = ""
        if re.search(r"(?i)\b(cin[eé]ma|salon|home\s*cin[eé]ma)\b", q):
            usage_default = "cinema"
        elif re.search(r"(?i)\b(bureau|office)\b", q):
            usage_default = "office"
        elif re.search(r"(?i)\b(portable|nomade)\b", q):
            usage_default = "portable"
        specs.extend(
            [
                {
                    "id": "quality_min",
                    "label": "Qualité min",
                    "type": "single",
                    "options": [
                        {"value": "all", "label": "Tous"},
                        {"value": "60", "label": "≥ 60"},
                        {"value": "70", "label": "Bon (≥ 70)"},
                        {"value": "80", "label": "Excellent (≥ 80)"},
                    ],
                    "default": "70",
                },
                {
                    "id": "projector_brand",
                    "label": "Marque",
                    "type": "single",
                    "options": [
                        {"value": "", "label": "Toutes"},
                        {"value": "epson", "label": "Epson"},
                        {"value": "benq", "label": "BenQ"},
                        {"value": "optoma", "label": "Optoma"},
                        {"value": "xgimi", "label": "XGIMI"},
                        {"value": "viewsonic", "label": "ViewSonic"},
                        {"value": "other", "label": "Autre marque pro"},
                    ],
                    "default": brand_default,
                },
                {
                    "id": "lumens_min",
                    "label": "Lumens min (ANSI)",
                    "type": "slider",
                    "min": 0,
                    "max": 5000,
                    "step": 500,
                    "default": lum_default,
                },
                {
                    "id": "resolution",
                    "label": "Résolution",
                    "type": "single",
                    "options": [
                        {"value": "", "label": "Indifférent"},
                        {"value": "1080p", "label": "Full HD 1080p"},
                        {"value": "4k", "label": "4K / UHD"},
                        {"value": "720p", "label": "720p HD"},
                    ],
                    "default": res_default,
                },
                {
                    "id": "hdmi",
                    "label": "HDMI",
                    "type": "single",
                    "options": [
                        {"value": "required", "label": "HDMI obligatoire"},
                        {"value": "any", "label": "Indifférent"},
                    ],
                    "default": "required" if not re.search(r"(?i)\bvga\b", q) else "any",
                },
                {
                    "id": "usage",
                    "label": "Usage",
                    "type": "single",
                    "options": [
                        {"value": "", "label": "Indifférent"},
                        {"value": "cinema", "label": "Cinéma maison"},
                        {"value": "home", "label": "Salon"},
                        {"value": "office", "label": "Bureau"},
                        {"value": "portable", "label": "Portable"},
                    ],
                    "default": usage_default,
                },
            ]
        )
    else:
        specs.append(
            {
                "id": "condition_min",
                "label": "État minimum",
                "type": "single",
                "options": [
                    {"value": "any", "label": "Tous"},
                    {"value": "good", "label": "Bon état"},
                ],
                "default": "good",
            }
        )
    return specs


def build_refined_query(base: str, refinements: Dict[str, str], *, category: str = "") -> str:
    """Enrichit la requête scrape avec les affinages (taille, type…) sans dupliquer."""
    q = (base or "").strip()
    if not q or q.startswith("http"):
        return q
    cat = _normalize_category(category)
    low = q.lower()
    parts = [q]
    size = (refinements.get("apparel_size") or "").strip().upper()
    if size and size not in low and f"taille {size.lower()}" not in low:
        parts.append(f"taille {size}")
    atype = (refinements.get("apparel_type") or "").strip().lower()
    if atype and atype not in low:
        type_map = {"sweat": "sweat", "hoodie": "hoodie", "tee": "t-shirt", "jacket": "veste"}
        word = type_map.get(atype, atype)
        if word not in low:
            parts.append(word)
    storage = (refinements.get("storage_min") or "").strip()
    if storage and storage not in low:
        parts.append(f"{storage} Go")
    brand = (refinements.get("projector_brand") or "").strip()
    if brand and brand not in low and brand != "other":
        parts.append(brand)
    return " ".join(parts).strip()


def refinements_to_preflight_overrides(refinements: Dict[str, str]) -> Dict[str, bool]:
    """Mappe les affinages idle → pré-filtres setup."""
    out: Dict[str, bool] = {
        "exclude_broken": True,
        "scam_shield": True,
        "exclude_mini_projector": True,
        "exclude_xga_office": True,
    }
    cond = (refinements.get("condition_min") or "").lower()
    if cond in ("good", "very_good", "new", "new_tags"):
        out["prefer_good_condition"] = True
    if cond in ("very_good", "new", "new_tags"):
        out["exclude_broken"] = True
    size = refinements.get("apparel_size") or ""
    if size:
        out["require_size_match"] = True
    storage = refinements.get("storage_min") or ""
    if storage in ("64", "128", "256"):
        out["min_storage_64"] = True
    pack = refinements.get("pack_level") or ""
    if pack == "console_only":
        out["require_console_unit"] = True
    elif pack in ("with_charger", "full"):
        out["prefer_charger"] = True
    elif pack == "with_games":
        out["prefer_charger"] = False
    lum = refinements.get("lumens_min") or ""
    try:
        lum_n = int(lum) if lum else 0
    except (TypeError, ValueError):
        lum_n = 0
    if lum_n >= 2000 or lum in ("2000", "3000"):
        out["min_lumens_2000"] = True
    hdmi = (refinements.get("hdmi") or "").strip()
    if hdmi == "required":
        out["require_hdmi"] = True
    brand = (refinements.get("projector_brand") or "").strip()
    if brand:
        out["proj_brand"] = True
    res = (refinements.get("resolution") or "").strip()
    if res:
        out["proj_resolution"] = True
    usage = (refinements.get("usage") or "").strip()
    if usage:
        out["proj_usage"] = True
    qmin = (refinements.get("quality_min") or "").strip()
    if qmin and qmin != "all":
        out["proj_photos"] = True
    return out


def refinements_to_pre_filter_defaults(refinements: Dict[str, str], category: str = "") -> Dict[str, str]:
    """Mappe les affinages idle → filtres post-run (valeurs par défaut)."""
    ref = dict(refinements or {})
    cat = _normalize_category(category)
    out: Dict[str, str] = {}
    if cat == "projector":
        lum = ref.get("lumens_min") or ""
        try:
            lum_n = int(lum) if lum else 0
        except (TypeError, ValueError):
            lum_n = 0
        if lum_n >= 4000:
            out["lumens_band"] = "4000+"
        elif lum_n >= 3000:
            out["lumens_band"] = "3000+"
        elif lum_n >= 2000:
            out["lumens_band"] = "2000+"
        elif lum_n >= 1000:
            out["lumens_band"] = "1000-3000"
        res = (ref.get("resolution") or "").strip()
        if res in ("1080p", "4k", "720p", "xga"):
            out["resolution"] = res
        hdmi = (ref.get("hdmi") or "").strip()
        if hdmi == "required":
            out["has_hdmi"] = "yes"
        usage = (ref.get("usage") or "").strip()
        if usage in ("home", "cinema", "office", "portable"):
            out["projector_usage"] = usage
        brand = (ref.get("projector_brand") or "").strip()
        if brand and brand != "other":
            out["projector_brand"] = brand
        elif brand == "other":
            out["recognized_brand"] = "on"
        qmin = (ref.get("quality_min") or "").strip()
        if qmin and qmin != "all":
            out["value_score_min"] = qmin
        out.setdefault("exclude_screen_only", "on")
        out.setdefault("exclude_mini_weak", "on")
    pmin = (ref.get("price_min") or "").strip()
    pmax = (ref.get("price_max") or "").strip()
    if pmin:
        out["price_min"] = pmin
    if pmax:
        out["price_max"] = pmax
    return out


def refinements_to_buyer_notes(refinements: Dict[str, str]) -> str:
    """Texte court pour buyer_priorities."""
    bits: List[str] = []
    size = refinements.get("apparel_size") or ""
    if size:
        bits.append(f"Taille {size} obligatoire")
    cond = refinements.get("condition_min") or ""
    cond_lbl = {
        "good": "bon état minimum",
        "very_good": "très bon état minimum",
        "new": "comme neuf",
        "new_tags": "neuf avec étiquette",
    }.get(cond, "")
    if cond_lbl:
        bits.append(cond_lbl)
    pack = refinements.get("pack_level") or ""
    pack_lbl = {
        "with_charger": "avec chargeur",
        "with_games": "avec jeux",
        "full": "pack complet",
        "console_only": "console seule acceptée",
    }.get(pack, "")
    if pack_lbl:
        bits.append(pack_lbl)
    storage = refinements.get("storage_min") or ""
    if storage:
        bits.append(f"stockage ≥ {storage} Go")
    brand = (refinements.get("projector_brand") or "").strip()
    if brand:
        bits.append(f"marque {brand.title()}" if brand != "other" else "marque pro reconnue")
    lum = refinements.get("lumens_min") or ""
    if lum and str(lum).isdigit() and int(lum) > 0:
        bits.append(f"≥ {lum} lumens ANSI")
    res = (refinements.get("resolution") or "").strip()
    if res:
        bits.append({"1080p": "Full HD", "4k": "4K", "720p": "720p"}.get(res, res))
    hdmi = (refinements.get("hdmi") or "").strip()
    if hdmi == "required":
        bits.append("HDMI moderne obligatoire")
    usage = (refinements.get("usage") or "").strip()
    if usage:
        bits.append(
            {
                "cinema": "cinéma maison",
                "home": "salon",
                "office": "bureau",
                "portable": "portable",
            }.get(usage, usage)
        )
    qmin = (refinements.get("quality_min") or "").strip()
    if qmin and qmin != "all":
        bits.append(f"qualité ≥ {qmin}")
    pmin = (refinements.get("price_min") or "").strip()
    pmax = (refinements.get("price_max") or "").strip()
    if pmin or pmax:
        bits.append(f"budget {pmin or '—'}–{pmax or '∞'} €")
    return " · ".join(bits)


def interactive_search_preview(
    raw: str,
    refinements: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Aperçu complet : marché + plateformes + affinages (0 token)."""
    ref = dict(refinements or {})
    local = preview_search_local(raw)
    cat = str(local.get("category") or "generic")
    plat_prof = category_platform_profile(cat, raw)
    refined_q = build_refined_query(raw, ref, category=cat)
    refine_specs = idle_refinement_specs(cat, raw)
    refine_summary = refinements_to_buyer_notes(ref)
    return {
        **local,
        "refined_query": refined_q,
        "platform_profile": plat_prof,
        "refinement_specs": refine_specs,
        "refinement_summary": refine_summary,
        "refinements": ref,
    }


def platform_strategy_lines(platforms: List[str], category: str) -> List[str]:
    from .engine import platform_style

    lines: List[str] = []
    for p in platforms:
        st = platform_style(p)
        if p == "vinted":
            lines.append(f"{st.label} : catalogue profond, triage titre strict, deep-fetch descriptions")
        elif p == "leboncoin":
            lines.append(f"{st.label} : volume local, annonces indépendantes")
        elif p == "ebay":
            lines.append(f"{st.label} : Buy-It-Now uniquement, médiane prix neuf/occasion")
        elif p == "wallapop":
            lines.append(f"{st.label} : annonces EU (prix souvent plus bas)")
        elif p == "cashexpress":
            lines.append(f"{st.label} : rachat/revente — volume variable")
        elif p == "rakuten":
            lines.append(f"{st.label} : reconditionné (optionnel, anti-bot)")
        else:
            lines.append(f"{st.label} : scraping standard")
    lines.append("Back Market : oracle prix reconditionné (référence, pas d'annonces)")
    if len(platforms) > 1:
        lines.append("Multi-sources : 1 carte = 1 annonce — pas de fusion cross-plateforme")
    return lines


def estimate_run_cost(platforms: List[str], depth: str = "balanced") -> Dict[str, Any]:
    d = _DEPTH_BUDGET.get(depth, _DEPTH_BUDGET["balanced"])
    n_plat = max(1, len(platforms))
    scrape_est = 0.0
    llm_est = float(d["budget_usd"])
    return {
        "depth": depth,
        "depth_label": d["label"],
        "platforms": n_plat,
        "llm_budget_usd": llm_est,
        "vision_max": d["vision_max"],
        "oracle_max": d["oracle_max"],
        "summary": f"~{llm_est:.2f}$ IA · {n_plat} source(s) · profil {d['label']}",
    }


def apply_depth_profile(depth: str) -> None:
    """Configure les limites IA via variables d'environnement (session)."""
    import os

    d = _DEPTH_BUDGET.get(depth, _DEPTH_BUDGET["balanced"])
    os.environ["VF_LLM_BUDGET_USD"] = str(d["budget_usd"])
    os.environ["VF_VISION_MAX"] = str(d["vision_max"])
    os.environ["VF_TOP_VISION_K"] = str(d["vision_max"])
    os.environ["VF_ORACLE_MAX"] = str(d["oracle_max"])
    if os.environ.get("VF_ORACLE", "").strip() == "":
        os.environ["VF_ORACLE"] = "1"
    if depth == "fast":
        os.environ["VF_DEEP_FETCH_WORKERS"] = "3"
        os.environ["VF_ORACLE_MAX"] = str(min(8, d["oracle_max"]))
    elif depth == "deep":
        os.environ["VF_DEEP_FETCH_WORKERS"] = "8"


DEAL_HUNT_MODES: Dict[str, Dict[str, str]] = {
    "balanced": {
        "label": "Équilibré",
        "hint": "Bon rapport qualité / prix — défaut raisonnable",
    },
    "steals": {
        "label": "Pépites petit budget",
        "hint": "Chasse aux bonnes affaires sous le marché",
    },
    "pro": {
        "label": "Pro / hyper quali",
        "hint": "État top, vendeur fiable, zéro compromis",
    },
}


def deal_hunt_notes(mode: str, category: str = "") -> str:
    """Consignes scoring selon le profil d'achat choisi."""
    m = (mode or "balanced").strip().lower()
    if m == "balanced":
        return ""
    cat = _normalize_category(category)
    if m == "steals":
        if cat.startswith("console_"):
            return (
                "Profil pépites : sous la médiane marché; état correct OK; "
                "rejeter arnaques prix trop bas; bonus pack complet à prix serré."
            )
        if cat == "projector":
            return "Profil pépites : ANSI réel, pas de dropship; prix sous médiane; défauts cosmétiques OK."
        if cat in ("phone", "laptop"):
            return "Profil pépites : iCloud/Google lock = rejet; batterie OK; prix net sous marché reconditionné."
        return "Profil pépites : priorité prix sous médiane avec état acceptable; éviter arnaques évidentes."
    if m == "pro":
        if cat.startswith("console_"):
            return (
                "Profil pro : New/très bon état; console seule ou pack complet; "
                "vendeur noté; rejeter HS/parties manquantes."
            )
        if cat == "projector":
            return "Profil pro : vraie résolution ANSI, faible heures lampe, marque fiable, état impeccable."
        return "Profil pro : état neuf/très bon, vendeur fiable (>10 avis), complet, sans défaut majeur."
    return ""


def brief_market_analysis(
    query: str,
    *,
    platforms: List[str],
    category: str = "",
    use_ai: bool = True,
) -> Dict[str, Any]:
    """
    Brief honnête pré-run (1 appel LLM court si clé dispo, sinon heuristique).
  """
    local = preview_search_local(query)
    cat = category or local.get("category") or "generic"
    base: Dict[str, Any] = {
        "category": cat,
        "honest_take": "",
        "pitfalls": [],
        "compare_note": "",
        "token_plan": estimate_run_cost(platforms, "balanced"),
        "strategies": platform_strategy_lines(platforms, cat),
    }
    base["pitfalls"] = category_brief_pitfalls(cat, platforms)

    if not use_ai:
        base["honest_take"] = (
            f"Marché {cat} — mode sans IA. Triage heuristique + scoring local. "
            f"{local.get('price_hint') or 'Fourchette prix à estimer après scrape.'}"
        )
        return base

    try:
        from .ai import api_key, _chat, _extract_json

        if not api_key():
            raise RuntimeError("no key")
        plat_txt = ", ".join(platforms)
        prompt = (
            f"Recherche: {query[:200]}\n"
            f"Catégorie détectée: {cat}\n"
            f"Plateformes: {plat_txt}\n"
            f"Modèle: {local.get('model_label') or '—'}\n"
            f"Prix typique: {local.get('price_hint') or '—'}\n\n"
            "Tu es un expert arbitrage occasion FR. Brief HONNÊTE et court pour l'acheteur.\n"
            "JSON:\n"
            '{"honest_take":"2 phrases max","pitfalls":["piège1","piège2"],'
            '"compare_note":"comment comparer les plateformes pour CE produit",'
            '"buyer_criteria":"5-8 critères concrets pour l acheteur (état, pack, modèle exact, pièges à éviter)",'
            '"preflight_ids":["exclude_broken","min_lumens_2000"]}\n'
            "preflight_ids = ids pertinents pour CE marché uniquement (pas de jeux/accessoires si projecteur). "
            "Ids possibles: exclude_broken, scam_shield, exclude_accessories, require_console_unit, "
            "require_new_3ds, require_brand_match, prefer_good_condition, min_storage_64, exclude_icloud, "
            "min_lumens_2000, require_hdmi, exclude_xga_office, exclude_mini_projector.\n"
            "Pas de promesses irréalistes. Mentionne limites du scraping."
        )
        raw = _chat(prompt, system="Analyste marché occasion. JSON seul.", max_tokens=280)
        obj = _extract_json(raw)
        if isinstance(obj, dict):
            if obj.get("honest_take"):
                base["honest_take"] = str(obj["honest_take"])
            if isinstance(obj.get("pitfalls"), list):
                base["pitfalls"] = [str(x) for x in obj["pitfalls"][:4]]
            if obj.get("compare_note"):
                base["compare_note"] = str(obj["compare_note"])
            if obj.get("buyer_criteria"):
                base["buyer_criteria"] = str(obj["buyer_criteria"]).strip()
            pf = obj.get("preflight_ids")
            if isinstance(pf, list):
                base["preflight_ids"] = [str(x) for x in pf[:8]]
    except Exception as exc:
        logger.debug("brief_market_analysis: %s", exc)
        base["honest_take"] = (
            f"Analyse {cat} — triage strict puis IA ciblée sur les annonces ambiguës seulement. "
            f"{local.get('price_hint') or ''}".strip()
        )
    return base


def enrich_search_intent(
    si: Any,
    *,
    query: str = "",
    preflight: Optional[Dict[str, bool]] = None,
    buyer_priorities: str = "",
) -> None:
    """
    Enrichit SearchIntent avec contraintes strictes dérivées de la requête et des pré-filtres.
    Mutates si in place.
    """
    blob = f"{query} {si.search_text or ''} {buyer_priorities or ''}".lower()
    pf = preflight or si.preflight_flags or {}
    si.preflight_flags = dict(pf)

    if pf.get("require_console_unit", True):
        si.exclude_patterns = list(si.exclude_patterns or [])
        si.exclude_patterns.append(
            re.compile(
                r"(?i)\b(bo[iî]te\s*vide|empty\s*box|sans\s*console|notice\s*seule|manuel\s*seul)\b"
            )
        )

    want_new = pf.get("require_new_3ds", False) or bool(_NEW_3DS_REQ.search(blob))
    if want_new and ("console_ds" in (si.product_families or []) or "3ds" in blob):
        if "console_ds" not in si.product_families:
            si.product_families = list(si.product_families or []) + ["console_ds"]
        si.required_ds_family = "new3ds_xl" if re.search(r"(?i)\bxl\b", blob) else "new3ds"
        si.require_all_patterns = list(si.require_all_patterns or [])
        si.require_all_patterns.append(_NEW_3DS_REQ)
        si.exclude_patterns = list(si.exclude_patterns or [])
        si.exclude_patterns.append(re.compile(r"(?i)\bdsi(\s*xl)?\b"))

    brand_m = _APPAREL_BRANDS.search(blob)
    if brand_m and pf.get("require_brand_match", True):
        si.required_brand = brand_m.group(1).lower().replace(" ", "")
    size_m = _APPAREL_SIZE.search(blob)
    if size_m and pf.get("require_size_match", False):
        si.apparel_size = size_m.group(1).upper()

    if "phone" in (si.product_families or []) or re.search(r"(?i)\biphone|galaxy\s*s\d+|pixel\s*\d", blob):
        if "phone" not in (si.product_families or []):
            si.product_families = list(si.product_families or []) + ["phone"]
        try:
            from .engine import infer_phone_model, infer_storage_gb

            stub = {"title": query or si.search_text or "", "ai_model_exact": query}
            key, _label = infer_phone_model(stub)
            if key != "unknown":
                si.required_phone_model_key = key
            st = infer_storage_gb(stub)
            if st:
                si.required_storage_gb = int(st)
        except Exception:
            pass
        if re.search(r"(?i)iphone\s*\d{2}\b", blob) and not re.search(r"(?i)\b(pro|plus|max)\b", blob):
            si.required_exclude_pro = True
            si.exclude_patterns = list(si.exclude_patterns or [])
            si.exclude_patterns.append(re.compile(r"(?i)\biphone\s*\d{2}\s*pro\b"))
            si.exclude_patterns.append(re.compile(r"(?i)\biphone\s*\d{2}\s*plus\b"))
            si.exclude_patterns.append(re.compile(r"(?i)\biphone\s*\d{2}\s*pro\s*max\b"))


def preflight_to_buyer_notes(active: Dict[str, bool], specs: List[Dict[str, Any]]) -> str:
    """Convertit les critères actifs en texte court (filtres + scoring)."""
    notes = specs_to_scoring_notes(active, specs)
    filt = [sp["label"] for sp in specs if sp.get("scope") in ("filter", "both") and active.get(sp["id"], sp.get("default", False))]
    parts = filt + [n for n in notes if n not in filt]
    return " · ".join(parts[:8])


# ================================================== enrich

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from typing import Any, Callable, Dict, List, Optional, Tuple

from .market import MarketplaceAdapter, get_adapter

logger = logging.getLogger("vf.enrich")

_SEM = Semaphore(6)


def _norm_plat(platform: str) -> str:
    p = (platform or "vinted").strip().lower()
    return "leboncoin" if p == "lbc" else p


def _platform_item_id(row: Dict[str, Any]) -> str:
    return str(row.get("platform_item_id") or row.get("id") or "").strip()


def _merge_detail(row: Dict[str, Any], detail: Dict[str, Any]) -> None:
    if not detail:
        return
    desc = str(detail.get("description") or detail.get("body") or "").strip()
    if desc and not str(row.get("description") or "").strip():
        row["description"] = desc
        row["description_excerpt"] = desc[:280]
    if not row.get("photo_url"):
        ph = detail.get("photo")
        if isinstance(ph, dict) and ph.get("url"):
            row["photo_url"] = str(ph["url"])
        elif detail.get("photo_url"):
            row["photo_url"] = str(detail["photo_url"])
    urls = detail.get("photo_urls")
    if isinstance(urls, list) and urls and not row.get("photo_urls"):
        row["photo_urls"] = urls
    for k in ("condition", "brand", "seller_username"):
        if detail.get(k) and not row.get(k):
            row[k] = detail[k]


def _vinted_item_id(row: Dict[str, Any]) -> str:
    plat = _norm_plat(str(row.get("platform") or "vinted"))
    if plat != "vinted":
        return _platform_item_id(row)
    pid = str(row.get("platform_item_id") or "").strip()
    if pid.isdigit():
        return pid
    rid = str(row.get("id") or "").strip()
    return rid if rid.isdigit() else pid


def fetch_listing_detail(
    row: Dict[str, Any],
    adapters: Dict[str, MarketplaceAdapter],
    *,
    headless: bool = True,
) -> Optional[Dict[str, Any]]:
    """Détail annonce — utilise l'adaptateur plateforme ou la description déjà scrapée."""
    if str(row.get("description") or "").strip():
        return None
    plat = _norm_plat(str(row.get("platform") or "vinted"))
    pid = _vinted_item_id(row) if plat == "vinted" else _platform_item_id(row)
    if not pid:
        return None
    from .engine import adapter_headless

    plat_headless = adapter_headless(plat, run_headless=headless)
    ad = adapters.get(plat)
    if ad is None:
        try:
            ad = get_adapter(plat, headless=plat_headless)
        except Exception as exc:
            logger.debug("adapter %s: %s", plat, exc)
            return None
    if not hasattr(ad, "get_item_detail"):
        return None
    try:
        if plat == "vinted":
            time.sleep(random.uniform(3.2, 8.7))
        else:
            time.sleep(random.uniform(0.4, 2.2))
        with _SEM:
            detail = ad.get_item_detail(pid)
        return detail if isinstance(detail, dict) else None
    except Exception as exc:
        logger.debug("detail %s:%s — %s", plat, pid, exc)
        return None


def refresh_vinted_session(
    adapters: Dict[str, MarketplaceAdapter],
    boot_url: str,
    *,
    headless: bool = True,
) -> bool:
    """Réutilise la session Vinted existante — pas de nouveau Chrome."""
    ad = adapters.get("vinted")
    if ad is None:
        return False
    try:
        if getattr(ad, "_client", None) is not None:
            return True
    except Exception:
        pass
    return False


def enrich_rows_deep(
    rows: List[Dict[str, Any]],
    adapters: Dict[str, MarketplaceAdapter],
    *,
    max_workers: int = 6,
    max_consecutive_fail: int = 12,
    headless: bool = True,
    boot_url: str = "",
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[int, List[str]]:
    """
    Complète descriptions / photos pour les lignes du top-K (toutes plateformes).
    Retourne (nb enrichis, échantillon échecs).
    """
    if adapters.get("vinted") and any(
        _norm_plat(str(r.get("platform") or "")) == "vinted"
        and not str(r.get("description") or "").strip()
        for r in rows
    ):
        refresh_vinted_session(adapters, boot_url, headless=headless)

    targets = [r for r in rows if not str(r.get("description") or "").strip()]
    if not targets:
        return 0, []

    def _plat_key(r: Dict[str, Any]) -> str:
        p = _norm_plat(str(r.get("platform") or "vinted"))
        return "leboncoin" if p == "lbc" else p

    lbc_first = [r for r in targets if _plat_key(r) == "leboncoin"]
    other_first = [r for r in targets if _plat_key(r) not in ("vinted", "leboncoin")]
    vinted_last = [r for r in targets if _plat_key(r) == "vinted"]
    ordered = lbc_first + other_first + vinted_last

    enriched = 0
    fail_samples: List[str] = []
    consecutive_vinted_fail = 0
    done = 0
    vinted_stop = False
    vinted_fail_limit = max(4, int(max_consecutive_fail * 0.6))

    def _one(r: Dict[str, Any]) -> Tuple[str, bool]:
        key = str(r.get("listing_key") or r.get("id") or "")
        detail = fetch_listing_detail(r, adapters, headless=headless)
        if detail:
            _merge_detail(r, detail)
            return key, True
        return key, False

    def _process_batch(batch: List[Dict[str, Any]], *, workers: int, vinted_only: bool) -> None:
        nonlocal enriched, done, consecutive_vinted_fail, vinted_stop
        if not batch:
            return
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            pending = list(batch)
            futs = {}
            while pending or futs:
                while pending and len(futs) < workers and not (vinted_stop and vinted_only):
                    r = pending.pop(0)
                    if vinted_only and vinted_stop:
                        break
                    futs[pool.submit(_one, r)] = r
                if not futs:
                    break
                for fut in as_completed(list(futs.keys())):
                    r = futs.pop(fut)
                    done += 1
                    try:
                        _, ok = fut.result()
                    except Exception:
                        ok = False
                    plat = _plat_key(r)
                    if ok:
                        enriched += 1
                        if plat == "vinted":
                            consecutive_vinted_fail = 0
                    else:
                        if len(fail_samples) < 4:
                            fail_samples.append(f"{plat}:{_platform_item_id(r)}")
                        if plat == "vinted":
                            consecutive_vinted_fail += 1
                            if consecutive_vinted_fail >= vinted_fail_limit:
                                vinted_stop = True
                                logger.info(
                                    "Deep fetch Vinted arrêté — priorité LBC/autres (%d échecs)",
                                    consecutive_vinted_fail,
                                )
                    if on_progress:
                        try:
                            on_progress(done, len(ordered))
                        except Exception:
                            pass
                    break

    non_vinted = lbc_first + other_first
    _process_batch(non_vinted, workers=min(8, max_workers), vinted_only=False)
    if vinted_last and not vinted_stop:
        _process_batch(vinted_last, workers=min(3, max_workers), vinted_only=True)
    return enriched, fail_samples

