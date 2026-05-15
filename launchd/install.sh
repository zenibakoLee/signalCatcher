#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOG_DIR="$SCRIPT_DIR/../data/logs"

mkdir -p "$LAUNCH_AGENTS"
mkdir -p "$LOG_DIR"

PLISTS=(
    "com.signalcatcher.daily"
    "com.signalcatcher.weekly"
    "com.signalcatcher.event"
)

case "${1:-install}" in
    install)
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
        echo "All schedules installed:"
        echo "  daily  — 매일 07:00"
        echo "  weekly — 매주 일요일 09:00"
        echo "  event  — 매일 20:00"
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
