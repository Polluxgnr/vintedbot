"""Wire every service together (composition root)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .ai.cascade import AICascade
from .ai.llm import LLM
from .config import Settings, get_settings
from .db import Database
from .engine import Hunter
from .niches import NicheCatalog
from .vinted.client import VintedClient


@dataclass
class Services:
    settings: Settings
    db: Database
    vinted: VintedClient
    llm: LLM
    hunter: Hunter
    niches: NicheCatalog

    async def close(self) -> None:
        await self.vinted.close()
        await self.llm.close()
        await self.db.close()


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def build_services(settings: Settings | None = None) -> Services:
    s = settings or get_settings()
    niches = NicheCatalog.load(s.niches_file)   # fail fast on a broken YAML, before opening resources
    db = Database(s.db_path)
    await db.connect()
    vinted = VintedClient(s.vinted_domain, s.vinted_min_request_interval, s.vinted_timeout)
    llm = LLM(s.openrouter_api_key, fallbacks=s.fallback_models, spend_today=db.ai_spend_today,
              record_usage=db.record_ai_usage)
    hunter = Hunter(s, db, vinted, AICascade(llm, s), niches)
    return Services(s, db, vinted, llm, hunter, niches)
