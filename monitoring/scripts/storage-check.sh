#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Storage mount & disk space check
# =============================================================================
# Checks:
#   1. STORAGE_MOUNT is actually mounted
#   2. Local disk (/) free space vs thresholds
#   3. Storage mount free space vs thresholds
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

NOTIFY="$SCRIPT_DIR/notify.sh"

# --- Check storage mount ---
if ! mountpoint -q "$STORAGE_MOUNT" 2>/dev/null; then
    MSG="Audiobook storage is **not mounted** at \`$STORAGE_MOUNT\`.\nABS will not be able to serve audio files."
    $NOTIFY WEBHOOK_CRITICAL "Storage mount missing" "$MSG" red "storage_unmounted"
    $NOTIFY WEBHOOK_STORAGE "Storage mount missing" "$MSG" red "storage_unmounted_ch"
    exit 1
fi

# Recovery notification
MOUNT_STATE_FILE="$STATE_DIR/storage_mount_state"
PREV_MOUNT_STATE=$(cat "$MOUNT_STATE_FILE" 2>/dev/null || echo "unknown")
if [[ "$PREV_MOUNT_STATE" == "unmounted" ]]; then
    $NOTIFY WEBHOOK_STORAGE "Storage mount recovered" "Audiobook storage is mounted again at \`$STORAGE_MOUNT\`." green
fi
echo "mounted" > "$MOUNT_STATE_FILE"

# --- Helper: check a filesystem path against thresholds ---
check_disk() {
    local LABEL="$1"
    local PATH_TO_CHECK="$2"
    local WARN_FREE="$3"
    local CRIT_FREE="$4"
    local STATE_PREFIX="$5"

    local USAGE_PCT
    USAGE_PCT=$(df "$PATH_TO_CHECK" | tail -1 | awk '{print $5}' | tr -d '%')
    local FREE_PCT=$(( 100 - USAGE_PCT ))

    local USED_H TOTAL_H
    USED_H=$(df -h "$PATH_TO_CHECK" | tail -1 | awk '{print $3}')
    TOTAL_H=$(df -h "$PATH_TO_CHECK" | tail -1 | awk '{print $2}')

    if (( FREE_PCT <= CRIT_FREE )); then
        MSG="**${LABEL}** is critically low on space.\n**Free:** ${FREE_PCT}% (${USED_H} used of ${TOTAL_H})\nThreshold: < ${CRIT_FREE}% free"
        $NOTIFY WEBHOOK_CRITICAL "$LABEL: critically low disk space" "$MSG" red "${STATE_PREFIX}_crit"
        $NOTIFY WEBHOOK_STORAGE "$LABEL: critically low disk space" "$MSG" red "${STATE_PREFIX}_crit_ch"
    elif (( FREE_PCT <= WARN_FREE )); then
        MSG="**${LABEL}** disk space is getting low.\n**Free:** ${FREE_PCT}% (${USED_H} used of ${TOTAL_H})\nThreshold: < ${WARN_FREE}% free"
        $NOTIFY WEBHOOK_STORAGE "$LABEL: low disk space warning" "$MSG" yellow "${STATE_PREFIX}_warn"
    fi
}

# --- Check local disk ---
check_disk "Local Disk" "/" "$STORAGE_WARN" "$STORAGE_CRIT" "local_disk"

# --- Check audiobook storage ---
check_disk "Audiobook Storage" "$STORAGE_MOUNT" "$STORAGE_WARN" "$STORAGE_CRIT" "storage_mount"
