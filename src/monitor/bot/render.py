from __future__ import annotations

from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from monitor.models import RepoCandidate


CALLBACK_PREFIX = "fb"
FeedbackAction = Literal["like", "dislike", "block_author", "block_topic"]
_ACTIONS: tuple[FeedbackAction, ...] = (
    "like",
    "dislike",
    "block_author",
    "block_topic",
)


def render_repo_message(
    repo: RepoCandidate, *, push_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Render a scored RepoCandidate into (text, InlineKeyboardMarkup).

    The 4 feedback buttons embed `push_id` in callback_data so the
    feedback handler can resolve the originating pushed_items row
    without re-querying the message.
    """
    lines = [
        f"⭐ {repo.full_name}  ({repo.final_score:.2f}/10)",
    ]
    if repo.summary:
        lines.append(f"一句话: {repo.summary}")
    if repo.recommendation_reason:
        lines.append(f"推荐: {repo.recommendation_reason}")
    lines.append(f"🔗 {repo.html_url}")
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("👍", callback_data=f"{CALLBACK_PREFIX}:like:{push_id}"),
            InlineKeyboardButton("👎", callback_data=f"{CALLBACK_PREFIX}:dislike:{push_id}"),
        ],
        [
            InlineKeyboardButton(
                "🚫 作者", callback_data=f"{CALLBACK_PREFIX}:block_author:{push_id}"
            ),
            InlineKeyboardButton(
                "🔕 topic", callback_data=f"{CALLBACK_PREFIX}:block_topic:{push_id}"
            ),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def parse_callback_data(data: str) -> tuple[FeedbackAction, int] | None:
    """Return (action, push_id) if `data` is a valid feedback callback,
    else None. Invalid shapes (wrong prefix, unknown action, non-integer
    id) all return None so the handler can no-op silently."""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action = parts[1]
    if action not in _ACTIONS:
        return None
    try:
        push_id = int(parts[2])
    except ValueError:
        return None
    return (action, push_id)  # type: ignore[return-value]
