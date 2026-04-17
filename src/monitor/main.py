from __future__ import annotations

import asyncio
import datetime as dt
import json
import signal
import sys
import traceback
from pathlib import Path

import structlog

from monitor.bot.app import create_application
from monitor.clients.github import GitHubClient
from monitor.clients.llm import LLMClient
from monitor.config import ConfigFile, Settings, load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging
from monitor.pipeline.digest import run_digest
from monitor.pipeline.surge import run_surge
from monitor.pipeline.weekly import build_weekly_digest
from monitor.scheduler import (
    create_scheduler,
    start_scheduler,
    stop_scheduler,
)
from monitor.scoring.preference import PreferenceBuilder
from monitor.scoring.rules import RuleEngine
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run() -> int:
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
    scheduler = None
    gh_client = None
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
        bot_app, scheduler, gh_client = await _maybe_start_bot_and_scheduler(
            settings, state, conn, stop,
        )

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        if scheduler is not None:
            try:
                await stop_scheduler(scheduler)
            except Exception:  # noqa: BLE001
                log.exception("shutdown.scheduler_stop_failed")
        if bot_app is not None:
            for step in (bot_app.updater.stop, bot_app.stop, bot_app.shutdown):
                try:
                    await step()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown.bot_step_failed", step=step.__name__)
        if gh_client is not None:
            try:
                await gh_client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.exception("shutdown.gh_client_close_failed")
        await conn.close()
        log.info("shutdown.done")
    return 0


async def _maybe_start_bot_and_scheduler(
    settings: Settings,
    state: DaemonState,
    conn,
    stop: asyncio.Event,
):
    """Returns (bot_app, scheduler, gh_client) tuple or (None, None, None)."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.disabled", reason="missing_credentials")
        log.info("scheduler.disabled", reason="telegram_disabled")
        return (None, None, None)

    llm_client = _build_llm_client(settings, state.config)

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

    # github_client is async context manager — enter it for the daemon lifetime.
    gh_client = GitHubClient(
        token=settings.github_token, request_timeout_s=20.0,
    )
    await gh_client.__aenter__()

    # `bot_app` is created below, but both /digest_now and the scheduler's
    # digest callable need to reference it. The trigger closes over a
    # mutable holder dict; we populate the dict *after* create_application
    # returns, so by the time any trigger fires the `app` key is live.
    bot_app_holder: dict = {"app": None}

    async def digest_trigger() -> dict:
        return await run_digest(
            push_type="digest",
            github_client=gh_client,
            llm_score_fn=(llm_client.score_repo if llm_client else _no_llm_score),
            rule_engine=RuleEngine(state.config),
            state=state,
            conn=conn,
            bot_app=bot_app_holder["app"],
            chat_id=settings.telegram_chat_id,
        )

    bot_app = create_application(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        conn=conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=state.config.preference_refresh_every,
        config_reloader=config_reloader,
        digest_trigger=digest_trigger,
    )
    bot_app_holder["app"] = bot_app  # now trigger's closure can see it

    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
    except Exception:
        log.exception("telegram.start_failed")
        try:
            await bot_app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            await gh_client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        return (None, None, None)

    log.info("telegram.started", chat_id=settings.telegram_chat_id)

    async def surge_callable() -> dict:
        return await run_surge(
            github_client=gh_client,
            llm_score_fn=(llm_client.score_repo if llm_client else _no_llm_score),
            rule_engine=RuleEngine(state.config),
            state=state,
            conn=conn,
            bot_app=bot_app,
            chat_id=settings.telegram_chat_id,
        )

    async def weekly_send_callable() -> None:
        text = await build_weekly_digest(conn)
        try:
            await bot_app.bot.send_message(
                chat_id=int(settings.telegram_chat_id), text=text,
            )
        except Exception:  # noqa: BLE001
            log.exception("weekly.send_failed")

    scheduler = create_scheduler(
        state=state,
        conn=conn,
        digest_callable=digest_trigger,
        surge_callable=surge_callable,
        weekly_send_callable=weekly_send_callable,
    )
    await start_scheduler(scheduler)
    log.info("scheduler.started")

    return (bot_app, scheduler, gh_client)


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


async def _no_llm_score(*_a, **_k):
    from monitor.scoring.types import LLMScoreError
    raise LLMScoreError("LLM not configured", cause="missing_key")


def cli() -> None:
    sys.exit(asyncio.run(run()))
