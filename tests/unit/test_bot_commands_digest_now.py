import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.commands import handle_digest_now
from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "dn.db"


def _fake_update() -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock())
    )


async def _make_state(tmp_db: Path) -> DaemonState:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    return await DaemonState.load(conn=conn, config=ConfigFile())


async def test_digest_now_runs_when_lock_free(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)
    trigger = AsyncMock(return_value={"repos_pushed": 3, "repos_scanned": 10})

    update = _fake_update()
    await handle_digest_now(update, state=state, digest_trigger=trigger)

    trigger.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "3" in reply and "10" in reply  # stats surfaced
    await state.conn.close()


async def test_digest_now_reports_already_running(tmp_db: Path) -> None:
    """If another digest is in-flight (lock held), the command replies
    with a 'busy' message rather than queueing."""
    state = await _make_state(tmp_db)

    slow_trigger_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_trigger():
        slow_trigger_started.set()
        await release.wait()
        return {"repos_pushed": 0, "repos_scanned": 0}

    update1 = _fake_update()
    update2 = _fake_update()

    task1 = asyncio.create_task(
        handle_digest_now(update1, state=state, digest_trigger=slow_trigger)
    )
    await slow_trigger_started.wait()
    # Now the lock is held; second call should bail immediately
    await handle_digest_now(update2, state=state, digest_trigger=AsyncMock())
    reply2 = update2.message.reply_text.await_args.args[0]
    assert "already running" in reply2.lower() or "运行中" in reply2

    release.set()
    await task1
    await state.conn.close()
