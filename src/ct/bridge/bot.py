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
from aiogram.types import CallbackQuery, Message, TelegramObject

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

        self._router.message.register(
            self.on_topic_message,
            F.message_thread_id.is_not(None) & F.text.is_not(None) & ~F.text.startswith("/"),
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
        else:
            await message.answer(
                "usage:\n"
                "  /macs                       — list registered runners\n"
                "  /macs add NAME HOST [PORT]  — register a runner over Tailscale\n"
                "  /macs remove NAME           — drop a runner"
            )

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
        """Text-based list of profiles. /profiles add ... is /save."""
        rows = await self.db.list_profiles()
        if not rows:
            await message.answer(
                "no saved profiles.\n"
                "save one with: /save <name> [dir=...] [mac=...] [model=...] [effort=...] [mode=...]"
            )
            return
        lines = ["saved profiles:"]
        for r in rows:
            bits: list[str] = []
            if r.dir: bits.append(f"dir={r.dir}")
            if r.runner_name: bits.append(f"mac={r.runner_name}")
            if r.model: bits.append(f"model={r.model}")
            if r.effort: bits.append(f"effort={r.effort}")
            if r.permission_mode: bits.append(f"mode={r.permission_mode}")
            details = ", ".join(bits) or "(no fields set; uses defaults)"
            lines.append(f"  • {r.name}  ({details})")
        lines.append("\nstart one with: /new <name>")
        await message.answer("\n".join(lines))

    async def cmd_defaults(self, message: Message) -> None:
        """Show/set bot-wide defaults. Profiles + /new args layer on top."""
        if message.text is None:
            return
        tokens = message.text.strip().split()
        # /defaults with no args → show
        if len(tokens) == 1:
            d = await self.db.all_defaults()
            lines = [
                "bot defaults (used when /new and profile don't set a field):",
                f"  mac:    {d.get('default_runner_name') or '(none → studio)'}",
                f"  model:  {d.get('default_model') or '(SDK default)'}",
                f"  effort: {d.get('default_effort') or '(SDK default)'}",
                f"  mode:   {d.get('default_permission_mode') or 'acceptEdits'}",
                "",
                "set with: /defaults [mac=...] [model=...] [effort=...] [mode=...]",
                "unset:    /defaults model=null  (etc.)",
            ]
            await message.answer("\n".join(lines))
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
        """Run the same session-creation flow as cmd_new but driven by a button."""
        args = ParsedArgs(
            name=profile_name, cwd=None, mac=None, model=None, effort=None, mode=None
        )
        resolved = await self._resolve_for_new(args)
        cwd = Path(resolved.cwd).expanduser()
        runner_name = resolved.runner_name

        try:
            self.runners.get(runner_name)
        except KeyError:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=f"⚠ {runner_name!r} runner not registered. use /macs add",
            )
            return

        if runner_name == self.default_runner and not cwd.is_dir():
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=f"⚠ profile {profile_name!r}: dir does not exist on {runner_name}: {cwd}",
            )
            return

        try:
            thread_id = await create_topic(
                self.bot, self.settings.telegram_chat_id, profile_name
            )
        except TelegramBadRequest as exc:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=f"⚠ couldn't create topic: {exc!s}",
            )
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
            log.exception("session.open_from_profile_failed", profile=profile_name)
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=f"⚠ couldn't open session: {exc!s}",
            )
            return
        handle_box.append(handle)

        await self.sessions.add(
            TopicSession(
                thread_id=thread_id,
                project_name=profile_name,
                cwd=str(cwd),
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=runner_name,
                model=resolved.model,
                effort=resolved.effort,
            )
        )
        await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=thread_id,
            text=(
                f"✓ session ready (from profile: {profile_name})\n"
                f"cwd:    {cwd}\n"
                f"runner: {runner_name}\n"
                f"mode:   {resolved.permission_mode}\n"
                f"model:  {resolved.model or '(SDK default)'}\n"
                f"effort: {resolved.effort or '(SDK default)'}\n\n"
                f"Type a message to start, or /m for buttons."
            ),
        )

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
        if await self._handle_new_menu_callback(query):
            return
        await query.answer()

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
