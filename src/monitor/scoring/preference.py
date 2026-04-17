from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiosqlite
import structlog

from monitor.db import put_preference_profile


log = structlog.get_logger(__name__)

LLMGenerateProfile = Callable[[str], Awaitable[str]]


@dataclass(slots=True)
class RegenerationResult:
    profile_text: str
    generated_at: dt.datetime
    based_on_feedback_count: int


class PreferenceBuilder:
    """Builds a natural-language user-preference profile from recent
    user_feedback rows and persists it in the single-row preference_profile
    table. Called by M4 after every Nth feedback write."""

    def __init__(
        self,
        *,
        conn: aiosqlite.Connection,
        llm_generate_profile: LLMGenerateProfile,
        max_per_action: int = 20,
        now: dt.datetime | None = None,
    ) -> None:
        self._conn = conn
        self._generate = llm_generate_profile
        self._max_per_action = max_per_action
        self._now = now or dt.datetime.now(dt.timezone.utc)

    async def regenerate(self) -> RegenerationResult | None:
        likes = await self._recent_feedback("like")
        dislikes = await self._recent_feedback("dislike")
        if not likes and not dislikes:
            return None

        prompt = self._build_prompt(likes, dislikes)
        profile_text = (await self._generate(prompt)).strip()
        count = len(likes) + len(dislikes)

        await put_preference_profile(
            self._conn,
            profile_text=profile_text,
            generated_at=self._now,
            based_on_feedback_count=count,
        )
        log.info(
            "preference.regenerated",
            based_on=count,
            profile_chars=len(profile_text),
        )
        return RegenerationResult(
            profile_text=profile_text,
            generated_at=self._now,
            based_on_feedback_count=count,
        )

    async def _recent_feedback(self, action: str) -> list[dict]:
        async with self._conn.execute(
            "SELECT repo_snapshot FROM user_feedback "
            "WHERE action = ? ORDER BY created_at DESC LIMIT ?",
            (action, self._max_per_action),
        ) as cur:
            rows = await cur.fetchall()
        result: list[dict] = []
        for row in rows:
            raw = row[0]
            if not raw:
                continue
            try:
                result.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _build_prompt(likes: list[dict], dislikes: list[dict]) -> str:
        def _fmt(items: list[dict]) -> str:
            if not items:
                return "(无)"
            lines = []
            for item in items:
                name = item.get("full_name", "?")
                topics = item.get("topics") or []
                topics_str = "、".join(topics) if topics else ""
                lines.append(f"- {name}  topics=[{topics_str}]")
            return "\n".join(lines)

        return (
            "根据下列用户反馈，用 250-300 字中文总结用户偏好。"
            "描述用户喜欢什么方向的开源项目、不喜欢什么，并给出一个一句话"
            "的选项偏好描述。不要列举具体仓库名，只描述特征。\n\n"
            f"用户 👍 的项目：\n{_fmt(likes)}\n\n"
            f"用户 👎 的项目：\n{_fmt(dislikes)}"
        )
