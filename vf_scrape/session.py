from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Set

from vf.engine import adapter_headless, bootstrap_timeout_sec, run_with_timeout
from vf.market import get_adapter

_log = logging.getLogger("vf_scrape.session")


class ScrapeSession:
    """Pool d'adapters — une session HTTP/navigateur réutilisée par plateforme."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._adapters: Dict[str, Any] = {}
        self._bootstrapped: Set[str] = set()

    def _norm(self, plat: str) -> str:
        p = (plat or "vinted").lower().replace("lbc", "leboncoin").replace("cash-express", "cashexpress")
        return p

    def _close_other_drivers(self, except_plat: str) -> None:
        for key, ad in list(self._adapters.items()):
            if key == except_plat:
                continue
            drv = getattr(ad, "_driver", None)
            if drv is None:
                continue
            try:
                if hasattr(ad, "close"):
                    ad.close()
                else:
                    from vf.engine import safe_quit_driver

                    safe_quit_driver(drv)
                    ad._driver = None  # type: ignore[attr-defined]
            except Exception:
                pass
            time.sleep(0.12)

    def adapter(self, plat: str) -> Any:
        key = self._norm(plat)
        if key not in self._adapters:
            h = adapter_headless(key, run_headless=self.headless)
            self._adapters[key] = get_adapter(key, headless=h)
        return self._adapters[key]

    def _do_bootstrap(self, key: str, query: str) -> None:
        ad = self.adapter(key)
        h = adapter_headless(key, run_headless=self.headless)
        q = (query or "").strip()
        if key == "vinted":
            if getattr(ad, "_bootstrapped", False) and getattr(ad, "_session_ready", lambda: False)():
                _log.debug("[%s] session déjà prête", key)
                return
            ad.bootstrap("https://www.vinted.fr", headless=h)
            return
        if hasattr(ad, "ensure_session"):
            ad.ensure_session(q or "test", headless=h)  # type: ignore[attr-defined]
            return
        if hasattr(ad, "build_search_url"):
            url = ad.build_search_url(q) if q else f"https://www.{key}.fr"
            ad.bootstrap(url, headless=h)
            return
        ad.bootstrap(q or "", headless=h)

    def is_bootstrapped(self, plat: str) -> bool:
        key = self._norm(plat)
        if key in self._bootstrapped:
            return True
        ad = self._adapters.get(key)
        if ad is None:
            return False
        if key == "vinted":
            if getattr(ad, "_bootstrapped", False) and getattr(ad, "_session_ready", lambda: False)():
                return True
        return bool(getattr(ad, "_bootstrapped", False))

    def bootstrap(self, plat: str, query: str = "") -> None:
        from vf.engine import _LAUNCH_LOCK

        key = self._norm(plat)
        if self.is_bootstrapped(key):
            _log.debug("[%s] bootstrap ignoré — session déjà prête", key)
            self._bootstrapped.add(key)
            return
        timeout_s = bootstrap_timeout_sec(key)
        _log.info("[%s] bootstrap (timeout %.0fs, headless=%s)…", key, timeout_s, self.headless)
        t0 = time.monotonic()
        # Fermer les autres drivers sous lock court — ne pas tenir le lock pendant bootstrap
        # (launch_uc_chrome l'acquiert aussi ; un thread timeout + lock imbriqué = deadlock).
        if key in ("rakuten", "cashexpress", "leboncoin", "ebay"):
            with _LAUNCH_LOCK:
                self._close_other_drivers(key)
        try:
            run_with_timeout(
                lambda: self._do_bootstrap(key, query),
                timeout_s,
                label=f"{key} bootstrap",
            )
        except TimeoutError as exc:
            _log.warning("[%s] %s — poursuite scrape", key, exc)
        except Exception as exc:
            _log.warning("[%s] bootstrap erreur: %s", key, exc)
            raise
        self._bootstrapped.add(key)
        _log.info("[%s] bootstrap terminé en %.1fs", key, time.monotonic() - t0)

    def close(self) -> None:
        for ad in self._adapters.values():
            try:
                if hasattr(ad, "close"):
                    ad.close()
            except Exception:
                pass
        self._adapters.clear()
        self._bootstrapped.clear()
