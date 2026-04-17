from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import List, Literal

import aiosqlite


SCHEMA_VERSION = 2

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


_MIGRATION_002_DDL = """
CREATE TABLE IF NOT EXISTS daemon_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS: List[str] = [_MIGRATION_001_DDL, _MIGRATION_002_DDL]


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
        if i == 2:
            await _migrate_002_seed(conn)
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


async def _migrate_002_seed(conn: aiosqlite.Connection) -> None:
    """Seed daemon_state with the singleton row. Idempotent: no-op if already present."""
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    await conn.execute(
        "INSERT OR IGNORE INTO daemon_state (id, paused, updated_at) VALUES (1, 0, ?)",
        (now,),
    )


BlacklistKind = Literal["repo", "author", "topic"]
BlacklistSource = Literal["manual", "feedback"]
CooldownState = Literal["never", "active", "expired"]


async def add_blacklist_entry(
    conn: aiosqlite.Connection,
    *,
    kind: BlacklistKind,
    value: str,
    source: BlacklistSource,
    source_ref: str | None = None,
    now: _dt.datetime | None = None,
) -> bool:
    """Returns True if the row was inserted; False if it already existed.

    Uses INSERT OR IGNORE against the UNIQUE (kind, value) index so the
    check-then-insert is a single atomic statement (no TOCTOU window).
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    async with conn.execute(
        """
        INSERT OR IGNORE INTO blacklist (kind, value, added_at, source, source_ref)
        VALUES (?, ?, ?, ?, ?)
        """,
        (kind, value, now.isoformat(), source, source_ref),
    ) as cur:
        inserted = cur.rowcount == 1
    await conn.commit()
    return inserted


async def is_blacklisted(
    conn: aiosqlite.Connection, *, kind: BlacklistKind, value: str
) -> bool:
    async with conn.execute(
        "SELECT 1 FROM blacklist WHERE kind = ? AND value = ? LIMIT 1",
        (kind, value),
    ) as cur:
        return (await cur.fetchone()) is not None


async def pushed_cooldown_state(
    conn: aiosqlite.Connection,
    full_name: str,
    now: _dt.datetime,
    *,
    digest_days: int,
) -> CooldownState:
    async with conn.execute(
        "SELECT MAX(pushed_at) FROM pushed_items WHERE full_name = ?",
        (full_name,),
    ) as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        return "never"
    last = _dt.datetime.fromisoformat(row[0])
    if last.tzinfo is None:
        last = last.replace(tzinfo=_dt.timezone.utc)
    delta = now - last
    return "expired" if delta.days >= digest_days else "active"


async def get_cached_llm_score(
    conn: aiosqlite.Connection,
    full_name: str,
    *,
    readme_sha256: str,
) -> "ScoreResult | None":
    # Imported lazily to avoid a cycle at module import (scoring.types is a
    # higher-level module than db).
    from monitor.scoring.types import ScoreResult

    async with conn.execute(
        """
        SELECT score, readme_completeness, summary, reason,
               matched_interests, red_flags
        FROM llm_score_cache
        WHERE full_name = ? AND readme_sha256 = ?
        LIMIT 1
        """,
        (full_name, readme_sha256),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return ScoreResult(
        score=float(row[0]),
        readme_completeness=float(row[1]),
        summary=row[2] or "",
        reason=row[3] or "",
        matched_interests=json.loads(row[4]) if row[4] else [],
        red_flags=json.loads(row[5]) if row[5] else [],
    )


async def put_cached_llm_score(
    conn: aiosqlite.Connection,
    full_name: str,
    *,
    readme_sha256: str,
    result: "ScoreResult",
    now: _dt.datetime | None = None,
) -> None:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    await conn.execute(
        """
        INSERT INTO llm_score_cache (
            full_name, readme_sha256, score, readme_completeness,
            summary, reason, matched_interests, red_flags, cached_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (full_name, readme_sha256) DO UPDATE SET
            score = excluded.score,
            readme_completeness = excluded.readme_completeness,
            summary = excluded.summary,
            reason = excluded.reason,
            matched_interests = excluded.matched_interests,
            red_flags = excluded.red_flags,
            cached_at = excluded.cached_at
        """,
        (
            full_name,
            readme_sha256,
            result.score,
            result.readme_completeness,
            result.summary,
            result.reason,
            json.dumps(result.matched_interests),
            json.dumps(result.red_flags),
            now.isoformat(),
        ),
    )
    await conn.commit()


async def get_preference_profile(
    conn: aiosqlite.Connection,
) -> dict | None:
    async with conn.execute(
        "SELECT profile_text, generated_at, based_on_feedback_count "
        "FROM preference_profile WHERE id = 1 LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "profile_text": row[0] or "",
        "generated_at": row[1],
        "based_on_feedback_count": int(row[2]) if row[2] is not None else 0,
    }


async def put_preference_profile(
    conn: aiosqlite.Connection,
    *,
    profile_text: str,
    generated_at: _dt.datetime,
    based_on_feedback_count: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO preference_profile
            (id, profile_text, generated_at, based_on_feedback_count)
        VALUES (1, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            profile_text = excluded.profile_text,
            generated_at = excluded.generated_at,
            based_on_feedback_count = excluded.based_on_feedback_count
        """,
        (profile_text, generated_at.isoformat(), based_on_feedback_count),
    )
    await conn.commit()


async def get_daemon_state(conn: aiosqlite.Connection) -> dict:
    async with conn.execute(
        "SELECT paused, updated_at FROM daemon_state WHERE id = 1 LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        # Migration 002 seeds the singleton, so this is only reachable
        # if the caller opened a DB without running migrations. Be safe.
        return {"paused": False, "updated_at": None}
    return {"paused": bool(row[0]), "updated_at": row[1]}


async def set_daemon_paused(
    conn: aiosqlite.Connection,
    *,
    paused: bool,
    now: _dt.datetime | None = None,
) -> None:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    await conn.execute(
        "UPDATE daemon_state SET paused = ?, updated_at = ? WHERE id = 1",
        (1 if paused else 0, now.isoformat()),
    )
    await conn.commit()


async def insert_pushed_item(
    conn: aiosqlite.Connection,
    *,
    repo: "RepoCandidate",
    push_type: Literal["digest", "surge"],
    tg_chat_id: str,
    tg_message_id: str | None = None,
    now: _dt.datetime | None = None,
) -> int:
    """Insert a pushed_items row from a scored RepoCandidate. Returns the
    new row's id so the caller can embed it in inline button callback_data
    before sending the TG message, then update_pushed_tg_message_id once
    the TG send returns a message_id."""
    # Lazy import: db.py is lower-level than monitor.models.
    from monitor.models import RepoCandidate  # noqa: F401

    now = now or _dt.datetime.now(_dt.timezone.utc)
    cur = await conn.execute(
        """
        INSERT INTO pushed_items (
            full_name, pushed_at, push_type,
            rule_score, llm_score, final_score,
            summary, reason, tg_chat_id, tg_message_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo.full_name,
            now.isoformat(),
            push_type,
            repo.rule_score,
            repo.llm_score,
            repo.final_score,
            repo.summary,
            repo.recommendation_reason,
            tg_chat_id,
            tg_message_id,
        ),
    )
    push_id = cur.lastrowid
    await conn.commit()
    assert push_id is not None  # SQLite always assigns an integer PK
    return push_id


async def update_pushed_tg_message_id(
    conn: aiosqlite.Connection,
    *,
    push_id: int,
    tg_message_id: str,
) -> None:
    await conn.execute(
        "UPDATE pushed_items SET tg_message_id = ? WHERE id = ?",
        (tg_message_id, push_id),
    )
    await conn.commit()


async def record_user_feedback(
    conn: aiosqlite.Connection,
    *,
    push_id: int,
    action: Literal["like", "dislike", "block_author", "block_topic"],
    repo_snapshot: dict,
    now: _dt.datetime | None = None,
) -> None:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    await conn.execute(
        """
        INSERT INTO user_feedback (push_id, action, created_at, repo_snapshot)
        VALUES (?, ?, ?, ?)
        """,
        (push_id, action, now.isoformat(), json.dumps(repo_snapshot)),
    )
    await conn.commit()


async def count_feedback_since_last_profile(conn: aiosqlite.Connection) -> int:
    """Count of user_feedback rows created after the current preference_profile's
    generated_at. Used to decide when to regenerate."""
    async with conn.execute(
        "SELECT generated_at FROM preference_profile WHERE id = 1 LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None or row[0] is None:
        # No profile yet — every existing feedback counts.
        async with conn.execute("SELECT COUNT(*) FROM user_feedback") as cur:
            count_row = await cur.fetchone()
        return int(count_row[0])
    async with conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE created_at > ?",
        (row[0],),
    ) as cur:
        count_row = await cur.fetchone()
    return int(count_row[0])
