#!/usr/bin/env python3
"""
build-cache: Build a WebP image cache from AudiobookShelf cover images.

For each book in the ABS database, this script:
  1. Finds the cover image in ABS metadata directory ({ABS_METADATA}/items/{uuid}/cover.*)
  2. Converts it to WebP at 800px wide using cwebp (must be installed)
  3. Creates a thumbnail at 200px wide, quality 30
  4. Saves to {IMAGE_CACHE_DIR}/{uuid}/cover.webp and cover-thumb.webp

The cache is served directly by Caddy/nginx for fast cover delivery without
hitting ABS at all.

Requires: cwebp (from libwebp-tools / webp package)

Configuration (env vars or .env file):
  ABS_DB           - Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)
  ABS_METADATA     - ABS metadata directory (default: /config/metadata)
  IMAGE_CACHE_DIR  - Output cache directory (default: /var/cache/image-cache)

Usage:
  build-cache.py             # Incremental: only process new/changed covers
  build-cache.py --force     # Rebuild all covers
  build-cache.py --dry-run   # Show what would be processed
  build-cache.py --verbose   # Print each file as it's processed
"""

import argparse
import glob
import os
import shutil
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
    metadata_dir = os.environ.get("ABS_METADATA", "/config/metadata")
    cache_dir = os.environ.get("IMAGE_CACHE_DIR", "/var/cache/image-cache")
    return db_path, metadata_dir, cache_dir


def check_cwebp():
    """Verify cwebp is installed and available."""
    if shutil.which("cwebp") is None:
        print("ERROR: cwebp not found. Install it:")
        print("  Ubuntu/Debian: apt install webp")
        print("  macOS:         brew install webp")
        print("  Alpine:        apk add libwebp-tools")
        sys.exit(1)


def find_cover(metadata_dir, item_id):
    """Find cover image for a book in ABS metadata directory.

    ABS stores covers at: {metadata_dir}/items/{uuid}/cover.{ext}
    Extensions can be jpg, jpeg, png, webp, etc.

    Returns the path to the cover file, or None if not found.
    """
    item_dir = os.path.join(metadata_dir, "items", item_id)
    if not os.path.isdir(item_dir):
        return None

    for fname in os.listdir(item_dir):
        lower = fname.lower()
        if lower.startswith("cover") and lower.endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff")
        ):
            return os.path.join(item_dir, fname)

    return None


def convert_to_webp(source, dest, width, quality=80):
    """Convert an image to WebP at the specified width and quality.

    Uses cwebp for the conversion. Width is set; height scales proportionally.

    Returns True on success, False on failure.
    """
    try:
        result = subprocess.run(
            [
                "cwebp",
                "-resize", str(width), "0",  # width x auto-height
                "-q", str(quality),
                "-quiet",
                source,
                "-o", dest,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def needs_update(source, dest):
    """Check if the cached file needs to be regenerated.

    Returns True if dest doesn't exist or source is newer than dest.
    """
    if not os.path.exists(dest):
        return True
    try:
        return os.path.getmtime(source) > os.path.getmtime(dest)
    except OSError:
        return True


def build_cache(db_path, metadata_dir, cache_dir, force=False, dry_run=False, verbose=False):
    """Build or update the image cache for all books.

    Returns (processed, skipped, errors, no_cover).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Get all library items with titles for logging
    items = conn.execute(
        "SELECT li.id as item_id, b.title "
        "FROM libraryItems li "
        "JOIN books b ON li.mediaId = b.id "
        "ORDER BY b.title"
    ).fetchall()
    conn.close()

    print(f"Found {len(items)} library items")

    processed = 0
    skipped = 0
    errors = 0
    no_cover = 0

    for item in items:
        item_id = item["item_id"]
        title = item["title"]

        # Find source cover
        cover_path = find_cover(metadata_dir, item_id)
        if not cover_path:
            no_cover += 1
            if verbose:
                print(f"  NO COVER: {title}")
            continue

        # Output paths
        out_dir = os.path.join(cache_dir, item_id)
        cover_webp = os.path.join(out_dir, "cover.webp")
        thumb_webp = os.path.join(out_dir, "cover-thumb.webp")

        # Check if update needed
        if not force and not needs_update(cover_path, cover_webp):
            skipped += 1
            continue

        if dry_run:
            if verbose:
                print(f"  WOULD PROCESS: {title}")
            processed += 1
            continue

        # Create output directory
        os.makedirs(out_dir, exist_ok=True)

        # Convert full-size cover (800px wide, quality 80)
        ok_cover = convert_to_webp(cover_path, cover_webp, width=800, quality=80)

        # Convert thumbnail (200px wide, quality 30)
        ok_thumb = convert_to_webp(cover_path, thumb_webp, width=200, quality=30)

        if ok_cover and ok_thumb:
            processed += 1
            if verbose:
                cover_size = os.path.getsize(cover_webp) if os.path.exists(cover_webp) else 0
                thumb_size = os.path.getsize(thumb_webp) if os.path.exists(thumb_webp) else 0
                print(
                    f"  OK: {title} "
                    f"(cover: {cover_size // 1024}KB, thumb: {thumb_size // 1024}KB)"
                )
            elif processed % 100 == 0:
                print(f"  Processed {processed}...")
        else:
            errors += 1
            # Clean up partial output
            if not ok_cover and os.path.exists(cover_webp):
                os.remove(cover_webp)
            if not ok_thumb and os.path.exists(thumb_webp):
                os.remove(thumb_webp)
            print(f"  ERROR: {title} (cover={'OK' if ok_cover else 'FAIL'}, thumb={'OK' if ok_thumb else 'FAIL'})")

    return processed, skipped, errors, no_cover


def main():
    parser = argparse.ArgumentParser(
        description="Build a WebP image cache from AudiobookShelf cover images. "
        "Covers are converted to 800px WebP (full) and 200px WebP (thumbnail) "
        "for fast serving via Caddy or nginx.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Requires: cwebp (apt install webp / brew install webp)\n\n"
            "Environment variables:\n"
            "  ABS_DB          Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)\n"
            "  ABS_METADATA    ABS metadata directory (default: /config/metadata)\n"
            "  IMAGE_CACHE_DIR Output cache directory (default: /var/cache/image-cache)\n"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild all covers, even if cache is up to date",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without creating files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each file as it's processed",
    )
    args = parser.parse_args()

    db_path, metadata_dir, cache_dir = get_config()

    print("abs-turbo image cache builder")
    print(f"  Database:    {db_path}")
    print(f"  Metadata:    {metadata_dir}")
    print(f"  Cache dir:   {cache_dir}")
    print(f"  Mode:        {'force rebuild' if args.force else 'incremental'}")
    if args.dry_run:
        print("  DRY RUN - no files will be written")
    print()

    # Validate
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}")
        print("  Set ABS_DB environment variable to point to absdatabase.sqlite")
        sys.exit(1)

    if not os.path.isdir(metadata_dir):
        print(f"ERROR: Metadata directory not found at {metadata_dir}")
        print("  Set ABS_METADATA environment variable (usually /config/metadata or")
        print("  /path/to/audiobookshelf/config/metadata)")
        sys.exit(1)

    check_cwebp()

    # Create cache directory
    if not args.dry_run:
        os.makedirs(cache_dir, exist_ok=True)

    start_time = time.time()

    processed, skipped, errors, no_cover = build_cache(
        db_path, metadata_dir, cache_dir,
        force=args.force, dry_run=args.dry_run, verbose=args.verbose,
    )

    elapsed = time.time() - start_time
    mode = "DRY RUN" if args.dry_run else ("FULL REBUILD" if args.force else "INCREMENTAL")

    # Cache size
    if os.path.isdir(cache_dir) and not args.dry_run:
        total_size = 0
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(cache_dir):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total_size += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    pass
        size_str = f"{total_size / (1024 * 1024):.1f}MB ({file_count} files)"
    else:
        size_str = "N/A"

    print()
    print("=" * 50)
    print(f"IMAGE CACHE BUILD {mode}")
    print(f"  Processed:    {processed}")
    print(f"  Skipped:      {skipped} (already up to date)")
    print(f"  No cover:     {no_cover}")
    print(f"  Errors:       {errors}")
    print(f"  Cache size:   {size_str}")
    print(f"  Time:         {elapsed:.1f}s")
    print("=" * 50)


if __name__ == "__main__":
    main()
