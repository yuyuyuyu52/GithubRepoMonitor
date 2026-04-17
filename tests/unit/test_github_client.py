import httpx
import pytest
import respx

from monitor.clients.github import GitHubClient, GitHubError
from tests.fixtures.github_payloads import (
    CONTRIBUTORS_PAYLOAD,
    ISSUES_CLOSED_PAYLOAD,
    REPO_DETAIL_WIDGET,
    SEARCH_REPOSITORIES_OK,
    TRENDING_HTML,
    events_payload,
)


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


async def test_client_double_enter_raises() -> None:
    c = GitHubClient(token=None, request_timeout_s=5.0)
    async with c:
        with pytest.raises(RuntimeError, match="already entered"):
            await c.__aenter__()


async def test_request_without_context_manager_raises() -> None:
    c = GitHubClient(token=None, request_timeout_s=5.0)
    with pytest.raises(RuntimeError, match="async with"):
        await c._request_json("/repos/a/b")


@respx.mock
async def test_request_json_retries_on_403_abuse_detection(
    client: GitHubClient, monkeypatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    respx.get("https://api.github.com/repos/a/b").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"Retry-After": "7"},
                text="You have triggered an abuse detection mechanism.",
            ),
            httpx.Response(200, json={"full_name": "a/b"}),
        ]
    )
    async with client:
        data = await client._request_json("/repos/a/b")
    assert data == {"full_name": "a/b"}
    assert slept == [7.0]


@respx.mock
async def test_request_json_exhausts_retries_on_persistent_429(
    client: GitHubClient, monkeypatch
) -> None:
    async def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("monitor.clients.github.asyncio.sleep", fake_sleep)
    respx.get("https://api.github.com/repos/a/b").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"}, text="rate limit")
    )
    async with client:
        with pytest.raises(GitHubError) as exc_info:
            await client._request_json("/repos/a/b")
    assert exc_info.value.status_code == 429
    assert "rate limit exhausted" in exc_info.value.message


@respx.mock
async def test_headers_override_merges_with_defaults(client: GitHubClient) -> None:
    """Passing an Accept override must not drop Authorization / User-Agent."""
    route = respx.get("https://api.github.com/repos/a/b/readme").mock(
        return_value=httpx.Response(200, text="# hi")
    )
    async with client:
        await client._request_text(
            "/repos/a/b/readme",
            headers_override={"Accept": "application/vnd.github.raw+json"},
        )
    req = route.calls.last.request
    assert req.headers["Accept"] == "application/vnd.github.raw+json"
    assert req.headers["User-Agent"] == "GithubRepoMonitor"
    assert req.headers["Authorization"] == "Bearer ghp_test"


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
