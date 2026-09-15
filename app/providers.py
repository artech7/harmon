"""Metadata lookups. Each provider returns the same shape so they can be chained.

A provider result looks like:
    {"artist":…, "album":…, "title":…, "track_no":…, "disc_no":…, "year":…,
     "genre":…, "mb_recording":…, "mb_release":…, "score": 0.0-1.0, "art_url":…}
Empty or unknown fields are simply left out, so a later provider can fill them.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import time
from difflib import SequenceMatcher
from typing import Any

import httpx

from . import db
from .config import get as get_config

_BASE_AGENT = "Harmon/1.0 (+https://github.com/artech7/harmon)"


def user_agent() -> str:
    """MusicBrainz throttles clients that do not identify themselves with a way
    to be contacted, so fold the address from settings into the header."""
    try:
        email = (get_config()["providers"].get("contact_email") or "").strip()
    except Exception:
        email = ""
    return f"Harmon/1.0 ( {email} )" if email else _BASE_AGENT
CACHE_TTL_DAYS = 30

_mb_lock = threading.Lock()
_mb_last = 0.0
_mb_backoff = 0.0        # extra seconds per request, grown when throttled

# A provider that fails repeatedly is not going to start working on the next
# track. Three strikes and it sits out the rest of the run: one line in the log
# instead of one per track, and no wasted request every second.
_strikes: dict[str, int] = {}
_benched: dict[str, float] = {}
STRIKES_BEFORE_BENCH = 3
STRIKES_BEFORE_BENCH_RATE_LIMIT = 8
BENCH_SECONDS = 1800


def mb_backoff_seconds() -> float:
    """Current extra delay per MusicBrainz request, for the UI to report."""
    return round(_mb_backoff, 2)


class RateLimited(RuntimeError):
    """Throttled rather than broken: expected, and self-correcting given time."""


def note_failure(name: str, detail: str, limit: int | None = None) -> None:
    """Count a strike. Three in a row and the source sits out for a while.

    Rate limiting gets a longer rope than a real error, because the backoff is
    already handling it and a brief 503 blip should not bench a working source.
    But it is not infinite rope: once requests are a full half-minute apart the
    pass has stopped being useful, and waiting is better than crawling.
    """
    limit = limit or STRIKES_BEFORE_BENCH
    _strikes[name] = _strikes.get(name, 0) + 1
    n = _strikes[name]

    if n == limit:
        _benched[name] = time.time() + BENCH_SECONDS
        db.log(f"{name} has failed {n} times in a row ({detail}). "
               f"Leaving it alone for {BENCH_SECONDS // 60} minutes.", "warn")
    elif n < limit:
        db.log(f"{name} lookup failed: {detail}", "warn")


# Kept for callers that predate the rename.
_note_failure = note_failure


def note_success(name: str) -> None:
    _strikes.pop(name, None)
    _benched.pop(name, None)


_note_success = note_success


def is_benched(name: str) -> bool:
    until = _benched.get(name)
    if not until:
        return False
    if time.time() > until:
        _benched.pop(name, None)
        _strikes.pop(name, None)
        return False
    return True


def revive_all() -> None:
    """Clear every strike, so a manual retry is never blocked by an old run."""
    _strikes.clear()
    _benched.clear()
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
    headers.setdefault("User-Agent", user_agent())
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url, headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()


# --- MusicBrainz ----------------------------------------------------------

def mb_base() -> str:
    mirror = (get_config()["providers"].get("musicbrainz_url") or "").rstrip("/")
    return mirror or "https://musicbrainz.org"


def mb_request(path: str, params: dict) -> dict:
    """Every MusicBrainz call goes through here, so the rate limit is honoured
    once rather than per call site. A mirror is your own hardware answering
    your own queries, so it skips the wait entirely."""
    base = mb_base()
    if base != "https://musicbrainz.org":
        return _get(base + path, params=params)

    global _mb_last, _mb_backoff
    with _mb_lock:
        wait = (1.05 + _mb_backoff) - (time.time() - _mb_last)
        if wait > 0:
            time.sleep(wait)
        _mb_last = time.time()
        try:
            data = _get(base + path, params=params)
            # Decay multiplicatively, mirroring the way it grew. Subtracting a
            # flat 0.5s meant a backoff at the 30s cap needed sixty clean
            # requests to recover, so in practice it never did.
            _mb_backoff = 0.0 if _mb_backoff < 0.3 else _mb_backoff * 0.7
            return data
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (429, 503):
                _mb_backoff = min(_mb_backoff * 2 + 1, 30.0)
                _mb_last = time.time()
                raise RateLimited(
                    f"rate limited, slowing to one request every "
                    f"{1.05 + _mb_backoff:.0f}s") from exc
            raise


def mb_find_release(artist: str, album: str, track_count: int = 0) -> dict | None:
    """Find the release that best matches an album we have on disk."""
    key = f"mbrel:{artist}|{album}|{track_count}"
    hit = _cached(key)
    if hit is not None:
        return hit or None

    query = f'release:"{album}"'
    if artist:
        query += f' AND artist:"{artist}"'
    if track_count:
        query += f" AND tracks:{track_count}"

    data = mb_request("/ws/2/release", {"query": query, "fmt": "json", "limit": 5})
    releases = data.get("releases") or []
    if not releases:
        # Track count is a strong hint but a wrong one when the local copy is
        # partial. Drop it and try again before giving up on the album.
        if track_count:
            data = mb_request("/ws/2/release", {
                "query": f'release:"{album}"' + (f' AND artist:"{artist}"' if artist else ""),
                "fmt": "json", "limit": 5,
            })
            releases = data.get("releases") or []
        if not releases:
            _cache(key, {})
            return None

    best, best_score = None, 0.0
    for rel in releases:
        credit = (rel.get("artist-credit") or [{}])[0].get("name")
        score = 0.6 * _similar(album, rel.get("title")) + 0.4 * _similar(artist, credit)
        if track_count:
            counts = [m.get("track-count") or 0 for m in (rel.get("media") or [])]
            if sum(counts) == track_count:
                score += 0.15
        if score > best_score:
            best, best_score = rel, score

    if not best or best_score < 0.55:
        _cache(key, {})
        return None
    result = {"id": best["id"], "score": round(min(best_score, 1.0), 3)}
    _cache(key, result)
    return result


def mb_release_tracks(release_mbid: str) -> dict | None:
    """The whole tracklist in one request — the reason batching is worth doing."""
    key = f"mbtracks:{release_mbid}"
    hit = _cached(key)
    if hit is not None:
        return hit or None

    data = mb_request(f"/ws/2/release/{release_mbid}",
                      {"inc": "recordings+artist-credits", "fmt": "json"})
    credit = (data.get("artist-credit") or [{}])[0].get("name")
    tracks = []
    for medium in data.get("media") or []:
        for trk in medium.get("track") or []:
            rec = trk.get("recording") or {}
            trk_credit = (rec.get("artist-credit") or data.get("artist-credit") or [{}])[0]
            tracks.append({
                "title": trk.get("title") or rec.get("title"),
                "artist": trk_credit.get("name"),
                "track_no": int(trk["position"]) if str(trk.get("position", "")).isdigit() else None,
                "disc_no": medium.get("position"),
                "length": (rec.get("length") or trk.get("length") or 0) / 1000 or None,
                "mb_recording": rec.get("id"),
            })

    result = {
        "album": data.get("title"),
        "album_artist": credit,
        "year": (data.get("date") or "")[:4] or None,
        "mb_release": release_mbid,
        "tracks": tracks,
    }
    _cache(key, result)
    return result


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

    data = mb_request("/ws/2/recording", {"query": query, "fmt": "json", "limit": 5})
    return _mb_best(data, track, title, artist, key)


def _mb_best(data: dict, track: dict, title: str, artist: str, key: str) -> dict | None:
    """Pick the closest recording out of a MusicBrainz search response."""
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
        raise

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
        raise

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

def _spotify_auth(raise_on_error: bool = False) -> str | None:
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
        if raise_on_error:
            raise
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
        raise

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


def check(name: str) -> dict:
    """Try a real call against one provider so the UI can say whether a key works."""
    cfg = get_config()["providers"]
    try:
        if name == "musicbrainz":
            mirror = (cfg.get("musicbrainz_url") or "").rstrip("/")
            _get(f"{mirror or 'https://musicbrainz.org'}/ws/2/recording",
                 params={"query": "recording:Creep", "fmt": "json", "limit": 1})
            return {"ok": True,
                    "message": "Your mirror answers. No rate limit applies."
                               if mirror else "Reachable. No key needed."}

        if name == "discogs":
            token = cfg["discogs_token"]
            if not token:
                return {"ok": False, "message": "No token set yet."}
            _get("https://api.discogs.com/database/search",
                 params={"q": "Pablo Honey", "per_page": 1, "token": token})
            return {"ok": True, "message": "Token works."}

        if name == "lastfm":
            key = cfg["lastfm_key"]
            if not key:
                return {"ok": False, "message": "No key set yet."}
            data = _get("https://ws.audioscrobbler.com/2.0/",
                        params={"method": "track.getInfo", "api_key": key, "format": "json",
                                "artist": "Radiohead", "track": "Creep"})
            if data.get("error"):
                return {"ok": False, "message": data.get("message") or "Last.fm rejected the key."}
            return {"ok": True, "message": "Key works."}

        if name == "acoustid":
            if not cfg.get("acoustid_key"):
                return {"ok": False, "message": "No key set yet."}
            if not fpcalc_available():
                return {"ok": False,
                        "message": "fpcalc is missing from the container. Rebuild the "
                                   "image so Chromaprint is installed."}
            data = _get("https://api.acoustid.org/v2/lookup", params={
                "client": cfg["acoustid_key"], "meta": "recordings",
                "duration": 300, "fingerprint": "invalid", "format": "json",
            })
            message = (data.get("error") or {}).get("message", "")
            if "invalid API key" in message.lower() or "invalid client" in message.lower():
                return {"ok": False, "message": "AcoustID rejected that key."}
            return {"ok": True, "message": "Key works and fpcalc is installed."}

        if name == "spotify":
            if not (cfg["spotify_client_id"] and cfg["spotify_client_secret"]):
                return {"ok": False, "message": "Client ID and secret both needed."}
            _spotify_token["expires"] = 0  # force a fresh sign-in rather than trusting a cache
            return ({"ok": True, "message": "Client ID and secret work."}
                    if _spotify_auth(raise_on_error=True)
                    else {"ok": False, "message": "Spotify refused the ID and secret."})

        return {"ok": False, "message": f"No such source: {name}"}

    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            hint = {
                "musicbrainz": "MusicBrainz turned the request away, which usually means "
                               "rate limiting rather than anything you configured. "
                               "Wait a minute and try again.",
                "discogs": "Discogs rejected that token. Make sure it is a personal access "
                           "token, not an application consumer key or secret.",
                "lastfm": "Last.fm rejected that key.",
                "spotify": "Spotify rejected those credentials.",
            }.get(name, "That was rejected.")
            return {"ok": False, "message": hint}
        if code == 503 and name == "musicbrainz":
            return {"ok": False,
                    "message": "MusicBrainz is rate limiting Harmon. Add a contact email "
                               "below — they throttle clients that do not identify "
                               "themselves, and it is the usual cause of this."}
        if code == 429:
            return {"ok": False, "message": "Rate limited. Wait a minute and try again."}
        return {"ok": False, "message": f"The service answered with {code}."}
    except Exception as exc:
        text = str(exc)
        if "name resolution" in text or "getaddrinfo" in text or "Name or service" in text:
            return {"ok": False, "message": "The container cannot resolve domain names. "
                                            "This is a Docker networking problem, not a key "
                                            "problem — see the DNS note in the README."}
        return {"ok": False, "message": f"Could not reach it: {text[:120]}"}


# --- AcoustID ------------------------------------------------------------

_acoustid_lock = threading.Lock()
_acoustid_last = 0.0


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


def fingerprint(path: str) -> tuple[int, str] | None:
    """Chromaprint hears the audio itself, so a wrong tag cannot mislead it."""
    if not fpcalc_available():
        return None
    try:
        out = subprocess.run(["fpcalc", "-json", path], capture_output=True,
                             text=True, timeout=90)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        return int(data["duration"]), data["fingerprint"]
    except Exception:
        return None


def acoustid(track: dict) -> dict | None:
    """Identify a file by what it sounds like rather than what it claims to be.

    This is the one source that works on a file with no usable tags at all,
    which makes it the right last resort after the album and per-track passes.
    """
    key_setting = get_config()["providers"].get("acoustid_key") or ""
    path = track.get("path")
    if not (key_setting and path):
        return None
    if not fpcalc_available():
        raise RuntimeError("fpcalc is not installed in this container")

    fp = fingerprint(path)
    if not fp:
        return None
    duration, code = fp

    cache_key = f"aid:{code[:120]}|{duration}"
    hit = _cached(cache_key)
    if hit is not None:
        return hit or None

    global _acoustid_last
    with _acoustid_lock:                      # free tier allows three a second
        wait = 0.35 - (time.time() - _acoustid_last)
        if wait > 0:
            time.sleep(wait)
        _acoustid_last = time.time()
        data = _get("https://api.acoustid.org/v2/lookup", params={
            "client": key_setting, "duration": duration, "fingerprint": code,
            "meta": "recordings+releasegroups+compress", "format": "json",
        })

    if data.get("status") != "ok":
        raise RuntimeError(data.get("error", {}).get("message", "AcoustID refused the request"))

    best_rec, best_score = None, 0.0
    for result in data.get("results") or []:
        score = float(result.get("score") or 0)
        for rec in result.get("recordings") or []:
            if score > best_score and rec.get("title"):
                best_rec, best_score = rec, score

    if not best_rec or best_score < 0.5:
        _cache(cache_key, {})
        return None

    groups = best_rec.get("releasegroups") or []
    album = next((g["title"] for g in groups if g.get("type") == "Album"), None)         or (groups[0].get("title") if groups else None)
    artists = best_rec.get("artists") or []

    result = {
        "title": best_rec.get("title"),
        "artist": ", ".join(a["name"] for a in artists) or None,
        "album_artist": artists[0]["name"] if artists else None,
        "album": album,
        "mb_recording": best_rec.get("id"),
        # A fingerprint match is evidence about the audio, so it earns more
        # trust than a text match, but it says nothing about which release.
        "score": round(min(0.55 + 0.4 * best_score, 0.95), 3),
        "source": "acoustid",
    }
    result = {k: v for k, v in result.items() if v not in (None, "")}
    _cache(cache_key, result)
    return result


LOOKUPS = {
    "musicbrainz": musicbrainz,
    "discogs": discogs,
    "lastfm": lastfm,
    "spotify": spotify,
    "acoustid": acoustid,
}


def gather(track: dict) -> tuple[list[dict], bool]:
    """Run every configured provider in the order the user set.

    Returns the hits, plus whether anything actually answered. A track that
    got no answer because every source errored is different from one that
    genuinely has no match, and only the second should count as checked.
    """
    order = get_config()["providers"]["order"]
    out: list[dict] = []
    answered = False

    for name in order:
        fn = LOOKUPS.get(name)
        if not fn or is_benched(name):
            continue
        try:
            result = fn(track)
        except RateLimited as exc:
            note_failure(name, str(exc)[:160], limit=STRIKES_BEFORE_BENCH_RATE_LIMIT)
            continue
        except Exception as exc:
            note_failure(name, str(exc)[:160])
            continue
        _note_success(name)
        answered = True          # it replied, even if it had nothing to offer
        if result:
            out.append(result)

    return out, answered


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
