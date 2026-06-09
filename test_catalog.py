#!/usr/bin/env python3
# TDD-Test — Schritt 0: Katalog-DB-Modul (Schema, upsert, Idempotenz, query).
import os, tempfile, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog import Catalog

def main():
    db = tempfile.mktemp(suffix=".db")
    try:
        cat = Catalog(db)
        cols = {r[1] for r in cat._conn().execute("PRAGMA table_info(files)")}
        need = {"hash", "wpath", "fname", "size", "mime", "cached", "last_check", "materialized_at"}
        assert need <= cols, f"Schema fehlen Spalten: {need - cols}"
        assert cat.count() == 0, "frischer Katalog muss leer sein"
        cat.upsert("abc", "/Folder/f.mkv", "f.mkv", 123, "video/x-matroska")
        assert cat.count() == 1, "nach upsert == 1"
        cat.upsert("abc", "/Folder/f.mkv", "f.mkv", 456, "video/x-matroska")
        assert cat.count() == 1, "upsert darf nicht duplizieren (idempotent auf hash+wpath)"
        row = cat.get("abc", "/Folder/f.mkv")
        assert row and row["size"] == 456, "upsert muss bestehende Zeile aktualisieren"
        print("OK: catalog schema + upsert + idempotenz + update")
    finally:
        if os.path.exists(db): os.remove(db)

if __name__ == "__main__":
    main()
