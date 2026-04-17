from __future__ import annotations

import asyncio
import signal
import sys
import traceback

import structlog

from monitor.config import load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging


log = structlog.get_logger(__name__)


async def run() -> int:
    # load_config + configure_logging run before the logger is available, so
    # any exception here has to go to stderr for systemd's journal.
    try:
        settings, config = load_config()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    configure_logging(settings.log_path)

    # Install signal handlers BEFORE blocking IO (connect, run_migrations) so a
    # SIGTERM arriving during migration still triggers graceful shutdown
    # instead of the OS default kill.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "startup",
        db_path=str(settings.db_path),
        config_path=str(settings.config_path) if settings.config_path else None,
        keywords=config.keywords,
        languages=config.languages,
    )

    try:
        conn = await connect(settings.db_path)
    except Exception:
        log.exception("startup.connect_failed")
        return 1

    try:
        try:
            applied = await run_migrations(conn)
        except Exception:
            log.exception("startup.migrations_failed")
            return 1
        log.info("migrations.applied", count=applied)

        if stop.is_set():
            log.info("shutdown.requested_during_startup")
            return 0

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        await conn.close()
        log.info("shutdown.done")
    return 0


def cli() -> None:
    sys.exit(asyncio.run(run()))
