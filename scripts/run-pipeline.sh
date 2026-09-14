#!/bin/bash
set -euo pipefail

MODE="${1:-}"
case "$MODE" in
    daily|event) ;;
    *) echo "usage: $0 {daily|event}" >&2; exit 64 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$PROJECT_DIR/.venv/bin/python"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

load_optional_secret() {
    local variable="$1"
    local service="$2"
    local value
    unset "$variable"
    if value="$(/usr/bin/security find-generic-password -s "$service" -w "$KEYCHAIN" 2>/dev/null)"; then
        printf -v "$variable" '%s' "$value"
        export "$variable"
    fi
    unset value
}

# Optional service credentials come only from the login Keychain; dotenv is ignored.
load_optional_secret GEMINI_API_KEY "${SIGNALCATCHER_GEMINI_KEYCHAIN_SERVICE:-gemini-api-key}"
load_optional_secret DISCORD_WEBHOOK_URL "${SIGNALCATCHER_DISCORD_WEBHOOK_KEYCHAIN_SERVICE:-signalcatcher-discord-webhook-url}"
load_optional_secret YOUTUBE_API_KEY "${SIGNALCATCHER_YOUTUBE_KEYCHAIN_SERVICE:-youtube-api-key}"
load_optional_secret GITHUB_TOKEN "${SIGNALCATCHER_GITHUB_KEYCHAIN_SERVICE:-github-token}"

if ! command -v codex >/dev/null 2>&1; then
    echo "[$MODE] Codex CLI is unavailable" >&2
    exit 78
fi
if ! codex login status >/dev/null 2>&1; then
    echo "[$MODE] Codex OAuth login is unavailable" >&2
    exit 78
fi
export SIGNALCATCHER_CODEX_PATH="$(command -v codex)"
cd "$PROJECT_DIR"
echo "[$MODE] Starting one fail-closed pipeline attempt..."
exec "$VENV" -m pipeline "$MODE"
