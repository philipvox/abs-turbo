#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Server health (CPU, RAM, load average)
# =============================================================================
# Checks system resources against configured thresholds and sends alerts
# to the SERVER and CRITICAL Discord channels as appropriate.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

NOTIFY="$SCRIPT_DIR/notify.sh"

# --- CPU usage ---
# Use /proc/stat for a 2-second sample (works on all Linux, no dependency on top format)
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
    # Fallback for non-Linux systems
    CPU_USAGE=$(top -bn2 -d1 2>/dev/null | grep "Cpu(s)" | tail -1 | awk '{printf "%.0f", 100 - $8}' || echo "0")
fi

if (( CPU_USAGE >= CPU_CRIT )); then
    MSG="CPU usage is at **${CPU_USAGE}%** (critical threshold: ${CPU_CRIT}%)."
    $NOTIFY WEBHOOK_CRITICAL "CPU critically high" "$MSG" red "cpu_crit"
    $NOTIFY WEBHOOK_SERVER "CPU critically high" "$MSG" red "cpu_crit_ch"
elif (( CPU_USAGE >= CPU_WARN )); then
    MSG="CPU usage is at **${CPU_USAGE}%** (warning threshold: ${CPU_WARN}%)."
    $NOTIFY WEBHOOK_SERVER "CPU usage high" "$MSG" yellow "cpu_warn"
fi

# --- RAM usage ---
RAM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo "0")
RAM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo "0")

if (( RAM_TOTAL_KB > 0 )); then
    RAM_USED_KB=$(( RAM_TOTAL_KB - RAM_AVAIL_KB ))
    RAM_PCT=$(( RAM_USED_KB * 100 / RAM_TOTAL_KB ))

    RAM_TOTAL_H=$(free -h 2>/dev/null | awk '/^Mem:/{print $2}' || echo "unknown")
    RAM_USED_H=$(free -h 2>/dev/null | awk '/^Mem:/{print $3}' || echo "unknown")

    if (( RAM_PCT >= RAM_CRIT )); then
        MSG="RAM usage is at **${RAM_PCT}%** (${RAM_USED_H} / ${RAM_TOTAL_H}).\nCritical threshold: ${RAM_CRIT}%"
        $NOTIFY WEBHOOK_CRITICAL "RAM critically high" "$MSG" red "ram_crit"
        $NOTIFY WEBHOOK_SERVER "RAM critically high" "$MSG" red "ram_crit_ch"
    elif (( RAM_PCT >= RAM_WARN )); then
        MSG="RAM usage is at **${RAM_PCT}%** (${RAM_USED_H} / ${RAM_TOTAL_H}).\nWarning threshold: ${RAM_WARN}%"
        $NOTIFY WEBHOOK_SERVER "RAM usage high" "$MSG" yellow "ram_warn"
    fi
fi

# --- Load average ---
if [[ -f /proc/loadavg ]]; then
    LOAD_1=$(awk '{print $1}' /proc/loadavg)
    LOAD_5=$(awk '{print $2}' /proc/loadavg)
    LOAD_15=$(awk '{print $3}' /proc/loadavg)

    # Compare using bc for floating point, or awk
    LOAD_ABOVE_CRIT=$(awk "BEGIN {print ($LOAD_1 >= $LOAD_CRIT) ? 1 : 0}")
    LOAD_ABOVE_WARN=$(awk "BEGIN {print ($LOAD_1 >= $LOAD_WARN) ? 1 : 0}")

    if (( LOAD_ABOVE_CRIT )); then
        MSG="Load average is **${LOAD_1}** (1m), ${LOAD_5} (5m), ${LOAD_15} (15m).\nCritical threshold: ${LOAD_CRIT}"
        $NOTIFY WEBHOOK_CRITICAL "Load average critically high" "$MSG" red "load_crit"
        $NOTIFY WEBHOOK_SERVER "Load average critically high" "$MSG" red "load_crit_ch"
    elif (( LOAD_ABOVE_WARN )); then
        MSG="Load average is **${LOAD_1}** (1m), ${LOAD_5} (5m), ${LOAD_15} (15m).\nWarning threshold: ${LOAD_WARN}"
        $NOTIFY WEBHOOK_SERVER "Load average high" "$MSG" yellow "load_warn"
    fi
fi
