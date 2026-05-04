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
    UserMessage,
)

from ct.sdk_adapter.tools import build_mcp_server

log = structlog.get_logger(__name__)

PermissionMode = Literal[
    "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"
]

# Appended to the SDK's default Claude Code system prompt for every bridge
# session. Claude self-gates on whether the project is actually a git repo
# (it'll run `git rev-parse` first) so this is a no-op for non-code chats.
GIT_WORKFLOW_DIRECTIVE = """
Git workflow for this session:
- If the working directory is a git repository (check with `git rev-parse \
--is-inside-work-tree`), commit your changes incrementally as you complete \
coherent units of work — a feature implemented, a bug fixed, a refactor \
stabilised. Don't wait until the end of the session.
- Use conventional-commit-style messages: `feat: …`, `fix: …`, \
`refactor: …`, `docs: …`, `chore: …`. One message per logical change, not \
a wall of text.
- Always `git add <specific paths>`. Never `git add -A` or `git add .` — \
that risks sweeping in `.env`, build artifacts, or other things that \
shouldn't be tracked.
- Never `git push` without being explicitly asked. Local commits only \
unless the user says otherwise.
- If a change is unfinished or in flux, hold off committing until it \
stabilises. Don't commit broken code "to save progress."
- If unsure whether something warrants a commit, ask the user.
- For non-code projects (no .git), skip this entirely — no commits, no \
mention of git.
"""


# Auto-test directive: nudge Claude to actually run the project's tests after
# meaningful Edit/Write changes. Heuristic-only — Claude inspects the project
# to figure out the right runner; if there's no test infrastructure, this
# directive is a no-op.
SVG_TOOL_DIRECTIVE = """
SVG→PNG rendering:
- A custom `svg_to_png` tool is registered for this session (visible as \
`mcp__ct__svg_to_png`). Use it when you've generated an SVG and the user \
wants a PNG — pass either `svg_path` (a file you wrote) or `svg_content` \
(inline string) plus `png_path`. Optional `width` / `height` set the \
output size; otherwise the SVG's intrinsic size is used.
- The tool wraps `rsvg-convert`. If it isn't installed on the runner, the \
tool returns a clear error pointing at `brew install librsvg`. Don't fall \
back to writing a Bash pipeline yourself unless that error appears.
"""


TEST_WORKFLOW_DIRECTIVE = """
Test workflow for this session:
- After non-trivial Edit/Write changes, look for a project test runner and \
run the tests that cover the change. Common runners include `pytest`, \
`npm test` / `pnpm test` / `yarn test`, `cargo test`, `go test ./...`, \
`uv run pytest`, `bun test`. Detect them from the project's manifest \
(`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, etc.) before \
guessing.
- Prefer running just the relevant test file or test name when you know \
which area you touched. Only fall back to the full suite when the change \
is broad or you're unsure.
- If tests fail, treat the failure as part of the work — diagnose, fix, \
re-run. Don't move on to a new change with the suite red.
- If there's no obvious test runner or test directory, skip this entirely \
— don't invent infrastructure for prototypes or one-off scripts.
- Don't run tests for purely cosmetic changes (formatting, comments, \
whitespace) where there's no behaviour to verify.
"""



@dataclass
class PermissionRequest:
    """One pending tool-use approval. The bridge receives this, surfaces the
    inline-button UI, then calls `SessionRunner.resolve_permission(...)` to release."""

    tool_use_id: str
    tool_name: str
    input_data: dict[str, Any]
    agent_id: str | None


PermissionRequestHandler = Callable[[PermissionRequest], Awaitable[None]]


# Per-session backstops on runaway tool loops or accidental cost spikes.
# Both are belt-and-braces against `bypassPermissions` profiles — even if a
# permission gate is off, a session can't grind forever or rack up unexpected
# spend. Override via env if you want different defaults for your fleet.
import os as _os
_DEFAULT_MAX_TURNS = int(_os.environ.get("CT_MAX_TURNS_PER_SESSION", "40"))
_DEFAULT_MAX_BUDGET_USD = float(_os.environ.get("CT_MAX_BUDGET_USD", "5.0"))
# Auto-fallback if the primary model is rate-limited. Empty string disables.
_DEFAULT_FALLBACK_MODEL = _os.environ.get("CT_FALLBACK_MODEL", "claude-sonnet-4-6")


class SessionRunner:
    def __init__(
        self,
        *,
        cwd: str,
        permission_mode: PermissionMode = "acceptEdits",
        on_permission_request: PermissionRequestHandler | None = None,
        on_session_id_assigned: Callable[[str], Awaitable[None]] | None = None,
        resume: str | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        thinking: bool = True,
        auto_allow_tools: set[str] | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        fallback_model: str | None = None,
        on_pre_compact: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.cwd = cwd
        self.permission_mode: PermissionMode = permission_mode
        self.on_permission_request = on_permission_request
        self.on_session_id_assigned = on_session_id_assigned
        self.resume = resume
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        # Adaptive thinking on by default — Claude picks the budget per turn.
        # The bridge defaults `True`; toggle off via `/think off`. Plumbed into
        # ClaudeAgentOptions.thinking at SDK connect time.
        self.thinking = thinking
        # Backstops — None means "use the env default". Setting an explicit
        # 0 / 0.0 disables that backstop for this session. The SDK enforces
        # both: max_turns trips a clean stop_reason, max_budget_usd halts on
        # cumulative API spend.
        self.max_turns = max_turns if max_turns is not None else _DEFAULT_MAX_TURNS
        self.max_budget_usd = (
            max_budget_usd if max_budget_usd is not None else _DEFAULT_MAX_BUDGET_USD
        )
        self.fallback_model = (
            fallback_model
            if fallback_model is not None
            else (_DEFAULT_FALLBACK_MODEL or None)
        )
        self.on_pre_compact = on_pre_compact
        # Tools the user has pre-trusted bot-wide (or per-profile, when that
        # plumbing arrives). Same in-memory set as approve-and-remember
        # populates at runtime — same trust ledger, different seed source.
        self._initial_auto_allow: set[str] = set(auto_allow_tools or set())

        # If we're resuming an existing SDK session, the bridge already knows
        # the id — surface it immediately so callers can rely on it before the
        # first turn produces an init message.
        self.session_id: str | None = resume

        self._client: ClaudeSDKClient | None = None
        # Future value is (decision, payload, remember). The third slot lets the
        # bridge signal "approve this tool, and skip the prompt for any further
        # use of the same tool within this session".
        self._pending: dict[str, asyncio.Future[tuple[str, Any, bool]]] = {}
        # Most-recent UserMessage.uuid we saw in the SDK stream. Updated in
        # `turn(...)` whenever a UserMessage flows through. The bridge's
        # /rewind passes this back to rewind_files() to restore tracked file
        # state to the moment that user message was received.
        self.last_user_message_uuid: str | None = None
        # Tool names the user has chosen to always-allow for this session.
        # Seeded from `auto_allow_tools` (bot/profile pre-trust list) and
        # extended at runtime via approve-and-remember. Cleared on stop —
        # deliberately NOT persisted across runner restarts so a fresh SDK
        # process gets a fresh trust ledger.
        self._remembered_allows: set[str] = set(self._initial_auto_allow)
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

        # Compose the system prompt: keep Claude Code's default preset and
        # APPEND our git-workflow directive (and any caller-supplied extra
        # text). Using SystemPromptPreset rather than a plain string preserves
        # Claude Code's full default behaviour — tool descriptions, planning
        # heuristics, formatting guidance, etc.
        append_parts: list[str] = [
            GIT_WORKFLOW_DIRECTIVE,
            TEST_WORKFLOW_DIRECTIVE,
            SVG_TOOL_DIRECTIVE,
        ]
        if self.system_prompt:
            append_parts.append(self.system_prompt)
        sdk_system_prompt: Any = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n".join(append_parts).strip(),
        }
        options_kwargs: dict[str, Any] = dict(
            cwd=self.cwd,
            permission_mode=self.permission_mode,
            resume=self.resume,
            system_prompt=sdk_system_prompt,
            can_use_tool=self._can_use_tool,
            # Isolate from the host's ~/.claude/settings.json. Without this, the
            # bridge inherits the user's `defaultMode`, hook bindings, plugin
            # allow rules, etc., which silently override our permission_mode.
            setting_sources=[],
            # Custom in-process tools (svg_to_png, etc.). Surfaced to Claude
            # under the `mcp__ct__<tool>` namespace. The MCP server is cheap
            # to rebuild per-session.
            mcp_servers={"ct": build_mcp_server()},
            # Dummy PreToolUse hook is REQUIRED for can_use_tool to fire in Python:
            # without it, the SDK closes the stream before the permission callback
            # can be invoked. Documented at docs.claude.com/en/agent-sdk/user-input.
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[self._keep_open_hook])
                ],
                # Best-effort heads-up before the SDK compacts conversation
                # history. The bridge posts "📦 compacting…" in the topic so
                # the user knows quality may shift; without this the user just
                # sees Claude lose context mid-stream.
                "PreCompact": [
                    HookMatcher(matcher=None, hooks=[self._pre_compact_hook])
                ],
            },
        )
        if self.model is not None:
            options_kwargs["model"] = self.model
        if self.effort is not None:
            options_kwargs["effort"] = self.effort
        # Thinking is set explicitly either way — passing the disabled config
        # rather than None future-proofs against any model whose default flips.
        options_kwargs["thinking"] = (
            {"type": "adaptive"} if self.thinking else {"type": "disabled"}
        )
        # Backstops: stop after N tool-using turns, or $X cumulative API spend
        # within this session. The SDK terminates with a clean ResultMessage,
        # which the runner already translates into T_TURN_END.
        if self.max_turns and self.max_turns > 0:
            options_kwargs["max_turns"] = self.max_turns
        if self.max_budget_usd and self.max_budget_usd > 0:
            options_kwargs["max_budget_usd"] = self.max_budget_usd
        if self.fallback_model:
            options_kwargs["fallback_model"] = self.fallback_model
        # File checkpointing: enables ClaudeSDKClient.rewind_files() so the
        # bridge's /rewind can restore on-disk file state to the moment a
        # specific user message was received. Requires the
        # `replay-user-messages` CLI flag so each UserMessage carries its
        # `uuid` in the response stream — the runner caches the most recent
        # uuid for the bridge to rewind to.
        options_kwargs["enable_file_checkpointing"] = True
        existing_extra = options_kwargs.get("extra_args") or {}
        options_kwargs["extra_args"] = {
            **dict(existing_extra),
            "replay-user-messages": None,
        }
        # Pipe the SDK CLI subprocess's stderr to our logger — without this,
        # CLI-level errors (auth failures, malformed envelopes from the CLI
        # side, deprecation warnings) go to whatever stderr the runner has,
        # which is structlog's stderr stream → the launchd log file. Routing
        # through structlog lets ops filter by tag.
        options_kwargs["stderr"] = self._on_sdk_stderr
        options = ClaudeAgentOptions(**options_kwargs)
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
                fut.set_result(("deny", "session stopped", False))
        self._pending.clear()
        self._remembered_allows.clear()

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
                    if self.on_session_id_assigned is not None:
                        try:
                            await self.on_session_id_assigned(sid)
                        except Exception:
                            log.exception(
                                "session.id_callback_failed", session_id=sid
                            )
            # Track the most recent UserMessage uuid so /rewind has a target.
            # Only set when extra_args includes replay-user-messages (which we
            # do at start()). UserMessage may also be a tool_result echo —
            # those have parent_tool_use_id set, skip them.
            if isinstance(msg, UserMessage) and getattr(msg, "uuid", None):
                if msg.parent_tool_use_id is None:
                    self.last_user_message_uuid = msg.uuid
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

    async def rewind_files(self, user_message_uuid: str | None = None) -> str | None:
        """Restore tracked files to their state at a specific user message.
        If user_message_uuid is None, uses self.last_user_message_uuid (the
        most recent user message we observed). Returns the uuid that was
        rewound to, or None if no checkpoint is available.

        Requires enable_file_checkpointing=True at session start (set in
        start()) so the SDK has been tracking edits."""
        if self._client is None:
            raise RuntimeError("SessionRunner not started")
        target = user_message_uuid or self.last_user_message_uuid
        if target is None:
            return None
        await self._client.rewind_files(target)
        log.info(
            "session.files_rewound", session_id=self.session_id, uuid=target
        )
        return target

    async def get_context_usage(self) -> dict[str, Any] | None:
        """Snapshot of the SDK's view of context window usage. Returns None
        if the client isn't connected (caller surfaces). The SDK returns a
        TypedDict but we widen to plain dict for transport over the WS."""
        if self._client is None:
            return None
        usage = await self._client.get_context_usage()
        return dict(usage) if usage is not None else None

    async def set_model(self, model: str) -> None:
        """Switch the model on the running ClaudeSDKClient. Takes effect on
        the next turn (the SDK applies it on the next assistant message)."""
        if self._client is None:
            raise RuntimeError("SessionRunner not started")
        await self._client.set_model(model)
        self.model = model
        log.info("session.model_changed", session_id=self.session_id, model=model)

    async def resolve_permission(
        self,
        tool_use_id: str,
        *,
        allow: bool,
        updated_input: dict[str, Any] | None = None,
        deny_message: str = "User denied this action",
        remember: bool = False,
    ) -> bool:
        """Bridge calls this when the user taps Approve/Deny. Returns True if a
        pending request matched (False if it had already resolved or is unknown).

        `remember=True` (only meaningful with allow=True) tells the runner to
        skip future approval prompts for this tool name within the current
        session. The actual rule is recorded inside `_can_use_tool` after the
        future resolves so the tool_name binding is in scope."""
        fut = self._pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        if allow:
            fut.set_result(("allow", updated_input, remember))
        else:
            fut.set_result(("deny", deny_message, False))
        return True

    # ---- internals ----------------------------------------------------------

    async def _can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        tool_use_id = context.tool_use_id

        # Approve-and-remember: if the user has previously chosen "always allow"
        # for this tool name in this session, skip the round-trip to Telegram.
        if tool_name in self._remembered_allows:
            log.info(
                "permission.remembered_auto_allow",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
            )
            return PermissionResultAllow(updated_input=input_data)

        # Fail-closed: no handler registered = deny.
        if self.on_permission_request is None:
            log.warning(
                "permission.no_handler", tool_use_id=tool_use_id, tool_name=tool_name
            )
            return PermissionResultDeny(message="no approval handler configured")

        # Key by tool_use_id, NOT session_id. Multiple tool_use blocks per turn
        # otherwise silently overwrite each other and the first never resolves.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, Any, bool]] = loop.create_future()
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
            decision, payload, remember = await fut
        finally:
            self._pending.pop(tool_use_id, None)

        if decision == "allow":
            if remember:
                self._remembered_allows.add(tool_name)
                log.info(
                    "permission.remember_added",
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                )
            return PermissionResultAllow(updated_input=payload or input_data)
        return PermissionResultDeny(message=str(payload or "denied"))

    @staticmethod
    async def _keep_open_hook(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Required no-op PreToolUse hook so can_use_tool fires in Python."""
        return {"continue_": True}

    async def _pre_compact_hook(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Fires before SDK conversation compaction. Notify the runner so it
        can emit a T_PRECOMPACT envelope; return continue_=True so we don't
        block the SDK's own decision."""
        if self.on_pre_compact is not None:
            try:
                await self.on_pre_compact()
            except Exception:
                log.exception(
                    "session.pre_compact_callback_failed", session_id=self.session_id
                )
        return {"continue_": True}

    def _on_sdk_stderr(self, line: str) -> None:
        """Callback for stderr lines from the SDK CLI subprocess. Logged at
        warn-level with the session id so a noisy CLI is greppable."""
        line = line.rstrip()
        if line:
            log.warning("sdk_cli_stderr", session_id=self.session_id, line=line)
