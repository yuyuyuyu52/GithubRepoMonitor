import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.clients.llm import LLMClient, SCORE_TOOL
from monitor.models import RepoCandidate
from monitor.scoring.types import LLMScoreError, ScoreResult


def _repo() -> RepoCandidate:
    now = dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)
    return RepoCandidate(
        full_name="acme/widget",
        html_url="https://github.com/acme/widget",
        description="widgets for agents",
        language="Python",
        stars=420,
        forks=21,
        created_at=now,
        pushed_at=now,
        owner_login="acme",
        readme_text="# widget\n## install\n",
        star_velocity_day=5.0,
        contributor_count=12,
    )


def _tool_use_response(payload: dict) -> SimpleNamespace:
    """Mimic anthropic.types.Message with a tool_use content block."""
    block = SimpleNamespace(type="tool_use", name="submit_repo_score", input=payload)
    return SimpleNamespace(content=[block])


def _text_only_response(text: str = "no tool here") -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


def _client_with_mock(response) -> LLMClient:
    fake_sdk = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    return LLMClient(
        api_key="test-key",
        base_url="https://example.invalid",
        model="minimax-m2",
        anthropic_client=fake_sdk,
    )


async def test_score_repo_returns_parsed_result() -> None:
    payload = {
        "score": 8.2,
        "readme_completeness": 0.9,
        "summary": "Strong agent framework",
        "reason": "Matches your interest in agents",
        "matched_interests": ["agent"],
        "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    result = await client.score_repo(_repo(), interest_tags=["agent"], preference_profile=None)

    assert isinstance(result, ScoreResult)
    assert result.score == 8.2


async def test_score_repo_sends_forced_tool_use() -> None:
    payload = {
        "score": 7.0,
        "readme_completeness": 0.5,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    await client.score_repo(_repo(), interest_tags=["agent"], preference_profile=None)

    create_mock = client._client.messages.create
    kwargs = create_mock.call_args.kwargs
    assert kwargs["tools"] == [SCORE_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_repo_score"}
    assert kwargs["model"] == "minimax-m2"


async def test_score_repo_injects_preference_profile_into_system() -> None:
    payload = {
        "score": 7.0, "readme_completeness": 0.5, "summary": "s", "reason": "r",
        "matched_interests": [], "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    await client.score_repo(
        _repo(),
        interest_tags=["agent"],
        preference_profile="用户偏好 rust tooling",
    )

    kwargs = client._client.messages.create.call_args.kwargs
    system_blocks = kwargs["system"]
    # Must be a list (so cache_control can be set per block), contain the
    # rubric and the preference profile.
    joined = " ".join(b["text"] for b in system_blocks)
    assert "rust tooling" in joined


async def test_score_repo_uses_ephemeral_cache_on_system_blocks() -> None:
    payload = {
        "score": 7.0, "readme_completeness": 0.5, "summary": "s", "reason": "r",
        "matched_interests": [], "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    await client.score_repo(_repo(), interest_tags=["agent"], preference_profile=None)

    kwargs = client._client.messages.create.call_args.kwargs
    assert any(
        b.get("cache_control") == {"type": "ephemeral"}
        for b in kwargs["system"]
    ), "at least one system block must be ephemeral-cached"


async def test_score_repo_raises_when_no_tool_use_block() -> None:
    client = _client_with_mock(_text_only_response("model refused to use the tool"))
    with pytest.raises(LLMScoreError) as excinfo:
        await client.score_repo(_repo(), interest_tags=[], preference_profile=None)
    assert "tool_use" in str(excinfo.value).lower() or excinfo.value.cause


async def test_score_repo_raises_when_tool_input_is_malformed() -> None:
    # score=50 is outside the ge=1, le=10 validator
    bad_payload = {
        "score": 50.0,
        "readme_completeness": 0.5,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(bad_payload))
    with pytest.raises(LLMScoreError):
        await client.score_repo(_repo(), interest_tags=[], preference_profile=None)


async def test_score_repo_raises_llm_score_error_on_sdk_failure() -> None:
    fake_sdk = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("network down")))
    )
    client = LLMClient(
        api_key="k", base_url="u", model="m", anthropic_client=fake_sdk,
    )
    with pytest.raises(LLMScoreError):
        await client.score_repo(_repo(), interest_tags=[], preference_profile=None)


async def test_score_repo_truncates_long_readme() -> None:
    """README beyond 12K chars must be truncated before hitting the wire."""
    payload = {
        "score": 7.0, "readme_completeness": 0.5, "summary": "s", "reason": "r",
        "matched_interests": [], "red_flags": [],
    }
    client = _client_with_mock(_tool_use_response(payload))

    huge = _repo()
    huge.readme_text = "x" * 150000  # well above README_TRUNCATE_CHARS=100000

    await client.score_repo(huge, interest_tags=[], preference_profile=None)

    kwargs = client._client.messages.create.call_args.kwargs
    user_text = kwargs["messages"][0]["content"]
    # user_text is either a string or a list of content blocks
    if isinstance(user_text, list):
        user_text = " ".join(b.get("text", "") for b in user_text if isinstance(b, dict))
    # Actual max ≈ 100000 (README cap) + ~400 (template overhead). A regression
    # that disabled truncation entirely would blow past 150000.
    assert len(user_text) < 100500
