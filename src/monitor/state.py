from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

import aiosqlite

from monitor.config import ConfigFile
from monitor.db import get_daemon_state, set_daemon_paused


@dataclass
class DaemonState:
    """Shared daemon-level state — accessed by the TG bot handlers (M4) and
    the M5 scheduler. Holds:

    - `config`: currently-active ConfigFile (replaceable by /reload)
    - `paused`: whether scheduled work should run (persisted in daemon_state)
    - `conn`: DB connection for write-through operations
    - `digest_lock`: asyncio.Lock serializing digest/surge/weekly runs and
      /digest_now so the pipeline is non-reentrant. Held by the scheduler
      job for the duration of a single run.
    """

    config: ConfigFile
    paused: bool
    conn: aiosqlite.Connection
    digest_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
