"""Reconnect-notice gating: short outages stay silent; long outages post once
in General (no per-topic spam)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ct.bridge.bot import BridgeBot


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (89, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (3661, "1h 1m"),
        (7200, "2h"),
        (86400, "24h"),
    ],
)
def test_humanize_duration(seconds: int, expected: str) -> None:
    assert BridgeBot._humanize_duration(seconds) == expected


def _make_bot() -> BridgeBot:
    bot = BridgeBot.__new__(BridgeBot)  # bypass __init__ since we only need the method
    bot.bot = MagicMock()
    bot.bot.send_message = AsyncMock()
    bot.settings = MagicMock()
    bot.settings.telegram_chat_id = -1001234567890
    return bot


async def test_short_outage_is_silent() -> None:
    bot = _make_bot()
    # Anything under the 5 min threshold is a sleep/wake blip — no notice.
    await bot.on_runner_reconnect("laptop", ["10", "20"], outage_seconds=42.0)
    bot.bot.send_message.assert_not_called()


async def test_long_outage_posts_once_in_general() -> None:
    bot = _make_bot()
    await bot.on_runner_reconnect("laptop", ["10", "20", "30"], outage_seconds=900.0)
    # Exactly one call (not one per session id).
    bot.bot.send_message.assert_awaited_once()
    kwargs = bot.bot.send_message.await_args.kwargs
    # General topic = no message_thread_id kwarg.
    assert "message_thread_id" not in kwargs
    assert "laptop" in kwargs["text"]
    assert "15m" in kwargs["text"]  # 900s humanized


async def test_threshold_boundary_posts() -> None:
    bot = _make_bot()
    # Threshold is inclusive on the post side: 5min exactly → notify.
    # Just-under (299s) stays silent — verified separately via other tests.
    await bot.on_runner_reconnect("studio", ["1"], outage_seconds=300.0)
    bot.bot.send_message.assert_awaited_once()


async def test_just_under_threshold_silent() -> None:
    bot = _make_bot()
    await bot.on_runner_reconnect("studio", ["1"], outage_seconds=299.9)
    bot.bot.send_message.assert_not_called()
