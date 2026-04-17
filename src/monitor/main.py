from __future__ import annotations

import asyncio
import json
import signal
import sys
import traceback
from pathlib import Path

import structlog

from monitor.bot.app import create_application
from monitor.clients.llm import LLMClient
from monitor.config import ConfigFile, Settings, load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging
from monitor.scoring.preference import PreferenceBuilder
from monitor.state import DaemonState


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

    bot_app = None
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

        state = await DaemonState.load(conn=conn, config=config)

        # Optional TG bot — only starts if both credentials are present.
        bot_app = await _maybe_start_bot(settings, config, state, conn)

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        if bot_app is not None:
            for step in (bot_app.updater.stop, bot_app.stop, bot_app.shutdown):
                try:
                    await step()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown.bot_step_failed", step=step.__name__)
        await conn.close()
        log.info("shutdown.done")
    return 0


async def _maybe_start_bot(
    settings: Settings,
    config: ConfigFile,
    state: DaemonState,
    conn,
):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.disabled", reason="missing_credentials")
        return None
    if not settings.minimax_api_key:
        # No LLM → PreferenceBuilder's regen calls will fail; bot runs but
        # regen is disabled. Log so the operator knows.
        log.info("telegram.llm_disabled", reason="missing_minimax_key")

    llm_client = _build_llm_client(settings, config)
    pref_builder = PreferenceBuilder(
        conn=conn,
        llm_generate_profile=llm_client.generate_text
        if llm_client is not None
        else _no_llm_generator,
    )

    async def config_reloader() -> ConfigFile:
        if settings.config_path is None:
            raise RuntimeError("MONITOR_CONFIG is not set")
        path = Path(settings.config_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ConfigFile.model_validate(payload)

    app = create_application(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        conn=conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=config.preference_refresh_every,
        config_reloader=config_reloader,
    )

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    except Exception:
        log.exception("telegram.start_failed")
        try:
            await app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        return None

    log.info("telegram.started", chat_id=settings.telegram_chat_id)
    return app


def _build_llm_client(settings: Settings, config: ConfigFile) -> LLMClient | None:
    if not settings.minimax_api_key:
        return None
    return LLMClient(
        api_key=settings.minimax_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
    )


async def _no_llm_generator(_prompt: str) -> str:
    raise RuntimeError("LLM not configured; preference regeneration skipped")


def cli() -> None:
    sys.exit(asyncio.run(run()))
