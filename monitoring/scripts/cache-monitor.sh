#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Cache utilization check
# =============================================================================
# Monitors disk usage of cache directories (moov cache, faststart cache, etc.)
# and alerts when utilization exceeds configured thresholds.
#
# Only useful if you run a moov/faststart proxy in front of ABS. Skip this
# script if you serve audio directly from ABS without caching layers.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

NOTIFY="$SCRIPT_DIR/notify.sh"

# --- Helper: check a cache path against thresholds ---
check_cache() {
    local LABEL="$1"
    local CACHE_PATH="$2"
    local STATE_PREFIX="$3"

    if [[ ! -d "$CACHE_PATH" ]]; then
        # Cache directory doesn't exist — skip silently (may not be configured)
        return 0
    fi

    # Get the filesystem that holds this cache
    local USAGE_PCT
    USAGE_PCT=$(df "$CACHE_PATH" | tail -1 | awk '{print $5}' | tr -d '%')

    local USED_H TOTAL_H AVAIL_H
    USED_H=$(df -h "$CACHE_PATH" | tail -1 | awk '{print $3}')
    TOTAL_H=$(df -h "$CACHE_PATH" | tail -1 | awk '{print $2}')
    AVAIL_H=$(df -h "$CACHE_PATH" | tail -1 | awk '{print $4}')

    # Get the size of the cache directory itself
    local CACHE_SIZE
    CACHE_SIZE=$(du -sh "$CACHE_PATH" 2>/dev/null | awk '{print $1}' || echo "unknown")

    if (( USAGE_PCT >= CACHE_CRIT )); then
        MSG="**${LABEL}** filesystem is at **${USAGE_PCT}%** capacity.\n"
        MSG+="**Cache size:** ${CACHE_SIZE}\n"
        MSG+="**Disk:** ${USED_H} used / ${TOTAL_H} total (${AVAIL_H} free)\n"
        MSG+="Critical threshold: ${CACHE_CRIT}%"
        $NOTIFY WEBHOOK_CRITICAL "$LABEL: critically full" "$MSG" red "${STATE_PREFIX}_crit"
        $NOTIFY WEBHOOK_CACHE "$LABEL: critically full" "$MSG" red "${STATE_PREFIX}_crit_ch"
    elif (( USAGE_PCT >= CACHE_WARN )); then
        MSG="**${LABEL}** filesystem is at **${USAGE_PCT}%** capacity.\n"
        MSG+="**Cache size:** ${CACHE_SIZE}\n"
        MSG+="**Disk:** ${USED_H} used / ${TOTAL_H} total (${AVAIL_H} free)\n"
        MSG+="Warning threshold: ${CACHE_WARN}%"
        $NOTIFY WEBHOOK_CACHE "$LABEL: high utilization" "$MSG" yellow "${STATE_PREFIX}_warn"
    fi
}

# --- Check configured caches ---
check_cache "Moov Cache" "$CACHE_VOLUME" "moov_cache"
check_cache "Faststart Cache" "$CACHE_DIR" "faststart_cache"
