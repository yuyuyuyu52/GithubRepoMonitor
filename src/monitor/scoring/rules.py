from __future__ import annotations

import datetime as dt

from monitor.config import ConfigFile
from monitor.models import RepoCandidate


class RuleEngine:
    """Coarse filter + deterministic rule score.

    `apply(repo)` returns True if the repo passes the stars/language/age
    thresholds (used by the filter stage before enrichment).

    `score(repo)` returns a 0-10 weighted combination of enriched signals
    (star velocity, fork ratio, freshness, contributor growth, issue
    response). Ported from legacy.RuleEngine with modern types and an
    injectable `now` for tests.
    """

    def __init__(self, config: ConfigFile, *, now: dt.datetime | None = None) -> None:
        self._config = config
        self._now = now or dt.datetime.now(dt.timezone.utc)

    def apply(self, repo: RepoCandidate) -> bool:
        if repo.stars < self._config.min_stars:
            return False
        if repo.language not in self._config.languages:
            return False
        max_age = dt.timedelta(days=self._config.max_repo_age_days)
        if (self._now - repo.created_at) > max_age:
            return False
        return True

    def score(self, repo: RepoCandidate) -> float:
        ratio = repo.fork_star_ratio or 0.0
        freshness_days = max((self._now - repo.pushed_at).days, 0)
        freshness_score = max(0.0, 10.0 - freshness_days / 10.0)
        response_score = (
            10.0
            if repo.avg_issue_response_hours == 0
            else max(0.0, 10.0 - repo.avg_issue_response_hours / 24.0)
        )
        combined = (
            min(repo.star_velocity_day, 10.0) * 0.25
            + min(repo.star_velocity_week * 2, 10.0) * 0.2
            + min(ratio * 20, 10.0) * 0.1
            + freshness_score * 0.2
            + min(repo.contributor_growth_week, 10) * 0.1
            + response_score * 0.15
        )
        return round(min(combined, 10.0), 2)
