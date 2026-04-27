"""Wrap claude_agent_sdk.ClaudeSDKClient for the bridge.

The bridge owns one SessionRunner per active Telegram topic. Each runner holds a
ClaudeSDKClient connection across many turns. Per-turn iteration is exposed via
`runner.turn(user_text)`, which yields every SDK message until the turn ends.

`can_use_tool` is wired through `on_permission_request`: when the SDK asks to
run a tool that isn't auto-approved, the runner calls the bridge's handler with
a PermissionRequest. The bridge surfaces the inline-button UI in Telegram and,
when the user taps, calls `runner.resolve_permission(tool_use_id, allow=...)`.
The runner returns the corresponding PermissionResult to the SDK.

Public API:
    runner = SessionRunner(
        cwd="/path/to/project",
        permission_mode="acceptEdits",
        on_permission_request=async_handler,
    )
    await runner.start()
    async for msg in runner.turn("user message"):
        ...                       # AssistantMessage / ToolResultBlock / etc.
        # session_id becomes available once the first SystemMessage(init) flows.
    await runner.set_permission_mode("bypassPermissions")
    await runner.resolve_permission(tool_use_id, allow=True)
    await runner.interrupt()
    await runner.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    ToolPermissionContext,
)

log = structlog.get_logger(__name__)

PermissionMode = Literal[
    "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"
]


@dataclass
class PermissionRequest:
    """One pending tool-use approval. The bridge receives this, surfaces the
    inline-button UI, then calls `SessionRunner.resolve_permission(...)` to release."""

    tool_use_id: str
    tool_name: str
    input_data: dict[str, Any]
    agent_id: str | None


PermissionRequestHandler = Callable[[PermissionRequest], Awaitable[None]]


class SessionRunner:
    def __init__(
        self,
        *,
        cwd: str,
        permission_mode: PermissionMode = "acceptEdits",
        on_permission_request: PermissionRequestHandler | None = None,
        resume: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.permission_mode: PermissionMode = permission_mode
        self.on_permission_request = on_permission_request
        self.resume = resume
        self.system_prompt = system_prompt

        self.session_id: str | None = None

        self._client: ClaudeSDKClient | None = None
        self._pending: dict[str, asyncio.Future[tuple[str, Any]]] = {}
        self._closed = False

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Connect the underlying ClaudeSDKClient.

        Note: `session_id` is NOT available yet — it arrives in the first
        SystemMessage(subtype='init') of the first `turn(...)`. Bridge code
        should expect `runner.session_id is None` until the first turn yields it.
        """
        if self._client is not None:
            raise RuntimeError("SessionRunner already started")

        options = ClaudeAgentOptions(
            cwd=self.cwd,
            permission_mode=self.permission_mode,
            resume=self.resume,
            system_prompt=self.system_prompt,
            can_use_tool=self._can_use_tool,
            # Isolate from the host's ~/.claude/settings.json. Without this, the
            # bridge inherits the user's `defaultMode`, hook bindings, plugin
            # allow rules, etc., which silently override our permission_mode.
            setting_sources=[],
            # Dummy PreToolUse hook is REQUIRED for can_use_tool to fire in Python:
            # without it, the SDK closes the stream before the permission callback
            # can be invoked. Documented at docs.claude.com/en/agent-sdk/user-input.
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[self._keep_open_hook])
                ]
            },
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        log.info("session.connected", cwd=self.cwd, mode=self.permission_mode)

    async def stop(self) -> None:
        """Cancel any pending approvals, disconnect the client."""
        if self._closed:
            return
        self._closed = True

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(("deny", "session stopped"))
        self._pending.clear()

        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        log.info("session.stopped", session_id=self.session_id)

    # ---- per-turn streaming -------------------------------------------------

    async def turn(self, text: str) -> AsyncIterator[Any]:
        """Send `text` as a user message and stream every SDK message until the
        assistant turn ends (terminating ResultMessage included).

        The iterator pauses naturally if `can_use_tool` is awaiting an approval —
        no special handling needed by callers.
        """
        if self._client is None or self._closed:
            raise RuntimeError("SessionRunner not started or already stopped")
        await self._client.query(text)
        async for msg in self._client.receive_response():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                sid = msg.data.get("session_id")
                if sid and self.session_id is None:
                    self.session_id = sid
                    log.info("session.id_assigned", session_id=sid)
            yield msg

    # ---- runtime control ----------------------------------------------------

    async def interrupt(self) -> None:
        """Cancel the in-flight assistant turn (if any)."""
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.interrupt()

    async def set_permission_mode(self, mode: PermissionMode) -> None:
        """Change permission mode. Per SDK docs, takes effect for the *next*
        tool request — any approval already awaiting `can_use_tool` resolution
        remains under the prior mode."""
        if self._client is None:
            raise RuntimeError("SessionRunner not started")
        await self._client.set_permission_mode(mode)
        self.permission_mode = mode
        log.info("session.permission_mode_changed", session_id=self.session_id, mode=mode)

    async def resolve_permission(
        self,
        tool_use_id: str,
        *,
        allow: bool,
        updated_input: dict[str, Any] | None = None,
        deny_message: str = "User denied this action",
    ) -> bool:
        """Bridge calls this when the user taps Approve/Deny. Returns True if a
        pending request matched (False if it had already resolved or is unknown)."""
        fut = self._pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        fut.set_result(("allow", updated_input) if allow else ("deny", deny_message))
        return True

    # ---- internals ----------------------------------------------------------

    async def _can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        tool_use_id = context.tool_use_id

        # Fail-closed: no handler registered = deny.
        if self.on_permission_request is None:
            log.warning(
                "permission.no_handler", tool_use_id=tool_use_id, tool_name=tool_name
            )
            return PermissionResultDeny(message="no approval handler configured")

        # Key by tool_use_id, NOT session_id. Multiple tool_use blocks per turn
        # otherwise silently overwrite each other and the first never resolves.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, Any]] = loop.create_future()
        self._pending[tool_use_id] = fut

        request = PermissionRequest(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            input_data=input_data,
            agent_id=context.agent_id,
        )

        try:
            await self.on_permission_request(request)
        except Exception as exc:
            self._pending.pop(tool_use_id, None)
            log.exception(
                "permission.handler_error", tool_use_id=tool_use_id, tool_name=tool_name
            )
            return PermissionResultDeny(message=f"approval handler error: {exc!r}")

        try:
            decision, payload = await fut
        finally:
            self._pending.pop(tool_use_id, None)

        if decision == "allow":
            return PermissionResultAllow(updated_input=payload or input_data)
        return PermissionResultDeny(message=str(payload or "denied"))

    @staticmethod
    async def _keep_open_hook(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Required no-op PreToolUse hook so can_use_tool fires in Python."""
        return {"continue_": True}
