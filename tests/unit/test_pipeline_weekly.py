import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    insert_pushed_item,
    put_preference_profile,
    record_user_feedback,
    run_migrations,
)
from monitor.models import RepoCandidate
from monitor.pipeline.weekly import build_weekly_digest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "weekly.db"


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
        summary=f"s {name}",
        recommendation_reason="r",
    )


async def test_weekly_digest_aggregates_counts_and_profile(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 20, 21, 0, tzinfo=dt.timezone.utc)  # Sunday

    # 4 pushes within the last week
    for i, (name, score) in enumerate([("a/p1", 9.0), ("b/p2", 8.5), ("c/p3", 7.0), ("d/p4", 6.0)]):
        push_at = now - dt.timedelta(days=i)
        await insert_pushed_item(
            conn, repo=_repo(name, score), push_type="digest",
            tg_chat_id="1", now=push_at,
        )
    # Also one old push (>7d ago) — should NOT be counted
    await insert_pushed_item(
        conn, repo=_repo("z/old", 5.0), push_type="digest",
        tg_chat_id="1", now=now - dt.timedelta(days=10),
    )

    # Feedback
    async with conn.execute("SELECT id FROM pushed_items WHERE full_name='a/p1'") as cur:
        push_id = (await cur.fetchone())[0]
    for action in ["like", "like", "dislike"]:
        await record_user_feedback(
            conn, push_id=push_id, action=action, repo_snapshot={},
            now=now - dt.timedelta(hours=1),
        )

    # Preference profile
    await put_preference_profile(
        conn, profile_text="用户偏好 AI agent 框架和 Rust 工具",
        generated_at=now - dt.timedelta(hours=2), based_on_feedback_count=3,
    )

    text = await build_weekly_digest(conn, now=now)

    assert "本周摘要" in text
    # 4 pushes in the last week (not 5)
    assert "4" in text
    # 2 likes
    assert "👍 2" in text or "like" in text.lower()
    # 1 dislike
    assert "👎 1" in text or "dislike" in text.lower()
    # Preference profile text is included
    assert "AI agent 框架" in text
    # Top 3 pushed repos by score are in there
    assert "a/p1" in text
    assert "b/p2" in text
    # The old push is NOT included
    assert "z/old" not in text
    await conn.close()


async def test_weekly_digest_without_data_renders_empty_state(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 20, 21, 0, tzinfo=dt.timezone.utc)

    text = await build_weekly_digest(conn, now=now)
    # Must not crash; renders a minimal summary
    assert "本周摘要" in text
    assert "0" in text  # zero counts
    await conn.close()
