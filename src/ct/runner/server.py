"""Runner daemon — hosts SessionRunner instances and exposes them over a
WebSocket wire protocol so the bridge can drive them from another process
(or another Mac, in Phase 3).

Architecture:
    websockets.serve()
        ↓ one task per accepted connection
    RunnerConnection
        ├── reader: pulls envelopes off the WS, dispatches to handlers
        ├── sessions: dict[str, RunnerSession]   # bridge-side id → SessionRunner
        └── writer is just `await ws.send(frame(env, secret))` — no separate task

Each RunnerSession holds one SessionRunner and the callbacks the bridge
configured at open-time. Permission requests come up out of the SDK adapter,
get translated into a `permission_request` envelope, and resolve when the
bridge ships back a `decide` envelope.

Session lifecycle (Phase 2):
    open      → SessionRunner created + connected → opened
    send X    → runner.turn(X), each SDK message translated to an envelope
                stream; ResultMessage closes the turn
    decide    → resolve_permission on the right session
    set_mode  → set_permission_mode
    interrupt → runner.interrupt()
    close     → runner.stop(), session removed
    (ws drop) → all sessions torn down (resume on reconnect is Phase 4 work)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import json
import logging
import os
import pathlib
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
import websockets
from websockets.asyncio.server import ServerConnection, serve

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    fork_session,
    get_session_messages,
)

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
    T_FORK,
    T_FORK_OK,
    T_GET_LOGS,
    T_INTERRUPT,
    T_LIST_DIR,
    T_LOGS,
    T_MKDIR,
    T_MKDIR_OK,
    T_UPLOAD,
    T_UPLOAD_OK,
    T_OPEN,
    T_OPENED,
    T_PERMISSION_REQUEST,
    T_PING,
    T_PONG,
    T_SDK_ID,
    T_SEND,
    T_SET_MODE,
    T_SYSTEM,
    T_TEXT,
    T_THINKING,
    T_TOOL_RESULT,
    T_TOOL_USE,
    T_TURN_END,
    closed_payload,
    dir_listing_payload,
    error_payload,
    export_ok_payload,
    fork_ok_payload,
    logs_payload,
    mkdir_ok_payload,
    upload_ok_payload,
    permission_request_payload,
    sdk_id_payload,
    system_payload,
    text_payload,
    tool_result_payload,
    tool_use_payload,
    turn_end_payload,
)
from ct.sdk_adapter.adapter import (
    PermissionMode,
    PermissionRequest,
    SessionRunner,
)

log = structlog.get_logger(__name__)


# ---- SDK -> envelope translation ----------------------------------------


def _translate_assistant(msg: AssistantMessage, sid: str) -> list[Envelope]:
    out: list[Envelope] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            if block.text and block.text.strip():
                out.append(Envelope(T_TEXT, sid, text_payload(block.text)))
        elif isinstance(block, ToolUseBlock):
            out.append(
                Envelope(
                    T_TOOL_USE,
                    sid,
                    tool_use_payload(
                        tool_use_id=block.id, name=block.name, input=block.input
                    ),
                )
            )
        elif isinstance(block, ThinkingBlock):
            out.append(Envelope(T_THINKING, sid, {}))
        elif isinstance(block, ToolResultBlock):
            # Tool results in an AssistantMessage are server-side tool returns.
            out.append(_translate_tool_result_block(block, sid))
    return out


def _translate_user(msg: UserMessage, sid: str) -> list[Envelope]:
    out: list[Envelope] = []
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            out.append(_translate_tool_result_block(block, sid))
    return out


def _translate_tool_result_block(block: ToolResultBlock, sid: str) -> Envelope:
    body = block.content if isinstance(block.content, str) else repr(block.content)
    return Envelope(
        T_TOOL_RESULT,
        sid,
        tool_result_payload(
            tool_use_id=getattr(block, "tool_use_id", "") or "",
            content=body,
            is_error=bool(getattr(block, "is_error", False)),
        ),
    )


def _translate_system(msg: SystemMessage, sid: str) -> Envelope:
    return Envelope(
        T_SYSTEM,
        sid,
        system_payload(subtype=msg.subtype or "", data=dict(msg.data or {})),
    )


# /logs uses SDK's get_session_messages, which returns SessionMessage objects
# (different shape from streaming messages — these are dicts loaded from JSONL).
# We compress each one into a short string for Telegram display.
_LOG_TEXT_CAP = 600  # per-entry char cap; tool output and long answers truncate


def _render_session_message_markdown(m: Any) -> str | None:
    """Verbose markdown rendering for /export. Unlike the /logs summary,
    this preserves full text and tool inputs/outputs so the export is a
    complete record of the conversation."""
    msg = getattr(m, "message", None)
    if not isinstance(msg, dict):
        return None
    role = m.type
    role_label = "## 👤 user" if role == "user" else "## 🤖 assistant"
    content = msg.get("content")
    blocks: list[str] = []
    if isinstance(content, str):
        if content.startswith("<command-name>") or content.startswith("<local-command-"):
            return None
        blocks.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    blocks.append(text)
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                blocks.append(
                    f"**🔧 tool: `{name}`**\n\n```json\n"
                    f"{json.dumps(inp, indent=2, ensure_ascii=False, default=str)}\n```"
                )
            elif btype == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = "".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in body
                    )
                body = str(body)
                if body.strip():
                    is_error = bool(block.get("is_error"))
                    label = "tool result (error)" if is_error else "tool result"
                    blocks.append(f"_{label}:_\n\n```\n{body}\n```")
            elif btype == "thinking":
                # Skip — usually internal scratchpad; users export for the conversation.
                continue
    body = "\n\n".join(b for b in blocks if b).strip()
    if not body:
        return None
    return f"{role_label}\n\n{body}"


def _summarise_session_message(m: Any) -> str | None:
    """Compress a SessionMessage to a one-string summary for /logs. Returns
    None for entries that should be hidden (slash-command system noise)."""
    msg = getattr(m, "message", None)
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                summary = ""
                if isinstance(inp, dict):
                    if "file_path" in inp:
                        summary = f" {inp['file_path']}"
                    elif "command" in inp:
                        summary = f" {str(inp['command'])[:80]}"
                    elif "pattern" in inp:
                        summary = f" {inp['pattern']}"
                parts.append(f"[🔧 {name}{summary}]")
            elif btype == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in body
                    )
                body = str(body).strip()
                if body:
                    parts.append(f"[result: {body[:200]}]")
            elif btype == "thinking":
                parts.append("[thinking…]")
    text = " ".join(p for p in parts if p).strip()
    if not text:
        return None
    # Hide slash-command bookkeeping entries — these are noise in /logs
    if text.startswith("<command-name>") or text.startswith("<local-command-"):
        return None
    if len(text) > _LOG_TEXT_CAP:
        text = text[:_LOG_TEXT_CAP] + "…"
    return text


def translate_sdk_message(msg: Any, sid: str) -> list[Envelope]:
    """Map one SDK message object to zero or more wire envelopes."""
    if isinstance(msg, AssistantMessage):
        return _translate_assistant(msg, sid)
    if isinstance(msg, UserMessage):
        return _translate_user(msg, sid)
    if isinstance(msg, SystemMessage):
        return [_translate_system(msg, sid)]
    # ResultMessage / RateLimitEvent / others handled by the caller (turn end
    # boundary on ResultMessage; rate-limit events suppressed for now).
    return []


# ---- per-connection state -----------------------------------------------


@dataclass
class RunnerSession:
    sid: str
    runner: SessionRunner | None = None
    turn_task: asyncio.Task[None] | None = None
    closed: bool = False


class RunnerConnection:
    """One bridge ↔ runner WebSocket. Owns N session runners multiplexed by sid."""

    def __init__(self, ws: ServerConnection, secret: bytes | None) -> None:
        self.ws = ws
        self.secret = secret
        self.sessions: dict[str, RunnerSession] = {}
        self._send_lock = asyncio.Lock()
        self._next_seq = 0

    async def serve(self) -> None:
        peer = getattr(self.ws, "remote_address", "?")
        log.info("runner.connection_opened", peer=str(peer))
        try:
            async for raw in self.ws:
                if not isinstance(raw, str):
                    continue
                try:
                    env = unframe(raw, self.secret)
                except ProtocolError as exc:
                    await self._send_error("", "protocol", str(exc))
                    continue
                # Dispatch is fire-and-forget so a long-running send doesn't
                # block decide / interrupt for the same or a different session.
                asyncio.create_task(self._dispatch(env))
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("runner.connection_failed", peer=str(peer))
        finally:
            await self._teardown()
            log.info("runner.connection_closed", peer=str(peer))

    async def _teardown(self) -> None:
        for s in list(self.sessions.values()):
            s.closed = True
            if s.turn_task is not None and not s.turn_task.done():
                s.turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await s.turn_task
            if s.runner is not None:
                with contextlib.suppress(Exception):
                    await s.runner.stop()
        self.sessions.clear()

    async def _send(self, env: Envelope) -> None:
        self._next_seq += 1
        env.seq = self._next_seq
        line = frame(env, self.secret)
        async with self._send_lock:
            await self.ws.send(line)

    async def _send_error(self, sid: str, kind: str, message: str) -> None:
        await self._send(Envelope(T_ERROR, sid, error_payload(kind=kind, message=message)))

    # ---- dispatch -------------------------------------------------------

    async def _dispatch(self, env: Envelope) -> None:
        try:
            if env.type == T_OPEN:
                await self._handle_open(env)
            elif env.type == T_SEND:
                await self._handle_send(env)
            elif env.type == T_DECIDE:
                await self._handle_decide(env)
            elif env.type == T_SET_MODE:
                await self._handle_set_mode(env)
            elif env.type == "set_model":
                await self._handle_set_model(env)
            elif env.type == T_INTERRUPT:
                await self._handle_interrupt(env)
            elif env.type == T_CLOSE:
                await self._handle_close(env)
            elif env.type == T_PING:
                await self._send(Envelope(T_PONG, env.id, {}))
            elif env.type == T_LIST_DIR:
                await self._handle_list_dir(env)
            elif env.type == T_MKDIR:
                await self._handle_mkdir(env)
            elif env.type == T_UPLOAD:
                await self._handle_upload(env)
            elif env.type == T_FORK:
                await self._handle_fork(env)
            elif env.type == T_GET_LOGS:
                await self._handle_get_logs(env)
            elif env.type == T_EXPORT:
                await self._handle_export(env)
            else:
                await self._send_error(env.id, "unsupported_type", env.type)
        except Exception as exc:
            log.exception("runner.dispatch_failed", env_type=env.type, sid=env.id)
            await self._send_error(env.id, "dispatch_failed", repr(exc))

    async def _handle_open(self, env: Envelope) -> None:
        if env.id in self.sessions:
            await self._send_error(env.id, "duplicate", "session already open")
            return
        cwd = env.payload.get("cwd")
        mode = env.payload.get("mode", "acceptEdits")
        resume = env.payload.get("resume")
        model = env.payload.get("model")
        effort = env.payload.get("effort")
        if not isinstance(cwd, str) or not cwd:
            await self._send_error(env.id, "bad_request", "cwd is required")
            return

        session = RunnerSession(sid=env.id)
        self.sessions[env.id] = session

        async def perm_handler(req: PermissionRequest) -> None:
            await self._send(
                Envelope(
                    T_PERMISSION_REQUEST,
                    env.id,
                    permission_request_payload(
                        tool_use_id=req.tool_use_id,
                        name=req.tool_name,
                        input=req.input_data,
                        agent_id=req.agent_id,
                    ),
                )
            )

        async def id_persister(sdk_session_id: str) -> None:
            await self._send(Envelope(T_SDK_ID, env.id, sdk_id_payload(sdk_session_id)))

        runner = SessionRunner(
            cwd=cwd,
            permission_mode=mode,  # type: ignore[arg-type]
            resume=resume,
            model=model if isinstance(model, str) else None,
            effort=effort if isinstance(effort, str) else None,
            on_permission_request=perm_handler,
            on_session_id_assigned=id_persister,
        )
        session.runner = runner
        try:
            await runner.start()
        except Exception as exc:
            self.sessions.pop(env.id, None)
            await self._send_error(env.id, "open_failed", repr(exc))
            return
        # If we resumed, surface that id immediately; otherwise the SDK will
        # emit it on the first turn.
        if resume:
            await self._send(Envelope(T_SDK_ID, env.id, sdk_id_payload(resume)))
        await self._send(Envelope(T_OPENED, env.id, {}))

    async def _handle_send(self, env: Envelope) -> None:
        session = self.sessions.get(env.id)
        if session is None or session.runner is None:
            await self._send_error(env.id, "no_session", "open the session first")
            return
        text = env.payload.get("text")
        if not isinstance(text, str):
            await self._send_error(env.id, "bad_request", "send.text must be a string")
            return
        if session.turn_task is not None and not session.turn_task.done():
            await self._send_error(env.id, "busy", "turn already in flight")
            return
        session.turn_task = asyncio.create_task(self._drive_turn(session, text))

    async def _drive_turn(self, session: RunnerSession, text: str) -> None:
        assert session.runner is not None
        try:
            async for msg in session.runner.turn(text):
                if isinstance(msg, ResultMessage):
                    break
                for out in translate_sdk_message(msg, session.sid):
                    await self._send(out)
            await self._send(
                Envelope(T_TURN_END, session.sid, turn_end_payload(reason="success"))
            )
        except asyncio.CancelledError:
            await self._send(
                Envelope(T_TURN_END, session.sid, turn_end_payload(reason="cancelled"))
            )
            raise
        except Exception as exc:
            log.exception("runner.turn_failed", sid=session.sid)
            await self._send_error(session.sid, "turn_failed", repr(exc))
            await self._send(
                Envelope(T_TURN_END, session.sid, turn_end_payload(reason="error"))
            )

    async def _handle_decide(self, env: Envelope) -> None:
        session = self.sessions.get(env.id)
        if session is None or session.runner is None:
            await self._send_error(env.id, "no_session", "open the session first")
            return
        p = env.payload
        tool_use_id = p.get("tool_use_id")
        allow = bool(p.get("allow", False))
        remember = bool(p.get("remember", False))
        if not isinstance(tool_use_id, str) or not tool_use_id:
            await self._send_error(env.id, "bad_request", "decide.tool_use_id required")
            return
        await session.runner.resolve_permission(
            tool_use_id,
            allow=allow,
            updated_input=p.get("updated_input"),
            deny_message=p.get("deny_message", "User denied this action"),
            remember=remember,
        )

    async def _handle_set_mode(self, env: Envelope) -> None:
        session = self.sessions.get(env.id)
        if session is None or session.runner is None:
            await self._send_error(env.id, "no_session", "open the session first")
            return
        mode = env.payload.get("mode")
        if not isinstance(mode, str):
            await self._send_error(env.id, "bad_request", "set_mode.mode required")
            return
        try:
            await session.runner.set_permission_mode(mode)  # type: ignore[arg-type]
        except Exception as exc:
            await self._send_error(env.id, "set_mode_failed", repr(exc))

    async def _handle_set_model(self, env: Envelope) -> None:
        session = self.sessions.get(env.id)
        if session is None or session.runner is None:
            await self._send_error(env.id, "no_session", "open the session first")
            return
        model = env.payload.get("model")
        if not isinstance(model, str):
            await self._send_error(env.id, "bad_request", "set_model.model required")
            return
        try:
            await session.runner.set_model(model)
        except Exception as exc:
            await self._send_error(env.id, "set_model_failed", repr(exc))

    async def _handle_interrupt(self, env: Envelope) -> None:
        session = self.sessions.get(env.id)
        if session is None or session.runner is None:
            return
        await session.runner.interrupt()

    # ---- connection-level RPCs (no session) ---------------------------------

    # Subdirs that are noise 99% of the time. The bridge's "show hidden" toggle
    # bypasses this filter (it still hides dotfiles in that mode? actually,
    # show_hidden = show *everything*; we make it the user's call).
    _DEFAULT_SKIP_NAMES = frozenset({
        ".venv", "venv", "node_modules", "__pycache__", ".git",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
        ".idea", ".vscode", ".DS_Store",
    })

    async def _handle_list_dir(self, env: Envelope) -> None:
        path_raw = env.payload.get("path")
        show_hidden = bool(env.payload.get("show_hidden", False))
        if not isinstance(path_raw, str) or not path_raw:
            await self._send_error(env.id, "bad_request", "list_dir.path required")
            return
        path = pathlib.Path(path_raw).expanduser()
        try:
            if not path.is_dir():
                await self._send_error(env.id, "not_a_dir", str(path))
                return
            items: list[dict[str, Any]] = []
            for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                name = entry.name
                if not show_hidden:
                    if name.startswith("."):
                        continue
                    if name in self._DEFAULT_SKIP_NAMES:
                        continue
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                items.append({"name": name, "is_dir": is_dir})
        except PermissionError as exc:
            await self._send_error(env.id, "permission_denied", str(exc))
            return
        except OSError as exc:
            await self._send_error(env.id, "io_error", str(exc))
            return
        await self._send(
            Envelope(T_DIR_LISTING, env.id, dir_listing_payload(str(path), items))
        )

    async def _handle_mkdir(self, env: Envelope) -> None:
        path_raw = env.payload.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            await self._send_error(env.id, "bad_request", "mkdir.path required")
            return
        path = pathlib.Path(path_raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            await self._send_error(env.id, "exists", str(path))
            return
        except PermissionError as exc:
            await self._send_error(env.id, "permission_denied", str(exc))
            return
        except OSError as exc:
            await self._send_error(env.id, "io_error", str(exc))
            return
        await self._send(Envelope(T_MKDIR_OK, env.id, mkdir_ok_payload(str(path))))

    async def _handle_upload(self, env: Envelope) -> None:
        path_raw = env.payload.get("path")
        content_b64 = env.payload.get("content_b64")
        if not isinstance(path_raw, str) or not path_raw:
            await self._send_error(env.id, "bad_request", "upload.path required")
            return
        if not isinstance(content_b64, str):
            await self._send_error(env.id, "bad_request", "upload.content_b64 required")
            return
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            await self._send_error(env.id, "bad_b64", str(exc))
            return
        path = pathlib.Path(path_raw).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except PermissionError as exc:
            await self._send_error(env.id, "permission_denied", str(exc))
            return
        except OSError as exc:
            await self._send_error(env.id, "io_error", str(exc))
            return
        await self._send(
            Envelope(T_UPLOAD_OK, env.id, upload_ok_payload(str(path), len(data)))
        )

    async def _handle_fork(self, env: Envelope) -> None:
        """Fork the on-disk transcript file via the SDK's fork_session().
        Synchronous file I/O — run on a thread so we don't block the event loop."""
        sdk_session_id = env.payload.get("sdk_session_id")
        cwd = env.payload.get("cwd")
        title = env.payload.get("title")
        if not isinstance(sdk_session_id, str) or not sdk_session_id:
            await self._send_error(env.id, "bad_request", "fork.sdk_session_id required")
            return
        if not isinstance(cwd, str) or not cwd:
            await self._send_error(env.id, "bad_request", "fork.cwd required")
            return
        try:
            result = await asyncio.to_thread(
                fork_session,
                sdk_session_id,
                directory=cwd,
                title=title if isinstance(title, str) else None,
            )
        except FileNotFoundError as exc:
            await self._send_error(env.id, "not_found", str(exc))
            return
        except ValueError as exc:
            await self._send_error(env.id, "bad_request", str(exc))
            return
        except Exception as exc:
            log.exception("runner.fork_failed", sdk_session_id=sdk_session_id, cwd=cwd)
            await self._send_error(env.id, "fork_failed", repr(exc))
            return
        await self._send(
            Envelope(T_FORK_OK, env.id, fork_ok_payload(result.session_id))
        )

    async def _handle_get_logs(self, env: Envelope) -> None:
        """Read recent transcript entries via SDK get_session_messages, then
        compress each into a (role, text) summary so the bridge can render
        them in a single Telegram message."""
        sdk_session_id = env.payload.get("sdk_session_id")
        cwd = env.payload.get("cwd")
        limit = env.payload.get("limit", 20)
        if not isinstance(sdk_session_id, str) or not sdk_session_id:
            await self._send_error(env.id, "bad_request", "get_logs.sdk_session_id required")
            return
        if not isinstance(cwd, str) or not cwd:
            await self._send_error(env.id, "bad_request", "get_logs.cwd required")
            return
        if not isinstance(limit, int) or limit <= 0:
            limit = 20
        try:
            msgs = await asyncio.to_thread(
                get_session_messages, sdk_session_id, directory=cwd, limit=limit
            )
        except Exception as exc:
            log.exception("runner.get_logs_failed", sdk_session_id=sdk_session_id)
            await self._send_error(env.id, "logs_failed", repr(exc))
            return
        entries: list[dict[str, Any]] = []
        for m in msgs:
            text = _summarise_session_message(m)
            if text is None:
                continue
            entries.append({"role": m.type, "text": text})
        await self._send(Envelope(T_LOGS, env.id, logs_payload(entries)))

    async def _handle_export(self, env: Envelope) -> None:
        """Read the entire transcript and render as a single markdown blob.
        The bridge attaches it as a `.md` file; full rendering happens here so
        the wire format stays simple."""
        sdk_session_id = env.payload.get("sdk_session_id")
        cwd = env.payload.get("cwd")
        if not isinstance(sdk_session_id, str) or not sdk_session_id:
            await self._send_error(env.id, "bad_request", "export.sdk_session_id required")
            return
        if not isinstance(cwd, str) or not cwd:
            await self._send_error(env.id, "bad_request", "export.cwd required")
            return
        try:
            msgs = await asyncio.to_thread(
                get_session_messages, sdk_session_id, directory=cwd, limit=None
            )
        except Exception as exc:
            log.exception("runner.export_failed", sdk_session_id=sdk_session_id)
            await self._send_error(env.id, "export_failed", repr(exc))
            return
        # Header makes the export self-describing.
        header_lines = [
            f"# Claude session transcript",
            "",
            f"- session_id: `{sdk_session_id}`",
            f"- cwd: `{cwd}`",
            f"- exported: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
            f"- messages: {len(msgs)}",
            "",
            "---",
            "",
        ]
        body_parts: list[str] = []
        for m in msgs:
            rendered = _render_session_message_markdown(m)
            if rendered:
                body_parts.append(rendered)
        markdown = "\n".join(header_lines) + "\n\n---\n\n".join(body_parts) + "\n"
        await self._send(Envelope(T_EXPORT_OK, env.id, export_ok_payload(markdown)))

    async def _handle_close(self, env: Envelope) -> None:
        session = self.sessions.pop(env.id, None)
        if session is None:
            return
        session.closed = True
        if session.turn_task is not None and not session.turn_task.done():
            session.turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.turn_task
        if session.runner is not None:
            with contextlib.suppress(Exception):
                await session.runner.stop()
        await self._send(Envelope(T_CLOSED, env.id, closed_payload(reason="requested")))


# ---- entry point --------------------------------------------------------


async def run(host: str, port: int, secret: bytes | None) -> int:
    async def handler(ws: ServerConnection) -> None:
        await RunnerConnection(ws, secret).serve()

    log.info("runner.starting", host=host, port=port, signed=secret is not None)
    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        log.info("runner.signal", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    async with serve(handler, host, port):
        log.info("runner.listening", host=host, port=port)
        await stop_event.wait()
    log.info("runner.stopped")
    return 0
