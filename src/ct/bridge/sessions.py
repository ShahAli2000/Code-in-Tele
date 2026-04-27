"""Session store: in-memory runner registry, write-through to SQLite.

The in-memory map (thread_id -> TopicSession) holds live SessionHandle
instances (proxies for runners on a runner daemon, possibly remote). Every
mutation also lands in `Db` so the bot can rehydrate after a restart.
`restore()` is the inverse — at boot it reads active rows from the DB and
uses `runner_factory` to reopen each session with `resume=sdk_session_id`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from ct.bridge.runner_client import SessionHandle
from ct.sdk_adapter.adapter import PermissionMode
from ct.store.db import Db

log = structlog.get_logger(__name__)


@dataclass
class TopicSession:
    """One live Claude session bound to a Telegram forum topic."""

    thread_id: int
    project_name: str
    cwd: str
    runner: SessionHandle
    turn_lock: asyncio.Lock
    runner_name: str = "studio"


@dataclass
class RestoreSpec:
    thread_id: int
    project_name: str
    cwd: str
    sdk_session_id: str | None
    permission_mode: PermissionMode
    runner_name: str = "studio"


# Bridge supplies this when calling restore(). Given the persisted metadata
# for a session, return a fully-opened SessionHandle (turn_lock and DB row
# are managed by SessionStore.restore itself).
RunnerFactory = Callable[
    ["RestoreSpec"], Awaitable[SessionHandle]
]


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

    async def restore(
        self,
        factory: RunnerFactory,
        *,
        default_runner_name: str = "studio",
    ) -> int:
        """For each active session in the DB, ask `factory` to reopen it on
        the appropriate runner with `resume=sdk_session_id`. Returns the count
        successfully restored. Sessions whose `sdk_session_id` is null (never
        had a first turn) or whose resume fails are marked orphaned + skipped.
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
                runner_name=default_runner_name,
            )
            try:
                handle = await factory(spec)
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
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=default_runner_name,
            )
            restored += 1
            log.info(
                "session.restored",
                thread_id=row.thread_id,
                project=row.project_name,
                sdk_session_id=row.sdk_session_id,
            )
        return restored
