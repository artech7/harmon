"""Harmon — HTTP API and web UI host."""
from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager

import mutagen
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import changes, config, db, dupes, enrich, scanner, transcode, worker

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.log("Harmon started")
    worker.start()
    yield
    worker.stop()


api = FastAPI(title="Harmon", version="1.0", lifespan=lifespan)


# --- status ---------------------------------------------------------------

@api.get("/api/status")
def status():
    stats = db.one(
        "SELECT COUNT(*) AS tracks, "
        "COALESCE(SUM(size),0) AS bytes, "
        "COALESCE(SUM(duration),0) AS seconds, "
        "COUNT(DISTINCT album_key) AS albums, "
        "COUNT(DISTINCT COALESCE(album_artist, artist)) AS artists, "
        "SUM(CASE WHEN has_art=0 THEN 1 ELSE 0 END) AS no_art, "
        "SUM(CASE WHEN genre IS NULL OR genre='' THEN 1 ELSE 0 END) AS no_genre "
        "FROM tracks WHERE missing=0"
    )
    dupe_stats = db.one(
        "SELECT SUM(CASE WHEN kind='same_album' THEN 1 ELSE 0 END) AS same_album, "
        "SUM(CASE WHEN kind='identical' THEN 1 ELSE 0 END) AS identical, "
        "SUM(CASE WHEN kind='cross_album' THEN 1 ELSE 0 END) AS cross_album "
        "FROM dupe_groups"
    )
    reclaimable = db.one(
        "SELECT COALESCE(SUM(t.size),0) AS n FROM dupe_members m "
        "JOIN tracks t ON t.id=m.track_id JOIN dupe_groups g ON g.id=m.group_id "
        "WHERE m.keeper=0 AND g.kind<>'cross_album'"
    )["n"]
    return {
        "shell": config.get()["shell"],
        "library": dict(stats),
        "duplicates": {k: (v or 0) for k, v in dict(dupe_stats).items()},
        "reclaimable_bytes": reclaimable,
        "changes": changes.counts(),
        "worker": worker.status(),
        "ffmpeg": transcode.ffmpeg_available(),
        "libraries": db.rows_to_dicts(db.query("SELECT * FROM libraries ORDER BY id")),
    }


@api.get("/api/activity")
def activity(limit: int = 40):
    return db.rows_to_dicts(
        db.query("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
    )


@api.get("/api/jobs")
def jobs(limit: int = 20):
    return db.rows_to_dicts(db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)))


# --- libraries ------------------------------------------------------------

@api.get("/api/libraries")
def list_libraries():
    return db.rows_to_dicts(db.query("SELECT * FROM libraries ORDER BY id"))


@api.post("/api/libraries")
def add_library(payload: dict = Body(...)):
    path = (payload.get("path") or "").rstrip("/")
    name = payload.get("name") or os.path.basename(path) or "Music"
    if not path:
        raise HTTPException(400, "A folder path is required")
    if not os.path.isdir(path):
        raise HTTPException(400, f"Harmon cannot see {path} from inside its container")
    try:
        lib_id = db.execute(
            "INSERT INTO libraries(name, path, watch) VALUES(?,?,?)",
            (name, path, 1 if payload.get("watch", True) else 0),
        )
    except Exception:
        raise HTTPException(400, "That folder is already set up as a library")
    db.log(f"Added library '{name}' at {path}")
    return {"id": lib_id}


@api.delete("/api/libraries/{lib_id}")
def remove_library(lib_id: int):
    db.execute("DELETE FROM libraries WHERE id=?", (lib_id,))
    return {"ok": True}


# --- settings -------------------------------------------------------------

@api.get("/api/config")
def get_config():
    return config.get()


@api.put("/api/config")
def put_config(patch: dict = Body(...)):
    codec = (patch.get("target") or {}).get("codec")
    if codec and codec not in config.CODECS:
        raise HTTPException(400, f"Unknown codec {codec}")
    return config.save(patch)


@api.get("/api/codecs")
def codecs():
    return config.CODECS


# --- actions --------------------------------------------------------------

@api.post("/api/run/{task}")
def run(task: str, payload: dict = Body(default={})):
    if worker.busy():
        raise HTTPException(409, f"Harmon is busy: {worker.status()['message']}")
    tasks = {
        "scan": lambda: worker.run_scan(bool(payload.get("force"))),
        "dupes": worker.run_dupes,
        "enrich": lambda: worker.run_enrich(payload.get("track_ids")),
        "apply": worker.run_apply,
        "convert": worker.run_conversions,
        "pipeline": worker.run_pipeline,
    }
    fn = tasks.get(task)
    if not fn:
        raise HTTPException(404, f"No such task: {task}")
    worker.spawn(fn)
    return {"started": task}


# --- duplicates -----------------------------------------------------------

@api.get("/api/dupes")
def dupe_list(kind: str | None = None, limit: int = 200, offset: int = 0):
    return dupes.list_groups(kind, limit, offset)


@api.get("/api/dupes/{group_id}")
def dupe_detail(group_id: int):
    detail = dupes.group_detail(group_id)
    if not detail:
        raise HTTPException(404, "Duplicate set not found")
    return detail


@api.post("/api/dupes/{group_id}/keeper/{track_id}")
def set_keeper(group_id: int, track_id: int):
    db.execute("UPDATE dupe_members SET keeper=0 WHERE group_id=?", (group_id,))
    db.execute("UPDATE dupe_members SET keeper=1 WHERE group_id=? AND track_id=?",
               (group_id, track_id))
    return dupes.group_detail(group_id)


@api.post("/api/dupes/{group_id}/stage")
def stage_group(group_id: int):
    return {"staged": changes.stage_deletes(group_id)}


@api.post("/api/dupes/stage-all")
def stage_all_dupes():
    groups = db.query("SELECT id FROM dupe_groups WHERE kind IN ('identical','same_album')")
    total = sum(changes.stage_deletes(g["id"]) for g in groups)
    return {"staged": total, "groups": len(groups)}


# --- changes --------------------------------------------------------------

@api.get("/api/changes")
def change_list(status: str = "pending", kind: str | None = None,
                limit: int = 300, offset: int = 0):
    return {"counts": changes.counts(), "items": changes.listing(status, kind, limit, offset)}


@api.post("/api/changes/decide")
def decide(payload: dict = Body(...)):
    ids = payload.get("ids") or []
    status = payload.get("status")
    if status not in ("approved", "rejected"):
        raise HTTPException(400, "status must be 'approved' or 'rejected'")
    return {"updated": changes.set_status(ids, status)}


@api.post("/api/changes/decide-all")
def decide_all(payload: dict = Body(default={})):
    kind = payload.get("kind")
    status = payload.get("status", "approved")
    min_conf = float(payload.get("min_confidence") or 0)
    sql = "UPDATE changes SET status=? WHERE status='pending' AND confidence >= ?"
    args: tuple = (status, min_conf)
    if kind:
        sql += " AND kind=?"
        args += (kind,)
    db.execute(sql, args)
    return changes.counts()


@api.delete("/api/changes/{change_id}")
def drop_change(change_id: int):
    db.execute("DELETE FROM changes WHERE id=? AND status IN ('pending','rejected','failed')",
               (change_id,))
    return {"ok": True}


# --- standardization ------------------------------------------------------

@api.get("/api/standardize")
def standardize_summary():
    return transcode.library_summary()

@api.post("/api/standardize/stage")
def standardize_stage(payload: dict = Body(default={})):
    if worker.busy():
        raise HTTPException(409, "Harmon is busy right now")
    return {"staged": transcode.stage_conversions(payload.get("track_ids"))}


# --- tracks ---------------------------------------------------------------

@api.get("/api/tracks")
def track_list(q: str | None = None, limit: int = 100, offset: int = 0,
               problems: bool = False):
    sql = "SELECT * FROM tracks WHERE missing=0"
    args: tuple = ()
    if q:
        sql += (" AND (title LIKE ? OR artist LIKE ? OR album LIKE ? OR album_artist LIKE ?)")
        like = f"%{q}%"
        args += (like, like, like, like)
    if problems:
        sql += (" AND (artist IS NULL OR artist='' OR album IS NULL OR album='' "
                "OR genre IS NULL OR genre='' OR has_art=0)")
    sql += " ORDER BY album_artist, album, disc_no, track_no LIMIT ? OFFSET ?"
    rows = db.rows_to_dicts(db.query(sql, args + (limit, offset)))
    for r in rows:
        r["verdict"] = transcode.assess(r)
    return rows


@api.get("/api/tracks/{track_id}/art")
def track_art(track_id: int):
    row = db.one("SELECT path FROM tracks WHERE id=?", (track_id,))
    if not row:
        raise HTTPException(404, "Track not found")
    try:
        audio = mutagen.File(row["path"])
    except Exception:
        raise HTTPException(404, "No artwork")
    data = mime = None
    if getattr(audio, "pictures", None):
        data, mime = audio.pictures[0].data, audio.pictures[0].mime
    elif audio is not None and audio.tags:
        for key in audio.tags.keys():
            if str(key).upper().startswith("APIC"):
                frame = audio.tags[key]
                data, mime = frame.data, frame.mime
                break
        if data is None and "covr" in audio.tags:
            data, mime = bytes(audio.tags["covr"][0]), "image/jpeg"
    if not data:
        raise HTTPException(404, "No artwork")
    return Response(content=data, media_type=mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# --- web ------------------------------------------------------------------

@api.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


api.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
