"""Walks library folders and keeps the tracks table in sync with disk."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata

import mutagen

from . import db
from .config import AUDIO_EXTS

LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff", "ape", "wavpack"}

_PAREN = re.compile(r"\((?:feat|ft|featuring|with)\.?[^)]*\)", re.I)
_BRACKET = re.compile(r"\[[^\]]*\]")

# Qualifiers that describe the same recording rather than a different one.
# Remixes, live takes and acoustic versions are deliberately absent — those are
# genuinely different recordings and should not collapse together.
_VERSION_WORDS = (
    r"re-?master(ed)?|\d{4}\s*re-?master(ed)?|remaster(ed)?\s*\d{4}|"
    r"radio edit|single version|album version|original mix|"
    r"mono|stereo|explicit|clean|bonus track|deluxe|anniversary edition|"
    r"digital remaster|expanded edition"
)
_VERSION_PAREN = re.compile(rf"[\(\[][^)\]]*\b(?:{_VERSION_WORDS})\b[^)\]]*[\)\]]", re.I)
_SUFFIX = re.compile(rf"\s*[-–]\s*(?:{_VERSION_WORDS})\b.*$", re.I)
_NOISE = re.compile(r"[^\w\s]", re.UNICODE)
# An underscore counts as a word character, so _NOISE leaves it alone. For
# comparison purposes a filesystem-eaten space should read as a space, or
# "Low_Tide" and "Low Tide" never look like the same song.
# In a comparison key an underscore is always a separator, never part of a
# word. It has to go first: "(2011_Remaster)" hides from the remaster pattern
# because _ is a word character and kills the \b boundary, and
# "Idol_(feat._X)" leaves a stray _ behind once the bracket is stripped.
_UNDERSCORE = re.compile(r"_+")
_SPACES = re.compile(r"\s+")
_COPY = re.compile(r"(\s+\(\d+\)|\s+-?\s*copy|\s+duplicate)\s*$", re.I)


# Everything normalize() throws away, kept so duplicate detection can tell
# "Idol (feat. Tech N9ne)" from "Idol (feat. KURT92)". Those are different
# recordings; dropping the qualifier makes them look like the same one.
_QUALIFIER = re.compile(r"[\(\[]([^)\]]+)[\)\]]|\s[-–]\s(.+)$")


def title_qualifier(text: str | None) -> str:
    """What distinguishes two tracks whose base titles match.

    Featured artists, remix credits, edit names. Returned normalized and
    sorted so the comparison does not depend on ordering or punctuation.
    """
    if not text:
        return ""
    parts = []
    for match in _QUALIFIER.finditer(_UNDERSCORE.sub(" ", str(text))):
        piece = match.group(1) or match.group(2) or ""
        piece = re.sub(r"^\s*(feat|ft|featuring|with)\.?\s*", "", piece, flags=re.I)
        piece = _NOISE.sub(" ", unicodedata.normalize("NFKD", piece).lower())
        piece = _SPACES.sub(" ", piece).strip()
        if piece:
            parts.append(piece)
    return "|".join(sorted(parts))


def normalize(text: str | None) -> str:
    """Collapse a title/artist down to something two files can be compared on."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", str(text)).lower()
    s = _UNDERSCORE.sub(" ", s)
    s = _PAREN.sub(" ", s)
    s = _VERSION_PAREN.sub(" ", s)
    s = _BRACKET.sub(" ", s)
    s = _SUFFIX.sub(" ", s)
    s = _COPY.sub(" ", s)
    s = s.replace("&", " and ")
    s = _NOISE.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s


def _first(tags, *keys) -> str | None:
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if not v:
            continue
        if isinstance(v, list):
            v = v[0]
        v = str(v).strip()
        if v:
            return v
    return None


# Multiple artists are stored as separate values, joined here with "; " so
# the database has one readable string. The artist_multi flag records that
# it came from real multiple values rather than a string that happens to
# contain a semicolon.
MULTI_JOIN = "; "


def _all(tags, key) -> list[str]:
    try:
        value = tags.get(key)
    except Exception:
        return []
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in values if str(v).strip()]


def _int(value) -> int | None:
    if value is None:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def content_hash(path: str, size: int) -> str:
    """Cheap but stable fingerprint: size plus head and tail of the file."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(262144))
        if size > 524288:
            f.seek(-262144, os.SEEK_END)
            h.update(f.read(262144))
    return h.hexdigest()


def read_tags(path: str) -> dict:
    """Pull the fields Harmon cares about out of whatever tag format the file uses."""
    audio = mutagen.File(path, easy=False)
    if audio is None:
        raise ValueError("unreadable audio file")

    easy = None
    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        pass
    tags = easy.tags if easy is not None and easy.tags else {}

    info = audio.info
    codec = type(audio).__name__.lower()
    for name in ("flac", "mp3", "opus", "wave", "aiff", "monkeysaudio", "wavpack"):
        if name in codec:
            codec = name
            break
    else:
        if "mp4" in codec or "m4a" in codec:
            sample = getattr(info, "codec", "") or ""
            codec = "alac" if "alac" in sample.lower() else "aac"
        elif "ogg" in codec:
            codec = "vorbis"
        elif "asf" in codec:
            codec = "wma"

    art = False
    try:
        if getattr(audio, "pictures", None):
            art = True
        elif audio.tags:
            keys = list(audio.tags.keys())
            art = any(str(k).upper().startswith("APIC") for k in keys) or "covr" in keys
    except Exception:
        pass

    return {
        "duration": round(float(getattr(info, "length", 0) or 0), 3),
        "bitrate": int((getattr(info, "bitrate", 0) or 0) / 1000),
        "samplerate": int(getattr(info, "sample_rate", 0) or 0),
        "channels": int(getattr(info, "channels", 0) or 0),
        "codec": codec,
        "lossless": 1 if codec in LOSSLESS_CODECS else 0,
        "title": _first(tags, "title") or os.path.splitext(os.path.basename(path))[0],
        "artist": MULTI_JOIN.join(_all(tags, "artist")) or None,
        "artist_multi": 1 if len(_all(tags, "artist")) > 1 else 0,
        "album_artist": _first(tags, "albumartist", "album artist"),
        "album": _first(tags, "album"),
        "track_no": _int(_first(tags, "tracknumber")),
        "disc_no": _int(_first(tags, "discnumber")),
        "year": (_first(tags, "date", "originaldate", "year") or "")[:4] or None,
        "genre": _first(tags, "genre"),
        "has_art": 1 if art else 0,
        "mb_recording": _first(tags, "musicbrainz_trackid", "musicbrainz_recordingid"),
        "mb_release": _first(tags, "musicbrainz_albumid"),
    }


def index_file(path: str, library_id: int | None = None, force: bool = False) -> str:
    """Add or refresh one file. Returns 'added', 'updated', 'unchanged' or 'skipped'."""
    if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
        return "skipped"
    try:
        st = os.stat(path)
    except OSError:
        return "skipped"

    existing = db.one("SELECT id, mtime, size FROM tracks WHERE path=?", (path,))
    if existing and not force and existing["mtime"] == st.st_mtime and existing["size"] == st.st_size:
        db.execute("UPDATE tracks SET missing=0 WHERE id=?", (existing["id"],))
        return "unchanged"

    try:
        meta = read_tags(path)
    except Exception as exc:
        db.log(f"Could not read {os.path.basename(path)}: {exc}", "warn")
        return "skipped"

    meta["path"] = path
    meta["folder"] = os.path.dirname(path)
    meta["library_id"] = library_id
    meta["size"] = st.st_size
    meta["mtime"] = st.st_mtime
    meta["content_hash"] = content_hash(path, st.st_size)
    artist_key = normalize(meta["album_artist"] or meta["artist"])
    meta["norm_key"] = f"{artist_key}|{normalize(meta['title'])}"
    meta["album_key"] = f"{artist_key}|{normalize(meta['album'])}"
    meta["missing"] = 0

    cols = list(meta.keys())
    if existing:
        sets = ", ".join(f"{c}=?" for c in cols)
        db.execute(
            f"UPDATE tracks SET {sets}, scanned_at=datetime('now') WHERE id=?",
            tuple(meta[c] for c in cols) + (existing["id"],),
        )
        return "updated"

    placeholders = ", ".join("?" for _ in cols)
    db.execute(
        f"INSERT INTO tracks({', '.join(cols)}) VALUES({placeholders})",
        tuple(meta[c] for c in cols),
    )
    return "added"


def scan(force: bool = False, progress=None) -> dict:
    """Walk every enabled library. Marks vanished files rather than deleting rows."""
    libs = db.query("SELECT * FROM libraries WHERE enabled=1")
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "missing": 0}
    seen: set[str] = set()

    files: list[tuple[str, int]] = []
    for lib in libs:
        for root, dirs, names in os.walk(lib["path"]):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "@eaDir"]
            for name in names:
                if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                    files.append((os.path.join(root, name), lib["id"]))

    total = max(len(files), 1)
    for i, (path, lib_id) in enumerate(files):
        counts[index_file(path, lib_id, force)] += 1
        seen.add(path)
        if progress and i % 25 == 0:
            progress(i / total, f"Scanned {i} of {len(files)} files")

    for row in db.query("SELECT id, path FROM tracks WHERE missing=0"):
        if row["path"] not in seen and not os.path.exists(row["path"]):
            db.execute("UPDATE tracks SET missing=1 WHERE id=?", (row["id"],))
            counts["missing"] += 1

    db.log(
        f"Scan finished: {counts['added']} new, {counts['updated']} changed, "
        f"{counts['missing']} no longer on disk"
    )
    return counts
