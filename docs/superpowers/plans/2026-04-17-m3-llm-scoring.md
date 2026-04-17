# M3 LLM Scoring + Preference Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **When touching `monitor/clients/llm.py` or anything Anthropic-SDK-related, also invoke the `claude-api` skill** (noted in the relevant task's Context block).

**Goal:** Turn the enriched `RepoCandidate` into a scored recommendation via (1) a rules-based coarse score, (2) an LLM-based quality score pulled from the Anthropic SDK pointed at MiniMax's Anthropic-compatible endpoint with forced tool use + ephemeral prompt caching, and (3) a heuristic fallback when the LLM path fails. Populate `final_score`, `summary`, `reason`. Cache LLM scores by `(full_name, readme_sha256)` so rescoring an unchanged README costs nothing, and drive a preference profile that's regenerated every N new feedback entries so personal taste folds into the LLM prompt.

**Architecture:** `monitor/scoring/` holds all scoring logic in four focused modules (`rules`, `heuristic`, `preference`, `score`). `monitor/clients/llm.py` wraps `anthropic.AsyncAnthropic` with a strict tool-use schema (`submit_repo_score`) and an ephemeral-cached system prompt. The orchestrator `score_repo()` runs `RuleEngine.score` → checks `llm_score_cache` → calls LLM (falls back to heuristic on any failure) → writes cache → computes `final = rule*α + llm*β`. The preference profile is built by `PreferenceBuilder.regenerate()` from recent `user_feedback` rows and persisted in the single-row `preference_profile` table; it's injected into the system prompt on every LLM call. No changes to `monitor.legacy` until M4.

**Tech Stack:** `anthropic>=0.39` (already in `pyproject.toml`), `pydantic>=2.6`, `tenacity>=8.2`, existing `aiosqlite`, `structlog`. `hashlib` for README fingerprints. Tests mock the Anthropic SDK via dependency injection, not network mocking.

---

## Background and Prerequisites

- **Branch state:** `m3-llm-scoring` branched from `main` (PR #3 merged). M1+M2 complete; 75 tests green.
- **Legacy:** `src/monitor/legacy.py` stays exactly as-is through M3. Its 4 tests continue to pass. The legacy `RuleEngine` and `_heuristic_analysis` are **copy-ported** to new modules with modern types — not moved, not re-exported.
- **Dependencies:** Already declared in `pyproject.toml` (M1). No new deps.
- **Config:** `ConfigFile.llm_model` (default `"minimax-m2"`), `ConfigFile.llm_base_url` (default `"https://api.minimax.chat/anthropic/v1"`), `ConfigFile.preference_refresh_every` (default 5), `ConfigFile.weights` (default 0.55/0.45) are all in place from M1. `Settings.minimax_api_key` comes from the env var `MINIMAX_API_KEY`.
- **DB schema:** M1 already created `llm_score_cache` and `preference_profile` tables plus the referenced `user_feedback`. M3 adds DAO helpers only; no new migrations.
- **Design source of truth:** `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`, §5 (LLM 集成) and §6 (偏好画像).

## File Structure

**New source files**
- `src/monitor/scoring/types.py` — `ScoreResult` pydantic model + `LLMScoreError` exception.
- `src/monitor/scoring/rules.py` — `RuleEngine` (coarse filter + rule score).
- `src/monitor/scoring/heuristic.py` — `heuristic_score_readme()` fallback.
- `src/monitor/scoring/preference.py` — `PreferenceBuilder.regenerate()`.
- `src/monitor/scoring/score.py` — `score_repo()` orchestrator.
- `src/monitor/clients/llm.py` — `LLMClient` wrapping `AsyncAnthropic` with forced tool use.

**New test files**
- `tests/unit/test_scoring_types.py`
- `tests/unit/test_rule_engine.py`
- `tests/unit/test_heuristic.py`
- `tests/unit/test_preference.py`
- `tests/unit/test_llm_client.py`
- `tests/unit/test_score.py` — orchestrator unit tests
- `tests/unit/test_db_scoring_dao.py` — DAO helpers for `llm_score_cache` + `preference_profile`
- `tests/integration/test_pipeline_m3.py` — end-to-end collect + enrich + score, LLM mocked

**Modified files**
- `src/monitor/db.py` — append DAOs (pattern established in M1 Task 8).
- `CLAUDE.md` — extend Architecture with an M3 additions subsection.

**Unchanged**
- `src/monitor/legacy.py`, `src/monitor/main.py`, `src/monitor/__main__.py`, everything under M1/M2 that's not explicitly modified.
- `pyproject.toml`, `README.md`.

---

## Task 1: Scoring types — `ScoreResult` + `LLMScoreError`

**Files:**
- Create: `src/monitor/scoring/types.py`
- Create: `tests/unit/test_scoring_types.py`

- [ ] **Step 1: Write failing test `tests/unit/test_scoring_types.py`**

```python
import pytest
from pydantic import ValidationError

from monitor.scoring.types import LLMScoreError, ScoreResult


def test_score_result_accepts_valid_payload() -> None:
    result = ScoreResult(
        score=8.5,
        readme_completeness=0.9,
        summary="一句话描述",
        reason="一句话理由",
        matched_interests=["agent"],
        red_flags=[],
    )
    assert result.score == 8.5
    assert result.matched_interests == ["agent"]
    assert result.red_flags == []


def test_score_result_rejects_out_of_range_score() -> None:
    with pytest.raises(ValidationError):
        ScoreResult(
            score=12.0,  # > 10 max
            readme_completeness=0.5,
            summary="x",
            reason="y",
            matched_interests=[],
            red_flags=[],
        )
    with pytest.raises(ValidationError):
        ScoreResult(
            score=5.0,
            readme_completeness=1.5,  # > 1.0 max
            summary="x",
            reason="y",
            matched_interests=[],
            red_flags=[],
        )


def test_score_result_ignores_unknown_fields() -> None:
    """LLM may return extra keys — accept them without failing."""
    result = ScoreResult.model_validate({
        "score": 7.0,
        "readme_completeness": 0.6,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
        "surprise_field": "noise",
    })
    assert result.score == 7.0


def test_llm_score_error_carries_reason() -> None:
    exc = LLMScoreError("bad tool_use block", cause="schema_mismatch")
    assert "bad tool_use" in str(exc)
    assert exc.cause == "schema_mismatch"
```

- [ ] **Step 2: Verify test fails**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
pytest tests/unit/test_scoring_types.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.scoring.types'`.

- [ ] **Step 3: Write `src/monitor/scoring/types.py`**

```python
from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field


class ScoreResult(BaseModel):
    """Structured LLM output for one repo.

    `extra="ignore"` because the MiniMax/Anthropic endpoint may append
    reasoning or debug fields we don't care about — silently drop them
    rather than raise on valid-but-verbose responses.
    """

    model_config = ConfigDict(extra="ignore")

    score: float = Field(ge=1.0, le=10.0)
    readme_completeness: float = Field(ge=0.0, le=1.0)
    summary: str
    reason: str
    matched_interests: List[str]
    red_flags: List[str]


class LLMScoreError(Exception):
    """Raised by LLMClient when an LLM call can't produce a valid ScoreResult.

    Includes network failures after retries exhausted, tool_use block
    missing/malformed, and pydantic validation failures. The scoring
    orchestrator catches this and falls back to heuristic scoring.
    """

    def __init__(self, message: str, *, cause: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_scoring_types.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/scoring/types.py tests/unit/test_scoring_types.py
git commit -m "feat(scoring): ScoreResult + LLMScoreError domain types"
```

---

## Task 2: RuleEngine — coarse filter + rule score

**Files:**
- Create: `src/monitor/scoring/rules.py`
- Create: `tests/unit/test_rule_engine.py`

Context: Legacy's `RuleEngine` at `src/monitor/legacy.py:319-347` provides the ported behavior. We re-implement with modern types (RepoCandidate from `monitor.models`, ConfigFile from `monitor.config`) and an injectable `now` for deterministic tests. Legacy untouched.

- [ ] **Step 1: Write failing test**

```python
import datetime as dt

import pytest

from monitor.config import ConfigFile
from monitor.models import RepoCandidate
from monitor.scoring.rules import RuleEngine


def _repo(
    *,
    name: str = "a/b",
    language: str = "Python",
    stars: int = 300,
    forks: int = 30,
    created_ago_days: int = 30,
    pushed_ago_days: int = 1,
    star_velocity_day: float = 2.0,
    star_velocity_week: float = 1.5,
    fork_star_ratio: float = 0.0,
    avg_issue_response_hours: float = 0.0,
    contributor_growth_week: int = 0,
) -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language=language,
        stars=stars,
        forks=forks,
        created_at=now - dt.timedelta(days=created_ago_days),
        pushed_at=now - dt.timedelta(days=pushed_ago_days),
        owner_login=name.split("/")[0],
        star_velocity_day=star_velocity_day,
        star_velocity_week=star_velocity_week,
        fork_star_ratio=fork_star_ratio,
        avg_issue_response_hours=avg_issue_response_hours,
        contributor_growth_week=contributor_growth_week,
    )


def _engine(**overrides) -> RuleEngine:
    cfg = ConfigFile(**overrides)
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    return RuleEngine(cfg, now=now)


def test_apply_rejects_low_stars() -> None:
    engine = _engine(min_stars=500, languages=["Python"], max_repo_age_days=180)
    assert engine.apply(_repo(stars=300)) is False


def test_apply_rejects_wrong_language() -> None:
    engine = _engine(min_stars=100, languages=["Rust"], max_repo_age_days=180)
    assert engine.apply(_repo(language="Python")) is False


def test_apply_rejects_too_old_repo() -> None:
    engine = _engine(min_stars=100, languages=["Python"], max_repo_age_days=30)
    assert engine.apply(_repo(created_ago_days=100)) is False


def test_apply_accepts_passing_repo() -> None:
    engine = _engine(min_stars=100, languages=["Python"], max_repo_age_days=180)
    assert engine.apply(_repo(stars=300, language="Python", created_ago_days=30)) is True


def test_score_is_a_weighted_combination_bounded_to_10() -> None:
    """Score must never exceed 10 even when every signal is maxed out."""
    engine = _engine()
    repo = _repo(
        star_velocity_day=1000.0,
        star_velocity_week=1000.0,
        fork_star_ratio=10.0,
        contributor_growth_week=1000,
        pushed_ago_days=0,
        avg_issue_response_hours=0.0,
    )
    score = engine.score(repo)
    assert 0.0 <= score <= 10.0


def test_score_zero_when_all_signals_flat() -> None:
    engine = _engine()
    repo = _repo(
        star_velocity_day=0.0,
        star_velocity_week=0.0,
        fork_star_ratio=0.0,
        avg_issue_response_hours=0.0,
        contributor_growth_week=0,
        pushed_ago_days=3650,  # very stale
    )
    score = engine.score(repo)
    # Freshness and response-score floors keep score above zero; assert it's
    # at least non-negative and bounded rather than an exact value.
    assert 0.0 <= score <= 10.0


def test_score_higher_for_fresher_and_faster_repo() -> None:
    engine = _engine()
    fresh = _repo(
        star_velocity_day=5.0,
        star_velocity_week=5.0,
        pushed_ago_days=0,
        contributor_growth_week=3,
        avg_issue_response_hours=1.0,
    )
    stale = _repo(
        star_velocity_day=0.1,
        star_velocity_week=0.1,
        pushed_ago_days=180,
        contributor_growth_week=0,
        avg_issue_response_hours=100.0,
    )
    assert engine.score(fresh) > engine.score(stale)
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_rule_engine.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.scoring.rules'`.

- [ ] **Step 3: Write `src/monitor/scoring/rules.py`**

```python
from __future__ import annotations

import datetime as dt

from monitor.config import ConfigFile
from monitor.models import RepoCandidate


class RuleEngine:
    """Coarse filter + deterministic rule score.

    `apply(repo)` returns True if the repo passes the stars/language/age
    thresholds (used by the filter stage before enrichment).

    `score(repo)` returns a 0-10 weighted combination of enriched signals
    (star velocity, fork ratio, freshness, contributor growth, issue
    response). Ported from legacy.RuleEngine with modern types and an
    injectable `now` for tests.
    """

    def __init__(self, config: ConfigFile, *, now: dt.datetime | None = None) -> None:
        self._config = config
        self._now = now or dt.datetime.now(dt.timezone.utc)

    def apply(self, repo: RepoCandidate) -> bool:
        if repo.stars < self._config.min_stars:
            return False
        if repo.language not in self._config.languages:
            return False
        max_age = dt.timedelta(days=self._config.max_repo_age_days)
        if (self._now - repo.created_at) > max_age:
            return False
        return True

    def score(self, repo: RepoCandidate) -> float:
        ratio = repo.fork_star_ratio or 0.0
        freshness_days = max((self._now - repo.pushed_at).days, 0)
        freshness_score = max(0.0, 10.0 - freshness_days / 10.0)
        response_score = (
            10.0
            if repo.avg_issue_response_hours == 0
            else max(0.0, 10.0 - repo.avg_issue_response_hours / 24.0)
        )
        combined = (
            min(repo.star_velocity_day, 10.0) * 0.25
            + min(repo.star_velocity_week * 2, 10.0) * 0.2
            + min(ratio * 20, 10.0) * 0.1
            + freshness_score * 0.2
            + min(repo.contributor_growth_week, 10) * 0.1
            + response_score * 0.15
        )
        return round(min(combined, 10.0), 2)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_rule_engine.py -v
```

Expected: **7 passed**.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/scoring/rules.py tests/unit/test_rule_engine.py
git commit -m "feat(scoring/rules): RuleEngine ported with injectable clock"
```

---

## Task 3: Heuristic fallback — `heuristic_score_readme()`

**Files:**
- Create: `src/monitor/scoring/heuristic.py`
- Create: `tests/unit/test_heuristic.py`

Context: Ports legacy's `_heuristic_analysis` (at `src/monitor/legacy.py:362-379`). Returns a `ScoreResult` so the orchestrator can treat fallback output identically to LLM output.

- [ ] **Step 1: Write failing test**

```python
import datetime as dt

from monitor.models import RepoCandidate
from monitor.scoring.heuristic import heuristic_score_readme
from monitor.scoring.types import ScoreResult


def _repo(readme: str = "", description: str = "") -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="a/b",
        html_url="https://github.com/a/b",
        description=description,
        language="Python",
        stars=100,
        forks=10,
        created_at=now,
        pushed_at=now,
        owner_login="a",
        readme_text=readme,
    )


def test_heuristic_rewards_complete_readme_sections() -> None:
    readme = "# repo\n## install\n## usage\n## architecture\n## license"
    result = heuristic_score_readme(_repo(readme=readme), interest_tags=["agent"])
    assert isinstance(result, ScoreResult)
    assert result.readme_completeness == 1.0
    # Score should be mid-range since no interest tags matched
    assert 4.0 <= result.score <= 10.0


def test_heuristic_matches_interest_tags_in_readme_or_description() -> None:
    repo = _repo(readme="This is an LLM agent framework", description="LLM agent")
    low = heuristic_score_readme(_repo(readme="generic project"), interest_tags=["agent", "llm"])
    high = heuristic_score_readme(repo, interest_tags=["agent", "llm"])
    assert high.score > low.score
    assert set(high.matched_interests) == {"agent", "llm"}


def test_heuristic_falls_back_to_summary_from_description() -> None:
    result = heuristic_score_readme(_repo(description="neat tool"), interest_tags=[])
    assert "neat tool" in result.summary


def test_heuristic_summary_when_no_description() -> None:
    result = heuristic_score_readme(_repo(description=""), interest_tags=[])
    assert result.summary  # must not be empty


def test_heuristic_reason_mentions_match_counts() -> None:
    result = heuristic_score_readme(
        _repo(readme="# repo\n## install\nbuild an agent"),
        interest_tags=["agent"],
    )
    assert "agent" in result.reason.lower() or "1" in result.reason
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_heuristic.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.scoring.heuristic'`.

- [ ] **Step 3: Write `src/monitor/scoring/heuristic.py`**

```python
from __future__ import annotations

from typing import Sequence

from monitor.models import RepoCandidate
from monitor.scoring.types import ScoreResult


def heuristic_score_readme(
    repo: RepoCandidate, *, interest_tags: Sequence[str]
) -> ScoreResult:
    """Ported from legacy's `_heuristic_analysis`. Returns a ScoreResult so
    the orchestrator can treat LLM and fallback outputs identically."""

    readme = repo.readme_text or ""
    lower = readme.lower()
    has_install = "install" in lower or "安装" in lower
    has_usage = "usage" in lower or "quick start" in lower or "使用" in lower
    has_arch = "architecture" in lower or "架构" in lower
    has_license = "license" in lower or "许可证" in lower
    completeness = sum([has_install, has_usage, has_arch, has_license]) / 4.0

    haystack = (repo.description + " " + readme).lower()
    matched: list[str] = [tag for tag in interest_tags if tag.lower() in haystack]

    # Score scales from ~4 (empty README, no matches) toward 10.
    score = min(10.0, 4.0 + completeness * 4.0 + len(matched))

    summary = repo.description.strip() or "README 中未提供明确描述"
    reason = (
        f"匹配兴趣标签 {len(matched)} 项，README 完整度 {completeness:.0%}，"
        f"近 24 小时 star 增速 {repo.star_velocity_day:.1f}。"
    )

    return ScoreResult(
        score=round(score, 2),
        readme_completeness=round(completeness, 2),
        summary=summary,
        reason=reason,
        matched_interests=matched,
        red_flags=[],
    )
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_heuristic.py -v
```

Expected: **5 passed**.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/scoring/heuristic.py tests/unit/test_heuristic.py
git commit -m "feat(scoring/heuristic): heuristic README fallback returning ScoreResult"
```

---

## Task 4: DB DAOs — `llm_score_cache` + `preference_profile`

**Files:**
- Modify: `src/monitor/db.py` (append new DAO functions at the bottom)
- Create: `tests/unit/test_db_scoring_dao.py`

- [ ] **Step 1: Write failing test**

```python
import datetime as dt
from pathlib import Path

import pytest

from monitor.db import (
    connect,
    get_cached_llm_score,
    get_preference_profile,
    put_cached_llm_score,
    put_preference_profile,
    run_migrations,
)
from monitor.scoring.types import ScoreResult


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "scoring.db"


def _score(score: float = 8.0) -> ScoreResult:
    return ScoreResult(
        score=score,
        readme_completeness=0.8,
        summary="s",
        reason="r",
        matched_interests=["agent"],
        red_flags=[],
    )


async def test_llm_score_cache_miss_returns_none(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    got = await get_cached_llm_score(conn, "a/b", readme_sha256="deadbeef")
    assert got is None
    await conn.close()


async def test_llm_score_cache_put_then_get(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_cached_llm_score(conn, "a/b", readme_sha256="abc", result=_score(8.5), now=now)

    got = await get_cached_llm_score(conn, "a/b", readme_sha256="abc")
    assert got is not None
    assert got.score == 8.5
    assert got.matched_interests == ["agent"]
    await conn.close()


async def test_llm_score_cache_different_hash_is_independent(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_cached_llm_score(conn, "a/b", readme_sha256="v1", result=_score(5.0), now=now)
    await put_cached_llm_score(conn, "a/b", readme_sha256="v2", result=_score(9.0), now=now)

    got_v1 = await get_cached_llm_score(conn, "a/b", readme_sha256="v1")
    got_v2 = await get_cached_llm_score(conn, "a/b", readme_sha256="v2")
    assert got_v1 is not None and got_v1.score == 5.0
    assert got_v2 is not None and got_v2.score == 9.0
    await conn.close()


async def test_llm_score_cache_put_overwrites_same_key(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_cached_llm_score(conn, "a/b", readme_sha256="abc", result=_score(5.0), now=now)
    await put_cached_llm_score(conn, "a/b", readme_sha256="abc", result=_score(8.0), now=now)

    got = await get_cached_llm_score(conn, "a/b", readme_sha256="abc")
    assert got is not None and got.score == 8.0
    await conn.close()


async def test_preference_profile_empty_returns_none(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    got = await get_preference_profile(conn)
    assert got is None
    await conn.close()


async def test_preference_profile_put_then_get(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_preference_profile(
        conn,
        profile_text="用户喜欢 AI agent 框架",
        generated_at=now,
        based_on_feedback_count=10,
    )

    got = await get_preference_profile(conn)
    assert got is not None
    assert got["profile_text"] == "用户喜欢 AI agent 框架"
    assert got["based_on_feedback_count"] == 10
    await conn.close()


async def test_preference_profile_upsert_replaces_previous(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)

    await put_preference_profile(conn, profile_text="v1", generated_at=now, based_on_feedback_count=5)
    await put_preference_profile(conn, profile_text="v2", generated_at=now, based_on_feedback_count=10)

    got = await get_preference_profile(conn)
    assert got is not None
    assert got["profile_text"] == "v2"
    assert got["based_on_feedback_count"] == 10
    await conn.close()
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_db_scoring_dao.py -v 2>&1 | tail -10
```

Expected: ImportError on the new DAO symbols.

- [ ] **Step 3: Append DAOs to `src/monitor/db.py`**

First, add `import json` to the existing top-of-file import block in `/Users/Zhuanz/Documents/GithubRepoMonitor/src/monitor/db.py` (keep it alphabetically grouped with the other stdlib imports: `import datetime as _dt`, `import json`, `from pathlib import Path`, etc.). Do NOT add any mid-file imports.

Then append to the bottom of `src/monitor/db.py` (after `pushed_cooldown_state`):

```python


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
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_db_scoring_dao.py -v
pytest tests/unit/test_db.py -v  # regression — pre-existing DAOs still work
```

Expected: 7 new passing + 9 pre-existing still passing.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/db.py tests/unit/test_db_scoring_dao.py
git commit -m "feat(db): llm_score_cache + preference_profile DAO helpers"
```

---

## Task 5: LLM client — `LLMClient` with forced tool use

**Files:**
- Create: `src/monitor/clients/llm.py`
- Create: `tests/unit/test_llm_client.py`

**Context for implementer:** This task uses the Anthropic Python SDK. **Before implementing, invoke the `claude-api` skill** to refresh on the current SDK's async patterns, forced tool use, ephemeral prompt caching, and structured output. The SDK is already installed (`anthropic>=0.39` in `pyproject.toml`). The base URL in the config points at MiniMax's Anthropic-compatible endpoint — the SDK's `base_url` parameter handles this without any special-casing.

MiniMax compatibility caveat (from spec §5): the endpoint MAY NOT support forced tool use or ephemeral cache. If a real call raises, the orchestrator in Task 7 catches `LLMScoreError` and falls back to heuristic. That means the LLM client itself can be strict — it raises `LLMScoreError` on any anomaly and doesn't try to self-heal. A one-off smoke script to verify MiniMax behavior is OUT OF SCOPE for this task (track it as an operational TODO for when real credentials exist).

- [ ] **Step 1: Write failing test**

```python
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.clients.llm import LLMClient, SCORE_TOOL
from monitor.models import RepoCandidate
from monitor.scoring.types import LLMScoreError, ScoreResult


def _repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="widgets for agents",
        language="Python",
        stars=420,
        forks=21,
        created_at=now,
        pushed_at=now,
        owner_login="acme",
        readme_text="# widget\n## install\n",
        star_velocity_day=5.0,
        contributor_count=12,
    )


def _tool_use_response(payload: dict) -> SimpleNamespace:
    """Mimic anthropic.types.Message with a tool_use content block."""
    block = SimpleNamespace(type="tool_use", name="submit_repo_score", input=payload)
    return SimpleNamespace(content=[block])


def _text_only_response(text: str = "no tool here") -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


def _client_with_mock(response) -> LLMClient:
    fake_sdk = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    return LLMClient(
        api_key="test-key",
        base_url="https://example.invalid",
        model="minimax-m2",
        anthropic_client=fake_sdk,
    )


async def test_score_repo_returns_parsed_result() -> None:
    payload = {
        "score": 8.2,
        "readme_completeness": 0.9,
        "summary": "Strong agent framework",
        "reason": "Matches your interest in agents",
        "matched_interests": ["agent"],
        "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    result = await client.score_repo(_repo(), interest_tags=["agent"], preference_profile=None)

    assert isinstance(result, ScoreResult)
    assert result.score == 8.2


async def test_score_repo_sends_forced_tool_use() -> None:
    payload = {
        "score": 7.0,
        "readme_completeness": 0.5,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    await client.score_repo(_repo(), interest_tags=["agent"], preference_profile=None)

    create_mock = client._client.messages.create
    kwargs = create_mock.call_args.kwargs
    assert kwargs["tools"] == [SCORE_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_repo_score"}
    assert kwargs["model"] == "minimax-m2"


async def test_score_repo_injects_preference_profile_into_system() -> None:
    payload = {
        "score": 7.0, "readme_completeness": 0.5, "summary": "s", "reason": "r",
        "matched_interests": [], "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    await client.score_repo(
        _repo(),
        interest_tags=["agent"],
        preference_profile="用户偏好 rust tooling",
    )

    kwargs = client._client.messages.create.call_args.kwargs
    system_blocks = kwargs["system"]
    # Must be a list (so cache_control can be set per block), contain the
    # rubric and the preference profile.
    joined = " ".join(b["text"] for b in system_blocks)
    assert "rust tooling" in joined


async def test_score_repo_uses_ephemeral_cache_on_system_blocks() -> None:
    payload = {
        "score": 7.0, "readme_completeness": 0.5, "summary": "s", "reason": "r",
        "matched_interests": [], "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    await client.score_repo(_repo(), interest_tags=["agent"], preference_profile=None)

    kwargs = client._client.messages.create.call_args.kwargs
    assert any(
        b.get("cache_control") == {"type": "ephemeral"}
        for b in kwargs["system"]
    ), "at least one system block must be ephemeral-cached"


async def test_score_repo_raises_when_no_tool_use_block() -> None:
    client = _client_with_mock(_text_only_response("model refused to use the tool"))
    with pytest.raises(LLMScoreError) as excinfo:
        await client.score_repo(_repo(), interest_tags=[], preference_profile=None)
    assert "tool_use" in str(excinfo.value).lower() or excinfo.value.cause


async def test_score_repo_raises_when_tool_input_is_malformed() -> None:
    # score=50 is outside the ge=1, le=10 validator
    bad_payload = {
        "score": 50.0,
        "readme_completeness": 0.5,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(bad_payload))
    with pytest.raises(LLMScoreError):
        await client.score_repo(_repo(), interest_tags=[], preference_profile=None)


async def test_score_repo_raises_llm_score_error_on_sdk_failure() -> None:
    fake_sdk = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("network down")))
    )
    client = LLMClient(
        api_key="k", base_url="u", model="m", anthropic_client=fake_sdk,
    )
    with pytest.raises(LLMScoreError):
        await client.score_repo(_repo(), interest_tags=[], preference_profile=None)


async def test_score_repo_truncates_long_readme() -> None:
    """README beyond 12K chars must be truncated before hitting the wire."""
    payload = {
        "score": 7.0, "readme_completeness": 0.5, "summary": "s", "reason": "r",
        "matched_interests": [], "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    huge = _repo()
    huge.readme_text = "x" * 30000

    await client.score_repo(huge, interest_tags=[], preference_profile=None)

    kwargs = client._client.messages.create.call_args.kwargs
    user_text = kwargs["messages"][0]["content"]
    # user_text is either a string or a list of content blocks
    if isinstance(user_text, list):
        user_text = " ".join(b.get("text", "") for b in user_text if isinstance(b, dict))
    assert len(user_text) < 20000
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_llm_client.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.clients.llm'`.

- [ ] **Step 3: Write `src/monitor/clients/llm.py`**

```python
from __future__ import annotations

from typing import Any, Sequence

import structlog
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from monitor.models import RepoCandidate
from monitor.scoring.types import LLMScoreError, ScoreResult


log = structlog.get_logger(__name__)

README_TRUNCATE_CHARS = 12000
DEFAULT_MAX_TOKENS = 1024

_TOOL_NAME = "submit_repo_score"

SCORE_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": "提交对该仓库的结构化评估。",
    "input_schema": {
        "type": "object",
        "required": [
            "score",
            "readme_completeness",
            "summary",
            "reason",
            "matched_interests",
            "red_flags",
        ],
        "properties": {
            "score": {
                "type": "number",
                "minimum": 1,
                "maximum": 10,
                "description": "1-10 综合评分",
            },
            "readme_completeness": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "README 完整度 0.0-1.0",
            },
            "summary": {
                "type": "string",
                "maxLength": 140,
                "description": "项目一句话摘要",
            },
            "reason": {
                "type": "string",
                "maxLength": 240,
                "description": "推荐理由，一句话",
            },
            "matched_interests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "命中的用户兴趣标签",
            },
            "red_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "值得警惕的信号（例如 README 空白、极冷门话题）",
            },
        },
    },
}


_RUBRIC = """你是开源项目评估助手。对每个仓库按以下维度打分 1-10：
- 工程质量：代码/README/文档完整度
- 活跃度：最近提交、issue 响应、贡献者增长
- 方向性：是否对用户的兴趣标签有明显匹配
- 独特性：相较同类项目的差异化
以 submit_repo_score 工具返回结构化结果。"""


class LLMClient:
    """Anthropic AsyncAnthropic pointed at MiniMax's compatible endpoint.

    Strict: any SDK error, missing tool_use block, or validation failure
    raises LLMScoreError. Upstream (`scoring.score.score_repo`) catches
    that and falls back to the heuristic scorer.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        anthropic_client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = anthropic_client or AsyncAnthropic(
            api_key=api_key, base_url=base_url
        )

    async def score_repo(
        self,
        repo: RepoCandidate,
        *,
        interest_tags: Sequence[str],
        preference_profile: str | None,
    ) -> ScoreResult:
        system_blocks = self._build_system(preference_profile)
        user_text = self._build_user_prompt(repo, interest_tags)

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                tools=[SCORE_TOOL],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:  # noqa: BLE001 - SDK surface is broad
            log.warning("llm.score_sdk_error", repo=repo.full_name, error=str(exc))
            raise LLMScoreError(str(exc), cause="sdk_error") from exc

        _log_usage(resp, repo.full_name)
        tool_input = _extract_tool_input(resp)
        if tool_input is None:
            raise LLMScoreError(
                "no submit_repo_score tool_use block in response",
                cause="missing_tool_use",
            )

        try:
            return ScoreResult.model_validate(tool_input)
        except ValidationError as exc:
            log.warning(
                "llm.score_validation_failed",
                repo=repo.full_name,
                error=str(exc),
            )
            raise LLMScoreError(str(exc), cause="schema_mismatch") from exc

    @staticmethod
    def _build_system(preference_profile: str | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _RUBRIC,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if preference_profile:
            blocks.append(
                {
                    "type": "text",
                    "text": f"用户偏好画像：\n{preference_profile}",
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    @staticmethod
    def _build_user_prompt(
        repo: RepoCandidate, interest_tags: Sequence[str]
    ) -> str:
        readme = (repo.readme_text or "")[:README_TRUNCATE_CHARS]
        tags_text = "、".join(interest_tags) if interest_tags else "(无)"
        return (
            f"仓库：{repo.full_name}\n"
            f"描述：{repo.description or '(空)'}\n"
            f"语言：{repo.language}\n"
            f"Stars：{repo.stars}，Forks：{repo.forks}\n"
            f"近 24h star 增速：{repo.star_velocity_day:.1f}\n"
            f"贡献者数：{repo.contributor_count}\n"
            f"平均 issue 响应：{repo.avg_issue_response_hours:.1f} 小时\n"
            f"用户兴趣标签：{tags_text}\n"
            f"\nREADME（截断 {README_TRUNCATE_CHARS} 字符）：\n{readme}"
        )


def _extract_tool_input(resp: Any) -> dict | None:
    content = getattr(resp, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
            candidate = getattr(block, "input", None)
            if isinstance(candidate, dict):
                return candidate
    return None


def _log_usage(resp: Any, repo_full_name: str) -> None:
    """Log Anthropic `usage` fields if present. Safe on responses that don't
    carry usage (e.g. mocks in tests)."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    log.info(
        "llm.usage",
        repo=repo_full_name,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
    )
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_llm_client.py -v
```

Expected: **8 passed**.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/llm.py tests/unit/test_llm_client.py
git commit -m "feat(clients/llm): AsyncAnthropic wrapper with forced tool use + ephemeral cache"
```

---

## Task 6: Preference builder — `PreferenceBuilder.regenerate()`

**Files:**
- Create: `src/monitor/scoring/preference.py`
- Create: `tests/unit/test_preference.py`

Context: Reads the last 20 likes + 20 dislikes from `user_feedback` (schema from M1), sends them to the LLM with a "summarize this user's taste" prompt, writes the result to `preference_profile`. `user_feedback` is currently empty (feedback buttons are M4) but the builder should already work so M4 only has to wire the button.

- [ ] **Step 1: Write failing test**

```python
import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from monitor.db import connect, get_preference_profile, run_migrations
from monitor.scoring.preference import PreferenceBuilder


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "pref.db"


async def _seed_feedback(conn, rows: list[tuple[str, str, dict]]) -> None:
    """rows = [(action, created_at_iso, snapshot_dict), ...]"""
    # Need a pushed_item to FK to.
    await conn.execute(
        "INSERT INTO pushed_items "
        "(full_name, pushed_at, push_type, rule_score, llm_score, final_score) "
        "VALUES ('x/y', '2026-01-01T00:00:00+00:00', 'digest', 0, 0, 0)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM pushed_items LIMIT 1") as cur:
        push_id = (await cur.fetchone())[0]
    for action, created_at, snapshot in rows:
        await conn.execute(
            "INSERT INTO user_feedback (push_id, action, created_at, repo_snapshot) "
            "VALUES (?, ?, ?, ?)",
            (push_id, action, created_at, json.dumps(snapshot)),
        )
    await conn.commit()


async def test_regenerate_writes_profile_from_feedback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    await _seed_feedback(conn, [
        ("like", "2026-04-10T00:00:00+00:00", {"full_name": "a/rust-cli", "topics": ["rust", "cli"]}),
        ("like", "2026-04-11T00:00:00+00:00", {"full_name": "b/rust-agent", "topics": ["rust", "agent"]}),
        ("dislike", "2026-04-12T00:00:00+00:00", {"full_name": "c/awesome-list", "topics": ["awesome"]}),
    ])

    fake_llm = AsyncMock(return_value="用户偏好 Rust 系统工具 + agent 框架，对 awesome-list 类型反感。")
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)

    builder = PreferenceBuilder(
        conn=conn,
        llm_generate_profile=fake_llm,
        now=now,
    )
    result = await builder.regenerate()

    assert result is not None
    assert "Rust" in result.profile_text
    assert result.based_on_feedback_count == 3
    assert result.generated_at == now

    stored = await get_preference_profile(conn)
    assert stored is not None
    assert stored["profile_text"] == result.profile_text
    await conn.close()


async def test_regenerate_returns_none_when_no_feedback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    fake_llm = AsyncMock(return_value="never called")
    builder = PreferenceBuilder(conn=conn, llm_generate_profile=fake_llm)

    result = await builder.regenerate()
    assert result is None
    fake_llm.assert_not_awaited()
    await conn.close()


async def test_regenerate_sends_prompt_with_recent_feedback(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    await _seed_feedback(conn, [
        ("like", "2026-04-10T00:00:00+00:00", {"full_name": "liked/repo", "topics": ["ai"]}),
        ("dislike", "2026-04-11T00:00:00+00:00", {"full_name": "hated/repo", "topics": ["awesome"]}),
    ])

    captured_prompt: list[str] = []

    async def fake_llm(prompt: str) -> str:
        captured_prompt.append(prompt)
        return "profile text"

    builder = PreferenceBuilder(conn=conn, llm_generate_profile=fake_llm)
    await builder.regenerate()

    prompt = captured_prompt[0]
    assert "liked/repo" in prompt
    assert "hated/repo" in prompt
    await conn.close()


async def test_regenerate_limits_to_recent_N_per_action(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    # 25 likes, 25 dislikes — builder should cap each at 20
    many = [
        ("like", f"2026-04-{i:02d}T00:00:00+00:00", {"full_name": f"like/repo-{i}"})
        for i in range(1, 26)
    ] + [
        ("dislike", f"2026-03-{i:02d}T00:00:00+00:00", {"full_name": f"dislike/repo-{i}"})
        for i in range(1, 26)
    ]
    await _seed_feedback(conn, many)

    seen_repos: set[str] = set()

    async def fake_llm(prompt: str) -> str:
        # Extract all full_names from the prompt
        for line in prompt.splitlines():
            if "/" in line:
                for word in line.split():
                    if "/" in word and word.strip(",.()[]{}").count("/") == 1:
                        seen_repos.add(word.strip(",.()[]{}"))
        return "p"

    builder = PreferenceBuilder(
        conn=conn, llm_generate_profile=fake_llm, max_per_action=20
    )
    await builder.regenerate()

    # Older entries beyond the top-20 most-recent per action should be absent.
    # The 5 oldest likes (repo-1..5) and 5 oldest dislikes should NOT appear.
    assert "like/repo-1" not in seen_repos
    assert "dislike/repo-1" not in seen_repos
    await conn.close()
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_preference.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.scoring.preference'`.

- [ ] **Step 3: Write `src/monitor/scoring/preference.py`**

```python
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiosqlite
import structlog

from monitor.db import put_preference_profile


log = structlog.get_logger(__name__)

LLMGenerateProfile = Callable[[str], Awaitable[str]]


@dataclass(slots=True)
class RegenerationResult:
    profile_text: str
    generated_at: dt.datetime
    based_on_feedback_count: int


class PreferenceBuilder:
    """Builds a natural-language user-preference profile from recent
    user_feedback rows and persists it in the single-row preference_profile
    table. Called by M4 after every Nth feedback write."""

    def __init__(
        self,
        *,
        conn: aiosqlite.Connection,
        llm_generate_profile: LLMGenerateProfile,
        max_per_action: int = 20,
        now: dt.datetime | None = None,
    ) -> None:
        self._conn = conn
        self._generate = llm_generate_profile
        self._max_per_action = max_per_action
        self._now = now or dt.datetime.now(dt.timezone.utc)

    async def regenerate(self) -> RegenerationResult | None:
        likes = await self._recent_feedback("like")
        dislikes = await self._recent_feedback("dislike")
        if not likes and not dislikes:
            return None

        prompt = self._build_prompt(likes, dislikes)
        profile_text = (await self._generate(prompt)).strip()
        count = len(likes) + len(dislikes)

        await put_preference_profile(
            self._conn,
            profile_text=profile_text,
            generated_at=self._now,
            based_on_feedback_count=count,
        )
        log.info(
            "preference.regenerated",
            based_on=count,
            profile_chars=len(profile_text),
        )
        return RegenerationResult(
            profile_text=profile_text,
            generated_at=self._now,
            based_on_feedback_count=count,
        )

    async def _recent_feedback(self, action: str) -> list[dict]:
        async with self._conn.execute(
            "SELECT repo_snapshot FROM user_feedback "
            "WHERE action = ? ORDER BY created_at DESC LIMIT ?",
            (action, self._max_per_action),
        ) as cur:
            rows = await cur.fetchall()
        result: list[dict] = []
        for row in rows:
            raw = row[0]
            if not raw:
                continue
            try:
                result.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _build_prompt(likes: list[dict], dislikes: list[dict]) -> str:
        def _fmt(items: list[dict]) -> str:
            if not items:
                return "(无)"
            lines = []
            for item in items:
                name = item.get("full_name", "?")
                topics = item.get("topics") or []
                topics_str = "、".join(topics) if topics else ""
                lines.append(f"- {name}  topics=[{topics_str}]")
            return "\n".join(lines)

        return (
            "根据下列用户反馈，用 250-300 字中文总结用户偏好。"
            "描述用户喜欢什么方向的开源项目、不喜欢什么，并给出一个一句话"
            "的选项偏好描述。不要列举具体仓库名，只描述特征。\n\n"
            f"用户 👍 的项目：\n{_fmt(likes)}\n\n"
            f"用户 👎 的项目：\n{_fmt(dislikes)}"
        )
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_preference.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/scoring/preference.py tests/unit/test_preference.py
git commit -m "feat(scoring/preference): PreferenceBuilder regenerating from user_feedback"
```

---

## Task 7: Score orchestrator — `score_repo()`

**Files:**
- Create: `src/monitor/scoring/score.py`
- Create: `tests/unit/test_score.py`

Context: This is the integration point. It:
1. Computes rule score via `RuleEngine.score()`
2. Hashes README to a SHA-256 fingerprint
3. Checks `llm_score_cache` — if hit, uses cached `ScoreResult`
4. On miss, loads preference profile, calls `LLMClient.score_repo`
5. If LLM raises `LLMScoreError`, falls back to `heuristic_score_readme`
6. Writes cache on success (either LLM or heuristic — both produce `ScoreResult`)
7. Computes `final_score = rule*α + llm*β` using `config.weights`
8. Mutates `repo.rule_score` / `repo.llm_score` / `repo.final_score` / `repo.summary` / `repo.recommendation_reason` / `repo.readme_completeness` in place.

- [ ] **Step 1: Write failing test**

```python
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from monitor.config import ConfigFile
from monitor.db import connect, get_cached_llm_score, run_migrations
from monitor.models import RepoCandidate
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.scoring.types import LLMScoreError, ScoreResult


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "score.db"


def _repo(readme: str = "# r\n## install\n") -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)
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
        readme_text=readme,
        star_velocity_day=3.0,
        star_velocity_week=0.5,
    )


def _llm_result(score: float = 8.0) -> ScoreResult:
    return ScoreResult(
        score=score,
        readme_completeness=0.9,
        summary="nice",
        reason="matches",
        matched_interests=["agent"],
        red_flags=[],
    )


async def test_score_repo_cache_miss_calls_llm_and_writes_cache(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    llm = AsyncMock(return_value=_llm_result(8.0))
    config = ConfigFile(keywords=["agent"], weights={"rule": 0.5, "llm": 0.5})
    repo = _repo()

    await score_repo(
        repo,
        config=config,
        rule_engine=RuleEngine(config),
        llm_score_fn=llm,
        conn=conn,
    )

    # LLM was called exactly once
    assert llm.await_count == 1
    # Cache now has the entry
    from hashlib import sha256
    h = sha256(repo.readme_text.encode("utf-8")).hexdigest()
    cached = await get_cached_llm_score(conn, repo.full_name, readme_sha256=h)
    assert cached is not None
    assert cached.score == 8.0

    assert repo.llm_score == 8.0
    assert repo.rule_score > 0.0
    # final = rule*0.5 + llm*0.5
    assert abs(repo.final_score - (repo.rule_score * 0.5 + 8.0 * 0.5)) < 0.01
    assert repo.summary == "nice"
    assert repo.recommendation_reason == "matches"
    await conn.close()


async def test_score_repo_cache_hit_skips_llm(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    llm = AsyncMock(return_value=_llm_result(8.0))
    config = ConfigFile(keywords=["agent"])
    repo = _repo()

    # First call populates cache
    await score_repo(repo, config=config, rule_engine=RuleEngine(config), llm_score_fn=llm, conn=conn)
    # Second call on an identical-readme repo should hit cache
    repo2 = _repo()
    repo2.rule_score = 0.0  # reset to ensure it gets recomputed
    await score_repo(repo2, config=config, rule_engine=RuleEngine(config), llm_score_fn=llm, conn=conn)

    assert llm.await_count == 1  # only first call hit the LLM
    assert repo2.llm_score == 8.0
    await conn.close()


async def test_score_repo_falls_back_to_heuristic_on_llm_error(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    async def failing_llm(*args, **kwargs):
        raise LLMScoreError("simulated", cause="sdk_error")

    config = ConfigFile(keywords=["agent"])
    repo = _repo(readme="# r\n## install\n## usage\n## license\nbuild an agent")

    await score_repo(repo, config=config, rule_engine=RuleEngine(config), llm_score_fn=failing_llm, conn=conn)

    assert repo.llm_score > 0.0  # heuristic produced a value
    assert repo.summary  # heuristic populated summary
    # Cache got populated with the heuristic's result so later runs don't retry LLM
    from hashlib import sha256
    h = sha256(repo.readme_text.encode("utf-8")).hexdigest()
    cached = await get_cached_llm_score(conn, repo.full_name, readme_sha256=h)
    assert cached is not None
    await conn.close()


async def test_score_repo_final_is_weighted_combination(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    llm = AsyncMock(return_value=_llm_result(9.0))
    config = ConfigFile(weights={"rule": 0.3, "llm": 0.7})
    repo = _repo()

    await score_repo(repo, config=config, rule_engine=RuleEngine(config), llm_score_fn=llm, conn=conn)

    expected = round(repo.rule_score * 0.3 + 9.0 * 0.7, 2)
    assert repo.final_score == expected
    await conn.close()


async def test_score_repo_llm_fn_gets_preference_profile_when_present(tmp_db: Path) -> None:
    conn = await connect(tmp_db)
    await run_migrations(conn)

    # Pre-populate preference profile
    from monitor.db import put_preference_profile
    await put_preference_profile(
        conn,
        profile_text="用户喜欢 Rust",
        generated_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc),
        based_on_feedback_count=10,
    )

    calls: list[str | None] = []

    async def capturing_llm(repo, *, interest_tags, preference_profile):
        calls.append(preference_profile)
        return _llm_result()

    config = ConfigFile(keywords=["agent"])
    await score_repo(_repo(), config=config, rule_engine=RuleEngine(config), llm_score_fn=capturing_llm, conn=conn)

    assert calls == ["用户喜欢 Rust"]
    await conn.close()
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_score.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.scoring.score'`.

- [ ] **Step 3: Write `src/monitor/scoring/score.py`**

```python
from __future__ import annotations

from hashlib import sha256
from typing import Awaitable, Callable, Protocol, Sequence

import aiosqlite
import structlog

from monitor.config import ConfigFile
from monitor.db import (
    get_cached_llm_score,
    get_preference_profile,
    put_cached_llm_score,
)
from monitor.models import RepoCandidate
from monitor.scoring.heuristic import heuristic_score_readme
from monitor.scoring.rules import RuleEngine
from monitor.scoring.types import LLMScoreError, ScoreResult


log = structlog.get_logger(__name__)


class LLMScoreFn(Protocol):
    async def __call__(
        self,
        repo: RepoCandidate,
        *,
        interest_tags: Sequence[str],
        preference_profile: str | None,
    ) -> ScoreResult: ...


async def score_repo(
    repo: RepoCandidate,
    *,
    config: ConfigFile,
    rule_engine: RuleEngine,
    llm_score_fn: LLMScoreFn,
    conn: aiosqlite.Connection,
) -> None:
    """Populate scoring fields on `repo` in place.

    Pipeline:
      1. rule score (deterministic, always runs)
      2. llm_score_cache lookup by (full_name, sha256(readme))
         → on miss: call llm_score_fn; on LLMScoreError → heuristic fallback
      3. persist (full_name, sha256, result) into llm_score_cache for next run
      4. final_score = rule*α + llm*β from config.weights
    """
    repo.rule_score = rule_engine.score(repo)

    readme = repo.readme_text or ""
    readme_hash = sha256(readme.encode("utf-8")).hexdigest()
    cached = await get_cached_llm_score(conn, repo.full_name, readme_sha256=readme_hash)
    if cached is not None:
        result = cached
        source = "cache"
    else:
        profile = await get_preference_profile(conn)
        profile_text = (profile or {}).get("profile_text") or None
        try:
            result = await llm_score_fn(
                repo,
                interest_tags=list(config.keywords),
                preference_profile=profile_text,
            )
            source = "llm"
        except LLMScoreError as exc:
            log.warning(
                "score.llm_failed_fallback_heuristic",
                repo=repo.full_name,
                cause=exc.cause,
                error=str(exc),
            )
            result = heuristic_score_readme(repo, interest_tags=list(config.keywords))
            source = "heuristic"

        await put_cached_llm_score(
            conn,
            repo.full_name,
            readme_sha256=readme_hash,
            result=result,
        )

    repo.llm_score = result.score
    repo.readme_completeness = result.readme_completeness
    repo.summary = result.summary
    repo.recommendation_reason = result.reason
    repo.final_score = round(
        repo.rule_score * config.weights.rule + repo.llm_score * config.weights.llm,
        2,
    )
    log.info(
        "score.done",
        repo=repo.full_name,
        source=source,
        rule_score=repo.rule_score,
        llm_score=repo.llm_score,
        final_score=repo.final_score,
    )
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_score.py -v
```

Expected: **5 passed**.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/scoring/score.py tests/unit/test_score.py
git commit -m "feat(scoring/score): orchestrator combining rule + llm + cache + fallback"
```

---

## Task 8: Integration test — collect + enrich + score with mocked LLM

**Files:**
- Create: `tests/integration/test_pipeline_m3.py`

Context: End-to-end validation on top of M2's integration test — now including the scoring layer. Uses `respx` for GitHub and a `unittest.mock.AsyncMock`-backed `LLMClient` stand-in.

- [ ] **Step 1: Write the integration test**

```python
import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from monitor.clients.github import GitHubClient
from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.models import RepoCandidate
from monitor.pipeline.collect import collect_candidates
from monitor.pipeline.enrich import enrich_repo
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.scoring.types import ScoreResult
from tests.fixtures.github_payloads import (
    CONTRIBUTORS_PAYLOAD,
    ISSUES_CLOSED_PAYLOAD,
    README_RAW,
    REPO_DETAIL_WIDGET,
    SEARCH_REPOSITORIES_OK,
    TRENDING_HTML,
    events_payload,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m3.db"


@respx.mock
async def test_full_pipeline_with_mocked_llm(tmp_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor.clients.github._now_utc",
        lambda: dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc),
    )

    # GitHub mocks: full happy-path like M2 integration
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json=SEARCH_REPOSITORIES_OK)
    )
    respx.get("https://github.com/trending").mock(
        return_value=httpx.Response(200, text=TRENDING_HTML)
    )
    respx.get("https://api.github.com/repos/acme/widget").mock(
        return_value=httpx.Response(200, json=REPO_DETAIL_WIDGET)
    )
    respx.get("https://api.github.com/repos/acme/gear").mock(
        return_value=httpx.Response(
            200,
            json={**REPO_DETAIL_WIDGET, "full_name": "acme/gear", "html_url": "https://github.com/acme/gear"},
        )
    )
    respx.get("https://api.github.com/repos/acme/widget/events").mock(
        return_value=httpx.Response(200, json=events_payload(day_watches=5, week_watches=14))
    )
    respx.get("https://api.github.com/repos/acme/widget/contributors").mock(
        return_value=httpx.Response(200, json=CONTRIBUTORS_PAYLOAD)
    )
    respx.get("https://api.github.com/repos/acme/widget/issues").mock(
        return_value=httpx.Response(200, json=ISSUES_CLOSED_PAYLOAD)
    )
    respx.get("https://api.github.com/repos/acme/widget/readme").mock(
        return_value=httpx.Response(200, text=README_RAW)
    )

    # Set up DB
    conn = await connect(tmp_db)
    await run_migrations(conn)

    # Fake LLM: returns a deterministic ScoreResult
    llm_result = ScoreResult(
        score=8.5,
        readme_completeness=0.9,
        summary="Strong widget library",
        reason="Matches your agent interest",
        matched_interests=["agent"],
        red_flags=[],
    )
    fake_llm = AsyncMock(return_value=llm_result)

    config = ConfigFile(
        keywords=["llm"], languages=["Python"], min_stars=100,
        weights={"rule": 0.5, "llm": 0.5},
    )

    async with GitHubClient(token=None, request_timeout_s=5.0) as client:
        candidates = await collect_candidates(
            client, keywords=config.keywords, languages=config.languages, min_stars=config.min_stars
        )
        by_name = {r.full_name: r for r in candidates}
        widget = by_name["acme/widget"]
        await enrich_repo(client, widget)
        await score_repo(
            widget,
            config=config,
            rule_engine=RuleEngine(config),
            llm_score_fn=fake_llm,
            conn=conn,
        )

    # Assertions
    assert widget.rule_score > 0.0
    assert widget.llm_score == 8.5
    expected_final = round(widget.rule_score * 0.5 + 8.5 * 0.5, 2)
    assert widget.final_score == expected_final
    assert widget.summary == "Strong widget library"
    assert widget.readme_completeness == 0.9
    assert fake_llm.await_count == 1
    await conn.close()


@respx.mock
async def test_pipeline_falls_back_to_heuristic_when_llm_raises(
    tmp_db: Path, monkeypatch
) -> None:
    from monitor.scoring.types import LLMScoreError

    monkeypatch.setattr(
        "monitor.clients.github._now_utc",
        lambda: dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc),
    )

    respx.get("https://api.github.com/repos/acme/widget/events").mock(
        return_value=httpx.Response(200, json=events_payload(day_watches=3, week_watches=7))
    )
    respx.get("https://api.github.com/repos/acme/widget/contributors").mock(
        return_value=httpx.Response(200, json=CONTRIBUTORS_PAYLOAD)
    )
    respx.get("https://api.github.com/repos/acme/widget/issues").mock(
        return_value=httpx.Response(200, json=ISSUES_CLOSED_PAYLOAD)
    )
    respx.get("https://api.github.com/repos/acme/widget/readme").mock(
        return_value=httpx.Response(200, text=README_RAW)
    )

    conn = await connect(tmp_db)
    await run_migrations(conn)

    async def failing_llm(*args, **kwargs):
        raise LLMScoreError("simulated", cause="sdk_error")

    config = ConfigFile(keywords=["agent"])

    from monitor.clients.github import _repo_from_api
    widget = _repo_from_api(REPO_DETAIL_WIDGET)

    async with GitHubClient(token=None, request_timeout_s=5.0) as client:
        await enrich_repo(client, widget)
        await score_repo(
            widget,
            config=config,
            rule_engine=RuleEngine(config),
            llm_score_fn=failing_llm,
            conn=conn,
        )

    # Heuristic produced a score > 0; pipeline did not crash
    assert widget.llm_score > 0.0
    assert widget.final_score > 0.0
    assert widget.summary  # heuristic fills summary from description
    await conn.close()
```

- [ ] **Step 2: Run test**

```bash
pytest tests/integration/test_pipeline_m3.py -v
```

Expected: **2 passed**.

- [ ] **Step 3: Full suite regression**

```bash
pytest tests/ 2>&1 | tail -5
```

Expected: **all tests pass**. Running tally after M3: M2 baseline was 75; M3 adds 4 (types) + 7 (rules) + 5 (heuristic) + 7 (db_scoring_dao) + 8 (llm_client) + 4 (preference) + 5 (score) + 2 (integration) = 42 new → **117 total**.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_pipeline_m3.py
git commit -m "test(integration): collect+enrich+score end-to-end with mocked LLM"
```

---

## Task 9: Update `CLAUDE.md` with M3 additions

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append a new `### M3 additions` subsection at the very end of the Architecture section**

Locate the final paragraph of the existing `### M2 additions` subsection and append (preserving one blank line between):

```markdown

### M3 additions

`src/monitor/scoring/` holds four focused modules: `rules.py` (`RuleEngine` — coarse filter + deterministic rule score, ported from legacy with an injectable clock), `heuristic.py` (`heuristic_score_readme` — returns a `ScoreResult` so orchestrator treats fallback output identically to LLM output), `preference.py` (`PreferenceBuilder.regenerate()` — reads last 20 👍 and 20 👎 from `user_feedback`, prompts the LLM for a 250-300 字 preference description, persists to the single-row `preference_profile` table), and `score.py` (`score_repo()` orchestrator — rule score + cached-or-new LLM score + heuristic fallback + weighted `final_score`).

`src/monitor/clients/llm.py` is the MiniMax-via-Anthropic-SDK client. `LLMClient.score_repo()` sends a forced `submit_repo_score` tool use with strict schema (score 1-10, readme_completeness 0-1, summary ≤140 chars, reason ≤240 chars, matched_interests, red_flags). The system prompt has two ephemeral-cached blocks: the scoring rubric (constant) and the current preference profile (refreshes every N feedback). Any SDK error, missing tool_use block, or pydantic validation failure is re-raised as `LLMScoreError` — the orchestrator in `scoring/score.py` catches it and falls back to the heuristic scorer.

`llm_score_cache` (DB table from M1) is keyed by `(full_name, readme_sha256)`: orchestrator skips LLM entirely on cache hit, and writes the cache on both LLM and heuristic success. An unchanged README costs zero LLM calls.

Data model contract: `RepoCandidate` receives `rule_score`, `llm_score`, `final_score`, `summary`, `recommendation_reason`, `readme_completeness` from this layer. These were declared at default zero/empty in M2's model; M3 is the first stage that populates them.

Tests use DI-mocked Anthropic SDK (no respx for LLM, we stub `client._client.messages.create` with `AsyncMock`). The MiniMax endpoint's actual forced-tool-use / ephemeral-cache behavior is NOT exercised in CI — verifying it requires a one-off smoke script with real credentials, tracked as an operational TODO before M3 is wired into the scheduler in M5.
```

- [ ] **Step 2: Verify tests still green**

```bash
pytest tests/ 2>&1 | tail -3
```

Expected: all tests pass (doc-only change).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: extend CLAUDE.md architecture for M3 scoring + LLM client"
```

---

## M3 Verification Criteria

At the end of M3:

- [x] `pytest tests/` — **~117 passed** (M2 baseline 75 + M3's 42)
- [x] `src/monitor/scoring/` has 5 files: `types.py`, `rules.py`, `heuristic.py`, `preference.py`, `score.py` (+ `__init__.py` from M1 skeleton)
- [x] `src/monitor/clients/llm.py` exists with `LLMClient` + `SCORE_TOOL`
- [x] `src/monitor/db.py` has 4 new DAOs (`get_cached_llm_score`, `put_cached_llm_score`, `get_preference_profile`, `put_preference_profile`)
- [x] `monitor.legacy` unchanged (4 tests still pass)
- [x] `python -m monitor` still boots cleanly and exits on SIGTERM (M3 doesn't touch main.py)
- [x] CLAUDE.md has an M3 additions subsection

## Out of Scope

- Live smoke test against real MiniMax credentials (tracked as operational TODO for before M5 scheduler cutover)
- Wiring `score_repo` into `main.py` — M5 scheduler does that
- Auto-triggering `PreferenceBuilder.regenerate()` after feedback — M4 wires the TG button callback; M3 just provides the callable
- Multi-provider LLM abstraction — scope says Anthropic SDK only
- Retries on LLM failures — strict-or-fallback is simpler than tenacity here; fallback to heuristic IS the retry strategy
- Telegram bot / commands / buttons (M4)
- APScheduler jobs / surge poll / weekly digest (M5)
- systemd / healthcheck / backup (M6)
