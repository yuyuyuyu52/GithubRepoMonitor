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
