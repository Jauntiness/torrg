#!/usr/bin/env python3
# Real-Source-Benchmark (NICHT-disruptiv, laeuft auf dem Host gegen das echte TorBox-Konto):
# vergleicht Single-CDN-Read vs. Dual-Source-Swarm (CDN+WebDAV) fuer dieselben N MiB eines
# echten gecachten Files. Kein Eingriff in den laufenden Container/Mount.
import os, sys, time, json, base64, subprocess, sqlite3, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm import SegmentSwarm

API = "https://api.torbox.app/v1/api"
UA = "Mozilla/5.0 (X11; Linux x86_64) torbox-webdav/1.0"
IDKEY = {"torrents": "torrent_id", "usenet": "usenet_id", "webdl": "web_id"}
SEG = 1024 * 1024
WINDOW = 24
BENCH_MB = int(os.environ.get("BENCH_MB", "64"))     # so viel pro Lauf lesen
START_OFF = 64 * 1024 * 1024                          # weit hinter dem head-Fenster anfangen


def load_env():
    for ln in open(os.path.join(os.path.dirname(__file__), ".env")):
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            os.environ.setdefault(k, v)


def api_get(path):
    out = subprocess.run(["curl", "-sf", "-H", f"Authorization: Bearer {os.environ['TORBOX_API_KEY']}",
                          f"{API}{path}"], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"curl rc={out.returncode}: {out.stderr.decode()[:200]}")
    return json.loads(out.stdout)


def cdn_url(meta):
    url = (f"/{meta['type']}/requestdl?token={os.environ['TORBOX_API_KEY']}"
           f"&{meta['idkey']}={meta['tid']}&file_id={meta['fid']}&redirect=false")
    cdn = api_get(url).get("data")
    if not cdn:
        raise RuntimeError("keine CDN-URL")
    return cdn


def opener_cdn(url):
    def open(off):
        return urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": UA, "Range": f"bytes={off}-"}), timeout=15)
    return open


def opener_native(wpath, auth):
    from urllib.parse import quote
    base = os.environ.get("TORBOX_WEBDAV_URL", "https://webdav.torbox.app")

    def open(off):
        u = base + quote("/" + wpath)
        return urllib.request.urlopen(urllib.request.Request(
            u, headers={"User-Agent": UA, "Authorization": auth,
                        "Range": f"bytes={off}-"}), timeout=30)
    return open


def pick_file():
    c = sqlite3.connect(os.path.join(os.path.dirname(__file__), "data/catalog.db"))
    row = c.execute("SELECT type,tid,fid,size,wpath FROM files "
                    "WHERE present=1 AND cached=1 AND deleted=0 AND size>?"
                    " ORDER BY size LIMIT 1", (START_OFF + BENCH_MB * SEG + SEG,)).fetchone()
    if not row:
        sys.exit("kein passendes File im Katalog")
    t, tid, fid, size, wpath = row
    return {"type": t, "idkey": IDKEY[t], "tid": tid, "fid": fid, "size": size, "wpath": wpath}


def bench_single(url, start, total):
    """Single-CDN: eine offene Verbindung, sequentiell lesen (wie der Slot-Pfad)."""
    t0 = time.monotonic()
    r = urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": UA, "Range": f"bytes={start}-"}), timeout=15)
    got = 0
    while got < total:
        c = r.read(min(SEG, total - got))
        if not c:
            break
        got += len(c)
    r.close()
    return got, time.monotonic() - t0


def bench_swarm(meta, url, start, total):
    cnt = {"cdn": 0, "web": 0}

    def wrap(name, fn):
        def o(off):
            cnt[name] += 1                 # Opens je Quelle (Connection-Reuse messen)
            return fn(off)
        return o
    auth = "Basic " + base64.b64encode(
        f"{os.environ['TORBOX_WEBDAV_USER']}:{os.environ['TORBOX_WEBDAV_PASS']}".encode()).decode()
    srcs = [("cdn", wrap("cdn", opener_cdn(url))),
            ("web", wrap("web", opener_native(meta["wpath"], auth)))]
    sw = SegmentSwarm(meta["size"], SEG, WINDOW, srcs,
                      hedge_k=2.0, cold_grace_s=0.3, hedge_min=0.3)
    t0 = time.monotonic()
    pos = start
    got = 0
    while got < total:
        n = min(SEG, total - got)
        data = sw.read(pos, n, timeout=30)
        if data is None:
            print("  ! swarm read timeout"); break
        got += len(data)
        pos += len(data)
        sw.advance(pos)
    dt = time.monotonic() - t0
    sw.close()
    return got, dt, cnt


def mbps(b, s):
    return (b * 8 / 1e6) / s if s else 0


def main():
    load_env()
    meta = pick_file()
    total = BENCH_MB * SEG
    start = START_OFF
    print(f"File: tid={meta['tid']} fid={meta['fid']} size={meta['size']/1e9:.2f}GB "
          f"wpath={meta['wpath'][:50]}")
    print(f"Lese {BENCH_MB} MiB ab Offset {start//SEG} MiB.\n")
    url = cdn_url(meta)

    got, dt = bench_single(url, start, total)
    print(f"[single CDN ] {got/1e6:6.1f} MB in {dt:5.2f}s = {mbps(got, dt):6.1f} Mbit/s")

    # frische URL fuer den Swarm-Lauf (Fairness; CDN-URL koennte expiren)
    url2 = cdn_url(meta)
    got, dt, cnt = bench_swarm(meta, url2, start, total)
    print(f"[swarm 2-src] {got/1e6:6.1f} MB in {dt:5.2f}s = {mbps(got, dt):6.1f} Mbit/s "
          f"(CDN {cnt['cdn']} opens, WebDAV {cnt['web']} opens)")


if __name__ == "__main__":
    main()
