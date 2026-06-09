#!/usr/bin/env python3
# TDD-Test — do_DELETE app-Schicht (pure Helfer):
#   delete_request   = korrekter TorBox-Control-Endpoint + Payload je Item-Typ.
#   files_in_release = Release-Ordner -> Datei-Liste; Kategorie/Root -> None (Massen-Lösch-Schutz).
import os, sys
os.environ.setdefault("TORBOX_API_KEY", "dummy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

def main():
    # delete_request: pro Typ richtiger Endpoint + id-Param + operation=delete
    assert app.delete_request("torrents", 5) == (
        "/torrents/controltorrent", {"torrent_id": 5, "operation": "delete"})
    assert app.delete_request("usenet", 7) == (
        "/usenet/controlusenetdownload", {"usenet_id": 7, "operation": "delete"})
    assert app.delete_request("webdl", 9) == (
        "/webdl/controlwebdownload", {"webdl_id": 9, "operation": "delete"})

    # files_in_release: ein Release-Ordner (alle Kinder = Dateien) -> Liste (hash,wpath,type,tid)
    release = {
        "ep1.mkv": {"wpath": "Show/ep1.mkv", "hash": "h1", "type": "torrents", "tid": 5},
        "ep2.mkv": {"wpath": "Show/ep2.mkv", "hash": "h1", "type": "torrents", "tid": 5},
    }
    got = app.files_in_release(release)
    assert got is not None and len(got) == 2, "Release-Ordner -> Datei-Liste"
    assert ("h1", "Show/ep1.mkv", "torrents", 5) in got

    # Kategorie/Root (Kinder = Unterordner, kein 'wpath') -> None = NICHT löschbar (Schutz)
    category = {"ReleaseA": release,
                "ReleaseB": {"x.mkv": {"wpath": "B/x.mkv", "hash": "h2", "type": "torrents", "tid": 6}}}
    assert app.files_in_release(category) is None, "Kategorie-DELETE darf nicht massenlöschen"
    assert app.files_in_release({}) is None, "leerer Knoten -> None"

    print("OK: delete_request payloads + files_in_release release-vs-category guard")

if __name__ == "__main__":
    main()
