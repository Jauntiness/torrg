#!/usr/bin/env bash
# Scan-Experiment-Helfer fuer torbox-fast (LAZY-Shim + rclone-VFS davor).
# Beweist, ob unser Probe-Cache reicht, damit Plex-Scans KEINEN aktiven TorBox-Download ausloesen.
#
# Ablauf:
#   ./scan_experiment.sh reset          # vor Scan #1: alles kalt (Probe + Access-Log leer, rclone-Cache vergessen)
#   <Plex-Scan #1 starten, abwarten>
#   ./scan_experiment.sh forget-rclone  # vor Scan #2: nur rclone-Cache vergessen (Probe BLEIBT) -> isoliert Probe
#   <Plex-Scan #2 starten, abwarten>
#   ./scan_experiment.sh report         # Auswertung: ab Session #2 sollte 'aktiv geladen' ~0 sein
set -euo pipefail
DIR=/srv/torrg
ALOG=$DIR/data/access.log
PROBE=$DIR/data/probe
RCLONE=torbox_webdav_rclone
SHIM=torbox_webdav
RC="rclone rc --rc-addr=:5572 --rc-no-auth"

forget_rclone() {
  echo "→ rclone VFS-Cache vergessen (kein Unmount) ..."
  docker exec "$RCLONE" $RC vfs/forget >/dev/null 2>&1 || echo "  (vfs/forget fehlgeschlagen — laeuft --rc?)"
}

case "${1:-}" in
  reset)
    echo "== RESET (kalt) =="
    docker stop "$SHIM" >/dev/null
    sudo rm -rf "$PROBE" "$ALOG"; sudo mkdir -p "$PROBE"
    docker start "$SHIM" >/dev/null
    echo "→ warte auf LAZY-Listing ..."
    for i in $(seq 1 50); do docker logs "$SHIM" 2>&1 | grep -q "LAZY-Listing aus Katalog" && break; sleep 2; done
    docker logs "$SHIM" 2>&1 | grep "LAZY-Listing aus Katalog" | tail -1
    forget_rclone
    echo "✓ kalt. Jetzt Plex-Scan #1 starten."
    ;;
  forget-rclone)
    echo "== Vor Scan #2: Probe BLEIBT, rclone-Cache weg =="
    forget_rclone
    echo "✓ Jetzt Plex-Scan #2 starten (head/tail kommen aus dem Probe-Cache)."
    ;;
  report)
    [ -f "$ALOG" ] || { echo "Kein Access-Log ($ALOG). Erst reset + Scan."; exit 1; }
    TMP=$(mktemp); sudo cat "$ALOG" > "$TMP"
    python3 "$DIR/scan_probe_report.py" "$TMP" "${@:2}"
    rm -f "$TMP"
    ;;
  status)
    echo "Shim:   $(docker ps --filter name=^/$SHIM$ --format '{{.Status}}')"
    echo "rclone: $(docker ps --filter name=^/$RCLONE$ --format '{{.Status}}')"
    echo "Access-Log: $(sudo wc -l < "$ALOG" 2>/dev/null || echo 0) Reads"
    echo "Probe:  $(sudo du -sh "$PROBE" 2>/dev/null | cut -f1 || echo 0)"
    docker exec "$RCLONE" $RC vfs/stats 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print("rclone VFS-Cache:",round(d.get("diskCache",{}).get("bytesUsed",0)/1024/1024),"MB")' 2>/dev/null || true
    ;;
  *)
    echo "Usage: $0 {reset|forget-rclone|report [--head-mb N --tail-mb N --gap S]|status}"; exit 1;;
esac
