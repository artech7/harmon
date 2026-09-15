"""Finds duplicate tracks and decides which copy to keep.

Two copies of the same song on *different* albums are a normal, wanted thing —
a track appears on its studio album and again on a greatest-hits release.
Harmon separates those out as 'cross_album' and never proposes deleting them.
Only copies sitting inside the same album are treated as real duplicates.
"""
from __future__ import annotations

import os
from collections import defaultdict

from . import db
from .config import get as get_config

CODEC_RANK = {
    "flac": 100, "alac": 95, "wav": 90, "aiff": 88, "ape": 85, "wavpack": 84,
    "opus": 60, "aac": 55, "vorbis": 50, "mp3": 45, "wma": 30,
}


def _bucket(track: dict) -> tuple:
    """What counts as "the same album" for the purpose of deleting something.

    The folder has to agree, not just the tag. Tags are the least reliable
    thing in a library — three different Coldplay releases can all carry the
    album name "Greatest Songs" because one bad tagger got at them, and
    trusting that groups a single, a standard album and a deluxe edition into
    one pile with two of them marked for deletion.

    A folder is what the person who built the library actually decided. So a
    removal needs both to agree: same folder *and* same album tag, on the same
    disc. Copies that are genuinely redundant across folders are still caught
    by the byte-identical check, which verifies the contents in full.
    """
    album = (track.get("album") or "").strip()
    folder = track.get("folder") or os.path.dirname(track.get("path") or "")
    return (folder, track["album_key"] if album else "", track.get("disc_no") or 0)


def _score(track: dict, rule: str) -> tuple:
    codec = CODEC_RANK.get((track.get("codec") or "").lower(), 10)
    bitrate = track.get("bitrate") or 0
    size = track.get("size") or 0
    has_art = track.get("has_art") or 0
    complete = sum(1 for f in ("artist", "album", "title", "year", "genre") if track.get(f))

    if rule == "size":
        return (size, codec, bitrate, has_art, complete)
    if rule == "lossless":
        return (track.get("lossless") or 0, codec, bitrate, size, complete)
    if rule == "newest":
        return (track.get("mtime") or 0, bitrate, size, complete)
    return (codec if track.get("lossless") else 0, bitrate, size, has_art, complete)


def _pick_keeper(members: list[dict], rule: str) -> int:
    best = max(members, key=lambda t: _score(t, rule))
    return best["id"]


def _reason(track: dict, keeper: dict) -> str:
    if track["id"] == keeper["id"]:
        bits = []
        if track.get("lossless"):
            bits.append("lossless")
        if track.get("bitrate"):
            bits.append(f"{track['bitrate']} kbps")
        if track.get("has_art"):
            bits.append("has artwork")
        return "Keeping: " + (", ".join(bits) if bits else "best available copy")
    if (track.get("bitrate") or 0) < (keeper.get("bitrate") or 0):
        return f"Lower bitrate ({track.get('bitrate') or '?'} vs {keeper.get('bitrate')} kbps)"
    if (track.get("size") or 0) < (keeper.get("size") or 0):
        return "Smaller file, same song"
    return "Duplicate copy"


def _store_group(kind: str, key: str, members: list[dict], keeper_id: int, title, artist) -> int:
    row = db.one("SELECT id FROM dupe_groups WHERE kind=? AND key=?", (kind, key))
    if row:
        gid = row["id"]
        db.execute("DELETE FROM dupe_members WHERE group_id=?", (gid,))
        db.execute("UPDATE dupe_groups SET title=?, artist=? WHERE id=?", (title, artist, gid))
    else:
        gid = db.execute(
            "INSERT INTO dupe_groups(kind, key, title, artist) VALUES(?,?,?,?)",
            (kind, key, title, artist),
        )
    keeper = next(m for m in members if m["id"] == keeper_id)
    for m in members:
        if kind == "cross_album":
            # Nothing is dropped here, so the wording describes rather than judges.
            reason = f"On “{m.get('album') or 'no album'}”"
            keeper_flag = 0
        else:
            reason = _reason(m, keeper)
            keeper_flag = 1 if m["id"] == keeper_id else 0
        db.execute(
            "INSERT INTO dupe_members(group_id, track_id, keeper, reason) VALUES(?,?,?,?)",
            (gid, m["id"], keeper_flag, reason),
        )
    return gid


def find() -> dict:
    """Rebuild every duplicate group from the current track table."""
    cfg = get_config()["dupes"]
    tolerance = float(cfg["duration_tolerance"])
    rule = cfg["keeper_rule"]

    db.execute("DELETE FROM dupe_members")
    db.execute("DELETE FROM dupe_groups")

    tracks = [dict(r) for r in db.query("SELECT * FROM tracks WHERE missing=0")]
    stats = {"identical": 0, "same_album": 0, "cross_album": 0, "reclaimable_bytes": 0}

    # 1. Byte-for-byte identical files, wherever they live.
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for t in tracks:
        if t["content_hash"]:
            by_hash[t["content_hash"]].append(t)

    identical_ids: set[int] = set()
    for h, members in by_hash.items():
        if len(members) < 2:
            continue
        keeper_id = _pick_keeper(members, rule)
        _store_group("identical", h, members, keeper_id, members[0]["title"], members[0]["artist"])
        identical_ids.update(m["id"] for m in members)
        stats["identical"] += 1
        stats["reclaimable_bytes"] += sum(m["size"] or 0 for m in members if m["id"] != keeper_id)

    # 2. Same song by tag + duration. Bucket by album to tell the two cases apart.
    by_song: dict[str, list[dict]] = defaultdict(list)
    for t in tracks:
        if t["id"] in identical_ids or not t["norm_key"] or t["norm_key"].strip("|") == "":
            continue
        by_song[t["norm_key"]].append(t)

    for key, group in by_song.items():
        if len(group) < 2:
            continue
        clusters: list[list[dict]] = []
        for t in sorted(group, key=lambda x: x["duration"] or 0):
            for c in clusters:
                if abs((t["duration"] or 0) - (c[0]["duration"] or 0)) <= tolerance:
                    c.append(t)
                    break
            else:
                clusters.append([t])

        for idx, cluster in enumerate(clusters):
            if len(cluster) < 2:
                continue
            albums: dict[tuple, list[dict]] = defaultdict(list)
            for t in cluster:
                albums[_bucket(t)].append(t)

            for album_key, members in albums.items():
                if len(members) < 2:
                    continue
                keeper_id = _pick_keeper(members, rule)
                _store_group(
                    "same_album", f"{key}#{idx}#{album_key}", members, keeper_id,
                    members[0]["title"], members[0]["album_artist"] or members[0]["artist"],
                )
                stats["same_album"] += 1
                stats["reclaimable_bytes"] += sum(
                    m["size"] or 0 for m in members if m["id"] != keeper_id
                )

            if len(albums) > 1:
                flat = [t for m in albums.values() for t in m]
                _store_group(
                    "cross_album", f"{key}#{idx}", flat, flat[0]["id"],
                    flat[0]["title"], flat[0]["album_artist"] or flat[0]["artist"],
                )
                stats["cross_album"] += 1

    db.log(
        f"Duplicate check: {stats['identical']} identical, {stats['same_album']} within an album, "
        f"{stats['cross_album']} across different albums (left alone)"
    )
    return stats


def group_detail(group_id: int) -> dict | None:
    g = db.one("SELECT * FROM dupe_groups WHERE id=?", (group_id,))
    if not g:
        return None
    members = db.rows_to_dicts(db.query(
        "SELECT t.*, m.keeper, m.reason FROM dupe_members m "
        "JOIN tracks t ON t.id = m.track_id WHERE m.group_id=? ORDER BY m.keeper DESC",
        (group_id,),
    ))
    folders = {m.get("folder") for m in members}
    return {
        **dict(g),
        "members": members,
        # Copies in one folder are an album with a duplicate in it. Copies
        # spread across folders are two albums that happen to share a name,
        # and deserve a look before anything is deleted.
        "same_folder": len(folders) == 1,
        "folders": sorted(f for f in folders if f),
    }


def list_groups(kind: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    sql = (
        "SELECT g.*, COUNT(m.track_id) AS copies, "
        "SUM(CASE WHEN m.keeper=0 THEN COALESCE(t.size,0) ELSE 0 END) AS reclaimable "
        "FROM dupe_groups g JOIN dupe_members m ON m.group_id=g.id "
        "JOIN tracks t ON t.id=m.track_id "
    )
    args: tuple = ()
    if kind:
        sql += "WHERE g.kind=? "
        args = (kind,)
    sql += "GROUP BY g.id ORDER BY reclaimable DESC LIMIT ? OFFSET ?"
    return db.rows_to_dicts(db.query(sql, args + (limit, offset)))
