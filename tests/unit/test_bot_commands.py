import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.commands import (
    handle_pause,
    handle_reload,
    handle_resume,
    handle_status,
    handle_top,
)
from monitor.config import ConfigFile
from monitor.db import connect, insert_pushed_item, run_migrations
from monitor.models import RepoCandidate
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "cmd.db"


def _fake_update() -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock())
    )


def _repo(name: str, score: float) -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=now,
        pushed_at=now,
        owner_login=name.split("/")[0],
        topics=[],
        final_score=score,
        summary=f"summary for {name}",
        recommendation_reason="r",
    )


async def test_top_lists_recent_pushed_items(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    for i, (name, score) in enumerate([("a/one", 9.0), ("a/two", 7.5), ("a/three", 5.0)]):
        await insert_pushed_item(
            conn,
            repo=_repo(name, score),
            push_type="digest",
            tg_chat_id="1",
            now=dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
            + dt.timedelta(seconds=i),
        )

    update = _fake_update()
    await handle_top(update, conn=conn, limit=2)

    update.message.reply_text.assert_awaited()
    reply = update.message.reply_text.await_args.args[0]
    assert "a/three" in reply  # most recent
    assert "a/two" in reply
    assert "a/one" not in reply  # excluded by limit=2
    await conn.close()


async def test_top_with_no_pushes_returns_placeholder(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    update = _fake_update()
    await handle_top(update, conn=conn, limit=10)
    reply = update.message.reply_text.await_args.args[0]
    assert "暂无" in reply or "no" in reply.lower()
    await conn.close()


async def test_status_reports_paused_flag_and_recent_runs(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    await state.set_paused(True)

    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    await conn.execute(
        "INSERT INTO run_log (kind, started_at, ended_at, status, stats) "
        "VALUES ('digest_morning', ?, ?, 'ok', ?)",
        (now.isoformat(), (now + dt.timedelta(seconds=3)).isoformat(), json.dumps({"repos_pushed": 4})),
    )
    await conn.commit()

    update = _fake_update()
    await handle_status(update, conn=conn, state=state)

    reply = update.message.reply_text.await_args.args[0]
    assert "paused" in reply.lower() or "暂停" in reply
    assert "digest_morning" in reply
    await conn.close()


async def test_pause_flips_state_and_confirms(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    assert state.paused is False

    update = _fake_update()
    await handle_pause(update, state=state)

    assert state.paused is True
    reply = update.message.reply_text.await_args.args[0]
    assert "暂停" in reply or "paused" in reply.lower()
    await conn.close()


async def test_resume_flips_state_and_confirms(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    await state.set_paused(True)

    update = _fake_update()
    await handle_resume(update, state=state)

    assert state.paused is False
    reply = update.message.reply_text.await_args.args[0]
    assert "恢复" in reply or "resumed" in reply.lower()
    await conn.close()


async def test_reload_invokes_reloader_and_swaps_config(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile(keywords=["old"]))

    new_config = ConfigFile(keywords=["new"])

    async def fake_reloader() -> ConfigFile:
        return new_config

    update = _fake_update()
    await handle_reload(update, state=state, config_reloader=fake_reloader)

    assert state.config.keywords == ["new"]
    reply = update.message.reply_text.await_args.args[0]
    assert "reload" in reply.lower() or "重载" in reply or "已更新" in reply
    await conn.close()


async def test_reload_reports_error_and_keeps_old_config(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile(keywords=["old"]))

    async def failing_reloader() -> ConfigFile:
        raise ValueError("bad json")

    update = _fake_update()
    await handle_reload(update, state=state, config_reloader=failing_reloader)

    # State unchanged
    assert state.config.keywords == ["old"]
    reply = update.message.reply_text.await_args.args[0]
    assert "bad json" in reply or "失败" in reply or "error" in reply.lower()
    await conn.close()
