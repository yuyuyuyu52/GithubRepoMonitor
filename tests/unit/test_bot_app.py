from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from monitor.bot.app import create_application
from monitor.config import ConfigFile
from monitor.db import connect, run_migrations
from monitor.state import DaemonState


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


async def _make_state(tmp_db: Path) -> DaemonState:
    conn = await connect(tmp_db)
    await run_migrations(conn)
    return await DaemonState.load(conn=conn, config=ConfigFile())


async def test_create_application_registers_five_commands(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)
    pref_builder = SimpleNamespace(regenerate=AsyncMock())

    async def reloader() -> ConfigFile:
        return ConfigFile()

    app = create_application(
        token="dummy-token",
        chat_id="12345",
        conn=state.conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=5,
        config_reloader=reloader,
    )

    command_names: list[str] = []
    for handler_list in app.handlers.values():
        for handler in handler_list:
            if isinstance(handler, CommandHandler):
                command_names.extend(sorted(handler.commands))

    assert sorted(command_names) == ["pause", "reload", "resume", "status", "top"]
    await state.conn.close()


async def test_create_application_registers_callback_query_handler(tmp_db: Path) -> None:
    state = await _make_state(tmp_db)
    pref_builder = SimpleNamespace(regenerate=AsyncMock())

    async def reloader() -> ConfigFile:
        return ConfigFile()

    app = create_application(
        token="dummy-token",
        chat_id="12345",
        conn=state.conn,
        state=state,
        pref_builder=pref_builder,
        refresh_threshold=5,
        config_reloader=reloader,
    )

    has_callback_handler = False
    for handler_list in app.handlers.values():
        for handler in handler_list:
            if isinstance(handler, CallbackQueryHandler):
                has_callback_handler = True
                break
    assert has_callback_handler
    await state.conn.close()
