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

Special-case: AskUserQuestion. The SDK's built-in tool whose tool_input is a
list of questions Claude wants the user to answer. The default approve/deny
UI is wrong here — the user should pick from the options, not approve the
tool. We branch in render_card; the answer flow lives in `_render_question`
+ `_handle_question_callback` and replaces the result via PermissionResultDeny
with a message shaped like `{"answers": [...]}` (the SDK delivers the deny
message as the tool_result content, which the model parses as the answer).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

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

# AskUserQuestion answer-card prefix:
#   "ct:q:<tool_use_id>:<q_idx>:<o_idx>"  — single-select option tap
#   "ct:q:<tool_use_id>:<q_idx>:t<o_idx>" — multiSelect toggle of an option
#   "ct:q:<tool_use_id>:<q_idx>:S"        — multiSelect submit
QB_PREFIX = "ct:q:"


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


def _format_input_summary(tool_name: str, input_data: dict) -> str:
    """Compact, scannable summary of tool input for permission cards.
    Lives here (not in streaming.py) because the quiet renderer no longer
    surfaces tool inputs to chat — only this module needs the formatter."""
    import json as _json
    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        desc = input_data.get("description", "")
        out = f"$ {cmd}"
        if desc:
            out += f"\n# {desc}"
        return out[:1200]
    if tool_name in ("Edit", "Write"):
        path = input_data.get("file_path", "")
        if tool_name == "Edit":
            old = (input_data.get("old_string", "") or "")[:300]
            new = (input_data.get("new_string", "") or "")[:300]
            return f"{path}\n--- old ---\n{old}\n--- new ---\n{new}"
        content = (input_data.get("content", "") or "")[:600]
        return f"{path}\n--- new file ---\n{content}"
    if tool_name == "Read":
        return input_data.get("file_path", "")
    try:
        return _json.dumps(input_data, indent=2)[:1000]
    except (TypeError, ValueError):
        return repr(input_data)[:1000]


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


@dataclass
class _QuestionState:
    """In-flight AskUserQuestion answer state. One entry per active tool call.
    Multi-question case: rendered sequentially — answers fill in as user taps.
    multiSelect: selected[q_idx] is the set of currently-toggled option indices
    for question q_idx; finalized into a comma-separated string in answers."""

    runner: SessionRunner
    chat_id: int
    thread_id: int
    questions: list[dict[str, Any]]
    answers: list[str] = field(default_factory=list)
    current_idx: int = 0
    card_message_id: int = 0
    selected: dict[int, set[int]] = field(default_factory=dict)


class PermissionsUI:
    """Stateful: tracks every approval card the bridge has surfaced so a button
    tap can find the right SessionRunner and edit the right card. Also persists
    each pending entry to the DB so we can clean up after a bridge restart."""

    def __init__(self, bot: Bot, db: Db) -> None:
        self._bot = bot
        self._db = db
        # tool_use_id -> (runner, chat_id, message_id, original_text)
        self._pending: dict[str, tuple[SessionRunner, int, int, str]] = {}
        # AskUserQuestion in-flight state; separate from _pending because the
        # finalize path (resolve via PermissionResultDeny with shaped JSON) is
        # different from the approve/deny path. Cancelled in cancel_pending_for.
        self._pending_questions: dict[str, _QuestionState] = {}

    async def render_card(
        self,
        *,
        runner: SessionRunner,
        chat_id: int,
        thread_id: int,
        request: PermissionRequest,
    ) -> None:
        # AskUserQuestion needs answer-input UI, not approve/deny. Branch off
        # to the question flow which finalizes via PermissionResultDeny with
        # a {"answers": [...]} shaped message.
        if request.tool_name == "AskUserQuestion":
            await self._render_question_flow(
                runner=runner, chat_id=chat_id, thread_id=thread_id,
                request=request,
            )
            return
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

    # ---- AskUserQuestion flow ---------------------------------------------

    async def _render_question_flow(
        self, *, runner: SessionRunner, chat_id: int, thread_id: int,
        request: PermissionRequest,
    ) -> None:
        questions = request.input_data.get("questions") or []
        if not isinstance(questions, list) or not questions:
            await runner.resolve_permission(
                request.tool_use_id, allow=False,
                deny_message="malformed AskUserQuestion: no questions",
            )
            return
        # Reject free-text-only questions for now — answering via ForceReply is
        # a separate plumbing task. With-options questions cover the common
        # case (Claude usually provides options when it asks).
        # But: fall back to deny with a helpful message so the user can
        # respond by typing in the topic naturally on the next turn.
        all_have_options = all(
            isinstance(q, dict) and isinstance(q.get("options"), list) and q["options"]
            for q in questions
        )
        if not all_have_options:
            preview = "\n\n".join(
                f"❓ {q.get('question', '?')}" for q in questions if isinstance(q, dict)
            )
            try:
                await self._bot.send_message(
                    chat_id=chat_id, message_thread_id=thread_id,
                    text=(
                        f"💬 Claude is asking:\n\n{preview}\n\n"
                        "(Free-text answer not supported via buttons yet — type "
                        "your reply as a normal message in this topic.)"
                    ),
                )
            except Exception:
                log.exception("question_card.send_failed", tool_use_id=request.tool_use_id)
            await runner.resolve_permission(
                request.tool_use_id, allow=False,
                deny_message="User will reply with their answer in the next message.",
            )
            return

        state = _QuestionState(
            runner=runner, chat_id=chat_id, thread_id=thread_id,
            questions=questions,
        )
        self._pending_questions[request.tool_use_id] = state
        await self._send_question_card(request.tool_use_id, state)

    def _format_question_card(
        self, tool_use_id: str, state: _QuestionState
    ) -> tuple[str, InlineKeyboardMarkup]:
        q_idx = state.current_idx
        q = state.questions[q_idx]
        header = (q.get("header") or "").strip()
        question = (q.get("question") or "").strip()
        multi = bool(q.get("multiSelect"))
        options = q.get("options") or []

        progress = (
            f"  ({q_idx + 1}/{len(state.questions)})" if len(state.questions) > 1 else ""
        )
        title = f"❓ Question{progress}"
        if header:
            title += f"  ·  {header}"

        body = question
        if multi:
            body += "\n\n(multi-select — tap to toggle, then ✓ Submit)"

        text = f"{title}\n\n{body}"

        if multi:
            selected = state.selected.setdefault(q_idx, set())
            buttons: list[list[InlineKeyboardButton]] = []
            for o_idx, opt in enumerate(options):
                if not isinstance(opt, dict):
                    continue
                label = (opt.get("label") or opt.get("value") or "?").strip()
                marker = "☑" if o_idx in selected else "☐"
                buttons.append([InlineKeyboardButton(
                    text=f"{marker} {label}"[:60],
                    callback_data=f"{QB_PREFIX}{tool_use_id}:{q_idx}:t{o_idx}",
                )])
            buttons.append([InlineKeyboardButton(
                text="✓ Submit",
                callback_data=f"{QB_PREFIX}{tool_use_id}:{q_idx}:S",
            )])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        else:
            buttons_flat: list[list[InlineKeyboardButton]] = []
            for o_idx, opt in enumerate(options):
                if not isinstance(opt, dict):
                    continue
                label = (opt.get("label") or opt.get("value") or "?").strip()
                buttons_flat.append([InlineKeyboardButton(
                    text=label[:60],
                    callback_data=f"{QB_PREFIX}{tool_use_id}:{q_idx}:{o_idx}",
                )])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons_flat)
        return text, keyboard

    async def _send_question_card(
        self, tool_use_id: str, state: _QuestionState
    ) -> None:
        text, keyboard = self._format_question_card(tool_use_id, state)
        try:
            sent = await self._bot.send_message(
                chat_id=state.chat_id,
                message_thread_id=state.thread_id,
                text=text,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            log.warning(
                "question_card.send_failed",
                tool_use_id=tool_use_id, error=str(exc),
            )
            await state.runner.resolve_permission(
                tool_use_id, allow=False,
                deny_message=f"answer card send failed: {exc!s}",
            )
            self._pending_questions.pop(tool_use_id, None)
            return
        state.card_message_id = sent.message_id

    async def _edit_question_card(
        self, tool_use_id: str, state: _QuestionState
    ) -> None:
        text, keyboard = self._format_question_card(tool_use_id, state)
        try:
            await self._bot.edit_message_text(
                chat_id=state.chat_id,
                message_id=state.card_message_id,
                text=text,
                reply_markup=keyboard,
            )
        except TelegramBadRequest:
            # Edit failed (rare; same content?) — non-fatal.
            pass

    async def _finalize_question(self, tool_use_id: str) -> None:
        state = self._pending_questions.pop(tool_use_id, None)
        if state is None:
            return
        # Lock the card with a summary of what was answered.
        summary_lines = ["✓ Answered"]
        for i, ans in enumerate(state.answers):
            qtext = (
                state.questions[i].get("question", "?")
                if i < len(state.questions) else "?"
            )
            summary_lines.append(f"  {i + 1}. {qtext}")
            summary_lines.append(f"     → {ans}")
        try:
            await self._bot.edit_message_text(
                chat_id=state.chat_id,
                message_id=state.card_message_id,
                text="\n".join(summary_lines),
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        # Resolve via deny with answer-shaped JSON. The SDK serves the deny
        # message back to the model as the AskUserQuestion tool_result.
        payload = {"answers": state.answers}
        await state.runner.resolve_permission(
            tool_use_id, allow=False,
            deny_message=json.dumps(payload, ensure_ascii=False),
        )
        log.info(
            "question.finalized",
            tool_use_id=tool_use_id, answer_count=len(state.answers),
        )

    async def _handle_question_callback(self, query: CallbackQuery) -> None:
        data = query.data or ""
        rest = data[len(QB_PREFIX):]
        # rest format: <tool_use_id>:<q_idx>:<op_payload>
        # op_payload is one of: "<n>" (single-select tap), "t<n>" (toggle),
        # "S" (multiSelect submit).
        try:
            tool_use_id, q_idx_s, op = rest.rsplit(":", 2)
            q_idx = int(q_idx_s)
        except (ValueError, IndexError):
            await query.answer("malformed", show_alert=False)
            return
        state = self._pending_questions.get(tool_use_id)
        if state is None:
            await query.answer("this question has expired", show_alert=False)
            return
        if q_idx != state.current_idx:
            # Stale tap from a prior question card.
            await query.answer("already answered", show_alert=False)
            return

        q = state.questions[q_idx]
        options = q.get("options") or []
        multi = bool(q.get("multiSelect"))

        if multi:
            if op == "S":
                # Submit current selection.
                selected = sorted(state.selected.get(q_idx, set()))
                if not selected:
                    await query.answer("pick at least one option first", show_alert=False)
                    return
                values = [
                    str(options[i].get("value") or options[i].get("label") or "")
                    for i in selected if i < len(options) and isinstance(options[i], dict)
                ]
                state.answers.append(", ".join(values))
                await query.answer(f"selected {len(selected)}")
            elif op.startswith("t"):
                try:
                    o_idx = int(op[1:])
                except ValueError:
                    await query.answer("malformed", show_alert=False)
                    return
                sel = state.selected.setdefault(q_idx, set())
                if o_idx in sel:
                    sel.remove(o_idx)
                else:
                    sel.add(o_idx)
                await query.answer()
                await self._edit_question_card(tool_use_id, state)
                return
            else:
                await query.answer("malformed", show_alert=False)
                return
        else:
            try:
                o_idx = int(op)
            except ValueError:
                await query.answer("malformed", show_alert=False)
                return
            if o_idx < 0 or o_idx >= len(options):
                await query.answer("option out of range", show_alert=False)
                return
            opt = options[o_idx]
            if not isinstance(opt, dict):
                await query.answer("option malformed", show_alert=False)
                return
            value = str(opt.get("value") or opt.get("label") or "")
            state.answers.append(value)
            await query.answer(f"✓ {opt.get('label', value)}"[:60])

        # Advance to next question, or finalize if done.
        state.current_idx += 1
        if state.current_idx >= len(state.questions):
            await self._finalize_question(tool_use_id)
            return
        # New question — replace the card in place.
        await self._edit_question_card(tool_use_id, state)

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
        """Returns True if this CallbackQuery was for a permission button or
        an AskUserQuestion answer button. False means "not ours; let other
        handlers see it"."""
        if query.data and query.data.startswith(QB_PREFIX):
            await self._handle_question_callback(query)
            return True
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
        # Same for in-flight question cards.
        q_drop = [
            k for k, s in self._pending_questions.items() if s.runner is runner
        ]
        for k in q_drop:
            self._pending_questions.pop(k, None)
