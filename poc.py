#!/usr/bin/env python3
# PoC: minimaler WebDAV-Server, der EINE TorBox-Datei per Range-Proxy vom CDN ausliefert.
# Beweist: rclone -> unser WebDAV -> CDN-Range-Proxy liefert CDN-Speed (~150 Mbit/s)?
import os, sys, json, subprocess, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("TORBOX_API_KEY", "")
PORT = int(os.environ.get("PORT", "8112"))
NAME = "test.mkv"

UA = "Mozilla/5.0 (X11; Linux x86_64) torbox-webdav/0.1"

def api_get(url):
    out = subprocess.run(["curl", "-sf", "-H", f"Authorization: Bearer {API_KEY}", url],
                         capture_output=True, timeout=40)
    if out.returncode != 0:
        raise RuntimeError(f"curl failed rc={out.returncode}: {out.stderr.decode()[:200]}")
    return json.loads(out.stdout)

def pick_file():
    d = api_get("https://api.torbox.app/v1/api/torrents/mylist?bypass_cache=true")
    best = None
    for t in d.get("data", []):
        if not t.get("cached"): continue
        for f in t.get("files", []):
            if f.get("mimetype") in ("video/x-matroska", "video/mp4") and f.get("size", 0) > 2_000_000_000:
                if best is None or f["size"] > best["size"]:
                    best = {"tid": t["id"], "fid": f["id"], "size": f["size"], "name": f.get("short_name")}
    return best

def resolve_cdn(tid, fid):
    url = f"https://api.torbox.app/v1/api/torrents/requestdl?token={API_KEY}&torrent_id={tid}&file_id={fid}&redirect=false"
    return api_get(url).get("data")

FILE = pick_file()
if not FILE:
    print("FEHLER: keine passende Datei im Account"); sys.exit(1)
CDN_URL = resolve_cdn(FILE["tid"], FILE["fid"])
SIZE = FILE["size"]
print(f"PoC serviert /{NAME}  size={SIZE} ({SIZE/1e9:.1f} GB)")
print(f"echte Datei: {FILE['name']}")
print(f"CDN-URL: {CDN_URL[:75]}...")

DIR_XML = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
 '<D:response><D:href>/</D:href><D:propstat><D:prop>'
 '<D:resourcetype><D:collection/></D:resourcetype>'
 '<D:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</D:getlastmodified>'
 '</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
 '<D:response><D:href>/{name}</D:href><D:propstat><D:prop>'
 '<D:resourcetype/><D:getcontentlength>{size}</D:getcontentlength>'
 '<D:getcontenttype>video/x-matroska</D:getcontenttype>'
 '<D:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</D:getlastmodified>'
 '</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
 '</D:multistatus>')
FILE_XML = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
 '<D:response><D:href>/{name}</D:href><D:propstat><D:prop>'
 '<D:resourcetype/><D:getcontentlength>{size}</D:getcontentlength>'
 '<D:getcontenttype>video/x-matroska</D:getcontenttype>'
 '<D:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</D:getlastmodified>'
 '</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
 '</D:multistatus>')

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _h(self, code, hdrs=None, body=b""):
        self.send_response(code)
        for k, v in (hdrs or {}).items(): self.send_header(k, v)
        if "Content-Length" not in (hdrs or {}): self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body: self.wfile.write(body)
    def do_OPTIONS(self):
        self._h(200, {"DAV": "1,2", "Allow": "OPTIONS, GET, HEAD, PROPFIND", "MS-Author-Via": "DAV"})
    def do_PROPFIND(self):
        xml = (DIR_XML if self.path.rstrip("/") in ("", "/") else FILE_XML).format(name=NAME, size=SIZE).encode()
        self._h(207, {"Content-Type": 'application/xml; charset="utf-8"'}, xml)
    def do_HEAD(self):
        self._h(200, {"Content-Length": str(SIZE), "Accept-Ranges": "bytes", "Content-Type": "video/x-matroska"})
    def do_GET(self):
        rng = self.headers.get("Range")
        hh = {"User-Agent": UA}
        if rng: hh["Range"] = rng
        req = urllib.request.Request(CDN_URL, headers=hh)
        try:
            up = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            self._h(e.code); return
        h = {"Accept-Ranges": "bytes", "Content-Type": "video/x-matroska"}
        if up.headers.get("Content-Range"): h["Content-Range"] = up.headers["Content-Range"]
        h["Content-Length"] = up.headers.get("Content-Length", str(SIZE))
        self.send_response(up.getcode())
        for k, v in h.items(): self.send_header(k, v)
        self.end_headers()
        try:
            while True:
                chunk = up.read(262144)
                if not chunk: break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def log_message(self, *a): pass

print(f"WebDAV PoC laeuft auf :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
