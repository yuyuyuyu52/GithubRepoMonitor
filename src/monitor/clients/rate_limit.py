from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Mapping


# Cap any sleep on a rate-limit wall. GitHub's primary limit resets hourly;
# anything longer than one cycle + buffer is anomalous (clock skew, malformed
# Retry-After header, etc.) and we'd rather wake up early and retry the
# request than stall the daemon for hours invisibly.
_MAX_SLEEP_S = 3700.0


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
        """Block until it is safe to make a request.

        Holds the lock across ``await asyncio.sleep`` so that exactly one
        coroutine sleeps per rate-limit window — the N-1 queued coroutines
        drain serially after reset instead of forming a thundering herd at
        the same instant.

        State is NOT re-checked after waking: if another coroutine updated
        the headers via ``update_from_headers`` during the sleep, those
        changes take effect on the next ``acquire()`` call, not this one.
        Sleep is capped at ``_MAX_SLEEP_S`` (~1 reset cycle + buffer) to
        defend against malformed reset headers or extreme clock skew.
        """
        async with self._lock:
            if self._remaining is None or self._remaining >= min_remaining:
                return
            if self._reset_at is None:
                return
            wait_s = (self._reset_at - _utcnow()).total_seconds()
            if wait_s <= 0:
                return
            await asyncio.sleep(min(wait_s, _MAX_SLEEP_S))

    def update_from_headers(self, headers: Mapping[str, object]) -> None:
        # GitHub returns integer epoch strings. Float formats, non-strings, and
        # out-of-range / huge epoch values are all silently ignored rather than
        # crashing the caller — a broken header leaves the limiter state on its
        # previous value, which is far better than tearing down a digest run.
        #
        # X-RateLimit-Resource tells us which quota these headers describe.
        # `/search/*` returns resource="search" with a 30/min budget, which is
        # orders of magnitude smaller than the 5000/hr core budget. Writing
        # those numbers into this limiter would cause every subsequent core
        # call to sleep until the search reset fires — a "remaining=29 < 50"
        # comparison is meaningless when mixing resources. SearchRateLimiter
        # already covers the /search spacing independently, so we just ignore
        # non-core headers here.
        resource = headers.get("X-RateLimit-Resource")
        if resource is not None and str(resource) != "core":
            return
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self._remaining = int(remaining)
            except (TypeError, ValueError):
                pass
        if reset is not None:
            try:
                self._reset_at = dt.datetime.fromtimestamp(
                    int(reset), tz=dt.timezone.utc
                )
            except (TypeError, ValueError, OverflowError, OSError):
                pass


class SearchRateLimiter:
    """Secondary limit: /search endpoints are 30/min. Spacing 2s between
    calls stays under that with headroom."""

    def __init__(self, min_interval_s: float = 2.0) -> None:
        self._min_interval = min_interval_s
        self._last_call: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            if self._last_call is not None:
                elapsed = time.monotonic() - self._last_call
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()
