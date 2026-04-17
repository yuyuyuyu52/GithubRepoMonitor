from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog


SECRET_FIELDS: frozenset[str] = frozenset({
    # Application-specific; extend as new credential fields are added.
    "github_token",
    "minimax_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "api_key",
    "authorization",
})


def _mask_secrets(
    _logger: Any,
    _method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in SECRET_FIELDS and event_dict[key] is not None:
            event_dict[key] = "***"
    return event_dict


def _resolve_level(level: str) -> int:
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"Invalid log level: {level!r}")
    return numeric


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
    # Close existing handlers before discarding them — clear() does not close.
    for existing in root.handlers[:]:
        try:
            existing.close()
        except Exception:  # noqa: BLE001 - never let a stale handler block reconfigure
            pass
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(_resolve_level(level))

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _mask_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
