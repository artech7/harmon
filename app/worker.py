"""Background work: the job queue, the automation pipeline and the library watcher."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, time as dtime

from . import albums, changes, db, dupes, enrich, scanner, transcode
from .config import get as get_config

_pipeline_lock = threading.Lock()
_stop = threading.Event()
_current: dict = {"kind": None, "progress": 0.0, "message": "Idle", "job_id": None}


def status() -> dict:
    return dict(_current)


def busy() -> bool:
    return _current["kind"] is not None


def _start_job(kind: str, track_id: int | None = None) -> int:
    job_id = db.execute(
        "INSERT INTO jobs(kind, track_id, state, message) VALUES(?,?,'running','Starting')",
        (kind, track_id),
    )
    _current.update({"kind": kind, "progress": 0.0, "message": "Starting", "job_id": job_id})
    return job_id


def _update(job_id: int, progress: float, message: str) -> None:
    _current.update({"progress": round(progress, 3), "message": message})
    db.execute(
        "UPDATE jobs SET progress=?, message=?, updated_at=datetime('now') WHERE id=?",
        (round(progress, 3), message, job_id),
    )


def _finish(job_id: int, state: str, message: str, **extra) -> None:
    db.execute(
        "UPDATE jobs SET state=?, message=?, progress=1, in_bytes=?, out_bytes=?, "
        "updated_at=datetime('now') WHERE id=?",
        (state, message, extra.get("in_bytes"), extra.get("out_bytes"), job_id),
    )
    _current.update({"kind": None, "progress": 0.0, "message": "Idle", "job_id": None})


def in_schedule_window() -> bool:
    cfg = get_config()["automation"]
    if not cfg["schedule_enabled"]:
        return True
    now = datetime.now().time()
    start = dtime.fromisoformat(cfg["schedule_start"])
    end = dtime.fromisoformat(cfg["schedule_end"])
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


# --- individual steps -----------------------------------------------------

def run_scan(force: bool = False) -> dict:
    with _pipeline_lock:
        job = _start_job("scan")
        try:
            result = scanner.scan(force, lambda p, m: _update(job, p * 0.7, m))
            _update(job, 0.75, "Looking for duplicates")
            result["dupes"] = dupes.find()
            _finish(job, "done", f"{result['added']} new, {result['updated']} changed")
            return result
        except Exception as exc:
            _finish(job, "failed", str(exc)[:300])
            raise


def run_dupes() -> dict:
    with _pipeline_lock:
        job = _start_job("dupes")
        try:
            result = dupes.find()
            _finish(job, "done", f"{result['same_album']} duplicate sets found")
            return result
        except Exception as exc:
            _finish(job, "failed", str(exc)[:300])
            raise


def run_enrich(track_ids: list[int] | None = None) -> dict:
    """Albums first, then whatever is left over one at a time.

    An album lookup costs two requests and returns a whole tracklist, so doing
    it this way round turns hours into minutes on a library that is organised
    into albums. Only the strays pay the per-track price.
    """
    with _pipeline_lock:
        job = _start_job("enrich")
        try:
            if track_ids is not None:
                result = enrich.propose_many(track_ids, lambda p, m: _update(job, p, m))
                _finish(job, "done", f"{result['staged']} suggestions ready to review")
                return result

            _update(job, 0.02, "Looking up albums")
            album_result = albums.run(progress=lambda p, m: _update(job, p * 0.8, m))

            leftovers = albums.loose_track_ids() + enrich.pending_track_ids()
            leftovers = list(dict.fromkeys(leftovers))
            track_result = {"staged": 0, "tracks": 0}
            if leftovers:
                _update(job, 0.82, f"{len(leftovers)} tracks need checking one by one")
                track_result = enrich.propose_many(
                    leftovers, lambda p, m: _update(job, 0.82 + p * 0.18, m))

            staged = album_result["staged"] + track_result["staged"]
            _finish(job, "done",
                    f"{staged} suggestions ready to review "
                    f"({album_result['albums']} albums, {len(leftovers)} single tracks)")
            return {"albums": album_result, "tracks": track_result, "staged": staged}
        except Exception as exc:
            _finish(job, "failed", str(exc)[:300])
            raise


def run_apply() -> dict:
    with _pipeline_lock:
        job = _start_job("apply")
        try:
            result = changes.apply_approved(lambda p, m: _update(job, p, m))
            _finish(job, "done", f"{result['applied']} changes written")
            return result
        except Exception as exc:
            _finish(job, "failed", str(exc)[:300])
            raise


def run_conversions(limit: int = 500) -> dict:
    """Work through approved conversions one file at a time."""
    with _pipeline_lock:
        rows = db.query(
            "SELECT c.id, c.track_id FROM changes c WHERE c.kind='convert' "
            "AND c.status='approved' ORDER BY c.id LIMIT ?", (limit,),
        )
        if not rows:
            return {"converted": 0, "failed": 0, "saved": 0}
        job = _start_job("convert")
        done = failed = saved = 0
        total = len(rows)
        try:
            for i, row in enumerate(rows):
                if _stop.is_set() or not in_schedule_window():
                    break
                base = i / total
                track = db.one("SELECT missing, path FROM tracks WHERE id=?", (row["track_id"],))
                if not track or track["missing"] or not os.path.exists(track["path"]):
                    db.execute(
                        "UPDATE changes SET status='rejected', error='File is no longer there' "
                        "WHERE id=?", (row["id"],),
                    )
                    continue
                try:
                    result = transcode.convert(
                        row["track_id"],
                        lambda p, m: _update(job, base + p / total, f"{m} ({i + 1}/{total})"),
                    )
                    saved += result["saved"]
                    db.execute(
                        "UPDATE changes SET status='applied', applied_at=datetime('now') WHERE id=?",
                        (row["id"],),
                    )
                    done += 1
                except Exception as exc:
                    db.execute("UPDATE changes SET status='failed', error=? WHERE id=?",
                               (str(exc)[:400], row["id"]))
                    failed += 1
            _finish(job, "done", f"{done} converted, {saved / 1048576:.0f} MB saved")
            return {"converted": done, "failed": failed, "saved": saved}
        except Exception as exc:
            _finish(job, "failed", str(exc)[:300])
            raise


# --- the full automated pass ---------------------------------------------

def run_pipeline() -> dict:
    """Scan, dedupe, enrich, stage conversions, then apply whatever is auto-approved."""
    cfg = get_config()["automation"]
    summary: dict = {}
    summary["scan"] = run_scan()

    if cfg["auto_enrich"]:
        summary["enrich"] = run_enrich()
    if cfg["auto_standardize"]:
        summary["staged_conversions"] = transcode.stage_conversions()

    approved = 0
    if cfg["auto_approve_tags"]:
        min_conf = get_config()["enrich"]["min_confidence"]
        db.execute(
            "UPDATE changes SET status='approved' WHERE status='pending' "
            "AND kind IN ('tag','art') AND confidence >= ?", (min_conf,),
        )
        approved += 1
    if cfg["auto_approve_deletes"]:
        db.execute("UPDATE changes SET status='approved' WHERE status='pending' AND kind='delete'")
    if cfg["auto_approve_converts"]:
        db.execute("UPDATE changes SET status='approved' WHERE status='pending' AND kind='convert'")

    pending_writes = db.one(
        "SELECT COUNT(*) AS n FROM changes WHERE status='approved' AND kind<>'convert'"
    )["n"]
    if pending_writes:
        summary["apply"] = run_apply()
    if in_schedule_window():
        summary["convert"] = run_conversions()
    return summary


# --- watcher --------------------------------------------------------------

def _watch_loop() -> None:
    """Re-scans on a timer. Polling beats filesystem events on NAS shares."""
    time.sleep(10)
    while not _stop.is_set():
        try:
            cfg = get_config()["automation"]
            interval = max(int(cfg["scan_interval_min"]), 1) * 60
            if cfg["auto_scan"] and not busy():
                run_pipeline()
        except Exception as exc:
            db.log(f"Automatic pass failed: {exc}", "error")
            interval = 300
        _stop.wait(interval)


_thread: threading.Thread | None = None


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_watch_loop, name="harmon-watcher", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def spawn(fn, *args, **kwargs) -> None:
    """Run a one-off task without blocking the request."""
    threading.Thread(target=lambda: _safe(fn, *args, **kwargs), daemon=True).start()


def _safe(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        db.log(f"{getattr(fn, '__name__', 'task')} failed: {exc}", "error")
