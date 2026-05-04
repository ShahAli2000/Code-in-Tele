"""Idle reaper: an idle session is closed and the SDK client disconnected;
an active session is left alone."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from ct.runner.server import RunnerConnection, RunnerSession


def _make_conn(*, idle_minutes: float, interval: float) -> RunnerConnection:
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    return RunnerConnection(
        ws=ws,
        secret=None,
        idle_reap_minutes=idle_minutes,
        reaper_interval_s=interval,
    )


def _add_session(conn: RunnerConnection, sid: str, last_activity: float) -> RunnerSession:
    runner = MagicMock()
    runner.session_id = f"sdk-{sid}"
    runner.stop = AsyncMock()
    sess = RunnerSession(sid=sid, runner=runner, last_activity=last_activity)
    conn.sessions[sid] = sess
    return sess


async def test_reaper_closes_idle_session_and_leaves_active_one_alone() -> None:
    # 0.01 minute = 0.6s threshold; reaper ticks every 0.05s.
    conn = _make_conn(idle_minutes=0.01, interval=0.05)

    now = time.monotonic()
    idle = _add_session(conn, "idle", last_activity=now - 5.0)  # well past threshold
    fresh = _add_session(conn, "fresh", last_activity=now)

    reaper = asyncio.create_task(conn._reaper_loop())
    try:
        # Two ticks is plenty for a 0.05s interval.
        await asyncio.sleep(0.2)
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass

    # Idle session: stopped + removed; T_CLOSED sent on the wire.
    idle.runner.stop.assert_awaited()
    assert "idle" not in conn.sessions
    assert conn.ws.send.await_count >= 1, "T_CLOSED envelope should have been sent"

    # Active session: untouched.
    fresh.runner.stop.assert_not_called()
    assert "fresh" in conn.sessions


async def test_reaper_skips_session_with_in_flight_turn() -> None:
    """A session with a running turn shouldn't be reaped even if its
    last_activity is stale — turns can stretch across long stretches with no
    outbound message (a slow tool, a long Claude pause)."""
    conn = _make_conn(idle_minutes=0.01, interval=0.05)
    sess = _add_session(conn, "busy", last_activity=time.monotonic() - 5.0)

    # Pretend a turn is mid-flight.
    async def _never() -> None:
        await asyncio.sleep(60.0)
    sess.turn_task = asyncio.create_task(_never())

    reaper = asyncio.create_task(conn._reaper_loop())
    try:
        await asyncio.sleep(0.2)
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
        sess.turn_task.cancel()
        try:
            await sess.turn_task
        except asyncio.CancelledError:
            pass

    sess.runner.stop.assert_not_called()
    assert "busy" in conn.sessions


async def test_teardown_cancels_reaper() -> None:
    conn = _make_conn(idle_minutes=30.0, interval=0.05)
    conn._reaper_task = asyncio.create_task(conn._reaper_loop())
    # Let it enter its sleep.
    await asyncio.sleep(0.01)

    await conn._teardown()

    assert conn._reaper_task is None
    assert conn.sessions == {}
