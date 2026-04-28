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
    runner_name: str = "studio"
    model: str | None = None
    effort: str | None = None


@dataclass
class PendingPermissionRow:
    tool_use_id: str
    thread_id: int
    message_id: int
    tool_name: str
    input_json: str
    created_at: str
    decided_at: str | None
    decision: str | None


@dataclass
class ProfileRow:
    name: str
    dir: str | None
    runner_name: str | None
    model: str | None
    effort: str | None
    permission_mode: str | None
    created_at: str
    updated_at: str


@dataclass
class MacRow:
    name: str
    host: str
    port: int
    added_at: str
    last_connected: str | None
    main_dir: str | None = None


class Db:
    """Async SQLite handle. One instance per process; safe to share across coroutines."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Create the parent dir if needed, open the connection, run schema.sql,
        then apply any forward-only column migrations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        # Make foreign-key checks the default; let SQLite manage WAL for crash safety.
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        schema = _SCHEMA_PATH.read_text()
        await self._conn.executescript(schema)
        await self._migrate()
        await self._conn.commit()
        log.info("db.opened", path=str(self.path))

    async def _migrate(self) -> None:
        """Add columns that CREATE IF NOT EXISTS can't introduce on existing
        tables. Each block here is idempotent — safe to run on every boot."""
        async with self.conn.execute("PRAGMA table_info(sessions)") as cur:
            session_cols = {row[1] for row in await cur.fetchall()}
        if "runner_name" not in session_cols:
            await self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN runner_name TEXT NOT NULL DEFAULT 'studio'"
            )
            log.info("db.migrated", change="sessions.runner_name added")
        if "model" not in session_cols:
            await self.conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
            log.info("db.migrated", change="sessions.model added")
        if "effort" not in session_cols:
            await self.conn.execute("ALTER TABLE sessions ADD COLUMN effort TEXT")
            log.info("db.migrated", change="sessions.effort added")

        async with self.conn.execute("PRAGMA table_info(macs)") as cur:
            mac_cols = {row[1] for row in await cur.fetchall()}
        if "main_dir" not in mac_cols:
            await self.conn.execute("ALTER TABLE macs ADD COLUMN main_dir TEXT")
            log.info("db.migrated", change="macs.main_dir added")

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
        runner_name: str = "studio",
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO sessions(
                thread_id, project_name, cwd, permission_mode, state,
                runner_name, model, effort
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (thread_id, project_name, cwd, permission_mode, runner_name, model, effort),
        )
        await self.conn.commit()

    async def update_session_model(self, thread_id: int, model: str | None) -> None:
        await self.conn.execute(
            "UPDATE sessions SET model = ?, last_activity = datetime('now') WHERE thread_id = ?",
            (model, thread_id),
        )
        await self.conn.commit()

    async def update_session_effort(self, thread_id: int, effort: str | None) -> None:
        await self.conn.execute(
            "UPDATE sessions SET effort = ?, last_activity = datetime('now') WHERE thread_id = ?",
            (effort, thread_id),
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
            "SELECT thread_id, project_name, cwd, sdk_session_id, permission_mode, state, "
            "created_at, last_activity, runner_name, model, effort "
            "FROM sessions WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SessionRow(*row)

    async def list_active_sessions(self) -> list[SessionRow]:
        async with self.conn.execute(
            "SELECT thread_id, project_name, cwd, sdk_session_id, permission_mode, state, "
            "created_at, last_activity, runner_name, model, effort "
            "FROM sessions WHERE state = 'active' ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [SessionRow(*r) for r in rows]

    # ---- pending permissions -----------------------------------------------

    async def insert_pending_permission(
        self,
        *,
        tool_use_id: str,
        thread_id: int,
        message_id: int,
        tool_name: str,
        input_json: str,
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO pending_permissions(
                tool_use_id, thread_id, message_id, tool_name, input_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (tool_use_id, thread_id, message_id, tool_name, input_json),
        )
        await self.conn.commit()

    async def mark_permission_decided(
        self, tool_use_id: str, decision: str
    ) -> None:
        await self.conn.execute(
            "UPDATE pending_permissions SET decided_at = datetime('now'), decision = ? "
            "WHERE tool_use_id = ?",
            (decision, tool_use_id),
        )
        await self.conn.commit()

    async def delete_pending_permission(self, tool_use_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM pending_permissions WHERE tool_use_id = ?", (tool_use_id,)
        )
        await self.conn.commit()

    async def list_undecided_permissions(self) -> list[PendingPermissionRow]:
        async with self.conn.execute(
            "SELECT tool_use_id, thread_id, message_id, tool_name, input_json, "
            "created_at, decided_at, decision "
            "FROM pending_permissions WHERE decided_at IS NULL ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [PendingPermissionRow(*r) for r in rows]

    # ---- profiles ----------------------------------------------------------

    async def upsert_profile(
        self,
        *,
        name: str,
        dir: str | None = None,
        runner_name: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO profiles(name, dir, runner_name, model, effort, permission_mode)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                dir = excluded.dir,
                runner_name = excluded.runner_name,
                model = excluded.model,
                effort = excluded.effort,
                permission_mode = excluded.permission_mode,
                updated_at = datetime('now')
            """,
            (name, dir, runner_name, model, effort, permission_mode),
        )
        await self.conn.commit()

    async def delete_profile(self, name: str) -> bool:
        async with self.conn.execute(
            "DELETE FROM profiles WHERE name = ?", (name,)
        ) as cur:
            deleted = cur.rowcount or 0
        await self.conn.commit()
        return deleted > 0

    async def get_profile(self, name: str) -> ProfileRow | None:
        async with self.conn.execute(
            "SELECT name, dir, runner_name, model, effort, permission_mode, "
            "created_at, updated_at FROM profiles WHERE name = ?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return ProfileRow(*row) if row else None

    async def list_profiles(self) -> list[ProfileRow]:
        async with self.conn.execute(
            "SELECT name, dir, runner_name, model, effort, permission_mode, "
            "created_at, updated_at FROM profiles ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [ProfileRow(*r) for r in rows]

    # ---- bot defaults (key/value via meta) ---------------------------------

    DEFAULT_KEYS = (
        "default_runner_name",
        "default_model",
        "default_effort",
        "default_permission_mode",
    )

    async def get_default(self, key: str) -> str | None:
        if key not in self.DEFAULT_KEYS:
            raise ValueError(f"unknown default key: {key!r}")
        async with self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        v = row[0]
        return v if v else None

    async def set_default(self, key: str, value: str | None) -> None:
        if key not in self.DEFAULT_KEYS:
            raise ValueError(f"unknown default key: {key!r}")
        if value is None:
            await self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, '') "
                "ON CONFLICT(key) DO UPDATE SET value=''",
                (key,),
            )
        else:
            await self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        await self.conn.commit()

    async def all_defaults(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for k in self.DEFAULT_KEYS:
            out[k] = await self.get_default(k)
        return out

    # ---- macs ---------------------------------------------------------------

    async def insert_mac(
        self, name: str, host: str, port: int, *, main_dir: str | None = None
    ) -> None:
        # Preserve existing main_dir if the row already exists and the caller
        # didn't pass one explicitly.
        existing = None
        if main_dir is None:
            async with self.conn.execute(
                "SELECT main_dir FROM macs WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                existing = row[0]
        await self.conn.execute(
            "INSERT OR REPLACE INTO macs(name, host, port, main_dir) VALUES (?, ?, ?, ?)",
            (name, host, port, main_dir if main_dir is not None else existing),
        )
        await self.conn.commit()

    async def update_mac_main_dir(self, name: str, main_dir: str | None) -> None:
        await self.conn.execute(
            "UPDATE macs SET main_dir = ? WHERE name = ?",
            (main_dir, name),
        )
        await self.conn.commit()

    async def get_mac(self, name: str) -> MacRow | None:
        async with self.conn.execute(
            "SELECT name, host, port, added_at, last_connected, main_dir "
            "FROM macs WHERE name = ?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return MacRow(*row) if row else None

    async def remove_mac(self, name: str) -> bool:
        async with self.conn.execute(
            "DELETE FROM macs WHERE name = ?", (name,)
        ) as cur:
            deleted = cur.rowcount or 0
        await self.conn.commit()
        return deleted > 0

    async def update_mac_connected(self, name: str) -> None:
        await self.conn.execute(
            "UPDATE macs SET last_connected = datetime('now') WHERE name = ?",
            (name,),
        )
        await self.conn.commit()

    async def list_macs(self) -> list[MacRow]:
        async with self.conn.execute(
            "SELECT name, host, port, added_at, last_connected, main_dir "
            "FROM macs ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [MacRow(*r) for r in rows]
