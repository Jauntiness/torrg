# Design: Light-by-default Streaming (torrg) — REV 2 (nach Opus-Review)

Status: ENTWURF v2 (2026-06-14). Rev 1 hatte 3 kritische Widersprueche (CDN-Takeover braucht
Dauer-Thread; heavy->light = voller Reconnect + Flapping; `cdn_ref` misst im Light-Pfad nur den
Plex-Abspiel-Takt, nicht die CDN-Kapazitaet). Diese Version behebt alle drei.

## Ziel & Prinzipien
1. **Leicht im Normalfall:** gesunder CDN-Stream = EINE Verbindung, sequenziell wiederverwendet,
   **keine per-Stream-Daemon-Threads, kein Read-Ahead-Window** → skaliert auf viele Streams.
2. **Schwer nur on-demand & EINSEITIG:** bei anhaltendem Stall einmal auf Heavy (`SegmentSwarm`)
   hochschalten — **nie im selben Playback zurueck** (kein Flapping). Heavy bleibt bis der Stream
   idle't. Gedeckelt per RAM-Budget.
3. **Kein CDN-Takeover mid-stream:** gewinnt beim Kaltstart WebDAV (CDN kalt), laeuft der Stream auf
   WebDAV weiter (47-60 Mbit/s reicht > Bitrate). CDN wird beim naechsten Reopen (Seek/Reconnect)
   wieder bevorzugt. KEIN Hintergrund-Warmer.
4. **Trigger = beobachteter Stall**, NICHT `cdn_ref`-Deviation. (`cdn_ref` bleibt INTERN im Swarm,
   wo Reads vom Client-Takt entkoppelt sind.)

## Kern: der bestehende Slot-Pfad IST schon der Light-Pfad
`get_stream_slot` + der `slot["lock"]`-Block in `app.py` (~828-896) ist bereits: 1 CDN-Verbindung,
sequenzieller Reuse, synchron im Handler-Thread, ueber Pausen offen, mit Native-Fallback. Wir
**erweitern** ihn um genau drei Dinge — neu in `lightstream.py` als testbare `LightSlot`-Klasse
(injizierte Opener, wie die Swarm-Tests), `_get` delegiert dorthin (Flag `LIGHTSTREAM=1`).

### LightSlot.read(start, length, timeout) -> bytes | None
Hält: `reader`+`pos` (aktuelle Quell-Verbindung), `slow_streak` (Zaehler), `mode` (light|heavy),
`heavy` (SegmentSwarm | None). Reads laufen SYNCHRON im Handler-Thread. Mutationen unter `lock`.

- **mode == heavy:** an `self.heavy.read(start,length,timeout)` delegieren. (Einseitig: bleibt heavy.)
- **Kaltstart** (kein `reader`) ODER **Seek** (`pos != start`): **Per-GET-Hedge-Open** — CDN oeffnen;
  liefert das 1. Byte nicht in `cold_grace`, WebDAV *fuer diesen GET* mitoeffnen (transienter Thread),
  erster gewinnt. Gewinner wird `reader`, `pos=start`. Verlierer schliessen. **Transient — kein
  Dauer-Thread, kein Takeover.** Auf dem Gewinner bleiben bis zum naechsten Reopen.
- **Sequenziell** (`pos == start`): aus `reader` lesen. **Stall-Erkennung:** dauert der Read >
  adaptivem `grace`, gilt der GET als „slow":
    - sofort WebDAV *fuer diesen GET* hedgen (one-shot, erster gewinnt) → Read wird trotzdem schnell.
    - `slow_streak += 1`. War der Read schnell → `slow_streak = 0`.
    - `slow_streak >= ESCALATE_N` UND RAM-Budget frei → **eskalieren**: `heavy = SegmentSwarm(...)`
      ab `pos`, `mode = heavy`. (Swarm `_anchored` startet sauber an der Position.)
- **Kurz/EOF:** liefert die Quelle weniger als `length` (Last-Segment/Truncation) → bis dahin
  ausliefern; truncation mid-stream → `reader` verwerfen, `None` zurueck (Handler nutzt Fallback).

### Wichtige Korrekturen ggü Rev 1
- **Kein Takeover** (C1): per-GET-Hedge ist echt transient; CDN-Bevorzugung passiert nur beim Reopen.
- **Einseitige Eskalation** (C2/C3): nie heavy→light im selben Playback → kein Reconnect-Flap.
- **Trigger = Stall, nicht `cdn_ref`** (M1): im windowlosen Light-Pfad ist Lesezeit = Client-Takt;
  ein wiederholt *wartender* GET ist das einzig valide CDN-Schwaeche-Signal. `cdn_ref`/Combine
  laufen erst INNERHALB des Heavy (dort korrekt).
- **Lock-Disziplin** (M2): Mode-Uebergang + `heavy`-Erzeugung unter `slot.lock`, idempotent
  (zweiter Request sieht `mode==heavy` und delegiert, erzeugt keinen 2. Swarm).

## Registry, Budgets, Lifecycle
- `light_slots[key] -> LightSlot`. `get_light_slot(key, meta, size)` erzeugt/holt (unter Registry-Lock).
- **RAM-Budget** (`SWARM_RAM_BUDGET`, z. B. 512 MiB): Eskalation nur wenn `belegt + window <= Budget`,
  sonst bleibt der Stream light (laeuft via CDN/Hedge, nur ohne Combine). In-use Heavy wird NIE
  evicted. Ersetzt `SWARM_MAX`.
- **Idle-Release** (`LIGHT_IDLE_RELEASE`, **wenige Minuten** — der Socket stirbt eh frueher, NICHT an
  URL-TTL koppeln): Slot ohne Read seit X → Verbindung schliessen + Slot verwerfen + Heavy freigeben.
  Ein Reaper, non-blocking `lock.acquire(blocking=False)` (wie `reaper_loop` app.py:142).
- **Scan-Schutz bleibt:** `_get` ruft LightSlot NUR fuer echte Body-Reads (`not in_probe_window`);
  head/tail-Scan-Reads laufen weiter ueber den Probe-Cache. UNVERAENDERT.

## Schnittstelle
```
class LightSlot:
    def __init__(self, key, size, seg, cdn_open, web_open, *, cold_grace_s, grace_fn,
                 escalate_n, make_heavy): ...        # make_heavy(pos)->SegmentSwarm | None (Budget)
    def read(self, start, length, timeout) -> bytes | None
    def close(self): ...
```
`_get` (body-read, nicht in_probe_window):
```
if LIGHTSTREAM:
    slot = get_light_slot(key, meta, size)
    data = slot.read(start, length, READ_TIMEOUT)
    if data is not None: <ausliefern>; return
    # None -> bestehender Single-Stream-Fallback
```

## Test-Plan (TDD, injizierte Fake-Opener)
1. **Kaltstart-Hedge:** kalter CDN (langsames 1. Byte) + schnelles WebDAV → Start via WebDAV, kein Warten; KEIN Dauer-Thread (Thread-Count flat nach dem Read).
2. **Gesund sequenziell:** schneller CDN → 1 Verbindung, CDN gewinnt, exakte Rekonstruktion, web nie geoeffnet.
3. **Per-GET-Hedge bei transientem Stall:** ein langsames Segment → WebDAV faengt's, danach wieder CDN, slow_streak reset.
4. **Einseitige Eskalation:** N langsame GETs → heavy erzeugt; danach bleibt heavy auch wenn CDN sich erholt (KEIN Flap).
5. **RAM-Budget:** Budget voll → keine Eskalation, Stream bleibt light + laeuft.
6. **Concurrency:** 50 parallele Light-Slots → alle exakt, Thread-Count ~flat (keine per-Stream-Threads).
7. **Seek:** Reopen mit Hedge, exakt.
8. **EOF/short read / timeout:** sauberes `None`→Fallback bzw. korrektes Last-Segment.
9. **Idempotenz unter Last:** zwei gleichzeitige Reads waehrend Eskalation → genau 1 Swarm, kein Leak.

## Rollout
1. `lightstream.py` + Tests (TDD), `LIGHTSTREAM=0` default → kein Prod-Impact.
2. Real-Benchmark: Kaltstart, gesund, Stall→Eskalation, viele parallele Streams, Resume-Reconnect.
3. `LIGHTSTREAM=1` deployen, A/B gegen `SWARM=1`, jederzeit zurueckschaltbar.
4. Nach Verifikation: `SWARM`-immer-an-Block, `SWARM_MAX`, `swarm_reaper_loop` entfernen;
   `SegmentSwarm` bleibt als Heavy-on-demand.
