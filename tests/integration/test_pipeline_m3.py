import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from monitor.clients.github import GitHubClient
from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.models import RepoCandidate
from monitor.pipeline.collect import collect_candidates
from monitor.pipeline.enrich import enrich_repo
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.scoring.types import ScoreResult
from tests.fixtures.github_payloads import (
    CONTRIBUTORS_PAYLOAD,
    ISSUES_CLOSED_PAYLOAD,
    README_RAW,
    REPO_DETAIL_WIDGET,
    SEARCH_REPOSITORIES_OK,
    TRENDING_HTML,
    events_payload,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "m3.db"


@respx.mock
async def test_full_pipeline_with_mocked_llm(tmp_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor.clients.github._now_utc",
        lambda: dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc),
    )

    # GitHub mocks: full happy-path like M2 integration
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json=SEARCH_REPOSITORIES_OK)
    )
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

    # Set up DB
    conn = await connect(tmp_db)
    await run_migrations(conn)

    # Fake LLM: returns a deterministic ScoreResult
    llm_result = ScoreResult(
        score=8.5,
        readme_completeness=0.9,
        summary="Strong widget library",
        reason="Matches your agent interest",
        matched_interests=["agent"],
        red_flags=[],
    )
    fake_llm = AsyncMock(return_value=llm_result)

    config = ConfigFile(
        keywords=["llm"], languages=["Python"], min_stars=100,
        weights={"rule": 0.5, "llm": 0.5},
    )

    async with GitHubClient(token=None, request_timeout_s=5.0) as client:
        candidates = await collect_candidates(
            client, keywords=config.keywords, languages=config.languages, min_stars=config.min_stars
        )
        by_name = {r.full_name: r for r in candidates}
        widget = by_name["acme/widget"]
        await enrich_repo(client, widget)
        await score_repo(
            widget,
            config=config,
            rule_engine=RuleEngine(config),
            llm_score_fn=fake_llm,
            conn=conn,
        )

    # Assertions
    assert widget.rule_score > 0.0
    assert widget.llm_score == 8.5
    expected_final = round(widget.rule_score * 0.5 + 8.5 * 0.5, 2)
    assert widget.final_score == expected_final
    assert widget.summary == "Strong widget library"
    assert widget.readme_completeness == 0.9
    assert fake_llm.await_count == 1
    await conn.close()


@respx.mock
async def test_pipeline_falls_back_to_heuristic_when_llm_raises(
    tmp_db: Path, monkeypatch
) -> None:
    from monitor.scoring.types import LLMScoreError

    monkeypatch.setattr(
        "monitor.clients.github._now_utc",
        lambda: dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc),
    )

    respx.get("https://api.github.com/repos/acme/widget/events").mock(
        return_value=httpx.Response(200, json=events_payload(day_watches=3, week_watches=7))
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

    conn = await connect(tmp_db)
    await run_migrations(conn)

    async def failing_llm(*args, **kwargs):
        raise LLMScoreError("simulated", cause="sdk_error")

    config = ConfigFile(keywords=["agent"])

    from monitor.clients.github import _repo_from_api
    widget = _repo_from_api(REPO_DETAIL_WIDGET)

    async with GitHubClient(token=None, request_timeout_s=5.0) as client:
        await enrich_repo(client, widget)
        await score_repo(
            widget,
            config=config,
            rule_engine=RuleEngine(config),
            llm_score_fn=failing_llm,
            conn=conn,
        )

    # Heuristic produced a score > 0; pipeline did not crash
    assert widget.llm_score > 0.0
    assert widget.final_score > 0.0
    assert widget.summary  # heuristic fills summary from description
    await conn.close()
