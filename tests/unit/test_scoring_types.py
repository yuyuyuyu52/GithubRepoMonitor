import pytest
from pydantic import ValidationError

from monitor.scoring.types import LLMScoreError, ScoreResult


def test_score_result_accepts_valid_payload() -> None:
    result = ScoreResult(
        score=8.5,
        readme_completeness=0.9,
        summary="一句话描述",
        reason="一句话理由",
        matched_interests=["agent"],
        red_flags=[],
    )
    assert result.score == 8.5
    assert result.matched_interests == ["agent"]
    assert result.red_flags == []


def test_score_result_rejects_out_of_range_score() -> None:
    with pytest.raises(ValidationError):
        ScoreResult(
            score=12.0,  # > 10 max
            readme_completeness=0.5,
            summary="x",
            reason="y",
            matched_interests=[],
            red_flags=[],
        )
    with pytest.raises(ValidationError):
        ScoreResult(
            score=5.0,
            readme_completeness=1.5,  # > 1.0 max
            summary="x",
            reason="y",
            matched_interests=[],
            red_flags=[],
        )


def test_score_result_ignores_unknown_fields() -> None:
    """LLM may return extra keys — accept them without failing."""
    result = ScoreResult.model_validate({
        "score": 7.0,
        "readme_completeness": 0.6,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
        "surprise_field": "noise",
    })
    assert result.score == 7.0


def test_llm_score_error_carries_reason() -> None:
    exc = LLMScoreError("bad tool_use block", cause="schema_mismatch")
    assert "bad tool_use" in str(exc)
    assert exc.cause == "schema_mismatch"


def test_score_result_rejects_oversized_summary_or_reason() -> None:
    """SCORE_TOOL declares summary≤800 and reason≤240; ScoreResult must
    enforce the same at runtime so the LLM-ignores-schema case fails loud."""
    base = {
        "score": 8.0,
        "readme_completeness": 0.8,
        "summary": "s",
        "reason": "r",
        "matched_interests": [],
        "red_flags": [],
    }
    # summary too long
    with pytest.raises(ValidationError):
        ScoreResult.model_validate({**base, "summary": "x" * 801})
    # reason too long
    with pytest.raises(ValidationError):
        ScoreResult.model_validate({**base, "reason": "y" * 241})
    # at the exact boundary, still valid
    ScoreResult.model_validate({**base, "summary": "x" * 800, "reason": "y" * 240})
