"""Nothing in Harmon touches a file until a change is approved here.

Every proposed edit lands in the `changes` table as 'pending'. Approving moves it
to 'approved'; the apply pass is the only code in the project that writes tags,
embeds artwork or removes files.
"""
from __future__ import annotations

import os
import shutil

import httpx
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

from . import db, scanner
from .config import get as get_config

EASY_KEYS = {
    "artist": "artist",
    "album_artist": "albumartist",
    "album": "album",
    "title": "title",
    "track_no": "tracknumber",
    "disc_no": "discnumber",
    "year": "date",
    "genre": "genre",
}


def counts() -> dict:
    rows = db.query("SELECT status, kind, COUNT(*) AS n FROM changes GROUP BY status, kind")
    out: dict = {"pending": 0, "approved": 0, "applied": 0, "rejected": 0, "failed": 0,
                 "by_kind": {}}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + r["n"]
        if r["status"] in ("pending", "approved"):
            out["by_kind"][r["kind"]] = out["by_kind"].get(r["kind"], 0) + r["n"]
    return out


def listing(status: str = "pending", kind: str | None = None,
            limit: int = 300, offset: int = 0) -> list[dict]:
    sql = (
        "SELECT c.*, t.path, t.title, t.artist, t.album FROM changes c "
        "JOIN tracks t ON t.id = c.track_id WHERE c.status = ?"
    )
    args: tuple = (status,)
    if kind:
        sql += " AND c.kind = ?"
        args += (kind,)
    sql += " ORDER BY c.confidence DESC, c.id LIMIT ? OFFSET ?"
    return db.rows_to_dicts(db.query(sql, args + (limit, offset)))


def set_status(ids: list[int], status: str) -> int:
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    db.execute(f"UPDATE changes SET status=? WHERE id IN ({marks}) AND status='pending'",
               (status, *ids))
    return len(ids)


def approve_all(kind: str | None = None, min_confidence: float = 0.0) -> int:
    sql = "UPDATE changes SET status='approved' WHERE status='pending' AND confidence >= ?"
    args: tuple = (min_confidence,)
    if kind:
        sql += " AND kind = ?"
        args += (kind,)
    db.execute(sql, args)
    return db.one("SELECT COUNT(*) AS n FROM changes WHERE status='approved'")["n"]


# --- writers --------------------------------------------------------------

def _write_tag(path: str, field: str, value: str) -> None:
    key = EASY_KEYS.get(field)
    if not key:
        raise ValueError(f"unsupported field {field}")
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("unreadable audio file")
    if audio.tags is None:
        audio.add_tags()
    audio[key] = str(value)
    audio.save()


def _embed_art(path: str, url: str, min_px: int) -> None:
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        r = client.get(url, headers={"User-Agent": "Harmon/1.0"})
        r.raise_for_status()
        data = r.content
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if len(data) < 4096:
        raise ValueError("artwork download looks empty")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        audio = FLAC(path)
        pic = Picture()
        pic.data, pic.type, pic.mime, pic.desc = data, 3, mime, "Cover"
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
    elif ext in (".m4a", ".mp4", ".alac", ".m4b"):
        audio = MP4(path)
        fmt = MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()
    elif ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path)
    else:
        raise ValueError(f"artwork embedding is not supported for {ext} files")


def _delete_file(path: str) -> None:
    cfg = get_config()["target"]
    if cfg["keep_originals"]:
        dest_root = cfg["originals_path"]
        dest = os.path.join(dest_root, "removed-duplicates", os.path.basename(path))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        base, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            dest = f"{base} ({n}){ext}"
            n += 1
        shutil.move(path, dest)
    else:
        os.remove(path)


# --- apply ----------------------------------------------------------------

def apply_approved(progress=None) -> dict:
    """The only place Harmon writes to the library."""
    rows = db.query(
        "SELECT c.*, t.path FROM changes c JOIN tracks t ON t.id=c.track_id "
        "WHERE c.status='approved' AND c.kind <> 'convert' ORDER BY "
        "CASE c.kind WHEN 'tag' THEN 0 WHEN 'art' THEN 1 ELSE 2 END, c.track_id"
    )
    result = {"applied": 0, "failed": 0, "touched": set()}
    min_px = get_config()["enrich"]["art_min_px"]
    total = max(len(rows), 1)

    for i, row in enumerate(rows):
        try:
            if row["kind"] == "tag":
                _write_tag(row["path"], row["field"], row["new_value"])
            elif row["kind"] == "art":
                _embed_art(row["path"], row["new_value"], min_px)
            elif row["kind"] == "delete":
                _delete_file(row["path"])
                db.execute("UPDATE tracks SET missing=1 WHERE id=?", (row["track_id"],))
            else:
                raise ValueError(f"unknown change type {row['kind']}")
            db.execute(
                "UPDATE changes SET status='applied', applied_at=datetime('now'), error=NULL "
                "WHERE id=?", (row["id"],),
            )
            result["applied"] += 1
            if row["kind"] != "delete":
                result["touched"].add((row["path"], row["track_id"]))
        except Exception as exc:
            db.execute("UPDATE changes SET status='failed', error=? WHERE id=?",
                       (str(exc)[:400], row["id"]))
            result["failed"] += 1
        if progress:
            progress((i + 1) / total, f"Applied {i + 1} of {len(rows)} changes")

    for path, _tid in result["touched"]:
        try:
            scanner.index_file(path, force=True)
        except Exception:
            pass

    result["touched"] = len(result["touched"])
    db.log(f"Applied {result['applied']} changes, {result['failed']} failed")
    return result


def stage_deletes(group_id: int) -> int:
    """Queue removal of every non-keeper in a duplicate group."""
    group = db.one("SELECT * FROM dupe_groups WHERE id=?", (group_id,))
    if not group or group["kind"] == "cross_album":
        return 0
    members = db.query(
        "SELECT m.track_id, m.reason, t.path, t.size FROM dupe_members m "
        "JOIN tracks t ON t.id=m.track_id WHERE m.group_id=? AND m.keeper=0",
        (group_id,),
    )
    for m in members:
        exists = db.one(
            "SELECT id FROM changes WHERE track_id=? AND kind='delete' "
            "AND status IN ('pending','approved')", (m["track_id"],),
        )
        if exists:
            continue
        db.execute(
            "INSERT INTO changes(kind, track_id, field, old_value, new_value, source, confidence) "
            "VALUES('delete',?,'file',?,'removed','duplicate-scan',0.95)",
            (m["track_id"], m["path"]),
        )
    db.execute("UPDATE dupe_groups SET reviewed=1 WHERE id=?", (group_id,))
    return len(members)
