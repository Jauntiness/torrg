#!/usr/bin/env python3
# TDD-Test — Schritt 5: ProbeCache speichert NUR Bytes in den head/tail-Fenstern, dient sie
# lokal aus (kein CDN/Materialize), und persistiert ueber Neustart.
import os, tempfile, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probecache import ProbeCache

def main():
    d = tempfile.mkdtemp()
    try:
        HEAD, TAIL, SIZE = 1024, 512, 10000
        pc = ProbeCache(d, HEAD, TAIL)
        H, W = "HASH1", "Rel/file.mkv"

        # Miss vor jeglichem put
        assert pc.get(H, W, 0, 100) is None, "leerer Cache -> Miss"

        # put deckt [0,2000) an -> nur [0,1024) (head-window) wird gespeichert
        pc.put(H, W, 0, b"A" * 2000, SIZE)
        got = pc.get(H, W, 0, 1024)
        assert got == b"A" * 1024, "head vollstaendig aus Cache"
        assert pc.get(H, W, 0, 2000) is None, "ueber head-window hinaus -> Miss (Body nicht gecacht)"
        assert pc.get(H, W, 1024, 10) is None, "knapp jenseits head -> Miss"

        # Body-Bereich (kein Fenster) -> nichts gespeichert
        pc.put(H, W, 5000, b"C" * 100, SIZE)
        assert pc.get(H, W, 5000, 100) is None, "Body wird NICHT gecacht (Disk-Schutz)"

        # tail-window: [SIZE-TAIL, SIZE) = [9488,10000)
        pc.put(H, W, 9000, b"B" * 1000, SIZE)        # deckt [9000,10000), nur [9488,10000) im Fenster
        assert pc.get(H, W, 9488, 512) == b"B" * 512, "tail aus Cache"
        assert pc.get(H, W, 9000, 100) is None, "vor tail-window -> Miss"

        # Persistenz: neue Instanz, selbes Verzeichnis
        pc2 = ProbeCache(d, HEAD, TAIL)
        assert pc2.get(H, W, 0, 1024) == b"A" * 1024, "head ueberlebt Neustart"
        assert pc2.get(H, W, 9488, 512) == b"B" * 512, "tail ueberlebt Neustart"

        # Andere Datei isoliert
        assert pc2.get("HASH2", W, 0, 100) is None, "anderer hash -> getrennt"
        print("OK: probecache head/tail-only + body-skip + persistenz + isolation")
    finally:
        shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__":
    main()
