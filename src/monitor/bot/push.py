from __future__ import annotations

from typing import Any, Literal

import aiosqlite
import structlog

from monitor.bot.render import render_repo_message
from monitor.db import insert_pushed_item, update_pushed_tg_message_id
from monitor.models import RepoCandidate


log = structlog.get_logger(__name__)


async def push_repo(
    repo: RepoCandidate,
    *,
    bot_app: Any,
    chat_id: str,
    conn: aiosqlite.Connection,
    push_type: Literal["digest", "surge"],
) -> int | None:
    """Insert pushed_items row → render → send → update tg_message_id.

    Returns the push_id on successful send, or None if Telegram send failed
    (the pushed_items row is left with tg_message_id=NULL; a future
    reconciliation job could clean these up if needed).
    """
    push_id = await insert_pushed_item(
        conn, repo=repo, push_type=push_type, tg_chat_id=chat_id
    )
    text, markup = render_repo_message(repo, push_id=push_id)
    if push_type == "surge":
        text = "🔥 热度突发\n" + text

    try:
        sent = await bot_app.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            reply_markup=markup,
            disable_web_page_preview=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "push.send_failed",
            repo=repo.full_name,
            push_type=push_type,
            error=str(exc),
        )
        return None

    message_id = getattr(sent, "message_id", None)
    if message_id is not None:
        await update_pushed_tg_message_id(
            conn, push_id=push_id, tg_message_id=str(message_id)
        )
    log.info(
        "push.sent",
        repo=repo.full_name,
        push_type=push_type,
        push_id=push_id,
        tg_message_id=message_id,
    )
    return push_id
