#!/usr/bin/env python3
# TDD — App-Glue fuer LIGHTSTREAM: RAM-Budget-Gate in _make_heavy + Budget-Freigabe im
# _HeavyAdapter.close() + get_light_slot-Registry. Kein Netz: SegmentSwarm wird gefaket.
import os, sys
os.environ.setdefault("TORBOX_API_KEY", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


class FakeSwarm:
    def __init__(self, *a, **k): pass
    def read(self, start, length, timeout): return b""
    def advance(self, pos): pass
    def close(self): pass


def test_heavy_budget_gate_and_release():
    app.SegmentSwarm = FakeSwarm                       # kein echtes Netz/Threads
    per = app.SWARM_WINDOW * app.SWARM_SEG
    app.SWARM_RAM_BUDGET = per + per // 2              # Platz fuer GENAU einen Heavy
    app.HEAVY_BYTES = 0
    meta = {"type": "torrents", "tid": 1, "fid": 0, "wpath": "x.mkv", "hash": "h"}

    mk = app._make_heavy(meta, ("torrents", 1, 0), 1000, "x.mkv")
    h1 = mk(0)
    assert h1 is not None, "erster Heavy muss im Budget passen"
    assert app.HEAVY_BYTES == per, f"Budget sollte {per} reserviert sein, war {app.HEAVY_BYTES}"

    h2 = mk(per)
    assert h2 is None, "zweiter Heavy ueberschreitet Budget -> None (Stream bleibt light)"
    assert app.HEAVY_BYTES == per, "abgelehnter Heavy darf NICHTS reservieren"

    h1.close()
    assert app.HEAVY_BYTES == 0, f"close() muss Budget freigeben, war {app.HEAVY_BYTES}"
    h1.close()                                         # idempotent
    assert app.HEAVY_BYTES == 0, "doppeltes close() darf Budget nicht negativ machen"

    h3 = mk(0)
    assert h3 is not None, "nach Freigabe muss wieder ein Heavy passen"
    h3.close()


def test_get_light_slot_registry():
    app.LIGHT_SLOTS.clear()
    meta = {"type": "torrents", "tid": 2, "fid": 0, "wpath": "y.mkv", "hash": "h2"}
    s1 = app.get_light_slot(("torrents", 2, 0), meta, 1000, "y.mkv")
    s2 = app.get_light_slot(("torrents", 2, 0), meta, 1000, "y.mkv")
    assert s1 is s2, "gleicher Key -> selber Slot (Verbindungs-Reuse)"
    s3 = app.get_light_slot(("torrents", 3, 0), meta, 1000, "y.mkv")
    assert s3 is not s1, "anderer Key -> eigener Slot"
    app.LIGHT_SLOTS.clear()


def main():
    test_heavy_budget_gate_and_release()
    test_get_light_slot_registry()
    print("OK: App-Glue — Heavy-RAM-Budget-Gate (1 Heavy passt, 2. abgelehnt), close() gibt frei "
          "(idempotent), get_light_slot-Registry (Key-Reuse)")


if __name__ == "__main__":
    main()
