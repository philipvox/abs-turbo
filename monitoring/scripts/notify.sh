#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Discord notification sender
# =============================================================================
# Usage: notify.sh WEBHOOK_VAR "Title" "Message" [color] [state_key]
#
# Arguments:
#   WEBHOOK_VAR  Name of the env var holding the Discord webhook URL
#   Title        Embed title
#   Message      Embed description (max 4000 chars, supports \n)
#   color        Optional: green, yellow, red, blue (default: blue)
#   state_key    Optional: rate-limit key (skips if sent within ALERT_COOLDOWN)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

WEBHOOK_VAR="$1"
TITLE="$2"
MESSAGE="$3"
COLOR_NAME="${4:-blue}"
STATE_KEY="${5:-}"

WEBHOOK_URL="${!WEBHOOK_VAR}"

declare -A COLORS=(
    [green]="3066993"
    [yellow]="15844367"
    [red]="15158332"
    [blue]="3447003"
)
COLOR="${COLORS[$COLOR_NAME]:-3447003}"

# Rate limiting — skip if this state_key was alerted recently
if [[ -n "$STATE_KEY" ]]; then
    STATE_FILE="$STATE_DIR/${STATE_KEY}"
    if [[ -f "$STATE_FILE" ]]; then
        LAST_ALERT=$(cat "$STATE_FILE")
        NOW=$(date +%s)
        ELAPSED=$(( NOW - LAST_ALERT ))
        if [[ $ELAPSED -lt $ALERT_COOLDOWN ]]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') RATE_LIMITED: $STATE_KEY (${ELAPSED}s < ${ALERT_COOLDOWN}s)" >> "$LOG_FILE"
            exit 0
        fi
    fi
fi

# Interpret escape sequences and truncate to Discord's embed limit
MESSAGE=$(echo -e "$MESSAGE")
MESSAGE="${MESSAGE:0:4000}"

PAYLOAD=$(jq -n \
    --arg title "$TITLE" \
    --arg desc "$MESSAGE" \
    --argjson color "$COLOR" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg footer "$SERVER_NAME" \
    '{
        embeds: [{
            title: $title,
            description: $desc,
            color: $color,
            footer: { text: $footer },
            timestamp: $ts
        }]
    }')

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$WEBHOOK_URL")

if [[ "$HTTP_CODE" == "204" || "$HTTP_CODE" == "200" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') SENT: [$COLOR_NAME] $TITLE" >> "$LOG_FILE"
    if [[ -n "$STATE_KEY" ]]; then
        date +%s > "$STATE_DIR/${STATE_KEY}"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') FAIL: HTTP $HTTP_CODE for $TITLE" >> "$LOG_FILE"
fi
