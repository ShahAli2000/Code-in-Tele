"""Async SQLite DAL for the bridge.

One Db instance per process. All writes go through here so the in-memory
session map and the on-disk store stay in sync. Migrations are not yet
needed — schema.sql uses CREATE IF NOT EXISTS so re-running on boot is safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _row_to_session(row: tuple) -> "SessionRow":
    """Build a SessionRow from a 12-column DB row. SQLite returns thinking as
    an INTEGER (0/1); coerce to bool so callers don't have to remember."""
    return SessionRow(*row[:11], thinking=bool(row[11]))


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
    # 1 = adaptive thinking on, 0 = off. Default ON for new sessions; existing
    # rows migrate to ON via the column default.
    thinking: bool = True


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
    # Free-form text appended to Claude Code's system prompt. Lets a profile
    # carry project-specific instructions (e.g. "you're working on a Rust
    # codebase, prefer cargo over python idioms").
    system_prompt: str | None = None


@dataclass
class MacRow:
    name: str
    host: str
    port: int
    added_at: str
    last_connected: str | None
    main_dir: str | None = None
    # Optional list of shortcut paths the user can quick-jump to from the
    # browse picker. Stored as a JSON array of strings; an empty list (or
    # None) means "no shortcuts; browse opens main_dir directly".
    shortcuts: list[str] = field(default_factory=list)


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
        if "thinking" not in session_cols:
            # DEFAULT 1 backfills existing rows to thinking-on, matching the
            # post-migration default for new sessions.
            await self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN thinking INTEGER NOT NULL DEFAULT 1"
            )
            log.info("db.migrated", change="sessions.thinking added")

        async with self.conn.execute("PRAGMA table_info(macs)") as cur:
            mac_cols = {row[1] for row in await cur.fetchall()}
        if "main_dir" not in mac_cols:
            await self.conn.execute("ALTER TABLE macs ADD COLUMN main_dir TEXT")
            log.info("db.migrated", change="macs.main_dir added")
        if "shortcuts" not in mac_cols:
            await self.conn.execute("ALTER TABLE macs ADD COLUMN shortcuts TEXT")
            log.info("db.migrated", change="macs.shortcuts added")

        async with self.conn.execute("PRAGMA table_info(profiles)") as cur:
            profile_cols = {row[1] for row in await cur.fetchall()}
        if "system_prompt" not in profile_cols:
            await self.conn.execute("ALTER TABLE profiles ADD COLUMN system_prompt TEXT")
            log.info("db.migrated", change="profiles.system_prompt added")

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
        thinking: bool = True,
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO sessions(
                thread_id, project_name, cwd, permission_mode, state,
                runner_name, model, effort, thinking
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                thread_id, project_name, cwd, permission_mode,
                runner_name, model, effort, 1 if thinking else 0,
            ),
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

    async def update_session_thinking(self, thread_id: int, thinking: bool) -> None:
        await self.conn.execute(
            "UPDATE sessions SET thinking = ?, last_activity = datetime('now') WHERE thread_id = ?",
            (1 if thinking else 0, thread_id),
        )
        await self.conn.commit()

    async def update_session_runner_name(
        self, thread_id: int, runner_name: str
    ) -> None:
        """Used by /move when a session migrates to a different mac. Restore
        on next bridge boot reads runner_name to know where to re-open."""
        await self.conn.execute(
            "UPDATE sessions SET runner_name = ?, last_activity = datetime('now') WHERE thread_id = ?",
            (runner_name, thread_id),
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
            "created_at, last_activity, runner_name, model, effort, thinking "
            "FROM sessions WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    async def list_active_sessions(self) -> list[SessionRow]:
        async with self.conn.execute(
            "SELECT thread_id, project_name, cwd, sdk_session_id, permission_mode, state, "
            "created_at, last_activity, runner_name, model, effort, thinking "
            "FROM sessions WHERE state = 'active' ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_session(r) for r in rows]

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

    async def list_undecided_permissions(
        self, *, older_than_minutes: int | None = None
    ) -> list[PendingPermissionRow]:
        sql = (
            "SELECT tool_use_id, thread_id, message_id, tool_name, input_json, "
            "created_at, decided_at, decision "
            "FROM pending_permissions WHERE decided_at IS NULL"
        )
        params: tuple = ()
        if older_than_minutes is not None:
            sql += " AND created_at <= datetime('now', ?)"
            sql += " ORDER BY created_at"
            params = (f"-{int(older_than_minutes)} minutes",)
        else:
            sql += " ORDER BY created_at"
        async with self.conn.execute(sql, params) as cur:
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

    async def update_profile_system_prompt(
        self, name: str, system_prompt: str | None
    ) -> bool:
        """Set or clear the per-profile system_prompt fragment. Edited
        independently of the rest of the profile because the text is usually
        too long to pass through `/save name dir=…` flag-style syntax."""
        async with self.conn.execute(
            "UPDATE profiles SET system_prompt = ?, updated_at = datetime('now') "
            "WHERE name = ?",
            (system_prompt, name),
        ) as cur:
            updated = cur.rowcount or 0
        await self.conn.commit()
        return updated > 0

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
            "created_at, updated_at, system_prompt FROM profiles WHERE name = ?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return ProfileRow(*row) if row else None

    async def list_profiles(self) -> list[ProfileRow]:
        async with self.conn.execute(
            "SELECT name, dir, runner_name, model, effort, permission_mode, "
            "created_at, updated_at, system_prompt FROM profiles ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [ProfileRow(*r) for r in rows]

    # ---- bot defaults (key/value via meta) ---------------------------------

    DEFAULT_KEYS = (
        "default_runner_name",
        "default_model",
        "default_effort",
        "default_permission_mode",
        "quiet_hours_start",   # HH:MM (24h, local TZ); empty = quiet hours off
        "quiet_hours_end",     # HH:MM (24h, local TZ)
        "default_auto_allow_tools",  # CSV of tool names to auto-approve at session start
        "dashboard_token",      # static token for web-UI auth; auto-generated at first boot
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

    # ---- message_log (lightweight observability) ---------------------------

    async def log_event(
        self, thread_id: int, kind: str, payload: dict | None = None
    ) -> None:
        """Append an event to message_log. Fire-and-forget; failures are
        logged but never raised — observability shouldn't ever break the
        actual session flow."""
        import json as _json
        body = None
        if payload is not None:
            try:
                body = _json.dumps(payload, default=str, ensure_ascii=False)[:2048]
            except (TypeError, ValueError):
                body = repr(payload)[:2048]
        try:
            await self.conn.execute(
                "INSERT INTO message_log(thread_id, kind, payload) VALUES (?, ?, ?)",
                (thread_id, kind, body),
            )
            await self.conn.commit()
        except Exception:
            log.exception("message_log.insert_failed", thread_id=thread_id, kind=kind)

    async def stats_overview(self) -> dict[str, Any]:
        """Aggregate message_log for /stats. Returns:
            {
                "total_events": int,
                "by_kind": {kind: count, ...},
                "top_tools": [(name, count), ...],   # at most 10
                "by_session": [(thread_id, total, last_ts), ...],
                "recent_24h": int,
            }"""
        import json as _json
        out: dict[str, Any] = {
            "total_events": 0,
            "by_kind": {},
            "top_tools": [],
            "by_session": [],
            "recent_24h": 0,
        }
        async with self.conn.execute("SELECT COUNT(*) FROM message_log") as cur:
            row = await cur.fetchone()
            out["total_events"] = int(row[0]) if row else 0
        if out["total_events"] == 0:
            return out
        async with self.conn.execute(
            "SELECT kind, COUNT(*) FROM message_log GROUP BY kind"
        ) as cur:
            for k, c in await cur.fetchall():
                out["by_kind"][k] = c
        async with self.conn.execute(
            "SELECT COUNT(*) FROM message_log "
            "WHERE ts >= datetime('now', '-1 day')"
        ) as cur:
            row = await cur.fetchone()
            out["recent_24h"] = int(row[0]) if row else 0

        # Tool usage: parse payload JSON for kind=tool_use
        tool_counts: dict[str, int] = {}
        async with self.conn.execute(
            "SELECT payload FROM message_log WHERE kind = 'tool_use'"
        ) as cur:
            async for (payload,) in cur:
                if not payload:
                    continue
                try:
                    p = _json.loads(payload)
                except Exception:
                    continue
                name = p.get("name") if isinstance(p, dict) else None
                if isinstance(name, str) and name:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
        out["top_tools"] = sorted(
            tool_counts.items(), key=lambda kv: kv[1], reverse=True
        )[:10]

        # Per-session: total events + last activity
        async with self.conn.execute(
            "SELECT thread_id, COUNT(*), MAX(ts) FROM message_log "
            "GROUP BY thread_id ORDER BY MAX(ts) DESC LIMIT 20"
        ) as cur:
            for tid, n, ts in await cur.fetchall():
                out["by_session"].append((int(tid), int(n), str(ts)))
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
            "SELECT name, host, port, added_at, last_connected, main_dir, "
            "shortcuts FROM macs WHERE name = ?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return _macrow_from_row(row) if row else None

    async def update_mac_shortcuts(self, name: str, shortcuts: list[str]) -> None:
        """Replace the shortcuts list for `name`. Pass an empty list to clear.
        Stored as a JSON array so SQLite stays type-stable."""
        body = json.dumps(list(shortcuts)) if shortcuts else None
        await self.conn.execute(
            "UPDATE macs SET shortcuts = ? WHERE name = ?", (body, name),
        )
        await self.conn.commit()

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
            "SELECT name, host, port, added_at, last_connected, main_dir, "
            "shortcuts FROM macs ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [_macrow_from_row(r) for r in rows]


def _macrow_from_row(row: tuple) -> MacRow:
    """Build a MacRow from a 7-tuple SELECT, decoding the JSON shortcuts col."""
    raw_shortcuts = row[6]
    parsed: list[str] = []
    if raw_shortcuts:
        try:
            decoded = json.loads(raw_shortcuts)
            if isinstance(decoded, list):
                parsed = [s for s in decoded if isinstance(s, str)]
        except (json.JSONDecodeError, TypeError):
            pass
    return MacRow(
        name=row[0], host=row[1], port=row[2],
        added_at=row[3], last_connected=row[4], main_dir=row[5],
        shortcuts=parsed,
    )
