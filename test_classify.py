#!/usr/bin/env python3
# TDD-Test — Klassifizierer mit GRUPPEN: movies/shows/music + _1080p_264-Spiegel + __all__.
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import classify_groups, parse_config, load_rules

def main():
    # Nicht-1080p Movie: nur movies + __all__
    assert set(classify_groups("28 Years Later 2026 2160p REMUX", ["x.mkv"])) == {"movies", "__all__"}
    # 1080p x264 Movie: movies + movies_1080p_264 + __all__
    c = classify_groups("Some Movie 2024 1080p BluRay x264-GRP", ["m.mkv"])
    assert set(c) == {"movies", "movies_1080p_264", "__all__"}, c
    # 1080p x264 Show: shows + shows_1080p_264 + __all__ (NICHT movies_1080p_264)
    c = classify_groups("Some Show S01E02 1080p WEB h264-GRP", ["e.mkv"])
    assert set(c) == {"shows", "shows_1080p_264", "__all__"}, c
    # 1080p HEVC Show (kein x264): nur shows + __all__, KEIN _1080p_264-Spiegel
    c = classify_groups("Dr Pol S22E03 1080p HEVC x265-MeGusta", ["e.mkv"])
    assert set(c) == {"shows", "__all__"}, c
    # Music
    assert set(classify_groups("Doja Cat - Planet Her (2021) FLAC", ["01.m4a"])) == {"music", "__all__"}
    # Music-Videos / zurg-portierte Regeln (2026-06-06, Houdini-Regression nach torbox-Umzug):
    # Pure "Artist - Title" ohne year/quality-marker -> music (NICHT movies)
    c = classify_groups("Dua Lipa - Houdini", ["Dua Lipa - Houdini.mp4"])
    assert set(c) == {"music", "__all__"}, c
    # "Official Music Video" Marker
    c = classify_groups("Taylor Swift - Anti-Hero (Official Music Video)", ["v.mkv"])
    assert set(c) == {"music", "__all__"}, c
    # "Artist - Title (Word)" am Ende (Remix/Blue/Acoustic-Variante)
    c = classify_groups("David Guetta - Im Good (Blue)", ["v.mp4"])
    assert set(c) == {"music", "__all__"}, c
    # "Artist - Title - Year" am ENDE (Anker muss gegen Release-NAME greifen, nicht concat-hay)
    c = classify_groups("Amber Mark - Pretty Idea - 2025", ["v.mkv"])
    assert set(c) == {"music", "__all__"}, c
    # "Artist - Title (Year)" am ENDE
    c = classify_groups("Peter Fox - Stadtaffe (2008)", ["v.mkv"])
    assert set(c) == {"music", "__all__"}, c
    # "Artist - Year - Title"
    c = classify_groups("Peter Fox - 2008 - Stadtaffe", ["v.mkv"])
    assert set(c) == {"music", "__all__"}, c
    # TorBox-Realitaet: single-file-Releases heissen MIT Extension ("Dua Lipa - Houdini.mp4")
    # -> $-Anker muessen auch nach Extension-Strip greifen
    c = classify_groups("Dua Lipa - Houdini.mp4", ["Dua Lipa - Houdini.mp4"])
    assert set(c) == {"music", "__all__"}, c
    c = classify_groups("David Guetta & Bebe Rexha - I'm Good (Blue).mp4", ["David Guetta & Bebe Rexha - I'm Good (Blue).mp4"])
    assert set(c) == {"music", "__all__"}, c
    c = classify_groups("Rita Ora - Poison.mp4", ["Rita Ora - Poison.mp4"])
    assert set(c) == {"music", "__all__"}, c
    # GEGENPROBE: Movies mit year+quality bleiben movies (kein false-positive durch neue Regeln)
    assert set(classify_groups("Oppenheimer (2023) 2160p REMUX", ["o.mkv"])) == {"movies", "__all__"}
    assert set(classify_groups("John Wick 2014 1080p BluRay x264-GRP", ["j.mkv"])) == {"movies", "movies_1080p_264", "__all__"}
    # GEGENPROBE Scopes (live-false-positives 2026-06-06):
    # YTS-Filename mit Codec-Tag ".AAC5.1" darf NICHT als Audio-Extension zaehlen
    c = classify_groups("A Good Person (2023) [2160p] [4K] [WEB] [5.1] [YTS.MX]",
                        ["A.Good.Person.2023.2160p.4K.WEB.x265.10bit.AAC5.1-[YTS.MX].mp4"])
    assert set(c) == {"movies", "__all__"}, c
    # Release-NAME endet auf Codec ".AAC" -> trotzdem movie (AAC ist kein Audio-FILE-Ende)
    c = classify_groups("Cutting.Through.Rocks.2025.1080p.WEB.AVC.AAC",
                        ["Cutting.Through.Rocks.2025.1080p.WEB.AVC.AAC.mp4"])
    assert set(c) == {"movies", "movies_1080p_264", "__all__"}, c  # 1080p+AVC -> Spiegel korrekt
    # Movie-Datei "Titel (Jahr).mp4" im Release darf NICHT die (Year)$-Music-Regel triggern
    c = classify_groups("Echo Valley (2025) [2160p] [WEBRip] [x265] [10bit] [5.1]",
                        ["Echo Valley (2025).mp4"])
    assert set(c) == {"movies", "__all__"}, c
    # Echte Audio-FILES (Album-Ordner ohne Marker im Namen) -> music via files-scope
    c = classify_groups("Some Album", ["01 a.mp3", "02 b.mp3"])
    assert set(c) == {"music", "__all__"}, c
    # 2160p Movie: kein 1080p-Spiegel
    assert set(classify_groups("Film 2025 2160p x265", ["m.mkv"])) == {"movies", "__all__"}

    # Config-Parser mit Sektionen (+ scope, default "all")
    txt = "# c\n[media]\nmovies = .*\n[all]\n__all__ = .*"
    assert parse_config(txt) == [("media", "movies", "all", ".*"), ("all", "__all__", "all", ".*")], "sections"
    # Scope-Suffix "kategorie@scope"
    txt = "[media]\nmusic@files = \\.mp3$\nmusic@name = ^x$\nmovies = .*"
    assert parse_config(txt) == [("media", "music", "files", "\\.mp3$"),
                                 ("media", "music", "name", "^x$"),
                                 ("media", "movies", "all", ".*")], "scopes"

    # Eigene Config + Gruppen-Semantik
    p = tempfile.mktemp()
    with open(p, "w") as f:
        f.write("[media]\nshows = s\\d+e\\d+\nmovies = .*\n[hd]\nmovies_1080p_264 = 1080p\n[all]\n__all__ = .*\n")
    r = load_rules(p)
    assert set(classify_groups("Movie 1080p", ["m.mkv"], r)) == {"movies", "movies_1080p_264", "__all__"}
    assert set(classify_groups("Show s01e01", ["e.mkv"], r)) == {"shows", "__all__"}, "kein 1080p -> kein Spiegel"
    os.remove(p)
    assert len(load_rules("/nope.conf")) == len(__import__("classify").DEFAULT_RULES), "fallback Defaults"
    print("OK: classify_groups (gruppen, 1080p-spiegel, __all__) + config-parser")

if __name__ == "__main__":
    main()
