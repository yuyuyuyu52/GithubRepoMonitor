from __future__ import annotations

import datetime as dt
from typing import Any

import aiosqlite
import structlog

from monitor.bot.push import push_repo
from monitor.db import (
    finish_run_log,
    get_latest_metric,
    get_surge_candidates,
    start_run_log,
    upsert_repository_metrics,
)
from monitor.models import RepoCandidate
from monitor.pipeline.enrich import enrich_repo
from monitor.scoring.rules import RuleEngine
from monitor.scoring.score import score_repo
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


async def run_surge(
    *,
    github_client: Any,
    llm_score_fn: Any,
    rule_engine: RuleEngine,
    state: DaemonState,
    conn: aiosqlite.Connection,
    bot_app: Any,
    chat_id: str,
    now: dt.datetime | None = None,
) -> dict:
    """Scan known repositories (cooldown expired) for velocity surges.

    For each candidate: fetch events (one API call), compare to the last
    metrics row, and if day_velocity * surge.velocity_multiple crossed
    AND surge.velocity_absolute_day is exceeded → enrich + score + push
    with the surge tag."""
    now = now or dt.datetime.now(dt.timezone.utc)

    if state.paused:
        log.info("surge.skipped_paused")
        return {"skipped": "paused"}

    run_id = await start_run_log(conn, kind="surge", now=now)
    stats: dict = {"candidates": 0, "surged": 0, "errors": []}
    status: str = "ok"

    try:
        surge_cfg = state.config.surge
        candidates = await get_surge_candidates(
            conn, now=now, cooldown_days=surge_cfg.cooldown_days
        )
        stats["candidates"] = len(candidates)

        for cand in candidates:
            full_name = cand["full_name"]
            try:
                day_v_new, week_v_new = await github_client.fetch_repo_events(full_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("surge.events_failed", repo=full_name, error=str(exc))
                stats["errors"].append(full_name)
                continue

            latest = await get_latest_metric(conn, full_name)
            day_v_old = (latest or {}).get("star_velocity_day") or 0.0

            # Multiplier threshold: avoid division by zero by treating old=0
            # as "no baseline" and requiring the absolute threshold only.
            if day_v_old > 0:
                ratio_ok = day_v_new >= day_v_old * surge_cfg.velocity_multiple
            else:
                ratio_ok = True
            absolute_ok = day_v_new >= surge_cfg.velocity_absolute_day

            if not (ratio_ok and absolute_ok):
                continue

            # Reconstitute RepoCandidate from the repositories row.
            repo = RepoCandidate(
                full_name=full_name,
                html_url=cand["html_url"] or f"https://github.com/{full_name}",
                description=cand["description"],
                language=cand["language"],
                stars=0,  # enrich does not refresh stars; carry 0 is fine for scoring
                forks=0,
                created_at=_parse_iso_utc(cand["created_at"]) or now,
                pushed_at=now,
                owner_login=cand["owner_login"],
                topics=list(cand["topics"]),
                star_velocity_day=day_v_new,
                star_velocity_week=week_v_new,
            )
            errors = await enrich_repo(github_client, repo)
            if errors:
                stats["errors"].extend(e.step for e in errors)
                status = "partial"
            await upsert_repository_metrics(conn, repo, now=now)
            await score_repo(
                repo,
                config=state.config,
                rule_engine=rule_engine,
                llm_score_fn=llm_score_fn,
                conn=conn,
            )
            pushed_id = await push_repo(
                repo, bot_app=bot_app, chat_id=chat_id, conn=conn, push_type="surge"
            )
            if pushed_id is not None:
                stats["surged"] += 1
    except Exception as exc:  # noqa: BLE001
        log.exception("surge.fatal")
        stats["fatal_error"] = str(exc)
        status = "failed"
    finally:
        await finish_run_log(
            conn, run_id=run_id, status=status, stats=stats,
            now=dt.datetime.now(dt.timezone.utc),
        )
    return stats


def _parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)
