#!/usr/bin/env python3
# torbox-webdav: schlanker WebDAV-Server, der den GANZEN TorBox-Account als Dateibaum
# exponiert und File-Reads per Range-Proxy ueber den schnellen CDN ausliefert (~CDN-Speed,
# statt TorBox' langsamem nativem WebDAV). Self-hosted "zurg fuer TorBox".
#
# Bewiesen im PoC: rclone -> dieser WebDAV -> CDN liefert RD/zurg-Paritaet.
import os, sys, json, time, threading, subprocess, logging, urllib.request, urllib.error
import concurrent.futures
from urllib.parse import quote, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import classify
from swarm import SegmentSwarm

API_KEY    = os.environ["TORBOX_API_KEY"]
PORT       = int(os.environ.get("PORT", "8112"))
LAZY        = os.environ.get("LAZY", "") == "1"            # Lazy-Materialize-Modus (v2); 0 = Direkt-Mount
CATALOG_DB  = os.environ.get("CATALOG_DB", "/data/catalog.db")
CAT         = None                                          # Catalog-Instanz (nur bei LAZY gesetzt)
PROBE       = None                                          # ProbeCache-Instanz (nur bei LAZY gesetzt)
PROBE_DIR   = os.environ.get("PROBE_DIR", "/data/probe")
PROBE_HEAD_MB = float(os.environ.get("PROBE_HEAD_MB", "16"))  # head-Fenster (Plex liest Container-Header)
PROBE_TAIL_MB = float(os.environ.get("PROBE_TAIL_MB", "2"))   # tail-Fenster (moov/Index am Ende)
PROBE_MAX_MB = float(os.environ.get("PROBE_MAX_MB", "0"))     # Gesamt-Budget (0=unbegrenzt); Disk-Schutz
PROBE_DEBUG = os.environ.get("PROBE_DEBUG", "") == "1"        # Probe-Hits loggen (Verifikation)
WARM = os.environ.get("WARM", "1") == "1"                     # proaktiv head/tail vorwaermen (on-add)
WARM_WORKERS = int(os.environ.get("WARM_WORKERS", "4"))       # parallele Vorwaerm-Fetches
WARM_IDLE_S = int(os.environ.get("WARM_IDLE_SECONDS", "300")) # Pause wenn nichts zu waermen
# Musik-Ausnahme: Audio-Files sind klein (2-50MB) mit winzigen Tags -> nicht 24/8MB warmen.
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".wma", ".alac", ".aiff")
MUSIC_HEAD_MB = float(os.environ.get("MUSIC_HEAD_MB", "4"))   # head fuer Audio (Tags vorne)
MUSIC_TAIL_MB = float(os.environ.get("MUSIC_TAIL_MB", "1"))   # tail fuer Audio (ID3v1 am Ende)
# Dynamische Vorwaerm-Groesse: Header korreliert grob mit Dateigroesse (groesser=hoehere Bitrate=
# tieferer Header). Gemessen: 1080p ~6.5MB@4GB, 2160p-REMUX ~21MB@70GB. Formel deckt beide ab.
WARM_MIN_HEAD_MB = float(os.environ.get("WARM_MIN_HEAD_MB", "6"))    # Floor (kleine Videos)
WARM_HEAD_PER_GB = float(os.environ.get("WARM_HEAD_PER_GB", "0.25")) # Steigung pro GB Dateigroesse

def warm_sizes(wp, sz):
    """(head_bytes, tail_bytes) zum proaktiven Vorwaermen. Audio klein (winzige Tags); Video
    size-basiert, gedeckelt aufs Probe-Fenster (PROBE_HEAD_MB/TAIL_MB) — das Fenster bleibt das
    Lazy-Fill-Sicherheitsnetz, falls ein File doch mehr liest als vorgewaermt."""
    if wp.lower().endswith(AUDIO_EXT):
        return (int(MUSIC_HEAD_MB * 1024 * 1024), int(MUSIC_TAIL_MB * 1024 * 1024))
    gb = sz / (1024 ** 3)
    head_mb = min(PROBE_HEAD_MB, max(WARM_MIN_HEAD_MB, WARM_MIN_HEAD_MB + gb * WARM_HEAD_PER_GB))
    return (int(head_mb * 1024 * 1024), int(PROBE_TAIL_MB * 1024 * 1024))
# Gebundener Superset (deckt Plex' gemessenen ≤21MB Header + MP4-moov-Tail; deterministisch,
# container-agnostisch, future-proof) — Warm-Groesse = Probe-Fenster PROBE_HEAD_MB/PROBE_TAIL_MB.
CATEGORIES_CONF = os.environ.get("CATEGORIES_CONF", "/config/categories.conf")  # zurg-Stil Regex-Split
RULES = classify.load_rules(CATEGORIES_CONF)                  # editierbar; fehlt -> Defaults
REDIRECT   = os.environ.get("TORBOX_REDIRECT", "") == "1"    # 307 auf CDN-URL statt proxen (DirectStream)
REFRESH_S  = int(os.environ.get("REFRESH_SECONDS", "900"))   # Account-Tree alle 15 min neu
CDN_TTL_S  = int(os.environ.get("CDN_TTL_SECONDS", "1800"))  # signierte CDN-URL 30 min cachen
UA         = "Mozilla/5.0 (X11; Linux x86_64) torbox-webdav/1.0"
API        = "https://api.torbox.app/v1/api"
VIDEO_MIME = ("video/x-matroska", "video/mp4", "video/avi", "video/x-msvideo", "video/mpeg")
LMOD       = "Mon, 01 Jan 2024 00:00:00 GMT"
CDN_OPEN_TIMEOUT = float(os.environ.get("CDN_OPEN_TIMEOUT", "5"))   # CDN-First-Byte; danach native-Fallback
# (gemessen: gesunde CDN-Files antworten <2s, haengende nie -> 5s trennt sauber, schneller Fallback)
# Fallback: natives TorBox-WebDAV fuer Files, deren CDN-URL haengt/scheitert (CatBox-Muster).
import base64
WEBDAV_BASE = os.environ.get("TORBOX_WEBDAV_URL", "https://webdav.torbox.app")
WEBDAV_USER = os.environ.get("TORBOX_WEBDAV_USER", "")
WEBDAV_PASS = os.environ.get("TORBOX_WEBDAV_PASS", "")
WEBDAV_AUTH = ("Basic " + base64.b64encode(f"{WEBDAV_USER}:{WEBDAV_PASS}".encode()).decode()) if WEBDAV_USER else ""
NATIVE_TTL  = int(os.environ.get("NATIVE_TTL", "300"))  # nach CDN-Fail X s lang nativ bevorzugen

# Dual-Source-Swarm: CDN (prio 1) + natives WebDAV (prio 2) fuellen ein begrenztes Read-Ahead-
# Fenster PARALLEL -> Durchsaetze addieren sich; saettigt CDN die Leitung, findet WebDAV keine
# Arbeit und pausiert von selbst (emergent, KEIN Mbps-Schwellwert). Opt-in via SWARM=1.
SWARM        = os.environ.get("SWARM", "") == "1"
SWARM_SEG    = int(float(os.environ.get("SWARM_SEG_MB", "1")) * 1024 * 1024)  # = rclone-Chunk (1 MiB)
SWARM_WINDOW = int(os.environ.get("SWARM_WINDOW", "24"))   # Segmente Read-Ahead (RAM = WINDOW*SEG je Stream)
SWARM_MAX    = int(os.environ.get("SWARM_MAX", "2"))       # max gleichzeitige Swarms (RAM-Deckel)
SWARM_READ_TIMEOUT = float(os.environ.get("SWARM_READ_TIMEOUT", "20"))  # pro read() bevor Fallback
SWARM_IDLE_S = int(os.environ.get("SWARM_IDLE_SECONDS", "90"))  # idle-Swarm schliessen (Threads/RAM frei)
# Hedge-Schwelle (WebDAV springt ein, wenn CDN das Kopf-Segment nicht in grace liefert).
# grace = max(HEDGE_MIN, EWMA(CDN-Segmentzeit) * HEDGE_K) -> selbst-justierend, KEIN fixer Wert.
SWARM_HEDGE_K      = float(os.environ.get("SWARM_HEDGE_K", "2.0"))        # Sensitivitaet (x CDN-Schnitt)
SWARM_HEDGE_BOOT   = float(os.environ.get("SWARM_HEDGE_BOOTSTRAP", "1.5"))# Grace beim Kaltstart (s)
SWARM_HEDGE_MIN    = float(os.environ.get("SWARM_HEDGE_MIN", "0.3"))      # Untergrenze grace (s)
# Combine: WebDAV fuellt parallel andere Segmente, sobald read() miss_n-mal in Folge auf den Puffer
# warten musste (CDN < Bedarf) -> Durchsaetze addieren sich. Aus, sobald Puffer wieder >refill_frac tief.
# Baseline-Tracking (v2): WebDAV-Test nur wenn CDN langsamer als sein Normal (cdn_fast > cdn_slow*K)
# UND Nachfrage ungedeckt. Half der Test (Skips) -> beide laufen; sonst Cooldown.
SWARM_REFILL_FRAC  = float(os.environ.get("SWARM_REFILL_FRAC", "0.85"))   # Puffer-Tiefe gilt als gedeckt
SWARM_DEVIATION    = float(os.environ.get("SWARM_DEVIATION", "1.6"))      # cdn_fast/cdn_slow-Faktor -> degradiert
SWARM_DEGRADE_PERSIST = float(os.environ.get("SWARM_DEGRADE_PERSIST", "2.0"))  # s anhaltend langsam (Anti-Jitter)
SWARM_TEST_WINDOW  = float(os.environ.get("SWARM_TEST_WINDOW", "3.0"))    # s WebDAV-Test
SWARM_COOLDOWN     = float(os.environ.get("SWARM_COOLDOWN", "30.0"))      # s Pause nach nutzlosem Test (Basis)
SWARM_COOLDOWN_MAX = float(os.environ.get("SWARM_COOLDOWN_MAX", "300.0")) # Backoff-Deckel (schwankende Leitung)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("torbox-webdav")

# ── Access-Log: pro GET die gelesene Byte-Range + Quelle (probe/cdn/native) als JSONL.
# Fuer die Scan-Analyse (welche File-Teile fasst ein Plex-Scan an? reicht der Probe-Cache?).
ACCESS_LOG = os.environ.get("ACCESS_LOG", "")
_alog_lock = threading.Lock()
def alog(src, start, length, size, wpath):
    if not ACCESS_LOG: return
    try:
        line = json.dumps({"t": round(time.time(), 3), "src": src, "start": start,
                           "end": start + length, "len": length, "size": size, "wpath": wpath})
        with _alog_lock, open(ACCESS_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── TorBox API via curl (Cloudflare blockt Python-urllib-TLS-Fingerprint) ──────────────
def api_get(path):
    out = subprocess.run(["curl", "-sf", "-H", f"Authorization: Bearer {API_KEY}", f"{API}{path}"],
                         capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"curl rc={out.returncode}: {out.stderr.decode()[:200]}")
    return json.loads(out.stdout)

# ── Account-Tree:  { folder_name: { file_name: {tid,fid,type,size} } } ─────────────────
TREE = {}              # folder -> {filename -> meta}
TREE_LOCK = threading.RLock()
CDN_CACHE = {}         # (type,tid,fid) -> (url, expires_at)
CDN_LOCK = threading.RLock()
STREAMS = {}           # key -> {pos,resp,lock,last}: offene CDN-Streams fuer Sequential-Reuse
STREAMS_LOCK = threading.Lock()

def get_stream_slot(key):
    with STREAMS_LOCK:
        slot = STREAMS.get(key)
        if slot is None:
            slot = {"pos": -1, "resp": None, "lock": threading.Lock(), "last": 0.0}
            STREAMS[key] = slot
        return slot

def reaper_loop():                 # idle CDN-Streams schliessen (kein fd-Leak)
    while True:
        time.sleep(30)
        now = time.time()
        with STREAMS_LOCK:
            slots = list(STREAMS.values())
        for slot in slots:
            if slot["resp"] is not None and now - slot["last"] > 90 and slot["lock"].acquire(blocking=False):
                try:
                    if slot["resp"] is not None and now - slot["last"] > 90:
                        try: slot["resp"].close()
                        except Exception: pass
                        slot["resp"] = None; slot["pos"] = -1
                finally:
                    slot["lock"].release()

# ── Dual-Source-Swarm-Registry ───────────────────────────────────────────────────────────
SWARMS = {}            # key -> {"swarm": SegmentSwarm, "size": int, "last": float}
SWARMS_LOCK = threading.Lock()

def _opener_cdn(meta, key):
    """Swarm-Quelle CDN: oeffnet einen FORWARD-Stream ab off (offene Verbindung -> Connection-
    Reuse fuer die ganze Strecke); bei abgelaufener URL (403/410) neu aufloesen."""
    def open(off):
        hdr = {"User-Agent": UA, "Range": f"bytes={off}-"}
        try:
            return urllib.request.urlopen(urllib.request.Request(cdn_url(meta), headers=hdr),
                                          timeout=CDN_OPEN_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (403, 410):
                with CDN_LOCK: CDN_CACHE.pop(key, None)
                return urllib.request.urlopen(urllib.request.Request(cdn_url(meta), headers=hdr),
                                              timeout=CDN_OPEN_TIMEOUT)
            raise
    return open

def _opener_native(meta):
    """Swarm-Quelle natives TorBox-WebDAV: Forward-Stream ab off."""
    def open(off):
        if not WEBDAV_AUTH:
            raise RuntimeError("kein WebDAV konfiguriert")
        u = WEBDAV_BASE + quote("/" + meta.get("wpath", ""))
        return urllib.request.urlopen(urllib.request.Request(
            u, headers={"User-Agent": UA, "Authorization": WEBDAV_AUTH,
                        "Range": f"bytes={off}-"}), timeout=30)
    return open

def get_swarm(key, meta, size):
    """Per-Stream-Swarm holen/erzeugen. Deckelt die Zahl gleichzeitiger Swarms (RAM)."""
    with SWARMS_LOCK:
        e = SWARMS.get(key)
        if e is None:
            while len(SWARMS) >= SWARM_MAX:                # aeltesten schliessen (LRU)
                ok = min(SWARMS, key=lambda k: SWARMS[k]["last"])
                try: SWARMS[ok]["swarm"].close()
                except Exception: pass
                del SWARMS[ok]
            srcs = [("cdn", _opener_cdn(meta, key))]
            if WEBDAV_AUTH:                                # WebDAV als prio-2-Hedge-Quelle dazu
                srcs.append(("web", _opener_native(meta)))
            e = {"swarm": SegmentSwarm(size, SWARM_SEG, SWARM_WINDOW, srcs,
                                       hedge_k=SWARM_HEDGE_K, hedge_bootstrap=SWARM_HEDGE_BOOT,
                                       hedge_min=SWARM_HEDGE_MIN, refill_frac=SWARM_REFILL_FRAC,
                                       deviation_factor=SWARM_DEVIATION, test_window_s=SWARM_TEST_WINDOW,
                                       cooldown_s=SWARM_COOLDOWN, degrade_persist_s=SWARM_DEGRADE_PERSIST,
                                       cooldown_max_s=SWARM_COOLDOWN_MAX),
                 "size": size, "last": 0.0}
            SWARMS[key] = e
            log.info(f"Swarm gestartet ({len(srcs)} Quellen, Fenster {SWARM_WINDOW}x{SWARM_SEG // 1048576}MiB) "
                     f"fuer {key}")
        e["last"] = time.time()
        return e["swarm"]

def swarm_reaper_loop():           # idle Swarms schliessen (Threads + Fenster-RAM frei)
    while True:
        time.sleep(30)
        now = time.time()
        with SWARMS_LOCK:
            dead = [k for k, e in SWARMS.items() if now - e["last"] > SWARM_IDLE_S]
            for k in dead:
                try: SWARMS[k]["swarm"].close()
                except Exception: pass
                del SWARMS[k]
        for k in dead:
            log.info(f"Swarm idle-geschlossen: {k}")

def nest_releases(flat):
    """Flache {release: {fname: meta}} -> verschachtelte {kategorie: {release: {fname: meta}}}
    via editierbare zurg-Stil Regex-Regeln (categories.conf). So zeigt der Mount oben
    movies/shows/music/other -> Release -> Datei."""
    nested = {}
    for folder, files in flat.items():                  # ein Release -> mehrere Kategorien (Gruppen)
        for cat in classify.classify_groups(folder, list(files.keys()), RULES):
            nested.setdefault(cat, {})[folder] = files  # selbe meta-Referenzen, keine Daten-Dopplung
    return nested

def build_tree():
    new = {}
    seen = set()                                        # Hashes, die JETZT im Account vorhanden sind
    for typ, idkey in (("torrents", "torrent_id"), ("usenet", "usenet_id"), ("webdl", "web_id")):
        offset = 0
        while True:
            try:
                d = api_get(f"/{typ}/mylist?bypass_cache=true&limit=1000&offset={offset}")
            except Exception as e:
                log.warning(f"{typ} offset {offset}: {e}"); break
            items = d.get("data") or []
            if not items: break
            for it in items:
                # "verfuegbar" = fertig/vorhanden ODER global gecacht. Unfertige Downloads raus.
                if not (it.get("download_present") or it.get("download_finished") or it.get("cached")):
                    continue
                if CAT is not None:
                    CAT.index_item(it, VIDEO_MIME, typ)                  # LAZY: Katalog befuellen
                    if it.get("hash"): seen.add(it["hash"])
                folder = (it.get("name") or str(it.get("id"))).strip("/").replace("/", "_")
                for f in it.get("files", []):
                    if f.get("mimetype") not in VIDEO_MIME: continue
                    fname = os.path.basename(f.get("short_name") or f.get("name") or str(f.get("id")))
                    if not fname: continue
                    bucket = new.setdefault(folder, {})
                    # bei Namenskollision Datei-id anhaengen
                    key = fname
                    if key in bucket: key = f"{os.path.splitext(fname)[0]}.{f.get('id')}{os.path.splitext(fname)[1]}"
                    bucket[key] = {"type": typ, "idkey": idkey, "tid": it.get("id"),
                                   "fid": f.get("id"), "size": int(f.get("size") or 0),
                                   # native-WebDAV-Pfad (Fallback) = "/" + f["name"] (voller realer Pfad,
                                   # NICHT torrent-name+short_name: die weichen ab, siehe American-Murder-Bug)
                                   "wpath": f.get("name") or f.get("short_name") or ""}
            if len(items) < 1000: break
            offset += 1000
    if CAT is not None: CAT.sync_present(seen)          # present-Flag: was JETZT im Account ist
    new = nest_releases(new)                           # movies/shows/music/other-Split
    nrel = sum(len(v) for v in new.values())
    nfiles = sum(len(files) for cat in new.values() for files in cat.values())
    with TREE_LOCK:
        TREE.clear(); TREE.update(new)
    log.info(f"Tree gebaut: {len(new)} Kategorien, {nrel} Releases, {nfiles} Dateien")
    if LAZY and CAT is not None: lazy_tree_swap()      # Listing aus Katalog (ueberlebt Expiry)

# ── LAZY: Listing aus Katalog statt Live-Account ─────────────────────────────────────────
IDKEY = {"torrents": "torrent_id", "usenet": "usenet_id", "webdl": "web_id"}

def tree_from_catalog(cat):
    """TREE-Struktur (folder -> {fname -> meta}) aus cached=1 Katalog-Rows. Account-unabhaengig
    (ueberlebt Expiry). Identische Pfad-/Kollisions-Logik wie build_tree."""
    new = {}
    for r in cat.all_listed():                          # present ODER global cached
        folder = r["folder"] or "_"
        fname = r["fname"] or os.path.basename(r["wpath"])
        bucket = new.setdefault(folder, {})
        key = fname
        if key in bucket:                                  # Namenskollision -> file-id anhaengen
            base, ext = os.path.splitext(fname)
            key = f"{base}.{r['fid']}{ext}"
        bucket[key] = {"type": r["type"], "idkey": IDKEY.get(r["type"], "torrent_id"),
                       "tid": r["tid"], "fid": r["fid"], "size": int(r["size"] or 0),
                       "wpath": r["wpath"], "hash": r["hash"]}
    return nest_releases(new)                           # movies/shows/music/other-Split

def lazy_tree_swap():
    """Ersetzt das Live-TREE durch die Katalog-Sicht (nur cached=1)."""
    if CAT is None: return
    new = tree_from_catalog(CAT)
    nrel = sum(len(v) for v in new.values())
    nfiles = sum(len(files) for cat in new.values() for files in cat.values())
    with TREE_LOCK:
        TREE.clear(); TREE.update(new)
    log.info(f"LAZY-Listing aus Katalog: {len(new)} Kategorien, {nrel} Releases, "
             f"{nfiles} Dateien (nur cached=1)")

def refresh_loop():
    while True:
        time.sleep(REFRESH_S)
        try: build_tree()
        except Exception as e: log.error(f"refresh: {e}")

# ── LAZY: Cache-Monitor — pollt TorBox checkcached, haelt Katalog-cached-Flag aktuell ────
CACHE_BATCH    = int(os.environ.get("CACHE_BATCH", "100"))         # Hashes je checkcached-Request
CACHE_CHECK_S  = int(os.environ.get("CACHE_CHECK_SECONDS", "3600"))# Re-Check-Intervall

def cached_status(data, requested):
    """checkcached-'data' ({hash:{...}} fuer gecachte) -> {hash: bool} fuer ALLE requested.
       Case-insensitiv (TorBox lowercased Hashes); data=None/leer -> alles False."""
    present = {h.lower() for h in data} if isinstance(data, dict) else set()
    return {h: (h.lower() in present) for h in requested}

def drop_probes_for_hash(h):
    """Bei Eviction die Probe-Blobs aller Dateien dieses Hash loeschen (Cache nicht mit toten Files
    wachsen lassen)."""
    if PROBE is None or CAT is None: return
    for wp in CAT.wpaths_for_hash(h):
        PROBE.drop(h, wp)

def cache_check_pass():
    """Ein Durchlauf: alle Katalog-Hashes via checkcached pruefen + cached-Flag setzen.
       Gibt (geprueft, davon_gecacht) zurueck."""
    if CAT is None: return (0, 0)
    hashes = CAT.all_hashes()
    now = time.time(); ncached = 0
    for i in range(0, len(hashes), CACHE_BATCH):
        batch = hashes[i:i + CACHE_BATCH]
        qs = "&".join(f"hash={h}" for h in batch)
        try:
            d = api_get(f"/torrents/checkcached?{qs}&format=object")
        except Exception as e:
            log.warning(f"checkcached batch@{i}: {e}"); continue   # Hiccup: Flag unveraendert lassen
        for h, c in cached_status(d.get("data"), batch).items():
            was = CAT.get_cached(h)
            CAT.set_cached(h, c, now)
            if c:
                ncached += 1
            elif was != 0 and not CAT.get_present(h):     # global-evicted UND nicht mehr present -> weg
                drop_probes_for_hash(h)
    return (len(hashes), ncached)

def cache_monitor_loop():
    while True:
        time.sleep(CACHE_CHECK_S)
        try:
            tot, nc = cache_check_pass()
            log.info(f"Cache-Monitor: {nc}/{tot} Hashes gecacht")
            lazy_tree_swap()                               # evicted aus Listing entfernen
        except Exception as e:
            log.error(f"cache-monitor: {e}")

def cdn_url(meta):
    k = (meta["type"], meta["tid"], meta["fid"])
    now = time.time()
    with CDN_LOCK:
        c = CDN_CACHE.get(k)
        if c and c[1] > now: return c[0]
    url = (f"{API}/{meta['type']}/requestdl?token={API_KEY}"
           f"&{meta['idkey']}={meta['tid']}&file_id={meta['fid']}&redirect=false")
    data = api_get(url.replace(API, ""))  # api_get prepends API
    cdn = data.get("data")
    if not cdn: raise RuntimeError("keine CDN-URL")
    with CDN_LOCK:
        CDN_CACHE[k] = (cdn, now + CDN_TTL_S)
    return cdn

# ── LAZY: Materialize-on-Read — Re-Add eines expirten (aber global cached) Hash ─────────
MAT_POLL_S = float(os.environ.get("MATERIALIZE_POLL_SECONDS", "1.0"))
MAT_TRIES  = int(os.environ.get("MATERIALIZE_TRIES", "10"))

def fid_for_wpath(files, wpath):
    """Mappt nativen Pfad (f['name']) auf die aktuelle file-id (nach Re-Add neu vergeben)."""
    for f in files or []:
        if f.get("name") == wpath:
            return f.get("id")
    return None

def api_post_create(magnet):
    """createtorrent via curl (Form-POST). add_only_if_cached -> kein teurer uncached-Pull.
    Gibt geparstes JSON zurueck, angereichert um '_http' (HTTP-Code) — fuer 429-Erkennung
    (createtorrent ist auf 60/Stunde limitiert)."""
    out = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", "-H", f"Authorization: Bearer {API_KEY}",
         "-F", f"magnet={magnet}", "-F", "add_only_if_cached=true",
         f"{API}/torrents/createtorrent"], capture_output=True, timeout=60)
    body, _, code = out.stdout.decode().rpartition("\n")
    try:
        d = json.loads(body)
    except Exception:
        d = {"success": False, "detail": body[:200]}
    d["_http"] = int(code) if code.strip().isdigit() else 0
    return d

def materialize(meta):
    """Re-Add des cached Hash (add_only_if_cached) + frische tid/fid fuer meta['wpath'] aufloesen.
    Aktualisiert meta + Katalog. True wenn danach servierbar; False wenn nicht (mehr) cached."""
    h = meta.get("hash")
    if not h:
        return False
    try:
        d = api_post_create(f"magnet:?xt=urn:btih:{h}")
    except Exception as e:
        log.error(f"materialize createtorrent {h[:12]}: {e}"); return False
    if d.get("_http") == 429:                                       # 60/h erschoepft -> NICHT evicten
        log.warning(f"materialize {h[:12]} rate-limited (createtorrent 60/h): {d.get('detail')}")
        return False
    if not d.get("success"):
        # WICHTIG: hier NICHT evicten/Probe-droppen. Ein Netzwerk-/Throttle-Fehler (curl rc!=0 -> _http=0)
        # ist KEINE Eviction. Selbst ein echtes "not cached" wird ausschliesslich von cache_check_pass
        # behandelt (definitives checkcached-Signal). So zerschiesst ein einzelner Re-Add-Fail nie die Probe.
        if d.get("_http") == 0:
            log.warning(f"materialize {h[:12]} TorBox nicht erreichbar — kein Re-Add, Probe bleibt")
        else:
            log.warning(f"materialize {h[:12]} nicht (mehr) cached: {d.get('detail')} (Eviction macht cache_check)")
        return False
    tid = (d.get("data") or {}).get("torrent_id")
    if tid is None:
        return False
    typ = meta.get("type") or "torrents"
    fid = None
    for _ in range(MAT_TRIES):                                       # cached re-add ~instant, sonst poll
        try:
            info = api_get(f"/{typ}/mylist?id={tid}&bypass_cache=true")
            data = info.get("data")
            item = data if isinstance(data, dict) else (data[0] if data else {})
            fid = fid_for_wpath(item.get("files"), meta["wpath"])
        except Exception as e:
            log.warning(f"materialize mylist {tid}: {e}")
        if fid is not None:
            break
        time.sleep(MAT_POLL_S)
    if fid is None:
        log.error(f"materialize {h[:12]}: fid fuer wpath nicht gefunden"); return False
    meta["tid"] = tid; meta["fid"] = fid
    if CAT is not None: CAT.update_location(h, meta["wpath"], tid, fid, time.time())
    log.info(f"materialized {h[:12]} -> tid={tid} fid={fid}")
    return True

def in_probe_window(start, length, size):
    """Liegt der Range KOMPLETT im head- ODER tail-Fenster? Solche Reads sind Scan-Reads
    (Header/moov) und duerfen NIE Materialize/Re-Add ausloesen — sonst re-addet ein Library-Scan
    abgelaufene Files (createtorrent 60/h, bot-haft). Nur echte Body-Reads (Playback) duerfen re-adden."""
    head = int(PROBE_HEAD_MB * 1024 * 1024)
    tail = int(PROBE_TAIL_MB * 1024 * 1024)
    return (start + length) <= head or start >= max(0, size - tail)

class PlaybackIntent:
    """Window-Read-Misses duerfen materialisieren, wenn sie nach PLAYBACK aussehen.
    (2026-06-06 One-Piece-Incident: expired File ohne Head-Probe konnte NIE starten, weil
    Plex beim Start nur den Header liest und Window-Reads kategorisch kein Re-Add durften.)
    Unterscheidung Playback vs Library-Scan:
      - Playback haemmert DENSELBEN File (Player/rclone-Retry-Loop): >= repeat_n Misses
        desselben Keys innerhalb repeat_window -> Intent.
      - Ein Scan fasst viele VERSCHIEDENE Files an: >= storm_keys distinct Keys innerhalb
        storm_window -> Storm -> Deny (die ersten <storm_keys Files koennen durchrutschen,
        gedeckelt durchs Budget — akzeptiert).
      - budget_per_hour Grants max (Schutz vor createtorrent-429, Limit 60/h).
      - cooldown je Key nach Grant (toter Hash loest nicht endlos Re-Adds aus).
    Thread-safe; miss() registriert den Miss und sagt, ob JETZT materialisiert werden darf."""
    def __init__(self, repeat_n=3, repeat_window=30, storm_keys=4, storm_window=120,
                 budget_per_hour=12, cooldown=600):
        self.repeat_n, self.repeat_window = repeat_n, repeat_window
        self.storm_keys, self.storm_window = storm_keys, storm_window
        self.budget, self.cooldown = budget_per_hour, cooldown
        self._misses = {}          # key -> [timestamps]
        self._grants = []          # timestamps aller Grants (Budget)
        self._granted_at = {}      # key -> letzter Grant (Cooldown)
        self._lock = threading.Lock()

    def miss(self, key, now):
        with self._lock:
            ts = self._misses.setdefault(key, [])
            ts.append(now)
            # prune (storm_window ist das laengste Fenster, das wir je Key brauchen)
            keep = max(self.repeat_window, self.storm_window)
            self._misses[key] = ts = [t for t in ts if now - t <= keep]
            for k in [k for k, v in self._misses.items() if not v or now - v[-1] > keep]:
                if k != key: self._misses.pop(k, None)
            self._grants = [t for t in self._grants if now - t <= 3600]
            # 1) Cooldown nach Grant fuer diesen Key
            if key in self._granted_at and now - self._granted_at[key] <= self.cooldown:
                return False
            # 2) Playback-Muster: genug Misses desselben Keys in kurzer Zeit?
            recent = [t for t in ts if now - t <= self.repeat_window]
            if len(recent) < self.repeat_n:
                return False
            # 3) Scan-Storm: zu viele distinct Keys mit frischen Misses?
            active = sum(1 for v in self._misses.values()
                         if any(now - t <= self.storm_window for t in v))
            if active >= self.storm_keys:
                return False
            # 4) Stunden-Budget
            if len(self._grants) >= self.budget:
                return False
            self._grants.append(now)
            self._granted_at[key] = now
            # Miss-Historie BEWUSST behalten: der Storm-Detektor braucht sie (Grant macht
            # den Key sonst unsichtbar); Re-Grant-Schutz uebernimmt der Cooldown.
            return True

INTENT = PlaybackIntent()

# ── Warmer: proaktiv head+tail vorwaermen, damit der Mount IMMER warm fuer Plex-Scans ist ──
def warm_fetch(meta, start, length):
    """Holt [start, start+length) fuer's Vorwaermen NATIVE-first (zuverlaessig, keine CDN-Hang-Waits),
    CDN als Fallback. bytes|None."""
    end = start + length - 1
    if WEBDAV_AUTH:                                           # natives WebDAV zuerst (haengt nicht)
        try:
            u = WEBDAV_BASE + quote("/" + meta.get("wpath", ""))
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Authorization": WEBDAV_AUTH,
                                                     "Range": f"bytes={start}-{end}"})
            return urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass
    try:                                                     # Fallback: CDN
        url = cdn_url(meta)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"})
        return urllib.request.urlopen(req, timeout=CDN_OPEN_TIMEOUT).read()
    except Exception:
        return None

def warm_file(meta):
    """Waermt size-basiert head+tail (warm_sizes) in die Probe. Sparsame Erstbefuellung; das
    24MB-Probe-Fenster bleibt das Lazy-Fill-Sicherheitsnetz. Idempotent. True wenn JETZT gewaermt."""
    if PROBE is None:
        return False
    h, wp, sz = meta.get("hash"), meta.get("wpath"), int(meta.get("size") or 0)
    if not h or not wp or sz <= 0:
        return False
    if PROBE.get(h, wp, 0, min(65536, sz)) is not None:      # schon gewaermt -> skip
        return False
    head_b, tail = warm_sizes(wp, sz)                        # size-basiert (Audio klein, Video skaliert)
    head = min(head_b, sz)
    d = warm_fetch(meta, 0, head)
    if not d:
        return False
    PROBE.put(h, wp, 0, d, sz)
    if sz > tail:
        td = warm_fetch(meta, sz - tail, tail)
        if td: PROBE.put(h, wp, sz - tail, td, sz)
    return True

def meta_from_row(r):
    return {"type": r["type"], "idkey": IDKEY.get(r["type"], "torrent_id"),
            "tid": r["tid"], "fid": r["fid"], "size": int(r["size"] or 0),
            "wpath": r["wpath"], "hash": r["hash"]}

def warm_pass():
    """Waermt alle present, noch nicht gewaermten Files (tid/fid gueltig -> kein Materialize).
    Gibt (jetzt_gewaermt, total_present) zurueck."""
    if CAT is None or PROBE is None:
        return (0, 0)
    rows = [r for r in CAT.all_listed() if r["present"] == 1]
    metas = [meta_from_row(r) for r in rows]
    n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WARM_WORKERS) as ex:
        for res in ex.map(warm_file, metas):                 # parallel, Pool begrenzt die Last
            if res: n += 1
    return (n, len(rows))

def warmer_loop():
    while True:
        try:
            n, tot = warm_pass()
            if n:
                log.info(f"Warmer: {n} Files vorgewaermt ({tot} present gesamt)")
        except Exception as e:
            log.error(f"warmer: {e}")
        time.sleep(WARM_IDLE_S)

# ── Pfad-Routing ───────────────────────────────────────────────────────────────────────
def walk(path):
    """Pfad im verschachtelten TREE aufloesen: ('file', meta) | ('dir', node) | (None, None).
    TREE = {kategorie: {release: {fname: meta}}}; Blatt = meta (hat 'wpath')."""
    p = unquote(path).strip("/")
    with TREE_LOCK:
        node = TREE
        if p:
            for part in p.split("/"):
                if not isinstance(node, dict) or part not in node:
                    return (None, None)
                node = node[part]
        if isinstance(node, dict) and "wpath" in node:
            return ("file", node)
        return ("dir", node)

# ── WebDAV XML ─────────────────────────────────────────────────────────────────────────
def resp_xml(href, is_dir, size=0):
    rt = "<D:resourcetype><D:collection/></D:resourcetype>" if is_dir else "<D:resourcetype/>"
    cl = "" if is_dir else f"<D:getcontentlength>{size}</D:getcontentlength><D:getcontenttype>video/x-matroska</D:getcontenttype>"
    return (f'<D:response><D:href>{href}</D:href><D:propstat><D:prop>{rt}{cl}'
            f'<D:getlastmodified>{LMOD}</D:getlastmodified></D:prop>'
            f'<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>')

def propfind_body(path, depth):
    kind, node = walk(path)
    if kind is None: return None
    base = quote("/" + unquote(path).strip("/")) if unquote(path).strip("/") else ""
    parts = []
    if kind == "file":                                      # eine Datei
        parts.append(resp_xml(base, False, node["size"]))
    else:                                                   # ein Verzeichnis (root/kategorie/release)
        parts.append(resp_xml(base + "/" if base else "/", True))
        if depth != "0":
            for name, child in node.items():
                is_file = isinstance(child, dict) and "wpath" in child
                href = (base + "/" + quote(name)) if base else ("/" + quote(name))
                parts.append(resp_xml(href, False, child.get("size", 0)) if is_file
                             else resp_xml(href + "/", True))
    return ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
            + "".join(parts) + "</D:multistatus>").encode()

# ── WebDAV-DELETE ────────────────────────────────────────────────────────────────────────
# TorBox kann via API nur GANZE Items löschen (controltorrent op=delete), kein per-file.
# Per-File-Delete wird emuliert: Datei einzeln im Katalog ausblenden (mark_deleted), den
# TorBox-Torrent erst löschen, wenn die LETZTE Datei des Torrents weg ist. Ordner-DELETE
# (ganzer Release) = Torrent direkt. Kategorie-/Root-DELETE wird verweigert (Massen-Schutz).
TORBOX_CONTROL = {
    "torrents": ("torrents/controltorrent", "torrent_id"),
    "usenet":   ("usenet/controlusenetdownload", "usenet_id"),
    "webdl":    ("webdl/controlwebdownload", "webdl_id"),
}

def delete_request(type, tid):
    """(endpoint, json_payload) für den TorBox-Delete-Call eines GANZEN Items. Pure -> testbar."""
    ep, idkey = TORBOX_CONTROL[type]
    return ("/" + ep, {idkey: tid, "operation": "delete"})

def files_in_release(node):
    """node = dir-Knoten aus walk(). Ist es ein RELEASE-Ordner (alle Kinder sind Dateien),
    gib [(hash, wpath, type, tid), ...]. Sonst (Kategorie/Root mit Unterordnern, oder leer)
    -> None — verhindert, dass ein Kategorie-/Root-DELETE den ganzen Mount löscht."""
    if not isinstance(node, dict) or not node:
        return None
    children = list(node.values())
    if not all(isinstance(c, dict) and "wpath" in c for c in children):
        return None
    return [(c.get("hash"), c.get("wpath"), c.get("type"), c.get("tid")) for c in children]

def delete_item(type, tid):
    """Löscht ein ganzes TorBox-Item via curl-JSON-POST (controltorrent op=delete). Best-effort:
    Fehler werden geloggt, nicht geworfen (z.B. Item schon weg). True bei HTTP 200."""
    try:
        path, payload = delete_request(type, tid)
    except KeyError:
        log.error(f"delete_item: unbekannter Typ {type}"); return False
    out = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
         "-H", f"Authorization: Bearer {API_KEY}", "-H", "Content-Type: application/json",
         "-d", json.dumps(payload), f"{API}{path}"], capture_output=True, timeout=30)
    code = out.stdout.decode().strip()
    ok = code == "200"
    (log.info if ok else log.warning)(f"TorBox delete {type}#{tid}: HTTP {code}")
    return ok

def delete_torrent_for_hash(h):
    """TorBox-Item für hash löschen (über die letzte bekannte Account-Location) + Probes droppen."""
    if CAT is None:
        return
    loc = CAT.item_for_hash(h)
    if loc and loc[1] is not None:
        delete_item(loc[0], loc[1])
    drop_probes_for_hash(h)

# ── HTTP-Handler ───────────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def handle(self):                       # rclone schliesst keep-alive-Verbindungen -> harmlos, nicht tracen
        try: super().handle()
        except (ConnectionResetError, BrokenPipeError): pass
    def _send(self, code, hdrs=None, body=b""):
        self.send_response(code)
        hdrs = hdrs or {}
        if "Content-Length" not in hdrs: hdrs["Content-Length"] = str(len(body))
        for k, v in hdrs.items(): self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD": self.wfile.write(body)
    def do_OPTIONS(self):
        self._send(200, {"DAV": "1,2", "Allow": "OPTIONS, GET, HEAD, PROPFIND, DELETE", "MS-Author-Via": "DAV"})
    def do_PROPFIND(self):
        body = propfind_body(self.path, self.headers.get("Depth", "1"))
        if body is None: return self._send(404)
        self._send(207, {"Content-Type": 'application/xml; charset="utf-8"'}, body)
    def do_HEAD(self): self._get(head=True)
    def do_GET(self):  self._get(head=False)
    def do_DELETE(self):
        # Per-File-Emulation (siehe TORBOX_CONTROL-Block). Nur sinnvoll mit Katalog (LAZY).
        if not (LAZY and CAT is not None):
            return self._send(405)
        kind, node = walk(self.path)
        if kind is None:
            # Idempotent: ein DELETE auf einen bereits weg-geräumten Pfad -> 204, nicht 404.
            # Plex/rclone löscht nach der letzten Datei den jetzt leeren Release-Ordner; das
            # Item ist da schon weg + aus dem Listing genommen -> der Ordner-rmdir trifft None.
            # 404 würde dem Client fälschlich einen Fehler melden, obwohl das Löschen klappte.
            return self._send(204)
        if kind == "file":
            h, wp = node.get("hash"), node.get("wpath")
            if not h:
                return self._send(409)                 # ohne hash kein Delete-State
            if CAT.mark_deleted(h, wp):                 # letzte Datei -> ganzen Torrent löschen
                delete_torrent_for_hash(h)
            lazy_tree_swap()                            # sofort aus dem Listing nehmen
            return self._send(204)
        # Verzeichnis: nur ein RELEASE-Ordner darf gelöscht werden (nicht Kategorie/Root).
        files = files_in_release(node)
        if files is None:
            return self._send(405)                      # Massen-Lösch-Schutz
        hashes = set()
        for h, wp, _t, _tid in files:
            if h:
                CAT.mark_deleted(h, wp); hashes.add(h)
        for h in hashes:                                # Ordner-Delete = ganzen Torrent löschen
            delete_torrent_for_hash(h)
        lazy_tree_swap()
        return self._send(204)
    def _get(self, head):
        kind, meta = walk(self.path)
        if kind != "file": return self._send(404)
        fname = unquote(self.path).rstrip("/").split("/")[-1]
        if head:
            return self._send(200, {"Content-Length": str(meta["size"]),
                                    "Accept-Ranges": "bytes", "Content-Type": "video/x-matroska"})
        rng = self.headers.get("Range")
        # Range parsen + exakte Laenge selbst berechnen (nie CDN-Content-Length vertrauen).
        size = meta["size"]
        if rng and rng.startswith("bytes="):
            se = rng[6:].split("-", 1)
            start = int(se[0]) if se[0] else 0
            end = int(se[1]) if len(se) > 1 and se[1] else size - 1
            end = min(end, size - 1)
            length, code = end - start + 1, 206
            hdrs = {"Content-Range": f"bytes {start}-{end}/{size}"}
        else:
            start, length, code, hdrs = 0, size, 200, {}
        hdrs.update({"Accept-Ranges": "bytes", "Content-Type": "video/x-matroska",
                     "Content-Length": str(length)})

        # Probe-Cache: liegt der Range komplett im lokal gecachten head/tail -> direkt ausliefern,
        # OHNE CDN/Materialize/createtorrent -> Plex-Scans sind nach Warmlauf API-frei (60/h-Schutz).
        if PROBE is not None and not REDIRECT:
            cached = PROBE.get(meta.get("hash"), meta.get("wpath"), start, length)
            if cached is not None:
                if PROBE_DEBUG: log.info(f"PROBE-HIT {(fname or '')[:30]} [{start}-{start+length}) {length}B")
                alog("probe", start, length, size, meta.get("wpath"))
                self.send_response(code)
                for k, v in hdrs.items(): self.send_header(k, v)
                self.end_headers()
                try: self.wfile.write(cached)
                except (BrokenPipeError, ConnectionResetError): self.close_connection = True
                return

        try:
            url = cdn_url(meta)                          # Hot-Path: tid/fid noch gueltig
        except Exception as e:
            # Re-Add bei echten Body-Reads (Playback) IMMER. Window-Reads (head/tail-Fenster, Probe
            # verfehlt) nur bei erkanntem Playback-Intent (PlaybackIntent: Retry-Muster auf DEMSELBEN
            # File, kein Scan-Storm, Stunden-Budget) — sonst wuerde ein Library-Scan abgelaufene Files
            # massenhaft re-adden (createtorrent 60/h), ABER ein Play-Druck auf ein expired File ohne
            # Head-Probe darf nicht ewig 404en (One-Piece-Incident 2026-06-06).
            scan = in_probe_window(start, length, size)
            allow = (not scan) or INTENT.miss(meta.get("hash") or meta.get("wpath"), time.time())
            if LAZY and allow and materialize(meta):     # expired -> Re-Add-by-Hash
                if scan: log.info(f"Window-Miss mit Playback-Intent -> materialized {(fname or '')[:40]}")
                try:
                    url = cdn_url(meta)
                except Exception as e2:
                    log.error(f"cdn_url nach materialize: {e2}"); return self._send(502)
            else:
                if scan: log.warning(f"Scan-Miss (Fenster, kein Intent/Storm/Budget) [{start}-{start+length}) {(fname or '')[:40]}")
                else: log.error(f"cdn_url: {e}")
                return self._send(404 if LAZY else 502)
        if REDIRECT:                       # DirectStream: rclone/Plex direkt auf den CDN umleiten
            return self._send(307, {"Location": url})

        # Sequential-Stream-Reuse: zusammenhaengende rclone-Reads laufen ueber EINE offene
        # CDN-Verbindung -> kein Per-Chunk-First-Byte-Overhead -> ~curl-direct-Speed.
        key = (meta["type"], meta["tid"], meta["fid"])

        # Dual-Source-Swarm (opt-in): NUR fuer echte Body-Reads (ausserhalb des head/tail-Probe-
        # Fensters = Playback, nicht Scan). CDN+WebDAV fuellen das Read-Ahead-Fenster parallel.
        # Liefert der Swarm rechtzeitig -> ausliefern; sonst Fallback auf Single-Stream unten.
        if SWARM and not in_probe_window(start, length, size):
            try:
                sw = get_swarm(key, meta, size)
                data = sw.read(start, length, SWARM_READ_TIMEOUT)
            except Exception as e:
                log.warning(f"Swarm-Fehler ({type(e).__name__}: {e}) -> Single-Stream-Fallback")
                data = None
            if data is not None:
                sw.advance(start + length)
                self.send_response(code)
                for k, v in hdrs.items(): self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                alog("swarm", start, length, size, meta.get("wpath"))
                return
            log.warning(f"Swarm-Timeout [{start}-{start+length}) {(fname or '')[:38]} -> Single-Stream")

        slot = get_stream_slot(key)
        def open_cdn(off):
            try:
                return urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": UA, "Range": f"bytes={off}-"}), timeout=CDN_OPEN_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code in (403, 410):                       # CDN-URL abgelaufen -> neu aufloesen
                    with CDN_LOCK: CDN_CACHE.pop(key, None)
                    return urllib.request.urlopen(urllib.request.Request(
                        cdn_url(meta), headers={"User-Agent": UA, "Range": f"bytes={off}-"}), timeout=CDN_OPEN_TIMEOUT)
                raise
        def open_native(off):                                  # Fallback: natives TorBox-WebDAV
            if not WEBDAV_AUTH: raise RuntimeError("kein WebDAV-Fallback konfiguriert")
            u = WEBDAV_BASE + quote("/" + meta.get("wpath", ""))
            return urllib.request.urlopen(urllib.request.Request(
                u, headers={"User-Agent": UA, "Authorization": WEBDAV_AUTH, "Range": f"bytes={off}-"}), timeout=30)
        with slot["lock"]:
            resp = slot["resp"]
            if resp is None or slot["pos"] != start:          # Seek oder erster Read -> neuer Stream
                if resp:
                    try: resp.close()
                    except Exception: pass
                    slot["resp"] = None
                prefer_native = slot.get("native", 0) > time.time()   # nach CDN-Fail X s nativ bevorzugen
                try:
                    resp = open_native(start) if prefer_native else open_cdn(start)
                    slot["src"] = "native" if prefer_native else "cdn"
                except Exception as e_cdn:
                    if prefer_native:
                        log.error(f"WebDAV-Fallback fail: {e_cdn}"); return self._send(502)
                    try:                                       # CDN gescheitert -> natives WebDAV
                        resp = open_native(start)
                        slot["native"] = time.time() + NATIVE_TTL
                        slot["src"] = "native"
                        log.warning(f"CDN-Fail '{(fname or '')[:38]}' -> WebDAV-Fallback ({type(e_cdn).__name__})")
                    except Exception as e_nat:
                        log.error(f"CDN+WebDAV beide fail: {type(e_cdn).__name__} / {e_nat}")
                        return self._send(502)
                slot["resp"] = resp; slot["pos"] = start
            slot["last"] = time.time()
            self.send_response(code)
            for k, v in hdrs.items(): self.send_header(k, v)
            self.end_headers()
            remaining = length
            abs_pos = start
            try:
                while remaining > 0:
                    chunk = resp.read(min(262144, remaining))
                    if not chunk: break
                    self.wfile.write(chunk)
                    if PROBE is not None:                 # head/tail-Bytes lokal cachen (Body wird verworfen)
                        PROBE.put(meta.get("hash"), meta.get("wpath"), abs_pos, chunk, size)
                    abs_pos += len(chunk)
                    remaining -= len(chunk)
                    slot["pos"] += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                alog(slot.get("src", "cdn"), start, length - remaining, size, meta.get("wpath"))
                try: resp.close()
                except Exception: pass
                slot["resp"] = None; slot["pos"] = -1
                self.close_connection = True
                return
            alog(slot.get("src", "cdn"), start, length - remaining, size, meta.get("wpath"))
            slot["last"] = time.time()
            if remaining > 0:                                 # upstream zu kurz -> Stream verwerfen
                try: resp.close()
                except Exception: pass
                slot["resp"] = None; slot["pos"] = -1
                self.close_connection = True
    def log_message(self, *a): pass

if __name__ == "__main__":
    if LAZY:
        from catalog import Catalog
        from probecache import ProbeCache
        os.makedirs(os.path.dirname(CATALOG_DB) or ".", exist_ok=True)
        CAT = Catalog(CATALOG_DB)
        PROBE = ProbeCache(PROBE_DIR, int(PROBE_HEAD_MB * 1024 * 1024), int(PROBE_TAIL_MB * 1024 * 1024),
                           max_bytes=int(PROBE_MAX_MB * 1024 * 1024))
        log.info(f"LAZY-Modus AKTIV — Katalog: {CATALOG_DB} ({CAT.count()} Eintraege), "
                 f"Probe-Cache: {PROBE_DIR} (head={PROBE_HEAD_MB}MB tail={PROBE_TAIL_MB}MB "
                 f"max={PROBE_MAX_MB or '∞'}MB)")
    log.info("Initiale Account-Auflistung ...")
    build_tree()
    if LAZY:                                   # initialer Cache-Check, dann periodisch
        tot, nc = cache_check_pass()
        log.info(f"Cache-Monitor (initial): {nc}/{tot} Hashes gecacht")
        lazy_tree_swap()                       # Listing nach Cache-Check (evicted raus)
        threading.Thread(target=cache_monitor_loop, daemon=True).start()
        if WARM:                               # proaktiv head/tail vorwaermen -> Mount immer scan-warm
            threading.Thread(target=warmer_loop, daemon=True).start()
            log.info("Warmer AKTIV — waermt head/tail aller present Files vor")
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=reaper_loop, daemon=True).start()
    if SWARM:
        threading.Thread(target=swarm_reaper_loop, daemon=True).start()
        log.info(f"Hybrid-Swarm AKTIV — CDN prio1 + WebDAV prio2 (lazy): Failover-Hedge bei Stall "
                 f"(grace=max({SWARM_HEDGE_MIN}s, EWMA*{SWARM_HEDGE_K})) + Baseline-Combine-Test bei "
                 f"CDN-Deviation>{SWARM_DEVIATION}x fuer {SWARM_DEGRADE_PERSIST}s (Test {SWARM_TEST_WINDOW}s, "
                 f"Cooldown {SWARM_COOLDOWN}->{SWARM_COOLDOWN_MAX}s Backoff); "
                 f"Fenster {SWARM_WINDOW}x{SWARM_SEG // 1048576}MiB, max {SWARM_MAX} Streams")
    log.info(f"torbox-webdav laeuft auf :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
