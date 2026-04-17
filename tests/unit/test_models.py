import datetime as dt

import pytest

from monitor.models import EnrichError, RepoCandidate


def test_repo_candidate_minimal_construction() -> None:
    repo = RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="",
        language="Python",
        stars=100,
        forks=10,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        owner_login="acme",
    )
    assert repo.full_name == "acme/widget"
    assert repo.topics == []
    assert repo.star_velocity_day == 0.0
    assert repo.readme_text == ""


def test_repo_candidate_topics_default_is_isolated() -> None:
    """Each instance must own its own topics list (no shared default)."""
    r1 = _make_min_repo("a/one")
    r2 = _make_min_repo("a/two")
    r1.topics.append("foo")
    assert r2.topics == []


def test_enrich_error_stores_step_and_message() -> None:
    err = EnrichError(step="events", message="HTTP 500", repo="acme/widget")
    assert err.step == "events"
    assert err.message == "HTTP 500"
    assert err.repo == "acme/widget"


def _make_min_repo(full_name: str) -> RepoCandidate:
    return RepoCandidate(
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description="",
        language="Python",
        stars=0,
        forks=0,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        owner_login=full_name.split("/")[0],
    )
