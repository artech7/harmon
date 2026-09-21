"""SQLite storage for Harmon."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get("HARMON_DB", "/config/harmon.db")

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS libraries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL UNIQUE,
    watch      INTEGER NOT NULL DEFAULT 1,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id   INTEGER,
    path         TEXT NOT NULL UNIQUE,
    size         INTEGER,
    mtime        REAL,
    content_hash TEXT,
    duration     REAL,
    bitrate      INTEGER,
    codec        TEXT,
    samplerate   INTEGER,
    channels     INTEGER,
    lossless     INTEGER DEFAULT 0,
    title        TEXT,
    artist       TEXT,
    album_artist TEXT,
    album        TEXT,
    track_no     INTEGER,
    disc_no      INTEGER,
    year         TEXT,
    genre        TEXT,
    has_art      INTEGER DEFAULT 0,
    mb_recording TEXT,
    mb_release   TEXT,
    norm_key     TEXT,
    album_key    TEXT,
    enriched_at  TEXT,
    standardized INTEGER DEFAULT 0,
    std_signature TEXT,
    folder       TEXT,
    lyrics       TEXT,
    artist_multi INTEGER DEFAULT 0,
    missing      INTEGER DEFAULT 0,
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_norm  ON tracks(norm_key);
CREATE INDEX IF NOT EXISTS idx_tracks_hash  ON tracks(content_hash);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_key);

CREATE TABLE IF NOT EXISTS dupe_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- 'identical' | 'same_album' | 'cross_album'
    key        TEXT NOT NULL,
    title      TEXT,
    artist     TEXT,
    reviewed   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dupe_key ON dupe_groups(kind, key);

CREATE TABLE IF NOT EXISTS dupe_members (
    group_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    keeper   INTEGER NOT NULL DEFAULT 0,
    reason   TEXT,
    PRIMARY KEY (group_id, track_id)
);

CREATE TABLE IF NOT EXISTS changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch      TEXT,
    kind       TEXT NOT NULL,          -- 'tag' | 'art' | 'delete' | 'convert'
    track_id   INTEGER NOT NULL,
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT,
    payload    TEXT,
    source     TEXT,
    confidence REAL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'pending',
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_status ON changes(status);
CREATE INDEX IF NOT EXISTS idx_changes_track  ON changes(track_id);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- 'scan' | 'enrich' | 'convert' | 'apply'
    track_id   INTEGER,
    state      TEXT NOT NULL DEFAULT 'queued',
    progress   REAL DEFAULT 0,
    message    TEXT,
    in_bytes   INTEGER,
    out_bytes  INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provider_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    # Lightweight forward migrations for stores created by an earlier version.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
    for column, ddl in (("std_signature", "TEXT"), ("folder", "TEXT"),
                        ("lyrics", "TEXT"), ("artist_multi", "INTEGER DEFAULT 0")):
        if column not in have:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {ddl}")
            if column == "folder":
                # Backfill from paths already indexed, so browsing works without
                # forcing a rescan. os.path.dirname beats doing this in SQL.
                rows = conn.execute("SELECT id, path FROM tracks").fetchall()
                conn.executemany(
                    "UPDATE tracks SET folder=? WHERE id=?",
                    [(os.path.dirname(r["path"]), r["id"]) for r in rows],
                )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_folder ON tracks(folder)")
    conn.commit()


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, args).fetchall()


def one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, args).fetchone()


def execute(sql: str, args: tuple = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, args)
        return cur.lastrowid


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# --- settings -------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def log(message: str, level: str = "info") -> None:
    execute("INSERT INTO activity(level, message) VALUES(?,?)", (level, message))
