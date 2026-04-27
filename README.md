# Claude–Telegram Bridge

Use Claude Code from Telegram. Each project becomes a forum topic in a Telegram supergroup; you talk to it like a chat. Built on the [Claude Agent SDK](https://docs.claude.com/en/agent-sdk/overview).

```
┌───── Telegram supergroup (forum mode) ─────┐
│  General │ project-A │ project-B │  ...    │   ← topics = sessions
└──────────────────┬─────────────────────────┘
                   │ long-poll
              ┌────▼─────┐
              │  bridge  │   aiogram + SQLite
              └────┬─────┘
                   │ WebSocket + HMAC over Tailscale
        ┌──────────┴───────────┐
        ▼                      ▼
   ┌─────────┐            ┌─────────┐
   │ runner  │   ...      │ runner  │   one per Mac, runs claude_agent_sdk
   └─────────┘            └─────────┘
```

## Status

Phase 0 — in-process MVP — in active development. See `~/.claude/plans/i-want-to-fully-giggly-leaf.md` for the full plan and phase breakdown.

## Phase −1: Setup (do this once, ~30 min)

You'll need to do these steps yourself before the bot can run. None of them require code changes.

### 1. Create a Telegram bot

1. Open Telegram, search for **`@BotFather`**, start a chat.
2. Send `/newbot`, follow prompts to pick a name and a `@username`.
3. Save the **bot token** BotFather gives you (looks like `1234567890:ABCdef...`). This goes into `TELEGRAM_BOT_TOKEN` in `.env`.
4. Optional: `/setprivacy` → **Disable** so the bot sees all messages in groups it's a member of.

### 2. Create a supergroup with forum topics enabled

1. Telegram → **New Group** → add at least one other contact (you can remove them after step 4) → name it (e.g. "Claude").
2. Group profile → **Edit** → **Group Type: Public** is *optional*; either works. Then **Topics: enable**. (You may need to upgrade the group to a supergroup first; Telegram does this automatically when you enable Topics or add many members.)
3. Add your bot to the group.
4. Group profile → **Administrators** → **Add Admin** → select your bot → grant at minimum **Manage Topics**. (Granting all admin permissions is fine for a single-user setup.)
5. You can now remove the seed contact from step 1 if you want a solo group.

### 3. Find the supergroup's `chat_id` AND your user ID

1. Send any message in the new group (e.g. "hello").
2. In a browser, open:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   Replace `<YOUR_BOT_TOKEN>` with the token from step 1.
3. Find the most recent `message` object. Two values to copy:
   - `chat.id` — a **negative integer** like `-1001234567890`. → `TELEGRAM_CHAT_ID` in `.env`.
   - `from.id` — a **positive integer** like `123456789`. That's your numeric Telegram user ID. → `TELEGRAM_ALLOWED_USER_IDS` in `.env`.
4. Alternative for the user ID: DM **`@userinfobot`** — it replies with your numeric ID.

### 4. Anthropic auth

Pick **one**:

- **API key** (recommended for SDK work). Get one at <https://platform.claude.com/>, set `ANTHROPIC_API_KEY` in `.env`.
- **Pro/Max OAuth via the bundled CLI**. Run `claude login` once on the machine that will execute Claude Code. Leave `ANTHROPIC_API_KEY` blank in `.env`. The SDK will pick up the bundled CLI's credentials automatically.

### 5. Generate the HMAC secret

For envelope auth between bridge and runners (Phase 3+), generate a random secret:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the output into `BRIDGE_HMAC_SECRET` in `.env`.

### 6. Tailscale (only needed when adding a second Mac in Phase 3)

Verify the Mac Studio is on your tailnet:

```bash
tailscale ip -4
```

Note the address — you'll need it later when registering the Mac via `/macs add`.

### 7. Drop your values into `.env`

```bash
cp .env.example .env
# edit .env with your real values
```

`.env` is git-ignored. Don't commit it.

## Local development

Once Phase 0 is wired up:

```bash
uv sync                              # install deps (.venv created automatically)
uv run python -m ct.bridge.main      # start the bridge
```

In the supergroup's General topic, type `/new test` — the bot should create a topic and respond. Then send a message in that topic to talk to Claude.

## Repository layout

```
src/ct/
  bridge/         # Telegram bot, command router, streaming UI, permissions UI
  runner/         # daemon owning Claude SDK lifecycles (one per Mac)
  sdk_adapter/    # wraps claude_agent_sdk (can_use_tool, hooks, session id capture)
  store/          # SQLite schema + DAL
  protocol/       # envelope dataclasses shared between bridge and runner
tests/            # pytest, asyncio
```

## Pitfalls (already designed against, don't re-introduce)

- Pending tool-approvals are keyed by **`tool_use_id`**, not `session_id`. A session-keyed pending map silently drops second-and-later approvals from the same turn.
- `can_use_tool` in Python only fires under streaming-input mode AND with a dummy `PreToolUse` hook returning `{"continue_": True}`. Without the hook, the SDK closes the stream before the callback runs.
- `set_permission_mode()` takes effect for the *next* tool request, not a pending one. Surface that explicitly in the topic header.
- Subagents inherit parent permission mode — flipping a session to `bypassPermissions` grants the same to anything spawned via the Agent tool.

See the full plan in `~/.claude/plans/i-want-to-fully-giggly-leaf.md` for the complete phase-by-phase build.
