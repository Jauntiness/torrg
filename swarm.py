#!/usr/bin/env python3
"""swarm.py — Dual-Source-Read-Ahead-Swarm fuer den TorBox-Shim.

Plex/rclone liest sequentiell in festen 1-MiB-Chunks. Um die Durchsaetze von CDN
(prio 1) und nativem TorBox-WebDAV (prio 2) zu KOMBINIEREN, ohne hartkodierte
Mbps-Schwellen, fuellen wir ein begrenztes Fenster VOR der aktuellen Leseposition
aus beiden Quellen parallel:

  - WindowBuffer  (hier, pure logic): haelt die Segmente des Fensters, verteilt sie
    per Work-Stealing (near=CDN/dringend, far=WebDAV/Tail) und gibt fertige Bytes
    geordnet heraus. Ist das Fenster voll (alles done/inflight), liefert claim() None
    -> der jeweilige Worker pausiert von selbst. Saettigt CDN die Leitung, haelt es
    das Fenster allein voll -> WebDAV findet keine Arbeit -> pausiert EMERGENT.
    Ist CDN langsam, bleibt Arbeit liegen -> WebDAV greift -> Durchsaetze addieren sich.

Nur das Fenster (Segmentzahl) ist ein Parameter (Speicher-Bound), KEINE Geschwindigkeit.
"""
import threading
import time


class WindowBuffer:
    """Sequentielles Read-Ahead-Fenster ueber feste Segmente. Thread-Safety macht der
    aufrufende Swarm-Shell (Lock); diese Klasse ist reine Logik."""

    def __init__(self, seg_size, window, size):
        self.seg_size = seg_size
        self.window = window
        self.size = size
        self.last_seg = (size - 1) // seg_size if size > 0 else -1  # hoechster gueltiger Index
        self.base_seg = 0
        self._data = {}        # idx -> bytes (fertig)
        self._inflight = set() # idx -> aktuell von einem Worker geholt

    # ── Fenster-Mathematik ──────────────────────────────────────────────────────
    def seg_len(self, idx):
        """Laenge von Segment idx (Last-Segment ggf. kuerzer)."""
        start = idx * self.seg_size
        return min(self.seg_size, self.size - start)

    def window_segs(self):
        """Segment-Indizes im aktuellen Fenster, auf die Dateigroesse geclippt."""
        hi = min(self.base_seg + self.window - 1, self.last_seg)
        return list(range(self.base_seg, hi + 1))

    # ── Work-Stealing (Run-basiert: jede Quelle streamt eine zusammenhaengende Strecke) ──
    def _pending(self):
        return [s for s in self.window_segs() if s not in self._data and s not in self._inflight]

    def claim_from(self, target):
        """Startsegment fuer einen neuen Run waehlen: niedrigstes pending-Segment >= target
        (damit die Quelle VORWAERTS Platz hat), sonst das niedrigste pending ueberhaupt
        (Front-Luecken auffuellen). None wenn nichts frei -> Worker pausiert (emergent).
        CDN ruft target=base (Front); die Hedge-Quelle nutzt first_wanted statt claim_from."""
        pending = self._pending()
        if not pending:
            return None
        ahead = [s for s in pending if s >= target]
        idx = ahead[0] if ahead else pending[0]
        self._inflight.add(idx)
        return idx

    def done_run(self, idx):
        """Laenge der zusammenhaengenden schon-erledigten/inflight Strecke ab idx (Segmente, die
        CDN beim Durchziehen redundant mitlesen wuerde)."""
        n = 0
        while (idx + n) <= self.last_seg and ((idx + n) in self._data or (idx + n) in self._inflight):
            n += 1
        return n

    def next_for_run(self, cur, skip_threshold):
        """Wie es nach Segment cur auf DERSELBEN offenen Verbindung weitergeht:
          ("read", idx) -> freies Segment: beanspruchen + lesen.
          ("plow", idx) -> schon erledigt, aber kurze Strecke -> mitlesen (re-download, billiger als
                           Reconnect). PLOW-Event = WebDAV hat hier NICHT geholfen (Doppellast).
          ("skip", None)-> schon erledigt, grosse Strecke -> Reconnect lohnt. SKIP-Event = WebDAV hat
                           CDN echte Arbeit erspart (es HILFT).
          ("wait", None)-> jenseits der Fensterkante: warten (Verbindung OFFEN halten).
          ("stop", None)-> Run terminal beenden (EOF / Seek).
        skip_threshold = ab wie vielen erledigten Segmenten sich der Reconnect (TTFB) lohnt."""
        if cur < self.base_seg or cur > self.base_seg + self.window - 1:
            return ("stop", None)                       # Seek: Run liegt ausserhalb des Fensters
        nxt = cur + 1
        if nxt > self.last_seg:
            return ("stop", None)                       # EOF
        if nxt < self.base_seg:
            return ("stop", None)                       # Seek hat das Fenster weitergeschoben
        if nxt > self.base_seg + self.window - 1:
            return ("wait", None)                       # jenseits der Kante -> Verbindung offen halten
        if nxt not in self._data and nxt not in self._inflight:
            self._inflight.add(nxt)
            return ("read", nxt)                         # frei -> beanspruchen + lesen
        # nxt schon erledigt/inflight (WebDAV): durchziehen oder ueberspringen?
        if self.done_run(nxt) >= skip_threshold:
            return ("skip", None)                        # grosse Strecke -> Reconnect (WebDAV half)
        return ("plow", nxt)                             # kurze Strecke -> mitlesen (WebDAV half nicht)

    def ready_ahead(self, seg):
        """Anzahl zusammenhaengend fertiger Segmente ab seg = Tiefe des Read-Ahead-Puffers vor
        der Leseposition. Sinkt sie, kommt CDN mit dem Verbrauch nicht nach -> Combine-Trigger."""
        n = 0
        while (seg + n) in self._data:
            n += 1
        return n

    def try_claim(self, idx):
        """idx beanspruchen, wenn im Fenster & frei (nicht fertig, nicht inflight). Fuer die
        Combine-Quelle, die ihre Partition auf DERSELBEN Verbindung fortsetzt."""
        if idx in self.window_segs() and idx not in self._data and idx not in self._inflight:
            self._inflight.add(idx)
            return True
        return False

    def first_wanted(self):
        """Niedrigstes Segment im Fenster, das noch NICHT fertig ist — auch wenn es schon
        inflight ist. Fuer die Hedge-Quelle (WebDAV), die bei Starvation genau das vom
        langsamen CDN blockierte Kopf-Segment redundant mitzieht (erster gewinnt)."""
        for s in self.window_segs():
            if s not in self._data:
                return s
        return None

    def complete(self, idx, data):
        """Geholte Bytes ablegen; Segment ist fertig. Idempotent: ist es schon fertig
        (Hedge-Rennen, langsamere Quelle kommt als zweite an), bleibt der erste Treffer."""
        self._inflight.discard(idx)
        if idx not in self._data:
            self._data[idx] = data

    def fail(self, idx):
        """Holen fehlgeschlagen -> zurueck in den Pool (re-claimbar)."""
        self._inflight.discard(idx)

    def has(self, idx):
        return idx in self._data

    # ── geordnete Ausgabe ───────────────────────────────────────────────────────
    def serve(self, start, length):
        """Bytes [start, start+length) zusammensetzen, sobald ALLE abgedeckten
        Segmente fertig sind; sonst None (Aufrufer wartet/holt notfalls selbst)."""
        end = start + length
        first = start // self.seg_size
        last = (end - 1) // self.seg_size
        if any(s not in self._data for s in range(first, last + 1)):
            return None
        out = bytearray()
        for s in range(first, last + 1):
            seg_start = s * self.seg_size
            lo = max(start, seg_start) - seg_start
            hi = min(end, seg_start + self.seg_len(s)) - seg_start
            out += self._data[s][lo:hi]
        return bytes(out)

    # ── Fenster bewegen ─────────────────────────────────────────────────────────
    def advance(self, consumed_byte):
        """Plex hat bis consumed_byte gelesen -> Fenster gleitet, alte Segmente raus."""
        new_base = consumed_byte // self.seg_size
        if new_base <= self.base_seg:
            return
        self.base_seg = new_base
        self._evict_below(new_base)

    def reset(self, start_byte):
        """Seek: alles verwerfen, Fenster auf start_byte neu setzen."""
        self.base_seg = start_byte // self.seg_size
        self._data.clear()
        self._inflight.clear()

    def _evict_below(self, base):
        for s in [s for s in self._data if s < base]:
            del self._data[s]
        self._inflight = {s for s in self._inflight if s >= base}


def _readexact(reader, n):
    """Genau n Bytes aus einem Forward-Stream lesen (HTTP kann chunked kurz liefern)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = reader.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# ── CDN-Referenz: "so schnell kann CDN normalerweise" (Segmentzeit; kleiner = schneller). ──────
# Asymmetrische EWMA: lernt SCHNELL nach unten (besseren/schnelleren Wert merken), vergisst nur
# TRAEGE nach oben. So wird auch ein File, dessen CDN von Anfang an langsam ist, als Abweichung
# erkannt (vs. die gemerkte schnelle Normalitaet). Modulglobal -> ein frischer Stream erbt die
# Referenz aus vorigen Files; bei dauerhaft langsamer Leitung steigt sie traege nach -> beruhigt sich.
REF_ALPHA_DOWN = 0.25    # schneller Wert -> schnell uebernehmen
REF_ALPHA_UP = 0.01      # langsamer Wert -> nur sehr traege vergessen (~100 Samples)
_cdn_ref_global = None
_cdn_ref_lock = threading.Lock()


def _ref_update(prev, dt):
    if prev is None:
        return dt
    a = REF_ALPHA_DOWN if dt < prev else REF_ALPHA_UP
    return (1 - a) * prev + a * dt


def _cdn_ref_seed():
    with _cdn_ref_lock:
        return _cdn_ref_global


def _cdn_ref_publish(val):
    global _cdn_ref_global
    with _cdn_ref_lock:
        _cdn_ref_global = val


def _cdn_ref_reset():            # nur fuer Tests (Modulglobal zwischen Faellen leeren)
    global _cdn_ref_global
    with _cdn_ref_lock:
        _cdn_ref_global = None


class SegmentSwarm:
    """Threaded Shell um WindowBuffer im Hybrid/Hedge-Modell.

    sources = [(name, opener), ...] nach Prioritaet. opener(offset) -> reader (.read(n)/.close()),
    ein FORWARD-Stream ab offset (offene Verbindung).

    Quelle 0 (CDN, prio 1) ist die EINZIGE praeemptive Quelle: sie streamt zusammenhaengende
    Strecken aus dem Fenster ab base (Connection-Reuse, im gesunden Fall die ganze Datei auf EINER
    Verbindung -> exakt Einzel-CDN-Speed, KEIN Overhead). Alle weiteren Quellen (WebDAV, prio 2)
    sind LAZY: sie laufen NUR, wenn read() hungert — d.h. das Kopf-Segment kommt nicht innerhalb
    des adaptiven Grace (EWMA der CDN-Segmentzeit x hedge_k; KEIN fixer Mbit/Byte-Wert). Dann zieht
    die Hedge-Quelle das blockierte Kopf-Segment redundant mit (erster gewinnt, complete idempotent)
    und fuellt vorwaerts -> Stall-Rescue + Kombination. Hungert read() nicht, bleibt WebDAV still.
    """
    FAIL_BACKOFF = 0.1   # s Pause nach Fehler, damit eine tote Quelle nicht busy-loopt
    EWMA_ALPHA = 0.3     # Glaettung TTFB/Steady (Plow-Skip-Schwelle)
    ALPHA_FAST = 0.4     # kurze Zeitskala ("jetzt")
    ALPHA_SLOW = 0.05    # lange Zeitskala (Baseline "normal")

    def __init__(self, size, seg_size, window, sources,
                 hedge_k=2.0, cold_grace_s=0.3, hedge_min=0.05, refill_frac=0.85,
                 deviation_factor=1.6, test_window_s=3.0, cooldown_s=30.0, degrade_persist_s=2.0,
                 cooldown_max_s=300.0):
        self.size = size
        self.seg_size = seg_size
        self.window = window
        self.buf = WindowBuffer(seg_size, window, size)
        self.cv = threading.Condition()
        self.sources = list(sources)
        self.closed = False
        self._anchored = False           # erst nach dem ersten read() steht der echte Play-Offset
        self.starved = False             # read() haengt hart am Kopf-Segment -> Hedge (Kopf mitziehen)
        self.hedge_k = hedge_k
        self.hedge_min = hedge_min
        # Kaltstart-Grace: solange CDN noch ungemessen ist (cdn_ewma None), nach so kurzer Zeit schon
        # WebDAV mitrennen lassen. CDN ist kalt oft 3-38s (Edge-Provisioning), WebDAV stabil ~2s ->
        # der Schnellere gewinnt den Kopf, CDN uebernimmt danach den Durchsatz.
        self.cold_grace_s = cold_grace_s
        # PLOW-vs-SKIP-Schwelle: cdn_ewma = Steady-Segmentzeit (ohne TTFB), cdn_ttfb = 1. Read je Run.
        self.cdn_ewma = None
        self.cdn_ttfb = None
        # BASELINE-TRACKING: cdn_fast=jetzt (kurze EWMA), cdn_ref=normal (asymmetrisch, lernt schnell-
        # runter/vergisst-traege-rauf, modulglobal geseedet). Wird cdn_fast deutlich groesser als
        # cdn_ref (CDN langsamer als seine gemerkte Normalitaet) UND Nachfrage ungedeckt -> WebDAV-Test.
        self.cdn_fast = None
        self.cdn_ref = _cdn_ref_seed()
        self.deviation_factor = deviation_factor
        self.test_window_s = test_window_s
        self.cooldown_s = cooldown_s
        self.degrade_persist_s = degrade_persist_s   # so lange muss CDN ANHALTEND langsam sein (Anti-Jitter)
        self._degraded_since = None
        # Exponential-Backoff: findet ein Test nichts (z.B. schwankende/volle Leitung, wo WebDAV eh
        # nicht hilft), waechst der Cooldown 30->60->120->...->cooldown_max. Ein hilfreicher Test
        # (Skips) setzt zurueck -> sofort wieder reaktiv.
        self.cooldown_max_s = cooldown_max_s
        self.useless_tests = 0
        self.combine_active = False      # WebDAV laeuft mit (im Test ODER bestaetigt nuetzlich)
        self.testing = False
        self.test_until = 0.0
        self.cooldown_until = 0.0
        self.skip_count = 0              # CDN hat WebDAV-Strecken uebersprungen = WebDAV half
        self.plow_count = 0             # CDN hat durchgeplowt = WebDAV half nicht (Doppellast)
        self.refill_seg = max(1, int(window * refill_frac))
        self.demand_unmet = False        # read() musste zuletzt warten (Nachfrage > aktuelles Angebot)
        self.threads = []
        for i in range(len(self.sources)):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True)
            t.start()
            self.threads.append(t)

    def _grace(self):
        if self.cdn_ewma is None:
            return self.cold_grace_s                 # Kaltstart: WebDAV sofort mitrennen (racet CDN)
        return max(self.hedge_min, self.cdn_ewma * self.hedge_k)

    def _engaged(self):
        return self.starved or self.combine_active

    def _manage_combine(self, now):
        """State-Machine (unter cv): Baseline-Deviation -> Test -> behalten/Cooldown. now injizierbar."""
        degraded = (self.cdn_fast is not None and self.cdn_ref is not None
                    and self.cdn_fast > self.cdn_ref * self.deviation_factor)
        # Debounce: nur eine ANHALTENDE Verlangsamung zaehlt (kurzer Jitter setzt den Timer, ist aber
        # vorbei, bevor degrade_persist_s um ist -> kein Test).
        if degraded:
            if self._degraded_since is None:
                self._degraded_since = now
        else:
            self._degraded_since = None
        sustained = degraded and (now - self._degraded_since >= self.degrade_persist_s)
        prev = self.combine_active
        if self.testing:
            if now >= self.test_until:
                self.testing = False
                if self.skip_count > 0:
                    self.combine_active = True           # WebDAV half (Skips) -> beide laufen weiter
                    self.useless_tests = 0               # erfolgreich -> Backoff zuruecksetzen
                else:
                    self.combine_active = False          # nur Plow -> nutzlos -> raus + Backoff waechst
                    self.useless_tests += 1
                    cd = min(self.cooldown_max_s, self.cooldown_s * (2 ** (self.useless_tests - 1)))
                    self.cooldown_until = now + cd
        elif self.combine_active:
            if not (degraded and self.demand_unmet):     # CDN wieder normal / Nachfrage gedeckt -> raus
                self.combine_active = False
        else:
            if sustained and self.demand_unmet and now >= self.cooldown_until:
                self.testing = True                      # ANHALTEND langsam -> kurzer Test starten
                self.combine_active = True
                self.test_until = now + self.test_window_s
                self.skip_count = 0
                self.plow_count = 0
        if self.combine_active != prev:
            self.cv.notify_all()

    @staticmethod
    def _ewma(prev, dt):
        return dt if prev is None else (1 - SegmentSwarm.EWMA_ALPHA) * prev + SegmentSwarm.EWMA_ALPHA * dt

    def _skip_threshold(self):
        """Ab wie vielen schon-erledigten Segmenten lohnt sich fuer CDN ein Reconnect (Skip)
        gegenueber dem Durchziehen (Plow)? = TTFB / Segmentzeit. Solange ungemessen: nie skippen
        (immer plow) -> CDN-Verbindung bleibt am Stueck, kein Reconnect-Sturm."""
        if self.cdn_ewma and self.cdn_ttfb:
            return max(1, round(self.cdn_ttfb / self.cdn_ewma))
        return self.window + 1

    # ── CDN (prio 1): praeemptiv, zusammenhaengende Strecken ab base ─────────────────────
    def _cdn_worker(self, opener):
        with self.cv:                       # erst starten, wenn read() den Play-Offset gesetzt hat
            while not self.closed and not self._anchored:
                self.cv.wait()              # sonst oeffnet CDN spekulativ bei Offset 0 (Fehl-Open)
        while True:
            with self.cv:
                idx = None
                while not self.closed:
                    idx = self.buf.claim_from(self.buf.base_seg)
                    if idx is not None:
                        break
                    self.cv.wait()
                if self.closed:
                    return
            reader = None
            cur = idx
            seg_in_run = 0
            try:
                reader = opener(cur * self.seg_size)
                while True:
                    t0 = time.monotonic()
                    data = _readexact(reader, self.buf.seg_len(cur))
                    if len(data) != self.buf.seg_len(cur):
                        raise IOError(f"short read @ seg {cur}")
                    seg_in_run += 1
                    with self.cv:
                        now = time.monotonic()
                        dt = now - t0
                        if seg_in_run == 1:   # 1. Read = Verbindungsaufbau (TTFB)
                            self.cdn_ttfb = self._ewma(self.cdn_ttfb, dt)
                        else:                 # ab dem 2. Read = Steady-State-Segmentzeit
                            self.cdn_ewma = self._ewma(self.cdn_ewma, dt)
                            # Baseline-Deviation (nur Steady-Reads, kein TTFB): cdn_fast = jetzt,
                            # cdn_ref = gemerkte Normalitaet (asymmetrisch + modulglobal publiziert).
                            self.cdn_fast = (dt if self.cdn_fast is None
                                             else (1 - self.ALPHA_FAST) * self.cdn_fast + self.ALPHA_FAST * dt)
                            self.cdn_ref = _ref_update(self.cdn_ref, dt)
                            _cdn_ref_publish(self.cdn_ref)
                        self.buf.complete(cur, data)      # idempotent (bei Plow: no-op)
                        self._manage_combine(now)         # Baseline-Test-State-Machine
                        self.cv.notify_all()
                        # Auf DERSELBEN Verbindung weiter; Plow-vs-Skip steckt in next_for_run.
                        stop = False
                        while True:
                            if self.closed:
                                stop = True; break
                            action, nxt = self.buf.next_for_run(cur, self._skip_threshold())
                            if action == "read":
                                cur = nxt; break          # freies Segment -> lesen
                            if action == "plow":
                                self.plow_count += 1      # WebDAV half hier nicht (Doppellast)
                                cur = nxt; break          # kurze erledigte Strecke -> mitlesen
                            if action == "skip":
                                self.skip_count += 1      # WebDAV hat CDN Arbeit erspart -> Reconnect
                                stop = True; break
                            if action == "stop":
                                stop = True; break        # EOF / Seek (terminal)
                            self.cv.wait()                # "wait": Kante -> Verbindung offen halten
                        if stop:
                            break
            except Exception:
                with self.cv:
                    self.buf.fail(cur)
                    self.cv.notify_all()
                time.sleep(self.FAIL_BACKOFF)
            finally:
                if reader is not None:
                    try: reader.close()
                    except Exception: pass

    # ── WebDAV (prio 2): lazy. Laeuft nur, wenn _engaged(): ────────────────────────────────
    #   starved         -> harter Stall: Kopf-Segment redundant mitziehen (Failover, rettet Position).
    #   combine_active  -> Baseline-Test/bestaetigt: WebDAV fuellt WEIT VORN parallel mit (ab Fenster-
    #                      mitte). Ob es summiert, entscheidet CDN per Plow-vs-Skip; die Skip/Plow-
    #                      Bilanz steuert wiederum, ob combine_active bleibt (_manage_combine).
    def _hedge_worker(self, opener):
        while True:
            with self.cv:
                while not self.closed and not self._engaged():
                    self.cv.wait()              # still, solange CDN allein reicht (Puffer voll)
                if self.closed:
                    return
                if self.starved:
                    idx = self.buf.first_wanted()                       # Kopf (auch wenn CDN inflight)
                else:
                    idx = self.buf.claim_from(self.buf.base_seg + self.window // 2)  # weit vorn
            if idx is None:
                with self.cv:
                    if not self.closed and self._engaged():
                        self.cv.wait(timeout=0.05)
                continue
            reader = None
            cur = idx
            try:
                reader = opener(cur * self.seg_size)
                while True:
                    data = _readexact(reader, self.buf.seg_len(cur))
                    if len(data) != self.buf.seg_len(cur):
                        raise IOError(f"short read @ seg {cur}")
                    with self.cv:
                        self.buf.complete(cur, data)   # idempotent: gewinnt ggf. gegen langsamen CDN
                        self.cv.notify_all()
                        if self.closed or not self._engaged() or not self.buf.try_claim(cur + 1):
                            break
                        cur += 1
            except Exception:
                with self.cv:
                    self.buf.fail(cur)
                    self.cv.notify_all()
                time.sleep(self.FAIL_BACKOFF)
            finally:
                if reader is not None:
                    try: reader.close()
                    except Exception: pass

    def _worker(self, i):
        name, opener = self.sources[i]
        if i == 0:
            self._cdn_worker(opener)
        else:
            self._hedge_worker(opener)

    def read(self, start, length, timeout):
        """Blockierend bis die Bytes [start, start+length) vom Swarm geholt sind.
        None bei Timeout. Erkennt Seeks (Position ausserhalb des Fensters) und resettet."""
        deadline = time.monotonic() + timeout
        with self.cv:
            seg = start // self.seg_size
            win = self.buf.window_segs()
            if not self._anchored or not win or seg < win[0] or seg > self.buf.base_seg + self.buf.window - 1:
                self.buf.reset(start)     # erster Read / Seek: Fenster auf Play-Offset verankern
                self._anchored = True     # weckt den CDN-Worker -> oeffnet direkt am richtigen Offset
                self.cv.notify_all()
            wait_start = None             # wann wir anfingen, auf DIESES Segment zu warten
            while True:
                data = self.buf.serve(start, length)
                if data is not None:
                    if wait_start is None:
                        # SOFORT bedient (Treffer): Nachfrage gedeckt, sofern Puffer auch tief genug.
                        if self.buf.ready_ahead(self.buf.base_seg) >= self.refill_seg:
                            self.demand_unmet = False
                        if self.starved:
                            self.starved = False
                            self.cv.notify_all()
                    else:
                        self.demand_unmet = True          # musste warten -> Nachfrage > aktuelles Angebot
                    return data
                now = time.monotonic()
                if wait_start is None:
                    wait_start = now
                else:
                    self.demand_unmet = True              # warten = ungedeckte Nachfrage (gated den Test)
                    if not self.starved and (now - wait_start) >= self._grace():
                        self.starved = True               # harter Stall am Kopf -> Hedge sofort (Failover)
                        self.cv.notify_all()
                remaining = deadline - now
                if remaining <= 0:
                    return None
                self.cv.wait(timeout=min(remaining, self._grace()))

    def advance(self, consumed_byte):
        with self.cv:
            self.buf.advance(consumed_byte)
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
