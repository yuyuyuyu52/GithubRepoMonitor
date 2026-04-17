from __future__ import annotations

import asyncio
import re
import datetime as dt
from typing import Any

import httpx
import structlog

from monitor.clients.rate_limit import RateLimiter, SearchRateLimiter
from monitor.models import RepoCandidate


log = structlog.get_logger(__name__)

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "GithubRepoMonitor"
GITHUB_TRENDING_URL = "https://github.com/trending"
_TRENDING_SLUG_RE = re.compile(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"')

_RETRYABLE_NETWORK_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

# GitHub returns 403 for several retryable conditions (primary/secondary rate
# limit, abuse detection). Match any of these phrases case-insensitively in
# the response body to distinguish from non-retryable 403s (permission denied).
_RATE_LIMIT_BODY_PHRASES = (
    "rate limit",
    "abuse detection",
    "secondary rate limit",
)


class GitHubError(Exception):
    """Non-retryable GitHub API error (4xx other than rate limit)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"GitHub {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class GitHubClient:
    """Async httpx client for the GitHub REST API.

    Must be used as an async context manager exactly once::

        async with GitHubClient(token=...) as client:
            ...

    Entering a second time (without exiting in between) raises RuntimeError —
    that would leak the previous httpx.AsyncClient and its connection pool.

    Handles:
      - User-Agent + optional Bearer auth headers
      - Primary rate limit (X-RateLimit-*) via RateLimiter.acquire()
      - Secondary rate limit on /search via SearchRateLimiter (applied from
        within search_repositories, not here)
      - 429 / 403-with-rate-limit-phrases retry via Retry-After
      - 5xx retry with exponential backoff (1, 2, 4, 8 s capped at 30)
      - Network error retry with the same backoff
      - Max 4 attempts total; then raise GitHubError or the network exception
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
        if self._http is not None:
            raise RuntimeError(
                "GitHubClient is already entered; do not nest `async with` blocks"
            )
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
        if self._http is None:
            raise RuntimeError(
                "GitHubClient must be used inside `async with`"
            )
        # Merge override on top of defaults so callers can tweak a single
        # header (e.g. Accept for raw README) without losing Authorization.
        headers = {**self._base_headers(), **(headers_override or {})}

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
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise
                log.info(
                    "github.network_error",
                    url=url_or_path,
                    attempt=attempt,
                    error=str(exc),
                )
                await asyncio.sleep(self._backoff(attempt))
                continue

            self.rate_limiter.update_from_headers(resp.headers)

            if _is_rate_limit_response(resp):
                retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                log.info(
                    "github.rate_limited",
                    url=url_or_path,
                    status=resp.status_code,
                    retry_after_s=retry_after,
                )
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise GitHubError(resp.status_code, "rate limit exhausted")
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                log.info(
                    "github.server_error",
                    url=url_or_path,
                    status=resp.status_code,
                    attempt=attempt,
                )
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise GitHubError(resp.status_code, resp.text[:200])
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise GitHubError(resp.status_code, resp.text[:200])

            return resp.json() if expect_json else resp.text

        # All exit paths above either return, raise, or continue. The loop can
        # only reach this line if MAX_ATTEMPTS is 0, which the constant forbids.
        raise AssertionError("unreachable: retry loop exhausted without decision")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(GitHubClient.BACKOFF_CAP_S, 2 ** attempt)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float:
        # GitHub always sends integer seconds; RFC 7231 also allows HTTP-date
        # which we do not handle (fall back to 60s default).
        if not value:
            return 60.0
        try:
            return float(value)
        except ValueError:
            return 60.0

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

    async def fetch_trending_repositories(self, *, max_repos: int = 20) -> list[RepoCandidate]:
        # httpx.AsyncClient honors a fully-qualified URL even when base_url is
        # set, so we can pass GITHUB_TRENDING_URL directly through _request_text.
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

    async def fetch_readme(self, full_name: str) -> str:
        """Fetch raw README text. Returns empty string on 404."""
        try:
            return await self._request_text(
                f"/repos/{full_name}/readme",
                headers_override={"Accept": "application/vnd.github.raw+json"},
            )
        except GitHubError as exc:
            if exc.status_code == 404:
                return ""
            raise


def _is_rate_limit_response(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        body = resp.text.lower()
        return any(phrase in body for phrase in _RATE_LIMIT_BODY_PHRASES)
    return False


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


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
