#!/usr/bin/env python3
# TDD-Test — do_DELETE Per-File-Emulation (Catalog-Schicht):
#   Datei-Delete blendet die Datei aus dem Mount aus; erst wenn die LETZTE gelistete
#   Datei eines Torrents gelöscht ist, ist der Torrent bei TorBox löschbar.
import os, tempfile, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog import Catalog

def main():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db)
        # Ein Torrent (hash h1, tid 5, type torrents) mit 2 Video-Files.
        cat.upsert("h1", "Show/ep1.mkv", "ep1.mkv", 100, "video/x-matroska", "torrents", "Show", 5, 1)
        cat.upsert("h1", "Show/ep2.mkv", "ep2.mkv", 100, "video/x-matroska", "torrents", "Show", 5, 2)
        listed = lambda: {r["wpath"] for r in cat.all_listed()}
        assert listed() == {"Show/ep1.mkv", "Show/ep2.mkv"}, "beide Files gelistet"

        # Datei 1 löschen -> ausgeblendet; Torrent NICHT komplett weg (ep2 bleibt).
        gone = cat.mark_deleted("h1", "Show/ep1.mkv")
        assert gone is False, "ep2 bleibt -> Torrent noch nicht löschbar"
        assert listed() == {"Show/ep2.mkv"}, "ep1 ist aus dem Listing ausgeblendet"

        # Datei 2 löschen -> jetzt alle weg -> Torrent löschbar.
        gone = cat.mark_deleted("h1", "Show/ep2.mkv")
        assert gone is True, "letzte Datei weg -> Torrent löschbar"
        assert listed() == set(), "nichts mehr gelistet"

        # item_for_hash liefert (type, tid) für den TorBox-Delete-Call (Rows bleiben, nur deleted=1).
        assert cat.item_for_hash("h1") == ("torrents", 5), "type+tid für Delete-API"

        print("OK: mark_deleted blendet aus + signalisiert torrent-deletable + item_for_hash")
    finally:
        if os.path.exists(db):
            os.remove(db)

if __name__ == "__main__":
    main()
