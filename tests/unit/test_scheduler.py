from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.scheduler import create_scheduler
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "sched.db"


async def _make_state(tmp_db: Path) -> DaemonState:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    return await DaemonState.load(conn=conn, config=ConfigFile())


async def test_create_scheduler_mounts_four_jobs(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)

    scheduler = create_scheduler(
        state=state,
        conn=state.conn,
        digest_callable=AsyncMock(return_value={}),
        surge_callable=AsyncMock(return_value={}),
        weekly_send_callable=AsyncMock(return_value=None),
    )
    assert isinstance(scheduler, AsyncIOScheduler)

    job_ids = sorted(j.id for j in scheduler.get_jobs())
    assert job_ids == ["digest_evening", "digest_morning", "surge_poll", "weekly_digest"]
    await state.conn.close()


async def test_scheduler_respects_config_times(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)

    scheduler = create_scheduler(
        state=state,
        conn=state.conn,
        digest_callable=AsyncMock(return_value={}),
        surge_callable=AsyncMock(return_value={}),
        weekly_send_callable=AsyncMock(return_value=None),
    )

    jobs = {j.id: j for j in scheduler.get_jobs()}
    # Morning digest: hour=8
    morning_trigger = jobs["digest_morning"].trigger
    assert str(morning_trigger.fields[morning_trigger.FIELD_NAMES.index("hour")]) == "8"
    # Evening digest: hour=20
    evening_trigger = jobs["digest_evening"].trigger
    assert str(evening_trigger.fields[evening_trigger.FIELD_NAMES.index("hour")]) == "20"
    await state.conn.close()
