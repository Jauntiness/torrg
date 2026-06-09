#!/usr/bin/env python3
# Kategorisierung fuer torbox-fast (zurg-Stil mit GRUPPEN): ein Release kann in mehreren
# Top-Level-Ordnern liegen (z.B. movies + movies_1080p_264 + __all__).
#   - Regeln stehen in GRUPPEN ([media], [media_1080p_264], [all], ...).
#   - INNERHALB einer Gruppe gewinnt die ERSTE passende Kategorie (mutually exclusive).
#   - Ueber Gruppen hinweg ADDITIV (eine Kategorie je Gruppe mit Treffer).
#   - So baut man quality-gefilterte Plex-Libraries (movies_1080p_264) NEBEN movies.
# Config = editierbare categories.conf, Format:  [gruppe] dann  kategorie = regex  Zeilen.
#
# SCOPES (2026-06-06, gegen false-positives bei $-Anker-Regeln):
#   kategorie       = regex   -> matcht Release-Name (roh + ext-gestrippt) UND concat-hay
#   kategorie@name  = regex   -> NUR Release-Name (roh + ext-gestrippt); fuer ^/$-Anker-Regeln
#   kategorie@files = regex   -> NUR einzelne Dateinamen (roh); fuer Audio-Extension am Datei-ENDE
import re

# Eingebaute Default-Regeln (group, category, scope, regex). Greifen ohne/leerer Config.
# Mehrere Zeilen je Kategorie erlaubt (ODER-verknuepft via first-match in der Gruppe).
# Music-Regeln portiert aus zurg config.yml (Music-Videos: Houdini-Fix 2026-06-06).
DEFAULT_RULES = [
    ("media", "shows",  "all", r"(s\d{1,2}[ ._-]?e\d{1,3}|\b\d{1,2}x\d{2}\b|\bs\d{1,2}\b|season[ ._-]?\d|complete[ ._-]?series)"),
    # Audio-Format-Marker im Namen (FLAC/MP3/kbps/...) — AAC bewusst NICHT (Codec-Tag in Movie-Releases)
    ("media", "music",  "all", r"\b(flac|mp3|\d{3}\s?kbps|24[ -]?bit|web[ -]?flac|vinyl|discography|ost|soundtrack)\b"),
    # Echte Audio-DATEIEN: Extension am Datei-ENDE (".AAC5.1"-Codec-Tags matchen so nicht)
    ("media", "music",  "files", r"\.(m4a|flac|mp3|wav|aac|ogg|opus)$"),
    # Music-Videos: "Official (Music) Video" / "Lyric Video" / " MV "
    ("media", "music",  "all", r"\b(official\s?(music\s?)?video|music\s?video|lyric\s?video|\smv\s)\b"),
    # Album/EP/Single mit Jahr
    ("media", "music",  "all", r"\b(album|ep|single|lp|mixtape)\s*[\(\[]?\d{4}[\)\]]?"),
    # "Artist - Year - Title" (Peter Fox - 2008 - Stadtaffe)
    ("media", "music",  "name", r"[a-z][a-z &.']{2,}\s*-\s*(19|20)\d{2}\s*-"),
    # "Artist - Title - Year" am ENDE
    ("media", "music",  "name", r"-\s*(19|20)\d{2}\s*$"),
    # "Artist - Title (Year)" / "[Year]" am ENDE (Movies haben quality NACH dem Jahr -> kein Match)
    ("media", "music",  "name", r"\s[\(\[](19|20)\d{2}[\)\]]\s*$"),
    # "Artist - Title (Wort)" am ENDE: "(Blue)", "(Remix)", "(Acoustic)"
    ("media", "music",  "name", r"\s\([a-z][a-z]+\)\s*$"),
    # Pure "Artist - Title" ohne JEDE Ziffer (kein year/quality) -> "Dua Lipa - Houdini"
    ("media", "music",  "name", r"^[a-z][a-z &.,']+\s-\s[a-z][a-z &.,']+$"),
    ("media", "movies", "all", r".*"),
    ("media_1080p_264", "shows_1080p_264",  "all", r"(?=.*(s\d{1,2}[ ._-]?e\d{1,3}|\b\d{1,2}x\d{2}\b|\bs\d{1,2}\b|season[ ._-]?\d|complete[ ._-]?series))(?=.*\b1080p\b)(?=.*\b(x264|h[ ._-]?264|avc)\b)"),
    ("media_1080p_264", "movies_1080p_264", "all", r"(?=.*\b1080p\b)(?=.*\b(x264|h[ ._-]?264|avc)\b)"),
    ("all", "__all__", "all", r".*"),
]

def compile_rules(rules):
    return [(g, c, s, re.compile(rx, re.IGNORECASE)) for g, c, s, rx in rules]

def parse_config(text):
    """Sektionen [gruppe] mit 'kategorie[@scope] = regex'-Zeilen
    -> [(gruppe, kategorie, scope, regex), ...]. Reihenfolge = Prioritaet (in der Gruppe).
    scope: all (default) | name | files."""
    rules, group = [], "media"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        cat, rx = line.split("=", 1)
        cat, rx = cat.strip(), rx.strip()
        scope = "all"
        if "@" in cat:
            cat, scope = cat.split("@", 1)
            cat, scope = cat.strip(), scope.strip().lower()
            if scope not in ("all", "name", "files"):
                scope = "all"
        if cat and rx:
            rules.append((group, cat, scope, rx))
    return rules

def load_rules(path):
    """Regeln aus Config laden; bei fehlend/leer/kaputt -> DEFAULT_RULES."""
    try:
        with open(path) as f:
            rules = parse_config(f.read())
        if rules:
            return compile_rules(rules)
    except Exception:
        pass
    return compile_rules(DEFAULT_RULES)

def _strip_ext(t):
    return re.sub(r"\.[a-z0-9]{1,5}$", "", t, flags=re.IGNORECASE)

def classify_groups(name, filenames=(), rules=None):
    """Liste der Kategorien (eine je Gruppe mit Treffer). rules = compile_rules(...); None -> Defaults."""
    if rules is None:
        rules = compile_rules(DEFAULT_RULES)
    files = list(filenames or [])
    raw_name = name or ""
    # name-Targets: roh + ext-gestrippt (TorBox single-file-Releases heissen MIT Extension,
    # "Dua Lipa - Houdini.mp4" -> sonst greifen $-Anker-Regeln nie)
    name_targets = [raw_name]
    stripped = _strip_ext(raw_name)
    if stripped != raw_name:
        name_targets.append(stripped)
    # all-Targets: name-Targets + concat-hay (Lookahead-Kombis, z.B. 1080p im Namen + x264 im File)
    all_targets = name_targets + [" ".join([raw_name] + files)]
    targets = {"name": name_targets, "files": files, "all": all_targets}
    chosen, out = set(), []
    for group, cat, scope, rx in rules:
        if group in chosen:
            continue                      # in dieser Gruppe schon entschieden (first-match)
        if any(rx.search(t) for t in targets.get(scope, all_targets)):
            chosen.add(group); out.append(cat)
    return out
