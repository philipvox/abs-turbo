#!/usr/bin/env python3
"""
abs-inode-sync: Fix stale inodes in AudiobookShelf database.

When audiobook files are served over SSHFS or NFS, filesystem remounts cause
inode numbers to change. ABS stores inodes in three places:
  1. books.audioFiles[].ino         -- audio file metadata
  2. libraryItems.libraryFiles[].ino -- used by /api/items/{id}/file/{ino} endpoint
  3. libraryItems.ino               -- folder inode

If any are stale, ABS returns 404 for file requests. This tool scans the
filesystem for current inodes, compares against the database, and fixes
mismatches.

Configuration (env vars or .env file):
  ABS_DB           - Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)
  AUDIOBOOKS_DIR   - Mount point of audiobook library (default: /audiobooks)
  ABS_CONTAINER    - Docker container name (default: audiobookshelf)
  SENTINEL_DIR     - Dir for sentinel file (default: next to ABS_DB)

Usage:
  abs-inode-sync.py                  # Smart mode: skip if no remount detected
  abs-inode-sync.py --force          # Force full sync regardless of sentinel
  abs-inode-sync.py --dry-run        # Show what would change without writing
  abs-inode-sync.py --no-restart     # Don't restart ABS container after fixing
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time


def load_env_file():
    """Load .env file from common locations if present."""
    for path in [".env", "/etc/abs-turbo/.env", os.path.expanduser("~/.abs-turbo.env")]:
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip("'\"")
                        if key not in os.environ:
                            os.environ[key] = value
            break


def get_config():
    """Read configuration from environment variables."""
    load_env_file()
    db_path = os.environ.get("ABS_DB", "/config/absdatabase.sqlite")
    audiobooks_dir = os.environ.get("AUDIOBOOKS_DIR", "/audiobooks")
    container = os.environ.get("ABS_CONTAINER", "audiobookshelf")
    sentinel_dir = os.environ.get("SENTINEL_DIR", os.path.dirname(db_path))
    return db_path, audiobooks_dir, container, sentinel_dir


def detect_library_prefix(db_path):
    """Auto-detect the library path prefix from the database.

    ABS stores paths like '/audiobooks/Author/Book'. We need to know
    what prefix to strip so we can match against relative filesystem paths.
    Returns the prefix string (e.g., 'audiobooks') or empty string.
    """
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT path FROM libraryItems LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0]:
            # Path looks like /audiobooks/Author/Book or /books/Author/Book
            parts = row[0].strip("/").split("/")
            if len(parts) >= 2:
                return parts[0]
    except Exception:
        pass
    return ""


def check_sentinel(audiobooks_dir, sentinel_dir):
    """Check if mount inode has changed since last sync.

    Returns True if sync is needed, False if mount hasn't changed.
    """
    sentinel_file = os.path.join(sentinel_dir, ".ino_sentinel")
    try:
        current_ino = str(os.stat(audiobooks_dir).st_ino)
    except OSError:
        print(f"ERROR: Mount not available at {audiobooks_dir}")
        return False

    if os.path.exists(sentinel_file):
        with open(sentinel_file) as f:
            saved_ino = f.read().strip()
        if saved_ino == current_ino:
            print(f"Mount inode unchanged ({current_ino}), no remount detected. Skipping.")
            return False
        else:
            print(f"Mount inode changed ({saved_ino} -> {current_ino}), remount detected!")
            return True
    else:
        print("No sentinel file found (first run). Will sync.")
        return True


def save_sentinel(audiobooks_dir, sentinel_dir):
    """Save current mount inode to sentinel file."""
    sentinel_file = os.path.join(sentinel_dir, ".ino_sentinel")
    try:
        current_ino = str(os.stat(audiobooks_dir).st_ino)
        os.makedirs(sentinel_dir, exist_ok=True)
        with open(sentinel_file, "w") as f:
            f.write(current_ino)
        print(f"Sentinel saved: {current_ino}")
    except OSError as e:
        print(f"Warning: could not save sentinel file: {e}")


def scan_filesystem(audiobooks_dir):
    """Walk the audiobook directory and build a map of relative_path -> inode.

    Scans both files and directories so we can fix all three inode locations.
    """
    print("Phase 1: Scanning filesystem...")
    inode_map = {}
    dir_count = 0
    scan_start = time.time()

    # Scan files
    for dirpath, dirnames, filenames in os.walk(audiobooks_dir):
        dir_count += 1
        try:
            with os.scandir(dirpath) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        rel = os.path.relpath(entry.path, audiobooks_dir)
                        try:
                            inode_map[rel] = str(entry.stat(follow_symlinks=False).st_ino)
                        except OSError:
                            pass
        except OSError:
            pass

        if dir_count % 500 == 0:
            elapsed = time.time() - scan_start
            print(f"  {dir_count} dirs, {len(inode_map)} files ({elapsed:.0f}s)")

    # Scan directories for folder inodes
    for dirpath, dirnames, filenames in os.walk(audiobooks_dir):
        rel = os.path.relpath(dirpath, audiobooks_dir)
        if rel == ".":
            continue
        try:
            inode_map[rel] = str(os.stat(dirpath).st_ino)
        except OSError:
            pass

    elapsed = time.time() - scan_start
    print(f"  Scan complete: {dir_count} dirs, {len(inode_map)} entries in {elapsed:.0f}s")
    return inode_map


def sync_inodes(db_path, inode_map, library_prefix, dry_run=False):
    """Compare database inodes against filesystem and fix mismatches.

    Updates all three inode locations in the ABS database:
      1. books.audioFiles[].ino
      2. libraryItems.libraryFiles[].ino
      3. libraryItems.ino (folder inode)

    Returns (books_checked, files_checked, mismatched, books_fixed, missing, errors).
    """
    print("Phase 2: Comparing against database...")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cursor = conn.execute(
        "SELECT b.id, b.audioFiles, li.id as item_id, li.ino as item_ino, "
        "li.path, li.libraryFiles, b.title "
        "FROM books b "
        "JOIN libraryItems li ON li.mediaId = b.id "
        "ORDER BY b.title"
    )
    rows = cursor.fetchall()
    print(f"  {len(rows)} books in DB, {len(inode_map)} entries on disk")

    total_files = 0
    mismatched = 0
    missing = 0
    books_fixed = 0
    errors = 0

    for row in rows:
        book_id = row["id"]
        item_id = row["item_id"]
        item_ino = str(row["item_ino"] or "")
        af_json = row["audioFiles"]
        lf_json = row["libraryFiles"]
        item_path = row["path"]
        title = row["title"]

        if not af_json:
            continue

        audio_files = json.loads(af_json)
        library_files = json.loads(lf_json) if lf_json else []

        # Strip the library prefix from DB path to get relative path
        # e.g., "/audiobooks/Author/Book" -> "Author/Book"
        rel_base = item_path.lstrip("/")
        if library_prefix and rel_base.startswith(library_prefix + "/"):
            rel_base = rel_base[len(library_prefix) + 1:]

        af_changed = False
        lf_changed = False
        item_ino_changed = False

        # Check folder inode
        folder_ino = inode_map.get(rel_base)
        if folder_ino and item_ino != folder_ino:
            item_ino_changed = True

        # Check audio file inodes
        for af in audio_files:
            total_files += 1
            stored_ino = str(af.get("ino", ""))
            rel_path = af.get("metadata", {}).get("relPath", "")
            if not rel_path:
                continue

            file_rel = os.path.join(rel_base, rel_path)
            disk_ino = inode_map.get(file_rel)

            if disk_ino is None:
                missing += 1
                continue

            if stored_ino != disk_ino:
                mismatched += 1
                af["ino"] = disk_ino
                af_changed = True

        # Check library file inodes
        for lf in library_files:
            stored_ino = str(lf.get("ino", ""))
            rel_path = lf.get("metadata", {}).get("relPath", "")
            if not rel_path:
                continue

            file_rel = os.path.join(rel_base, rel_path)
            disk_ino = inode_map.get(file_rel)

            if disk_ino is not None and stored_ino != disk_ino:
                lf["ino"] = disk_ino
                lf_changed = True

        # Apply updates
        if af_changed or lf_changed or item_ino_changed:
            books_fixed += 1
            if not dry_run:
                try:
                    if af_changed:
                        conn.execute(
                            "UPDATE books SET audioFiles = ? WHERE id = ?",
                            (json.dumps(audio_files), book_id),
                        )
                    if lf_changed or item_ino_changed:
                        if item_ino_changed and folder_ino:
                            conn.execute(
                                "UPDATE libraryItems SET libraryFiles = ?, ino = ? WHERE id = ?",
                                (json.dumps(library_files), folder_ino, item_id),
                            )
                        elif lf_changed:
                            conn.execute(
                                "UPDATE libraryItems SET libraryFiles = ? WHERE id = ?",
                                (json.dumps(library_files), item_id),
                            )
                except Exception as e:
                    errors += 1
                    print(f"  ERROR updating '{title}': {e}")
            print(f"  Fixed: {title}")

    if not dry_run and books_fixed > 0:
        conn.commit()
    conn.close()

    return len(rows), total_files, mismatched, books_fixed, missing, errors


def restart_abs(container_name):
    """Restart the ABS Docker container so it picks up inode changes."""
    print(f"Restarting ABS container '{container_name}'...")
    try:
        result = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            print(f"ABS container '{container_name}' restarted successfully.")
        else:
            print(f"Warning: docker restart failed: {result.stderr.strip()}")
            print(f"  Restart manually with: docker restart {container_name}")
    except FileNotFoundError:
        print(f"Warning: docker not found. Restart ABS manually: docker restart {container_name}")
    except subprocess.TimeoutExpired:
        print(f"Warning: docker restart timed out. Check container status manually.")
    except Exception as e:
        print(f"Warning: could not restart container: {e}")
        print(f"  Restart manually with: docker restart {container_name}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync filesystem inodes with AudiobookShelf database. "
        "Fixes 404 errors caused by SSHFS/NFS remounts changing inode numbers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables:\n"
            "  ABS_DB           Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)\n"
            "  AUDIOBOOKS_DIR   Audiobook library mount point (default: /audiobooks)\n"
            "  ABS_CONTAINER    Docker container name (default: audiobookshelf)\n"
            "  SENTINEL_DIR     Directory for sentinel file (default: same dir as ABS_DB)\n"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force sync even if mount inode hasn't changed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying the database",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Don't restart ABS container after fixing inodes",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Scan locally (kept for compatibility; this is now the only mode)",
    )
    args = parser.parse_args()

    db_path, audiobooks_dir, container, sentinel_dir = get_config()

    print(f"abs-inode-sync")
    print(f"  Database:    {db_path}")
    print(f"  Audiobooks:  {audiobooks_dir}")
    print(f"  Container:   {container}")
    print(f"  Sentinel:    {sentinel_dir}")
    print()

    # Validate paths
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}")
        print("  Set ABS_DB environment variable to point to absdatabase.sqlite")
        sys.exit(1)

    if not os.path.isdir(audiobooks_dir):
        print(f"ERROR: Audiobook directory not found at {audiobooks_dir}")
        print("  Set AUDIOBOOKS_DIR environment variable to your library mount point")
        sys.exit(1)

    # Sentinel check (skip sync if mount hasn't changed)
    if not args.force and not args.dry_run:
        if not check_sentinel(audiobooks_dir, sentinel_dir):
            return

    start_time = time.time()

    # Auto-detect library prefix from DB
    library_prefix = detect_library_prefix(db_path)
    if library_prefix:
        print(f"  Detected library prefix: /{library_prefix}/")
    print()

    # Scan filesystem
    inode_map = scan_filesystem(audiobooks_dir)
    scan_time = time.time() - start_time

    if not inode_map:
        print("ERROR: No files found on filesystem. Is the mount working?")
        sys.exit(1)

    # Compare and fix
    books_checked, files_checked, mismatched, books_fixed, missing_files, errors = (
        sync_inodes(db_path, inode_map, library_prefix, dry_run=args.dry_run)
    )

    elapsed = time.time() - start_time
    mode = "DRY RUN" if args.dry_run else "APPLIED"

    print()
    print("=" * 50)
    print(f"INODE SYNC {mode}")
    print(f"  Books checked:        {books_checked}")
    print(f"  Audio files checked:  {files_checked}")
    print(f"  Inode mismatches:     {mismatched}")
    print(f"  Books fixed:          {books_fixed}")
    print(f"  Files missing on disk: {missing_files}")
    print(f"  Errors:               {errors}")
    print(f"  Scan time:            {scan_time:.0f}s")
    print(f"  Total time:           {elapsed:.0f}s")
    print("=" * 50)

    # Save sentinel
    if not args.dry_run:
        save_sentinel(audiobooks_dir, sentinel_dir)

    # Restart ABS if we fixed anything
    if not args.dry_run and books_fixed > 0 and not args.no_restart:
        restart_abs(container)
    elif books_fixed > 0 and args.no_restart:
        print(f"Restart skipped (--no-restart). Run: docker restart {container}")


if __name__ == "__main__":
    main()
