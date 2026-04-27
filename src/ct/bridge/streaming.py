"""Render Claude messages into Telegram messages.

Phase 0 MVP — no partial-message editing. Each AssistantMessage's text becomes
one Telegram message; tool_use blocks and tool results are separate messages so
the user sees the cadence of "Claude said X → tool → result → Claude said Y".

Phase 0+ refinement (deferred) will enable `include_partial_messages=True` on
the SDK and debounce-edit a single message for the "live transcript" feel.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

log = structlog.get_logger(__name__)

# Telegram's hard message-size limit is 4096 chars; leave headroom for the
# "..." truncation marker and any HTML/Markdown framing.
MAX_TG_MESSAGE = 3800


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


class TopicRenderer:
    """Renders messages from one SessionRunner.turn() into one Telegram topic."""

    def __init__(self, bot: Bot, chat_id: int, thread_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id

    async def _send(self, text: str) -> None:
        """Send text to the topic, spilling to a document if oversized."""
        if not text.strip():
            return
        if len(text) <= MAX_TG_MESSAGE:
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    message_thread_id=self.thread_id,
                    text=text,
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
            )
            await self.bot.send_document(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                document=BufferedInputFile(
                    text.encode("utf-8", errors="replace"), filename="output.txt"
                ),
            )
        except TelegramBadRequest as exc:
            log.warning("send_document.failed", error=str(exc))

    # ---- public renderers --------------------------------------------------

    async def render_text(self, text: str) -> None:
        await self._send(text)

    async def render_tool_use(self, tool_name: str, input_data: dict[str, Any]) -> None:
        summary = _format_input_summary(tool_name, input_data)
        await self._send(f"🔧 {tool_name}\n\n{summary}")

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
