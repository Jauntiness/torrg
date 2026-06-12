#!/usr/bin/env python3
# TDD-Test — _manage_combine: die Baseline-Tracking-State-Machine (User-Modell v2).
#   cdn_fast (jetzt) vs cdn_ref (gemerkte Normalitaet): wird CDN langsamer als sein
#   Normalwert UND die Nachfrage ist ungedeckt -> kurzer WebDAV-TEST. Half er (Skip-Events) ->
#   beide laufen weiter; half er nicht (nur Plow) -> Cooldown. Erholt sich CDN -> WebDAV raus.
#   Reine Logik, Zeit injiziert -> deterministisch (keine Threads: sources=[]).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swarm import SegmentSwarm


def mk():
    # Keine Quellen -> keine Worker-Threads; wir testen nur die State-Machine.
    s = SegmentSwarm(size=1000, seg_size=4, window=8, sources=[],
                     deviation_factor=1.6, test_window_s=3.0, cooldown_s=30.0, degrade_persist_s=2.0)
    return s


def set_speed(s, fast, ref):
    s.cdn_fast = fast      # "jetzt"
    s.cdn_ref = ref        # gemerkte Normalitaet (klein = schnell)


def mc(s, now):
    with s.cv:
        s._manage_combine(now)


def degrade(s, t0):
    """Hilfsfunktion: degradiert + Debounce (persist 2s) abwarten -> Test startet bei t0+2.1."""
    set_speed(s, 0.12, 0.05); s.demand_unmet = True
    mc(s, t0)              # setzt _degraded_since
    mc(s, t0 + 2.1)        # >= persist -> Test


def main():
    # 1) CDN normal (fast≈slow) -> kein Test, kein Combine.
    s = mk(); set_speed(s, 0.05, 0.05); s.demand_unmet = True
    mc(s, 100.0)
    assert not s.combine_active and not s.testing, "normaler CDN -> nichts"

    # 2) CDN ANHALTEND degradiert + Nachfrage ungedeckt -> Test startet (nach Debounce).
    s = mk(); degrade(s, 100.0)
    assert s.testing and s.combine_active, "anhaltend degradiert -> Test (WebDAV an)"
    assert abs(s.test_until - (102.1 + 3.0)) < 1e-9, "Test-Fenster 3s ab Trigger"

    # 2b) DEBOUNCE: kurz degradiert (< persist) -> KEIN Test.
    s = mk(); set_speed(s, 0.12, 0.05); s.demand_unmet = True
    mc(s, 100.0); mc(s, 101.0)        # nur 1s < 2s persist
    assert not s.testing, "kurzzeitige Degradierung -> noch kein Test"

    # 2c) JITTER: degradiert -> wieder normal (Timer-Reset) -> erneut degradiert -> Timer faengt neu an.
    s = mk(); set_speed(s, 0.12, 0.05); s.demand_unmet = True
    mc(s, 100.0)                       # _degraded_since=100
    set_speed(s, 0.05, 0.05); mc(s, 100.5)   # normal -> reset
    assert s._degraded_since is None, "Erholung resettet Debounce-Timer"
    set_speed(s, 0.12, 0.05); mc(s, 101.0); mc(s, 102.0)   # neu seit 101, erst 1s -> noch kein Test
    assert not s.testing, "Jitter (reset) verhindert Test"

    # 2d) degradiert ANHALTEND ABER Nachfrage gedeckt -> KEIN Test.
    s = mk(); set_speed(s, 0.12, 0.05); s.demand_unmet = False
    mc(s, 100.0); mc(s, 102.1)
    assert not s.testing and not s.combine_active, "gedeckte Nachfrage -> kein Test"

    # 3) Test endet MIT Skips -> WebDAV half -> combine bleibt aktiv.
    s = mk(); degrade(s, 100.0)
    s.skip_count = 2
    mc(s, 200.0)           # weit nach Test-Fenster
    assert s.combine_active and not s.testing, "Skips -> beide laufen weiter"

    # 4) Test endet OHNE Skips (nur Plow) -> WebDAV half nicht -> aus + Cooldown.
    s = mk(); degrade(s, 100.0)
    s.skip_count = 0; s.plow_count = 5
    mc(s, 200.0)
    assert not s.combine_active and not s.testing, "kein Skip -> WebDAV raus"
    assert s.cooldown_until == 200.0 + 30.0, "Cooldown gesetzt"

    # 5) Im Cooldown: anhaltend degradiert -> KEIN neuer Test; nach Cooldown wieder.
    mc(s, 210.0)           # < cooldown_until (230)
    assert not s.testing, "Cooldown -> kein Re-Test"
    set_speed(s, 0.12, 0.05); s.demand_unmet = True
    mc(s, 240.0); mc(s, 242.1)
    assert s.testing and s.combine_active, "nach Cooldown -> Re-Test"

    # 6) combine aktiv, CDN erholt sich (fast≈slow) -> WebDAV automatisch raus.
    s = mk(); s.combine_active = True; s.testing = False; set_speed(s, 0.05, 0.05); s.demand_unmet = True
    mc(s, 300.0)
    assert not s.combine_active, "CDN wieder normal -> WebDAV raus"

    # 7) Exponential-Backoff: nutzlose Tests -> wachsender Cooldown; Reset bei nuetzlichem Test; Cap.
    def useless_cycle(s, t):
        set_speed(s, 0.05, 0.05); mc(s, t - 0.1)          # erst normal -> Debounce-Timer reset
        set_speed(s, 0.12, 0.05); s.demand_unmet = True
        mc(s, t); mc(s, t + 2.1)                          # anhaltend degradiert -> Test
        assert s.testing, f"Test sollte starten (t={t})"
        s.skip_count = 0                                  # Test bringt nichts
        end = t + 5.2
        mc(s, end)
        return s.cooldown_until - end
    s = mk()
    assert abs(useless_cycle(s, 100.0) - 30.0) < 1e-9, "1. nutzloser Test -> 30s"
    assert abs(useless_cycle(s, 140.0) - 60.0) < 1e-9, "2. -> 60s"
    assert abs(useless_cycle(s, 210.0) - 120.0) < 1e-9, "3. -> 120s"
    # nuetzlicher Test (Skips) -> Backoff-Reset
    set_speed(s, 0.05, 0.05); mc(s, 339.9)
    set_speed(s, 0.12, 0.05); s.demand_unmet = True; mc(s, 340.0); mc(s, 342.1)
    s.skip_count = 3; mc(s, 345.2)
    assert s.useless_tests == 0, "Skips -> Backoff zurueckgesetzt"
    # CDN erholt -> combine raus; naechster nutzloser Test wieder bei 30s
    set_speed(s, 0.05, 0.05); mc(s, 346.0)
    assert abs(useless_cycle(s, 400.0) - 30.0) < 1e-9, "nach Reset -> wieder 30s"
    # Cap: viele nutzlose Tests -> gedeckelt auf cooldown_max (300).
    s2 = mk(); s2.useless_tests = 10
    set_speed(s2, 0.12, 0.05); s2.demand_unmet = True
    mc(s2, 500.0); mc(s2, 502.1); s2.skip_count = 0; mc(s2, 505.2)
    assert abs((s2.cooldown_until - 505.2) - 300.0) < 1e-9, "Cooldown auf 300s gedeckelt"

    print("OK: _manage_combine — Debounce gegen Jitter, anhaltende Deviation triggert Test, "
          "Skip=behalten, kein-Skip=Cooldown, Erholung=raus")


if __name__ == "__main__":
    main()
