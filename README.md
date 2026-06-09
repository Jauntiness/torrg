# torrg

A self-hosted WebDAV bridge that turns your [TorBox](https://torbox.app) account
into an **eviction-proof, scan-safe** media library for Plex / Jellyfin / Infuse —
with near-Real-Debrid streaming speed. Mount it with `rclone` and point your media
server at it.

torrg is more than a mount. It keeps a **persistent catalog** of your whole library
that survives TorBox's 30-day eviction, **automatically re-adds expired torrents**
the moment you press play, and answers Plex's library scans from a **purpose-built
local cache** so a scan over thousands of files costs *zero* TorBox API calls.

> Independent project, not affiliated with TorBox. The lazy-materialize and
> probe-cache ideas are a self-hosted reimplementation of CatBox's two core
> patterns; the WebDAV+rclone mount shape follows zurg. Uses only TorBox's public API.

## The problem it solves

A naive "mount TorBox and point Plex at it" breaks three ways:

1. **TorBox evicts content after ~30 days of inactivity** → your library rots and
   files 404.
2. **TorBox rate-limits re-adds to 60/hour** → letting Plex re-add on access gets
   you throttled.
3. **Plex scans read file headers constantly** → a naive cache would hammer TorBox
   on every scan.

torrg is built specifically to survive all three.

## Features

### 🗄️ Eviction-proof library (lazy re-materialize)
A SQLite **catalog** persists every file in your library — hash, location, cached
state — independent of what's currently live in your TorBox account. When Plex
actually plays a file whose TorBox copy has expired, torrg **re-adds it by hash on
the fly** (`createtorrent` + `add_only_if_cached`, instant for globally-cached
content) and resolves a fresh download URL. Your library never rots, and your
TorBox account stays lean — only recently-played content is materialized.

### 🛡️ Scan-safe probe cache (built for Plex)
A local **probe cache** stores just the head/tail byte windows of each file (the
container header + index that Plex reads to analyze media). Library scans and codec
probes are answered entirely from this cache — **never touching TorBox**. A
`PlaybackIntent` state machine distinguishes a real playback (sustained, sequential
reads) from a scan (small header reads), so **scans never trigger a re-add** — only
genuine playback does, under an hourly budget that respects the 60/hour limit. Net
result: a full Plex scan over your whole library costs zero TorBox API calls and
zero re-adds.

### ⚡ Fast CDN streaming
Reads are proxied as byte ranges straight from TorBox's fast CDN (`requestdl`),
reaching ~Real-Debrid parity instead of TorBox's slow native WebDAV. URLs are
cached and refreshed on expiry, with an automatic **fallback to native TorBox
WebDAV** for the occasional file whose CDN URL stalls (the "CatBox" pattern).

### 🔥 Proactive warmer
Optionally pre-warms each file's head/tail window (size-aware: bigger headers for
bigger files, tiny windows for audio) so first-play and first-scan are instant
without a cold round-trip.

### 🗂️ Category split
A zurg-style, editable `categories.conf` groups content into `movies` / `shows` /
`music` (and 1080p mirrors), so you can map each to its own Plex library.

## How it works

```
TorBox API ──poll──> catalog.db (persistent, survives eviction)
                          │
rclone mount ──WebDAV──> torrg ──> probe cache hit?  ──yes──> serve locally (no TorBox)
                                   │
                                   no, real playback ──> materialize (re-add by hash)
                                                          └─> CDN range-proxy ──(stall)──> native WebDAV fallback
```

## Setup

```bash
cp .env.example .env                       # fill in TORBOX_API_KEY (+ optional WebDAV creds)
cp docker-compose.example.yml docker-compose.yml   # adjust the host paths marked "<-- ADJUST"
docker compose up -d --build
```

Two services start: `torrg` (the WebDAV server) and an `rclone` sidecar that mounts
it. Point your media server's libraries at the mount's category folders and disable
its auto-scan on the FUSE path (torrg/your-grabber triggers scans).

### Key environment variables

| Var | Default | Purpose |
|---|---|---|
| `TORBOX_API_KEY` | — | **Required.** TorBox API key (Settings → API). |
| `PORT` | `8112` | WebDAV listen port. |
| `LAZY` | `1` | Eviction-proof catalog + materialize-on-play. `0` = plain direct mount. |
| `CATALOG_DB` | `/data/catalog.db` | Catalog persistence. |
| `REFRESH_SECONDS` | `900` | How often the account is polled. |
| `PROBE_DIR` | `/data/probe` | Probe-window cache (put on a fast/cache disk). |
| `PROBE_HEAD_MB` / `PROBE_TAIL_MB` | `24` / `8` | Probe window covering Plex's header/index reads. |
| `PROBE_MAX_MB` | `0` | Probe cache size cap (0 = unbounded). |
| `WARM` | `1` | Proactively warm head/tail of present files. |
| `CATEGORIES_CONF` | `/config/categories.conf` | movies/shows/music split rules. |
| `TORBOX_WEBDAV_USER` / `_PASS` | — | Native-WebDAV fallback creds (optional). |

## Development

Pure Python 3 standard library at runtime (plus `curl` and `rclone` in the
container). Tests use `pytest`:

```bash
pip install -r requirements-dev.txt
pytest
```

`poc.py` is the original single-file proof-of-concept that established CDN-range
streaming parity.

## Status

Young and evolving — runs a real Plex library in production, but expect rough
edges. Issues and PRs welcome.
