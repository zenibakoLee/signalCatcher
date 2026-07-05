#!/bin/bash
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$PROJECT_DIR/.venv/bin/python"
MAX_RETRIES=2
RETRY_DELAY=300  # 5 minutes

cd "$PROJECT_DIR"
source "$PROJECT_DIR/.env" 2>/dev/null || true

for attempt in $(seq 0 $MAX_RETRIES); do
    if [ "$attempt" -gt 0 ]; then
        echo "[daily] Retry $attempt/$MAX_RETRIES after ${RETRY_DELAY}s wait..."
        sleep $RETRY_DELAY
    fi

    echo "[daily] Starting pipeline (attempt $((attempt + 1))/$((MAX_RETRIES + 1)))..."
    if "$VENV" -m pipeline daily; then
        echo "[daily] Pipeline completed successfully"
        exit 0
    fi

    echo "[daily] Pipeline failed (attempt $((attempt + 1)))"
done

echo "[daily] All retries exhausted"

if [ -n "$DISCORD_WEBHOOK_URL" ]; then
    curl -s -H "Content-Type: application/json" \
        -d '{"embeds":[{"title":"⚠️ 시그널 캐처 파이프라인 실패","description":"daily 파이프라인이 3회 시도 후 모두 실패했습니다. 수동 확인 필요.","color":12601355}]}' \
        "$DISCORD_WEBHOOK_URL" > /dev/null 2>&1
fi

exit 1
