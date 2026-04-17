from __future__ import annotations

from typing import Any

import aiosqlite
import structlog
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from monitor.bot import commands, feedback
from monitor.bot.commands import ConfigReloader
from monitor.state import DaemonState


log = structlog.get_logger(__name__)


def create_application(
    *,
    token: str,
    chat_id: str,
    conn: aiosqlite.Connection,
    state: DaemonState,
    pref_builder: Any,
    refresh_threshold: int,
    config_reloader: ConfigReloader,
) -> Application:
    """Build a PTB Application with all M4 handlers registered.

    Returns the Application without starting polling — caller owns the
    lifecycle (initialize / start / start_polling / stop) so shutdown
    sequencing can be composed with the rest of the daemon.
    """
    app = ApplicationBuilder().token(token).build()

    # Only process updates from the configured chat. Silent drop otherwise.
    # `filters.Chat(chat_id=int(chat_id))` uses numeric ids — telegram chat
    # ids can be negative (groups), but we store them as strings in Settings.
    chat_filter = filters.Chat(chat_id=int(chat_id))

    # Commands
    app.add_handler(
        CommandHandler(
            "top",
            _wrap(lambda update, _ctx: commands.handle_top(update, conn=conn)),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "status",
            _wrap(
                lambda update, _ctx: commands.handle_status(
                    update, conn=conn, state=state
                )
            ),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "pause",
            _wrap(lambda update, _ctx: commands.handle_pause(update, state=state)),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "resume",
            _wrap(lambda update, _ctx: commands.handle_resume(update, state=state)),
            filters=chat_filter,
        )
    )
    app.add_handler(
        CommandHandler(
            "reload",
            _wrap(
                lambda update, _ctx: commands.handle_reload(
                    update, state=state, config_reloader=config_reloader
                )
            ),
            filters=chat_filter,
        )
    )

    # Feedback buttons
    app.add_handler(
        CallbackQueryHandler(
            _wrap(
                lambda update, _ctx: feedback.handle_feedback_callback(
                    update,
                    conn=conn,
                    pref_builder=pref_builder,
                    refresh_threshold=refresh_threshold,
                )
            ),
            pattern=r"^fb:",
        )
    )

    app.add_error_handler(_error_handler)
    return app


def _wrap(coro_factory):
    """Adapt a plain `async (update) -> None` function into a PTB-shaped
    `async (update, context) -> None` handler. All our handlers use `update`
    only, so the context is discarded."""
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await coro_factory(update, context)
    return _handler


async def _error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    log.warning("bot.handler_error", error=str(context.error))
