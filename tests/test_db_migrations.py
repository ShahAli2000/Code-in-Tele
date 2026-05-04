"""Schema migrations are idempotent and forward-only — Db.open() runs them
on every boot and must not fail on:
1. A fresh DB (everything created by schema.sql).
2. A DB that already has all the migrations applied.
3. A DB at every intermediate state (one column added, others not yet).

Without this test a silent break in the migration sequence would only
surface on next deploy when the bridge fails to start."""

from __future__ import annotations

import pathlib

import aiosqlite
import pytest

from ct.store.db import Db


async def test_fresh_db_opens_cleanly(tmp_path: pathlib.Path) -> None:
    db = Db(tmp_path / "fresh.db")
    await db.open()
    # All migration target columns should exist on the sessions table.
    async with db.conn.execute("PRAGMA table_info(sessions)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    for col in ("runner_name", "model", "effort", "thinking"):
        assert col in cols, f"sessions.{col} missing after fresh open"
    await db.close()


async def test_open_is_idempotent(tmp_path: pathlib.Path) -> None:
    db = Db(tmp_path / "idemp.db")
    await db.open()
    await db.close()
    # Re-open: migrations should be no-ops, not raise.
    db2 = Db(tmp_path / "idemp.db")
    await db2.open()
    await db2.close()


async def test_migration_from_pre_runner_name_state(tmp_path: pathlib.Path) -> None:
    """Simulate a DB from before the multi-mac migration — sessions table
    has no runner_name column. Db.open() must add it."""
    path = tmp_path / "old.db"
    # Hand-craft a sessions table without runner_name.
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """
            CREATE TABLE sessions (
                thread_id INTEGER PRIMARY KEY,
                project_name TEXT NOT NULL UNIQUE,
                cwd TEXT NOT NULL,
                sdk_session_id TEXT,
                permission_mode TEXT NOT NULL DEFAULT 'acceptEdits',
                state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_activity TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
            "INSERT INTO sessions(thread_id, project_name, cwd) VALUES (1, 'p', '/tmp')"
        )
        await conn.commit()

    # Now open via Db — migrations should add runner_name + model + effort + thinking
    # without losing the existing row.
    db = Db(path)
    await db.open()
    async with db.conn.execute("PRAGMA table_info(sessions)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert "runner_name" in cols
    assert "model" in cols
    assert "effort" in cols
    assert "thinking" in cols
    # Existing row preserved with the default values.
    row = await db.get_session(1)
    assert row is not None
    assert row.runner_name == "studio"
    assert row.thinking is True
    await db.close()
