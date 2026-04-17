import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from monitor.config import ConfigFile
from monitor.db import connect, get_cached_llm_score, run_migrations
from monitor.models import RepoCandidate
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.scoring.types import LLMScoreError, ScoreResult


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "score.db"


def _repo(readme: str = "# r\n## install\n") -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)
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
        readme_text=readme,
        star_velocity_day=3.0,
        star_velocity_week=0.5,
    )


def _llm_result(score: float = 8.0) -> ScoreResult:
    return ScoreResult(
        score=score,
        readme_completeness=0.9,
        summary="nice",
        reason="matches",
        matched_interests=["agent"],
        red_flags=[],
    )


async def test_score_repo_cache_miss_calls_llm_and_writes_cache(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    llm = AsyncMock(return_value=_llm_result(8.0))
    config = ConfigFile(keywords=["agent"], weights={"rule": 0.5, "llm": 0.5})
    repo = _repo()

    await score_repo(
        repo,
        config=config,
        rule_engine=RuleEngine(config),
        llm_score_fn=llm,
        conn=conn,
    )

    # LLM was called exactly once
    assert llm.await_count == 1
    # Cache now has the entry
    from hashlib import sha256
    h = sha256(repo.readme_text.encode("utf-8")).hexdigest()
    cached = await get_cached_llm_score(conn, repo.full_name, readme_sha256=h)
    assert cached is not None
    assert cached.score == 8.0

    assert repo.llm_score == 8.0
    assert repo.rule_score > 0.0
    # final = rule*0.5 + llm*0.5
    assert abs(repo.final_score - (repo.rule_score * 0.5 + 8.0 * 0.5)) < 0.01
    assert repo.summary == "nice"
    assert repo.recommendation_reason == "matches"
    await conn.close()


async def test_score_repo_cache_hit_skips_llm(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    llm = AsyncMock(return_value=_llm_result(8.0))
    config = ConfigFile(keywords=["agent"])
    repo = _repo()

    # First call populates cache
    await score_repo(repo, config=config, rule_engine=RuleEngine(config), llm_score_fn=llm, conn=conn)
    # Second call on an identical-readme repo should hit cache
    repo2 = _repo()
    repo2.rule_score = 0.0  # reset to ensure it gets recomputed
    await score_repo(repo2, config=config, rule_engine=RuleEngine(config), llm_score_fn=llm, conn=conn)

    assert llm.await_count == 1  # only first call hit the LLM
    assert repo2.llm_score == 8.0
    await conn.close()


async def test_score_repo_falls_back_to_heuristic_on_llm_error(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    async def failing_llm(*args, **kwargs):
        raise LLMScoreError("simulated", cause="sdk_error")

    config = ConfigFile(keywords=["agent"])
    repo = _repo(readme="# r\n## install\n## usage\n## license\nbuild an agent")

    await score_repo(repo, config=config, rule_engine=RuleEngine(config), llm_score_fn=failing_llm, conn=conn)

    assert repo.llm_score > 0.0  # heuristic produced a value
    assert repo.summary  # heuristic populated summary
    # Cache got populated with the heuristic's result so later runs don't retry LLM
    from hashlib import sha256
    h = sha256(repo.readme_text.encode("utf-8")).hexdigest()
    cached = await get_cached_llm_score(conn, repo.full_name, readme_sha256=h)
    assert cached is not None
    await conn.close()


async def test_score_repo_final_is_weighted_combination(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    llm = AsyncMock(return_value=_llm_result(9.0))
    config = ConfigFile(weights={"rule": 0.3, "llm": 0.7})
    repo = _repo()

    await score_repo(repo, config=config, rule_engine=RuleEngine(config), llm_score_fn=llm, conn=conn)

    expected = round(repo.rule_score * 0.3 + 9.0 * 0.7, 2)
    assert repo.final_score == expected
    await conn.close()


async def test_score_repo_llm_fn_gets_preference_profile_when_present(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    # Pre-populate preference profile
    from monitor.db import put_preference_profile
    await put_preference_profile(
        conn,
        profile_text="用户喜欢 Rust",
        generated_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc),
        based_on_feedback_count=10,
    )

    calls: list[str | None] = []

    async def capturing_llm(repo, *, interest_tags, preference_profile):
        calls.append(preference_profile)
        return _llm_result()

    config = ConfigFile(keywords=["agent"])
    await score_repo(_repo(), config=config, rule_engine=RuleEngine(config), llm_score_fn=capturing_llm, conn=conn)

    assert calls == ["用户喜欢 Rust"]
    await conn.close()
