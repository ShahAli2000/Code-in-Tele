"""Inline-keyboard cards for /menu (per-topic action card), /defaults
(bot-wide defaults editor), and /profiles (profile list + delete).

Callback-data layouts (each card has its own prefix):
    Per-topic action card (`ct:m:`):
        ct:m:<sid>:pick:<field>            show submenu (model|effort|mode)
        ct:m:<sid>:set:<field>:<value>     apply value, return to root
        ct:m:<sid>:back                    return to root view
        ct:m:<sid>:close                   close session

    Bot defaults (`ct:d:`):
        ct:d:pick:<field>                  show submenu (mac|model|effort|mode)
        ct:d:set:<field>:<value>           apply, return to root
        ct:d:set:<field>:_unset            clear
        ct:d:back                          return to root view

    Profiles list (`ct:p:`):
        ct:p:back                          back to list
        ct:p:rm:<name>                     show delete confirmation
        ct:p:rm_yes:<name>                 actually delete
        ct:p:show:<name>                   show profile details

Telegram caps callback_data at 64 bytes. Profile names are bounded by us.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

if TYPE_CHECKING:
    from ct.bridge.sessions import TopicSession

log = structlog.get_logger(__name__)

CB_PREFIX = "ct:m:"

MODEL_OPTIONS = ("opus", "sonnet", "haiku")
EFFORT_OPTIONS = ("low", "medium", "high", "max")
MODE_OPTIONS = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk")

# Resolve aliases the same way bot.py does — duplicated here on purpose so
# this module stays import-loop-free.
MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _cb(sid: str, *parts: str) -> str:
    return CB_PREFIX + sid + ":" + ":".join(parts)


def _format_root(session: "TopicSession") -> str:
    runner = session.runner
    return (
        f"📁 {session.project_name}  ·  {session.runner_name}:{session.cwd}\n"
        f"\n"
        f"model:   {session.model or '(SDK default)'}\n"
        f"effort:  {session.effort or '(SDK default)'}\n"
        f"mode:    {runner.permission_mode}"
    )


def _root_keyboard(sid: str, session: "TopicSession") -> InlineKeyboardMarkup:
    runner = session.runner
    model_label = (session.model or "default").rsplit("-", 1)[0].replace("claude-", "")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🤖 {model_label}",
                    callback_data=_cb(sid, "pick", "model"),
                ),
                InlineKeyboardButton(
                    text=f"💪 {session.effort or 'default'}",
                    callback_data=_cb(sid, "pick", "effort"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"⚙ {runner.permission_mode}",
                    callback_data=_cb(sid, "pick", "mode"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🛑 close session",
                    callback_data=_cb(sid, "close"),
                ),
            ],
        ]
    )


def _pick_keyboard(sid: str, field: str) -> InlineKeyboardMarkup:
    if field == "model":
        rows = [
            [
                InlineKeyboardButton(
                    text=opt, callback_data=_cb(sid, "set", "model", opt)
                )
                for opt in MODEL_OPTIONS
            ]
        ]
    elif field == "effort":
        rows = [
            [
                InlineKeyboardButton(
                    text=opt, callback_data=_cb(sid, "set", "effort", opt)
                )
                for opt in EFFORT_OPTIONS
            ]
        ]
    elif field == "mode":
        # Two rows for visual fit.
        rows = [
            [
                InlineKeyboardButton(
                    text=opt, callback_data=_cb(sid, "set", "mode", opt)
                )
                for opt in MODE_OPTIONS[:3]
            ],
            [
                InlineKeyboardButton(
                    text=opt, callback_data=_cb(sid, "set", "mode", opt)
                )
                for opt in MODE_OPTIONS[3:]
            ],
        ]
    else:
        rows = []
    rows.append(
        [InlineKeyboardButton(text="← back", callback_data=_cb(sid, "back"))]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_root(
    bot: Bot, chat_id: int, thread_id: int, session: "TopicSession"
) -> None:
    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=_format_root(session),
        reply_markup=_root_keyboard(str(thread_id), session),
    )


def parse_callback(data: str) -> tuple[str, list[str]] | None:
    """Returns (sid, [verb, ...args]) or None if not ours."""
    if not data.startswith(CB_PREFIX):
        return None
    rest = data[len(CB_PREFIX) :]
    parts = rest.split(":")
    if not parts:
        return None
    sid, *verb_args = parts
    return sid, verb_args


async def handle_callback(
    query: CallbackQuery,
    *,
    sessions_get,         # callable: thread_id -> TopicSession | None
    db,                   # ct.store.db.Db
    bot: Bot,
    on_close,             # async callable: (TopicSession) -> None
) -> bool:
    """Returns True if this callback was for the action card."""
    parsed = parse_callback(query.data or "")
    if parsed is None:
        return False
    sid, verb_args = parsed
    if not verb_args:
        await query.answer("malformed", show_alert=False)
        return True
    try:
        thread_id = int(sid)
    except ValueError:
        await query.answer("bad sid", show_alert=False)
        return True
    session = sessions_get(thread_id)
    if session is None:
        await query.answer("no session in this topic anymore", show_alert=False)
        return True

    verb = verb_args[0]
    if verb == "back" or verb == "root":
        await _safe_edit(
            bot, query, text=_format_root(session),
            keyboard=_root_keyboard(sid, session),
        )
        await query.answer()
        return True
    if verb == "pick" and len(verb_args) >= 2:
        field = verb_args[1]
        await _safe_edit(
            bot, query,
            text=_format_root(session) + f"\n\nchoose {field}:",
            keyboard=_pick_keyboard(sid, field),
        )
        await query.answer()
        return True
    if verb == "set" and len(verb_args) >= 3:
        field, value = verb_args[1], verb_args[2]
        await _apply_set(query, session, db, field, value)
        await _safe_edit(
            bot, query, text=_format_root(session),
            keyboard=_root_keyboard(sid, session),
        )
        await query.answer(f"{field} → {value}")
        return True
    if verb == "close":
        await on_close(session)
        await _safe_edit(
            bot, query,
            text=f"📁 {session.project_name}\n— ✗ session closed",
            keyboard=None,
        )
        await query.answer("closed")
        return True
    await query.answer("unknown verb", show_alert=False)
    return True


async def _apply_set(
    query: CallbackQuery, session, db, field: str, value: str
) -> None:
    if field == "model":
        canonical = MODEL_ALIASES.get(value, value)
        try:
            await session.runner.set_model(canonical)
        except Exception:
            log.exception("menu.set_model_failed", thread_id=session.thread_id)
            return
        session.model = canonical
        await db.update_session_model(session.thread_id, canonical)
    elif field == "effort":
        session.effort = value
        await db.update_session_effort(session.thread_id, value)
        # NB: takes effect on next session per SDK constraints.
    elif field == "mode":
        try:
            await session.runner.set_permission_mode(value)
        except Exception:
            log.exception("menu.set_mode_failed", thread_id=session.thread_id)
            return
        await db.update_permission_mode(session.thread_id, value)


async def _safe_edit(
    bot: Bot, query: CallbackQuery, *, text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    if query.message is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            text=text,
            reply_markup=keyboard,
        )
    except TelegramBadRequest as exc:
        # "message is not modified" is fine — happens on no-op edits
        if "not modified" not in str(exc):
            log.warning("menu.edit_failed", error=str(exc))


# ====== bot defaults editor (ct:d:*) ===================================

DEFAULTS_PREFIX = "ct:d:"
_UNSET = "_unset"  # sentinel value in callback_data


async def _format_defaults(db) -> str:
    d = await db.all_defaults()
    return (
        "⚙ bot defaults\n"
        "(used when /new and the chosen profile don't pin the field)\n"
        "\n"
        f"mac:    {d.get('default_runner_name') or '(none)'}\n"
        f"model:  {d.get('default_model') or '(SDK default)'}\n"
        f"effort: {d.get('default_effort') or '(SDK default)'}\n"
        f"mode:   {d.get('default_permission_mode') or 'acceptEdits'}"
    )


def _defaults_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 mac",   callback_data=DEFAULTS_PREFIX + "pick:mac"),
                InlineKeyboardButton(text="🤖 model", callback_data=DEFAULTS_PREFIX + "pick:model"),
            ],
            [
                InlineKeyboardButton(text="💪 effort", callback_data=DEFAULTS_PREFIX + "pick:effort"),
                InlineKeyboardButton(text="⚙ mode",    callback_data=DEFAULTS_PREFIX + "pick:mode"),
            ],
        ]
    )


def _defaults_pick_keyboard(field: str, runner_names: list[str]) -> InlineKeyboardMarkup:
    if field == "mac":
        rows = [[
            InlineKeyboardButton(text=n, callback_data=DEFAULTS_PREFIX + f"set:mac:{n}")
            for n in runner_names[:3]
        ]]
        if len(runner_names) > 3:
            rows.append([
                InlineKeyboardButton(text=n, callback_data=DEFAULTS_PREFIX + f"set:mac:{n}")
                for n in runner_names[3:6]
            ])
    elif field == "model":
        rows = [[
            InlineKeyboardButton(text=n, callback_data=DEFAULTS_PREFIX + f"set:model:{n}")
            for n in MODEL_OPTIONS
        ]]
    elif field == "effort":
        rows = [[
            InlineKeyboardButton(text=n, callback_data=DEFAULTS_PREFIX + f"set:effort:{n}")
            for n in EFFORT_OPTIONS
        ]]
    elif field == "mode":
        rows = [
            [InlineKeyboardButton(text=n, callback_data=DEFAULTS_PREFIX + f"set:mode:{n}")
             for n in MODE_OPTIONS[:3]],
            [InlineKeyboardButton(text=n, callback_data=DEFAULTS_PREFIX + f"set:mode:{n}")
             for n in MODE_OPTIONS[3:]],
        ]
    else:
        rows = []
    rows.append([
        InlineKeyboardButton(text="❌ unset", callback_data=DEFAULTS_PREFIX + f"set:{field}:{_UNSET}"),
        InlineKeyboardButton(text="← back", callback_data=DEFAULTS_PREFIX + "back"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_FIELD_TO_DB_KEY = {
    "mac": "default_runner_name",
    "model": "default_model",
    "effort": "default_effort",
    "mode": "default_permission_mode",
}


async def render_defaults(bot: Bot, chat_id: int, db) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=await _format_defaults(db),
        reply_markup=_defaults_root_keyboard(),
    )


async def handle_defaults_callback(
    query: CallbackQuery, *, db, bot: Bot, runner_names_fn,
) -> bool:
    data = query.data or ""
    if not data.startswith(DEFAULTS_PREFIX):
        return False
    rest = data[len(DEFAULTS_PREFIX):]
    parts = rest.split(":")
    if not parts:
        await query.answer("malformed")
        return True
    verb = parts[0]
    if verb == "back":
        await _safe_edit(bot, query, text=await _format_defaults(db),
                         keyboard=_defaults_root_keyboard())
        await query.answer()
        return True
    if verb == "pick" and len(parts) >= 2:
        field = parts[1]
        await _safe_edit(
            bot, query,
            text=await _format_defaults(db) + f"\n\nchoose default {field}:",
            keyboard=_defaults_pick_keyboard(field, runner_names_fn()),
        )
        await query.answer()
        return True
    if verb == "set" and len(parts) >= 3:
        field, value = parts[1], parts[2]
        db_key = _FIELD_TO_DB_KEY.get(field)
        if not db_key:
            await query.answer("unknown field")
            return True
        actual = None if value == _UNSET else MODEL_ALIASES.get(value, value) if field == "model" else value
        await db.set_default(db_key, actual)
        await _safe_edit(bot, query, text=await _format_defaults(db),
                         keyboard=_defaults_root_keyboard())
        await query.answer(f"{field} → {actual or '(unset)'}")
        return True
    await query.answer("unknown")
    return True


# ====== profiles list (ct:p:*) =========================================

PROFILES_PREFIX = "ct:p:"


async def _format_profile_one(profile) -> str:
    bits: list[str] = []
    if profile.dir: bits.append(f"dir={profile.dir}")
    if profile.runner_name: bits.append(f"mac={profile.runner_name}")
    if profile.model: bits.append(f"model={profile.model}")
    if profile.effort: bits.append(f"effort={profile.effort}")
    if profile.permission_mode: bits.append(f"mode={profile.permission_mode}")
    body = "\n".join(f"  {b}" for b in bits) or "  (no fields set; uses defaults)"
    return f"📁 {profile.name}\n{body}"


def _profiles_list_keyboard(profiles) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in profiles:
        rows.append([
            InlineKeyboardButton(
                text=f"📁 {p.name}",
                callback_data=PROFILES_PREFIX + f"show:{p.name}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=PROFILES_PREFIX + f"rm:{p.name}",
            ),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _format_profiles_list(db) -> str:
    profiles = await db.list_profiles()
    if not profiles:
        return "no saved profiles.\nuse /save <name> [dir=...] to add one."
    return f"📚 saved profiles ({len(profiles)})"


async def render_profiles(bot: Bot, chat_id: int, db) -> None:
    profiles = await db.list_profiles()
    text = await _format_profiles_list(db)
    if not profiles:
        await bot.send_message(chat_id=chat_id, text=text)
        return
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_profiles_list_keyboard(profiles),
    )


async def handle_profiles_callback(query: CallbackQuery, *, db, bot: Bot) -> bool:
    data = query.data or ""
    if not data.startswith(PROFILES_PREFIX):
        return False
    rest = data[len(PROFILES_PREFIX):]
    parts = rest.split(":", 1)
    verb = parts[0]
    if verb == "back":
        profiles = await db.list_profiles()
        await _safe_edit(
            bot, query, text=await _format_profiles_list(db),
            keyboard=_profiles_list_keyboard(profiles) if profiles else None,
        )
        await query.answer()
        return True
    if verb == "show" and len(parts) >= 2:
        name = parts[1]
        profile = await db.get_profile(name)
        if profile is None:
            await query.answer("profile no longer exists")
            return True
        await _safe_edit(
            bot, query,
            text=await _format_profile_one(profile)
                + "\n\nuse /save with same name to edit, or /new to start.",
            keyboard=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 delete", callback_data=PROFILES_PREFIX + f"rm:{name}"),
                InlineKeyboardButton(text="← back", callback_data=PROFILES_PREFIX + "back"),
            ]]),
        )
        await query.answer()
        return True
    if verb == "rm" and len(parts) >= 2:
        name = parts[1]
        await _safe_edit(
            bot, query,
            text=f"🗑 delete profile {name!r}?\n\nthis only removes the saved\nconfig — any active sessions stay.",
            keyboard=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✓ yes, delete", callback_data=PROFILES_PREFIX + f"rm_yes:{name}"),
                InlineKeyboardButton(text="✗ cancel", callback_data=PROFILES_PREFIX + "back"),
            ]]),
        )
        await query.answer()
        return True
    if verb == "rm_yes" and len(parts) >= 2:
        name = parts[1]
        ok = await db.delete_profile(name)
        profiles = await db.list_profiles()
        await _safe_edit(
            bot, query,
            text=f"{'✓ deleted' if ok else '⚠ not found'}: {name}\n\n" + await _format_profiles_list(db),
            keyboard=_profiles_list_keyboard(profiles) if profiles else None,
        )
        await query.answer("deleted" if ok else "not found")
        return True
    await query.answer("unknown")
    return True


# ====== folder browser (ct:b:*) =========================================

BROWSE_PREFIX = "ct:b:"


def browse_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="📂 browse", callback_data=BROWSE_PREFIX + "start")


def mac_picker_keyboard(mac_names: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Two macs per row
    for i in range(0, len(mac_names), 2):
        rows.append([
            InlineKeyboardButton(
                text=f"👤 {n}", callback_data=BROWSE_PREFIX + f"m:{n}"
            )
            for n in mac_names[i : i + 2]
        ])
    rows.append([InlineKeyboardButton(text="← back", callback_data="ct:n:back_to_root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_FOLDER_NAME_DISPLAY_LIMIT = 32  # truncate long names in button labels


def _trunc_button(name: str) -> str:
    if len(name) <= _FOLDER_NAME_DISPLAY_LIMIT:
        return name
    return name[: _FOLDER_NAME_DISPLAY_LIMIT - 1] + "…"


def navigate_keyboard(
    *,
    folder_items: list[tuple[str, bool]],
    show_hidden: bool,
    can_go_up: bool,
) -> InlineKeyboardMarkup:
    """Folder-navigation keyboard. Each folder button drills INTO that folder
    rather than opening a session in it. The bottom rows let the user create,
    rename, go up, toggle hidden, confirm-open, or cancel.

    Folder positions are encoded as `cd:<index>` so callback_data stays bounded
    regardless of folder name length. The bridge maintains a per-message cache
    of `folder_items` to look up the index → name mapping.
    """
    rows: list[list[InlineKeyboardButton]] = []
    dirs = [(n, is_dir) for n, is_dir in folder_items if is_dir][:24]
    for i in range(0, len(dirs), 2):
        row = []
        for j, (name, _) in enumerate(dirs[i : i + 2]):
            row.append(InlineKeyboardButton(
                text=f"📁 {_trunc_button(name)}",
                callback_data=BROWSE_PREFIX + f"cd:{i + j}",
            ))
        rows.append(row)
    if not dirs:
        rows.append([InlineKeyboardButton(
            text="(no folders here)", callback_data=BROWSE_PREFIX + "noop",
        )])

    rows.append([
        InlineKeyboardButton(text="➕ new folder", callback_data=BROWSE_PREFIX + "newdir"),
        InlineKeyboardButton(text="✏ rename", callback_data=BROWSE_PREFIX + "setname"),
    ])
    bottom_row: list[InlineKeyboardButton] = []
    if can_go_up:
        bottom_row.append(InlineKeyboardButton(text="⬆ up", callback_data=BROWSE_PREFIX + "up"))
    bottom_row.append(InlineKeyboardButton(
        text=("👁‍🗨 hide hidden" if show_hidden else "👁 show hidden"),
        callback_data=BROWSE_PREFIX + "hidden",
    ))
    rows.append(bottom_row)
    rows.append([
        InlineKeyboardButton(text="✓ open session here", callback_data=BROWSE_PREFIX + "open"),
    ])
    rows.append([
        InlineKeyboardButton(text="✗ cancel", callback_data=BROWSE_PREFIX + "back"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def navigate_text(
    *,
    mac_name: str,
    path: str,
    name: str,
    folder_items: list[tuple[str, bool]],
    show_hidden: bool,
) -> str:
    n_dirs = sum(1 for _, is_dir in folder_items if is_dir)
    n_files = sum(1 for _, is_dir in folder_items if not is_dir)
    files_note = ""
    if n_files > 0:
        files = [n for n, is_dir in folder_items if not is_dir][:5]
        files_note = (
            f"\n  (plus {n_files} non-folders: {', '.join(files)}"
            f"{'…' if n_files > len(files) else ''})"
        )
    hidden_note = "  ·  hidden visible" if show_hidden else ""
    return (
        f"📂 {mac_name}:{path}{hidden_note}\n"
        f"session name: {name}\n"
        f"\n{n_dirs} folder(s) here.{files_note}\n"
        f"\ntap a folder to drill in. when you're at the right place,"
        f"\ntap ✓ open session here."
    )
