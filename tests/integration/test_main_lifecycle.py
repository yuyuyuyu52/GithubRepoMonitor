import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="signal handling differs on Windows"
)


async def _start_process(tmp_path: Path) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    env["MONITOR_DB_PATH"] = str(tmp_path / "mon.db")
    for key in ("MONITOR_CONFIG", "MONITOR_LOG_PATH", "GITHUB_TOKEN",
                "MINIMAX_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env.pop(key, None)
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "monitor",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def test_main_starts_runs_migrations_and_exits_on_sigterm(tmp_path: Path) -> None:
    proc = await _start_process(tmp_path)
    try:
        startup_seen = False
        for _ in range(50):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            if not line:
                break
            if b"migrations.applied" in line:
                startup_seen = True
                break
        assert startup_seen, "did not observe migrations.applied log line"

        proc.send_signal(signal.SIGTERM)
        rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
        assert rc == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert (tmp_path / "mon.db").exists()


async def test_main_logs_telegram_disabled_when_no_credentials(tmp_path: Path) -> None:
    proc = await _start_process(tmp_path)
    try:
        disabled_seen = False
        for _ in range(50):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            if not line:
                break
            if b"telegram.disabled" in line:
                disabled_seen = True
                break
        assert disabled_seen, "daemon should log telegram.disabled when token missing"
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
