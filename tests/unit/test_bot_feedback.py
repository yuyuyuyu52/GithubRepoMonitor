import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.feedback import handle_feedback_callback
from monitor.db import (
    connect,
    insert_pushed_item,
    is_blacklisted,
    run_migrations,
)
from monitor.models import RepoCandidate
from monitor.scoring.preference import PreferenceBuilder, RegenerationResult


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "fb.db"


def _repo(name: str = "acme/widget", topics: list[str] | None = None) -> RepoCandidate:
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
        topics=topics if topics is not None else ["agent"],
        rule_score=5.0,
        llm_score=6.0,
        final_score=5.5,
        summary="s",
        recommendation_reason="r",
    )


def _fake_callback_query(data: str) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking PTB's CallbackQuery."""
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def _fake_update(callback_query) -> SimpleNamespace:
    return SimpleNamespace(callback_query=callback_query)


async def _insert_sample(conn, repo: RepoCandidate) -> int:
    # Also seed the `repositories` row so _load_repo_snapshot can retrieve
    # topics. The M5 scheduler will populate this via the collect stage;
    # tests do it directly.
    await conn.execute(
        "INSERT OR REPLACE INTO repositories (full_name, owner_login, topics) "
        "VALUES (?, ?, ?)",
        (repo.full_name, repo.owner_login, json.dumps(repo.topics or [])),
    )
    await conn.commit()
    return await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="12345"
    )


async def test_like_callback_writes_feedback_and_acks(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo()
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:like:{push_id}")
    update = _fake_update(cq)

    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update,
        conn=conn,
        pref_builder=pref_builder,
        refresh_threshold=5,
    )

    cq.answer.assert_awaited()
    cq.edit_message_text.assert_awaited()  # message is updated with ack

    async with conn.execute(
        "SELECT action, push_id FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("like", push_id)]
    # Blacklist untouched for a like
    assert await is_blacklisted(conn, kind="author", value="acme") is False
    pref_builder.regenerate.assert_not_awaited()
    await conn.close()


async def test_block_author_callback_adds_to_blacklist(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo(name="spammy/repo")
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:block_author:{push_id}")
    update = _fake_update(cq)
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update, conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    assert await is_blacklisted(conn, kind="author", value="spammy") is True
    # user_feedback row recorded alongside the blacklist entry
    async with conn.execute(
        "SELECT action FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("block_author",)]
    await conn.close()


async def test_block_topic_callback_adds_first_topic_to_blacklist(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo(topics=["agent", "llm"])
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:block_topic:{push_id}")
    update = _fake_update(cq)
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update, conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    # First topic picked
    assert await is_blacklisted(conn, kind="topic", value="agent") is True
    # Second topic NOT auto-blacklisted
    assert await is_blacklisted(conn, kind="topic", value="llm") is False
    await conn.close()


async def test_block_topic_with_no_topics_does_not_crash(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo(topics=[])
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:block_topic:{push_id}")
    update = _fake_update(cq)
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update, conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    # No topic entry was added
    async with conn.execute(
        "SELECT COUNT(*) FROM blacklist WHERE kind = 'topic'"
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 0
    # Feedback row is still recorded
    async with conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 1
    await conn.close()


async def test_feedback_threshold_triggers_preference_regen(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo()
    push_id = await _insert_sample(conn, repo)
    pref_builder = SimpleNamespace(
        regenerate=AsyncMock(
            return_value=RegenerationResult(
                profile_text="new", generated_at=dt.datetime.now(dt.timezone.utc),
                based_on_feedback_count=3,
            )
        )
    )

    # refresh_threshold=3: first 2 likes don't trigger; 3rd does.
    for i in range(3):
        cq = _fake_callback_query(f"fb:like:{push_id}")
        await handle_feedback_callback(
            _fake_update(cq), conn=conn, pref_builder=pref_builder, refresh_threshold=3
        )

    assert pref_builder.regenerate.await_count == 1
    await conn.close()


async def test_invalid_callback_data_is_noop(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    cq = _fake_callback_query("garbage")
    pref_builder = SimpleNamespace(regenerate=AsyncMock())

    await handle_feedback_callback(
        _fake_update(cq), conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    cq.answer.assert_awaited()  # always ack so TG button isn't stuck spinning
    cq.edit_message_text.assert_not_awaited()  # no message to edit
    async with conn.execute("SELECT COUNT(*) FROM user_feedback") as cur:
        count = (await cur.fetchone())[0]
    assert count == 0
    await conn.close()
