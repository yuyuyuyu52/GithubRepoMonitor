from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import aiosqlite

from monitor.config import ConfigFile
from monitor.db import get_daemon_state, set_daemon_paused


@dataclass
class DaemonState:
    """Shared daemon-level state — accessed by the TG bot handlers today,
    by the M5 scheduler tomorrow. Holds:

    - `config`: the currently-active ConfigFile (replaceable by /reload)
    - `paused`: whether scheduled work (M5) should run

    `paused` is persisted to the daemon_state table so it survives
    daemon restarts. `config` is purely in-memory; the next restart reloads
    from the same config.json anyway.
    """

    config: ConfigFile
    paused: bool
    conn: aiosqlite.Connection

    @classmethod
    async def load(
        cls, *, conn: aiosqlite.Connection, config: ConfigFile
    ) -> "DaemonState":
        db_state = await get_daemon_state(conn)
        return cls(config=config, paused=bool(db_state["paused"]), conn=conn)

    async def set_paused(self, paused: bool, *, now: dt.datetime | None = None) -> None:
        await set_daemon_paused(self.conn, paused=paused, now=now)
        self.paused = paused

    def reload_config(self, config: ConfigFile) -> None:
        self.config = config
