"""A canonical genre list, and the rules for getting everything onto it.

A library ends up with hundreds of one-off genres for three reasons: sources
spell things differently ("Nu Metal", "nu-metal", "Nu_Metal"), they disagree
about specificity ("Rock" vs "Alternative Rock" vs "Post-Grunge"), and writing
several genres per track multiplies the distinct values — three genres from a
pool of forty produce thousands of possible strings.

So: one genre per artist, drawn from a fixed vocabulary, with everything else
mapped onto it. You can add your own genres and your own mappings; those win
over the built-in ones.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import db

# The built-in vocabulary. Broad enough to place most music, small enough to
# stay useful as a browsing axis — which is the whole point of a genre tag.
CANON = [
    "Rock", "Alternative Rock", "Indie Rock", "Classic Rock", "Hard Rock",
    "Folk Rock", "Southern Rock", "Psychedelic Rock", "Progressive Rock",
    "Post-Rock", "Grunge", "Shoegaze", "New Wave", "Post-Punk", "Punk",
    "Pop Punk", "Emo", "Hardcore", "Post-Hardcore", "Metal", "Heavy Metal",
    "Alternative Metal", "Nu Metal", "Thrash Metal", "Death Metal",
    "Black Metal", "Doom Metal", "Power Metal", "Progressive Metal",
    "Metalcore", "Deathcore", "Industrial", "Pop", "Synthpop", "Electropop",
    "Dance", "Electronic", "House", "Techno", "Trance", "Drum & Bass",
    "Dubstep", "Trip Hop", "Ambient", "Lo-Fi", "Hip Hop", "Rap", "Trap",
    "R&B", "Soul", "Funk", "Disco", "Jazz", "Blues", "Country", "Bluegrass",
    "Americana", "Folk", "Singer-Songwriter", "Acoustic", "Classical",
    "Soundtrack", "Reggae", "Ska", "Latin", "World", "Gospel", "Experimental",
]

# Spellings and near-misses that should land on a canonical name. Only the
# ones normalization alone cannot solve.
ALIASES = {
    "alt rock": "Alternative Rock", "alt. rock": "Alternative Rock",
    "alternative": "Alternative Rock", "indie": "Indie Rock",
    "alt metal": "Alternative Metal", "alt. metal": "Alternative Metal",
    "numetal": "Nu Metal", "nu-metal": "Nu Metal",
    "post grunge": "Grunge", "post-grunge": "Grunge",
    "hiphop": "Hip Hop", "hip-hop": "Hip Hop", "rap music": "Rap",
    "rnb": "R&B", "r and b": "R&B", "rhythm and blues": "R&B",
    "dnb": "Drum & Bass", "drum and bass": "Drum & Bass",
    "drum n bass": "Drum & Bass", "jungle": "Drum & Bass",
    "edm": "Electronic", "electronica": "Electronic", "idm": "Electronic",
    "synth pop": "Synthpop", "synthwave": "Synthpop", "electro pop": "Electropop",
    "prog rock": "Progressive Rock", "prog": "Progressive Rock",
    "prog metal": "Progressive Metal",
    "screamo": "Post-Hardcore", "melodic hardcore": "Hardcore",
    "melodic death metal": "Death Metal", "melodeath": "Death Metal",
    "speed metal": "Thrash Metal", "groove metal": "Heavy Metal",
    "sludge": "Doom Metal", "stoner rock": "Hard Rock", "stoner metal": "Doom Metal",
    "djent": "Progressive Metal",
    "singer songwriter": "Singer-Songwriter",
    "lofi": "Lo-Fi", "lo fi": "Lo-Fi", "chillhop": "Lo-Fi",
    "film score": "Soundtrack", "score": "Soundtrack", "ost": "Soundtrack",
    "video game music": "Soundtrack", "chiptune": "Soundtrack",
    "worship": "Gospel", "christian": "Gospel", "christian rock": "Gospel",
    "country rock": "Country", "alt country": "Americana",
    "celtic": "Folk", "folk punk": "Folk",
    "dance pop": "Dance", "eurodance": "Dance", "disco house": "House",
    "deep house": "House", "tech house": "House", "progressive house": "House",
    "dub": "Reggae", "dancehall": "Reggae", "roots reggae": "Reggae",
    "noise": "Experimental", "drone": "Ambient", "avant garde": "Experimental",
    "k-pop": "Pop", "j-pop": "Pop", "j-rock": "Rock", "visual kei": "Rock",
    "afrobeat": "World", "salsa": "Latin", "flamenco": "Latin",
    "bossa nova": "Latin", "reggaeton": "Latin",
    "bebop": "Jazz", "swing": "Jazz", "smooth jazz": "Jazz", "jazz fusion": "Jazz",
    "ragtime": "Jazz", "big band": "Jazz",
    "baroque": "Classical", "opera": "Classical", "orchestral": "Classical",
    "romantic": "Classical", "contemporary classical": "Classical",
    "grime": "Rap", "drill": "Trap", "trap metal": "Trap",
    "hardstyle": "Dance", "breakbeat": "Electronic", "garage": "Electronic",
    "emo rap": "Rap", "pop rock": "Rock", "soft rock": "Rock",
    "arena rock": "Classic Rock", "glam rock": "Classic Rock",
    "blues rock": "Blues", "rock and roll": "Classic Rock",
    "rockabilly": "Classic Rock", "surf rock": "Classic Rock",
    "neo soul": "Soul", "motown": "Soul", "gospel soul": "Gospel",
}

_NORM = re.compile(r"[^\w&]+")


def norm(text: str) -> str:
    return _NORM.sub(" ", (text or "").lower()).strip()


def _rules() -> dict:
    """The user's own genres and mappings, which override the built-ins."""
    return db.get_setting("genre_rules", {"custom": [], "map": {}})


def save_rules(rules: dict) -> dict:
    db.set_setting("genre_rules", rules)
    return rules


def vocabulary() -> list[str]:
    """Everything a genre is allowed to be: built-ins plus your own.

    CANON is written in family order because that is how it reads as source.
    Anywhere it is presented as a list to pick from, alphabetical is the only
    order that lets you find something in sixty-odd entries.
    """
    rules = _rules()
    combined = CANON + [g for g in rules.get("custom", []) if g not in CANON]
    return sorted(combined, key=lambda g: g.lower())


def canonicalize(tag: str) -> str | None:
    """Put one incoming tag onto the vocabulary, or return None if it will not go.

    Order matters: your own mappings first, then your own genres, then the
    built-in aliases, then the built-in list, then a containment match so
    "melodic doom metal" finds "Doom Metal" rather than falling through.
    """
    if not tag:
        return None
    key = norm(tag)
    if not key:
        return None

    rules = _rules()

    mapped = rules.get("map", {}).get(key)
    if mapped:
        return mapped

    for custom in rules.get("custom", []):
        if norm(custom) == key:
            return custom

    if key in ALIASES:
        return ALIASES[key]

    for name in CANON:
        if norm(name) == key:
            return name

    # "melodic doom metal" contains "doom metal". Prefer the longest match so
    # it lands on Doom Metal rather than the broader Metal.
    hits = [n for n in vocabulary() if norm(n) in key]
    if hits:
        return max(hits, key=lambda n: len(norm(n)))

    return None


def canonicalize_all(tags: list[str], limit: int = 1) -> list[str]:
    out, seen = [], set()
    for tag in tags:
        value = canonicalize(tag)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
        if len(out) >= limit:
            break
    return out


def add_custom(name: str) -> dict:
    rules = _rules()
    name = name.strip()
    if name and name not in rules.setdefault("custom", []):
        rules["custom"].append(name)
        db.log(f"Added the genre {name!r}")
    return save_rules(rules)


def remove_custom(name: str) -> dict:
    rules = _rules()
    rules["custom"] = [g for g in rules.get("custom", []) if g != name]
    rules["map"] = {k: v for k, v in rules.get("map", {}).items() if v != name}
    return save_rules(rules)


def add_mapping(from_tag: str, to_genre: str) -> dict:
    """Send one spelling to a genre, permanently."""
    rules = _rules()
    rules.setdefault("map", {})[norm(from_tag)] = to_genre
    return save_rules(rules)


def remove_mapping(from_tag: str) -> dict:
    rules = _rules()
    rules.get("map", {}).pop(norm(from_tag), None)
    return save_rules(rules)


def distribution() -> dict:
    """Every genre value actually in the library, and where each would land.

    This is the view that makes the long tail visible: how many distinct
    values you have, how many are one-offs, and which of them the vocabulary
    cannot place without your help.
    """
    rows = db.query(
        "SELECT genre, COUNT(*) AS tracks, COUNT(DISTINCT COALESCE(album_artist, artist)) "
        "  AS artists "
        "FROM tracks WHERE missing=0 AND genre IS NOT NULL AND genre <> '' "
        "GROUP BY genre ORDER BY genre COLLATE NOCASE"
    )

    items, unmapped = [], []
    landing: dict[str, int] = defaultdict(int)
    for r in rows:
        # A stored value can itself hold several genres; split before judging.
        parts = [p.strip() for p in str(r["genre"]).split(";") if p.strip()]
        target = canonicalize_all(parts, limit=1)
        entry = {
            "value": r["genre"], "tracks": r["tracks"], "artists": r["artists"],
            "becomes": target[0] if target else None,
        }
        items.append(entry)
        if target:
            landing[target[0]] += r["tracks"]
        else:
            unmapped.append(entry)

    return {
        "distinct": len(items),
        "one_offs": sum(1 for i in items if i["tracks"] == 1),
        "items": items,
        "unmapped": unmapped,
        "after": sorted(
            ({"genre": g, "tracks": n} for g, n in landing.items()),
            key=lambda e: e["genre"].lower(),
        ),
        "vocabulary": vocabulary(),
    }


def stage_cleanup(only_mapped: bool = True) -> dict:
    """Propose canonical genres for everything already tagged.

    Existing tags are read, put onto the vocabulary, and staged where they
    differ. Values the vocabulary cannot place are left alone by default —
    those are the ones worth a decision from you rather than a guess.
    """
    rows = db.query(
        "SELECT id, genre FROM tracks "
        "WHERE missing=0 AND genre IS NOT NULL AND genre <> ''"
    )
    staged = skipped = 0
    for row in rows:
        parts = [p.strip() for p in str(row["genre"]).split(";") if p.strip()]
        target = canonicalize_all(parts, limit=1)
        if not target:
            skipped += 1
            continue
        if target[0] == row["genre"]:
            continue
        exists = db.one(
            "SELECT id FROM changes WHERE track_id=? AND field='genre' "
            "AND status IN ('pending','approved')", (row["id"],),
        )
        if exists:
            continue
        db.execute(
            "INSERT INTO changes(kind, track_id, field, old_value, new_value, source, "
            "confidence) VALUES('tag',?,'genre',?,?,'genre-cleanup',0.9)",
            (row["id"], row["genre"], target[0]),
        )
        staged += 1

    if staged:
        db.log(f"Genre cleanup staged {staged} changes; "
               f"{skipped} values could not be placed on the list")
    return {"staged": staged, "unplaced": skipped}


def tracks_for(value: str, limit: int = 60) -> dict:
    """Who and what carries one genre value.

    A count alone does not tell you where a stray tag belongs. Seeing that
    "Sea Shanty" is four artists you recognise as folk does.
    """
    artists = db.query(
        "SELECT COALESCE(album_artist, artist) AS artist, COUNT(*) AS tracks "
        "FROM tracks WHERE missing=0 AND genre=? "
        "GROUP BY artist ORDER BY tracks DESC LIMIT 40",
        (value,),
    )
    tracks = db.query(
        "SELECT id, title, artist, album_artist, album, path, folder "
        "FROM tracks WHERE missing=0 AND genre=? "
        "ORDER BY album_artist, album, disc_no, track_no LIMIT ?",
        (value, limit),
    )
    total = db.one("SELECT COUNT(*) AS n FROM tracks WHERE missing=0 AND genre=?",
                   (value,))["n"]
    return {
        "value": value,
        "total": total,
        "becomes": (canonicalize_all([p.strip() for p in value.split(";") if p.strip()],
                                     limit=1) or [None])[0],
        "artists": db.rows_to_dicts(artists),
        "tracks": db.rows_to_dicts(tracks),
    }


def _artist_norm(name: str) -> str:
    from .scanner import normalize
    return normalize(name or "")


def artist_pins() -> dict:
    return _rules().get("artists", {})


def pin_artist(artist: str, genre: str) -> dict:
    """Fix an artist to a genre for good, so lookups stop second-guessing it."""
    rules = _rules()
    rules.setdefault("artists", {})[_artist_norm(artist)] = genre
    return save_rules(rules)


def unpin_artist(artist: str) -> dict:
    rules = _rules()
    rules.get("artists", {}).pop(_artist_norm(artist), None)
    return save_rules(rules)


def pinned_genre(artist: str) -> str | None:
    return artist_pins().get(_artist_norm(artist))


def artists_elsewhere(value: str, target: str | None = None) -> dict:
    """The artists in one genre value, and where else in the library they sit.

    Mapping a stray genre fixes the tracks carrying it and leaves the same
    artist scattered under everything else they were tagged with. This finds
    that scattering so it can be dealt with in one decision per artist —
    per artist, because "Epic Music" might be Two Steps From Hell and Tool,
    and those two do not belong in the same place.
    """
    here = db.query(
        "SELECT COALESCE(album_artist, artist) AS artist, COUNT(*) AS tracks "
        "FROM tracks WHERE missing=0 AND genre=? AND COALESCE(album_artist, artist) <> '' "
        "GROUP BY artist ORDER BY tracks DESC",
        (value,),
    )

    out = []
    for row in here:
        artist = row["artist"]
        others = db.query(
            "SELECT genre, COUNT(*) AS tracks FROM tracks "
            "WHERE missing=0 AND COALESCE(album_artist, artist) = ? "
            "  AND genre IS NOT NULL AND genre <> '' AND genre <> ? "
            "GROUP BY genre ORDER BY tracks DESC",
            (artist, value),
        )
        others = db.rows_to_dicts(others)
        if not others:
            continue
        out.append({
            "artist": artist,
            "tracks_here": row["tracks"],
            "elsewhere": others,
            "tracks_elsewhere": sum(o["tracks"] for o in others),
            "pinned": pinned_genre(artist),
        })

    return {"value": value, "target": target, "artists": out}


def assign_artist(artist: str, genre: str, pin: bool = True) -> dict:
    """Stage every track by an artist onto one genre.

    Staged, not written — it goes through Review like everything else. The pin
    is what stops a later lookup quietly undoing the decision.
    """
    if genre not in vocabulary():
        raise ValueError(f"{genre!r} is not one of your genres")

    rows = db.query(
        "SELECT id, genre FROM tracks WHERE missing=0 "
        "AND COALESCE(album_artist, artist) = ?", (artist,),
    )
    staged = 0
    for row in rows:
        if (row["genre"] or "") == genre:
            continue
        exists = db.one(
            "SELECT id FROM changes WHERE track_id=? AND field='genre' "
            "AND status IN ('pending','approved')", (row["id"],),
        )
        if exists:
            db.execute(
                "UPDATE changes SET new_value=?, source='artist-genre', confidence=0.95 "
                "WHERE track_id=? AND field='genre' AND status='pending'",
                (genre, row["id"]),
            )
            continue
        db.execute(
            "INSERT INTO changes(kind, track_id, field, old_value, new_value, source, "
            "confidence) VALUES('tag',?,'genre',?,?,'artist-genre',0.95)",
            (row["id"], row["genre"], genre),
        )
        staged += 1

    if pin:
        pin_artist(artist, genre)
    db.log(f"{artist}: {staged} tracks staged as {genre}")
    return {"artist": artist, "genre": genre, "staged": staged, "tracks": len(rows)}
