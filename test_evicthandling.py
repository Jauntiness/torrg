#!/usr/bin/env python3
# TDD-Test — Schritt 6: Evicted-Handling end-to-end (echte App-Wiring).
#  - cache_check_pass evicted einen Hash -> aus Listing + Probe-Blobs gedroppt.
#  - resolve() auf evicted = None (-> GET 404).
#  - Re-Cache (checkcached wieder 1) -> Datei taucht via lazy_tree_swap wieder auf.
import os, tempfile, shutil, sys, time
os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app
from catalog import Catalog
from probecache import ProbeCache

def main():
    db = tempfile.mktemp(suffix=".db")
    pdir = tempfile.mkdtemp()
    try:
        cat = Catalog(db); app.CAT = cat
        probe = ProbeCache(pdir, 16 * 1024 * 1024, 2 * 1024 * 1024); app.PROBE = probe
        H, W = "HH", "Rel/x.mkv"
        cat.index_item({"id": 1, "hash": H, "name": "Rel", "files": [
            {"id": 1, "name": W, "short_name": "x.mkv", "size": 1000, "mimetype": "video/mp4"}]},
            app.VIDEO_MIME, "torrents")
        probe.put(H, W, 0, b"A" * 4096, 1000000)         # Probe vorhanden
        assert probe.get(H, W, 0, 4096) == b"A" * 4096, "Probe initial da"

        P = "/movies/Rel/x.mkv"   # classify("Rel",["x.mkv"]) -> movies (.mkv)
        app.lazy_tree_swap()
        assert app.walk(P)[0] == "file", "vor Evict gelistet"

        # ECHTES weg: File verlaesst Account (sync_present ohne ihn) UND ist nicht global cached
        cat.sync_present(set())                          # build_tree sieht ihn nicht mehr -> present=0
        app.api_get = lambda path: {"data": {}}          # checkcached -> nicht (global) cached
        tot, nc = app.cache_check_pass()
        assert nc == 0 and cat.get_cached(H) == 0, "global-evicted"
        assert probe.get(H, W, 0, 4096) is None, "Probe gedroppt (present=0 UND cached=0)"
        assert os.listdir(pdir) == [], "Probe-Dateien physisch weg"
        app.lazy_tree_swap()
        assert app.walk(P)[0] is None, "weg (present=0,cached=0) -> GET 404"

        # Re-Cache: checkcached liefert den Hash wieder -> wieder cached -> wieder gelistet
        app.api_get = lambda path: {"data": {H: {"name": "Rel", "size": 1000, "hash": H}}}
        tot, nc = app.cache_check_pass()
        assert nc == 1 and cat.get_cached(H) == 1, "re-cached erkannt"
        app.lazy_tree_swap()
        assert app.walk(P)[0] == "file", "Datei taucht nach Re-Cache wieder auf"
        print("OK: evict -> aus Listing + Probe gedroppt + 404; re-cache -> wieder gelistet")
    finally:
        app.CAT = None; app.PROBE = None
        if os.path.exists(db): os.remove(db)
        shutil.rmtree(pdir, ignore_errors=True)

if __name__ == "__main__":
    main()
