"""French translation of foreign listings. The AI does it when OpenRouter is reachable; otherwise the free
MyMemory API (no key, ~5000 chars/day anonymously) keeps cards readable and lets defect detection read them."""

from __future__ import annotations

import html
import logging

import httpx

from .lang import detect

log = logging.getLogger(__name__)

MYMEMORY = "https://api.mymemory.translated.net/get"


class FreeTranslator:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=12)
        self._exhausted = False

    async def close(self) -> None:
        await self._http.aclose()

    async def _one(self, text: str, lang: str) -> str | None:
        if not text.strip() or self._exhausted:
            return None
        try:
            r = await self._http.get(MYMEMORY, params={"q": text[:480], "langpair": f"{lang}|fr"})
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("MyMemory failed: %s", exc)
            return None
        if data.get("quotaFinished"):
            self._exhausted = True
            log.warning("MyMemory daily quota reached: translations paused until tomorrow")
            return None
        out = (data.get("responseData") or {}).get("translatedText")
        if not out or "MYMEMORY WARNING" in out:
            return None
        return html.unescape(out).strip(" -–•|")

    async def translate(self, title: str, text: str, lang: str | None = None) -> dict[str, str] | None:
        lang = lang or detect(f"{title} {text}")
        if not lang or lang in ("fr", "en"):
            return None
        t = await self._one(title, lang)
        d = await self._one(text, lang) if text else ""
        if not t and not d:
            return None
        return {"title": t or title, "text": d or ""}
