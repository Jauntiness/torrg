#!/usr/bin/env python3
"""Light-by-default Streaming-Slot (torrg) — siehe DESIGN_lightstream.md (REV 2).

Gesunder Stream = EINE Quell-Verbindung, sequenziell im Handler-Thread wiederverwendet,
KEINE per-Stream-Daemon-Threads, kein Read-Ahead-Window -> skaliert auf viele Streams.

Kaltstart/Seek = Per-GET-Hedge: CDN zuerst; liefert es das 1. Byte nicht in `cold_grace`,
WebDAV *fuer diesen GET* mitoeffnen, erster gewinnt. Verlierer schliesst sich selbst (kein
Reaper-Thread, kein Takeover). CDN-Bevorzugung passiert nur beim naechsten Reopen.

(Stall-Hedge auf sequenziellen Reads + einseitige Eskalation auf SegmentSwarm folgen in
spaeteren TDD-Batches — diese Datei deckt zunaechst Kaltstart/Reuse/Seek/Thread-Disziplin.)
"""
import threading
import time


class LightSlot:
    def __init__(self, cdn_open, web_open, cold_grace_s=0.3, *,
                 grace_s=0.5, escalate_n=3, make_heavy=None):
        self.cdn_open = cdn_open
        self.web_open = web_open
        self.cold_grace_s = cold_grace_s
        self.grace_s = grace_s          # Read langsamer als das -> GET gilt als "slow"
        self.escalate_n = escalate_n    # so viele Stalls in Folge -> Heavy
        self.make_heavy = make_heavy    # make_heavy(pos) -> Heavy | None (RAM-Budget)
        self.lock = threading.Lock()
        self.reader = None          # aktuelle Quell-Verbindung
        self.pos = -1               # Offset, an dem `reader` als naechstes liefert
        self.mode = "light"         # light | heavy (einseitig)
        self.heavy = None           # SegmentSwarm o. ae. nach Eskalation
        self.slow_streak = 0        # aufeinanderfolgende langsame GETs

    # --- oeffentlich -------------------------------------------------------
    def read(self, start, length, timeout):
        """Bis zu `length` Bytes ab `start`. None => Handler nutzt Single-Stream-Fallback."""
        with self.lock:
            # Einseitig: einmal heavy, immer heavy (bis zum Slot-Verwerfen).
            if self.mode == "heavy":
                return self.heavy.read(start, length, timeout)

            reopen = self.reader is None or self.pos != start
            if reopen:
                self._close_reader_locked()
                reader, data = self._hedge_first(start, length, timeout)
                if reader is None or not data:
                    return None
                self.reader = reader
                self.pos = start + len(data)
                return data

            # sequenziell aus der offenen Verbindung, mit Stall-Messung
            t0 = time.monotonic()
            try:
                data = self.reader.read(length)
            except Exception:
                self._close_reader_locked()
                return None
            if not data:                      # Truncation mid-stream -> Fallback
                self._close_reader_locked()
                return None
            self.pos += len(data)
            self._note_pace(time.monotonic() - t0)
            return data

    def _note_pace(self, dt):
        """Stall-Buchhaltung + einseitige Eskalation (unter self.lock)."""
        if dt <= self.grace_s:
            self.slow_streak = 0
            return
        self.slow_streak += 1
        if (self.slow_streak >= self.escalate_n and self.mode == "light"
                and self.heavy is None and self.make_heavy is not None):
            heavy = self.make_heavy(self.pos)     # Heavy startet an aktueller Position
            if heavy is not None:                 # None => RAM-Budget voll -> light bleiben
                self.heavy = heavy
                self.mode = "heavy"
                self._close_reader_locked()       # Light-Verbindung freigeben

    def close(self):
        with self.lock:
            self._close_reader_locked()
            if self.heavy is not None:
                try:
                    self.heavy.close()
                except Exception:
                    pass
                self.heavy = None

    # --- intern ------------------------------------------------------------
    def _close_reader_locked(self):
        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass
            self.reader = None
        self.pos = -1

    def _hedge_first(self, start, length, timeout):
        """Kaltstart/Seek-Race: CDN zuerst, nach `cold_grace` WebDAV dazu, erster gewinnt.

        Der Verlierer schliesst seine Verbindung selbst, sobald sein Read fertig ist — kein
        zusaetzlicher Reaper-Thread, keine Dauer-Threads.
        """
        win = {}                              # 'reader','data' des Gewinners
        cond = threading.Condition()
        finished = set()

        def pull(name, opener):
            reader = None
            data = None
            try:
                reader = opener(start)
                data = reader.read(length)
            except Exception:
                data = None
            keep = False
            with cond:
                if data and "reader" not in win:
                    win["reader"], win["data"] = reader, data
                    keep = True
                finished.add(name)
                cond.notify_all()
            if not keep and reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass

        deadline = time.monotonic() + max(timeout, self.cold_grace_s)
        threading.Thread(target=pull, args=("cdn", self.cdn_open), daemon=True).start()

        with cond:
            cond.wait_for(lambda: "reader" in win or "cdn" in finished,
                          timeout=self.cold_grace_s)
            if "reader" in win:
                return win["reader"], win["data"]

        # CDN noch nicht da (kalt) oder leer fertig -> WebDAV fuer diesen GET dazuhedgen
        expect = {"cdn"}
        if self.web_open is not None:
            expect = {"cdn", "web"}
            threading.Thread(target=pull, args=("web", self.web_open), daemon=True).start()

        with cond:
            cond.wait_for(lambda: "reader" in win or finished >= expect,
                          timeout=max(0.0, deadline - time.monotonic()))
            if "reader" in win:
                return win["reader"], win["data"]
        return None, None
