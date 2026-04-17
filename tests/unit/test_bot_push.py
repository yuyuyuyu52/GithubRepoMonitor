import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.push import push_repo
from monitor.db import connect, run_migrations
from monitor.models import RepoCandidate


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "push.db"


def _repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="widgets",
        language="Python",
        stars=420,
        forks=21,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login="acme",
        topics=["agent"],
        rule_score=7.0,
        llm_score=8.2,
        final_score=7.7,
        summary="Solid widget",
        recommendation_reason="matches",
    )


def _fake_bot_app(send_message_return=None, send_message_exc=None) -> SimpleNamespace:
    """Mimic PTB Application.bot.send_message. Returns a SimpleNamespace
    with a `.message_id` so push_repo can update the pushed_items row."""
    if send_message_exc is not None:
        send = AsyncMock(side_effect=send_message_exc)
    else:
        send = AsyncMock(return_value=send_message_return or SimpleNamespace(message_id=12345))
    return SimpleNamespace(bot=SimpleNamespace(send_message=send))


async def test_push_repo_inserts_row_sends_message_and_updates_tg_id(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    bot_app = _fake_bot_app()

    push_id = await push_repo(
        _repo(),
        bot_app=bot_app,
        chat_id="12345",
        conn=conn,
        push_type="digest",
    )
    assert push_id is not None

    bot_app.bot.send_message.assert_awaited()
    kwargs = bot_app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    # Text contains the repo name + the message rendered by render_repo_message
    assert "acme/widget" in kwargs["text"]
    # Digest push has NO 🔥 prefix
    assert "🔥" not in kwargs["text"]
    assert kwargs["reply_markup"] is not None

    async with conn.execute(
        "SELECT tg_message_id FROM pushed_items WHERE id = ?", (push_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "12345"
    await conn.close()


async def test_push_repo_surge_adds_fire_prefix(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    bot_app = _fake_bot_app()

    await push_repo(
        _repo(),
        bot_app=bot_app,
        chat_id="12345",
        conn=conn,
        push_type="surge",
    )
    kwargs = bot_app.bot.send_message.await_args.kwargs
    assert kwargs["text"].startswith("🔥")
    await conn.close()


async def test_push_repo_returns_none_on_send_failure(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    bot_app = _fake_bot_app(send_message_exc=RuntimeError("telegram down"))

    push_id = await push_repo(
        _repo(),
        bot_app=bot_app,
        chat_id="12345",
        conn=conn,
        push_type="digest",
    )
    assert push_id is None
    # Row WAS inserted (id generated), but tg_message_id remains NULL
    async with conn.execute(
        "SELECT COUNT(*) FROM pushed_items WHERE tg_message_id IS NULL"
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 1
    await conn.close()
