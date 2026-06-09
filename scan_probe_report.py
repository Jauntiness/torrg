#!/usr/bin/env python3
# Scan-Probe-Report: analysiert das ACCESS_LOG des torbox-webdav-Shims und beweist, welche
# Byte-Teile eines Files ein Plex-Scan anfasst — und ob unser Probe-Cache (head/tail) ausreicht,
# damit Folgescans KEINEN aktiven Download (CDN/native) mehr ausloesen.
#
# Workflow:
#   1. Shim mit LAZY=1 + ACCESS_LOG=/data/access.log laufen lassen (Plex liest darueber).
#   2. Plex-Scan #1 starten (kalt: fuellt Probe). Dann Scan #2 (warm). Optional #3.
#   3. python3 scan_probe_report.py /pfad/access.log
# Der Report clustert die Reads automatisch in Scan-Sessions (Zeitluecken) und zeigt pro Session
# je File: Reads, Bytes, Quelle (probe=lokal / cdn+native=aktiv geladen), ob alles in die
# head/tail-Fenster faellt, und ein Urteil. Beweis = Folgescans haben download_bytes == 0.
import sys, json, argparse

def within_windows(start, end, size, head, tail):
    """Liegt [start,end) komplett im head-Fenster [0,head) ODER tail-Fenster [size-tail,size)?"""
    in_head = end <= min(head, size)
    in_tail = bool(tail) and size > tail and start >= max(0, size - tail)
    return in_head or in_tail

def merge(ranges):
    out = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out

def parse(path):
    recs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    recs.sort(key=lambda r: r.get("t", 0))
    return recs

def sessionize(recs, gap=120):
    """Reads in Scan-Sessions clustern: neue Session wenn Zeitluecke > gap Sekunden."""
    sessions, cur, last = [], [], None
    for r in recs:
        t = r.get("t", 0)
        if last is not None and t - last > gap:
            sessions.append(cur); cur = []
        cur.append(r); last = t
    if cur:
        sessions.append(cur)
    return sessions

def analyze_session(recs, head, tail):
    """Pro wpath: reads, bytes, by_src, gemergte Ranges, all_in_window, download_bytes."""
    files = {}
    for r in recs:
        w = r.get("wpath") or "?"
        f = files.setdefault(w, {"reads": 0, "bytes": 0, "by_src": {}, "ranges": [],
                                 "size": r.get("size", 0), "all_in_window": True})
        s, e = r.get("start", 0), r.get("end", 0)
        src = r.get("src", "?")
        f["reads"] += 1
        f["bytes"] += r.get("len", e - s)
        f["by_src"][src] = f["by_src"].get(src, 0) + r.get("len", e - s)
        f["ranges"].append([s, e])
        f["size"] = r.get("size", f["size"])
        if not within_windows(s, e, f["size"], head, tail):
            f["all_in_window"] = False
    for f in files.values():
        f["ranges"] = merge(f["ranges"])
        f["download_bytes"] = sum(b for s, b in f["by_src"].items() if s in ("cdn", "native"))
        f["max_off"] = max((e for _, e in f["ranges"]), default=0)
    return files

def _mb(n): return f"{n/1024/1024:.2f}MB"

def main():
    ap = argparse.ArgumentParser(description="Scan-Probe-Report fuer torbox-webdav ACCESS_LOG")
    ap.add_argument("logfile")
    ap.add_argument("--head-mb", type=float, default=16.0)
    ap.add_argument("--tail-mb", type=float, default=2.0)
    ap.add_argument("--gap", type=float, default=120.0, help="Session-Trenn-Zeitluecke in s")
    a = ap.parse_args()
    head = int(a.head_mb * 1024 * 1024); tail = int(a.tail_mb * 1024 * 1024)
    recs = parse(a.logfile)
    if not recs:
        print("Keine Access-Log-Eintraege gefunden."); return
    sessions = sessionize(recs, a.gap)
    print(f"Access-Log: {len(recs)} Reads in {len(sessions)} Scan-Session(s). "
          f"Probe-Fenster: head={a.head_mb}MB tail={a.tail_mb}MB\n")
    seen_dl = {}
    for i, sess in enumerate(sessions, 1):
        stats = analyze_session(sess, head, tail)
        t0 = sess[0].get("t", 0)
        tot_reads = sum(f["reads"] for f in stats.values())
        tot_dl = sum(f["download_bytes"] for f in stats.values())
        dl_files = [w for w, f in stats.items() if f["download_bytes"] > 0]
        oob = [w for w, f in stats.items() if not f["all_in_window"]]
        print(f"━━ Session #{i}  (t0={t0}, {len(stats)} Files, {tot_reads} Reads) ━━")
        print(f"   aktiv geladen (cdn+native): {_mb(tot_dl)} ueber {len(dl_files)} File(s)")
        print(f"   ausserhalb head/tail-Fenster: {len(oob)} File(s)" +
              (f" -> {', '.join(w[:40] for w in oob[:4])}" if oob else ""))
        for w, f in sorted(stats.items(), key=lambda kv: -kv[1]["download_bytes"])[:8]:
            srcs = " ".join(f"{s}={_mb(b)}" for s, b in sorted(f["by_src"].items()))
            flag = "OK(Fenster)" if f["all_in_window"] else "!!Body/ausserhalb"
            print(f"     {w[-46:]:46}  reads={f['reads']:>3} {flag:18} "
                  f"dl={_mb(f['download_bytes'])}  [{srcs}]  maxoff={_mb(f['max_off'])}/{_mb(f['size'])}")
        # Vergleich zu Vorsessions: wurde ein File jetzt probe-frei?
        if i > 1:
            became_free = [w for w in seen_dl if stats.get(w, {}).get("download_bytes", 0) == 0]
            still_dl = [w for w in stats if stats[w]["download_bytes"] > 0]
            print(f"   ggue. Vorsession: {len(became_free)} File(s) jetzt download-frei; "
                  f"{len(still_dl)} File(s) laden weiter aktiv")
        seen_dl = {w: f["download_bytes"] for w, f in stats.items()}
        print()
    print("BEWEIS-Kriterium: ab Session #2 sollte 'aktiv geladen' gegen 0 gehen (alle Scan-Reads aus "
          "dem Probe-Cache). Files mit '!!Body/ausserhalb' brauchen groessere head/tail-Fenster ODER "
          "loesen bei jedem Scan einen Download aus.")

if __name__ == "__main__":
    main()
