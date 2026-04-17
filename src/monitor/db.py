from __future__ import annotations

from pathlib import Path
from typing import List

import aiosqlite


SCHEMA_VERSION = 1

_MIGRATION_001_DDL = """
CREATE TABLE IF NOT EXISTS repositories (
    full_name        TEXT PRIMARY KEY,
    html_url         TEXT,
    description      TEXT,
    language         TEXT,
    topics           TEXT,
    owner_login      TEXT,
    created_at       TEXT,
    first_seen_at    TEXT,
    last_enriched_at TEXT
);

CREATE TABLE IF NOT EXISTS repository_metrics (
    full_name                TEXT NOT NULL,
    collected_at             TEXT NOT NULL,
    stars                    INTEGER,
    forks                    INTEGER,
    star_velocity_day        REAL,
    star_velocity_week       REAL,
    fork_star_ratio          REAL,
    avg_issue_response_hours REAL,
    contributor_count        INTEGER,
    contributor_growth_week  INTEGER,
    readme_completeness      REAL,
    PRIMARY KEY (full_name, collected_at)
);
CREATE INDEX IF NOT EXISTS ix_repository_metrics_full_collected
    ON repository_metrics (full_name, collected_at DESC);

CREATE TABLE IF NOT EXISTS pushed_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name      TEXT NOT NULL,
    pushed_at      TEXT NOT NULL,
    push_type      TEXT NOT NULL CHECK (push_type IN ('digest', 'surge')),
    rule_score     REAL NOT NULL,
    llm_score      REAL NOT NULL,
    final_score    REAL NOT NULL,
    summary        TEXT,
    reason         TEXT,
    tg_chat_id     TEXT,
    tg_message_id  TEXT
);
CREATE INDEX IF NOT EXISTS ix_pushed_items_full_pushed_at
    ON pushed_items (full_name, pushed_at DESC);

CREATE TABLE IF NOT EXISTS user_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    push_id       INTEGER NOT NULL,
    action        TEXT NOT NULL
                      CHECK (action IN ('like','dislike','block_author','block_topic')),
    created_at    TEXT NOT NULL,
    repo_snapshot TEXT,
    FOREIGN KEY (push_id) REFERENCES pushed_items(id)
);
CREATE INDEX IF NOT EXISTS ix_user_feedback_created_at
    ON user_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_user_feedback_push_id
    ON user_feedback (push_id);

CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('repo','author','topic')),
    value      TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('manual','feedback')),
    source_ref TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_blacklist_kind_value
    ON blacklist (kind, value);

CREATE TABLE IF NOT EXISTS preference_profile (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    profile_text            TEXT,
    generated_at            TEXT,
    based_on_feedback_count INTEGER
);

CREATE TABLE IF NOT EXISTS llm_score_cache (
    full_name           TEXT NOT NULL,
    readme_sha256       TEXT NOT NULL,
    score               REAL NOT NULL,
    readme_completeness REAL NOT NULL,
    summary             TEXT,
    reason              TEXT,
    matched_interests   TEXT,
    red_flags           TEXT,
    cached_at           TEXT NOT NULL,
    PRIMARY KEY (full_name, readme_sha256)
);

CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT CHECK (status IN ('ok','partial','failed')),
    stats      TEXT
);
"""


_MIGRATIONS: List[str] = [_MIGRATION_001_DDL]


async def connect(db_path: Path) -> aiosqlite.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.commit()
    return conn


async def current_version(conn: aiosqlite.Connection) -> int:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    async with conn.execute("SELECT MAX(version) FROM schema_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def run_migrations(conn: aiosqlite.Connection) -> int:
    """Apply pending migrations in order, returning how many were applied.

    Crash-safety model: within one migration, ``executescript`` auto-commits
    each DDL statement it runs (it is NOT atomic across the script), and
    per-statement DDL like ``ALTER TABLE`` also commits immediately. The
    trailing ``conn.commit()`` only hardens the DML from ``_migrate_001_data``
    plus the ``schema_version`` insert. A crash before that commit leaves
    schema already applied but no version row — re-running is safe because
    all DDL uses ``IF NOT EXISTS`` and ``_migrate_001_data`` is idempotent
    via its pre-insert existence check. Future migrations must preserve
    these invariants: every DDL idempotent, every data step idempotent.
    """
    version = await current_version(conn)
    applied = 0
    for i, ddl in enumerate(_MIGRATIONS, start=1):
        if i <= version:
            continue
        await conn.executescript(ddl)
        if i == 1:
            await _migrate_001_data(conn)
        await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (i,))
        await conn.commit()
        applied += 1
    return applied


async def _migrate_001_data(conn: aiosqlite.Connection) -> None:
    """Data migration for v1: copy seen_repositories into pushed_items and
    add missing columns to legacy repository_metrics tables."""

    async with conn.execute("PRAGMA table_info(repository_metrics)") as cur:
        existing_cols = {row[1] for row in await cur.fetchall()}
    if existing_cols and "stars" not in existing_cols:
        await conn.execute("ALTER TABLE repository_metrics ADD COLUMN stars INTEGER")
    if existing_cols and "forks" not in existing_cols:
        await conn.execute("ALTER TABLE repository_metrics ADD COLUMN forks INTEGER")

    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='seen_repositories'"
    ) as cur:
        has_legacy = await cur.fetchone() is not None

    if not has_legacy:
        return

    async with conn.execute(
        "SELECT full_name, first_seen_at, last_score FROM seen_repositories"
    ) as cur:
        rows = await cur.fetchall()

    for row in rows:
        full_name = row[0]
        pushed_at = row[1]
        last_score = float(row[2]) if row[2] is not None else 0.0
        async with conn.execute(
            "SELECT 1 FROM pushed_items "
            "WHERE full_name = ? AND pushed_at = ? LIMIT 1",
            (full_name, pushed_at),
        ) as cur:
            if await cur.fetchone() is not None:
                continue
        await conn.execute(
            """
            INSERT INTO pushed_items
                (full_name, pushed_at, push_type,
                 rule_score, llm_score, final_score,
                 summary, reason, tg_chat_id, tg_message_id)
            VALUES (?, ?, 'digest', 0.0, 0.0, ?, NULL, NULL, NULL, NULL)
            """,
            (full_name, pushed_at, last_score),
        )
