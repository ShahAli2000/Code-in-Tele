#!/usr/bin/env bash
# Install the bridge and runner as long-running services.
#
# Detects the host OS and picks the right supervisor:
#   macOS  → launchd LaunchAgent (~/Library/LaunchAgents/<label>.plist)
#   Linux  → systemd --user unit  (~/.config/systemd/user/<label>.service)
#
# Run on the always-on host that runs both daemons:
#   bash deploy/install.sh
#
# Install only the runner (e.g. on a secondary machine that hosts a session
# but doesn't run the Telegram bot):
#   BRIDGE_ENABLED=0 bash deploy/install.sh
#
# Idempotent — re-running re-renders the unit files and reloads the services.

set -euo pipefail

BRIDGE_LABEL="${BRIDGE_LABEL:-ct-bridge}"
RUNNER_LABEL="${RUNNER_LABEL:-ct-runner}"
BRIDGE_ENABLED="${BRIDGE_ENABLED:-1}"
RUNNER_ENABLED="${RUNNER_ENABLED:-1}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "✗ no .env in $PROJECT_DIR — copy .env.example and fill it in first." >&2
    exit 1
fi

UV_PATH="$(command -v uv 2>/dev/null || true)"
if [[ -z "$UV_PATH" ]]; then
    for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
        if [[ -x "$candidate" ]]; then UV_PATH="$candidate"; break; fi
    done
fi
if [[ -z "$UV_PATH" || ! -x "$UV_PATH" ]]; then
    echo "✗ uv not on PATH. Install with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "  (or: brew install uv  on macOS)" >&2
    exit 1
fi
echo "✓ uv found at $UV_PATH"

# Soft warning — Claude Pro/Max OAuth users need the bundled CLI for `claude
# login`. API-key users (ANTHROPIC_API_KEY in .env) don't need this at all.
if [[ "$BRIDGE_ENABLED" = "1" || "$RUNNER_ENABLED" = "1" ]]; then
    if [[ ! -x "$HOME/.local/bin/claude" ]] && ! command -v claude >/dev/null 2>&1; then
        echo "ℹ Claude Code CLI not found." >&2
        echo "  If you're using ANTHROPIC_API_KEY (set in .env), this is fine." >&2
        echo "  If you're using Pro/Max OAuth, install + log in once:" >&2
        echo "    curl -fsSL https://claude.ai/install.sh | bash" >&2
        echo "    claude   # then /login" >&2
    fi
fi

mkdir -p "$PROJECT_DIR/state"

# Sync deps so the .venv exists before the supervisor starts the process.
"$UV_PATH" sync --project "$PROJECT_DIR" >/dev/null
echo "✓ uv sync ok"

OS="$(uname -s)"

render_template() {
    local template="$1" target="$2" label="$3"
    sed \
        -e "s|{{HOME}}|$HOME|g" \
        -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
        -e "s|{{LABEL}}|$label|g" \
        -e "s|{{UV_PATH}}|$UV_PATH|g" \
        "$template" > "$target"
    echo "  ✓ wrote $target"
}

# ---- macOS / launchd -----------------------------------------------------

install_launchd() {
    local label="$1" template="$2"
    local LA="$HOME/Library/LaunchAgents"
    mkdir -p "$LA"
    local plist="$LA/${label}.plist"
    render_template "$template" "$plist" "$label"
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    sleep 2
    if launchctl list | grep -q "$label"; then
        local pid
        pid=$(launchctl list | awk -v L="$label" '$3==L {print $1}')
        echo "  ✓ $label running, pid=$pid"
    else
        echo "  ⚠ $label loaded but not yet visible in launchctl list (KeepAlive will retry on crash)"
    fi
}

# ---- Linux / systemd --user ----------------------------------------------

install_systemd() {
    local label="$1" template="$2"
    local SD="$HOME/.config/systemd/user"
    mkdir -p "$SD"
    local unit="$SD/${label}.service"
    render_template "$template" "$unit" "$label"
    systemctl --user daemon-reload
    systemctl --user enable "${label}.service" >/dev/null
    systemctl --user restart "${label}.service"
    sleep 2
    if systemctl --user is-active --quiet "${label}.service"; then
        local pid
        pid=$(systemctl --user show -p MainPID --value "${label}.service")
        echo "  ✓ $label running, pid=$pid"
    else
        echo "  ⚠ $label not active — check: systemctl --user status ${label}.service"
    fi
}

# ---- dispatch ------------------------------------------------------------

install_one() {
    local label="$1" plist_template="$2" service_template="$3"
    case "$OS" in
        Darwin)
            install_launchd "$label" "$plist_template"
            ;;
        Linux)
            install_systemd "$label" "$service_template"
            ;;
        *)
            echo "✗ unsupported OS: $OS (deploy/install.sh supports macOS + Linux)" >&2
            exit 1
            ;;
    esac
    echo "  log: $PROJECT_DIR/state/${label#*-}.log"
}

if [[ "$RUNNER_ENABLED" = "1" ]]; then
    install_one "$RUNNER_LABEL" \
        "$PROJECT_DIR/deploy/runner.plist.template" \
        "$PROJECT_DIR/deploy/runner.service.template"
fi

if [[ "$BRIDGE_ENABLED" = "1" ]]; then
    # Give the runner a head-start so the bridge's connect-retry loop gets
    # lucky on the first attempt.
    sleep 1
    install_one "$BRIDGE_LABEL" \
        "$PROJECT_DIR/deploy/bridge.plist.template" \
        "$PROJECT_DIR/deploy/bridge.service.template"
fi

echo
case "$OS" in
    Darwin)
        echo "uninstall:"
        echo "  launchctl unload \"$HOME/Library/LaunchAgents/${BRIDGE_LABEL}.plist\" \"$HOME/Library/LaunchAgents/${RUNNER_LABEL}.plist\""
        echo "  rm \"$HOME/Library/LaunchAgents/${BRIDGE_LABEL}.plist\" \"$HOME/Library/LaunchAgents/${RUNNER_LABEL}.plist\""
        ;;
    Linux)
        echo "uninstall:"
        echo "  systemctl --user disable --now ${BRIDGE_LABEL}.service ${RUNNER_LABEL}.service"
        echo "  rm \"$HOME/.config/systemd/user/${BRIDGE_LABEL}.service\" \"$HOME/.config/systemd/user/${RUNNER_LABEL}.service\""
        echo
        echo "Tip: to keep services running after SSH logout/reboot, run once:"
        echo "  loginctl enable-linger \$USER"
        ;;
esac
