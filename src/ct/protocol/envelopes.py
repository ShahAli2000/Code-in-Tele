"""Wire protocol for bridge ↔ runner.

Every message on the wire is an `Envelope` — a single JSON object with version,
type, the bridge-side session id (a string the bridge picks; we use the
Telegram thread_id), a monotonic seq, and a typed payload.

The runner is multi-tenant: a single connected bridge can drive many sessions
multiplexed by `id`. All payloads are normalised away from the SDK's Python
types so the wire format is stable across SDK upgrades.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1

# Bridge → Runner types
T_OPEN = "open"               # start a session
T_SEND = "send"               # send a user message
T_DECIDE = "decide"           # resolve a pending permission
T_SET_MODE = "set_mode"       # change permission mode
T_INTERRUPT = "interrupt"     # cancel in-flight turn
T_CLOSE = "close"             # close the session
T_PING = "ping"               # heartbeat

# Runner → Bridge types
T_OPENED = "opened"           # ack of open
T_SDK_ID = "sdk_id"           # sdk_session_id assigned (during first turn)
T_TEXT = "text"               # assistant text chunk
T_TOOL_USE = "tool_use"       # claude wants to use a tool (tool_use_id, name, input)
T_TOOL_RESULT = "tool_result" # tool finished (tool_use_id, content, is_error)
T_THINKING = "thinking"       # claude is thinking (no content surfaced)
T_SYSTEM = "system"           # SDK SystemMessage (init, hooks, etc.)
T_PERMISSION_REQUEST = "permission_request"  # can_use_tool fired
T_TURN_END = "turn_end"       # ResultMessage seen, turn complete
T_CLOSED = "closed"           # session torn down
T_ERROR = "error"             # any failure
T_PONG = "pong"               # heartbeat reply

ALL_TYPES: frozenset[str] = frozenset({
    T_OPEN, T_SEND, T_DECIDE, T_SET_MODE, T_INTERRUPT, T_CLOSE, T_PING,
    "set_model",
    "list_dir", "mkdir", "upload", "fork", "get_logs", "export", "get_file",
    "search",
    T_OPENED, T_SDK_ID, T_TEXT, T_TOOL_USE, T_TOOL_RESULT, T_THINKING,
    T_SYSTEM, T_PERMISSION_REQUEST, T_TURN_END, T_CLOSED, T_ERROR, T_PONG,
    "dir_listing", "mkdir_ok", "upload_ok", "fork_ok", "logs", "export_ok",
    "file", "search_results",
})


@dataclass
class Envelope:
    type: str
    id: str                              # bridge-side session id (e.g. str(thread_id))
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)
    v: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "type": self.type,
            "id": self.id,
            "seq": self.seq,
            "ts": self.ts,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Envelope":
        if d.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {d.get('v')!r}")
        t = d.get("type")
        if t not in ALL_TYPES:
            raise ProtocolError(f"unknown envelope type: {t!r}")
        sid = d.get("id")
        if not isinstance(sid, str):
            raise ProtocolError(f"envelope id must be a string, got {type(sid).__name__}")
        payload = d.get("payload") or {}
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be an object")
        return cls(
            type=t,
            id=sid,
            payload=payload,
            seq=int(d.get("seq", 0)),
            ts=float(d.get("ts", time.time())),
            v=int(d.get("v", PROTOCOL_VERSION)),
        )

    @classmethod
    def from_json(cls, s: str) -> "Envelope":
        try:
            d = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid JSON: {exc!s}") from exc
        if not isinstance(d, dict):
            raise ProtocolError("envelope must be a JSON object")
        return cls.from_dict(d)


class ProtocolError(ValueError):
    """Malformed envelope, unknown type, or version mismatch."""


# ---- payload helpers ------------------------------------------------------
# Thin builders so callers don't sprinkle dict literals everywhere; the runtime
# checks happen in handlers, not here.


def open_payload(*, cwd: str, mode: str, resume: str | None = None,
                 system_prompt: str | None = None,
                 model: str | None = None,
                 effort: str | None = None,
                 auto_allow_tools: list[str] | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"cwd": cwd, "mode": mode}
    if resume is not None:
        p["resume"] = resume
    if system_prompt is not None:
        p["system_prompt"] = system_prompt
    if model is not None:
        p["model"] = model
    if effort is not None:
        p["effort"] = effort
    if auto_allow_tools:
        p["auto_allow_tools"] = list(auto_allow_tools)
    return p


# T_SET_MODEL: bridge → runner, change the model live (uses ClaudeSDKClient.set_model)
T_SET_MODEL = "set_model"


def set_model_payload(model: str) -> dict[str, Any]:
    return {"model": model}


# Connection-level RPCs (not session-scoped). The bridge uses Envelope.id as a
# request_id (not a session_id) and the runner echoes it on the reply so the
# bridge can correlate. Phase 7 introduces these for the folder-picker UX.
T_LIST_DIR = "list_dir"            # bridge → runner: payload {path, show_hidden}
T_DIR_LISTING = "dir_listing"      # runner → bridge: payload {path, items: [{name, is_dir}]}
T_MKDIR = "mkdir"                  # bridge → runner: payload {path}
T_MKDIR_OK = "mkdir_ok"            # runner → bridge: payload {path}

# Phase 8 — uploads. Bridge ships bytes to runner so files end up on the
# runner's filesystem regardless of which mac it lives on.
T_UPLOAD = "upload"                # bridge → runner: payload {path, content_b64}
T_UPLOAD_OK = "upload_ok"          # runner → bridge: payload {path, size}

# /fork — fork an existing SDK session. fork_session() reads/writes the on-disk
# .jsonl file in ~/.claude/projects/, so the operation MUST run on the runner
# that originally hosted the session (its file isn't on the bridge for remote
# macs). Connection-level RPC keyed by Envelope.id as request_id.
T_FORK = "fork"                    # bridge → runner: payload {sdk_session_id, cwd, title?}
T_FORK_OK = "fork_ok"              # runner → bridge: payload {sdk_session_id}

# /logs — read recent turns from the SDK transcript on disk. Like fork, runs on
# whichever runner owns the .jsonl file.
T_GET_LOGS = "get_logs"            # bridge → runner: payload {sdk_session_id, cwd, limit}
T_LOGS = "logs"                    # runner → bridge: payload {entries: [{role, text}]}

# /export — full markdown transcript, no truncation. Server-side rendering so
# the bridge doesn't need to know the JSONL message shape.
T_EXPORT = "export"                # bridge → runner: payload {sdk_session_id, cwd}
T_EXPORT_OK = "export_ok"          # runner → bridge: payload {markdown}

# /get — bridge asks the runner to read a file off disk and ship the bytes
# back. Capped at MAX_FILE_BYTES so we never blow Telegram's upload limit.
T_GET_FILE = "get_file"            # bridge → runner: payload {path}
T_FILE = "file"                    # runner → bridge: payload {path, content_b64, size}

# /search — case-insensitive substring search across the runner's transcripts.
# Bridge fans this out to every connected runner and aggregates.
T_SEARCH = "search"                # bridge → runner: payload {pattern, max_results}
T_SEARCH_RESULTS = "search_results"
# runner → bridge: payload {matches: [{sdk_session_id, role, snippet, project_name}]}


def list_dir_payload(path: str, show_hidden: bool = False) -> dict[str, Any]:
    return {"path": path, "show_hidden": show_hidden}


def dir_listing_payload(path: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"path": path, "items": items}


def mkdir_payload(path: str) -> dict[str, Any]:
    return {"path": path}


def mkdir_ok_payload(path: str) -> dict[str, Any]:
    return {"path": path}


def upload_payload(path: str, content_b64: str) -> dict[str, Any]:
    return {"path": path, "content_b64": content_b64}


def upload_ok_payload(path: str, size: int) -> dict[str, Any]:
    return {"path": path, "size": size}


def fork_payload(*, sdk_session_id: str, cwd: str,
                 title: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"sdk_session_id": sdk_session_id, "cwd": cwd}
    if title is not None:
        p["title"] = title
    return p


def fork_ok_payload(sdk_session_id: str) -> dict[str, Any]:
    return {"sdk_session_id": sdk_session_id}


def get_logs_payload(*, sdk_session_id: str, cwd: str,
                     limit: int = 20) -> dict[str, Any]:
    return {"sdk_session_id": sdk_session_id, "cwd": cwd, "limit": limit}


def logs_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"entries": entries}


def export_payload(*, sdk_session_id: str, cwd: str) -> dict[str, Any]:
    return {"sdk_session_id": sdk_session_id, "cwd": cwd}


def export_ok_payload(markdown: str) -> dict[str, Any]:
    return {"markdown": markdown}


def get_file_payload(path: str) -> dict[str, Any]:
    return {"path": path}


def file_payload(*, path: str, content_b64: str, size: int) -> dict[str, Any]:
    return {"path": path, "content_b64": content_b64, "size": size}


def search_payload(*, pattern: str, max_results: int = 30) -> dict[str, Any]:
    return {"pattern": pattern, "max_results": max_results}


def search_results_payload(matches: list[dict[str, Any]]) -> dict[str, Any]:
    return {"matches": matches}


def send_payload(text: str) -> dict[str, Any]:
    return {"text": text}


def decide_payload(*, tool_use_id: str, allow: bool,
                   updated_input: dict[str, Any] | None = None,
                   deny_message: str | None = None,
                   remember: bool = False) -> dict[str, Any]:
    p: dict[str, Any] = {"tool_use_id": tool_use_id, "allow": allow}
    if allow and updated_input is not None:
        p["updated_input"] = updated_input
    if not allow and deny_message is not None:
        p["deny_message"] = deny_message
    if remember:
        p["remember"] = True
    return p


def set_mode_payload(mode: str) -> dict[str, Any]:
    return {"mode": mode}


def text_payload(text: str) -> dict[str, Any]:
    return {"text": text}


def tool_use_payload(*, tool_use_id: str, name: str,
                     input: dict[str, Any]) -> dict[str, Any]:
    return {"tool_use_id": tool_use_id, "name": name, "input": input}


def tool_result_payload(*, tool_use_id: str, content: Any,
                        is_error: bool) -> dict[str, Any]:
    return {"tool_use_id": tool_use_id, "content": content, "is_error": is_error}


def system_payload(*, subtype: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"subtype": subtype, "data": data}


def permission_request_payload(*, tool_use_id: str, name: str,
                               input: dict[str, Any],
                               agent_id: str | None) -> dict[str, Any]:
    return {
        "tool_use_id": tool_use_id,
        "name": name,
        "input": input,
        "agent_id": agent_id,
    }


def sdk_id_payload(sdk_session_id: str) -> dict[str, Any]:
    return {"sdk_session_id": sdk_session_id}


def turn_end_payload(reason: str) -> dict[str, Any]:
    return {"reason": reason}


def closed_payload(reason: str) -> dict[str, Any]:
    return {"reason": reason}


def error_payload(*, kind: str, message: str) -> dict[str, Any]:
    return {"kind": kind, "message": message}
