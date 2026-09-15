"""Codec and bitrate standardization.

Files are converted to a temporary output, verified, and only then swapped in.
The source is moved to the originals folder rather than overwritten, so a bad
conversion is always a file move away from being undone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from . import db, scanner
from .config import CODECS, FFMPEG_ENCODER, get as get_config

BITRATE_TOLERANCE = 0.12  # a 256k target accepts anything from ~225k to ~287k


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
         "-show_streams", path],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError("ffprobe could not read the file")
    return json.loads(out.stdout)


def effective_bitrate() -> int:
    """The bitrate a conversion will actually use, given the current mode."""
    cfg = get_config()["target"]
    spec = CODECS[cfg["codec"]]
    if spec["lossless"]:
        return 0
    if cfg.get("bitrate_mode") == "codec_only":
        return int(spec.get("best_bitrate") or cfg["bitrate"])
    return int(cfg["bitrate"] or 0)


def target_signature() -> str:
    """Identifies the current target so a file is never converted to it twice."""
    cfg = get_config()["target"]
    spec = CODECS[cfg["codec"]]
    setting = cfg.get("quality") if spec["lossless"] else effective_bitrate()
    mode = cfg.get("bitrate_mode", "fixed")
    return f"{cfg['codec']}:{mode}:{setting}:{cfg.get('samplerate') or 'src'}"


def assess(track: dict) -> dict:
    """Decide whether a track already matches the target format, and say why."""
    cfg = get_config()["target"]
    target_codec = cfg["codec"]
    target_bitrate = effective_bitrate()
    codec_only = cfg.get("bitrate_mode") == "codec_only"
    spec = CODECS[target_codec]
    codec = (track.get("codec") or "").lower()
    bitrate = int(track.get("bitrate") or 0)

    # Already put through the pipeline at these exact settings — leave it be.
    # Encoders rarely land on the nominal bitrate exactly, and without this a
    # 256k target would re-encode its own 249k output on every pass.
    if track.get("std_signature") and track["std_signature"] == target_signature():
        return {"action": "keep", "state": "match",
                "reason": f"Converted by Harmon to {spec['label']} already"}

    if track.get("lossless") and not spec["lossless"] and not cfg["convert_lossless"]:
        return {"action": "keep", "state": "protected",
                "reason": f"Lossless {codec.upper()} — protected by your 'leave lossless alone' setting"}

    if codec == target_codec:
        if spec["lossless"]:
            return {"action": "keep", "state": "match", "reason": f"Already {spec['label']}"}
        # Codec-only mode: the format is right, so there is nothing to do.
        # Bitrate never triggers a conversion here, whatever it happens to be.
        if codec_only:
            return {"action": "keep", "state": "match",
                    "reason": f"Already {spec['label']} at {bitrate or '?'} kbps"}
        low = target_bitrate * (1 - BITRATE_TOLERANCE)
        high = target_bitrate * (1 + BITRATE_TOLERANCE)
        if low <= bitrate <= high:
            return {"action": "keep", "state": "match",
                    "reason": f"Already {spec['label']} at {bitrate} kbps"}
        if bitrate < low and cfg["skip_if_lower_bitrate"]:
            return {"action": "keep", "state": "protected",
                    "reason": f"{bitrate} kbps is below your target — re-encoding would not add quality"}
        return {"action": "convert", "state": "convert",
                "reason": f"{spec['label']} {bitrate} kbps → {target_bitrate} kbps"}

    if spec["lossless"] and not track.get("lossless"):
        return {"action": "keep", "state": "protected",
                "reason": f"{codec.upper()} is lossy — converting to {spec['label']} would only grow the file"}

    return {"action": "convert", "state": "convert",
            "reason": f"{codec.upper() or 'Unknown'} → {spec['label']}"
                      + (f" {target_bitrate} kbps" if not spec["lossless"] else "")}


def build_command(src: str, dst: str) -> list[str]:
    cfg = get_config()["target"]
    codec = cfg["codec"]
    spec = CODECS[codec]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-map", "0:a:0", "-map_metadata", "0", "-vn",
           "-c:a", FFMPEG_ENCODER[codec]]

    if codec == "flac":
        cmd += ["-compression_level", str(cfg.get("quality") or 5)]
    elif not spec["lossless"]:
        cmd += ["-b:a", f"{effective_bitrate()}k"]
    if cfg.get("samplerate"):
        cmd += ["-ar", str(cfg["samplerate"])]
    if codec in ("aac", "alac"):
        cmd += ["-movflags", "+faststart"]

    cmd.append(dst)
    return cmd


def convert(track_id: int, progress=None) -> dict:
    """Convert one track in place. Returns sizes so the UI can show what was saved."""
    track = db.one("SELECT * FROM tracks WHERE id=?", (track_id,))
    if not track:
        raise ValueError("track not found")
    src = track["path"]
    if not os.path.exists(src):
        raise FileNotFoundError(src)

    cfg = get_config()["target"]
    spec = CODECS[cfg["codec"]]
    ext = "." + spec["container"]
    in_bytes = os.path.getsize(src)

    tmp_dir = tempfile.mkdtemp(prefix="harmon-")
    tmp_out = os.path.join(tmp_dir, os.path.splitext(os.path.basename(src))[0] + ext)
    try:
        if progress:
            progress(0.05, f"Converting to {spec['label']}")
        proc = subprocess.run(build_command(src, tmp_out), capture_output=True, text=True,
                              timeout=3600)
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            raise RuntimeError((proc.stderr or "ffmpeg failed").strip()[:400])

        if progress:
            progress(0.75, "Checking the converted file")
        info = probe(tmp_out)
        out_duration = float(info.get("format", {}).get("duration") or 0)
        if track["duration"] and abs(out_duration - track["duration"]) > 2.0:
            raise RuntimeError(
                f"Converted file is {out_duration:.0f}s but the source is {track['duration']:.0f}s"
            )
        out_bytes = os.path.getsize(tmp_out)
        if out_bytes < 4096:
            raise RuntimeError("Converted file is empty")

        final = os.path.splitext(src)[0] + ext
        if cfg["keep_originals"]:
            rel = os.path.basename(os.path.dirname(src))
            dest_dir = os.path.join(cfg["originals_path"], rel)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            n = 1
            base, oext = os.path.splitext(dest)
            while os.path.exists(dest):
                dest = f"{base} ({n}){oext}"
                n += 1
            shutil.move(src, dest)
        else:
            os.remove(src)

        if progress:
            progress(0.9, "Putting the new file in place")
        shutil.move(tmp_out, final)

        # Move this row onto the new path first — reindexing before the update
        # would insert a second row and collide with the unique path index.
        db.execute("DELETE FROM tracks WHERE path=? AND id<>?", (final, track_id))
        db.execute(
            "UPDATE tracks SET path=?, standardized=1, std_signature=? WHERE id=?",
            (final, target_signature(), track_id),
        )
        scanner.index_file(final, track["library_id"], force=True)

        saved = in_bytes - out_bytes
        db.log(
            f"Converted {os.path.basename(final)} to {spec['label']} "
            f"({saved / 1048576:+.1f} MB)"
        )
        return {"in_bytes": in_bytes, "out_bytes": out_bytes, "saved": saved, "path": final}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def reconcile_pending() -> int:
    """Drop staged conversions that the current target no longer asks for.

    Changes are staged against the settings in force at the time. Switch from
    a fixed bitrate to codec-only and yesterday's queue is still full of files
    that now need nothing done to them. Nothing else re-reads that queue, so
    it has to be re-read here.
    """
    rows = db.query(
        "SELECT c.id, c.track_id FROM changes c WHERE c.kind='convert' AND c.status='pending'"
    )
    dropped = 0
    for row in rows:
        track = db.one("SELECT * FROM tracks WHERE id=?", (row["track_id"],))
        if not track or track["missing"] or assess(dict(track))["action"] != "convert":
            db.execute("DELETE FROM changes WHERE id=?", (row["id"],))
            dropped += 1
    if dropped:
        db.log(f"Dropped {dropped} queued conversions that your current format "
               f"settings no longer ask for")
    return dropped


def stage_conversions(track_ids: list[int] | None = None) -> int:
    """Queue every track that does not match the target format, pending approval."""
    if track_ids:
        marks = ",".join("?" for _ in track_ids)
        rows = db.query(f"SELECT * FROM tracks WHERE missing=0 AND id IN ({marks})",
                        tuple(track_ids))
    else:
        rows = db.query("SELECT * FROM tracks WHERE missing=0")

    reconcile_pending()

    doomed = {
        r["track_id"] for r in db.query(
            "SELECT track_id FROM changes WHERE kind='delete' "
            "AND status IN ('pending','approved')"
        )
    }

    staged = 0
    for row in rows:
        track = dict(row)
        if track["id"] in doomed:
            continue  # this copy is on its way out; converting it would be wasted work
        verdict = assess(track)
        if verdict["action"] != "convert":
            continue
        exists = db.one(
            "SELECT id FROM changes WHERE track_id=? AND kind='convert' "
            "AND status IN ('pending','approved')", (track["id"],),
        )
        if exists:
            continue
        db.execute(
            "INSERT INTO changes(kind, track_id, field, old_value, new_value, source, confidence) "
            "VALUES('convert',?,'format',?,?,'standardization',1.0)",
            (track["id"], f"{(track['codec'] or '?').upper()} {track['bitrate'] or '?'} kbps",
             verdict["reason"]),
        )
        staged += 1
    if staged:
        db.log(f"Standardization check queued {staged} files for conversion")
    return staged


def library_summary() -> dict:
    """What the library looks like against the target, for the Standardize screen."""
    reconcile_pending()
    rows = db.query("SELECT * FROM tracks WHERE missing=0")
    summary = {"matching": 0, "needs_convert": 0, "protected": 0, "by_codec": {}, "reasons": {}}
    bucket = {"match": "matching", "convert": "needs_convert", "protected": "protected"}
    for row in rows:
        track = dict(row)
        codec = (track.get("codec") or "unknown").upper()
        summary["by_codec"][codec] = summary["by_codec"].get(codec, 0) + 1
        verdict = assess(track)
        summary[bucket[verdict["state"]]] += 1
        summary["reasons"][verdict["reason"]] = summary["reasons"].get(verdict["reason"], 0) + 1
    return summary
