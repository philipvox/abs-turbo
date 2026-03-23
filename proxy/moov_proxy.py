#!/usr/bin/env python3
"""
abs-turbo Moov Cache Proxy for AudiobookShelf.

An aiohttp reverse proxy that sits between your web server (Caddy/nginx)
and AudiobookShelf. It caches moov atoms (ftyp+moov) from M4B/M4A/MP4
files for instant playback start, and optionally maintains a faststart
cache of fully-processed recent files.

Two modes:
  - Proxy mode (default): runs the caching reverse proxy
  - Build mode (--build-cache): walks the audiobook directory, extracts
    ftyp+moov atoms from every audio file, writes the moov cache, and exits

Configuration is via environment variables (see defaults below).
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import struct
import sqlite3
import sys
import time
from pathlib import Path

from aiohttp import web, ClientSession

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------

ABS_URL = os.environ.get("ABS_URL", "http://localhost:8000")
ABS_DB = os.environ.get("ABS_DB", "/config/absdatabase.sqlite")
AUDIOBOOKS_DIR = os.environ.get("AUDIOBOOKS_DIR", "/audiobooks")
MOOV_CACHE_DIR = os.environ.get("MOOV_CACHE_DIR", "/cache/moov")
FASTSTART_CACHE_DIR = os.environ.get("FASTSTART_CACHE_DIR", "/cache/faststart")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "13379"))
FASTSTART_MAX_GB = float(os.environ.get("FASTSTART_MAX_GB", "30"))
FASTSTART_MAX_DAYS = int(os.environ.get("FASTSTART_MAX_DAYS", "30"))

# Derived constants
MAX_FASTSTART_BYTES = int(FASTSTART_MAX_GB * 1024 * 1024 * 1024)
FASTSTART_MAX_AGE = FASTSTART_MAX_DAYS * 86400

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_file_map = {}          # "item_id/file_ino" -> rel_path
_index = {}             # rel_path -> moov cache entry
_rewritten_cache = {}   # rel_path -> rewritten ftyp+moov bytes
_faststart_index = {}   # rel_path -> faststart cache entry
_caching_in_progress = set()


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

def print_banner():
    print("=" * 60)
    print("  abs-turbo  --  Moov Cache Proxy for AudiobookShelf")
    print("=" * 60)
    print(f"  ABS_URL            = {ABS_URL}")
    print(f"  ABS_DB             = {ABS_DB}")
    print(f"  AUDIOBOOKS_DIR     = {AUDIOBOOKS_DIR}")
    print(f"  MOOV_CACHE_DIR     = {MOOV_CACHE_DIR}")
    print(f"  FASTSTART_CACHE_DIR= {FASTSTART_CACHE_DIR}")
    print(f"  PROXY_PORT         = {PROXY_PORT}")
    print(f"  FASTSTART_MAX_GB   = {FASTSTART_MAX_GB}")
    print(f"  FASTSTART_MAX_DAYS = {FASTSTART_MAX_DAYS}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def get_token(request):
    """Extract bearer token from query string or Authorization header."""
    token = request.query.get("token", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return token


# ---------------------------------------------------------------------------
# Index / file-map loading
# ---------------------------------------------------------------------------

def load_index():
    """Load the moov cache index from disk."""
    global _index, _rewritten_cache
    index_path = os.path.join(MOOV_CACHE_DIR, "index.json")
    if os.path.exists(index_path):
        with open(index_path) as f:
            _index = json.load(f)
    _rewritten_cache = {}
    print(f"[MOOV] Loaded cache index: {len(_index)} entries")


def load_faststart_index():
    """Load the faststart cache index from disk."""
    global _faststart_index
    index_path = os.path.join(FASTSTART_CACHE_DIR, "index.json")
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                _faststart_index = json.load(f)
        except Exception:
            _faststart_index = {}
    print(f"[FASTSTART] Loaded index: {len(_faststart_index)} entries")


def _detect_library_prefix():
    """
    Auto-detect the library path prefix from the ABS database.

    ABS stores absolute paths like '/audiobooks/Author/Book' in
    libraryItems.path. We need to know what prefix to strip so that
    rel_path lines up with files under AUDIOBOOKS_DIR.

    Returns the prefix string to strip (e.g. '/audiobooks/') or '/'
    as a fallback.
    """
    try:
        conn = sqlite3.connect(f"file:{ABS_DB}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT path FROM libraryItems LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            # e.g. path = '/audiobooks/Author/Book'
            # We want to find the component that maps to AUDIOBOOKS_DIR
            sample_path = row[0]
            # Walk up from the sample path and see which prefix,
            # when stripped, gives us a relative path that exists
            # under AUDIOBOOKS_DIR
            parts = sample_path.strip("/").split("/")
            # Try stripping 1 component, then 2, etc.
            for i in range(1, len(parts)):
                prefix = "/" + "/".join(parts[:i]) + "/"
                remainder = "/".join(parts[i:])
                candidate = os.path.join(AUDIOBOOKS_DIR, remainder)
                if os.path.exists(candidate):
                    print(f"[MOOV] Auto-detected library prefix: {prefix}")
                    return prefix
            # Fallback: use first path component as prefix
            prefix = "/" + parts[0] + "/"
            print(f"[MOOV] Using first-component prefix: {prefix}")
            return prefix
    except Exception as e:
        print(f"[MOOV] Could not detect library prefix: {e}")
    return "/"


def build_file_map():
    """
    Build a mapping from 'item_id/file_ino' to relative file paths
    by reading the ABS SQLite database.
    """
    global _file_map
    _file_map = {}

    prefix = _detect_library_prefix()

    try:
        conn = sqlite3.connect(f"file:{ABS_DB}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT li.id, li.path, b.audioFiles FROM libraryItems li "
            "JOIN books b ON li.mediaId = b.id"
        )
        for item_id, item_path, audio_files_json in cursor:
            if not audio_files_json:
                continue
            try:
                audio_files = json.loads(audio_files_json)
            except Exception:
                continue

            # Strip the detected prefix to get relative path
            rel_base = item_path
            if rel_base.startswith(prefix):
                rel_base = rel_base[len(prefix):]
            else:
                rel_base = rel_base.lstrip("/")

            for af in audio_files:
                ino = str(af.get("ino", ""))
                rel_path_in_file = af.get("metadata", {}).get("relPath", "")
                if ino and rel_path_in_file:
                    full_rel = os.path.join(rel_base, rel_path_in_file)
                    _file_map[f"{item_id}/{ino}"] = full_rel
        conn.close()
    except Exception as e:
        print(f"[MOOV] Error building file map: {e}")

    print(f"[MOOV] Built file map: {len(_file_map)} entries")


# ---------------------------------------------------------------------------
# Moov cache lookup
# ---------------------------------------------------------------------------

def get_cache_for_request(item_id, file_ino):
    """Look up moov cache entry for a given item_id/file_ino pair."""
    key = f"{item_id}/{file_ino}"
    rel_path = _file_map.get(key)
    if not rel_path:
        return None, None, None

    entry = _index.get(rel_path)
    if not entry:
        return rel_path, None, None

    cache_file = os.path.join(MOOV_CACHE_DIR, f"{entry['cache_key']}.mp4")
    if os.path.exists(cache_file):
        return rel_path, cache_file, entry

    return rel_path, None, None


# ---------------------------------------------------------------------------
# stco / co64 offset rewriting
# ---------------------------------------------------------------------------

def rewrite_stco_offsets(moov_data, shift):
    """Rewrite stco/co64 chunk offsets in moov atom data."""
    data = bytearray(moov_data)
    _rewrite_atoms(data, 0, len(data), shift)
    return bytes(data)


def _rewrite_atoms(data, start, end, shift):
    """Recursively walk atoms and rewrite stco/co64 offsets."""
    pos = start
    while pos < end - 8:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        atom_type = data[pos + 4:pos + 8]

        if size < 8:
            break
        if size == 1 and pos + 16 <= end:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]

        atom_end = pos + size
        if atom_end > end:
            break

        if atom_type == b"stco":
            header_size = 8
            version_flags = 4
            offset = pos + header_size + version_flags
            count = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            for _ in range(count):
                if offset + 4 > atom_end:
                    break
                old_val = struct.unpack(">I", data[offset:offset + 4])[0]
                new_val = old_val + shift
                struct.pack_into(">I", data, offset, new_val)
                offset += 4

        elif atom_type == b"co64":
            header_size = 8
            version_flags = 4
            offset = pos + header_size + version_flags
            count = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            for _ in range(count):
                if offset + 8 > atom_end:
                    break
                old_val = struct.unpack(">Q", data[offset:offset + 8])[0]
                new_val = old_val + shift
                struct.pack_into(">Q", data, offset, new_val)
                offset += 8

        elif atom_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl",
                           b"udta", b"edts", b"meta"):
            child_start = pos + 8
            if atom_type == b"meta":
                child_start = pos + 12
            _rewrite_atoms(data, child_start, atom_end, shift)

        pos = atom_end


def get_rewritten_moov(rel_path, cache_file, entry):
    """
    Return the ftyp+moov data with stco/co64 offsets rewritten so the
    virtual file layout is [ftyp][moov][mdat].
    """
    if rel_path in _rewritten_cache:
        return _rewritten_cache[rel_path]

    raw = open(cache_file, "rb").read()
    moov_at_start = entry["moov_offset"] < 1000

    if moov_at_start:
        _rewritten_cache[rel_path] = raw
        return raw

    moov_size = entry["moov_size"]
    ftyp_size = entry["cache_size"] - moov_size
    ftyp_data = raw[:ftyp_size]
    moov_data = raw[ftyp_size:]
    rewritten_moov = rewrite_stco_offsets(moov_data, moov_size)
    result = ftyp_data + rewritten_moov
    _rewritten_cache[rel_path] = result
    print(f"[STREAM] Rewrote stco offsets for {rel_path} (shift: +{moov_size}B)")
    return result


# ---------------------------------------------------------------------------
# Faststart cache
# ---------------------------------------------------------------------------

def get_faststart_file(rel_path):
    """Look up a faststart-cached file for the given relative path."""
    if rel_path not in _faststart_index:
        return None
    entry = _faststart_index[rel_path]
    cache_file = os.path.join(FASTSTART_CACHE_DIR, f"{entry['cache_key']}.m4b")
    if os.path.exists(cache_file):
        return cache_file, entry
    return None, None


def _faststart_cache_size():
    """Total bytes used by the faststart cache."""
    total = 0
    for entry in _faststart_index.values():
        total += entry.get("cache_size", 0)
    return total


def _save_faststart_index():
    """Persist the faststart index to disk."""
    os.makedirs(FASTSTART_CACHE_DIR, exist_ok=True)
    index_path = os.path.join(FASTSTART_CACHE_DIR, "index.json")
    with open(index_path, "w") as f:
        json.dump(_faststart_index, f)


def _evict_old_faststart():
    """Remove faststart entries older than FASTSTART_MAX_AGE."""
    now = time.time()
    to_remove = []
    for rel_path, entry in _faststart_index.items():
        last_access = entry.get("last_access", entry.get("mtime", 0))
        if now - last_access > FASTSTART_MAX_AGE:
            to_remove.append(rel_path)

    for rel_path in to_remove:
        entry = _faststart_index.pop(rel_path)
        cache_file = os.path.join(FASTSTART_CACHE_DIR, f"{entry['cache_key']}.m4b")
        try:
            os.remove(cache_file)
            print(f"[FASTSTART] Evicted (age): {rel_path}")
        except FileNotFoundError:
            pass

    if to_remove:
        _save_faststart_index()

    return len(to_remove)


async def _background_faststart_cache(rel_path, item_id, file_ino, token, cache_entry):
    """Download and cache a fully faststart-processed copy of a file."""
    if rel_path in _caching_in_progress:
        return
    _caching_in_progress.add(rel_path)

    try:
        if _faststart_cache_size() >= MAX_FASTSTART_BYTES:
            _evict_old_faststart()
            if _faststart_cache_size() >= MAX_FASTSTART_BYTES:
                print(f"[FASTSTART] Cache full, skipping: {rel_path}")
                return

        cache_key = hashlib.md5(rel_path.encode()).hexdigest()[:16]
        cache_path = os.path.join(FASTSTART_CACHE_DIR, f"{cache_key}.m4b")

        moov_cache_file = os.path.join(MOOV_CACHE_DIR, f"{cache_entry['cache_key']}.mp4")
        file_size = cache_entry["file_size"]
        moov_size = cache_entry["moov_size"]
        moov_cache_size = cache_entry["cache_size"]
        ftyp_size = moov_cache_size - moov_size
        moov_at_start = cache_entry["moov_offset"] < 1000

        header_data = get_rewritten_moov(rel_path, moov_cache_file, cache_entry)

        abs_url = f"{ABS_URL}/api/items/{item_id}/file/{file_ino}"
        if token:
            abs_url += f"?token={token}"

        if moov_at_start:
            mdat_start = moov_cache_size
            mdat_end = file_size - 1
        else:
            mdat_start = ftyp_size
            mdat_end = file_size - moov_size - 1

        mdat_range = f"bytes={mdat_start}-{mdat_end}"

        async with ClientSession() as session:
            async with session.get(abs_url, headers={"Range": mdat_range}) as resp:
                if resp.status not in (200, 206):
                    print(f"[FASTSTART] Failed to download mdat for {rel_path}: HTTP {resp.status}")
                    return
                mdat_data = await resp.read()

        os.makedirs(FASTSTART_CACHE_DIR, exist_ok=True)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(header_data)
            f.write(mdat_data)
        os.rename(tmp_path, cache_path)

        total_written = len(header_data) + len(mdat_data)
        _faststart_index[rel_path] = {
            "cache_key": cache_key,
            "mtime": cache_entry.get("mtime", 0),
            "file_size": total_written,
            "cache_size": total_written,
            "last_access": time.time(),
        }
        _save_faststart_index()
        print(f"[FASTSTART] Cached: {rel_path} ({total_written / (1024 * 1024):.1f} MB)")

    except Exception as e:
        print(f"[FASTSTART] Error caching {rel_path}: {e}")
    finally:
        _caching_in_progress.discard(rel_path)


def _trigger_faststart_cache(rel_path, item_id, file_ino, token, cache_entry):
    """Kick off background faststart caching if not already cached/in-progress."""
    if rel_path in _faststart_index:
        _faststart_index[rel_path]["last_access"] = time.time()
        return
    if rel_path in _caching_in_progress:
        return
    loop = asyncio.get_event_loop()
    loop.create_task(_background_faststart_cache(rel_path, item_id, file_ino, token, cache_entry))


# ---------------------------------------------------------------------------
# Serving faststart files
# ---------------------------------------------------------------------------

async def _serve_faststart(request, rel_path, faststart_path, range_header):
    """Serve a file directly from the faststart cache."""
    file_size = os.path.getsize(faststart_path)

    if request.method == "HEAD":
        return web.Response(
            headers={
                "Content-Type": "audio/mp4",
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "X-Moov-Cache": "FASTSTART",
            }
        )

    if not range_header:
        print(f"[FASTSTART] {rel_path} -- serving full file ({file_size}B)")
        with open(faststart_path, "rb") as f:
            data = f.read()
        return web.Response(
            body=data,
            status=200,
            headers={
                "Content-Type": "audio/mp4",
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "X-Moov-Cache": "FASTSTART",
            },
        )

    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        return web.Response(status=416, text="Invalid range")

    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)

    if start >= file_size:
        return web.Response(status=416, text="Range not satisfiable")

    length = end - start + 1

    if rel_path in _faststart_index:
        _faststart_index[rel_path]["last_access"] = time.time()

    print(f"[FASTSTART] {rel_path} -- bytes {start}-{end} ({length}B)")

    with open(faststart_path, "rb") as f:
        f.seek(start)
        data = f.read(length)

    return web.Response(
        body=data,
        status=206,
        headers={
            "Content-Type": "audio/mp4",
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "X-Moov-Cache": "FASTSTART",
        },
    )


# ---------------------------------------------------------------------------
# Proxy to ABS
# ---------------------------------------------------------------------------

async def _proxy_to_abs(request, item_id, file_ino, token, range_header):
    """Proxy a request directly to AudiobookShelf."""
    abs_url = f"{ABS_URL}/api/items/{item_id}/file/{file_ino}"
    if token:
        abs_url += f"?token={token}"

    headers = {}
    if range_header:
        headers["Range"] = range_header

    print(f"[STREAM-PROXY] {item_id}/{file_ino} -- no cache, proxying to ABS")

    async with ClientSession() as session:
        async with session.get(abs_url, headers=headers) as resp:
            resp_headers = {
                "Content-Type": resp.headers.get("Content-Type", "audio/mp4"),
                "Accept-Ranges": "bytes",
            }
            if "Content-Length" in resp.headers:
                resp_headers["Content-Length"] = resp.headers["Content-Length"]
            if "Content-Range" in resp.headers:
                resp_headers["Content-Range"] = resp.headers["Content-Range"]

            body = web.StreamResponse(status=resp.status, headers=resp_headers)
            await body.prepare(request)
            async for chunk in resp.content.iter_any():
                await body.write(chunk)
            await body.write_eof()
            return body


# ---------------------------------------------------------------------------
# Request handlers: /api/items/{item_id}/file/{file_ino}/stream
# ---------------------------------------------------------------------------

async def handle_stream(request):
    """
    Stream endpoint for MSE playback: serves the complete file as
    [ftyp][moov][mdat] with stco/co64 offset rewriting for moov-at-end files.
    """
    item_id = request.match_info["item_id"]
    file_ino = request.match_info["file_ino"]
    token = get_token(request)
    range_header = request.headers.get("Range", "")

    rel_path, cache_file, cache_entry = get_cache_for_request(item_id, file_ino)

    # Try faststart cache first
    if rel_path:
        result = get_faststart_file(rel_path)
        if result and result[0]:
            return await _serve_faststart(request, rel_path, result[0], range_header)

    # No moov cache entry -- proxy directly
    if not cache_file or not cache_entry:
        return await _proxy_to_abs(request, item_id, file_ino, token, range_header)

    # Trigger background faststart caching
    _trigger_faststart_cache(rel_path, item_id, file_ino, token, cache_entry)

    moov_at_start = cache_entry["moov_offset"] < 1000
    file_size = cache_entry["file_size"]
    cache_size = cache_entry["cache_size"]
    moov_size = cache_entry["moov_size"]
    ftyp_size = cache_size - moov_size

    header_data = get_rewritten_moov(rel_path, cache_file, cache_entry)

    if moov_at_start:
        mdat_abs_start_in_original = cache_size
    else:
        mdat_abs_start_in_original = ftyp_size

    mdat_size = file_size - cache_size

    # HEAD request
    if request.method == "HEAD":
        if cache_entry:
            return web.Response(
                headers={
                    "Content-Type": "audio/mp4",
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes",
                    "X-Moov-Cache": "HIT",
                    "X-Moov-Size": str(cache_size),
                }
            )
        else:
            return web.Response(
                headers={
                    "Content-Type": "audio/mp4",
                    "Accept-Ranges": "bytes",
                    "X-Moov-Cache": "MISS",
                }
            )

    # Range request
    if range_header:
        return await _handle_stream_range(
            request, item_id, file_ino, token, rel_path,
            range_header, header_data, cache_entry,
            mdat_abs_start_in_original, file_size, cache_size, mdat_size
        )

    # No range -- serve moov-only as 206
    print(f"[STREAM] {rel_path} -- serving moov-only ({len(header_data)}B from cache, file: {file_size}B)")
    return web.Response(
        body=header_data,
        status=206,
        headers={
            "Content-Type": "audio/mp4",
            "Content-Length": str(len(header_data)),
            "Content-Range": f"bytes 0-{len(header_data) - 1}/{file_size}",
            "Accept-Ranges": "bytes",
            "X-Moov-Cache": "MOOV-ONLY",
            "X-Moov-Size": str(cache_size),
        },
    )


async def _handle_stream_range(
    request, item_id, file_ino, token, rel_path,
    range_header, header_data, cache_entry,
    mdat_abs_start_in_original, file_size, cache_size, mdat_size
):
    """Handle a Range request on the /stream endpoint."""
    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        return web.Response(status=416, text="Invalid range")

    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)

    if start >= file_size:
        return web.Response(status=416, text="Range not satisfiable")

    total_requested = end - start + 1

    # Entirely within cache
    if start < cache_size and end < cache_size:
        chunk = header_data[start:end + 1]
        print(f"[STREAM-RANGE] {rel_path} -- bytes {start}-{end} from cache ({len(chunk)}B)")
        return web.Response(
            body=chunk,
            status=206,
            headers={
                "Content-Type": "audio/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "X-Moov-Cache": "STREAM-CACHE",
            },
        )

    # Spans cache + mdat
    if start < cache_size:
        print(f"[STREAM-RANGE] {rel_path} -- bytes {start}-{end} spanning cache+mdat")
        resp = web.StreamResponse(
            status=206,
            headers={
                "Content-Type": "audio/mp4",
                "Content-Length": str(total_requested),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "X-Moov-Cache": "STREAM-SPAN",
            },
        )
        await resp.prepare(request)

        cache_chunk = header_data[start:]
        await resp.write(cache_chunk)

        mdat_bytes_needed = total_requested - len(cache_chunk)

        abs_url = f"{ABS_URL}/api/items/{item_id}/file/{file_ino}"
        if token:
            abs_url += f"?token={token}"

        abs_range = f"bytes={mdat_abs_start_in_original}-{mdat_abs_start_in_original + mdat_bytes_needed - 1}"

        try:
            async with ClientSession() as session:
                async with session.get(abs_url, headers={"Range": abs_range}) as abs_resp:
                    async for chunk in abs_resp.content.iter_any():
                        try:
                            await resp.write(chunk)
                        except (ConnectionResetError, ConnectionError, BrokenPipeError):
                            return resp
        except Exception:
            pass

        try:
            await resp.write_eof()
        except Exception:
            pass
        return resp

    # Entirely within mdat
    mdat_offset_in_stream = start - cache_size
    abs_start = mdat_abs_start_in_original + mdat_offset_in_stream
    abs_end_offset = end - cache_size
    abs_end = mdat_abs_start_in_original + abs_end_offset

    print(f"[STREAM-RANGE] {rel_path} -- bytes {start}-{end} from mdat (ABS range {abs_start}-{abs_end})")

    abs_url = f"{ABS_URL}/api/items/{item_id}/file/{file_ino}"
    if token:
        abs_url += f"?token={token}"

    abs_range = f"bytes={abs_start}-{abs_end}"

    async with ClientSession() as session:
        async with session.get(abs_url, headers={"Range": abs_range}) as abs_resp:
            resp = web.StreamResponse(
                status=206,
                headers={
                    "Content-Type": "audio/mp4",
                    "Content-Length": str(total_requested),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "X-Moov-Cache": "STREAM-MDAT",
                },
            )
            await resp.prepare(request)
            try:
                async for chunk in abs_resp.content.iter_any():
                    try:
                        await resp.write(chunk)
                    except (ConnectionResetError, ConnectionError, BrokenPipeError):
                        return resp
            except Exception:
                pass
            try:
                await resp.write_eof()
            except Exception:
                pass
            return resp


# ---------------------------------------------------------------------------
# Request handlers: /api/items/{item_id}/file/{file_ino}
# ---------------------------------------------------------------------------

async def handle_audio(request):
    """
    Main audio file handler. Serves moov atoms from cache for range
    requests that fall within the cached region, proxies everything
    else to ABS.
    """
    item_id = request.match_info["item_id"]
    file_ino = request.match_info["file_ino"]
    token = get_token(request)
    range_header = request.headers.get("Range", "")

    rel_path, cache_file, cache_entry = get_cache_for_request(item_id, file_ino)

    # Try faststart cache first
    if rel_path:
        result = get_faststart_file(rel_path)
        if result and result[0]:
            return await _serve_faststart(request, rel_path, result[0], range_header)

    if cache_file and cache_entry:
        _trigger_faststart_cache(rel_path, item_id, file_ino, token, cache_entry)

        cache_size = cache_entry["cache_size"]
        file_size = cache_entry["file_size"]
        moov_at_start = cache_entry["moov_offset"] < 1000
        moov_size = cache_entry["moov_size"]
        ftyp_size = cache_size - moov_size

        header_data = get_rewritten_moov(rel_path, cache_file, cache_entry)

        if not range_header:
            print(f"[MOOV-DOWNLOAD] {rel_path} -- non-range request, proxying full file from ABS")
            return await _proxy_to_abs(request, item_id, file_ino, token, range_header)

        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1

            # Entirely within cache
            if start < cache_size and end < cache_size:
                chunk = header_data[start:end + 1]
                print(f"[MOOV-HIT] {rel_path} -- range {start}-{end} from cache ({len(chunk)}B)")
                return web.Response(
                    body=chunk,
                    status=206,
                    headers={
                        "Content-Type": "audio/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "X-Moov-Cache": "HIT-RANGE",
                    },
                )

            # mdat region for moov-at-end files
            if not moov_at_start and start >= cache_size:
                mdat_offset_requested = start - cache_size
                abs_start = ftyp_size + mdat_offset_requested
                abs_end = ftyp_size + (end - cache_size)
                total_requested = end - start + 1

                abs_url = f"{ABS_URL}/api/items/{item_id}/file/{file_ino}"
                if token:
                    abs_url += f"?token={token}"

                print(f"[MOOV-MDAT] {rel_path} -- mapping {start}-{end} to ABS {abs_start}-{abs_end}")

                async with ClientSession() as session:
                    async with session.get(abs_url, headers={"Range": f"bytes={abs_start}-{abs_end}"}) as resp:
                        body = web.StreamResponse(
                            status=206,
                            headers={
                                "Content-Type": "audio/mp4",
                                "Content-Length": str(total_requested),
                                "Content-Range": f"bytes {start}-{end}/{file_size}",
                                "Accept-Ranges": "bytes",
                                "X-Moov-Cache": "MDAT-MAPPED",
                            },
                        )
                        await body.prepare(request)
                        try:
                            async for chunk in resp.content.iter_any():
                                try:
                                    await body.write(chunk)
                                except (ConnectionResetError, ConnectionError, BrokenPipeError):
                                    return body
                        except Exception:
                            pass
                        try:
                            await body.write_eof()
                        except Exception:
                            pass
                        return body

    # Cache miss -- proxy to ABS
    abs_url = f"{ABS_URL}/api/items/{item_id}/file/{file_ino}"
    if token:
        abs_url += f"?token={token}"

    headers = {}
    if range_header:
        headers["Range"] = range_header

    miss_reason = "no-cache" if not cache_file else "range-beyond"
    print(f"[MOOV-MISS:{miss_reason}] {rel_path or item_id} -- proxying (Range: {range_header or 'none'})")

    async with ClientSession() as session:
        async with session.get(abs_url, headers=headers) as resp:
            resp_headers = {
                "Content-Type": resp.headers.get("Content-Type", "audio/mp4"),
                "Accept-Ranges": "bytes",
                "X-Moov-Cache": f"MISS-{miss_reason}",
            }
            if "Content-Length" in resp.headers:
                resp_headers["Content-Length"] = resp.headers["Content-Length"]
            if "Content-Range" in resp.headers:
                resp_headers["Content-Range"] = resp.headers["Content-Range"]

            if range_header:
                body = web.StreamResponse(status=resp.status, headers=resp_headers)
                await body.prepare(request)
                async for chunk in resp.content.iter_any():
                    await body.write(chunk)
                await body.write_eof()
                return body
            else:
                data = await resp.read()
                return web.Response(body=data, status=resp.status, headers=resp_headers)


# ---------------------------------------------------------------------------
# Request handlers: /health, /reload
# ---------------------------------------------------------------------------

async def handle_health(request):
    """Health check endpoint."""
    return web.json_response({
        "status": "ok",
        "cached_moovs": len(_index),
        "file_mappings": len(_file_map),
        "rewritten_cache": len(_rewritten_cache),
        "cache_dir": MOOV_CACHE_DIR,
        "faststart_cached": len(_faststart_index),
    })


async def handle_reload(request):
    """Reload all indexes and the file map from disk/DB."""
    load_index()
    load_faststart_index()
    build_file_map()
    return web.json_response({
        "status": "reloaded",
        "cached_moovs": len(_index),
        "file_mappings": len(_file_map),
        "faststart_cached": len(_faststart_index),
    })


# ---------------------------------------------------------------------------
# Moov cache builder (--build-cache mode)
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = (".m4b", ".m4a", ".mp4")


def find_atoms(filepath, max_read=100 * 1024 * 1024):
    """
    Parse top-level MP4 atoms from a file up to max_read bytes.
    Returns a list of (type_str, offset, size) tuples.
    Stops after finding the moov atom.
    """
    atoms = []
    try:
        with open(filepath, "rb") as f:
            pos = 0
            while pos < max_read:
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break
                size = struct.unpack(">I", header[:4])[0]
                atype = header[4:8].decode("ascii", errors="ignore")
                if size < 8:
                    break
                atoms.append((atype, pos, size))
                if atype == "moov":
                    break
                pos += size
    except Exception:
        pass
    return atoms


def extract_moov_cache(filepath, cache_path):
    """
    Extract ftyp + moov atoms from an audio file and write them
    to cache_path. Returns metadata dict or None on failure.
    """
    atoms = find_atoms(filepath)
    ftyp = moov = None
    for atype, offset, size in atoms:
        if atype == "ftyp":
            ftyp = (offset, size)
        elif atype == "moov":
            moov = (offset, size)

    if not ftyp or not moov:
        return None

    try:
        with open(filepath, "rb") as src, open(cache_path, "wb") as dst:
            src.seek(ftyp[0])
            dst.write(src.read(ftyp[1]))
            src.seek(moov[0])
            dst.write(src.read(moov[1]))
        return {
            "ftyp_size": ftyp[1],
            "moov_offset": moov[0],
            "moov_size": moov[1],
        }
    except Exception:
        return None


def build_cache():
    """
    Walk AUDIOBOOKS_DIR, extract ftyp+moov from every audio file,
    write to MOOV_CACHE_DIR with an index.json. Skips files already
    in the index.
    """
    print(f"[BUILD] Walking {AUDIOBOOKS_DIR} ...")
    os.makedirs(MOOV_CACHE_DIR, exist_ok=True)

    # Load existing index
    index_path = os.path.join(MOOV_CACHE_DIR, "index.json")
    index = {}
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                index = json.load(f)
            print(f"[BUILD] Existing index has {len(index)} entries")
        except Exception:
            index = {}

    total_files = 0
    new_cached = 0
    skipped = 0
    errors = 0

    for root, dirs, files in os.walk(AUDIOBOOKS_DIR):
        for fname in files:
            if not fname.lower().endswith(AUDIO_EXTENSIONS):
                continue

            total_files += 1
            filepath = os.path.join(root, fname)
            rel_path = os.path.relpath(filepath, AUDIOBOOKS_DIR)

            # Skip already-indexed files
            if rel_path in index:
                skipped += 1
                continue

            cache_key = hashlib.md5(rel_path.encode()).hexdigest()[:16]
            cache_path = os.path.join(MOOV_CACHE_DIR, f"{cache_key}.mp4")

            # Skip if cache file already exists (orphaned from index)
            if os.path.exists(cache_path):
                skipped += 1
                continue

            try:
                file_size = os.path.getsize(filepath)
                mtime = os.path.getmtime(filepath)
            except OSError:
                errors += 1
                continue

            result = extract_moov_cache(filepath, cache_path)
            if result:
                index[rel_path] = {
                    "cache_key": cache_key,
                    "mtime": mtime,
                    "moov_size": result["moov_size"],
                    "moov_offset": result["moov_offset"],
                    "file_size": file_size,
                    "cache_size": os.path.getsize(cache_path),
                }
                new_cached += 1
                if new_cached % 50 == 0:
                    print(f"[BUILD] ... {new_cached} new files cached so far")
            else:
                errors += 1

    # Save index
    with open(index_path, "w") as f:
        json.dump(index, f)

    total_cache_size = sum(e.get("cache_size", 0) for e in index.values())

    print(f"[BUILD] Done.")
    print(f"[BUILD]   Total audio files found: {total_files}")
    print(f"[BUILD]   Already cached (skipped): {skipped}")
    print(f"[BUILD]   Newly cached:             {new_cached}")
    print(f"[BUILD]   Errors:                   {errors}")
    print(f"[BUILD]   Total index entries:       {len(index)}")
    print(f"[BUILD]   Total cache size:          {total_cache_size / (1024 * 1024 * 1024):.2f} GB")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_proxy():
    """Start the moov cache proxy server."""
    load_index()
    load_faststart_index()
    build_file_map()

    app = web.Application()
    app.router.add_get("/api/items/{item_id}/file/{file_ino}", handle_audio)
    app.router.add_get("/api/items/{item_id}/file/{file_ino}/stream", handle_stream)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/reload", handle_reload)

    print(f"[MOOV] Proxy starting on port {PROXY_PORT}...")
    web.run_app(app, host="0.0.0.0", port=PROXY_PORT)


def main():
    parser = argparse.ArgumentParser(
        description="abs-turbo Moov Cache Proxy for AudiobookShelf"
    )
    parser.add_argument(
        "--build-cache",
        action="store_true",
        help="Build/update the moov cache from AUDIOBOOKS_DIR and exit (don't start proxy)",
    )
    args = parser.parse_args()

    print_banner()

    if args.build_cache:
        build_cache()
    else:
        run_proxy()


if __name__ == "__main__":
    main()
