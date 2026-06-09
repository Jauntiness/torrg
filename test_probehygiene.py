#!/usr/bin/env python3
# TDD-Test — Schritt 6: ProbeCache.drop() + PROBE_MAX-Budget (Disk-Schutz, soft, kein Crash).
import os, tempfile, shutil, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probecache import ProbeCache

def test_drop():
    d = tempfile.mkdtemp()
    try:
        pc = ProbeCache(d, 2000, 0)
        pc.put("H", "w/a.mkv", 0, b"A" * 1000, 10000)
        assert pc.get("H", "w/a.mkv", 0, 1000) == b"A" * 1000, "vor drop gecacht"
        assert os.listdir(d), "Dateien auf Disk"
        pc.drop("H", "w/a.mkv")
        assert pc.get("H", "w/a.mkv", 0, 1000) is None, "nach drop weg"
        assert os.listdir(d) == [], "Blob+Sidecar geloescht"
        pc.drop("H", "w/a.mkv")                       # idempotent
        print("OK: drop entfernt Blob+Sidecar, idempotent")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_lru_evict_under_budget():
    d = tempfile.mkdtemp()
    try:
        pc = ProbeCache(d, 2000, 0, max_bytes=1500)
        pc.put("H1", "w", 0, b"A" * 1000, 10000)     # total 1000
        time.sleep(0.02)                             # H1 aelter (LRU)
        pc.put("H2", "w", 0, b"B" * 1000, 10000)     # 2000>1500 -> LRU-Evict H1
        assert pc.get("H2", "w", 0, 1000) == b"B" * 1000, "neuestes bleibt"
        assert pc.get("H1", "w", 0, 1000) is None, "aeltestes evicted (LRU)"
        assert pc._total() <= 1500, "unter Budget"
        print("OK: LRU-Evict haelt Budget")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_noop_when_single_file_exceeds_budget():
    d = tempfile.mkdtemp()
    try:
        pc = ProbeCache(d, 5000, 0, max_bytes=500)
        # 1000 Bytes > 500 Budget, nichts zu evicten -> put = no-op, KEIN Crash
        pc.put("H", "w", 0, b"A" * 1000, 10000)
        assert pc.get("H", "w", 0, 1000) is None, "ueber Budget -> nicht gecacht (no-op)"
        assert pc._total() <= 500, "Budget nie ueberschritten"
        print("OK: put ueber Budget = no-op, kein Crash")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def test_put_never_raises():
    d = tempfile.mkdtemp()
    try:
        pc = ProbeCache(d, 2000, 0, max_bytes=0)
        shutil.rmtree(d)                             # Verzeichnis weg -> put darf NICHT crashen
        pc.put("H", "w", 0, b"A" * 100, 10000)       # schluckt Fehler
        print("OK: put wirft nie (Serving-Pfad geschuetzt)")
    finally:
        shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__":
    test_drop()
    test_lru_evict_under_budget()
    test_noop_when_single_file_exceeds_budget()
    test_put_never_raises()
