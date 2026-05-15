#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/data/logs"
URL_FILE="$LOG_DIR/tunnel-url.txt"

mkdir -p "$LOG_DIR"

source "$PROJECT_DIR/.env" 2>/dev/null || true

cloudflared tunnel --url http://localhost:3000 2>&1 | while IFS= read -r line; do
    echo "$line"
    if echo "$line" | grep -qE 'https://.*trycloudflare\.com'; then
        url=$(echo "$line" | grep -oE 'https://[a-zA-Z0-9._-]+\.trycloudflare\.com')
        if [ -n "$url" ]; then
            echo "$url" > "$URL_FILE"
            echo "[tunnel] URL saved: $url"

            if [ -n "$DISCORD_WEBHOOK_URL" ]; then
                curl -s -H "Content-Type: application/json" \
                    -d "{\"embeds\":[{\"title\":\"대시보드 터널 시작\",\"description\":\"**${url}**\",\"color\":3766611}]}" \
                    "$DISCORD_WEBHOOK_URL" > /dev/null 2>&1
                echo "[tunnel] URL sent to Discord"
            fi
        fi
    fi
done
