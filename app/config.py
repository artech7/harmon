"""Harmon settings: defaults, persistence, validation."""
from __future__ import annotations

import copy
import os
from typing import Any

from . import db

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv", ".alac", ".m4b",
}

# Codecs Harmon can convert to, with the practical bitrate choices for each.
CODECS: dict[str, dict[str, Any]] = {
    "flac": {
        "label": "FLAC",
        "blurb": "Lossless. Identical to the source, roughly half the size of WAV.",
        "container": "flac",
        "lossless": True,
        "bitrates": [],
        "quality": ["0", "5", "8"],
        "quality_label": "Compression level (higher is smaller, slower)",
    },
    "alac": {
        "label": "ALAC",
        "blurb": "Apple's lossless format. Use this if Apple devices are your main player.",
        "container": "m4a",
        "lossless": True,
        "bitrates": [],
        "quality": [],
    },
    "aac": {
        "label": "AAC",
        "blurb": "Lossy, excellent quality per megabyte. The safest all-round choice.",
        "container": "m4a",
        "lossless": False,
        "bitrates": [128, 160, 192, 256, 320],
    },
    "mp3": {
        "label": "MP3",
        "blurb": "Lossy, plays on absolutely everything. Larger than AAC for the same quality.",
        "container": "mp3",
        "lossless": False,
        "bitrates": [128, 192, 256, 320],
    },
    "opus": {
        "label": "Opus",
        "blurb": "Lossy, the best quality at low bitrates. Older hardware may not play it.",
        "container": "opus",
        "lossless": False,
        "bitrates": [96, 128, 160, 192, 256],
    },
}

FFMPEG_ENCODER = {
    "flac": "flac",
    "alac": "alac",
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
}

# Metadata fields Harmon will correct, in the order they appear in the UI.
ENRICH_FIELDS = ["artist", "album_artist", "album", "title", "track_no", "disc_no", "year", "genre"]

DEFAULTS: dict[str, Any] = {
    "shell": {
        # Address of your Forge instance, so the rail can switch between the two
        # apps. Settable in the UI; this is just the starting value.
        "forge_url": os.environ.get("HARMON_FORGE_URL", ""),
    },
    "target": {
        "codec": "aac",
        "bitrate": 256,
        "quality": "5",
        "samplerate": 0,          # 0 = keep source
        "convert_lossless": False,  # leave FLAC/ALAC alone by default
        "skip_if_lower_bitrate": True,
        "keep_originals": True,
        "originals_path": "/originals",
    },
    "providers": {
        "order": ["musicbrainz", "discogs", "lastfm", "spotify"],
        "art_order": ["coverartarchive", "discogs", "spotify"],
        "discogs_token": "",
        "lastfm_key": "",
        "spotify_client_id": "",
        "spotify_client_secret": "",
        "contact_email": "",
    },
    "enrich": {
        "fields": {f: True for f in ENRICH_FIELDS},
        "embed_art": True,
        "art_min_px": 600,
        "min_confidence": 0.82,
        "overwrite_existing": False,  # only fill blanks unless confidence is high
    },
    "dupes": {
        "duration_tolerance": 3.0,
        "keeper_rule": "bitrate",   # bitrate | size | lossless | newest
        "cross_album_action": "keep_both",
    },
    "automation": {
        "auto_scan": True,
        "scan_interval_min": 30,
        "auto_enrich": True,
        "auto_standardize": True,
        "auto_approve_tags": False,
        "auto_approve_converts": False,
        "auto_approve_deletes": False,
        "schedule_enabled": False,
        "schedule_start": "01:00",
        "schedule_end": "07:00",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get() -> dict:
    return deep_merge(DEFAULTS, db.get_setting("config", {}) or {})


def save(patch: dict) -> dict:
    merged = deep_merge(get(), patch)
    db.set_setting("config", merged)
    return merged


def target_extension() -> str:
    cfg = get()["target"]
    return "." + CODECS[cfg["codec"]]["container"]
