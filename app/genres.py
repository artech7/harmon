"""Working out what an artist actually plays.

Genre is the field most likely to be wrong, for three reasons Harmon has to
handle separately:

1. A search result that was never checked. Asking Discogs for "10 Years"
   returns dozens of releases and the first one may be nothing to do with the
   band you have. Matching has to be verified, not assumed.
2. Crowd tags that are not genres. Last.fm's top tag for a band is often
   "seen live" or "00s" or "female vocalists", which describe the listener
   rather than the music.
3. One track at a time. Genre belongs to an artist far more than to a track,
   so deciding it per file produces a library where one band spans four
   genres depending on which lookup happened to answer.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import db

# A tag counts as a genre if it contains one of these roots. Cheap, and it
# holds up far better than any fixed list of genre names: "alternative metal",
# "post-hardcore" and "melodic death metal" all pass, "seen live" does not.
GENRE_ROOTS = {
    "rock", "metal", "punk", "pop", "jazz", "blues", "soul", "funk", "disco",
    "folk", "country", "bluegrass", "americana", "classical", "opera",
    "baroque", "orchestral", "electronic", "electronica", "house", "techno",
    "trance", "dubstep", "drum and bass", "dnb", "edm", "ambient", "industrial",
    "hip hop", "hip-hop", "rap", "trap", "grime", "r&b", "rnb", "reggae",
    "ska", "dub", "dancehall", "latin", "salsa", "flamenco", "bossa",
    "gospel", "worship", "indie", "alternative", "grunge", "emo", "hardcore",
    "shoegaze", "psychedelic", "progressive", "prog", "garage", "surf",
    "synthpop", "synthwave", "new wave", "post-punk", "post-rock",
    "singer-songwriter", "acoustic", "soundtrack", "score", "world",
    "experimental", "noise", "drone", "swing", "bebop", "ragtime",
    "hardstyle", "breakbeat", "jungle", "lo-fi", "lofi", "chiptune",
    "celtic", "afrobeat", "k-pop", "j-pop", "j-rock", "visual kei", "metalcore",
    "deathcore", "screamo", "nu metal", "nu-metal", "djent", "doom", "sludge",
    "stoner", "thrash", "speed metal", "black metal", "death metal", "power metal",
}

# Tags that pass the root test but describe the listener, not the music.
NOT_A_GENRE = re.compile(
    r"^(seen live|favou?rites?|my |albums i |stuff i |music i |"
    r"\d{2,4}s?$|best of|awesome|beautiful|love|cool|good|great|amazing|"
    r"male vocalists?|female vocalists?|vocalists?|"
    r"american|british|english|scottish|irish|canadian|australian|german|"
    r"swedish|norwegian|finnish|japanese|korean|french|spanish|italian|"
    r"usa|uk|under 2000 listeners|spotify|radio|playlist|mp3|favorite songs)",
    re.I,
)


def looks_like_genre(tag: str) -> bool:
    tag = (tag or "").strip()
    if not tag or len(tag) > 40:
        return False
    if NOT_A_GENRE.match(tag):
        return False
    low = tag.lower()
    return any(root in low for root in GENRE_ROOTS)


def clean_tags(tags: list[str], limit: int = 3) -> list[str]:
    """Keep the genre-shaped tags, in order, without duplicates."""
    out, seen = [], set()
    for tag in tags:
        if not looks_like_genre(tag):
            continue
        pretty = " ".join(w.capitalize() if w.islower() else w
                          for w in str(tag).strip().split())
        key = pretty.lower()
        if key not in seen:
            seen.add(key)
            out.append(pretty)
        if len(out) >= limit:
            break
    return out


def vote(candidates: list[dict]) -> tuple[list[str], float]:
    """Pick genres from several sources, favouring the ones they agree on.

    Each candidate is {"genres": [...], "weight": float, "source": str}.
    Agreement matters more than any single source's ranking: two sources
    independently saying "Alternative Metal" is worth more than one source
    confidently saying "Jazz".
    """
    scores: dict[str, float] = defaultdict(float)
    sources: dict[str, set] = defaultdict(set)
    display: dict[str, str] = {}

    for c in candidates:
        for rank, genre in enumerate(c["genres"]):
            key = genre.lower()
            display.setdefault(key, genre)
            scores[key] += c["weight"] * (1.0 - 0.25 * rank)
            sources[key].add(c["source"])

    if not scores:
        return [], 0.0

    for key in scores:
        if len(sources[key]) > 1:
            scores[key] *= 1.0 + 0.4 * (len(sources[key]) - 1)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_key, top_score = ranked[0]

    # Drop anything trailing far behind the winner. Without this a single
    # unsupported answer still rides along in third place, which is how you
    # end up with "Alternative Metal; Post-Grunge; Jazz".
    ranked = [(k, v) for k, v in ranked if v >= 0.5 * top_score]

    # Confidence tracks agreement and margin, not how sure one source claimed
    # to be. A single unsupported answer stays low no matter who gave it.
    agreeing = len(sources[top_key])
    margin = 1.0 if len(ranked) == 1 else min(1.0, top_score / max(ranked[1][1], 0.01) - 1)
    confidence = 0.5 + 0.18 * (agreeing - 1) + 0.15 * min(margin, 1.0)

    return [display[k] for k, _ in ranked[:3]], round(min(confidence, 0.95), 3)


def artist_key(track: dict) -> str:
    from .scanner import normalize
    return normalize(track.get("album_artist") or track.get("artist") or "")


def cached_for_artist(key: str) -> dict | None:
    row = db.one(
        "SELECT value FROM provider_cache WHERE key=? AND created_at > datetime('now','-30 days')",
        (f"genre:{key}",),
    )
    if not row:
        return None
    import json
    value = json.loads(row["value"])
    return value or None


def cache_for_artist(key: str, genres: list[str], confidence: float) -> None:
    import json
    db.execute(
        "INSERT INTO provider_cache(key, value, created_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, created_at=datetime('now')",
        (f"genre:{key}", json.dumps({"genres": genres, "confidence": confidence})),
    )


def resolve(track: dict, results: list[dict]) -> tuple[str | None, float]:
    """The genre for this track's artist, agreed across whatever answered.

    Cached per artist, so every track by a band gets the same answer and the
    lookup happens once rather than once per file.
    """
    key = artist_key(track)
    if key:
        hit = cached_for_artist(key)
        if hit:
            return "; ".join(hit["genres"]) or None, hit["confidence"]

    # Crowd tags describe how people actually talk about a band; a shop's
    # catalogue categories are broader but better verified. Weight to suit.
    weights = {"lastfm": 1.0, "discogs": 0.85, "spotify": 0.8,
               "musicbrainz": 0.7, "acoustid": 0.5}
    candidates = []
    for r in results:
        raw = r.get("genre")
        if not raw:
            continue
        genres = clean_tags([g.strip() for g in str(raw).split(";")])
        if genres:
            candidates.append({
                "genres": genres,
                "weight": weights.get(r.get("source"), 0.6),
                "source": r.get("source", "unknown"),
            })

    genres, confidence = vote(candidates)
    if key and genres:
        cache_for_artist(key, genres, confidence)
    return ("; ".join(genres) or None), confidence
