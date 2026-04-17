import datetime as dt

from monitor.models import RepoCandidate
from monitor.scoring.heuristic import heuristic_score_readme
from monitor.scoring.types import ScoreResult


def _repo(readme: str = "", description: str = "") -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="a/b",
        html_url="https://github.com/a/b",
        description=description,
        language="Python",
        stars=100,
        forks=10,
        created_at=now,
        pushed_at=now,
        owner_login="a",
        readme_text=readme,
    )


def test_heuristic_rewards_complete_readme_sections() -> None:
    readme = "# repo\n## install\n## usage\n## architecture\n## license"
    result = heuristic_score_readme(_repo(readme=readme), interest_tags=["agent"])
    assert isinstance(result, ScoreResult)
    assert result.readme_completeness == 1.0
    # Score should be mid-range since no interest tags matched
    assert 4.0 <= result.score <= 10.0


def test_heuristic_matches_interest_tags_in_readme_or_description() -> None:
    repo = _repo(readme="This is an LLM agent framework", description="LLM agent")
    low = heuristic_score_readme(_repo(readme="generic project"), interest_tags=["agent", "llm"])
    high = heuristic_score_readme(repo, interest_tags=["agent", "llm"])
    assert high.score > low.score
    assert set(high.matched_interests) == {"agent", "llm"}


def test_heuristic_falls_back_to_summary_from_description() -> None:
    result = heuristic_score_readme(_repo(description="neat tool"), interest_tags=[])
    assert "neat tool" in result.summary


def test_heuristic_summary_when_no_description() -> None:
    result = heuristic_score_readme(_repo(description=""), interest_tags=[])
    assert result.summary  # must not be empty


def test_heuristic_reason_mentions_match_counts() -> None:
    result = heuristic_score_readme(
        _repo(readme="# repo\n## install\nbuild an agent"),
        interest_tags=["agent"],
    )
    assert "agent" in result.reason.lower() or "1" in result.reason


def test_heuristic_truncates_summary_to_scoreresult_max_length() -> None:
    """ScoreResult.summary has max_length=140. A long GitHub description
    must be truncated, not crash the heuristic via ValidationError — that
    would defeat the "LLM failure → heuristic fallback" contract."""
    long_desc = "A" * 300
    # Must not raise
    result = heuristic_score_readme(_repo(description=long_desc), interest_tags=[])
    assert len(result.summary) <= 140
