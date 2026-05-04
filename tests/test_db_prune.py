"""DB prune: closed sessions older than N days are deleted along with
their message_log + pending_permissions rows."""

from __future__ import annotations

import pathlib

import pytest

from ct.store.db import Db


@pytest.fixture
async def db(tmp_path: pathlib.Path) -> Db:
    d = Db(tmp_path / "test.db")
    await d.open()
    yield d
    await d.close()


async def test_prune_keeps_active_sessions(db: Db) -> None:
    await db.insert_session(
        thread_id=1, project_name="alive", cwd="/tmp", permission_mode="acceptEdits"
    )
    counts = await db.prune_old_closed(days=30)
    assert counts["sessions"] == 0
    row = await db.get_session(1)
    assert row is not None and row.state == "active"


async def test_prune_keeps_recently_closed_sessions(db: Db) -> None:
    await db.insert_session(
        thread_id=2, project_name="just-closed", cwd="/tmp", permission_mode="acceptEdits"
    )
    await db.mark_closed(2)
    # Just closed → last_activity = now → not pruned at any positive day count.
    counts = await db.prune_old_closed(days=1)
    assert counts["sessions"] == 0
    assert (await db.get_session(2)) is not None


async def test_prune_removes_old_closed_sessions(db: Db) -> None:
    await db.insert_session(
        thread_id=3, project_name="ancient", cwd="/tmp", permission_mode="acceptEdits"
    )
    # Backdate via raw SQL — there's no public API for "make this look old".
    await db.conn.execute(
        "UPDATE sessions SET state='closed', last_activity = datetime('now', '-100 days') "
        "WHERE thread_id = 3"
    )
    await db.conn.commit()
    counts = await db.prune_old_closed(days=90)
    assert counts["sessions"] == 1
    assert (await db.get_session(3)) is None


async def test_prune_cleans_dependent_rows(db: Db) -> None:
    await db.insert_session(
        thread_id=4, project_name="with-history", cwd="/tmp", permission_mode="acceptEdits"
    )
    await db.log_event(thread_id=4, kind="text", payload={"sample": "x"})
    await db.log_event(thread_id=4, kind="text", payload={"sample": "y"})
    await db.conn.execute(
        "UPDATE sessions SET state='closed', last_activity = datetime('now', '-100 days') "
        "WHERE thread_id = 4"
    )
    await db.conn.commit()
    counts = await db.prune_old_closed(days=90)
    assert counts["sessions"] == 1
    assert counts["message_log"] == 2
    # message_log for that thread is gone too
    async with db.conn.execute(
        "SELECT COUNT(*) FROM message_log WHERE thread_id = 4"
    ) as cur:
        assert (await cur.fetchone())[0] == 0


async def test_vacuum_runs_without_error(db: Db) -> None:
    # Just exercise the path — VACUUM is hard to assert anything semantic about.
    await db.vacuum()
