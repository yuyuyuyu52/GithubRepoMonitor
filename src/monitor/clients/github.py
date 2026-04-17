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


def _is_rate_limit_response(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        body = resp.text.lower()
        return any(phrase in body for phrase in _RATE_LIMIT_BODY_PHRASES)
    return False
