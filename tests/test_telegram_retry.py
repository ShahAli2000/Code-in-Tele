"""RetryAfterMiddleware backs off on TelegramRetryAfter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramRetryAfter

from ct.bridge._telegram_retry import RetryAfterMiddleware, MAX_RETRIES


def _retry_exc(seconds: int = 1) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=MagicMock(),
        message="Too Many Requests",
        retry_after=seconds,
    )


async def test_first_call_succeeds_passthrough() -> None:
    mw = RetryAfterMiddleware()
    bot = MagicMock()
    method = MagicMock()
    method.__class__.__name__ = "SendMessage"
    next_call = AsyncMock(return_value="ok")

    result = await mw(next_call, bot, method)

    assert result == "ok"
    next_call.assert_awaited_once_with(bot, method)


async def test_retries_after_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fake out asyncio.sleep so the test runs instantly.
    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr("ct.bridge._telegram_retry.asyncio.sleep", fake_sleep)

    mw = RetryAfterMiddleware()
    bot = MagicMock()
    method = MagicMock()
    method.__class__.__name__ = "SendMessage"
    next_call = AsyncMock(side_effect=[_retry_exc(2), "ok"])

    result = await mw(next_call, bot, method)

    assert result == "ok"
    assert next_call.await_count == 2
    assert len(sleeps) == 1
    # We sleep at least the server-supplied retry_after.
    assert 2.0 <= sleeps[0] < 3.0  # plus tiny jitter under 0.5s


async def test_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(s: float) -> None:
        return None
    monkeypatch.setattr("ct.bridge._telegram_retry.asyncio.sleep", fake_sleep)

    mw = RetryAfterMiddleware()
    bot = MagicMock()
    method = MagicMock()
    method.__class__.__name__ = "SendMessage"
    # Always 429 → should raise after MAX_RETRIES + 1 attempts.
    next_call = AsyncMock(side_effect=_retry_exc(1))

    with pytest.raises(TelegramRetryAfter):
        await mw(next_call, bot, method)
    assert next_call.await_count == MAX_RETRIES + 1


async def test_retry_after_capped_at_max_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malicious or buggy retry_after of 9999s shouldn't pin the worker."""
    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr("ct.bridge._telegram_retry.asyncio.sleep", fake_sleep)

    mw = RetryAfterMiddleware()
    bot = MagicMock()
    method = MagicMock()
    method.__class__.__name__ = "SendMessage"
    next_call = AsyncMock(side_effect=[_retry_exc(9999), "ok"])

    await mw(next_call, bot, method)
    assert sleeps[0] <= 60.5  # MAX_SLEEP_S + jitter
