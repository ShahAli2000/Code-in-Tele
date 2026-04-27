#!/usr/bin/env bash
# Install the Claude–Telegram bridge and runner as macOS LaunchAgents.
#
# Run on the Mac that hosts both daemons (typically your always-on Mac Studio):
#   bash deploy/install.sh
#
# To install only the runner (e.g. on a secondary Mac that hosts a session
# but doesn't run the Telegram bot):
#   BRIDGE_ENABLED=0 bash deploy/install.sh
#
# Idempotent — re-running re-renders the plists and reloads the agents.

set -euo pipefail

BRIDGE_LABEL="${BRIDGE_LABEL:-uk.shahrestani.ct-bridge}"
RUNNER_LABEL="${RUNNER_LABEL:-uk.shahrestani.ct-runner}"
BRIDGE_ENABLED="${BRIDGE_ENABLED:-1}"
RUNNER_ENABLED="${RUNNER_ENABLED:-1}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "✗ no .env in $PROJECT_DIR — copy .env.example and fill it in first." >&2
    exit 1
fi
if [[ ! -x "$HOME/.local/bin/uv" ]]; then
    echo "✗ uv not found at \$HOME/.local/bin/uv. Install with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
if [[ "$BRIDGE_ENABLED" = "1" || "$RUNNER_ENABLED" = "1" ]]; then
    if [[ ! -x "$HOME/.local/bin/claude" ]]; then
        echo "⚠ Claude Code CLI not found at \$HOME/.local/bin/claude." >&2
        echo "  Install with: curl -fsSL https://claude.ai/install.sh | bash" >&2
        echo "  Then run \`claude\` once and use /login to authenticate (Pro/Max)." >&2
    fi
fi

mkdir -p "$PROJECT_DIR/state"
mkdir -p "$LA"

render_plist() {
    local template="$1" target="$2" label="$3"
    sed \
        -e "s|{{HOME}}|$HOME|g" \
        -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
        -e "s|{{LABEL}}|$label|g" \
        "$template" > "$target"
    echo "  ✓ wrote $target"
}

reload_agent() {
    local plist="$1" label="$2"
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    sleep 2
    if launchctl list | grep -q "$label"; then
        local pid
        pid=$(launchctl list | awk -v L="$label" '$3==L {print $1}')
        echo "  ✓ $label running, pid=$pid"
    else
        echo "  ⚠ $label loaded but not visible in launchctl list yet (KeepAlive will retry on crash)"
    fi
}

# Sync deps (.venv must exist before launchd runs uv on a fresh deploy)
"$HOME/.local/bin/uv" sync --project "$PROJECT_DIR" >/dev/null
echo "✓ uv sync ok"

if [[ "$RUNNER_ENABLED" = "1" ]]; then
    render_plist \
        "$PROJECT_DIR/deploy/runner.plist.template" \
        "$LA/${RUNNER_LABEL}.plist" \
        "$RUNNER_LABEL"
    reload_agent "$LA/${RUNNER_LABEL}.plist" "$RUNNER_LABEL"
    echo "  log: $PROJECT_DIR/state/runner.log"
fi

if [[ "$BRIDGE_ENABLED" = "1" ]]; then
    # Give the runner a head-start so the bridge's connect-retry loop gets
    # lucky on the first attempt.
    sleep 1
    render_plist \
        "$PROJECT_DIR/deploy/bridge.plist.template" \
        "$LA/${BRIDGE_LABEL}.plist" \
        "$BRIDGE_LABEL"
    reload_agent "$LA/${BRIDGE_LABEL}.plist" "$BRIDGE_LABEL"
    echo "  log: $PROJECT_DIR/state/bridge.log"
fi

echo
echo "uninstall:"
echo "  launchctl unload \"$LA/${BRIDGE_LABEL}.plist\" \"$LA/${RUNNER_LABEL}.plist\""
echo "  rm \"$LA/${BRIDGE_LABEL}.plist\" \"$LA/${RUNNER_LABEL}.plist\""
