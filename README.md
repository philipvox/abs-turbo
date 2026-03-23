# abs-turbo

A performance and operations toolkit for [AudiobookShelf](https://github.com/advplyr/audiobookshelf) power users. Caches audio metadata for instant playback, adds books without scanning, monitors everything via Discord, and keeps network-mounted libraries stable.

---

## Is this for you?

**abs-turbo solves specific problems that most ABS users don't have.** If any of these describe your setup, keep reading:

- Your audiobook library is on network storage (NAS, SSHFS, NFS, SMB) and playback takes forever to start
- You have hundreds or thousands of books and adding new ones triggers a multi-hour library scan
- Your library is mounted via SSHFS and books randomly return 404 after reboots
- You want health monitoring and Discord alerts for your ABS server
- You serve ABS over the internet and want faster cover image loading

**If your setup is simple** (ABS on the same machine as your files, small library, local access only), you probably don't need this. ABS works great out of the box for that use case.

---

## What's inside

| Module | What it does | You need it if... |
|--------|-------------|-------------------|
| [**abs-proxy**](#abs-proxy) | Two-tier audio cache proxy. Caches moov atoms for instant playback start. | Playback is slow over network storage |
| [**abs-add**](#abs-add) | Adds books directly to the database. No scan needed. | Full scans take too long |
| [**abs-inode-sync**](#abs-inode-sync) | Fixes inode mismatches after SSHFS/NFS remounts. | Files return 404 after reboots |
| [**abs-backup**](#abs-backup) | Automated database backups with integrity checks. | You want peace of mind |
| [**abs-export-metadata**](#abs-export-metadata) | Exports curated metadata to sidecar files. | You've edited metadata in ABS and want to protect it |
| [**image-cache**](#image-cache) | Pre-generates WebP covers served as static files. | Cover images load slowly |
| [**monitoring**](#monitoring) | Discord alerts for health, storage, Docker, and resources. | You want to know when things break |

Every module is independent. Use one, use all, or mix and match.

---

## Architecture

abs-turbo sits between your reverse proxy and ABS as a sidecar. It doesn't modify ABS itself.

```
  Client (phone app, browser)
         |
         v
  +----- Reverse Proxy (Caddy/nginx) -----+
  |                                        |
  |  /api/items/*/cover  --> Image Cache   |  Static WebP files on disk
  |  /api/items/*/file/* --> abs-proxy     |  Moov cache + faststart cache
  |  (everything else)   --> ABS           |  API, web UI, login, websocket
  |                                        |
  +----------------------------------------+
         |                    |
         v                    v
    abs-proxy (:13379)     ABS (:8000)
         |                    |
         v                    v
    Local cache         Audio files (local or network-mounted)
```

### How the audio proxy works

M4B/M4A files have a **moov atom** — an index that maps timestamps to byte positions. If it's at the end of the file (the default for most encoders), the player must download the entire file before playback can start. For a 500MB audiobook over a network mount, that means waiting minutes.

abs-proxy fixes this with two cache tiers:

```
  Audio request arrives
         |
         v
  1. Faststart cache hit? -----> Serve entire file from local SSD
     (recently played books,     (zero network latency)
      LRU with 30-day TTL)
         |
         no
         v
  2. Moov cache hit? ----------> Serve [ftyp+moov] from cache,
     (all books, permanent,      stream [mdat] from ABS
      ~1-5MB per book)           (playback starts instantly)
         |
         no
         v
  3. Proxy to ABS transparently
     (cache miss — extracts moov
      for next time)
```

The moov cache stores only the ftyp and moov atoms (~1-5MB per book vs 50-500MB for the full file). When serving a moov-at-end file, the proxy rewrites `stco`/`co64` chunk offset tables so the player sees a valid `[ftyp][moov][mdat]` stream. The audio data (`mdat`) streams from ABS in real-time — no full download needed.

---

## Quick start

### Prerequisites

- Python 3.9+
- A running AudiobookShelf instance
- Access to the ABS SQLite database file (`absdatabase.sqlite`)
- Access to the audiobook files on disk

### Option A: Docker Compose (proxy only)

```bash
git clone https://github.com/philipvox/abs-turbo.git
cd abs-turbo

# Configure
cp .env.example .env
nano .env  # Set ABS_URL, ABS_CONFIG_DIR, AUDIOBOOKS_DIR at minimum

# Build the moov cache (one-time, reads every audio file's first few MB)
docker compose run --rm abs-proxy python3 moov_proxy.py --build-cache

# Start the proxy
docker compose up -d
```

### Option B: Bare metal

```bash
git clone https://github.com/philipvox/abs-turbo.git
cd abs-turbo

cp .env.example .env
nano .env

# Install the proxy dependency
pip install aiohttp

# Build the moov cache
python3 proxy/moov_proxy.py --build-cache

# Run the proxy
python3 proxy/moov_proxy.py
```

Then configure your reverse proxy to route audio requests through abs-proxy (see [Reverse Proxy Setup](#reverse-proxy-setup)).

---

## Module reference

### abs-proxy

**Moov cache proxy for instant playback start.**

The proxy is an aiohttp server that intercepts audio file requests (`/api/items/{id}/file/{ino}`), serves cached moov atoms, and streams the rest from ABS. It also maintains an optional faststart cache of fully processed files for recently played books.

#### Usage

```bash
# Proxy mode (default) — runs the caching reverse proxy
python3 proxy/moov_proxy.py

# Build mode — walks audiobook directory, extracts moov atoms, exits
python3 proxy/moov_proxy.py --build-cache

# Docker
docker compose up -d abs-proxy
docker compose run --rm abs-proxy python3 moov_proxy.py --build-cache
```

#### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/items/{id}/file/{ino}` | GET | Audio file with moov cache acceleration |
| `/api/items/{id}/file/{ino}/stream` | GET | Audio stream (MSE browser playback) |
| `/health` | GET | Health check with cache statistics (JSON) |
| `/reload` | POST | Reload cache indexes and file map from DB |

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_URL` | `http://localhost:8000` | URL where ABS is reachable |
| `ABS_DB` | `/config/absdatabase.sqlite` | Path to ABS SQLite database |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Root path to audiobook files |
| `MOOV_CACHE_DIR` | `/cache/moov` | Directory for moov atom cache |
| `FASTSTART_CACHE_DIR` | `/cache/faststart` | Directory for faststart file cache |
| `PROXY_PORT` | `13379` | Port the proxy listens on |
| `FASTSTART_MAX_GB` | `30` | Maximum size of faststart cache (GB) |
| `FASTSTART_MAX_DAYS` | `30` | Maximum age of faststart entries (days) |

#### Cache sizing

- **Moov cache**: ~10-15 bytes per second of audio across your entire library. A 2,000-book library with an average of 10 hours per book is roughly 700MB-1GB. This cache is permanent and never cleaned — it's small enough to keep forever.
- **Faststart cache**: Stores full copies of recently played files. Size depends on how many books are actively being listened to. 30GB covers roughly 30-60 recent books. The LRU eviction policy automatically removes the oldest entries when the size or age limit is reached.

#### How the cache build works

`--build-cache` walks `AUDIOBOOKS_DIR`, finds every `.m4b`, `.m4a`, and `.mp4` file, reads the first few megabytes to locate the moov atom, and writes it to `MOOV_CACHE_DIR`. For files that already have moov-at-front (faststart), only the ftyp+moov portion is cached. For moov-at-end files, the entire moov atom is extracted. This is a read-only operation on your audio files.

The cache builder also creates an index file mapping ABS item IDs and file inodes to filesystem paths, using data from `ABS_DB`. This index is loaded at proxy startup so it can translate API requests to file lookups.

---

### abs-add

**Add audiobooks to ABS without triggering a library scan.**

ABS only discovers new books through a full library scan. With network storage and large libraries, a scan can take hours because ABS stats every file. abs-add writes directly to the SQLite database, creating all necessary records in seconds.

#### Usage

```bash
# Add specific book folders
python3 tools/abs-add.py /audiobooks/Author/Book
python3 tools/abs-add.py /audiobooks/Author/Series/Book1 /audiobooks/Author/Series/Book2

# Auto-detect all new books (walks AUDIOBOOKS_DIR, finds folders not in DB)
python3 tools/abs-add.py --scan

# Dry run
python3 tools/abs-add.py --scan --dry-run

# Target a specific library
python3 tools/abs-add.py --library "My Audiobooks" /audiobooks/Author/Book

# Don't restart ABS after adding (useful for batch operations)
python3 tools/abs-add.py --no-restart --scan
```

#### Flags

| Flag | Description |
|------|-------------|
| `PATH [PATH...]` | One or more book folder paths to add |
| `--scan` | Auto-detect new books by comparing filesystem to database |
| `--dry-run` | Show what would be added without writing anything |
| `--library NAME` | Target a specific library by name (auto-detected if only one) |
| `--no-restart` | Skip restarting the ABS container after adding |

#### What it does

1. Probes each audio file with `ffprobe` for duration, codec, bitrate, channels, and embedded chapters
2. Reads `metadata.abs` sidecar file if present (title, author, narrator, series, genres, tags, description)
3. Falls back to folder name parsing for author/title if no sidecar exists
4. Generates chapter list from embedded chapters or file boundaries
5. Inserts records into `libraryItems`, `books`, `bookAuthors`, `bookSeries` tables
6. Uses `PRAGMA table_info()` to detect available columns — adapts to ABS schema changes across versions
7. Restarts the ABS container so it loads the new database entries

#### Requirements

- `ffprobe` (comes with ffmpeg)
- Write access to `ABS_DB`

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_DB` | `/config/absdatabase.sqlite` | Path to ABS database |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Root audiobook directory |
| `ABS_CONTAINER` | `audiobookshelf` | Docker container name for restart |
| `ABS_URL` | `http://localhost:8000` | ABS API URL (for verification) |

---

### abs-inode-sync

**Fix stale inodes after SSHFS or NFS remounts.**

SSHFS generates synthetic inode numbers that change every time the mount is re-established. ABS stores inodes in three places in its database:

1. `books.audioFiles[].ino` — audio file metadata
2. `libraryItems.libraryFiles[].ino` — used by the `/api/items/{id}/file/{ino}` endpoint (the critical one — stale inodes here cause 404s)
3. `libraryItems.ino` — folder inode

If any of these are stale, ABS returns 404 for file requests. This tool scans the filesystem for current inodes and updates the database.

#### Usage

```bash
# Smart mode (default) — uses a sentinel file to detect remounts, skips if unchanged
python3 tools/abs-inode-sync.py

# Force full sync regardless of sentinel
python3 tools/abs-inode-sync.py --force

# Show what would change without writing
python3 tools/abs-inode-sync.py --force --dry-run

# Don't restart ABS after fixing
python3 tools/abs-inode-sync.py --no-restart
```

#### Flags

| Flag | Description |
|------|-------------|
| `--force` | Skip sentinel check, scan everything |
| `--dry-run` | Show mismatches without fixing them |
| `--no-restart` | Don't restart ABS container after sync |

#### How the sentinel works

On each run, the tool checks the inode of a sentinel file in `SENTINEL_DIR`. If the inode matches the last recorded value, the mount hasn't changed and the sync is skipped. This makes it safe to run frequently via cron — it only does real work when needed.

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_DB` | `/config/absdatabase.sqlite` | Path to ABS database |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Audiobook mount point |
| `ABS_CONTAINER` | `audiobookshelf` | Docker container name for restart |
| `SENTINEL_DIR` | Same dir as `ABS_DB` | Where to store the sentinel file |

---

### abs-backup

**Database backup with WAL checkpoint, integrity check, and retention cleanup.**

```bash
# Run backup
bash tools/abs-backup.sh

# Skip WAL checkpoint (if sqlite3 is not available or ABS is not running)
bash tools/abs-backup.sh --no-wal
```

#### What it does

1. **WAL checkpoint**: Flushes the SQLite write-ahead log to the main database file, ensuring the backup is complete
2. **Safe copy**: Uses `sqlite3 .backup` for a consistent copy (falls back to `cp` if sqlite3 is unavailable)
3. **Integrity check**: Runs `PRAGMA integrity_check` on the backup and reports book/item counts
4. **Retention cleanup**: Removes backups older than `BACKUP_RETENTION` days

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_DB` | `/config/absdatabase.sqlite` | Source database path |
| `ABS_BACKUP_DIR` | `/backups` | Backup destination directory |
| `ABS_CONTAINER` | `audiobookshelf` | Docker container name |
| `BACKUP_RETENTION` | `30` | Days to keep old backups |

---

### abs-export-metadata

**Export curated metadata to `metadata.abs` sidecar files.**

ABS reads `metadata.abs` files during library scans. If you've spent time curating your metadata (genres, tags, descriptions, series assignments) in the ABS UI, these sidecar files protect it from being overwritten by Audible or other matched metadata sources.

```bash
# Export all books
python3 tools/abs-export-metadata.py

# Preview what would be written
python3 tools/abs-export-metadata.py --dry-run

# See each file as it's written
python3 tools/abs-export-metadata.py --verbose
```

#### Flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Show what would be written without creating files |
| `--verbose`, `-v` | Print each file path as it's written |

#### Output format

Each book gets a `metadata.abs` file in its folder:

```
;DIFFUSE metadata
title=The Great Gatsby
authors=F. Scott Fitzgerald
narrators=Jake Gyllenhaal
series=
genres=Fiction, Classic
tags=American Literature, Jazz Age
description=A story of the mysteriously wealthy Jay Gatsby...
publishedYear=1925
language=English
```

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_DB` | `/config/absdatabase.sqlite` | Path to ABS database |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Root audiobook directory |

---

### image-cache

**Pre-generate WebP cover images for fast static serving.**

Instead of ABS dynamically resizing cover images on each request, this tool pre-generates optimized WebP versions that your reverse proxy serves as static files. ABS is never hit for cover requests.

```bash
# Incremental build (only new/changed covers)
python3 image-cache/build-cache.py

# Full rebuild
python3 image-cache/build-cache.py --force

# Preview
python3 image-cache/build-cache.py --dry-run

# Verbose output
python3 image-cache/build-cache.py --verbose

# Docker (one-shot)
docker compose run --rm image-cache-builder
docker compose run --rm image-cache-builder --force
```

#### Output structure

```
{IMAGE_CACHE_DIR}/
  {item-uuid}/
    cover.webp         # 800px wide, quality 80
    cover-thumb.webp   # 200px wide, quality 30
```

Each book's UUID (from the ABS database) maps to a folder containing the full-size cover and a thumbnail. The reverse proxy rewrites `/api/items/{uuid}/cover` requests to serve these static files.

#### Requirements

- `cwebp` (from the `webp` package)
  - Ubuntu/Debian: `apt install webp`
  - macOS: `brew install webp`
  - Alpine: `apk add libwebp-tools`

#### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_DB` | `/config/absdatabase.sqlite` | Path to ABS database |
| `ABS_METADATA` | `/config/metadata` | ABS metadata directory (where covers are stored) |
| `IMAGE_CACHE_DIR` | `/var/cache/image-cache` | Output directory for WebP files |

---

### Monitoring

**Discord webhook alerts for ABS health, storage, Docker containers, server resources, and cache utilization.**

The monitoring system is a set of bash scripts designed to run via cron. Each script checks one aspect of your server and sends a Discord embed when something changes state (OK -> warning, warning -> critical, etc.). A daily digest provides an at-a-glance summary with visual bar graphs.

#### Setup

```bash
cd monitoring

# Configure
cp config.env.example config.env
nano config.env  # Fill in Discord webhook URLs and paths

# Install cron entries (interactive — shows what will be added)
bash install-cron.sh
```

The installer asks what time to run daily tasks and shows exactly which cron entries will be added before making any changes.

#### Creating Discord webhooks

1. Open your Discord server
2. Go to **Server Settings > Integrations > Webhooks**
3. Click **New Webhook** for each channel you want alerts in
4. Copy the webhook URL into `config.env`

You can use the same webhook URL for multiple alert types, or route them to different channels:

```bash
# All alerts to one channel
WEBHOOK_ABS="https://discord.com/api/webhooks/..."
WEBHOOK_SERVER="https://discord.com/api/webhooks/..."  # same URL
WEBHOOK_STORAGE="https://discord.com/api/webhooks/..."  # same URL

# Or separate channels for different alert types
WEBHOOK_ABS="https://discord.com/api/webhooks/.../abs-alerts"
WEBHOOK_SERVER="https://discord.com/api/webhooks/.../server-health"
WEBHOOK_CRITICAL="https://discord.com/api/webhooks/.../pages"  # high-priority
```

#### Monitor scripts

| Script | Runs | Checks |
|--------|------|--------|
| `abs-monitor.sh` | Every 30 min | ABS Docker container running + HTTP health endpoint responds |
| `storage-check.sh` | Every 30 min | Storage mount status, free disk space, latency |
| `server-health.sh` | Every 30 min | CPU usage, RAM usage, load average vs thresholds |
| `docker-check.sh` | Every 30 min | All Docker container states — alerts on stopped/restarting/unhealthy |
| `cache-monitor.sh` | Every 30 min | Cache volume utilization + moov proxy health endpoint |
| `abs-backup.sh` | Daily | Database backup with Discord success/failure notification |
| `daily-digest.sh` | Daily | Rich embed summary of all metrics with bar graphs |

#### Daily digest example

The daily digest sends a rich Discord embed that looks like this:

```
+------------------------------------------+
| Daily Health Digest                      |
|                                          |
| All Systems Operational                  |
|                                          |
| CPU     [||||......] 15%                 |
| RAM     [||||||....] 42%                 |
|                                          |
| Local Disk         [||||||....] 51%      |
| Audiobook Storage  [||||......] 34%      |
|                                          |
| Containers                               |
|   audiobookshelf — Up 14 days            |
|   abs-turbo-proxy — Up 14 days           |
|                                          |
| ABS API: healthy (287ms)                 |
|                                          |
| Footer: My ABS Server · Today at 4:30   |
+------------------------------------------+
```

#### Alert thresholds

Configure in `config.env`:

| Metric | Warning | Critical | Unit |
|--------|---------|----------|------|
| CPU | 85 | 95 | % used |
| RAM | 75 | 90 | % used |
| Storage | 20 | 10 | % free (alerts when free drops below) |
| Load | 5.0 | 8.0 | 1-min average |
| Cache | 90 | 95 | % used |

#### Rate limiting

Alerts include a cooldown period (`ALERT_COOLDOWN`, default 900 seconds / 15 minutes) to prevent spam. If the same issue is detected within the cooldown window, the alert is suppressed and logged instead. The daily digest always sends regardless of cooldown.

#### Dependencies

- `bash`, `curl`, `jq`
- `docker` (for container monitoring)
- `sqlite3` (for backup integrity checks)

---

## Reverse proxy setup

The reverse proxy is what ties everything together. It routes requests to the right backend:

| Request pattern | Destination | Why |
|----------------|-------------|-----|
| `/api/items/*/cover*` | Image cache (static files) | Pre-built WebP, no ABS overhead |
| `/api/items/*/file/*` | abs-proxy (:13379) | Moov cache for instant playback |
| Everything else | ABS (:8000) | API, web UI, login, websocket |

### Caddy

Copy the template and fill in your values:

```bash
cp caddy/Caddyfile.template /etc/caddy/Caddyfile
```

Replace the placeholders:
- `{$DOMAIN}` — your domain (e.g., `audiobooks.example.com`) or `localhost:8080`
- `{$ABS_PORT}` — ABS port (default: `8000`)
- `{$PROXY_PORT}` — abs-proxy port (default: `13379`)
- `{$IMAGE_CACHE_DIR}` — image cache path (default: `/var/cache/image-cache`)

Or use Caddy's native environment variable support:

```bash
DOMAIN=audiobooks.example.com \
ABS_PORT=8000 \
PROXY_PORT=13379 \
IMAGE_CACHE_DIR=/var/cache/image-cache \
caddy run --config /etc/caddy/Caddyfile
```

### nginx

```bash
cp nginx/abs-turbo.conf.template /etc/nginx/sites-available/abs-turbo.conf
ln -s /etc/nginx/sites-available/abs-turbo.conf /etc/nginx/sites-enabled/
```

Replace `YOUR_DOMAIN`, `ABS_PORT`, `PROXY_PORT`, and SSL certificate paths. Then:

```bash
nginx -t && systemctl reload nginx
```

The nginx template includes:
- HTTP to HTTPS redirect
- SSL configuration (replace with your cert paths or use certbot)
- 2GB upload limit for uploading books via ABS UI
- WebSocket support for real-time sync
- Fallback to ABS when image cache misses

---

## Configuration reference

All tools read from environment variables. The `.env.example` file documents every variable with defaults.

```bash
cp .env.example .env
```

### Core variables (used by most tools)

| Variable | Default | Used by |
|----------|---------|---------|
| `ABS_URL` | `http://localhost:8000` | proxy, abs-add, monitoring |
| `ABS_DB` | `/config/absdatabase.sqlite` | all tools |
| `AUDIOBOOKS_DIR` | `/audiobooks` | proxy, abs-add, inode-sync, export-metadata |
| `ABS_CONTAINER` | `audiobookshelf` | abs-add, inode-sync, backup, monitoring |

### Config file search order

The Python tools look for `.env` files in this order (first found wins):
1. `.env` in the current working directory
2. `/etc/abs-turbo/.env`
3. `~/.abs-turbo.env`

Environment variables set before running a tool always take precedence over `.env` file values.

---

## Docker Compose deployment

The included `docker-compose.yml` runs the moov proxy as a sidecar:

```yaml
services:
  abs-proxy:
    build: ./proxy
    ports:
      - "${PROXY_PORT:-13379}:${PROXY_PORT:-13379}"
    volumes:
      - ${ABS_CONFIG_DIR:-./abs-config}:/config:ro
      - ${AUDIOBOOKS_DIR:-/mnt/audiobooks}:/audiobooks:ro
      - moov-cache:/cache/moov
      - faststart-cache:/cache/faststart
```

The proxy needs **read-only** access to:
- The ABS config directory (for `absdatabase.sqlite`)
- The audiobook files (for moov atom extraction and streaming)

Cache volumes are read-write and persist across container restarts.

### Building the image cache via Docker

The image cache builder is defined with a `tools` profile so it only runs when explicitly requested:

```bash
# Build/update the cover cache
docker compose run --rm image-cache-builder

# Force rebuild all covers
docker compose run --rm image-cache-builder --force
```

### Connecting to your existing ABS container

If ABS runs in a **separate** Docker Compose file:

```bash
# Option 1: Use host networking (default)
# Set ABS_URL=http://host.docker.internal:8000 in .env
# Works if ABS publishes its port on the host

# Option 2: Shared Docker network
docker network create abs-net
# Add 'networks: [abs-net]' to both compose files
# Set ABS_URL=http://audiobookshelf:80
```

If ABS runs in the **same** compose file, add it directly and use the service name:

```bash
ABS_URL=http://audiobookshelf:80
```

---

## Bare metal deployment

For running directly on the host without Docker:

```bash
# 1. Install dependencies
pip install aiohttp
apt install ffmpeg webp jq curl  # or brew install on macOS

# 2. Configure
cp .env.example /etc/abs-turbo/.env
nano /etc/abs-turbo/.env

# 3. Build the moov cache
source /etc/abs-turbo/.env
python3 proxy/moov_proxy.py --build-cache

# 4. Run the proxy (consider using systemd or screen/tmux)
python3 proxy/moov_proxy.py &

# 5. Set up monitoring (optional)
cd monitoring
cp config.env.example config.env
nano config.env
bash install-cron.sh

# 6. Build image cache (optional)
python3 image-cache/build-cache.py
```

### systemd service (example)

```ini
[Unit]
Description=abs-turbo moov cache proxy
After=network.target docker.service

[Service]
Type=simple
EnvironmentFile=/etc/abs-turbo/.env
ExecStart=/usr/bin/python3 /opt/abs-turbo/proxy/moov_proxy.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Typical workflows

### Adding a new book

```bash
# 1. Copy the book to your library
cp -r "Author/Book Title/" /mnt/audiobooks/Author/

# 2. Add to ABS database (no scan needed)
python3 tools/abs-add.py /mnt/audiobooks/Author/Book\ Title

# 3. Update the moov cache for the new book
python3 proxy/moov_proxy.py --build-cache

# 4. Update cover cache (optional)
python3 image-cache/build-cache.py
```

### After an SSHFS remount

```bash
# Fix inodes (smart mode — only runs if mount changed)
python3 tools/abs-inode-sync.py

# Or force a full sync
python3 tools/abs-inode-sync.py --force
```

### Protecting curated metadata before a scan

```bash
# Export metadata.abs files for all books
python3 tools/abs-export-metadata.py

# Now safe to run a library scan — ABS reads the sidecar files
```

### Setting up automated daily operations via cron

```bash
# Monitoring handles its own cron via install-cron.sh
cd monitoring && bash install-cron.sh

# For inode sync and moov cache updates, add to crontab manually:
# Daily at 4:00 AM — backup the database
0 4 * * * /opt/abs-turbo/tools/abs-backup.sh

# Daily at 4:30 AM — fix inodes if mount changed
30 4 * * * python3 /opt/abs-turbo/tools/abs-inode-sync.py

# Weekly — rebuild moov cache (catches any new books)
0 5 * * 0 python3 /opt/abs-turbo/proxy/moov_proxy.py --build-cache
```

---

## Compatibility

Tested with AudiobookShelf **2.7.0 through 2.17.x**.

The tools that write to the database (`abs-add`, `abs-inode-sync`) use `PRAGMA table_info()` to detect available columns at runtime and build queries dynamically. This means they adapt automatically to ABS schema changes across versions — no code changes needed when ABS adds or removes columns.

---

## Important warnings

**abs-add and abs-inode-sync write directly to the ABS database.** Always back up your database before using them. `abs-backup.sh` is included for this purpose.

ABS must be restarted after direct database modifications so it reloads the data into memory. The tools handle this automatically by restarting the Docker container. Use `--no-restart` to disable this if you're doing batch operations and want to restart once at the end.

**The moov proxy does not modify your audio files.** All caching is to separate directories. Your original files are only ever read, never written.

---

## Requirements summary

| Dependency | Required by | Install |
|-----------|-------------|---------|
| Python 3.9+ | proxy, abs-add, inode-sync, export-metadata, image-cache | System package |
| `aiohttp` | proxy | `pip install aiohttp` |
| `ffprobe` | abs-add | `apt install ffmpeg` / `brew install ffmpeg` |
| `cwebp` | image-cache | `apt install webp` / `brew install webp` |
| `sqlite3` | abs-backup | Usually pre-installed |
| `jq` | monitoring | `apt install jq` / `brew install jq` |
| `curl` | monitoring | Usually pre-installed |
| `docker` | monitoring (container checks), abs-add/inode-sync (restart) | Docker Engine |

---

## License

MIT
