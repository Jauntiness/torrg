#!/usr/bin/env python3
# TDD-Test — Scan-Analyse: within_windows() + per-File-Aggregation + Sessionisierung.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_probe_report as R

def test_within_windows():
    # size=1000, head=100, tail=50 -> Fenster [0,100) und [950,1000)
    assert R.within_windows(0, 100, 1000, 100, 50) is True, "ganz im head"
    assert R.within_windows(950, 1000, 1000, 100, 50) is True, "ganz im tail"
    assert R.within_windows(0, 101, 1000, 100, 50) is False, "1 byte ueber head"
    assert R.within_windows(500, 600, 1000, 100, 50) is False, "Body -> ausserhalb"
    assert R.within_windows(0, 50, 50, 100, 50) is True, "kleine Datei voll gelesen -> im head"
    print("OK: within_windows")

def test_sessionize():
    recs = [{"t": 100}, {"t": 101}, {"t": 105}, {"t": 500}, {"t": 501}]
    s = R.sessionize(recs, gap=60)
    assert len(s) == 2 and len(s[0]) == 3 and len(s[1]) == 2, f"2 Sessions per Zeitluecke, got {[len(x) for x in s]}"
    print("OK: sessionize")

def test_analyze_session():
    # zwei Files; F1 nur head (probe), F2 ein Body-Read via cdn
    recs = [
        {"t": 1, "src": "cdn",   "start": 0,   "end": 100, "len": 100, "size": 1000, "wpath": "F1"},
        {"t": 1, "src": "probe", "start": 0,   "end": 50,  "len": 50,  "size": 1000, "wpath": "F1"},
        {"t": 1, "src": "native","start": 500, "end": 600, "len": 100, "size": 1000, "wpath": "F2"},
    ]
    stats = R.analyze_session(recs, head=100, tail=50)
    f1 = stats["F1"]; f2 = stats["F2"]
    assert f1["reads"] == 2 and f1["bytes"] == 150, "F1 reads/bytes"
    assert f1["by_src"]["cdn"] == 100 and f1["by_src"]["probe"] == 50, "F1 src-bytes"
    assert f1["all_in_window"] is True, "F1 nur head -> in Fenster"
    assert f1["download_bytes"] == 100, "F1 aktiv geladen = cdn+native (probe zaehlt nicht)"
    assert f2["all_in_window"] is False, "F2 Body -> ausserhalb Fenster"
    assert f2["download_bytes"] == 100, "F2 native = aktiv geladen"
    print("OK: analyze_session")

if __name__ == "__main__":
    test_within_windows()
    test_sessionize()
    test_analyze_session()
