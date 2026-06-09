#!/usr/bin/env python3
# TDD-Test — Warmer (gebundener Superset): waermt head=PROBE_HEAD_MB + tail=PROBE_TAIL_MB JEDES
# present Files in die Probe; NUR present (kein Materialize), idempotent (skip wenn schon gewaermt).
import os, tempfile, shutil, sys
os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app
from catalog import Catalog
from probecache import ProbeCache

def main():
    db = tempfile.mktemp(suffix=".db"); pdir = tempfile.mkdtemp()
    try:
        cat = Catalog(db); app.CAT = cat
        app.PROBE = ProbeCache(pdir, 8 * 1024 * 1024, 4 * 1024 * 1024)
        app.PROBE_HEAD_MB = 8; app.PROBE_TAIL_MB = 4
        SZ = 50 * 1024 * 1024
        cat.upsert("A", "m/a.mkv", "a.mkv", SZ, "video/mp4", "torrents", "m", 10, 1)
        cat.upsert("B", "m/b.mkv", "b.mkv", SZ, "video/mp4", "torrents", "m", 11, 1)
        cat.upsert("OLD", "m/o.mkv", "o.mkv", SZ, "video/mp4", "torrents", "m", 12, 1)
        cat.upsert("MUS", "m/song.flac", "song.flac", SZ, "audio/flac", "music", "m", 13, 1)
        cat.set_cached("OLD", True, 1.0)
        cat.sync_present({"A", "B", "MUS"})                # OLD = expired (present=0)
        app.MUSIC_HEAD_MB = 4; app.MUSIC_TAIL_MB = 1

        calls = []
        app.warm_fetch = lambda meta, start, length: (calls.append((meta["hash"], start, length)) or b"X" * length)

        # --- Size-Modell (warm_sizes) direkt: Audio klein, Video skaliert mit Groesse, gedeckelt ---
        app.PROBE_HEAD_MB = 24; app.PROBE_TAIL_MB = 8
        app.WARM_MIN_HEAD_MB = 6; app.WARM_HEAD_PER_GB = 0.25
        MB = 1024 * 1024; GB = 1024 ** 3
        assert 6 * MB <= app.warm_sizes("a.mkv", 50 * MB)[0] < int(6.05 * MB), "kleines Video ~Floor 6MB"
        assert app.warm_sizes("a.mkv", 4 * GB)[0] == 7 * MB, "4GB-1080p -> 7MB (deckt 6.5)"
        assert app.warm_sizes("a.mkv", 70 * GB)[0] == int(23.5 * MB), "70GB-REMUX -> 23.5MB (deckt 21)"
        assert app.warm_sizes("a.mkv", 200 * GB)[0] == 24 * MB, "riesig -> gedeckelt aufs Fenster 24MB"
        assert app.warm_sizes("a.mkv", 8 * GB)[0] > app.warm_sizes("a.mkv", 2 * GB)[0], "monoton steigend"
        assert app.warm_sizes("song.flac", 50 * MB) == (4 * MB, 1 * MB), "Audio -> 4MB/1MB"
        app.PROBE_HEAD_MB = 8; app.PROBE_TAIL_MB = 4    # zurueck fuer warm_pass-Integration unten

        n, tot = app.warm_pass()
        assert (n, tot) == (3, 3), f"3 present gewaermt, expired ignoriert; got {(n,tot)}"
        # warm_file nutzt warm_sizes: Video A size-basiert (~Floor), Musik 4MB
        exp_head = app.warm_sizes("m/a.mkv", SZ)[0]
        assert any(h == "A" and l == exp_head for h, s, l in calls), f"Video A head={exp_head}"
        assert any(h == "MUS" and l == 4 * 1024 * 1024 for h, s, l in calls), "Musik: nur 4MB head"
        assert exp_head != 4 * 1024 * 1024, "Video != Musik-Groesse"
        assert app.PROBE.get("A", "m/a.mkv", 0, 1024) == b"X" * 1024, "A head gewaermt"
        assert app.PROBE.get("A", "m/a.mkv", SZ - 1024, 1024) == b"X" * 1024, "A tail gewaermt"
        assert app.PROBE.get("B", "m/b.mkv", 0, 1024) == b"X" * 1024, "B head gewaermt"
        assert all(h != "OLD" for h, _, _ in calls), "expired NICHT gewaermt (kein Materialize)"
        n2, _ = app.warm_pass()
        assert n2 == 0, f"schon gewaermt -> 0 neu; got {n2}"
        print("OK: Warmer waermt present head+tail (gebundener Superset), idempotent, expired-skip")
    finally:
        app.CAT = None; app.PROBE = None
        if os.path.exists(db): os.remove(db)
        shutil.rmtree(pdir, ignore_errors=True)

if __name__ == "__main__":
    main()
