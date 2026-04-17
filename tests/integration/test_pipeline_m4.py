import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.feedback import handle_feedback_callback
from monitor.bot.render import render_repo_message
from monitor.db import (
    connect,
    insert_pushed_item,
    is_blacklisted,
    run_migrations,
)
from monitor.models import RepoCandidate


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m4.db"


def _scored_repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="Widget",
        language="Python",
        stars=500,
        forks=50,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login="acme",
        topics=["agent", "llm"],
        rule_score=7.0,
        llm_score=8.5,
        final_score=7.7,
        summary="Solid widget library",
        recommendation_reason="Matches your agent interest",
    )


async def _seed_repositories_row(conn, repo: RepoCandidate) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO repositories (full_name, owner_login, topics) "
        "VALUES (?, ?, ?)",
        (repo.full_name, repo.owner_login, json.dumps(repo.topics)),
    )
    await conn.commit()


async def test_score_render_push_and_dislike_callback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _scored_repo()
    await _seed_repositories_row(conn, repo)
    push_id = await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="12345"
    )

    # Render produces text with the repo name and a 4-button keyboard
    text, markup = render_repo_message(repo, push_id=push_id)
    assert repo.full_name in text
    assert markup.inline_keyboard  # non-empty

    # Simulate the user pressing 👎
    cq = SimpleNamespace(
        data=f"fb:dislike:{push_id}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        SimpleNamespace(callback_query=cq),
        conn=conn,
        pref_builder=pref_builder,
        refresh_threshold=5,
    )

    # user_feedback row written
    async with conn.execute(
        "SELECT action FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert [tuple(r) for r in rows] == [("dislike",)]

    # blacklist untouched for a plain 👎
    assert await is_blacklisted(conn, kind="author", value="acme") is False
    await conn.close()


async def test_block_author_flow(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _scored_repo()
    await _seed_repositories_row(conn, repo)
    push_id = await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="12345"
    )

    cq = SimpleNamespace(
        data=f"fb:block_author:{push_id}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        SimpleNamespace(callback_query=cq),
        conn=conn,
        pref_builder=pref_builder,
        refresh_threshold=5,
    )

    assert await is_blacklisted(conn, kind="author", value="acme") is True
    async with conn.execute(
        "SELECT action FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert [tuple(r) for r in rows] == [("block_author",)]
    await conn.close()
