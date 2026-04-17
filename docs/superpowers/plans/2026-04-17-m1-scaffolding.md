# M1 脚手架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前单文件 demo 改造成带包结构、依赖管理、配置系统、SQLite 迁移框架和结构化日志的脚手架，为 M2-M6 做地基。结束时仍保留 demo 可跑，所有已有测试在新结构下绿。

**Architecture:** `src/monitor/` 成为唯一 Python 包，demo 临时降级为 `monitor.legacy`。Pydantic 管配置三层（defaults + JSON + env），`aiosqlite` + WAL 管数据库，线性编号迁移表控 schema，`structlog` 出 JSON 日志。`python -m monitor` 能启动一个空壳 daemon（开 DB、跑迁移、挂信号、`SIGTERM` 优雅退出），为 M2+ 填内容。

**Tech Stack:** Python 3.11+, `httpx`, `aiosqlite`, `pydantic` + `pydantic-settings`, `anthropic`, `python-telegram-bot` v21, `APScheduler`, `tenacity`, `structlog`. 测试 `pytest` + `pytest-asyncio` + `respx`。构建 `setuptools` src 布局。

---

## 背景与前置

- 当前状态：`src/github_repo_monitor.py` 单文件 ~580 行；`tests/test_monitor.py` 4 个 `unittest` 测试；无 `pyproject.toml`；纯 stdlib。
- 本地 Python：`/opt/homebrew/bin/python3`（3.14.2，符合 3.11+ 要求）。
- 完整设计：`docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`。

## 文件映射

**新增**
- `pyproject.toml` — 依赖、构建、脚本入口
- `src/monitor/__init__.py`
- `src/monitor/config.py` — pydantic Settings + ConfigFile + `load_config()`
- `src/monitor/db.py` — schema、migration runner、基础 DAO
- `src/monitor/logging_config.py` — structlog JSON 配置
- `src/monitor/main.py` — async 入口 + 生命周期
- `src/monitor/clients/__init__.py`（空占位，M2 填）
- `src/monitor/pipeline/__init__.py`（空占位，M2 填）
- `src/monitor/scoring/__init__.py`（空占位，M3 填）
- `src/monitor/bot/__init__.py`（空占位，M4 填）
- `tests/unit/__init__.py`
- `tests/unit/test_config.py`
- `tests/unit/test_db.py`
- `tests/unit/test_logging_config.py`
- `tests/integration/__init__.py`
- `tests/integration/test_main_lifecycle.py`

**移动**
- `src/github_repo_monitor.py` → `src/monitor/legacy.py`

**修改**
- `tests/test_monitor.py` — import 路径 `src.github_repo_monitor` → `monitor.legacy`
- `.gitignore` — 增加 `.venv/`、`*.egg-info/`、`dist/`、`build/`、`.pytest_cache/`
- `README.md` — 更新运行 / 测试命令

---

## Task 1: 添加 pyproject.toml 和依赖

**Files:**
- Create: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: 写 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "monitor"
version = "0.1.0"
description = "GitHub repo monitor - productionized"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "aiosqlite>=0.20",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "anthropic>=0.39",
    "python-telegram-bot>=21.0",
    "apscheduler>=3.10",
    "tenacity>=8.2",
    "structlog>=24.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
]

[project.scripts]
monitor = "monitor.main:cli"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: 更新 `.gitignore`**

追加到现有内容末尾：

```
.venv/
*.egg-info/
dist/
build/
.pytest_cache/
```

- [ ] **Step 3: 创建 venv 并装依赖**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected: 安装成功，`which python` 指向 `.venv/bin/python`。

- [ ] **Step 4: 确认 pytest 能发现现有测试**

```bash
source .venv/bin/activate
pytest tests/ --collect-only 2>&1 | head -20
```

Expected: 能发现现有 `tests/test_monitor.py` 的 4 个测试（此时会报 import 错误，正常，下一 task 修）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "chore: add pyproject.toml with project deps and dev tooling"
```

---

## Task 2: 把 demo 降级为 monitor.legacy

**Files:**
- Create: `src/monitor/__init__.py`
- Move: `src/github_repo_monitor.py` → `src/monitor/legacy.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: 创建 monitor 包**

```bash
mkdir -p src/monitor
touch src/monitor/__init__.py
```

- [ ] **Step 2: 移动 demo 文件（git mv 保留历史）**

```bash
git mv src/github_repo_monitor.py src/monitor/legacy.py
```

- [ ] **Step 3: 更新测试 import**

`tests/test_monitor.py` 顶部：

```python
from monitor.legacy import MonitorConfig, MonitorPipeline, RepoCandidate, RuleEngine, SQLiteStore, parse_dt
```

（只改这一行，其他测试代码不动。）

- [ ] **Step 4: 跑测试验证绿**

```bash
source .venv/bin/activate
pytest tests/test_monitor.py -v
```

Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
git add src/monitor/__init__.py src/monitor/legacy.py tests/test_monitor.py
git commit -m "refactor: move demo into monitor.legacy pending replacement"
```

---

## Task 3: 创建子包骨架

**Files:**
- Create: `src/monitor/clients/__init__.py`, `src/monitor/pipeline/__init__.py`, `src/monitor/scoring/__init__.py`, `src/monitor/bot/__init__.py`
- Create: `tests/unit/__init__.py`, `tests/integration/__init__.py`

- [ ] **Step 1: 建目录 + 空 `__init__.py`**

```bash
mkdir -p src/monitor/clients src/monitor/pipeline src/monitor/scoring src/monitor/bot
touch src/monitor/clients/__init__.py src/monitor/pipeline/__init__.py src/monitor/scoring/__init__.py src/monitor/bot/__init__.py

mkdir -p tests/unit tests/integration
touch tests/unit/__init__.py tests/integration/__init__.py
```

- [ ] **Step 2: 验证 import 能 resolve**

```bash
source .venv/bin/activate
python -c "import monitor.clients, monitor.pipeline, monitor.scoring, monitor.bot; print('ok')"
```

Expected: `ok`。

- [ ] **Step 3: Commit**

```bash
git add src/monitor/ tests/unit/__init__.py tests/integration/__init__.py
git commit -m "chore: add module skeleton for clients/pipeline/scoring/bot"
```

---

## Task 4: Config — 写失败的测试

**Files:**
- Create: `tests/unit/test_config.py`

- [ ] **Step 1: 写测试**

`tests/unit/test_config.py`:

```python
import json
from pathlib import Path

import pytest

from monitor.config import ConfigFile, Settings, load_config


def test_defaults_when_no_file_and_no_env(monkeypatch, tmp_path):
    for var in ("MONITOR_CONFIG", "MONITOR_DB_PATH", "GITHUB_TOKEN",
                "MINIMAX_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)

    settings, config = load_config()

    assert settings.github_token is None
    assert config.keywords == ["agent", "llm", "monitor", "tooling"]
    assert config.min_stars == 100
    assert config.weights.rule == 0.55
    assert config.weights.llm == 0.45


def test_config_file_overrides_defaults(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "keywords": ["rust"],
        "min_stars": 500,
        "surge": {"velocity_multiple": 5.0, "velocity_absolute_day": 50, "cooldown_days": 2},
    }), encoding="utf-8")
    monkeypatch.setenv("MONITOR_CONFIG", str(cfg_path))

    _, config = load_config()

    assert config.keywords == ["rust"]
    assert config.min_stars == 500
    assert config.surge.velocity_multiple == 5.0
    assert config.surge.cooldown_days == 2
    # unspecified fields stay at defaults
    assert config.max_repo_age_days == 180


def test_env_vars_populate_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("MINIMAX_API_KEY", "mx_y")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg_z")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    settings, _ = load_config()

    assert settings.github_token == "ghp_x"
    assert settings.minimax_api_key == "mx_y"
    assert settings.telegram_bot_token == "tg_z"
    assert settings.telegram_chat_id == "12345"


def test_missing_config_file_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITOR_CONFIG", str(tmp_path / "nope.json"))

    _, config = load_config()

    assert config.keywords == ["agent", "llm", "monitor", "tooling"]
```

- [ ] **Step 2: 跑测试验证失败**

```bash
source .venv/bin/activate
pytest tests/unit/test_config.py -v
```

Expected: 4 条 FAIL 或 collection error（`monitor.config` 还不存在）。

---

## Task 5: Config — 实现 pydantic 模型和 loader

**Files:**
- Create: `src/monitor/config.py`

- [ ] **Step 1: 写 `src/monitor/config.py`**

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScoringWeights(BaseModel):
    rule: float = 0.55
    llm: float = 0.45


class SurgeThresholds(BaseModel):
    velocity_multiple: float = 3.0
    velocity_absolute_day: float = 20.0
    cooldown_days: int = 3


class ConfigFile(BaseModel):
    """Contents of the JSON config file (pointed to by MONITOR_CONFIG)."""

    keywords: List[str] = Field(
        default_factory=lambda: ["agent", "llm", "monitor", "tooling"]
    )
    languages: List[str] = Field(
        default_factory=lambda: ["Python", "Rust", "Go"]
    )
    min_stars: int = 100
    max_repo_age_days: int = 180
    top_n: int = 10
    digest_cooldown_days: int = 14
    surge: SurgeThresholds = Field(default_factory=SurgeThresholds)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    llm_model: str = "minimax-m2"
    llm_base_url: str = "https://api.minimax.chat/anthropic/v1"
    preference_refresh_every: int = 5


class Settings(BaseSettings):
    """Runtime settings — paths and secrets from env vars."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    config_path: Optional[Path] = Field(default=None, alias="MONITOR_CONFIG")
    db_path: Path = Field(default=Path("monitor.db"), alias="MONITOR_DB_PATH")
    log_path: Optional[Path] = Field(default=None, alias="MONITOR_LOG_PATH")
    github_token: Optional[str] = Field(default=None, alias="GITHUB_TOKEN")
    minimax_api_key: Optional[str] = Field(default=None, alias="MINIMAX_API_KEY")
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, alias="TELEGRAM_CHAT_ID")


def load_config(settings: Settings | None = None) -> tuple[Settings, ConfigFile]:
    settings = settings or Settings()
    if settings.config_path and settings.config_path.exists():
        payload = json.loads(settings.config_path.read_text(encoding="utf-8"))
        config = ConfigFile.model_validate(payload)
    else:
        config = ConfigFile()
    return settings, config
```

- [ ] **Step 2: 跑测试通过**

```bash
source .venv/bin/activate
pytest tests/unit/test_config.py -v
```

Expected: 4 passed。

- [ ] **Step 3: Commit**

```bash
git add src/monitor/config.py tests/unit/test_config.py
git commit -m "feat(config): pydantic Settings + ConfigFile with three-layer loading"
```

---

## Task 6: DB — 写迁移的失败测试（基础版）

**Files:**
- Create: `tests/unit/test_db.py`

- [ ] **Step 1: 写测试**

`tests/unit/test_db.py`:

```python
import datetime as dt
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from monitor.db import connect, run_migrations, current_version, SCHEMA_VERSION


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
```

- [ ] **Step 2: 跑测试验证失败**

```bash
source .venv/bin/activate
pytest tests/unit/test_db.py -v
```

Expected: 5 条 FAIL / collection error（`monitor.db` 还没实现）。

---

## Task 7: DB — 实现 schema + connect + migration runner

**Files:**
- Create: `src/monitor/db.py`

- [ ] **Step 1: 写 `src/monitor/db.py`**

```python
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
        await conn.execute(
            """
            INSERT INTO pushed_items
                (full_name, pushed_at, push_type,
                 rule_score, llm_score, final_score,
                 summary, reason, tg_chat_id, tg_message_id)
            VALUES (?, ?, 'digest', 0.0, 0.0, ?, NULL, NULL, NULL, NULL)
            """,
            (row[0], row[1], float(row[2])),
        )
```

- [ ] **Step 2: 跑测试通过**

```bash
source .venv/bin/activate
pytest tests/unit/test_db.py -v
```

Expected: 5 passed。

- [ ] **Step 3: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db.py
git commit -m "feat(db): schema v1 with WAL, idempotent migration runner, legacy data copy"
```

---

## Task 8: DB — 基础 DAO 助手（cooldown + blacklist）

**Files:**
- Modify: `src/monitor/db.py`
- Modify: `tests/unit/test_db.py`

说明：M1 只加两个后面每个 milestone 都会用到、且能直接写测试的 DAO。其他 DAO 各 milestone 自行新增。

- [ ] **Step 1: 在 `tests/unit/test_db.py` 追加测试**

```python
from monitor.db import add_blacklist_entry, is_blacklisted, pushed_cooldown_state


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
```

- [ ] **Step 2: 跑测试验证失败**

```bash
pytest tests/unit/test_db.py::test_blacklist_add_and_check tests/unit/test_db.py::test_pushed_cooldown_state -v
```

Expected: ImportError / function not defined。

- [ ] **Step 3: 在 `src/monitor/db.py` 尾部追加 DAO 函数**

```python
import datetime as _dt
from typing import Literal


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
    """Returns True if the row was inserted; False if it already existed."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    async with conn.execute(
        "SELECT 1 FROM blacklist WHERE kind = ? AND value = ? LIMIT 1",
        (kind, value),
    ) as cur:
        if await cur.fetchone():
            return False
    await conn.execute(
        """
        INSERT INTO blacklist (kind, value, added_at, source, source_ref)
        VALUES (?, ?, ?, ?, ?)
        """,
        (kind, value, now.isoformat(), source, source_ref),
    )
    await conn.commit()
    return True


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
```

- [ ] **Step 4: 跑所有 db 测试**

```bash
pytest tests/unit/test_db.py -v
```

Expected: 7 passed（原 5 + 新 2）。

- [ ] **Step 5: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db.py
git commit -m "feat(db): blacklist + pushed cooldown DAO helpers"
```

---

## Task 9: 结构化日志配置 — 写失败的测试

**Files:**
- Create: `tests/unit/test_logging_config.py`

- [ ] **Step 1: 写测试**

`tests/unit/test_logging_config.py`:

```python
import json
import logging

import pytest
import structlog

from monitor.logging_config import configure_logging


@pytest.fixture(autouse=True)
def reset_structlog():
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_logs_emit_json_with_timestamp_and_level(capsys):
    configure_logging()
    log = structlog.get_logger("test")

    log.info("hello", foo="bar")

    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "no log line emitted"
    payload = json.loads(captured[-1])
    assert payload["event"] == "hello"
    assert payload["foo"] == "bar"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_secret_fields_are_masked(capsys):
    configure_logging()
    log = structlog.get_logger("test")

    log.info("startup", github_token="ghp_supersecret", unrelated="ok")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["github_token"] == "***"
    assert payload["unrelated"] == "ok"
```

- [ ] **Step 2: 跑测试验证失败**

```bash
pytest tests/unit/test_logging_config.py -v
```

Expected: collection error（`monitor.logging_config` 不存在）。

---

## Task 10: 结构化日志配置 — 实现

**Files:**
- Create: `src/monitor/logging_config.py`

- [ ] **Step 1: 写 `src/monitor/logging_config.py`**

```python
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable

import structlog


SECRET_FIELDS: frozenset[str] = frozenset({
    "github_token",
    "minimax_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "api_key",
    "authorization",
})


def _mask_secrets(_logger, _method, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in SECRET_FIELDS and event_dict[key] is not None:
            event_dict[key] = "***"
    return event_dict


def configure_logging(
    log_path: Path | None = None,
    *,
    level: str = "INFO",
) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mask_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
```

- [ ] **Step 2: 跑测试通过**

```bash
pytest tests/unit/test_logging_config.py -v
```

Expected: 2 passed。

- [ ] **Step 3: Commit**

```bash
git add src/monitor/logging_config.py tests/unit/test_logging_config.py
git commit -m "feat(logging): structlog JSON pipeline with secret masking"
```

---

## Task 11: Main 入口 — 写集成测试

**Files:**
- Create: `tests/integration/test_main_lifecycle.py`

- [ ] **Step 1: 写测试**

`tests/integration/test_main_lifecycle.py`:

```python
import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="signal handling differs on Windows"
)


async def _start_process(tmp_path: Path) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    env["MONITOR_DB_PATH"] = str(tmp_path / "mon.db")
    for key in ("MONITOR_CONFIG", "MONITOR_LOG_PATH", "GITHUB_TOKEN",
                "MINIMAX_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env.pop(key, None)
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "monitor",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def test_main_starts_runs_migrations_and_exits_on_sigterm(tmp_path: Path) -> None:
    proc = await _start_process(tmp_path)

    # Wait for the startup log line to ensure migrations have run.
    startup_seen = False
    for _ in range(50):
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
        if not line:
            break
        if b"migrations.applied" in line:
            startup_seen = True
            break
    assert startup_seen, "did not observe migrations.applied log line"

    proc.send_signal(signal.SIGTERM)
    rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    assert rc == 0

    assert (tmp_path / "mon.db").exists()
```

- [ ] **Step 2: 跑测试验证失败**

```bash
pytest tests/integration/test_main_lifecycle.py -v
```

Expected: FAIL（`monitor.main` 模块或 `__main__` 还不存在）。

---

## Task 12: Main 入口 — 实现装配 + 生命周期

**Files:**
- Create: `src/monitor/main.py`
- Create: `src/monitor/__main__.py`

- [ ] **Step 1: 写 `src/monitor/main.py`**

```python
from __future__ import annotations

import asyncio
import signal
import sys

import structlog

from monitor.config import load_config
from monitor.db import connect, run_migrations
from monitor.logging_config import configure_logging


log = structlog.get_logger(__name__)


async def run() -> int:
    settings, config = load_config()
    configure_logging(settings.log_path)
    log.info(
        "startup",
        db_path=str(settings.db_path),
        keywords=config.keywords,
        languages=config.languages,
    )

    conn = await connect(settings.db_path)
    try:
        applied = await run_migrations(conn)
        log.info("migrations.applied", count=applied)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        log.info("ready")
        await stop.wait()
    finally:
        log.info("shutdown.begin")
        await conn.close()
        log.info("shutdown.done")
    return 0


def cli() -> None:
    sys.exit(asyncio.run(run()))
```

- [ ] **Step 2: 写 `src/monitor/__main__.py`**

```python
from monitor.main import cli


if __name__ == "__main__":
    cli()
```

- [ ] **Step 3: 跑集成测试通过**

```bash
pytest tests/integration/test_main_lifecycle.py -v
```

Expected: 1 passed。

- [ ] **Step 4: Commit**

```bash
git add src/monitor/main.py src/monitor/__main__.py tests/integration/test_main_lifecycle.py
git commit -m "feat(main): async entrypoint with config/db/logging bootstrap and graceful shutdown"
```

---

## Task 13: 全局验证 + README 更新

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 跑整套测试**

```bash
source .venv/bin/activate
pytest tests/ -v
```

Expected: 所有测试 PASS（demo 4 + config 4 + db 7 + logging 2 + main 1 = 18）。

- [ ] **Step 2: 手动跑一次 daemon，确认 SIGTERM 能退**

```bash
source .venv/bin/activate
MONITOR_DB_PATH=/tmp/m1_check.db python -m monitor &
DPID=$!
sleep 2
kill -TERM $DPID
wait $DPID
echo "exit code: $?"
```

Expected: 看到 JSON 启动日志，`kill -TERM` 后干净退出，`exit code: 0`。

- [ ] **Step 3: 更新 `README.md` 运行和测试部分**

找到 "## 运行方式" 和 "## 测试" 部分，替换为：

```markdown
## 运行方式

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 常驻守护进程（当前仅装载配置 + 跑 DB 迁移 + 等 SIGTERM；M2+ 会加载采集/打分/推送）
python -m monitor

# 旧版 demo 仍可直接跑（会被后续 milestone 逐步替换）
python -m monitor.legacy
```

## 测试

```bash
source .venv/bin/activate
pytest                                   # 跑全部
pytest tests/unit -v                     # 只跑单元
pytest tests/unit/test_db.py -v          # 单文件
pytest tests/unit/test_db.py::test_fresh_db_runs_all_migrations -v  # 单测试
```
```

- [ ] **Step 4: 更新 `CLAUDE.md` 的 Commands 区块**

把 "Run the monitor pipeline end-to-end:" 段落及下面的 "Tests" 段落，替换为：

```markdown
## Commands

Install deps (src layout, editable):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the daemon (M1 scaffolding: config load + DB migrate + wait for SIGTERM):

```bash
python -m monitor
```

Legacy demo (still runnable until M4 replaces it):

```bash
python -m monitor.legacy
```

Tests (pytest, async enabled by pyproject config):

```bash
pytest                                    # all
pytest tests/unit -v                      # unit only
pytest tests/unit/test_db.py::test_fresh_db_runs_all_migrations -v  # single test
```
```

然后**整段替换** `CLAUDE.md` 里原有的 `## Architecture` 段落（包括它下面的 "Config resolution order"、"Conventions worth preserving"、"Testing pattern" 三个子段），替换为下方新内容：

```markdown
## Architecture

**Migration status (as of 2026-04-17):** The productized daemon lives in `src/monitor/`. The pre-productization single-file pipeline is preserved at `src/monitor/legacy.py` and will be replaced module-by-module over M2–M6. Both coexist during the transition; tests for `monitor.legacy` continue to pass.

The productized pipeline orchestrated by `src/monitor/main.py` runs as a single async daemon that (in final form) holds three concurrent tasks: TG bot long-polling, APScheduler with four jobs (morning/evening digest, 30-min surge poll, weekly digest), and the pipeline executor guarded by an `asyncio.Lock` for non-reentrance. M1 only wires up config loading, DB migrations, structured logging, and SIGTERM handling — pipeline/bot/scheduler modules are stubs.

Design spec: `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`.

### Config resolution order

`Settings` (pydantic-settings) reads secrets and paths from env vars (`GITHUB_TOKEN`, `MINIMAX_API_KEY`, `TELEGRAM_*`, `MONITOR_DB_PATH`, `MONITOR_CONFIG`, `MONITOR_LOG_PATH`). `ConfigFile` (regular pydantic BaseModel) is loaded from the JSON file pointed to by `MONITOR_CONFIG` and holds tuning knobs (keywords, thresholds, weights, model name). Code defaults apply where neither env nor file provides a value.

### Schema migrations

`src/monitor/db.py` tracks `SCHEMA_VERSION` (code constant) and the `schema_version` table. On startup `run_migrations` applies anything missing — idempotent, safe to re-run. Migration 001 creates the new table set and, if an old `seen_repositories` table exists (from the demo), copies rows into `pushed_items` while adding `stars`/`forks` columns to `repository_metrics`.

### Legacy conventions still worth preserving

- Timezone-aware datetimes everywhere; `parse_dt` normalizes Z/offset ISO strings to UTC.
- Chinese user-facing strings (carried into M4's Telegram renders).
- No-external-HTTP-client was the demo rule; M2 replaces urllib with httpx across the new codebase.
```

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: refresh README and CLAUDE.md for M1 structure"
```

- [ ] **Step 6: 清理 /tmp 验证文件**

```bash
rm -f /tmp/m1_check.db
```

---

## M1 验收标准

结束时应该满足：

- [x] `pytest tests/` 全绿（18 条测试）
- [x] `python -m monitor` 能启动、输出 JSON 日志、`SIGTERM` 干净退出
- [x] 旧 demo `python -m monitor.legacy` 仍可跑
- [x] 从 demo 时代 `monitor.db` 启动也能正常跑迁移（数据被复制到 `pushed_items`）
- [x] `src/monitor/` 有完整子包骨架，M2+ 直接往里加文件
- [x] README、CLAUDE.md 反映新状态

## 不做的事

- 不替换 `monitor.legacy` 的 GitHub / Telegram / 评分逻辑（M2/M3/M4 做）
- 不加 `respx` / live 测试（M2 在加 GitHub client 时一起）
- 不碰 systemd / healthcheck / backup 脚本（M6）
