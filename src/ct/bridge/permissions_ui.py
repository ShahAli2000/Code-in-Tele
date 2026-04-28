"""Render approval cards as Telegram inline keyboards and route taps back.

When the SDK's `can_use_tool` fires, the bridge's session-specific handler asks
this module to surface a card. The card has Approve/Deny buttons whose callback
data encodes the `tool_use_id`. When the user taps, aiogram fires a
CallbackQuery handler that this module also owns; the handler looks up which
runner is waiting on which `tool_use_id` and resolves accordingly.

State lives in two places:
- in-memory `_pending` for live sessions (the runner still has a future open)
- `pending_permissions` table for survival across bridge restart, so a bridge
  reboot doesn't leave dead inline-button cards forever. On boot, the bridge
  marks any leftover rows as expired and edits the messages to say so.
"""

from __future__ import annotations

import json

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ct.bridge.streaming import _format_input_summary
from ct.sdk_adapter.adapter import PermissionRequest, SessionRunner
from ct.store.db import Db

log = structlog.get_logger(__name__)

# Callback data is bounded to 64 bytes by Telegram. Format:
#   "ct:p:<decision>:<tool_use_id>"
# decision is one char:
#   a = allow
#   d = deny
#   r = allow + remember (always allow this tool name in this session)
# tool_use_id is ~30 chars.
CB_PREFIX = "ct:p:"


def _make_callback_data(tool_use_id: str, decision: str) -> str:
    return f"{CB_PREFIX}{decision}:{tool_use_id}"


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Returns (decision, tool_use_id) or None if not one of ours."""
    if not data.startswith(CB_PREFIX):
        return None
    rest = data[len(CB_PREFIX) :]
    decision, _, tool_use_id = rest.partition(":")
    if decision not in ("a", "d", "r") or not tool_use_id:
        return None
    return decision, tool_use_id


def _format_card(request: PermissionRequest) -> str:
    summary = _format_input_summary(request.tool_name, request.input_data)
    return (
        f"🤔 Approval needed\n"
        f"🔧 {request.tool_name}\n\n"
        f"{summary}"
    )


def _make_keyboard(tool_use_id: str, tool_name: str = "") -> InlineKeyboardMarkup:
    # Two rows so the high-blast-radius "always allow" is visually distinct
    # from the per-call Approve/Deny.
    remember_label = (
        f"✓ Always allow {tool_name} (this session)" if tool_name
        else "✓ Always allow (this session)"
    )
    # Telegram caps button labels around 64 chars; tool names are short but
    # be defensive.
    if len(remember_label) > 60:
        remember_label = remember_label[:57] + "…"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✓ Approve",
                    callback_data=_make_callback_data(tool_use_id, "a"),
                ),
                InlineKeyboardButton(
                    text="✗ Deny",
                    callback_data=_make_callback_data(tool_use_id, "d"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=remember_label,
                    callback_data=_make_callback_data(tool_use_id, "r"),
                ),
            ],
        ]
    )


class PermissionsUI:
    """Stateful: tracks every approval card the bridge has surfaced so a button
    tap can find the right SessionRunner and edit the right card. Also persists
    each pending entry to the DB so we can clean up after a bridge restart."""

    def __init__(self, bot: Bot, db: Db) -> None:
        self._bot = bot
        self._db = db
        # tool_use_id -> (runner, chat_id, message_id, original_text)
        self._pending: dict[str, tuple[SessionRunner, int, int, str]] = {}

    async def render_card(
        self,
        *,
        runner: SessionRunner,
        chat_id: int,
        thread_id: int,
        request: PermissionRequest,
    ) -> None:
        text = _format_card(request)
        keyboard = _make_keyboard(request.tool_use_id, request.tool_name)
        try:
            sent = await self._bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            log.warning(
                "permission_card.send_failed",
                tool_use_id=request.tool_use_id,
                error=str(exc),
            )
            # Fail-closed: deny so the SDK doesn't hang forever.
            await runner.resolve_permission(
                request.tool_use_id,
                allow=False,
                deny_message=f"approval card couldn't be sent: {exc!s}",
            )
            return
        self._pending[request.tool_use_id] = (runner, chat_id, sent.message_id, text)
        try:
            await self._db.insert_pending_permission(
                tool_use_id=request.tool_use_id,
                thread_id=thread_id,
                message_id=sent.message_id,
                tool_name=request.tool_name,
                input_json=json.dumps(request.input_data, default=str)[:8000],
            )
        except Exception:
            log.exception("permission_card.persist_failed", tool_use_id=request.tool_use_id)

    async def expire_stale(self, chat_id: int, max_age_minutes: int = 30) -> int:
        """Expire any in-flight permission card older than `max_age_minutes`.
        Called by the bridge's periodic idle-check task. Distinct from
        expire_orphans (which clears DB rows whose runners are gone) — this
        one releases the runner-side future as deny so the SDK unblocks.
        Returns count expired."""
        rows = await self._db.list_undecided_permissions(
            older_than_minutes=max_age_minutes
        )
        if not rows:
            return 0
        for row in rows:
            entry = self._pending.pop(row.tool_use_id, None)
            if entry is not None:
                runner, _, _, _ = entry
                # Release the SDK callback so it doesn't hang the next turn.
                # If the runner is no longer tracking this tool_use_id (race
                # with bridge reconnect), this is a harmless no-op.
                try:
                    await runner.resolve_permission(
                        row.tool_use_id,
                        allow=False,
                        deny_message="approval card timed out",
                    )
                except Exception:
                    log.exception(
                        "permission_card.expire_resolve_failed",
                        tool_use_id=row.tool_use_id,
                    )
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=row.message_id,
                    text=(
                        f"🤔 Approval needed\n"
                        f"🔧 {row.tool_name}\n\n"
                        f"— ✗ EXPIRED ({max_age_minutes} min idle; ask Claude again if you still want this)"
                    ),
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass
            await self._db.mark_permission_decided(row.tool_use_id, "expired")
        log.info("permission_card.expired_stale", count=len(rows))
        return len(rows)

    async def expire_orphans(self, chat_id: int) -> int:
        """Called once at bridge boot: any pending rows still in the DB are
        leftovers from before the restart. The runner-side futures backing
        them have already resolved-as-deny during disconnect cleanup, so we
        just edit the messages to remove the buttons and mark a synthetic
        decision in the DB. Returns the count of rows expired."""
        rows = await self._db.list_undecided_permissions()
        for row in rows:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=row.message_id,
                    text=(
                        f"🤔 Approval needed\n"
                        f"🔧 {row.tool_name}\n\n"
                        f"— ✗ EXPIRED (bridge restarted; ask Claude again if you still want this)"
                    ),
                    reply_markup=None,
                )
            except TelegramBadRequest:
                # Message may have been deleted from Telegram; that's ok
                pass
            await self._db.mark_permission_decided(row.tool_use_id, "expired")
        if rows:
            log.info("permission_card.expired_orphans", count=len(rows))
        return len(rows)

    async def handle_callback(self, query: CallbackQuery) -> bool:
        """Returns True if this CallbackQuery was for a permission button.
        False means "not ours; let other handlers see it"."""
        if not query.data or not query.data.startswith(CB_PREFIX):
            return False
        parsed = parse_callback_data(query.data)
        if parsed is None:
            await query.answer("malformed approval data", show_alert=False)
            return True
        decision, tool_use_id = parsed
        entry = self._pending.pop(tool_use_id, None)
        if entry is None:
            await query.answer("this approval has expired", show_alert=False)
            return True

        runner, chat_id, message_id, original_text = entry
        allow = decision in ("a", "r")
        remember = decision == "r"
        resolved = await runner.resolve_permission(
            tool_use_id,
            allow=allow,
            deny_message="User denied via Telegram",
            remember=remember,
        )
        if not resolved:
            await query.answer("already resolved", show_alert=False)
        else:
            if decision == "r":
                await query.answer("approved + remembered")
            else:
                await query.answer("approved" if allow else "denied")
        try:
            await self._db.delete_pending_permission(tool_use_id)
        except Exception:
            log.exception("permission_card.delete_failed", tool_use_id=tool_use_id)

        # Replace the card with a finalized version (no buttons).
        if decision == "r":
            marker = "✓ APPROVED + remembered for this session"
        elif allow:
            marker = "✓ APPROVED"
        else:
            marker = "✗ DENIED"
        finalized = f"{original_text}\n\n— {marker}"
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=finalized,
                reply_markup=None,
            )
        except TelegramBadRequest as exc:
            log.warning(
                "permission_card.edit_failed",
                tool_use_id=tool_use_id,
                error=str(exc),
            )
        return True

    def cancel_pending_for(self, runner: SessionRunner) -> None:
        """Drop in-flight approval entries belonging to a runner being torn down.
        We don't bother editing those cards — the resolve call has already
        landed via SessionRunner.stop()."""
        to_drop = [k for k, v in self._pending.items() if v[0] is runner]
        for k in to_drop:
            self._pending.pop(k, None)
