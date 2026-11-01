"""OpenRouter client with JSON extraction, real cost tracking and a daily budget guard."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    data: Any
    cost: float
    tokens_in: int
    tokens_out: int
    model: str


def _repair_truncated(text: str) -> Any:
    """A reply cut by max_tokens: keep every complete field, close the open brackets."""
    stack: list[str] = []
    in_str = esc = False
    cuts: list[tuple[int, str]] = []          # (position of a top-level-safe comma, closers needed there)
    for i, ch in enumerate(text):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
        elif ch == ",":
            cuts.append((i, "".join(reversed(stack))))
    for pos, closers in reversed(cuts):
        try:
            return json.loads(text[:pos] + closers)
        except json.JSONDecodeError:
            continue
    return None


def extract_json(text: str) -> Any:
    """Models wrap JSON in prose or ```json fences, or get cut by max_tokens - recover the payload."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    body = text[start:]
    try:
        return json.JSONDecoder().raw_decode(body)[0]     # JSON followed by prose
    except json.JSONDecodeError:
        return _repair_truncated(body)


def image_part(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


class LLM:
    def __init__(
        self,
        api_key: str,
        *,
        fallbacks: list[str] | None = None,
        spend_today: Callable[[], Awaitable[float]],
        record_usage: Callable[[str, int, float, int, int], Awaitable[None]],
    ):
        self.api_key = api_key
        self.fallbacks = fallbacks or []
        self._spend_today = spend_today
        self._record = record_usage
        self._http = httpx.AsyncClient(timeout=90)
        self._sem = asyncio.Semaphore(4)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._http.aclose()

    async def ensure_budget(self, budget_usd: float, reserve_ratio: float = 1.0) -> None:
        """Tier-aware guard: expensive tiers stop earlier (reserve_ratio < 1) so triage keeps running."""
        if await self._spend_today() >= budget_usd * reserve_ratio:
            raise BudgetExceeded(f"Budget IA du jour atteint ({budget_usd:.2f}$)")

    async def complete(
        self,
        model: str,
        system: str,
        content: str | list[dict],
        *,
        tier: int,
        max_tokens: int = 1200,
        temperature: float = 0.2,
        reasoning: int = 0,
        want_json: bool = True,
    ) -> LLMResult:
        if not self.enabled:
            raise RuntimeError("OPENROUTER_API_KEY manquante")
        payload = {
            "model": model,
            "models": [model, *[m for m in self.fallbacks if m != model]],  # OpenRouter-side failover
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Recent models switch to hidden reasoning on long prompts and can burn the whole token budget
            # before answering (empty content). Control it explicitly: off, or a bounded thinking budget.
            "reasoning": {"max_tokens": reasoning} if reasoning else {"enabled": False},
            "usage": {"include": True},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Title": "Vinted Hunter"}

        async with self._sem:
            for attempt in range(3):
                try:
                    resp = await self._http.post(API_URL, json=payload, headers=headers)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    body = resp.json()
                    break
                except httpx.TransportError as exc:
                    log.warning("OpenRouter transport error: %s", exc)
                    await asyncio.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError("OpenRouter indisponible")

        if "error" in body:
            raise RuntimeError(f"OpenRouter: {body['error']}")
        text = body["choices"][0]["message"].get("content") or ""
        usage = body.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        used_model = body.get("model", model)
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        await self._record(used_model, tier, cost, tin, tout)
        log.info("LLM tier %s %s: %s+%s tok, $%.5f", tier, used_model, tin, tout, cost)
        return LLMResult(text, extract_json(text) if want_json else None, cost, tin, tout, used_model)
