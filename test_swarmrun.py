#!/usr/bin/env python3
# TDD-Test — SegmentSwarm, Hybrid/Hedge-Modell:
#   CDN (prio 1) ist die einzige PRAEEMPTIVE Quelle und streamt zusammenhaengend (Connection-
#   Reuse). WebDAV (prio 2) ist LAZY: es laeuft NUR, wenn der Reader hungert (CDN liefert das
#   gebrauchte Segment nicht rechtzeitig). Dann zieht WebDAV das blockierte Kopf-Segment redundant
#   mit (Hedge, erster gewinnt) und fuellt read-ahead -> Stall-Rescue + Kombination, ohne im
#   gesunden Fall Overhead zu erzeugen. Schwelle = adaptiver Grace (EWMA der CDN-Segmentzeit),
#   KEIN hardcoded Mbit/Byte-Wert.
#
# Verifiziert (echte Threads, FAKE-Opener per Dependency-Injection):
#   - correctness: sequentielles read() rekonstruiert die Datei exakt.
#   - lazy/no-overhead: gesunder schneller CDN -> WebDAV oeffnet NIE (opens==0), CDN ~1 Open.
#   - rescue: langsamer/stockender CDN -> WebDAV springt ein, read() liefert schnell+korrekt.
#   - seek / timeout.
import os, sys, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm import SegmentSwarm


class FakeReader:
    def __init__(self, blob, off, delay, bcnt, name):
        self.blob, self.pos, self.delay, self.bcnt, self.name = blob, off, delay, bcnt, name

    def read(self, n):
        if self.delay:
            time.sleep(self.delay)
        d = self.blob[self.pos:self.pos + n]
        self.pos += len(d)
        if self.bcnt is not None:
            self.bcnt[self.name] = self.bcnt.get(self.name, 0) + len(d)
        return d

    def close(self):
        pass


def make_opener(blob, name, delay=0.0, fail=False, opens=None, bcnt=None):
    def opener(off):
        if opens is not None:
            opens[name] = opens.get(name, 0) + 1
        if fail:
            raise RuntimeError("boom")
        return FakeReader(blob, off, delay, bcnt, name)
    return (name, opener)


def drain(s, blob, step=4, pace=0.0):
    out = bytearray()
    pos = 0
    while pos < len(blob):
        chunk = s.read(pos, min(step, len(blob) - pos), timeout=5)
        assert chunk is not None, f"read None bei {pos}"
        out += chunk
        pos += len(chunk)
        s.advance(pos)
        if pace:
            time.sleep(pace)        # Client langsamer als die Quelle (echtes Playback, nicht flat-out)
    return bytes(out)


def test_correctness():
    blob = bytes((i * 7) % 256 for i in range(120 * 4))
    s = SegmentSwarm(size=len(blob), seg_size=4, window=16,
                     sources=[make_opener(blob, "cdn", delay=0.001),
                              make_opener(blob, "web", delay=0.001)],
                     cold_grace_s=0.5)
    assert drain(s, blob) == blob, "rekonstruierte Datei stimmt exakt"
    s.close()


def test_lazy_no_overhead_when_cdn_healthy():
    # Echtes Playback: Client (pace 0.02/seg) langsamer als CDN (0.002/seg) -> Puffer bleibt voll ->
    # read() trifft immer sofort -> KEIN Combine -> WebDAV bleibt still, CDN 1 Verbindung.
    blob = bytes((i * 3) % 256 for i in range(120 * 4))
    opens = {}
    s = SegmentSwarm(size=len(blob), seg_size=4, window=32,
                     sources=[make_opener(blob, "cdn", delay=0.002, opens=opens),
                              make_opener(blob, "web", delay=0.002, opens=opens)],
                     cold_grace_s=0.5, hedge_k=2.0)
    assert drain(s, blob, pace=0.02) == blob
    s.close()
    assert opens.get("web", 0) == 0, f"WebDAV lief obwohl CDN gesund ({opens})"
    assert opens.get("cdn", 0) <= 3, f"CDN zu viele Opens, kein Connection-Reuse ({opens})"


def test_secondary_rescues_slow_cdn():
    blob = bytes((i * 5) % 256 for i in range(40 * 4))
    opens, bcnt = {}, {}
    # CDN sehr langsam (0.3s/Segment), WebDAV schnell; Grace klein -> Hedge springt an.
    s = SegmentSwarm(size=len(blob), seg_size=4, window=8,
                     sources=[make_opener(blob, "cdn", delay=0.30, opens=opens, bcnt=bcnt),
                              make_opener(blob, "web", delay=0.01, opens=opens, bcnt=bcnt)],
                     cold_grace_s=0.05, hedge_k=1.0, hedge_min=0.02)
    t0 = time.monotonic()
    out = drain(s, blob)
    dt = time.monotonic() - t0
    s.close()
    assert out == blob, "trotz langsamem CDN exakt rekonstruiert (Rescue)"
    assert bcnt.get("web", 0) > 0, "WebDAV hat den langsamen CDN gerettet (Bytes beigetragen)"
    # CDN allein braeuchte ~40*0.3=12s; mit Rescue deutlich schneller.
    assert dt < 6.0, f"Rescue war nicht schnell genug ({dt:.1f}s)"


class SlowFirstReader:
    """Erster Read teuer (kalter CDN-Edge-Spike), danach schnell."""
    def __init__(self, blob, off, first_delay, name, bcnt):
        self.blob, self.pos, self.fd, self.name, self.bcnt, self.first = blob, off, first_delay, name, bcnt, True

    def read(self, n):
        if self.first:
            time.sleep(self.fd); self.first = False
        d = self.blob[self.pos:self.pos + n]
        self.pos += len(d)
        if self.bcnt is not None:
            self.bcnt[self.name] = self.bcnt.get(self.name, 0) + len(d)
        return d

    def close(self):
        pass


def test_cold_start_races_webdav():
    # Kalter CDN: 1. Byte erst nach 3s (Edge-Provisioning). WebDAV schnell. Der Start darf NICHT auf
    # den langsamen CDN warten -> WebDAV rennt nach cold_grace mit und liefert den Kopf.
    blob = bytes((i * 9) % 256 for i in range(20 * 4))
    bcnt = {}
    s = SegmentSwarm(size=len(blob), seg_size=4, window=8,
                     sources=[("cdn", lambda off: SlowFirstReader(blob, off, 3.0, "cdn", bcnt)),
                              ("web", lambda off: SlowFirstReader(blob, off, 0.02, "web", bcnt))],
                     cold_grace_s=0.3)
    t0 = time.monotonic()
    chunk = s.read(0, 4, timeout=10)
    dt = time.monotonic() - t0
    s.close()
    assert chunk == blob[0:4], "Kopf korrekt geliefert"
    assert dt < 1.5, f"Start hat auf den kalten CDN gewartet statt WebDAV zu racen ({dt:.2f}s)"
    assert bcnt.get("web", 0) > 0, "WebDAV hat den Start nicht abgefangen"


def test_seek():
    blob = bytes(range(256)) * 4
    s = SegmentSwarm(size=len(blob), seg_size=16, window=4,
                     sources=[make_opener(blob, "cdn")], cold_grace_s=0.5)
    assert s.read(512, 16, timeout=5) == blob[512:528], "Seek vorwaerts"
    assert s.read(0, 16, timeout=5) == blob[0:16], "Seek zurueck"
    assert s.read(600, 16, timeout=5) == blob[600:616], "unaligned Seek"
    s.close()


def test_timeout_all_fail():
    s = SegmentSwarm(size=64, seg_size=16, window=2,
                     sources=[make_opener(b"x" * 64, "cdn", fail=True)], cold_grace_s=0.05)
    assert s.read(0, 16, timeout=0.4) is None, "permanent failende Quelle -> read None (kein Hang)"
    s.close()


def main():
    test_correctness()
    test_lazy_no_overhead_when_cdn_healthy()
    test_secondary_rescues_slow_cdn()
    test_cold_start_races_webdav()
    test_seek()
    test_timeout_all_fail()
    print("OK: SegmentSwarm hybrid — correctness + lazy/no-overhead + rescue + cold-start-race + seek + timeout")


if __name__ == "__main__":
    main()
