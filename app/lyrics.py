"""Lyrics: what the library has, and filling in what it does not.

Synced lyrics (`.lrc`, with timestamps) are the goal — they are what makes a
player scroll along with the song, and what Jellyfin, Navidrome and Subsonic
clients look for. Plain text is a fallback worth keeping but not worth
preferring.

Everything here writes sidecar files next to the track and never touches the
audio file itself. A sidecar is reversible by deleting it; rewriting 21,000
files' tags is not. Embedded lyrics are still *read*, so a track that already
has them is not sent off to be downloaded again.
"""
from __future__ import annotations

import os
import threading
import time

import httpx
import mutagen

from . import db

LRCLIB = "https://lrclib.net/api/get"
_lock = threading.Lock()
_last = 0.0

# LRCLIB asks for sequential requests with a 200–500ms gap between them,
# measured from when one finishes to when the next starts. 300ms sits in the
# middle of that. The lock makes them sequential; the stamp below is taken
# after the response so the gap is a real gap rather than overlapping the
# request's own duration.
MIN_INTERVAL = 0.30

# Consecutive times the service itself has refused, which is different from a
# track simply not being in their database.
_service_failures = 0


class ServiceUnavailable(RuntimeError):
    """LRCLIB is refusing, not answering "no". Wait rather than push on."""


# How long to wait after each consecutive outage before trying again. A run
# started at bedtime should survive their bad half-hour, not give up on it.
OUTAGE_WAITS = [60, 180, 600, 1800, 3600]


def _wait(seconds: float, progress=None, note: str = "") -> None:
    """Sleep in slices so the progress callback still runs.

    The callback is what raises when you press Stop, so sleeping through it in
    one go would make a waiting job unstoppable for an hour.
    """
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(2.0, left))
        if progress:
            progress(None, f"{note} — trying again in {int(left)}s")

SYNCED, UNSYNCED, NONE = "synced", "unsynced", "none"


def sidecar(path: str, ext: str) -> str:
    return os.path.splitext(path)[0] + ext


def _has_embedded(path: str) -> str | None:
    """Read lyrics out of the audio file's own tags, whatever the format.

    Only used to decide whether a track already has lyrics. Harmon never
    writes here.
    """
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None or not audio.tags:
        return None

    try:
        # MP3 carries them in USLT (unsynced) or SYLT (synced) frames.
        for key in audio.tags.keys():
            upper = str(key).upper()
            if upper.startswith("SYLT"):
                return SYNCED
            if upper.startswith("USLT"):
                frame = audio.tags[key]
                text = getattr(frame, "text", "") or ""
                return SYNCED if "[" in text[:200] and "]" in text[:200] else UNSYNCED

        # Vorbis comments (FLAC, OGG) and MP4 use plain fields.
        for field in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS", "\xa9lyr"):
            value = audio.tags.get(field)
            if not value:
                continue
            text = value[0] if isinstance(value, list) else str(value)
            # An LRC body starts its lines with a [mm:ss.xx] timestamp.
            return SYNCED if "[" in str(text)[:200] and "]" in str(text)[:200] else UNSYNCED
    except Exception:
        return None
    return None


def state_for(path: str) -> str:
    """Synced, unsynced, or nothing — in that order of preference."""
    if os.path.exists(sidecar(path, ".lrc")):
        return SYNCED
    embedded = _has_embedded(path)
    if embedded == SYNCED:
        return SYNCED
    if os.path.exists(sidecar(path, ".txt")) or embedded == UNSYNCED:
        return UNSYNCED
    return NONE


def scan(progress=None) -> dict:
    """Work out what every track has. This is the "what do I already own" pass."""
    rows = db.query("SELECT id, path FROM tracks WHERE missing=0")
    counts = {SYNCED: 0, UNSYNCED: 0, NONE: 0}
    total = max(len(rows), 1)

    for i, row in enumerate(rows):
        state = state_for(row["path"])
        counts[state] += 1
        db.execute("UPDATE tracks SET lyrics=? WHERE id=?", (state, row["id"]))
        if progress and i % 50 == 0:
            progress(i / total, f"Checked {i} of {len(rows)} tracks")

    db.log(f"Lyrics: {counts[SYNCED]} synced, {counts[UNSYNCED]} plain text only, "
           f"{counts[NONE]} with none")
    return counts


def coverage() -> dict:
    rows = db.query(
        "SELECT COALESCE(lyrics, 'unknown') AS state, COUNT(*) AS n "
        "FROM tracks WHERE missing=0 GROUP BY state"
    )
    counts = {r["state"]: r["n"] for r in rows}
    total = sum(counts.values())
    return {
        "total": total,
        "synced": counts.get(SYNCED, 0),
        "unsynced": counts.get(UNSYNCED, 0),
        "none": counts.get(NONE, 0),
        "unknown": counts.get("unknown", 0),
        "staged": db.one(
            "SELECT COUNT(*) AS n FROM changes WHERE kind='lyrics' "
            "AND status IN ('pending','approved')")["n"],
    }


def _get(params: dict) -> dict | None:
    """One request, sequential and paced, as LRCLIB asks.

    They are a free service with no key and no hard limit published, which is
    a request to behave rather than an invitation not to.
    """
    global _last
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                r = client.get(LRCLIB, params=params, headers={
                    "User-Agent": "Harmon/1.0 (https://github.com/artech7/harmon)",
                })
        finally:
            # Stamped after the response, so the next request waits a full
            # interval from this one finishing — not from it starting.
            _last = time.time()

    global _service_failures

    if r.status_code == 404:
        _service_failures = 0          # they answered; this track just is not there
        return None

    # 429 is "slow down", 503 is "not right now", 502/504 are a bad day at
    # their proxy. None of them are about this track, and all of them get
    # worse if you keep knocking.
    if r.status_code in (429, 502, 503, 504):
        _service_failures += 1
        header = r.headers.get("Retry-After")
        pause = float(header) if header and header.isdigit() else min(
            2 ** min(_service_failures, 6), 60)
        if _service_failures <= 2:
            db.log(f"LRCLIB answered {r.status_code}; waiting {pause:.0f}s "
                   f"before trying again", "warn")
        time.sleep(min(pause, 60))
        raise ServiceUnavailable(
            f"LRCLIB is unavailable ({r.status_code}); waited {pause:.0f}s")

    r.raise_for_status()
    _service_failures = 0
    return r.json()


def lookup(track: dict) -> dict | None:
    """Ask LRCLIB for one track.

    Duration is part of the match, which is what keeps a radio edit from
    getting the album version's timings.
    """
    artist = track.get("album_artist") or track.get("artist")
    title = track.get("title")
    if not (artist and title):
        return None

    params = {"artist_name": artist, "track_name": title}
    if track.get("album"):
        params["album_name"] = track["album"]
    if track.get("duration"):
        params["duration"] = int(round(track["duration"]))

    data = _get(params)
    if not data and params.get("duration"):
        # Our duration can be a second or two off theirs; try without it
        # before giving up on the track.
        params.pop("duration", None)
        data = _get(params)
    if not data or data.get("instrumental"):
        return None

    synced = (data.get("syncedLyrics") or "").strip()
    plain = (data.get("plainLyrics") or "").strip()
    if synced:
        return {"kind": SYNCED, "ext": ".lrc", "body": synced}
    if plain:
        return {"kind": UNSYNCED, "ext": ".txt", "body": plain}
    return None


def stage(track_ids: list[int] | None = None, limit: int = 0, progress=None) -> dict:
    """Look tracks up and stage the sidecars. Writes nothing to disk.

    Tracks that already have synced lyrics are left alone. Tracks with plain
    text only are still tried, because a `.lrc` is an upgrade on a `.txt` —
    and per your setting the `.txt` stays where it is, since players prefer
    the `.lrc` when both are present.
    """
    if track_ids:
        marks = ",".join("?" for _ in track_ids)
        rows = db.query(f"SELECT * FROM tracks WHERE missing=0 AND id IN ({marks})",
                        tuple(track_ids))
    else:
        sql = ("SELECT * FROM tracks WHERE missing=0 AND COALESCE(lyrics,'none') <> 'synced' "
               "ORDER BY album_artist, album, disc_no, track_no")
        rows = db.query(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ())

    result = {"checked": 0, "synced": 0, "plain": 0, "missing": 0, "failed": 0,
              "unavailable": False, "outages": 0}
    total = max(len(rows), 1)

    for i, row in enumerate(rows):
        track = dict(row)
        result["checked"] += 1
        # LRCLIB being down is not a reason to abandon the run. Wait it out,
        # with the waits getting longer, and only give up after an hour of
        # them refusing.
        found = None
        for attempt in range(len(OUTAGE_WAITS) + 1):
            try:
                found = lookup(track)
                result["outages"] = 0
                break
            except ServiceUnavailable:
                if attempt >= len(OUTAGE_WAITS):
                    result["unavailable"] = True
                    break
                pause = OUTAGE_WAITS[attempt]
                result["outages"] += 1
                if attempt == 0:
                    db.log(f"LRCLIB is not responding. Waiting and retrying — "
                           f"this run keeps going on its own.", "warn")
                _wait(pause, progress, f"LRCLIB unavailable ({result['checked']} done)")
            except Exception as exc:
                result["failed"] += 1
                if result["failed"] <= 3:
                    db.log(f"LRCLIB lookup failed: {str(exc)[:140]}", "warn")
                elif result["failed"] == 20:
                    db.log("LRCLIB has failed 20 times in a row; stopping this pass.",
                           "warn")
                    result["unavailable"] = True
                break

        if result["unavailable"]:
            db.log(
                f"Gave up after {result['checked']} tracks: LRCLIB stayed down for "
                f"over an hour. Nothing is lost — what was found is in Review, and "
                f"this picks up where it left off.", "warn")
            break

        if not found:
            result["missing"] += 1
        else:
            # Plain text is only worth writing when there is nothing at all;
            # it is not an upgrade on a `.txt` that already exists.
            if found["kind"] == UNSYNCED and track.get("lyrics") == UNSYNCED:
                result["missing"] += 1
            else:
                target = sidecar(track["path"], found["ext"])
                exists = db.one(
                    "SELECT id FROM changes WHERE track_id=? AND kind='lyrics' "
                    "AND status IN ('pending','approved')", (track["id"],),
                )
                if not exists:
                    db.execute(
                        "INSERT INTO changes(kind, track_id, field, old_value, new_value, "
                        "payload, source, confidence) "
                        "VALUES('lyrics',?,?,?,?,?,'lrclib',?)",
                        (track["id"], found["kind"], track.get("lyrics") or NONE,
                         os.path.basename(target), found["body"],
                         0.95 if found["kind"] == SYNCED else 0.8),
                    )
                result["synced" if found["kind"] == SYNCED else "plain"] += 1

        if progress:
            progress((i + 1) / total,
                     f"{i + 1} of {len(rows)}: {result['synced']} synced lyrics found")

    db.log(f"LRCLIB: {result['synced']} synced and {result['plain']} plain lyric files "
           f"staged from {result['checked']} tracks")
    return result


def write_sidecar(track_id: int, filename: str, body: str) -> str:
    """The only place lyrics reach the disk. Called from the apply pass."""
    row = db.one("SELECT path FROM tracks WHERE id=?", (track_id,))
    if not row:
        raise ValueError("track not found")
    target = os.path.join(os.path.dirname(row["path"]), filename)
    with open(target, "w", encoding="utf-8") as f:
        f.write(body if body.endswith("\n") else body + "\n")
    db.execute(
        "UPDATE tracks SET lyrics=? WHERE id=?",
        (SYNCED if filename.lower().endswith(".lrc") else UNSYNCED, track_id),
    )
    return target
