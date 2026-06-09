# torrg

**Self-hosted "zurg for TorBox":** exposes your whole [TorBox](https://torbox.app)
account as a read-only WebDAV filesystem and serves reads through TorBox's fast
CDN via an HTTP range-proxy — giving near-Real-Debrid streaming parity instead of
TorBox's slow native WebDAV. Mount it with `rclone` and point Plex / Jellyfin /
Infuse at it for an infinite, zero-local-storage library.

> Independent project, not affiliated with TorBox. Inspired by the debrid-bridge
> ecosystem (zurg, decypharr). Uses only TorBox's public API.

## Why

TorBox's native WebDAV is reliable but slow (~72 Mbit/s in testing). torrg instead
builds a catalog of your account and proxies byte-range reads straight from
TorBox's CDN download URLs, reaching CDN speed — with an automatic fallback to the
native WebDAV for the occasional file whose CDN URL stalls.

## How it works

- **Account → file tree.** Polls the TorBox API (`torrents` / `usenet` / `webdl`)
  and presents every file as a normal path over WebDAV (`PROPFIND` + `GET`/`HEAD`).
- **CDN range-proxy.** `GET` with a `Range` header is streamed from the file's
  TorBox CDN URL; URLs are cached and refreshed on expiry.
- **Native-WebDAV fallback.** Files whose CDN URL hangs are served from TorBox's
  native WebDAV instead (the "CatBox" pattern), transparent to the client.
- **LAZY mode.** The catalog survives TorBox cache expiry; entries are
  materialized on read (re-added if cached) so the mount stays stable.
- **Probe cache + warmer.** Head/tail byte windows are cached and proactively
  warmed per file, so media-server scans and playback starts are instant without
  re-hitting the CDN for every probe.
- **Category split.** A zurg-style, editable `categories.conf` groups content into
  `movies` / `shows` / `music` so you can map them to separate Plex libraries.

## Setup

```bash
cp .env.example .env                       # fill in TORBOX_API_KEY (+ optional WebDAV creds)
cp docker-compose.example.yml docker-compose.yml   # adjust the host paths marked "<-- ADJUST"
docker compose up -d --build
```

This starts two services: `torrg` (the WebDAV server) and an `rclone` sidecar that
mounts it at `/mnt/remote/torbox-fast`. Point your media server's library at the
mount's `content/` folders and disable its auto-scan on the FUSE path.

### Key environment variables

| Var | Default | Purpose |
|---|---|---|
| `TORBOX_API_KEY` | — | **Required.** TorBox API key (Settings → API). |
| `PORT` | `8112` | WebDAV listen port. |
| `REFRESH_SECONDS` | `900` | How often the account tree is refreshed. |
| `LAZY` | `1` | Catalog survives expiry; materialize-on-read. |
| `CATALOG_DB` | `/data/catalog.db` | Catalog persistence. |
| `PROBE_DIR` | `/data/probe` | Probe-blob cache (put on a fast/cache disk). |
| `PROBE_MAX_MB` | `32000` | Probe cache size cap. |
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
