"""Aiogram bot wiring: middleware (allowlist + chat-id), commands, message
routing, and inline-button callbacks.

Phase 2 onwards: every Claude session lives on a runner daemon (local or
remote), reached over WebSocket via RunnerPool / SessionHandle. The bridge
itself never imports the SDK directly.
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

from ct.bridge.permissions_ui import PermissionsUI
from ct.bridge.runner_client import RunnerPool, SessionHandle
from ct.bridge.sessions import RestoreSpec, SessionStore, TopicSession
from ct.bridge.streaming import TopicRenderer
from ct.bridge.topics import GENERAL_TOPIC_ID, create_topic
from ct.config import Settings
from ct.protocol.envelopes import (
    Envelope,
    T_SYSTEM,
    T_TEXT,
    T_THINKING,
    T_TOOL_RESULT,
    T_TOOL_USE,
)
from ct.sdk_adapter.adapter import PermissionMode, PermissionRequest
from ct.store.db import Db

log = structlog.get_logger(__name__)

VALID_MODES: tuple[PermissionMode, ...] = (
    "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
)


def _parse_new_args(text: str) -> tuple[str, str | None, str | None]:
    """Parse `/new <name> [dir=<path>] [mac=<runner-name>]`.
    Returns (name, dir-or-None, mac-or-None)."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("usage: /new <name> [dir=<path>] [mac=<runner>]")
    rest = parts[1].strip()
    name = rest
    cwd: str | None = None
    mac: str | None = None
    if " dir=" in rest:
        idx = rest.index(" dir=")
        name = rest[:idx].strip()
        rest = rest[idx + 1:]  # keep the rest including 'dir=...'
    if " mac=" in rest:
        idx = rest.index(" mac=")
        if cwd is None:
            # `mac=` came before any `dir=` parsing
            name = rest[:idx].strip() if name == rest else name
        rest_after_mac = rest[idx + len(" mac="):].strip()
        # Take up to next space
        mac = rest_after_mac.split()[0] if rest_after_mac else None
        rest = rest[:idx]
    if rest.startswith("dir="):
        cwd = rest[len("dir="):].strip()
    elif "dir=" in rest:
        cwd = rest[rest.index("dir=") + len("dir="):].strip()
    if not name:
        raise ValueError("project name can't be empty")
    return name, cwd, mac


class BridgeBot:
    """One bot instance, one supergroup, many forum-topic sessions."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        db: Db,
        runner_pool: RunnerPool,
        *,
        default_runner: str = "studio",
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.db = db
        self.runners = runner_pool
        self.default_runner = default_runner
        self.sessions = SessionStore(db)
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
        self._router.message.register(self.cmd_macs, Command("macs"))
        self._router.message.register(self.cmd_help, Command("help", "start"))

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
            "  /new <name> [dir=<path>] [mac=<runner>] — start a session in its own topic\n"
            "  /list — show active sessions\n"
            "  /permissions [mode] — show or change permission mode for this topic\n"
            "  /close — close this topic's session\n"
            "  /macs — list registered runners\n"
            "  /help — this message\n\n"
            "Just type in a topic to talk to that session's Claude.\n"
            "Permission modes: " + ", ".join(VALID_MODES)
        )

    async def cmd_macs(self, message: Message) -> None:
        names = self.runners.names()
        if not names:
            await message.answer("no runners registered.")
            return
        lines = ["registered runners:"]
        for n in names:
            conn = self.runners.get(n)
            lines.append(f"  • {n}  ({conn.host}:{conn.port})")
        await message.answer("\n".join(lines))

    async def cmd_new(self, message: Message) -> None:
        if message.text is None:
            return
        try:
            name, dir_arg, mac_arg = _parse_new_args(message.text)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}")
            return
        cwd = Path(dir_arg).expanduser() if dir_arg else self.settings.project_root
        runner_name = mac_arg or self.default_runner

        # Validate runner is registered
        try:
            self.runners.get(runner_name)
        except KeyError:
            await message.answer(
                f"⚠ no runner registered as {runner_name!r}. "
                f"available: {', '.join(self.runners.names()) or '(none)'}"
            )
            return

        # Validate cwd exists. For the local "studio" runner the bridge and
        # runner share a filesystem so this check is authoritative; for remote
        # runners (Phase 3) we skip the local check and let the runner reject.
        if runner_name == self.default_runner and not cwd.is_dir():
            await message.answer(
                f"⚠ directory does not exist on {runner_name}: {cwd}\n"
                f"create it first, or pass a different dir=<path>"
            )
            return

        try:
            thread_id = await create_topic(self.bot, self.settings.telegram_chat_id, name)
        except TelegramBadRequest as exc:
            await message.answer(f"⚠ couldn't create topic: {exc!s}")
            return

        sid = str(thread_id)

        # Late-binding: handle callbacks need to reference the SessionHandle
        # that we don't have until open_session returns.
        handle_box: list[SessionHandle] = []

        async def perm_handler(req: PermissionRequest) -> None:
            await self.permissions_ui.render_card(
                runner=handle_box[0],
                chat_id=self.settings.telegram_chat_id,
                thread_id=thread_id,
                request=req,
            )

        try:
            handle = await self.runners.get(runner_name).open_session(
                sid=sid,
                cwd=str(cwd),
                mode="acceptEdits",
                on_permission_request=perm_handler,
                on_session_id_assigned=self._make_id_persister(thread_id),
            )
        except Exception as exc:
            log.exception("session.open_failed", name=name, runner=runner_name, cwd=str(cwd))
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=f"⚠ couldn't open session on runner {runner_name!r}: {exc!s}",
            )
            return
        handle_box.append(handle)

        await self.sessions.add(
            TopicSession(
                thread_id=thread_id,
                project_name=name,
                cwd=str(cwd),
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=runner_name,
            )
        )
        await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=thread_id,
            text=(
                f"✓ session ready\n"
                f"project: {name}\n"
                f"cwd:     {cwd}\n"
                f"runner:  {runner_name}\n"
                f"mode:    acceptEdits  (use /permissions to change)\n\n"
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
                f"  • {s.project_name}  (runner={s.runner_name}, cwd={s.cwd})  "
                f"mode={s.runner.permission_mode}"
            )
        await message.answer("\n".join(lines))

    async def cmd_permissions(self, message: Message) -> None:
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
            await session.runner.set_permission_mode(mode)  # type: ignore[arg-type]
        except Exception as exc:
            await message.answer(f"⚠ couldn't change mode: {exc!r}")
            return
        await self.sessions.update_permission_mode(session.thread_id, mode)
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
            await session.runner.close()
        except Exception:
            log.exception("session.close_failed", thread_id=session.thread_id)
        await self.sessions.close(session.thread_id)
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
                async for env in session.runner.turn(message.text):
                    await self._dispatch_envelope(env, renderer)
            except Exception as exc:
                log.exception("turn.failed", thread_id=thread_id)
                await renderer.render_error(f"turn failed: {exc!r}")
            else:
                await self.sessions.touch(thread_id)

    async def _dispatch_envelope(self, env: Envelope, renderer: TopicRenderer) -> None:
        if env.type == T_TEXT:
            text = env.payload.get("text", "")
            if text and isinstance(text, str) and text.strip():
                await renderer.render_text(text)
        elif env.type == T_TOOL_USE:
            await renderer.render_tool_use(
                env.payload.get("name", ""), env.payload.get("input", {}) or {}
            )
        elif env.type == T_TOOL_RESULT:
            await renderer.render_tool_result(
                env.payload.get("content", ""),
                bool(env.payload.get("is_error", False)),
            )
        elif env.type == T_THINKING:
            await renderer.render_thinking()
        elif env.type == T_SYSTEM:
            # Silent for Phase 0/1/2 (init, hook lifecycle, etc.)
            return

    # ---- callback queries ---------------------------------------------------

    async def on_callback(self, query: CallbackQuery) -> None:
        if await self.permissions_ui.handle_callback(query):
            return
        await query.answer()

    # ---- runner-side callbacks ---------------------------------------------

    def _make_id_persister(
        self, thread_id: int
    ) -> Callable[[str], Awaitable[None]]:
        async def persist(sdk_session_id: str) -> None:
            await self.sessions.update_sdk_session_id(thread_id, sdk_session_id)

        return persist

    # ---- restore-on-boot ----------------------------------------------------

    async def restore_sessions(self) -> int:
        """Rehydrate sessions from the DB. For each persisted active session,
        ask the configured runner to open the session with `resume=`."""

        async def factory(spec: RestoreSpec) -> SessionHandle:
            handle_box: list[SessionHandle] = []

            async def perm_handler(req: PermissionRequest) -> None:
                await self.permissions_ui.render_card(
                    runner=handle_box[0],
                    chat_id=self.settings.telegram_chat_id,
                    thread_id=spec.thread_id,
                    request=req,
                )

            # Phase 2: every restored session goes back to the default runner.
            # Phase 3 will read spec.runner_name from the DB.
            runner_name = self.default_runner
            handle = await self.runners.get(runner_name).open_session(
                sid=str(spec.thread_id),
                cwd=spec.cwd,
                mode=spec.permission_mode,  # type: ignore[arg-type]
                resume=spec.sdk_session_id,
                on_permission_request=perm_handler,
                on_session_id_assigned=self._make_id_persister(spec.thread_id),
            )
            handle_box.append(handle)
            return handle

        n = await self.sessions.restore(factory, default_runner_name=self.default_runner)
        log.info("bridge.sessions_restored", count=n)
        return n

    # ---- lifecycle ----------------------------------------------------------

    async def shutdown(self) -> None:
        for s in self.sessions.all():
            self.permissions_ui.cancel_pending_for(s.runner)
            try:
                await s.runner.close()
            except Exception:
                log.exception("session.close_failed", thread_id=s.thread_id)
        await self.runners.close_all()
