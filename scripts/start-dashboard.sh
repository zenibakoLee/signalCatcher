#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DASHBOARD_DIR="$PROJECT_DIR/dashboard"

cd "$DASHBOARD_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# Kill any stale process on port 3000
if lsof -ti:3000 > /dev/null 2>&1; then
    echo "[dashboard] Port 3000 in use, killing stale process..."
    lsof -ti:3000 | xargs kill -9 2>/dev/null || true
    sleep 2
fi

if [ ! -d ".next" ] || [ "$(find src -newer .next/BUILD_ID -print -quit 2>/dev/null)" ]; then
    echo "[dashboard] Building..."
    npm run build
fi

echo "[dashboard] Starting production server on port 3000"
exec npm start
