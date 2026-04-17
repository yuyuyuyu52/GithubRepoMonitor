import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    count_feedback_since_last_profile,
    get_daemon_state,
    insert_pushed_item,
    record_user_feedback,
    run_migrations,
    set_daemon_paused,
    update_pushed_tg_message_id,
)
from monitor.models import RepoCandidate


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
    assert [tuple(r) for r in rows] == [("badactor",)]
    await conn.close()


def _sample_repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
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
        topics=["agent", "llm"],
        rule_score=7.5,
        llm_score=8.2,
        final_score=7.85,
        summary="Widget library",
        recommendation_reason="Matches interests",
    )


async def test_insert_pushed_item_returns_id_and_persists(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)

    push_id = await insert_pushed_item(
        conn,
        repo=_sample_repo(),
        push_type="digest",
        tg_chat_id="12345",
        now=now,
    )
    assert isinstance(push_id, int)
    assert push_id > 0

    async with conn.execute(
        "SELECT full_name, push_type, rule_score, llm_score, final_score, "
        "summary, reason, tg_chat_id, tg_message_id "
        "FROM pushed_items WHERE id = ?",
        (push_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acme/widget"
    assert row[1] == "digest"
    assert row[2] == 7.5
    assert row[3] == 8.2
    assert row[4] == 7.85
    assert row[5] == "Widget library"
    assert row[6] == "Matches interests"
    assert row[7] == "12345"
    assert row[8] is None  # tg_message_id filled in later
    await conn.close()


async def test_update_pushed_tg_message_id(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    push_id = await insert_pushed_item(
        conn, repo=_sample_repo(), push_type="digest", tg_chat_id="1"
    )

    await update_pushed_tg_message_id(conn, push_id=push_id, tg_message_id="msg-999")

    async with conn.execute(
        "SELECT tg_message_id FROM pushed_items WHERE id = ?", (push_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "msg-999"
    await conn.close()


async def test_record_user_feedback_inserts_row(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    push_id = await insert_pushed_item(
        conn, repo=_sample_repo(), push_type="digest", tg_chat_id="1"
    )
    now = dt.datetime(2026, 4, 18, 13, 0, tzinfo=dt.timezone.utc)

    snapshot = {"full_name": "acme/widget", "owner_login": "acme", "topics": ["agent"]}
    await record_user_feedback(
        conn,
        push_id=push_id,
        action="like",
        repo_snapshot=snapshot,
        now=now,
    )

    async with conn.execute(
        "SELECT push_id, action, created_at, repo_snapshot FROM user_feedback"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == push_id
    assert rows[0][1] == "like"
    assert rows[0][2] == now.isoformat()
    import json
    assert json.loads(rows[0][3])["full_name"] == "acme/widget"
    await conn.close()


async def test_count_feedback_since_last_profile(tmp_db: Path) -> None:
    """Count of user_feedback rows added since the preference_profile was
    last generated. Drives PreferenceBuilder regen trigger."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    push_id = await insert_pushed_item(
        conn, repo=_sample_repo(), push_type="digest", tg_chat_id="1"
    )

    # No profile yet, no feedback → count 0
    assert await count_feedback_since_last_profile(conn) == 0

    # Add 2 feedback rows before any profile exists → count 2
    t0 = dt.datetime(2026, 4, 18, 10, 0, tzinfo=dt.timezone.utc)
    await record_user_feedback(
        conn, push_id=push_id, action="like", repo_snapshot={}, now=t0
    )
    await record_user_feedback(
        conn,
        push_id=push_id,
        action="dislike",
        repo_snapshot={},
        now=t0 + dt.timedelta(minutes=1),
    )
    assert await count_feedback_since_last_profile(conn) == 2

    # Write a preference_profile with generated_at between the 2 rows
    from monitor.db import put_preference_profile
    await put_preference_profile(
        conn,
        profile_text="p",
        generated_at=t0 + dt.timedelta(seconds=30),
        based_on_feedback_count=1,
    )
    # Now only 1 feedback row is "since" the profile
    assert await count_feedback_since_last_profile(conn) == 1
    await conn.close()
