"""Bridge-side runner client.

The bridge no longer hosts SessionRunner instances directly. Instead it owns
one `RunnerConnection` per registered runner daemon (Mac), opens sessions on
the chosen runner, and routes per-session events back to the right Telegram
topic via in-memory callbacks/queues.

Public surface used by the bridge:
    pool = RunnerPool(secret=...)
    await pool.add_runner(name="studio", host="127.0.0.1", port=8765)

    handle = await pool.open_session(
        runner_name="studio",
        sid=str(thread_id),
        cwd="/path",
        mode="acceptEdits",
        on_permission_request=async_handler,
        on_session_id_assigned=async_handler,
    )

    async for env in handle.turn("hello"):
        # env.type ∈ {text, tool_use, tool_result, thinking, system}
        ...
    await handle.set_permission_mode("default")
    await handle.resolve_permission(tool_use_id, allow=True)
    await handle.close()
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from dataclasses import dataclass

import structlog
import websockets
from websockets.asyncio.client import ClientConnection, connect

from ct.protocol.auth import frame, unframe
from ct.protocol.envelopes import (
    Envelope,
    ProtocolError,
    T_CLOSE,
    T_CLOSED,
    T_DECIDE,
    T_DIR_LISTING,
    T_ERROR,
    T_EXPORT,
    T_EXPORT_OK,
    T_FILE,
    T_FORK,
    T_FORK_OK,
    T_GET_FILE,
    T_GET_LOGS,
    T_INTERRUPT,
    T_LIST_DIR,
    T_LOGS,
    T_MKDIR,
    T_MKDIR_OK,
    T_SEARCH,
    T_SEARCH_RESULTS,
    T_UPLOAD,
    T_UPLOAD_OK,
    T_OPEN,
    T_OPENED,
    T_PERMISSION_REQUEST,
    T_SDK_ID,
    T_SEND,
    T_SET_MODE,
    T_SYSTEM,
    T_TEXT,
    T_THINKING,
    T_TOOL_RESULT,
    T_TOOL_USE,
    T_TURN_END,
    decide_payload,
    export_payload,
    fork_payload,
    get_file_payload,
    get_logs_payload,
    list_dir_payload,
    mkdir_payload,
    search_payload,
    upload_payload,
    open_payload,
    send_payload,
    set_mode_payload,
)
from ct.sdk_adapter.adapter import PermissionMode, PermissionRequest

log = structlog.get_logger(__name__)

# Events the bridge cares about during a turn (everything except control-plane
# acks and permission requests, which are routed elsewhere).
_TURN_STREAM_TYPES = frozenset(
    {T_TEXT, T_TOOL_USE, T_TOOL_RESULT, T_THINKING, T_SYSTEM}
)

PermissionHandler = Callable[[PermissionRequest], Awaitable[None]]
SdkIdHandler = Callable[[str], Awaitable[None]]


@dataclass
class _SessionState:
    sid: str
    cwd: str
    on_permission_request: PermissionHandler | None
    on_session_id_assigned: SdkIdHandler | None
    sdk_session_id: str | None = None
    permission_mode: str = "acceptEdits"
    model: str | None = None
    effort: str | None = None
    opened: asyncio.Event = None  # type: ignore[assignment]
    closed: asyncio.Event = None  # type: ignore[assignment]
    turn_queue: asyncio.Queue | None = None  # only set during a turn
    open_error: str | None = None

    def __post_init__(self) -> None:
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()


class SessionHandle:
    """Bridge-facing handle to one remote session. Mirrors the slice of
    SessionRunner that the bridge actually uses."""

    def __init__(self, conn: "RunnerConnection", state: _SessionState) -> None:
        self._conn = conn
        self._state = state

    @property
    def sid(self) -> str:
        return self._state.sid

    @property
    def session_id(self) -> str | None:
        return self._state.sdk_session_id

    @property
    def permission_mode(self) -> str:
        return self._state.permission_mode

    async def turn(self, text: str) -> AsyncIterator[Envelope]:
        """Send `text`, yield each envelope event until the turn ends."""
        if self._state.closed.is_set():
            raise RuntimeError(f"session {self.sid} already closed")
        # One turn at a time per session — bridge enforces this with topic locks
        # but we also defend here so a stale turn_queue isn't reused.
        self._state.turn_queue = asyncio.Queue()
        try:
            await self._conn._send(
                Envelope(T_SEND, self.sid, send_payload(text))
            )
            while True:
                env = await self._state.turn_queue.get()
                if env is None:
                    return
                if env.type == T_TURN_END:
                    return
                if env.type == T_ERROR:
                    raise RuntimeError(
                        f"runner error: {env.payload.get('kind','?')}: "
                        f"{env.payload.get('message','?')}"
                    )
                yield env
        finally:
            self._state.turn_queue = None

    async def resolve_permission(
        self,
        tool_use_id: str,
        *,
        allow: bool,
        updated_input: dict | None = None,
        deny_message: str = "User denied this action",
        remember: bool = False,
    ) -> bool:
        await self._conn._send(
            Envelope(
                T_DECIDE,
                self.sid,
                decide_payload(
                    tool_use_id=tool_use_id,
                    allow=allow,
                    updated_input=updated_input,
                    deny_message=deny_message,
                    remember=remember,
                ),
            )
        )
        # The runner doesn't ack decide; treat send-success as resolution.
        return True

    async def set_permission_mode(self, mode: PermissionMode) -> None:
        await self._conn._send(
            Envelope(T_SET_MODE, self.sid, set_mode_payload(mode))
        )
        self._state.permission_mode = mode

    async def set_model(self, model: str) -> None:
        """Live model swap on the running ClaudeSDKClient. Effective on the
        next assistant turn."""
        await self._conn._send(
            Envelope("set_model", self.sid, {"model": model})
        )
        self._state.model = model

    @property
    def model(self) -> str | None:
        return self._state.model

    @property
    def effort(self) -> str | None:
        return self._state.effort

    async def upload_file(self, path: str, content: bytes) -> tuple[str, int]:
        """Convenience: ship bytes to the runner. Used by the bridge's media
        handlers so /new mac=laptop sessions can receive uploads from Telegram."""
        return await self._conn.upload_file(path, content)

    async def interrupt(self) -> None:
        await self._conn._send(Envelope(T_INTERRUPT, self.sid, {}))

    async def close(self) -> None:
        if self._state.closed.is_set():
            return
        await self._conn._send(Envelope(T_CLOSE, self.sid, {}))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._state.closed.wait(), timeout=5.0)
        self._conn._sessions.pop(self.sid, None)


class RunnerConnection:
    """One WebSocket connection to one runner daemon, with auto-reconnect.

    When the underlying WS dies (peer down, network blip, runner restart),
    the reader loop schedules a reconnect with exponential backoff and, on
    success, re-issues T_OPEN(resume=sdk_session_id) for every session it
    was tracking. In-flight turns get a synthetic error envelope so the
    bridge surfaces the disconnect to the user without hanging.
    """

    def __init__(
        self,
        *,
        name: str,
        host: str,
        port: int,
        secret: bytes | None,
        on_reconnect: Callable[[str, list[str]], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.secret = secret
        self.on_reconnect = on_reconnect
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._sessions: dict[str, _SessionState] = {}
        # Connection-level RPCs (list_dir, mkdir): request_id -> future for the reply.
        self._pending_calls: dict[str, asyncio.Future[Envelope]] = {}
        self._send_lock = asyncio.Lock()
        self._auto_reconnect = True

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        if self._ws is not None:
            return
        log.info("runner_client.connecting", name=self.name, url=self.url)
        self._ws = await connect(self.url)
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"runner-reader-{self.name}"
        )
        log.info("runner_client.connected", name=self.name)

    async def close(self) -> None:
        # Suppress auto-reconnect first so a final WS close doesn't trigger
        # one more reconnect spin.
        self._auto_reconnect = False
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        for state in list(self._sessions.values()):
            state.closed.set()
            if state.turn_queue is not None:
                state.turn_queue.put_nowait(None)
        self._sessions.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    env = unframe(raw, self.secret)
                except ProtocolError as exc:
                    log.warning("runner_client.protocol_error", error=str(exc))
                    continue
                await self._dispatch(env)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("runner_client.reader_failed", name=self.name)
        # Reader exited (ws closed). Decide whether to reconnect.
        await self._on_disconnect()

    async def _on_disconnect(self) -> None:
        log.info(
            "runner_client.disconnected",
            name=self.name,
            sessions=len(self._sessions),
            will_reconnect=self._auto_reconnect,
        )
        self._ws = None
        # Fail any in-flight turns so iterators exit cleanly.
        for state in self._sessions.values():
            if state.turn_queue is not None:
                err = Envelope(
                    T_ERROR,
                    state.sid,
                    {"kind": "runner_disconnected",
                     "message": f"runner {self.name!r} dropped — will reconnect"},
                )
                state.turn_queue.put_nowait(err)
        # Fail any in-flight RPCs (list_dir/mkdir) so callers don't hang.
        for fut in list(self._pending_calls.values()):
            if not fut.done():
                fut.set_exception(
                    RuntimeError(f"runner {self.name!r} disconnected mid-RPC")
                )
        self._pending_calls.clear()
        if self._auto_reconnect and self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(), name=f"runner-reconnect-{self.name}"
            )

    async def _reconnect_loop(self) -> None:
        """Try to re-establish the WS forever (until close() is called).
        Backoff: 1s, 2s, 5s, 15s, 60s cap."""
        delays = [1.0, 2.0, 5.0, 15.0]
        attempt = 0
        try:
            while self._auto_reconnect:
                attempt += 1
                delay = delays[min(attempt - 1, len(delays) - 1)] if attempt <= len(delays) else 60.0
                try:
                    new_ws = await connect(self.url)
                except (OSError, ConnectionRefusedError) as exc:
                    log.info(
                        "runner_client.reconnect_attempt_failed",
                        name=self.name,
                        attempt=attempt,
                        next_delay=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                # Success.
                self._ws = new_ws
                self._reader_task = asyncio.create_task(
                    self._reader_loop(), name=f"runner-reader-{self.name}"
                )
                log.info(
                    "runner_client.reconnected",
                    name=self.name,
                    after_attempts=attempt,
                    sessions=len(self._sessions),
                )
                reopened = await self._reopen_sessions()
                if self.on_reconnect is not None and reopened:
                    try:
                        await self.on_reconnect(self.name, reopened)
                    except Exception:
                        log.exception("runner_client.on_reconnect_callback_failed")
                return
        finally:
            self._reconnect_task = None

    async def _reopen_sessions(self) -> list[str]:
        """Re-issue T_OPEN(resume=) for each tracked session. Returns the
        list of sids that were successfully re-opened."""
        reopened: list[str] = []
        for sid, state in list(self._sessions.items()):
            if state.sdk_session_id is None:
                # Never had a first turn — no resumable id. Drop it; the bridge
                # will see the closed event and report.
                log.info("runner_client.dropping_unresumable", sid=sid)
                state.closed.set()
                if state.turn_queue is not None:
                    state.turn_queue.put_nowait(None)
                self._sessions.pop(sid, None)
                continue
            # Reset opened so a fresh wait() blocks until OPENED arrives.
            state.opened.clear()
            state.open_error = None
            payload = {
                "cwd": state.cwd,
                "mode": state.permission_mode,
                "resume": state.sdk_session_id,
            }
            if state.model is not None:
                payload["model"] = state.model
            if state.effort is not None:
                payload["effort"] = state.effort
            try:
                await self._send(Envelope("open", sid, payload))
            except Exception:
                log.exception("runner_client.reopen_send_failed", sid=sid)
                continue
            reopened.append(sid)
        return reopened

    async def _dispatch(self, env: Envelope) -> None:
        # Connection-level RPC replies arrive with env.id = request_id, not a
        # session id. Dispatch those first.
        if env.id in self._pending_calls:
            fut = self._pending_calls.pop(env.id)
            if not fut.done():
                fut.set_result(env)
            return

        state = self._sessions.get(env.id)
        if state is None:
            log.debug("runner_client.event_for_unknown_session", sid=env.id, type=env.type)
            return

        if env.type == T_OPENED:
            state.opened.set()
            return
        if env.type == T_CLOSED:
            state.closed.set()
            if state.turn_queue is not None:
                state.turn_queue.put_nowait(None)
            return
        if env.type == T_SDK_ID:
            sid_value = env.payload.get("sdk_session_id")
            if isinstance(sid_value, str) and sid_value:
                state.sdk_session_id = sid_value
                if state.on_session_id_assigned is not None:
                    try:
                        await state.on_session_id_assigned(sid_value)
                    except Exception:
                        log.exception(
                            "runner_client.id_handler_failed", sid=env.id
                        )
            return
        if env.type == T_PERMISSION_REQUEST:
            if state.on_permission_request is not None:
                p = env.payload
                req = PermissionRequest(
                    tool_use_id=str(p.get("tool_use_id", "")),
                    tool_name=str(p.get("name", "")),
                    input_data=dict(p.get("input", {}) or {}),
                    agent_id=p.get("agent_id"),
                )
                try:
                    await state.on_permission_request(req)
                except Exception:
                    log.exception(
                        "runner_client.perm_handler_failed", sid=env.id
                    )
            return
        if env.type == T_ERROR:
            # Errors during open: surface via opened.set + open_error so
            # open_session() can re-raise. Errors during a turn: enqueue.
            if not state.opened.is_set():
                state.open_error = (
                    f"{env.payload.get('kind','?')}: "
                    f"{env.payload.get('message','?')}"
                )
                state.opened.set()
                return
            if state.turn_queue is not None:
                state.turn_queue.put_nowait(env)
            return
        if env.type == T_TURN_END or env.type in _TURN_STREAM_TYPES:
            if state.turn_queue is not None:
                state.turn_queue.put_nowait(env)
            return

    async def _send(self, env: Envelope) -> None:
        if self._ws is None:
            raise RuntimeError(f"runner {self.name!r} is not connected")
        line = frame(env, self.secret)
        async with self._send_lock:
            await self._ws.send(line)

    # ---- connection-level RPCs (no session) --------------------------------

    async def _call_rpc(
        self, env_type: str, payload: dict, *, timeout: float = 10.0
    ) -> Envelope:
        """Send an RPC envelope, await the reply correlated by id. Reply may be
        the success type (e.g. T_DIR_LISTING) or T_ERROR."""
        request_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Envelope] = loop.create_future()
        self._pending_calls[request_id] = fut
        try:
            await self._send(Envelope(env_type, request_id, payload))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_calls.pop(request_id, None)
            raise
        finally:
            self._pending_calls.pop(request_id, None)

    async def list_dir(
        self, path: str, *, show_hidden: bool = False
    ) -> tuple[str, list[tuple[str, bool]]]:
        """List subdirs/files under `path` on the runner. Returns (resolved_path,
        items) where items are (name, is_dir). Hidden + venv/etc. filtered out
        unless show_hidden=True."""
        env = await self._call_rpc(
            T_LIST_DIR, list_dir_payload(path, show_hidden)
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        if env.type != T_DIR_LISTING:
            raise RuntimeError(f"unexpected reply type: {env.type}")
        items = [
            (item["name"], bool(item.get("is_dir", False)))
            for item in env.payload.get("items", [])
            if isinstance(item, dict) and "name" in item
        ]
        return env.payload.get("path", path), items

    async def mkdir(self, path: str) -> str:
        """Create a directory (mkdir -p) on the runner. Returns the resolved
        absolute path. Raises if it already exists or is denied."""
        env = await self._call_rpc(T_MKDIR, mkdir_payload(path))
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        return env.payload.get("path", path)

    async def upload_file(self, path: str, content: bytes) -> tuple[str, int]:
        """Write `content` to `path` on the runner. Parent dirs are created if
        missing. Returns (resolved_path, size). Raises on error.

        Uploads are base64-encoded inside the JSON envelope. For larger files
        this could be replaced with a binary WS frame; current implementation
        is fine for ≤ 20 MB (Telegram bot file limit)."""
        b64 = base64.b64encode(content).decode("ascii")
        env = await self._call_rpc(
            T_UPLOAD, upload_payload(path, b64),
            timeout=60.0,  # upload can take longer for large files
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        return env.payload.get("path", path), int(env.payload.get("size", len(content)))

    async def get_logs(
        self, *, sdk_session_id: str, cwd: str, limit: int = 20
    ) -> list[tuple[str, str]]:
        """Fetch the last `limit` transcript entries (role, text) for the
        given SDK session. Read off-disk by the runner via SDK
        get_session_messages, with slash-command noise filtered out."""
        env = await self._call_rpc(
            T_GET_LOGS,
            get_logs_payload(sdk_session_id=sdk_session_id, cwd=cwd, limit=limit),
            timeout=15.0,
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        out: list[tuple[str, str]] = []
        for entry in env.payload.get("entries", []):
            if not isinstance(entry, dict):
                continue
            role = entry.get("role", "?")
            text = entry.get("text", "")
            if isinstance(role, str) and isinstance(text, str):
                out.append((role, text))
        return out

    async def search(
        self, *, pattern: str, max_results: int = 30
    ) -> list[dict[str, Any]]:
        """Ask the runner to substring-match `pattern` across every transcript
        on disk. Returns raw match dicts ({sdk_session_id, role, snippet,
        project_dir}); the bridge decides how to render them."""
        env = await self._call_rpc(
            T_SEARCH,
            search_payload(pattern=pattern, max_results=max_results),
            timeout=15.0,
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        matches = env.payload.get("matches") or []
        return [m for m in matches if isinstance(m, dict)]

    async def get_file(self, path: str) -> tuple[str, bytes]:
        """Read `path` on the runner. Returns (resolved_path, bytes). Raises
        on permission/IO errors or if size exceeds the runner-side cap."""
        env = await self._call_rpc(
            T_GET_FILE, get_file_payload(path),
            timeout=60.0,  # large files take a while to base64-encode + ship
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        b64 = env.payload.get("content_b64")
        if not isinstance(b64, str):
            raise RuntimeError("file reply missing content_b64")
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise RuntimeError(f"bad b64 in file reply: {exc!s}") from exc
        return env.payload.get("path", path), data

    async def export_transcript(
        self, *, sdk_session_id: str, cwd: str
    ) -> str:
        """Fetch the full session transcript pre-rendered as markdown by the
        runner. Used by /export to produce a `.md` document attachment."""
        env = await self._call_rpc(
            T_EXPORT,
            export_payload(sdk_session_id=sdk_session_id, cwd=cwd),
            timeout=30.0,
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        markdown = env.payload.get("markdown", "")
        if not isinstance(markdown, str):
            raise RuntimeError("export reply missing markdown")
        return markdown

    async def fork_session(
        self, *, sdk_session_id: str, cwd: str, title: str | None = None
    ) -> str:
        """Fork the on-disk transcript file for `sdk_session_id`. Returns the
        new sdk_session_id. The runner runs fork_session() (synchronous file
        I/O) on a thread, then replies with the new id. Caller is expected to
        then open_session(resume=<new_id>) to bring the fork to life."""
        env = await self._call_rpc(
            T_FORK,
            fork_payload(sdk_session_id=sdk_session_id, cwd=cwd, title=title),
            timeout=30.0,
        )
        if env.type == T_ERROR:
            raise RuntimeError(
                f"{env.payload.get('kind','?')}: {env.payload.get('message','?')}"
            )
        new_id = env.payload.get("sdk_session_id")
        if not isinstance(new_id, str) or not new_id:
            raise RuntimeError("fork reply missing sdk_session_id")
        return new_id

    async def open_session(
        self,
        *,
        sid: str,
        cwd: str,
        mode: PermissionMode,
        resume: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        thinking: bool = True,
        system_prompt: str | None = None,
        auto_allow_tools: list[str] | None = None,
        on_permission_request: PermissionHandler | None = None,
        on_session_id_assigned: SdkIdHandler | None = None,
    ) -> SessionHandle:
        if sid in self._sessions:
            raise RuntimeError(f"session {sid} already open on runner {self.name!r}")
        state = _SessionState(
            sid=sid,
            cwd=cwd,
            on_permission_request=on_permission_request,
            on_session_id_assigned=on_session_id_assigned,
            permission_mode=mode,
            sdk_session_id=resume,
            model=model,
            effort=effort,
        )
        self._sessions[sid] = state
        await self._send(
            Envelope(
                T_OPEN, sid,
                open_payload(
                    cwd=cwd, mode=mode, resume=resume, model=model, effort=effort,
                    thinking=thinking,
                    system_prompt=system_prompt,
                    auto_allow_tools=auto_allow_tools,
                ),
            )
        )
        try:
            await asyncio.wait_for(state.opened.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            self._sessions.pop(sid, None)
            raise RuntimeError(f"runner {self.name!r} didn't ack open within 30s")
        if state.open_error:
            self._sessions.pop(sid, None)
            raise RuntimeError(f"runner {self.name!r} rejected open: {state.open_error}")
        return SessionHandle(self, state)


class RunnerPool:
    """Many named runners; the bridge picks one when starting a session."""

    def __init__(
        self,
        secret: bytes | None,
        on_reconnect: Callable[[str, list[str]], Awaitable[None]] | None = None,
    ) -> None:
        self.secret = secret
        self.on_reconnect = on_reconnect
        self._connections: dict[str, RunnerConnection] = {}

    async def add_runner(
        self,
        *,
        name: str,
        host: str,
        port: int,
        max_attempts: int = 15,
        retry_interval: float = 2.0,
    ) -> None:
        """Connect to a runner daemon, retrying on transient errors.

        At launchd boot time both the bridge and the runner come up roughly
        simultaneously; the bridge frequently wins the race and tries to
        connect before the runner is listening. Retrying for ~30s covers that.
        """
        if name in self._connections:
            raise RuntimeError(f"runner {name!r} already registered")
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            conn = RunnerConnection(
                name=name,
                host=host,
                port=port,
                secret=self.secret,
                on_reconnect=self.on_reconnect,
            )
            try:
                await conn.connect()
            except (OSError, ConnectionRefusedError) as exc:
                last_exc = exc
                log.info(
                    "runner_client.connect_retry",
                    name=name,
                    attempt=attempt,
                    max=max_attempts,
                    error=str(exc),
                )
                await asyncio.sleep(retry_interval)
                continue
            self._connections[name] = conn
            return
        raise RuntimeError(
            f"couldn't reach runner {name!r} at {host}:{port} after "
            f"{max_attempts} attempts: {last_exc!r}"
        )

    def get(self, name: str) -> RunnerConnection:
        if name not in self._connections:
            raise KeyError(f"no runner registered as {name!r}")
        return self._connections[name]

    def names(self) -> list[str]:
        return list(self._connections)

    async def close_all(self) -> None:
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()
