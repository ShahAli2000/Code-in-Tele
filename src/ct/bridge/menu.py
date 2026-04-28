"""Per-topic action card — `/menu` (or `/m`) brings up an inline-keyboard
view of the current session's settings; each tappable button drills into
a submenu and pops back. The same message is edited rather than re-sent so
the topic doesn't fill up with stale cards.

Callback-data layout:
    ct:m:<sid>:root              — main view (pseudo, not emitted)
    ct:m:<sid>:pick:<field>      — show submenu for `field` (model|effort|mode)
    ct:m:<sid>:set:<field>:<v>   — apply value, return to root
    ct:m:<sid>:close             — close session
    ct:m:<sid>:back              — return to root view

Where <sid> is `str(thread_id)`. Total stays well under Telegram's 64-byte
callback_data cap because all the values we use are short literals.
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
