from __future__ import annotations

from typing import Any, Protocol

import aiosqlite
import structlog

from monitor.bot.render import parse_callback_data
from monitor.db import (
    add_blacklist_entry,
    count_feedback_since_last_profile,
    record_user_feedback,
)


log = structlog.get_logger(__name__)


class PreferenceBuilderLike(Protocol):
    async def regenerate(self) -> Any: ...


async def handle_feedback_callback(
    update: Any,
    *,
    conn: aiosqlite.Connection,
    pref_builder: PreferenceBuilderLike,
    refresh_threshold: int,
    allowed_chat_id: str | None = None,
) -> None:
    """Handle one feedback-button press.

    `update` is a PTB Update-shaped object — we only touch
    `update.callback_query.{data,answer,edit_message_text}` so tests can
    pass a `SimpleNamespace` without spinning up a real PTB app.
    """
    cq = update.callback_query

    # Chat-id gate: PTB's CallbackQueryHandler doesn't compose cleanly with
    # filters.Chat, so we enforce here. None means no enforcement (tests).
    if allowed_chat_id is not None:
        msg = getattr(cq, "message", None)
        chat = getattr(msg, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is None or str(chat_id) != str(allowed_chat_id):
            log.info("feedback.rejected_foreign_chat", chat_id=chat_id)
            try:
                await cq.answer()
            except Exception:  # noqa: BLE001
                pass
            return

    await cq.answer()  # always ack so the TG button stops spinning

    parsed = parse_callback_data(cq.data or "")
    if parsed is None:
        log.info("feedback.invalid_callback_data", data=cq.data)
        return
    action, push_id = parsed

    repo_snapshot = await _load_repo_snapshot(conn, push_id)
    if not repo_snapshot:
        # Stale button: push_id refers to a pushed_items row that no longer
        # exists (DB wiped / different bot / older test run). Writing
        # user_feedback would fail the FK; instead acknowledge and move on.
        log.info("feedback.stale_push_id", push_id=push_id, action=action)
        try:
            await cq.edit_message_text("⚠️ 这条推送的记录已不存在（DB 已重置？）")
        except Exception as exc:  # noqa: BLE001
            log.warning("feedback.edit_stale_failed", push_id=push_id, error=str(exc))
        return

    await record_user_feedback(
        conn,
        push_id=push_id,
        action=action,
        repo_snapshot=repo_snapshot,
    )

    if action == "block_author":
        owner = repo_snapshot.get("owner_login")
        if owner:
            await add_blacklist_entry(
                conn,
                kind="author",
                value=owner,
                source="feedback",
                source_ref=str(push_id),
            )
    elif action == "block_topic":
        topics = repo_snapshot.get("topics") or []
        if topics:
            await add_blacklist_entry(
                conn,
                kind="topic",
                value=topics[0],
                source="feedback",
                source_ref=str(push_id),
            )

    ack_text = _render_ack(action, repo_snapshot)
    try:
        await cq.edit_message_text(ack_text)
    except Exception as exc:  # noqa: BLE001 - edit may fail if msg too old; log and move on
        log.warning(
            "feedback.edit_message_failed",
            push_id=push_id,
            action=action,
            error=str(exc),
        )

    # Trigger preference regen when we have enough new feedback.
    new_count = await count_feedback_since_last_profile(conn)
    if new_count >= refresh_threshold:
        try:
            await pref_builder.regenerate()
        except Exception as exc:  # noqa: BLE001 - regen is best-effort
            log.warning("feedback.pref_regen_failed", error=str(exc))


async def _load_repo_snapshot(conn: aiosqlite.Connection, push_id: int) -> dict:
    """Snapshot enough of the pushed repo to drive blacklist / preference
    decisions. Returns an empty dict when the push_id is unknown — callers
    should treat that as a stale button and skip persistence.

    We pull from pushed_items rather than joining to repositories to keep
    the feedback handler independent of the (still-evolving) repo
    metadata schema."""
    async with conn.execute(
        "SELECT full_name, summary FROM pushed_items WHERE id = ?",
        (push_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return {}
    full_name = row[0] or ""
    owner = full_name.split("/", 1)[0] if "/" in full_name else ""
    # Topics aren't stored in pushed_items; in practice the scheduler will
    # have written them to the repositories table. For feedback purposes
    # we query that table; if missing, degrade gracefully.
    async with conn.execute(
        "SELECT topics FROM repositories WHERE full_name = ?",
        (full_name,),
    ) as cur:
        topics_row = await cur.fetchone()
    topics: list[str] = []
    if topics_row and topics_row[0]:
        try:
            import json as _json
            parsed = _json.loads(topics_row[0])
            if isinstance(parsed, list):
                topics = [str(t) for t in parsed]
        except (TypeError, ValueError):
            pass
    return {
        "full_name": full_name,
        "owner_login": owner,
        "topics": topics,
        "summary": row[1] or "",
    }


def _render_ack(action: str, snapshot: dict) -> str:
    name = snapshot.get("full_name", "(未知项目)")
    summary = snapshot.get("summary", "")
    prefix = {
        "like": "✅ 已 👍",
        "dislike": "✅ 已 👎",
        "block_author": "✅ 已屏蔽作者",
        "block_topic": "✅ 已屏蔽 topic",
    }.get(action, "✅ 已记录")
    base = f"{prefix}: {name}"
    return f"{base}\n{summary}" if summary else base
