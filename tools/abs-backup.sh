#!/usr/bin/env bash
#
# abs-backup.sh - Backup AudiobookShelf database with WAL checkpoint and retention cleanup
#
# Configuration (env vars or .env file):
#   ABS_DB           - Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)
#   ABS_BACKUP_DIR   - Backup destination directory (default: /backups)
#   ABS_CONTAINER    - Docker container name (default: audiobookshelf)
#   BACKUP_RETENTION - Days to keep backups (default: 30)
#
# Usage:
#   abs-backup.sh              # Run backup with defaults
#   ABS_DB=/path/to/db.sqlite abs-backup.sh
#   abs-backup.sh --no-wal     # Skip WAL checkpoint (if ABS is running)
#

set -euo pipefail

# Load .env file if present
for envfile in .env /etc/abs-turbo/.env "$HOME/.abs-turbo.env"; do
    if [[ -f "$envfile" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$envfile"
        set +a
        break
    fi
done

# Configuration with defaults
ABS_DB="${ABS_DB:-/config/absdatabase.sqlite}"
ABS_BACKUP_DIR="${ABS_BACKUP_DIR:-/backups}"
ABS_CONTAINER="${ABS_CONTAINER:-audiobookshelf}"
BACKUP_RETENTION="${BACKUP_RETENTION:-30}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${ABS_BACKUP_DIR}/absdatabase_${TIMESTAMP}.sqlite"

NO_WAL=false
if [[ "${1:-}" == "--no-wal" ]]; then
    NO_WAL=true
fi

echo "abs-backup"
echo "  Database:   ${ABS_DB}"
echo "  Backup dir: ${ABS_BACKUP_DIR}"
echo "  Retention:  ${BACKUP_RETENTION} days"
echo ""

# Validate source database exists
if [[ ! -f "$ABS_DB" ]]; then
    echo "ERROR: Database not found at ${ABS_DB}"
    echo "  Set ABS_DB environment variable to point to absdatabase.sqlite"
    exit 1
fi

# Create backup directory if needed
mkdir -p "$ABS_BACKUP_DIR"

# Step 1: WAL checkpoint (flush write-ahead log to main database)
# This ensures the backup contains all committed transactions.
# Safe to run while ABS is active — SQLite handles locking.
if [[ "$NO_WAL" == false ]]; then
    echo "Step 1: WAL checkpoint..."
    if WAL_RESULT=$(sqlite3 "$ABS_DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>&1); then
        echo "  WAL checkpoint: ${WAL_RESULT}"
    else
        echo "  Warning: WAL checkpoint failed (${WAL_RESULT}). Continuing with backup anyway."
    fi
else
    echo "Step 1: WAL checkpoint skipped (--no-wal)"
fi

# Step 2: Copy database
echo "Step 2: Copying database..."
DB_SIZE=$(du -sh "$ABS_DB" 2>/dev/null | cut -f1 || echo "unknown")
echo "  Source size: ${DB_SIZE}"

# Use sqlite3 .backup for a consistent copy (handles locks properly)
if command -v sqlite3 &>/dev/null; then
    sqlite3 "$ABS_DB" ".backup '${BACKUP_FILE}'"
else
    # Fallback to cp if sqlite3 not available
    echo "  Warning: sqlite3 not found, using cp (less safe if ABS is writing)"
    cp "$ABS_DB" "$BACKUP_FILE"
fi

if [[ -f "$BACKUP_FILE" ]]; then
    BACKUP_SIZE=$(du -sh "$BACKUP_FILE" 2>/dev/null | cut -f1 || echo "unknown")
    echo "  Backup created: ${BACKUP_FILE} (${BACKUP_SIZE})"
else
    echo "ERROR: Backup file was not created"
    exit 1
fi

# Step 3: Verify backup integrity
echo "Step 3: Verifying backup..."
if command -v sqlite3 &>/dev/null; then
    INTEGRITY=$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>&1)
    if [[ "$INTEGRITY" == "ok" ]]; then
        echo "  Integrity check: PASSED"
    else
        echo "  WARNING: Integrity check returned: ${INTEGRITY}"
    fi

    BOOK_COUNT=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM books;" 2>/dev/null || echo "?")
    ITEM_COUNT=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM libraryItems;" 2>/dev/null || echo "?")
    echo "  Books: ${BOOK_COUNT}, Library items: ${ITEM_COUNT}"
else
    echo "  Skipped (sqlite3 not available)"
fi

# Step 4: Retention cleanup
echo "Step 4: Retention cleanup (removing backups older than ${BACKUP_RETENTION} days)..."
DELETED=0
while IFS= read -r old_backup; do
    rm -f "$old_backup"
    DELETED=$((DELETED + 1))
    echo "  Removed: $(basename "$old_backup")"
done < <(find "$ABS_BACKUP_DIR" -name "absdatabase_*.sqlite" -type f -mtime +"$BACKUP_RETENTION" 2>/dev/null)

if [[ $DELETED -eq 0 ]]; then
    echo "  No old backups to remove"
else
    echo "  Removed ${DELETED} old backup(s)"
fi

# Summary
TOTAL_BACKUPS=$(find "$ABS_BACKUP_DIR" -name "absdatabase_*.sqlite" -type f 2>/dev/null | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$ABS_BACKUP_DIR" 2>/dev/null | cut -f1 || echo "unknown")

echo ""
echo "=================================================="
echo "BACKUP COMPLETE"
echo "  File:           ${BACKUP_FILE}"
echo "  Size:           ${BACKUP_SIZE}"
echo "  Total backups:  ${TOTAL_BACKUPS}"
echo "  Total size:     ${TOTAL_SIZE}"
echo "=================================================="
