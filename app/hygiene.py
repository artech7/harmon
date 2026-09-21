"""Artist tag hygiene.

The thing that shatters an artist list is not bad metadata so much as bad
*shapes*: names that came from filenames (`3_Doors_Down`), and collaborations
written into `album_artist` so every guest spawns their own artist entry.

Nothing here invents information. It only reshapes what is already in the tag,
and every proposal goes through the normal Review screen before it is written.
"""
from __future__ import annotations

import json
import re

from . import db

# "Aaron Lewis feat. Willie Nelson" -> primary is everything before this.
FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|w/|with)\s+", re.I)

# Slash and semicolon usually mean "more than one artist" — but not always,
# and the exceptions are famous. See _splittable below.
SLASH = re.compile(r"\s*[/;|]\s*")

# "Adam Calhoun, Brodnax" is two artists. "Earth, Wind & Fire" is one band.
# There is no way to tell them apart from the string, so comma splitting is
# off unless asked for, and is staged at low confidence when it is on.
COMMA = re.compile(r"\s*,\s+")

# Any underscore with something on both sides of it is a space a filesystem
# ate. Requiring word characters was too strict: it left "Iron_&_Wine" and
# "Earth,_Wind_&_Fire" untouched, because & and , are not word characters.
# A leading or trailing underscore is usually deliberate styling (_moshang)
# and survives.
UNDERSCORE = re.compile(r"(?<=\S)_+(?=\S)")
SPACES = re.compile(r"\s{2,}")
TRAILING = re.compile(r"[\s,;/|&-]+$")

# Names where a slash is part of the name rather than a separator. The length
# guard below catches most of these on its own; this is for the ones it misses.
SLASH_NAMES = {
    "ac/dc", "acdc", "and/or", "ad/hd", "n/a", "b/w", "w/e",
    "sunn o)))", "hooverphonic", "kc/dc",
}


def tidy(name: str | None) -> str:
    """Mechanical cleanup only: no casing changes, no guessing."""
    if not name:
        return ""
    text = str(name)
    # An underscore between two word characters is a space that a filesystem
    # ate. It does not matter whether the rest of the name already has spaces:
    # "Hollywood_Undead feat. Tech N9ne" is as wrong as "3_Doors_Down".
    text = UNDERSCORE.sub(" ", text)
    text = SPACES.sub(" ", text).strip()
    text = TRAILING.sub("", text)
    return text


def _splittable(text: str) -> bool:
    """Is this slash a separator, or part of somebody's name?"""
    if text.lower().strip() in SLASH_NAMES:
        return False
    parts = [p.strip() for p in SLASH.split(text) if p.strip()]
    if len(parts) < 2:
        return False
    # AC/DC, T/O, B/W — initialisms, not two artists. Two characters or fewer
    # on a side means an initialism. Three is a real name: requiring four
    # locked "Dax/Elle King" together as though it were AC/DC.
    return all(len(p) > 2 for p in parts)


def split_credit(text: str, allow_comma: bool = False) -> list[str]:
    """Break a credit string into the artists it names, most important first."""
    text = tidy(text)
    if not text:
        return []

    feat_parts = FEAT.split(text)
    head, guests = feat_parts[0], feat_parts[1:]

    artists = [head] if not _splittable(head) else \
        [p.strip() for p in SLASH.split(head) if p.strip()]

    if allow_comma and len(artists) == 1:
        commas = [p.strip() for p in COMMA.split(artists[0]) if p.strip()]
        if len(commas) > 1 and all(len(p) > 2 for p in commas):
            artists = commas

    for guest in guests:
        artists.extend(
            [p.strip() for p in SLASH.split(guest) if p.strip()]
            if _splittable(guest) else [guest.strip()]
        )

    seen, out = set(), []
    for a in artists:
        key = a.lower()
        if a and key not in seen:
            seen.add(key)
            out.append(a)
    return out


def assess(track: dict, allow_comma: bool = False) -> list[dict]:
    """What Harmon would change about this track's artist tags, and why."""
    artist = track.get("artist") or ""
    album_artist = track.get("album_artist") or ""
    proposals: list[dict] = []

    clean_artist = tidy(artist)
    parts = split_credit(artist, allow_comma)
    primary = parts[0] if parts else clean_artist

    # 0. A collaboration written as one string becomes several real values.
    #    Once that proposal exists it supersedes the plain tidy below — both
    #    write the artist field, and whichever applied second would win, so
    #    a tidy landing after this one would flatten the list straight back.
    split_proposed = False
    if len(parts) > 1 and not track.get("artist_multi"):
        confidence = 0.9 if len(parts) == 2 else 0.82
        if allow_comma and COMMA.search(artist):
            confidence = min(confidence, 0.6)
        proposals.append({
            "field": "artists",
            "old": artist,
            "new": "; ".join(parts),
            "values": parts,
            "confidence": confidence,
            "reason": f"Stored as {len(parts)} separate artists, so players list "
                      f"each one instead of treating the credit as a new artist",
        })
        split_proposed = True

    # 1. The artist tag itself: filename shape, stray separators, spacing.
    if clean_artist and clean_artist != artist and not split_proposed:
        proposals.append({
            "field": "artist",
            "old": artist,
            "new": clean_artist,
            "confidence": 0.95 if "_" in artist else 0.88,
            "reason": "Name looks like it came from a filename"
                      if "_" in artist else "Tidied spacing and separators",
        })

    # 2. The album artist: the one that decides how the library groups.
    #    Compare against what is actually stored, not a tidied copy of it —
    #    otherwise an album artist of "3_Doors_Down" looks correct, because
    #    tidying it gives the same answer the proposal already holds.
    clean_album_artist = tidy(album_artist)
    if primary and primary != album_artist:
        if not album_artist:
            confidence, reason = 0.9, "No album artist set, so every credit becomes its own entry"
        elif "_" in album_artist and primary == clean_album_artist:
            confidence, reason = 0.95, "Album artist looks like it came from a filename"
        elif len(parts) > 1:
            confidence = 0.8 if len(parts) == 2 else 0.72
            reason = f"Collapses {len(parts)} credited artists under the primary one"
        elif primary == clean_album_artist:
            confidence, reason = 0.92, "Tidied the album artist"
        else:
            confidence, reason = 0.7, "Album artist does not match the track's primary artist"

        if allow_comma and COMMA.search(artist or "") and len(parts) > 1:
            confidence = min(confidence, 0.6)
            reason += " (comma-separated, so check this one)"

        proposals.append({
            "field": "album_artist",
            "old": album_artist or None,
            "new": primary,
            "confidence": confidence,
            "reason": reason,
        })

    return proposals


def preview(limit: int = 300, allow_comma: bool = False) -> dict:
    """A dry run: what would change, grouped so it can be read at a glance."""
    rows = db.query("SELECT * FROM tracks WHERE missing=0")
    by_change: dict[tuple, dict] = {}
    tracks_touched = set()

    for row in rows:
        for p in assess(dict(row), allow_comma):
            key = (p["field"], p["old"] or "", p["new"])
            entry = by_change.setdefault(key, {
                "field": p["field"], "old": p["old"], "new": p["new"],
                "reason": p["reason"], "confidence": p["confidence"], "tracks": 0,
            })
            entry["tracks"] += 1
            tracks_touched.add(row["id"])

    items = sorted(by_change.values(), key=lambda e: -e["tracks"])
    distinct_before = len({
        (r["album_artist"] or r["artist"] or "").lower() for r in rows
    })
    distinct_after = len({
        (next((p["new"] for p in assess(dict(r), allow_comma)
               if p["field"] == "album_artist"), None)
         or r["album_artist"] or r["artist"] or "").lower()
        for r in rows
    })

    return {
        "items": items[:limit],
        "total_changes": sum(e["tracks"] for e in by_change.values()),
        "tracks": len(tracks_touched),
        "artists_before": distinct_before,
        "artists_after": distinct_after,
    }


def stage(allow_comma: bool = False, min_confidence: float = 0.0) -> int:
    """Queue the changes for Review. Writes nothing to disk."""
    rows = db.query("SELECT * FROM tracks WHERE missing=0")
    staged = 0
    for row in rows:
        track = dict(row)
        for p in assess(track, allow_comma):
            if p["confidence"] < min_confidence:
                continue
            exists = db.one(
                "SELECT id FROM changes WHERE track_id=? AND kind='tag' AND field=? "
                "AND status IN ('pending','approved')", (track["id"], p["field"]),
            )
            if exists:
                continue
            db.execute(
                "INSERT INTO changes(kind, track_id, field, old_value, new_value, payload, "
                "source, confidence) VALUES('tag',?,?,?,?,?,?,?)",
                (track["id"], p["field"], p["old"], p["new"],
                 json.dumps(p["values"]) if p.get("values") else None,
                 "name-cleanup", round(p["confidence"], 3)),
            )
            staged += 1
    if staged:
        db.log(f"Artist name cleanup staged {staged} changes")
    return staged
