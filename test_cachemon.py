#!/usr/bin/env python3
# TDD-Test — Schritt 2: cached_status() parst checkcached-Antwort -> {hash: bool} fuer alle angefragten.
import os, sys
os.environ.setdefault("TORBOX_API_KEY", "test")       # app.py liest Key beim Import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import cached_status

def main():
    # checkcached 'data'-Objekt: nur gecachte Hashes sind als Keys praesent.
    data = {"AAAA": {"name": "x", "size": 1, "hash": "AAAA"},
            "bbbb": {"name": "y", "size": 2, "hash": "bbbb"}}
    requested = ["AAAA", "BBBB", "CCCC"]               # gemischte Schreibweise
    st = cached_status(data, requested)
    assert st["AAAA"] is True,  "exakter Hash gecacht"
    assert st["BBBB"] is True,  "case-insensitiv: BBBB==bbbb gecacht"
    assert st["CCCC"] is False, "fehlender Hash = nicht gecacht"
    assert set(st) == set(requested), "Status fuer ALLE angefragten Hashes"
    # leere Antwort -> alle nicht gecacht
    st2 = cached_status({}, requested)
    assert all(v is False for v in st2.values()), "leere data = nichts gecacht"
    # data kann None sein (API-Hiccup) -> defensiv alle False
    st3 = cached_status(None, ["AAAA"])
    assert st3 == {"AAAA": False}, "None-data defensiv"
    print("OK: cached_status case-insensitiv + vollstaendig + defensiv")

if __name__ == "__main__":
    main()
