#!/usr/bin/env python3
# TDD-Test — Baseline-Combine (User-Modell), Integration mit echten Threads:
#   WebDAV mischt nur mit, wenn CDN langsamer ist als seine gemerkte Normalitaet (cdn_ref) UND
#   die Nachfrage ungedeckt ist.
#     - Steady-schneller CDN, flat-out (Copy) -> KEINE Abweichung -> WebDAV idle (kein Klau).
#     - CDN bricht mitten im Stream ein -> Abweichung -> Test -> WebDAV dazu.
#     - CDN von Anfang an langsam, ABER Referenz aus vorigen Files kennt "CDN kann schneller"
#       -> Abweichung -> WebDAV dazu (der geschlossene Rest-Fall).
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm import SegmentSwarm, _cdn_ref_reset, _cdn_ref_publish

SEG = 4


class TtfbReader:
    def __init__(self, blob, off, delay_fn, ttfb, name, bcnt):
        self.blob, self.pos, self.delay_fn, self.ttfb, self.name, self.bcnt = blob, off, delay_fn, ttfb, name, bcnt
        self.first = True

    def read(self, n):
        if self.first:
            time.sleep(self.ttfb)
            self.first = False
        time.sleep(self.delay_fn(self.pos) * (n / SEG))
        d = self.blob[self.pos:self.pos + n]
        self.pos += len(d)
        if self.bcnt is not None:
            self.bcnt[self.name] = self.bcnt.get(self.name, 0) + len(d)
        return d

    def close(self):
        pass


def opener(blob, name, delay, ttfb=0.05, opens=None, bcnt=None):
    fn = delay if callable(delay) else (lambda pos: delay)
    def o(off):
        if opens is not None:
            opens[name] = opens.get(name, 0) + 1
        return TtfbReader(blob, off, fn, ttfb, name, bcnt)
    return (name, o)


def drain(s, blob, pace=0.0):
    out = bytearray()
    pos = 0
    while pos < len(blob):
        chunk = s.read(pos, min(SEG, len(blob) - pos), timeout=15)
        assert chunk is not None, f"read None bei {pos}"
        out += chunk
        pos += len(chunk)
        s.advance(pos)
        if pace:
            time.sleep(pace)
    return bytes(out)


def mkblob(nsegs):
    return bytes((i * 13) % 256 for i in range(nsegs * SEG))


def test_steady_cdn_flatout_no_webdav():
    _cdn_ref_reset()
    blob = mkblob(60)
    opens = {}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=12,
                     sources=[opener(blob, "cdn", 0.01, opens=opens), opener(blob, "web", 0.01, opens=opens)],
                     deviation_factor=1.6, degrade_persist_s=0.3, test_window_s=0.3, cooldown_s=2.0)
    out = drain(s, blob)
    s.close()
    assert out == blob, "exakt rekonstruiert"
    assert opens.get("web", 0) == 0, f"WebDAV lief trotz steady CDN (Bandbreiten-Klau!) {opens}"


def test_cdn_degrades_webdav_engages():
    _cdn_ref_reset()
    blob = mkblob(70)
    bcnt = {}
    cdn_delay = lambda pos: 0.01 if (pos // SEG) < 24 else 0.09
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=12,
                     sources=[opener(blob, "cdn", cdn_delay, bcnt=bcnt), opener(blob, "web", 0.02, bcnt=bcnt)],
                     deviation_factor=1.6, degrade_persist_s=0.5, test_window_s=0.5, cooldown_s=1.0)
    out = drain(s, blob)
    s.close()
    assert out == blob, "exakt rekonstruiert (auch im Einbruch)"
    assert bcnt.get("web", 0) > 0, "WebDAV ist beim CDN-Einbruch nicht dazugekommen"


def test_cdn_slow_from_start_with_warm_ref():
    # Referenz aus vorigen (schnellen) Files: CDN kann 0.01. Jetzt ein File mit CDN langsam (0.06)
    # AB BYTE 1 -> trotzdem als Abweichung erkannt -> WebDAV dazu (der geschlossene Rest-Fall).
    _cdn_ref_reset()
    _cdn_ref_publish(0.01)                 # "CDN war zuletzt schnell" (0.01s/Segment)
    blob = mkblob(60)
    bcnt = {}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=12,
                     sources=[opener(blob, "cdn", 0.06, bcnt=bcnt), opener(blob, "web", 0.02, bcnt=bcnt)],
                     deviation_factor=1.6, degrade_persist_s=0.3, test_window_s=0.5, cooldown_s=1.0)
    out = drain(s, blob)
    s.close()
    assert out == blob, "exakt rekonstruiert"
    assert bcnt.get("web", 0) > 0, "WebDAV kam trotz warmer Referenz nicht dazu (slow-from-start-Luecke)"


def test_no_combine_when_buffer_full():
    _cdn_ref_reset()
    blob = mkblob(30)
    opens = {}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=12,
                     sources=[opener(blob, "cdn", 0.003, opens=opens), opener(blob, "web", 0.003, opens=opens)],
                     deviation_factor=1.6, degrade_persist_s=0.3, test_window_s=0.3, cooldown_s=2.0)
    out = drain(s, blob, pace=0.03)
    s.close()
    assert out == blob
    assert opens.get("web", 0) == 0, f"WebDAV lief obwohl CDN gesund + Puffer voll {opens}"


def main():
    test_steady_cdn_flatout_no_webdav()
    test_cdn_degrades_webdav_engages()
    test_cdn_slow_from_start_with_warm_ref()
    test_no_combine_when_buffer_full()
    print("OK: Baseline-Combine — steady CDN idle (kein Klau), CDN-Einbruch holt WebDAV, "
          "slow-from-start mit warmer Referenz holt WebDAV, voller Puffer = idle")


if __name__ == "__main__":
    main()
