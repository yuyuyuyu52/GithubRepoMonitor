import datetime as dt
from pathlib import Path

import pytest

from monitor.config import ConfigFile
from monitor.db import connect, get_daemon_state, run_migrations, set_daemon_paused
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


async def test_daemon_state_loads_initial_paused_from_db(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    # Persist paused=True then load
    await set_daemon_paused(conn, paused=True)

    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    assert state.paused is True
    assert state.config is config
    await conn.close()


async def test_daemon_state_set_paused_writes_through(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    assert state.paused is False

    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    await state.set_paused(True, now=now)
    assert state.paused is True

    # Write-through: a fresh load sees the persisted value
    state2 = await DaemonState.load(conn=conn, config=config)
    assert state2.paused is True
    db_state = await get_daemon_state(conn)
    assert db_state["updated_at"] == now.isoformat()
    await conn.close()


async def test_daemon_state_reload_config_replaces_live_reference(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    config_a = ConfigFile(keywords=["agent"])
    config_b = ConfigFile(keywords=["rust"])
    state = await DaemonState.load(conn=conn, config=config_a)
    assert state.config.keywords == ["agent"]

    state.reload_config(config_b)
    assert state.config.keywords == ["rust"]
    # Prior reference unchanged
    assert config_a.keywords == ["agent"]
    await conn.close()
