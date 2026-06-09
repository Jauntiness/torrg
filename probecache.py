#!/usr/bin/env python3
# Probe-Cache: speichert pro (hash,wpath) NUR die Bytes, die in ein head/tail-Fenster fallen
# (das, was Plex-Scans lesen). Treffer werden lokal ausgeliefert -> KEIN CDN-/Materialize-Call,
# kein createtorrent (60/h-Limit!). Body-Bytes werden bewusst NICHT gecacht (Disk-Schutz).
# Range-Tracking via Intervall-Liste (reihenfolge-unabhaengig korrekt), persistiert als JSON-Sidecar.
#
# Schritt 6: drop(hash,wpath) bei Eviction (Cache nicht mit toten Files wachsen lassen) +
# max_bytes-Budget (Disk-Schutz: LRU-Evict, sonst put=no-op; WARNUNG, NIE Crash).
import os, json, threading, hashlib, logging

log = logging.getLogger("torbox-webdav")

class ProbeCache:
    def __init__(self, dir, head_bytes, tail_bytes, max_bytes=0):
        self.dir = dir
        self.head = int(head_bytes)
        self.tail = int(tail_bytes)
        self.max_bytes = int(max_bytes or 0)          # 0 = unbegrenzt
        self._lock = threading.Lock()
        self._locks = {}
        self._sizes = {}                              # key -> gespeicherte Bytes (Summe der Ranges)
        self._warned = False
        os.makedirs(dir, exist_ok=True)
        self._scan_existing()

    def _scan_existing(self):
        try:
            names = os.listdir(self.dir)
        except Exception:
            return
        for n in names:
            if not n.endswith(".json"):
                continue
            key = n[:-5]
            meta = self._load_meta(os.path.join(self.dir, n))
            self._sizes[key] = sum(e - s for s, e in meta.get("ranges", []))

    def _key(self, hash, wpath):
        return hashlib.sha1(f"{hash}\x00{wpath}".encode()).hexdigest()

    def _klock(self, key):
        with self._lock:
            l = self._locks.get(key)
            if l is None:
                l = self._locks[key] = threading.Lock()
            return l

    def _paths(self, key):
        return os.path.join(self.dir, key + ".blob"), os.path.join(self.dir, key + ".json")

    def _windows(self, size):
        w = [(0, min(self.head, size))]
        if self.tail and size > self.tail:
            w.append((max(0, size - self.tail), size))
        return [(s, e) for s, e in w if s < e]

    @staticmethod
    def _load_meta(mp):
        try:
            with open(mp) as f:
                return json.load(f)
        except Exception:
            return {"ranges": []}

    @staticmethod
    def _covered(ranges, start, end):
        for s, e in ranges:
            if s <= start and end <= e:
                return True
        return False

    @staticmethod
    def _merge(ranges):
        out = []
        for s, e in sorted(ranges):
            if out and s <= out[-1][1]:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return out

    def _total(self):
        return sum(self._sizes.values())

    def _delete_files(self, key):
        bp, mp = self._paths(key)
        for p in (bp, mp):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except Exception as e:
                log.warning(f"probe rm {p}: {e}")

    def drop(self, hash, wpath):
        """Probe-Blob+Sidecar fuer (hash,wpath) loeschen (bei Eviction). Idempotent, wirft nie."""
        try:
            key = self._key(hash, wpath)
            with self._klock(key):
                self._delete_files(key)
                self._sizes.pop(key, None)
        except Exception as e:
            log.warning(f"probe drop: {e}")

    def _evict_lru(self, need, keep_key):
        """Aelteste Probes (nach Blob-mtime) entfernen, bis total+need <= max_bytes. keep_key bleibt."""
        candidates = [k for k in list(self._sizes) if k != keep_key]
        def mtime(k):
            try:
                return os.path.getmtime(self._paths(k)[0])
            except Exception:
                return 0.0
        candidates.sort(key=mtime)                    # aelteste zuerst
        for k in candidates:
            if self._total() + need <= self.max_bytes:
                break
            self._delete_files(k)
            self._sizes.pop(k, None)
            log.info(f"probe LRU-evict {k[:12]} (Budget {self.max_bytes//1024//1024}MB)")

    def get(self, hash, wpath, start, length):
        """Bytes wenn [start,start+length) vollstaendig gecacht, sonst None."""
        if length <= 0:
            return None
        key = self._key(hash, wpath)
        bp, mp = self._paths(key)
        end = start + length
        with self._klock(key):
            meta = self._load_meta(mp)
            if not self._covered(meta.get("ranges", []), start, end):
                return None
            try:
                with open(bp, "rb") as f:
                    f.seek(start)
                    data = f.read(length)
                return data if len(data) == length else None
            except Exception:
                return None

    def put(self, hash, wpath, start, data, size):
        """Speichert nur die Teil-Bytes von [start,start+len(data)), die in ein head/tail-Fenster
        fallen. Respektiert max_bytes (LRU-Evict, sonst no-op + WARNUNG). Wirft NIE."""
        try:
            if not data:
                return
            end = start + len(data)
            subs = []
            for ws, we in self._windows(size):
                s = max(start, ws)
                e = min(end, we)
                if s < e:
                    subs.append((s, e))
            if not subs:
                return
            key = self._key(hash, wpath)
            bp, mp = self._paths(key)
            with self._klock(key):
                add = sum(e - s for s, e in subs)     # Obergrenze neuer Bytes
                if self.max_bytes:
                    have = self._sizes.get(key, 0)
                    if self._total() + add > self.max_bytes:
                        self._evict_lru(add, keep_key=key)
                    # immer noch kein Platz und Datei ist neu -> no-op (Disk-Schutz vor Trefferrate)
                    if self._total() + add > self.max_bytes and have == 0:
                        if not self._warned:
                            log.warning(f"Probe-Cache am Budget ({self.max_bytes//1024//1024}MB) "
                                        f"-> neue Probes werden uebersprungen (Server laeuft weiter)")
                            self._warned = True
                        return
                if not os.path.exists(bp):
                    open(bp, "wb").close()
                with open(bp, "r+b") as f:
                    for s, e in subs:
                        f.seek(s)
                        f.write(data[s - start:e - start])
                meta = self._load_meta(mp)
                ranges = meta.get("ranges", [])
                ranges.extend([list(x) for x in subs])
                meta["ranges"] = self._merge(ranges)
                meta["size"] = size
                tmp = mp + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(meta, f)
                os.replace(tmp, mp)
                self._sizes[key] = sum(e - s for s, e in meta["ranges"])
        except Exception as e:
            log.warning(f"probe put: {e}")
