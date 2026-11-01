"""Wire every service together (composition root)."""

from __future__ import annotations

import logging
import socket
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

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


def setup_logging(level: str, log_file: Path | None = None) -> None:
    """Console logging, plus a rotating file when running in the background (no console to read)."""
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:                        # pythonw / background task: no console
        handlers.append(logging.StreamHandler())
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers or None)
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_instance_lock: socket.socket | None = None


def acquire_single_instance(port: int = 47821) -> bool:
    """Only one bot per machine: two pollers on the same Telegram token kick each other off.

    Binding a localhost port is an OS-level mutex that disappears automatically if the process dies.
    """
    global _instance_lock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)                                # also lets Docker's health check see the bot is alive
    except OSError:
        sock.close()
        return False
    _instance_lock = sock
    return True


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
