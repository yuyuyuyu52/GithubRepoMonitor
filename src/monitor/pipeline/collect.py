from __future__ import annotations

from typing import Protocol, Sequence

import structlog

from monitor.models import RepoCandidate


log = structlog.get_logger(__name__)


class SupportsCandidateFetch(Protocol):
    async def search_repositories(
        self, *, keyword: str, language: str, min_stars: int
    ) -> list[RepoCandidate]: ...

    async def fetch_trending_repositories(self) -> list[RepoCandidate]: ...


async def collect_candidates(
    client: SupportsCandidateFetch,
    *,
    keywords: Sequence[str],
    languages: Sequence[str],
    min_stars: int,
) -> list[RepoCandidate]:
    """Run search across keyword x language + trending. Dedupe by full_name.

    Failures in individual search pairs or in trending are logged and swallowed;
    the caller still gets whatever succeeded.
    """
    collected: dict[str, RepoCandidate] = {}

    for keyword in keywords:
        for language in languages:
            try:
                repos = await client.search_repositories(
                    keyword=keyword, language=language, min_stars=min_stars
                )
            except Exception as exc:  # noqa: BLE001 - we log and proceed
                log.warning(
                    "collect.search_failed",
                    keyword=keyword,
                    language=language,
                    error=str(exc),
                )
                continue
            for repo in repos:
                collected.setdefault(repo.full_name, repo)

    try:
        trending = await client.fetch_trending_repositories()
    except Exception as exc:  # noqa: BLE001
        log.warning("collect.trending_failed", error=str(exc))
        trending = []
    for repo in trending:
        collected.setdefault(repo.full_name, repo)

    return list(collected.values())
