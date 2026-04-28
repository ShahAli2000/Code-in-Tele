"""Aiogram bot wiring: middleware (allowlist + chat-id), commands, message
routing, and inline-button callbacks.

Phase 2 onwards: every Claude session lives on a runner daemon (local or
remote), reached over WebSocket via RunnerPool / SessionHandle. The bridge
itself never imports the SDK directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    Message,
    TelegramObject,
)

from ct.bridge import menu as menu_ui
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


VALID_EFFORTS = ("low", "medium", "high", "max")
MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _resolve_model(name: str) -> str:
    """Map common short names to canonical model ids; pass-through otherwise."""
    return MODEL_ALIASES.get(name, name)


@dataclass
class ParsedArgs:
    name: str
    cwd: str | None
    mac: str | None
    model: str | None
    effort: str | None
    mode: str | None


def _parse_kv_args(text: str, *, require_name: bool = True) -> ParsedArgs:
    """Parse `<command> <name>... [dir=...] [mac=...] [model=...] [effort=...]
    [mode=...]`. Tokens are split on whitespace; any `key=value` token is
    consumed as that flag, anything else becomes part of the name. Order-
    independent. Used by /new, /save, /defaults."""
    tokens = text.strip().split()
    if len(tokens) < 2 and require_name:
        raise ValueError(
            "usage: /<cmd> <name> [dir=<path>] [mac=<runner>] "
            "[model=<opus|sonnet|haiku|...>] [effort=<low|medium|high|max>] "
            "[mode=<default|acceptEdits|bypassPermissions|dontAsk|plan>]"
        )

    name_parts: list[str] = []
    cwd: str | None = None
    mac: str | None = None
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    for token in tokens[1:]:
        if token.startswith("dir="):
            cwd = token[len("dir="):]
        elif token.startswith("mac="):
            mac = token[len("mac="):]
        elif token.startswith("model="):
            model = _resolve_model(token[len("model="):])
        elif token.startswith("effort="):
            effort = token[len("effort="):]
            if effort not in VALID_EFFORTS:
                raise ValueError(
                    f"effort must be one of {', '.join(VALID_EFFORTS)}; got {effort!r}"
                )
        elif token.startswith("mode="):
            mode = token[len("mode="):]
            if mode not in VALID_MODES:
                raise ValueError(
                    f"mode must be one of {', '.join(VALID_MODES)}; got {mode!r}"
                )
        else:
            name_parts.append(token)

    name = " ".join(name_parts)
    if require_name and not name:
        raise ValueError("name can't be empty")
    return ParsedArgs(
        name=name, cwd=cwd or None, mac=mac or None,
        model=model, effort=effort, mode=mode,
    )


# Backward-compat shim so existing call sites (cmd_new) keep working.
_parse_new_args = _parse_kv_args


@dataclass
class ResolvedSession:
    """Final settings for a /new after applying explicit args > profile > defaults."""
    name: str
    cwd: str
    runner_name: str
    model: str | None
    effort: str | None
    permission_mode: str


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
        self.runners.on_reconnect = self.on_runner_reconnect
        # Wire the callback onto any RunnerConnections that already exist
        # (main.py adds the studio runner + DB-registered macs before
        # constructing the bridge, so they were created with on_reconnect=None).
        for _conn in self.runners._connections.values():  # type: ignore[attr-defined]
            _conn.on_reconnect = self.on_runner_reconnect
        self.default_runner = default_runner
        self.started_at = datetime.now(timezone.utc)
        self.sessions = SessionStore(db)
        self.permissions_ui = PermissionsUI(bot, db)
        # ForceReply tracking — message_id -> {action, ...} for ad-hoc prompts.
        self._pending_replies: dict[int, dict[str, Any]] = {}
        # Per-user pending action — fallback when the user types in General
        # without explicitly replying to the bot's prompt. Same shape as
        # _pending_replies; consumed on the user's next non-command text in
        # General (or any topic with thread_id=GENERAL_TOPIC_ID/None).
        self._pending_by_user: dict[int, dict[str, Any]] = {}
        # Browse-flow state per browse-card message_id.
        # Shape: {mac, path, main_dir, name, show_hidden, items: [(name, is_dir)]}
        self._browse_state: dict[int, dict[str, Any]] = {}
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
        self._router.message.register(self.cmd_model, Command("model"))
        self._router.message.register(self.cmd_effort, Command("effort"))
        self._router.message.register(self.cmd_status, Command("status"))
        self._router.message.register(self.cmd_save, Command("save"))
        self._router.message.register(self.cmd_defaults, Command("defaults"))
        self._router.message.register(self.cmd_profiles, Command("profiles"))
        self._router.message.register(self.cmd_menu, Command("menu", "m"))
        self._router.message.register(self.cmd_help, Command("help", "start"))

        # Single text-message handler. Dispatches:
        #   - explicit replies to bot ForceReply prompts
        #   - per-user pending actions (no explicit reply needed)
        #   - falls through to topic-message logic for normal conversation
        self._router.message.register(
            self.on_text_message,
            F.text.is_not(None) & ~F.text.startswith("/"),
        )

        self._router.callback_query.register(self.on_callback)

    # ---- commands -----------------------------------------------------------

    async def cmd_help(self, message: Message) -> None:
        await message.answer(
            "Claude → Telegram bridge\n\n"
            "Quick start:\n"
            "  /save mb dir=~/Local-Files mac=laptop    — save a profile once\n"
            "  /new mb                                  — start a session from it\n"
            "  /m                                       — buttons for the current topic\n"
            "\nAll commands:\n"
            "  /new [name] [dir=...] [mac=...] [model=...] [effort=...] [mode=...]\n"
            "      — no args: profile picker. with name: lookup profile + apply args.\n"
            "  /save <name> [...]   — save/update a profile\n"
            "  /profiles            — list saved profiles\n"
            "  /defaults [...]      — show/set bot-wide defaults\n"
            "  /menu  (or /m)       — buttons for this topic\n"
            "  /list                — active sessions\n"
            "  /permissions [mode]  — change mode (or use /m)\n"
            "  /model [name]        — live-swap model (or use /m)\n"
            "  /effort [level]      — set effort (or use /m)\n"
            "  /close               — close this topic's session\n"
            "  /macs [add|remove]   — manage registered runners\n"
            "  /status              — uptime, runners, sessions\n\n"
            "Just type in a topic to talk to Claude.\n"
            f"Permission modes: {', '.join(VALID_MODES)}\n"
            f"Effort levels:    {', '.join(VALID_EFFORTS)}\n"
            f"Model aliases:    {', '.join(MODEL_ALIASES)}"
        )

    async def cmd_macs(self, message: Message) -> None:
        if message.text is None:
            return
        parts = message.text.strip().split()
        # `/macs` or `/macs list` -> show; `/macs add ...` / `/macs remove ...` -> mutate
        if len(parts) <= 1 or parts[1] == "list":
            await self._macs_list(message)
        elif parts[1] == "add" and len(parts) >= 4:
            name = parts[2]
            host = parts[3]
            port = self.settings.runner_port
            # support `host:port` and trailing-arg port
            if ":" in host and host.rsplit(":", 1)[-1].isdigit():
                host, port_s = host.rsplit(":", 1)
                port = int(port_s)
            elif len(parts) >= 5 and parts[4].isdigit():
                port = int(parts[4])
            await self._macs_add(message, name, host, port)
        elif parts[1] == "remove" and len(parts) >= 3:
            await self._macs_remove(message, parts[2])
        elif parts[1] == "config" and len(parts) >= 3:
            await self._macs_config(message, parts[2:])
        else:
            await message.answer(
                "usage:\n"
                "  /macs                                  — list registered runners\n"
                "  /macs add NAME HOST [PORT]             — register a runner\n"
                "  /macs remove NAME                      — drop a runner\n"
                "  /macs config NAME main_dir=<path>      — set the browse root for that mac"
            )

    async def _macs_config(self, message: Message, rest_tokens: list[str]) -> None:
        if not rest_tokens:
            await message.answer("usage: /macs config NAME main_dir=<path>")
            return
        name = rest_tokens[0]
        if name not in self.runners.names() and not await self.db.get_mac(name):
            await message.answer(f"⚠ no mac registered as {name!r}")
            return
        new_main_dir: str | None = None
        for tok in rest_tokens[1:]:
            if tok.startswith("main_dir="):
                new_main_dir = tok[len("main_dir="):]
        if new_main_dir is None:
            await message.answer(
                "usage: /macs config NAME main_dir=<path>\n"
                "(currently the only configurable field)"
            )
            return
        # null/none clears
        if new_main_dir.lower() in ("null", "none", ""):
            await self.db.update_mac_main_dir(name, None)
            await message.answer(f"✓ {name}: main_dir cleared (will use $HOME)")
        else:
            await self.db.update_mac_main_dir(name, new_main_dir)
            await message.answer(f"✓ {name}: main_dir = {new_main_dir}")

    async def _macs_list(self, message: Message) -> None:
        names = self.runners.names()
        if not names:
            await message.answer("no runners registered.")
            return
        lines = ["registered runners:"]
        for n in names:
            conn = self.runners.get(n)
            lines.append(f"  • {n}  ({conn.host}:{conn.port})")
        await message.answer("\n".join(lines))

    async def _macs_add(
        self, message: Message, name: str, host: str, port: int
    ) -> None:
        if name == self.default_runner:
            await message.answer(
                f"⚠ {name!r} is the implicit local runner; pick another name."
            )
            return
        if name in self.runners.names():
            await message.answer(f"⚠ {name!r} is already registered.")
            return
        try:
            # Fewer attempts here than at boot — interactive caller would
            # rather see a fast no than wait 30 s.
            await self.runners.add_runner(
                name=name, host=host, port=port, max_attempts=3, retry_interval=1.5
            )
        except Exception as exc:
            await message.answer(f"⚠ couldn't connect to {host}:{port}: {exc!s}")
            return
        await self.db.insert_mac(name, host, port)
        await self.db.update_mac_connected(name)
        await message.answer(
            f"✓ {name} registered ({host}:{port}).\n"
            f"use it with: /new <project> mac={name} dir=<path>"
        )

    async def _macs_remove(self, message: Message, name: str) -> None:
        if name == self.default_runner:
            await message.answer(
                f"⚠ can't remove the local {self.default_runner!r} runner."
            )
            return
        # Refuse if any active session is bound to that mac.
        bound = [s for s in self.sessions.all() if s.runner_name == name]
        if bound:
            names = ", ".join(s.project_name for s in bound)
            await message.answer(
                f"⚠ {name!r} has active sessions ({names}); /close them first."
            )
            return
        try:
            conn = self.runners.get(name)
        except KeyError:
            removed_db = await self.db.remove_mac(name)
            if removed_db:
                await message.answer(f"✓ {name} removed (was in DB but not connected).")
            else:
                await message.answer(f"⚠ no runner registered as {name!r}.")
            return
        await conn.close()
        # Drop from pool and DB
        self.runners._connections.pop(name, None)  # type: ignore[attr-defined]
        await self.db.remove_mac(name)
        await message.answer(f"✓ {name} disconnected and removed.")

    async def _resolve_for_new(self, args: ParsedArgs) -> "ResolvedSession":
        """Apply precedence: explicit args > profile > bot defaults > hard-coded fallback."""
        profile = await self.db.get_profile(args.name) if args.name else None
        defaults = await self.db.all_defaults()

        def first(*vals: str | None) -> str | None:
            for v in vals:
                if v is not None and v != "":
                    return v
            return None

        cwd = first(
            args.cwd,
            profile.dir if profile else None,
        )
        if cwd is None:
            cwd = str(self.settings.project_root)
        runner_name = first(
            args.mac,
            profile.runner_name if profile else None,
            defaults.get("default_runner_name"),
        ) or self.default_runner
        model = first(
            args.model,
            profile.model if profile else None,
            defaults.get("default_model"),
        )
        effort = first(
            args.effort,
            profile.effort if profile else None,
            defaults.get("default_effort"),
        )
        mode = first(
            args.mode,
            profile.permission_mode if profile else None,
            defaults.get("default_permission_mode"),
        ) or "acceptEdits"
        return ResolvedSession(
            name=args.name,
            cwd=cwd,
            runner_name=runner_name,
            model=model,
            effort=effort,
            permission_mode=mode,
        )

    async def cmd_new(self, message: Message) -> None:
        if message.text is None:
            return
        # /new with no args → menu (profile picker), implemented in 6c
        tokens = message.text.strip().split()
        if len(tokens) == 1:
            await self._show_new_menu(message)
            return
        try:
            args = _parse_kv_args(message.text)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}")
            return
        resolved = await self._resolve_for_new(args)
        cwd = Path(resolved.cwd).expanduser()
        runner_name = resolved.runner_name

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
            thread_id = await create_topic(self.bot, self.settings.telegram_chat_id, args.name)
        except TelegramBadRequest as exc:
            await message.answer(f"⚠ couldn't create topic: {exc!s}")
            return

        sid = str(thread_id)

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
                mode=resolved.permission_mode,  # type: ignore[arg-type]
                model=resolved.model,
                effort=resolved.effort,
                on_permission_request=perm_handler,
                on_session_id_assigned=self._make_id_persister(thread_id),
            )
        except Exception as exc:
            log.exception(
                "session.open_failed", name=args.name, runner=runner_name, cwd=str(cwd)
            )
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
                project_name=args.name,
                cwd=str(cwd),
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=runner_name,
                model=resolved.model,
                effort=resolved.effort,
            )
        )
        ready_lines = [
            "✓ session ready",
            f"project: {args.name}",
            f"cwd:     {cwd}",
            f"runner:  {runner_name}",
            f"mode:    {resolved.permission_mode}",
            f"model:   {resolved.model or 'SDK default'}",
            f"effort:  {resolved.effort or 'SDK default'}",
            "",
            "Type a message to start, or /menu for buttons.",
        ]
        await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=thread_id,
            text="\n".join(ready_lines),
        )

    async def cmd_list(self, message: Message) -> None:
        active = self.sessions.all()
        if not active:
            await message.answer("no active sessions. use /new <name> to start one.")
            return
        lines = ["active sessions:"]
        for s in active:
            extras: list[str] = [f"runner={s.runner_name}"]
            if s.model:
                extras.append(f"model={s.model}")
            if s.effort:
                extras.append(f"effort={s.effort}")
            lines.append(
                f"  • {s.project_name}  ({', '.join(extras)}, cwd={s.cwd})  "
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

    async def cmd_model(self, message: Message) -> None:
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/model only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        if message.text is None:
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 1:
            await message.answer(
                f"current model: {session.model or '(SDK default)'}\n"
                f"aliases: {', '.join(MODEL_ALIASES)}\n"
                f"usage: /model <name-or-alias>"
            )
            return
        raw = parts[1].strip()
        model = _resolve_model(raw)
        try:
            await session.runner.set_model(model)
        except Exception as exc:
            await message.answer(f"⚠ couldn't change model: {exc!r}")
            return
        session.model = model
        await self.db.update_session_model(session.thread_id, model)
        await message.answer(f"✓ model → {model} (effective on next turn)")

    async def cmd_effort(self, message: Message) -> None:
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/effort only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        if message.text is None:
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 1:
            await message.answer(
                f"current effort: {session.effort or '(SDK default)'}\n"
                f"levels: {', '.join(VALID_EFFORTS)}\n"
                f"usage: /effort <level>\n"
                f"note: takes effect on the *next* session — for the running\n"
                f"      session, use /close + /new with effort=<level> set."
            )
            return
        level = parts[1].strip()
        if level not in VALID_EFFORTS:
            await message.answer(
                f"⚠ effort must be one of {', '.join(VALID_EFFORTS)}; got {level!r}"
            )
            return
        session.effort = level
        await self.db.update_session_effort(session.thread_id, level)
        await message.answer(
            f"✓ effort → {level}\n"
            f"saved to DB. takes effect when the session is next opened\n"
            f"(/close this topic and /new with effort={level} to apply now)."
        )

    async def cmd_save(self, message: Message) -> None:
        """Save (upsert) a profile. Any field you don't set is null and will
        fall back to /defaults at /new time."""
        if message.text is None:
            return
        try:
            args = _parse_kv_args(message.text)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}\nusage: /save <name> [dir=...] [mac=...] [model=...] [effort=...] [mode=...]")
            return
        await self.db.upsert_profile(
            name=args.name,
            dir=args.cwd,
            runner_name=args.mac,
            model=args.model,
            effort=args.effort,
            permission_mode=args.mode,
        )
        # Echo back what got saved
        fields = [
            ("dir", args.cwd),
            ("mac", args.mac),
            ("model", args.model),
            ("effort", args.effort),
            ("mode", args.mode),
        ]
        details = "\n".join(
            f"  {k}: {v}" for k, v in fields if v is not None
        ) or "  (no fields set — will use /defaults)"
        await message.answer(
            f"✓ profile saved: {args.name}\n{details}\n\n"
            f"start with: /new {args.name}"
        )

    async def cmd_profiles(self, message: Message) -> None:
        """Button-driven list of profiles. Tap to show details + delete."""
        await menu_ui.render_profiles(self.bot, self.settings.telegram_chat_id, self.db)

    async def cmd_defaults(self, message: Message) -> None:
        """Show/set bot-wide defaults. /defaults alone → button card.
        /defaults key=value [...] → set via text."""
        if message.text is None:
            return
        tokens = message.text.strip().split()
        # /defaults with no args → button-driven card
        if len(tokens) == 1:
            await menu_ui.render_defaults(self.bot, self.settings.telegram_chat_id, self.db)
            return
        # /defaults key=value [...] — try to parse via the same helper
        try:
            args = _parse_kv_args(message.text, require_name=False)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}")
            return
        changes: list[str] = []
        # Map: arg field → DB key
        for arg_val, key, name in [
            (args.mac, "default_runner_name", "mac"),
            (args.model, "default_model", "model"),
            (args.effort, "default_effort", "effort"),
            (args.mode, "default_permission_mode", "mode"),
        ]:
            if arg_val is None:
                continue
            v = None if arg_val == "null" else arg_val
            await self.db.set_default(key, v)
            changes.append(f"  {name} → {v or '(none)'}")
        if not changes:
            await message.answer(
                "⚠ nothing to change. /defaults shows current; pass key=value to set."
            )
            return
        await message.answer("✓ defaults updated:\n" + "\n".join(changes))

    async def cmd_menu(self, message: Message) -> None:
        """Per-topic action card with inline buttons."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/menu only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer(
                "no session in this topic. use /new in General to start one."
            )
            return
        await menu_ui.render_root(
            self.bot, self.settings.telegram_chat_id,
            message.message_thread_id, session,
        )

    async def _show_new_menu(self, message: Message) -> None:
        """Profile-picker keyboard. Tap a profile → instant session."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        profiles = await self.db.list_profiles()
        rows: list[list[InlineKeyboardButton]] = []
        # Two profiles per row
        for i in range(0, len(profiles), 2):
            row = []
            for p in profiles[i : i + 2]:
                row.append(
                    InlineKeyboardButton(
                        text=f"📁 {p.name}",
                        callback_data=f"ct:n:p:{p.name}",
                    )
                )
            rows.append(row)
        rows.append([menu_ui.browse_button()])
        rows.append(
            [
                InlineKeyboardButton(text="ℹ️ defaults", callback_data="ct:n:show_defaults"),
                InlineKeyboardButton(text="❓ usage", callback_data="ct:n:usage"),
            ]
        )
        if profiles:
            text = (
                "🚀 start a new session\n\n"
                "tap a profile to begin, or type:\n"
                "  /new <name> [dir=...] [mac=...] [model=...] [effort=...]"
            )
        else:
            text = (
                "no saved profiles yet.\n\n"
                "save one with:\n"
                "  /save mb dir=~/Local-Files mac=laptop model=opus\n\n"
                "then start with: /new mb"
            )
        await message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _handle_new_menu_callback(self, query: CallbackQuery) -> bool:
        """Handle ct:n:* callback queries from the /new picker."""
        data = query.data or ""
        if not data.startswith("ct:n:"):
            return False
        rest = data[len("ct:n:") :]
        if rest == "show_defaults":
            d = await self.db.all_defaults()
            await query.answer(
                f"defaults — mac:{d.get('default_runner_name') or 'studio'} "
                f"model:{d.get('default_model') or 'sdk'} "
                f"effort:{d.get('default_effort') or 'sdk'} "
                f"mode:{d.get('default_permission_mode') or 'acceptEdits'}",
                show_alert=True,
            )
            return True
        if rest == "usage":
            await query.answer(
                "type: /new <name> [dir=...] [mac=...] [model=...] [effort=...] [mode=...]",
                show_alert=True,
            )
            return True
        if rest.startswith("p:"):
            profile_name = rest[len("p:") :]
            await query.answer(f"starting {profile_name}…")
            # Synthesise a ParsedArgs and run the create flow
            await self._create_session_from_profile(query, profile_name)
            return True
        return False

    async def _create_session_from_profile(
        self, query: CallbackQuery, profile_name: str
    ) -> None:
        """Run the session-creation flow driven by a profile-button tap."""
        args = ParsedArgs(
            name=profile_name, cwd=None, mac=None, model=None, effort=None, mode=None
        )
        resolved = await self._resolve_for_new(args)
        await self._execute_new_session(resolved, source_label=f"profile: {profile_name}")

    async def _execute_new_session(
        self, resolved: "ResolvedSession", *, source_label: str = ""
    ) -> int | None:
        """Common path: validate runner + cwd, create topic, open session, add
        to store, post ready card. Returns the new thread_id or None on failure
        (errors are surfaced via Telegram messages)."""
        cwd = Path(resolved.cwd).expanduser()
        runner_name = resolved.runner_name

        try:
            self.runners.get(runner_name)
        except KeyError:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=(
                    f"⚠ no runner registered as {runner_name!r}. "
                    f"available: {', '.join(self.runners.names()) or '(none)'}"
                ),
            )
            return None

        if runner_name == self.default_runner and not cwd.is_dir():
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=f"⚠ directory does not exist on {runner_name}: {cwd}",
            )
            return None

        try:
            thread_id = await create_topic(
                self.bot, self.settings.telegram_chat_id, resolved.name
            )
        except TelegramBadRequest as exc:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=f"⚠ couldn't create topic: {exc!s}",
            )
            return None

        sid = str(thread_id)
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
                mode=resolved.permission_mode,  # type: ignore[arg-type]
                model=resolved.model,
                effort=resolved.effort,
                on_permission_request=perm_handler,
                on_session_id_assigned=self._make_id_persister(thread_id),
            )
        except Exception as exc:
            log.exception("session.open_failed", name=resolved.name, runner=runner_name)
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=f"⚠ couldn't open session: {exc!s}",
            )
            return None
        handle_box.append(handle)

        await self.sessions.add(
            TopicSession(
                thread_id=thread_id,
                project_name=resolved.name,
                cwd=str(cwd),
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=runner_name,
                model=resolved.model,
                effort=resolved.effort,
            )
        )
        suffix = f" ({source_label})" if source_label else ""
        await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=thread_id,
            text=(
                f"✓ session ready{suffix}\n"
                f"project: {resolved.name}\n"
                f"cwd:     {cwd}\n"
                f"runner:  {runner_name}\n"
                f"mode:    {resolved.permission_mode}\n"
                f"model:   {resolved.model or '(SDK default)'}\n"
                f"effort:  {resolved.effort or '(SDK default)'}\n\n"
                f"Type a message to start, or /m for buttons."
            ),
        )
        return thread_id

    async def cmd_status(self, message: Message) -> None:
        """Snapshot of bot health: uptime, runner connectivity, active sessions."""
        now = datetime.now(timezone.utc)
        uptime = now - self.started_at
        # Format uptime as Hh Mm or Dd Hh Mm
        total_seconds = int(uptime.total_seconds())
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m"

        lines = [
            "📊 bridge status",
            f"uptime:   {uptime_str} (since {self.started_at.strftime('%Y-%m-%d %H:%M UTC')})",
            f"chat_id:  {self.settings.telegram_chat_id}",
            "",
            "runners:",
        ]
        for name in self.runners.names():
            conn = self.runners.get(name)
            state = "✓ connected" if conn.connected else "✗ disconnected"
            sess_count = len([s for s in self.sessions.all() if s.runner_name == name])
            lines.append(f"  • {name}  ({conn.host}:{conn.port})  {state}  {sess_count} session(s)")

        active = self.sessions.all()
        lines.append("")
        if not active:
            lines.append("sessions: none active")
        else:
            lines.append(f"sessions: {len(active)} active")
            for s in active:
                lines.append(
                    f"  • {s.project_name}  runner={s.runner_name}  "
                    f"mode={s.runner.permission_mode}"
                    + (f"  model={s.model}" if s.model else "")
                    + (f"  effort={s.effort}" if s.effort else "")
                )

        await message.answer("\n".join(lines))

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
        if await menu_ui.handle_callback(
            query,
            sessions_get=self.sessions.get,
            db=self.db,
            bot=self.bot,
            on_close=self._close_session_via_menu,
        ):
            return
        if await menu_ui.handle_defaults_callback(
            query, db=self.db, bot=self.bot,
            runner_names_fn=self.runners.names,
        ):
            return
        if await menu_ui.handle_profiles_callback(query, db=self.db, bot=self.bot):
            return
        if await self._handle_browse_callback(query):
            return
        if await self._handle_new_menu_callback(query):
            return
        await query.answer()

    # ---- folder browser callbacks (ct:b:*) --------------------------------

    async def _handle_browse_callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith(menu_ui.BROWSE_PREFIX):
            return False
        rest = data[len(menu_ui.BROWSE_PREFIX) :]
        parts = rest.split(":", 1)
        verb = parts[0]

        if verb == "back":
            # discard browse state for this message and return to /new menu
            if query.message is not None:
                self._browse_state.pop(query.message.message_id, None)
            await self._edit_to_new_menu(query)
            await query.answer()
            return True

        if verb == "noop":
            await query.answer()
            return True

        if verb == "start":
            names = self.runners.names()
            if not names:
                await query.answer("no runners registered", show_alert=True)
                return True
            if len(names) == 1:
                await self._begin_browse(query, names[0], show_hidden=False)
            else:
                await menu_ui._safe_edit(
                    self.bot, query,
                    text="📂 browse — pick a mac:",
                    keyboard=menu_ui.mac_picker_keyboard(names),
                )
                await query.answer()
            return True

        if verb == "m" and len(parts) >= 2:
            mac_name = parts[1]
            await self._begin_browse(query, mac_name, show_hidden=False)
            return True

        # Past this point, all verbs operate on the per-message browse state.
        if query.message is None:
            await query.answer("no message")
            return True
        state = self._browse_state.get(query.message.message_id)
        if state is None:
            await query.answer("browse session expired — tap [📂 browse] again", show_alert=True)
            return True

        if verb == "cd" and len(parts) >= 2:
            try:
                idx = int(parts[1])
            except ValueError:
                await query.answer("bad index")
                return True
            dirs = [(n, is_dir) for n, is_dir in state["items"] if is_dir]
            if not (0 <= idx < len(dirs)):
                await query.answer("folder no longer there — refreshing")
                await self._refresh_browse(query, state)
                return True
            child_name, _ = dirs[idx]
            new_path = str(Path(state["path"]) / child_name)
            state["path"] = new_path
            state["name"] = child_name  # default chat name = folder name
            await self._refresh_browse(query, state)
            return True

        if verb == "up":
            parent = str(Path(state["path"]).parent)
            if parent == state["path"]:
                await query.answer("already at /")
                return True
            state["path"] = parent
            state["name"] = Path(parent).name or state["mac"]
            await self._refresh_browse(query, state)
            return True

        if verb == "hidden":
            state["show_hidden"] = not state.get("show_hidden", False)
            await self._refresh_browse(query, state)
            return True

        if verb == "newdir":
            await self._prompt_new_folder_in_state(query, state)
            return True

        if verb == "setname":
            await self._prompt_set_name(query, state)
            return True

        if verb == "open":
            await self._confirm_open_browse(query, state)
            return True

        await query.answer("unknown")
        return True

    async def _begin_browse(
        self, query: CallbackQuery, mac_name: str, *, show_hidden: bool
    ) -> None:
        try:
            conn = self.runners.get(mac_name)
        except KeyError:
            await query.answer(f"runner {mac_name!r} not registered", show_alert=True)
            return
        mac_row = await self.db.get_mac(mac_name)
        main_dir = (mac_row.main_dir if mac_row else None) or "~"
        try:
            resolved_path, items = await conn.list_dir(main_dir, show_hidden=show_hidden)
        except Exception as exc:
            await menu_ui._safe_edit(
                self.bot, query,
                text=f"⚠ couldn't list {mac_name}:{main_dir}\n\n{exc!s}",
                keyboard=None,
            )
            await query.answer()
            return
        # Remember the resolved main_dir so 'up' knows when not to go further
        # (we let it go above main_dir but limit at the filesystem root)
        state = {
            "mac": mac_name,
            "path": resolved_path,
            "main_dir": resolved_path,
            "name": Path(resolved_path).name or mac_name,
            "show_hidden": show_hidden,
            "items": items,
        }
        if query.message is not None:
            self._browse_state[query.message.message_id] = state
        await menu_ui._safe_edit(
            self.bot, query,
            text=menu_ui.navigate_text(
                mac_name=mac_name, path=resolved_path,
                name=state["name"], folder_items=items,
                show_hidden=show_hidden,
            ),
            keyboard=menu_ui.navigate_keyboard(
                folder_items=items, show_hidden=show_hidden,
                can_go_up=str(Path(resolved_path).parent) != resolved_path,
            ),
        )
        await query.answer()

    async def _refresh_browse(
        self, query: CallbackQuery, state: dict[str, Any]
    ) -> None:
        try:
            conn = self.runners.get(state["mac"])
            resolved_path, items = await conn.list_dir(
                state["path"], show_hidden=state.get("show_hidden", False)
            )
        except Exception as exc:
            await menu_ui._safe_edit(
                self.bot, query,
                text=f"⚠ couldn't list {state['mac']}:{state['path']}\n\n{exc!s}",
                keyboard=None,
            )
            await query.answer()
            return
        state["path"] = resolved_path
        state["items"] = items
        await menu_ui._safe_edit(
            self.bot, query,
            text=menu_ui.navigate_text(
                mac_name=state["mac"], path=resolved_path,
                name=state["name"], folder_items=items,
                show_hidden=state.get("show_hidden", False),
            ),
            keyboard=menu_ui.navigate_keyboard(
                folder_items=items,
                show_hidden=state.get("show_hidden", False),
                can_go_up=str(Path(resolved_path).parent) != resolved_path,
            ),
        )
        await query.answer()

    async def _prompt_new_folder_in_state(
        self, query: CallbackQuery, state: dict[str, Any]
    ) -> None:
        if query.message is None:
            return
        sent = await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text=(
                f"➕ new folder under {state['mac']}:{state['path']}\n"
                f"send the folder name (no slashes). reply or just type."
            ),
            reply_markup=ForceReply(),
        )
        action = {
            "action": "new_folder",
            "browse_message_id": query.message.message_id,
        }
        self._pending_replies[sent.message_id] = action
        if query.from_user is not None:
            self._pending_by_user[query.from_user.id] = action
        await query.answer("type a name…")

    async def _prompt_set_name(
        self, query: CallbackQuery, state: dict[str, Any]
    ) -> None:
        if query.message is None:
            return
        sent = await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text=(
                f"✏ session name (currently: {state['name']})\n"
                f"send a custom name, or '-' to use the folder name."
            ),
            reply_markup=ForceReply(),
        )
        action = {
            "action": "set_name",
            "browse_message_id": query.message.message_id,
        }
        self._pending_replies[sent.message_id] = action
        if query.from_user is not None:
            self._pending_by_user[query.from_user.id] = action
        await query.answer("type a name…")

    async def _confirm_open_browse(
        self, query: CallbackQuery, state: dict[str, Any]
    ) -> None:
        await query.answer(f"opening {state['name']}…")
        # Drop browse state — we're done navigating
        if query.message is not None:
            self._browse_state.pop(query.message.message_id, None)
        args = ParsedArgs(
            name=state["name"], cwd=state["path"], mac=state["mac"],
            model=None, effort=None, mode=None,
        )
        resolved = await self._resolve_for_new(args)
        await self._execute_new_session(
            resolved, source_label=f"via browse on {state['mac']}"
        )
        await menu_ui._safe_edit(
            self.bot, query,
            text=f"✓ opened {state['mac']}:{state['path']} as {state['name']!r}",
            keyboard=None,
        )

    async def _edit_to_new_menu(self, query: CallbackQuery) -> None:
        """Re-render the /new menu inside an existing message (used by 'back')."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        profiles = await self.db.list_profiles()
        rows: list[list[InlineKeyboardButton]] = []
        for i in range(0, len(profiles), 2):
            row = []
            for p in profiles[i : i + 2]:
                row.append(InlineKeyboardButton(
                    text=f"📁 {p.name}", callback_data=f"ct:n:p:{p.name}"
                ))
            rows.append(row)
        rows.append([menu_ui.browse_button()])
        rows.append([
            InlineKeyboardButton(text="ℹ️ defaults", callback_data="ct:n:show_defaults"),
            InlineKeyboardButton(text="❓ usage", callback_data="ct:n:usage"),
        ])
        text = "🚀 start a new session"
        await menu_ui._safe_edit(
            self.bot, query, text=text,
            keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # ---- ForceReply handler -----------------------------------------------

    async def on_text_message(self, message: Message) -> None:
        """Universal text-message handler. Order:
        1. Explicit reply to a ForceReply prompt → consume.
        2. User has pending action AND message is in General → consume.
        3. Otherwise → delegate to on_topic_message (Claude turn / no-op).
        """
        # 1. explicit reply
        if message.reply_to_message is not None:
            parent_id = message.reply_to_message.message_id
            pending = self._pending_replies.pop(parent_id, None)
            if pending is not None:
                # Also clear the per-user fallback so we don't double-fire
                if message.from_user is not None:
                    self._pending_by_user.pop(message.from_user.id, None)
                await self._dispatch_pending(message, pending)
                return

        # 2. per-user pending in General topic (or main chat)
        in_general = (
            message.message_thread_id is None
            or message.message_thread_id == GENERAL_TOPIC_ID
        )
        if in_general and message.from_user is not None:
            pending = self._pending_by_user.pop(message.from_user.id, None)
            if pending is not None:
                # Also remove any matching by-message-id pending entries
                bmsg_id = pending.get("browse_message_id")
                # (no easy reverse lookup; the by-message-id dict is keyed by
                # the *prompt* message_id, not the browse card. Leave stale
                # entries; they're harmless and get GC'd next time.)
                await self._dispatch_pending(message, pending)
                return

        # 3. fall through to existing topic-message logic
        await self.on_topic_message(message)

    async def _dispatch_pending(
        self, message: Message, pending: dict[str, Any]
    ) -> None:
        action = pending.get("action")
        if action == "new_folder":
            await self._handle_new_folder_reply(message, pending)
        elif action == "set_name":
            await self._handle_set_name_reply(message, pending)
        else:
            log.warning("pending.unknown_action", action=action)

    async def _handle_new_folder_reply(
        self, message: Message, pending: dict[str, Any]
    ) -> None:
        browse_msg_id = pending.get("browse_message_id")
        state = self._browse_state.get(browse_msg_id) if browse_msg_id else None
        if state is None:
            await message.answer("⚠ browse session expired — tap [📂 browse] again")
            return
        folder_name = (message.text or "").strip()
        if not folder_name or "/" in folder_name or folder_name in (".", ".."):
            await message.answer("⚠ folder name can't be empty, contain '/', or be '.' / '..'")
            return
        try:
            conn = self.runners.get(state["mac"])
        except KeyError:
            await message.answer(f"⚠ runner {state['mac']!r} no longer registered")
            return
        full_path = str(Path(state["path"]) / folder_name)
        try:
            await conn.mkdir(full_path)
        except RuntimeError as exc:
            await message.answer(f"⚠ couldn't create {folder_name}: {exc!s}")
            return
        # Drill into the new folder so the user sees they're inside it
        state["path"] = full_path
        state["name"] = folder_name
        # Re-render the browse card. Build a fake CallbackQuery-like wrapper
        # so we can reuse _refresh_browse, OR just edit directly here.
        try:
            resolved_path, items = await conn.list_dir(
                full_path, show_hidden=state.get("show_hidden", False)
            )
        except Exception as exc:
            await message.answer(f"⚠ created {folder_name} but couldn't list it: {exc!s}")
            return
        state["items"] = items
        try:
            await self.bot.edit_message_text(
                chat_id=self.settings.telegram_chat_id,
                message_id=browse_msg_id,
                text=menu_ui.navigate_text(
                    mac_name=state["mac"], path=resolved_path,
                    name=state["name"], folder_items=items,
                    show_hidden=state.get("show_hidden", False),
                ),
                reply_markup=menu_ui.navigate_keyboard(
                    folder_items=items,
                    show_hidden=state.get("show_hidden", False),
                    can_go_up=str(Path(resolved_path).parent) != resolved_path,
                ),
            )
        except TelegramBadRequest:
            pass
        await message.answer(f"✓ created {full_path} — drilling in")

    async def _handle_set_name_reply(
        self, message: Message, pending: dict[str, Any]
    ) -> None:
        browse_msg_id = pending.get("browse_message_id")
        state = self._browse_state.get(browse_msg_id) if browse_msg_id else None
        if state is None:
            await message.answer("⚠ browse session expired — tap [📂 browse] again")
            return
        raw = (message.text or "").strip()
        if raw == "-" or not raw:
            new_name = Path(state["path"]).name or state["mac"]
        else:
            new_name = raw[:64]  # bounded for forum-topic name
        state["name"] = new_name
        try:
            await self.bot.edit_message_text(
                chat_id=self.settings.telegram_chat_id,
                message_id=browse_msg_id,
                text=menu_ui.navigate_text(
                    mac_name=state["mac"], path=state["path"],
                    name=new_name, folder_items=state["items"],
                    show_hidden=state.get("show_hidden", False),
                ),
                reply_markup=menu_ui.navigate_keyboard(
                    folder_items=state["items"],
                    show_hidden=state.get("show_hidden", False),
                    can_go_up=str(Path(state["path"]).parent) != state["path"],
                ),
            )
        except TelegramBadRequest:
            pass
        await message.answer(f"✓ name → {new_name}")

    async def _close_session_via_menu(self, session: TopicSession) -> None:
        """Called by the action card's close button."""
        self.permissions_ui.cancel_pending_for(session.runner)
        try:
            await session.runner.close()
        except Exception:
            log.exception("session.close_failed", thread_id=session.thread_id)
        await self.sessions.close(session.thread_id)

    # ---- runner-side callbacks ---------------------------------------------

    async def on_runner_reconnect(self, name: str, sids: list[str]) -> None:
        """Called by RunnerPool after a runner's WS reconnects. Post a sticky
        note in each topic whose session was just re-opened with resume=."""
        log.info("bridge.runner_reconnected", name=name, sessions=sids)
        for sid in sids:
            try:
                thread_id = int(sid)
            except ValueError:
                continue
            session = self.sessions.get(thread_id)
            if session is None:
                continue
            try:
                await self.bot.send_message(
                    chat_id=self.settings.telegram_chat_id,
                    message_thread_id=thread_id,
                    text=(
                        f"⚡ runner {name!r} reconnected — session resumed.\n"
                        f"any in-flight turn was lost; re-send your last message if needed."
                    ),
                )
            except Exception:
                log.exception("bridge.reconnect_announce_failed", thread_id=thread_id)

    def _make_id_persister(
        self, thread_id: int
    ) -> Callable[[str], Awaitable[None]]:
        async def persist(sdk_session_id: str) -> None:
            await self.sessions.update_sdk_session_id(thread_id, sdk_session_id)

        return persist

    # ---- restore-on-boot ----------------------------------------------------

    async def expire_pending_approvals(self) -> int:
        """Called at boot: any pending permission rows in DB are leftovers
        from before the restart and the runner-side futures have already
        denied. Edit the messages to mark them expired."""
        return await self.permissions_ui.expire_orphans(self.settings.telegram_chat_id)

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

            # Use the runner the session was originally on. Falls back to
            # default if that runner is no longer registered (e.g. user did
            # /macs remove). The restore call will then either succeed on
            # default or fail and mark the session orphaned.
            runner_name = spec.runner_name
            if runner_name not in self.runners.names():
                log.warning(
                    "bridge.session_runner_missing_falling_back",
                    thread_id=spec.thread_id,
                    requested=runner_name,
                    fallback=self.default_runner,
                )
                runner_name = self.default_runner
            handle = await self.runners.get(runner_name).open_session(
                sid=str(spec.thread_id),
                cwd=spec.cwd,
                mode=spec.permission_mode,  # type: ignore[arg-type]
                resume=spec.sdk_session_id,
                model=spec.model,
                effort=spec.effort,
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
