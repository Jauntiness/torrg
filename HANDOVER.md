# HANDOVER — torbox-webdav LAZY-Materialize-v2 (Bau läuft)

## TL;DR / Wiedereinstieg
Wir bauen den **LAZY-Materialize-Modus** für den torbox-webdav-Shim, **inkrementell nach `LAZY_PLAN.md` mit
TDD**. **Schritte 0–6 ERLEDIGT+verifiziert (inkl. Schritt-4-Live-e2e am 2026-06-03). NEXT = NUR noch
Schritt 7 (Plex-Integration, erst NACH DMM-Migration). KERN-BAU IST FERTIG.**

## Schritt-4-Live-e2e Ergebnis (2026-06-03, Quota frei)
- `materialize()` live bewiesen: garbage-tid → createtorrent-Re-Add → frische tid=35353477/fid=0 in **1.12s**,
  `cdn_url` löst signierte CDN-URL auf, Katalog persistiert.
- Bytes-nach-materialize bewiesen via **native WebDAV (HTTP 206, 1KB in 3.4s)** + Prod-rclone-Mount lieferte
  1MB durch die volle Pipeline.
- **CDN-Node-Störung beobachtet:** Node `nexus-156.nord.tb-cdn.st` hängt bei Byte-Reads (TCP-connect ok,
  nie HTTP-Antwort, 30s-Timeout) — betrifft ALLE Files (auch nie-re-addedte), also TorBox-CDN-seitig,
  kein Code-Bug. Der CDN→native-Fallback fängt es ab (Prod-Mount las 1MB in 16.8s = ~10s CDN-Timeout +
  native). TUNING-Idee (nicht umgesetzt): `CDN_OPEN_TIMEOUT` (akt. 10s) bei häufigen CDN-Hängern senken
  für schnelleren Fallback-Start.

## ⚠️ HARTE API-GRENZE (2026-06-03 entdeckt)
`POST /torrents/createtorrent` ist auf **60 Calls / Stunde** limitiert (HTTP 429 `{"detail":"60 per 1 hour"}`).
Das ist DAS Argument fuer den Probe-Cache (Schritt 5): Materialize NUR bei echtem Playback einer expirten
Datei, NIE bei Plex-Scans. Bei einem Scan-Storm ueber viele expirte Dateien wuerde das Limit sofort reissen.
`materialize()` behandelt 429 sauber (zurueckweichen, KEIN faelschliches Evict).
Alles in `/srv/torrg/`, hinter `LAZY`-env-Flag. **`LAZY=0` = der LIVE
Direkt-Mount-Shim (Prod, Container `torbox_webdav`) — bei jedem Schritt unberührt halten.**

## Zuerst lesen
- `LAZY_SPEC.md` — Was/Warum, verifizierte Fakten, User-Entscheidungen.
- `LAZY_PLAN.md` — 8 Schritte (0–7) + Zwischentest je Schritt.
- Memory `project_torbox_webdav_shim` — volle Projekt-Historie, Entscheidungen, API-Deps.

## Code-Stand
- **app.py** (Shim): Direkt-Mount (LAZY=0) ist Prod. Bei LAZY=1 kommt Katalog dazu.
  - Globals (oben): `LAZY`, `CATALOG_DB`, `CAT` (None ausser bei LAZY).
  - `build_tree()`: pro cached-Item `if CAT is not None: CAT.index_item(it, VIDEO_MIME)`.
  - `__main__`: bei LAZY → `CAT = Catalog(CATALOG_DB)`.
- **catalog.py** — SQLite-Katalog. Klasse `Catalog(path)`, Methoden:
  `upsert(hash,wpath,fname,size,mime)`, `index_item(item,video_mimes)->n` (nur Video-Files, idempotent),
  `set_cached(hash,bool,ts)`, `get(hash,wpath)`, `count(cached_only=False)`, `all_cached()`,
  `all_hashes()`, `_conn()` (thread-local, WAL).
  Schema: `files(hash,wpath,fname,size,mime,cached,last_check,materialized_at, PK(hash,wpath))`.
- **test_catalog.py**, **test_cataloger.py** — TDD-Tests (laufen mit `python3 test_X.py`, kein Docker).
- **Dockerfile**: `COPY app.py catalog.py /app/`. **compose** `torbox_webdav`: env `LAZY=${LAZY:-0}`,
  `CATALOG_DB=/data/catalog.db`, volume `./data:/data`.

## Tests (das Muster für jeden Schritt)
1. **Unit/TDD** (reine catalog-Logik, kein Docker): `cd .../torbox-webdav && python3 test_X.py`.
   → RED schreiben (richtiger Fehlergrund) → minimal GREEN → vorherige Tests als Regression.
2. **Integration LAZY=1** (Wegwerf-Container, Prod NICHT anfassen):
   ```bash
   cd /srv/torrg && docker compose build torbox_webdav
   rm -rf /tmp/lazytest && mkdir -p /tmp/lazytest
   docker run -d --rm --name tbwd_lazy --network torbox-webdav_tbwd \
     -e TORBOX_API_KEY=<your-torbox-api-key> -e LAZY=1 -e CATALOG_DB=/data/catalog.db \
     -v /tmp/lazytest:/data torbox-webdav:local
   # auf "Tree gebaut" warten, /tmp/lazytest/catalog.db inspizieren, dann: docker rm -f tbwd_lazy
   ```
3. **Prod-Check**: `torbox_webdav` (LAZY=0) muss nach jedem Schritt unverändert laufen.

## Methodik (so weitermachen)
- **executing-plans + TDD**: pro Schritt RED→GREEN→Integration→Regression, alles hinter `LAZY`.
- superpowers-Plugin ist enabled (`claude plugin install superpowers@claude-plugins-official`), lädt aber
  erst in NEUER Session; sonst Methodik manuell anwenden.
- Kein git-Repo → Isolation = `LAZY`-Flag (nicht worktree).
- Bei Blocker: stoppen + fragen, nicht raten.

## Schritt 2 (Cache-Monitor) — ERLEDIGT
- app.py: `cached_status(data, requested)` (reine Parse-Logik, case-insensitiv, None-safe),
  `cache_check_pass()` (Batch von `CACHE_BATCH=100` Hashes → `checkcached?hash=..&hash=..&format=object`
  → `CAT.set_cached`), `cache_monitor_loop()` (alle `CACHE_CHECK_S=3600`). `__main__`: bei LAZY initialer
  Pass + Loop-Thread.
- **Verifiziert:** Batch-API-Format bestätigt (echter Hash = Key in `data`, Fake fehlt, mehrere `&hash=` ok).
  Integration: 147/147 Hashes gecacht in ~0.76s (2 Batches), 781 Rows cached=1+last_check. Negativ-Pfad
  (mock api_get) markiert evicted→cached=0. Tests: `test_cachemon.py` + Regression grün. Prod unberührt.

## Schritt 3 (Lazy-Listing aus Katalog) — ERLEDIGT
- catalog.py: Schema um `type, folder, tid, fid` erweitert (+ idempotente ALTER-Migration `_MIGRATE`).
  `index_item(item, video_mimes, type)` (NEUE Signatur, 3. Arg!) speichert folder=sanitized-name,
  tid=item-id, fid=file-id. `upsert(...,type,folder,tid,fid)`.
- app.py: `IDKEY` map; `tree_from_catalog(cat)` baut TREE-Struktur aus `cat.all_cached()` (nur cached=1,
  gleiche Pfad-/Kollisions-Logik wie build_tree, meta enthält zusätzlich `hash` für S4-Re-Add);
  `lazy_tree_swap()` swappt TREE auf Katalog-Sicht. Wiring: am Ende von `build_tree` (bei LAZY), nach
  initialem cache_check in `__main__`, und nach jedem `cache_check_pass` im Monitor-Loop (evicted raus).
- **Verifiziert:** Tests `test_cataloger.py` (neue Felder) + `test_lazylisting.py` (cached-only/meta/idkey/
  kollision) + Regression grün. Integration LAZY=1: Katalog-Listing == Live (147 Ordner/781 Dateien),
  PROPFIND root=148 collections, HEAD korrekte Content-Length. Evicted-Pfad in-process: lazy_tree_swap→
  propfind_body listet cached, evicted verschwindet, resolve()=None. Prod LAZY=0 unberührt (kein
  LAZY-Log, PROPFIND=148).

## Schritt 4 (Materialize-on-Read + Re-Add-by-Hash) — LOGIK ERLEDIGT, Live-e2e offen
- app.py: `fid_for_wpath(files, wpath)` (wpath→frische fid), `api_post_create(magnet)` (curl Form-POST,
  liefert JSON + `_http`-Code), `materialize(meta)` (createtorrent add_only_if_cached → 429-Guard →
  not-cached→evict → mylist?id=tid poll → fid_for_wpath → meta tid/fid + `CAT.update_location`).
  GET-Handler `_get`: optimistisch `cdn_url(meta)`; bei Fehler & LAZY → `materialize(meta)` → retry;
  sonst 404 (LAZY) / 502.
- catalog.py: `update_location(hash,wpath,tid,fid,ts)` (UPDATE tid/fid/materialized_at).
- **Verifiziert (Unit, gemockt):** `test_materialize.py` — fid_for_wpath, update_location, success,
  not-cached→evict, **429→False ohne evict**. Regression grün.
- **Verifiziert (Live-API, einzeln):** createtorrent gibt bei vorhandenem cached Hash `torrent_id` zurück
  ("Found Cached Torrent"); `mylist?id=<tid>` liefert Einzeldict mit `files[]` (id+name) → fid-Map ok.
- **OFFEN (Live-e2e in einem Zug):** garbage-tid→materialize→CDN-Bytes Test lief ins 429 (DMM-Migration
  verbraucht die 60/h). Sobald Quota frei: `python3` in-process (Skript war: temp-Katalog seeden mit
  realem Video-Hash+wpath, meta tid/fid=garbage, `materialize(meta)` timen, dann `cdn_url`+Range-Read
  1KB; assert frische tid/fid + Bytes + Katalog persistiert). Realer Test-Hash zuletzt:
  `13ae814ac6805b7ee3bb129c6661c15e841d6922`.

## Schritt 5 (Probe-Cache) — ERLEDIGT
- `probecache.py`: `ProbeCache(dir, head_bytes, tail_bytes)`. Speichert pro (hash,wpath) NUR Bytes in
  head-Fenster `[0,HEAD)` + tail-Fenster `[size-TAIL,size)` (Plex-Scan-Regionen); Body wird verworfen
  (Disk-Schutz). Intervall-Tracking (reihenfolge-unabhängig) + JSON-Sidecar (persistent über Neustart).
  `get(hash,wpath,start,length)`→bytes|None (nur wenn voll gecacht), `put(...,data,size)` clippt auf Fenster.
- app.py: Globals `PROBE`, `PROBE_DIR=/data/probe`, `PROBE_HEAD_MB=16`, `PROBE_TAIL_MB=2`, `PROBE_DEBUG`.
  `__main__` instanziiert PROBE bei LAZY. `_get` umgebaut: Range-Parse VOR `cdn_url`; **Probe-Hit serviert
  lokal VOR cdn_url/materialize** (→ Scans API-frei); Stream-Loop tee't Bytes per Chunk in `PROBE.put`.
  Dockerfile kopiert probecache.py.
- **Verifiziert:** `test_probecache.py` (head/tail-only, body-skip, persistenz, isolation) + Regression grün.
  Integration LAZY=1: 1. Header-GET=Miss→CDN→Blob (1MB on disk); 2. identischer GET=**PROBE-HIT (Log) +
  gleiche md5, kein CDN**; Body-GET@20MB → Bytes via CDN, Blob UNVERÄNDERT (kein Body-Cache), kein Hit.
  Prod LAZY=0 unberührt.

## Schritt 6 (Evicted-Handling sauber + Probe-Cache-Hygiene) — ERLEDIGT
- catalog.py: `wpaths_for_hash(hash)`, `get_cached(hash)` (MAX(cached); 1/0/None).
- app.py: `drop_probes_for_hash(h)` (dropt Probes aller wpaths eines Hash). Eingehängt an BEIDE
  Evict-Stellen: `cache_check_pass` (bei frisch evicted, `was != 0`) + `materialize`-not-cached.
  `PROBE_MAX_MB` global → an ProbeCache(max_bytes=...).
- probecache.py: `drop(hash,wpath)` (Blob+Sidecar löschen, idempotent, wirft nie); `max_bytes`-Budget
  mit In-Memory-Größentracking `_sizes` (Summe der Ranges, NICHT logische Sparse-Größe!), `_scan_existing`
  beim Start, `_evict_lru` (nach Blob-mtime), put: bei Budget-Überschreitung erst LRU-evict, sonst no-op
  + einmalige WARNUNG; **put komplett in try/except → wirft NIE** (Serving-Pfad geschützt).
- Evicted-404: evicted (cached=0) fällt via lazy_tree_swap aus TREE → `resolve()` = None → GET 404 (war
  schon korrekt, jetzt getestet). Re-Cache: cached=0→1 via cache_check_pass + lazy_tree_swap → Datei
  taucht wieder auf (Katalog-Row bleibt, nur Flag toggelt).
- **Verifiziert:** `test_probehygiene.py` (drop idempotent, LRU-Evict hält Budget, no-op wenn Single-File>
  Budget, put wirft nie) + `test_evicthandling.py` (evict→aus Listing+Probe gedroppt+404; re-cache→wieder
  gelistet) + Regression (8 Suites grün). Integration LAZY=1 mit PROBE_MAX_MB=1: 3 Header-Reads →
  LRU-evict im Log, Probe-Dir bleibt ≤1MB (1 Blob), Server lebt (PROPFIND), 404 auf Fantasie-Pfad.
  Prod LAZY=0 unberührt.

## Kategorisierung movies/shows/music (2026-06-03) — zurg-Stil, editierbar
WebDAV ist jetzt VERSCHACHTELT: `{kategorie: {release: {fname: meta}}}`. Top-Level:
`__all__` (flach, ALLE Releases — fuer die cli_debrid-Union) + `movies`/`shows`/`music`/`other` (fuer Plex).
- `classify.py`: geordnete Regex-Regeln, erste passende gewinnt, sonst `other`. `categories.conf`
  (editierbar, gemountet → /config/categories.conf) im Format `kategorie = regex`. Default-Regeln eingebaut.
- app.py: `nest_releases(flat)` haengt jedes Release an seine Kategorie UND an `__all__` (gleiche
  meta-Referenzen, keine Daten-Dopplung). `tree_from_catalog`+`build_tree` rufen es. `walk(path)` ersetzt
  `resolve` (generisch, beliebige Tiefe); `propfind_body` generisch. `_get` nutzt walk.
- **Union-Branch ist `/source/torbox-fast/__all__`** (flach, wie zurg/__all__) — NICHT die verschachtelte
  Wurzel (sonst hingen movies/shows/music-Dirs in der Union-Wurzel). Verifiziert: Union-Wurzel flach, 1494.
- **Plex-Anbindung:** bind `/srv/media/mnt/torbox-fast:/mnt/torbox-fast:rshared` in
  Plex-compose, dann Library-Locations `/mnt/torbox-fast/movies`, `/mnt/torbox-fast/shows`,
  `/mnt/torbox-fast/music`. (Musik separat — eigene Pipeline; music-Kategorie hat nur die wenigen
  Audio-als-Video-getaggten TorBox-Alben.)
- Verifiziert: Mount zeigt __all__(149)/movies(21)/shows(122)/music(6); PROPFIND 3 Ebenen; GET nested ok.
  Tests `test_classify.py` + angepasste test_lazylisting/test_evicthandling. Nach categories.conf-Edit:
  `docker compose up -d torbox_webdav` (oder warten auf Refresh) + ggf. rclone `vfs/refresh`.

## Gruppen + 1080p-Spiegel + present-Fix (2026-06-03)
**Gruppen-Klassifizierung (classify.py):** `classify_groups()` → ein Release kann in MEHREREN Kategorien
liegen (eine je Gruppe). categories.conf jetzt SEKTIONS-Format: `[media]` shows/music/movies,
`[media_1080p_264]` shows_1080p_264/movies_1080p_264 (1080p+x264/h264/avc, lookahead-AND), `[all]` __all__.
Innerhalb Gruppe first-match, ueber Gruppen additiv. Mount: movies(21)/shows(122)/music(6)/
movies_1080p_264(3)/shows_1080p_264(19)/__all__(149). Plex quality-libraries → /mnt/torbox-fast/
movies_1080p_264 bzw. shows_1080p_264. `nest_releases` nutzt classify_groups. Test: test_classify.py.

**present-Fix (uncached Handling):** catalog `present`-Spalte (+Migration). `build_tree` nimmt jetzt
"verfuegbare" Items (download_present/finished/cached, nicht nur cached) auf, sammelt `seen`-Hashes,
ruft `CAT.sync_present(seen)`. `tree_from_catalog` nutzt `all_listed()` (present=1 OR cached=1).
`cache_check_pass` dropt Probes nur noch wenn `was!=0 AND not get_present` (wirklich weg). → uncached-aber-
vorhandene Files bleiben gemountet/abspielbar, werden NICHT materialize-getrackt, verschwinden erst bei
echtem Expiry (present=0 & cached=0). Materialize bleibt auf global-cached beschraenkt (add_only_if_cached).
Tests: test_present.py + angepasste test_evicthandling/test_lazylisting (echtes weg = sync_present()).

**⚠ Mount-Propagation:** `docker compose restart torbox_webdav_rclone` BRICHT Plex' /mnt/torbox-fast
("Transport endpoint not connected") trotz :rshared → danach `docker restart plex`. BESSER: statt rclone-
Restart `docker exec torbox_webdav_rclone rclone rc --rc-addr=:5572 --rc-no-auth vfs/refresh recursive=true`
fuer Struktur-/Dir-Aenderungen (kein Unmount). Plex-Bind liegt in plex/docker-compose.yml (:rshared).

## Scan-Range-Analyse-Tooling (2026-06-03) — fuer Schritt 7
- app.py: `ACCESS_LOG`-env (JSONL-Pfad). Pro GET eine Zeile `{t,src,start,end,len,size,wpath}` mit
  src=probe|cdn|native (probe=lokal/0-Download, cdn+native=aktiv geladen). Helper `alog()`, gehookt im
  Probe-Hit-Zweig + vor dem CDN/native-Streaming (slot["src"]).
- `scan_probe_report.py <access.log> [--head-mb 16 --tail-mb 2 --gap 120]`: clustert Reads in
  Scan-Sessions (Zeitluecke), zeigt pro File: Reads, Bytes, src-Aufteilung, ob alles in head/tail-Fenster,
  `download_bytes` (cdn+native). BEWEIS: ab Session #2 sollte download_bytes→0 (Scan aus Probe). `!!Body/
  ausserhalb` = Read jenseits head/tail (echter Plex-Scan macht das nicht). Test: `test_scanreport.py`.
- Smoke-Test bestaetigt: Scan1 (kalt) 2.06MB geladen → Scan2 (warm) head+tail=probe=2.00MB lokal,
  Download fiel auf den (synthetischen) Body-Read. Mechanik beweisbar.

## CDN-Befund praezisiert (2026-06-03)
NICHT unser Tool (BELEGT): roher curl auf TorBox' eigene signierte requestdl-URLs (ohne App/rclone) haengt
bei MANCHEN Files, liefert bei anderen sofort (206) — selber Node nexus-156. Korreliert mit frisch
migrierten (hoehere tid). Native WebDAV liefert alle. CDN→native-Fallback im Shim deckt es ab.
URSACHE = HYPOTHESE (User, unbestätigt): evtl. TorBox-CDN-Provisioning-Wartezeit ~15min nach Add/Re-Add.
Nicht gemessen — nur moegliche Erklaerung. Fuers Probe-Sizing irrelevant: Probe-Treffer umgehen CDN ganz.

## Schritt 7 (Plex-Integration) — AKTIVIERT 2026-06-03 (Plex-Teil offen beim User)
### ERLEDIGT:
1. **torbox-fast LAZY=1** (compose `torbox_webdav`): `LAZY=1` + `PROBE_DIR=/data/probe`
   `PROBE_MAX_MB=10000` `ACCESS_LOG=/data/access.log`. Läuft produktiv (Katalog 149/783, Cache-Monitor
   149/149, Probe füllt, Access-Log schreibt). Mount serviert über LAZY-Pfad (verifiziert mit frischem File).
2. **Union-Branch** `/source/torbox-fast` in `debrid-usenet-union` ergänzt → 1386→1494 Einträge (+108;
   ~41 überlappten via first-found). Union healthy.
3. **rclone --rc** (`--rc-addr=:5572 --rc-no-auth`) am Sidecar → VFS-Cache per `vfs/forget` leerbar ohne Unmount.

### WICHTIG — 2 Cache-Layer: rclone-VFS (30G/24h, vfs-cache-mode=full) sitzt VOR unserem Shim.
Repeat-Reads <24h werden von rclone bedient → erreichen den Shim NICHT (kein Download, aber auch kein
Probe-/Access-Log-Eintrag). Unser Probe-Cache greift, wenn rclone-Cache kalt ist (>24h/>30G) — plus
Materialize bei Expiry (kann rclone nicht).

### Scan-Experiment (User führt Plex-Scan, Tooling steht):
`scan_experiment.sh {reset|forget-rclone|report|status}` + `scan_probe_report.py`.
- `./scan_experiment.sh reset` → Probe+Access-Log leer, rclone-Cache vergessen (kalt). Dann **Plex-Scan #1**.
- `./scan_experiment.sh forget-rclone` → nur rclone-Cache weg, Probe BLEIBT. Dann **Plex-Scan #2**.
- `./scan_experiment.sh report [--head-mb 16 --tail-mb 2]` → BEWEIS: Session #2 `download_bytes`≈0
  (Scan-Reads aus Probe). `!!Body/ausserhalb`-Files → head/tail-Fenster anpassen.
- ACHTUNG Tuning: head=16MB×783≈12.5GB; nach echtem Scan die wirklich gelesenen maxoff prüfen und
  PROBE_HEAD_MB ggf. senken.
- `/mnt/torbox-fast` (LAZY=1) als Plex-Library-Location. Echter Scan misst Probe-Ranges + createtorrent=0
  nach Warmlauf; echtes Playback expirter Datei → Materialize-Latenz. PROBE_HEAD/TAIL_MB justieren.
- ACHTUNG: Cold-Scan einer KOMPLETT expirten Library würde pro File 1 createtorrent brauchen → 60/h-Limit.
  Mitigation: Probes während der ~14-30d-Frische-Phase warmlaufen lassen (1 Scan), dann bleiben Scans frei.

## Schritte 3–7: in `LAZY_PLAN.md`
Listing-aus-Katalog → Materialize-on-Read + Re-Add-by-Hash (`createtorrent` add_only_if_cached) →
Probe-Cache → Evicted-Handling → Plex-Integration (erst nach Migration).

## Gelernte Stolperfallen (diese Session)
- `pkill -f <muster>` matcht die Harness-Shell selbst, wenn das Muster in der cmdline steht → killt sich
  selbst (exit 144). Stattdessen `fuser -k PORT` oder exakte PIDs.
- TorBox-API hinter Cloudflare blockt Python-urllib (403) → curl-subprocess.
- Nativer WebDAV-Pfad = `"/" + f["name"]` (NICHT torrent-name+short_name — weichen ab, American-Murder-Bug).
- CDN pro Datei mal zickig (transiente Last) → nativer WebDAV-Fallback fängt's (schon gebaut, `slot["native"]`).
- Speed NUR back-to-back messen (geteilte CDN-Last → cross-time-Zahlen = Rauschen).
- rclone-Mount-Config (gesetzt): `--vfs-read-ahead=0 --vfs-read-chunk-size=1M --vfs-read-chunk-size-limit=128M
  --buffer-size=0M` = scan-leicht + dynamisch-schnell.
- Reliability-Fallback-Creds in `.env` (chmod 600), via compose `${TORBOX_WEBDAV_USER/PASS}`.
