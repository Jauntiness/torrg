#!/usr/bin/env python3
# TDD-Test — Schritt 1+3: index_item() indexiert nur Video-Files, idempotent, MIT
# type/folder/tid/fid (fuer account-unabhaengige Listing-Rekonstruktion in Schritt 3).
import os, tempfile, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog import Catalog

VIDEO = ("video/x-matroska", "video/mp4")

def main():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db)
        item = {"id": 42, "hash": "H1", "name": "Some.Release/Bad", "files": [
            {"id": 1, "name": "Folder/ep1.mkv", "short_name": "ep1.mkv", "size": 111, "mimetype": "video/x-matroska"},
            {"id": 2, "name": "Folder/ep2.mkv", "short_name": "ep2.mkv", "size": 222, "mimetype": "video/mp4"},
            {"id": 3, "name": "Folder/info.nfo", "short_name": "info.nfo", "size": 1, "mimetype": "text/plain"},
        ]}
        n = cat.index_item(item, VIDEO, "torrents")
        assert n == 2, f"nur Video-Files indexieren, got {n}"
        assert cat.count() == 2, "2 Video-Rows"
        cat.index_item(item, VIDEO, "torrents")           # re-run
        assert cat.count() == 2, "re-index darf nicht duplizieren"
        row = cat.get("H1", "Folder/ep1.mkv")
        assert row and row["fname"] == "ep1.mkv" and row["size"] == 111, "Basis-Felder"
        # NEU Schritt 3: type/folder/tid/fid persistiert
        assert row["type"] == "torrents", "type gespeichert"
        assert row["folder"] == "Some.Release_Bad", "folder = sanitized name (/ -> _)"
        assert row["tid"] == 42, "tid = item id"
        assert row["fid"] == 1, "fid = file id"
        assert len([h for h in cat.all_hashes() if h == "H1"]) == 1, "1 distinct hash"
        print("OK: cataloger video-only + idempotent + type/folder/tid/fid")
    finally:
        if os.path.exists(db): os.remove(db)

if __name__ == "__main__":
    main()
