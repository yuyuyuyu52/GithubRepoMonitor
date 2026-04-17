import httpx
import pytest
import respx

from monitor.clients.github import GitHubClient, GitHubError
from tests.fixtures.github_payloads import SEARCH_REPOSITORIES_OK


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
