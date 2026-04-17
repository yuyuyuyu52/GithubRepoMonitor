import datetime as dt
import json
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    finish_run_log,
    run_migrations,
    start_run_log,
    upsert_repositories,
    upsert_repository_metrics,
)
from monitor.models import RepoCandidate


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m5.db"


def _repo(name: str = "acme/widget") -> RepoCandidate:
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
        topics=["agent", "llm"],
        star_velocity_day=5.0,
        star_velocity_week=1.2,
        fork_star_ratio=0.05,
        avg_issue_response_hours=12.0,
        contributor_count=8,
        contributor_growth_week=2,
        readme_completeness=0.75,
    )


async def test_start_run_log_returns_id_and_persists(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)

    run_id = await start_run_log(conn, kind="digest_morning", now=now)
    assert isinstance(run_id, int) and run_id > 0

    async with conn.execute(
        "SELECT kind, started_at, ended_at, status FROM run_log WHERE id = ?",
        (run_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "digest_morning"
    assert row[1] == now.isoformat()
    assert row[2] is None  # ended_at not set yet
    assert row[3] is None  # status not set yet
    await conn.close()


async def test_finish_run_log_writes_ended_and_stats(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    start = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)
    run_id = await start_run_log(conn, kind="digest_morning", now=start)

    end = start + dt.timedelta(seconds=45)
    stats = {"repos_scanned": 30, "repos_pushed": 5, "llm_calls": 5, "errors": []}
    await finish_run_log(conn, run_id=run_id, status="ok", stats=stats, now=end)

    async with conn.execute(
        "SELECT ended_at, status, stats FROM run_log WHERE id = ?", (run_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == end.isoformat()
    assert row[1] == "ok"
    assert json.loads(row[2]) == stats
    await conn.close()


async def test_upsert_repositories_inserts_then_updates(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)

    repo_v1 = _repo()
    repo_v1.description = "v1 description"
    await upsert_repositories(conn, [repo_v1], now=now)

    async with conn.execute(
        "SELECT description, language, topics, owner_login, first_seen_at FROM repositories WHERE full_name = ?",
        (repo_v1.full_name,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "v1 description"
    assert row[1] == "Python"
    assert json.loads(row[2]) == ["agent", "llm"]
    assert row[3] == "acme"
    first_seen = row[4]
    assert first_seen == now.isoformat()

    # Update with new description. first_seen must NOT change; last_enriched_at updates.
    repo_v2 = _repo()
    repo_v2.description = "v2 description"
    later = now + dt.timedelta(hours=1)
    await upsert_repositories(conn, [repo_v2], now=later)

    async with conn.execute(
        "SELECT description, first_seen_at, last_enriched_at FROM repositories WHERE full_name = ?",
        (repo_v2.full_name,),
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "v2 description"
    assert row[1] == first_seen  # unchanged
    assert row[2] == later.isoformat()
    await conn.close()


async def test_upsert_repository_metrics_appends_rows(tmp_db: Path) -> None:
    """Each enrich pass appends a new row for time-series analytics."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    repo = _repo()

    t0 = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)
    await upsert_repository_metrics(conn, repo, now=t0)
    await upsert_repository_metrics(conn, repo, now=t0 + dt.timedelta(hours=12))

    async with conn.execute(
        "SELECT collected_at, star_velocity_day FROM repository_metrics "
        "WHERE full_name = ? ORDER BY collected_at",
        (repo.full_name,),
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == t0.isoformat()
    assert rows[0][1] == 5.0
    assert rows[1][0] == (t0 + dt.timedelta(hours=12)).isoformat()
    await conn.close()
