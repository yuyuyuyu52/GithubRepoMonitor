import json
from pathlib import Path

import pytest

from monitor.config import ConfigFile, Settings, load_config


def test_defaults_when_no_file_and_no_env(monkeypatch, tmp_path):
    for var in ("MONITOR_CONFIG", "MONITOR_DB_PATH", "GITHUB_TOKEN",
                "MINIMAX_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)

    settings, config = load_config()

    assert settings.github_token is None
    assert config.keywords == ["agent", "llm", "monitor", "tooling"]
    assert config.min_stars == 100
    assert config.weights.rule == 0.55
    assert config.weights.llm == 0.45


def test_config_file_overrides_defaults(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "keywords": ["rust"],
        "min_stars": 500,
        "surge": {"velocity_multiple": 5.0, "velocity_absolute_day": 50, "cooldown_days": 2},
    }), encoding="utf-8")
    monkeypatch.setenv("MONITOR_CONFIG", str(cfg_path))

    _, config = load_config()

    assert config.keywords == ["rust"]
    assert config.min_stars == 500
    assert config.surge.velocity_multiple == 5.0
    assert config.surge.cooldown_days == 2
    # unspecified fields stay at defaults
    assert config.max_repo_age_days == 180


def test_env_vars_populate_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("MINIMAX_API_KEY", "mx_y")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg_z")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    settings, _ = load_config()

    assert settings.github_token == "ghp_x"
    assert settings.minimax_api_key == "mx_y"
    assert settings.telegram_bot_token == "tg_z"
    assert settings.telegram_chat_id == "12345"


def test_missing_config_file_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITOR_CONFIG", str(tmp_path / "nope.json"))

    _, config = load_config()

    assert config.keywords == ["agent", "llm", "monitor", "tooling"]
