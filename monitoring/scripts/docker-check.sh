#!/bin/bash
# =============================================================================
# abs-turbo monitoring — Docker container health check
# =============================================================================
# Iterates all Docker containers and alerts on any that are not running.
# Tracks previous state to send recovery notifications.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

NOTIFY="$SCRIPT_DIR/notify.sh"

# Ensure Docker is available
if ! command -v docker &>/dev/null; then
    $NOTIFY WEBHOOK_CRITICAL "Docker not found" "The \`docker\` command is not available on this system." red "docker_missing"
    exit 1
fi

if ! docker info &>/dev/null; then
    $NOTIFY WEBHOOK_CRITICAL "Docker daemon unreachable" "Cannot connect to the Docker daemon. Is it running?" red "docker_daemon"
    exit 1
fi

# --- Check each container ---
ISSUES=""
ISSUE_COUNT=0
DOCKER_STATE_DIR="$STATE_DIR/docker"
mkdir -p "$DOCKER_STATE_DIR"

while IFS='|' read -r NAME STATUS STATE; do
    [[ -z "$NAME" ]] && continue

    PREV_FILE="$DOCKER_STATE_DIR/${NAME}"
    PREV_STATE=$(cat "$PREV_FILE" 2>/dev/null || echo "unknown")

    if [[ "$STATE" != "running" ]]; then
        RESTART_COUNT=$(docker inspect -f '{{.RestartCount}}' "$NAME" 2>/dev/null || echo "unknown")
        ISSUES+="\\n- \`${NAME}\` is **${STATE}** (status: ${STATUS}, restarts: ${RESTART_COUNT})"
        (( ISSUE_COUNT++ ))
    else
        # Recovery: was not running before, now it is
        if [[ "$PREV_STATE" != "running" && "$PREV_STATE" != "unknown" ]]; then
            $NOTIFY WEBHOOK_DOCKER "Container recovered: $NAME" "Container \`${NAME}\` is running again." green
        fi
    fi

    echo "$STATE" > "$PREV_FILE"
done <<< "$(docker ps -a --format '{{.Names}}|{{.Status}}|{{.State}}')"

# --- Send alert if any containers are down ---
if (( ISSUE_COUNT > 0 )); then
    MSG="**${ISSUE_COUNT} container(s)** not running:${ISSUES}"
    $NOTIFY WEBHOOK_DOCKER "Docker containers down" "$MSG" red "docker_down"

    # Also alert CRITICAL if the ABS container specifically is affected
    ABS_STATE=$(docker inspect -f '{{.State.Status}}' "$ABS_CONTAINER" 2>/dev/null || echo "not_found")
    if [[ "$ABS_STATE" != "running" ]]; then
        $NOTIFY WEBHOOK_CRITICAL "ABS container is $ABS_STATE" "The AudiobookShelf container \`$ABS_CONTAINER\` is **${ABS_STATE}**." red "abs_docker_crit"
    fi
fi
