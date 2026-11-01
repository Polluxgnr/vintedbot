"""Static configuration loaded from environment / .env.

Runtime-tunable knobs (min profit, budget, pause...) live in the database and
are edited from Telegram; see `RuntimeSettings`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -----------------------------------------------------------
    telegram_token: str = ""
    # Comma separated chat ids allowed to use the bot. Empty = first /start claims the bot.
    telegram_allowed_chats: str = ""

    # --- AI (OpenRouter: one key, every model) -----------------------------
    openrouter_api_key: str = ""
    model_triage: str = "anthropic/claude-haiku-5.5"   # batch text screening, ~$0.0005 / 20 items
    model_vision: str = "anthropic/claude-haiku-5.5"   # photo check, ~$0.001 / item
    model_deep: str = "anthropic/claude-sonnet-5.5"    # final verdict on top deals, ~$0.04 / item
    model_expert: str = "anthropic/claude-opus-5.5"    # on-demand "expert" button only
    model_fallbacks: str = "google/gemini-3.5-flash-lite,openai/gpt-5.4-mini"

    # --- Vinted --------------------------------------------------------------
    vinted_domain: str = "www.vinted.fr"
    vinted_min_request_interval: float = 3.0   # seconds between requests (politeness)
    vinted_timeout: float = 30.0

    # --- Storage -------------------------------------------------------------
    # Kept outside the project on purpose: a live SQLite file inside a synced
    # folder (Google Drive, OneDrive...) gets locked or corrupted by the sync client.
    db_path: Path = Path.home() / ".vinted_hunter" / "hunter.db"
    niches_file: Path = ROOT / "config" / "niches.yaml"

    # --- Engine --------------------------------------------------------------
    watch_interval_s: int = 120         # how often each watch is re-polled
    max_details_per_cycle: int = 8     # detail-page fetches per watch cycle (cascade on requests too)
    max_triage_per_cycle: int = 40
    max_vision_per_cycle: int = 6

    log_level: str = "INFO"

    @field_validator("db_path", "niches_file", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else ROOT / v

    @property
    def allowed_chats(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_chats.replace(" ", "").split(",") if x}

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.model_fallbacks.split(",") if m.strip()]


class RuntimeSettings(BaseModel):
    """Knobs editable live from Telegram (/settings). Persisted as JSON in the kv table."""

    alert_mode: str = "pepites"        # "pepites" = only verified insane deals | "normal"
    min_profit_eur: float = 30.0
    min_margin_pct: float = 40.0
    min_discount_pct: float = 35.0     # price at least 35 % under the estimated value
    min_confidence: float = 0.6
    min_score: int = 70
    ai_daily_budget_usd: float = 1.5
    auto_deep: bool = True             # run the expensive tier automatically on exceptional deals
    deep_min_profit_eur: float = 40.0
    alerts_paused: bool = False
    # Buyer-side Vinted fees (protection fee), used to compute the real landed cost.
    protection_fixed_eur: float = 0.70
    protection_pct: float = 5.0
    quiet_hours: list[int] = Field(default_factory=list)  # e.g. [0,1,2,3,4,5,6] -> no push at night


@lru_cache
def get_settings() -> Settings:
    return Settings()
