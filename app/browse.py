"""Browsing the library as folders.

Everything else in Harmon is organised by what it wants to change. Sometimes
you just want to see what is actually in a directory — which is how you built
the library in the first place, and how you think about it.

Built from the indexed paths rather than by walking the disk, so it is fast on
a NAS share and only shows files Harmon actually knows about.
"""
from __future__ import annotations

import os

from . import db


def roots() -> list[dict]:
    """The configured library folders, with what is under each."""
    out = []
    for lib in db.query("SELECT * FROM libraries ORDER BY name"):
        stats = db.one(
            "SELECT COUNT(*) AS tracks, COALESCE(SUM(size),0) AS bytes "
            "FROM tracks WHERE missing=0 AND path LIKE ?",
            (lib["path"].rstrip("/") + "/%",),
        )
        out.append({
            "name": lib["name"], "path": lib["path"].rstrip("/"),
            "tracks": stats["tracks"], "bytes": stats["bytes"],
        })
    return out


def _is_library_path(path: str) -> bool:
    """Only browse inside a configured library — never the rest of the disk."""
    path = os.path.normpath(path)
    for lib in db.query("SELECT path FROM libraries"):
        root = os.path.normpath(lib["path"])
        if path == root or path.startswith(root + "/"):
            return True
    return False


def listing(path: str) -> dict:
    """Subfolders and tracks directly inside one folder."""
    path = os.path.normpath(path).rstrip("/") or "/"
    if not _is_library_path(path):
        raise ValueError("That folder is outside your configured libraries")

    prefix = path + "/"
    depth = prefix.count("/")

    # Immediate children only: every deeper folder collapses into its ancestor.
    children: dict[str, dict] = {}
    for row in db.query(
        "SELECT folder, COUNT(*) AS tracks, COALESCE(SUM(size),0) AS bytes "
        "FROM tracks WHERE missing=0 AND folder LIKE ? AND folder <> ? "
        "GROUP BY folder", (prefix + "%", path),
    ):
        rest = row["folder"][len(prefix):]
        head = rest.split("/", 1)[0]
        child = prefix + head
        entry = children.setdefault(child, {"path": child, "name": head,
                                            "tracks": 0, "bytes": 0})
        entry["tracks"] += row["tracks"]
        entry["bytes"] += row["bytes"]

    tracks = db.rows_to_dicts(db.query(
        "SELECT t.id, t.path, t.title, t.artist, t.album_artist, t.album, "
        "       t.track_no, t.disc_no, t.year, t.genre, t.codec, t.bitrate, "
        "       t.size, t.duration, t.has_art, t.lossless, t.std_signature, "
        "       (SELECT COUNT(*) FROM changes c "
        "        WHERE c.track_id = t.id AND c.status IN ('pending','approved')) AS pending "
        "FROM tracks t WHERE t.missing=0 AND t.folder = ? "
        "ORDER BY t.disc_no, t.track_no, t.path", (path,),
    ))

    for t in tracks:
        t["name"] = os.path.basename(t["path"])

    crumbs, walk = [], path
    while _is_library_path(walk):
        crumbs.append({"name": os.path.basename(walk) or walk, "path": walk})
        parent = os.path.dirname(walk)
        if parent == walk:
            break
        walk = parent
    crumbs.reverse()

    totals = db.one(
        "SELECT COUNT(*) AS tracks, COALESCE(SUM(size),0) AS bytes "
        "FROM tracks WHERE missing=0 AND (folder = ? OR folder LIKE ?)",
        (path, prefix + "%"),
    )

    return {
        "path": path,
        "parent": os.path.dirname(path) if _is_library_path(os.path.dirname(path)) else None,
        "crumbs": crumbs,
        "folders": sorted(children.values(), key=lambda f: f["name"].lower()),
        "tracks": tracks,
        "total_tracks": totals["tracks"],
        "total_bytes": totals["bytes"],
    }


def folder_for_track(track_id: int) -> str | None:
    row = db.one("SELECT folder FROM tracks WHERE id=?", (track_id,))
    return row["folder"] if row else None
