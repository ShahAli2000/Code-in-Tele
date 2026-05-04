"""Aiogram outgoing-request middleware that handles Telegram's 429
TelegramRetryAfter for us. Without this, the bridge logs the error and
moves on — losing heartbeat updates, permission cards, or final answers
during bursts (e.g. multi-session boot, simultaneous turn ends).

Strategy: respect the server-supplied retry_after, sleep for that long
plus a small jitter, retry up to MAX_RETRIES. Beyond that, raise — caller
sees the error like before.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType

log = structlog.get_logger(__name__)

MAX_RETRIES = 2
# Bound the sleep so a malicious "retry_after: 999999" can't pin a worker
# forever. Telegram's actual values for normal bots are typically 1–30s.
MAX_SLEEP_S = 60.0


class RetryAfterMiddleware(BaseRequestMiddleware):
    """Retry on 429s with the server-supplied retry_after."""

    async def __call__(  # type: ignore[override]
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt >= MAX_RETRIES:
                    log.warning(
                        "telegram.retry_after_exhausted",
                        method=method.__class__.__name__,
                        retry_after=exc.retry_after,
                        attempt=attempt,
                    )
                    raise
                sleep_for = min(float(exc.retry_after), MAX_SLEEP_S)
                # Tiny jitter so a fleet of senders doesn't perfectly resync.
                sleep_for += random.uniform(0.0, 0.5)
                log.info(
                    "telegram.retry_after",
                    method=method.__class__.__name__,
                    retry_after=exc.retry_after,
                    sleeping=round(sleep_for, 2),
                    attempt=attempt,
                )
                await asyncio.sleep(sleep_for)
        # Unreachable: the loop either returns a response or re-raises.
        raise RuntimeError("retry middleware fell through")  # pragma: no cover


def install(bot: Bot) -> None:
    """Register the retry middleware on the bot's HTTP session. Idempotent —
    we only install one instance per bot."""
    for mw in bot.session.middleware:
        if isinstance(mw, RetryAfterMiddleware):
            return
    bot.session.middleware.register(RetryAfterMiddleware())
    log.info("telegram.retry_middleware_installed")
