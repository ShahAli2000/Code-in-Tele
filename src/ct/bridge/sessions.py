"""Session store: in-memory runner registry, write-through to SQLite.

The in-memory map (thread_id -> TopicSession) holds the live SessionRunner
instances. Every mutation also lands in `Db` so the bot can rehydrate after a
restart. `restore()` is the inverse — at boot it reads active rows from the DB
and uses `runner_factory` to recreate live runners with `resume=sdk_session_id`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from ct.sdk_adapter.adapter import PermissionMode, SessionRunner
from ct.store.db import Db

log = structlog.get_logger(__name__)


@dataclass
class TopicSession:
    """One live Claude session bound to a Telegram forum topic."""

    thread_id: int
    project_name: str
    cwd: str
    runner: SessionRunner
    turn_lock: asyncio.Lock


# Bridge supplies this when calling restore(). Given the persisted metadata for
# a session, return a fully-wired SessionRunner (handlers attached) — but
# don't call .start() yet; restore() does that.
RunnerFactory = Callable[
    ["RestoreSpec"], Awaitable[SessionRunner]
]


@dataclass
class RestoreSpec:
    thread_id: int
    project_name: str
    cwd: str
    sdk_session_id: str | None
    permission_mode: PermissionMode


class SessionStore:
    """Thread-id -> TopicSession registry, persisted via Db."""

    def __init__(self, db: Db) -> None:
        self._db = db
        self._by_thread: dict[int, TopicSession] = {}

    # ---- runtime mutations (always write through to DB) --------------------

    async def add(self, session: TopicSession) -> None:
        if session.thread_id in self._by_thread:
            raise RuntimeError(f"thread_id {session.thread_id} already has a session")
        await self._db.insert_session(
            thread_id=session.thread_id,
            project_name=session.project_name,
            cwd=session.cwd,
            permission_mode=session.runner.permission_mode,
        )
        self._by_thread[session.thread_id] = session

    async def update_sdk_session_id(self, thread_id: int, sdk_session_id: str) -> None:
        await self._db.update_sdk_session_id(thread_id, sdk_session_id)

    async def update_permission_mode(self, thread_id: int, mode: str) -> None:
        await self._db.update_permission_mode(thread_id, mode)

    async def touch(self, thread_id: int) -> None:
        await self._db.touch(thread_id)

    async def close(self, thread_id: int) -> TopicSession | None:
        await self._db.mark_closed(thread_id)
        return self._by_thread.pop(thread_id, None)

    # ---- read paths (cheap, in-memory) -------------------------------------

    def get(self, thread_id: int) -> TopicSession | None:
        return self._by_thread.get(thread_id)

    def all(self) -> list[TopicSession]:
        return list(self._by_thread.values())

    def __len__(self) -> int:
        return len(self._by_thread)

    # ---- restore-on-boot ---------------------------------------------------

    async def restore(self, factory: RunnerFactory) -> int:
        """For each active session in the DB, ask `factory` for a runner with
        the right resume id, start it, and add to the in-memory map.

        Returns the number of sessions successfully restored. Sessions whose
        `sdk_session_id` is null (never had a first turn so no resumable id) or
        whose resume fails are marked orphaned in the DB and skipped.
        """
        rows = await self._db.list_active_sessions()
        restored = 0
        for row in rows:
            if row.sdk_session_id is None:
                await self._db.mark_orphaned(
                    row.thread_id, reason="never had first turn (no sdk_session_id)"
                )
                log.info(
                    "session.restore_skipped_no_sdk_id",
                    thread_id=row.thread_id,
                    project=row.project_name,
                )
                continue
            spec = RestoreSpec(
                thread_id=row.thread_id,
                project_name=row.project_name,
                cwd=row.cwd,
                sdk_session_id=row.sdk_session_id,
                permission_mode=row.permission_mode,  # type: ignore[arg-type]
            )
            try:
                runner = await factory(spec)
                await runner.start()
            except Exception as exc:
                log.exception(
                    "session.restore_failed",
                    thread_id=row.thread_id,
                    sdk_session_id=row.sdk_session_id,
                )
                await self._db.mark_orphaned(
                    row.thread_id, reason=f"resume failed: {exc!r}"
                )
                continue
            self._by_thread[row.thread_id] = TopicSession(
                thread_id=row.thread_id,
                project_name=row.project_name,
                cwd=row.cwd,
                runner=runner,
                turn_lock=asyncio.Lock(),
            )
            restored += 1
            log.info(
                "session.restored",
                thread_id=row.thread_id,
                project=row.project_name,
                sdk_session_id=row.sdk_session_id,
            )
        return restored
