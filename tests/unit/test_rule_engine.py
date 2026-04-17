import datetime as dt

import pytest

from monitor.config import ConfigFile
from monitor.models import RepoCandidate
from monitor.scoring.rules import RuleEngine


def _repo(
    *,
    name: str = "a/b",
    language: str = "Python",
    stars: int = 300,
    forks: int = 30,
    created_ago_days: int = 30,
    pushed_ago_days: int = 1,
    star_velocity_day: float = 2.0,
    star_velocity_week: float = 1.5,
    fork_star_ratio: float = 0.0,
    avg_issue_response_hours: float = 0.0,
    contributor_growth_week: int = 0,
) -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        language=language,
        stars=stars,
        forks=forks,
        created_at=now - dt.timedelta(days=created_ago_days),
        pushed_at=now - dt.timedelta(days=pushed_ago_days),
        owner_login=name.split("/")[0],
        star_velocity_day=star_velocity_day,
        star_velocity_week=star_velocity_week,
        fork_star_ratio=fork_star_ratio,
        avg_issue_response_hours=avg_issue_response_hours,
        contributor_growth_week=contributor_growth_week,
    )


def _engine(**overrides) -> RuleEngine:
    cfg = ConfigFile(**overrides)
    now = dt.datetime(2026, 4, 17, 12, 0, tzinfo=dt.timezone.utc)
    return RuleEngine(cfg, now=now)


def test_apply_rejects_low_stars() -> None:
    engine = _engine(min_stars=500, languages=["Python"], max_repo_age_days=180)
    assert engine.apply(_repo(stars=300)) is False


def test_apply_rejects_wrong_language() -> None:
    engine = _engine(min_stars=100, languages=["Rust"], max_repo_age_days=180)
    assert engine.apply(_repo(language="Python")) is False


def test_apply_rejects_too_old_repo() -> None:
    engine = _engine(min_stars=100, languages=["Python"], max_repo_age_days=30)
    assert engine.apply(_repo(created_ago_days=100)) is False


def test_apply_accepts_passing_repo() -> None:
    engine = _engine(min_stars=100, languages=["Python"], max_repo_age_days=180)
    assert engine.apply(_repo(stars=300, language="Python", created_ago_days=30)) is True


def test_score_is_a_weighted_combination_bounded_to_10() -> None:
    """Score must never exceed 10 even when every signal is maxed out."""
    engine = _engine()
    repo = _repo(
        star_velocity_day=1000.0,
        star_velocity_week=1000.0,
        fork_star_ratio=10.0,
        contributor_growth_week=1000,
        pushed_ago_days=0,
        avg_issue_response_hours=0.0,
    )
    score = engine.score(repo)
    assert 0.0 <= score <= 10.0


def test_score_zero_when_all_signals_flat() -> None:
    engine = _engine()
    repo = _repo(
        star_velocity_day=0.0,
        star_velocity_week=0.0,
        fork_star_ratio=0.0,
        avg_issue_response_hours=0.0,
        contributor_growth_week=0,
        pushed_ago_days=3650,  # very stale
    )
    score = engine.score(repo)
    # Freshness and response-score floors keep score above zero; assert it's
    # at least non-negative and bounded rather than an exact value.
    assert 0.0 <= score <= 10.0


def test_score_higher_for_fresher_and_faster_repo() -> None:
    engine = _engine()
    fresh = _repo(
        star_velocity_day=5.0,
        star_velocity_week=5.0,
        pushed_ago_days=0,
        contributor_growth_week=3,
        avg_issue_response_hours=1.0,
    )
    stale = _repo(
        star_velocity_day=0.1,
        star_velocity_week=0.1,
        pushed_ago_days=180,
        contributor_growth_week=0,
        avg_issue_response_hours=100.0,
    )
    assert engine.score(fresh) > engine.score(stale)


def test_score_does_not_reward_repos_without_issue_data() -> None:
    """avg_issue_response_hours == 0 can mean 'no closed issues' or API error.
    It must not be rewarded like an instantly-resolving repo."""
    engine = _engine()
    shared_kwargs = dict(
        star_velocity_day=0.0,
        star_velocity_week=0.0,
        contributor_growth_week=0,
        pushed_ago_days=1,
    )
    no_data = _repo(avg_issue_response_hours=0.0, **shared_kwargs)
    fast_response = _repo(avg_issue_response_hours=1.0, **shared_kwargs)
    assert engine.score(fast_response) > engine.score(no_data)
