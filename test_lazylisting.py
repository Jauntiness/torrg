#!/usr/bin/env python3
# TDD-Test — Schritt 3: tree_from_catalog() baut die TREE-Struktur aus der Katalog-DB
# (nur cached=1), account-unabhaengig. Evicted (cached=0) erscheint NICHT.
import os, tempfile, sys
os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog import Catalog
from app import tree_from_catalog

VIDEO = ("video/x-matroska", "video/mp4")

def main():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db)
        # torrents-Item, 2 Files (eines davon Namens-Kollision gleicher basename)
        cat.index_item({"id": 10, "hash": "AAA", "name": "Show.S01", "files": [
            {"id": 1, "name": "Show.S01/ep.mkv", "short_name": "ep.mkv", "size": 100, "mimetype": "video/x-matroska"},
            {"id": 2, "name": "Show.S01/sub/ep.mkv", "short_name": "ep.mkv", "size": 200, "mimetype": "video/mp4"},
        ]}, VIDEO, "torrents")
        # usenet-Item, 1 File
        cat.index_item({"id": 20, "hash": "BBB", "name": "Movie.2024", "files": [
            {"id": 5, "name": "Movie.2024/m.mkv", "short_name": "m.mkv", "size": 999, "mimetype": "video/x-matroska"},
        ]}, VIDEO, "usenet")
        # evicted-Item
        cat.index_item({"id": 30, "hash": "CCC", "name": "Gone.2024", "files": [
            {"id": 9, "name": "Gone.2024/g.mkv", "short_name": "g.mkv", "size": 1, "mimetype": "video/mp4"},
        ]}, VIDEO, "webdl")
        cat.set_cached("CCC", False, 1.0)                 # nicht global cached
        cat.sync_present({"AAA", "BBB"})                  # CCC auch nicht mehr im Account -> ganz weg

        tree = tree_from_catalog(cat)   # verschachtelt: {kategorie: {release: {fname: meta}}}

        # evicted nirgends; nur cached=1; Kategorien movies/shows
        all_releases = {rel for cat_ in tree.values() for rel in cat_}
        assert "Gone.2024" not in all_releases, "evicted (cached=0) NICHT im Listing"
        assert all_releases == {"Show.S01", "Movie.2024"}, f"nur cached=1, got {all_releases}"
        assert "shows" in tree and "Show.S01" in tree["shows"], "Show.S01 -> shows (S01)"
        assert "movies" in tree and "Movie.2024" in tree["movies"], "Movie.2024 -> movies (Jahr)"

        # Movie: meta-Felder + idkey-Ableitung
        m = tree["movies"]["Movie.2024"]["m.mkv"]
        assert m["type"] == "usenet" and m["idkey"] == "usenet_id", "usenet idkey"
        assert m["tid"] == 20 and m["fid"] == 5 and m["size"] == 999, "tid/fid/size"
        assert m["wpath"] == "Movie.2024/m.mkv", "wpath = nativer Pfad"

        # Show: 2 Files mit gleichem basename -> Kollision aufgeloest (fid angehaengt)
        show = tree["shows"]["Show.S01"]
        assert len(show) == 2, f"beide Files gelistet trotz gleichem basename, got {list(show)}"
        assert "ep.mkv" in show, "erstes behaelt basename"
        assert any(k != "ep.mkv" and k.endswith(".mkv") for k in show), "zweites disambiguiert"
        assert show["ep.mkv"]["idkey"] == "torrent_id", "torrents idkey"

        print("OK: tree_from_catalog nested(kategorie) + cached-only + meta + idkey + kollision")
    finally:
        if os.path.exists(db): os.remove(db)

if __name__ == "__main__":
    main()
