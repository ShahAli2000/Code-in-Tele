"""Aiogram bot wiring: middleware (allowlist + chat-id), commands, message
routing, and inline-button callbacks.

Phase 2 onwards: every Claude session lives on a runner daemon (local or
remote), reached over WebSocket via RunnerPool / SessionHandle. The bridge
itself never imports the SDK directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    ForceReply,
    Message,
    TelegramObject,
)

from ct.bridge import menu as menu_ui
from ct.bridge import transcription
from ct.bridge.permissions_ui import PermissionsUI
from ct.bridge.runner_client import RunnerPool, SessionHandle
from ct.bridge.sessions import RestoreSpec, SessionStore, TopicSession
from ct.bridge.streaming import TopicRenderer
from ct.bridge.topics import GENERAL_TOPIC_ID, create_topic
from ct.config import Settings
from ct.dashboard.tunnel import TunnelManager, public_url_with_token
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
    # Free-form system-prompt fragment carried by the profile (when /new uses
    # one). Appended to Claude Code's default + git/test directives at session
    # open. None when not set or no profile was used.
    system_prompt: str | None = None
    # Bot-wide pre-trusted tools — every new session starts with these tool
    # names already in its remembered-allows set. Lives in defaults; per-
    # profile override is deferred.
    auto_allow_tools: tuple[str, ...] = ()
    # Adaptive thinking on by default. /think on|off changes the per-session
    # value; /defaults thinking=… can flip the bot-wide default later.
    thinking: bool = True


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
        self.runners.on_idle_reaped = self.on_runner_idle_reaped
        self.runners.on_pre_compact = self.on_runner_pre_compact
        # Wire the callback onto any RunnerConnections that already exist
        # (main.py adds the studio runner + DB-registered macs before
        # constructing the bridge, so they were created with on_reconnect=None).
        for _conn in self.runners._connections.values():  # type: ignore[attr-defined]
            _conn.on_reconnect = self.on_runner_reconnect
            _conn.on_idle_reaped = self.on_runner_idle_reaped
            _conn.on_pre_compact = self.on_runner_pre_compact
        self.default_runner = default_runner
        self.started_at = datetime.now(timezone.utc)
        self.sessions = SessionStore(db)
        self.permissions_ui = PermissionsUI(bot, db)
        # In-memory mirror of the meta defaults table. Populated on
        # restore_sessions() and kept in sync via cmd_quiet / cmd_defaults so
        # that hot-path checks (e.g. _is_quiet_now on every render) don't hit
        # SQLite per send.
        self._defaults_cache: dict[str, str | None] = {}
        # Cloudflared tunnel for the dashboard. Lazy-spawned on /tunnel on.
        self._tunnel = TunnelManager()
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
        # When a mac has shortcuts, the browse entry shows a picker before
        # drilling in. Keyed by the picker message_id; cleared when the user
        # taps a starting point.
        # Shape: {mac_name, show_hidden, paths: [main_dir, *shortcuts]}
        self._browse_starting_points: dict[int, dict[str, Any]] = {}
        # Single-step /undo. Holds a snapshot of the most recent destructive
        # action (close session, delete profile). Overwritten by each new
        # action — there's no stack. In-memory only; lost on restart, which
        # matches the safety-net intent ("I just made a mistake," not
        # "yesterday's mistake"). Shape: {"action": str, "performed_at": float
        # (monotonic), "payload": dict}.
        self._last_destructive: dict[str, Any] | None = None
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
        self._router.message.register(self.cmd_think, Command("think"))
        self._router.message.register(self.cmd_status, Command("status"))
        self._router.message.register(self.cmd_save, Command("save"))
        self._router.message.register(self.cmd_defaults, Command("defaults"))
        self._router.message.register(self.cmd_profiles, Command("profiles"))
        self._router.message.register(self.cmd_menu, Command("menu", "m"))
        self._router.message.register(self.cmd_fork, Command("fork"))
        self._router.message.register(self.cmd_restart, Command("restart"))
        self._router.message.register(self.cmd_undo, Command("undo"))
        self._router.message.register(self.cmd_move, Command("move"))
        self._router.message.register(self.cmd_resume, Command("resume"))
        self._router.message.register(self.cmd_logs, Command("logs"))
        self._router.message.register(self.cmd_export, Command("export"))
        self._router.message.register(self.cmd_get, Command("get"))
        self._router.message.register(self.cmd_quiet, Command("quiet"))
        self._router.message.register(self.cmd_search, Command("search"))
        self._router.message.register(self.cmd_stats, Command("stats"))
        self._router.message.register(self.cmd_context, Command("context"))
        self._router.message.register(self.cmd_rewind, Command("rewind"))
        self._router.message.register(self.cmd_allow, Command("allow"))
        self._router.message.register(self.cmd_tunnel, Command("tunnel"))
        self._router.message.register(self.cmd_help, Command("help", "start"))

        # Single text-message handler. Dispatches:
        #   - explicit replies to bot ForceReply prompts
        #   - per-user pending actions (no explicit reply needed)
        #   - falls through to topic-message logic for normal conversation
        self._router.message.register(
            self.on_text_message,
            F.text.is_not(None) & ~F.text.startswith("/"),
        )

        # Media handlers — photo, document, voice, audio. Each sends through
        # the same on_media_message; aiogram needs separate registrations
        # because F.photo / F.document / etc. don't combine cleanly with `|`.
        for media_filter in (F.photo, F.document, F.voice, F.audio):
            self._router.message.register(self.on_media_message, media_filter)

        self._router.callback_query.register(self.on_callback)

        # Anything a handler raises lands here. Log it + DM the admin so a
        # silent failure doesn't sit in the log file un-noticed.
        self._router.errors.register(self.on_handler_error)

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
            "  /think on|off        — toggle adaptive thinking for this session\n"
            "  /close               — close this topic's session\n"
            "  /fork [name]         — branch this session into a new topic\n"
            "  /restart             — reconnect SDK to this transcript (unstick)\n"
            "  /undo                — reverse last destructive action (close, profile delete) within 30 min\n"
            "  /move mac=NAME       — migrate this session to another runner (transcript preserved)\n"
            "  /resume              — re-attach orphaned sessions (mac was offline at boot)\n"
            "  /logs [N]            — show last N transcript entries (default 15)\n"
            "  /export              — full transcript as a markdown file\n"
            "  /get <path>          — download a file from this session's runner\n"
            "  /quiet [HH:MM HH:MM] — silence non-permission pings during a window\n"
            "  /search <pattern>    — substring search across all past transcripts\n"
            "  /stats               — turns, tool use, active sessions (from message_log)\n"
            "  /allow               — bot-wide auto-allow tool list (skip approval cards)\n"
            "  /tunnel [on|off]     — expose the local dashboard via cloudflared\n"
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
                "  /macs                                       — list registered runners\n"
                "  /macs add NAME HOST [PORT]                  — register a runner\n"
                "  /macs remove NAME                           — drop a runner\n"
                "  /macs config NAME main_dir=<path>           — set the browse root\n"
                "  /macs config NAME add_shortcut=<path>       — add a browse shortcut\n"
                "  /macs config NAME rm_shortcut=<path>        — remove a shortcut\n"
                "  /macs config NAME show                      — show current config"
            )

    async def _macs_config(self, message: Message, rest_tokens: list[str]) -> None:
        if not rest_tokens:
            await message.answer(
                "usage: /macs config NAME [main_dir=<path>] "
                "[add_shortcut=<path>] [rm_shortcut=<path>] [show]"
            )
            return
        name = rest_tokens[0]
        if name not in self.runners.names() and not await self.db.get_mac(name):
            await message.answer(f"⚠ no mac registered as {name!r}")
            return
        mac_row = await self.db.get_mac(name)
        if mac_row is None:
            # Studio-only edge case: insert minimal row
            conn = self.runners.get(name)
            await self.db.insert_mac(name, conn.host, conn.port)
            mac_row = await self.db.get_mac(name)

        new_main_dir: str | None = None
        add_shortcut: str | None = None
        rm_shortcut: str | None = None
        show_only = False
        for tok in rest_tokens[1:]:
            if tok.startswith("main_dir="):
                new_main_dir = tok[len("main_dir="):]
            elif tok.startswith("add_shortcut="):
                add_shortcut = tok[len("add_shortcut="):]
            elif tok.startswith("rm_shortcut="):
                rm_shortcut = tok[len("rm_shortcut="):]
            elif tok == "show":
                show_only = True

        if show_only and not (new_main_dir or add_shortcut or rm_shortcut):
            await self._macs_config_show(message, name, mac_row)
            return

        changes: list[str] = []
        # main_dir
        if new_main_dir is not None:
            if new_main_dir.lower() in ("null", "none", ""):
                await self.db.update_mac_main_dir(name, None)
                changes.append("main_dir cleared (uses $HOME)")
            else:
                await self.db.update_mac_main_dir(name, new_main_dir)
                changes.append(f"main_dir = {new_main_dir}")
        # shortcuts: add/rm operate on the current list (re-read after main_dir
        # update is fine — we don't read main_dir back from mac_row).
        current_shortcuts = list(mac_row.shortcuts if mac_row else [])
        if add_shortcut:
            if add_shortcut not in current_shortcuts:
                current_shortcuts.append(add_shortcut)
                await self.db.update_mac_shortcuts(name, current_shortcuts)
                changes.append(f"+ shortcut: {add_shortcut}")
            else:
                changes.append(f"shortcut already exists: {add_shortcut}")
        if rm_shortcut:
            if rm_shortcut in current_shortcuts:
                current_shortcuts.remove(rm_shortcut)
                await self.db.update_mac_shortcuts(name, current_shortcuts)
                changes.append(f"- shortcut: {rm_shortcut}")
            else:
                changes.append(f"shortcut not found: {rm_shortcut}")

        if not changes:
            await message.answer(
                "⚠ nothing to change. /macs config NAME show prints current."
            )
            return
        await message.answer(f"✓ {name}:\n" + "\n".join(f"  {c}" for c in changes))

    async def _macs_config_show(
        self, message: Message, name: str, mac_row: Any | None,
    ) -> None:
        if mac_row is None:
            await message.answer(f"⚠ {name!r} has no row in the macs table yet")
            return
        lines = [f"⚙️  {name}"]
        lines.append(f"  main_dir:  {mac_row.main_dir or '(default $HOME)'}")
        if mac_row.shortcuts:
            lines.append("  shortcuts:")
            for s in mac_row.shortcuts:
                lines.append(f"    • {s}")
        else:
            lines.append("  shortcuts: (none)")
        await message.answer("\n".join(lines))

    async def _macs_list(self, message: Message) -> None:
        names = self.runners.names()
        if not names:
            await message.answer("no runners registered.")
            return
        import time as _time
        now = _time.time()
        lines = ["registered runners:"]
        for n in names:
            conn = self.runners.get(n)
            # Status emoji is set from the health daemon's cached pings rather
            # than just `conn.connected`, so a runner whose WS is alive but
            # whose pings are stalling shows up 🟡 instead of falsely 🟢.
            status = self._mac_status_label(conn, now)
            lines.append(f"  {status}  {n}  ({conn.host}:{conn.port})")
        await message.answer("\n".join(lines))

    @staticmethod
    def _mac_status_label(conn, now: float) -> str:
        """🟢 connected + ping ok ≤90s, 🟡 ping stale or never seen yet,
        🔴 WS not connected. ⚪ used while the first ping is still in flight
        right after registration."""
        if not conn.connected:
            return "🔴"
        last = conn.last_ping_ok_ts
        if last is None:
            return "⚪"
        age = now - last
        if age <= 90:
            ms = conn.last_ping_ms or 0.0
            return f"🟢 ({ms:.0f}ms)"
        if age <= 300:
            return f"🟡 ({int(age)}s ago)"
        return f"🔴 (no reply >{int(age)}s)"

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
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await message.answer(
            f"🛑 remove runner {name!r}?\n\n"
            f"this disconnects the WebSocket and drops the row from the DB. "
            f"any sessions whose runner_name = {name!r} (currently none) "
            f"would be orphaned. you can re-add later with /macs add.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✓ yes, remove",
                    callback_data=f"ct:mr:yes:{name}",
                ),
                InlineKeyboardButton(text="✗ cancel", callback_data="ct:mr:no"),
            ]]),
        )

    async def _handle_macs_remove_callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith("ct:mr:"):
            return False
        rest = data[len("ct:mr:") :]
        if rest == "no":
            await menu_ui._safe_edit(
                self.bot, query, text="✗ remove cancelled", keyboard=None,
            )
            await query.answer("cancelled")
            return True
        if rest.startswith("yes:"):
            name = rest[len("yes:"):]
            if name == self.default_runner:
                await query.answer("can't remove the local runner")
                return True
            # Re-check guards (state may have changed between prompt and tap)
            bound = [s for s in self.sessions.all() if s.runner_name == name]
            if bound:
                await menu_ui._safe_edit(
                    self.bot, query,
                    text=f"⚠ {name!r} has active sessions now; /close first.",
                    keyboard=None,
                )
                await query.answer()
                return True
            try:
                conn = self.runners.get(name)
            except KeyError:
                removed_db = await self.db.remove_mac(name)
                msg = (
                    f"✓ {name} removed (was in DB but not connected)"
                    if removed_db else f"⚠ no runner registered as {name!r}"
                )
                await menu_ui._safe_edit(self.bot, query, text=msg, keyboard=None)
                await query.answer()
                return True
            await conn.close()
            self.runners._connections.pop(name, None)  # type: ignore[attr-defined]
            await self.db.remove_mac(name)
            await menu_ui._safe_edit(
                self.bot, query,
                text=f"✓ {name} disconnected and removed",
                keyboard=None,
            )
            await query.answer("removed")
            return True
        await query.answer()
        return True

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
        # Auto-allow tools is a bot-wide default for now (no profile override).
        auto_allow_csv = (defaults.get("default_auto_allow_tools") or "").strip()
        auto_allow = tuple(
            t.strip() for t in auto_allow_csv.split(",") if t.strip()
        )
        return ResolvedSession(
            name=args.name,
            cwd=cwd,
            runner_name=runner_name,
            model=model,
            effort=effort,
            permission_mode=mode,
            system_prompt=profile.system_prompt if profile else None,
            auto_allow_tools=auto_allow,
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
                thinking=resolved.thinking,
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
                thinking=resolved.thinking,
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

    async def cmd_think(self, message: Message) -> None:
        """/think on | off | status — toggle adaptive thinking for this session.

        Default for new sessions is ON. Toggle takes effect immediately by
        reconnecting the SDK with resume=<sdk_session_id> — the conversation
        continues from the same transcript."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/think only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        if message.text is None:
            return
        parts = message.text.strip().split(maxsplit=1)
        verb = parts[1].strip().lower() if len(parts) >= 2 else "status"
        if verb in ("status", "show"):
            state = "on (adaptive)" if session.thinking else "off"
            await message.answer(
                f"thinking: {state}\n"
                f"usage: /think on | off"
            )
            return
        if verb not in ("on", "off"):
            await message.answer("usage: /think on | off | status")
            return
        target = (verb == "on")
        if target == session.thinking:
            await message.answer(
                f"thinking already {'on' if target else 'off'} for this session."
            )
            return
        # Persist first so a crash mid-restart still picks up the new value
        # on next boot. Then live-reconnect the SDK so the change applies now.
        session.thinking = target
        await self.db.update_session_thinking(session.thread_id, target)
        await message.answer(
            f"⏳ thinking → {'on' if target else 'off'} — restarting session…"
        )
        try:
            await self._restart_session(session)
        except Exception as exc:
            log.exception("think.restart_failed", thread_id=session.thread_id)
            await message.answer(f"⚠ restart failed: {exc!s}")
            return
        await message.answer(
            f"✓ thinking {'on' if target else 'off'} — transcript intact, ready for next turn."
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
            self._defaults_cache[key] = v
            changes.append(f"  {name} → {v or '(none)'}")
        if not changes:
            await message.answer(
                "⚠ nothing to change. /defaults shows current; pass key=value to set."
            )
            return
        await message.answer("✓ defaults updated:\n" + "\n".join(changes))

    async def cmd_allow(self, message: Message) -> None:
        """Show or set the bot-wide auto-allow tool list. Tools in this set
        are pre-approved at session start — no permission card the first time
        they're used. Affects future sessions; current sessions keep whatever
        their `_remembered_allows` already contains.

        Usage:
            /allow                     — show current
            /allow Read,Glob,Grep      — set CSV
            /allow off                 — clear
            /allow add Bash            — add one tool
            /allow rm Bash             — remove one tool
        """
        text = (message.text or "").strip()
        parts = text.split()
        current_csv = (self._defaults_cache.get("default_auto_allow_tools") or "").strip()
        current_set: set[str] = set(
            t.strip() for t in current_csv.split(",") if t.strip()
        )

        if len(parts) == 1:
            shown = ", ".join(sorted(current_set)) if current_set else "(none)"
            await message.answer(
                f"🔓 auto-allow tools (bot-wide)\n"
                f"current: {shown}\n\n"
                f"set:    /allow Read,Glob,Grep\n"
                f"add:    /allow add Bash\n"
                f"rm:     /allow rm Bash\n"
                f"clear:  /allow off\n\n"
                f"applies to NEW sessions; current sessions keep their own ledger."
            )
            return

        if len(parts) == 2 and parts[1].lower() == "off":
            new_set: set[str] = set()
        elif len(parts) == 3 and parts[1].lower() == "add":
            new_set = current_set | {parts[2]}
        elif len(parts) == 3 and parts[1].lower() == "rm":
            new_set = current_set - {parts[2]}
        elif len(parts) == 2 and parts[1] not in {"add", "rm", "off"}:
            # Treat as full CSV replacement
            new_set = {t.strip() for t in parts[1].split(",") if t.strip()}
        else:
            await message.answer(
                "usage: /allow [CSV | add NAME | rm NAME | off]"
            )
            return

        new_csv = ",".join(sorted(new_set)) if new_set else None
        await self.db.set_default("default_auto_allow_tools", new_csv)
        self._defaults_cache["default_auto_allow_tools"] = new_csv
        shown = ", ".join(sorted(new_set)) if new_set else "(none)"
        await message.answer(f"✓ auto-allow now: {shown}")

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
        self, resolved: "ResolvedSession", *, source_label: str = "",
        resume: str | None = None,
    ) -> int | None:
        """Common path: validate runner + cwd, create topic, open session, add
        to store, post ready card. Returns the new thread_id or None on failure
        (errors are surfaced via Telegram messages).

        If `resume` is set, the SDK reattaches to that session id (used by /fork
        to bring a forked transcript to life)."""
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
                thinking=resolved.thinking,
                resume=resume,
                system_prompt=resolved.system_prompt,
                auto_allow_tools=list(resolved.auto_allow_tools),
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
                thinking=resolved.thinking,
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

    # ---- quiet hours ------------------------------------------------------

    @staticmethod
    def _parse_hhmm(s: str) -> dt_time | None:
        s = s.strip()
        if not s or ":" not in s:
            return None
        try:
            h, m = s.split(":", 1)
            return dt_time(hour=int(h), minute=int(m))
        except (ValueError, TypeError):
            return None

    def _is_quiet_now(self) -> bool:
        """Cheap synchronous check used by TopicRenderer on every send.
        Reads cached defaults populated from the DB at startup. Returns False
        if quiet hours aren't configured. Local-time semantics: an `start`
        later than `end` (e.g. 22:00..08:00) means "overnight"."""
        start = self._parse_hhmm(self._defaults_cache.get("quiet_hours_start") or "")
        end = self._parse_hhmm(self._defaults_cache.get("quiet_hours_end") or "")
        if start is None or end is None:
            return False
        now = datetime.now().time()
        if start <= end:
            return start <= now < end
        # Overnight window
        return now >= start or now < end

    async def cmd_quiet(self, message: Message) -> None:
        """Show or set quiet hours window. Usage:
            /quiet                   — show current setting
            /quiet 22:00 08:00       — set 10pm to 8am (overnight)
            /quiet off               — disable
        During quiet hours, non-permission messages are sent silently
        (Telegram's disable_notification). Permission cards still ping."""
        text = (message.text or "").strip()
        parts = text.split()
        if len(parts) == 1:
            start = self._defaults_cache.get("quiet_hours_start") or "(unset)"
            end = self._defaults_cache.get("quiet_hours_end") or "(unset)"
            active = "active now" if self._is_quiet_now() else "inactive now"
            await message.answer(
                f"🌙 quiet hours\n"
                f"start: {start}\n"
                f"end:   {end}\n"
                f"({active})\n"
                f"(times are local to the bridge host — Mac Studio)\n\n"
                f"set:    /quiet 22:00 08:00\n"
                f"clear:  /quiet off"
            )
            return
        if len(parts) == 2 and parts[1].lower() == "off":
            await self.db.set_default("quiet_hours_start", None)
            await self.db.set_default("quiet_hours_end", None)
            self._defaults_cache["quiet_hours_start"] = None
            self._defaults_cache["quiet_hours_end"] = None
            await message.answer("✓ quiet hours disabled")
            return
        if len(parts) == 3:
            start = self._parse_hhmm(parts[1])
            end = self._parse_hhmm(parts[2])
            if start is None or end is None:
                await message.answer("⚠ format: HH:MM HH:MM (e.g. 22:00 08:00)")
                return
            await self.db.set_default("quiet_hours_start", parts[1])
            await self.db.set_default("quiet_hours_end", parts[2])
            self._defaults_cache["quiet_hours_start"] = parts[1]
            self._defaults_cache["quiet_hours_end"] = parts[2]
            await message.answer(f"✓ quiet hours set: {parts[1]} → {parts[2]}")
            return
        await message.answer(
            "usage: /quiet [HH:MM HH:MM | off]\n"
            "current: /quiet"
        )

    async def cmd_stats(self, message: Message) -> None:
        """Aggregate counts from message_log: turns, top tools, per-session
        activity. Empty if message_log was added recently and no turns have
        run since."""
        s = await self.db.stats_overview()
        if s["total_events"] == 0:
            await message.answer(
                "📊 stats\n\n"
                "no events logged yet. message_log fills as turns run."
            )
            return
        lines = [
            "📊 stats",
            f"total events:  {s['total_events']:,}",
            f"last 24h:      {s['recent_24h']:,}",
            "",
            "by kind:",
        ]
        # Stable order so the eye lands on the same row each time
        kind_order = ["user_msg", "assistant_text", "tool_use", "tool_result", "turn_end"]
        for k in kind_order:
            if k in s["by_kind"]:
                lines.append(f"  {k:<16} {s['by_kind'][k]:,}")
        # Anything else
        for k, v in s["by_kind"].items():
            if k not in kind_order:
                lines.append(f"  {k:<16} {v:,}")

        if s["top_tools"]:
            lines.append("")
            lines.append("top tools:")
            for name, count in s["top_tools"]:
                lines.append(f"  {name:<20} {count:,}")

        if s["by_session"]:
            lines.append("")
            lines.append("most active sessions:")
            # Resolve thread_id → project_name when known
            for tid, count, ts in s["by_session"][:10]:
                topic = self.sessions.get(tid)
                tag = f"{topic.project_name}" if topic else f"thread:{tid}"
                # Trim ts to "YYYY-MM-DD HH:MM" for screen real estate
                ts_short = ts[:16] if isinstance(ts, str) else "?"
                lines.append(f"  {tag:<24} {count:>5}  last: {ts_short}")

        await message.answer("\n".join(lines))

    # ---- web dashboard tunnel --------------------------------------------

    async def cmd_tunnel(self, message: Message) -> None:
        """/tunnel on | off | status — control the cloudflared subprocess
        that exposes the local dashboard to the public internet via a
        trycloudflare.com URL.

        Quick Tunnels are anonymous + ephemeral. The token in the URL is
        the only auth — guard the link like a password."""
        text = (message.text or "").strip().split()
        verb = text[1].lower() if len(text) >= 2 else "status"

        if verb == "status":
            await message.answer(self._tunnel.status_text())
            return

        if verb in ("on", "start"):
            if self._tunnel.is_running:
                await message.answer(self._tunnel.status_text())
                return
            await message.answer("⏳ starting tunnel …")
            try:
                public = await self._tunnel.start(
                    local_port=self.settings.dashboard_port,
                    local_host=self._defaults_cache.get("dashboard_host") or "127.0.0.1",
                )
            except Exception as exc:
                log.exception("tunnel.start_failed")
                await message.answer(f"⚠ tunnel failed: {exc!s}")
                return
            token = self._defaults_cache.get("dashboard_token") or ""
            url_with_token = public_url_with_token(public, token) or public
            await message.answer(
                f"🌐 tunnel up\n{public}\n\n"
                f"with token (open this):\n{url_with_token}\n\n"
                f"⚠ this URL is publicly reachable; the token is the only auth."
            )
            return

        if verb in ("off", "stop"):
            if not self._tunnel.is_running:
                await message.answer("💤 tunnel was already off.")
                return
            await self._tunnel.stop()
            await message.answer("💤 tunnel stopped.")
            return

        await message.answer("usage: /tunnel [on | off | status]")

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

    # /context warns at this fill ratio (0–1.0). Below this is fine.
    CONTEXT_WARN_RATIO = 0.80

    async def cmd_context(self, message: Message) -> None:
        """/context — show how full the SDK's context window is for this
        topic's session. Uses ClaudeSDKClient.get_context_usage() under the
        hood. Per-session — must be invoked inside a session topic."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/context only works inside a session topic.")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic — /new to start one.")
            return
        try:
            usage = await session.runner.get_context_usage()
        except Exception as exc:
            await message.answer(f"⚠ context query failed: {exc}")
            return
        total = int(usage.get("totalTokens", 0))
        eff_max = int(usage.get("maxTokens", 0))
        raw_max = int(usage.get("rawMaxTokens", eff_max or 0))
        pct = float(usage.get("percentage", 0.0))
        model = str(usage.get("model", "?"))
        warn = "  ⚠️  approaching limit — expect autocompact soon" if pct >= self.CONTEXT_WARN_RATIO * 100 else ""
        lines = [
            f"📊 context for {session.project_name!r}",
            f"  model:    {model}",
            f"  tokens:   {total:,} / {eff_max:,} ({pct:.1f}%){warn}",
        ]
        if raw_max and raw_max != eff_max:
            lines.append(f"  raw_max:  {raw_max:,}  (effective max reduced by autocompact buffer)")
        cats = usage.get("categories") or []
        if cats:
            lines.append("  by category:")
            for cat in cats:
                if not isinstance(cat, dict):
                    continue
                name = cat.get("name", "?")
                tk = int(cat.get("tokens", 0))
                lines.append(f"    • {name}: {tk:,}")
        await message.answer("\n".join(lines))

    async def cmd_rewind(self, message: Message) -> None:
        """/rewind — restore tracked file state to the most recent user
        message in this session. Different from /undo (which reverses
        action-level destructive ops like close/delete-profile). /rewind
        operates on actual on-disk file changes from the session's Edit /
        Write tools, and survives bridge restarts (state lives in the SDK's
        on-disk checkpoint store)."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/rewind only works inside a session topic.")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic — /new to start one.")
            return
        async with session.turn_lock:
            try:
                target = await session.runner.rewind_files()
            except Exception as exc:
                msg = str(exc)
                if "no_checkpoint" in msg:
                    await message.answer(
                        "↶ nothing to rewind — no prior turn checkpoint yet "
                        "(send a message first, then /rewind reverses Claude's "
                        "file changes since)."
                    )
                else:
                    await message.answer(f"⚠ rewind failed: {exc}")
                return
        await message.answer(
            f"↶ files rewound to state at user message `{target[:8]}…` — "
            "send a new message to continue."
        )

    async def cmd_search(self, message: Message) -> None:
        """Fan-out substring search across all runners' transcripts. Anywhere
        the pattern appeared in a user/assistant message, surface a snippet
        and (when known) the bridge topic name. Usage: /search <pattern>"""
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("usage: /search <pattern>\nmatches anywhere in past transcripts.")
            return
        pattern = parts[1].strip()
        await message.answer(f"🔎 searching for {pattern!r} across {len(self.runners.names())} runner(s)…")

        # Fan out — every runner searches its own ~/.claude/projects in parallel.
        async def _one(name: str) -> tuple[str, list[dict]]:
            try:
                return name, await self.runners.get(name).search(pattern=pattern)
            except Exception as exc:
                log.warning("search.runner_failed", name=name, error=str(exc))
                return name, []

        runner_names = self.runners.names()
        results = await asyncio.gather(*[_one(n) for n in runner_names])

        # Cross-reference matches against known sessions so we can show the
        # project name + topic link where possible.
        known_by_sdk_id: dict[str, TopicSession] = {}
        for s in self.sessions.all():
            sid = s.runner.session_id
            if sid:
                known_by_sdk_id[sid] = s

        total = sum(len(matches) for _, matches in results)
        if total == 0:
            await message.answer(f"no matches for {pattern!r}.")
            return

        lines = [f"🔎 {total} match(es) for {pattern!r}:"]
        for runner_name, matches in results:
            if not matches:
                continue
            for m in matches:
                sdk_id = m.get("sdk_session_id", "")
                role = m.get("role", "?")
                snippet = m.get("snippet", "")
                topic = known_by_sdk_id.get(sdk_id)
                tag = "👤" if role == "user" else "🤖"
                if topic is not None:
                    header = f"\n📁 {topic.project_name} ({runner_name}) {tag} {role}"
                else:
                    header = f"\n📁 (closed/unknown) ({runner_name}) {tag} {role}"
                lines.append(header)
                lines.append(snippet)
        body = "\n".join(lines)
        if len(body) <= 3500:
            await message.answer(body)
        else:
            from aiogram.types import BufferedInputFile
            await message.answer_document(
                BufferedInputFile(body.encode("utf-8"), filename=f"search-{pattern[:20]}.txt"),
                caption=f"🔎 {total} matches for {pattern!r}",
            )

    async def cmd_get(self, message: Message) -> None:
        """Pull a file off the runner and ship it to Telegram as an attachment.
        Usage: /get <path>. Path is resolved on the session's runner (so a
        laptop session reads from the laptop's filesystem)."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/get only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("usage: /get <path>\n(absolute path, or relative to the session's cwd)")
            return
        path_arg = parts[1].strip()
        # Relative paths resolve against the session's cwd.
        if not path_arg.startswith("/") and not path_arg.startswith("~"):
            path_arg = str(Path(session.cwd) / path_arg)
        await message.answer(f"⏳ fetching {path_arg} from {session.runner_name} …")
        try:
            resolved, content = await self.runners.get(session.runner_name).get_file(
                path_arg
            )
        except Exception as exc:
            log.exception("session.get_file_failed", thread_id=session.thread_id)
            await message.answer(f"⚠ couldn't fetch: {exc!s}")
            return
        from aiogram.types import BufferedInputFile
        filename = Path(resolved).name or "file"
        await message.answer_document(
            BufferedInputFile(content, filename=filename),
            caption=f"📥 {resolved} ({len(content)} bytes)",
        )

    async def cmd_export(self, message: Message) -> None:
        """Send the full session transcript as a markdown file. Uses the
        runner's on-disk .jsonl as source — survives bridge restarts."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/export only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        sdk_id = session.runner.session_id
        if not sdk_id:
            await message.answer(
                "⚠ this session has no transcript yet — send a message first."
            )
            return
        await message.answer("⏳ rendering transcript …")
        try:
            md = await self.runners.get(session.runner_name).export_transcript(
                sdk_session_id=sdk_id, cwd=session.cwd,
            )
        except Exception as exc:
            log.exception("session.export_failed", thread_id=session.thread_id)
            await message.answer(f"⚠ export failed: {exc!s}")
            return
        from aiogram.types import BufferedInputFile
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        filename = f"{session.project_name}-{ts}.md"
        await message.answer_document(
            BufferedInputFile(md.encode("utf-8"), filename=filename),
            caption=f"📦 transcript export — {session.project_name}",
        )

    async def cmd_logs(self, message: Message) -> None:
        """Show the last N transcript entries for this session, read off-disk
        from the SDK's .jsonl file. Usage: /logs [N] (default 15)."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/logs only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        sdk_id = session.runner.session_id
        if not sdk_id:
            await message.answer(
                "⚠ this session has no transcript yet — send a message first."
            )
            return

        # Optional limit arg
        text = (message.text or "").strip()
        parts = text.split()
        limit = 15
        if len(parts) >= 2:
            try:
                limit = max(1, min(int(parts[1]), 100))
            except ValueError:
                pass

        try:
            entries = await self.runners.get(session.runner_name).get_logs(
                sdk_session_id=sdk_id, cwd=session.cwd, limit=limit,
            )
        except Exception as exc:
            log.exception("session.logs_failed", thread_id=session.thread_id)
            await message.answer(f"⚠ couldn't fetch logs: {exc!s}")
            return

        if not entries:
            await message.answer("(transcript is empty — no surfaced messages yet.)")
            return

        lines = [f"📜 last {len(entries)} entries:"]
        for role, text_body in entries:
            tag = "👤" if role == "user" else "🤖"
            lines.append(f"\n{tag} {role}\n{text_body}")
        body = "\n".join(lines)

        # Spill to a document if it's too long for one Telegram message.
        if len(body) <= 3500:
            await message.answer(body)
        else:
            from aiogram.types import BufferedInputFile
            await message.answer_document(
                BufferedInputFile(
                    body.encode("utf-8"),
                    filename=f"logs-{session.project_name}.txt",
                ),
                caption=f"📜 last {len(entries)} entries (sent as file)",
            )

    async def cmd_fork(self, message: Message) -> None:
        """Branch this topic's session into a new topic with the same transcript
        and settings. Useful for trying a different approach without losing the
        original conversation. Usage: /fork [new-name]"""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/fork only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        sdk_id = session.runner.session_id
        if not sdk_id:
            await message.answer(
                "⚠ this session has no sdk_session_id yet — send at least one "
                "message before forking so the transcript exists on disk."
            )
            return

        # Optional name arg; default = "<orig>-fork".
        text = (message.text or message.caption or "").strip()
        parts = text.split(maxsplit=1)
        new_name = parts[1].strip() if len(parts) > 1 else f"{session.project_name}-fork"

        await message.answer(f"⏳ forking {session.project_name!r} → {new_name!r} …")

        try:
            new_sdk_id = await self.runners.get(session.runner_name).fork_session(
                sdk_session_id=sdk_id,
                cwd=session.cwd,
                title=new_name,
            )
        except Exception as exc:
            log.exception(
                "session.fork_failed",
                thread_id=session.thread_id,
                sdk_session_id=sdk_id,
            )
            await message.answer(f"⚠ fork failed: {exc!s}")
            return

        # Inherit everything from the source session — same mac, cwd, model,
        # effort, mode. New name and new sdk_session_id (resume target).
        # Re-fetch the live runner's current permission_mode in case it was
        # changed since /new.
        permission_mode = session.runner.permission_mode
        resolved = ResolvedSession(
            name=new_name,
            cwd=session.cwd,
            runner_name=session.runner_name,
            model=session.model,
            effort=session.effort,
            permission_mode=permission_mode,
            thinking=session.thinking,
        )
        await self._execute_new_session(
            resolved,
            source_label=f"fork of {session.project_name}",
            resume=new_sdk_id,
        )

    async def cmd_close(self, message: Message) -> None:
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/close only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        # Confirm before tearing down — destructive (kills the live SDK
        # connection; the topic stays but talking in it needs a /new).
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await message.answer(
            f"🛑 close session {session.project_name!r}?\n\n"
            f"the topic will stay in Telegram with its history, but "
            f"you'd need /new to start a new session in it.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✓ yes, close",
                    callback_data=f"ct:cc:yes:{session.thread_id}",
                ),
                InlineKeyboardButton(text="✗ cancel", callback_data="ct:cc:no"),
            ]]),
        )

    # Tunable: how long after a destructive action /undo can still reverse it.
    # 30 min is long enough to catch "I just made a mistake" while short
    # enough to avoid acting on stale state.
    UNDO_TTL_S = 1800

    def _record_destructive(self, action: str, payload: dict[str, Any]) -> None:
        """Capture the most recent destructive action so /undo can reverse it.
        Single-slot cache — older entries get overwritten. Keep payloads small
        and self-contained (no live references to TopicSession etc.)."""
        import time as _time
        self._last_destructive = {
            "action": action,
            "payload": payload,
            "performed_at": _time.monotonic(),
        }

    def _consume_destructive(self) -> dict[str, Any] | None:
        """Pop the recorded action if it's still within the TTL window. None
        if nothing's been recorded since boot, or it's older than UNDO_TTL_S."""
        import time as _time
        record = self._last_destructive
        if record is None:
            return None
        if _time.monotonic() - record["performed_at"] > self.UNDO_TTL_S:
            self._last_destructive = None
            return None
        self._last_destructive = None
        return record

    async def cmd_undo(self, message: Message) -> None:
        """/undo — reverse the most recent destructive action (close session,
        delete profile) within the last 30 min. Single-step only; no stack."""
        record = self._consume_destructive()
        if record is None:
            await message.answer(
                "🤷 nothing to undo (or the last action is older than 30 min)."
            )
            return
        action = record["action"]
        payload = record["payload"]
        try:
            if action == "close":
                await self._undo_close(payload)
                await message.answer(
                    f"✓ session {payload['project_name']!r} reopened on "
                    f"{payload['runner_name']} — transcript intact."
                )
            elif action == "profile_delete":
                await self.db.upsert_profile(
                    name=payload["name"],
                    dir=payload.get("dir"),
                    runner_name=payload.get("runner_name"),
                    model=payload.get("model"),
                    effort=payload.get("effort"),
                    permission_mode=payload.get("permission_mode"),
                )
                # system_prompt is a separate write because upsert_profile
                # doesn't take it (set via the menu's edit flow).
                if payload.get("system_prompt"):
                    await self.db.update_profile_system_prompt(
                        payload["name"], payload["system_prompt"],
                    )
                await message.answer(f"✓ profile {payload['name']!r} restored.")
            else:
                await message.answer(f"⚠ unknown undoable action: {action!r}")
        except Exception as exc:
            log.exception("undo.failed", action=action)
            await message.answer(f"⚠ undo failed: {exc!s}")

    async def _undo_close(self, payload: dict[str, Any]) -> None:
        """Reopen a previously-closed session on its original runner with
        resume=<sdk_session_id>. The DB row was marked closed; flip it to
        active and re-attach a SessionHandle. The TopicSession dataclass is
        rebuilt from the snapshot (in-memory state was dropped on close)."""
        thread_id = payload["thread_id"]
        sdk_id = payload.get("sdk_session_id")
        runner_name = payload["runner_name"]
        # Make sure the runner is still registered and reachable.
        try:
            self.runners.get(runner_name)
        except KeyError as exc:
            raise RuntimeError(
                f"runner {runner_name!r} is no longer registered"
            ) from exc

        handle_box: list[SessionHandle] = []

        async def perm_handler(req: PermissionRequest) -> None:
            await self.permissions_ui.render_card(
                runner=handle_box[0],
                chat_id=self.settings.telegram_chat_id,
                thread_id=thread_id,
                request=req,
            )

        handle = await self.runners.get(runner_name).open_session(
            sid=str(thread_id),
            cwd=payload["cwd"],
            mode=payload["permission_mode"],  # type: ignore[arg-type]
            model=payload.get("model"),
            effort=payload.get("effort"),
            thinking=payload.get("thinking", True),
            resume=sdk_id,
            on_permission_request=perm_handler,
            on_session_id_assigned=self._make_id_persister(thread_id),
        )
        handle_box.append(handle)
        # Re-add to in-memory map + flip the DB row back to active.
        await self.sessions.add(
            TopicSession(
                thread_id=thread_id,
                project_name=payload["project_name"],
                cwd=payload["cwd"],
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=runner_name,
                model=payload.get("model"),
                effort=payload.get("effort"),
                thinking=payload.get("thinking", True),
            )
        )

    async def cmd_resume(self, message: Message) -> None:
        """/resume —
        - In a session topic with an orphaned row → re-attach that session.
        - In a session topic with an active row   → "already active".
        - In General (or topic with no row)       → picker of orphaned sessions.

        A session is orphaned when its runner was unreachable at bridge boot,
        when /idle-reaped, or when /move failed mid-flight. The DB row keeps
        `runner_name` and `sdk_session_id` so we re-open with resume= once
        the runner is back online.
        """
        thread_id = message.message_thread_id
        in_topic = thread_id is not None and thread_id != GENERAL_TOPIC_ID

        # In-topic: skip the picker and resume THIS topic's session.
        if in_topic:
            row = await self.db.get_session(thread_id)
            if row is None:
                await message.answer(
                    "no session in this topic — use /new <name> in General to start one."
                )
                return
            if row.state == "active":
                await message.answer(
                    "✓ session already active — no resume needed."
                )
                return
            if row.state != "orphaned":
                await message.answer(
                    f"session state is {row.state!r}, can't resume "
                    f"(only `orphaned` sessions can /resume)."
                )
                return
            err = await self._resume_orphaned_row(row, thread_id)
            if err is None:
                await message.answer(
                    f"✓ resumed {row.project_name!r} — transcript intact, "
                    "ready for next turn."
                )
            else:
                await message.answer(f"⚠ resume failed: {err}")
            return

        # General (or unknown topic): picker.
        rows = await self.db.list_orphaned_sessions()
        if not rows:
            await message.answer("✓ no orphaned sessions.")
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        lines = ["orphaned sessions (tap to resume):"]
        buttons: list[list[InlineKeyboardButton]] = []
        for row in rows:
            # 🟢 if its runner is currently connected (resume should succeed),
            # 🔴 otherwise (the user can still tap; we'll surface the failure).
            try:
                conn = self.runners.get(row.runner_name)
                emoji = "🟢" if conn.connected else "🔴"
            except KeyError:
                emoji = "❌"  # runner no longer registered — unrecoverable
            sdk_short = (row.sdk_session_id or "")[:8] or "(no sdk id)"
            lines.append(
                f"  {emoji} {row.project_name} · {row.runner_name} · sdk={sdk_short}"
            )
            buttons.append([InlineKeyboardButton(
                text=f"↻ resume {row.project_name}",
                callback_data=f"ct:rsm:{row.thread_id}",
            )])
        await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def _resume_orphaned_row(
        self, row: Any, thread_id: int
    ) -> str | None:
        """Re-open the SDK session described by `row` with resume=. Returns
        None on success, or a short error message string. Shared by /resume
        in-topic and the picker callback so both paths behave identically."""
        if not row.sdk_session_id:
            return "no sdk_session_id (never had a first turn, nothing to resume)"
        try:
            conn = self.runners.get(row.runner_name)
        except KeyError:
            return f"runner {row.runner_name!r} no longer registered — /macs add first"
        if not conn.connected:
            return f"{row.runner_name!r} isn't connected — wake it up and try again"

        handle_box: list[SessionHandle] = []

        async def perm_handler(req: PermissionRequest) -> None:
            await self.permissions_ui.render_card(
                runner=handle_box[0],
                chat_id=self.settings.telegram_chat_id,
                thread_id=thread_id,
                request=req,
            )

        try:
            handle = await conn.open_session(
                sid=str(thread_id),
                cwd=row.cwd,
                mode=row.permission_mode,  # type: ignore[arg-type]
                model=row.model,
                effort=row.effort,
                thinking=row.thinking,
                resume=row.sdk_session_id,
                on_permission_request=perm_handler,
                on_session_id_assigned=self._make_id_persister(thread_id),
            )
        except Exception as exc:
            log.exception("resume.open_failed", thread_id=thread_id)
            return f"runner {row.runner_name!r} rejected open: {exc!s}"
        handle_box.append(handle)

        # sessions.add does INSERT OR REPLACE which flips state back to
        # 'active' and updates the in-memory map.
        try:
            await self.sessions.add(TopicSession(
                thread_id=thread_id,
                project_name=row.project_name,
                cwd=row.cwd,
                runner=handle,
                turn_lock=asyncio.Lock(),
                runner_name=row.runner_name,
                model=row.model,
                effort=row.effort,
                thinking=row.thinking,
            ))
        except RuntimeError as exc:
            # In-memory entry already present — most likely a stale handle
            # from a prior reap that wasn't evicted (pre-fix sessions). Evict
            # and retry once so the user isn't stuck.
            if "already has a session" in str(exc):
                self.sessions.evict_in_memory(thread_id)
                await self.sessions.add(TopicSession(
                    thread_id=thread_id,
                    project_name=row.project_name,
                    cwd=row.cwd,
                    runner=handle,
                    turn_lock=asyncio.Lock(),
                    runner_name=row.runner_name,
                    model=row.model,
                    effort=row.effort,
                    thinking=row.thinking,
                ))
            else:
                raise
        return None

    async def _handle_resume_callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith("ct:rsm:"):
            return False
        try:
            thread_id = int(data[len("ct:rsm:"):])
        except ValueError:
            await query.answer("malformed")
            return True
        row = await self.db.get_session(thread_id)
        if row is None:
            await query.answer("no DB row", show_alert=True)
            return True
        if row.state != "orphaned":
            await query.answer(f"state is {row.state!r}, not orphaned", show_alert=True)
            return True
        err = await self._resume_orphaned_row(row, thread_id)
        if err is not None:
            await query.answer(f"resume failed: {err}", show_alert=True)
            return True
        await query.answer(f"resumed {row.project_name}")
        # Confirm in the session's topic itself so the user sees the resume
        # in the right place when they switch to it.
        try:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text="✓ session resumed — transcript intact, ready for next turn.",
            )
        except Exception:
            log.exception("resume.topic_post_failed", thread_id=thread_id)
        return True

    async def cmd_move(self, message: Message) -> None:
        """/move mac=<dest> — migrate this session to another runner.

        Reads the SDK transcript from the source runner, writes it on the
        destination, opens a fresh SDK there with resume=<sdk_session_id>,
        then tears down the source. The conversation continues seamlessly
        from the user's perspective.

        Caveats:
        - The cwd must already exist on the destination runner; the bridge
          can't migrate your project directory.
        - Files under <cwd>/_uploads/ aren't transferred yet — if Claude
          referenced an upload, it'll be a dead path on the destination.
        - Held under turn_lock — if a turn is mid-stream, the move waits.
        """
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/move only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return

        parts = (message.text or "").strip().split()
        dest_name: str | None = None
        for tok in parts[1:]:
            if tok.startswith("mac="):
                dest_name = tok[len("mac="):].strip()
            elif tok.startswith("dest="):
                dest_name = tok[len("dest="):].strip()
        if not dest_name:
            await message.answer(
                f"usage: /move mac=<name>\n"
                f"current: {session.runner_name}\n"
                f"registered: {', '.join(self.runners.names()) or '(none)'}"
            )
            return
        if dest_name == session.runner_name:
            await message.answer(f"⚠ session is already on {dest_name!r}.")
            return
        try:
            dest_conn = self.runners.get(dest_name)
        except KeyError:
            await message.answer(
                f"⚠ no runner registered as {dest_name!r}. "
                f"registered: {', '.join(self.runners.names()) or '(none)'}"
            )
            return
        if not dest_conn.connected:
            await message.answer(
                f"⚠ {dest_name!r} is not currently connected — wake it up first."
            )
            return

        sdk_id = session.runner.session_id
        if not sdk_id:
            await message.answer(
                "⚠ session has no SDK session id yet — send a message first to "
                "establish the transcript before /move."
            )
            return

        sid = str(session.thread_id)
        # Pre-flight: bail before any destructive read/write if dest already
        # has this sid. Almost never hits but a "session already open" error
        # mid-move would be miserable to debug.
        if dest_conn.has_session(sid):
            await message.answer(
                f"⚠ {dest_name!r} already has a session with sid={sid}. "
                f"this shouldn't happen — possibly a stale handle from a "
                f"previous failed /move. try /restart first."
            )
            return

        # Hold the turn lock for the whole orchestration so a turn that's
        # mid-stream completes (or doesn't start) while we shuffle SDKs.
        async with session.turn_lock:
            await message.answer(
                f"⏳ moving session {session.project_name!r} → {dest_name}…"
            )

            # 1. Read the .jsonl transcript from the source runner.
            #    The SDK encodes cwd by replacing '/' with '-'; matching path:
            #    ~/.claude/projects/<encoded-cwd>/<sdk_session_id>.jsonl
            encoded_cwd = session.cwd.replace("/", "-")
            transcript_path = f"~/.claude/projects/{encoded_cwd}/{sdk_id}.jsonl"
            try:
                _, transcript_bytes = await self.runners.get(
                    session.runner_name
                ).get_file(transcript_path)
            except Exception as exc:
                await message.answer(
                    f"⚠ couldn't read source transcript at {transcript_path}: {exc!s}"
                )
                return

            # 2. Write it to the destination at the same logical path.
            try:
                await dest_conn.upload_file(transcript_path, transcript_bytes)
            except Exception as exc:
                await message.answer(
                    f"⚠ couldn't write transcript to {dest_name!r}: {exc!s}"
                )
                return

            # 3. DB-first: flip runner_name on disk before any in-memory or
            # runner-side change. If everything else fails, restart sees the
            # session on the dest mac and tries to resume there.
            try:
                await self.db.update_session_runner_name(
                    session.thread_id, dest_name,
                )
            except Exception as exc:
                await message.answer(f"⚠ couldn't persist runner_name change: {exc!s}")
                return

            # 4. Open a fresh SDK on the destination with resume=. perm_handler
            # captures the new handle through the box trick so it routes to the
            # right place once the swap below happens.
            handle_box: list[SessionHandle] = []

            async def perm_handler(req: PermissionRequest) -> None:
                await self.permissions_ui.render_card(
                    runner=handle_box[0],
                    chat_id=self.settings.telegram_chat_id,
                    thread_id=session.thread_id,
                    request=req,
                )

            try:
                new_handle = await dest_conn.open_session(
                    sid=sid,
                    cwd=session.cwd,
                    mode=session.runner.permission_mode,  # type: ignore[arg-type]
                    model=session.model,
                    effort=session.effort,
                    thinking=session.thinking,
                    resume=sdk_id,
                    on_permission_request=perm_handler,
                    on_session_id_assigned=self._make_id_persister(session.thread_id),
                )
            except Exception as exc:
                # Source is still alive — undo the DB row update so things
                # stay consistent.
                with contextlib.suppress(Exception):
                    await self.db.update_session_runner_name(
                        session.thread_id, session.runner_name,
                    )
                await message.answer(
                    f"⚠ /move aborted: dest {dest_name!r} couldn't open: {exc!s}\n"
                    f"source session is unchanged."
                )
                return
            handle_box.append(new_handle)

            # 5. Swap in-memory + tear down the source SDK. Source close is
            # best-effort; if it fails the bridge has a leaked SDK process on
            # the source runner but the user-facing flow is fine.
            source_name = session.runner_name
            old_handle = session.runner
            self.permissions_ui.cancel_pending_for(old_handle)
            session.runner = new_handle
            session.runner_name = dest_name
            with contextlib.suppress(Exception):
                await old_handle.close()

        await message.answer(
            f"✓ moved {session.project_name!r} → {dest_name!r}.\n"
            f"transcript intact via resume=. send a message to continue.\n\n"
            f"💡 files under {session.cwd}/_uploads/ on {source_name!r} "
            f"weren't transferred — rsync them manually if Claude needs to re-read them."
        )

    async def cmd_restart(self, message: Message) -> None:
        """Reattach the SDK to this topic's transcript. Kills the in-flight
        turn (if any), tears down the SDK process on the runner, then opens a
        fresh SDK with resume=<sdk_session_id> so the conversation continues.
        Useful when a turn is wedged and /interrupt won't recover it."""
        if message.message_thread_id is None or message.message_thread_id == GENERAL_TOPIC_ID:
            await message.answer("/restart only works inside a session topic")
            return
        session = self.sessions.get(message.message_thread_id)
        if session is None:
            await message.answer("no session in this topic.")
            return
        sdk_id = session.runner.session_id
        if not sdk_id:
            await message.answer(
                "⚠ this session has no sdk_session_id yet — there's nothing "
                "to restart. send a message first."
            )
            return

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await message.answer(
            f"🔄 restart session {session.project_name!r}?\n\n"
            f"this will cancel any in-flight turn and reconnect the SDK to "
            f"the same transcript. use it when /interrupt isn't enough.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✓ yes, restart",
                    callback_data=f"ct:rs:yes:{session.thread_id}",
                ),
                InlineKeyboardButton(text="✗ cancel", callback_data="ct:rs:no"),
            ]]),
        )

    async def _handle_restart_confirm_callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith("ct:rs:"):
            return False
        rest = data[len("ct:rs:") :]
        if rest == "no":
            await menu_ui._safe_edit(
                self.bot, query, text="✗ restart cancelled", keyboard=None,
            )
            await query.answer("cancelled")
            return True
        if not rest.startswith("yes:"):
            await query.answer()
            return True
        try:
            thread_id = int(rest[len("yes:"):])
        except ValueError:
            await query.answer("malformed")
            return True
        session = self.sessions.get(thread_id)
        if session is None:
            await menu_ui._safe_edit(
                self.bot, query, text="⚠ session no longer exists", keyboard=None,
            )
            await query.answer()
            return True
        await menu_ui._safe_edit(
            self.bot, query, text="⏳ restarting session …", keyboard=None,
        )
        try:
            await self._restart_session(session)
        except Exception as exc:
            log.exception("session.restart_failed", thread_id=thread_id)
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=f"⚠ restart failed: {exc!s}",
            )
            await query.answer("restart failed")
            return True
        await query.answer("restarted")
        await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            message_thread_id=thread_id,
            text="✓ session restarted — transcript intact, ready for next turn.",
        )
        return True

    async def _restart_session(self, session: "TopicSession") -> None:
        """Tear down the runner-side SDK, then re-open on the same thread_id so
        the in-memory TopicSession swaps to a fresh handle. The DB row is
        untouched — sdk_session_id, model, effort, mode all persist.

        If the session never had a first turn yet, `session_id` is None: skip
        the resume so the fresh SDK starts with an empty transcript (there's
        nothing to preserve). This lets /think and /restart work right after
        /new without crashing."""
        sdk_id = session.runner.session_id  # may be None for a never-turned session
        # Cancel any pending permission card so it doesn't latch on to the
        # dead handle.
        self.permissions_ui.cancel_pending_for(session.runner)
        # Tear down the runner-side session (sends T_CLOSE, awaits CLOSED).
        with contextlib.suppress(Exception):
            await session.runner.close()

        # Re-open. perm_handler captures the new handle through the box trick.
        thread_id = session.thread_id
        handle_box: list[SessionHandle] = []

        async def perm_handler(req: PermissionRequest) -> None:
            await self.permissions_ui.render_card(
                runner=handle_box[0],
                chat_id=self.settings.telegram_chat_id,
                thread_id=thread_id,
                request=req,
            )

        # /restart spawns a fresh SDK process, so the in-memory remembered
        # ledger is wiped. Reapply the bot-wide auto-allow list so the user
        # doesn't have to re-tap "Always allow" for the same trusted tools.
        allow_csv = (
            self._defaults_cache.get("default_auto_allow_tools") or ""
        ).strip()
        allow_list = [t.strip() for t in allow_csv.split(",") if t.strip()]
        new_handle = await self.runners.get(session.runner_name).open_session(
            sid=str(thread_id),
            cwd=session.cwd,
            mode=session.runner.permission_mode,  # type: ignore[arg-type]
            model=session.model,
            effort=session.effort,
            thinking=session.thinking,
            resume=sdk_id,
            auto_allow_tools=allow_list or None,
            on_permission_request=perm_handler,
            on_session_id_assigned=self._make_id_persister(thread_id),
        )
        handle_box.append(new_handle)
        # Swap the handle on the in-memory record. turn_lock kept — anything
        # waiting on it will block until this restart completes.
        session.runner = new_handle

    async def _handle_close_confirm_callback(self, query: CallbackQuery) -> bool:
        data = query.data or ""
        if not data.startswith("ct:cc:"):
            return False
        rest = data[len("ct:cc:") :]
        if rest == "no":
            await menu_ui._safe_edit(
                self.bot, query, text="✗ close cancelled", keyboard=None,
            )
            await query.answer("cancelled")
            return True
        if rest.startswith("yes:"):
            try:
                thread_id = int(rest[len("yes:"):])
            except ValueError:
                await query.answer("malformed")
                return True
            session = self.sessions.get(thread_id)
            if session is None:
                await menu_ui._safe_edit(
                    self.bot, query,
                    text="⚠ session no longer exists",
                    keyboard=None,
                )
                await query.answer()
                return True
            await self._close_session_via_menu(session)
            await menu_ui._safe_edit(
                self.bot, query,
                text=f"✓ session {session.project_name!r} closed",
                keyboard=None,
            )
            await query.answer("closed")
            return True
        await query.answer()
        return True

    # ---- topic-text handler -------------------------------------------------

    # ---- media uploads (photo / document / voice / audio) -----------------

    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

    async def on_media_message(self, message: Message) -> None:
        thread_id = message.message_thread_id
        if thread_id is None or thread_id == GENERAL_TOPIC_ID:
            await message.answer(
                "uploads need a session topic — open one with /new first."
            )
            return
        session = self.sessions.get(thread_id)
        if session is None:
            await message.answer("⚠ no session in this topic")
            return

        # Resolve the media kind, file_id, suggested filename
        file_id: str | None = None
        suggested: str = ""
        kind: str = ""
        if message.photo:
            file_id = message.photo[-1].file_id  # largest variant
            suggested = "photo.jpg"
            kind = "image"
        elif message.document:
            file_id = message.document.file_id
            suggested = message.document.file_name or "document.bin"
            kind = "document"
        elif message.voice:
            file_id = message.voice.file_id
            suggested = "voice.ogg"
            kind = "audio"
        elif message.audio:
            file_id = message.audio.file_id
            suggested = message.audio.file_name or "audio.mp3"
            kind = "audio"
        if file_id is None:
            return

        # Download from Telegram
        try:
            tg_file = await self.bot.get_file(file_id)
            buf = io.BytesIO()
            await self.bot.download_file(tg_file.file_path, buf)
            content = buf.getvalue()
        except Exception as exc:
            log.exception("media.download_failed", thread_id=thread_id)
            await message.answer(f"⚠ couldn't download from Telegram: {exc!s}")
            return

        # Build target path under <cwd>/_uploads/
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe = self._SAFE_NAME_RE.sub("_", suggested) or "file"
        target_path = str(Path(session.cwd) / "_uploads" / f"{ts}-{safe}")

        # Ship to the runner's filesystem (works for both local studio + remote
        # macs because RunnerConnection.upload_file goes over the WS).
        try:
            resolved_path, size = await session.runner.upload_file(target_path, content)
        except Exception as exc:
            log.exception("media.upload_to_runner_failed", thread_id=thread_id)
            await message.answer(f"⚠ upload to runner {session.runner_name!r} failed: {exc!s}")
            return

        size_str = (
            f"{size:,} B" if size < 1024 else
            f"{size/1024:.1f} KB" if size < 1024 * 1024 else
            f"{size/1024/1024:.1f} MB"
        )
        await message.answer(f"⬆ uploaded {size_str} → {resolved_path}")

        # For audio, transcribe locally before synthesizing the prompt so
        # Claude sees what was said. Off-loop via asyncio.to_thread.
        transcript: str | None = None
        if kind == "audio" and transcription.is_available():
            await message.answer("🎙 transcribing…")
            ext = ".ogg" if message.voice else f".{(suggested.rsplit('.', 1) + ['mp3'])[1]}"
            try:
                transcript = await transcription.transcribe_bytes(
                    content, suffix=ext
                )
            except Exception as exc:
                log.exception("transcribe.failed", thread_id=thread_id)
                await message.answer(f"⚠ transcription failed: {exc!s}")
            if transcript:
                # Show the user what we heard, capped so very long ones don't
                # blow up the chat
                shown = transcript if len(transcript) <= 500 else transcript[:500] + "…"
                await message.answer(f"📝 transcript:\n{shown}")

        # Synthesize a user message so Claude knows the file is there + sees
        # the caption. For audio with a transcript, the transcript is the
        # primary message.
        caption = (message.caption or "").strip()
        if kind == "audio":
            if transcript:
                prompt = (
                    f"(I sent a voice/audio message; transcribed locally.)\n"
                    f"Audio file saved at `{resolved_path}` ({size_str}).\n"
                    f"\nTranscript:\n{transcript}"
                )
            else:
                prompt = (
                    f"I uploaded an audio file at `{resolved_path}` "
                    f"({size_str}). I couldn't transcribe it locally — "
                    f"the file is saved if another tool needs it."
                )
        else:
            prompt = (
                f"I uploaded a file at `{resolved_path}` ({size_str}). "
                f"Use the Read tool to look at it."
            )
        if caption:
            prompt += f"\n\nCaption: {caption}"

        # Run a turn so Claude responds to the upload
        renderer = TopicRenderer(
            self.bot, self.settings.telegram_chat_id, thread_id,
            silent=self._is_quiet_now,
        )
        await self.db.log_event(
            thread_id, "user_msg",
            {"kind": "media", "preview": prompt[:120]},
        )
        async with session.turn_lock:
            renderer.start()
            try:
                async for env in session.runner.turn(prompt):
                    await self._dispatch_envelope(env, renderer, thread_id)
            except Exception as exc:
                log.exception("media.turn_failed", thread_id=thread_id)
                await renderer.render_error(f"turn failed: {exc!r}")
                await self.db.log_event(
                    thread_id, "turn_end", {"reason": "error"},
                )
            else:
                await self.sessions.touch(thread_id)
                await self.db.log_event(
                    thread_id, "turn_end", {"reason": "success"},
                )
            finally:
                await renderer.finish()

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
        renderer = TopicRenderer(
            self.bot, self.settings.telegram_chat_id, thread_id,
            silent=self._is_quiet_now,
        )
        # Log the user's turn-opening message — first event of every turn.
        await self.db.log_event(
            thread_id, "user_msg",
            {"len": len(message.text), "preview": message.text[:120]},
        )
        async with session.turn_lock:
            renderer.start()
            try:
                async for env in session.runner.turn(message.text):
                    await self._dispatch_envelope(env, renderer, thread_id)
            except Exception as exc:
                log.exception("turn.failed", thread_id=thread_id)
                await renderer.render_error(f"turn failed: {exc!r}")
                await self.db.log_event(
                    thread_id, "turn_end", {"reason": "error"},
                )
            else:
                await self.sessions.touch(thread_id)
                await self.db.log_event(
                    thread_id, "turn_end", {"reason": "success"},
                )
            finally:
                await renderer.finish()

    async def _dispatch_envelope(
        self, env: Envelope, renderer: TopicRenderer, thread_id: int | None = None
    ) -> None:
        if env.type == T_TEXT:
            text = env.payload.get("text", "")
            if text and isinstance(text, str) and text.strip():
                await renderer.render_text(text)
                if thread_id is not None:
                    await self.db.log_event(
                        thread_id, "assistant_text",
                        {"len": len(text), "preview": text[:120]},
                    )
        elif env.type == T_TOOL_USE:
            await renderer.render_tool_use(
                env.payload.get("name", ""), env.payload.get("input", {}) or {}
            )
            if thread_id is not None:
                await self.db.log_event(
                    thread_id, "tool_use",
                    {"name": env.payload.get("name", "")},
                )
        elif env.type == T_TOOL_RESULT:
            is_error = bool(env.payload.get("is_error", False))
            await renderer.render_tool_result(env.payload.get("content", ""), is_error)
            if thread_id is not None:
                await self.db.log_event(
                    thread_id, "tool_result", {"is_error": is_error},
                )
        elif env.type == T_THINKING:
            thinking_text = env.payload.get("thinking", "") or ""
            await renderer.render_thinking(thinking_text)
            if thread_id is not None and thinking_text.strip():
                # Persist thinking text so the dashboard can surface it. Trimmed
                # preview to keep message_log rows bounded.
                await self.db.log_event(
                    thread_id, "thinking",
                    {"len": len(thinking_text), "preview": thinking_text[:240]},
                )
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
            on_restart=self._restart_session,
        ):
            return
        if await menu_ui.handle_defaults_callback(
            query, db=self.db, bot=self.bot,
            runner_names_fn=self.runners.names,
        ):
            return
        if await menu_ui.handle_profiles_callback(
            query, db=self.db, bot=self.bot,
            on_destructive=self._record_destructive,
        ):
            return
        if await self._handle_profile_setprompt_callback(query):
            return
        if await self._handle_browse_callback(query):
            return
        if await self._handle_new_menu_callback(query):
            return
        if await self._handle_resume_callback(query):
            return
        if await self._handle_close_confirm_callback(query):
            return
        if await self._handle_restart_confirm_callback(query):
            return
        if await self._handle_macs_remove_callback(query):
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

        # Starting-point picker (multi-shortcut mac). Click a row → drill in.
        if verb == "pick" and len(parts) >= 2:
            if query.message is None:
                await query.answer()
                return True
            picker = self._browse_starting_points.pop(query.message.message_id, None)
            if picker is None:
                await query.answer(
                    "picker expired — tap [📂 browse] again", show_alert=True,
                )
                return True
            try:
                idx = int(parts[1])
            except ValueError:
                await query.answer("malformed")
                return True
            paths = picker["paths"]
            if not (0 <= idx < len(paths)):
                await query.answer("out of range")
                return True
            try:
                conn = self.runners.get(picker["mac_name"])
            except KeyError:
                await query.answer(
                    f"runner {picker['mac_name']!r} not registered", show_alert=True,
                )
                return True
            await self._open_browse_at(
                query, mac_name=picker["mac_name"], conn=conn,
                path=paths[idx], show_hidden=picker.get("show_hidden", False),
            )
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
        shortcuts = list(mac_row.shortcuts) if mac_row else []
        # When shortcuts are configured, ask the user to pick a starting point
        # before drilling in. Without shortcuts, behaviour is unchanged.
        if shortcuts:
            await self._render_starting_point_picker(
                query, mac_name=mac_name, main_dir=main_dir,
                shortcuts=shortcuts, show_hidden=show_hidden,
            )
            return
        await self._open_browse_at(
            query, mac_name=mac_name, conn=conn, path=main_dir,
            show_hidden=show_hidden,
        )

    async def _render_starting_point_picker(
        self, query: CallbackQuery, *, mac_name: str, main_dir: str,
        shortcuts: list[str], show_hidden: bool,
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        # callback_data is bounded — use a per-message map keyed by index so
        # long paths don't blow the 64-byte budget.
        if query.message is None:
            await query.answer()
            return
        starting_points = [main_dir, *shortcuts]
        msg_id = query.message.message_id
        self._browse_starting_points[msg_id] = {
            "mac_name": mac_name,
            "show_hidden": show_hidden,
            "paths": starting_points,
        }
        rows: list[list[InlineKeyboardButton]] = []
        for i, p in enumerate(starting_points):
            label = "🏠 " + p if i == 0 else "📁 " + p
            # Telegram caps button text at 64 chars
            if len(label) > 60:
                label = label[:57] + "…"
            rows.append([InlineKeyboardButton(
                text=label, callback_data=f"ct:b:pick:{i}",
            )])
        await menu_ui._safe_edit(
            self.bot, query,
            text=f"📂 browse {mac_name} — pick a starting folder:",
            keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await query.answer()

    async def _open_browse_at(
        self, query: CallbackQuery, *, mac_name: str, conn: Any, path: str,
        show_hidden: bool,
    ) -> None:
        try:
            resolved_path, items = await conn.list_dir(path, show_hidden=show_hidden)
        except Exception as exc:
            await menu_ui._safe_edit(
                self.bot, query,
                text=f"⚠ couldn't list {mac_name}:{path}\n\n{exc!s}",
                keyboard=None,
            )
            await query.answer()
            return
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

    async def _handle_profile_setprompt_callback(self, query: CallbackQuery) -> bool:
        """Owned by bot.py (not menu_ui) because it needs to register a
        ForceReply pending action — the user types the actual prompt as a
        normal message after tapping the button."""
        data = query.data or ""
        if not data.startswith(menu_ui.PROFILES_PREFIX + "setprompt:"):
            return False
        name = data[len(menu_ui.PROFILES_PREFIX + "setprompt:") :]
        profile = await self.db.get_profile(name)
        if profile is None:
            await query.answer("profile no longer exists")
            return True
        existing = (profile.system_prompt or "").strip()
        existing_preview = (
            f"current:\n{existing[:200]}{'…' if len(existing) > 200 else ''}\n\n"
            if existing else ""
        )
        sent = await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text=(
                f"✏ system prompt for profile {name!r}\n\n"
                f"{existing_preview}"
                f"reply with the new prompt, or 'off' to clear it. "
                f"this is appended to Claude Code's default + git/test directives."
            ),
            reply_markup=ForceReply(),
        )
        action = {"action": "set_profile_prompt", "profile_name": name}
        self._pending_replies[sent.message_id] = action
        if query.from_user is not None:
            self._pending_by_user[query.from_user.id] = action
        await query.answer("type the prompt…")
        return True

    async def _handle_set_profile_prompt_reply(
        self, message: Message, pending: dict[str, Any]
    ) -> None:
        name = pending.get("profile_name")
        if not name:
            await message.answer("⚠ pending action lost the profile name — try again")
            return
        text = (message.text or "").strip()
        clear = text.lower() == "off" or text == ""
        ok = await self.db.update_profile_system_prompt(
            name, None if clear else text
        )
        if not ok:
            await message.answer(f"⚠ profile {name!r} no longer exists")
            return
        if clear:
            await message.answer(f"✓ system prompt cleared for {name!r}")
        else:
            preview = text[:120] + ("…" if len(text) > 120 else "")
            await message.answer(
                f"✓ system prompt set for {name!r}\n\npreview:\n{preview}"
            )

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
        elif action == "set_profile_prompt":
            await self._handle_set_profile_prompt_reply(message, pending)
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
        try:
            resolved_path, items = await conn.list_dir(
                full_path, show_hidden=state.get("show_hidden", False)
            )
        except Exception as exc:
            await message.answer(f"⚠ created {folder_name} but couldn't list it: {exc!s}")
            return
        state["items"] = items
        # The original browse card is far up in chat; send a NEW navigate card
        # right here so the user can immediately confirm + open. Drop the old
        # browse_state entry to avoid stale taps on the original card.
        self._browse_state.pop(browse_msg_id, None)
        sent = await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text=f"✓ created {full_path}\n\n" + menu_ui.navigate_text(
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
        # Anchor the new card so subsequent button taps find this state.
        self._browse_state[sent.message_id] = state

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
        # Send a fresh card here (rather than editing the original way up the
        # chat) so the user sees the rename land + can immediately confirm.
        self._browse_state.pop(browse_msg_id, None)
        sent = await self.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text=f"✓ name → {new_name}\n\n" + menu_ui.navigate_text(
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
        self._browse_state[sent.message_id] = state

    async def _close_session_via_menu(self, session: TopicSession) -> None:
        """Called by the action card's close button. Snapshots the session
        first so /undo can re-open it within the TTL window."""
        # Capture before tearing anything down — once close() runs, runner.close
        # nulls session_id and the in-memory state goes away. We only need
        # enough to call open_session(...) again with resume=.
        self._record_destructive("close", {
            "thread_id": session.thread_id,
            "project_name": session.project_name,
            "cwd": session.cwd,
            "runner_name": session.runner_name,
            "model": session.model,
            "effort": session.effort,
            "thinking": session.thinking,
            "permission_mode": session.runner.permission_mode,
            "sdk_session_id": session.runner.session_id,
        })
        self.permissions_ui.cancel_pending_for(session.runner)
        try:
            await session.runner.close()
        except Exception:
            log.exception("session.close_failed", thread_id=session.thread_id)
        await self.sessions.close(session.thread_id)

    # ---- crash notifications ----------------------------------------------

    async def on_handler_error(self, event: ErrorEvent) -> None:
        """Last-resort handler — logs unhandled exceptions and DMs the admin
        so failures don't sit silently in the log file."""
        exc = event.exception
        update = event.update
        update_repr = (
            f"update_id={update.update_id}" if update is not None else "(no update)"
        )
        log.exception("dispatcher.handler_error", update=update_repr)
        if not self.settings.telegram_allowed_user_ids:
            return
        admin_id = self.settings.telegram_allowed_user_ids[0]
        try:
            # Trim long tracebacks to ~3.5 KB to fit Telegram message limits
            err_text = f"{type(exc).__name__}: {exc!s}"
            await self.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠ bot error\n"
                    f"{update_repr}\n\n"
                    f"```\n{err_text[:3500]}\n```"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            log.exception("dispatcher.error_notify_failed")

    # ---- runner-side callbacks ---------------------------------------------

    # Outages shorter than this are silenced (laptop sleep/wake typically
    # drops the WS for 10–30s and produces pure noise). Longer outages get
    # ONE notice in General — not per-topic — since the per-topic notice
    # cluttered chats and the in-flight turn already gets a "runner dropped"
    # error inline via the streaming renderer when relevant.
    _RECONNECT_NOTICE_THRESHOLD_S = 300.0  # 5 min

    @staticmethod
    def _humanize_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h}h"

    async def on_runner_reconnect(
        self, name: str, sids: list[str], outage_seconds: float
    ) -> None:
        """Called by RunnerPool after a runner's WS reconnects. Silent for
        short outages (sleep/wake blips); posts ONE notice in General when
        the outage was long enough that the user probably wants to know."""
        log.info(
            "bridge.runner_reconnected",
            name=name,
            sessions=len(sids),
            outage_seconds=round(outage_seconds, 1),
        )
        if outage_seconds < self._RECONNECT_NOTICE_THRESHOLD_S:
            return
        try:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=(
                    f"⚡ runner {name!r} reconnected after "
                    f"{self._humanize_duration(outage_seconds)} outage — "
                    f"{len(sids)} session(s) resumed."
                ),
            )
        except Exception:
            log.exception("bridge.reconnect_announce_failed", runner=name)

    async def on_runner_pre_compact(self, name: str, sid: str) -> None:
        """Called when the SDK is about to compact this session's
        conversation history. Heads-up notice in the topic so the user knows
        Claude's context is shifting (response quality may change after)."""
        log.info("bridge.session_pre_compact", runner=name, sid=sid)
        try:
            thread_id = int(sid)
        except ValueError:
            return
        if self.sessions.get(thread_id) is None:
            return
        try:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text="📦 conversation compacting — older history will be summarised, recent turns kept.",
            )
        except Exception:
            log.exception("bridge.pre_compact_announce_failed", thread_id=thread_id)

    async def on_runner_idle_reaped(self, name: str, sid: str) -> None:
        """Called when the runner's idle reaper closes a session. The SDK CLI
        subprocess has been freed; mark the session orphaned in DB and evict
        the in-memory TopicSession so:
          (a) the bridge's auto-reconnect doesn't re-open it (the runner_client
              already pops from its _sessions map for the same reason),
          (b) /resume's open_session call doesn't trip the "session already
              open" guard,
          (c) the user's next message in the topic falls into the "no session"
              path instead of trying a stale handle that would error.
        """
        log.info("bridge.session_idle_reaped", runner=name, sid=sid)
        try:
            thread_id = int(sid)
        except ValueError:
            return
        if self.sessions.get(thread_id) is None:
            # Already evicted (e.g. user just /resume'd) — nothing to announce.
            return
        with contextlib.suppress(Exception):
            await self.db.mark_orphaned(thread_id, reason="idle_reaped")
        # Drop the in-memory TopicSession. The handle is closed; keeping it
        # around would loop the cycle (auto-reconnect re-opens, 30 min idle
        # reaps again, ...). Already-shipped fix in runner_client also pops.
        self.sessions.evict_in_memory(thread_id)
        try:
            await self.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                message_thread_id=thread_id,
                text=(
                    "💤 Session paused after 30+ min idle to free resources.\n"
                    "Run /resume to re-attach this session and continue."
                ),
            )
        except Exception:
            log.exception("bridge.idle_reap_announce_failed", thread_id=thread_id)

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
        # Populate the cache once at startup. Anything mutating defaults at
        # runtime (cmd_quiet, _macs_set_default, etc.) updates this too.
        self._defaults_cache = await self.db.all_defaults()

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
            # NOTE: `/allow` is intentionally NOT applied on session restore —
            # restored sessions are already underway, and silently granting
            # pre-trust after an unattended bridge crash would expand
            # permissions the user didn't reauthorize. The user can /restart
            # a session to opt in. /new and /restart pick it up.
            handle = await self.runners.get(runner_name).open_session(
                sid=str(spec.thread_id),
                cwd=spec.cwd,
                mode=spec.permission_mode,  # type: ignore[arg-type]
                resume=spec.sdk_session_id,
                model=spec.model,
                effort=spec.effort,
                thinking=spec.thinking,
                on_permission_request=perm_handler,
                on_session_id_assigned=self._make_id_persister(spec.thread_id),
            )
            handle_box.append(handle)
            return handle

        n = await self.sessions.restore(factory, default_runner_name=self.default_runner)
        log.info("bridge.sessions_restored", count=n)
        return n

    # ---- lifecycle ----------------------------------------------------------

    # ---- idle housekeeping --------------------------------------------------

    # Tunables: tolerable lag between idle event and notice. Default checks
    # every 5 minutes, expires permission cards older than 30 min.
    IDLE_CHECK_INTERVAL_S = 300
    PERMISSION_MAX_AGE_MIN = 30

    async def _idle_check_loop(self) -> None:
        """Background task — periodically expire stale permission cards.
        Lives for the lifetime of the bridge process. Wakes once per
        IDLE_CHECK_INTERVAL_S; each tick is bounded so a slow Telegram edit
        can't pile up cycles."""
        while True:
            try:
                await asyncio.sleep(self.IDLE_CHECK_INTERVAL_S)
            except asyncio.CancelledError:
                return
            try:
                count = await self.permissions_ui.expire_stale(
                    chat_id=self.settings.telegram_chat_id,
                    max_age_minutes=self.PERMISSION_MAX_AGE_MIN,
                )
                if count:
                    log.info("idle.expired_permissions", count=count)
            except Exception:
                log.exception("idle.tick_failed")

    def start_idle_check(self) -> asyncio.Task:
        """Called by main.py after polling starts. Returned task is cancelled
        on shutdown so the bridge exits cleanly."""
        return asyncio.create_task(self._idle_check_loop(), name="bridge-idle-check")

    # Tunables: how often to round-trip every connected runner. Light enough
    # not to interfere with real traffic; tight enough that an unresponsive
    # runner shows up red within a couple of minutes.
    HEALTH_CHECK_INTERVAL_S = 60
    HEALTH_CHECK_TIMEOUT_S = 5.0

    async def _health_check_loop(self) -> None:
        """Background task — ping every registered runner once per
        HEALTH_CHECK_INTERVAL_S. Failures are caught and logged, leaving the
        runner's last_ping_ok_ts stale (which /macs renders as 🟡 or 🔴)."""
        while True:
            try:
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL_S)
            except asyncio.CancelledError:
                return
            for name in self.runners.names():
                conn = self.runners.get(name)
                if not conn.connected:
                    # Reconnect logic owns this case; nothing to ping.
                    continue
                try:
                    await conn.ping(timeout=self.HEALTH_CHECK_TIMEOUT_S)
                except Exception as exc:
                    log.warning(
                        "health_check.ping_failed",
                        name=name, error=str(exc),
                    )

    def start_health_check(self) -> asyncio.Task:
        """Same lifecycle pattern as start_idle_check — main.py owns the task
        and cancels it on shutdown."""
        return asyncio.create_task(
            self._health_check_loop(), name="bridge-health-check"
        )

    # DB prune: closed sessions older than this are deleted along with their
    # message_log + pending_permissions rows. Daemon runs once per
    # PRUNE_INTERVAL_S (default 24h). Both knobs are env-overridable.
    PRUNE_RETENTION_DAYS = int(os.environ.get("CT_PRUNE_RETENTION_DAYS", "90"))
    PRUNE_INTERVAL_S = int(os.environ.get("CT_PRUNE_INTERVAL_S", str(24 * 3600)))
    # VACUUM after a prune that freed at least this many sessions — vacuum is
    # cheap on small DBs but not free, no point doing it for a single row.
    PRUNE_VACUUM_THRESHOLD = 50

    async def _prune_loop(self) -> None:
        # Stagger: wait once at boot before the first pass so we don't hit
        # the DB while sessions are still restoring.
        try:
            await asyncio.sleep(300)  # 5 min
        except asyncio.CancelledError:
            return
        while True:
            try:
                counts = await self.db.prune_old_closed(days=self.PRUNE_RETENTION_DAYS)
                if counts["sessions"] >= self.PRUNE_VACUUM_THRESHOLD:
                    await self.db.vacuum()
                    log.info("prune.vacuumed", after_sessions=counts["sessions"])
            except Exception:
                log.exception("prune.tick_failed")
            try:
                await asyncio.sleep(self.PRUNE_INTERVAL_S)
            except asyncio.CancelledError:
                return

    def start_prune_daemon(self) -> asyncio.Task:
        return asyncio.create_task(self._prune_loop(), name="bridge-prune")

    async def shutdown(self) -> None:
        for s in self.sessions.all():
            self.permissions_ui.cancel_pending_for(s.runner)
            try:
                await s.runner.close()
            except Exception:
                log.exception("session.close_failed", thread_id=s.thread_id)
        await self.runners.close_all()
