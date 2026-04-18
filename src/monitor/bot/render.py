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


_TOPICS_SHOWN = 5


def render_repo_message(
    repo: RepoCandidate, *, push_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Render a scored RepoCandidate into (text, InlineKeyboardMarkup).

    The 4 feedback buttons embed `push_id` in callback_data so the
    feedback handler can resolve the originating pushed_items row
    without re-querying the message.

    Layout is intentionally emoji-free: title + score, blank line, LLM
    project description (multi-sentence), blank line, a pipe-separated
    stats line and a topics line, blank line, url.
    """
    lines: list[str] = [f"{repo.full_name}  —  {repo.final_score:.2f}/10"]
    if repo.summary:
        lines.append("")
        lines.append(repo.summary)
    lines.append("")
    lines.append(_format_stats_line(repo))
    if repo.topics:
        topics_str = ", ".join(repo.topics[:_TOPICS_SHOWN])
        lines.append(f"topics: {topics_str}")
    lines.append("")
    lines.append(repo.html_url)
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("赞", callback_data=f"{CALLBACK_PREFIX}:like:{push_id}"),
            InlineKeyboardButton("踩", callback_data=f"{CALLBACK_PREFIX}:dislike:{push_id}"),
        ],
        [
            InlineKeyboardButton(
                "屏蔽作者", callback_data=f"{CALLBACK_PREFIX}:block_author:{push_id}"
            ),
            InlineKeyboardButton(
                "屏蔽话题", callback_data=f"{CALLBACK_PREFIX}:block_topic:{push_id}"
            ),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def _format_stats_line(repo: RepoCandidate) -> str:
    parts = [f"{repo.stars:,} stars", f"{repo.forks:,} forks"]
    if repo.star_velocity_day > 0:
        parts.append(f"+{repo.star_velocity_day:.0f} stars/24h")
    return " | ".join(parts)


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
