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

load_required_secret() {
    local variable="$1"
    local service="$2"
    local value
    unset "$variable"
    if ! value="$(/usr/bin/security find-generic-password -s "$service" -w "$KEYCHAIN" 2>/dev/null)"; then
        echo "[$MODE] Required Keychain item is unavailable: $service" >&2
        exit 78
    fi
    printf -v "$variable" '%s' "$value"
    export "$variable"
    unset value
}

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

# Secrets are loaded only from the login Keychain; dotenv secrets are ignored.
load_required_secret OPENAI_API_KEY "${SIGNALCATCHER_OPENAI_KEYCHAIN_SERVICE:-openai-api-key}"
load_optional_secret GEMINI_API_KEY "${SIGNALCATCHER_GEMINI_KEYCHAIN_SERVICE:-gemini-api-key}"
load_optional_secret DISCORD_WEBHOOK_URL "${SIGNALCATCHER_DISCORD_WEBHOOK_KEYCHAIN_SERVICE:-signalcatcher-discord-webhook-url}"
load_optional_secret YOUTUBE_API_KEY "${SIGNALCATCHER_YOUTUBE_KEYCHAIN_SERVICE:-youtube-api-key}"
load_optional_secret GITHUB_TOKEN "${SIGNALCATCHER_GITHUB_KEYCHAIN_SERVICE:-github-token}"

export SIGNALCATCHER_OPENAI_DAILY_BUDGET_USD="${SIGNALCATCHER_OPENAI_DAILY_BUDGET_USD:-2.00}"
cd "$PROJECT_DIR"
echo "[$MODE] Starting one fail-closed pipeline attempt..."
exec "$VENV" -m pipeline "$MODE"
