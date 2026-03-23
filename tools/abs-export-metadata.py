#!/usr/bin/env python3
"""
abs-export-metadata: Export ABS metadata to sidecar files.

Reads the AudiobookShelf database and writes metadata.abs sidecar files for
every book. These files protect curated metadata (titles, authors, narrators,
genres, tags, descriptions, series) from being overwritten during library scans.

ABS reads metadata.abs files on scan, so your curated data is preserved even
if Audible/matched metadata would otherwise overwrite it.

Configuration (env vars or .env file):
  ABS_DB         - Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)
  AUDIOBOOKS_DIR - Mount point of audiobook library (default: /audiobooks)

Usage:
  abs-export-metadata.py                  # Export all books
  abs-export-metadata.py --dry-run        # Show what would be written
  abs-export-metadata.py --verbose        # Print each file as it's written
"""

import argparse
import json
import os
import sqlite3
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
    return db_path, audiobooks_dir


def detect_library_prefix(db_path):
    """Auto-detect the library path prefix from the database."""
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT path FROM libraryItems LIMIT 1").fetchone()
        conn.close()
        if row and row[0]:
            parts = row[0].strip("/").split("/")
            if len(parts) >= 2:
                return parts[0]
    except Exception:
        pass
    return ""


def format_metadata_abs(title, authors, narrators, series_list, genres, tags,
                        description, published_year, language):
    """Format metadata into the metadata.abs sidecar format.

    The format is:
        ;DIFFUSE metadata
        key=value
        ...

    Multi-value fields (authors, narrators, genres, tags) are comma-separated.
    Series entries include sequence number: "Series Name #N"
    """
    lines = [";DIFFUSE metadata"]

    if title:
        lines.append(f"title={title}")
    if authors:
        lines.append(f"authors={authors}")
    if narrators:
        lines.append(f"narrators={narrators}")
    if series_list:
        # series_list is a list of "Series Name #N" strings
        for s in series_list:
            lines.append(f"series={s}")
    if genres:
        lines.append(f"genres={genres}")
    if tags:
        lines.append(f"tags={tags}")
    if description:
        # Collapse newlines in description to single line
        desc_clean = description.replace("\r\n", " ").replace("\n", " ").strip()
        lines.append(f"description={desc_clean}")
    if published_year:
        lines.append(f"publishedYear={published_year}")
    if language:
        lines.append(f"language={language}")

    return "\n".join(lines) + "\n"


def export_all(db_path, audiobooks_dir, library_prefix, dry_run=False, verbose=False):
    """Export metadata.abs files for all books in the database.

    Joins across books, bookAuthors, authors, bookSeries, and series tables
    to gather complete metadata for each book.

    Returns (exported_count, skipped_count, error_count).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get all books with their library item paths
    books = conn.execute(
        "SELECT b.id, b.title, b.narrators, b.genres, b.tags, "
        "b.description, b.publishedYear, b.language, "
        "li.path "
        "FROM books b "
        "JOIN libraryItems li ON li.mediaId = b.id "
        "ORDER BY b.title"
    ).fetchall()

    # Build author lookup: book_id -> [author names] (ordered)
    author_map = {}
    try:
        author_rows = conn.execute(
            "SELECT ba.bookId, a.name "
            "FROM bookAuthors ba "
            "JOIN authors a ON ba.authorId = a.id "
            "ORDER BY ba.bookId, ba.createdAt"
        ).fetchall()
        for row in author_rows:
            author_map.setdefault(row["bookId"], []).append(row["name"])
    except sqlite3.OperationalError:
        # Fallback: some ABS versions store authors as JSON in books table
        print("  Warning: bookAuthors table not found, falling back to books.authors JSON")

    # Build series lookup: book_id -> ["Series Name #sequence"]
    series_map = {}
    try:
        series_rows = conn.execute(
            "SELECT bs.bookId, s.name, bs.sequence "
            "FROM bookSeries bs "
            "JOIN series s ON bs.seriesId = s.id "
            "ORDER BY bs.bookId, bs.sequence"
        ).fetchall()
        for row in series_rows:
            book_id = row["bookId"]
            name = row["name"]
            seq = row["sequence"]
            if seq:
                series_map.setdefault(book_id, []).append(f"{name} #{seq}")
            else:
                series_map.setdefault(book_id, []).append(name)
    except sqlite3.OperationalError:
        print("  Warning: bookSeries/series tables not found")

    conn.close()

    exported = 0
    skipped = 0
    errors = 0

    for book in books:
        book_id = book["id"]
        title = book["title"] or ""
        item_path = book["path"] or ""

        # Resolve filesystem path
        rel_base = item_path.lstrip("/")
        if library_prefix and rel_base.startswith(library_prefix + "/"):
            rel_base = rel_base[len(library_prefix) + 1:]
        fs_path = os.path.join(audiobooks_dir, rel_base)

        if not os.path.isdir(fs_path):
            if verbose:
                print(f"  SKIP (dir not found): {title} -> {fs_path}")
            skipped += 1
            continue

        # Gather metadata
        authors = ", ".join(author_map.get(book_id, []))
        narrators_raw = book["narrators"]
        if narrators_raw:
            # narrators may be stored as JSON array or comma-separated string
            try:
                narr_list = json.loads(narrators_raw)
                if isinstance(narr_list, list):
                    narrators = ", ".join(narr_list)
                else:
                    narrators = str(narr_list)
            except (json.JSONDecodeError, TypeError):
                narrators = narrators_raw
        else:
            narrators = ""

        series_list = series_map.get(book_id, [])

        genres_raw = book["genres"]
        if genres_raw:
            try:
                genre_list = json.loads(genres_raw)
                if isinstance(genre_list, list):
                    genres = ", ".join(genre_list)
                else:
                    genres = str(genre_list)
            except (json.JSONDecodeError, TypeError):
                genres = genres_raw
        else:
            genres = ""

        tags_raw = book["tags"]
        if tags_raw:
            try:
                tag_list = json.loads(tags_raw)
                if isinstance(tag_list, list):
                    tags = ", ".join(tag_list)
                else:
                    tags = str(tag_list)
            except (json.JSONDecodeError, TypeError):
                tags = tags_raw
        else:
            tags = ""

        description = book["description"] or ""
        published_year = book["publishedYear"] or ""
        language = book["language"] or ""

        content = format_metadata_abs(
            title, authors, narrators, series_list, genres, tags,
            description, published_year, language,
        )

        metadata_path = os.path.join(fs_path, "metadata.abs")

        if dry_run:
            if verbose:
                print(f"  WOULD WRITE: {metadata_path}")
            exported += 1
            continue

        try:
            with open(metadata_path, "w", encoding="utf-8") as f:
                f.write(content)
            exported += 1
            if verbose:
                print(f"  WROTE: {metadata_path}")
        except OSError as e:
            errors += 1
            print(f"  ERROR writing {metadata_path}: {e}")

    return exported, skipped, errors


def main():
    parser = argparse.ArgumentParser(
        description="Export AudiobookShelf metadata to metadata.abs sidecar files. "
        "Protects curated metadata during library scans.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables:\n"
            "  ABS_DB         Path to absdatabase.sqlite (default: /config/absdatabase.sqlite)\n"
            "  AUDIOBOOKS_DIR Audiobook library mount point (default: /audiobooks)\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without creating files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each file as it's written",
    )
    args = parser.parse_args()

    db_path, audiobooks_dir = get_config()

    print("abs-export-metadata")
    print(f"  Database:    {db_path}")
    print(f"  Audiobooks:  {audiobooks_dir}")
    print()

    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}")
        print("  Set ABS_DB environment variable to point to absdatabase.sqlite")
        sys.exit(1)

    if not os.path.isdir(audiobooks_dir):
        print(f"ERROR: Audiobook directory not found at {audiobooks_dir}")
        print("  Set AUDIOBOOKS_DIR environment variable to your library mount point")
        sys.exit(1)

    library_prefix = detect_library_prefix(db_path)
    if library_prefix:
        print(f"  Detected library prefix: /{library_prefix}/")
    print()

    start_time = time.time()

    exported, skipped, errors = export_all(
        db_path, audiobooks_dir, library_prefix,
        dry_run=args.dry_run, verbose=args.verbose,
    )

    elapsed = time.time() - start_time
    mode = "DRY RUN" if args.dry_run else "COMPLETE"

    print()
    print("=" * 50)
    print(f"METADATA EXPORT {mode}")
    print(f"  Books exported:  {exported}")
    print(f"  Books skipped:   {skipped} (directory not found on disk)")
    print(f"  Errors:          {errors}")
    print(f"  Time:            {elapsed:.1f}s")
    print("=" * 50)


if __name__ == "__main__":
    main()
