#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Daily health digest
# =============================================================================
# Sends a beautiful Discord embed with bar graphs summarizing:
#   CPU, RAM, load average, local disk, audiobook storage,
#   Docker container statuses, and ABS API health.
#
# Designed to run once daily via cron.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

# --- System info ---
UPTIME=$(uptime -p 2>/dev/null | sed 's/up //' || uptime | sed 's/.*up //' | sed 's/,.*load.*//')
HOSTNAME=$(hostname)

# --- CPU usage (2-second sample) ---
if [[ -f /proc/stat ]]; then
    read -r _ USER1 NICE1 SYS1 IDLE1 IO1 IRQ1 SOFT1 _ < /proc/stat
    TOTAL1=$(( USER1 + NICE1 + SYS1 + IDLE1 + IO1 + IRQ1 + SOFT1 ))
    sleep 2
    read -r _ USER2 NICE2 SYS2 IDLE2 IO2 IRQ2 SOFT2 _ < /proc/stat
    TOTAL2=$(( USER2 + NICE2 + SYS2 + IDLE2 + IO2 + IRQ2 + SOFT2 ))
    IDLE_DELTA=$(( IDLE2 - IDLE1 ))
    TOTAL_DELTA=$(( TOTAL2 - TOTAL1 ))
    if (( TOTAL_DELTA > 0 )); then
        CPU_USAGE=$(( 100 - (IDLE_DELTA * 100 / TOTAL_DELTA) ))
    else
        CPU_USAGE=0
    fi
else
    CPU_USAGE=$(top -bn2 -d1 2>/dev/null | grep "Cpu(s)" | tail -1 | awk '{printf "%.0f", 100 - $8}' || echo "0")
fi

# --- RAM ---
RAM_TOTAL_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo "0")
RAM_USED_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $3}' || echo "0")
RAM_TOTAL_H=$(free -h 2>/dev/null | awk '/^Mem:/{print $2}' || echo "?")
RAM_USED_H=$(free -h 2>/dev/null | awk '/^Mem:/{print $3}' || echo "?")
if (( RAM_TOTAL_MB > 0 )); then
    RAM_PCT=$(( RAM_USED_MB * 100 / RAM_TOTAL_MB ))
else
    RAM_PCT=0
fi

# --- Load average ---
if [[ -f /proc/loadavg ]]; then
    LOAD_1=$(awk '{print $1}' /proc/loadavg)
    LOAD_5=$(awk '{print $2}' /proc/loadavg)
    LOAD_15=$(awk '{print $3}' /proc/loadavg)
else
    LOAD_1=$(uptime | awk -F'load averages?: ' '{print $2}' | awk -F'[, ]+' '{print $1}')
    LOAD_5=$(uptime | awk -F'load averages?: ' '{print $2}' | awk -F'[, ]+' '{print $2}')
    LOAD_15=$(uptime | awk -F'load averages?: ' '{print $2}' | awk -F'[, ]+' '{print $3}')
fi

# --- Local disk ---
LOCAL_USED=$(df -h / | tail -1 | awk '{print $3}')
LOCAL_TOTAL=$(df -h / | tail -1 | awk '{print $2}')
LOCAL_PCT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')

# --- Audiobook storage ---
if mountpoint -q "$STORAGE_MOUNT" 2>/dev/null; then
    SB_MOUNTED=true
    SB_USED=$(df -h "$STORAGE_MOUNT" | tail -1 | awk '{print $3}')
    SB_TOTAL=$(df -h "$STORAGE_MOUNT" | tail -1 | awk '{print $2}')
    SB_PCT=$(df "$STORAGE_MOUNT" | tail -1 | awk '{print $5}' | tr -d '%')
else
    SB_MOUNTED=false
fi

# --- ABS health ---
ABS_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$ABS_URL/healthcheck" 2>/dev/null || echo "000")

# --- Bar graph generator ---
make_bar() {
    local pct=$1
    local filled=$(( pct / 10 ))
    local empty=$(( 10 - filled ))
    local bar=""
    for ((i=0; i<filled; i++)); do bar+="\u25b0"; done
    for ((i=0; i<empty; i++)); do bar+="\u25b1"; done
    echo "$bar"
}

CPU_BAR=$(make_bar "$CPU_USAGE")
RAM_BAR=$(make_bar "$RAM_PCT")
LOCAL_BAR=$(make_bar "$LOCAL_PCT")
[[ "$SB_MOUNTED" == true ]] && SB_BAR=$(make_bar "$SB_PCT")

# --- Status icons ---
level_icon() {
    local pct=$1
    local warn=$2
    local crit=$3
    if (( pct >= crit )); then echo "\ud83d\udd34"
    elif (( pct >= warn )); then echo "\ud83d\udfe1"
    else echo "\ud83d\udfe2"
    fi
}

CPU_ICON=$(level_icon "$CPU_USAGE" "$CPU_WARN" "$CPU_CRIT")
RAM_ICON=$(level_icon "$RAM_PCT" "$RAM_WARN" "$RAM_CRIT")
LOCAL_ICON=$(level_icon "$LOCAL_PCT" 80 90)
[[ "$SB_MOUNTED" == true ]] && SB_ICON=$(level_icon "$SB_PCT" 80 90)

# --- Docker containers ---
DOCKER_STATUS=""
ALL_HEALTHY=true
if command -v docker &>/dev/null && docker info &>/dev/null; then
    while IFS='|' read -r NAME STATUS STATE; do
        [[ -z "$NAME" ]] && continue
        if [[ "$STATE" == "running" ]]; then
            UP_SINCE=$(echo "$STATUS" | sed 's/Up //')
            DOCKER_STATUS+="\\n> \u2705 \`${NAME}\` \u2014 Up ${UP_SINCE}"
        else
            DOCKER_STATUS+="\\n> \u274c \`${NAME}\` \u2014 ${STATUS}"
            ALL_HEALTHY=false
        fi
    done <<< "$(docker ps -a --format '{{.Names}}|{{.Status}}|{{.State}}')"
else
    DOCKER_STATUS="\\n> \u26a0\ufe0f Docker not available"
    ALL_HEALTHY=false
fi

# --- ABS status ---
[[ "$ABS_HTTP" == "200" ]] && ABS_STATUS="\u2705 Online" || ABS_STATUS="\u274c HTTP $ABS_HTTP"

# --- Overall status ---
OVERALL="\ud83d\udfe2 All Systems Operational"
if [[ "$ALL_HEALTHY" == false ]] || [[ "$SB_MOUNTED" == false ]] || \
   (( CPU_USAGE >= CPU_CRIT )) || (( RAM_PCT >= RAM_CRIT )); then
    OVERALL="\ud83d\udd34 Issues Detected"
elif (( CPU_USAGE >= CPU_WARN )) || (( RAM_PCT >= RAM_WARN )); then
    OVERALL="\ud83d\udfe1 Warnings Present"
fi

# --- Audiobook storage field ---
STORAGE_VALUE="\u274c **NOT MOUNTED**"
if [[ "$SB_MOUNTED" == true ]]; then
    STORAGE_VALUE="${SB_ICON} \`${SB_BAR}\` ${SB_PCT}%\n${SB_USED} / ${SB_TOTAL}"
fi

# --- Build and send payload ---
PAYLOAD=$(jq -n \
    --arg overall "$OVERALL" \
    --arg uptime "$UPTIME" \
    --arg hostname "$HOSTNAME" \
    --arg cpu_icon "$CPU_ICON" \
    --arg cpu_bar "$CPU_BAR" \
    --argjson cpu_pct "$CPU_USAGE" \
    --arg ram_icon "$RAM_ICON" \
    --arg ram_bar "$RAM_BAR" \
    --argjson ram_pct "$RAM_PCT" \
    --arg ram_detail "$RAM_USED_H / $RAM_TOTAL_H" \
    --arg load "1m: $LOAD_1  \u00b7  5m: $LOAD_5  \u00b7  15m: $LOAD_15" \
    --arg local_icon "$LOCAL_ICON" \
    --arg local_bar "$LOCAL_BAR" \
    --argjson local_pct "$LOCAL_PCT" \
    --arg local_detail "$LOCAL_USED / $LOCAL_TOTAL" \
    --arg sb_value "$STORAGE_VALUE" \
    --arg docker_val "$DOCKER_STATUS" \
    --arg abs_status "$ABS_STATUS" \
    --arg server_name "$SERVER_NAME" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{
        embeds: [
            {
                title: "\ud83d\udcca Daily Health Digest",
                description: ($overall + "\n\n```\n\u2601\ufe0f  " + $hostname + "\n\u23f1\ufe0f  " + $uptime + "\n```"),
                color: 3447003,
                fields: [
                    {
                        name: "CPU",
                        value: ($cpu_icon + " `" + $cpu_bar + "` " + ($cpu_pct | tostring) + "%"),
                        inline: true
                    },
                    {
                        name: "RAM",
                        value: ($ram_icon + " `" + $ram_bar + "` " + ($ram_pct | tostring) + "%\n" + $ram_detail),
                        inline: true
                    },
                    {
                        name: "Load Average",
                        value: ("\ud83d\udcc8 " + $load),
                        inline: false
                    },
                    {
                        name: "\ud83d\udcbe Local Disk",
                        value: ($local_icon + " `" + $local_bar + "` " + ($local_pct | tostring) + "%\n" + $local_detail),
                        inline: true
                    },
                    {
                        name: "\ud83d\udce6 Audiobook Storage",
                        value: $sb_value,
                        inline: true
                    },
                    {
                        name: "\ud83d\udc33 Containers",
                        value: $docker_val,
                        inline: false
                    },
                    {
                        name: "\ud83d\udcda ABS API",
                        value: $abs_status,
                        inline: true
                    }
                ],
                footer: { text: $server_name },
                timestamp: $ts
            }
        ]
    }')

# Use WEBHOOK_DIGEST if set, otherwise fall back to WEBHOOK_SERVER
WEBHOOK_URL="${WEBHOOK_DIGEST:-$WEBHOOK_SERVER}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$WEBHOOK_URL")

if [[ "$HTTP_CODE" == "204" || "$HTTP_CODE" == "200" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') SENT: Daily Health Digest" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') FAIL: HTTP $HTTP_CODE for Daily Digest" >> "$LOG_FILE"
fi
