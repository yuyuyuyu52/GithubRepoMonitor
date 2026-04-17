from __future__ import annotations

import datetime as dt

import aiosqlite

from monitor.db import (
    get_feedback_counts_since,
    get_latest_run_logs,
    get_preference_profile,
    get_pushed_since,
)


async def build_weekly_digest(
    conn: aiosqlite.Connection,
    *,
    now: dt.datetime | None = None,
    window_days: int = 7,
) -> str:
    """Aggregate the last `window_days` of activity into a text block for
    the Sunday weekly digest push."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=window_days)

    pushes = await get_pushed_since(conn, since=since)
    feedback = await get_feedback_counts_since(conn, since=since)
    profile = await get_preference_profile(conn)
    runs = await get_latest_run_logs(conn, limit=50)
    recent_runs = [r for r in runs if r.get("started_at") and r["started_at"] >= since.isoformat()]

    # Week label: ISO year-week
    iso = now.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"

    lines = [f"📊 本周摘要 ({week_label})"]

    # Pushes + feedback headline
    like_count = feedback.get("like", 0)
    dislike_count = feedback.get("dislike", 0)
    lines.append(
        f"🔥 新推送 {len(pushes)}，你 👍 {like_count} / 👎 {dislike_count}"
    )

    # Top 3 by final_score
    if pushes:
        top3 = sorted(pushes, key=lambda p: p["final_score"], reverse=True)[:3]
        lines.append("📈 本周推送 Top 3:")
        for i, p in enumerate(top3, 1):
            lines.append(
                f"  {i}. {p['full_name']}  {p['final_score']:.2f}/10"
            )

    # Preference profile
    if profile and profile.get("profile_text"):
        count = profile.get("based_on_feedback_count") or 0
        lines.append("")
        lines.append(f"🎯 兴趣画像（基于 {count} 条反馈）")
        lines.append(profile["profile_text"])

    # Run statistics
    if recent_runs:
        ok_count = sum(1 for r in recent_runs if r.get("status") == "ok")
        failed_count = sum(1 for r in recent_runs if r.get("status") == "failed")
        surge_count = sum(1 for r in recent_runs if (r.get("kind") or "").startswith("surge"))
        lines.append("")
        lines.append("📋 运行统计")
        lines.append(
            f"  digest {ok_count}/{len(recent_runs) - surge_count}，"
            f"surge {surge_count} 次，失败 {failed_count}"
        )

    return "\n".join(lines)
