#!/usr/bin/env bash
# Upgrade an existing Code-in-Tele install on this host.
#
# What it does, in order:
#   1. Refuses if the working tree has uncommitted changes (override with FORCE=1).
#   2. git fetch + git pull --ff-only on origin/main (override branch with BRANCH=).
#   3. uv sync to pick up any dependency changes.
#   4. Detects which services are running on this host (bridge and/or runner)
#      and reloads only those — no surprise reactivation of a service the
#      user disabled.
#
# Run on every host that hosts a daemon. Order across hosts matters when the
# protocol changes: upgrade RUNNERS first, then the bridge. (An old runner
# rejects unknown envelopes from a newer bridge as protocol errors; the
# reverse is fine — newer runners still understand older bridges.)
#
# Usage:
#   bash deploy/upgrade.sh                # standard upgrade
#   FORCE=1 bash deploy/upgrade.sh        # proceed even with a dirty tree
#   BRANCH=develop bash deploy/upgrade.sh # pull from a different branch

set -euo pipefail

BRIDGE_LABEL="${BRIDGE_LABEL:-ct-bridge}"
RUNNER_LABEL="${RUNNER_LABEL:-ct-runner}"
BRANCH="${BRANCH:-main}"
FORCE="${FORCE:-0}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -d .git ]]; then
    echo "✗ $PROJECT_DIR is not a git checkout — upgrade.sh expects a clone." >&2
    exit 1
fi

# ---- pre-flight: clean tree -----------------------------------------------

if [[ "$FORCE" != "1" ]]; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "✗ working tree has uncommitted changes:" >&2
        git status --short >&2
        echo >&2
        echo "  Commit, stash, or discard them first." >&2
        echo "  Override (NOT recommended) with: FORCE=1 bash deploy/upgrade.sh" >&2
        exit 1
    fi
fi

# ---- fetch + report -------------------------------------------------------

echo "→ fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"

LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse "origin/$BRANCH")"

if [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]]; then
    echo "✓ already at latest ($LOCAL_HEAD) — nothing to pull"
    SKIP_PULL=1
else
    SKIP_PULL=0
    AHEAD=$(git rev-list --count "$LOCAL_HEAD".."$REMOTE_HEAD")
    echo "→ $AHEAD new commit(s) on origin/$BRANCH:"
    git --no-pager log --oneline "$LOCAL_HEAD".."$REMOTE_HEAD" | sed 's/^/    /'
    echo
fi

# ---- pull -----------------------------------------------------------------

if [[ "$SKIP_PULL" != "1" ]]; then
    echo "→ git pull --ff-only origin $BRANCH"
    git pull --ff-only origin "$BRANCH"
    echo "✓ now at $(git rev-parse --short HEAD)"
fi

# ---- uv sync (always; cheap if nothing changed) ---------------------------

UV_PATH="$(command -v uv 2>/dev/null || true)"
if [[ -z "$UV_PATH" ]]; then
    for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
        if [[ -x "$candidate" ]]; then UV_PATH="$candidate"; break; fi
    done
fi
if [[ -z "$UV_PATH" || ! -x "$UV_PATH" ]]; then
    echo "✗ uv not on PATH — install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
echo "→ uv sync"
"$UV_PATH" sync --project "$PROJECT_DIR" >/dev/null
echo "✓ deps in sync"

# ---- detect + reload installed services -----------------------------------

OS="$(uname -s)"

reload_launchd() {
    local label="$1"
    local plist="$HOME/Library/LaunchAgents/${label}.plist"
    if [[ ! -f "$plist" ]]; then
        return 1  # not installed on this host
    fi
    echo "→ reloading $label"
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    sleep 2
    if launchctl list | grep -q "$label"; then
        local pid
        pid=$(launchctl list | awk -v L="$label" '$3==L {print $1}')
        echo "  ✓ $label running, pid=$pid"
    else
        echo "  ⚠ $label loaded but not yet visible (KeepAlive will retry)"
    fi
    return 0
}

reload_systemd() {
    local label="$1"
    if ! systemctl --user list-unit-files "${label}.service" --no-legend 2>/dev/null \
            | grep -q "${label}.service"; then
        return 1
    fi
    echo "→ restarting $label"
    systemctl --user daemon-reload
    systemctl --user restart "${label}.service"
    sleep 2
    if systemctl --user is-active --quiet "${label}.service"; then
        local pid
        pid=$(systemctl --user show -p MainPID --value "${label}.service")
        echo "  ✓ $label running, pid=$pid"
    else
        echo "  ⚠ $label not active — check: systemctl --user status ${label}.service"
    fi
    return 0
}

reload_one() {
    local label="$1"
    case "$OS" in
        Darwin) reload_launchd "$label" ;;
        Linux)  reload_systemd "$label" ;;
        *)
            echo "✗ unsupported OS: $OS" >&2
            exit 1
            ;;
    esac
}

reloaded_any=0
# Order: runner first so the bridge reconnects to the new runner immediately
# rather than retrying through backoff against the old one.
if reload_one "$RUNNER_LABEL"; then
    reloaded_any=1
fi
if reload_one "$BRIDGE_LABEL"; then
    reloaded_any=1
fi

if [[ "$reloaded_any" = "0" ]]; then
    echo "ℹ no Code-in-Tele services installed on this host — nothing to reload."
    echo "  (Looked for $RUNNER_LABEL and $BRIDGE_LABEL.)"
fi

echo
echo "✓ upgrade complete."
echo
echo "If you have multiple hosts (e.g. a separate runner box):"
echo "  1. Run this script on each runner host first."
echo "  2. Then on the bridge host."
echo "  Reason: a new bridge can speak protocol envelopes an old runner doesn't"
echo "  know yet, but new runners still understand older bridges."
