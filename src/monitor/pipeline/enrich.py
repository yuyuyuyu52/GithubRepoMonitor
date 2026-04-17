from __future__ import annotations

import asyncio
from typing import Protocol

import structlog

from monitor.models import EnrichError, RepoCandidate


log = structlog.get_logger(__name__)


class SupportsEnrichFetch(Protocol):
    async def fetch_repo_events(self, full_name: str) -> tuple[float, float]: ...
    async def fetch_contributors_growth(self, full_name: str) -> tuple[int, int]: ...
    async def fetch_issue_response_hours(self, full_name: str) -> float: ...
    async def fetch_readme(self, full_name: str) -> str: ...


async def enrich_repo(
    client: SupportsEnrichFetch, repo: RepoCandidate
) -> list[EnrichError]:
    """Populate RepoCandidate enrichment fields in place.

    Each endpoint call is isolated. A failure in one step leaves its fields at
    their previous values (default zeros on a fresh candidate) and appends an
    EnrichError to the returned list. The list is intended to be merged into
    run_log.stats.errors by the caller.

    asyncio.CancelledError is re-raised explicitly in every catch so a
    scheduler-driven shutdown propagates promptly. (On 3.11+ CancelledError
    inherits from BaseException, so `except Exception` already misses it, but
    the explicit re-raise makes the intent loud and keeps us robust against
    custom exception hierarchies that might chain Cancelled onto Exception.)
    """
    errors: list[EnrichError] = []

    repo.fork_star_ratio = (repo.forks / repo.stars) if repo.stars else 0.0

    try:
        day_vel, week_vel = await client.fetch_repo_events(repo.full_name)
        repo.star_velocity_day = day_vel
        repo.star_velocity_week = week_vel
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.events_failed", repo=repo.full_name, error=str(exc))
        errors.append(EnrichError(step="events", message=str(exc), repo=repo.full_name))

    try:
        total, growth = await client.fetch_contributors_growth(repo.full_name)
        repo.contributor_count = total
        repo.contributor_growth_week = growth
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.contributors_failed", repo=repo.full_name, error=str(exc))
        errors.append(
            EnrichError(step="contributors", message=str(exc), repo=repo.full_name)
        )

    try:
        repo.avg_issue_response_hours = await client.fetch_issue_response_hours(
            repo.full_name
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.issues_failed", repo=repo.full_name, error=str(exc))
        errors.append(EnrichError(step="issues", message=str(exc), repo=repo.full_name))

    try:
        repo.readme_text = await client.fetch_readme(repo.full_name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich.readme_failed", repo=repo.full_name, error=str(exc))
        errors.append(EnrichError(step="readme", message=str(exc), repo=repo.full_name))

    return errors
