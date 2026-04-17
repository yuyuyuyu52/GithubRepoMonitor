from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: float = 0.55
    llm: float = 0.45


class SurgeThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    velocity_multiple: float = 3.0
    velocity_absolute_day: float = 20.0
    cooldown_days: int = 3


class ConfigFile(BaseModel):
    """Contents of the JSON config file (pointed to by MONITOR_CONFIG).

    Raises pydantic.ValidationError on unknown keys so operator typos fail loud."""

    model_config = ConfigDict(extra="forbid")

    keywords: List[str] = Field(
        default_factory=lambda: ["agent", "llm", "monitor", "tooling"]
    )
    languages: List[str] = Field(
        default_factory=lambda: ["Python", "Rust", "Go"]
    )
    min_stars: int = 100
    max_repo_age_days: int = 180
    top_n: int = 10
    digest_cooldown_days: int = 14
    surge: SurgeThresholds = Field(default_factory=SurgeThresholds)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    llm_model: str = "minimax-m2"
    llm_base_url: str = "https://api.minimax.chat/anthropic/v1"
    preference_refresh_every: int = 5


class Settings(BaseSettings):
    """Runtime settings - paths and secrets from env vars."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    config_path: Optional[Path] = Field(default=None, alias="MONITOR_CONFIG")
    db_path: Path = Field(default=Path("monitor.db"), alias="MONITOR_DB_PATH")
    log_path: Optional[Path] = Field(default=None, alias="MONITOR_LOG_PATH")
    github_token: Optional[str] = Field(default=None, alias="GITHUB_TOKEN")
    minimax_api_key: Optional[str] = Field(default=None, alias="MINIMAX_API_KEY")
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, alias="TELEGRAM_CHAT_ID")


def load_config(settings: Settings | None = None) -> tuple[Settings, ConfigFile]:
    settings = settings or Settings()
    if settings.config_path and settings.config_path.exists():
        payload = json.loads(settings.config_path.read_text(encoding="utf-8"))
        config = ConfigFile.model_validate(payload)
    else:
        config = ConfigFile()
    return settings, config
