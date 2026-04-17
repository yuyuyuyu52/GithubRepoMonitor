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
