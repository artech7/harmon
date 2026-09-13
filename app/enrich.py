"""Turns provider lookups into staged changes. Writes nothing to disk."""
from __future__ import annotations

import uuid

from . import db, providers
from .config import ENRICH_FIELDS, get as get_config
from .scanner import normalize

TITLE_FIELDS = {"artist", "album_artist", "album", "title", "genre"}


def _resolve(field: str, results: list[dict]) -> tuple[object, str, float] | None:
    """First provider in the configured order that has a value for this field wins."""
    for r in results:
        value = r.get(field)
        if value not in (None, ""):
            return value, r.get("source", "unknown"), float(r.get("score") or 0.5)
    return None


def _meaningfully_different(field: str, old, new) -> bool:
    if old in (None, ""):
        return True
    if field in TITLE_FIELDS:
        return normalize(str(old)) != normalize(str(new)) or str(old).strip() != str(new).strip()
    return str(old).strip() != str(new).strip()


def propose(track_id: int, batch: str | None = None) -> list[dict]:
    """Look one track up and stage whatever needs correcting. Returns the staged rows."""
    track = db.one("SELECT * FROM tracks WHERE id=?", (track_id,))
    if not track:
        return []
    track = dict(track)
    cfg = get_config()["enrich"]
    batch = batch or uuid.uuid4().hex[:12]

    results = providers.gather(track)
    if not results:
        db.execute("UPDATE tracks SET enriched_at=datetime('now') WHERE id=?", (track_id,))
        return []

    # Clear any earlier pending suggestions for this track so we never stack duplicates.
    db.execute("DELETE FROM changes WHERE track_id=? AND status='pending' AND kind IN ('tag','art')",
               (track_id,))

    staged: list[dict] = []
    for field in ENRICH_FIELDS:
        if not cfg["fields"].get(field, True):
            continue
        resolved = _resolve(field, results)
        if not resolved:
            continue
        new_value, source, score = resolved
        old_value = track.get(field)

        if not _meaningfully_different(field, old_value, new_value):
            continue
        if old_value not in (None, "") and not cfg["overwrite_existing"]:
            if score < max(cfg["min_confidence"], 0.9):
                continue
        if score < cfg["min_confidence"] and old_value in (None, ""):
            if score < 0.55:
                continue

        cid = db.execute(
            "INSERT INTO changes(batch, kind, track_id, field, old_value, new_value, source, "
            "confidence) VALUES(?,'tag',?,?,?,?,?,?)",
            (batch, track_id, field, str(old_value) if old_value is not None else None,
             str(new_value), source, round(score, 3)),
        )
        staged.append({"id": cid, "field": field, "old": old_value, "new": new_value,
                       "source": source, "confidence": round(score, 3)})

    if cfg["embed_art"] and not track.get("has_art"):
        art_url = providers.find_art(track, results)
        if art_url:
            cid = db.execute(
                "INSERT INTO changes(batch, kind, track_id, field, old_value, new_value, source, "
                "confidence) VALUES(?,'art',?,'artwork',?,?,?,?)",
                (batch, track_id, "none", art_url,
                 next((r.get("source") for r in results if r.get("art_url")), "coverartarchive"),
                 0.8),
            )
            staged.append({"id": cid, "field": "artwork", "old": None, "new": art_url,
                           "source": "artwork", "confidence": 0.8})

    mb = next((r for r in results if r.get("source") == "musicbrainz"), None)
    if mb:
        db.execute(
            "UPDATE tracks SET mb_recording=COALESCE(?, mb_recording), "
            "mb_release=COALESCE(?, mb_release) WHERE id=?",
            (mb.get("mb_recording"), mb.get("mb_release"), track_id),
        )
    db.execute("UPDATE tracks SET enriched_at=datetime('now') WHERE id=?", (track_id,))
    return staged


def propose_many(track_ids: list[int], progress=None) -> dict:
    batch = uuid.uuid4().hex[:12]
    total = max(len(track_ids), 1)
    staged = 0
    for i, tid in enumerate(track_ids):
        staged += len(propose(tid, batch))
        if progress:
            progress((i + 1) / total, f"Checked {i + 1} of {len(track_ids)} tracks")
    db.log(f"Metadata check staged {staged} suggested changes across {len(track_ids)} tracks")
    return {"batch": batch, "staged": staged, "tracks": len(track_ids)}


def pending_track_ids(limit: int = 500) -> list[int]:
    """Tracks that have never been looked up, oldest first."""
    rows = db.query(
        "SELECT id FROM tracks WHERE missing=0 AND enriched_at IS NULL "
        "ORDER BY scanned_at LIMIT ?",
        (limit,),
    )
    return [r["id"] for r in rows]
