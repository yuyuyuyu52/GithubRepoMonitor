from __future__ import annotations

from typing import Any, Awaitable, Callable

import aiosqlite
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from monitor.state import DaemonState


log = structlog.get_logger(__name__)

DigestCallable = Callable[[], Awaitable[dict]]
SurgeCallable = Callable[[], Awaitable[dict]]
WeeklySendCallable = Callable[[], Awaitable[None]]


def create_scheduler(
    *,
    state: DaemonState,
    conn: aiosqlite.Connection,
    digest_callable: DigestCallable,
    surge_callable: SurgeCallable,
    weekly_send_callable: WeeklySendCallable,
) -> AsyncIOScheduler:
    """Mount 4 scheduled jobs. Each job is lock-guarded to prevent
    overlapping runs. `digest_callable` / `surge_callable` /
    `weekly_send_callable` are pre-bound partials that know about the
    deps (github_client, llm_score_fn, etc.) without the scheduler
    itself needing to."""
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def _guarded_digest() -> None:
        if state.digest_lock.locked():
            log.info("scheduler.digest_skipped_lock_held")
            return
        async with state.digest_lock:
            try:
                await digest_callable()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.digest_raised")

    async def _guarded_surge() -> None:
        if state.digest_lock.locked():
            log.info("scheduler.surge_skipped_lock_held")
            return
        async with state.digest_lock:
            try:
                await surge_callable()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.surge_raised")

    async def _guarded_weekly() -> None:
        if state.digest_lock.locked():
            log.info("scheduler.weekly_skipped_lock_held")
            return
        async with state.digest_lock:
            try:
                await weekly_send_callable()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.weekly_raised")

    scheduler.add_job(
        _guarded_digest,
        CronTrigger(hour=8, minute=0),
        id="digest_morning",
        name="Morning digest",
        max_instances=1,
    )
    scheduler.add_job(
        _guarded_digest,
        CronTrigger(hour=20, minute=0),
        id="digest_evening",
        name="Evening digest",
        max_instances=1,
    )
    scheduler.add_job(
        _guarded_surge,
        IntervalTrigger(minutes=30),
        id="surge_poll",
        name="Surge poll",
        max_instances=1,
    )
    scheduler.add_job(
        _guarded_weekly,
        CronTrigger(day_of_week="sun", hour=21, minute=0),
        id="weekly_digest",
        name="Weekly digest",
        max_instances=1,
    )
    return scheduler


async def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.start()


async def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.shutdown(wait=False)
