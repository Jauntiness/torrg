#!/usr/bin/env python3
# TDD-Test — present-Fix: uncached-aber-vorhandene Files bleiben gemountet (present), werden NICHT
# fuer Keepalive/Materialize beachtet, und verschwinden erst bei echtem Expiry (present=0 & cached=0).
import os, tempfile, shutil, sys
os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app
from catalog import Catalog
from probecache import ProbeCache

def test_all_listed_present_or_cached():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db)
        cat.upsert("CACHED", "a/x.mkv", "x.mkv", 1, "video/mp4", "torrents", "a", 1, 1)
        cat.upsert("UNCACHED", "b/y.mkv", "y.mkv", 1, "video/mp4", "torrents", "b", 2, 2)
        cat.set_cached("CACHED", True, 1.0)
        cat.set_cached("UNCACHED", False, 1.0)           # nie global cached
        cat.sync_present({"CACHED", "UNCACHED"})         # beide aktuell vorhanden
        listed = {r["hash"] for r in cat.all_listed()}
        assert listed == {"CACHED", "UNCACHED"}, "beide gelistet (present)"
        # UNCACHED verlaesst den Account -> present=0; cached=0 -> weg
        cat.sync_present({"CACHED"})
        listed = {r["hash"] for r in cat.all_listed()}
        assert listed == {"CACHED"}, "uncached weg bei Expiry; cached bleibt (re-materialisierbar)"
        print("OK: all_listed = present OR cached")
    finally:
        if os.path.exists(db): os.remove(db)

def test_uncached_present_stays_and_no_drop():
    db = tempfile.mktemp(suffix=".db"); pdir = tempfile.mkdtemp()
    try:
        cat = Catalog(db); app.CAT = cat
        probe = ProbeCache(pdir, 16*1024*1024, 2*1024*1024); app.PROBE = probe
        H, W = "U", "Rel/u.mkv"
        cat.upsert(H, W, "u.mkv", 1000, "video/mp4", "torrents", "Rel", 1, 1)
        cat.sync_present({H})                            # vorhanden
        probe.put(H, W, 0, b"A"*4096, 1000000)
        # checkcached sagt: NICHT global cached (uncached File)
        app.api_get = lambda path: {"data": {}}
        app.cache_check_pass()
        assert cat.get_cached(H) == 0, "uncached -> cached=0"
        assert cat.get_present(H) == 1, "aber present bleibt 1"
        assert probe.get(H, W, 0, 4096) == b"A"*4096, "Probe NICHT gedroppt solange present"
        app.lazy_tree_swap()
        assert app.walk("/movies/Rel/u.mkv")[0] == "file", "uncached-present bleibt gemountet"
        print("OK: uncached-present bleibt gelistet + Probe nicht gedroppt")
    finally:
        app.CAT = None; app.PROBE = None
        if os.path.exists(db): os.remove(db)
        shutil.rmtree(pdir, ignore_errors=True)

if __name__ == "__main__":
    test_all_listed_present_or_cached()
    test_uncached_present_stays_and_no_drop()
