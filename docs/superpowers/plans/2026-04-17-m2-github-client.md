# M2 GitHub Client + Pipeline (Collect/Enrich) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the demo's sync urllib-based `GitHubClient` with an async httpx client that respects primary + secondary rate limits, retries transient failures, and feeds the new `pipeline.collect` / `pipeline.enrich` stages. Enrich is per-field fault-tolerant: a single endpoint failure cannot abort the whole repo.

**Architecture:** `monitor/clients/github.py` owns all HTTP to GitHub. A primary `RateLimiter` tracks `X-RateLimit-Remaining` / `Reset` and blocks new requests when within 50 of exhaustion. A separate `SearchRateLimiter` enforces ≥2 s between `/search/repositories` calls (GitHub's secondary limit is 30/min). The request loop handles 429/secondary (`Retry-After`), 5xx (exp-backoff), network errors (exp-backoff), and raises all other 4xx as fatal. `pipeline/collect.py` assembles candidates via keyword×language search plus a Trending HTML scrape, deduped into a `dict` keyed by `full_name`. `pipeline/enrich.py` hits the Events/Issues/Contributors/README endpoints, each wrapped in isolated `try/except` — failure of one endpoint logs an entry to a `list[EnrichError]` but leaves other fields intact.

**Tech Stack:** `httpx` (async), `respx` (mock tests), `tenacity` (only in places where decorator form helps — most retry logic is explicit), domain model as `@dataclass(slots=True)`, structured errors as Python enums + dicts. No changes to `monitor.legacy` until M4.

---

## Background and Prerequisites

- **Branch state:** `m2-github-client` from `m1-scaffolding` (PR #2). M1 is complete (22 tests passing); `src/monitor/` has `config.py`, `db.py`, `logging_config.py`, `main.py`, `__main__.py`. Empty subpackage stubs exist for `clients/`, `pipeline/`, `scoring/`, `bot/`.
- **Legacy:** `src/monitor/legacy.py` stays exactly as-is through M2. Its 4 tests continue to pass. We do not modify, refactor, or partially import from it.
- **Dependencies:** M1's `pyproject.toml` already declares `httpx>=0.27`, `respx>=0.21` (dev), `tenacity>=8.2`, `structlog>=24.1`. No new deps.
- **Design source of truth:** `docs/superpowers/specs/2026-04-17-github-repo-monitor-productization-design.md`, §4 (data flow) and §7 (reliability).

## File Structure

**New source files**

- `src/monitor/models.py` — `RepoCandidate` dataclass + `EnrichError` dataclass. Shared domain model for M2-M5.
- `src/monitor/clients/rate_limit.py` — `RateLimiter` (primary) + `SearchRateLimiter` (secondary).
- `src/monitor/clients/github.py` — `GitHubClient` (async httpx) with 7 fetch methods.
- `src/monitor/pipeline/collect.py` — `Collector` / `collect_candidates()` orchestrator.
- `src/monitor/pipeline/enrich.py` — `enrich_repo()` orchestrator + error plumbing.

**New test files**

- `tests/fixtures/__init__.py` (empty package marker)
- `tests/fixtures/github_payloads.py` — canonical response dict literals + an HTML trending snippet.
- `tests/unit/test_models.py`
- `tests/unit/test_rate_limit.py`
- `tests/unit/test_github_client.py` — mocked with `respx`.
- `tests/unit/test_collect.py` — mocked client via protocol shim.
- `tests/unit/test_enrich.py` — mocked client, per-field failure isolation.
- `tests/integration/test_pipeline_m2.py` — end-to-end collect → enrich with respx.

**Documentation updates**

- `CLAUDE.md` — Architecture section extended with an `## M2 additions` subsection.

**Unchanged**

- `src/monitor/legacy.py`
- `src/monitor/config.py`, `db.py`, `logging_config.py`, `main.py`, `__main__.py`
- `tests/test_monitor.py` (legacy tests)
- `pyproject.toml`
- `README.md` (until M2 ships; will refresh in M5 when scheduler wires it in)

---

## Task 1: Fixture module — canonical GitHub payloads

**Files:**
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/github_payloads.py`

- [ ] **Step 1: Create empty package marker**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
touch tests/fixtures/__init__.py
```

- [ ] **Step 2: Write `tests/fixtures/github_payloads.py`**

```python
"""Canonical GitHub API response payloads for mocked tests.

Minimal but representative — each dict has the fields the production code
reads from. Do not add fields we don't parse; doing so creates maintenance
drag with no test value.
"""
from __future__ import annotations


SEARCH_REPOSITORIES_OK = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {
            "full_name": "acme/widget",
            "html_url": "https://github.com/acme/widget",
            "description": "Widgets for agents",
            "language": "Python",
            "stargazers_count": 420,
            "forks_count": 21,
            "created_at": "2026-01-05T12:00:00Z",
            "pushed_at": "2026-04-16T10:00:00Z",
            "owner": {"login": "acme"},
            "topics": ["agent", "llm"],
        },
        {
            "full_name": "acme/gear",
            "html_url": "https://github.com/acme/gear",
            "description": "Reliable gear",
            "language": "Python",
            "stargazers_count": 180,
            "forks_count": 9,
            "created_at": "2026-02-10T00:00:00Z",
            "pushed_at": "2026-04-17T00:00:00Z",
            "owner": {"login": "acme"},
            "topics": ["tooling"],
        },
    ],
}

REPO_DETAIL_WIDGET = {
    "full_name": "acme/widget",
    "html_url": "https://github.com/acme/widget",
    "description": "Widgets for agents",
    "language": "Python",
    "stargazers_count": 420,
    "forks_count": 21,
    "created_at": "2026-01-05T12:00:00Z",
    "pushed_at": "2026-04-16T10:00:00Z",
    "owner": {"login": "acme"},
    "topics": ["agent", "llm"],
}


def events_payload(day_watches: int = 5, week_watches: int = 12) -> list[dict]:
    """Build a /events response: `day_watches` within the last 24h, and
    `week_watches - day_watches` in the last 7d but older than 24h."""
    import datetime as dt
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    events: list[dict] = []
    for i in range(day_watches):
        events.append({
            "type": "WatchEvent",
            "created_at": (now - dt.timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    older = max(week_watches - day_watches, 0)
    for i in range(older):
        events.append({
            "type": "WatchEvent",
            "created_at": (now - dt.timedelta(days=1, hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    # Add some non-watch noise we should ignore.
    events.append({"type": "PushEvent", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return events


CONTRIBUTORS_PAYLOAD = [
    {"login": "alice", "contributions": 120},
    {"login": "bob", "contributions": 8},
    {"login": "carol", "contributions": 1},
    {"login": "dave", "contributions": 1},
]  # total 4, growth (contributions <= 1) == 2


ISSUES_CLOSED_PAYLOAD = [
    {
        "number": 10,
        "created_at": "2026-04-10T00:00:00Z",
        "closed_at": "2026-04-11T12:00:00Z",  # 36h
        "pull_request": None,
    },
    {
        "number": 11,
        "created_at": "2026-04-12T00:00:00Z",
        "closed_at": "2026-04-12T06:00:00Z",  # 6h
        "pull_request": None,
    },
    {
        "number": 12,
        "created_at": "2026-04-13T00:00:00Z",
        "closed_at": "2026-04-13T00:30:00Z",
        "pull_request": {"url": "..."},  # PR, must be skipped
    },
]  # expected mean = (36 + 6) / 2 = 21.0 hours


README_RAW = (
    "# acme/widget\n\n"
    "## Install\n```bash\npip install widget\n```\n\n"
    "## Usage\nSee docs.\n\n"
    "## License\nMIT\n"
)


TRENDING_HTML = """<!doctype html><html><body>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/widget">acme / widget</a></h2>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/gear">acme / gear</a></h2>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/widget">acme / widget</a></h2>
</article>
</body></html>"""


def rate_limit_headers(remaining: int = 4999, reset_epoch: int = 9999999999) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_epoch),
    }
```

- [ ] **Step 3: Verify the module imports cleanly**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
python -c "from tests.fixtures import github_payloads as p; print(len(p.SEARCH_REPOSITORIES_OK['items']), len(p.events_payload()))"
```

Expected: `2 13` (2 items in search, 13 events total — 5 today + 7 older + 1 PushEvent).

- [ ] **Step 4: Commit**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
git add tests/fixtures/__init__.py tests/fixtures/github_payloads.py
git commit -m "test(fixtures): add canonical GitHub API payloads for mocked tests"
```

---

## Task 2: Domain model — `monitor/models.py`

**Files:**
- Create: `src/monitor/models.py`
- Create: `tests/unit/test_models.py`

- [ ] **Step 1: Write failing test `tests/unit/test_models.py`**

```python
import datetime as dt

import pytest

from monitor.models import EnrichError, RepoCandidate


def test_repo_candidate_minimal_construction() -> None:
    repo = RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        owner_login="acme",
    )
    assert repo.full_name == "acme/widget"
    assert repo.topics == []
    assert repo.star_velocity_day == 0.0
    assert repo.readme_text == ""


def test_repo_candidate_topics_default_is_isolated() -> None:
    """Each instance must own its own topics list (no shared default)."""
    r1 = _make_min_repo("a/one")
    r2 = _make_min_repo("a/two")
    r1.topics.append("foo")
    assert r2.topics == []


def test_enrich_error_stores_step_and_message() -> None:
    err = EnrichError(step="events", message="HTTP 500", repo="acme/widget")
    assert err.step == "events"
    assert err.message == "HTTP 500"
    assert err.repo == "acme/widget"


def _make_min_repo(full_name: str) -> RepoCandidate:
    return RepoCandidate(
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description="",
        language="Python",
        stars=0,
        forks=0,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        owner_login=full_name.split("/")[0],
    )
```

- [ ] **Step 2: Verify test fails**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
pytest tests/unit/test_models.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.models'`.

- [ ] **Step 3: Write `src/monitor/models.py`**

```python
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List


@dataclass(slots=True)
class RepoCandidate:
    """Shared domain model for repos flowing through collect -> enrich -> score -> push.

    Fields are populated at distinct stages; downstream stages must not assume
    a field has been filled. Defaults represent "unknown" for numeric metrics.
    """

    # Populated by collect (search / trending / repo detail)
    full_name: str
    html_url: str
    description: str
    language: str
    stars: int
    forks: int
    created_at: dt.datetime
    pushed_at: dt.datetime
    owner_login: str
    topics: List[str] = field(default_factory=list)

    # Populated by enrich
    readme_text: str = ""
    star_velocity_day: float = 0.0
    star_velocity_week: float = 0.0
    fork_star_ratio: float = 0.0
    avg_issue_response_hours: float = 0.0
    contributor_count: int = 0
    contributor_growth_week: int = 0
    readme_completeness: float = 0.0

    # Populated by score (M3)
    rule_score: float = 0.0
    llm_score: float = 0.0
    final_score: float = 0.0
    summary: str = ""
    recommendation_reason: str = ""


@dataclass(slots=True)
class EnrichError:
    """One endpoint failure during enrich. Collected into run_log.stats.errors."""

    step: str
    message: str
    repo: str
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_models.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/models.py tests/unit/test_models.py
git commit -m "feat(models): RepoCandidate + EnrichError shared domain model"
```

---

## Task 3: Rate limiters — `monitor/clients/rate_limit.py`

**Files:**
- Create: `src/monitor/clients/rate_limit.py`
- Create: `tests/unit/test_rate_limit.py`

- [ ] **Step 1: Write failing test `tests/unit/test_rate_limit.py`**

```python
import asyncio
import datetime as dt
import time

import pytest

from monitor.clients.rate_limit import RateLimiter, SearchRateLimiter


async def test_rate_limiter_defaults_allow_requests(monkeypatch) -> None:
    rl = RateLimiter()
    # No headers seen yet — we trust GitHub's default 5000/hr and let the call through.
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.rate_limit.asyncio.sleep", fake_sleep)
    await rl.acquire()
    assert slept == []


async def test_rate_limiter_sleeps_when_remaining_below_threshold(monkeypatch) -> None:
    rl = RateLimiter()
    # Simulate headers saying we're almost out with a reset 30s from now.
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    reset_at = now + dt.timedelta(seconds=30)
    rl.update_from_headers({
        "X-RateLimit-Remaining": "10",
        "X-RateLimit-Reset": str(int(reset_at.timestamp())),
    })

    class _Clock:
        value = now

    def fake_utcnow() -> dt.datetime:
        return _Clock.value

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        _Clock.value = _Clock.value + dt.timedelta(seconds=s)

    monkeypatch.setattr("monitor.clients.rate_limit._utcnow", fake_utcnow)
    monkeypatch.setattr("monitor.clients.rate_limit.asyncio.sleep", fake_sleep)

    await rl.acquire(min_remaining=50)
    assert slept, "expected to sleep"
    assert 29 <= slept[0] <= 31


async def test_rate_limiter_does_not_sleep_when_reset_is_past(monkeypatch) -> None:
    rl = RateLimiter()
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    rl.update_from_headers({
        "X-RateLimit-Remaining": "10",
        "X-RateLimit-Reset": str(int((now - dt.timedelta(seconds=5)).timestamp())),
    })

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.rate_limit._utcnow", lambda: now)
    monkeypatch.setattr("monitor.clients.rate_limit.asyncio.sleep", fake_sleep)

    await rl.acquire(min_remaining=50)
    assert slept == []


async def test_search_limiter_enforces_min_interval(monkeypatch) -> None:
    limiter = SearchRateLimiter(min_interval_s=2.0)
    slept: list[float] = []

    class _Clock:
        value = 1000.0

    def fake_monotonic() -> float:
        return _Clock.value

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        _Clock.value += s

    monkeypatch.setattr("monitor.clients.rate_limit.time.monotonic", fake_monotonic)
    monkeypatch.setattr("monitor.clients.rate_limit.asyncio.sleep", fake_sleep)

    await limiter.acquire()  # first call — no wait
    assert slept == []

    _Clock.value += 0.5  # only 0.5s elapsed since first call
    await limiter.acquire()
    assert len(slept) == 1
    assert 1.49 <= slept[0] <= 1.51

    _Clock.value += 5.0  # well past threshold
    await limiter.acquire()
    assert len(slept) == 1  # no new sleep
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_rate_limit.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.clients.rate_limit'`.

- [ ] **Step 3: Write `src/monitor/clients/rate_limit.py`**

```python
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Mapping


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RateLimiter:
    """Tracks GitHub's primary rate limit (5000/hr authenticated).

    Reads X-RateLimit-Remaining / X-RateLimit-Reset from response headers after
    every request. Before a new request, if remaining < min_remaining and the
    reset time is still in the future, sleeps until the reset.
    """

    def __init__(self) -> None:
        self._remaining: int | None = None
        self._reset_at: dt.datetime | None = None
        self._lock = asyncio.Lock()

    async def acquire(self, min_remaining: int = 50) -> None:
        async with self._lock:
            if self._remaining is None or self._remaining >= min_remaining:
                return
            if self._reset_at is None:
                return
            wait_s = (self._reset_at - _utcnow()).total_seconds()
            if wait_s <= 0:
                return
            await asyncio.sleep(wait_s)

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self._remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self._reset_at = dt.datetime.fromtimestamp(int(reset), tz=dt.timezone.utc)
            except ValueError:
                pass


class SearchRateLimiter:
    """Secondary limit: /search endpoints are 30/min. Spacing 2s between
    calls stays under that with headroom."""

    def __init__(self, min_interval_s: float = 2.0) -> None:
        self._min_interval = min_interval_s
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if self._last_call and elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_rate_limit.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/rate_limit.py tests/unit/test_rate_limit.py
git commit -m "feat(clients): primary + secondary rate limiters for GitHub API"
```

---

## Task 4: GitHubClient core — constructor + `_request_json` + retry loop

This task establishes the request pipeline. Subsequent tasks (5-10) only add method-specific URL building and response parsing on top.

**Files:**
- Create: `src/monitor/clients/github.py`
- Create: `tests/unit/test_github_client.py`

- [ ] **Step 1: Write failing test `tests/unit/test_github_client.py`**

```python
import httpx
import pytest
import respx

from monitor.clients.github import GitHubClient, GitHubError


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient(token="ghp_test", request_timeout_s=5.0)


@respx.mock
async def test_request_json_sends_expected_headers(client: GitHubClient) -> None:
    route = respx.get("https://api.github.com/repos/a/b").mock(
        return_value=httpx.Response(200, json={"full_name": "a/b"})
    )
    async with client:
        data = await client._request_json("/repos/a/b")
    assert data == {"full_name": "a/b"}
    req = route.calls.last.request
    assert req.headers["User-Agent"] == "GithubRepoMonitor"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["Authorization"] == "Bearer ghp_test"


@respx.mock
async def test_request_json_retries_on_429_with_retry_after(client: GitHubClient, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    route = respx.get("https://api.github.com/repos/a/b").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, text="rate limit exceeded"),
            httpx.Response(200, json={"full_name": "a/b"}),
        ]
    )
    async with client:
        data = await client._request_json("/repos/a/b")
    assert data == {"full_name": "a/b"}
    assert route.call_count == 2
    assert slept == [3.0]


@respx.mock
async def test_request_json_retries_on_500_with_backoff(client: GitHubClient, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    route = respx.get("https://api.github.com/repos/a/b").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"full_name": "a/b"}),
        ]
    )
    async with client:
        data = await client._request_json("/repos/a/b")
    assert data == {"full_name": "a/b"}
    assert route.call_count == 3
    assert slept == [1.0, 2.0]


@respx.mock
async def test_request_json_retries_on_network_error(client: GitHubClient, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    route = respx.get("https://api.github.com/repos/a/b").mock(
        side_effect=[
            httpx.ConnectError("network down"),
            httpx.Response(200, json={"full_name": "a/b"}),
        ]
    )
    async with client:
        data = await client._request_json("/repos/a/b")
    assert data == {"full_name": "a/b"}
    assert slept == [1.0]


@respx.mock
async def test_request_json_raises_on_404(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with client:
        with pytest.raises(GitHubError) as exc_info:
            await client._request_json("/repos/a/b")
    assert exc_info.value.status_code == 404


@respx.mock
async def test_request_json_fails_after_max_retries(client: GitHubClient, monkeypatch) -> None:
    async def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    respx.get("https://api.github.com/repos/a/b").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with client:
        with pytest.raises(GitHubError):
            await client._request_json("/repos/a/b")


@respx.mock
async def test_request_json_updates_rate_limiter_from_headers(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b").mock(
        return_value=httpx.Response(
            200,
            json={},
            headers={"X-RateLimit-Remaining": "4321", "X-RateLimit-Reset": "9999999999"},
        )
    )
    async with client:
        await client._request_json("/repos/a/b")
    assert client.rate_limiter._remaining == 4321


async def test_client_without_token_omits_authorization_header() -> None:
    anon = GitHubClient(token=None, request_timeout_s=5.0)
    async with anon:
        headers = anon._base_headers()
    assert "Authorization" not in headers
    assert headers["User-Agent"] == "GithubRepoMonitor"
```

- [ ] **Step 2: Verify test fails**

```bash
pytest tests/unit/test_github_client.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'monitor.clients.github'`.

- [ ] **Step 3: Write `src/monitor/clients/github.py`**

```python
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from monitor.clients.rate_limit import RateLimiter, SearchRateLimiter


log = structlog.get_logger(__name__)

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "GithubRepoMonitor"

_RETRYABLE_NETWORK_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class GitHubError(Exception):
    """Non-retryable GitHub API error (4xx other than rate limit)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"GitHub {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class GitHubClient:
    """Async httpx client for the GitHub REST API.

    Handles:
      - User-Agent + optional Bearer auth headers
      - Primary rate limit (X-RateLimit-*) via RateLimiter.acquire()
      - Secondary rate limit on /search via SearchRateLimiter (applied from
        within search_repositories, not here)
      - 429 / secondary limit retry via Retry-After
      - 5xx retry with exponential backoff (1, 2, 4, 8 s capped at 30)
      - Network error retry with the same backoff
      - Max 4 attempts total; then raise GitHubError or the network exception

    The async context manager owns the underlying httpx.AsyncClient.
    """

    MAX_ATTEMPTS = 4
    BACKOFF_CAP_S = 30.0

    def __init__(
        self,
        token: str | None = None,
        *,
        request_timeout_s: float = 20.0,
        rate_limiter: RateLimiter | None = None,
        search_rate_limiter: SearchRateLimiter | None = None,
    ) -> None:
        self._token = token
        self._timeout_s = request_timeout_s
        self.rate_limiter = rate_limiter or RateLimiter()
        self.search_rate_limiter = search_rate_limiter or SearchRateLimiter()
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            timeout=self._timeout_s,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        return await self._retrying_request("GET", path, params=params, expect_json=True)

    async def _request_text(
        self,
        url: str,
        *,
        headers_override: dict[str, str] | None = None,
    ) -> str:
        return await self._retrying_request(
            "GET",
            url,
            headers_override=headers_override,
            expect_json=False,
        )

    async def _retrying_request(
        self,
        method: str,
        url_or_path: str,
        *,
        params: dict[str, str] | None = None,
        headers_override: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        assert self._http is not None, "GitHubClient used outside context manager"
        headers = headers_override or self._base_headers()

        last_error: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            await self.rate_limiter.acquire()
            try:
                resp = await self._http.request(
                    method,
                    url_or_path,
                    params=params,
                    headers=headers,
                )
            except _RETRYABLE_NETWORK_ERRORS as exc:
                last_error = exc
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(self._backoff(attempt))
                continue

            self.rate_limiter.update_from_headers(resp.headers)

            if resp.status_code == 429 or (
                resp.status_code == 403 and "rate limit" in resp.text.lower()
            ):
                retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                log.info(
                    "github.rate_limited",
                    status=resp.status_code,
                    retry_after_s=retry_after,
                )
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise GitHubError(resp.status_code, "rate limit exhausted")
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                log.info("github.server_error", status=resp.status_code, attempt=attempt)
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise GitHubError(resp.status_code, resp.text[:200])
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise GitHubError(resp.status_code, resp.text[:200])

            return resp.json() if expect_json else resp.text

        assert last_error is not None
        raise last_error

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(GitHubClient.BACKOFF_CAP_S, 2 ** attempt)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float:
        if not value:
            return 60.0
        try:
            return float(value)
        except ValueError:
            return 60.0
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): async core with retry, rate-limit, and 429/5xx handling"
```

---

## Task 5: `search_repositories`

**Files:**
- Modify: `src/monitor/clients/github.py`
- Modify: `tests/unit/test_github_client.py`

- [ ] **Step 1: Append tests to `tests/unit/test_github_client.py`**

Add near the top (after existing imports): `from tests.fixtures.github_payloads import SEARCH_REPOSITORIES_OK`.

Then append at the end of the file:

```python


@respx.mock
async def test_search_repositories_parses_items(client: GitHubClient) -> None:
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json=SEARCH_REPOSITORIES_OK)
    )
    async with client:
        repos = await client.search_repositories(
            keyword="llm", language="Python", min_stars=100
        )
    assert [r.full_name for r in repos] == ["acme/widget", "acme/gear"]
    assert repos[0].stars == 420
    assert repos[0].topics == ["agent", "llm"]
    assert repos[0].owner_login == "acme"


@respx.mock
async def test_search_repositories_waits_for_secondary_limiter(
    client: GitHubClient, monkeypatch
) -> None:
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    called: list[str] = []
    orig_search_acquire = client.search_rate_limiter.acquire

    async def instrumented_acquire() -> None:
        called.append("search")
        await orig_search_acquire()

    monkeypatch.setattr(client.search_rate_limiter, "acquire", instrumented_acquire)
    async with client:
        await client.search_repositories(keyword="llm", language="Python", min_stars=100)
    assert called == ["search"]


@respx.mock
async def test_search_repositories_builds_correct_query(client: GitHubClient) -> None:
    route = respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with client:
        await client.search_repositories(keyword="agent", language="Rust", min_stars=50)
    req = route.calls.last.request
    q = req.url.params.get("q")
    assert "agent" in q
    assert "language:Rust" in q
    assert "stars:>=50" in q
    assert "archived:false" in q
    assert req.url.params["sort"] == "stars"
    assert req.url.params["order"] == "desc"
    assert req.url.params["per_page"] == "30"
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_github_client.py::test_search_repositories_parses_items -v 2>&1 | tail -10
```

Expected: `AttributeError: 'GitHubClient' object has no attribute 'search_repositories'`.

- [ ] **Step 3: Append to `src/monitor/clients/github.py`**

Add this import near the top:

```python
import datetime as dt

from monitor.models import RepoCandidate
```

Add at the end of the `GitHubClient` class body:

```python
    async def search_repositories(
        self, *, keyword: str, language: str, min_stars: int
    ) -> list[RepoCandidate]:
        await self.search_rate_limiter.acquire()
        q = f"{keyword} language:{language} stars:>={min_stars} archived:false"
        params = {
            "q": q,
            "sort": "stars",
            "order": "desc",
            "per_page": "30",
        }
        payload = await self._request_json("/search/repositories", params=params)
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [_repo_from_api(item) for item in items]
```

Add this module-level helper function at the very bottom of the file:

```python
def _repo_from_api(item: dict) -> RepoCandidate:
    return RepoCandidate(
        full_name=item.get("full_name", ""),
        html_url=item.get("html_url", ""),
        description=item.get("description") or "",
        language=item.get("language") or "Unknown",
        stars=int(item.get("stargazers_count", 0)),
        forks=int(item.get("forks_count", 0)),
        created_at=_parse_dt(item.get("created_at", "1970-01-01T00:00:00Z")),
        pushed_at=_parse_dt(item.get("pushed_at", "1970-01-01T00:00:00Z")),
        owner_login=(item.get("owner") or {}).get("login", ""),
        topics=list(item.get("topics") or []),
    )


def _parse_dt(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 11 passed (8 previous + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): search_repositories with secondary rate limit"
```

---

## Task 6: `fetch_repository_detail`

**Files:**
- Modify: `src/monitor/clients/github.py`
- Modify: `tests/unit/test_github_client.py`

- [ ] **Step 1: Append tests**

Add near the existing imports: `from tests.fixtures.github_payloads import REPO_DETAIL_WIDGET`.

Append:

```python


@respx.mock
async def test_fetch_repository_detail_returns_candidate(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/acme/widget").mock(
        return_value=httpx.Response(200, json=REPO_DETAIL_WIDGET)
    )
    async with client:
        repo = await client.fetch_repository_detail("acme/widget")
    assert repo is not None
    assert repo.full_name == "acme/widget"
    assert repo.stars == 420


@respx.mock
async def test_fetch_repository_detail_returns_none_on_404(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/acme/missing").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with client:
        repo = await client.fetch_repository_detail("acme/missing")
    assert repo is None
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_github_client.py::test_fetch_repository_detail_returns_candidate -v
```

Expected: AttributeError.

- [ ] **Step 3: Add method to `GitHubClient`**

Append inside the class:

```python
    async def fetch_repository_detail(self, full_name: str) -> RepoCandidate | None:
        try:
            payload = await self._request_json(f"/repos/{full_name}")
        except GitHubError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(payload, dict):
            return None
        return _repo_from_api(payload)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 13 passed (11 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): fetch_repository_detail with 404 as None"
```

---

## Task 7: `fetch_trending_repositories`

**Files:**
- Modify: `src/monitor/clients/github.py`
- Modify: `tests/unit/test_github_client.py`

- [ ] **Step 1: Append tests**

Add to imports at top of test file: `from tests.fixtures.github_payloads import REPO_DETAIL_WIDGET, TRENDING_HTML`.

Append tests:

```python


@respx.mock
async def test_fetch_trending_repositories_scrapes_html_and_fetches_details(
    client: GitHubClient,
) -> None:
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
    async with client:
        repos = await client.fetch_trending_repositories()
    names = [r.full_name for r in repos]
    # Duplicates in HTML are deduped; order preserved.
    assert names == ["acme/widget", "acme/gear"]


@respx.mock
async def test_fetch_trending_repositories_skips_404_details(client: GitHubClient) -> None:
    respx.get("https://github.com/trending").mock(
        return_value=httpx.Response(200, text=TRENDING_HTML)
    )
    respx.get("https://api.github.com/repos/acme/widget").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.get("https://api.github.com/repos/acme/gear").mock(
        return_value=httpx.Response(
            200,
            json={**REPO_DETAIL_WIDGET, "full_name": "acme/gear", "html_url": "https://github.com/acme/gear"},
        )
    )
    async with client:
        repos = await client.fetch_trending_repositories()
    assert [r.full_name for r in repos] == ["acme/gear"]


@respx.mock
async def test_fetch_trending_repositories_returns_empty_on_html_failure(
    client: GitHubClient, monkeypatch
) -> None:
    async def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    respx.get("https://github.com/trending").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    async with client:
        repos = await client.fetch_trending_repositories()
    assert repos == []
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_github_client.py::test_fetch_trending_repositories_scrapes_html_and_fetches_details -v
```

Expected: AttributeError.

- [ ] **Step 3: Add to `GitHubClient`**

Add at top of `src/monitor/clients/github.py` (after existing imports):

```python
import re
```

Add a module-level constant:

```python
GITHUB_TRENDING_URL = "https://github.com/trending"
_TRENDING_SLUG_RE = re.compile(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"')
```

Append inside `GitHubClient`:

```python
    async def fetch_trending_repositories(self, *, max_repos: int = 20) -> list[RepoCandidate]:
        # httpx.AsyncClient honors a fully-qualified URL even when base_url is set,
        # so we can pass GITHUB_TRENDING_URL directly through _request_text.
        try:
            html = await self._request_text(GITHUB_TRENDING_URL)
        except (GitHubError, *_RETRYABLE_NETWORK_ERRORS):
            log.info("github.trending_fetch_failed")
            return []

        seen: set[str] = set()
        repos: list[RepoCandidate] = []
        for slug in _TRENDING_SLUG_RE.findall(html):
            if slug in seen:
                continue
            seen.add(slug)
            detail = await self.fetch_repository_detail(slug)
            if detail is not None:
                repos.append(detail)
            if len(repos) >= max_repos:
                break
        return repos
```

No changes to `_request_text` or `_retrying_request` are needed — they already accept any URL and httpx will route full URLs to their own host rather than `base_url`.

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 16 passed (13 + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): fetch_trending_repositories with HTML slug scrape"
```

---

## Task 8: `fetch_repo_events` (star velocity day/week)

**Files:**
- Modify: `src/monitor/clients/github.py`
- Modify: `tests/unit/test_github_client.py`

- [ ] **Step 1: Append tests**

Add to imports: `from tests.fixtures.github_payloads import events_payload`.

Append:

```python


@respx.mock
async def test_fetch_repo_events_counts_watch_events(client: GitHubClient, monkeypatch) -> None:
    import datetime as dt
    fixed_now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr("monitor.clients.github._now_utc", lambda: fixed_now)

    respx.get("https://api.github.com/repos/a/b/events").mock(
        return_value=httpx.Response(200, json=events_payload(day_watches=5, week_watches=14))
    )
    async with client:
        day, week = await client.fetch_repo_events("a/b")
    # 5 WatchEvents within last 24h; 14 within last 7d (mean per day = 2.0)
    assert day == 5.0
    assert week == pytest.approx(2.0)


@respx.mock
async def test_fetch_repo_events_returns_zeros_on_http_error(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b/events").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with client:
        day, week = await client.fetch_repo_events("a/b")
    assert (day, week) == (0.0, 0.0)
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_github_client.py::test_fetch_repo_events_counts_watch_events -v
```

Expected: AttributeError.

- [ ] **Step 3: Add to `GitHubClient`**

At module level (near the other module helpers), add:

```python
def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
```

Append inside `GitHubClient`:

```python
    async def fetch_repo_events(self, full_name: str) -> tuple[float, float]:
        """Returns (star_velocity_day, star_velocity_week) in stars/day.

        star_velocity_day = WatchEvent count within last 24h.
        star_velocity_week = WatchEvent count within last 7d / 7.
        """
        try:
            payload = await self._request_json(
                f"/repos/{full_name}/events", params={"per_page": "100"}
            )
        except GitHubError:
            return (0.0, 0.0)
        if not isinstance(payload, list):
            return (0.0, 0.0)

        now = _now_utc()
        day_ago = now - dt.timedelta(days=1)
        week_ago = now - dt.timedelta(days=7)
        day_count = 0
        week_count = 0
        for event in payload:
            if not isinstance(event, dict) or event.get("type") != "WatchEvent":
                continue
            created_raw = event.get("created_at")
            if not created_raw:
                continue
            try:
                created_at = _parse_dt(created_raw)
            except ValueError:
                continue
            if created_at >= week_ago:
                week_count += 1
                if created_at >= day_ago:
                    day_count += 1
        return (float(day_count), week_count / 7.0)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): fetch_repo_events for star velocity"
```

---

## Task 9: `fetch_contributors_growth` + `fetch_issue_response_hours`

**Files:**
- Modify: `src/monitor/clients/github.py`
- Modify: `tests/unit/test_github_client.py`

- [ ] **Step 1: Append tests**

Add to imports: `from tests.fixtures.github_payloads import CONTRIBUTORS_PAYLOAD, ISSUES_CLOSED_PAYLOAD`.

Append:

```python


@respx.mock
async def test_fetch_contributors_growth(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b/contributors").mock(
        return_value=httpx.Response(200, json=CONTRIBUTORS_PAYLOAD)
    )
    async with client:
        total, growth = await client.fetch_contributors_growth("a/b")
    assert total == 4
    assert growth == 2  # alice (120) and bob (8) are established; carol+dave are new


@respx.mock
async def test_fetch_contributors_growth_returns_zero_on_error(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b/contributors").mock(
        return_value=httpx.Response(403, json={"message": "Repository access blocked"})
    )
    async with client:
        total, growth = await client.fetch_contributors_growth("a/b")
    assert (total, growth) == (0, 0)


@respx.mock
async def test_fetch_issue_response_hours_averages_closed_issues(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b/issues").mock(
        return_value=httpx.Response(200, json=ISSUES_CLOSED_PAYLOAD)
    )
    async with client:
        hours = await client.fetch_issue_response_hours("a/b")
    # 36h and 6h (PR is skipped) -> mean 21.0
    assert hours == pytest.approx(21.0)


@respx.mock
async def test_fetch_issue_response_hours_returns_zero_when_no_real_issues(
    client: GitHubClient,
) -> None:
    respx.get("https://api.github.com/repos/a/b/issues").mock(
        return_value=httpx.Response(200, json=[{"pull_request": {"url": "x"}}])
    )
    async with client:
        hours = await client.fetch_issue_response_hours("a/b")
    assert hours == 0.0
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_github_client.py::test_fetch_contributors_growth -v
```

Expected: AttributeError.

- [ ] **Step 3: Add to `GitHubClient`**

Append inside the class:

```python
    async def fetch_contributors_growth(self, full_name: str) -> tuple[int, int]:
        """Returns (contributor_count, new_contributors_approx).

        new_contributors_approx = count of contributors with <= 1 contribution,
        used as a cheap proxy for recent joiners.
        """
        try:
            payload = await self._request_json(
                f"/repos/{full_name}/contributors", params={"per_page": "100"}
            )
        except GitHubError:
            return (0, 0)
        if not isinstance(payload, list):
            return (0, 0)
        total = len(payload)
        growth = 0
        for contributor in payload:
            if not isinstance(contributor, dict):
                continue
            try:
                if int(contributor.get("contributions", 0)) <= 1:
                    growth += 1
            except (TypeError, ValueError):
                continue
        return (total, growth)

    async def fetch_issue_response_hours(self, full_name: str) -> float:
        """Mean hours between open and close of the last up-to-10 real issues
        (PRs excluded) that were closed."""
        try:
            payload = await self._request_json(
                f"/repos/{full_name}/issues",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": "30",
                },
            )
        except GitHubError:
            return 0.0
        if not isinstance(payload, list):
            return 0.0

        intervals: list[float] = []
        for issue in payload:
            if not isinstance(issue, dict):
                continue
            if issue.get("pull_request"):
                continue
            created = issue.get("created_at")
            closed = issue.get("closed_at")
            if not created or not closed:
                continue
            try:
                delta = _parse_dt(closed) - _parse_dt(created)
            except ValueError:
                continue
            intervals.append(delta.total_seconds() / 3600.0)
            if len(intervals) >= 10:
                break
        if not intervals:
            return 0.0
        return sum(intervals) / len(intervals)
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): contributors growth + issue response time"
```

---

## Task 10: `fetch_readme`

**Files:**
- Modify: `src/monitor/clients/github.py`
- Modify: `tests/unit/test_github_client.py`

- [ ] **Step 1: Append tests**

Add to imports: `from tests.fixtures.github_payloads import README_RAW`.

Append:

```python


@respx.mock
async def test_fetch_readme_returns_raw_text(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b/readme").mock(
        return_value=httpx.Response(200, text=README_RAW)
    )
    async with client:
        text = await client.fetch_readme("a/b")
    assert text == README_RAW
    assert "## Install" in text


@respx.mock
async def test_fetch_readme_sends_raw_accept_header(client: GitHubClient) -> None:
    route = respx.get("https://api.github.com/repos/a/b/readme").mock(
        return_value=httpx.Response(200, text=README_RAW)
    )
    async with client:
        await client.fetch_readme("a/b")
    req = route.calls.last.request
    assert req.headers["Accept"] == "application/vnd.github.raw+json"


@respx.mock
async def test_fetch_readme_returns_empty_on_missing_readme(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/a/b/readme").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with client:
        text = await client.fetch_readme("a/b")
    assert text == ""
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_github_client.py::test_fetch_readme_returns_raw_text -v
```

Expected: AttributeError.

- [ ] **Step 3: Add to `GitHubClient`**

Append inside the class:

```python
    async def fetch_readme(self, full_name: str) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github.raw+json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            return await self._request_text(
                f"/repos/{full_name}/readme",
                headers_override=headers,
            )
        except GitHubError as exc:
            if exc.status_code == 404:
                return ""
            raise
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_github_client.py -v
```

Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/clients/github.py tests/unit/test_github_client.py
git commit -m "feat(clients/github): fetch_readme with raw accept header and 404 fallback"
```

---

## Task 11: `pipeline/collect.py`

**Files:**
- Create: `src/monitor/pipeline/collect.py`
- Create: `tests/unit/test_collect.py`

- [ ] **Step 1: Write failing test**

`tests/unit/test_collect.py`:

```python
import datetime as dt

import pytest

from monitor.models import RepoCandidate
from monitor.pipeline.collect import collect_candidates


def _repo(name: str) -> RepoCandidate:
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        owner_login=name.split("/")[0],
    )


class FakeClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str, int]] = []
        self.trending_calls = 0
        self._search_results: dict[tuple[str, str], list[RepoCandidate]] = {}
        self._trending: list[RepoCandidate] = []

    def set_search(self, keyword: str, language: str, repos: list[RepoCandidate]) -> None:
        self._search_results[(keyword, language)] = repos

    def set_trending(self, repos: list[RepoCandidate]) -> None:
        self._trending = repos

    async def search_repositories(self, *, keyword: str, language: str, min_stars: int):
        self.search_calls.append((keyword, language, min_stars))
        return list(self._search_results.get((keyword, language), []))

    async def fetch_trending_repositories(self):
        self.trending_calls += 1
        return list(self._trending)


async def test_collect_searches_cross_product_of_keywords_and_languages() -> None:
    client = FakeClient()
    client.set_search("llm", "Python", [_repo("a/one")])
    client.set_search("llm", "Rust", [_repo("a/two")])
    client.set_search("agent", "Python", [_repo("a/three")])
    client.set_search("agent", "Rust", [])

    repos = await collect_candidates(
        client,
        keywords=["llm", "agent"],
        languages=["Python", "Rust"],
        min_stars=100,
    )

    assert {r.full_name for r in repos} == {"a/one", "a/two", "a/three"}
    assert sorted(client.search_calls) == [
        ("agent", "Python", 100),
        ("agent", "Rust", 100),
        ("llm", "Python", 100),
        ("llm", "Rust", 100),
    ]
    assert client.trending_calls == 1


async def test_collect_dedupes_across_searches_and_trending() -> None:
    client = FakeClient()
    client.set_search("llm", "Python", [_repo("a/dup"), _repo("a/uniq")])
    client.set_trending([_repo("a/dup"), _repo("a/trend")])

    repos = await collect_candidates(
        client,
        keywords=["llm"],
        languages=["Python"],
        min_stars=100,
    )

    names = [r.full_name for r in repos]
    assert sorted(names) == ["a/dup", "a/trend", "a/uniq"]
    assert len(names) == len(set(names))


async def test_collect_tolerates_search_failure_for_single_pair(monkeypatch) -> None:
    client = FakeClient()
    client.set_search("llm", "Python", [_repo("a/ok")])
    client.set_trending([])

    orig = client.search_repositories

    async def flaky_search(**kwargs):
        if kwargs["language"] == "Rust":
            raise RuntimeError("boom")
        return await orig(**kwargs)

    monkeypatch.setattr(client, "search_repositories", flaky_search)

    repos = await collect_candidates(
        client,
        keywords=["llm"],
        languages=["Python", "Rust"],
        min_stars=100,
    )
    # Rust failed but Python's result survives
    assert [r.full_name for r in repos] == ["a/ok"]
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_collect.py -v
```

Expected: `ModuleNotFoundError: No module named 'monitor.pipeline.collect'`.

- [ ] **Step 3: Write `src/monitor/pipeline/collect.py`**

```python
from __future__ import annotations

from typing import Protocol, Sequence

import structlog

from monitor.models import RepoCandidate


log = structlog.get_logger(__name__)


class SupportsCandidateFetch(Protocol):
    async def search_repositories(
        self, *, keyword: str, language: str, min_stars: int
    ) -> list[RepoCandidate]: ...

    async def fetch_trending_repositories(self) -> list[RepoCandidate]: ...


async def collect_candidates(
    client: SupportsCandidateFetch,
    *,
    keywords: Sequence[str],
    languages: Sequence[str],
    min_stars: int,
) -> list[RepoCandidate]:
    """Run search across keyword x language + trending. Dedupe by full_name.

    Failures in individual search pairs or in trending are logged and swallowed;
    the caller still gets whatever succeeded.
    """
    collected: dict[str, RepoCandidate] = {}

    for keyword in keywords:
        for language in languages:
            try:
                repos = await client.search_repositories(
                    keyword=keyword, language=language, min_stars=min_stars
                )
            except Exception as exc:  # noqa: BLE001 - we log and proceed
                log.warning(
                    "collect.search_failed",
                    keyword=keyword,
                    language=language,
                    error=str(exc),
                )
                continue
            for repo in repos:
                collected.setdefault(repo.full_name, repo)

    try:
        trending = await client.fetch_trending_repositories()
    except Exception as exc:  # noqa: BLE001
        log.warning("collect.trending_failed", error=str(exc))
        trending = []
    for repo in trending:
        collected.setdefault(repo.full_name, repo)

    return list(collected.values())
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_collect.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/pipeline/collect.py tests/unit/test_collect.py
git commit -m "feat(pipeline/collect): dedupe candidates across search+trending with failure tolerance"
```

---

## Task 12: `pipeline/enrich.py` with per-field isolation

**Files:**
- Create: `src/monitor/pipeline/enrich.py`
- Create: `tests/unit/test_enrich.py`

- [ ] **Step 1: Write failing test**

`tests/unit/test_enrich.py`:

```python
import datetime as dt

import pytest

from monitor.models import EnrichError, RepoCandidate
from monitor.pipeline.enrich import enrich_repo


def _repo(name: str = "a/b", stars: int = 100, forks: int = 10) -> RepoCandidate:
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=stars,
        forks=forks,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        owner_login=name.split("/")[0],
    )


class FakeClient:
    def __init__(self) -> None:
        self.fail_steps: set[str] = set()
        self.events = (3.0, 0.5)
        self.contributors = (12, 2)
        self.issue_hours = 8.0
        self.readme = "# title\n## Install\n"

    async def fetch_repo_events(self, full_name: str):
        if "events" in self.fail_steps:
            raise RuntimeError("events down")
        return self.events

    async def fetch_contributors_growth(self, full_name: str):
        if "contributors" in self.fail_steps:
            raise RuntimeError("contributors down")
        return self.contributors

    async def fetch_issue_response_hours(self, full_name: str):
        if "issues" in self.fail_steps:
            raise RuntimeError("issues down")
        return self.issue_hours

    async def fetch_readme(self, full_name: str):
        if "readme" in self.fail_steps:
            raise RuntimeError("readme down")
        return self.readme


async def test_enrich_populates_all_metrics_on_happy_path() -> None:
    repo = _repo(stars=200, forks=50)
    client = FakeClient()

    errors = await enrich_repo(client, repo)

    assert errors == []
    assert repo.star_velocity_day == 3.0
    assert repo.star_velocity_week == 0.5
    assert repo.contributor_count == 12
    assert repo.contributor_growth_week == 2
    assert repo.avg_issue_response_hours == 8.0
    assert repo.readme_text == "# title\n## Install\n"
    # fork_star_ratio = forks / stars = 50/200 = 0.25
    assert repo.fork_star_ratio == pytest.approx(0.25)


async def test_enrich_fork_star_ratio_handles_zero_stars() -> None:
    repo = _repo(stars=0, forks=5)
    client = FakeClient()

    await enrich_repo(client, repo)

    assert repo.fork_star_ratio == 0.0


async def test_enrich_isolates_per_field_failure() -> None:
    repo = _repo()
    client = FakeClient()
    client.fail_steps = {"events", "readme"}

    errors = await enrich_repo(client, repo)

    # events + readme failed, contributors + issues succeeded
    assert {e.step for e in errors} == {"events", "readme"}
    assert repo.star_velocity_day == 0.0  # untouched default
    assert repo.readme_text == ""
    assert repo.contributor_count == 12
    assert repo.avg_issue_response_hours == 8.0


async def test_enrich_errors_carry_repo_full_name() -> None:
    repo = _repo("foo/bar")
    client = FakeClient()
    client.fail_steps = {"issues"}

    errors = await enrich_repo(client, repo)

    assert len(errors) == 1
    assert errors[0].repo == "foo/bar"
    assert errors[0].step == "issues"
```

- [ ] **Step 2: Verify tests fail**

```bash
pytest tests/unit/test_enrich.py -v
```

Expected: `ModuleNotFoundError: No module named 'monitor.pipeline.enrich'`.

- [ ] **Step 3: Write `src/monitor/pipeline/enrich.py`**

```python
from __future__ import annotations

from typing import Protocol

import structlog

from monitor.models import EnrichError, RepoCandidate


log = structlog.get_logger(__name__)


class SupportsEnrichFetch(Protocol):
    async def fetch_repo_events(self, full_name: str) -> tuple[float, float]: ...
    async def fetch_contributors_growth(self, full_name: str) -> tuple[int, int]: ...
    async def fetch_issue_response_hours(self, full_name: str) -> float: ...
    async def fetch_readme(self, full_name: str) -> str: ...


async def enrich_repo(
    client: SupportsEnrichFetch, repo: RepoCandidate
) -> list[EnrichError]:
    """Populate RepoCandidate enrichment fields in place.

    Each endpoint call is isolated. A failure in one step leaves its fields at
    their previous values (default zeros on a fresh candidate) and appends an
    EnrichError to the returned list. The list is intended to be merged into
    run_log.stats.errors by the caller.
    """
    errors: list[EnrichError] = []

    repo.fork_star_ratio = (repo.forks / repo.stars) if repo.stars else 0.0

    try:
        day_vel, week_vel = await client.fetch_repo_events(repo.full_name)
        repo.star_velocity_day = day_vel
        repo.star_velocity_week = week_vel
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.events_failed", repo=repo.full_name, error=str(exc))
        errors.append(EnrichError(step="events", message=str(exc), repo=repo.full_name))

    try:
        total, growth = await client.fetch_contributors_growth(repo.full_name)
        repo.contributor_count = total
        repo.contributor_growth_week = growth
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.contributors_failed", repo=repo.full_name, error=str(exc))
        errors.append(
            EnrichError(step="contributors", message=str(exc), repo=repo.full_name)
        )

    try:
        repo.avg_issue_response_hours = await client.fetch_issue_response_hours(
            repo.full_name
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.issues_failed", repo=repo.full_name, error=str(exc))
        errors.append(EnrichError(step="issues", message=str(exc), repo=repo.full_name))

    try:
        repo.readme_text = await client.fetch_readme(repo.full_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.readme_failed", repo=repo.full_name, error=str(exc))
        errors.append(EnrichError(step="readme", message=str(exc), repo=repo.full_name))

    return errors
```

- [ ] **Step 4: Tests pass**

```bash
pytest tests/unit/test_enrich.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/monitor/pipeline/enrich.py tests/unit/test_enrich.py
git commit -m "feat(pipeline/enrich): per-field tolerant enrichment with EnrichError trail"
```

---

## Task 13: End-to-end integration test — collect + enrich against mocked GitHub

**Files:**
- Create: `tests/integration/test_pipeline_m2.py`

- [ ] **Step 1: Write test**

```python
import httpx
import pytest
import respx

from monitor.clients.github import GitHubClient
from monitor.pipeline.collect import collect_candidates
from monitor.pipeline.enrich import enrich_repo
from tests.fixtures.github_payloads import (
    CONTRIBUTORS_PAYLOAD,
    ISSUES_CLOSED_PAYLOAD,
    README_RAW,
    REPO_DETAIL_WIDGET,
    SEARCH_REPOSITORIES_OK,
    TRENDING_HTML,
    events_payload,
)


@respx.mock
async def test_collect_then_enrich_happy_path(monkeypatch) -> None:
    import datetime as dt

    monkeypatch.setattr(
        "monitor.clients.github._now_utc",
        lambda: dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc),
    )

    # Search returns widget + gear for one (keyword, lang) pair.
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json=SEARCH_REPOSITORIES_OK)
    )
    # Trending page lists widget again (dedup) and gear.
    respx.get("https://github.com/trending").mock(
        return_value=httpx.Response(200, text=TRENDING_HTML)
    )
    # Trending detail fetches for both slugs.
    respx.get("https://api.github.com/repos/acme/widget").mock(
        return_value=httpx.Response(200, json=REPO_DETAIL_WIDGET)
    )
    respx.get("https://api.github.com/repos/acme/gear").mock(
        return_value=httpx.Response(
            200,
            json={
                **REPO_DETAIL_WIDGET,
                "full_name": "acme/gear",
                "html_url": "https://github.com/acme/gear",
            },
        )
    )
    # Enrichment endpoints for widget. Gear enrichment not exercised here.
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

    async with GitHubClient(token=None, request_timeout_s=5.0) as client:
        candidates = await collect_candidates(
            client, keywords=["llm"], languages=["Python"], min_stars=100
        )
        by_name = {r.full_name: r for r in candidates}
        assert set(by_name) == {"acme/widget", "acme/gear"}

        widget = by_name["acme/widget"]
        errors = await enrich_repo(client, widget)

    assert errors == []
    assert widget.star_velocity_day == 5.0
    assert widget.contributor_count == 4
    assert widget.avg_issue_response_hours == pytest.approx(21.0)
    assert "## Install" in widget.readme_text


@respx.mock
async def test_enrich_tolerates_one_endpoint_failure(monkeypatch) -> None:
    import datetime as dt

    monkeypatch.setattr(
        "monitor.clients.github._now_utc",
        lambda: dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc),
    )

    async def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)

    respx.get("https://api.github.com/repos/acme/widget/events").mock(
        return_value=httpx.Response(500, text="events boom")
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

    repo_payload = {
        "full_name": "acme/widget",
        "html_url": "https://github.com/acme/widget",
        "description": "",
        "language": "Python",
        "stargazers_count": 100,
        "forks_count": 10,
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-04-01T00:00:00Z",
        "owner": {"login": "acme"},
        "topics": [],
    }
    from monitor.clients.github import _repo_from_api

    widget = _repo_from_api(repo_payload)

    async with GitHubClient(token=None, request_timeout_s=5.0) as client:
        errors = await enrich_repo(client, widget)

    # events failed with retryable 500 -> GitHubError bubbled -> one EnrichError
    assert len(errors) == 1
    assert errors[0].step == "events"
    # Other fields still populated
    assert widget.contributor_count == 4
    assert widget.avg_issue_response_hours == pytest.approx(21.0)
    assert widget.readme_text == README_RAW
    # Event metrics untouched defaults
    assert widget.star_velocity_day == 0.0
```

- [ ] **Step 2: Verify tests pass**

```bash
cd /Users/Zhuanz/Documents/GithubRepoMonitor
source .venv/bin/activate
pytest tests/integration/test_pipeline_m2.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Full suite regression check**

```bash
pytest tests/ -v 2>&1 | tail -10
```

Expected: all tests pass. M1 baseline was 22; M2 adds: 3 (models) + 4 (rate_limit) + 25 (github_client) + 3 (collect) + 4 (enrich) + 2 (integration) = 41 new, totaling 63.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_pipeline_m2.py
git commit -m "test(integration): end-to-end collect+enrich against mocked GitHub"
```

---

## Task 14: Update `CLAUDE.md` with M2 additions

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append new section after existing "Legacy conventions" subsection**

Locate the last line of the Architecture section in `/Users/Zhuanz/Documents/GithubRepoMonitor/CLAUDE.md` (currently the "No-external-HTTP-client" bullet). Append immediately after, separated by a blank line:

```markdown

### M2 additions

`src/monitor/clients/github.py` is the async httpx client. All requests go through `_retrying_request`, which handles primary rate limits (via `RateLimiter.acquire()` before each call and header-driven state updates after), 429/secondary-limit retries that honor `Retry-After`, 5xx retries with exponential backoff (1/2/4/8 s capped at 30), and network-error retries under the same budget (max 4 attempts). `/search/repositories` additionally goes through `SearchRateLimiter` (2 s minimum spacing). 4xx other than rate-limit raise `GitHubError(status_code, message)` immediately.

`src/monitor/pipeline/collect.py` exposes `collect_candidates(client, keywords, languages, min_stars)` — keyword × language search cross-product plus trending scrape, deduped by `full_name`. Individual search-pair failures are logged and swallowed.

`src/monitor/pipeline/enrich.py` exposes `enrich_repo(client, repo) -> list[EnrichError]`. Each of the four enrichment endpoints (events, contributors, issues, readme) is tried in isolation; a failure there records an `EnrichError(step, message, repo)` but leaves other fields and the repo usable.

The shared domain model is `monitor.models.RepoCandidate` (`@dataclass(slots=True)`) plus `EnrichError`. Fields are populated at distinct stages: collect fills metadata; enrich fills metrics + readme; M3 will fill scoring fields; M4 will fill push metadata.

Tests use fixtures from `tests/fixtures/github_payloads.py` (canonical dict literals) with `respx` mocking httpx. No live GitHub calls in the suite yet — a live smoke test is deferred to M5 when the scheduler wires everything up.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: extend CLAUDE.md architecture for M2 client + pipeline modules"
```

---

## M2 Verification Criteria

At the end of M2 you should see:

- [x] `pytest tests/` — expected ~63 passing tests (22 from M1 + 41 from M2)
- [x] `src/monitor/clients/github.py` exists with 7 fetch methods + `GitHubError`
- [x] `src/monitor/clients/rate_limit.py` exports `RateLimiter` + `SearchRateLimiter`
- [x] `src/monitor/pipeline/collect.py` exports `collect_candidates`
- [x] `src/monitor/pipeline/enrich.py` exports `enrich_repo` + uses `EnrichError`
- [x] `src/monitor/models.py` exports `RepoCandidate` + `EnrichError`
- [x] `src/monitor/legacy.py` unchanged; its 4 tests still pass
- [x] `python -m monitor` still starts and exits cleanly on SIGTERM (M1 lifecycle intact — M2 did not touch main.py)
- [x] CLAUDE.md architecture section reflects M2 additions

## Out of Scope

- Wiring `collect_candidates` / `enrich_repo` into `main.py` (M5's scheduler will do this)
- Live-API smoke tests (deferred to M5)
- LLM scoring (M3)
- Telegram notification / feedback (M4)
- Systemd unit files, healthcheck, backup, logrotate (M6)
- Deleting `monitor.legacy` (not before M4 fully replaces its responsibilities)
