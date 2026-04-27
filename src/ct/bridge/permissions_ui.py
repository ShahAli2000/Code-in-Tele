"""Render approval cards as Telegram inline keyboards and route taps back.

When the SDK's `can_use_tool` fires, the bridge's session-specific handler asks
this module to surface a card. The card has Approve/Deny buttons whose callback
data encodes the `tool_use_id`. When the user taps, aiogram fires a
CallbackQuery handler that this module also owns; the handler looks up which
runner is waiting on which `tool_use_id` and resolves accordingly.

State (pending approval -> runner + message) lives in memory for Phase 0.
"""

from __future__ import annotations

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

log = structlog.get_logger(__name__)

# Callback data is bounded to 64 bytes by Telegram. Format:
#   "ct:p:<decision>:<tool_use_id>"
# decision is one char (a=allow, d=deny). tool_use_id is ~30 chars.
CB_PREFIX = "ct:p:"


def _make_callback_data(tool_use_id: str, decision: str) -> str:
    return f"{CB_PREFIX}{decision}:{tool_use_id}"


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Returns (decision, tool_use_id) or None if not one of ours."""
    if not data.startswith(CB_PREFIX):
        return None
    rest = data[len(CB_PREFIX) :]
    decision, _, tool_use_id = rest.partition(":")
    if decision not in ("a", "d") or not tool_use_id:
        return None
    return decision, tool_use_id


def _format_card(request: PermissionRequest) -> str:
    summary = _format_input_summary(request.tool_name, request.input_data)
    return (
        f"🤔 Approval needed\n"
        f"🔧 {request.tool_name}\n\n"
        f"{summary}"
    )


def _make_keyboard(tool_use_id: str) -> InlineKeyboardMarkup:
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
            ]
        ]
    )


class PermissionsUI:
    """Stateful: tracks every approval card the bridge has surfaced so a button
    tap can find the right SessionRunner and edit the right card."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
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
        keyboard = _make_keyboard(request.tool_use_id)
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
        allow = decision == "a"
        resolved = await runner.resolve_permission(
            tool_use_id, allow=allow, deny_message="User denied via Telegram"
        )
        if not resolved:
            await query.answer("already resolved", show_alert=False)
        else:
            await query.answer("approved" if allow else "denied")

        # Replace the card with a finalized version (no buttons).
        marker = "✓ APPROVED" if allow else "✗ DENIED"
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
