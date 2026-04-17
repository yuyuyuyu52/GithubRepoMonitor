from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitor.clients.llm import LLMClient
from monitor.scoring.types import LLMScoreError


def _text_block_response(text: str) -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


def _client_with_mock(response) -> LLMClient:
    fake_sdk = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=response))
    )
    return LLMClient(
        api_key="k",
        base_url="u",
        model="minimax-m2",
        anthropic_client=fake_sdk,
    )


async def test_generate_text_returns_first_text_block() -> None:
    client = _client_with_mock(_text_block_response("用户偏好 Rust 系统工具"))
    result = await client.generate_text("prompt")
    assert result == "用户偏好 Rust 系统工具"


async def test_generate_text_sends_model_and_prompt() -> None:
    client = _client_with_mock(_text_block_response("ok"))
    await client.generate_text("say hi please")

    create_mock = client._client.messages.create
    kwargs = create_mock.call_args.kwargs
    assert kwargs["model"] == "minimax-m2"
    assert kwargs["messages"] == [{"role": "user", "content": "say hi please"}]
    # No tools on generate_text — it's a free-form chat call
    assert "tools" not in kwargs or kwargs["tools"] in (None, [])


async def test_generate_text_raises_llm_score_error_on_sdk_failure() -> None:
    fake_sdk = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("network down"))
        )
    )
    client = LLMClient(api_key="k", base_url="u", model="m", anthropic_client=fake_sdk)
    with pytest.raises(LLMScoreError):
        await client.generate_text("prompt")


async def test_generate_text_raises_when_no_text_block() -> None:
    # Response has only tool_use, no text
    block = SimpleNamespace(type="tool_use", name="x", input={})
    resp = SimpleNamespace(content=[block])
    client = _client_with_mock(resp)
    with pytest.raises(LLMScoreError):
        await client.generate_text("prompt")
