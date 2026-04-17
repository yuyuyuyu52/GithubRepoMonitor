from __future__ import annotations

from typing import Sequence

from monitor.models import RepoCandidate
from monitor.scoring.types import ScoreResult


def heuristic_score_readme(
    repo: RepoCandidate, *, interest_tags: Sequence[str]
) -> ScoreResult:
    """Ported from legacy's `_heuristic_analysis`. Returns a ScoreResult so
    the orchestrator can treat LLM and fallback outputs identically."""

    readme = repo.readme_text or ""
    lower = readme.lower()
    has_install = "install" in lower or "安装" in lower
    has_usage = "usage" in lower or "quick start" in lower or "使用" in lower
    has_arch = "architecture" in lower or "架构" in lower
    has_license = "license" in lower or "许可证" in lower
    completeness = sum([has_install, has_usage, has_arch, has_license]) / 4.0

    haystack = (repo.description + " " + readme).lower()
    matched: list[str] = [tag for tag in interest_tags if tag.lower() in haystack]

    # Score scales from ~4 (empty README, no matches) toward 10.
    score = min(10.0, 4.0 + completeness * 4.0 + len(matched))

    summary = repo.description.strip() or "README 中未提供明确描述"
    reason = (
        f"匹配兴趣标签 {len(matched)} 项，README 完整度 {completeness:.0%}，"
        f"近 24 小时 star 增速 {repo.star_velocity_day:.1f}。"
    )

    return ScoreResult(
        score=round(score, 2),
        readme_completeness=round(completeness, 2),
        summary=summary,
        reason=reason,
        matched_interests=matched,
        red_flags=[],
    )
