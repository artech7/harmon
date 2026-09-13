"""Metadata lookups. Each provider returns the same shape so they can be chained.

A provider result looks like:
    {"artist":…, "album":…, "title":…, "track_no":…, "disc_no":…, "year":…,
     "genre":…, "mb_recording":…, "mb_release":…, "score": 0.0-1.0, "art_url":…}
Empty or unknown fields are simply left out, so a later provider can fill them.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from difflib import SequenceMatcher
from typing import Any

import httpx

from . import db
from .config import get as get_config

USER_AGENT = "Harmon/1.0 (self-hosted music library tool)"
CACHE_TTL_DAYS = 30

_mb_lock = threading.Lock()
_mb_last = 0.0
_spotify_token: dict[str, Any] = {"value": None, "expires": 0.0}


def _cached(key: str):
    row = db.one(
        "SELECT value FROM provider_cache WHERE key=? "
        "AND created_at > datetime('now', ?)",
        (key, f"-{CACHE_TTL_DAYS} days"),
    )
    return json.loads(row["value"]) if row else None


def _cache(key: str, value) -> None:
    db.execute(
        "INSERT INTO provider_cache(key, value, created_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, created_at=datetime('now')",
        (key, json.dumps(value)),
    )


def _similar(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, str(a).lower().strip(), str(b).lower().strip()).ratio()


def _get(url: str, **kwargs) -> Any:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url, headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()


# --- MusicBrainz ----------------------------------------------------------

def musicbrainz(track: dict) -> dict | None:
    """Canonical artist/album/title/track numbers. Free, but capped at 1 req/sec."""
    artist = track.get("album_artist") or track.get("artist") or ""
    title = track.get("title") or ""
    if not title:
        return None

    key = f"mb:{artist}|{title}|{int(track.get('duration') or 0)}"
    hit = _cached(key)
    if hit is not None:
        return hit or None

    query = f'recording:"{title}"'
    if artist:
        query += f' AND artist:"{artist}"'
    if track.get("album"):
        query += f' AND release:"{track["album"]}"'

    global _mb_last
    with _mb_lock:
        wait = 1.05 - (time.time() - _mb_last)
        if wait > 0:
            time.sleep(wait)
        _mb_last = time.time()
        try:
            data = _get(
                "https://musicbrainz.org/ws/2/recording",
                params={"query": query, "fmt": "json", "limit": 5},
            )
        except Exception as exc:
            db.log(f"MusicBrainz lookup failed: {exc}", "warn")
            return None

    best, best_score = None, 0.0
    for rec in data.get("recordings", []):
        credit = rec.get("artist-credit") or [{}]
        rec_artist = credit[0].get("name") if credit else None
        score = 0.55 * _similar(title, rec.get("title")) + 0.35 * _similar(artist, rec_artist)
        if track.get("duration") and rec.get("length"):
            delta = abs(track["duration"] - rec["length"] / 1000)
            score += 0.10 * max(0.0, 1 - delta / 10)
        if score > best_score:
            best, best_score = rec, score

    if not best or best_score < 0.5:
        _cache(key, {})
        return None

    credit = best.get("artist-credit") or [{}]
    release = (best.get("releases") or [{}])[0]
    media = (release.get("media") or [{}])[0]
    trk = (media.get("track") or [{}])[0]

    result = {
        "title": best.get("title"),
        "artist": credit[0].get("name") if credit else None,
        "album_artist": credit[0].get("name") if credit else None,
        "album": release.get("title"),
        "year": (release.get("date") or "")[:4] or None,
        "track_no": int(trk["number"]) if str(trk.get("number", "")).isdigit() else None,
        "disc_no": media.get("position"),
        "mb_recording": best.get("id"),
        "mb_release": release.get("id"),
        "score": round(min(best_score, 1.0), 3),
        "source": "musicbrainz",
    }
    result = {k: v for k, v in result.items() if v not in (None, "")}
    _cache(key, result)
    return result


def coverartarchive(release_mbid: str, min_px: int = 600) -> str | None:
    if not release_mbid:
        return None
    key = f"caa:{release_mbid}"
    hit = _cached(key)
    if hit is not None:
        return hit or None
    try:
        data = _get(f"https://coverartarchive.org/release/{release_mbid}")
    except Exception:
        _cache(key, "")
        return None
    for image in data.get("images", []):
        if image.get("front"):
            thumbs = image.get("thumbnails", {})
            url = thumbs.get("1200") or thumbs.get("large") or image.get("image")
            _cache(key, url)
            return url
    _cache(key, "")
    return None


# --- Discogs --------------------------------------------------------------

def discogs(track: dict) -> dict | None:
    """Good for genres, styles and release years. Needs a personal access token."""
    token = get_config()["providers"]["discogs_token"]
    if not token:
        return None
    artist = track.get("album_artist") or track.get("artist") or ""
    title = track.get("title") or ""
    key = f"dc:{artist}|{track.get('album')}|{title}"
    hit = _cached(key)
    if hit is not None:
        return hit or None

    params = {"type": "release", "per_page": 5, "token": token}
    if track.get("album"):
        params["release_title"] = track["album"]
    else:
        params["track"] = title
    if artist:
        params["artist"] = artist

    try:
        data = _get("https://api.discogs.com/database/search", params=params)
    except Exception as exc:
        db.log(f"Discogs lookup failed: {exc}", "warn")
        return None

    results = data.get("results") or []
    if not results:
        _cache(key, {})
        return None
    top = results[0]
    genres = (top.get("style") or []) + (top.get("genre") or [])
    result = {
        "album": (top.get("title") or "").split(" - ")[-1] or None,
        "year": str(top.get("year")) if top.get("year") else None,
        "genre": "; ".join(dict.fromkeys(genres[:3])) or None,
        "art_url": top.get("cover_image"),
        "score": 0.7,
        "source": "discogs",
    }
    result = {k: v for k, v in result.items() if v not in (None, "")}
    _cache(key, result)
    return result


# --- Last.fm --------------------------------------------------------------

def lastfm(track: dict) -> dict | None:
    """Crowd tags — the most human-readable genres of the four sources."""
    api_key = get_config()["providers"]["lastfm_key"]
    if not api_key:
        return None
    artist = track.get("artist") or track.get("album_artist")
    title = track.get("title")
    if not (artist and title):
        return None
    key = f"lf:{artist}|{title}"
    hit = _cached(key)
    if hit is not None:
        return hit or None
    try:
        data = _get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "track.getInfo", "api_key": api_key, "format": "json",
                "artist": artist, "track": title, "autocorrect": 1,
            },
        )
    except Exception as exc:
        db.log(f"Last.fm lookup failed: {exc}", "warn")
        return None

    info = data.get("track") or {}
    tags = [t["name"].title() for t in (info.get("toptags", {}).get("tag") or [])[:3]]
    result = {
        "title": info.get("name"),
        "artist": (info.get("artist") or {}).get("name"),
        "album": (info.get("album") or {}).get("title"),
        "genre": "; ".join(tags) or None,
        "score": 0.65,
        "source": "lastfm",
    }
    result = {k: v for k, v in result.items() if v not in (None, "")}
    _cache(key, result)
    return result


# --- Spotify --------------------------------------------------------------

def _spotify_auth() -> str | None:
    cfg = get_config()["providers"]
    cid, secret = cfg["spotify_client_id"], cfg["spotify_client_secret"]
    if not (cid and secret):
        return None
    if _spotify_token["value"] and _spotify_token["expires"] > time.time():
        return _spotify_token["value"]
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}"},
            )
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:
        db.log(f"Spotify sign-in failed: {exc}", "warn")
        return None
    _spotify_token["value"] = payload["access_token"]
    _spotify_token["expires"] = time.time() + payload.get("expires_in", 3600) - 60
    return _spotify_token["value"]


def spotify(track: dict) -> dict | None:
    """Artist-level genres and high-resolution album art."""
    token = _spotify_auth()
    if not token:
        return None
    artist = track.get("album_artist") or track.get("artist") or ""
    title = track.get("title") or ""
    key = f"sp:{artist}|{title}"
    hit = _cached(key)
    if hit is not None:
        return hit or None

    q = f"track:{title}" + (f" artist:{artist}" if artist else "")
    try:
        data = _get(
            "https://api.spotify.com/v1/search",
            params={"q": q, "type": "track", "limit": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:
        db.log(f"Spotify lookup failed: {exc}", "warn")
        return None

    items = (data.get("tracks") or {}).get("items") or []
    if not items:
        _cache(key, {})
        return None
    top = items[0]
    album = top.get("album") or {}
    images = sorted(album.get("images") or [], key=lambda i: -(i.get("width") or 0))

    genre = None
    artist_id = (top.get("artists") or [{}])[0].get("id")
    if artist_id:
        try:
            ainfo = _get(
                f"https://api.spotify.com/v1/artists/{artist_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            genre = "; ".join(g.title() for g in (ainfo.get("genres") or [])[:3]) or None
        except Exception:
            pass

    result = {
        "title": top.get("name"),
        "artist": ", ".join(a["name"] for a in top.get("artists", [])) or None,
        "album_artist": (album.get("artists") or [{}])[0].get("name"),
        "album": album.get("name"),
        "year": (album.get("release_date") or "")[:4] or None,
        "track_no": top.get("track_number"),
        "disc_no": top.get("disc_number"),
        "genre": genre,
        "art_url": images[0]["url"] if images else None,
        "score": 0.72,
        "source": "spotify",
    }
    result = {k: v for k, v in result.items() if v not in (None, "")}
    _cache(key, result)
    return result


LOOKUPS = {
    "musicbrainz": musicbrainz,
    "discogs": discogs,
    "lastfm": lastfm,
    "spotify": spotify,
}


def gather(track: dict) -> list[dict]:
    """Run every configured provider, in the order the user set, and keep the hits."""
    order = get_config()["providers"]["order"]
    out = []
    for name in order:
        fn = LOOKUPS.get(name)
        if not fn:
            continue
        try:
            result = fn(track)
        except Exception as exc:
            db.log(f"{name} raised: {exc}", "warn")
            result = None
        if result:
            out.append(result)
    return out


def find_art(track: dict, results: list[dict]) -> str | None:
    cfg = get_config()
    min_px = cfg["enrich"]["art_min_px"]
    for name in cfg["providers"]["art_order"]:
        if name == "coverartarchive":
            mbid = next((r.get("mb_release") for r in results if r.get("mb_release")), None) \
                or track.get("mb_release")
            url = coverartarchive(mbid, min_px) if mbid else None
        else:
            url = next((r.get("art_url") for r in results if r.get("source") == name), None)
        if url:
            return url
    return None
