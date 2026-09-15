"""Album-batched metadata.

Looking a track up on its own costs one request. Looking up the release it
belongs to costs two — and returns every track on it. For a library organised
into albums that is roughly a tenfold reduction, and the matches are better:
a whole tracklist resolves ambiguity that a single title cannot.

Tracks that do not belong to a recognisable album fall through to the
per-track path, and then to fingerprinting.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from . import db, enrich, providers
from .config import ENRICH_FIELDS, get as get_config
from .scanner import normalize

# Fields an album lookup can speak to. Genre is absent on purpose: a release
# lookup carries no usable genre, and genre belongs to the artist anyway, so
# it is decided by vote in genres.py during the per-track pass.
ALBUM_FIELDS = ["artist", "album_artist", "album", "title", "track_no", "disc_no", "year"]


def groups(limit: int = 0) -> list[dict]:
    """Albums with tracks Harmon has not checked yet, biggest first."""
    rows = db.query(
        "SELECT album_key, "
        "  COALESCE(album_artist, artist) AS artist, album, "
        "  COUNT(*) AS tracks, "
        "  SUM(CASE WHEN enriched_at IS NULL THEN 1 ELSE 0 END) AS unchecked "
        "FROM tracks "
        "WHERE missing=0 AND album IS NOT NULL AND album <> '' "
        "GROUP BY album_key HAVING unchecked > 0 "
        "ORDER BY tracks DESC" + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    )
    return db.rows_to_dicts(rows)


def loose_track_ids(limit: int = 500) -> list[int]:
    """Tracks with no album to batch with."""
    rows = db.query(
        "SELECT id FROM tracks WHERE missing=0 AND enriched_at IS NULL "
        "AND (album IS NULL OR album = '') ORDER BY scanned_at LIMIT ?",
        (limit,),
    )
    return [r["id"] for r in rows]


def _match(local: list[dict], remote: list[dict]) -> dict[int, dict]:
    """Pair our files against the official tracklist.

    Title first, because it is the most reliable thing in a bad tag. Then
    track number, for files whose titles are filenames. Duration breaks ties.
    """
    pairs: dict[int, dict] = {}
    taken: set[int] = set()

    by_title: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(remote):
        by_title[normalize(r["title"])].append(i)

    for track in local:
        want = normalize(track.get("title"))
        options = [i for i in by_title.get(want, []) if i not in taken]
        if options and track.get("duration"):
            options.sort(key=lambda i: abs((remote[i]["length"] or 0) - track["duration"]))
        if options:
            pairs[track["id"]] = remote[options[0]]
            taken.add(options[0])
            continue

        # No title match: fall back to position, then to the closest length.
        if track.get("track_no"):
            fallback = [
                i for i, r in enumerate(remote)
                if i not in taken and r["track_no"] == track["track_no"]
                and (not track.get("disc_no") or r["disc_no"] == track["disc_no"])
            ]
            if fallback:
                pairs[track["id"]] = remote[fallback[0]]
                taken.add(fallback[0])
                continue

        if track.get("duration"):
            near = [
                (abs((remote[i]["length"] or 0) - track["duration"]), i)
                for i in range(len(remote))
                if i not in taken and remote[i]["length"]
            ]
            if near:
                delta, i = min(near)
                if delta <= 2.0:
                    pairs[track["id"]] = remote[i]
                    taken.add(i)

    return pairs


def _confidence(track: dict, remote: dict, release_score: float) -> float:
    score = 0.55 + 0.35 * release_score
    if track.get("duration") and remote.get("length"):
        delta = abs(track["duration"] - remote["length"])
        score += 0.10 * max(0.0, 1 - delta / 8)
    if normalize(track.get("title")) == normalize(remote.get("title")):
        score += 0.05
    return round(min(score, 0.98), 3)


def enrich_album(album_key: str, batch: str | None = None) -> dict:
    """Two requests for a whole album. Stages changes; writes nothing."""
    rows = db.query("SELECT * FROM tracks WHERE album_key=? AND missing=0", (album_key,))
    if not rows:
        return {"matched": 0, "staged": 0, "tracks": 0}
    local = [dict(r) for r in rows]
    first = local[0]
    artist = first.get("album_artist") or first.get("artist") or ""
    album = first.get("album") or ""
    batch = batch or uuid.uuid4().hex[:12]

    release = providers.mb_find_release(artist, album, len(local))
    if not release:
        return {"matched": 0, "staged": 0, "tracks": len(local), "reason": "no release found"}

    detail = providers.mb_release_tracks(release["id"])
    if not detail or not detail["tracks"]:
        return {"matched": 0, "staged": 0, "tracks": len(local), "reason": "release had no tracklist"}

    cfg = get_config()["enrich"]
    pairs = _match(local, detail["tracks"])
    staged = 0

    for track in local:
        remote = pairs.get(track["id"])
        if not remote:
            continue
        confidence = _confidence(track, remote, release["score"])

        proposed = {
            "title": remote["title"],
            "artist": remote["artist"],
            "album_artist": detail["album_artist"],
            "album": detail["album"],
            "track_no": remote["track_no"],
            "disc_no": remote["disc_no"],
            "year": detail["year"],
        }

        db.execute(
            "DELETE FROM changes WHERE track_id=? AND status='pending' AND kind='tag' "
            "AND source='musicbrainz-album'", (track["id"],),
        )

        for field in ALBUM_FIELDS:
            if field not in ENRICH_FIELDS or not cfg["fields"].get(field, True):
                continue
            value = proposed.get(field)
            if value in (None, ""):
                continue
            if not enrich._meaningfully_different(field, track.get(field), value):
                continue
            if track.get(field) not in (None, "") and not cfg["overwrite_existing"]:
                if confidence < max(cfg["min_confidence"], 0.9):
                    continue
            if confidence < cfg["min_confidence"] and track.get(field) in (None, ""):
                if confidence < 0.55:
                    continue

            db.execute(
                "INSERT INTO changes(batch, kind, track_id, field, old_value, new_value, "
                "source, confidence) VALUES(?,'tag',?,?,?,?,'musicbrainz-album',?)",
                (batch, track["id"], field,
                 str(track[field]) if track.get(field) is not None else None,
                 str(value), confidence),
            )
            staged += 1

        db.execute(
            "UPDATE tracks SET mb_recording=COALESCE(?, mb_recording), mb_release=?, "
            "enriched_at=datetime('now') WHERE id=?",
            (remote.get("mb_recording"), detail["mb_release"], track["id"]),
        )

    # Artwork is per-release, so one lookup covers every track on the album.
    if cfg["embed_art"]:
        art = providers.coverartarchive(detail["mb_release"], cfg["art_min_px"])
        if art:
            for track in local:
                if track.get("has_art") or track["id"] not in pairs:
                    continue
                exists = db.one(
                    "SELECT id FROM changes WHERE track_id=? AND kind='art' "
                    "AND status IN ('pending','approved')", (track["id"],),
                )
                if exists:
                    continue
                db.execute(
                    "INSERT INTO changes(batch, kind, track_id, field, old_value, new_value, "
                    "source, confidence) VALUES(?,'art',?,'artwork','none',?,"
                    "'coverartarchive',0.85)",
                    (batch, track["id"], art),
                )
                staged += 1

    return {"matched": len(pairs), "staged": staged, "tracks": len(local)}


def run(limit: int = 0, progress=None) -> dict:
    """Work through every album with unchecked tracks."""
    providers.revive_all()
    batch = uuid.uuid4().hex[:12]
    albums = groups(limit)
    totals = {"albums": len(albums), "matched": 0, "staged": 0, "tracks": 0,
              "unmatched_albums": 0, "requests": 0, "stopped_early": False}

    for i, album in enumerate(albums):
        # The album path used to swallow its own errors, so a source could fail
        # on every album of the run without ever earning a strike. Route
        # failures through the same breaker the per-track path uses.
        if providers.is_benched("musicbrainz"):
            totals["stopped_early"] = True
            db.log(
                f"Stopped the album pass after {i} of {len(albums)} albums: "
                f"MusicBrainz is not answering. Harmon will pick up where it "
                f"left off on the next pass.", "warn"
            )
            break

        try:
            result = enrich_album(album["album_key"], batch)
            providers.note_success("musicbrainz")
        except providers.RateLimited as exc:
            providers.note_failure(
                "musicbrainz", str(exc)[:140],
                limit=providers.STRIKES_BEFORE_BENCH_RATE_LIMIT,
            )
            continue
        except Exception as exc:
            providers.note_failure("musicbrainz", f"{album['album']!r}: {str(exc)[:110]}")
            continue
        totals["matched"] += result["matched"]
        totals["staged"] += result["staged"]
        totals["tracks"] += result["tracks"]
        totals["requests"] += 2
        if not result["matched"]:
            totals["unmatched_albums"] += 1
        if progress:
            # Raises if the user has asked the job to stop, so a long pass can
            # be interrupted between albums rather than only between runs.
            progress((i + 1) / max(len(albums), 1),
                     f"Album {i + 1} of {len(albums)}: {album['album'] or 'untitled'}")

    if not totals["stopped_early"]:
        db.log(
            f"Album pass: {totals['matched']} of {totals['tracks']} tracks matched across "
            f"{totals['albums']} albums, {totals['staged']} changes staged "
            f"(~{totals['requests']} requests instead of {totals['tracks']})"
        )
    return totals
