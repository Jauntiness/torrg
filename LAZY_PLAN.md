# PLAN — Lazy-Materialize-Modus (inkrementell, jeder Schritt mit Zwischentest)

Referenz: LAZY_SPEC.md. Bauweise: kleiner Schritt → Test/Verifikation → erst dann weiter.
`LAZY=0` (default) muss in JEDEM Schritt das bestehende Direkt-Mount-Verhalten unverändert lassen.

---

## Schritt 0 — Scaffolding: Modus-Flag + Katalog-DB-Modul
**Bauen:** `LAZY` env-Flag; SQLite-Modul (Schema `files(hash PK, wpath, size, mime, cached, last_check,
materialized_at)`), Init beim Start. Keine Verhaltensänderung bei LAZY=0.
**Test:** Server startet mit/ohne LAZY; bei LAZY=0 identisches Verhalten (PROPFIND/GET wie bisher,
Speed-Spotcheck). DB-Datei wird angelegt, Schema korrekt.

## Schritt 1 — Cataloger: Account → Katalog-DB
**Bauen:** beim Tree-Build zusätzlich jede Video-Datei in `files` upserten (hash, wpath, size, mime).
**Test:** `SELECT COUNT(*)` == Anzahl Video-Files im Account (≈779). Spot-check: 3 Zeilen, wpath/size
stimmen mit API überein. Re-Run = keine Duplikate (upsert).

## Schritt 2 — Cache-Monitor: checkcached über Katalog
**Bauen:** Hintergrund-Loop, Batch-`checkcached` über alle Katalog-Hashes (Batch-Größe ermitteln),
`cached`+`last_check` setzen.
**Test:** bekannter gecachter Hash → `cached=1`. Fake-Hash injiziert → `cached=0`. Loop aktualisiert
`last_check`. Messung: Dauer für alle ~779 Hashes.

## Schritt 3 — Lazy-Listing aus Katalog (statt Live-Account)
**Bauen:** bei LAZY=1 baut PROPFIND/Tree aus der Katalog-DB (nur `cached=1`), nicht aus dem Live-Account.
**Test:** Eintrag im Account simuliert-entfernt (oder einfach Katalog behält ihn) → Listing zeigt die
Datei weiterhin (volle Library). evicted (cached=0) → erscheint NICHT im Listing.

## Schritt 4 — Materialize-on-Read + Re-Add-by-Hash
**Bauen:** bei LAZY=1, GET → falls Hash nicht aktiv im Account: `createtorrent(magnet=hash,
add_only_if_cached)` → ready-poll → CDN-URL → via bestehende Serve-Pipeline (Stream-Reuse + Fallback).
**Test:** Account-Eintrag einer gecachten Datei entfernen → Datei lesen → wird re-added → liefert Bytes.
**Latenz messen** (Materialize-Zeit). checkcached=not-cached → 404.

## Schritt 5 — Probe-Cache (on-first-access)
**Bauen:** servierte Byte-Ranges pro Hash lokal cachen (sparse file/blob). GET-Range innerhalb
gecachter Probe → lokal serven, KEIN Materialize. Range jenseits → Materialize (Schritt 4).
**Test:** Datei 1× lesen (Header) → Probe gecacht. 2. „Scan" derselben Region → **kein createtorrent**
im Log, Bytes aus Cache. Read jenseits Probe → Materialize triggert. Logge createtorrent-Calls zum Zählen.

## Schritt 6 — Evicted-Handling sauber
**Bauen:** cached=0 → GET 404/410 an rclone, Eintrag aus Listing; Katalog-Flag bleibt (für Re-Cache-
Erkennung). Optional: Notify/Log für cli-debrid-Übergabe.
**Test:** Hash als evicted markieren → Listing ohne Datei, GET→404. Wird Hash später wieder cached
(checkcached=1) → erscheint wieder.

## Schritt 7 — Plex-Integration (NACH Migration, mit Plex)
**Bauen:** `/mnt/torbox-fast` (LAZY=1) als Plex-Library-Location.
**Test:** echter Plex-Scan → misst tatsächliche Probe-Ranges + Scan-Traffic (createtorrent-Count ~0 nach
Warmlauf). Echtes Playback abgelaufener Datei → Materialize-Latenz + flüssig. `PROBE_HEAD/TAIL_MB`
justieren. Optional: proaktives Probe-Pre-Caching (Spec v2).

---

## Reihenfolge-Logik
- 0–2 = Fundament, jetzt ohne Plex verifizierbar (Katalog + Monitoring).
- 3–6 = Lazy-Kern, mit synthetischen Tests (Account-Eintrag entfernen/simulieren) verifizierbar.
- 7 = echte Validierung + Tuning, erst wenn Plex drankommt (nach Migration).
- Jeder Schritt hinter `LAZY`-Flag → Direkt-Mount bleibt jederzeit als stabiler Fallback nutzbar.
