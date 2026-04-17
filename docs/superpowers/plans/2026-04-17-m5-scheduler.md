# M5 Scheduler + Surge + Weekly Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Do NOT remove `conn.row_factory = aiosqlite.Row` from `src/monitor/db.py` — prior subagents did this twice in M4 and both times it had to be reversed.**

**Goal:** Wire M2 collect + M2 enrich + M3 score + M4 bot into four scheduled jobs that actually push to Telegram: `digest_morning` (08:00) + `digest_evening` (20:00) + `surge_poll` (every 30 min) + `weekly_digest` (Sunday 21:00). Add `/digest_now` manual trigger. Single `asyncio.Lock` keeps these non-reentrant. Delete `monitor.legacy` — its role is fully replaced.

**Architecture:** `monitor/scheduler.py` wraps `apscheduler.schedulers.asyncio.AsyncIOScheduler` and mounts 4 jobs, each guarded by `DaemonState.digest_lock`. `monitor/pipeline/filter.py` applies rule + blacklist + cooldown in one pass. `monitor/pipeline/digest.py` orchestrates collect → filter → enrich → score → push. `monitor/pipeline/surge.py` is the shorter-path twin that reads existing candidates from the DB (no new search) and reuses enrich + score + push. `monitor/pipeline/weekly.py` builds a text report by aggregating pushed_items + user_feedback + run_log. `monitor/bot/push.py` centralizes the insert→render→send→update-tg-id sequence both digest and surge use. `main.py` grows a few more deps (scheduler lifecycle) and the bot commands file grows `/digest_now`. Legacy module and its tests are deleted at the end.

**Tech Stack:** `apscheduler>=3.10` (declared in M1), existing `aiosqlite` / `structlog` / `python-telegram-bot`. No new deps.

---

## Background and Prerequisites

- **Branch state:** `m5-scheduler` branched from `main` (PR #6 M4 merged, PR #8 pending copilot move). M1-M4 complete; 164 tests green.
- **Legacy:** `src/monitor/legacy.py` stays live until M5 Task 12 deletes it. Its 4 tests at `tests/test_monitor.py` are deleted in the same commit.
- **Dependencies:** `apscheduler>=3.10` declared in M1's `pyproject.toml`; no new adds.
- **Config:** `ConfigFile.top_n`, `digest_cooldown_days`, `surge`, `preference_refresh_every`, `weights` all set in M1. `Settings.telegram_chat_id`, `github_token`, `minimax_api_key` from env.
- **DB schema:** Both `run_log` and `repository_metrics` / `repositories` tables already exist from M1 migration 001. M5 only adds DAO helpers — no schema changes.
- **Design source of truth:** `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md` — §3 (process model), §4 (data flow + surge), §7 (reliability), §8 (weekly digest).

## File Structure

**New source files**
- `src/monitor/pipeline/filter.py` — `apply_filters(repos, *, rule_engine, conn, digest_cooldown_days, now=None) -> list[RepoCandidate]`
- `src/monitor/pipeline/digest.py` — `run_digest(*, push_type, github_client, llm_score_fn, rule_engine, state, conn, bot_app, chat_id, now=None) -> dict` (stats)
- `src/monitor/pipeline/surge.py` — `run_surge(*, github_client, llm_score_fn, rule_engine, state, conn, bot_app, chat_id, now=None) -> dict`
- `src/monitor/pipeline/weekly.py` — `build_weekly_digest(conn, now) -> str`
- `src/monitor/bot/push.py` — `push_repo(repo, *, bot_app, chat_id, conn, push_type) -> int | None`
- `src/monitor/scheduler.py` — `create_scheduler(deps) -> AsyncIOScheduler`; `start_scheduler(scheduler)` / `stop_scheduler(scheduler)`

**New test files**
- `tests/unit/test_db_m5_dao.py` — all 8 new DAOs
- `tests/unit/test_pipeline_filter.py`
- `tests/unit/test_bot_push.py`
- `tests/unit/test_pipeline_digest.py`
- `tests/unit/test_pipeline_surge.py`
- `tests/unit/test_pipeline_weekly.py`
- `tests/unit/test_bot_commands_digest_now.py` — appended test for new `/digest_now`
- `tests/unit/test_scheduler.py`
- `tests/integration/test_pipeline_m5.py` — full end-to-end

**Modified files**
- `src/monitor/db.py` — append 8 new DAOs (run_log start/finish, repositories upsert, repository_metrics upsert, get_latest_metric, get_surge_candidates, get_pushed_since, get_feedback_counts_since)
- `src/monitor/state.py` — add `digest_lock: asyncio.Lock` field
- `src/monitor/bot/commands.py` — append `handle_digest_now`
- `src/monitor/bot/app.py` — register the new command handler; thread scheduler-level deps through
- `src/monitor/main.py` — boot scheduler after bot; graceful shutdown
- `CLAUDE.md` — append `### M5 additions` subsection

**Deleted files**
- `src/monitor/legacy.py` (replaced by the productized pipeline)
- `tests/test_monitor.py` (tested legacy)

---

## Task 1: DB DAOs — run_log + repositories + metrics upsert

**Files:**
- Modify: `src/monitor/db.py`
- Create: `tests/unit/test_db_m5_dao.py`

- [ ] **Step 1: Write failing test `tests/unit/test_db_m5_dao.py`**

```python
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
```

- [ ] **Step 2: Verify tests fail**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
pytest tests/unit/test_db_m5_dao.py -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'start_run_log'` (or similar).

- [ ] **Step 3: Append DAOs to `src/monitor/db.py`**

Append at the very bottom of `src/monitor/db.py` (after existing DAOs):

```python


async def start_run_log(
    conn: aiosqlite.Connection,
    *,
    kind: str,
    now: _dt.datetime | None = None,
) -> int:
    """Open a run_log entry. Returns the id; caller passes it to
    finish_run_log on completion."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cur = await conn.execute(
        "INSERT INTO run_log (kind, started_at) VALUES (?, ?)",
        (kind, now.isoformat()),
    )
    run_id = cur.lastrowid
    await conn.commit()
    assert run_id is not None
    return run_id


async def finish_run_log(
    conn: aiosqlite.Connection,
    *,
    run_id: int,
    status: Literal["ok", "partial", "failed"],
    stats: dict,
    now: _dt.datetime | None = None,
) -> None:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    await conn.execute(
        "UPDATE run_log SET ended_at = ?, status = ?, stats = ? WHERE id = ?",
        (now.isoformat(), status, json.dumps(stats), run_id),
    )
    await conn.commit()


async def upsert_repositories(
    conn: aiosqlite.Connection,
    repos: list["RepoCandidate"],
    *,
    now: _dt.datetime | None = None,
) -> None:
    """Bulk upsert. first_seen_at is write-once (INSERT only); last_enriched_at
    is refreshed every call. The M5 digest pipeline calls this right after
    collect so surge can later read topics/owner without fetching."""
    from monitor.models import RepoCandidate  # noqa: F401

    now = now or _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat()
    for repo in repos:
        await conn.execute(
            """
            INSERT INTO repositories (
                full_name, html_url, description, language, topics,
                owner_login, created_at, first_seen_at, last_enriched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (full_name) DO UPDATE SET
                html_url = excluded.html_url,
                description = excluded.description,
                language = excluded.language,
                topics = excluded.topics,
                owner_login = excluded.owner_login,
                created_at = excluded.created_at,
                last_enriched_at = excluded.last_enriched_at
            """,
            (
                repo.full_name,
                repo.html_url,
                repo.description,
                repo.language,
                json.dumps(list(repo.topics)),
                repo.owner_login,
                repo.created_at.isoformat(),
                now_iso,
                now_iso,
            ),
        )
    await conn.commit()


async def upsert_repository_metrics(
    conn: aiosqlite.Connection,
    repo: "RepoCandidate",
    *,
    now: _dt.datetime | None = None,
) -> None:
    """Append a metrics snapshot (time-series row keyed by
    (full_name, collected_at)). Upsert semantics guard against the rare
    collision when two collections happen within the same second."""
    from monitor.models import RepoCandidate  # noqa: F401

    now = now or _dt.datetime.now(_dt.timezone.utc)
    await conn.execute(
        """
        INSERT INTO repository_metrics (
            full_name, collected_at,
            stars, forks, star_velocity_day, star_velocity_week,
            fork_star_ratio, avg_issue_response_hours,
            contributor_count, contributor_growth_week, readme_completeness
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (full_name, collected_at) DO UPDATE SET
            stars = excluded.stars,
            forks = excluded.forks,
            star_velocity_day = excluded.star_velocity_day,
            star_velocity_week = excluded.star_velocity_week,
            fork_star_ratio = excluded.fork_star_ratio,
            avg_issue_response_hours = excluded.avg_issue_response_hours,
            contributor_count = excluded.contributor_count,
            contributor_growth_week = excluded.contributor_growth_week,
            readme_completeness = excluded.readme_completeness
        """,
        (
            repo.full_name,
            now.isoformat(),
            repo.stars,
            repo.forks,
            repo.star_velocity_day,
            repo.star_velocity_week,
            repo.fork_star_ratio,
            repo.avg_issue_response_hours,
            repo.contributor_count,
            repo.contributor_growth_week,
            repo.readme_completeness,
        ),
    )
    await conn.commit()
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_db_m5_dao.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **168 passed** (164 + 4 new).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db_m5_dao.py
git commit -m "feat(db): run_log start/finish + repositories/metrics upsert DAOs"
```

---

## Task 2: DB DAOs — surge + weekly read helpers

**Files:**
- Modify: `src/monitor/db.py`
- Modify: `tests/unit/test_db_m5_dao.py`

- [ ] **Step 1: Append tests**

Extend the imports at the top of `tests/unit/test_db_m5_dao.py`:

```python
from monitor.db import (
    connect,
    finish_run_log,
    get_feedback_counts_since,
    get_latest_metric,
    get_pushed_since,
    get_surge_candidates,
    insert_pushed_item,
    record_user_feedback,
    run_migrations,
    start_run_log,
    upsert_repositories,
    upsert_repository_metrics,
)
```

Append 4 tests at the end of the file:

```python


async def test_get_latest_metric_returns_most_recent(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    repo = _repo()
    t0 = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)
    await upsert_repository_metrics(conn, repo, now=t0)
    # Bump velocity for second snapshot
    repo.star_velocity_day = 12.0
    await upsert_repository_metrics(conn, repo, now=t0 + dt.timedelta(hours=12))

    latest = await get_latest_metric(conn, repo.full_name)
    assert latest is not None
    assert latest["star_velocity_day"] == 12.0
    assert latest["collected_at"] == (t0 + dt.timedelta(hours=12)).isoformat()

    # Missing repo returns None
    missing = await get_latest_metric(conn, "no/such")
    assert missing is None
    await conn.close()


async def test_get_surge_candidates_filters_by_cooldown(tmp_db: Path) -> None:
    """Surge pool = repos in `repositories` whose last pushed_at is either
    NULL or older than surge cooldown (default 3d). Active-cooldown pushes
    must be excluded."""
    conn = await connect(tmp_db)
    await run_migrations(conn)

    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)
    # Seed three repos into repositories
    for name in ("a/never", "b/recent", "c/stale"):
        await upsert_repositories(conn, [_repo(name)], now=now)

    # b/recent: pushed 1 day ago (within 3d cooldown — should be excluded)
    await conn.execute(
        "INSERT INTO pushed_items (full_name, pushed_at, push_type, rule_score, "
        "llm_score, final_score, tg_chat_id) VALUES (?, ?, 'digest', 0, 0, 0, '1')",
        ("b/recent", (now - dt.timedelta(days=1)).isoformat()),
    )
    # c/stale: pushed 10 days ago (past cooldown — should be included)
    await conn.execute(
        "INSERT INTO pushed_items (full_name, pushed_at, push_type, rule_score, "
        "llm_score, final_score, tg_chat_id) VALUES (?, ?, 'digest', 0, 0, 0, '1')",
        ("c/stale", (now - dt.timedelta(days=10)).isoformat()),
    )
    await conn.commit()

    candidates = await get_surge_candidates(conn, now=now, cooldown_days=3)
    names = sorted(c["full_name"] for c in candidates)
    assert names == ["a/never", "c/stale"]
    await conn.close()


async def test_get_pushed_since_filters_by_timestamp(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    t0 = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)
    for i, offset in enumerate([-8, -3, -1]):  # days
        at = (t0 + dt.timedelta(days=offset)).isoformat()
        await conn.execute(
            "INSERT INTO pushed_items (full_name, pushed_at, push_type, rule_score, "
            "llm_score, final_score, tg_chat_id) VALUES (?, ?, 'digest', 1, 1, ?, '1')",
            (f"a/repo-{i}", at, float(i)),
        )
    await conn.commit()

    since = t0 - dt.timedelta(days=7)
    rows = await get_pushed_since(conn, since=since)
    names = sorted(r["full_name"] for r in rows)
    # The -8-day repo is EXCLUDED (before 'since'); the others are included
    assert names == ["a/repo-1", "a/repo-2"]
    await conn.close()


async def test_get_feedback_counts_since(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    # Seed a pushed_items row to satisfy FK
    push_id = await insert_pushed_item(
        conn, repo=_repo(), push_type="digest", tg_chat_id="1"
    )
    t0 = dt.datetime(2026, 4, 18, 8, 0, tzinfo=dt.timezone.utc)

    for action, offset in [
        ("like", -8),      # before the since window
        ("like", -1),      # in window
        ("like", -0.5),    # in window
        ("dislike", -2),   # in window
        ("block_author", -1),  # in window but not counted
    ]:
        await record_user_feedback(
            conn,
            push_id=push_id,
            action=action,
            repo_snapshot={},
            now=t0 + dt.timedelta(days=offset),
        )

    since = t0 - dt.timedelta(days=7)
    counts = await get_feedback_counts_since(conn, since=since)
    assert counts == {"like": 2, "dislike": 1, "block_author": 1, "block_topic": 0}
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_db_m5_dao.py -v 2>&1 | tail -10
```

Expected: ImportError on the new DAO names.

- [ ] **Step 3: Append DAOs to `src/monitor/db.py`**

```python


async def get_latest_metric(
    conn: aiosqlite.Connection, full_name: str
) -> dict | None:
    async with conn.execute(
        """
        SELECT collected_at, stars, forks, star_velocity_day, star_velocity_week,
               fork_star_ratio, avg_issue_response_hours,
               contributor_count, contributor_growth_week, readme_completeness
        FROM repository_metrics
        WHERE full_name = ?
        ORDER BY collected_at DESC
        LIMIT 1
        """,
        (full_name,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "collected_at": row[0],
        "stars": row[1],
        "forks": row[2],
        "star_velocity_day": row[3],
        "star_velocity_week": row[4],
        "fork_star_ratio": row[5],
        "avg_issue_response_hours": row[6],
        "contributor_count": row[7],
        "contributor_growth_week": row[8],
        "readme_completeness": row[9],
    }


async def get_surge_candidates(
    conn: aiosqlite.Connection,
    *,
    now: _dt.datetime,
    cooldown_days: int,
) -> list[dict]:
    """Repos from `repositories` that are either never pushed OR whose last
    push is older than cooldown_days. Used by the surge poll — we never
    re-surface anything pushed inside the cooldown window."""
    cutoff = (now - _dt.timedelta(days=cooldown_days)).isoformat()
    async with conn.execute(
        """
        SELECT r.full_name, r.html_url, r.description, r.language,
               r.topics, r.owner_login, r.created_at
        FROM repositories r
        LEFT JOIN (
            SELECT full_name, MAX(pushed_at) AS last_pushed_at
            FROM pushed_items
            GROUP BY full_name
        ) p ON p.full_name = r.full_name
        WHERE p.last_pushed_at IS NULL OR p.last_pushed_at < ?
        """,
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "full_name": r[0],
            "html_url": r[1],
            "description": r[2] or "",
            "language": r[3] or "Unknown",
            "topics": json.loads(r[4]) if r[4] else [],
            "owner_login": r[5] or "",
            "created_at": r[6],
        }
        for r in rows
    ]


async def get_pushed_since(
    conn: aiosqlite.Connection,
    *,
    since: _dt.datetime,
) -> list[dict]:
    async with conn.execute(
        """
        SELECT id, full_name, pushed_at, push_type, final_score, summary
        FROM pushed_items
        WHERE pushed_at >= ?
        ORDER BY pushed_at DESC
        """,
        (since.isoformat(),),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "full_name": r[1],
            "pushed_at": r[2],
            "push_type": r[3],
            "final_score": r[4],
            "summary": r[5] or "",
        }
        for r in rows
    ]


async def get_feedback_counts_since(
    conn: aiosqlite.Connection,
    *,
    since: _dt.datetime,
) -> dict[str, int]:
    async with conn.execute(
        """
        SELECT action, COUNT(*)
        FROM user_feedback
        WHERE created_at >= ?
        GROUP BY action
        """,
        (since.isoformat(),),
    ) as cur:
        rows = await cur.fetchall()
    counts: dict[str, int] = {
        "like": 0,
        "dislike": 0,
        "block_author": 0,
        "block_topic": 0,
    }
    for action, count in rows:
        if action in counts:
            counts[action] = int(count)
    return counts
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_db_m5_dao.py -v
```

Expected: **8 passed** (4 from Task 1 + 4 new).

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **172 passed** (168 + 4).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db_m5_dao.py
git commit -m "feat(db): surge candidates + pushed_since + feedback counts + latest_metric DAOs"
```

---

## Task 3: `DaemonState.digest_lock`

**Files:**
- Modify: `src/monitor/state.py`
- Modify: `tests/unit/test_state.py`

- [ ] **Step 1: Append test**

Append to `tests/unit/test_state.py`:

```python


async def test_daemon_state_has_digest_lock(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    state = await DaemonState.load(conn=conn, config=ConfigFile())

    import asyncio
    assert isinstance(state.digest_lock, asyncio.Lock)
    # Not held initially
    assert not state.digest_lock.locked()
    async with state.digest_lock:
        assert state.digest_lock.locked()
    assert not state.digest_lock.locked()
    await conn.close()
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_state.py::test_daemon_state_has_digest_lock -v
```

Expected: `AttributeError: 'DaemonState' object has no attribute 'digest_lock'`.

- [ ] **Step 3: Add `digest_lock` field to `DaemonState`**

Open `src/monitor/state.py`. Add `import asyncio` at the top (next to `import datetime as dt`). Modify the `@dataclass` decorator to allow `field` usage and add the lock:

Replace the entire module contents of `src/monitor/state.py` with:

```python
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

import aiosqlite

from monitor.config import ConfigFile
from monitor.db import get_daemon_state, set_daemon_paused


@dataclass
class DaemonState:
    """Shared daemon-level state — accessed by the TG bot handlers (M4) and
    the M5 scheduler. Holds:

    - `config`: currently-active ConfigFile (replaceable by /reload)
    - `paused`: whether scheduled work should run (persisted in daemon_state)
    - `conn`: DB connection for write-through operations
    - `digest_lock`: asyncio.Lock serializing digest/surge/weekly runs and
      /digest_now so the pipeline is non-reentrant. Held by the scheduler
      job for the duration of a single run.
    """

    config: ConfigFile
    paused: bool
    conn: aiosqlite.Connection
    digest_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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

Expected: **4 passed** (3 existing + 1 new).

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **173 passed** (172 + 1).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/state.py tests/unit/test_state.py
git commit -m "feat(state): DaemonState.digest_lock for non-reentrant pipeline"
```

---

## Task 4: `pipeline/filter.py` — rule + blacklist + cooldown

**Files:**
- Create: `src/monitor/pipeline/filter.py`
- Create: `tests/unit/test_pipeline_filter.py`

- [ ] **Step 1: Write failing test `tests/unit/test_pipeline_filter.py`**

```python
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
        topics=topics or [],
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
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_pipeline_filter.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.pipeline.filter'`.

- [ ] **Step 3: Write `src/monitor/pipeline/filter.py`**

```python
from __future__ import annotations

import datetime as dt

import aiosqlite
import structlog

from monitor.db import is_blacklisted, pushed_cooldown_state
from monitor.models import RepoCandidate
from monitor.scoring.rules import RuleEngine


log = structlog.get_logger(__name__)


async def apply_filters(
    repos: list[RepoCandidate],
    *,
    rule_engine: RuleEngine,
    conn: aiosqlite.Connection,
    digest_cooldown_days: int,
    now: dt.datetime | None = None,
) -> list[RepoCandidate]:
    """Coarse filter stage: rule engine, blacklist (repo/author/topic),
    cooldown. Runs BEFORE enrichment so we don't waste API calls on repos
    that won't be pushed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    survivors: list[RepoCandidate] = []
    for repo in repos:
        if not rule_engine.apply(repo):
            log.debug("filter.rule_drop", repo=repo.full_name)
            continue

        if await is_blacklisted(conn, kind="repo", value=repo.full_name):
            log.info("filter.blacklist_drop", repo=repo.full_name, kind="repo")
            continue
        if await is_blacklisted(conn, kind="author", value=repo.owner_login):
            log.info("filter.blacklist_drop", repo=repo.full_name, kind="author")
            continue
        topic_hit = False
        for topic in repo.topics:
            if await is_blacklisted(conn, kind="topic", value=topic):
                log.info(
                    "filter.blacklist_drop",
                    repo=repo.full_name,
                    kind="topic",
                    value=topic,
                )
                topic_hit = True
                break
        if topic_hit:
            continue

        state = await pushed_cooldown_state(
            conn, repo.full_name, now, digest_days=digest_cooldown_days
        )
        if state == "active":
            log.debug("filter.cooldown_active", repo=repo.full_name)
            continue

        survivors.append(repo)
    return survivors
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_pipeline_filter.py -v
```

Expected: **6 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **179 passed** (173 + 6).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/pipeline/filter.py tests/unit/test_pipeline_filter.py
git commit -m "feat(pipeline/filter): apply_filters combining rules + blacklist + cooldown"
```

---

## Task 5: `bot/push.py` — common send flow

**Files:**
- Create: `src/monitor/bot/push.py`
- Create: `tests/unit/test_bot_push.py`

- [ ] **Step 1: Write failing test `tests/unit/test_bot_push.py`**

```python
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.push import push_repo
from monitor.db import connect, run_migrations
from monitor.models import RepoCandidate


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "push.db"


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
        topics=["agent"],
        rule_score=7.0,
        llm_score=8.2,
        final_score=7.7,
        summary="Solid widget",
        recommendation_reason="matches",
    )


def _fake_bot_app(send_message_return=None, send_message_exc=None) -> SimpleNamespace:
    """Mimic PTB Application.bot.send_message. Returns a SimpleNamespace
    with a `.message_id` so push_repo can update the pushed_items row."""
    if send_message_exc is not None:
        send = AsyncMock(side_effect=send_message_exc)
    else:
        send = AsyncMock(return_value=send_message_return or SimpleNamespace(message_id=12345))
    return SimpleNamespace(bot=SimpleNamespace(send_message=send))


async def test_push_repo_inserts_row_sends_message_and_updates_tg_id(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    bot_app = _fake_bot_app()

    push_id = await push_repo(
        _repo(),
        bot_app=bot_app,
        chat_id="12345",
        conn=conn,
        push_type="digest",
    )
    assert push_id is not None

    bot_app.bot.send_message.assert_awaited()
    kwargs = bot_app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    # Text contains the repo name + the message rendered by render_repo_message
    assert "acme/widget" in kwargs["text"]
    # Digest push has NO 🔥 prefix
    assert "🔥" not in kwargs["text"]
    assert kwargs["reply_markup"] is not None

    async with conn.execute(
        "SELECT tg_message_id FROM pushed_items WHERE id = ?", (push_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == "12345"
    await conn.close()


async def test_push_repo_surge_adds_fire_prefix(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    bot_app = _fake_bot_app()

    await push_repo(
        _repo(),
        bot_app=bot_app,
        chat_id="12345",
        conn=conn,
        push_type="surge",
    )
    kwargs = bot_app.bot.send_message.await_args.kwargs
    assert kwargs["text"].startswith("🔥")
    await conn.close()


async def test_push_repo_returns_none_on_send_failure(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    bot_app = _fake_bot_app(send_message_exc=RuntimeError("telegram down"))

    push_id = await push_repo(
        _repo(),
        bot_app=bot_app,
        chat_id="12345",
        conn=conn,
        push_type="digest",
    )
    assert push_id is None
    # Row WAS inserted (id generated), but tg_message_id remains NULL
    async with conn.execute(
        "SELECT COUNT(*) FROM pushed_items WHERE tg_message_id IS NULL"
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 1
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_bot_push.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.bot.push'`.

- [ ] **Step 3: Write `src/monitor/bot/push.py`**

```python
from __future__ import annotations

from typing import Any, Literal

import aiosqlite
import structlog

from monitor.bot.render import render_repo_message
from monitor.db import insert_pushed_item, update_pushed_tg_message_id
from monitor.models import RepoCandidate


log = structlog.get_logger(__name__)


async def push_repo(
    repo: RepoCandidate,
    *,
    bot_app: Any,
    chat_id: str,
    conn: aiosqlite.Connection,
    push_type: Literal["digest", "surge"],
) -> int | None:
    """Insert pushed_items row → render → send → update tg_message_id.

    Returns the push_id on successful send, or None if Telegram send failed
    (the pushed_items row is left with tg_message_id=NULL; a future
    reconciliation job could clean these up if needed).
    """
    push_id = await insert_pushed_item(
        conn, repo=repo, push_type=push_type, tg_chat_id=chat_id
    )
    text, markup = render_repo_message(repo, push_id=push_id)
    if push_type == "surge":
        text = "🔥 热度突发\n" + text

    try:
        sent = await bot_app.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            reply_markup=markup,
            disable_web_page_preview=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "push.send_failed",
            repo=repo.full_name,
            push_type=push_type,
            error=str(exc),
        )
        return None

    message_id = getattr(sent, "message_id", None)
    if message_id is not None:
        await update_pushed_tg_message_id(
            conn, push_id=push_id, tg_message_id=str(message_id)
        )
    log.info(
        "push.sent",
        repo=repo.full_name,
        push_type=push_type,
        push_id=push_id,
        tg_message_id=message_id,
    )
    return push_id
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_bot_push.py -v
```

Expected: **3 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **182 passed** (179 + 3).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/bot/push.py tests/unit/test_bot_push.py
git commit -m "feat(bot/push): push_repo helper wrapping insert+render+send+update_tg_id"
```

---

## Task 6: `pipeline/digest.py` — digest orchestrator

**Files:**
- Create: `src/monitor/pipeline/digest.py`
- Create: `tests/unit/test_pipeline_digest.py`

Context: Orchestrates the full collect → filter → enrich → score → push pipeline. Writes a `run_log` entry. Returns a stats dict. Takes explicit deps for testability — tests inject a fake GitHubClient + AsyncMock LLM + fake bot.

- [ ] **Step 1: Write failing test `tests/unit/test_pipeline_digest.py`**

```python
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
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_pipeline_digest.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.pipeline.digest'`.

- [ ] **Step 3: Write `src/monitor/pipeline/digest.py`**

```python
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

import aiosqlite
import structlog

from monitor.bot.push import push_repo
from monitor.db import (
    finish_run_log,
    start_run_log,
    upsert_repositories,
    upsert_repository_metrics,
)
from monitor.pipeline.collect import collect_candidates
from monitor.pipeline.enrich import enrich_repo
from monitor.pipeline.filter import apply_filters
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run_digest(
    *,
    push_type: Literal["digest", "surge"] = "digest",
    github_client: Any,
    llm_score_fn: Any,
    rule_engine: RuleEngine,
    state: DaemonState,
    conn: aiosqlite.Connection,
    bot_app: Any,
    chat_id: str,
    now: dt.datetime | None = None,
) -> dict:
    """Collect → filter → enrich → score → push. Writes a run_log entry.

    push_type="digest" is used for scheduled morning/evening runs and
    /digest_now. Surge has its own slimmer path (`pipeline/surge.py`) that
    reuses push_repo but skips the collect+filter stages.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if state.paused:
        log.info("digest.skipped_paused", push_type=push_type)
        return {"skipped": "paused"}

    run_id = await start_run_log(conn, kind=f"digest_{push_type}", now=now)
    stats: dict = {
        "repos_scanned": 0,
        "repos_pushed": 0,
        "llm_calls": 0,
        "enrich_errors": [],
        "fatal_error": None,
    }
    status: Literal["ok", "partial", "failed"] = "ok"

    try:
        candidates = await collect_candidates(
            github_client,
            keywords=list(state.config.keywords),
            languages=list(state.config.languages),
            min_stars=state.config.min_stars,
        )
        stats["repos_scanned"] = len(candidates)

        if candidates:
            await upsert_repositories(conn, candidates, now=now)

        survivors = await apply_filters(
            candidates,
            rule_engine=rule_engine,
            conn=conn,
            digest_cooldown_days=state.config.digest_cooldown_days,
            now=now,
        )

        top_n = state.config.top_n
        for repo in survivors[:top_n]:
            errors = await enrich_repo(github_client, repo)
            if errors:
                stats["enrich_errors"].extend(e.step for e in errors)
                status = "partial"
            await upsert_repository_metrics(conn, repo, now=now)

            await score_repo(
                repo,
                config=state.config,
                rule_engine=rule_engine,
                llm_score_fn=llm_score_fn,
                conn=conn,
            )
            stats["llm_calls"] += 1

            pushed_id = await push_repo(
                repo,
                bot_app=bot_app,
                chat_id=chat_id,
                conn=conn,
                push_type=push_type,
            )
            if pushed_id is not None:
                stats["repos_pushed"] += 1
    except Exception as exc:  # noqa: BLE001 - log + persist before re-raising? No: we persist status=failed and return stats
        log.exception("digest.fatal", push_type=push_type)
        stats["fatal_error"] = str(exc)
        status = "failed"
    finally:
        await finish_run_log(
            conn, run_id=run_id, status=status, stats=stats, now=dt.datetime.now(dt.timezone.utc)
        )
    return stats
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_pipeline_digest.py -v
```

Expected: **3 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **185 passed** (182 + 3).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/pipeline/digest.py tests/unit/test_pipeline_digest.py
git commit -m "feat(pipeline/digest): run_digest orchestrator with run_log + top_n + pause guard"
```

---

## Task 7: `pipeline/surge.py` — re-surface hot repos

**Files:**
- Create: `src/monitor/pipeline/surge.py`
- Create: `tests/unit/test_pipeline_surge.py`

Context: `run_surge` pulls already-known candidates from `repositories` (cooldown-expired) and re-fetches only events velocity. If `day_velocity_now / day_velocity_last >= velocity_multiple` AND `day_velocity_now >= velocity_absolute_day`, trigger full enrich → score → push with `push_type="surge"`.

- [ ] **Step 1: Write failing test**

```python
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

    async def fetch_repo_events(self, full_name: str):
        return self.events_results.get(full_name, (0.0, 0.0))

    async def fetch_contributors_growth(self, full_name: str):
        return self.contributors_results.get(full_name, (5, 1))

    async def fetch_issue_response_hours(self, full_name: str):
        return self.issues_results.get(full_name, 12.0)

    async def fetch_readme(self, full_name: str):
        return self.readme_results.get(full_name, "# title\n## install")


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
    """Past metric day_velocity=2; new day_velocity=10 (5x, absolute 10 > 20 no — wait — let's make it 25 vs 5).
    Thresholds: multiple=3, absolute=20. So we need new >= 3*old AND new >= 20."""
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 18, 12, 0, tzinfo=dt.timezone.utc)

    repo = _repo(star_velocity_day=5.0)
    await upsert_repositories(conn, [repo], now=now - dt.timedelta(hours=12))
    await upsert_repository_metrics(conn, repo, now=now - dt.timedelta(hours=12))

    client = FakeClient()
    client.events_results = {repo.full_name: (25.0, 10.0)}  # surge: new=25, old=5 → 5x, >20

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
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_pipeline_surge.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.pipeline.surge'`.

- [ ] **Step 3: Write `src/monitor/pipeline/surge.py`**

```python
from __future__ import annotations

import datetime as dt
from typing import Any

import aiosqlite
import structlog

from monitor.bot.push import push_repo
from monitor.db import (
    finish_run_log,
    get_latest_metric,
    get_surge_candidates,
    start_run_log,
    upsert_repository_metrics,
)
from monitor.models import RepoCandidate
from monitor.pipeline.enrich import enrich_repo
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run_surge(
    *,
    github_client: Any,
    llm_score_fn: Any,
    rule_engine: RuleEngine,
    state: DaemonState,
    conn: aiosqlite.Connection,
    bot_app: Any,
    chat_id: str,
    now: dt.datetime | None = None,
) -> dict:
    """Scan known repositories (cooldown expired) for velocity surges.

    For each candidate: fetch events (one API call), compare to the last
    metrics row, and if day_velocity * surge.velocity_multiple crossed
    AND surge.velocity_absolute_day is exceeded → enrich + score + push
    with the surge tag."""
    now = now or dt.datetime.now(dt.timezone.utc)

    if state.paused:
        log.info("surge.skipped_paused")
        return {"skipped": "paused"}

    run_id = await start_run_log(conn, kind="surge", now=now)
    stats: dict = {"candidates": 0, "surged": 0, "errors": []}
    status: str = "ok"

    try:
        surge_cfg = state.config.surge
        candidates = await get_surge_candidates(
            conn, now=now, cooldown_days=surge_cfg.cooldown_days
        )
        stats["candidates"] = len(candidates)

        for cand in candidates:
            full_name = cand["full_name"]
            try:
                day_v_new, week_v_new = await github_client.fetch_repo_events(full_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("surge.events_failed", repo=full_name, error=str(exc))
                stats["errors"].append(full_name)
                continue

            latest = await get_latest_metric(conn, full_name)
            day_v_old = (latest or {}).get("star_velocity_day") or 0.0

            # Multiplier threshold: avoid division by zero by treating old=0
            # as "no baseline" and requiring the absolute threshold only.
            if day_v_old > 0:
                ratio_ok = day_v_new >= day_v_old * surge_cfg.velocity_multiple
            else:
                ratio_ok = True
            absolute_ok = day_v_new >= surge_cfg.velocity_absolute_day

            if not (ratio_ok and absolute_ok):
                continue

            # Reconstitute RepoCandidate from the repositories row.
            repo = RepoCandidate(
                full_name=full_name,
                html_url=cand["html_url"] or f"https://github.com/{full_name}",
                description=cand["description"],
                language=cand["language"],
                stars=0,  # enrich does not refresh stars; carry 0 is fine for scoring
                forks=0,
                created_at=_parse_iso_utc(cand["created_at"]) or now,
                pushed_at=now,
                owner_login=cand["owner_login"],
                topics=list(cand["topics"]),
                star_velocity_day=day_v_new,
                star_velocity_week=week_v_new,
            )
            errors = await enrich_repo(github_client, repo)
            if errors:
                stats["errors"].extend(e.step for e in errors)
                status = "partial"
            await upsert_repository_metrics(conn, repo, now=now)
            await score_repo(
                repo,
                config=state.config,
                rule_engine=rule_engine,
                llm_score_fn=llm_score_fn,
                conn=conn,
            )
            pushed_id = await push_repo(
                repo, bot_app=bot_app, chat_id=chat_id, conn=conn, push_type="surge"
            )
            if pushed_id is not None:
                stats["surged"] += 1
    except Exception as exc:  # noqa: BLE001
        log.exception("surge.fatal")
        stats["fatal_error"] = str(exc)
        status = "failed"
    finally:
        await finish_run_log(
            conn, run_id=run_id, status=status, stats=stats,
            now=dt.datetime.now(dt.timezone.utc),
        )
    return stats


def _parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_pipeline_surge.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **189 passed** (185 + 4).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/pipeline/surge.py tests/unit/test_pipeline_surge.py
git commit -m "feat(pipeline/surge): run_surge detector for velocity-breaking repos"
```

---

## Task 8: `pipeline/weekly.py` — weekly digest text

**Files:**
- Create: `src/monitor/pipeline/weekly.py`
- Create: `tests/unit/test_pipeline_weekly.py`

Context: Pure SQL aggregation + string formatting. No LLM calls, no network. Returns a text string ready to pass into `bot.send_message`.

- [ ] **Step 1: Write failing test**

```python
import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    insert_pushed_item,
    put_preference_profile,
    record_user_feedback,
    run_migrations,
)
from monitor.models import RepoCandidate
from monitor.pipeline.weekly import build_weekly_digest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "weekly.db"


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
        summary=f"s {name}",
        recommendation_reason="r",
    )


async def test_weekly_digest_aggregates_counts_and_profile(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 20, 21, 0, tzinfo=dt.timezone.utc)  # Sunday

    # 4 pushes within the last week
    for i, (name, score) in enumerate([("a/p1", 9.0), ("b/p2", 8.5), ("c/p3", 7.0), ("d/p4", 6.0)]):
        push_at = now - dt.timedelta(days=i)
        await insert_pushed_item(
            conn, repo=_repo(name, score), push_type="digest",
            tg_chat_id="1", now=push_at,
        )
    # Also one old push (>7d ago) — should NOT be counted
    await insert_pushed_item(
        conn, repo=_repo("z/old", 5.0), push_type="digest",
        tg_chat_id="1", now=now - dt.timedelta(days=10),
    )

    # Feedback
    async with conn.execute("SELECT id FROM pushed_items WHERE full_name='a/p1'") as cur:
        push_id = (await cur.fetchone())[0]
    for action in ["like", "like", "dislike"]:
        await record_user_feedback(
            conn, push_id=push_id, action=action, repo_snapshot={},
            now=now - dt.timedelta(hours=1),
        )

    # Preference profile
    await put_preference_profile(
        conn, profile_text="用户偏好 AI agent 框架和 Rust 工具",
        generated_at=now - dt.timedelta(hours=2), based_on_feedback_count=3,
    )

    text = await build_weekly_digest(conn, now=now)

    assert "本周摘要" in text
    # 4 pushes in the last week (not 5)
    assert "4" in text
    # 2 likes
    assert "👍 2" in text or "like" in text.lower()
    # 1 dislike
    assert "👎 1" in text or "dislike" in text.lower()
    # Preference profile text is included
    assert "AI agent 框架" in text
    # Top 3 pushed repos by score are in there
    assert "a/p1" in text
    assert "b/p2" in text
    # The old push is NOT included
    assert "z/old" not in text
    await conn.close()


async def test_weekly_digest_without_data_renders_empty_state(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 20, 21, 0, tzinfo=dt.timezone.utc)

    text = await build_weekly_digest(conn, now=now)
    # Must not crash; renders a minimal summary
    assert "本周摘要" in text
    assert "0" in text  # zero counts
    await conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_pipeline_weekly.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.pipeline.weekly'`.

- [ ] **Step 3: Write `src/monitor/pipeline/weekly.py`**

```python
from __future__ import annotations

import datetime as dt

import aiosqlite

from monitor.db import (
    get_feedback_counts_since,
    get_latest_run_logs,
    get_preference_profile,
    get_pushed_since,
)


async def build_weekly_digest(
    conn: aiosqlite.Connection,
    *,
    now: dt.datetime | None = None,
    window_days: int = 7,
) -> str:
    """Aggregate the last `window_days` of activity into a text block for
    the Sunday weekly digest push."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=window_days)

    pushes = await get_pushed_since(conn, since=since)
    feedback = await get_feedback_counts_since(conn, since=since)
    profile = await get_preference_profile(conn)
    runs = await get_latest_run_logs(conn, limit=50)
    recent_runs = [r for r in runs if r.get("started_at") and r["started_at"] >= since.isoformat()]

    # Week label: ISO year-week
    iso = now.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"

    lines = [f"📊 本周摘要 ({week_label})"]

    # Pushes + feedback headline
    like_count = feedback.get("like", 0)
    dislike_count = feedback.get("dislike", 0)
    lines.append(
        f"🔥 新推送 {len(pushes)}，你 👍 {like_count} / 👎 {dislike_count}"
    )

    # Top 3 by final_score
    if pushes:
        top3 = sorted(pushes, key=lambda p: p["final_score"], reverse=True)[:3]
        lines.append("📈 本周推送 Top 3:")
        for i, p in enumerate(top3, 1):
            lines.append(
                f"  {i}. {p['full_name']}  {p['final_score']:.2f}/10"
            )

    # Preference profile
    if profile and profile.get("profile_text"):
        count = profile.get("based_on_feedback_count") or 0
        lines.append("")
        lines.append(f"🎯 兴趣画像（基于 {count} 条反馈）")
        lines.append(profile["profile_text"])

    # Run statistics
    if recent_runs:
        ok_count = sum(1 for r in recent_runs if r.get("status") == "ok")
        failed_count = sum(1 for r in recent_runs if r.get("status") == "failed")
        surge_count = sum(1 for r in recent_runs if (r.get("kind") or "").startswith("surge"))
        lines.append("")
        lines.append("📋 运行统计")
        lines.append(
            f"  digest {ok_count}/{len(recent_runs) - surge_count}，"
            f"surge {surge_count} 次，失败 {failed_count}"
        )

    return "\n".join(lines)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_pipeline_weekly.py -v
```

Expected: **2 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **191 passed** (189 + 2).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/pipeline/weekly.py tests/unit/test_pipeline_weekly.py
git commit -m "feat(pipeline/weekly): build_weekly_digest text aggregator"
```

---

## Task 9: `/digest_now` command

**Files:**
- Modify: `src/monitor/bot/commands.py`
- Modify: `src/monitor/bot/app.py`
- Create: `tests/unit/test_bot_commands_digest_now.py`

Context: The handler takes a `digest_trigger` callable injected by `create_application`. The callable (bound in `main.py` to `run_digest` with all its deps) is awaited under `state.digest_lock` so concurrent triggers fall back to "already running" reply.

- [ ] **Step 1: Write failing test**

```python
import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.bot.commands import handle_digest_now
from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "dn.db"


def _fake_update() -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock())
    )


async def _make_state(tmp_db: Path) -> DaemonState:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    return await DaemonState.load(conn=conn, config=ConfigFile())


async def test_digest_now_runs_when_lock_free(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)
    trigger = AsyncMock(return_value={"repos_pushed": 3, "repos_scanned": 10})

    update = _fake_update()
    await handle_digest_now(update, state=state, digest_trigger=trigger)

    trigger.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "3" in reply and "10" in reply  # stats surfaced
    await state.conn.close()


async def test_digest_now_reports_already_running(tmp_db: Path) -> None:
    """If another digest is in-flight (lock held), the command replies
    with a 'busy' message rather than queueing."""
    state = await _make_state(tmp_db)

    slow_trigger_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_trigger():
        slow_trigger_started.set()
        await release.wait()
        return {"repos_pushed": 0, "repos_scanned": 0}

    update1 = _fake_update()
    update2 = _fake_update()

    task1 = asyncio.create_task(
        handle_digest_now(update1, state=state, digest_trigger=slow_trigger)
    )
    await slow_trigger_started.wait()
    # Now the lock is held; second call should bail immediately
    await handle_digest_now(update2, state=state, digest_trigger=AsyncMock())
    reply2 = update2.message.reply_text.await_args.args[0]
    assert "already running" in reply2.lower() or "运行中" in reply2

    release.set()
    await task1
    await state.conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_bot_commands_digest_now.py -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'handle_digest_now'`.

- [ ] **Step 3: Add handler to `src/monitor/bot/commands.py`**

Append to the end of `src/monitor/bot/commands.py`:

```python


DigestTrigger = Callable[[], Awaitable[dict]]


async def handle_digest_now(
    update: Any,
    *,
    state: DaemonState,
    digest_trigger: DigestTrigger,
) -> None:
    """Trigger an immediate digest run. Non-reentrant via state.digest_lock:
    if another digest is in-flight, reply 'busy' rather than queue."""
    lock = state.digest_lock
    if lock.locked():
        await update.message.reply_text("⏳ digest already running — try again later.")
        return
    async with lock:
        try:
            stats = await digest_trigger()
        except Exception as exc:  # noqa: BLE001
            log.exception("digest_now.failed")
            await update.message.reply_text(f"❌ digest_now 失败: {exc}")
            return
    pushed = stats.get("repos_pushed", 0)
    scanned = stats.get("repos_scanned", 0)
    await update.message.reply_text(
        f"✅ digest_now 完成：扫描 {scanned}，推送 {pushed}"
    )
```

- [ ] **Step 4: Register handler in `src/monitor/bot/app.py`**

Add `digest_trigger` to `create_application`'s signature. After the existing 5 `CommandHandler` registrations, add one more:

Open `src/monitor/bot/app.py`. Modify the `create_application` signature to add a `digest_trigger` keyword arg:

```python
def create_application(
    *,
    token: str,
    chat_id: str,
    conn: aiosqlite.Connection,
    state: DaemonState,
    pref_builder: Any,
    refresh_threshold: int,
    config_reloader: ConfigReloader,
    digest_trigger: Any,
) -> Application:
```

Right after the existing `"reload"` CommandHandler registration (before the feedback CallbackQueryHandler), add:

```python
    app.add_handler(
        CommandHandler(
            "digest_now",
            _wrap(
                lambda update, _ctx: commands.handle_digest_now(
                    update, state=state, digest_trigger=digest_trigger
                )
            ),
            filters=chat_filter,
        )
    )
```

- [ ] **Step 5: Update test_bot_app.py to pass digest_trigger**

Open `/Users/Zhuanz/Documents/GithubRepoMonitor/tests/unit/test_bot_app.py`. Find the two calls to `create_application` (in `test_create_application_registers_five_commands` and `test_create_application_registers_callback_query_handler`). Each currently passes 7 kwargs; add `digest_trigger=AsyncMock()`:

The `test_create_application_registers_five_commands` assertion about the command set will also need updating from 5 to 6:

Change `assert sorted(command_names) == ["pause", "reload", "resume", "status", "top"]` to:
`assert sorted(command_names) == ["digest_now", "pause", "reload", "resume", "status", "top"]`

Rename the test appropriately from `...five_commands` to `...six_commands` (rename both test function and its file assertion to match).

- [ ] **Step 6: Tests pass**

```bash
pytest tests/unit/test_bot_commands_digest_now.py tests/unit/test_bot_app.py -v
```

Expected: 2 (digest_now) + 2 (app) = **4 passed**.

- [ ] **Step 7: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **193 passed** (191 + 2 new, -0 existing since 2 test_bot_app tests were modified in place).

- [ ] **Step 8: Commit**

```bash
git add src/monitor/bot/commands.py src/monitor/bot/app.py \
        tests/unit/test_bot_commands_digest_now.py tests/unit/test_bot_app.py
git commit -m "feat(bot): /digest_now command with lock-guarded non-reentrance"
```

---

## Task 10: `scheduler.py` — four APScheduler jobs

**Files:**
- Create: `src/monitor/scheduler.py`
- Create: `tests/unit/test_scheduler.py`

Context: `create_scheduler(deps) -> AsyncIOScheduler` mounts 4 jobs. Each job wraps the underlying coroutine (`run_digest` / `run_surge` / `build_weekly_digest+send`) under `state.digest_lock`. `start_scheduler(scheduler)` calls `.start()`. `stop_scheduler(scheduler)` calls `.shutdown(wait=False)`.

- [ ] **Step 1: Write failing test**

```python
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.scheduler import create_scheduler
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "sched.db"


async def _make_state(tmp_db: Path) -> DaemonState:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    return await DaemonState.load(conn=conn, config=ConfigFile())


async def test_create_scheduler_mounts_four_jobs(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)

    scheduler = create_scheduler(
        state=state,
        conn=state.conn,
        digest_callable=AsyncMock(return_value={}),
        surge_callable=AsyncMock(return_value={}),
        weekly_send_callable=AsyncMock(return_value=None),
    )
    assert isinstance(scheduler, AsyncIOScheduler)

    job_ids = sorted(j.id for j in scheduler.get_jobs())
    assert job_ids == ["digest_evening", "digest_morning", "surge_poll", "weekly_digest"]
    await state.conn.close()


async def test_scheduler_respects_config_times(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)

    scheduler = create_scheduler(
        state=state,
        conn=state.conn,
        digest_callable=AsyncMock(return_value={}),
        surge_callable=AsyncMock(return_value={}),
        weekly_send_callable=AsyncMock(return_value=None),
    )

    jobs = {j.id: j for j in scheduler.get_jobs()}
    # Morning digest: hour=8
    morning_trigger = jobs["digest_morning"].trigger
    assert str(morning_trigger.fields[morning_trigger.FIELD_NAMES.index("hour")]) == "8"
    # Evening digest: hour=20
    evening_trigger = jobs["digest_evening"].trigger
    assert str(evening_trigger.fields[evening_trigger.FIELD_NAMES.index("hour")]) == "20"
    await state.conn.close()
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_scheduler.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.scheduler'`.

- [ ] **Step 3: Write `src/monitor/scheduler.py`**

```python
from __future__ import annotations

from typing import Any, Awaitable, Callable

import aiosqlite
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from monitor.state import DaemonState


log = structlog.get_logger(__name__)

DigestCallable = Callable[[], Awaitable[dict]]
SurgeCallable = Callable[[], Awaitable[dict]]
WeeklySendCallable = Callable[[], Awaitable[None]]


def create_scheduler(
    *,
    state: DaemonState,
    conn: aiosqlite.Connection,
    digest_callable: DigestCallable,
    surge_callable: SurgeCallable,
    weekly_send_callable: WeeklySendCallable,
) -> AsyncIOScheduler:
    """Mount 4 scheduled jobs. Each job is lock-guarded to prevent
    overlapping runs. `digest_callable` / `surge_callable` /
    `weekly_send_callable` are pre-bound partials that know about the
    deps (github_client, llm_score_fn, etc.) without the scheduler
    itself needing to."""
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def _guarded_digest() -> None:
        if state.digest_lock.locked():
            log.info("scheduler.digest_skipped_lock_held")
            return
        async with state.digest_lock:
            try:
                await digest_callable()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.digest_raised")

    async def _guarded_surge() -> None:
        if state.digest_lock.locked():
            log.info("scheduler.surge_skipped_lock_held")
            return
        async with state.digest_lock:
            try:
                await surge_callable()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.surge_raised")

    async def _guarded_weekly() -> None:
        if state.digest_lock.locked():
            log.info("scheduler.weekly_skipped_lock_held")
            return
        async with state.digest_lock:
            try:
                await weekly_send_callable()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.weekly_raised")

    scheduler.add_job(
        _guarded_digest,
        CronTrigger(hour=8, minute=0),
        id="digest_morning",
        name="Morning digest",
        max_instances=1,
    )
    scheduler.add_job(
        _guarded_digest,
        CronTrigger(hour=20, minute=0),
        id="digest_evening",
        name="Evening digest",
        max_instances=1,
    )
    scheduler.add_job(
        _guarded_surge,
        IntervalTrigger(minutes=30),
        id="surge_poll",
        name="Surge poll",
        max_instances=1,
    )
    scheduler.add_job(
        _guarded_weekly,
        CronTrigger(day_of_week="sun", hour=21, minute=0),
        id="weekly_digest",
        name="Weekly digest",
        max_instances=1,
    )
    return scheduler


async def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.start()


async def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.shutdown(wait=False)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_scheduler.py -v
```

Expected: **2 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **195 passed** (193 + 2).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/scheduler.py tests/unit/test_scheduler.py
git commit -m "feat(scheduler): APScheduler with 4 lock-guarded jobs (digest/surge/weekly)"
```

---

## Task 11: `main.py` — wire scheduler into lifecycle

**Files:**
- Modify: `src/monitor/main.py`
- Modify: `tests/integration/test_main_lifecycle.py`

Context: `main.run()` now also constructs the scheduler + its deps, starts it alongside the bot, and shuts it down in `finally` before the bot. Without TG credentials the scheduler does NOT start (because pushing would have no destination).

- [ ] **Step 1: Extend integration test**

Append to `tests/integration/test_main_lifecycle.py`:

```python


async def test_main_logs_scheduler_disabled_when_no_telegram_credentials(tmp_path: Path) -> None:
    """Without TG creds the scheduler has no destination, so both the bot
    AND the scheduler are skipped. We already assert telegram.disabled in
    the prior test; this one asserts the scheduler follows."""
    proc = await _start_process(tmp_path)
    try:
        scheduler_disabled_seen = False
        for _ in range(50):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            if not line:
                break
            if b"scheduler.disabled" in line:
                scheduler_disabled_seen = True
                break
        assert scheduler_disabled_seen, "daemon should log scheduler.disabled when TG is disabled"
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
```

- [ ] **Step 2: Run — expect 2 of 3 lifecycle tests pass, new one fails**

```bash
pytest tests/integration/test_main_lifecycle.py -v 2>&1 | tail -10
```

Expected: 2 pass + 1 fail (new `scheduler.disabled` assertion).

- [ ] **Step 3: Rewrite `src/monitor/main.py`**

Replace the full contents of `/Users/Zhuanz/Documents/GithubRepoMonitor/src/monitor/main.py` with:

```python
from __future__ import annotations

import asyncio
import datetime as dt
import json
import signal
import sys
import traceback
from pathlib import Path

import structlog

from monitor.bot.app import create_application
from monitor.clients.github import GitHubClient
from monitor.clients.llm import LLMClient
from monitor.config import ConfigFile, Settings, load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging
from monitor.pipeline.digest import run_digest
from monitor.pipeline.surge import run_surge
from monitor.pipeline.weekly import build_weekly_digest
from monitor.scheduler import (
    create_scheduler,
    start_scheduler,
    stop_scheduler,
)
from monitor.scoring.preference import PreferenceBuilder
from monitor.scoring.rules import RuleEngine
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run() -> int:
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
    scheduler = None
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
        bot_app = await _maybe_start_bot_and_scheduler(
            settings, state, conn, stop,
        )
        # _maybe_start_bot_and_scheduler returns the tuple via a closure-like
        # pattern — see the helper for why this approach is simpler than
        # threading two separate return values through try/finally.
        if isinstance(bot_app, tuple):
            bot_app, scheduler = bot_app

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        if scheduler is not None:
            try:
                await stop_scheduler(scheduler)
            except Exception:  # noqa: BLE001
                log.exception("shutdown.scheduler_stop_failed")
        if bot_app is not None:
            for step in (bot_app.updater.stop, bot_app.stop, bot_app.shutdown):
                try:
                    await step()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown.bot_step_failed", step=step.__name__)
        await conn.close()
        log.info("shutdown.done")
    return 0


async def _maybe_start_bot_and_scheduler(
    settings: Settings,
    state: DaemonState,
    conn,
    stop: asyncio.Event,
):
    """Returns (bot_app, scheduler) tuple or (None, None)."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("telegram.disabled", reason="missing_credentials")
        log.info("scheduler.disabled", reason="telegram_disabled")
        return (None, None)

    llm_client = _build_llm_client(settings, state.config)

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

    # github_client is async context manager — enter it for the daemon lifetime.
    gh_client = GitHubClient(
        token=settings.github_token, request_timeout_s=20.0,
    )
    await gh_client.__aenter__()

    # `bot_app` is created below, but both /digest_now and the scheduler's
    # digest callable need to reference it. The trigger closes over a
    # mutable holder dict; we populate the dict *after* create_application
    # returns, so by the time any trigger fires the `app` key is live.
    bot_app_holder: dict = {"app": None}

    async def digest_trigger() -> dict:
        return await run_digest(
            push_type="digest",
            github_client=gh_client,
            llm_score_fn=(llm_client.score_repo if llm_client else _no_llm_score),
            rule_engine=RuleEngine(state.config),
            state=state,
            conn=conn,
            bot_app=bot_app_holder["app"],
            chat_id=settings.telegram_chat_id,
        )

    bot_app = create_application(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        conn=conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=state.config.preference_refresh_every,
        config_reloader=config_reloader,
        digest_trigger=digest_trigger,
    )
    bot_app_holder["app"] = bot_app  # now trigger's closure can see it

    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
    except Exception:
        log.exception("telegram.start_failed")
        try:
            await bot_app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            await gh_client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        return (None, None)

    log.info("telegram.started", chat_id=settings.telegram_chat_id)

    async def surge_callable() -> dict:
        return await run_surge(
            github_client=gh_client,
            llm_score_fn=(llm_client.score_repo if llm_client else _no_llm_score),
            rule_engine=RuleEngine(state.config),
            state=state,
            conn=conn,
            bot_app=bot_app,
            chat_id=settings.telegram_chat_id,
        )

    async def weekly_send_callable() -> None:
        text = await build_weekly_digest(conn)
        try:
            await bot_app.bot.send_message(
                chat_id=int(settings.telegram_chat_id), text=text,
            )
        except Exception:  # noqa: BLE001
            log.exception("weekly.send_failed")

    scheduler = create_scheduler(
        state=state,
        conn=conn,
        digest_callable=digest_trigger,
        surge_callable=surge_callable,
        weekly_send_callable=weekly_send_callable,
    )
    await start_scheduler(scheduler)
    log.info("scheduler.started")

    return (bot_app, scheduler)


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


async def _no_llm_score(*_a, **_k):
    from monitor.scoring.types import LLMScoreError
    raise LLMScoreError("LLM not configured", cause="missing_key")


def cli() -> None:
    sys.exit(asyncio.run(run()))
```

**Design note on the holder dict:** `bot_app` is built *after* `create_application` — but `create_application` needs a `digest_trigger` callable for `/digest_now`, AND the scheduler needs the same callable with access to `bot_app`. Rather than building two separate triggers or mutating handler state, we pass a mutable `bot_app_holder = {"app": None}` dict. The trigger closes over it; after `bot_app` is built we set `bot_app_holder["app"] = bot_app`. Both `/digest_now` and the scheduler's digest job read the live value on every invocation.

- [ ] **Step 4: Tests pass**

```bash
pytest tests/integration/test_main_lifecycle.py -v
```

Expected: **3 passed**.

- [ ] **Step 5: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **196 passed** (195 + 1 new integration test).

- [ ] **Step 6: Commit**

```bash
git add src/monitor/main.py tests/integration/test_main_lifecycle.py
git commit -m "feat(main): wire scheduler into daemon lifecycle"
```

---

## Task 12: Delete `monitor.legacy`

**Files:**
- Delete: `src/monitor/legacy.py`
- Delete: `tests/test_monitor.py`

Context: The new pipeline (M2 client + M3 scoring + M4 bot + M5 scheduler) now fully replaces legacy's role. Its 4 remaining tests (`test_monitor.py`) only verify legacy behavior. Delete both; README's "legacy runnable" mention goes stale but can be cleaned up in the final CLAUDE.md task.

- [ ] **Step 1: Delete files**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
git rm src/monitor/legacy.py tests/test_monitor.py
```

- [ ] **Step 2: Full suite**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: **192 passed** (196 - 4 legacy tests).

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: delete monitor.legacy — fully replaced by M2-M5 pipeline"
```

---

## Task 13: CLAUDE.md M5 additions

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append subsection**

Locate the final paragraph of `### M4 additions` in `/Users/Zhuanz/Documents/GithubRepoMonitor/CLAUDE.md`. Append at the END of the file (one blank line between sections):

```markdown

### M5 additions

`src/monitor/scheduler.py` hosts the `AsyncIOScheduler` with four jobs (`digest_morning` @ 08:00, `digest_evening` @ 20:00, `surge_poll` every 30 min, `weekly_digest` Sunday 21:00). Each job is guarded by `DaemonState.digest_lock` so two jobs (or `/digest_now`) cannot overlap; an overlapping trigger logs "skipped" and exits.

`src/monitor/pipeline/` grows three orchestrators: `digest.py` (`run_digest` collect→filter→enrich→score→push with `run_log` + top_n + pause guard), `surge.py` (`run_surge` re-surfaces cooldown-expired repos whose events velocity crossed the `surge.velocity_multiple × previous` AND `surge.velocity_absolute_day` thresholds), and `weekly.py` (pure SQL aggregate of pushed_items + user_feedback + run_log + preference_profile into a text block for the Sunday push).

`src/monitor/pipeline/filter.py` is the coarse filter stage used by `run_digest`: rule engine + blacklist (repo/author/topic) + pushed_cooldown_state, all in one pass before enrichment.

`src/monitor/bot/push.py` centralizes the push send flow (`insert_pushed_item` → `render_repo_message` → `bot.send_message` → `update_pushed_tg_message_id`). Called by both `run_digest` and `run_surge`; surge adds a 🔥 prefix.

`src/monitor/bot/commands.py` grows `/digest_now` — it attempts to acquire `state.digest_lock` and replies busy if held (no queueing).

DB: migration 002 was from M4. M5 adds no schema changes, only DAOs: `start_run_log` / `finish_run_log` for run accounting; `upsert_repositories` + `upsert_repository_metrics` for post-enrich persistence; `get_latest_metric` + `get_surge_candidates` for surge; `get_pushed_since` + `get_feedback_counts_since` for the weekly aggregate.

`src/monitor/main.py` now opens a `GitHubClient` for the daemon lifetime, builds `LLMClient` (if keyed), constructs all four pre-bound scheduler callables, and installs the scheduler alongside the bot. Shutdown order is scheduler → bot → conn, each step individually guarded.

`monitor.legacy` is gone. The productized pipeline is the single entry. Legacy tests at `tests/test_monitor.py` were deleted in the same commit.

Operational note: the daemon uses `timezone="Asia/Shanghai"` for the scheduler. Morning digest at 08:00 Shanghai → 00:00 UTC; evening 20:00 Shanghai → 12:00 UTC.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: extend CLAUDE.md architecture for M5 scheduler + pipeline orchestrators"
```

---

## M5 Verification Criteria

- [x] `pytest tests/` — **~192 passed** (164 from M4 + ~32 new - 4 deleted legacy)
- [x] `src/monitor/scheduler.py` exists with `create_scheduler` / `start_scheduler` / `stop_scheduler`
- [x] `src/monitor/pipeline/` has `digest.py` / `surge.py` / `weekly.py` / `filter.py`
- [x] `src/monitor/bot/push.py` exists
- [x] `src/monitor/bot/commands.py` has `handle_digest_now`
- [x] `DaemonState.digest_lock` field
- [x] 8 new DAOs in `db.py`
- [x] `src/monitor/legacy.py` deleted
- [x] `tests/test_monitor.py` deleted
- [x] `python -m monitor` with TG creds → boots bot + scheduler
- [x] `python -m monitor` without TG creds → logs `telegram.disabled` + `scheduler.disabled`, exits on SIGTERM

## Out of Scope (M6)

- systemd service unit + healthcheck timer
- Log rotation (logrotate config)
- SQLite backup script (daily `.backup`)
- Live smoke test against real credentials
- Production deployment docs
