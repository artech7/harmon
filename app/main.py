"""Harmon — HTTP API and web UI host."""
from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager

import mutagen
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import albums, browse, changes, config, db, dupes, enrich, genrebook, genres, hygiene, netcheck, providers, scanner, transcode, worker

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
    order = config.get()["providers"]["order"]
    return {
        "shell": config.get()["shell"],
        "sources": {
            "benched": [n for n in order if providers.is_benched(n)],
            "mb_backoff": providers.mb_backoff_seconds(),
        },
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


@api.get("/api/albums/plan")
def albums_plan():
    """What the next metadata pass will cost, before committing to it."""
    pending = albums.groups()
    loose = len(albums.loose_track_ids(100000))
    album_tracks = sum(g["unchecked"] for g in pending)
    batched = len(pending) * 2 + loose
    per_track = album_tracks + loose
    return {
        "albums": len(pending),
        "album_tracks": album_tracks,
        "loose_tracks": loose,
        "requests_batched": batched,
        "requests_per_track": per_track,
        "seconds_batched": batched,          # the public server allows one a second
        "seconds_per_track": per_track,
        "fingerprinting": bool(config.get()["providers"].get("acoustid_key"))
                          and providers.fpcalc_available(),
    }


@api.get("/api/hygiene/preview")
def hygiene_preview(allow_comma: bool = False, limit: int = 300):
    return hygiene.preview(limit, allow_comma)


@api.post("/api/hygiene/stage")
def hygiene_stage(payload: dict = Body(default={})):
    if worker.busy():
        raise HTTPException(409, "Harmon is busy right now")
    return {"staged": hygiene.stage(
        bool(payload.get("allow_comma")),
        float(payload.get("min_confidence") or 0),
    )}


@api.get("/api/netcheck")
def netcheck_run():
    return netcheck.run()


@api.post("/api/providers/{name}/test")
def test_provider(name: str):
    providers.revive_all()   # a deliberate retry should never hit an old strike
    return providers.check(name)


@api.get("/api/providers/state")
def provider_state():
    """Which sources are currently sitting out, and why."""
    order = config.get()["providers"]["order"]
    return {name: {"benched": providers.is_benched(name)} for name in order}


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


@api.get("/api/genres")
def genres_overview():
    return genrebook.distribution()


@api.post("/api/genres/custom")
def genres_add_custom(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "A name is required")
    return genrebook.add_custom(name)


@api.delete("/api/genres/custom/{name}")
def genres_remove_custom(name: str):
    return genrebook.remove_custom(name)


@api.post("/api/genres/map")
def genres_map(payload: dict = Body(...)):
    source = (payload.get("from") or "").strip()
    target = (payload.get("to") or "").strip()
    if not source:
        raise HTTPException(400, "Nothing to map")
    if not target:
        return genrebook.remove_mapping(source)
    if target not in genrebook.vocabulary():
        raise HTTPException(400, f"{target!r} is not one of your genres")
    return genrebook.add_mapping(source, target)


@api.post("/api/genres/cleanup")
def genres_cleanup():
    if worker.busy():
        raise HTTPException(409, "Harmon is busy right now")
    return genrebook.stage_cleanup()


@api.post("/api/genres/reset")
def genres_reset():
    """Forget every cached genre and drop the staged genre changes.

    Worth doing after the matching rules change, since anything decided under
    the old ones is still sitting in the queue.
    """
    cached = db.one("SELECT COUNT(*) AS n FROM provider_cache WHERE key LIKE 'genre:%'")["n"]
    db.execute("DELETE FROM provider_cache WHERE key LIKE 'genre:%'")
    staged = db.one("SELECT COUNT(*) AS n FROM changes "
                    "WHERE field='genre' AND status='pending'")["n"]
    db.execute("DELETE FROM changes WHERE field='genre' AND status='pending'")
    db.execute("UPDATE tracks SET enriched_at=NULL WHERE id IN "
               "(SELECT track_id FROM changes WHERE field='genre')")
    db.log(f"Cleared {cached} cached artist genres and {staged} staged genre changes")
    return {"cached_cleared": cached, "staged_cleared": staged}


@api.get("/api/browse")
def browse_folder(path: str | None = None):
    if not path:
        return {"roots": browse.roots()}
    try:
        return browse.listing(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@api.get("/api/browse/for-track/{track_id}")
def browse_for_track(track_id: int):
    folder = browse.folder_for_track(track_id)
    if not folder:
        raise HTTPException(404, "Track not found")
    return browse.listing(folder)


@api.get("/api/changes/group-items")
def group_items(kind: str, band: str, field: str | None = None, source: str | None = None,
                status: str = "pending", limit: int = 200, offset: int = 0):
    """Every change in one group, not just the samples."""
    bands = {"high": (0.9, 1.01), "good": (0.75, 0.9), "low": (-0.01, 0.75)}
    lo, hi = bands.get(band, (-0.01, 1.01))
    rows = db.query(
        "SELECT c.id, c.old_value, c.new_value, c.confidence, "
        "       t.id AS track_id, t.title, t.artist, t.album, t.path, t.folder "
        "FROM changes c JOIN tracks t ON t.id = c.track_id "
        "WHERE c.status=? AND c.kind=? AND c.source IS ? "
        "  AND (c.field IS ? OR (c.field IS NULL AND ? IS NULL)) "
        "  AND c.confidence >= ? AND c.confidence < ? "
        "ORDER BY t.artist, t.album, t.title LIMIT ? OFFSET ?",
        (status, kind, source, field, field, lo, hi, limit, offset),
    )
    total = db.one(
        "SELECT COUNT(*) AS n FROM changes WHERE status=? AND kind=? AND source IS ? "
        "AND (field IS ? OR (field IS NULL AND ? IS NULL)) "
        "AND confidence >= ? AND confidence < ?",
        (status, kind, source, field, field, lo, hi),
    )["n"]
    return {"total": total, "offset": offset, "items": db.rows_to_dicts(rows)}


@api.get("/api/changes/grouped")
def changes_grouped(status: str = "pending"):
    return {"counts": changes.counts(), "groups": changes.grouped(status)}


@api.post("/api/changes/decide-group")
def decide_group(payload: dict = Body(...)):
    status = payload.get("status")
    from_status = payload.get("from_status", "pending")
    if status not in ("approved", "rejected", "pending"):
        raise HTTPException(400, "status must be 'approved', 'rejected' or 'pending'")
    if from_status not in ("pending", "approved", "rejected"):
        raise HTTPException(400, "from_status must be a reversible status")
    n = changes.decide_group(
        payload["kind"], payload.get("field"), payload.get("source"),
        payload.get("band", "all"), status, from_status,
    )
    return {"updated": n, "counts": changes.counts()}


@api.post("/api/worker/stop")
def worker_stop():
    if not worker.request_stop():
        raise HTTPException(409, "Nothing is running")
    return {"stopping": True, "job": worker.status()}


@api.post("/api/changes/decide")
def decide(payload: dict = Body(...)):
    ids = payload.get("ids") or []
    status = payload.get("status")
    if status not in ("approved", "rejected", "pending"):
        raise HTTPException(400, "status must be 'approved', 'rejected' or 'pending'")
    return {"updated": changes.set_status(ids, status), "counts": changes.counts()}


@api.post("/api/changes/decide-all")
def decide_all(payload: dict = Body(default={})):
    kind = payload.get("kind")
    status = payload.get("status", "approved")
    min_conf = float(payload.get("min_confidence") or 0)
    from_status = payload.get("from_status", "pending")
    sql = "UPDATE changes SET status=? WHERE status=? AND confidence >= ?"
    args: tuple = (status, from_status, min_conf)
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

def _asset_version() -> str:
    """Changes whenever the front end does, so the browser cannot serve a
    stale app.js alongside a fresh index.html — the mismatch that produces
    errors on every poll."""
    newest = 0.0
    for name in ("app.js", "app.css", "index.html"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(WEB_DIR, name)))
        except OSError:
            pass
    return str(int(newest))


@api.get("/")
def index():
    with open(os.path.join(WEB_DIR, "index.html")) as f:
        html = f.read()
    version = _asset_version()
    html = (html.replace('href="/app.css"', f'href="/app.css?v={version}"')
                .replace('src="/app.js"', f'src="/app.js?v={version}"'))
    return Response(
        content=html, media_type="text/html",
        # The shell itself must never be cached, or the versioned asset links
        # inside it never reach the browser.
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


api.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
