#!/usr/bin/env python3
# TDD-Test — WindowBuffer: der pure-logic Kern des Dual-Source-Read-Ahead-Swarms.
#
# Plex/rclone liest sequentiell in 1-MiB-Chunks. Ein Swarm fuellt ein begrenztes Fenster VOR
# der Leseposition aus ZWEI Quellen (CDN + WebDAV). Jede Quelle streamt eine ZUSAMMENHAENGENDE
# Strecke ueber EINE Verbindung (Connection-Reuse, kein Per-Segment-Setup). Diese Klasse haelt
# das Fenster, vergibt Start-Segmente (claim_from) und erlaubt das Weiterlesen auf derselben
# Verbindung (claim_specific), und gibt fertige Bytes geordnet heraus.
#
# Verhalten unter Test:
#   - window_segs/seg_len: Fenster-Mathematik inkl. kurzem Last-Segment.
#   - claim_from(target): Run-Start vorwaerts ab target; wrappt auf Front-Luecken.
#   - claim_specific(idx): naechstes Segment auf derselben Verbindung beanspruchen.
#   - claim_from==None wenn alles done/inflight  ->  EMERGENTE Pause (kein Mbps-Schwellwert).
#   - serve: geordnetes Reassembly, None solange ein abgedecktes Segment fehlt; Sub-Segment-Reads.
#   - complete out-of-order, fail (zurueck in den Pool), advance (Fenster gleitet + Eviction), reset.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm import WindowBuffer


def main():
    b = WindowBuffer(seg_size=4, window=6, size=40)    # 10 Segmente (0..9), Fenster=6 -> [0..5]
    assert b.window_segs() == [0, 1, 2, 3, 4, 5], b.window_segs()

    # CDN startet vorn (target=0); WebDAV-Run startet in der Mitte (target=3) -> eigene Strecke.
    assert b.claim_from(0) == 0
    assert b.claim_from(1) == 1                         # contiguous weiter ab Front
    assert b.claim_from(3) == 3
    assert b.claim_from(3) == 4                         # niedrigstes pending >=3
    # pending jetzt {2,5}: claim_from(3) -> niedrigstes >=3 = 5
    assert b.claim_from(3) == 5
    # pending {2}: nichts >=3 -> wrap auf Front-Luecke = 2
    assert b.claim_from(3) == 2
    assert b.claim_from(0) is None                     # alles inflight -> Pause (emergent)

    # serve: None solange Daten fehlen; out-of-order complete; geordnetes Reassembly.
    assert b.serve(0, 4) is None
    b.complete(2, b"2222"); b.complete(0, b"0000")
    assert b.has(0) and b.has(2) and not b.has(1)
    assert b.serve(0, 4) == b"0000"
    assert b.serve(0, 8) is None                        # Seg1 fehlt -> kein Teil-Serve
    b.complete(1, b"1111")
    assert b.serve(0, 8) == b"00001111"                 # ueber Segmentgrenze
    assert b.serve(2, 4) == b"0011"                     # Sub-Segment (Byte 2..5)

    # fail: inflight-Segment zurueck in den Pool (re-claimbar).
    b.fail(3)
    assert b.has(3) is False
    assert b.claim_from(3) == 3

    # advance: Plex bis Byte 8 konsumiert -> Fenster gleitet (base_seg=2), Seg0/1 evicted.
    b.complete(3, b"3333"); b.complete(4, b"4444"); b.complete(5, b"5555")
    b.advance(8)
    assert b.window_segs() == [2, 3, 4, 5, 6, 7]        # Seg6,7 neu im Fenster
    assert b.has(0) is False and b.has(1) is False      # evicted
    assert b.has(2) and b.has(5)                        # noch im Fenster
    assert b.claim_from(2) == 6                          # niedrigstes pending >=2 (2..5 done) = 6

    # reset (Seek): alles verwerfen, neue Basis.
    b.reset(24)                                          # base_seg=6
    assert b.window_segs() == [6, 7, 8, 9]              # auf Dateigroesse geclippt
    assert b.has(2) is False
    assert b.claim_from(6) == 6

    # next_for_run: CDN haelt EINE Verbindung ueber die Fensterkante hinweg (Connection-Reuse).
    TH = 3                                              # skip_threshold fuer die Tests
    bn = WindowBuffer(seg_size=4, window=3, size=40)   # segs 0..9, Fenster [0,1,2]
    bn.claim_from(0); bn.complete(0, b"....")
    assert bn.next_for_run(0, TH) == ("read", 1)        # naechstes frei -> selbe Verbindung
    bn.complete(1, b"....")
    assert bn.next_for_run(1, TH) == ("read", 2)
    bn.complete(2, b"....")
    assert bn.next_for_run(2, TH) == ("wait", None)     # Fensterkante: warten, NICHT schliessen
    bn.advance(4)                                       # base=1 -> Fenster [1,2,3]
    assert bn.next_for_run(2, TH) == ("read", 3)        # 3 jetzt frei im Fenster -> weiterlesen
    bn.complete(3, b"....")
    bn.reset(36)                                       # Seek (base=9): cur=3 abgehaengt
    assert bn.next_for_run(3, TH) == ("stop", None)     # nxt<base -> Run beenden
    bn3 = WindowBuffer(seg_size=4, window=20, size=40)
    assert bn3.next_for_run(9, TH) == ("stop", None)    # nxt > last_seg (EOF) -> stop

    # PLOW vs SKIP: nxt schon von WebDAV erledigt.
    bp = WindowBuffer(seg_size=4, window=10, size=80)   # segs 0..19
    bp.complete(1, b"X"); bp.complete(2, b"X")          # kurze erledigte Strecke (2) ab nxt=1
    assert bp.done_run(1) == 2
    assert bp.next_for_run(0, 3) == ("plow", 1)         # 2 < 3 -> PLOW (mitlesen statt reconnect)
    bp.complete(3, b"X")                                # jetzt Strecke 1,2,3 = 3
    assert bp.done_run(1) == 3
    assert bp.next_for_run(0, 3) == ("skip", None)      # 3 >= 3 -> SKIP (reconnect lohnt)

    # ready_ahead: zusammenhaengende fertige Segmente ab pos = Read-Ahead-Puffertiefe (Combine-Trigger).
    br = WindowBuffer(seg_size=4, window=8, size=80)   # segs 0..19
    for sg in (0, 1, 2):
        br.complete(sg, b"....")
    assert br.ready_ahead(0) == 3                       # 0,1,2 fertig, 3 nicht
    assert br.ready_ahead(2) == 1
    assert br.ready_ahead(3) == 0
    # try_claim: belegt idx wenn im Fenster & frei (Combine-Fortsetzung auf selber Verbindung).
    assert br.try_claim(5) is True
    assert br.try_claim(5) is False                     # schon inflight
    assert br.try_claim(0) is False                     # schon fertig
    assert br.try_claim(99) is False                    # ausserhalb Fenster

    # kurzes Last-Segment: size=18, seg_size=4 -> Seg4 = Byte 16..17 (len 2).
    b2 = WindowBuffer(seg_size=4, window=10, size=18)
    assert b2.window_segs() == [0, 1, 2, 3, 4]
    assert b2.seg_len(0) == 4 and b2.seg_len(4) == 2
    b2.claim_from(0)
    b2.complete(3, b"3333"); b2.complete(4, b"EE")
    assert b2.serve(14, 4) == b"33EE"                   # Byte 14..17 (2 aus Seg3 + 2 aus Seg4)

    print("OK: WindowBuffer claim_from/next_for_run/first_wanted/serve/advance/reset/short-tail")


if __name__ == "__main__":
    main()
