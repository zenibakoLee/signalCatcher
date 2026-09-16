#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOG_DIR="$SCRIPT_DIR/../data/logs"

mkdir -p "$LAUNCH_AGENTS"
mkdir -p "$LOG_DIR"

PLISTS=(
    "com.signalcatcher.daily"
    "com.signalcatcher.superstar-weekly"
    "com.signalcatcher.event"
    "com.signalcatcher.dashboard"
    "com.signalcatcher.tunnel"
)

case "${1:-install}" in
    install)
        # Remove the differently named legacy weekly agent.
        legacy_name="com.signalcatcher.weekly"
        launchctl bootout "gui/$(id -u)/$legacy_name" 2>/dev/null || true
        rm -f "$LAUNCH_AGENTS/$legacy_name.plist"

        for name in "${PLISTS[@]}"; do
            src="$SCRIPT_DIR/$name.plist"
            dst="$LAUNCH_AGENTS/$name.plist"

            # Unload if already loaded
            launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true

            ln -sf "$src" "$dst"
            launchctl bootstrap "gui/$(id -u)" "$dst"
            echo "Installed: $name"
        done
        echo ""
        echo "All services installed:"
        echo "  daily     — 매일 07:00"
        echo "  superstar — 매주 일요일 09:00 KST"
        echo "  event     — 매일 20:00"
        echo "  dashboard — 상시 (port 3000)"
        echo "  tunnel    — 상시 (Cloudflare Quick Tunnel)"
        echo ""
        echo "Verify: launchctl list | grep signalcatcher"
        ;;

    uninstall)
        for name in "${PLISTS[@]}"; do
            launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
            rm -f "$LAUNCH_AGENTS/$name.plist"
            echo "Uninstalled: $name"
        done
        ;;

    status)
        for name in "${PLISTS[@]}"; do
            if launchctl print "gui/$(id -u)/$name" &>/dev/null; then
                echo "$name: loaded"
            else
                echo "$name: not loaded"
            fi
        done
        ;;

    *)
        echo "Usage: $0 {install|uninstall|status}"
        exit 1
        ;;
esac
