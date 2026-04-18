import json
import logging

import pytest
import structlog

from monitor.logging_config import configure_logging


@pytest.fixture(autouse=True)
def reset_structlog():
    # Reset BEFORE each test too, not just after — cheap insurance against
    # order-dependent contamination (e.g. pytest-randomly or module-level
    # loggers in other modules caching a stale bind).
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    for h in logging.getLogger().handlers[:]:
        try:
            h.close()
        except Exception:
            pass
    logging.getLogger().handlers.clear()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    for h in logging.getLogger().handlers[:]:
        try:
            h.close()
        except Exception:
            pass
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


def test_invalid_level_raises():
    with pytest.raises(ValueError, match="Invalid log level"):
        configure_logging(level="bogus")


def test_noisy_third_party_loggers_are_gagged():
    """httpx/telegram/apscheduler loggers leak credentials or spam plain text
    at INFO. configure_logging must raise their threshold to WARNING so bot
    tokens never reach /var/log/monitor/app.log or journald."""
    configure_logging()
    for name in ("httpx", "httpcore", "telegram", "telegram.ext",
                 "apscheduler", "apscheduler.scheduler",
                 "apscheduler.executors.default"):
        assert logging.getLogger(name).level >= logging.WARNING, (
            f"{name} logger must be >= WARNING to avoid leaking secrets in URLs"
        )


def test_configure_logging_is_idempotent_for_handlers(tmp_path):
    # Calling twice with a file path must not leak FDs — the first handler is
    # closed before being dropped.
    log_path = tmp_path / "app.log"
    configure_logging(log_path)
    first_root_handlers = list(logging.getLogger().handlers)
    first_file_handler = next(
        h for h in first_root_handlers if isinstance(h, logging.FileHandler)
    )

    configure_logging(log_path)

    assert first_file_handler.stream is None or first_file_handler.stream.closed, (
        "previous FileHandler should have been closed on reconfigure"
    )
    # Root should now have exactly 2 handlers again (stdout + file), no duplicates.
    current_handlers = logging.getLogger().handlers
    assert len(current_handlers) == 2
