from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field


class ScoreResult(BaseModel):
    """Structured LLM output for one repo.

    `extra="ignore"` because the MiniMax/Anthropic endpoint may append
    reasoning or debug fields we don't care about — silently drop them
    rather than raise on valid-but-verbose responses.
    """

    model_config = ConfigDict(extra="ignore")

    score: float = Field(ge=1.0, le=10.0)
    readme_completeness: float = Field(ge=0.0, le=1.0)
    # max_length mirrors the limits advertised in SCORE_TOOL's JSON schema.
    # If the LLM ignores the soft constraint and returns an oversized string,
    # pydantic raises ValidationError → LLMClient wraps to LLMScoreError →
    # orchestrator falls back to heuristic.
    summary: str = Field(max_length=140)
    reason: str = Field(max_length=240)
    matched_interests: List[str]
    red_flags: List[str]


class LLMScoreError(Exception):
    """Raised by LLMClient when an LLM call can't produce a valid ScoreResult.

    Includes network failures after retries exhausted, tool_use block
    missing/malformed, and pydantic validation failures. The scoring
    orchestrator catches this and falls back to heuristic scoring.
    """

    def __init__(self, message: str, *, cause: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause
