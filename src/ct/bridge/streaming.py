"""Quiet TopicRenderer — minimal Telegram chat noise, full detail on the dashboard.

Per-turn lifecycle:
1. `start()` kicks off a heartbeat task. After 5s of silence the task posts
   "working…"; every 2 min it edits the message with the elapsed minute count;
   from the 10-min mark it appends the currently-running tool (when known).
2. `render_text` / `render_tool_use` / `render_tool_result` / `render_thinking`
   are buffer-only — nothing reaches Telegram during the turn. Tool-use and
   tool-result envelopes still get logged to `message_log` by the dispatcher,
   so the web dashboard sees the full transcript.
3. `finish()` cancels the heartbeat, deletes the "working…" message (or edits
   it to "✓ done" if delete fails), and sends the final assistant text as a
   fresh Telegram message — fresh-message semantics so the user gets a
   notification on the answer (edits often skip notifications).
4. `render_error` cancels the heartbeat and surfaces the error fresh; the
   subsequent `finish()` is a no-op.

The "final answer" is the assistant text that arrives AFTER the last
`tool_result`. Earlier text blocks (the "I'll check the file" interludes) are
discarded, since they're noise without the tool output that follows.

Approval cards live outside this file — `permissions_ui.py` posts them
immediately while the heartbeat is still ticking.
"""

from __future__ import annotations

import asyncio
import time
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

# Time before the first heartbeat appears. Short turns finish before this and
# the user sees just the answer — no clutter.
HEARTBEAT_FIRST_DELAY_S = 5.0
# Cadence for subsequent heartbeat edits.
HEARTBEAT_INTERVAL_S = 120.0
# After this many seconds, append the running tool name to the heartbeat so
# the user has a hint why a turn is taking long.
HEARTBEAT_TOOL_HINT_AFTER_S = 600.0


class TopicRenderer:
    """One renderer per turn per topic. Don't reuse across turns — `start()`
    spawns a task that owns the heartbeat lifecycle for exactly one turn."""

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
        self._started_at: float | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_msg_id: int | None = None
        # Buffer of assistant text seen since the last tool_result. Only this
        # buffer's join-output reaches Telegram on `finish()`.
        self._post_tool_texts: list[str] = []
        self._current_tool: str | None = None
        self._errored = False
        self._finished = False

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Begin the turn. Spawns the heartbeat task; idempotent."""
        if self._started_at is not None:
            return
        self._started_at = time.monotonic()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"ct-heartbeat-{self.thread_id}"
        )

    async def finish(self) -> None:
        """End the turn. Cancels the heartbeat, removes the working-message,
        and posts the final assistant text as a fresh Telegram message."""
        if self._finished:
            return
        self._finished = True
        await self._cancel_heartbeat()
        if self._errored:
            # render_error already surfaced the failure; don't post a "final".
            await self._delete_heartbeat_msg()
            return
        await self._delete_heartbeat_msg()
        final = "\n\n".join(t for t in self._post_tool_texts if t).strip()
        if not final:
            # The turn ended with no post-tool assistant text (e.g. Claude only
            # ran tools and didn't summarize). A short ack keeps the user
            # informed that the turn actually completed.
            final = "✓ done"
        await self._send_final(final)

    # ---- per-envelope hooks (called from bot.py dispatch) -------------------

    async def render_text(self, text: str) -> None:
        if not text or not text.strip():
            return
        self._post_tool_texts.append(text.strip())

    async def render_tool_use(self, tool_name: str, input_data: dict[str, Any]) -> None:
        # Suppressed in chat. The dispatcher logs it to message_log so the
        # dashboard sees the call. Only side effect here: track which tool is
        # currently running so the 10-min heartbeat hint can name it.
        self._current_tool = tool_name

    async def render_tool_result(self, content: Any, is_error: bool) -> None:
        # The "post-tool" text buffer resets here. Any assistant text that
        # arrives next becomes part of the (possibly final) answer; earlier
        # interludes like "let me check the file" don't leak into the answer.
        self._current_tool = None
        self._post_tool_texts = []

    async def render_thinking(self, thinking_text: str = "") -> None:
        # Quiet by design — chat stays clean. Dispatcher persists the text to
        # message_log so the dashboard can surface it.
        return

    async def render_error(self, message: str) -> None:
        self._errored = True
        await self._cancel_heartbeat()
        await self._delete_heartbeat_msg()
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                text=f"⚠ {message}",
                disable_notification=self._silent(),
            )
        except TelegramBadRequest as exc:
            log.warning("send_error.failed", error=str(exc))

    # ---- internals ----------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        try:
            await asyncio.sleep(HEARTBEAT_FIRST_DELAY_S)
            try:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    message_thread_id=self.thread_id,
                    text="working…",
                    # Heartbeats never notify — they're progress indicators,
                    # not the user-facing answer.
                    disable_notification=True,
                )
                self._heartbeat_msg_id = msg.message_id
            except TelegramBadRequest as exc:
                log.warning("heartbeat.send_failed", error=str(exc))
                return

            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                if self._started_at is None:
                    return
                elapsed_s = int(time.monotonic() - self._started_at)
                elapsed_min = elapsed_s // 60
                text = f"working… ({elapsed_min}m"
                if elapsed_s >= HEARTBEAT_TOOL_HINT_AFTER_S and self._current_tool:
                    text += f", running {self._current_tool}"
                text += ")"
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self._heartbeat_msg_id,
                        text=text,
                    )
                except TelegramBadRequest:
                    # Edit can fail if the user deleted the message, or if the
                    # text matches what's already there. Either is harmless.
                    pass
        except asyncio.CancelledError:
            pass

    async def _cancel_heartbeat(self) -> None:
        task = self._heartbeat_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("heartbeat.task_failed_on_cancel")

    async def _delete_heartbeat_msg(self) -> None:
        msg_id = self._heartbeat_msg_id
        if msg_id is None:
            return
        self._heartbeat_msg_id = None
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=msg_id)
            return
        except TelegramBadRequest:
            pass
        # Fallback: try to edit it to a tiny "done" marker so chat doesn't
        # leave a stale "working…" line behind. If even that fails we give up.
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=msg_id, text="✓ done",
            )
        except TelegramBadRequest:
            pass

    async def _send_final(self, text: str) -> None:
        silent = self._silent()
        if len(text) <= MAX_TG_MESSAGE:
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    message_thread_id=self.thread_id,
                    text=text,
                    disable_notification=silent,
                )
            except TelegramBadRequest as exc:
                log.warning("send_final.failed", error=str(exc))
            return
        # Oversized: spill to .txt document with a one-line summary in chat.
        head = text[:200].splitlines()[0]
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                text=f"[long answer — sent as file] {head}",
                disable_notification=silent,
            )
            await self.bot.send_document(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                document=BufferedInputFile(
                    text.encode("utf-8", errors="replace"),
                    filename="answer.txt",
                ),
                disable_notification=silent,
            )
        except TelegramBadRequest as exc:
            log.warning("send_final_doc.failed", error=str(exc))
