import datetime as dt

import pytest

from monitor.models import EnrichError, RepoCandidate
from monitor.pipeline.enrich import enrich_repo


def _repo(name: str = "a/b", stars: int = 100, forks: int = 10) -> RepoCandidate:
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language="Python",
        stars=stars,
        forks=forks,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        pushed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        owner_login=name.split("/")[0],
    )


class FakeClient:
    def __init__(self) -> None:
        self.fail_steps: set[str] = set()
        self.events = (3.0, 0.5)
        self.contributors = (12, 2)
        self.issue_hours = 8.0
        self.readme = "# title\n## Install\n"

    async def fetch_repo_events(self, full_name: str):
        if "events" in self.fail_steps:
            raise RuntimeError("events down")
        return self.events

    async def fetch_contributors_growth(self, full_name: str):
        if "contributors" in self.fail_steps:
            raise RuntimeError("contributors down")
        return self.contributors

    async def fetch_issue_response_hours(self, full_name: str):
        if "issues" in self.fail_steps:
            raise RuntimeError("issues down")
        return self.issue_hours

    async def fetch_readme(self, full_name: str):
        if "readme" in self.fail_steps:
            raise RuntimeError("readme down")
        return self.readme


async def test_enrich_populates_all_metrics_on_happy_path() -> None:
    repo = _repo(stars=200, forks=50)
    client = FakeClient()

    errors = await enrich_repo(client, repo)

    assert errors == []
    assert repo.star_velocity_day == 3.0
    assert repo.star_velocity_week == 0.5
    assert repo.contributor_count == 12
    assert repo.contributor_growth_week == 2
    assert repo.avg_issue_response_hours == 8.0
    assert repo.readme_text == "# title\n## Install\n"
    # fork_star_ratio = forks / stars = 50/200 = 0.25
    assert repo.fork_star_ratio == pytest.approx(0.25)


async def test_enrich_fork_star_ratio_handles_zero_stars() -> None:
    repo = _repo(stars=0, forks=5)
    client = FakeClient()

    await enrich_repo(client, repo)

    assert repo.fork_star_ratio == 0.0


async def test_enrich_isolates_per_field_failure() -> None:
    repo = _repo()
    client = FakeClient()
    client.fail_steps = {"events", "readme"}

    errors = await enrich_repo(client, repo)

    # events + readme failed, contributors + issues succeeded
    assert {e.step for e in errors} == {"events", "readme"}
    assert repo.star_velocity_day == 0.0  # untouched default
    assert repo.readme_text == ""
    assert repo.contributor_count == 12
    assert repo.avg_issue_response_hours == 8.0


async def test_enrich_errors_carry_repo_full_name() -> None:
    repo = _repo("foo/bar")
    client = FakeClient()
    client.fail_steps = {"issues"}

    errors = await enrich_repo(client, repo)

    assert len(errors) == 1
    assert errors[0].repo == "foo/bar"
    assert errors[0].step == "issues"
