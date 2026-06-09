#!/usr/bin/env python3
# TDD-Test — Schritt 4: Materialize-on-Read + Re-Add-by-Hash.
#  - fid_for_wpath(): mappt nativen Pfad -> frische file-id nach Re-Add.
#  - Catalog.update_location(): persistiert frische tid/fid/materialized_at.
#  - materialize(): createtorrent(add_only_if_cached) -> mylist -> meta+Katalog aktualisiert;
#    not-cached -> False + cached=0. (API gemockt.)
import os, tempfile, sys, time
os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app
from catalog import Catalog

def test_fid_for_wpath():
    files = [{"id": 6, "name": "Rel/a.mkv"}, {"id": 9, "name": "Rel/sub/b.mkv"}]
    assert app.fid_for_wpath(files, "Rel/sub/b.mkv") == 9, "exakter wpath-match"
    assert app.fid_for_wpath(files, "Rel/a.mkv") == 6
    assert app.fid_for_wpath(files, "nope") is None, "kein match -> None"
    assert app.fid_for_wpath(None, "x") is None, "None-files defensiv"
    print("OK: fid_for_wpath")

def test_update_location():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db)
        cat.upsert("H", "Rel/a.mkv", "a.mkv", 10, "video/mp4", "torrents", "Rel", 1, 1)
        cat.update_location("H", "Rel/a.mkv", 555, 77, 123.0)
        r = cat.get("H", "Rel/a.mkv")
        assert r["tid"] == 555 and r["fid"] == 77 and r["materialized_at"] == 123.0, "frische location"
    finally:
        if os.path.exists(db): os.remove(db)
    print("OK: update_location")

def test_materialize_success():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db); app.CAT = cat
        cat.upsert("HH", "Rel/x.mkv", "x.mkv", 10, "video/mp4", "torrents", "Rel", 111, 1)
        meta = {"hash": "HH", "wpath": "Rel/x.mkv", "type": "torrents", "tid": 111, "fid": 1, "size": 10}
        app.api_post_create = lambda magnet: {"success": True, "data": {"torrent_id": 777}}
        app.api_get = lambda path: {"data": {"files": [{"id": 99, "name": "Rel/x.mkv"}]}}
        ok = app.materialize(meta)
        assert ok is True, "materialize erfolgreich"
        assert meta["tid"] == 777 and meta["fid"] == 99, "meta auf frische tid/fid"
        r = cat.get("HH", "Rel/x.mkv")
        assert r["tid"] == 777 and r["fid"] == 99 and r["materialized_at"] > 0, "Katalog persistiert"
    finally:
        app.CAT = None
        if os.path.exists(db): os.remove(db)
    print("OK: materialize success -> meta+Katalog aktualisiert")

def test_materialize_not_cached():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db); app.CAT = cat
        cat.upsert("DEAD", "Rel/d.mkv", "d.mkv", 10, "video/mp4", "torrents", "Rel", 1, 1)
        meta = {"hash": "DEAD", "wpath": "Rel/d.mkv", "type": "torrents", "tid": 1, "fid": 1}
        app.api_post_create = lambda magnet: {"success": False, "_http": 200, "detail": "Torrent not cached."}
        ok = app.materialize(meta)
        assert ok is False, "not-cached -> False"
        # FIX A: materialize evictet/droppt NICHT mehr selbst — das macht nur cache_check_pass (definitiv).
        assert cat.get("DEAD", "Rel/d.mkv")["cached"] == 1, "materialize evictet NICHT (bleibt cached=1)"
    finally:
        app.CAT = None
        if os.path.exists(db): os.remove(db)
    print("OK: materialize not-cached -> False, evictet NICHT (cache_check-Job)")

def test_materialize_network_fail():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db); app.CAT = cat
        cat.upsert("NET", "Rel/n.mkv", "n.mkv", 10, "video/mp4", "torrents", "Rel", 1, 1)
        meta = {"hash": "NET", "wpath": "Rel/n.mkv", "type": "torrents", "tid": 1, "fid": 1}
        app.api_post_create = lambda magnet: {"success": False, "_http": 0, "detail": ""}  # curl rc!=0
        ok = app.materialize(meta)
        assert ok is False, "netzwerk-fail -> False"
        assert cat.get("NET", "Rel/n.mkv")["cached"] == 1, "Netzwerk-Fehler ist KEINE Eviction (bleibt cached=1)"
    finally:
        app.CAT = None
        if os.path.exists(db): os.remove(db)
    print("OK: materialize network-fail -> False, NICHT evicted (kein Probe-Drop)")

def test_in_probe_window():
    app.PROBE_HEAD_MB = 24; app.PROBE_TAIL_MB = 8
    MB = 1024 * 1024; size = 1000 * MB
    assert app.in_probe_window(0, 1 * MB, size) is True, "head-Anfang = Scan"
    assert app.in_probe_window(23 * MB, 1 * MB, size) is True, "head-Ende (<=24MB) = Scan"
    assert app.in_probe_window(24 * MB, 1 * MB, size) is False, "knapp jenseits head = Body/Playback"
    assert app.in_probe_window(size - 8 * MB, 8 * MB, size) is True, "tail = Scan"
    assert app.in_probe_window(size - 9 * MB, 1 * MB, size) is False, "vor tail = Body"
    assert app.in_probe_window(500 * MB, 1 * MB, size) is False, "Mitte = Playback -> Re-Add erlaubt"
    assert app.in_probe_window(0, 5 * MB, 5 * MB) is True, "kleines File komplett im head-Fenster"
    print("OK: in_probe_window — Scan-Window vs Body korrekt")

def test_materialize_rate_limited():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db); app.CAT = cat
        cat.upsert("RL", "Rel/r.mkv", "r.mkv", 10, "video/mp4", "torrents", "Rel", 1, 1)
        meta = {"hash": "RL", "wpath": "Rel/r.mkv", "type": "torrents", "tid": 1, "fid": 1}
        app.api_post_create = lambda magnet: {"success": False, "_http": 429, "detail": "60 per 1 hour"}
        ok = app.materialize(meta)
        assert ok is False, "rate-limited -> False"
        assert cat.get("RL", "Rel/r.mkv")["cached"] == 1, "rate-limit darf NICHT evicten (bleibt cached=1)"
    finally:
        app.CAT = None
        if os.path.exists(db): os.remove(db)
    print("OK: materialize rate-limited -> False, NICHT evicted")

if __name__ == "__main__":
    test_fid_for_wpath()
    test_update_location()
    test_materialize_success()
    test_materialize_not_cached()
    test_materialize_network_fail()
    test_materialize_rate_limited()
    test_in_probe_window()
