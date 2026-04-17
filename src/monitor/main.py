from __future__ import annotations

import asyncio
import signal
import sys

import structlog

from monitor.config import load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging


log = structlog.get_logger(__name__)


async def run() -> int:
    settings, config = load_config()
    configure_logging(settings.log_path)
    log.info(
        "startup",
        db_path=str(settings.db_path),
        keywords=config.keywords,
        languages=config.languages,
    )

    conn = await connect(settings.db_path)
    try:
        applied = await run_migrations(conn)
        log.info("migrations.applied", count=applied)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        await conn.close()
        log.info("shutdown.done")
    return 0


def cli() -> None:
    sys.exit(asyncio.run(run()))
