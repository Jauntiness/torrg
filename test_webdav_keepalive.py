#!/usr/bin/env python3
# Regress-Test — WebDAV Keep-Alive Body-Drain (Live-Incident 2026-06-14):
#   rclones PROPFIND schickt einen XML-Body. Liest der Handler ihn nicht, bleibt er im Socket und
#   der naechste Request auf derselben HTTP/1.1-Keep-Alive-Verbindung liest '<?xml...' als
#   Request-Zeile -> 400 -> rclone-Pool desynct -> Stream haengt. Fix = _drain_body().
import os, sys, socket, threading
os.environ.setdefault("TORBOX_API_KEY", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app
from http.server import ThreadingHTTPServer


def recv_response(s):
    """Eine HTTP-Response lesen (Header bis \\r\\n\\r\\n + Body laut Content-Length)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            break
        buf += d
    head, _, rest = buf.partition(b"\r\n\r\n")
    n = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":", 1)[1].strip())
    while len(rest) < n:
        d = s.recv(4096)
        if not d:
            break
        rest += d
    return head + b"\r\n\r\n" + rest[:n]


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.settimeout(5)
        # 1) PROPFIND MIT XML-Body (genau wie rclone)
        xml = b'<?xml version="1.0" encoding="utf-8"?><propfind xmlns="DAV:"><allprop/></propfind>'
        s.sendall(b"PROPFIND / HTTP/1.1\r\nHost: x\r\nDepth: 0\r\nContent-Length: "
                  + str(len(xml)).encode() + b"\r\n\r\n" + xml)
        r1 = recv_response(s)
        assert r1.startswith(b"HTTP/1.1"), f"keine valide 1. Response: {r1[:60]}"

        # 2) Zweiter Request auf DERSELBEN Keep-Alive-Verbindung.
        s.sendall(b"OPTIONS / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        r2 = recv_response(s)
        # Ohne Body-Drain saehe der Server hier '<?xml...' als Request-Zeile -> 400.
        assert b" 400 " not in r2.split(b"\r\n", 1)[0], \
            f"Keep-Alive desynct — Body nicht gedrained! Statuszeile: {r2.split(chr(13).encode(),1)[0]}"
        assert b" 200 " in r2.split(b"\r\n", 1)[0], f"2. Request nicht sauber bedient: {r2[:60]}"
        s.close()

        # 3) Gegenprobe ohne Drain: simuliert den Bug -> muss 400 liefern (beweist, dass der Test greift).
        orig = app.H._drain_body
        app.H._drain_body = lambda self: None        # Drain deaktivieren
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5); s.settimeout(5)
            s.sendall(b"PROPFIND / HTTP/1.1\r\nHost: x\r\nDepth: 0\r\nContent-Length: "
                      + str(len(xml)).encode() + b"\r\n\r\n" + xml)
            recv_response(s)
            s.sendall(b"OPTIONS / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            rbug = recv_response(s)
            assert b" 400 " in rbug.split(b"\r\n", 1)[0], \
                "Ohne Drain MUESSTE der 2. Request 400 werfen — Test greift sonst nicht"
            s.close()
        finally:
            app.H._drain_body = orig
    finally:
        srv.shutdown()
    print("OK: WebDAV Keep-Alive — PROPFIND-Body wird gedrained, Folge-Request bleibt in Sync "
          "(ohne Drain -> 400, mit Drain -> 200)")


if __name__ == "__main__":
    main()
