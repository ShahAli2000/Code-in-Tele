"""Render Claude messages into Telegram messages.

Phase 0 MVP — no partial-message editing. Each AssistantMessage's text becomes
one Telegram message; tool_use blocks and tool results are separate messages so
the user sees the cadence of "Claude said X → tool → result → Claude said Y".

Phase 0+ refinement (deferred) will enable `include_partial_messages=True` on
the SDK and debounce-edit a single message for the "live transcript" feel.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Callable
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

log = structlog.get_logger(__name__)

# Telegram's hard message-size limit is 4096 chars; leave headroom for the
# "..." truncation marker and any HTML/Markdown framing.
MAX_TG_MESSAGE = 3800

# When an Edit/Write input (old_string, new_string, or content) exceeds this
# many chars, render the change as a unified-diff document attachment instead
# of cramming a truncated inline preview that hides most of the change.
DIFF_INLINE_LIMIT = 800


def _truncate(s: str, n: int = MAX_TG_MESSAGE) -> str:
    if len(s) <= n:
        return s
    return s[: n - 30] + "\n…[truncated]"


def _format_input_summary(tool_name: str, input_data: dict[str, Any]) -> str:
    """Compact, scannable summary of tool input for chat messages."""
    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        desc = input_data.get("description", "")
        out = f"$ {cmd}"
        if desc:
            out += f"\n# {desc}"
        return out[:1200]
    if tool_name in ("Edit", "Write"):
        path = input_data.get("file_path", "")
        if tool_name == "Edit":
            old = (input_data.get("old_string", "") or "")[:300]
            new = (input_data.get("new_string", "") or "")[:300]
            return f"{path}\n--- old ---\n{old}\n--- new ---\n{new}"
        else:
            content = (input_data.get("content", "") or "")[:600]
            return f"{path}\n--- new file ---\n{content}"
    if tool_name == "Read":
        return input_data.get("file_path", "")
    try:
        return json.dumps(input_data, indent=2)[:1000]
    except (TypeError, ValueError):
        return repr(input_data)[:1000]


def _build_unified_diff(path: str, old: str, new: str) -> str:
    """Generate a unified diff for an Edit operation. Same shape as
    `git diff` so any reader will be at home."""
    base = os.path.basename(path) or "file"
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{base}",
            tofile=f"b/{base}",
            n=3,
        )
    )


def _diff_changed_lines(old: str, new: str) -> tuple[int, int]:
    """Approximate (added, removed) line counts for the inline preview header.
    Cheap — we already split for the diff, so we count opcodes."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old_lines, new_lines
    ).get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


class TopicRenderer:
    """Renders messages from one SessionRunner.turn() into one Telegram topic."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        thread_id: int,
        *,
        silent: Callable[[], bool] | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        # Called per-send so quiet-hours changes take effect mid-turn instead
        # of being baked into the renderer at construction time.
        self._silent = silent or (lambda: False)

    async def _send(self, text: str) -> None:
        """Send text to the topic, spilling to a document if oversized."""
        if not text.strip():
            return
        silent = self._silent()
        if len(text) <= MAX_TG_MESSAGE:
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    message_thread_id=self.thread_id,
                    text=text,
                    disable_notification=silent,
                )
                return
            except TelegramBadRequest as exc:
                log.warning("send_message.failed", error=str(exc))
                return
        # Oversized: ship as a .txt document with a one-line summary in chat
        head = text[:200].splitlines()[0]
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                text=f"[long output — sent as file] {head}",
                disable_notification=silent,
            )
            await self.bot.send_document(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                document=BufferedInputFile(
                    text.encode("utf-8", errors="replace"), filename="output.txt"
                ),
                disable_notification=silent,
            )
        except TelegramBadRequest as exc:
            log.warning("send_document.failed", error=str(exc))

    # ---- public renderers --------------------------------------------------

    async def render_text(self, text: str) -> None:
        await self._send(text)

    async def render_tool_use(self, tool_name: str, input_data: dict[str, Any]) -> None:
        # Edit gets a unified diff regardless of size — the format is the same
        # one a programmer reads in a PR, and small edits read just as well
        # (often better) than the side-by-side old/new chunks. Above the
        # inline limit we spill the diff to a document attachment.
        if tool_name == "Edit":
            old = input_data.get("old_string", "") or ""
            new = input_data.get("new_string", "") or ""
            path = input_data.get("file_path", "") or "file"
            if len(old) > DIFF_INLINE_LIMIT or len(new) > DIFF_INLINE_LIMIT:
                await self._render_edit_as_diff(path, old, new)
                return
            await self._render_edit_inline_diff(path, old, new)
            return
        if tool_name == "Write":
            content = input_data.get("content", "") or ""
            if len(content) > DIFF_INLINE_LIMIT:
                await self._render_write_as_document(
                    input_data.get("file_path", "") or "file", content
                )
                return
        summary = _format_input_summary(tool_name, input_data)
        await self._send(f"🔧 {tool_name}\n\n{summary}")

    async def _render_edit_inline_diff(self, path: str, old: str, new: str) -> None:
        """Inline unified diff for small Edit operations — same format as
        `git diff`, just kept in chat. Falls back to the simple chunk-based
        view when the diff would be larger than the chunks (rare; happens for
        very small edits where header overhead dominates)."""
        diff = _build_unified_diff(path, old, new)
        if not diff.strip():
            await self._send(f"🔧 Edit\n\n{path}\n(no textual change)")
            return
        added, removed = _diff_changed_lines(old, new)
        body = f"🔧 Edit  {path}\n+{added}/-{removed} lines\n\n{diff}"
        await self._send(body)

    async def _render_edit_as_diff(self, path: str, old: str, new: str) -> None:
        added, removed = _diff_changed_lines(old, new)
        diff = _build_unified_diff(path, old, new)
        if not diff.strip():
            await self._send(f"🔧 Edit\n\n{path}\n(no textual change)")
            return
        header = f"🔧 Edit  {path}\n+{added}/-{removed} lines"
        silent = self._silent()
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, message_thread_id=self.thread_id, text=header,
                disable_notification=silent,
            )
            await self.bot.send_document(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                document=BufferedInputFile(
                    diff.encode("utf-8", errors="replace"),
                    filename=f"{os.path.basename(path) or 'edit'}.diff",
                ),
                disable_notification=silent,
            )
        except TelegramBadRequest as exc:
            log.warning("send_diff.failed", error=str(exc))

    async def _render_write_as_document(self, path: str, content: str) -> None:
        size = len(content)
        lines = content.count("\n") + 1
        header = f"📝 Write  {path}\n{lines} lines, {size} bytes"
        silent = self._silent()
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, message_thread_id=self.thread_id, text=header,
                disable_notification=silent,
            )
            await self.bot.send_document(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                document=BufferedInputFile(
                    content.encode("utf-8", errors="replace"),
                    filename=os.path.basename(path) or "new-file.txt",
                ),
                disable_notification=silent,
            )
        except TelegramBadRequest as exc:
            log.warning("send_write_doc.failed", error=str(exc))

    async def render_tool_result(self, content: Any, is_error: bool) -> None:
        body = content if isinstance(content, str) else repr(content)
        prefix = "✗ tool error" if is_error else "✓ tool result"
        if not body.strip():
            await self._send(f"{prefix} (empty)")
            return
        await self._send(f"{prefix}\n\n{body}")

    async def render_thinking(self) -> None:
        # Phase 0: don't surface thinking content (often verbose internal reasoning)
        pass

    async def render_error(self, message: str) -> None:
        await self._send(f"⚠ {message}")
