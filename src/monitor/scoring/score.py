from __future__ import annotations

from hashlib import sha256
from typing import Protocol, Sequence

import aiosqlite
import structlog

from monitor.config import ConfigFile
from monitor.db import (
    get_cached_llm_score,
    get_preference_profile,
    put_cached_llm_score,
)
from monitor.models import RepoCandidate
from monitor.scoring.heuristic import heuristic_score_readme
from monitor.scoring.rules import RuleEngine
from monitor.scoring.types import LLMScoreError, ScoreResult


log = structlog.get_logger(__name__)


class LLMScoreFn(Protocol):
    async def __call__(
        self,
        repo: RepoCandidate,
        *,
        interest_tags: Sequence[str],
        preference_profile: str | None,
    ) -> ScoreResult: ...


async def score_repo(
    repo: RepoCandidate,
    *,
    config: ConfigFile,
    rule_engine: RuleEngine,
    llm_score_fn: LLMScoreFn,
    conn: aiosqlite.Connection,
) -> None:
    """Populate scoring fields on `repo` in place.

    Pipeline:
      1. rule score (deterministic, always runs)
      2. llm_score_cache lookup by (full_name, sha256(readme))
         → on miss: call llm_score_fn; on LLMScoreError → heuristic fallback
      3. persist (full_name, sha256, result) into llm_score_cache for next run
      4. final_score = rule*α + llm*β from config.weights
    """
    repo.rule_score = rule_engine.score(repo)

    readme = repo.readme_text or ""
    readme_hash = sha256(readme.encode("utf-8")).hexdigest()
    cached = await get_cached_llm_score(conn, repo.full_name, readme_sha256=readme_hash)
    if cached is not None:
        result = cached
        source = "cache"
    else:
        profile = await get_preference_profile(conn)
        profile_text = (profile or {}).get("profile_text") or None
        try:
            result = await llm_score_fn(
                repo,
                interest_tags=list(config.keywords),
                preference_profile=profile_text,
            )
            source = "llm"
        except LLMScoreError as exc:
            log.warning(
                "score.llm_failed_fallback_heuristic",
                repo=repo.full_name,
                cause=exc.cause,
                error=str(exc),
            )
            result = heuristic_score_readme(repo, interest_tags=list(config.keywords))
            source = "heuristic"

    # Mutate the repo BEFORE the cache write so a cache-write failure (e.g.
    # transient SQLite lock under future multi-writer scenarios) doesn't
    # silently drop an otherwise-valid LLM result. Cache is a performance
    # optimization for future runs; the current repo's score is the primary
    # output and must land on the model object regardless.
    repo.llm_score = result.score
    repo.readme_completeness = result.readme_completeness
    repo.summary = result.summary
    repo.recommendation_reason = result.reason
    repo.final_score = round(
        repo.rule_score * config.weights.rule + repo.llm_score * config.weights.llm,
        2,
    )

    if source != "cache":
        try:
            await put_cached_llm_score(
                conn,
                repo.full_name,
                readme_sha256=readme_hash,
                result=result,
            )
        except (aiosqlite.Error, OSError) as exc:
            log.warning(
                "score.cache_write_failed",
                repo=repo.full_name,
                source=source,
                error=str(exc),
            )
    log.info(
        "score.done",
        repo=repo.full_name,
        source=source,
        rule_score=repo.rule_score,
        llm_score=repo.llm_score,
        final_score=repo.final_score,
    )
