from __future__ import annotations

from typing import Any, Awaitable, Callable

import aiosqlite
import structlog

from monitor.config import ConfigFile
from monitor.db import get_recent_pushes, get_latest_run_logs
from monitor.state import DaemonState


log = structlog.get_logger(__name__)

ConfigReloader = Callable[[], Awaitable[ConfigFile]]


async def handle_top(
    update: Any,
    *,
    conn: aiosqlite.Connection,
    limit: int = 10,
) -> None:
    pushes = await get_recent_pushes(conn, limit=limit)
    if not pushes:
        await update.message.reply_text("📭 暂无推送记录")
        return
    lines = ["🔝 最近推送"]
    for p in pushes:
        lines.append(
            f"• {p['full_name']}  {p['final_score']:.2f}/10\n  {p['summary']}"
        )
    await update.message.reply_text("\n".join(lines))


async def handle_status(
    update: Any,
    *,
    conn: aiosqlite.Connection,
    state: DaemonState,
) -> None:
    runs = await get_latest_run_logs(conn, limit=3)
    lines = ["📊 状态"]
    if state.paused:
        lines.append("⏸ paused (暂停中)")
    else:
        lines.append("▶️ running")
    if not runs:
        lines.append("最近运行: 无")
    else:
        lines.append("最近运行:")
        for r in runs:
            status = r.get("status") or "?"
            stats = r.get("stats") or {}
            pushed = stats.get("repos_pushed", "?")
            lines.append(
                f"• {r['kind']}  {r['started_at']}  [{status}]  推送={pushed}"
            )
    await update.message.reply_text("\n".join(lines))


async def handle_pause(update: Any, *, state: DaemonState) -> None:
    await state.set_paused(True)
    await update.message.reply_text("⏸ 已暂停，定时采集/推送不会触发。`/resume` 恢复。")


async def handle_resume(update: Any, *, state: DaemonState) -> None:
    await state.set_paused(False)
    await update.message.reply_text("▶️ 已恢复，下一次定时触发照常跑。")


async def handle_reload(
    update: Any,
    *,
    state: DaemonState,
    config_reloader: ConfigReloader,
) -> None:
    try:
        new_config = await config_reloader()
    except Exception as exc:  # noqa: BLE001 - surface any reload error to the operator
        log.warning("commands.reload_failed", error=str(exc))
        await update.message.reply_text(f"❌ 重载失败: {exc}")
        return
    state.reload_config(new_config)
    await update.message.reply_text("✅ 配置已重载")
