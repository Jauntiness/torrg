#!/usr/bin/env python3
# TDD — LightSlot: leichter Default-Pfad (1 Verbindung, synchron, KEINE Dauer-Threads).
#   Kaltstart = Per-GET-Hedge (CDN zuerst; liefert es nicht in cold_grace -> WebDAV mitoeffnen,
#   Schnellerer gewinnt). Gesund: CDN gewinnt, WebDAV nie geoeffnet. Sequenzieller Reuse. Seek.
import os, sys, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lightstream import LightSlot

SEG = 4


class FakeReader:
    """Forward-Stream ueber blob ab off. ttfb = Verzoegerung des 1. read (Connection-Setup/kalt)."""
    def __init__(self, blob, off, ttfb, per, name, opens, bcnt):
        self.blob, self.pos, self.ttfb, self.per, self.name = blob, off, ttfb, per, name
        self.bcnt = bcnt
        self.first = True

    def read(self, n):
        if self.first:
            if self.ttfb:
                time.sleep(self.ttfb)
            self.first = False
        if self.per:
            time.sleep(self.per * (n / SEG))
        d = self.blob[self.pos:self.pos + n]
        self.pos += len(d)
        if self.bcnt is not None:
            self.bcnt[self.name] = self.bcnt.get(self.name, 0) + len(d)
        return d

    def close(self):
        pass


def opener(blob, name, ttfb=0.0, per=0.0, opens=None, bcnt=None):
    def o(off):
        if opens is not None:
            opens[name] = opens.get(name, 0) + 1
        return FakeReader(blob, off, ttfb, per, name, opens, bcnt)
    return o


def mkblob(nsegs):
    return bytes((i * 7) % 256 for i in range(nsegs * SEG))


def drain(slot, blob, step=SEG):
    out = bytearray()
    pos = 0
    while pos < len(blob):
        d = slot.read(pos, min(step, len(blob) - pos), timeout=5)
        assert d is not None, f"read None bei {pos}"
        out += d
        pos += len(d)
    return bytes(out)


def test_healthy_cdn_web_never_opens():
    blob = mkblob(30)
    opens = {}
    slot = LightSlot(opener(blob, "cdn", ttfb=0.001, opens=opens),
                     opener(blob, "web", opens=opens), cold_grace_s=0.3)
    assert drain(slot, blob) == blob, "exakt rekonstruiert"
    slot.close()
    assert opens.get("web", 0) == 0, f"WebDAV lief obwohl CDN gesund ({opens})"
    assert opens.get("cdn", 0) == 1, f"CDN sollte EINE Verbindung sein (Reuse), war {opens}"


def test_cold_cdn_web_wins_start():
    # CDN kalt (1. Byte erst nach 1.5s), WebDAV schnell -> Start via WebDAV, kein Warten auf CDN.
    blob = mkblob(20)
    opens, bcnt = {}, {}
    slot = LightSlot(opener(blob, "cdn", ttfb=1.5, opens=opens, bcnt=bcnt),
                     opener(blob, "web", ttfb=0.02, opens=opens, bcnt=bcnt), cold_grace_s=0.3)
    t0 = time.monotonic()
    d = slot.read(0, SEG, timeout=10)
    dt = time.monotonic() - t0
    assert d == blob[0:SEG], "Kopf korrekt"
    assert dt < 1.0, f"hat auf kalten CDN gewartet statt WebDAV ({dt:.2f}s)"
    assert bcnt.get("web", 0) > 0, "WebDAV hat den Start nicht gewonnen"
    slot.close()


def test_no_daemon_threads():
    # Nach einem gesunden Read duerfen KEINE per-Stream-Dauer-Threads zurueckbleiben.
    blob = mkblob(10)
    before = threading.active_count()
    slot = LightSlot(opener(blob, "cdn", ttfb=0.001), opener(blob, "web"), cold_grace_s=0.3)
    drain(slot, blob)
    time.sleep(0.2)
    after = threading.active_count()
    slot.close()
    assert after - before <= 1, f"Light-Slot hat Dauer-Threads hinterlassen ({before}->{after})"


def test_seek_reopens():
    blob = mkblob(40)
    opens = {}
    slot = LightSlot(opener(blob, "cdn", ttfb=0.001, opens=opens), opener(blob, "web", opens=opens),
                     cold_grace_s=0.3)
    assert slot.read(0, SEG, timeout=5) == blob[0:SEG]
    assert slot.read(80, SEG, timeout=5) == blob[80:80 + SEG], "Seek vorwaerts"     # off 20 segs
    assert slot.read(0, SEG, timeout=5) == blob[0:SEG], "Seek zurueck"
    slot.close()
    assert opens.get("cdn", 0) == 3, f"3 Reopens (Start + 2 Seeks) erwartet, war {opens}"


# ---- Batch 2: einseitige Eskalation light -> heavy bei anhaltendem Stall ----------------

class SelReader:
    """Vorwaerts-Stream; Reads mit Index in slow_idx schlafen `dur` (selektiver Stall)."""
    def __init__(self, blob, off, slow_idx, dur):
        self.blob, self.pos, self.slow_idx, self.dur, self.i = blob, off, slow_idx, dur, 0

    def read(self, n):
        if self.i in self.slow_idx:
            time.sleep(self.dur)
        self.i += 1
        d = self.blob[self.pos:self.pos + n]
        self.pos += len(d)
        return d

    def close(self):
        pass


def sel_opener(blob, slow_idx, dur, opens=None):
    def o(off):
        if opens is not None:
            opens["cdn"] = opens.get("cdn", 0) + 1
        return SelReader(blob, off, slow_idx, dur)
    return o


class FakeHeavy:
    """Steht fuer SegmentSwarm — Random-Access read(start,length,timeout)."""
    def __init__(self, blob):
        self.blob, self.reads = blob, 0

    def read(self, start, length, timeout):
        self.reads += 1
        return self.blob[start:start + length]

    def close(self):
        pass


def test_sustained_stall_escalates_once_and_stays_heavy():
    # CDN dauerhaft langsam, KEIN WebDAV -> nach escalate_n Stalls EINMAL auf Heavy hoch,
    # danach bleibt heavy (einseitig); kein zweiter Swarm trotz weiterer Reads (idempotent).
    blob = mkblob(20)
    calls = []
    heavy = FakeHeavy(blob)
    slot = LightSlot(opener(blob, "cdn", per=0.5), None, cold_grace_s=0.3,
                     grace_s=0.3, escalate_n=3, make_heavy=lambda pos: (calls.append(pos), heavy)[1])
    for i in range(10):
        d = slot.read(i * SEG, SEG, timeout=10)
        assert d == blob[i * SEG:(i + 1) * SEG], f"Segment {i} falsch"
    slot.close()
    assert len(calls) == 1, f"genau EINE Eskalation erwartet (idempotent/einseitig), war {calls}"
    assert calls[0] == 4 * SEG, f"Heavy sollte an aktueller Position starten, war {calls[0]}"
    assert heavy.reads >= 5, f"nach Eskalation muss Heavy die restlichen Reads liefern, {heavy.reads}"


def test_budget_full_stays_light():
    # make_heavy liefert None (RAM-Budget voll) -> keine Eskalation, Stream laeuft light weiter.
    blob = mkblob(12)
    calls = []
    slot = LightSlot(opener(blob, "cdn", per=0.5), None, cold_grace_s=0.3,
                     grace_s=0.3, escalate_n=2, make_heavy=lambda pos: calls.append(pos))
    for i in range(8):
        d = slot.read(i * SEG, SEG, timeout=10)
        assert d == blob[i * SEG:(i + 1) * SEG], f"Segment {i} falsch (light muss weiter liefern)"
    assert slot.mode == "light", f"ohne Budget muss light bleiben, war {slot.mode}"
    assert len(calls) >= 1, "Eskalation sollte versucht worden sein"
    slot.close()


def test_transient_stall_resets_streak():
    # Ein einzelnes langsames Segment zwischen schnellen -> Streak resettet, KEINE Eskalation.
    blob = mkblob(12)
    calls = []
    slot = LightSlot(sel_opener(blob, slow_idx={1}, dur=0.5), None, cold_grace_s=0.3,
                     grace_s=0.3, escalate_n=3, make_heavy=lambda pos: calls.append(pos))
    for i in range(8):
        assert slot.read(i * SEG, SEG, timeout=10) == blob[i * SEG:(i + 1) * SEG]
    slot.close()
    assert calls == [], f"transienter Stall darf NICHT eskalieren, war {calls}"


# ---- Batch 3: Skalierung (viele parallele Streams) + Fallback-Vertrag ------------------

def test_many_parallel_streams_flat_threads():
    # 50 gleichzeitige Light-Slots -> alle exakt, KEINE per-Stream-Dauer-Threads zurueck.
    import threading as _t
    N = 50
    blobs = [mkblob(15) for _ in range(N)]
    slots = [LightSlot(opener(b, "cdn", ttfb=0.001), opener(b, "web"), cold_grace_s=0.3)
             for b in blobs]
    results = [None] * N
    before = _t.active_count()

    def work(i):
        results[i] = drain(slots[i], blobs[i])

    threads = [_t.Thread(target=work, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.3)
    after = _t.active_count()
    for s in slots:
        s.close()
    for i in range(N):
        assert results[i] == blobs[i], f"Stream {i} nicht exakt rekonstruiert"
    assert after - before <= 2, f"Light-Slots haben Dauer-Threads hinterlassen ({before}->{after})"


def test_truncation_returns_none():
    # Quelle bricht mitten im Stream ab (leerer Read) -> read() liefert None (Handler -> Fallback).
    class TruncReader:
        def __init__(self, blob, off):
            self.blob, self.pos, self.n = blob, off, 0

        def read(self, n):
            self.n += 1
            if self.n >= 2:          # 2. Read liefert nichts mehr (Truncation)
                return b""
            d = self.blob[self.pos:self.pos + n]
            self.pos += len(d)
            return d

        def close(self):
            pass

    blob = mkblob(8)
    slot = LightSlot(lambda off: TruncReader(blob, off), None, cold_grace_s=0.3)
    assert slot.read(0, SEG, timeout=5) == blob[0:SEG], "erster Read korrekt"
    assert slot.read(SEG, SEG, timeout=5) is None, "Truncation muss None liefern"
    slot.close()


def test_both_sources_fail_returns_none():
    def boom(off):
        raise OSError("Quelle tot")
    slot = LightSlot(boom, boom, cold_grace_s=0.1)
    assert slot.read(0, SEG, timeout=2) is None, "beide Quellen tot -> None (Fallback)"
    slot.close()


def main():
    test_healthy_cdn_web_never_opens()
    test_cold_cdn_web_wins_start()
    test_no_daemon_threads()
    test_seek_reopens()
    test_sustained_stall_escalates_once_and_stays_heavy()
    test_budget_full_stays_light()
    test_transient_stall_resets_streak()
    test_many_parallel_streams_flat_threads()
    test_truncation_returns_none()
    test_both_sources_fail_returns_none()
    print("OK: LightSlot — gesund (1 Verbindung, web idle), Kaltstart-Hedge (web gewinnt), "
          "keine Dauer-Threads, Seek-Reopen; einseitige Eskalation bei Stall (idempotent), "
          "Budget-Gate, transienter Stall resettet Streak")


if __name__ == "__main__":
    main()
