#!/usr/bin/env python3
"""
abs-add: Add audiobooks to AudiobookShelf without a full library scan.

Writes directly to the ABS SQLite database, creating all necessary records
(libraryItem, book, authors, series, bookAuthors, bookSeries) from the
audio files on disk and any metadata.abs sidecar.

Configuration via environment variables:
  ABS_DB           Path to absdatabase.sqlite  (default: /config/absdatabase.sqlite)
  AUDIOBOOKS_DIR   Root of the audiobook files  (default: /audiobooks)
  ABS_CONTAINER    Docker container name        (default: audiobookshelf)
  ABS_URL          ABS API base URL             (default: http://localhost:8000)

Usage:
  abs-add /audiobooks/Author/Book
  abs-add /audiobooks/Author/Series/Book1 /audiobooks/Author/Series/Book2
  abs-add --scan
  abs-add --scan --dry-run
  abs-add --scan --library "My Audiobooks"
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration — all from env vars with sensible defaults
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("ABS_DB", "/config/absdatabase.sqlite")
AUDIOBOOKS_DIR = os.environ.get("AUDIOBOOKS_DIR", "/audiobooks")
ABS_CONTAINER = os.environ.get("ABS_CONTAINER", "audiobookshelf")
ABS_URL = os.environ.get("ABS_URL", "http://localhost:8000")

AUDIO_EXTENSIONS = {".m4b", ".m4a", ".mp4", ".mp3", ".flac", ".ogg", ".opus", ".wma", ".aac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def natural_sort_key(filename):
    """Sort key that handles embedded numbers naturally (Chapter 2 < Chapter 10)."""
    parts = re.split(r"(\d+)", filename)
    result = []
    for part in parts:
        if part.isdigit():
            result.append((0, int(part)))
        else:
            result.append((1, part.lower()))
    return result


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000 +00:00")


def now_ms():
    return int(time.time() * 1000)


def strip_article_prefix(name):
    """Remove leading The/A/An for sort-friendly versions."""
    return re.sub(r"^(The|A|An)\s+", "", name, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Database introspection
# ---------------------------------------------------------------------------


def get_table_columns(conn, table_name):
    """Return the set of column names for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor}


def resolve_library(conn, library_name=None):
    """
    Resolve the library ID and folder ID from the database.

    If *library_name* is given, match by name.  Otherwise pick the first
    (and usually only) library.

    Returns (library_id, library_folder_id, library_folder_path).
    """
    if library_name:
        row = conn.execute(
            "SELECT id, name FROM libraries WHERE name = ?", (library_name,)
        ).fetchone()
        if not row:
            # Try case-insensitive
            row = conn.execute(
                "SELECT id, name FROM libraries WHERE LOWER(name) = LOWER(?)",
                (library_name,),
            ).fetchone()
        if not row:
            available = [
                r[0] for r in conn.execute("SELECT name FROM libraries").fetchall()
            ]
            print(f"Error: library '{library_name}' not found.", file=sys.stderr)
            print(f"Available libraries: {', '.join(available)}", file=sys.stderr)
            sys.exit(1)
        library_id = row[0]
    else:
        rows = conn.execute("SELECT id, name FROM libraries").fetchall()
        if not rows:
            print("Error: no libraries found in the ABS database.", file=sys.stderr)
            sys.exit(1)
        if len(rows) > 1:
            names = [r[1] for r in rows]
            print(
                f"Multiple libraries found: {', '.join(names)}",
                file=sys.stderr,
            )
            print("Use --library <name> to pick one.", file=sys.stderr)
            sys.exit(1)
        library_id = rows[0][0]

    folder_row = conn.execute(
        "SELECT id, path FROM libraryFolders WHERE libraryId = ? LIMIT 1",
        (library_id,),
    ).fetchone()
    if not folder_row:
        print(
            f"Error: no library folder found for library {library_id}.",
            file=sys.stderr,
        )
        sys.exit(1)

    return library_id, folder_row[0], folder_row[1]


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------


def ffprobe_file(filepath):
    """Run ffprobe and return parsed JSON, or None on failure."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception as e:
        print(f"  ffprobe error for {filepath}: {e}")
        return None


# ---------------------------------------------------------------------------
# metadata.abs parser
# ---------------------------------------------------------------------------


def parse_metadata_abs(folder):
    """
    Parse a metadata.abs sidecar file.

    Returns a dict with keys like title, subtitle, authors (list),
    narrators (list), genres (list), tags (list), series (list of tuples),
    description, publishedYear, publisher, isbn, asin, language, etc.
    """
    abs_path = os.path.join(folder, "metadata.abs")
    if not os.path.exists(abs_path):
        return {}

    metadata = {}
    series_list = []

    with open(abs_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if key == "series":
                m = re.match(r"^(.+?)\s*#(\S+)$", value)
                if m:
                    series_list.append((m.group(1).strip(), m.group(2)))
                else:
                    series_list.append((value, None))
            elif key in ("genres", "tags", "narrators", "authors"):
                metadata[key] = [x.strip() for x in value.split(",")]
            else:
                metadata[key] = value

    if series_list:
        metadata["series"] = series_list
    return metadata


# ---------------------------------------------------------------------------
# Audio file entry builder
# ---------------------------------------------------------------------------


def build_audio_file_entry(filepath, folder, index, audiobooks_dir, library_folder_path):
    """
    Build the JSON structure for one audio file as stored in books.audioFiles.
    """
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    rel_path = os.path.relpath(filepath, folder)

    stat = os.stat(filepath)
    probe = ffprobe_file(filepath)
    if not probe:
        return None

    # Find the first audio stream
    audio_stream = None
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break
    if not audio_stream:
        return None

    fmt = probe.get("format", {})
    duration = float(fmt.get("duration", 0))
    bit_rate = int(fmt.get("bit_rate", 0))
    format_name = fmt.get("format_long_name", fmt.get("format_name", ""))
    codec = audio_stream.get("codec_name", "")
    channels = int(audio_stream.get("channels", 2))
    channel_layout = audio_stream.get("channel_layout", "stereo")
    time_base = audio_stream.get("time_base", "1/44100")
    language = audio_stream.get("tags", {}).get("language", "")

    # Detect embedded cover art (video stream = cover)
    has_cover = any(
        s.get("codec_type") == "video" for s in probe.get("streams", [])
    )

    # Parse embedded chapters
    embedded_chapters = []
    for i, ch in enumerate(probe.get("chapters", [])):
        ch_start = float(ch.get("start_time", 0))
        ch_end = float(ch.get("end_time", 0))
        ch_title = ch.get("tags", {}).get("title", f"Chapter {i + 1}")
        embedded_chapters.append(
            {"id": i, "start": ch_start, "end": ch_end, "title": ch_title}
        )

    # Try to extract track number from filename
    track_num = None
    disc_num = None
    m = re.match(r"^(\d+)\s*[-.]?\s+", filename)
    if m:
        track_num = int(m.group(1))

    # Encoder metadata
    meta_tags = {}
    fmt_tags = fmt.get("tags", {})
    if "encoder" in fmt_tags:
        meta_tags["tagEncoder"] = fmt_tags["encoder"]

    mime_map = {
        ".m4b": "audio/mp4",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".aac": "audio/aac",
    }
    mime_type = mime_map.get(ext, "audio/mpeg")

    # ABS-relative path: library_folder_path + relative from audiobooks_dir
    rel_from_root = os.path.relpath(filepath, audiobooks_dir)
    abs_path = os.path.join(library_folder_path, rel_from_root)

    ts = now_ms()

    return {
        "index": index,
        "ino": str(stat.st_ino),
        "metadata": {
            "filename": filename,
            "ext": ext,
            "path": abs_path,
            "relPath": rel_path,
            "size": stat.st_size,
            "mtimeMs": int(stat.st_mtime * 1000),
            "ctimeMs": int(stat.st_ctime * 1000),
            "birthtimeMs": 0,
        },
        "addedAt": ts,
        "updatedAt": ts,
        "trackNumFromMeta": None,
        "discNumFromMeta": None,
        "trackNumFromFilename": track_num,
        "discNumFromFilename": disc_num,
        "manuallyVerified": False,
        "exclude": False,
        "error": None,
        "format": format_name,
        "duration": duration,
        "bitRate": bit_rate,
        "language": language or None,
        "codec": codec,
        "timeBase": time_base,
        "channels": channels,
        "channelLayout": channel_layout,
        "chapters": embedded_chapters if embedded_chapters else [],
        "embeddedCoverArt": "mjpeg" if has_cover else None,
        "metaTags": meta_tags,
        "mimeType": mime_type,
    }


# ---------------------------------------------------------------------------
# Chapter builder
# ---------------------------------------------------------------------------


def build_chapters(audio_files):
    """
    Build the unified chapter list from audio file entries.

    If a file has multiple embedded chapters, they are used as-is (offset by
    cumulative duration).  Otherwise each file becomes one chapter.
    """
    sorted_af = sorted(audio_files, key=lambda x: x.get("index", 0))
    chapters = []
    cumulative = 0.0
    ch_id = 0

    for af in sorted_af:
        duration = af.get("duration", 0)
        embedded = af.get("chapters", [])
        filename = af["metadata"]["filename"]

        if embedded and len(embedded) > 1:
            for ec in embedded:
                chapters.append(
                    {
                        "id": ch_id,
                        "start": round(cumulative + ec["start"], 6),
                        "end": round(cumulative + ec["end"], 6),
                        "title": ec.get("title", f"Chapter {ch_id + 1}"),
                    }
                )
                ch_id += 1
        elif embedded and len(embedded) == 1:
            chapters.append(
                {
                    "id": ch_id,
                    "start": round(cumulative, 6),
                    "end": round(cumulative + duration, 6),
                    "title": embedded[0].get("title", filename),
                }
            )
            ch_id += 1
        else:
            title = re.sub(r"\.\w+$", "", filename)
            chapters.append(
                {
                    "id": ch_id,
                    "start": round(cumulative, 6),
                    "end": round(cumulative + duration, 6),
                    "title": title,
                }
            )
            ch_id += 1

        cumulative += duration

    return chapters


# ---------------------------------------------------------------------------
# Author / Series helpers
# ---------------------------------------------------------------------------


def get_or_create_author(conn, author_name, library_id):
    row = conn.execute(
        "SELECT id FROM authors WHERE name = ? AND libraryId = ?",
        (author_name, library_id),
    ).fetchone()
    if row:
        return row[0]

    author_id = str(uuid.uuid4())
    parts = author_name.split()
    if len(parts) > 1:
        last_first = f"{parts[-1]}, {' '.join(parts[:-1])}"
    else:
        last_first = author_name

    now = now_iso()
    conn.execute(
        "INSERT INTO authors (id, name, lastFirst, asin, description, imagePath, "
        "createdAt, updatedAt, libraryId) VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?)",
        (author_id, author_name, last_first, now, now, library_id),
    )
    return author_id


def get_or_create_series(conn, series_name, library_id):
    row = conn.execute(
        "SELECT id FROM series WHERE name = ? AND libraryId = ?",
        (series_name, library_id),
    ).fetchone()
    if row:
        return row[0]

    series_id = str(uuid.uuid4())
    name_no_prefix = strip_article_prefix(series_name)
    now = now_iso()
    conn.execute(
        "INSERT INTO series (id, name, nameIgnorePrefix, description, "
        "createdAt, updatedAt, libraryId) VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (series_id, series_name, name_no_prefix, now, now, library_id),
    )
    return series_id


# ---------------------------------------------------------------------------
# Dynamic INSERT builder
# ---------------------------------------------------------------------------


def dynamic_insert(conn, table, data_dict):
    """
    INSERT a row using only columns that actually exist in the table.

    *data_dict* maps column name -> value.  Columns not present in the table
    schema are silently skipped, so the same code works across ABS versions.
    """
    existing_columns = get_table_columns(conn, table)
    cols = []
    vals = []
    for col, val in data_dict.items():
        if col in existing_columns:
            cols.append(col)
            vals.append(val)

    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    conn.execute(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", vals
    )


# ---------------------------------------------------------------------------
# Core: add a single book
# ---------------------------------------------------------------------------


def add_book(folder_path, conn, library_id, library_folder_id, library_folder_path, dry_run=False):
    """
    Add one audiobook folder to the database.

    Returns True if the book was added (or would be, in dry-run mode).
    """
    folder_path = os.path.abspath(folder_path)
    audiobooks_dir = AUDIOBOOKS_DIR

    if not os.path.isdir(folder_path):
        print(f"Error: {folder_path} is not a directory")
        return False

    if not folder_path.startswith(os.path.abspath(audiobooks_dir)):
        print(f"Error: folder must be under {audiobooks_dir}")
        return False

    # Discover files on disk
    audio_files_on_disk = []
    cover_path = None

    for fname in os.listdir(folder_path):
        fpath = os.path.join(folder_path, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext in AUDIO_EXTENSIONS:
            audio_files_on_disk.append(fname)
        elif ext in IMAGE_EXTENSIONS and "cover" in fname.lower():
            cover_path = fpath

    if not audio_files_on_disk:
        print(f"Error: no audio files found in {folder_path}")
        return False

    audio_files_on_disk.sort(key=natural_sort_key)

    # Compute the path as ABS sees it
    rel_path = os.path.relpath(folder_path, audiobooks_dir)
    abs_item_path = os.path.join(library_folder_path, rel_path)

    # Check for duplicates
    existing = conn.execute(
        "SELECT id FROM libraryItems WHERE path = ?", (abs_item_path,)
    ).fetchone()
    if existing:
        print(f"Already in database: {rel_path}")
        return False

    # Parse metadata sidecar
    metadata = parse_metadata_abs(folder_path)

    folder_name = os.path.basename(folder_path)
    title = metadata.get("title", folder_name)

    print(f"{'[DRY RUN] ' if dry_run else ''}Adding: {title}")
    print(f"  Path: {rel_path}")
    print(f"  Audio files: {len(audio_files_on_disk)}")

    if dry_run:
        return True

    # Probe every audio file
    audio_file_entries = []
    for i, fname in enumerate(audio_files_on_disk):
        fpath = os.path.join(folder_path, fname)
        entry = build_audio_file_entry(
            fpath, folder_path, i + 1, audiobooks_dir, library_folder_path
        )
        if entry:
            audio_file_entries.append(entry)
            if (i + 1) % 50 == 0:
                print(f"  Probed {i + 1}/{len(audio_files_on_disk)} files...")

    if not audio_file_entries:
        print("  Error: could not probe any audio files")
        return False

    print(f"  Probed {len(audio_file_entries)} audio files successfully")

    chapters = build_chapters(audio_file_entries)
    total_duration = sum(af["duration"] for af in audio_file_entries)
    total_size = sum(af["metadata"]["size"] for af in audio_file_entries)
    print(f"  Duration: {total_duration / 3600:.1f} hours, {len(chapters)} chapters")

    book_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    now = now_iso()
    ts = now_ms()

    folder_stat = os.stat(folder_path)
    title_no_prefix = strip_article_prefix(title)

    narrators = json.dumps(metadata.get("narrators", []))
    tags = json.dumps(metadata.get("tags", []))
    genres = json.dumps(metadata.get("genres", []))

    abs_cover_path = cover_path  # Keep as the on-disk path; ABS resolves it

    # -- Build libraryFiles list (all files in the folder) --
    library_files = []
    for fname in os.listdir(folder_path):
        fpath = os.path.join(folder_path, fname)
        if not os.path.isfile(fpath):
            continue
        fstat = os.stat(fpath)
        ext = os.path.splitext(fname)[1].lower()
        if ext in AUDIO_EXTENSIONS:
            file_type = "audio"
        elif ext in IMAGE_EXTENSIONS:
            file_type = "image"
        elif fname == "metadata.abs":
            file_type = "metadata"
        else:
            file_type = "unknown"
        library_files.append(
            {
                "ino": str(fstat.st_ino),
                "metadata": {
                    "filename": fname,
                    "ext": ext,
                    "path": os.path.join(abs_item_path, fname),
                    "relPath": fname,
                    "size": fstat.st_size,
                    "mtimeMs": int(fstat.st_mtime * 1000),
                    "ctimeMs": int(fstat.st_ctime * 1000),
                    "birthtimeMs": 0,
                },
                "addedAt": ts,
                "updatedAt": ts,
                "fileType": file_type,
            }
        )

    # -- INSERT book --
    dynamic_insert(
        conn,
        "books",
        {
            "id": book_id,
            "title": title,
            "titleIgnorePrefix": title_no_prefix,
            "subtitle": metadata.get("subtitle"),
            "publishedYear": metadata.get("publishedYear"),
            "publishedDate": None,
            "publisher": metadata.get("publisher"),
            "description": metadata.get("description"),
            "isbn": metadata.get("isbn"),
            "asin": metadata.get("asin"),
            "language": metadata.get("language"),
            "explicit": 1 if metadata.get("explicit") == "1" else 0,
            "abridged": 1 if metadata.get("abridged") == "1" else 0,
            "coverPath": abs_cover_path,
            "duration": total_duration,
            "narrators": narrators,
            "audioFiles": json.dumps(audio_file_entries),
            "ebookFile": None,
            "chapters": json.dumps(chapters),
            "tags": tags,
            "genres": genres,
            "createdAt": now,
            "updatedAt": now,
        },
    )

    # -- INSERT libraryItem --
    dynamic_insert(
        conn,
        "libraryItems",
        {
            "id": item_id,
            "ino": str(folder_stat.st_ino),
            "path": abs_item_path,
            "relPath": rel_path,
            "mediaId": book_id,
            "mediaType": "book",
            "isFile": 0,
            "isMissing": 0,
            "isInvalid": 0,
            "mtime": now,
            "ctime": now,
            "birthtime": now,
            "size": total_size,
            "lastScan": now,
            "lastScanVersion": "2.17.0",
            "libraryId": library_id,
            "libraryFolderId": library_folder_id,
            "libraryFiles": json.dumps(library_files),
            "createdAt": now,
            "updatedAt": now,
        },
    )

    # -- Authors --
    authors = metadata.get("authors", [])
    if not authors:
        # Fall back to the first path component (author folder)
        parts = rel_path.split(os.sep)
        if parts:
            authors = [parts[0]]

    for author_name in authors:
        author_id = get_or_create_author(conn, author_name, library_id)
        ba_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO bookAuthors (id, createdAt, bookId, authorId) "
            "VALUES (?, ?, ?, ?)",
            (ba_id, now, book_id, author_id),
        )

    # -- Series --
    series_list = metadata.get("series", [])
    for series_name, sequence in series_list:
        series_id = get_or_create_series(conn, series_name, library_id)
        bs_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO bookSeries (id, sequence, createdAt, bookId, seriesId) "
            "VALUES (?, ?, ?, ?, ?)",
            (bs_id, sequence, now, book_id, series_id),
        )

    conn.commit()

    print(f"  Inserted into database: item={item_id}, book={book_id}")
    if authors:
        print(f"  Authors: {', '.join(authors)}")
    if series_list:
        for sname, seq in series_list:
            print(f"  Series: {sname} #{seq}" if seq else f"  Series: {sname}")

    return True


# ---------------------------------------------------------------------------
# Scan mode: auto-detect new books
# ---------------------------------------------------------------------------


def scan_for_new_books(conn, library_id, library_folder_id, library_folder_path, dry_run=False):
    """Walk the audiobooks directory and add any folders not yet in the DB."""
    audiobooks_dir = AUDIOBOOKS_DIR
    print("Scanning for new books...")

    known_paths = set()
    for row in conn.execute("SELECT path FROM libraryItems"):
        known_paths.add(row[0])

    print(f"Known books in DB: {len(known_paths)}")

    new_folders = []

    for author_dir in sorted(os.listdir(audiobooks_dir)):
        author_path = os.path.join(audiobooks_dir, author_dir)
        if not os.path.isdir(author_path) or author_dir.startswith("."):
            continue

        for item in sorted(os.listdir(author_path)):
            item_path = os.path.join(author_path, item)
            if not os.path.isdir(item_path) or item.startswith("."):
                continue

            rel = os.path.relpath(item_path, audiobooks_dir)
            abs_path = os.path.join(library_folder_path, rel)

            has_audio = any(
                os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
                for f in os.listdir(item_path)
                if os.path.isfile(os.path.join(item_path, f))
            )

            if has_audio and abs_path not in known_paths:
                new_folders.append(item_path)
                continue

            # Check one level deeper (Author/Series/Book)
            if not has_audio:
                for sub_item in sorted(os.listdir(item_path)):
                    sub_path = os.path.join(item_path, sub_item)
                    if not os.path.isdir(sub_path) or sub_item.startswith("."):
                        continue

                    sub_rel = os.path.relpath(sub_path, audiobooks_dir)
                    sub_abs_path = os.path.join(library_folder_path, sub_rel)

                    sub_has_audio = any(
                        os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
                        for f in os.listdir(sub_path)
                        if os.path.isfile(os.path.join(sub_path, f))
                    )
                    if sub_has_audio and sub_abs_path not in known_paths:
                        new_folders.append(sub_path)

    if not new_folders:
        print("No new books found.")
        return 0

    print(f"Found {len(new_folders)} new book(s):")
    for f in new_folders:
        print(f"  {os.path.relpath(f, audiobooks_dir)}")
    print()

    added = 0
    for folder in new_folders:
        if add_book(folder, conn, library_id, library_folder_id, library_folder_path, dry_run=dry_run):
            added += 1

    return added


# ---------------------------------------------------------------------------
# Container restart
# ---------------------------------------------------------------------------


def restart_abs_container():
    """Restart the ABS Docker container so it picks up DB changes."""
    container = ABS_CONTAINER
    print(f"Restarting ABS container '{container}'...")

    if not shutil.which("docker"):
        print("  Warning: docker not found in PATH. Skipping restart.")
        print("  You may need to restart the ABS container manually.")
        return

    result = subprocess.run(
        ["docker", "restart", container], capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ABS restarted.")
    else:
        stderr = result.stderr.strip()
        print(f"  Warning: docker restart failed: {stderr}")
        print("  You may need to restart the ABS container manually.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="abs-add",
        description="Add audiobooks to AudiobookShelf without a full library scan.",
        epilog=(
            "Environment variables:\n"
            "  ABS_DB           Path to absdatabase.sqlite  (default: /config/absdatabase.sqlite)\n"
            "  AUDIOBOOKS_DIR   Root of the audiobook files  (default: /audiobooks)\n"
            "  ABS_CONTAINER    Docker container name        (default: audiobookshelf)\n"
            "  ABS_URL          ABS API base URL             (default: http://localhost:8000)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "folders",
        nargs="*",
        metavar="FOLDER",
        help="One or more audiobook folders to add",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Auto-detect new books by comparing the filesystem to the database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be added without writing to the database",
    )
    parser.add_argument(
        "--library",
        metavar="NAME",
        help="Target a specific ABS library by name (required if multiple exist)",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Don't restart the ABS container after adding books",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.scan and not args.folders:
        parser.print_help()
        sys.exit(1)

    if not os.path.isfile(DB_PATH):
        print(f"Error: database not found at {DB_PATH}", file=sys.stderr)
        print(
            "Set the ABS_DB environment variable to the correct path.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    try:
        library_id, library_folder_id, library_folder_path = resolve_library(
            conn, args.library
        )

        if args.scan:
            added = scan_for_new_books(
                conn, library_id, library_folder_id, library_folder_path,
                dry_run=args.dry_run,
            )
        else:
            added = 0
            for folder in args.folders:
                if add_book(
                    folder, conn, library_id, library_folder_id,
                    library_folder_path, dry_run=args.dry_run,
                ):
                    added += 1

        if added > 0 and not args.dry_run:
            if args.no_restart:
                print(
                    f"\nAdded {added} book(s). Skipping container restart (--no-restart)."
                )
            else:
                print(f"\nAdded {added} book(s).")
                restart_abs_container()
        elif added > 0 and args.dry_run:
            print(f"\n[DRY RUN] Would add {added} book(s).")
        else:
            print("\nNo books added.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
