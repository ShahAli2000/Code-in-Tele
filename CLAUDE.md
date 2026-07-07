# Code-in-Tele (Claude-Telegram)

Telegram bridge to Claude Code: each project is a forum topic in a Telegram supergroup. An always-on **bridge** (aiogram + SQLite + web dashboard) talks to per-host **runners** (WebSocket + HMAC-signed envelopes over Tailscale) that own Claude Agent SDK sessions. Python 3.12+, async-first, uv-managed. Public repo: github.com/ShahAli2000/Code-in-Tele — README.md is the full user-facing setup/troubleshooting doc.

## Commands

- `uv sync` — install deps
- `uv run pytest` — test suite (44 tests; `asyncio_mode=auto`, so no `@pytest.mark.asyncio` markers)
- `uv run ruff check src tests` — lint (line-length 100). NOT clean: ~116 pre-existing errors. Don't mass-fix; just add no new ones.
- `uv run mypy src` — also not clean (~37 pre-existing errors); same rule.
- `uv run python -m ct.bridge.main` — run bridge in foreground (dev)
- `uv run python -m ct.runner.main` — run runner in foreground (dev)
- `uv run python scripts/smoke_sdk.py` — end-to-end SDK smoke test without Telegram; spends real Claude tokens
- `uv run python -c "import ct.bridge.main"` — quick import/syntax check after edits

### Deploy / services (launchd on macOS, systemd --user on Linux; auto-detected)

- `bash deploy/install.sh` — install/re-render both services; `BRIDGE_ENABLED=0` = runner only. Requires a filled `.env`.
- `bash deploy/upgrade.sh` — `git pull --ff-only` + `uv sync` + reload; refuses on a dirty tree (`FORCE=1` overrides).
- Restart after code edits: `launchctl kickstart -k gui/$(id -u)/ct-bridge` (or `ct-runner`); Linux: `systemctl --user restart ct-bridge`. Logs: `tail -F state/bridge.log` / `state/runner.log`.
- Multi-host upgrade order: **runners first, then bridge** — an older runner rejects unknown envelopes from a newer bridge.
- The live deployment topology (which host runs what, service labels, the frozen-git-checkout special case) lives in project memory (`deploy-topology`) — consult it before deploying; don't rediscover.

## Architecture

Data flow: Telegram long-poll → bridge (`ct.bridge`) → HMAC-signed JSON envelopes over WebSocket → runner (`ct.runner`) → `claude_agent_sdk` session → events stream back → quiet renderer posts only heartbeats, permission cards, and the final answer in the topic; the full transcript (tool use, thinking) goes to SQLite and the web dashboard.

- `src/ct/bridge/` — Telegram side, one per fleet
  - `bot.py` — command router + all handlers (~3.6k lines; deliberately monolithic — a split was considered and rejected)
  - `main.py` — entry point; also owns the `BOT_COMMANDS` autocomplete list
  - `runner_client.py` — `RunnerPool`: one WS client per runner, auto-reconnect with backoff (1/2/5/15/60s cap)
  - `streaming.py` — quiet-UX renderer; `permissions_ui.py` — inline Approve/Deny cards; `menu.py`, `topics.py`, `sessions.py`, `transcription.py` (mlx-whisper voice)
- `src/ct/runner/server.py` — per-host daemon; WS server on `CT_RUNNER_HOST:CT_RUNNER_PORT` (default 8765), owns SDK lifecycles, idle-reaps sessions after `CT_IDLE_REAP_MINUTES` (default 30)
- `src/ct/sdk_adapter/` — wraps `claude_agent_sdk`: `SessionRunner` (can_use_tool → approval flow, session-id capture for `resume=`), `tools.py` (svg_to_png MCP tool)
- `src/ct/protocol/` — envelope dataclasses + HMAC auth shared by bridge and runner. Any change here is a protocol change; see upgrade order above.
- `src/ct/store/db.py` — SQLite DAL; migrations are forward-only idempotent `ALTER TABLE`s in `Db._migrate`, applied at boot. Add new columns; never rewrite existing migration steps.
- `src/ct/dashboard/` — aiohttp read-only web UI running as a sibling app inside the bridge process (same loop, same DB); token auth; optional cloudflared tunnel (`/tunnel on`)
- `src/ct/config.py` — all settings load from `.env` via `load_settings()`; vars documented in `.env.example` and the README table
- `deploy/` — install/upgrade scripts + plist/systemd templates · `tests/` — pytest · `state/` — runtime SQLite + logs (gitignored)

## Gotchas

- `.env` here holds real secrets (bot token, HMAC secret) and `state/ct.db` may be the live production DB if this checkout is a deployment host — never commit either, never hand-edit the DB. Staging rules: git-hygiene memory.
- Code edits do nothing to a supervised install until you reload the service (kickstart/systemctl above).
- Bridge refuses to boot with >1 ID in `TELEGRAM_ALLOWED_USER_IDS` unless `CT_ALLOW_MULTIPLE_USERS=1`.
- Runner file ops are path-contained to `$HOME` (override: `CT_RUNNER_FS_ROOTS=/path1:/path2`); HMAC envelopes have a ±60 s replay window; WS frames capped at 32 MiB.
- `mlx-whisper` + bundled ffmpeg install only on Apple Silicon (pyproject env markers) — code must tolerate their absence on Linux/Intel.
- Keep README.md in sync when changing commands, env vars, or deploy scripts.
