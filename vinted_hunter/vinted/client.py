"""Async client for Vinted's public JSON API.

Design goals: be a *polite* client - the IP it runs from is the owner's home connection, the same one
they buy from. One shared session, a global minimum interval between requests, a hard hourly request
cap, and on the FIRST 429/403 the client stops (no retry) and pauses 10 min, doubling on repeat blocks.
No proxy rotation or anti-bot evasion: if Vinted pushes back, we slow down.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any

import httpx

from ..models import Listing, SearchSpec
from .parser import item_id_from_url, item_status, parse_catalog_html, parse_item_page

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


class VintedUnavailable(RuntimeError):
    """Raised while the circuit breaker is open."""


class VintedClient:
    def __init__(self, domain: str = "www.vinted.fr", min_interval: float = 2.0, timeout: float = 20.0,
                 max_per_hour: int = 360):
        self.domain = domain
        self.base = f"https://{domain}"
        self.min_interval = min_interval
        self._http = httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._session_ready = False
        self._failures = 0
        self._open_until = 0.0
        self.requests_made = 0
        self.max_per_hour = max_per_hour
        self._window: deque[float] = deque()      # request timestamps of the last hour
        self._strikes = 0                         # consecutive 429/403 blocks (decay after 6 h clean)
        self._last_block = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    @property
    def cooldown_remaining(self) -> int:
        return max(0, int(self._open_until - time.monotonic()))

    @property
    def requests_last_hour(self) -> int:
        cutoff = time.monotonic() - 3600
        while self._window and self._window[0] < cutoff:
            self._window.popleft()
        return len(self._window)

    def has_budget(self, share: float = 1.0) -> bool:
        """Background work (harvester) only runs below `share` of the hourly cap, so searches always have room."""
        return not self.circuit_open and self.requests_last_hour < self.max_per_hour * share

    # ------------------------------------------------------------------ core
    async def _bootstrap(self) -> None:
        """Visiting the homepage gives the anonymous session cookie the API expects."""
        self._http.cookies.clear()
        await self._throttle()
        resp = await self._http.get(self.base + "/", headers={"Accept": "text/html"})
        self._session_ready = resp.status_code < 400
        log.info("Vinted session bootstrap: HTTP %s", resp.status_code)

    async def _throttle(self) -> None:
        async with self._lock:
            if self.requests_last_hour >= self.max_per_hour:      # hourly cap reached: wait for a slot
                await asyncio.sleep(self._window[0] + 3600 - time.monotonic() + 1)
            wait = self._last_request + self.min_interval * random.uniform(1.0, 1.4) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            self._window.append(self._last_request)
            self.requests_made += 1

    async def _get(self, path: str, params: Any = None, *, as_json: bool = True, retries: int = 3) -> Any:
        if self.circuit_open:
            raise VintedUnavailable(f"Vinted en pause ({self.cooldown_remaining}s)")
        if not self._session_ready:
            await self._bootstrap()

        for attempt in range(retries):
            await self._throttle()
            try:
                headers = None if as_json else {"Accept": "text/html,application/xhtml+xml"}
                resp = await self._http.get(self.base + path, params=params, headers=headers)
            except httpx.TransportError as exc:
                log.warning("Vinted transport error on %s: %s %s", path, type(exc).__name__, exc)
                await asyncio.sleep(2 ** attempt)
                continue

            if resp.status_code == 401:            # session expired
                await self._bootstrap()
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code in (403, 429):
                # Rate-limited: stop at once. Retrying a 429 is exactly what gets an IP banned.
                if time.monotonic() - self._last_block > 6 * 3600:
                    self._strikes = 0
                self._strikes += 1
                self._last_block = time.monotonic()
                pause = min(3600, 600 * 2 ** (self._strikes - 1))
                self._open_until = time.monotonic() + pause
                self._session_ready = False
                log.warning("Vinted HTTP %s on %s: pausing all requests %ss (block #%s)",
                            resp.status_code, path, pause, self._strikes)
                raise VintedUnavailable(f"Vinted limite les requêtes, pause {pause // 60} min")
            if resp.status_code >= 500:
                self._failures += 1
                backoff = min(600, 15 * 2 ** self._failures)
                log.warning("Vinted HTTP %s on %s, backing off %ss", resp.status_code, path, backoff)
                if self._failures >= 3:
                    self._open_until = time.monotonic() + backoff
                    self._session_ready = False
                    raise VintedUnavailable(f"Vinted indisponible, pause {backoff}s")
                await asyncio.sleep(min(backoff, 30))
                continue

            resp.raise_for_status()
            self._failures = 0
            return resp.json() if as_json else resp.text
        # Retries exhausted (network): never return None here, callers read None as "404 / item gone".
        raise VintedUnavailable("Vinted injoignable (réseau)")

    # ------------------------------------------------------------- endpoints
    # Vinted retired the public /api/v2/catalog/items JSON endpoint (404) and the
    # item/user JSON endpoints answer 403 to anonymous clients, so we read the same
    # server-rendered HTML pages a browser gets.
    async def search(self, spec: SearchSpec, *, page: int = 1, per_page: int = 96,
                     order: str = "newest_first") -> list[Listing]:
        """One catalog page (~46 listings). `per_page` is kept for API compatibility."""
        params: list[tuple[str, Any]] = [("search_text", spec.query), ("order", order)]
        if page > 1:
            params.append(("page", page))
        params += [("catalog[]", c) for c in spec.catalog_ids]
        params += [("brand_ids[]", b) for b in spec.brand_ids]
        params += [("status_ids[]", int(c)) for c in spec.conditions]
        if spec.price_min:
            params.append(("price_from", spec.price_min))
        if spec.price_max:
            params.append(("price_to", spec.price_max))
        if spec.price_min or spec.price_max:
            params.append(("currency", "EUR"))
        page_html = await self._get("/catalog", params, as_json=False)
        return parse_catalog_html(page_html, self.domain) if page_html else []

    async def enrich(self, listing: Listing) -> Listing:
        """Load full description, every photo and the seller's rating from the item page."""
        try:
            page = await self._get(f"/items/{listing.id}", as_json=False)
            if page:
                return parse_item_page(page, listing, self.domain) or listing
        except httpx.HTTPError as exc:
            log.debug("Could not enrich %s: %s", listing.id, exc)
        return listing

    async def seller_wardrobe(self, user_id: int) -> list[Listing]:
        """The seller's other listings (first page of their profile)."""
        page = await self._get(f"/member/{user_id}", as_json=False)
        return parse_catalog_html(page, self.domain) if page else []

    async def item_status(self, item_id: int) -> tuple[str, float | None]:
        """Status for the sales database: active | reserved | sold | gone (404 = deleted or sold)."""
        page = await self._get(f"/items/{item_id}", as_json=False)
        if page is None:
            return "gone", None
        return item_status(page)

    async def listing_from_url(self, url: str) -> Listing | None:
        """Build a listing from a bare URL (used by /analyze)."""
        item_id = item_id_from_url(url)
        if not item_id:
            return None
        page = await self._get(f"/items/{item_id}", as_json=False)
        return parse_item_page(page, None, self.domain, item_id) if page else None
