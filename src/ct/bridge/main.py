"""Entry point for the bridge.

Run from the project root:
    uv run python -m ct.bridge.main

Long-polls Telegram (no public IP / webhook needed). Wires aiogram dispatcher,
allowlist middleware, command + topic-message handlers, permission inline-button
callback. Stops every active SessionRunner cleanly on SIGINT / SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from ct.bridge.bot import BridgeBot
from ct.config import load_settings
from ct.store.db import Db


def _configure_logging(level: str, fmt: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


async def _run() -> int:
    settings = load_settings()
    _configure_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger("ct.bridge.main")

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    db = Db(settings.db_path)
    await db.open()
    bridge = BridgeBot(bot=bot, settings=settings, db=db)

    me = await bot.get_me()
    log.info(
        "bridge.starting",
        bot_username=me.username,
        bot_id=me.id,
        chat_id=settings.telegram_chat_id,
        allowed_users=len(settings.telegram_allowed_user_ids),
        db_path=str(settings.db_path),
    )

    # Rehydrate active sessions from the DB before polling, so messages that
    # land on an existing topic immediately route to a live runner.
    restored = await bridge.restore_sessions()
    log.info("bridge.ready", restored_sessions=restored)

    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        log.info("bridge.signal", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    polling_task = asyncio.create_task(
        bridge.dp.start_polling(
            bot,
            allowed_updates=bridge.dp.resolve_used_update_types(),
            handle_signals=False,
        ),
        name="bridge-polling",
    )

    await stop_event.wait()
    log.info("bridge.shutting_down")

    bridge.dp.stop_polling.set() if hasattr(bridge.dp, "stop_polling") else None
    polling_task.cancel()
    try:
        await polling_task
    except (asyncio.CancelledError, Exception):
        pass

    await bridge.shutdown()
    await bot.session.close()
    await db.close()
    log.info("bridge.stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
