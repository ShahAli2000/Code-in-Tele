"""Async SQLite DAL for the bridge.

One Db instance per process. All writes go through here so the in-memory
session map and the on-disk store stay in sync. Migrations are not yet
needed — schema.sql uses CREATE IF NOT EXISTS so re-running on boot is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


@dataclass
class SessionRow:
    thread_id: int
    project_name: str
    cwd: str
    sdk_session_id: str | None
    permission_mode: str
    state: str
    created_at: str
    last_activity: str


class Db:
    """Async SQLite handle. One instance per process; safe to share across coroutines."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Create the parent dir if needed, open the connection, run schema.sql."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        # Make foreign-key checks the default; let SQLite manage WAL for crash safety.
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        schema = _SCHEMA_PATH.read_text()
        await self._conn.executescript(schema)
        await self._conn.commit()
        log.info("db.opened", path=str(self.path))

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Db not opened — call await db.open() first")
        return self._conn

    # ---- sessions -----------------------------------------------------------

    async def insert_session(
        self,
        *,
        thread_id: int,
        project_name: str,
        cwd: str,
        permission_mode: str,
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO sessions(
                thread_id, project_name, cwd, permission_mode, state
            ) VALUES (?, ?, ?, ?, 'active')
            """,
            (thread_id, project_name, cwd, permission_mode),
        )
        await self.conn.commit()

    async def update_sdk_session_id(self, thread_id: int, sdk_session_id: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET sdk_session_id = ?, last_activity = datetime('now') WHERE thread_id = ?",
            (sdk_session_id, thread_id),
        )
        await self.conn.commit()

    async def update_permission_mode(self, thread_id: int, mode: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET permission_mode = ?, last_activity = datetime('now') WHERE thread_id = ?",
            (mode, thread_id),
        )
        await self.conn.commit()

    async def touch(self, thread_id: int) -> None:
        await self.conn.execute(
            "UPDATE sessions SET last_activity = datetime('now') WHERE thread_id = ?",
            (thread_id,),
        )
        await self.conn.commit()

    async def mark_closed(self, thread_id: int) -> None:
        await self.conn.execute(
            "UPDATE sessions SET state = 'closed', last_activity = datetime('now') WHERE thread_id = ?",
            (thread_id,),
        )
        await self.conn.commit()

    async def mark_orphaned(self, thread_id: int, reason: str = "") -> None:
        await self.conn.execute(
            "UPDATE sessions SET state = 'orphaned', last_activity = datetime('now') WHERE thread_id = ?",
            (thread_id,),
        )
        await self.conn.commit()
        log.warning("db.session_orphaned", thread_id=thread_id, reason=reason)

    async def get_session(self, thread_id: int) -> Optional[SessionRow]:
        async with self.conn.execute(
            "SELECT thread_id, project_name, cwd, sdk_session_id, permission_mode, state, created_at, last_activity "
            "FROM sessions WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SessionRow(*row)

    async def list_active_sessions(self) -> list[SessionRow]:
        async with self.conn.execute(
            "SELECT thread_id, project_name, cwd, sdk_session_id, permission_mode, state, created_at, last_activity "
            "FROM sessions WHERE state = 'active' ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [SessionRow(*r) for r in rows]
