#!/bin/bash
# Runs the scan that is due now. launchd calls this every 5 minutes (com.tradingdesk.scanner.plist).
# Keys come from ~/.config/trading-desk/env (chmod 600, never in git):
#   export WEBULL_APP_KEY=...  WEBULL_APP_SECRET=...  WEBULL_HOST=api.sandbox.webull.com (paper key only)
set -euo pipefail
DESK="${DESK_HOME:-$HOME/Trading-Agent}"
ENV_FILE="$HOME/.config/trading-desk/env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
cd "$DESK"
export DESK_DATA_DIR="$DESK/data"
mkdir -p "$DESK_DATA_DIR"
exec "$DESK/.venv/bin/python" -m desk.scanner >> "$DESK_DATA_DIR/scanner.out" 2>&1
