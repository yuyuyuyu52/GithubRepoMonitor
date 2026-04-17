import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.config import ConfigFile
from monitor.db import connect, get_latest_run_logs, run_migrations
from monitor.models import RepoCandidate
from monitor.pipeline.digest import run_digest
from monitor.scoring.rules import RuleEngine
from monitor.scoring.types import ScoreResult
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "digest.db"


def _repo(name: str, stars: int = 500) -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="d",
        language="Python",
        stars=stars,
        forks=20,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login=name.split("/")[0],
        topics=["agent"],
    )


class FakeClient:
    def __init__(self) -> None:
        self.search_results: list[RepoCandidate] = []
        self.trending_results: list[RepoCandidate] = []
        self.events_results: dict[str, tuple[float, float]] = {}
        self.contributors_results: dict[str, tuple[int, int]] = {}
        self.issues_results: dict[str, float] = {}
        self.readme_results: dict[str, str] = {}

    async def search_repositories(self, *, keyword, language, min_stars):
        return list(self.search_results)

    async def fetch_trending_repositories(self):
        return list(self.trending_results)

    async def fetch_repo_events(self, full_name: str):
        return self.events_results.get(full_name, (1.0, 0.5))

    async def fetch_contributors_growth(self, full_name: str):
        return self.contributors_results.get(full_name, (5, 1))

    async def fetch_issue_response_hours(self, full_name: str):
        return self.issues_results.get(full_name, 12.0)

    async def fetch_readme(self, full_name: str):
        return self.readme_results.get(full_name, "# title\n## install")


def _fake_bot_app() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=111))
        )
    )


def _score_result() -> ScoreResult:
    return ScoreResult(
        score=8.0, readme_completeness=0.8, summary="s", reason="r",
        matched_interests=[], red_flags=[],
    )


async def test_run_digest_happy_path(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    client = FakeClient()
    client.search_results = [_repo("a/one"), _repo("a/two")]
    client.trending_results = []

    llm = AsyncMock(return_value=_score_result())
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    config = ConfigFile(
        keywords=["llm"], languages=["Python"], min_stars=100,
        top_n=10, digest_cooldown_days=14,
    )
    state = await DaemonState.load(conn=conn, config=config)
    bot_app = _fake_bot_app()

    stats = await run_digest(
        push_type="digest",
        github_client=client,
        llm_score_fn=llm,
        rule_engine=RuleEngine(config, now=now),
        state=state,
        conn=conn,
        bot_app=bot_app,
        chat_id="12345",
        now=now,
    )

    assert stats["repos_scanned"] == 2
    assert stats["repos_pushed"] == 2
    assert bot_app.bot.send_message.await_count == 2

    # A run_log entry was written with status='ok'
    latest = await get_latest_run_logs(conn, limit=1)
    assert latest[0]["kind"] == "digest_digest"
    assert latest[0]["status"] == "ok"
    assert latest[0]["stats"]["repos_pushed"] == 2
    await conn.close()


async def test_run_digest_skips_when_paused(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    await state.set_paused(True)

    client = FakeClient()
    client.search_results = [_repo("a/repo")]
    bot_app = _fake_bot_app()

    stats = await run_digest(
        push_type="digest",
        github_client=client,
        llm_score_fn=AsyncMock(return_value=_score_result()),
        rule_engine=RuleEngine(config, now=now),
        state=state,
        conn=conn,
        bot_app=bot_app,
        chat_id="12345",
        now=now,
    )
    assert stats == {"skipped": "paused"}
    bot_app.bot.send_message.assert_not_awaited()
    await conn.close()


async def test_run_digest_respects_top_n(tmp_db: Path) -> None:
    """5 survivors but top_n=2 → only 2 pushed."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    config = ConfigFile(
        keywords=["llm"], languages=["Python"], min_stars=100, top_n=2,
    )
    state = await DaemonState.load(conn=conn, config=config)

    client = FakeClient()
    client.search_results = [_repo(f"a/r{i}") for i in range(5)]
    bot_app = _fake_bot_app()

    stats = await run_digest(
        push_type="digest",
        github_client=client,
        llm_score_fn=AsyncMock(return_value=_score_result()),
        rule_engine=RuleEngine(config, now=now),
        state=state,
        conn=conn,
        bot_app=bot_app,
        chat_id="12345",
        now=now,
    )
    assert stats["repos_scanned"] == 5
    assert stats["repos_pushed"] == 2
    await conn.close()
