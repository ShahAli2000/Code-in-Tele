"""Aiogram bot wiring: middleware (allowlist + chat-id), commands, message
routing, and inline-button callbacks.

Phase 0 MVP. Everything is in one class for now; Phase 1 will likely extract
commands into their own modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, TelegramObject
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ct.bridge.permissions_ui import PermissionsUI
from ct.bridge.sessions import SessionStore, TopicSession
from ct.bridge.streaming import TopicRenderer
from ct.bridge.topics import GENERAL_TOPIC_ID, create_topic
from ct.config import Settings
from ct.sdk_adapter.adapter import (
    PermissionMode,
    PermissionRequest,
    SessionRunner,
)

log = structlog.get_logger(__name__)

VALID_MODES: tuple[PermissionMode, ...] = (
    "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
)


def _parse_new_args(text: str) -> tuple[str, str | None]:
    """Parse `/new <name> [dir=<path>]`. Returns (name, dir-or-None)."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("usage: /new <name> [dir=<path>]")
    rest = parts[1].strip()
    name = rest
    cwd: str | None = None
    if " dir=" in rest:
        idx = rest.index(" dir=")
        name = rest[:idx].strip()
        cwd = rest[idx + len(" dir="):].strip()
    elif rest.startswith("dir="):
        # `/new dir=...` without a name — bail.
        raise ValueError("usage: /new <name> [dir=<path>]")
    if not name:
        raise ValueError("project name can't be empty")
    return name, cwd


class BridgeBot:
    """One bot instance, one supergroup, many forum-topic sessions."""

    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self.sessions = SessionStore()
        self.permissions_ui = PermissionsUI(bot)
        self.dp = Dispatcher()
        self._router = Router()
        self._wire_middleware_and_handlers()
        self.dp.include_router(self._router)

    # ---- middleware ---------------------------------------------------------

    async def _allowlist_middleware(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Drop any update that's not from our chat or our allowlist."""
        chat_id = None
        user_id = None
        if isinstance(event, Message):
            chat_id = event.chat.id
            user_id = event.from_user.id if event.from_user else None
        elif isinstance(event, CallbackQuery):
            chat_id = event.message.chat.id if event.message else None
            user_id = event.from_user.id if event.from_user else None

        if chat_id is not None and chat_id != self.settings.telegram_chat_id:
            log.warning("auth.wrong_chat", chat_id=chat_id, user_id=user_id)
            return None
        if user_id is None or not self.settings.is_user_allowed(user_id):
            log.warning("auth.user_not_allowed", chat_id=chat_id, user_id=user_id)
            return None
        return await handler(event, data)

    # ---- registration -------------------------------------------------------

    def _wire_middleware_and_handlers(self) -> None:
        self._router.message.middleware(self._allowlist_middleware)
        self._router.callback_query.middleware(self._allowlist_middleware)

        self._router.message.register(self.cmd_new, Command("new"))
        self._router.message.register(self.cmd_list, Command("list"))
        self._router.message.register(self.cmd_permissions, Command("permissions"))
        self._router.message.register(self.cmd_close, Command("close"))
        self._router.message.register(self.cmd_help, Command("help", "start"))

        # Free-text in any non-General topic with text → run a turn.
        self._router.message.register(
            self.on_topic_message,
            F.message_thread_id.is_not(None) & F.text.is_not(None) & ~F.text.startswith("/"),
        )

        self._router.callback_query.register(self.on_callback)

    # ---- commands -----------------------------------------------------------

    async def cmd_help(self, message: Message) -> None:
        await message.answer(
            "Claude → Telegram bridge\n\n"
            "Commands:\n"
            "  /new <name> [dir=<path>] — create a new session in its own topic\n"
            "  /list — show active sessions\n"
            "  /permissions [mode] — show or change permission mode for this topic\n"
            "  /close — close this topic's session\n"
            "  /help — this message\n\n"
            "Just type in a topic to talk to that session's Claude.\n"
            "Permission modes: " + ", ".join(VALID_MODES)
        )

    async def cmd_new(self, message: Message) -> None:
        if message.text is None:
            return
        try:
            name, dir_arg = _parse_new_args(message.text)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}")
            return
        cwd = Path(dir_arg).expanduser() if dir_arg else self.settings.project_root
        if not cwd.is_dir():
            await message.answer(f"⚠ directory does not exist: {cwd}")
            return

        try:
            thread_id = await create_topic(self.bot, self.settings.telegram_chat_id, name)
        except TelegramBadRequest as exc:
            await message.answer(f"⚠ couldn't create topic: {exc!s}")
            return

        runner = SessionRunner(cwd=str(cwd), permission_mode="acceptEdits")
        runner.on_permission_request = self._make_perm_handler(thread_id, runner)

        try:
            await runner.start()
        except Exception as exc:
            log.exception("session.start_failed", name=name, cwd=str(cwd))
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=f"⚠ couldn't start session: {exc!r}",
            )
            return

        self.sessions.add(
            TopicSession(
                thread_id=thread_id,
                project_name=name,
                cwd=str(cwd),
                runner=runner,
                turn_lock=asyncio.Lock(),
            )
        )
        await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=thread_id,
            text=(
                f"✓ session ready\n"
                f"project: {name}\n"
                f"cwd: {cwd}\n"
                f"mode: acceptEdits  (use /permissions to change)\n\n"
                f"Type a message to start."
            ),
        )

    async def cmd_list(self, message: Message) -> None:
        active = self.sessions.all()
        if not active:
            await message.answer("no active sessions. use /new <name> to start one.")
            return
        lines = ["active sessions:"]
        for s in active:
            lines.append(
                f"  • {s.project_name}  ({s.cwd})  mode={s.runner.permission_mode}"
            )
        await message.answer("\n".join(lines))

    async def cmd_permissions(self, message: Message) -> None:
        # Only meaningful inside a topic
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/permissions only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic. use /new in General to start one.")
            return
        if message.text is None:
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 1:
            await message.answer(
                f"current mode: {session.runner.permission_mode}\n"
                f"available: {', '.join(VALID_MODES)}\n"
                f"usage: /permissions <mode>"
            )
            return
        mode = parts[1].strip()
        if mode not in VALID_MODES:
            await message.answer(
                f"⚠ unknown mode {mode!r}. valid: {', '.join(VALID_MODES)}"
            )
            return
        try:
            await session.runner.set_permission_mode(mode)
        except Exception as exc:
            await message.answer(f"⚠ couldn't change mode: {exc!r}")
            return
        await message.answer(
            f"✓ permission mode → {mode}\n"
            "(applies to the next tool request; any approval already waiting "
            "will resolve under the previous mode)"
        )

    async def cmd_close(self, message: Message) -> None:
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/close only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        self.permissions_ui.cancel_pending_for(session.runner)
        try:
            await session.runner.stop()
        except Exception:
            log.exception("session.stop_failed", thread_id=session.thread_id)
        self.sessions.remove(session.thread_id)
        await message.answer("✓ session closed. (topic remains; you can keep history.)")

    # ---- topic-text handler -------------------------------------------------

    async def on_topic_message(self, message: Message) -> None:
        thread_id = message.message_thread_id
        if thread_id is None or thread_id == GENERAL_TOPIC_ID:
            return
        if not message.text:
            return
        session = self.sessions.get(thread_id)
        if session is None:
            await message.answer(
                "⚠ no session in this topic — use /new <name> in General to start one."
            )
            return
        renderer = TopicRenderer(self.bot, self.settings.telegram_chat_id, thread_id)
        async with session.turn_lock:
            try:
                async for sdk_msg in session.runner.turn(message.text):
                    await self._dispatch_sdk_message(sdk_msg, renderer)
            except Exception as exc:
                log.exception("turn.failed", thread_id=thread_id)
                await renderer.render_error(f"turn failed: {exc!r}")

    async def _dispatch_sdk_message(self, sdk_msg: Any, renderer: TopicRenderer) -> None:
        if isinstance(sdk_msg, AssistantMessage):
            for block in sdk_msg.content:
                if isinstance(block, TextBlock):
                    if block.text and block.text.strip():
                        await renderer.render_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    await renderer.render_tool_use(block.name, block.input)
                elif isinstance(block, ThinkingBlock):
                    await renderer.render_thinking()
        elif isinstance(sdk_msg, UserMessage):
            for block in sdk_msg.content:
                if isinstance(block, ToolResultBlock):
                    is_error = bool(getattr(block, "is_error", False))
                    await renderer.render_tool_result(block.content, is_error)
        elif isinstance(sdk_msg, ResultMessage):
            return
        elif isinstance(sdk_msg, SystemMessage):
            # Silent for Phase 0 (init, hook lifecycle, etc.)
            return

    # ---- callback queries ---------------------------------------------------

    async def on_callback(self, query: CallbackQuery) -> None:
        if await self.permissions_ui.handle_callback(query):
            return
        # No other callback owners yet
        await query.answer()

    # ---- permission handler factory ----------------------------------------

    def _make_perm_handler(
        self, thread_id: int, runner: SessionRunner
    ) -> Callable[[PermissionRequest], Awaitable[None]]:
        async def handler(req: PermissionRequest) -> None:
            await self.permissions_ui.render_card(
                runner=runner,
                chat_id=self.settings.telegram_chat_id,
                thread_id=thread_id,
                request=req,
            )

        return handler

    # ---- lifecycle ----------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop every active session before the bot disconnects."""
        for s in self.sessions.all():
            self.permissions_ui.cancel_pending_for(s.runner)
            try:
                await s.runner.stop()
            except Exception:
                log.exception("session.stop_failed", thread_id=s.thread_id)
