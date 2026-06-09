#!/usr/bin/env python3
# Katalog-DB (SQLite) fuer den LAZY-Modus: persistente Liste der TorBox-Library, ueberlebt
# Account-Expiry. Pro (hash,wpath): cached-Flag, last_check, materialized_at.
import sqlite3, threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    hash            TEXT NOT NULL,
    wpath           TEXT NOT NULL,           -- native-WebDAV-Pfad (f["name"])
    fname           TEXT,                    -- Anzeigename (basename)
    size            INTEGER,
    mime            TEXT,
    type            TEXT,                    -- torrents|usenet|webdl (fuer idkey + Re-Add)
    folder          TEXT,                    -- TREE-Ordnername (sanitized item-name), Plex-Pfad-stabil
    tid             INTEGER,                 -- letzte bekannte Account-item-id (geht bei Expiry stale)
    fid             INTEGER,                 -- letzte bekannte file-id im item
    cached          INTEGER DEFAULT 1,       -- 1=GLOBAL gecacht (checkcached) -> re-materialisierbar
    present         INTEGER DEFAULT 1,       -- 1=aktuell im Account vorhanden (mylist) -> mountbar
    deleted         INTEGER DEFAULT 0,       -- 1=vom User via WebDAV-DELETE gelöscht -> aus Mount ausgeblendet
    last_check      REAL    DEFAULT 0,       -- letzter checkcached-Zeitpunkt
    materialized_at REAL    DEFAULT 0,       -- letzter Re-Add-Zeitpunkt
    PRIMARY KEY (hash, wpath)
);
CREATE INDEX IF NOT EXISTS idx_hash ON files(hash);
"""
# Spalten, die spaeter dazukamen -> idempotent nachziehen (ALTER ADD COLUMN bei bestehender DB).
_MIGRATE = ["type TEXT", "folder TEXT", "tid INTEGER", "fid INTEGER", "present INTEGER DEFAULT 1",
            "deleted INTEGER DEFAULT 0"]

class Catalog:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        c = self._conn()
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(files)")}
        for col in _MIGRATE:
            name = col.split()[0]
            if name not in have:
                c.execute(f"ALTER TABLE files ADD COLUMN {col}")
        c.commit()

    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.c = c
        return c

    def upsert(self, hash, wpath, fname, size, mime, type=None, folder=None, tid=None, fid=None):
        c = self._conn()
        c.execute(
            "INSERT INTO files(hash,wpath,fname,size,mime,type,folder,tid,fid) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(hash,wpath) DO UPDATE SET "
            "fname=excluded.fname, size=excluded.size, mime=excluded.mime, "
            "type=excluded.type, folder=excluded.folder, tid=excluded.tid, fid=excluded.fid",
            (hash, wpath, fname, size, mime, type, folder, tid, fid))
        c.commit()

    def index_item(self, item, video_mimes, type):
        """Indexiert alle Video-Files eines TorBox-mylist-Items in den Katalog. Gibt Anzahl zurueck.
        type = torrents|usenet|webdl. folder = sanitized item-name (Plex-pfad-stabil)."""
        h = item.get("hash") or ""
        if not h:
            return 0
        folder = (item.get("name") or str(item.get("id"))).strip("/").replace("/", "_")
        tid = item.get("id")
        n = 0
        for f in item.get("files", []):
            if f.get("mimetype") not in video_mimes:
                continue
            wpath = f.get("name") or f.get("short_name") or ""
            if not wpath:
                continue
            fname = (f.get("short_name") or wpath).split("/")[-1]
            self.upsert(h, wpath, fname, int(f.get("size") or 0), f.get("mimetype"),
                        type, folder, tid, f.get("id"))
            n += 1
        return n

    def set_cached(self, hash, cached, ts):
        c = self._conn()
        c.execute("UPDATE files SET cached=?, last_check=? WHERE hash=?", (1 if cached else 0, ts, hash))
        c.commit()

    def update_location(self, hash, wpath, tid, fid, ts):
        """Frische Account-Location nach Re-Add (Materialize) persistieren."""
        c = self._conn()
        c.execute("UPDATE files SET tid=?, fid=?, materialized_at=? WHERE hash=? AND wpath=?",
                  (tid, fid, ts, hash, wpath))
        c.commit()

    def get(self, hash, wpath):
        return self._conn().execute(
            "SELECT * FROM files WHERE hash=? AND wpath=?", (hash, wpath)).fetchone()

    def count(self, cached_only=False):
        q = "SELECT COUNT(*) FROM files" + (" WHERE cached=1" if cached_only else "")
        return self._conn().execute(q).fetchone()[0]

    def all_cached(self):
        return self._conn().execute("SELECT * FROM files WHERE cached=1").fetchall()

    def all_listed(self):
        """Im Mount sichtbar: aktuell vorhanden (present) ODER global re-materialisierbar (cached),
        und NICHT vom User gelöscht (deleted)."""
        return self._conn().execute(
            "SELECT * FROM files WHERE (present=1 OR cached=1) AND COALESCE(deleted,0)=0").fetchall()

    def mark_deleted(self, hash, wpath):
        """Blendet eine Datei aus dem Mount aus (WebDAV-DELETE). Gibt True zurück, wenn danach
        KEINE gelistete Datei dieses Torrents mehr übrig ist — dann ist der TorBox-Torrent löschbar."""
        c = self._conn()
        c.execute("UPDATE files SET deleted=1 WHERE hash=? AND wpath=?", (hash, wpath))
        c.commit()
        remaining = c.execute(
            "SELECT COUNT(*) FROM files WHERE hash=? AND (present=1 OR cached=1) "
            "AND COALESCE(deleted,0)=0", (hash,)).fetchone()[0]
        return remaining == 0

    def item_for_hash(self, hash):
        """(type, tid) der letzten bekannten Account-Location für den TorBox-Delete-Call;
        None wenn keine bekannt. Rows bleiben nach mark_deleted erhalten (nur deleted=1)."""
        r = self._conn().execute(
            "SELECT type, tid FROM files WHERE hash=? AND tid IS NOT NULL "
            "ORDER BY materialized_at DESC LIMIT 1", (hash,)).fetchone()
        return (r["type"], r["tid"]) if r else None

    def sync_present(self, present_hashes):
        """present=1 fuer die uebergebenen Hashes (aktuell in der mylist), present=0 fuer alle anderen."""
        c = self._conn()
        c.execute("UPDATE files SET present=0")
        ph = list(present_hashes)
        if ph:
            qs = ",".join("?" * len(ph))
            c.execute(f"UPDATE files SET present=1 WHERE hash IN ({qs})", tuple(ph))
        c.commit()

    def get_present(self, hash):
        r = self._conn().execute("SELECT MAX(present) FROM files WHERE hash=?", (hash,)).fetchone()
        return None if r is None or r[0] is None else r[0]

    def all_hashes(self):
        return [r[0] for r in self._conn().execute("SELECT DISTINCT hash FROM files")]

    def wpaths_for_hash(self, hash):
        return [r[0] for r in self._conn().execute(
            "SELECT wpath FROM files WHERE hash=?", (hash,))]

    def get_cached(self, hash):
        """1 wenn (irgendeine Datei von) hash cached, 0 wenn evicted, None wenn unbekannt."""
        r = self._conn().execute(
            "SELECT MAX(cached) FROM files WHERE hash=?", (hash,)).fetchone()
        return None if r is None or r[0] is None else r[0]
