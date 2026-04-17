import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    get_daemon_state,
    run_migrations,
    set_daemon_paused,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m4.db"


async def test_daemon_state_default_is_not_paused(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await get_daemon_state(conn)
    assert state["paused"] is False
    # generated row should exist after migration with a valid timestamp
    assert state["updated_at"] is not None
    await conn.close()


async def test_set_daemon_paused_roundtrip(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    await set_daemon_paused(conn, paused=True, now=now)
    state = await get_daemon_state(conn)
    assert state["paused"] is True
    assert state["updated_at"] == now.isoformat()

    await set_daemon_paused(conn, paused=False, now=now + dt.timedelta(minutes=5))
    state = await get_daemon_state(conn)
    assert state["paused"] is False
    await conn.close()


async def test_migrations_from_v1_to_v2_preserves_existing_rows(tmp_db: Path) -> None:
    """Re-running migrations on a v1 DB must add daemon_state without
    touching existing tables."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    # Insert an unrelated row to verify it survives v2
    await conn.execute(
        "INSERT INTO blacklist (kind, value, added_at, source) "
        "VALUES ('author', 'badactor', '2026-01-01T00:00:00+00:00', 'manual')"
    )
    await conn.commit()

    applied = await run_migrations(conn)
    assert applied == 0  # already at latest

    async with conn.execute("SELECT value FROM blacklist") as cur:
        rows = await cur.fetchall()
    assert rows == [("badactor",)]
    await conn.close()
