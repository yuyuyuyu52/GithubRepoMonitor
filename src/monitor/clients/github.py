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
