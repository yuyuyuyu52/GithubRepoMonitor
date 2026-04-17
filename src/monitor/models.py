from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List


@dataclass(slots=True)
class RepoCandidate:
    """Shared domain model for repos flowing through collect -> enrich -> score -> push.

    Fields are populated at distinct stages; downstream stages must not assume
    a field has been filled. Defaults represent "unknown" for numeric metrics.
    """

    # Populated by collect (search / trending / repo detail)
    full_name: str
    html_url: str
    description: str
    language: str
    stars: int
    forks: int
    created_at: dt.datetime
    pushed_at: dt.datetime
    owner_login: str
    topics: List[str] = field(default_factory=list)

    # Populated by enrich
    readme_text: str = ""
    star_velocity_day: float = 0.0
    star_velocity_week: float = 0.0
    fork_star_ratio: float = 0.0
    avg_issue_response_hours: float = 0.0
    contributor_count: int = 0
    contributor_growth_week: int = 0
    readme_completeness: float = 0.0

    # Populated by score (M3)
    rule_score: float = 0.0
    llm_score: float = 0.0
    final_score: float = 0.0
    summary: str = ""
    recommendation_reason: str = ""


@dataclass(slots=True)
class EnrichError:
    """One endpoint failure during enrich. Collected into run_log.stats.errors."""

    step: str
    message: str
    repo: str
