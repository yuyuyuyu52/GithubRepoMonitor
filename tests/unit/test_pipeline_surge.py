import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.config import ConfigFile
from monitor.db import (
    connect,
    run_migrations,
    upsert_repositories,
    upsert_repository_metrics,
)
from monitor.models import RepoCandidate
from monitor.pipeline.surge import run_surge
from monitor.scoring.rules import RuleEngine
from monitor.scoring.types import ScoreResult
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "surge.db"


def _repo(name: str = "acme/widget", star_velocity_day: float = 2.0) -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="widgets",
        language="Python",
        stars=420,
        forks=21,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login=name.split("/")[0],
        topics=["agent"],
        star_velocity_day=star_velocity_day,
        star_velocity_week=1.0,
        contributor_count=5,
    )


class FakeClient:
    def __init__(self) -> None:
        self.events_results: dict[str, tuple[float, float]] = {}
        self.contributors_results: dict[str, tuple[int, int]] = {}
        self.issues_results: dict[str, float] = {}
        self.readme_results: dict[str, str] = {}
        self.detail_results: dict[str, RepoCandidate] = {}

    async def fetch_repo_events(self, full_name: str):
        return self.events_results.get(full_name, (0.0, 0.0))

    async def fetch_contributors_growth(self, full_name: str):
        return self.contributors_results.get(full_name, (5, 1))

    async def fetch_issue_response_hours(self, full_name: str):
        return self.issues_results.get(full_name, 12.0)

    async def fetch_readme(self, full_name: str):
        return self.readme_results.get(full_name, "# title\n## install")

    async def fetch_repository_detail(self, full_name: str):
        return self.detail_results.get(full_name)


def _fake_bot_app() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=222))
        )
    )


def _result() -> ScoreResult:
    return ScoreResult(
        score=7.0, readme_completeness=0.5, summary="s", reason="r",
        matched_interests=[], red_flags=[],
    )


async def test_run_surge_triggers_when_velocity_multiplies(tmp_db: Path) -> None:
    """Thresholds (ConfigFile defaults): multiple=3, absolute=20. New=25 vs old=5 → 5x ratio, 25≥20. Triggers."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    repo = _repo(star_velocity_day=5.0)
    await upsert_repositories(conn, [repo], now=now - dt.timedelta(hours=12))
    await upsert_repository_metrics(conn, repo, now=now - dt.timedelta(hours=12))

    client = FakeClient()
    client.events_results = {repo.full_name: (25.0, 10.0)}  # surge: new=25, old=5 → 5x, >20
    client.detail_results = {repo.full_name: repo}

    config = ConfigFile()  # surge defaults: multiple=3.0, absolute=20.0, cooldown=3
    state = await DaemonState.load(conn=conn, config=config)
    bot_app = _fake_bot_app()

    stats = await run_surge(
        github_client=client,
        llm_score_fn=AsyncMock(return_value=_result()),
        rule_engine=RuleEngine(config, now=now),
        state=state,
        conn=conn,
        bot_app=bot_app,
        chat_id="12345",
        now=now,
    )
    assert stats["candidates"] == 1
    assert stats["surged"] == 1
    assert bot_app.bot.send_message.await_count == 1

    # Push message has 🔥 prefix
    kwargs = bot_app.bot.send_message.await_args.kwargs
    assert kwargs["text"].startswith("🔥")
    await conn.close()


async def test_run_surge_skips_when_multiplier_not_met(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    repo = _repo(star_velocity_day=5.0)
    await upsert_repositories(conn, [repo], now=now - dt.timedelta(hours=12))
    await upsert_repository_metrics(conn, repo, now=now - dt.timedelta(hours=12))

    client = FakeClient()
    # new=8, old=5 → 1.6x (below 3x multiple)
    client.events_results = {repo.full_name: (8.0, 2.0)}

    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    bot_app = _fake_bot_app()

    stats = await run_surge(
        github_client=client,
        llm_score_fn=AsyncMock(return_value=_result()),
        rule_engine=RuleEngine(config, now=now),
        state=state,
        conn=conn,
        bot_app=bot_app,
        chat_id="12345",
        now=now,
    )
    assert stats["candidates"] == 1
    assert stats["surged"] == 0
    bot_app.bot.send_message.assert_not_awaited()
    await conn.close()


async def test_run_surge_skips_when_absolute_not_met(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    repo = _repo(star_velocity_day=2.0)
    await upsert_repositories(conn, [repo], now=now - dt.timedelta(hours=12))
    await upsert_repository_metrics(conn, repo, now=now - dt.timedelta(hours=12))

    client = FakeClient()
    # new=10, old=2 → 5x (above multiplier) BUT 10 < 20 absolute
    client.events_results = {repo.full_name: (10.0, 2.0)}

    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    bot_app = _fake_bot_app()

    stats = await run_surge(
        github_client=client,
        llm_score_fn=AsyncMock(return_value=_result()),
        rule_engine=RuleEngine(config, now=now),
        state=state,
        conn=conn,
        bot_app=bot_app,
        chat_id="12345",
        now=now,
    )
    assert stats["surged"] == 0
    await conn.close()


async def test_run_surge_skipped_when_paused(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    await state.set_paused(True)

    client = FakeClient()
    stats = await run_surge(
        github_client=client,
        llm_score_fn=AsyncMock(return_value=_result()),
        rule_engine=RuleEngine(ConfigFile(), now=dt.datetime.now(dt.timezone.utc)),
        state=state,
        conn=conn,
        bot_app=_fake_bot_app(),
        chat_id="1",
    )
    assert stats == {"skipped": "paused"}
    await conn.close()
