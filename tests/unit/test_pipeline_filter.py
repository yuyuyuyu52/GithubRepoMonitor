import datetime as dt
from pathlib import Path

import pytest

from monitor.config import ConfigFile
from monitor.db import (
    add_blacklist_entry,
    connect,
    insert_pushed_item,
    run_migrations,
)
from monitor.models import RepoCandidate
from monitor.pipeline.filter import apply_filters
from monitor.scoring.rules import RuleEngine


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "filter.db"


def _repo(
    name: str = "a/b",
    language: str = "Python",
    stars: int = 500,
    topics: list[str] | None = None,
) -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language=language,
        stars=stars,
        forks=10,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login=name.split("/")[0],
        topics=topics if topics is not None else [],
    )


async def test_apply_filters_drops_rules_violators(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    config = ConfigFile(min_stars=300, languages=["Python"])
    engine = RuleEngine(config, now=now)

    repos = [
        _repo("a/ok", stars=500),
        _repo("a/few_stars", stars=100),
        _repo("a/wrong_lang", stars=1000, language="Haskell"),
    ]
    survivors = await apply_filters(
        repos, rule_engine=engine, conn=conn,
        digest_cooldown_days=14, now=now,
    )
    assert [r.full_name for r in survivors] == ["a/ok"]
    await conn.close()


async def test_apply_filters_drops_blacklisted_author(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    await add_blacklist_entry(conn, kind="author", value="spammy", source="manual")
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    engine = RuleEngine(ConfigFile(min_stars=100, languages=["Python"]), now=now)

    repos = [_repo("ok/repo"), _repo("spammy/repo")]
    survivors = await apply_filters(
        repos, rule_engine=engine, conn=conn,
        digest_cooldown_days=14, now=now,
    )
    assert [r.full_name for r in survivors] == ["ok/repo"]
    await conn.close()


async def test_apply_filters_drops_blacklisted_repo(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    await add_blacklist_entry(conn, kind="repo", value="a/nope", source="manual")
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    engine = RuleEngine(ConfigFile(min_stars=100, languages=["Python"]), now=now)

    repos = [_repo("a/nope"), _repo("a/yes")]
    survivors = await apply_filters(
        repos, rule_engine=engine, conn=conn,
        digest_cooldown_days=14, now=now,
    )
    assert [r.full_name for r in survivors] == ["a/yes"]
    await conn.close()


async def test_apply_filters_drops_repo_with_blacklisted_topic(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    await add_blacklist_entry(conn, kind="topic", value="awesome-list", source="manual")
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    engine = RuleEngine(ConfigFile(min_stars=100, languages=["Python"]), now=now)

    repos = [
        _repo("a/keep", topics=["rust", "cli"]),
        _repo("a/drop", topics=["agent", "awesome-list"]),  # any match → drop
    ]
    survivors = await apply_filters(
        repos, rule_engine=engine, conn=conn,
        digest_cooldown_days=14, now=now,
    )
    assert [r.full_name for r in survivors] == ["a/keep"]
    await conn.close()


async def test_apply_filters_drops_repo_in_active_cooldown(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    engine = RuleEngine(ConfigFile(min_stars=100, languages=["Python"]), now=now)

    # 5 days ago — inside the 14-day cooldown
    repo = _repo("a/recent")
    await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="1",
        now=now - dt.timedelta(days=5),
    )

    survivors = await apply_filters(
        [repo, _repo("a/new")], rule_engine=engine, conn=conn,
        digest_cooldown_days=14, now=now,
    )
    assert [r.full_name for r in survivors] == ["a/new"]
    await conn.close()


async def test_apply_filters_accepts_expired_cooldown(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    engine = RuleEngine(ConfigFile(min_stars=100, languages=["Python"]), now=now)

    # 20 days ago — outside the 14-day cooldown; should re-surface
    repo = _repo("a/stale")
    await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="1",
        now=now - dt.timedelta(days=20),
    )

    survivors = await apply_filters(
        [repo], rule_engine=engine, conn=conn,
        digest_cooldown_days=14, now=now,
    )
    assert [r.full_name for r in survivors] == ["a/stale"]
    await conn.close()
