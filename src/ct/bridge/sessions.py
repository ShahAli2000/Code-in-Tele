"""In-memory session store for Phase 0.

Maps Telegram `message_thread_id` (the topic ID) to a SessionRunner. One topic
holds one Claude session; the lifetime of a session is the lifetime of the
process. Phase 1 will swap this for a SQLite-backed store with resume.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ct.sdk_adapter.adapter import SessionRunner


@dataclass
class TopicSession:
    """One Claude session bound to a Telegram forum topic."""

    thread_id: int           # Telegram message_thread_id
    project_name: str        # human-readable, set by /new
    cwd: str                 # working directory the SDK runs against
    runner: SessionRunner
    turn_lock: asyncio.Lock  # serialises turns within this topic


class SessionStore:
    """Thread-id -> TopicSession registry."""

    def __init__(self) -> None:
        self._by_thread: dict[int, TopicSession] = {}

    def add(self, session: TopicSession) -> None:
        if session.thread_id in self._by_thread:
            raise RuntimeError(f"thread_id {session.thread_id} already has a session")
        self._by_thread[session.thread_id] = session

    def get(self, thread_id: int) -> TopicSession | None:
        return self._by_thread.get(thread_id)

    def remove(self, thread_id: int) -> TopicSession | None:
        return self._by_thread.pop(thread_id, None)

    def all(self) -> list[TopicSession]:
        return list(self._by_thread.values())

    def __len__(self) -> int:
        return len(self._by_thread)
