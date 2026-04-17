from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable

import structlog


SECRET_FIELDS: frozenset[str] = frozenset({
    "github_token",
    "minimax_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "api_key",
    "authorization",
})


def _mask_secrets(_logger, _method, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in SECRET_FIELDS and event_dict[key] is not None:
            event_dict[key] = "***"
    return event_dict


def configure_logging(
    log_path: Path | None = None,
    *,
    level: str = "INFO",
) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mask_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
