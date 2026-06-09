#!/usr/bin/env python3
# TDD-Test — PlaybackIntent: Window-Read-Misses duerfen materialisieren, wenn sie nach
# PLAYBACK aussehen (gleicher File wird wiederholt gehaemmert), aber NICHT bei Library-Scans
# (viele VERSCHIEDENE Files) und nie ueber das Stunden-Budget (429-Schutz, 60/h-API-Limit).
# Hintergrund: 2026-06-06 One-Piece-Incident — expired File ohne Head-Probe konnte nie
# starten, weil Window-Reads kategorisch kein Re-Add ausloesen durften.
import os, sys
os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import PlaybackIntent

def main():
    # 1) Einzelnes Playback: gleicher Key wiederholt -> ab REPEAT_N-tem Miss Grant
    pi = PlaybackIntent(repeat_n=3, repeat_window=30, storm_keys=4, storm_window=120,
                        budget_per_hour=12, cooldown=600)
    t = 1000.0
    assert pi.miss("hashA", t) is False, "1. Miss: noch kein Intent"
    assert pi.miss("hashA", t + 1) is False, "2. Miss: noch kein Intent"
    assert pi.miss("hashA", t + 2) is True, "3. Miss in 30s: Playback-Intent -> Grant"

    # 2) Cooldown: nach Grant fuer denselben Key erstmal kein weiterer Grant (toter Hash-Schutz)
    assert pi.miss("hashA", t + 3) is False, "direkt nach Grant: Cooldown"
    assert pi.miss("hashA", t + 4) is False
    # nach Ablauf des Cooldowns wieder moeglich
    assert pi.miss("hashA", t + 700) is False
    assert pi.miss("hashA", t + 701) is False
    assert pi.miss("hashA", t + 702) is True, "Cooldown abgelaufen -> wieder Grant"

    # 3) Scan-Storm: viele VERSCHIEDENE Keys in kurzer Zeit -> Deny (auch bei 3+ Retries je Key)
    pi = PlaybackIntent(repeat_n=3, repeat_window=30, storm_keys=4, storm_window=120,
                        budget_per_hour=12, cooldown=600)
    t = 2000.0
    for i, k in enumerate(["k1", "k2", "k3", "k4", "k5"]):
        for r in range(3):
            granted = pi.miss(k, t + i * 5 + r)
            if i >= 3:   # ab dem 4. distinct Key ist der Storm erkannt
                assert granted is False, f"Storm erkannt, aber {k} bekam Grant"

    # 4) Zwei PARALLELE Playbacks (2 distinct Keys) sind KEIN Storm -> beide kriegen Grant
    pi = PlaybackIntent(repeat_n=3, repeat_window=30, storm_keys=4, storm_window=120,
                        budget_per_hour=12, cooldown=600)
    t = 3000.0
    g1 = [pi.miss("filmA", t + i) for i in range(3)]
    g2 = [pi.miss("filmB", t + 0.5 + i) for i in range(3)]
    assert g1 == [False, False, True], g1
    assert g2 == [False, False, True], g2

    # 5) Stunden-Budget: nicht mehr als budget_per_hour Grants
    pi = PlaybackIntent(repeat_n=3, repeat_window=30, storm_keys=99, storm_window=120,
                        budget_per_hour=2, cooldown=600)
    t = 4000.0
    # Key 1 + 2 verbrauchen das Budget (zeitlich entzerrt, kein Storm: storm_keys=99)
    for j, k in enumerate(["b1", "b2"]):
        base = t + j * 200
        assert [pi.miss(k, base + i) for i in range(3)][-1] is True, f"{k} sollte Grant kriegen"
    base = t + 500
    assert [pi.miss("b3", base + i) for i in range(3)][-1] is False, "Budget erschoepft -> Deny"
    # nach >1h ist Budget wieder frei
    base = t + 4000
    assert [pi.miss("b4", base + i) for i in range(3)][-1] is True, "Budget nach 1h wieder frei"

    # 6) Alte Misses zaehlen nicht: 3 Misses ueber > repeat_window verteilt -> kein Grant
    pi = PlaybackIntent(repeat_n=3, repeat_window=30, storm_keys=4, storm_window=120,
                        budget_per_hour=12, cooldown=600)
    t = 5000.0
    assert pi.miss("slow", t) is False
    assert pi.miss("slow", t + 40) is False
    assert pi.miss("slow", t + 80) is False, "Misses zu weit auseinander -> kein Playback-Muster"

    print("OK: PlaybackIntent (repeat/cooldown/storm/parallel/budget/pruning)")

if __name__ == "__main__":
    main()
