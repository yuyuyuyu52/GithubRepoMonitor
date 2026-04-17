import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    get_cached_llm_score,
    get_preference_profile,
    put_cached_llm_score,
    put_preference_profile,
    run_migrations,
)
from monitor.scoring.types import ScoreResult


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "scoring.db"


def _score(score: float = 8.0) -> ScoreResult:
    return ScoreResult(
        score=score,
        readme_completeness=0.8,
        summary="s",
        reason="r",
        matched_interests=["agent"],
        red_flags=[],
    )


async def test_llm_score_cache_miss_returns_none(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    got = await get_cached_llm_score(conn, "a/b", readme_sha256="deadbeef")
    assert got is None
    await conn.close()


async def test_llm_score_cache_put_then_get(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_cached_llm_score(conn, "a/b", readme_sha256="abc", result=_score(8.5), now=now)

    got = await get_cached_llm_score(conn, "a/b", readme_sha256="abc")
    assert got is not None
    assert got.score == 8.5
    assert got.matched_interests == ["agent"]
    await conn.close()


async def test_llm_score_cache_different_hash_is_independent(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_cached_llm_score(conn, "a/b", readme_sha256="v1", result=_score(5.0), now=now)
    await put_cached_llm_score(conn, "a/b", readme_sha256="v2", result=_score(9.0), now=now)

    got_v1 = await get_cached_llm_score(conn, "a/b", readme_sha256="v1")
    got_v2 = await get_cached_llm_score(conn, "a/b", readme_sha256="v2")
    assert got_v1 is not None and got_v1.score == 5.0
    assert got_v2 is not None and got_v2.score == 9.0
    await conn.close()


async def test_llm_score_cache_put_overwrites_same_key(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_cached_llm_score(conn, "a/b", readme_sha256="abc", result=_score(5.0), now=now)
    await put_cached_llm_score(conn, "a/b", readme_sha256="abc", result=_score(8.0), now=now)

    got = await get_cached_llm_score(conn, "a/b", readme_sha256="abc")
    assert got is not None and got.score == 8.0
    await conn.close()


async def test_preference_profile_empty_returns_none(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    got = await get_preference_profile(conn)
    assert got is None
    await conn.close()


async def test_preference_profile_put_then_get(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_preference_profile(
        conn,
        profile_text="用户喜欢 AI agent 框架",
        generated_at=now,
        based_on_feedback_count=10,
    )

    got = await get_preference_profile(conn)
    assert got is not None
    assert got["profile_text"] == "用户喜欢 AI agent 框架"
    assert got["based_on_feedback_count"] == 10
    await conn.close()


async def test_preference_profile_upsert_replaces_previous(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_preference_profile(conn, profile_text="v1", generated_at=now, based_on_feedback_count=5)
    await put_preference_profile(conn, profile_text="v2", generated_at=now, based_on_feedback_count=10)

    got = await get_preference_profile(conn)
    assert got is not None
    assert got["profile_text"] == "v2"
    assert got["based_on_feedback_count"] == 10
    await conn.close()
