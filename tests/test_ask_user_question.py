"""AskUserQuestion answer flow: option taps collect into state.answers; the
final resolve passes JSON-shaped {"answers": [...]} as the deny message
which the SDK serves as the tool_result content."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from ct.bridge.permissions_ui import (
    PermissionsUI,
    QB_PREFIX,
    _QuestionState,
)
from ct.sdk_adapter.adapter import PermissionRequest


def _make_ui() -> PermissionsUI:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=12345))
    bot.edit_message_text = AsyncMock()
    db = MagicMock()
    return PermissionsUI(bot=bot, db=db)


def _make_runner() -> MagicMock:
    runner = MagicMock()
    runner.resolve_permission = AsyncMock(return_value=True)
    return runner


def _make_query(callback_data: str) -> MagicMock:
    q = MagicMock()
    q.data = callback_data
    q.answer = AsyncMock()
    return q


async def test_single_question_single_select_finalizes_with_chosen_value() -> None:
    ui = _make_ui()
    runner = _make_runner()
    request = PermissionRequest(
        tool_use_id="toolu_q1",
        tool_name="AskUserQuestion",
        input_data={
            "questions": [{
                "question": "Pick one",
                "header": "Color",
                "multiSelect": False,
                "options": [
                    {"label": "Red", "value": "red"},
                    {"label": "Blue", "value": "blue"},
                ],
            }],
        },
        agent_id=None,
    )

    await ui.render_card(
        runner=runner, chat_id=-100, thread_id=42, request=request,
    )
    # Initial card sent
    assert ui._bot.send_message.await_count == 1

    # User taps "Blue" (option 1)
    q = _make_query(f"{QB_PREFIX}toolu_q1:0:1")
    handled = await ui.handle_callback(q)
    assert handled is True

    # Resolution: deny with JSON-shaped {"answers": ["blue"]}
    runner.resolve_permission.assert_awaited_once()
    call = runner.resolve_permission.await_args
    assert call.args[0] == "toolu_q1"
    assert call.kwargs["allow"] is False
    payload = json.loads(call.kwargs["deny_message"])
    assert payload == {"answers": ["blue"]}


async def test_multi_select_toggle_then_submit() -> None:
    ui = _make_ui()
    runner = _make_runner()
    request = PermissionRequest(
        tool_use_id="toolu_ms",
        tool_name="AskUserQuestion",
        input_data={
            "questions": [{
                "question": "Pick any",
                "multiSelect": True,
                "options": [
                    {"label": "A", "value": "a"},
                    {"label": "B", "value": "b"},
                    {"label": "C", "value": "c"},
                ],
            }],
        },
        agent_id=None,
    )
    await ui.render_card(runner=runner, chat_id=-100, thread_id=42, request=request)

    # Toggle A and C
    await ui.handle_callback(_make_query(f"{QB_PREFIX}toolu_ms:0:t0"))
    await ui.handle_callback(_make_query(f"{QB_PREFIX}toolu_ms:0:t2"))
    # Should not have resolved yet
    runner.resolve_permission.assert_not_called()

    # Submit
    await ui.handle_callback(_make_query(f"{QB_PREFIX}toolu_ms:0:S"))
    runner.resolve_permission.assert_awaited_once()
    payload = json.loads(runner.resolve_permission.await_args.kwargs["deny_message"])
    assert payload == {"answers": ["a, c"]}


async def test_multi_question_sequential() -> None:
    ui = _make_ui()
    runner = _make_runner()
    request = PermissionRequest(
        tool_use_id="toolu_mq",
        tool_name="AskUserQuestion",
        input_data={
            "questions": [
                {
                    "question": "Q1",
                    "options": [{"label": "X", "value": "x"}, {"label": "Y", "value": "y"}],
                },
                {
                    "question": "Q2",
                    "options": [{"label": "P", "value": "p"}, {"label": "Q", "value": "q"}],
                },
            ],
        },
        agent_id=None,
    )
    await ui.render_card(runner=runner, chat_id=-100, thread_id=42, request=request)

    # Answer Q1 → "y"
    await ui.handle_callback(_make_query(f"{QB_PREFIX}toolu_mq:0:1"))
    runner.resolve_permission.assert_not_called()  # still 1 question to go

    # Answer Q2 → "p"
    await ui.handle_callback(_make_query(f"{QB_PREFIX}toolu_mq:1:0"))
    runner.resolve_permission.assert_awaited_once()
    payload = json.loads(runner.resolve_permission.await_args.kwargs["deny_message"])
    assert payload == {"answers": ["y", "p"]}


async def test_free_text_question_falls_back_to_natural_reply() -> None:
    """Questions without options aren't supported via buttons (no ForceReply
    plumbing yet). The bot posts the question and asks the user to type a
    reply naturally; resolves with a deny message hinting at the next turn."""
    ui = _make_ui()
    runner = _make_runner()
    request = PermissionRequest(
        tool_use_id="toolu_ft",
        tool_name="AskUserQuestion",
        input_data={
            "questions": [{"question": "Free text Q", "options": []}],
        },
        agent_id=None,
    )
    await ui.render_card(runner=runner, chat_id=-100, thread_id=42, request=request)

    # Should have posted the question text and resolved with a hint.
    assert ui._bot.send_message.await_count == 1
    runner.resolve_permission.assert_awaited_once()
    args = runner.resolve_permission.await_args
    assert args.kwargs["allow"] is False
    assert "next message" in args.kwargs["deny_message"]


async def test_stale_tap_does_not_finalize_again() -> None:
    """If the user taps a button on a question whose answer was already
    recorded (e.g. user double-taps), we no-op gracefully — never call
    resolve_permission a second time."""
    ui = _make_ui()
    runner = _make_runner()
    request = PermissionRequest(
        tool_use_id="toolu_dt",
        tool_name="AskUserQuestion",
        input_data={
            "questions": [{
                "question": "Q",
                "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
            }],
        },
        agent_id=None,
    )
    await ui.render_card(runner=runner, chat_id=-100, thread_id=42, request=request)

    # First tap finalizes.
    await ui.handle_callback(_make_query(f"{QB_PREFIX}toolu_dt:0:0"))
    runner.resolve_permission.assert_awaited_once()

    # Second tap on same callback → "expired" path (state already popped).
    second = _make_query(f"{QB_PREFIX}toolu_dt:0:1")
    await ui.handle_callback(second)
    assert runner.resolve_permission.await_count == 1
    # Should have answered with an alert/notice, not raised.
    second.answer.assert_awaited()
