#!/bin/bash
# =============================================================================
# abs-turbo monitoring — AudiobookShelf container & health check
# =============================================================================
# Checks:
#   1. Docker container is running (alerts CRITICAL + ABS channels if down)
#   2. ABS /healthcheck endpoint returns HTTP 200
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

NOTIFY="$SCRIPT_DIR/notify.sh"

# --- Container status ---
ABS_STATUS=$(docker inspect -f '{{.State.Status}}' "$ABS_CONTAINER" 2>/dev/null || echo "not_found")
PREV_STATUS_FILE="$STATE_DIR/abs_container_status"
PREV_STATUS=$(cat "$PREV_STATUS_FILE" 2>/dev/null || echo "unknown")

if [[ "$ABS_STATUS" != "running" ]]; then
    RESTART_COUNT=$(docker inspect -f '{{.RestartCount}}' "$ABS_CONTAINER" 2>/dev/null || echo "unknown")
    MSG="AudiobookShelf container (\`$ABS_CONTAINER\`) is **${ABS_STATUS}**. It has restarted ${RESTART_COUNT} times."
    $NOTIFY WEBHOOK_CRITICAL "AudiobookShelf is down" "$MSG" red "abs_down"
    $NOTIFY WEBHOOK_ABS "AudiobookShelf is down" "$MSG" red "abs_down_channel"
    echo "$ABS_STATUS" > "$PREV_STATUS_FILE"
    exit 1
fi

# Recovery notification — was down, now running again
if [[ "$PREV_STATUS" != "running" && "$PREV_STATUS" != "unknown" ]]; then
    $NOTIFY WEBHOOK_ABS "AudiobookShelf recovered" "Container \`$ABS_CONTAINER\` is running again." green
fi

echo "running" > "$PREV_STATUS_FILE"

# --- HTTP health check ---
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$ABS_URL/healthcheck" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" != "200" ]]; then
    MSG="AudiobookShelf health check failed with HTTP status **${HTTP_CODE}**.\nURL: \`$ABS_URL/healthcheck\`"
    $NOTIFY WEBHOOK_ABS "AudiobookShelf health check failed" "$MSG" yellow "abs_http_fail"
fi
