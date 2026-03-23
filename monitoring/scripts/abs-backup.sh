#!/bin/bash
# =============================================================================
# abs-turbo monitoring — ABS database & metadata backup
# =============================================================================
# Backs up:
#   1. ABS SQLite database (with WAL checkpoint)
#   2. ABS metadata/items directory (tar.gz)
#
# Prunes backups older than BACKUP_RETENTION_DAYS.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.env"

NOTIFY="$SCRIPT_DIR/notify.sh"

DATE=$(date '+%Y-%m-%d')

mkdir -p "$BACKUP_DIR"

# --- Preflight checks ---
if [[ ! -f "$ABS_DB" ]]; then
    $NOTIFY WEBHOOK_CRITICAL "Backup failed" "ABS database not found at \`${ABS_DB}\`." red "backup_fail"
    exit 1
fi

if ! mountpoint -q "$STORAGE_MOUNT" 2>/dev/null; then
    $NOTIFY WEBHOOK_CRITICAL "Backup failed" "Storage not mounted at \`$STORAGE_MOUNT\`." red "backup_fail"
    exit 1
fi

# --- Database backup ---
BACKUP_FILE="$BACKUP_DIR/absdatabase-${DATE}.sqlite"
cp "$ABS_DB" "$BACKUP_FILE"
[[ -f "${ABS_DB}-wal" ]] && cp "${ABS_DB}-wal" "${BACKUP_FILE}-wal"
[[ -f "${ABS_DB}-shm" ]] && cp "${ABS_DB}-shm" "${BACKUP_FILE}-shm"

# Checkpoint WAL into the backup to create a self-contained database file
if command -v sqlite3 &>/dev/null && [[ -f "${BACKUP_FILE}-wal" ]]; then
    sqlite3 "$BACKUP_FILE" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    rm -f "${BACKUP_FILE}-wal" "${BACKUP_FILE}-shm"
fi

# --- Metadata backup ---
METADATA_BACKUP="$BACKUP_DIR/metadata-items-${DATE}.tar.gz"
if [[ -d "$ABS_METADATA/items" ]]; then
    tar czf "$METADATA_BACKUP" -C "$ABS_METADATA" items/ 2>/dev/null
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') WARN: Metadata items directory not found at $ABS_METADATA/items" >> "$LOG_FILE"
fi

# --- Validate backup size ---
DB_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE" 2>/dev/null || echo "0")
META_SIZE=0
if [[ -f "$METADATA_BACKUP" ]]; then
    META_SIZE=$(stat -c%s "$METADATA_BACKUP" 2>/dev/null || stat -f%z "$METADATA_BACKUP" 2>/dev/null || echo "0")
fi

if (( DB_SIZE < 1000 )); then
    $NOTIFY WEBHOOK_CRITICAL "Backup failed" "Database backup is suspiciously small (${DB_SIZE} bytes)." red "backup_fail"
    exit 1
fi

# --- Prune old backups ---
find "$BACKUP_DIR" -name "absdatabase-*.sqlite" -mtime +$BACKUP_RETENTION_DAYS -delete
find "$BACKUP_DIR" -name "metadata-items-*.tar.gz" -mtime +$BACKUP_RETENTION_DAYS -delete

# --- Report ---
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "absdatabase-*.sqlite" | wc -l)
DB_SIZE_H=$(numfmt --to=iec "$DB_SIZE" 2>/dev/null || echo "${DB_SIZE} bytes")
META_SIZE_H=$(numfmt --to=iec "$META_SIZE" 2>/dev/null || echo "${META_SIZE} bytes")

MSG="Daily backup completed.\n**Database:** ${DB_SIZE_H}\n**Metadata:** ${META_SIZE_H}\n**Backups stored:** ${BACKUP_COUNT} (${BACKUP_RETENTION_DAYS}-day retention)"
$NOTIFY WEBHOOK_ABS "Daily backup completed" "$MSG" green
