import datetime as dt

import pytest
from telegram import InlineKeyboardMarkup

from monitor.bot.render import (
    CALLBACK_PREFIX,
    parse_callback_data,
    render_repo_message,
)
from monitor.models import RepoCandidate


def _repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 18, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="widgets",
        language="Python",
        stars=420,
        forks=21,
        created_at=now - dt.timedelta(days=30),
        pushed_at=now - dt.timedelta(days=1),
        owner_login="acme",
        topics=["agent", "llm"],
        rule_score=7.5,
        llm_score=8.2,
        final_score=7.85,
        summary="Widget library",
        recommendation_reason="Matches your agent interest",
    )


def test_render_repo_message_contains_core_fields() -> None:
    text, markup = render_repo_message(_repo(), push_id=42)
    assert "acme/widget" in text
    assert "7.85" in text  # final_score
    assert "Widget library" in text  # summary
    assert "Matches your agent interest" in text  # reason
    assert "https://github.com/acme/widget" in text
    assert isinstance(markup, InlineKeyboardMarkup)


def test_render_repo_message_has_four_buttons_with_callback_data() -> None:
    _, markup = render_repo_message(_repo(), push_id=42)
    # InlineKeyboardMarkup.inline_keyboard is list[list[InlineKeyboardButton]]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 4

    labels_to_actions = {
        "👍": "like",
        "👎": "dislike",
        "🚫 作者": "block_author",
        "🔕 topic": "block_topic",
    }
    for button in buttons:
        matched = False
        for emoji_prefix, action in labels_to_actions.items():
            if emoji_prefix in button.text:
                expected = f"{CALLBACK_PREFIX}:{action}:42"
                assert button.callback_data == expected
                matched = True
                break
        assert matched, f"unexpected button label: {button.text}"


def test_parse_callback_data_roundtrips() -> None:
    assert parse_callback_data("fb:like:42") == ("like", 42)
    assert parse_callback_data("fb:block_author:9") == ("block_author", 9)
    # Invalid payloads return None
    assert parse_callback_data("unrelated") is None
    assert parse_callback_data("fb:like") is None
    assert parse_callback_data("fb:unknown_action:5") is None
    assert parse_callback_data("fb:like:not_a_number") is None


def test_render_truncates_or_falls_back_on_missing_summary() -> None:
    repo = _repo()
    repo.summary = ""
    repo.recommendation_reason = ""
    text, _ = render_repo_message(repo, push_id=1)
    # Should still render without crashing; name + url always present
    assert "acme/widget" in text
    assert "https://github.com/acme/widget" in text
