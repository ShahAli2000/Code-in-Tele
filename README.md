# Code-in-Tele

> Use Claude Code from Telegram. Each project becomes a forum topic in a supergroup; you talk to it like a chat. One always-on bridge handles Telegram + a fleet of runners (Mac and Linux) over Tailscale executes the actual work.

Built on the [Claude Agent SDK](https://docs.claude.com/en/agent-sdk/overview) and [aiogram](https://docs.aiogram.dev/).

```
┌───── Telegram supergroup (forum mode) ─────┐
│  General │ project-A │ project-B │  ...    │   ← topics = sessions
└──────────────────┬─────────────────────────┘
                   │ long-poll
              ┌────▼─────┐
              │  bridge  │   aiogram + SQLite + dashboard
              └────┬─────┘
                   │ WebSocket + HMAC over Tailscale
        ┌──────────┴───────────┐
        ▼                      ▼
   ┌─────────┐            ┌─────────┐
   │ runner  │   ...      │ runner  │   one per host, runs claude_agent_sdk
   └─────────┘            └─────────┘
```

## Features

- **Per-project sessions** — `/new myproject dir=~/code/myproject` spins up a forum topic with its own Claude Code session, working directory, model, effort level, and approval mode. Send a message in the topic to talk to Claude.
- **Multi-host fleet** — register additional Macs or Linux boxes with `/macs add NAME tailscale-ip`. Pick a runner per session: `/new myproject mac=laptop`. Each session lives on the runner's filesystem.
- **Quiet UX** — chat shows your question, then the answer. Claude's tool use, intermediate reasoning, and thinking blocks live on the web dashboard, not in Telegram. Long turns get heartbeats (`working… (3m)`) instead of streaming clutter.
- **Adaptive thinking on by default** — the SDK's adaptive-thinking budget is enabled per session. Toggle off with `/think off`.
- **Permission cards** — destructive tool uses surface as inline `[Approve] [Approve + remember] [Deny]` buttons. The remember option scopes auto-allow to that tool name for the rest of the session.
- **Web dashboard** — read-only view of every session, transcript, pending approval, recent activity. Bound to your Tailscale IP by default; expose externally with `/tunnel on` (cloudflared Quick Tunnel).
- **Media uploads from your phone** — photos, documents, voice notes. Voice gets transcribed locally with `mlx-whisper` (Apple Silicon only). Files land under `<cwd>/_uploads/` on the runner.
- **Resilience** — bridge restarts preserve sessions via SDK `resume=`. Runner reconnects auto-reattach. `/resume` re-attaches sessions whose runner was offline at boot. `/undo` reverses the last destructive action (close, profile delete) within 30 minutes. `/move mac=laptop` migrates a session between hosts with the transcript intact.
- **Profiles** — `/save myproj dir=~/code/myproj effort=high` saves a config you can launch with `/new myproj`. Per-profile system prompts let you bake project-specific guidance.
- **Operational basics** — `/stats`, `/logs`, `/export`, `/search`, `/get` (download a file from a runner), `/macs` (green/yellow/red health per runner), `/quiet 22:00-07:00` (silent notifications window).

## What you'll need

- **Telegram account** + a phone or desktop client.
- **At least one always-on host** (macOS or Linux) on Tailscale. This runs both the bridge and a local runner.
- **Optional secondary hosts** to spread sessions across (your laptop, a VPS, a Linux box). Each one needs Tailscale + the runner installed.
- **Anthropic auth** — *one* of:
  - **Claude Pro / Max subscription** + the bundled CLI (`claude login`). Recommended; flat-rate, no per-token billing for sessions.
  - **`ANTHROPIC_API_KEY`** from <https://platform.claude.com/>. Pay per token; works without a subscription.
- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) on every host that runs the bridge or a runner.
- **About 30 minutes** for first-time setup.

## Quick start

```bash
git clone https://github.com/<YOU>/Code-in-Tele.git
cd Code-in-Tele
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_USER_IDS,
# BRIDGE_HMAC_SECRET, and (optionally) ANTHROPIC_API_KEY.
bash deploy/install.sh
```

Then in the supergroup's General topic, type `/new test`. The bot creates a topic and posts "session ready." Send a message in that topic to talk to Claude.

If something fails, see the rest of this README — the steps below cover the bits the quick start glossed over.

## Setup walkthrough

### 1. Create the Telegram bot

1. Open Telegram, search for **`@BotFather`**, start a chat.
2. Send `/newbot`, follow prompts to pick a display name and a `@username`.
3. Save the **bot token** BotFather gives you (looks like `1234567890:ABCdef...`). Goes into `TELEGRAM_BOT_TOKEN` in `.env`.
4. Optional: `/setprivacy` → **Disable** so the bot sees all messages in the group, not just commands.

### 2. Create the supergroup with topics enabled

1. Telegram → **New Group** → add at least one other contact (you can remove them after step 4) → name it (e.g. `Code-in-Tele`).
2. Group profile → **Edit** → **Topics: enable**. (Telegram automatically upgrades the group to a supergroup when you enable Topics.)
3. Add your bot to the group.
4. Group profile → **Administrators** → **Add Admin** → select your bot → grant at minimum **Manage Topics**. (Granting all admin permissions is fine for a single-user setup.)
5. You can remove the seed contact from step 1 if you want a solo group.

### 3. Find the supergroup's chat ID and your user ID

1. Send any message in the new group (e.g. "hello").
2. In a browser, open:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. Find the most recent `message` object. Two values to copy:
   - `chat.id` — a **negative integer** like `-1001234567890`. → `TELEGRAM_CHAT_ID` in `.env`.
   - `from.id` — a **positive integer** like `123456789`. That's your numeric Telegram user ID. → `TELEGRAM_ALLOWED_USER_IDS` in `.env` (comma-separated if you want to allow more users).

Alternative for the user ID: DM **`@userinfobot`** — it replies with your numeric ID.

### 4. Anthropic auth — pick one

**Option A: Pro/Max OAuth (recommended).** On every host that will run the bridge *or* a runner:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude   # then /login in the prompt
```

Leave `ANTHROPIC_API_KEY` blank in `.env`. The SDK picks up the OAuth credentials from the keychain.

**Option B: API key.** Get one at <https://platform.claude.com/>, set `ANTHROPIC_API_KEY=sk-ant-...` in `.env`. No CLI install needed.

### 5. Install Tailscale on every host

<https://tailscale.com/download>. Sign in with the same account on every device. Note each host's tailnet IP (`tailscale ip -4`) — you'll register additional hosts later with `/macs add NAME 100.x.y.z`.

### 6. Generate the HMAC secret

The bridge and runners HMAC-sign every envelope between them. Generate one secret, **share it across every `.env` on every host**:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the output into `BRIDGE_HMAC_SECRET=` on every host's `.env`.

### 7. Install on the always-on host

```bash
git clone https://github.com/<YOU>/Code-in-Tele.git
cd Code-in-Tele
cp .env.example .env
# Edit .env with the values from steps 1, 3, 4, 6.
bash deploy/install.sh
```

`install.sh` auto-detects your OS and installs the right supervisor:
- **macOS** → LaunchAgent in `~/Library/LaunchAgents/`
- **Linux** → systemd `--user` service in `~/.config/systemd/user/`

On Linux, run `loginctl enable-linger $USER` once so the services keep running after SSH logout.

Verify:
- macOS: `launchctl list | grep ct-`
- Linux: `systemctl --user status ct-bridge ct-runner`
- Logs: `tail -F state/bridge.log`

In the supergroup's General topic, type `/new test`. You should see a "session ready" message in a new topic. Send a message in that topic to talk to Claude.

### 8. Add a secondary host (optional)

On any other Mac or Linux box on the same tailnet, repeat steps 4–6, then:

```bash
git clone https://github.com/<YOU>/Code-in-Tele.git
cd Code-in-Tele
cp .env.example .env
# Edit .env. Set CT_RUNNER_HOST to this host's tailnet IP so the
# bridge can reach it (default 127.0.0.1 only listens on loopback).
BRIDGE_ENABLED=0 bash deploy/install.sh
```

`BRIDGE_ENABLED=0` installs only the runner — the Telegram bridge stays on the always-on host.

Then in Telegram, register the new host with the bridge:

```
/macs add laptop 100.x.y.z
```

Now `/new myproj mac=laptop` opens a session on the laptop runner.

## Configuration reference

All settings come from `.env`. See [`.env.example`](.env.example) for inline comments. Key vars:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✓ | — | Bot token from BotFather. |
| `TELEGRAM_CHAT_ID` | ✓ | — | Negative integer; the supergroup ID. |
| `TELEGRAM_ALLOWED_USER_IDS` | ✓ | — | Comma-separated user IDs allowed to interact. |
| `BRIDGE_HMAC_SECRET` | ✓ | — | Random 32+ char secret. Same value on every host's `.env`. |
| `ANTHROPIC_API_KEY` | optional | — | Sets up API-key auth. Leave blank to use Pro/Max OAuth. |
| `CT_DB_PATH` | — | `./state/ct.db` | SQLite store. Auto-created. |
| `CT_RUNNER_HOST` | — | `127.0.0.1` | Where the runner listens on this host. Set to the tailnet IP on a runner-only host. |
| `CT_RUNNER_PORT` | — | `8765` | Runner WebSocket port. |
| `CT_DASHBOARD_HOST` | — | (auto: tailnet IP) | Where the web dashboard binds. Auto-detects via `tailscale ip -4`; falls back to `127.0.0.1` if Tailscale CLI is missing. Set explicitly to override. |
| `CT_DASHBOARD_PORT` | — | `8766` | Dashboard port. |
| `CT_LOG_LEVEL` | — | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `CT_LOG_FORMAT` | — | `console` | `console` (human) or `json` (structured). |

## Optional add-ons

### Web dashboard (always on)

The dashboard auto-starts with the bridge. After install, the bridge logs the URL with the auth token:

```
dashboard.url   local='http://100.x.y.z:8766/?token=...'
```

Bookmark it. The token is generated on first boot and persisted, so the URL keeps working across restarts. Reachable from any device on your tailnet.

### Off-tailnet access via cloudflared (`/tunnel on`)

To open the dashboard from outside Tailscale (your phone on cellular, a friend's browser):

```bash
brew install cloudflared        # macOS
# or, on Linux: see https://pkg.cloudflare.com/
```

Then in Telegram: `/tunnel on`. The bot replies with a `https://*.trycloudflare.com` URL with the token appended. `/tunnel off` to tear down.

### SVG → PNG conversion (`mcp__ct__svg_to_png` tool)

Claude can generate SVGs. To render them to PNGs, install `librsvg` on the runner:

```bash
brew install librsvg                          # macOS
sudo apt install librsvg2-bin                 # Debian/Ubuntu
sudo dnf install librsvg2-tools               # Fedora
```

The tool is auto-discovered; ask Claude to "convert this SVG to PNG" in any session.

### Voice transcription (Apple Silicon only)

`mlx-whisper` runs locally on Apple Silicon. Voice notes you send into a session get transcribed and the transcript becomes the prompt. No setup needed beyond `bash deploy/install.sh` (the dependency is auto-pinned to Apple Silicon hosts).

## Architecture

- **Bridge** — Telegram bot, command router, topic↔session map, web dashboard. One per fleet, runs on the always-on host. Long-polls Telegram so you don't need to expose a webhook.
- **Runner** — daemon that owns Claude Agent SDK lifecycles. One per host. Bound to the tailnet IP, authenticated by HMAC.
- **Store** — SQLite on the always-on host. Sessions, profiles, mac registry, pending approvals, message log. Schema migrations run forward-only on boot.
- **Transport** — WebSocket + JSON envelopes over Tailscale. HMAC covers in-flight auth; Tailscale provides the trust boundary.

Both the bridge and the local runner can live in the same process for development (`uv run python -m ct.bridge.main`) or as separate supervised services in production (what `deploy/install.sh` sets up).

## Repository layout

```
src/ct/
  bridge/         # Telegram bot, command router, streaming UI, permissions UI
  runner/         # daemon owning Claude SDK lifecycles (one per host)
  sdk_adapter/    # wraps claude_agent_sdk (can_use_tool, hooks, session-id capture)
  dashboard/      # aiohttp web UI (sessions, transcripts, approvals, settings)
  store/          # SQLite schema + DAL + migrations
  protocol/       # envelope dataclasses shared between bridge and runner
deploy/
  install.sh                  # OS-detecting installer (launchd or systemd)
  bridge.plist.template       # macOS LaunchAgent
  runner.plist.template
  bridge.service.template     # Linux systemd --user unit
  runner.service.template
tests/            # pytest, asyncio
```

## Troubleshooting

**Bot doesn't reply when I message a topic.**
- Check the bot is actually a member of the group (not just a contact in the BotFather chat).
- Confirm `TELEGRAM_CHAT_ID` is the *negative* integer from `getUpdates`.
- Confirm your user ID is in `TELEGRAM_ALLOWED_USER_IDS`. Bots silently reject everyone else.
- Tail `state/bridge.log` for `aiogram.dispatcher` errors.

**`/new myproj` says "couldn't open session on runner".**
- The runner isn't reachable. Check `tailscale status` from the bridge host can ping the runner host.
- Confirm `BRIDGE_HMAC_SECRET` is the same in `.env` on both hosts.
- Tail `state/runner.log` on the target host.

**`/macs` shows 🟡 or 🔴 next to a host.**
- 🟡 = WebSocket connected but pings stalling. Likely the host went to sleep mid-conversation.
- 🔴 = WebSocket disconnected. Auto-reconnect retries with backoff (1s/2s/5s/15s/60s cap).
- Wake the host (open the lid / `wake-on-lan` / `mosh in`) and watch `/macs` again in 60 s.

**Sessions show "orphaned" after a reboot.**
- The runner was unreachable when the bridge restored sessions on boot. Once the runner is back online, run `/resume` in General — buttons appear for each orphaned session.

**Voice messages aren't being transcribed.**
- Voice transcription is Apple-Silicon-only via `mlx-whisper`. On Linux/Intel hosts, the runner returns the audio file at `<cwd>/_uploads/<timestamp>-voice.ogg` without transcription; you'd need to ask Claude to read/process it directly.

**Bridge or runner won't start after editing code.**
- macOS: `launchctl kickstart -k gui/$(id -u)/ct-bridge` (or `ct-runner`)
- Linux: `systemctl --user restart ct-bridge.service`
- Tail logs immediately: `tail -F state/bridge.log`
- Common cause: a syntax error. `uv run python -c "import ct.bridge.main"` to surface it.

## Contributing

PRs welcome. The project is small enough that there's no formal CONTRIBUTING.md yet — open an issue to discuss bigger changes first. Coding conventions:

- Python 3.12+, async-first
- `ruff` for lint; `mypy` for typing where it helps
- `uv run pytest` for tests
- Conventional-commit-style messages (`feat:`, `fix:`, `refactor:`, `docs:`, …)

## License

MIT — see [LICENSE](LICENSE).
