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


async def test_rate_limiter_caps_anomalous_reset_header(monkeypatch) -> None:
    """A reset header far in the future (clock skew / malformed) must not
    block a coroutine for hours."""
    rl = RateLimiter()
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    ten_hours = now + dt.timedelta(hours=10)
    rl.update_from_headers({
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(int(ten_hours.timestamp())),
    })

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.rate_limit._utcnow", lambda: now)
    monkeypatch.setattr("monitor.clients.rate_limit.asyncio.sleep", fake_sleep)

    await rl.acquire(min_remaining=50)
    assert slept, "expected to sleep"
    # Cap is 3700s; the raw header wants 36000s.
    assert slept[0] <= 3700.0
