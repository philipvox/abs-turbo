# abs-turbo

A performance and operations toolkit for [AudiobookShelf](https://github.com/advplyr/audiobookshelf). Solves three major pain points: slow playback start, slow library scans, and lack of monitoring.

## The Problem

AudiobookShelf is great, but self-hosters with large libraries hit some walls:

- **Slow playback start**: M4B files often have the "moov atom" (metadata index) at the end. Players must download the entire file before playback begins — sometimes 500MB+ per book.
- **Slow library scans**: The only way to add books is a full library scan. With network storage (NAS, SSHFS, NFS), this can take hours for large libraries.
- **No monitoring**: ABS has no built-in health alerts. You find out it's down when someone complains.

## The Solution

abs-turbo is a sidecar toolkit that runs alongside your existing ABS installation:

| Module | What it does |
|--------|-------------|
| **abs-proxy** | Caches moov atoms locally for instant playback start. Two-tier cache: moov (all books) + faststart (recently played). |
| **abs-add** | Adds books directly to the ABS database without scanning. Seconds instead of hours. |
| **abs-monitor** | Discord alerts for ABS health, storage, Docker, and server resources. Includes a daily digest with visual graphs. |
| **abs-inode-sync** | Fixes inode mismatches after SSHFS/NFS remounts (prevents 404 errors). |
| **abs-backup** | Automated daily database backups with WAL checkpointing and retention. |
| **abs-export-metadata** | Exports curated metadata to `metadata.abs` sidecar files, protecting it during scans. |
| **image-cache** | Pre-generates WebP covers served as static files — faster than ABS's on-the-fly processing. |

## Quick Start

### Option A: Docker Compose (recommended)

```bash
# Clone the repo
git clone https://github.com/youruser/abs-turbo.git
cd abs-turbo

# Copy and edit config
cp .env.example .env
# Edit .env with your paths and settings

# Build the moov cache for your library (one-time, takes a few minutes)
docker compose run --rm abs-proxy python3 moov_proxy.py --build-cache

# Start the proxy
docker compose up -d
```

Then update your reverse proxy config (see [Caddy](#caddy) or [nginx](#nginx) templates).

### Option B: Bare metal

```bash
# Install dependencies
pip install aiohttp

# Copy and edit config
cp .env.example .env
source .env

# Build the moov cache
python3 proxy/moov_proxy.py --build-cache

# Start the proxy
python3 proxy/moov_proxy.py
# Or use the provided systemd service file
```

## Modules

### abs-proxy (Moov Cache Proxy)

The centerpiece. Sits between your reverse proxy and ABS, serving cached moov atoms for instant playback.

**How it works:**

```
Player request → Caddy/nginx → abs-proxy (:13379)
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
              Faststart cache  Moov cache      ABS fallback
              (full file,      (ftyp+moov,    (proxied
               recent books)    all books)     transparently)
```

1. **Faststart cache** (LRU, 30-day TTL): Complete faststart-processed files for recently played books. Zero-latency playback — the file is served entirely from local disk.
2. **Moov cache** (permanent): Just the ftyp+moov atoms (~1-5MB per book vs 50-500MB full file). The proxy serves the moov from cache and streams the audio data from ABS. Playback starts instantly.
3. **Fallback**: If neither cache has the file, the request is proxied transparently to ABS.

**Initial cache build:**

```bash
# Build moov cache for all books (walks your audiobook directory)
python3 proxy/moov_proxy.py --build-cache

# The faststart cache auto-populates as books are played
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_URL` | `http://localhost:8000` | ABS server URL |
| `ABS_DB` | `/config/absdatabase.sqlite` | Path to ABS database |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Path to audiobook files |
| `MOOV_CACHE_DIR` | `/cache/moov` | Moov atom cache directory |
| `FASTSTART_CACHE_DIR` | `/cache/faststart` | Faststart file cache directory |
| `PROXY_PORT` | `13379` | Port for the proxy |
| `FASTSTART_MAX_GB` | `30` | Max size of faststart cache in GB |
| `FASTSTART_MAX_DAYS` | `30` | Max age of faststart cache entries |

**Endpoints:**

| Endpoint | Description |
|----------|-------------|
| `GET /api/items/{id}/file/{ino}` | Audio file (with moov cache) |
| `GET /api/items/{id}/file/{ino}/stream` | Audio file (MSE browser playback) |
| `GET /health` | Health check + cache stats |
| `POST /reload` | Reload cache indexes and file map |

### abs-add (No-Scan Book Addition)

Add books to ABS by writing directly to the SQLite database. No library scan needed.

```bash
# Add a specific book
python3 tools/abs-add.py /path/to/Author/Book

# Auto-detect all new books
python3 tools/abs-add.py --scan

# Dry run (show what would be added)
python3 tools/abs-add.py --scan --dry-run

# Target a specific library (if you have multiple)
python3 tools/abs-add.py --library "Main Library" /path/to/Book
```

**Requirements:** `ffprobe` must be installed (comes with ffmpeg).

**How it works:**
1. Probes each audio file with `ffprobe` for duration, codec, bitrate, chapters
2. Reads `metadata.abs` sidecar if present (title, author, series, genres, tags)
3. Builds chapter list from embedded chapters or file boundaries
4. Inserts into `libraryItems`, `books`, `bookAuthors`, `bookSeries` tables
5. Restarts the ABS container so it loads the new entries

### abs-monitor (Discord Health Alerts)

Monitoring scripts that send Discord alerts on state changes. Rate-limited to avoid spam.

```bash
# Install monitoring
cd monitoring
cp config.env.example config.env
# Edit config.env with your Discord webhook URLs and paths
bash install-cron.sh
```

**Monitors:**
- ABS container health + HTTP responsiveness
- Storage mount status, space, and latency
- Server CPU, RAM, and load average
- Docker container states
- Cache volume health and proxy status
- Daily digest with visual bar graphs

**Daily digest example:**

```
📊 Daily Health Digest
🟢 All Systems Operational

CPU  🟢 ▰▰▱▱▱▱▱▱▱▱ 15%
RAM  🟢 ▰▰▰▰▱▱▱▱▱▱ 42%

💾 Local Disk  🟢 ▰▰▰▰▰▱▱▱▱▱ 51%
📦 Audiobook Storage  🟢 ▰▰▰▱▱▱▱▱▱▱ 34%

🐳 Containers
  ✅ audiobookshelf — Up 14 days
```

### abs-inode-sync (SSHFS/NFS Inode Fixer)

If you mount audiobooks via SSHFS or certain NFS configurations, inode numbers change on every remount. ABS stores inodes in three places — if any are stale, file requests return 404.

```bash
# Smart mode (skips if mount hasn't changed)
python3 tools/abs-inode-sync.py

# Force full sync
python3 tools/abs-inode-sync.py --force

# Dry run
python3 tools/abs-inode-sync.py --force --dry-run
```

### abs-backup

Automated database backup with WAL checkpointing.

```bash
# Run manually
bash tools/abs-backup.sh

# Or set up via monitoring cron (includes Discord notifications)
```

### abs-export-metadata

Exports curated metadata to `metadata.abs` sidecar files, protecting it from being overwritten during library scans.

```bash
python3 tools/abs-export-metadata.py
python3 tools/abs-export-metadata.py --dry-run
```

### image-cache (Cover Optimization)

Pre-generates WebP covers served as static files by your reverse proxy.

```bash
# Build cache for all books
python3 image-cache/build-cache.py

# Only process new/changed covers
python3 image-cache/build-cache.py --incremental
```

**Requires:** `cwebp` (install via `apt install webp` or `brew install webp`)

## Reverse Proxy Configuration

### Caddy

Copy `caddy/Caddyfile.template` and customize:

```bash
cp caddy/Caddyfile.template /etc/caddy/Caddyfile
# Edit with your domain, ports, and paths
caddy reload
```

### nginx

Copy `nginx/abs-turbo.conf.template` and customize:

```bash
cp nginx/abs-turbo.conf.template /etc/nginx/conf.d/abs-turbo.conf
# Edit with your domain, ports, and paths
nginx -t && nginx -s reload
```

## Configuration

All tools read from environment variables. Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
```

Key settings:

| Variable | Description |
|----------|-------------|
| `ABS_URL` | Your ABS server URL |
| `ABS_DB` | Path to ABS SQLite database |
| `AUDIOBOOKS_DIR` | Path to your audiobook files |
| `ABS_CONTAINER` | Docker container name (default: `audiobookshelf`) |

See `.env.example` for the full list with descriptions.

## Requirements

- Python 3.9+
- `aiohttp` (for the proxy)
- `ffprobe` (for abs-add — comes with ffmpeg)
- `cwebp` (for image-cache — optional)
- `jq` + `curl` (for monitoring scripts)
- Docker (if using Docker Compose deployment)

## Compatibility

Tested with AudiobookShelf versions 2.7.0 through 2.17.x. The tools that write to the database (abs-add, abs-inode-sync) depend on ABS's SQLite schema, which can change between versions. abs-add auto-detects the schema and adapts its queries.

## Warning

**abs-add and abs-inode-sync write directly to the ABS database.** Always back up your database before using them. The abs-backup tool is provided for this purpose.

ABS must be restarted after database modifications so it loads the changes into memory. The tools handle this automatically (configurable with `--no-restart`).

## License

MIT
