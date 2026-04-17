import datetime as dt
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from monitor.db import connect, run_migrations, current_version, SCHEMA_VERSION, add_blacklist_entry, is_blacklisted, pushed_cooldown_state


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    return tmp_path / "test.db"


async def test_fresh_db_runs_all_migrations(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    applied = await run_migrations(conn)
    assert applied == SCHEMA_VERSION
    assert await current_version(conn) == SCHEMA_VERSION
    await conn.close()


async def test_migration_runner_is_idempotent(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    applied_second = await run_migrations(conn)
    assert applied_second == 0
    await conn.close()


async def test_all_expected_tables_exist_after_migration(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        tables = {row[0] for row in await cur.fetchall()}
    expected = {
        "repositories", "repository_metrics", "pushed_items",
        "user_feedback", "blacklist", "preference_profile",
        "llm_score_cache", "run_log", "schema_version",
    }
    assert expected.issubset(tables)
    await conn.close()


async def test_wal_mode_enabled(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    async with conn.execute("PRAGMA journal_mode") as cur:
        mode = (await cur.fetchone())[0]
    assert mode.lower() == "wal"
    await conn.close()


async def test_migration_001_copies_legacy_seen_repositories(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    # Simulate the demo's schema with one row
    raw = sqlite3.connect(db_path)
    raw.executescript("""
        CREATE TABLE seen_repositories (
            full_name TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_score REAL NOT NULL
        );
        CREATE TABLE repository_metrics (
            full_name TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            star_velocity_day REAL,
            star_velocity_week REAL,
            fork_star_ratio REAL,
            avg_issue_response_hours REAL,
            contributor_count INTEGER,
            contributor_growth_week INTEGER,
            readme_completeness REAL,
            PRIMARY KEY (full_name, collected_at)
        );
        INSERT INTO seen_repositories VALUES ('a/b', '2026-04-01T00:00:00+00:00', 7.5);
    """)
    raw.commit()
    raw.close()

    conn = await connect(db_path)
    await run_migrations(conn)
    async with conn.execute(
        "SELECT full_name, push_type, final_score FROM pushed_items WHERE full_name='a/b'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "a/b"
    assert row[1] == "digest"
    assert row[2] == 7.5

    # New columns added on legacy repository_metrics
    async with conn.execute("PRAGMA table_info(repository_metrics)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert "stars" in cols
    assert "forks" in cols
    await conn.close()


async def test_migration_001_is_idempotent_after_partial_crash(tmp_path: Path) -> None:
    """A crashed-mid-migration DB must not produce duplicate pushed_items rows."""
    db_path = tmp_path / "partial.db"
    raw = sqlite3.connect(db_path)
    raw.executescript("""
        CREATE TABLE seen_repositories (
            full_name TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_score REAL NOT NULL
        );
        INSERT INTO seen_repositories VALUES ('a/b', '2026-04-01T00:00:00+00:00', 7.5);
    """)
    raw.commit()
    raw.close()

    # First run applies migration fully.
    conn = await connect(db_path)
    await run_migrations(conn)
    await conn.close()

    # Simulate partial crash: wipe schema_version so the runner tries again,
    # but leave the pushed_items row in place.
    raw = sqlite3.connect(db_path)
    raw.execute("DELETE FROM schema_version")
    raw.commit()
    raw.close()

    conn = await connect(db_path)
    applied = await run_migrations(conn)
    assert applied == 1

    async with conn.execute(
        "SELECT COUNT(*) FROM pushed_items WHERE full_name='a/b'"
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 1, "expected exactly one pushed_items row, no duplicates"
    await conn.close()


async def test_blacklist_add_and_check(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    added = await add_blacklist_entry(conn, kind="author", value="spammy-org",
                                      source="manual")
    assert added is True
    dup = await add_blacklist_entry(conn, kind="author", value="spammy-org",
                                    source="manual")
    assert dup is False

    assert await is_blacklisted(conn, kind="author", value="spammy-org") is True
    assert await is_blacklisted(conn, kind="author", value="other") is False
    await conn.close()


async def test_pushed_cooldown_state(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    old = (now - dt.timedelta(days=30)).isoformat()
    recent = (now - dt.timedelta(days=3)).isoformat()

    await conn.execute(
        "INSERT INTO pushed_items (full_name, pushed_at, push_type, "
        "rule_score, llm_score, final_score) VALUES (?, ?, 'digest', 1, 1, 1)",
        ("a/old", old),
    )
    await conn.execute(
        "INSERT INTO pushed_items (full_name, pushed_at, push_type, "
        "rule_score, llm_score, final_score) VALUES (?, ?, 'digest', 1, 1, 1)",
        ("a/recent", recent),
    )
    await conn.commit()

    assert await pushed_cooldown_state(conn, "a/new", now, digest_days=14) == "never"
    assert await pushed_cooldown_state(conn, "a/old", now, digest_days=14) == "expired"
    assert await pushed_cooldown_state(conn, "a/recent", now, digest_days=14) == "active"
    await conn.close()
