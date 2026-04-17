# M4 Telegram Bot + Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `python-telegram-bot` async Application to the daemon: render scored `RepoCandidate`s into messages with 4 inline buttons (👍 / 👎 / 🚫 作者 / 🔕 topic), route button callbacks into `user_feedback` + `blacklist` writes, trigger `PreferenceBuilder.regenerate()` every Nth feedback, and expose 5 slash commands (`/top` `/status` `/pause` `/resume` `/reload`). `DaemonState` wraps a persistent `paused` flag + a live `ConfigFile` reference so M5's scheduler will read them cleanly.

**Architecture:** `src/monitor/bot/` holds four focused modules (`render`, `feedback`, `commands`, `app`). `src/monitor/state.py` is the `DaemonState` singleton shared between bot handlers and (later) M5's scheduler. DB schema v2 adds a single-row `daemon_state` table for pause-across-restart. `LLMClient` gains a `generate_text(prompt) -> str` method so `PreferenceBuilder` can run without knowing the SDK internals. `main.py` boots the bot alongside the existing lifecycle. Tests never touch the real Telegram API — handlers are pure async functions exercised with synthetic `Update` / `CallbackQuery` objects.

**Tech Stack:** `python-telegram-bot>=21.0` (already declared in `pyproject.toml`), existing `aiosqlite`, `structlog`, `anthropic`. PTB v21 uses `Application` + `ApplicationBuilder`; we use manual lifecycle (`initialize`/`start`/`updater.start_polling`) for explicit control. No new dependencies.

---

## Background and Prerequisites

- **Branch state:** `m4-telegram-bot` branched from `main` (PR #4 merged). M1-M3 complete; 123 tests green.
- **Legacy:** `src/monitor/legacy.py` still untouched. Its 4 tests continue to pass. Legacy will be deletable after M4 (its pipeline is fully replaced by M2-M4 in the `src/monitor/` namespace), but deletion itself is deferred to M5 when the scheduler fully wires the new pipeline end-to-end.
- **Dependencies:** `python-telegram-bot>=21.0` declared in M1's `pyproject.toml`; no new adds.
- **Config:** `Settings.telegram_bot_token` + `Settings.telegram_chat_id` from env vars (M1). `ConfigFile.preference_refresh_every` defaults to 5 (M1).
- **DB schema:** M1 schema v1 has `pushed_items`, `user_feedback`, `blacklist`, `preference_profile`. M4 adds a **new migration v2** for a single-row `daemon_state` table (pause flag persistence).
- **Design source of truth:** `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`, §3 (architecture/process model), §5 (preference feedback), §7 (reliability/lifecycle).

## File Structure

**New source files**
- `src/monitor/state.py` — `DaemonState` wrapping paused flag + live `ConfigFile` reference. Reads initial `paused` from DB on construction, writes-through on change.
- `src/monitor/bot/render.py` — `render_repo_message(repo, push_id) -> (text, InlineKeyboardMarkup)`.
- `src/monitor/bot/feedback.py` — async callback handler for `CallbackQuery.data` starting with `fb:`.
- `src/monitor/bot/commands.py` — async handlers for `/top` `/status` `/pause` `/resume` `/reload`.
- `src/monitor/bot/app.py` — `create_application(token, chat_id, ...) -> telegram.ext.Application`. Wires handlers + chat-id filter + error handler.

**New test files**
- `tests/unit/test_db_m4_dao.py` — all 7 new DAOs (daemon_state getters/setter, pushed_items insert + update_tg, user_feedback record + count_since, read-helpers for /top + /status).
- `tests/unit/test_state.py` — `DaemonState` load-from-DB, reload_config, set_paused write-through.
- `tests/unit/test_bot_render.py` — message text format + 4 button callback_data shape.
- `tests/unit/test_bot_feedback.py` — callback → DB writes + blacklist updates + preference regen trigger.
- `tests/unit/test_bot_commands.py` — each slash command's expected reply / side-effect.
- `tests/unit/test_llm_generate_text.py` — LLMClient.generate_text happy path + SDK error wrapping.
- `tests/integration/test_pipeline_m4.py` — end-to-end: score a repo → insert pushed_item → render → simulate callback → verify DB state.

**Modified files**
- `src/monitor/db.py` — add migration 002 + 7 DAOs. Bump `SCHEMA_VERSION = 2`.
- `src/monitor/clients/llm.py` — add `LLMClient.generate_text(prompt) -> str`.
- `src/monitor/main.py` — wire bot startup/shutdown into async `run()`. Pass `DaemonState` + conn into `create_application`.
- `CLAUDE.md` — append `### M4 additions` subsection.

**Unchanged**
- `src/monitor/legacy.py`, `src/monitor/pipeline/*`, `src/monitor/scoring/*` (except `preference.py` usage is unaffected; builder is still called the same way).
- `pyproject.toml`, `README.md`.

---

## Task 1: DB schema v2 + daemon_state DAOs

**Files:**
- Modify: `src/monitor/db.py`
- Create: `tests/unit/test_db_m4_dao.py`

- [ ] **Step 1: Write failing test `tests/unit/test_db_m4_dao.py`**

```python
import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    get_daemon_state,
    run_migrations,
    set_daemon_paused,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m4.db"


async def test_daemon_state_default_is_not_paused(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await get_daemon_state(conn)
    assert state["paused"] is False
    # generated row should exist after migration with a valid timestamp
    assert state["updated_at"] is not None
    await conn.close()


async def test_set_daemon_paused_roundtrip(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    await set_daemon_paused(conn, paused=True, now=now)
    state = await get_daemon_state(conn)
    assert state["paused"] is True
    assert state["updated_at"] == now.isoformat()

    await set_daemon_paused(conn, paused=False, now=now + dt.timedelta(minutes=5))
    state = await get_daemon_state(conn)
    assert state["paused"] is False
    await conn.close()


async def test_migrations_from_v1_to_v2_preserves_existing_rows(tmp_db: Path) -> None:
    """Re-running migrations on a v1 DB must add daemon_state without
    touching existing tables."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    # Insert an unrelated row to verify it survives v2
    await conn.execute(
        "INSERT INTO blacklist (kind, value, added_at, source) "
        "VALUES ('author', 'badactor', '2026-01-01T00:00:00+00:00', 'manual')"
    )
    await conn.commit()

    applied = await run_migrations(conn)
    assert applied == 0  # already at latest

    async with conn.execute("SELECT value FROM blacklist") as cur:
        rows = await cur.fetchall()
    assert rows == [("badactor",)]
    await conn.close()
```

- [ ] **Step 2: Verify test fails**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
pytest tests/unit/test_db_m4_dao.py -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'get_daemon_state'` (the DAOs don't exist yet).

- [ ] **Step 3: Add migration 002 to `src/monitor/db.py`**

Locate the existing `_MIGRATION_001_DDL = """..."""` block. Immediately after it, add a new constant for migration 002:

```python
_MIGRATION_002_DDL = """
CREATE TABLE IF NOT EXISTS daemon_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    updated_at TEXT NOT NULL
);
"""
```

Find the existing `SCHEMA_VERSION = 1` constant and change it to:

```python
SCHEMA_VERSION = 2
```

Find the existing `_MIGRATIONS: List[str] = [_MIGRATION_001_DDL]` line and change it to:

```python
_MIGRATIONS: List[str] = [_MIGRATION_001_DDL, _MIGRATION_002_DDL]
```

Find `run_migrations`. Currently its `if i == 1: await _migrate_001_data(conn)` block handles v1 data seeding. Add a v2 data seed right after it:

```python
        if i == 2:
            await _migrate_002_seed(conn)
```

Append a new migration-data helper at the end of the migration helpers (next to `_migrate_001_data`):

```python
async def _migrate_002_seed(conn: aiosqlite.Connection) -> None:
    """Seed daemon_state with the singleton row. Idempotent: no-op if already present."""
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    await conn.execute(
        "INSERT OR IGNORE INTO daemon_state (id, paused, updated_at) VALUES (1, 0, ?)",
        (now,),
    )
```

- [ ] **Step 4: Append `get_daemon_state` + `set_daemon_paused` DAOs**

At the very bottom of `src/monitor/db.py` (after all existing DAOs):

```python


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
```

- [ ] **Step 5: Tests pass**

```bash
pytest tests/unit/test_db_m4_dao.py tests/unit/test_db.py tests/unit/test_db_scoring_dao.py -v
```

Expected: 3 new passing (m4 tests) + all 8 + 7 pre-existing db tests still passing = **18 passed**.

- [ ] **Step 6: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **126 passed** (123 + 3 new).

- [ ] **Step 7: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db_m4_dao.py
git commit -m "feat(db): schema v2 with daemon_state table + get/set DAOs"
```

---

## Task 2: pushed_items + user_feedback write DAOs

**Files:**
- Modify: `src/monitor/db.py`
- Modify: `tests/unit/test_db_m4_dao.py`

- [ ] **Step 1: Append tests to `tests/unit/test_db_m4_dao.py`**

Add to the imports at the top:

```python
from monitor.db import (
    connect,
    count_feedback_since_last_profile,
    get_daemon_state,
    insert_pushed_item,
    record_user_feedback,
    run_migrations,
    set_daemon_paused,
    update_pushed_tg_message_id,
)
from monitor.models import RepoCandidate
```

Append these 4 tests at the end:

```python


def _sample_repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
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
        topics=["agent", "llm"],
        rule_score=7.5,
        llm_score=8.2,
        final_score=7.85,
        summary="Widget library",
        recommendation_reason="Matches interests",
    )


async def test_insert_pushed_item_returns_id_and_persists(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)

    push_id = await insert_pushed_item(
        conn,
        repo=_sample_repo(),
        push_type="digest",
        tg_chat_id="12345",
        now=now,
    )
    assert isinstance(push_id, int)
    assert push_id > 0

    async with conn.execute(
        "SELECT full_name, push_type, rule_score, llm_score, final_score, "
        "summary, reason, tg_chat_id, tg_message_id "
        "FROM pushed_items WHERE id = ?",
        (push_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "acme/widget"
    assert row[1] == "digest"
    assert row[2] == 7.5
    assert row[3] == 8.2
    assert row[4] == 7.85
    assert row[5] == "Widget library"
    assert row[6] == "Matches interests"
    assert row[7] == "12345"
    assert row[8] is None  # tg_message_id filled in later
    await conn.close()


async def test_update_pushed_tg_message_id(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    push_id = await insert_pushed_item(
        conn, repo=_sample_repo(), push_type="digest", tg_chat_id="1"
    )

    await update_pushed_tg_message_id(conn, push_id=push_id, tg_message_id="msg-999")

    async with conn.execute(
        "SELECT tg_message_id FROM pushed_items WHERE id = ?", (push_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "msg-999"
    await conn.close()


async def test_record_user_feedback_inserts_row(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    push_id = await insert_pushed_item(
        conn, repo=_sample_repo(), push_type="digest", tg_chat_id="1"
    )
    now = dt.datetime(2026, 4, 18, 13, 0, tzinfo=dt.timezone.utc)

    snapshot = {"full_name": "acme/widget", "owner_login": "acme", "topics": ["agent"]}
    await record_user_feedback(
        conn,
        push_id=push_id,
        action="like",
        repo_snapshot=snapshot,
        now=now,
    )

    async with conn.execute(
        "SELECT push_id, action, created_at, repo_snapshot FROM user_feedback"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == push_id
    assert rows[0][1] == "like"
    assert rows[0][2] == now.isoformat()
    import json
    assert json.loads(rows[0][3])["full_name"] == "acme/widget"
    await conn.close()


async def test_count_feedback_since_last_profile(tmp_db: Path) -> None:
    """Count of user_feedback rows added since the preference_profile was
    last generated. Drives PreferenceBuilder regen trigger."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    push_id = await insert_pushed_item(
        conn, repo=_sample_repo(), push_type="digest", tg_chat_id="1"
    )

    # No profile yet, no feedback → count 0
    assert await count_feedback_since_last_profile(conn) == 0

    # Add 2 feedback rows before any profile exists → count 2
    t0 = dt.datetime(2026, 4, 18, 10, 0, tzinfo=dt.timezone.utc)
    await record_user_feedback(
        conn, push_id=push_id, action="like", repo_snapshot={}, now=t0
    )
    await record_user_feedback(
        conn,
        push_id=push_id,
        action="dislike",
        repo_snapshot={},
        now=t0 + dt.timedelta(minutes=1),
    )
    assert await count_feedback_since_last_profile(conn) == 2

    # Write a preference_profile with generated_at between the 2 rows
    from monitor.db import put_preference_profile
    await put_preference_profile(
        conn,
        profile_text="p",
        generated_at=t0 + dt.timedelta(seconds=30),
        based_on_feedback_count=1,
    )
    # Now only 1 feedback row is "since" the profile
    assert await count_feedback_since_last_profile(conn) == 1
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_db_m4_dao.py -v 2>&1 | tail -10
```

Expected: ImportError on the new DAO names.

- [ ] **Step 3: Add DAOs to `src/monitor/db.py`**

Append at the very bottom of `src/monitor/db.py`:

```python


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
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_db_m4_dao.py -v
```

Expected: **7 passed** (3 from Task 1 + 4 new).

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **130 passed** (126 + 4 new).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db_m4_dao.py
git commit -m "feat(db): pushed_items insert/update + user_feedback record + feedback-count-since"
```

---

## Task 3: Read DAOs for /top and /status

**Files:**
- Modify: `src/monitor/db.py`
- Modify: `tests/unit/test_db_m4_dao.py`

- [ ] **Step 1: Append tests to `tests/unit/test_db_m4_dao.py`**

Extend the existing import line from `monitor.db` to also pull:

```python
from monitor.db import (
    connect,
    count_feedback_since_last_profile,
    get_daemon_state,
    get_latest_run_logs,
    get_recent_pushes,
    insert_pushed_item,
    record_user_feedback,
    run_migrations,
    set_daemon_paused,
    update_pushed_tg_message_id,
)
```

Append these 2 tests at the end of the file:

```python


async def test_get_recent_pushes_returns_most_recent_first(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    t0 = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)

    # Insert 3 pushes at known times
    for i, delta in enumerate([0, 60, 120]):
        await conn.execute(
            "INSERT INTO pushed_items "
            "(full_name, pushed_at, push_type, rule_score, llm_score, final_score, "
            " summary, reason, tg_chat_id) "
            "VALUES (?, ?, 'digest', 1, 1, ?, ?, ?, ?)",
            (
                f"a/repo-{i}",
                (t0 + dt.timedelta(seconds=delta)).isoformat(),
                float(i),
                f"s{i}",
                f"r{i}",
                "12345",
            ),
        )
    await conn.commit()

    rows = await get_recent_pushes(conn, limit=2)
    assert [r["full_name"] for r in rows] == ["a/repo-2", "a/repo-1"]
    assert rows[0]["final_score"] == 2.0
    assert rows[0]["summary"] == "s2"
    await conn.close()


async def test_get_latest_run_logs_returns_most_recent_first(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    t0 = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)

    import json
    for i, delta in enumerate([0, 60, 120]):
        started = (t0 + dt.timedelta(seconds=delta)).isoformat()
        ended = (t0 + dt.timedelta(seconds=delta + 5)).isoformat()
        await conn.execute(
            "INSERT INTO run_log (kind, started_at, ended_at, status, stats) "
            "VALUES (?, ?, ?, 'ok', ?)",
            (f"digest_{i}", started, ended, json.dumps({"repos_pushed": i})),
        )
    await conn.commit()

    rows = await get_latest_run_logs(conn, limit=2)
    assert [r["kind"] for r in rows] == ["digest_2", "digest_1"]
    assert rows[0]["status"] == "ok"
    assert rows[0]["stats"] == {"repos_pushed": 2}
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_db_m4_dao.py::test_get_recent_pushes_returns_most_recent_first -v 2>&1 | tail -5
```

Expected: ImportError on `get_recent_pushes`.

- [ ] **Step 3: Add read DAOs to `src/monitor/db.py`**

Append at the very bottom of `src/monitor/db.py`:

```python


async def get_recent_pushes(
    conn: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[dict]:
    async with conn.execute(
        """
        SELECT id, full_name, pushed_at, push_type,
               rule_score, llm_score, final_score,
               summary, reason
        FROM pushed_items
        ORDER BY pushed_at DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "full_name": r[1],
            "pushed_at": r[2],
            "push_type": r[3],
            "rule_score": r[4],
            "llm_score": r[5],
            "final_score": r[6],
            "summary": r[7] or "",
            "reason": r[8] or "",
        }
        for r in rows
    ]


async def get_latest_run_logs(
    conn: aiosqlite.Connection,
    *,
    limit: int = 5,
) -> list[dict]:
    async with conn.execute(
        """
        SELECT id, kind, started_at, ended_at, status, stats
        FROM run_log
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    result: list[dict] = []
    for r in rows:
        stats_raw = r[5]
        try:
            stats = json.loads(stats_raw) if stats_raw else {}
        except (TypeError, ValueError):
            stats = {}
        result.append(
            {
                "id": r[0],
                "kind": r[1],
                "started_at": r[2],
                "ended_at": r[3],
                "status": r[4],
                "stats": stats,
            }
        )
    return result
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_db_m4_dao.py -v
```

Expected: **9 passed** (7 previous + 2 new).

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **132 passed** (130 + 2 new).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db_m4_dao.py
git commit -m "feat(db): recent_pushes + latest_run_logs read DAOs for /top and /status"
```

---

## Task 4: DaemonState singleton

**Files:**
- Create: `src/monitor/state.py`
- Create: `tests/unit/test_state.py`

- [ ] **Step 1: Write failing test `tests/unit/test_state.py`**

```python
import datetime as dt
from pathlib import Path

import pytest

from monitor.config import ConfigFile
from monitor.db import connect, get_daemon_state, run_migrations, set_daemon_paused
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


async def test_daemon_state_loads_initial_paused_from_db(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    # Persist paused=True then load
    await set_daemon_paused(conn, paused=True)

    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    assert state.paused is True
    assert state.config is config
    await conn.close()


async def test_daemon_state_set_paused_writes_through(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    config = ConfigFile()
    state = await DaemonState.load(conn=conn, config=config)
    assert state.paused is False

    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    await state.set_paused(True, now=now)
    assert state.paused is True

    # Write-through: a fresh load sees the persisted value
    state2 = await DaemonState.load(conn=conn, config=config)
    assert state2.paused is True
    db_state = await get_daemon_state(conn)
    assert db_state["updated_at"] == now.isoformat()
    await conn.close()


async def test_daemon_state_reload_config_replaces_live_reference(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    config_a = ConfigFile(keywords=["agent"])
    config_b = ConfigFile(keywords=["rust"])
    state = await DaemonState.load(conn=conn, config=config_a)
    assert state.config.keywords == ["agent"]

    state.reload_config(config_b)
    assert state.config.keywords == ["rust"]
    # Prior reference unchanged
    assert config_a.keywords == ["agent"]
    await conn.close()
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_state.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.state'`.

- [ ] **Step 3: Write `src/monitor/state.py`**

```python
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import aiosqlite

from monitor.config import ConfigFile
from monitor.db import get_daemon_state, set_daemon_paused


@dataclass
class DaemonState:
    """Shared daemon-level state — accessed by the TG bot handlers today,
    by the M5 scheduler tomorrow. Holds:

    - `config`: the currently-active ConfigFile (replaceable by /reload)
    - `paused`: whether scheduled work (M5) should run

    `paused` is persisted to the daemon_state table so it survives
    daemon restarts. `config` is purely in-memory; the next restart reloads
    from the same config.json anyway.
    """

    config: ConfigFile
    paused: bool
    conn: aiosqlite.Connection

    @classmethod
    async def load(
        cls, *, conn: aiosqlite.Connection, config: ConfigFile
    ) -> "DaemonState":
        db_state = await get_daemon_state(conn)
        return cls(config=config, paused=bool(db_state["paused"]), conn=conn)

    async def set_paused(self, paused: bool, *, now: dt.datetime | None = None) -> None:
        await set_daemon_paused(self.conn, paused=paused, now=now)
        self.paused = paused

    def reload_config(self, config: ConfigFile) -> None:
        self.config = config
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_state.py -v
```

Expected: **3 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **135 passed** (132 + 3).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/state.py tests/unit/test_state.py
git commit -m "feat(state): DaemonState singleton wrapping live config + persistent paused flag"
```

---

## Task 5: LLMClient.generate_text for free-form prompts

**Files:**
- Modify: `src/monitor/clients/llm.py`
- Create: `tests/unit/test_llm_generate_text.py`

- [ ] **Step 1: Write failing test `tests/unit/test_llm_generate_text.py`**

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.clients.llm import LLMClient
from monitor.scoring.types import LLMScoreError


def _text_block_response(text: str) -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


def _client_with_mock(response) -> LLMClient:
    fake_sdk = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=response))
    )
    return LLMClient(
        api_key="k",
        base_url="u",
        model="minimax-m2",
        anthropic_client=fake_sdk,
    )


async def test_generate_text_returns_first_text_block() -> None:
    client = _client_with_mock(_text_block_response("用户偏好 Rust 系统工具"))
    result = await client.generate_text("prompt")
    assert result == "用户偏好 Rust 系统工具"


async def test_generate_text_sends_model_and_prompt() -> None:
    client = _client_with_mock(_text_block_response("ok"))
    await client.generate_text("say hi please")

    create_mock = client._client.messages.create
    kwargs = create_mock.call_args.kwargs
    assert kwargs["model"] == "minimax-m2"
    assert kwargs["messages"] == [{"role": "user", "content": "say hi please"}]
    # No tools on generate_text — it's a free-form chat call
    assert "tools" not in kwargs or kwargs["tools"] in (None, [])


async def test_generate_text_raises_llm_score_error_on_sdk_failure() -> None:
    fake_sdk = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("network down"))
        )
    )
    client = LLMClient(api_key="k", base_url="u", model="m", anthropic_client=fake_sdk)
    with pytest.raises(LLMScoreError):
        await client.generate_text("prompt")


async def test_generate_text_raises_when_no_text_block() -> None:
    # Response has only tool_use, no text
    block = SimpleNamespace(type="tool_use", name="x", input={})
    resp = SimpleNamespace(content=[block])
    client = _client_with_mock(resp)
    with pytest.raises(LLMScoreError):
        await client.generate_text("prompt")
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_llm_generate_text.py -v 2>&1 | tail -10
```

Expected: `AttributeError: 'LLMClient' object has no attribute 'generate_text'`.

- [ ] **Step 3: Add `generate_text` to `LLMClient`**

Open `/Users/Zhuanz/Documents/GithubRepoMonitor/src/monitor/clients/llm.py`. Locate the `score_repo` method. Immediately after it, inside the class, add:

```python
    async def generate_text(self, prompt: str) -> str:
        """Free-form text completion — used by PreferenceBuilder to summarize
        recent feedback into a preference profile. No tool use; we expect a
        plain text-block response.

        Raises LLMScoreError on SDK failure or when no text block is returned
        so the caller can decide whether to ignore (preference regen is
        best-effort) or log and continue.
        """
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.generate_text_sdk_error", error=str(exc))
            raise LLMScoreError(str(exc), cause="sdk_error") from exc

        _log_usage(resp, "generate_text")
        content = getattr(resp, "content", None) or []
        for block in content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    return text
        raise LLMScoreError(
            "no text block in generate_text response", cause="missing_text"
        )
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_llm_generate_text.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **139 passed** (135 + 4).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/clients/llm.py tests/unit/test_llm_generate_text.py
git commit -m "feat(clients/llm): generate_text for free-form prompts used by PreferenceBuilder"
```

---

## Task 6: bot/render.py — message + inline keyboard

**Files:**
- Create: `src/monitor/bot/render.py`
- Create: `tests/unit/test_bot_render.py`

- [ ] **Step 1: Write failing test `tests/unit/test_bot_render.py`**

```python
import datetime as dt

import pytest
from telegram import InlineKeyboardMarkup

from monitor.bot.render import (
    CALLBACK_PREFIX,
    parse_callback_data,
    render_repo_message,
)
from monitor.models import RepoCandidate


def _repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
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
        topics=["agent", "llm"],
        rule_score=7.5,
        llm_score=8.2,
        final_score=7.85,
        summary="Widget library",
        recommendation_reason="Matches your agent interest",
    )


def test_render_repo_message_contains_core_fields() -> None:
    text, markup = render_repo_message(_repo(), push_id=42)
    assert "acme/widget" in text
    assert "7.85" in text  # final_score
    assert "Widget library" in text  # summary
    assert "Matches your agent interest" in text  # reason
    assert "https://github.com/acme/widget" in text
    assert isinstance(markup, InlineKeyboardMarkup)


def test_render_repo_message_has_four_buttons_with_callback_data() -> None:
    _, markup = render_repo_message(_repo(), push_id=42)
    # InlineKeyboardMarkup.inline_keyboard is list[list[InlineKeyboardButton]]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 4

    labels_to_actions = {
        "👍": "like",
        "👎": "dislike",
        "🚫 作者": "block_author",
        "🔕 topic": "block_topic",
    }
    for button in buttons:
        matched = False
        for emoji_prefix, action in labels_to_actions.items():
            if emoji_prefix in button.text:
                expected = f"{CALLBACK_PREFIX}:{action}:42"
                assert button.callback_data == expected
                matched = True
                break
        assert matched, f"unexpected button label: {button.text}"


def test_parse_callback_data_roundtrips() -> None:
    assert parse_callback_data("fb:like:42") == ("like", 42)
    assert parse_callback_data("fb:block_author:9") == ("block_author", 9)
    # Invalid payloads return None
    assert parse_callback_data("unrelated") is None
    assert parse_callback_data("fb:like") is None
    assert parse_callback_data("fb:unknown_action:5") is None
    assert parse_callback_data("fb:like:not_a_number") is None


def test_render_truncates_or_falls_back_on_missing_summary() -> None:
    repo = _repo()
    repo.summary = ""
    repo.recommendation_reason = ""
    text, _ = render_repo_message(repo, push_id=1)
    # Should still render without crashing; name + url always present
    assert "acme/widget" in text
    assert "https://github.com/acme/widget" in text
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_bot_render.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.bot.render'`.

- [ ] **Step 3: Write `src/monitor/bot/render.py`**

```python
from __future__ import annotations

from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from monitor.models import RepoCandidate


CALLBACK_PREFIX = "fb"
FeedbackAction = Literal["like", "dislike", "block_author", "block_topic"]
_ACTIONS: tuple[FeedbackAction, ...] = (
    "like",
    "dislike",
    "block_author",
    "block_topic",
)


def render_repo_message(
    repo: RepoCandidate, *, push_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Render a scored RepoCandidate into (text, InlineKeyboardMarkup).

    The 4 feedback buttons embed `push_id` in callback_data so the
    feedback handler can resolve the originating pushed_items row
    without re-querying the message.
    """
    lines = [
        f"⭐ {repo.full_name}  ({repo.final_score:.2f}/10)",
    ]
    if repo.summary:
        lines.append(f"一句话: {repo.summary}")
    if repo.recommendation_reason:
        lines.append(f"推荐: {repo.recommendation_reason}")
    lines.append(f"🔗 {repo.html_url}")
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("👍", callback_data=f"{CALLBACK_PREFIX}:like:{push_id}"),
            InlineKeyboardButton("👎", callback_data=f"{CALLBACK_PREFIX}:dislike:{push_id}"),
        ],
        [
            InlineKeyboardButton(
                "🚫 作者", callback_data=f"{CALLBACK_PREFIX}:block_author:{push_id}"
            ),
            InlineKeyboardButton(
                "🔕 topic", callback_data=f"{CALLBACK_PREFIX}:block_topic:{push_id}"
            ),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def parse_callback_data(data: str) -> tuple[FeedbackAction, int] | None:
    """Return (action, push_id) if `data` is a valid feedback callback,
    else None. Invalid shapes (wrong prefix, unknown action, non-integer
    id) all return None so the handler can no-op silently."""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action = parts[1]
    if action not in _ACTIONS:
        return None
    try:
        push_id = int(parts[2])
    except ValueError:
        return None
    return (action, push_id)  # type: ignore[return-value]
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_bot_render.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **143 passed** (139 + 4).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/bot/render.py tests/unit/test_bot_render.py
git commit -m "feat(bot/render): render_repo_message + 4 feedback buttons + callback parser"
```

---

## Task 7: bot/feedback.py — callback handler

**Files:**
- Create: `src/monitor/bot/feedback.py`
- Create: `tests/unit/test_bot_feedback.py`

Context: the feedback handler parses callback data, writes to `user_feedback`, updates `blacklist` for block-* actions, edits the source message to show "已记录", and triggers `PreferenceBuilder.regenerate()` when the feedback count since the last profile refresh hits `preference_refresh_every`.

The handler is written as a pure async function that takes:
- `conn`: `aiosqlite.Connection` for DB writes
- `pref_builder`: `PreferenceBuilder` for regen trigger
- `refresh_threshold`: int (e.g., 5)
- `update`: an object with `.callback_query` (PTB Update)

This shape avoids a PTB `ContextTypes.DEFAULT_TYPE` dependency in the handler itself — M4 Task 9 wires the real PTB update via a thin adapter.

- [ ] **Step 1: Write failing test `tests/unit/test_bot_feedback.py`**

```python
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.feedback import handle_feedback_callback
from monitor.db import (
    connect,
    insert_pushed_item,
    is_blacklisted,
    run_migrations,
)
from monitor.models import RepoCandidate
from monitor.scoring.preference import PreferenceBuilder, RegenerationResult


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "fb.db"


def _repo(name: str = "acme/widget", topics: list[str] | None = None) -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=now,
        pushed_at=now,
        owner_login=name.split("/")[0],
        topics=topics or ["agent"],
        rule_score=5.0,
        llm_score=6.0,
        final_score=5.5,
        summary="s",
        recommendation_reason="r",
    )


def _fake_callback_query(data: str, push_id_in_data: int | None = None) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking PTB's CallbackQuery."""
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def _fake_update(callback_query) -> SimpleNamespace:
    return SimpleNamespace(callback_query=callback_query)


async def _insert_sample(conn, repo: RepoCandidate) -> int:
    # Also seed the `repositories` row so _load_repo_snapshot can retrieve
    # topics. The M5 scheduler will populate this via the collect stage;
    # tests do it directly.
    import json
    await conn.execute(
        "INSERT OR REPLACE INTO repositories (full_name, owner_login, topics) "
        "VALUES (?, ?, ?)",
        (repo.full_name, repo.owner_login, json.dumps(repo.topics or [])),
    )
    await conn.commit()
    return await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="12345"
    )


async def test_like_callback_writes_feedback_and_acks(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo()
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:like:{push_id}")
    update = _fake_update(cq)

    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update,
        conn=conn,
        pref_builder=pref_builder,
        refresh_threshold=5,
    )

    cq.answer.assert_awaited()
    cq.edit_message_text.assert_awaited()  # message is updated with ack

    async with conn.execute(
        "SELECT action, push_id FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("like", push_id)]
    # Blacklist untouched for a like
    assert await is_blacklisted(conn, kind="author", value="acme") is False
    pref_builder.regenerate.assert_not_awaited()
    await conn.close()


async def test_block_author_callback_adds_to_blacklist(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo(name="spammy/repo")
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:block_author:{push_id}")
    update = _fake_update(cq)
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update, conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    assert await is_blacklisted(conn, kind="author", value="spammy") is True
    # user_feedback row recorded alongside the blacklist entry
    async with conn.execute(
        "SELECT action FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("block_author",)]
    await conn.close()


async def test_block_topic_callback_adds_first_topic_to_blacklist(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo(topics=["agent", "llm"])
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:block_topic:{push_id}")
    update = _fake_update(cq)
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update, conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    # First topic picked
    assert await is_blacklisted(conn, kind="topic", value="agent") is True
    # Second topic NOT auto-blacklisted
    assert await is_blacklisted(conn, kind="topic", value="llm") is False
    await conn.close()


async def test_block_topic_with_no_topics_does_not_crash(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo(topics=[])
    push_id = await _insert_sample(conn, repo)
    cq = _fake_callback_query(f"fb:block_topic:{push_id}")
    update = _fake_update(cq)
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        update, conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    # No topic entry was added
    async with conn.execute(
        "SELECT COUNT(*) FROM blacklist WHERE kind = 'topic'"
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 0
    # Feedback row is still recorded
    async with conn.execute(
        "SELECT COUNT(*) FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 1
    await conn.close()


async def test_feedback_threshold_triggers_preference_regen(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _repo()
    push_id = await _insert_sample(conn, repo)
    pref_builder = SimpleNamespace(
        regenerate=AsyncMock(
            return_value=RegenerationResult(
                profile_text="new", generated_at=dt.datetime.now(dt.timezone.utc),
                based_on_feedback_count=3,
            )
        )
    )

    # refresh_threshold=3: first 2 likes don't trigger; 3rd does.
    for i in range(3):
        cq = _fake_callback_query(f"fb:like:{push_id}")
        await handle_feedback_callback(
            _fake_update(cq), conn=conn, pref_builder=pref_builder, refresh_threshold=3
        )

    assert pref_builder.regenerate.await_count == 1
    await conn.close()


async def test_invalid_callback_data_is_noop(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    cq = _fake_callback_query("garbage")
    pref_builder = SimpleNamespace(regenerate=AsyncMock())

    await handle_feedback_callback(
        _fake_update(cq), conn=conn, pref_builder=pref_builder, refresh_threshold=5
    )

    cq.answer.assert_awaited()  # always ack so TG button isn't stuck spinning
    cq.edit_message_text.assert_not_awaited()  # no message to edit
    async with conn.execute("SELECT COUNT(*) FROM user_feedback") as cur:
        count = (await cur.fetchone())[0]
    assert count == 0
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_bot_feedback.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.bot.feedback'`.

- [ ] **Step 3: Write `src/monitor/bot/feedback.py`**

```python
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Protocol

import aiosqlite
import structlog

from monitor.bot.render import parse_callback_data
from monitor.db import (
    add_blacklist_entry,
    count_feedback_since_last_profile,
    record_user_feedback,
)


log = structlog.get_logger(__name__)


class PreferenceBuilderLike(Protocol):
    async def regenerate(self) -> Any: ...


async def handle_feedback_callback(
    update: Any,
    *,
    conn: aiosqlite.Connection,
    pref_builder: PreferenceBuilderLike,
    refresh_threshold: int,
) -> None:
    """Handle one feedback-button press.

    `update` is a PTB Update-shaped object — we only touch
    `update.callback_query.{data,answer,edit_message_text}` so tests can
    pass a `SimpleNamespace` without spinning up a real PTB app.
    """
    cq = update.callback_query
    await cq.answer()  # always ack so the TG button stops spinning

    parsed = parse_callback_data(cq.data or "")
    if parsed is None:
        log.info("feedback.invalid_callback_data", data=cq.data)
        return
    action, push_id = parsed

    repo_snapshot = await _load_repo_snapshot(conn, push_id)

    await record_user_feedback(
        conn,
        push_id=push_id,
        action=action,
        repo_snapshot=repo_snapshot,
    )

    if action == "block_author":
        owner = repo_snapshot.get("owner_login")
        if owner:
            await add_blacklist_entry(
                conn,
                kind="author",
                value=owner,
                source="feedback",
                source_ref=str(push_id),
            )
    elif action == "block_topic":
        topics = repo_snapshot.get("topics") or []
        if topics:
            await add_blacklist_entry(
                conn,
                kind="topic",
                value=topics[0],
                source="feedback",
                source_ref=str(push_id),
            )

    ack_text = _render_ack(action, repo_snapshot)
    try:
        await cq.edit_message_text(ack_text)
    except Exception as exc:  # noqa: BLE001 - edit may fail if msg too old; log and move on
        log.warning(
            "feedback.edit_message_failed",
            push_id=push_id,
            action=action,
            error=str(exc),
        )

    # Trigger preference regen when we have enough new feedback.
    new_count = await count_feedback_since_last_profile(conn)
    if new_count >= refresh_threshold:
        try:
            await pref_builder.regenerate()
        except Exception as exc:  # noqa: BLE001 - regen is best-effort
            log.warning("feedback.pref_regen_failed", error=str(exc))


async def _load_repo_snapshot(conn: aiosqlite.Connection, push_id: int) -> dict:
    """Snapshot enough of the pushed repo to drive blacklist / preference
    decisions. We pull from pushed_items rather than joining to repositories
    to keep the feedback handler independent of the (still-evolving) repo
    metadata schema."""
    async with conn.execute(
        "SELECT full_name, summary FROM pushed_items WHERE id = ?",
        (push_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {}
    full_name = row[0] or ""
    owner = full_name.split("/", 1)[0] if "/" in full_name else ""
    # Topics aren't stored in pushed_items; in practice the scheduler will
    # have written them to the repositories table. For feedback purposes
    # we query that table; if missing, degrade gracefully.
    async with conn.execute(
        "SELECT topics FROM repositories WHERE full_name = ?",
        (full_name,),
    ) as cur:
        topics_row = await cur.fetchone()
    topics: list[str] = []
    if topics_row and topics_row[0]:
        try:
            import json as _json
            parsed = _json.loads(topics_row[0])
            if isinstance(parsed, list):
                topics = [str(t) for t in parsed]
        except (TypeError, ValueError):
            pass
    return {
        "full_name": full_name,
        "owner_login": owner,
        "topics": topics,
        "summary": row[1] or "",
    }


def _render_ack(action: str, snapshot: dict) -> str:
    name = snapshot.get("full_name", "(未知项目)")
    summary = snapshot.get("summary", "")
    prefix = {
        "like": "✅ 已 👍",
        "dislike": "✅ 已 👎",
        "block_author": "✅ 已屏蔽作者",
        "block_topic": "✅ 已屏蔽 topic",
    }.get(action, "✅ 已记录")
    base = f"{prefix}: {name}"
    return f"{base}\n{summary}" if summary else base
```

Note: `_load_repo_snapshot` looks up `topics` from the `repositories` table. That table is populated by the M5 scheduler's pipeline. The test helper `_insert_sample` above already seeds the `repositories` row directly so topics flow through correctly — M4 is the first milestone that exercises this join, so the seeding is on the test side.

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_bot_feedback.py -v
```

Expected: **6 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **149 passed** (143 + 6).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/bot/feedback.py tests/unit/test_bot_feedback.py
git commit -m "feat(bot/feedback): callback handler writing feedback + blacklist + regen trigger"
```

---

## Task 8: bot/commands.py — slash command handlers

**Files:**
- Create: `src/monitor/bot/commands.py`
- Create: `tests/unit/test_bot_commands.py`

Context: Five commands. Each handler is a pure async function taking the same adapter-shaped args (`update`, `conn`, `state`, `settings_reloader` where needed). Handlers reply by calling `update.message.reply_text(...)`; tests verify with `AsyncMock`.

- [ ] **Step 1: Write failing test `tests/unit/test_bot_commands.py`**

```python
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.commands import (
    handle_pause,
    handle_reload,
    handle_resume,
    handle_status,
    handle_top,
)
from monitor.config import ConfigFile
from monitor.db import connect, insert_pushed_item, run_migrations
from monitor.models import RepoCandidate
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "cmd.db"


def _fake_update() -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock())
    )


def _repo(name: str, score: float) -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=now,
        pushed_at=now,
        owner_login=name.split("/")[0],
        topics=[],
        final_score=score,
        summary=f"summary for {name}",
        recommendation_reason="r",
    )


async def test_top_lists_recent_pushed_items(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    for i, (name, score) in enumerate([("a/one", 9.0), ("a/two", 7.5), ("a/three", 5.0)]):
        await insert_pushed_item(
            conn,
            repo=_repo(name, score),
            push_type="digest",
            tg_chat_id="1",
            now=dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
            + dt.timedelta(seconds=i),
        )

    update = _fake_update()
    await handle_top(update, conn=conn, limit=2)

    update.message.reply_text.assert_awaited()
    reply = update.message.reply_text.await_args.args[0]
    assert "a/three" in reply  # most recent
    assert "a/two" in reply
    assert "a/one" not in reply  # excluded by limit=2
    await conn.close()


async def test_top_with_no_pushes_returns_placeholder(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    update = _fake_update()
    await handle_top(update, conn=conn, limit=10)
    reply = update.message.reply_text.await_args.args[0]
    assert "暂无" in reply or "no" in reply.lower()
    await conn.close()


async def test_status_reports_paused_flag_and_recent_runs(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    await state.set_paused(True)

    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    await conn.execute(
        "INSERT INTO run_log (kind, started_at, ended_at, status, stats) "
        "VALUES ('digest_morning', ?, ?, 'ok', ?)",
        (now.isoformat(), (now + dt.timedelta(seconds=3)).isoformat(), json.dumps({"repos_pushed": 4})),
    )
    await conn.commit()

    update = _fake_update()
    await handle_status(update, conn=conn, state=state)

    reply = update.message.reply_text.await_args.args[0]
    assert "paused" in reply.lower() or "暂停" in reply
    assert "digest_morning" in reply
    await conn.close()


async def test_pause_flips_state_and_confirms(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    assert state.paused is False

    update = _fake_update()
    await handle_pause(update, state=state)

    assert state.paused is True
    reply = update.message.reply_text.await_args.args[0]
    assert "暂停" in reply or "paused" in reply.lower()
    await conn.close()


async def test_resume_flips_state_and_confirms(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())
    await state.set_paused(True)

    update = _fake_update()
    await handle_resume(update, state=state)

    assert state.paused is False
    reply = update.message.reply_text.await_args.args[0]
    assert "恢复" in reply or "resumed" in reply.lower()
    await conn.close()


async def test_reload_invokes_reloader_and_swaps_config(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile(keywords=["old"]))

    new_config = ConfigFile(keywords=["new"])

    async def fake_reloader() -> ConfigFile:
        return new_config

    update = _fake_update()
    await handle_reload(update, state=state, config_reloader=fake_reloader)

    assert state.config.keywords == ["new"]
    reply = update.message.reply_text.await_args.args[0]
    assert "reload" in reply.lower() or "重载" in reply or "已更新" in reply
    await conn.close()


async def test_reload_reports_error_and_keeps_old_config(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile(keywords=["old"]))

    async def failing_reloader() -> ConfigFile:
        raise ValueError("bad json")

    update = _fake_update()
    await handle_reload(update, state=state, config_reloader=failing_reloader)

    # State unchanged
    assert state.config.keywords == ["old"]
    reply = update.message.reply_text.await_args.args[0]
    assert "bad json" in reply or "失败" in reply or "error" in reply.lower()
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_bot_commands.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.bot.commands'`.

- [ ] **Step 3: Write `src/monitor/bot/commands.py`**

```python
from __future__ import annotations

from typing import Any, Awaitable, Callable

import aiosqlite
import structlog

from monitor.config import ConfigFile
from monitor.db import get_recent_pushes, get_latest_run_logs
from monitor.state import DaemonState


log = structlog.get_logger(__name__)

ConfigReloader = Callable[[], Awaitable[ConfigFile]]


async def handle_top(
    update: Any,
    *,
    conn: aiosqlite.Connection,
    limit: int = 10,
) -> None:
    pushes = await get_recent_pushes(conn, limit=limit)
    if not pushes:
        await update.message.reply_text("📭 暂无推送记录")
        return
    lines = ["🔝 最近推送"]
    for p in pushes:
        lines.append(
            f"• {p['full_name']}  {p['final_score']:.2f}/10\n  {p['summary']}"
        )
    await update.message.reply_text("\n".join(lines))


async def handle_status(
    update: Any,
    *,
    conn: aiosqlite.Connection,
    state: DaemonState,
) -> None:
    runs = await get_latest_run_logs(conn, limit=3)
    lines = ["📊 状态"]
    if state.paused:
        lines.append("⏸ paused (暂停中)")
    else:
        lines.append("▶️ running")
    if not runs:
        lines.append("最近运行: 无")
    else:
        lines.append("最近运行:")
        for r in runs:
            status = r.get("status") or "?"
            stats = r.get("stats") or {}
            pushed = stats.get("repos_pushed", "?")
            lines.append(
                f"• {r['kind']}  {r['started_at']}  [{status}]  推送={pushed}"
            )
    await update.message.reply_text("\n".join(lines))


async def handle_pause(update: Any, *, state: DaemonState) -> None:
    await state.set_paused(True)
    await update.message.reply_text("⏸ 已暂停，定时采集/推送不会触发。`/resume` 恢复。")


async def handle_resume(update: Any, *, state: DaemonState) -> None:
    await state.set_paused(False)
    await update.message.reply_text("▶️ 已恢复，下一次定时触发照常跑。")


async def handle_reload(
    update: Any,
    *,
    state: DaemonState,
    config_reloader: ConfigReloader,
) -> None:
    try:
        new_config = await config_reloader()
    except Exception as exc:  # noqa: BLE001 - surface any reload error to the operator
        log.warning("commands.reload_failed", error=str(exc))
        await update.message.reply_text(f"❌ 重载失败: {exc}")
        return
    state.reload_config(new_config)
    await update.message.reply_text("✅ 配置已重载")
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_bot_commands.py -v
```

Expected: **7 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **156 passed** (149 + 7).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/bot/commands.py tests/unit/test_bot_commands.py
git commit -m "feat(bot/commands): /top /status /pause /resume /reload handlers"
```

---

## Task 9: bot/app.py — Application wiring

**Files:**
- Create: `src/monitor/bot/app.py`
- Create: `tests/unit/test_bot_app.py`

Context: `create_application` builds a PTB `Application` with token, registers all 5 commands + the feedback CallbackQueryHandler + a chat-ID filter that silently ignores updates from any chat other than the configured `chat_id`. Tests verify the Application has the expected handlers attached but do NOT start polling — no live Telegram connection.

- [ ] **Step 1: Write failing test `tests/unit/test_bot_app.py`**

```python
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from monitor.bot.app import create_application
from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


async def _make_state(tmp_db: Path) -> DaemonState:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    return await DaemonState.load(conn=conn, config=ConfigFile())


async def test_create_application_registers_five_commands(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)
    pref_builder = SimpleNamespace(regenerate=AsyncMock())

    async def reloader() -> ConfigFile:
        return ConfigFile()

    app = create_application(
        token="dummy-token",
        chat_id="12345",
        conn=state.conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=5,
        config_reloader=reloader,
    )

    command_names: list[str] = []
    for handler_list in app.handlers.values():
        for handler in handler_list:
            if isinstance(handler, CommandHandler):
                command_names.extend(sorted(handler.commands))

    assert sorted(command_names) == ["pause", "reload", "resume", "status", "top"]
    await state.conn.close()


async def test_create_application_registers_callback_query_handler(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)
    pref_builder = SimpleNamespace(regenerate=AsyncMock())

    async def reloader() -> ConfigFile:
        return ConfigFile()

    app = create_application(
        token="dummy-token",
        chat_id="12345",
        conn=state.conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=5,
        config_reloader=reloader,
    )

    has_callback_handler = False
    for handler_list in app.handlers.values():
        for handler in handler_list:
            if isinstance(handler, CallbackQueryHandler):
                has_callback_handler = True
                break
    assert has_callback_handler
    await state.conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_bot_app.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.bot.app'`.

- [ ] **Step 3: Write `src/monitor/bot/app.py`**

```python
from __future__ import annotations

from typing import Any

import aiosqlite
import structlog
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from monitor.bot import commands, feedback
from monitor.bot.commands import ConfigReloader
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


def create_application(
    *,
    token: str,
    chat_id: str,
    conn: aiosqlite.Connection,
    state: DaemonState,
    pref_builder: Any,
    refresh_threshold: int,
    config_reloader: ConfigReloader,
) -> Application:
    """Build a PTB Application with all M4 handlers registered.

    Returns the Application without starting polling — caller owns the
    lifecycle (initialize / start / start_polling / stop) so shutdown
    sequencing can be composed with the rest of the daemon.
    """
    app = ApplicationBuilder().token(token).build()

    # Only process updates from the configured chat. Silent drop otherwise.
    # `filters.Chat(chat_id=int(chat_id))` uses numeric ids — telegram chat
    # ids can be negative (groups), but we store them as strings in Settings.
    chat_filter = filters.Chat(chat_id=int(chat_id))

    # Commands
    app.add_handler(
        CommandHandler(
            "top",
            _wrap(lambda update, _ctx: commands.handle_top(update, conn=conn)),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "status",
            _wrap(
                lambda update, _ctx: commands.handle_status(
                    update, conn=conn, state=state
                )
            ),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "pause",
            _wrap(lambda update, _ctx: commands.handle_pause(update, state=state)),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "resume",
            _wrap(lambda update, _ctx: commands.handle_resume(update, state=state)),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "reload",
            _wrap(
                lambda update, _ctx: commands.handle_reload(
                    update, state=state, config_reloader=config_reloader
                )
            ),
            filters=chat_filter,
        )
    )

    # Feedback buttons
    app.add_handler(
        CallbackQueryHandler(
            _wrap(
                lambda update, _ctx: feedback.handle_feedback_callback(
                    update,
                    conn=conn,
                    pref_builder=pref_builder,
                    refresh_threshold=refresh_threshold,
                )
            ),
            pattern=r"^fb:",
        )
    )

    app.add_error_handler(_error_handler)
    return app


def _wrap(coro_factory):
    """Adapt a plain `async (update) -> None` function into a PTB-shaped
    `async (update, context) -> None` handler. All our handlers use `update`
    only, so the context is discarded."""
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await coro_factory(update, context)
    return _handler


async def _error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    log.warning("bot.handler_error", error=str(context.error))
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_bot_app.py -v
```

Expected: **2 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **158 passed** (156 + 2).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/bot/app.py tests/unit/test_bot_app.py
git commit -m "feat(bot/app): Application wiring with chat-id filter + error handler"
```

---

## Task 10: main.py — wire bot into daemon lifecycle

**Files:**
- Modify: `src/monitor/main.py`
- Modify: `tests/integration/test_main_lifecycle.py`

Context: `run()` currently loads config, configures logging, opens DB, runs migrations, waits for SIGTERM. M4 extends it: after migrations, construct DaemonState, LLMClient, PreferenceBuilder; if both `telegram_bot_token` and `telegram_chat_id` are set, build + start the PTB application; on SIGTERM, stop the bot cleanly before closing DB.

If credentials are absent, skip the bot entirely (existing behavior: daemon starts and waits for SIGTERM, no TG).

- [ ] **Step 1: Extend the main_lifecycle test to cover the bot-disabled path**

Open `/Users/Zhuanz/Documents/GithubRepoMonitor/tests/integration/test_main_lifecycle.py`. The existing test already asserts the daemon starts and exits cleanly on SIGTERM with no credentials. That's the bot-disabled path after M4 — still green. But add a second test asserting the daemon emits `telegram.disabled` when credentials are absent:

Append this test at the END of the file:

```python


async def test_main_logs_telegram_disabled_when_no_credentials(tmp_path: Path) -> None:
    proc = await _start_process(tmp_path)
    try:
        disabled_seen = False
        for _ in range(50):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            if not line:
                break
            if b"telegram.disabled" in line:
                disabled_seen = True
                break
        assert disabled_seen, "daemon should log telegram.disabled when token missing"
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
```

- [ ] **Step 2: Run the lifecycle tests — first one still passes, second one fails**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
pytest tests/integration/test_main_lifecycle.py -v 2>&1 | tail -10
```

Expected: 1 passed + 1 failed (`assert disabled_seen, "daemon should log telegram.disabled..."` — current code doesn't emit that log).

- [ ] **Step 3: Update `src/monitor/main.py` to wire the bot**

Replace the full contents of `/Users/Zhuanz/Documents/GithubRepoMonitor/src/monitor/main.py` with:

```python
from __future__ import annotations

import asyncio
import json
import signal
import sys
import traceback
from pathlib import Path

import structlog

from monitor.bot.app import create_application
from monitor.clients.llm import LLMClient
from monitor.config import ConfigFile, Settings, load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging
from monitor.scoring.preference import PreferenceBuilder
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run() -> int:
    # load_config + configure_logging run before the logger is available, so
    # any exception here has to go to stderr for systemd's journal.
    try:
        settings, config = load_config()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    configure_logging(settings.log_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "startup",
        db_path=str(settings.db_path),
        config_path=str(settings.config_path) if settings.config_path else None,
        keywords=config.keywords,
        languages=config.languages,
    )

    try:
        conn = await connect(settings.db_path)
    except Exception:
        log.exception("startup.connect_failed")
        return 1

    bot_app = None
    try:
        try:
            applied = await run_migrations(conn)
        except Exception:
            log.exception("startup.migrations_failed")
            return 1
        log.info("migrations.applied", count=applied)

        if stop.is_set():
            log.info("shutdown.requested_during_startup")
            return 0

        state = await DaemonState.load(conn=conn, config=config)

        # Optional TG bot — only starts if both credentials are present.
        bot_app = await _maybe_start_bot(settings, config, state, conn)

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        if bot_app is not None:
            try:
                await bot_app.updater.stop()
                await bot_app.stop()
                await bot_app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("shutdown.bot_stop_failed")
        await conn.close()
        log.info("shutdown.done")
    return 0


async def _maybe_start_bot(
    settings: Settings,
    config: ConfigFile,
    state: DaemonState,
    conn,
):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.disabled", reason="missing_credentials")
        return None
    if not settings.minimax_api_key:
        # No LLM → PreferenceBuilder's regen calls will fail; bot runs but
        # regen is disabled. Log so the operator knows.
        log.info("telegram.llm_disabled", reason="missing_minimax_key")

    llm_client = _build_llm_client(settings, config)
    pref_builder = PreferenceBuilder(
        conn=conn,
        llm_generate_profile=llm_client.generate_text
        if llm_client is not None
        else _no_llm_generator,
    )

    async def config_reloader() -> ConfigFile:
        if settings.config_path is None:
            raise RuntimeError("MONITOR_CONFIG is not set")
        path = Path(settings.config_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ConfigFile.model_validate(payload)

    app = create_application(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        conn=conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=config.preference_refresh_every,
        config_reloader=config_reloader,
    )

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    except Exception:
        log.exception("telegram.start_failed")
        try:
            await app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        return None

    log.info("telegram.started", chat_id=settings.telegram_chat_id)
    return app


def _build_llm_client(settings: Settings, config: ConfigFile) -> LLMClient | None:
    if not settings.minimax_api_key:
        return None
    return LLMClient(
        api_key=settings.minimax_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
    )


async def _no_llm_generator(_prompt: str) -> str:
    raise RuntimeError("LLM not configured; preference regeneration skipped")


def cli() -> None:
    sys.exit(asyncio.run(run()))
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/integration/test_main_lifecycle.py -v
```

Expected: **2 passed** (original + new `telegram.disabled` log assertion).

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **159 passed** (158 + 1 new integration test).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/main.py tests/integration/test_main_lifecycle.py
git commit -m "feat(main): wire bot into daemon lifecycle with graceful start/stop"
```

---

## Task 11: Integration test — end-to-end score → push → feedback

**Files:**
- Create: `tests/integration/test_pipeline_m4.py`

Context: Exercises the full M4 write path that M5's scheduler will ultimately trigger: score a repo, insert a pushed_items row, render a message, simulate a feedback callback, verify user_feedback + blacklist are written. No PTB runtime — the callback handler is called directly with a SimpleNamespace Update.

- [ ] **Step 1: Write the integration test**

```python
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.feedback import handle_feedback_callback
from monitor.bot.render import render_repo_message
from monitor.db import (
    connect,
    insert_pushed_item,
    is_blacklisted,
    run_migrations,
)
from monitor.models import RepoCandidate


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m4.db"


def _scored_repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="Widget",
        language="Python",
        stars=500,
        forks=50,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login="acme",
        topics=["agent", "llm"],
        rule_score=7.0,
        llm_score=8.5,
        final_score=7.7,
        summary="Solid widget library",
        recommendation_reason="Matches your agent interest",
    )


async def _seed_repositories_row(conn, repo: RepoCandidate) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO repositories (full_name, owner_login, topics) "
        "VALUES (?, ?, ?)",
        (repo.full_name, repo.owner_login, json.dumps(repo.topics)),
    )
    await conn.commit()


async def test_score_render_push_and_dislike_callback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _scored_repo()
    await _seed_repositories_row(conn, repo)
    push_id = await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="12345"
    )

    # Render produces text with the repo name and a 4-button keyboard
    text, markup = render_repo_message(repo, push_id=push_id)
    assert repo.full_name in text
    assert markup.inline_keyboard  # non-empty

    # Simulate the user pressing 👎
    cq = SimpleNamespace(
        data=f"fb:dislike:{push_id}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        SimpleNamespace(callback_query=cq),
        conn=conn,
        pref_builder=pref_builder,
        refresh_threshold=5,
    )

    # user_feedback row written
    async with conn.execute(
        "SELECT action FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("dislike",)]

    # blacklist untouched for a plain 👎
    assert await is_blacklisted(conn, kind="author", value="acme") is False
    await conn.close()


async def test_block_author_flow(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    repo = _scored_repo()
    await _seed_repositories_row(conn, repo)
    push_id = await insert_pushed_item(
        conn, repo=repo, push_type="digest", tg_chat_id="12345"
    )

    cq = SimpleNamespace(
        data=f"fb:block_author:{push_id}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    pref_builder = SimpleNamespace(regenerate=AsyncMock(return_value=None))

    await handle_feedback_callback(
        SimpleNamespace(callback_query=cq),
        conn=conn,
        pref_builder=pref_builder,
        refresh_threshold=5,
    )

    assert await is_blacklisted(conn, kind="author", value="acme") is True
    async with conn.execute(
        "SELECT action FROM user_feedback WHERE push_id = ?", (push_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [("block_author",)]
    await conn.close()
```

- [ ] **Step 2: Run test**

```bash
pytest tests/integration/test_pipeline_m4.py -v
```

Expected: **2 passed**.

- [ ] **Step 3: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **161 passed** (159 + 2).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_pipeline_m4.py
git commit -m "test(integration): score→push→feedback end-to-end with simulated callback"
```

---

## Task 12: CLAUDE.md M4 additions

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append `### M4 additions` after the existing M3 subsection**

Locate the final paragraph of the `### M3 additions` subsection in `/Users/Zhuanz/Documents/GithubRepoMonitor/CLAUDE.md`. Append immediately after (preserving one blank line between sections):

```markdown

### M4 additions

`src/monitor/bot/` has four focused modules. `render.py` turns a scored `RepoCandidate` into a message text + 4-button `InlineKeyboardMarkup` with callback data shaped `fb:{action}:{push_id}`. `feedback.py` parses those callbacks, writes `user_feedback` + `blacklist` rows, edits the source message to show acknowledgement, and triggers `PreferenceBuilder.regenerate()` once `count_feedback_since_last_profile` crosses `config.preference_refresh_every`. `commands.py` exposes `/top` `/status` `/pause` `/resume` `/reload` as pure async handlers. `app.py` wires them into a PTB `Application` with a chat-id filter — updates from any chat other than the configured `TELEGRAM_CHAT_ID` are silently ignored.

`src/monitor/state.py` introduces `DaemonState`: a singleton that holds the live `ConfigFile` reference and a `paused` bool persisted to the new `daemon_state` table (migration 002). M4 flips the flag via `/pause` / `/resume`; M5's scheduler reads it before every tick. `/reload` re-reads the JSON config file and swaps `state.config` atomically.

`LLMClient.generate_text(prompt)` was added alongside `score_repo` so `PreferenceBuilder` can call it without touching the SDK internals. It uses a plain messages call (no tool use) and raises `LLMScoreError` on SDK failure or missing text block.

`src/monitor/main.py` now boots the bot alongside the existing lifecycle. Without both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, the bot is skipped (logs `telegram.disabled`) — M1's existing SIGTERM integration test still passes because that scenario is the no-bot path. Without `MINIMAX_API_KEY` the bot still runs but preference regeneration is a no-op.

DB additions: migration 002 creates `daemon_state`. Seven new DAOs in `db.py` — `get_daemon_state`, `set_daemon_paused`, `insert_pushed_item`, `update_pushed_tg_message_id`, `record_user_feedback`, `count_feedback_since_last_profile`, `get_recent_pushes`, `get_latest_run_logs`. Tests continue the DI-mocked pattern — no real Telegram API calls in the suite.

M4 does NOT yet fire push messages on a schedule. That is M5's scheduler task: it will call `score_repo`, then `insert_pushed_item`, then `render_repo_message`, then `bot.send_message(chat_id=..., text=text, reply_markup=markup)`, then `update_pushed_tg_message_id(id, msg_id)`. M4 provides all the building blocks.
```

- [ ] **Step 2: Full suite still passes**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **161 passed** (doc-only change).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: extend CLAUDE.md architecture for M4 Telegram bot + feedback loop"
```

---

## M4 Verification Criteria

- [x] `pytest tests/` — **~161 passed** (123 from M3 + ~38 new)
- [x] `src/monitor/bot/` has 4 modules: `render.py`, `feedback.py`, `commands.py`, `app.py`
- [x] `src/monitor/state.py` exports `DaemonState`
- [x] `src/monitor/db.py` exposes 7 new M4 DAOs; `SCHEMA_VERSION = 2`
- [x] `LLMClient.generate_text` exists alongside `score_repo`
- [x] `python -m monitor` with no TG credentials → starts, logs `telegram.disabled`, exits clean on SIGTERM
- [x] `python -m monitor` with TG + MINIMAX credentials would boot the bot (not verified in CI — operational)
- [x] `monitor.legacy` unchanged; its 4 tests still pass
- [x] CLAUDE.md has an M4 additions subsection

## Out of Scope (M5+)

- Scheduler — `digest_morning` / `digest_evening` / `surge_poll` / `weekly_digest` APScheduler jobs (M5)
- Pipeline wiring — calling `score_repo` then `insert_pushed_item` then `bot.send_message` from scheduled jobs (M5)
- `/digest_now` command — needs scheduler-triggerable digest function (M5)
- Weekly digest message (aggregate of pushed_items + feedback + run_log — M5)
- systemd / healthcheck / backup / logrotate (M6)
- Live Telegram smoke test (operational TODO)
- Deleting `monitor.legacy` — do it in M5 once the scheduler is the sole entry for the new pipeline
