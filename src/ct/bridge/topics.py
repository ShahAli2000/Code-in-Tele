"""Forum topic helpers — wrap aiogram's createForumTopic and friends."""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

log = structlog.get_logger(__name__)

# Telegram's General topic is always thread_id=1; can't be modified.
GENERAL_TOPIC_ID = 1

# 32-color icon palette Telegram allows for forum topics. Picked deterministically
# off the project name so /new "foo" always gets the same color.
_TOPIC_ICON_COLORS = (
    7322096, 16766590, 13338331, 9367192,
    16749490, 16478047,
)


def _color_for_name(name: str) -> int:
    return _TOPIC_ICON_COLORS[hash(name) % len(_TOPIC_ICON_COLORS)]


async def create_topic(bot: Bot, chat_id: int, name: str) -> int:
    """Create a forum topic, return its message_thread_id."""
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=name[:128],
            icon_color=_color_for_name(name),
        )
    except TelegramBadRequest as exc:
        log.error("topic.create_failed", name=name, error=str(exc))
        raise
    log.info("topic.created", name=name, thread_id=topic.message_thread_id)
    return topic.message_thread_id


async def close_topic(bot: Bot, chat_id: int, thread_id: int) -> None:
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except TelegramBadRequest as exc:
        log.warning("topic.close_failed", thread_id=thread_id, error=str(exc))
