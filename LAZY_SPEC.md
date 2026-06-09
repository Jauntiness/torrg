# SPEC — Lazy-Materialize-Modus für torbox-webdav

## Ziel
Optionaler `LAZY`-Modus: der Shim exponiert einen **persistenten Katalog** der TorBox-Library
(Plex sieht IMMER die volle Library), **materialisiert** Files nur bei echtem Playback in den
TorBox-Account (instant für global-gecachte Hashes) und beantwortet **Plex-Scans aus einem lokalen
Probe-Cache**. Resultat: TorBox-Account bleibt schlank, Plex muss nie neu scannen, kein unnötiger
Scan-/CDN-Traffic, keine cli-debrid-Re-Grab-Churn.

## Problem / Motivation
- TorBox-Account-Einträge laufen ab (`expires_at` pro Torrent, durch Zugriff zurückgesetzt). Ungesehener
  Long-Tail läuft ab.
- Aktuell (keep-full + cli-debrid-Re-Grab) löst das UNSAUBER: Re-Grab holt eine NEUE Release → Datei
  verschwindet aus Plex → teurer Initial-Re-Scan. Obwohl die Datei global oft noch gecacht ist
  (kein echter Re-Download nötig).
- Lazy re-addet denselben **Hash** → dieselbe Datei → kein Plex-Change → instant (gecacht).

## Verifizierte Fakten (2026-06-02)
- `GET /torrents/checkcached?hash=<h>&format=object` → gecacht: `{h:{name,size}}`, sonst `{}`. Funktioniert
  auch für Hashes NICHT im Account (globaler Cache-Check). Batch wahrscheinlich (mehrere hash-Params).
- Gecachte Downloads zählen NICHT gegen Active-Limits ("infinite cached" erlaubt).
- `POST /torrents/createtorrent` (magnet aus Hash) + `add_only_if_cached` → Re-Add; für gecacht instant.
- Account-Einträge haben `expires_at` (~14d beobachtet); Zugriff resettet (final mit Plex zu verifizieren).
- Nativer WebDAV-Pfad pro Datei = `"/" + f["name"]` (schon im Fallback genutzt).

## Architektur / Komponenten
1. **Katalog-DB (SQLite):** pro Datei {hash, wpath, size, mime, cached, last_check, materialized_at,
   probe_ranges}. Überlebt Account-Expiry. Befüllt aus Account-Enumeration (jetzt, während Migration).
2. **Cache-Monitor:** periodisch Batch-`checkcached` über alle Katalog-Hashes → cached-Flag setzen.
   evicted → aus Katalog/Listing droppen (cli-debrid re-grabt diese ECHT-evicteten Nischen-Files).
3. **Probe-Cache:** lokal die Byte-Ranges, die Plex beim Scan liest (Header/moov), pro Hash. Wird OHNE
   Materialisieren ausgeliefert.
4. **Materialize-on-Read:** Read JENSEITS der Probe-Region (= echtes Playback) → Hash sicherstellen
   (falls nicht aktiv: `createtorrent` add_only_if_cached) → CDN-URL → serven (bestehender
   Stream-Reuse + nativer Fallback).
5. **WebDAV-Serve:** PROPFIND/Tree aus Katalog (volle Library immer sichtbar); GET-Routing:
   Probe-Cache-Hit → lokal; sonst materialisieren+serven.
6. **Modus-Flag `LAZY` (default 0):** aus = aktuelles Direkt-Mount-Verhalten unverändert.

## Materialize-Logik (GET [start,end], LAZY=1)
- Range innerhalb Probe-Cache → aus Probe-Cache serven. KEIN createtorrent, KEIN/minimaler CDN-Touch.
- Range jenseits Probe-Region → Materialize:
  - `checkcached(hash)`: nicht gecacht → 404/410 an rclone → Katalog markiert evicted → cli-debrid.
  - gecacht: falls Hash nicht aktiv im Account → `createtorrent(magnet, add_only_if_cached)` → ready
    (instant) → CDN-URL auflösen → via bestehende Pipeline serven. Servierte Probe-Region cachen.

## Probe-Cache-Strategie
- v1: **on-first-access** — erster Scan materialisiert einmal, cached die gelesene Region; Folge-Scans
  cache-served. (Kein Plex-Test vorab nötig.)
- v2-Optimierung: **proaktiv beim Katalogisieren** vorab cachen (Head N MB + Tail M MB), solange Content
  im Account ist → sogar Initial-Scan cache-served. Größe N/M nach Plex-Beobachtung justieren.

## Lifecycle (User-Entscheidung 2026-06-02)
- **Eintrag NATÜRLICH ablaufen lassen** (nicht aktiv entfernen). Re-Add ist on-demand.

## Evicted-Handoff (User-Entscheidung)
- Global-evicteter Hash → aus Katalog droppen → cli-debrid re-grabt die **aktuell beste Release**
  (muss nicht die alte sein). Bonus: re-grabben statt stures Re-Add desselben alten Hashes sieht fuer
  TorBox weniger "bot-haft" aus. Handoff an cli-debrid genau so gewollt.

## Integration (User-Entscheidung)
- Lazy-Mount (`torbox-fast`) laeuft ZUERST PARALLEL zum bestehenden RD/zurg, zum Testen.
- Wenn RD in ein paar Tagen auslaeuft, wird `torbox-fast` (LAZY) DIE Quelle fuer cli-debrid + Plex.

## Config / Flags
`LAZY`(0/1), `CATALOG_DB`(pfad), `PROBE_HEAD_MB`(default 16), `PROBE_TAIL_MB`(4),
`CACHE_CHECK_INTERVAL`(default 3600s), `MATERIALIZE_TIMEOUT`(30s).

## Erfolgskriterien
- Plex-Library zeigt vollen Katalog, auch wenn Account-Einträge abgelaufen sind.
- Scan einer katalogisierten-aber-nicht-materialisierten Datei → aus Probe-Cache, KEIN createtorrent,
  minimale/keine CDN-Bytes.
- Playback einer abgelaufenen-aber-global-gecachten Datei → Materialize < wenige s → streamt (CDN-Speed).
- Global-evicteter Hash → aus Katalog gedroppt → cli-debrid re-grabt.
- Keine Regression im Direkt-Mount-Modus (LAZY=0).

## Non-Goals
- Add-Seite (qBit/Torznab) → bleibt cli-debrid/rdt-client.
- Crowdsourced Cross-User-Probe-Data → nur lokal.
- 307-DirectStream → separat, hier nicht.

## Offene Fragen / zu verifizieren
- Resettet normaler Plex-Zugriff `expires_at`? (Nach Migration beobachten.)
- Welche Byte-Ranges liest Plex/ffprobe beim Scan genau (nur Head? moov am Tail?)? (Beobachten.)
- `createtorrent`-Re-Add-Latenz für gecachten Hash (messen).
- Batch-Limit von `checkcached` (Anzahl Hashes pro Call).
