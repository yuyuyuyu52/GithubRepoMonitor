import json
import logging

import pytest
import structlog

from monitor.logging_config import configure_logging


@pytest.fixture(autouse=True)
def reset_structlog():
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_logs_emit_json_with_timestamp_and_level(capsys):
    configure_logging()
    log = structlog.get_logger("test")

    log.info("hello", foo="bar")

    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "no log line emitted"
    payload = json.loads(captured[-1])
    assert payload["event"] == "hello"
    assert payload["foo"] == "bar"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_secret_fields_are_masked(capsys):
    configure_logging()
    log = structlog.get_logger("test")

    log.info("startup", github_token="ghp_supersecret", unrelated="ok")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["github_token"] == "***"
    assert payload["unrelated"] == "ok"
