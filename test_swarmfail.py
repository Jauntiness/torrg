#!/usr/bin/env python3
# TDD-Test — Failover bei Mid-Stream-Ausfaellen (genau die User-Frage):
#   Faellt CDN oder WebDAV waehrend des Streams zeitweise aus und kommt zurueck — werden die
#   fehlenden Teile von der jeweils anderen Quelle aufgefangen, oder bricht der Stream?
# Modell: eine Quelle ist fuer bestimmte Segmente "down" (Connect- ODER Mid-Stream-Read-Fail);
# der Swarm MUSS die Datei trotzdem exakt+vollstaendig liefern, solange irgendeine Quelle das
# Segment kann. Echte Threads, FAKE-Opener.
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm import SegmentSwarm

SEG = 4


class FlakyReader:
    def __init__(self, blob, off, down, name, bcnt):
        self.blob, self.pos, self.down, self.name, self.bcnt = blob, off, down, name, bcnt

    def read(self, n):
        seg = self.pos // SEG
        if self.down(seg):                       # Quelle faellt MITTEN im Read aus
            raise IOError(f"{self.name} down @ seg {seg}")
        time.sleep(0.002)
        d = self.blob[self.pos:self.pos + n]
        self.pos += len(d)
        if self.bcnt is not None:
            self.bcnt[self.name] = self.bcnt.get(self.name, 0) + len(d)
        return d

    def close(self):
        pass


def flaky(blob, name, down, delay=0.0, bcnt=None):
    def opener(off):
        if down(off // SEG):                     # Quelle ist beim CONNECT down
            raise IOError(f"{name} connect-fail @ seg {off // SEG}")
        if delay:
            time.sleep(delay)
        return FlakyReader(blob, off, down, name, bcnt)
    return (name, opener)


def drain(s, blob):
    out = bytearray()
    pos = 0
    while pos < len(blob):
        chunk = s.read(pos, min(SEG, len(blob) - pos), timeout=8)
        assert chunk is not None, f"read None bei {pos} (Stream gebrochen!)"
        out += chunk
        pos += len(chunk)
        s.advance(pos)
    return bytes(out)


def mkblob(nsegs):
    return bytes((i * 11) % 256 for i in range(nsegs * SEG))


def test_cdn_outage_caught_by_webdav():
    # CDN faellt fuer Segmente 10..20 aus; WebDAV gesund -> WebDAV fuellt die Luecke, CDN
    # uebernimmt danach wieder. Stream MUSS vollstaendig+korrekt sein.
    blob = mkblob(40)
    bcnt = {}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=8,
                     sources=[flaky(blob, "cdn", lambda x: 10 <= x <= 20, bcnt=bcnt),
                              flaky(blob, "web", lambda x: False, bcnt=bcnt)],
                     cold_grace_s=0.05, hedge_k=1.0, hedge_min=0.02)
    assert drain(s, blob) == blob, "CDN-Ausfall: Datei exakt rekonstruiert"
    assert bcnt.get("web", 0) > 0, "WebDAV hat die CDN-Luecke aufgefangen"
    assert bcnt.get("cdn", 0) > 0, "CDN hat vor/nach dem Ausfall geliefert"
    s.close()


def test_intermittent_cdn_flaps():
    # CDN flappt mehrfach (kurze Ausfaelle, kommt zurueck); WebDAV gesund. Kein Bruch.
    blob = mkblob(40)
    flap = {5, 6, 7, 15, 16, 27, 28, 29}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=6,
                     sources=[flaky(blob, "cdn", lambda x: x in flap),
                              flaky(blob, "web", lambda x: False)],
                     cold_grace_s=0.05, hedge_k=1.0, hedge_min=0.02)
    assert drain(s, blob) == blob, "CDN-Flapping: Datei exakt rekonstruiert"
    s.close()


def test_webdav_dead_is_harmless_for_healthy_cdn():
    # WebDAV faellt komplett aus; CDN gesund. WebDAV ist eh idle -> Stream laeuft unbeeintraechtigt.
    blob = mkblob(30)
    bcnt = {}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=8,
                     sources=[flaky(blob, "cdn", lambda x: False, bcnt=bcnt),
                              flaky(blob, "web", lambda x: True, bcnt=bcnt)],  # web immer tot
                     cold_grace_s=0.5, hedge_k=2.0)
    assert drain(s, blob) == blob, "WebDAV tot: gesunder CDN-Stream unbeeintraechtigt"
    assert bcnt.get("web", 0) == 0, "toter WebDAV hat nichts geliefert (war nie noetig)"
    s.close()


def test_alternating_outages_each_covers_the_other():
    # CDN down auf 8..14, WebDAV down auf 22..27 — sonst beide ok. Jede Quelle deckt die Luecke
    # der anderen. (CDN leicht verzoegert, damit WebDAV im CDN-Loch wirklich gerufen wird.)
    blob = mkblob(40)
    bcnt = {}
    s = SegmentSwarm(size=len(blob), seg_size=SEG, window=6,
                     sources=[flaky(blob, "cdn", lambda x: 8 <= x <= 14, delay=0.0, bcnt=bcnt),
                              flaky(blob, "web", lambda x: 22 <= x <= 27, bcnt=bcnt)],
                     cold_grace_s=0.05, hedge_k=1.0, hedge_min=0.02)
    assert drain(s, blob) == blob, "wechselnde Ausfaelle: Datei exakt rekonstruiert"
    assert bcnt.get("web", 0) > 0 and bcnt.get("cdn", 0) > 0, "beide haben beigetragen"
    s.close()


def main():
    test_cdn_outage_caught_by_webdav()
    test_intermittent_cdn_flaps()
    test_webdav_dead_is_harmless_for_healthy_cdn()
    test_alternating_outages_each_covers_the_other()
    print("OK: Failover — CDN-Ausfall/Flapping von WebDAV aufgefangen; toter WebDAV harmlos; "
          "wechselnde Ausfaelle gegenseitig gedeckt")


if __name__ == "__main__":
    main()
