from __future__ import annotations

import datetime as dt
from typing import Any, Literal

import aiosqlite
import structlog

from monitor.bot.push import push_repo
from monitor.db import (
    finish_run_log,
    start_run_log,
    upsert_repositories,
    upsert_repository_metrics,
)
from monitor.pipeline.collect import collect_candidates
from monitor.pipeline.enrich import enrich_repo
from monitor.pipeline.filter import apply_filters
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run_digest(
    *,
    push_type: Literal["digest", "surge"] = "digest",
    github_client: Any,
    llm_score_fn: Any,
    rule_engine: RuleEngine,
    state: DaemonState,
    conn: aiosqlite.Connection,
    bot_app: Any,
    chat_id: str,
    now: dt.datetime | None = None,
) -> dict:
    """Collect → filter → enrich → score → push. Writes a run_log entry.

    push_type="digest" is used for scheduled morning/evening runs and
    /digest_now. Surge has its own slimmer path (`pipeline/surge.py`) that
    reuses push_repo but skips the collect+filter stages.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if state.paused:
        log.info("digest.skipped_paused", push_type=push_type)
        return {"skipped": "paused"}

    run_id = await start_run_log(conn, kind=f"digest_{push_type}", now=now)
    stats: dict = {
        "repos_scanned": 0,
        "repos_pushed": 0,
        "llm_calls": 0,
        "enrich_errors": [],
        "fatal_error": None,
    }
    status: Literal["ok", "partial", "failed"] = "ok"

    try:
        candidates = await collect_candidates(
            github_client,
            keywords=list(state.config.keywords),
            languages=list(state.config.languages),
            min_stars=state.config.min_stars,
        )
        stats["repos_scanned"] = len(candidates)

        if candidates:
            await upsert_repositories(conn, candidates, now=now)

        survivors = await apply_filters(
            candidates,
            rule_engine=rule_engine,
            conn=conn,
            digest_cooldown_days=state.config.digest_cooldown_days,
            now=now,
        )

        top_n = state.config.top_n
        for repo in survivors[:top_n]:
            errors = await enrich_repo(github_client, repo)
            if errors:
                stats["enrich_errors"].extend(e.step for e in errors)
                status = "partial"
            await upsert_repository_metrics(conn, repo, now=now)

            await score_repo(
                repo,
                config=state.config,
                rule_engine=rule_engine,
                llm_score_fn=llm_score_fn,
                conn=conn,
            )
            stats["llm_calls"] += 1

            pushed_id = await push_repo(
                repo,
                bot_app=bot_app,
                chat_id=chat_id,
                conn=conn,
                push_type=push_type,
            )
            if pushed_id is not None:
                stats["repos_pushed"] += 1
    except Exception as exc:  # noqa: BLE001
        log.exception("digest.fatal", push_type=push_type)
        stats["fatal_error"] = str(exc)
        status = "failed"
    finally:
        await finish_run_log(
            conn, run_id=run_id, status=status, stats=stats,
            now=dt.datetime.now(dt.timezone.utc),
        )
    return stats
