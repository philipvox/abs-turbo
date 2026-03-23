#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Cron installer
# =============================================================================
# Interactive script that adds monitoring cron entries to the current user's
# crontab. Shows what will be added and asks for confirmation.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$SCRIPT_DIR/scripts"

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${BOLD}========================================${NC}"
echo -e "${BOLD}  abs-turbo Monitoring — Cron Installer${NC}"
echo -e "${BOLD}========================================${NC}"
echo

# --- Preflight checks ---
if [[ ! -f "$SCRIPT_DIR/config.env" ]]; then
    echo -e "${RED}ERROR:${NC} config.env not found at $SCRIPT_DIR/config.env"
    echo "       Copy config.env.example to config.env and fill in your values first."
    exit 1
fi

for script in notify.sh abs-monitor.sh storage-check.sh docker-check.sh server-health.sh cache-monitor.sh abs-backup.sh daily-digest.sh; do
    if [[ ! -f "$SCRIPTS/$script" ]]; then
        echo -e "${RED}ERROR:${NC} Missing script: $SCRIPTS/$script"
        exit 1
    fi
done

# Make sure all scripts are executable
chmod +x "$SCRIPTS"/*.sh

# --- Ask for daily task time ---
echo -e "${CYAN}What time should the daily backup and digest run?${NC}"
echo "Enter hour in 24h format (0-23). Default: 4"
read -rp "Hour [4]: " DAILY_HOUR
DAILY_HOUR="${DAILY_HOUR:-4}"

# Validate
if ! [[ "$DAILY_HOUR" =~ ^[0-9]+$ ]] || (( DAILY_HOUR < 0 || DAILY_HOUR > 23 )); then
    echo -e "${RED}Invalid hour. Using default (4).${NC}"
    DAILY_HOUR=4
fi

DIGEST_MINUTE=30
BACKUP_MINUTE=0

echo

# --- Build cron entries ---
CRON_TAG="# abs-turbo-monitoring"

# Every 30 minutes checks
ENTRY_ABS="*/30 * * * * $SCRIPTS/abs-monitor.sh >> /dev/null 2>&1 $CRON_TAG"
ENTRY_STORAGE="*/30 * * * * $SCRIPTS/storage-check.sh >> /dev/null 2>&1 $CRON_TAG"
ENTRY_DOCKER="*/30 * * * * $SCRIPTS/docker-check.sh >> /dev/null 2>&1 $CRON_TAG"
ENTRY_HEALTH="*/30 * * * * $SCRIPTS/server-health.sh >> /dev/null 2>&1 $CRON_TAG"
ENTRY_CACHE="*/30 * * * * $SCRIPTS/cache-monitor.sh >> /dev/null 2>&1 $CRON_TAG"

# Daily tasks
ENTRY_BACKUP="$BACKUP_MINUTE $DAILY_HOUR * * * $SCRIPTS/abs-backup.sh >> /dev/null 2>&1 $CRON_TAG"
ENTRY_DIGEST="$DIGEST_MINUTE $DAILY_HOUR * * * $SCRIPTS/daily-digest.sh >> /dev/null 2>&1 $CRON_TAG"

echo -e "${BOLD}The following cron entries will be added:${NC}"
echo
echo -e "${GREEN}Every 30 minutes:${NC}"
echo "  - ABS container & health check"
echo "  - Storage mount & disk space"
echo "  - Docker container check"
echo "  - Server health (CPU, RAM, load)"
echo "  - Cache utilization"
echo
echo -e "${GREEN}Daily at ${DAILY_HOUR}:${BACKUP_MINUTE}0:${NC}"
echo "  - ABS database backup"
echo
echo -e "${GREEN}Daily at ${DAILY_HOUR}:${DIGEST_MINUTE}:${NC}"
echo "  - Health digest (Discord embed)"
echo

echo -e "${YELLOW}Cron entries:${NC}"
echo "---"
echo "$ENTRY_ABS"
echo "$ENTRY_STORAGE"
echo "$ENTRY_DOCKER"
echo "$ENTRY_HEALTH"
echo "$ENTRY_CACHE"
echo "$ENTRY_BACKUP"
echo "$ENTRY_DIGEST"
echo "---"
echo

# --- Check for existing entries ---
EXISTING_CRON=$(crontab -l 2>/dev/null || true)
if echo "$EXISTING_CRON" | grep -q "$CRON_TAG"; then
    echo -e "${YELLOW}WARNING:${NC} Existing abs-turbo-monitoring cron entries detected."
    echo "They will be removed and replaced with the new entries."
    echo
fi

# --- Confirm ---
read -rp "$(echo -e "${BOLD}Install these cron entries? [y/N]:${NC} ")" CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo "Aborted. No changes made."
    exit 0
fi

# --- Install ---
# Remove any existing abs-turbo entries, then add new ones
NEW_CRON=$(echo "$EXISTING_CRON" | grep -v "$CRON_TAG" || true)

# Add a blank line separator if there's existing content
if [[ -n "$NEW_CRON" ]]; then
    NEW_CRON+=$'\n'
fi

NEW_CRON+="$ENTRY_ABS"$'\n'
NEW_CRON+="$ENTRY_STORAGE"$'\n'
NEW_CRON+="$ENTRY_DOCKER"$'\n'
NEW_CRON+="$ENTRY_HEALTH"$'\n'
NEW_CRON+="$ENTRY_CACHE"$'\n'
NEW_CRON+="$ENTRY_BACKUP"$'\n'
NEW_CRON+="$ENTRY_DIGEST"

echo "$NEW_CRON" | crontab -

echo
echo -e "${GREEN}Cron entries installed successfully.${NC}"
echo

# --- Create state directory ---
source "$SCRIPT_DIR/config.env"
mkdir -p "$STATE_DIR" 2>/dev/null && echo -e "State directory created: ${CYAN}$STATE_DIR${NC}" || true
mkdir -p "$STATE_DIR/docker" 2>/dev/null || true

# --- Create log file ---
touch "$LOG_FILE" 2>/dev/null && echo -e "Log file: ${CYAN}$LOG_FILE${NC}" || true

echo
echo -e "${BOLD}Done!${NC} Monitoring is now active."
echo "View logs:  tail -f $LOG_FILE"
echo "Edit cron:  crontab -e"
echo "Remove all: crontab -l | grep -v '$CRON_TAG' | crontab -"
