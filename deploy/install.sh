#!/usr/bin/env bash
# Install the Claude–Telegram bridge as a macOS LaunchAgent.
#
# Run on the Mac that will host the bridge (typically your always-on Mac Studio):
#   bash deploy/install.sh
#
# Idempotent — re-running re-renders the plist and reloads the agent. Safe to
# use after `git pull` to pick up any plist-template changes.

set -euo pipefail

LABEL="${BRIDGE_LABEL:-uk.shahrestani.ct-bridge}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_TEMPLATE="$PROJECT_DIR/deploy/bridge.plist.template"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "✗ no .env in $PROJECT_DIR — copy .env.example and fill it in first." >&2
    exit 1
fi
if [[ ! -x "$HOME/.local/bin/uv" ]]; then
    echo "✗ uv not found at \$HOME/.local/bin/uv. Install with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
if [[ ! -x "$HOME/.local/bin/claude" ]]; then
    echo "⚠ Claude Code CLI not found at \$HOME/.local/bin/claude." >&2
    echo "  Install with: curl -fsSL https://claude.ai/install.sh | bash" >&2
    echo "  Then run \`claude\` once and use /login to authenticate (Pro/Max)." >&2
    echo "  Continuing anyway — bot will fail at runtime if SDK can't authenticate." >&2
fi

mkdir -p "$PROJECT_DIR/state"
mkdir -p "$HOME/Library/LaunchAgents"

# Render the template
sed \
    -e "s|{{HOME}}|$HOME|g" \
    -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
    -e "s|{{LABEL}}|$LABEL|g" \
    "$PLIST_TEMPLATE" > "$PLIST_TARGET"

echo "✓ wrote $PLIST_TARGET"

# Sync deps so .venv exists before launchd tries to run uv
"$HOME/.local/bin/uv" sync --project "$PROJECT_DIR" >/dev/null
echo "✓ uv sync ok"

# Reload (unload-then-load is idempotent across version of launchctl)
launchctl unload "$PLIST_TARGET" 2>/dev/null || true
launchctl load "$PLIST_TARGET"
echo "✓ launchctl loaded $LABEL"

sleep 2
if launchctl list | grep -q "$LABEL"; then
    pid=$(launchctl list | awk -v L="$LABEL" '$3==L {print $1}')
    echo "✓ running, pid=$pid"
    echo "  log: $PROJECT_DIR/state/bridge.log"
    echo "  uninstall: launchctl unload \"$PLIST_TARGET\" && rm \"$PLIST_TARGET\""
else
    echo "✗ launchctl couldn't find $LABEL after load" >&2
    exit 1
fi
