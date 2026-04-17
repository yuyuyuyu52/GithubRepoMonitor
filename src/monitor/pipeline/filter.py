from __future__ import annotations

import datetime as dt

import aiosqlite
import structlog

from monitor.db import is_blacklisted, pushed_cooldown_state
from monitor.models import RepoCandidate
from monitor.scoring.rules import RuleEngine


log = structlog.get_logger(__name__)


async def apply_filters(
    repos: list[RepoCandidate],
    *,
    rule_engine: RuleEngine,
    conn: aiosqlite.Connection,
    digest_cooldown_days: int,
    now: dt.datetime | None = None,
) -> list[RepoCandidate]:
    """Coarse filter stage: rule engine, blacklist (repo/author/topic),
    cooldown. Runs BEFORE enrichment so we don't waste API calls on repos
    that won't be pushed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    survivors: list[RepoCandidate] = []
    for repo in repos:
        if not rule_engine.apply(repo):
            log.debug("filter.rule_drop", repo=repo.full_name)
            continue

        if await is_blacklisted(conn, kind="repo", value=repo.full_name):
            log.info("filter.blacklist_drop", repo=repo.full_name, kind="repo")
            continue
        if await is_blacklisted(conn, kind="author", value=repo.owner_login):
            log.info("filter.blacklist_drop", repo=repo.full_name, kind="author")
            continue
        topic_hit = False
        for topic in repo.topics:
            if await is_blacklisted(conn, kind="topic", value=topic):
                log.info(
                    "filter.blacklist_drop",
                    repo=repo.full_name,
                    kind="topic",
                    value=topic,
                )
                topic_hit = True
                break
        if topic_hit:
            continue

        state = await pushed_cooldown_state(
            conn, repo.full_name, now, digest_days=digest_cooldown_days
        )
        if state == "active":
            log.debug("filter.cooldown_active", repo=repo.full_name)
            continue

        survivors.append(repo)
    return survivors
