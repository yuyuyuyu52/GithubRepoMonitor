import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from monitor.db import connect, get_preference_profile, run_migrations
from monitor.scoring.preference import PreferenceBuilder


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "pref.db"


async def _seed_feedback(conn, rows: list[tuple[str, str, dict]]) -> None:
    """rows = [(action, created_at_iso, snapshot_dict), ...]"""
    # Need a pushed_item to FK to.
    await conn.execute(
        "INSERT INTO pushed_items "
        "(full_name, pushed_at, push_type, rule_score, llm_score, final_score) "
        "VALUES ('x/y', '2026-01-01T00:00:00+00:00', 'digest', 0, 0, 0)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM pushed_items LIMIT 1") as cur:
        push_id = (await cur.fetchone())[0]
    for action, created_at, snapshot in rows:
        await conn.execute(
            "INSERT INTO user_feedback (push_id, action, created_at, repo_snapshot) "
            "VALUES (?, ?, ?, ?)",
            (push_id, action, created_at, json.dumps(snapshot)),
        )
    await conn.commit()


async def test_regenerate_writes_profile_from_feedback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    await _seed_feedback(conn, [
        ("like", "2026-04-10T00:00:00+00:00", {"full_name": "a/rust-cli", "topics": ["rust", "cli"]}),
        ("like", "2026-04-11T00:00:00+00:00", {"full_name": "b/rust-agent", "topics": ["rust", "agent"]}),
        ("dislike", "2026-04-12T00:00:00+00:00", {"full_name": "c/awesome-list", "topics": ["awesome"]}),
    ])

    fake_llm = AsyncMock(return_value="用户偏好 Rust 系统工具 + agent 框架，对 awesome-list 类型反感。")
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)

    builder = PreferenceBuilder(
        conn=conn,
        llm_generate_profile=fake_llm,
        now=now,
    )
    result = await builder.regenerate()

    assert result is not None
    assert "Rust" in result.profile_text
    assert result.based_on_feedback_count == 3
    assert result.generated_at == now

    stored = await get_preference_profile(conn)
    assert stored is not None
    assert stored["profile_text"] == result.profile_text
    await conn.close()


async def test_regenerate_returns_none_when_no_feedback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    fake_llm = AsyncMock(return_value="never called")
    builder = PreferenceBuilder(conn=conn, llm_generate_profile=fake_llm)

    result = await builder.regenerate()
    assert result is None
    fake_llm.assert_not_awaited()
    await conn.close()


async def test_regenerate_sends_prompt_with_recent_feedback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    await _seed_feedback(conn, [
        ("like", "2026-04-10T00:00:00+00:00", {"full_name": "liked/repo", "topics": ["ai"]}),
        ("dislike", "2026-04-11T00:00:00+00:00", {"full_name": "hated/repo", "topics": ["awesome"]}),
    ])

    captured_prompt: list[str] = []

    async def fake_llm(prompt: str) -> str:
        captured_prompt.append(prompt)
        return "profile text"

    builder = PreferenceBuilder(conn=conn, llm_generate_profile=fake_llm)
    await builder.regenerate()

    prompt = captured_prompt[0]
    assert "liked/repo" in prompt
    assert "hated/repo" in prompt
    await conn.close()


async def test_regenerate_limits_to_recent_N_per_action(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    # 25 likes, 25 dislikes — builder should cap each at 20
    many = [
        ("like", f"2026-04-{i:02d}T00:00:00+00:00", {"full_name": f"like/repo-{i}"})
        for i in range(1, 26)
    ] + [
        ("dislike", f"2026-03-{i:02d}T00:00:00+00:00", {"full_name": f"dislike/repo-{i}"})
        for i in range(1, 26)
    ]
    await _seed_feedback(conn, many)

    seen_repos: set[str] = set()

    async def fake_llm(prompt: str) -> str:
        # Extract all full_names from the prompt
        for line in prompt.splitlines():
            if "/" in line:
                for word in line.split():
                    if "/" in word and word.strip(",.()[]{}").count("/") == 1:
                        seen_repos.add(word.strip(",.()[]{}"))
        return "p"

    builder = PreferenceBuilder(
        conn=conn, llm_generate_profile=fake_llm, max_per_action=20
    )
    await builder.regenerate()

    # Older entries beyond the top-20 most-recent per action should be absent.
    # The 5 oldest likes (repo-1..5) and 5 oldest dislikes should NOT appear.
    assert "like/repo-1" not in seen_repos
    assert "dislike/repo-1" not in seen_repos
    await conn.close()


async def test_regenerate_uses_current_time_not_construction_time(tmp_db: Path, monkeypatch) -> None:
    """Without a `now=` override, regenerate() must stamp the profile with
    the current time — otherwise long-lived daemons would freeze
    generated_at at construction and break count_feedback_since_last_profile."""
    import datetime as dt

    conn = await connect(tmp_db)
    await run_migrations(conn)

    # Seed some feedback
    await _seed_feedback(conn, [
        ("like", "2026-04-10T00:00:00+00:00", {"full_name": "a/b"}),
    ])

    fake_llm = AsyncMock(return_value="profile")
    # Construct WITHOUT `now=` so the builder computes now at call time
    builder = PreferenceBuilder(conn=conn, llm_generate_profile=fake_llm)

    fixed = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    class _FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("monitor.scoring.preference.dt.datetime", _FakeDT)

    result = await builder.regenerate()
    assert result is not None
    assert result.generated_at == fixed
    await conn.close()
