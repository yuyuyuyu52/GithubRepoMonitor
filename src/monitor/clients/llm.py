from __future__ import annotations

from typing import Any, Sequence

import structlog
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from monitor.models import RepoCandidate
from monitor.scoring.types import LLMScoreError, ScoreResult


log = structlog.get_logger(__name__)

README_TRUNCATE_CHARS = 12000
DEFAULT_MAX_TOKENS = 1024

_TOOL_NAME = "submit_repo_score"

SCORE_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": "提交对该仓库的结构化评估。",
    "input_schema": {
        "type": "object",
        "required": [
            "score",
            "readme_completeness",
            "summary",
            "reason",
            "matched_interests",
            "red_flags",
        ],
        "properties": {
            "score": {
                "type": "number",
                "minimum": 1,
                "maximum": 10,
                "description": "1-10 综合评分",
            },
            "readme_completeness": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "README 完整度 0.0-1.0",
            },
            "summary": {
                "type": "string",
                "maxLength": 140,
                "description": "项目一句话摘要",
            },
            "reason": {
                "type": "string",
                "maxLength": 240,
                "description": "推荐理由，一句话",
            },
            "matched_interests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "命中的用户兴趣标签",
            },
            "red_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "值得警惕的信号（例如 README 空白、极冷门话题）",
            },
        },
    },
}


_RUBRIC = """你是开源项目评估助手。对每个仓库按以下维度打分 1-10：
- 工程质量：代码/README/文档完整度
- 活跃度：最近提交、issue 响应、贡献者增长
- 方向性：是否对用户的兴趣标签有明显匹配
- 独特性：相较同类项目的差异化
以 submit_repo_score 工具返回结构化结果。"""


class LLMClient:
    """Anthropic AsyncAnthropic pointed at MiniMax's compatible endpoint.

    Strict: any SDK error, missing tool_use block, or validation failure
    raises LLMScoreError. Upstream (`scoring.score.score_repo`) catches
    that and falls back to the heuristic scorer.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        anthropic_client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = anthropic_client or AsyncAnthropic(
            api_key=api_key, base_url=base_url
        )

    async def score_repo(
        self,
        repo: RepoCandidate,
        *,
        interest_tags: Sequence[str],
        preference_profile: str | None,
    ) -> ScoreResult:
        system_blocks = self._build_system(preference_profile)
        user_text = self._build_user_prompt(repo, interest_tags)

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                tools=[SCORE_TOOL],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:  # noqa: BLE001 - SDK surface is broad
            log.warning("llm.score_sdk_error", repo=repo.full_name, error=str(exc))
            raise LLMScoreError(str(exc), cause="sdk_error") from exc

        _log_usage(resp, repo.full_name)
        tool_input = _extract_tool_input(resp)
        if tool_input is None:
            raise LLMScoreError(
                "no submit_repo_score tool_use block in response",
                cause="missing_tool_use",
            )

        try:
            return ScoreResult.model_validate(tool_input)
        except ValidationError as exc:
            log.warning(
                "llm.score_validation_failed",
                repo=repo.full_name,
                error=str(exc),
            )
            raise LLMScoreError(str(exc), cause="schema_mismatch") from exc

    async def generate_text(self, prompt: str) -> str:
        """Free-form text completion — used by PreferenceBuilder to summarize
        recent feedback into a preference profile. No tool use; we expect a
        plain text-block response.

        Raises LLMScoreError on SDK failure or when no text block is returned
        so the caller can decide whether to ignore (preference regen is
        best-effort) or log and continue.
        """
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.generate_text_sdk_error", error=str(exc))
            raise LLMScoreError(str(exc), cause="sdk_error") from exc

        _log_usage(resp, "generate_text")
        content = getattr(resp, "content", None) or []
        for block in content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    return text
        raise LLMScoreError(
            "no text block in generate_text response", cause="missing_text"
        )

    @staticmethod
    def _build_system(preference_profile: str | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _RUBRIC,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if preference_profile:
            blocks.append(
                {
                    "type": "text",
                    "text": f"用户偏好画像：\n{preference_profile}",
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    @staticmethod
    def _build_user_prompt(
        repo: RepoCandidate, interest_tags: Sequence[str]
    ) -> str:
        readme = (repo.readme_text or "")[:README_TRUNCATE_CHARS]
        tags_text = "、".join(interest_tags) if interest_tags else "(无)"
        return (
            f"仓库：{repo.full_name}\n"
            f"描述：{repo.description or '(空)'}\n"
            f"语言：{repo.language}\n"
            f"Stars：{repo.stars}，Forks：{repo.forks}\n"
            f"近 24h star 增速：{repo.star_velocity_day:.1f}\n"
            f"贡献者数：{repo.contributor_count}\n"
            f"平均 issue 响应：{repo.avg_issue_response_hours:.1f} 小时\n"
            f"用户兴趣标签：{tags_text}\n"
            f"\nREADME（截断 {README_TRUNCATE_CHARS} 字符）：\n{readme}"
        )


def _extract_tool_input(resp: Any) -> dict | None:
    content = getattr(resp, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
            candidate = getattr(block, "input", None)
            if isinstance(candidate, dict):
                return candidate
    return None


def _log_usage(resp: Any, repo_full_name: str) -> None:
    """Log Anthropic `usage` fields if present. Safe on responses that don't
    carry usage (e.g. mocks in tests)."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    log.info(
        "llm.usage",
        repo=repo_full_name,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
    )
