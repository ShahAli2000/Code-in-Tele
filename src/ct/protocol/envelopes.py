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
    T_OPENED, T_SDK_ID, T_TEXT, T_TOOL_USE, T_TOOL_RESULT, T_THINKING,
    T_SYSTEM, T_PERMISSION_REQUEST, T_TURN_END, T_CLOSED, T_ERROR, T_PONG,
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
                 effort: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"cwd": cwd, "mode": mode}
    if resume is not None:
        p["resume"] = resume
    if system_prompt is not None:
        p["system_prompt"] = system_prompt
    if model is not None:
        p["model"] = model
    if effort is not None:
        p["effort"] = effort
    return p


# T_SET_MODEL: bridge → runner, change the model live (uses ClaudeSDKClient.set_model)
T_SET_MODEL = "set_model"


def set_model_payload(model: str) -> dict[str, Any]:
    return {"model": model}


def send_payload(text: str) -> dict[str, Any]:
    return {"text": text}


def decide_payload(*, tool_use_id: str, allow: bool,
                   updated_input: dict[str, Any] | None = None,
                   deny_message: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"tool_use_id": tool_use_id, "allow": allow}
    if allow and updated_input is not None:
        p["updated_input"] = updated_input
    if not allow and deny_message is not None:
        p["deny_message"] = deny_message
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
