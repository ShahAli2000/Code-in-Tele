"""Manual smoke test for ct.sdk_adapter.adapter.SessionRunner.

Validates the Claude Agent SDK wiring (session lifecycle, streaming, tool-use
approvals) end-to-end without Telegram. Permission mode is 'default' so every
tool fires the approval flow — useful for confirming the can_use_tool path is
actually live.

Run from the repo root:
    uv run python scripts/smoke_sdk.py

Costs against your Claude credentials (Pro/Max OAuth or API key) — nothing
crazy, but treat each turn as a real Claude call.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from ct.sdk_adapter.adapter import PermissionRequest, SessionRunner


def _truncate(s: str, n: int = 400) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_input(d: dict) -> str:
    try:
        return _truncate(json.dumps(d, indent=2))
    except (TypeError, ValueError):
        return _truncate(repr(d))


async def make_permission_handler(runner_box: list[SessionRunner]):
    async def handler(req: PermissionRequest) -> None:
        runner = runner_box[0]
        print(f"\n  ─── tool approval ────────────────────")
        print(f"  tool: {req.tool_name}    use_id: {req.tool_use_id}")
        print(f"  input:\n{_format_input(req.input_data)}")
        choice = (await asyncio.to_thread(input, "  approve? (y/n/d=deny+reason): ")).strip().lower()
        if choice.startswith("y"):
            await runner.resolve_permission(req.tool_use_id, allow=True)
        elif choice.startswith("d"):
            reason = (await asyncio.to_thread(input, "  reason: ")).strip()
            await runner.resolve_permission(
                req.tool_use_id, allow=False, deny_message=reason or "denied"
            )
        else:
            await runner.resolve_permission(req.tool_use_id, allow=False)
        print("  ──────────────────────────────────────\n")

    return handler


async def consume_turn(runner: SessionRunner, user_text: str) -> None:
    async for msg in runner.turn(user_text):
        if isinstance(msg, SystemMessage):
            if msg.subtype not in ("init", "hook_started", "hook_response"):
                print(f"  [system:{msg.subtype}] {_truncate(repr(msg.data))}")
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    print(f"\n  Claude: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool_use: {block.name}({block.id})]")
                elif isinstance(block, ThinkingBlock):
                    print(f"  [thinking…]")
                elif isinstance(block, ToolResultBlock):
                    body = block.content if isinstance(block.content, str) else repr(block.content)
                    print(f"  [tool_result: {_truncate(body, 300)}]")
        elif isinstance(msg, ResultMessage):
            print(f"\n  [turn ended]")


async def main() -> int:
    cwd = os.getcwd()
    runner_box: list[SessionRunner] = []
    handler = await make_permission_handler(runner_box)

    runner = SessionRunner(
        cwd=cwd,
        permission_mode="default",  # prompt for every tool
        on_permission_request=handler,
    )
    runner_box.append(runner)

    try:
        await runner.start()
    except Exception as exc:
        print(f"  start failed: {exc}")
        return 1

    print(f"  cwd:  {cwd}")
    print(f"  mode: default (every tool prompts for approval)")
    print(f"  type 'quit' to exit\n")

    try:
        while True:
            user = await asyncio.to_thread(input, "  you: ")
            user = user.strip()
            if user.lower() in ("quit", "exit", "q"):
                break
            if not user:
                continue
            await consume_turn(runner, user)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await runner.stop()
        print("  bye.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
