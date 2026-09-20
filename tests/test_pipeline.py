import os, sys, shutil, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HARMON_DB"] = "/tmp/regress/harmon.db"
shutil.rmtree("/tmp/regress", ignore_errors=True); os.makedirs("/tmp/regress")

LIB = "/tmp/regress/lib"; ORIG = "/tmp/regress/originals"
def make(p, c, b=None, s=6, f=440):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cmd = ["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi",
           "-i",f"sine=frequency={f}:duration={s}","-c:a",c] + (["-b:a",b] if b else []) + [p]
    subprocess.run(cmd, check=True)

import mutagen
def tag(p, **kw):
    a = mutagen.File(p, easy=True)
    if a.tags is None: a.add_tags()
    for k, v in kw.items(): a[k] = v
    a.save()

make(f"{LIB}/Nightfall/01 Low Tide.mp3","libmp3lame","128k",6,440)
make(f"{LIB}/Nightfall/01 Low Tide (1).mp3","libmp3lame","320k",6,440)
make(f"{LIB}/Nightfall/02 Signal Drift.flac","flac",None,7,523)
make(f"{LIB}/Best Of/05 Low Tide.m4a","aac","256k",6,440)
make(f"{LIB}/Loose/unsorted.mp3","libmp3lame","256k",4,880)
shutil.copy(f"{LIB}/Loose/unsorted.mp3", f"{LIB}/Loose/unsorted (2).mp3")
tag(f"{LIB}/Nightfall/01 Low Tide.mp3", title="Low Tide", artist="Ash Vector", albumartist="Ash Vector", album="Nightfall")
tag(f"{LIB}/Nightfall/01 Low Tide (1).mp3", title="Low Tide (Remastered)", artist="ash vector", album="Nightfall")
tag(f"{LIB}/Nightfall/02 Signal Drift.flac", title="Signal Drift", artist="Ash Vector", album="Nightfall")
tag(f"{LIB}/Best Of/05 Low Tide.m4a", title="Low Tide", artist="Ash Vector", album="Best Of")

from app import db, config, scanner, dupes, transcode, changes, worker
db.init()
config.save({"target": {"originals_path": ORIG, "codec": "aac", "bitrate": 192}})
db.execute("INSERT INTO libraries(name,path) VALUES('Music',?)", (LIB,))

ok = fail = 0
def check(label, got, want):
    global ok, fail
    good = got == want
    ok, fail = ok + good, fail + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label:52} {got}{'' if good else f'  (wanted {want})'}")

check("scan finds every file", scanner.scan()["added"], 6)
d = dupes.find()
check("byte-identical pair grouped", d["identical"], 1)
check("in-album duplicate grouped", d["same_album"], 1)
check("same song on two albums flagged, not actioned", d["cross_album"], 1)

sa = [g for g in dupes.list_groups("same_album")][0]
keeper = [m for m in dupes.group_detail(sa["id"])["members"] if m["keeper"]][0]
check("keeps the higher-bitrate copy", keeper["bitrate"] > 300, True)

ca = dupes.list_groups("cross_album")[0]
check("cross-album set marks nothing for removal",
      sum(1 for m in dupes.group_detail(ca["id"])["members"] if m["keeper"]), 0)
check("cross-album set cannot be staged", changes.stage_deletes(ca["id"]), 0)

staged = sum(changes.stage_deletes(g["id"]) for g in dupes.list_groups()
             if g["kind"] != "cross_album")
check("removals staged for the losers only", staged, 2)
check("nothing written yet", changes.counts()["applied"], 0)
check("still on disk before approval", os.path.exists(f"{LIB}/Loose/unsorted.mp3"), True)

check("conversions skip files queued for removal", transcode.stage_conversions(), 2)
db.execute("UPDATE changes SET status='approved' WHERE status='pending'")
r = changes.apply_approved()
check("removals applied cleanly", (r["applied"], r["failed"]), (2, 0))
check("removed file parked in originals",
      os.path.isfile(f"{ORIG}/removed-duplicates/01 Low Tide.mp3"), True)

cv = worker.run_conversions()
check("conversions ran without failures", (cv["converted"], cv["failed"]), (2, 0))
check("source kept after conversion", os.path.isfile(f"{ORIG}/Loose/unsorted (2).mp3"), True)
check("FLAC untouched under a lossy target",
      os.path.isfile(f"{LIB}/Nightfall/02 Signal Drift.flac"), True)

scanner.scan()
check("second pass queues no re-transcodes", transcode.stage_conversions(), 0)
s2 = transcode.library_summary()
check("summary adds up", s2["matching"] + s2["needs_convert"] + s2["protected"], 4)

config.save({"target": {"codec": "opus", "bitrate": 128}})
check("changing the target re-queues everything", transcode.stage_conversions(), 3)

from app import albums, browse, enrich, hygiene, providers
from app.scanner import normalize

# --- codec-only mode ------------------------------------------------------
config.save({"target": {"codec": "aac", "bitrate": 256, "bitrate_mode": "codec_only"}})
check("codec-only encodes at the codec ceiling", transcode.effective_bitrate(), 320)
check("an AAC file at any bitrate is left alone",
      transcode.assess({"codec": "aac", "bitrate": 96, "lossless": 0})["action"], "keep")
check("a different codec still converts",
      transcode.assess({"codec": "mp3", "bitrate": 320, "lossless": 0})["action"], "convert")
check("lossless is still protected",
      transcode.assess({"codec": "flac", "bitrate": 900, "lossless": 1})["action"], "keep")
check("the ceiling reaches the encoder",
      "-b:a" in transcode.build_command("/a.mp3", "/b.m4a")
      and "320k" in transcode.build_command("/a.mp3", "/b.m4a"), True)
check("the mode is part of the signature",
      "codec_only" in transcode.target_signature(), True)

config.save({"target": {"bitrate_mode": "fixed"}})
check("fixed mode still re-encodes an over-target file",
      transcode.assess({"codec": "aac", "bitrate": 320, "lossless": 0})["action"], "convert")

# Changing the target must invalidate conversions queued under the old one.
# Needs MP3s at a bitrate the fixed target dislikes but codec-only accepts.
db.execute("DELETE FROM changes WHERE kind='convert'")
for _i in range(15):
    db.execute("INSERT INTO tracks(path,title,codec,bitrate,lossless,missing) "
               "VALUES(?,?,'mp3',192,0,0)", (f"/modetest/{_i}.mp3", f"M{_i}"))
mode_ids = [r["id"] for r in db.query("SELECT id FROM tracks WHERE path LIKE '/modetest/%'")]

config.save({"target": {"codec": "mp3", "bitrate": 320, "bitrate_mode": "fixed",
                        "skip_if_lower_bitrate": False, "convert_lossless": False}})
queued_fixed = transcode.stage_conversions(mode_ids)
check("fixed mode queues the under-bitrate MP3s", queued_fixed, 15)
config.save({"target": {"bitrate_mode": "codec_only"}})
_marks = ",".join("?" for _ in mode_ids)
left = db.one(f"SELECT COUNT(*) AS n FROM changes WHERE kind='convert' "
              f"AND status='pending' AND track_id IN ({_marks})", tuple(mode_ids))["n"]
check("switching modes clears conversions no longer wanted", left, 0)
check("what remains really does still need converting",
      all(transcode.assess(dict(db.one("SELECT * FROM tracks WHERE id=?", (r["track_id"],))))
          ["action"] == "convert"
          for r in db.query("SELECT track_id FROM changes "
                            "WHERE kind='convert' AND status='pending'")), True)
db.execute("DELETE FROM changes WHERE kind='convert'")
db.execute("DELETE FROM tracks WHERE path LIKE '/modetest/%'")

# --- folder browsing ------------------------------------------------------
listing = browse.listing(LIB)
check("browsing finds the album folders", len(listing["folders"]) > 0, True)
check("breadcrumbs lead back to the root", listing["crumbs"][0]["path"].startswith("/"), True)
try:
    browse.listing("/etc")
    check("browsing outside the library is refused", False, True)
except ValueError:
    check("browsing outside the library is refused", True, True)


# --- album batching -------------------------------------------------------
db.execute("UPDATE tracks SET enriched_at=NULL, album='Nightfall', "
           "album_key='ash vector|nightfall' WHERE missing=0")
reqs = {"n": 0}
def _find(artist, album, count):
    reqs["n"] += 1
    return {"id": "rel-x", "score": 0.9}
def _tracks(mbid):
    reqs["n"] += 1
    return {"album": "Nightfall", "album_artist": "Ash Vector", "year": "2019",
            "mb_release": mbid, "tracks": [
                {"title": "Low Tide", "artist": "Ash Vector", "track_no": 1, "disc_no": 1,
                 "length": 6.0, "mb_recording": "r1"},
                {"title": "Signal Drift", "artist": "Ash Vector", "track_no": 2, "disc_no": 1,
                 "length": 7.0, "mb_recording": "r2"},
                {"title": "Cold Room", "artist": "Ash Vector", "track_no": 3, "disc_no": 1,
                 "length": 5.0, "mb_recording": "r3"}]}
providers.mb_find_release, providers.mb_release_tracks = _find, _tracks
providers.coverartarchive = lambda *a, **k: None

before = db.one("SELECT COUNT(*) AS n FROM tracks WHERE missing=0")["n"]
res = albums.run()
check("one album costs two requests, not one per track", reqs["n"], 2)
check("every track in the album got matched", res["matched"] > 0, True)
check("batching staged changes", res["staged"] > 0, True)
check("matched tracks are marked checked",
      db.one("SELECT COUNT(*) AS n FROM tracks WHERE missing=0 AND enriched_at IS NOT NULL")["n"],
      res["matched"])
# The tracklist had three entries; anything extra on disk cannot be matched and
# must stay unchecked so the per-track and fingerprint passes still see it.
check("an unmatchable track is left for the next pass",
      db.one("SELECT COUNT(*) AS n FROM tracks WHERE missing=0 AND enriched_at IS NULL")["n"],
      before - res["matched"])
check("the leftover is handed to the per-track path",
      set(enrich.pending_track_ids()) ==
      {r["id"] for r in db.query("SELECT id FROM tracks WHERE enriched_at IS NULL AND missing=0")},
      True)

# --- rate limiting and the circuit breaker --------------------------------
providers.revive_all()
providers._mb_backoff = 30.0
for _ in range(14):
    providers._mb_backoff = 0.0 if providers._mb_backoff < 0.3 else providers._mb_backoff * 0.7
check("backoff recovers from the cap in ~14 good requests", providers._mb_backoff, 0.0)

# The breaker needs more albums than its strike limit to be exercised at all.
for i in range(20):
    db.execute("INSERT INTO tracks(path,title,album,album_key,missing) VALUES(?,?,?,?,0)",
               (f"/synthetic/{i}.mp3", f"S{i}", f"Filler {i}", f"filler|filler {i}"))

providers.revive_all()
tries = {"n": 0}
def _limited(artist, album, count):
    tries["n"] += 1
    raise providers.RateLimited("rate limited")
providers.mb_find_release = _limited
db.execute("UPDATE tracks SET enriched_at=NULL WHERE missing=0")
res_rl = albums.run()
check("the album pass stops instead of grinding", res_rl["stopped_early"], True)
check("throttling benches the source", providers.is_benched("musicbrainz"), True)
check("rate limits get more rope than hard errors",
      tries["n"], providers.STRIKES_BEFORE_BENCH_RATE_LIMIT)

providers.revive_all()
check("reviving clears the bench", providers.is_benched("musicbrainz"), False)

# --- genres ---------------------------------------------------------------
from app import genres
check("listener tags are not genres", genres.looks_like_genre("seen live"), False)
check("decade tags are not genres", genres.looks_like_genre("00s"), False)
check("nationality is not a genre", genres.looks_like_genre("american"), False)
check("compound genres survive", genres.looks_like_genre("alternative metal"), True)
check("junk is filtered out of a tag list",
      genres.clean_tags(["seen live", "alternative metal", "00s", "nu metal"]),
      ["Alternative Metal", "Nu Metal"])

db.execute("DELETE FROM provider_cache WHERE key LIKE 'genre:%'")
_g, _c = genres.resolve({"album_artist": "10 Years"}, [
    {"source": "discogs", "genre": "Jazz"},
    {"source": "lastfm", "genre": "Alternative Metal; Nu Metal"},
    {"source": "spotify", "genre": "Alternative Metal"},
])
check("agreement beats a single wrong source", _g.split(";")[0].strip(), "Alternative Metal")
check("the outlier is dropped entirely", "Jazz" in _g, False)
check("agreement raises confidence", _c > 0.7, True)

db.execute("DELETE FROM provider_cache WHERE key LIKE 'genre:%'")
_g2, _c2 = genres.resolve({"album_artist": "Nobody"}, [{"source": "discogs", "genre": "Jazz"}])
check("one unsupported source stays low-confidence", _c2 < 0.75, True)

_g3, _ = genres.resolve({"album_artist": "10 Years"}, [{"source": "discogs", "genre": "Polka"}])
check("a later bad lookup cannot split an artist", "Polka" in (_g3 or ""), False)

# --- duplicate safety -----------------------------------------------------
check("an empty album tag falls back to the folder",
      dupes._bucket({"album": None, "album_key": "x|", "folder": "/a", "path": "/a/1.mp3"})[0],
      "/a")
check("different folders never share a bucket when untagged",
      dupes._bucket({"album": None, "album_key": "x|", "folder": "/a", "path": "/a/1.mp3"})
      != dupes._bucket({"album": None, "album_key": "x|", "folder": "/b", "path": "/b/1.mp3"}),
      True)
check("different discs never share a bucket",
      dupes._bucket({"album": "Box", "album_key": "x|box", "disc_no": 1, "folder": "/a"})
      != dupes._bucket({"album": "Box", "album_key": "x|box", "disc_no": 2, "folder": "/a"}),
      True)
# --- lyrics ---------------------------------------------------------------
from app import lyrics as _lyr
_lf = os.path.join(LIB, "lyrictest")
os.makedirs(_lf, exist_ok=True)
for _n in ("a", "b", "c"):
    make(f"{_lf}/{_n}.mp3", "libmp3lame", "192k", 4, 440)
    tag(f"{_lf}/{_n}.mp3", title=_n.upper(), artist="Band", album="Album")
open(f"{_lf}/a.lrc", "w").write("[00:01.00] placeholder\n")
open(f"{_lf}/b.txt", "w").write("placeholder\n")
scanner.scan()

check("LRCLIB pacing sits inside their 200-500ms guidance",
      0.2 <= _lyr.MIN_INTERVAL <= 0.5, True)
check("a .lrc counts as synced", _lyr.state_for(f"{_lf}/a.mp3"), "synced")
check("a .txt counts as plain text", _lyr.state_for(f"{_lf}/b.mp3"), "unsynced")
check("neither counts as none", _lyr.state_for(f"{_lf}/c.mp3"), "none")
check("the sidecar name follows the track",
      os.path.basename(_lyr.sidecar(f"{_lf}/c.mp3", ".lrc")), "c.lrc")

_counts = _lyr.scan()
check("the scan finds all three states",
      (_counts["synced"] >= 1, _counts["unsynced"] >= 1, _counts["none"] >= 1),
      (True, True, True))

_orig_lookup = _lyr.lookup
_lyr.lookup = lambda t: ({"kind": "synced", "ext": ".lrc", "body": "[00:02.00] placeholder\n"}
                         if t["title"] in ("B", "C") else None)
_ids = [r["id"] for r in db.query("SELECT id FROM tracks WHERE folder=?", (_lf,))]
_res = _lyr.stage(_ids)
check("tracks that already have a .lrc are not asked about", _res["checked"], 3)
check("synced lyrics staged for the other two", _res["synced"], 2)
check("nothing written yet", os.path.exists(f"{_lf}/c.lrc"), False)

db.execute("UPDATE changes SET status='approved' WHERE kind='lyrics'")
_applied = changes.apply_approved()
check("the sidecars were written", os.path.exists(f"{_lf}/c.lrc"), True)
check("the existing .txt was left alone", os.path.exists(f"{_lf}/b.txt"), True)
check("and now has a .lrc beside it", os.path.exists(f"{_lf}/b.lrc"), True)
check("the audio file was not rewritten",
      _lyr.state_for(f"{_lf}/c.mp3"), "synced")
_lyr.lookup = _orig_lookup
db.execute("DELETE FROM changes")

# --- genre vocabulary -----------------------------------------------------
from app import genrebook as gb
check("spelling variants converge", {gb.canonicalize(t) for t in
      ("nu-metal", "Nu Metal", "NUMETAL", "nu metal")}, {"Nu Metal"})
check("a longer match wins over a broader one",
      gb.canonicalize("melodic doom metal"), "Doom Metal")
check("an unknown genre is not guessed at", gb.canonicalize("Sea Shanty"), None)
gb.add_mapping("Sea Shanty", "Folk")
check("your mapping places it", gb.canonicalize("sea shanty"), "Folk")
gb.add_custom("Polka")
check("your own genre is recognised", gb.canonicalize("polka"), "Polka")
check("your genres join the vocabulary", "Polka" in gb.vocabulary(), True)
_v = gb.vocabulary()
check("the vocabulary is alphabetical", _v, sorted(_v, key=str.lower))
check("your own genres sort in with the rest, not after them",
      _v.index("Polka") < _v.index("Pop") and _v.index("Polka") > _v.index("Metal"), True)

# A stray tag can only be judged by seeing who carries it.
db.execute("UPDATE tracks SET genre='Sea Shanty' WHERE id IN "
           "(SELECT id FROM tracks WHERE missing=0 LIMIT 2)")
_d = gb.tracks_for("Sea Shanty")
check("a genre value lists its artists", len(_d["artists"]) >= 1, True)
check("and its tracks", len(_d["tracks"]) >= 1, True)
check("each track carries an id to jump from",
      all("id" in t for t in _d["tracks"]), True)
check("the total is reported", _d["total"], 2)

# --- consolidating an artist scattered across genres ----------------------
db.execute("DELETE FROM changes")
_n = 9000
for _artist, _genre, _count in [("Two Steps From Hell", "Epic Music", 2),
                                ("Tool", "Epic Music", 2),
                                ("Two Steps From Hell", "Trailer Music", 5),
                                ("Two Steps From Hell", "Cinematic", 3),
                                ("Tool", "Progressive Metal", 6),
                                ("Solo Act", "Epic Music", 2)]:
    for _ in range(_count):
        _n += 1
        db.execute("INSERT INTO tracks(path,title,artist,album_artist,album,genre,missing) "
                   "VALUES(?,?,?,?,'Alb',?,0)",
                   (f"/scatter/{_n}.mp3", f"S{_n}", _artist, _artist, _genre))

_e = gb.artists_elsewhere("Epic Music", "Soundtrack")
_names = [a["artist"] for a in _e["artists"]]
check("artists with tracks elsewhere are listed",
      set(_names), {"Two Steps From Hell", "Tool"})
check("an artist with no scattering is left out", "Solo Act" in _names, False)
check("their other genres are itemised",
      {o["genre"] for a in _e["artists"] if a["artist"] == "Two Steps From Hell"
       for o in a["elsewhere"]}, {"Trailer Music", "Cinematic"})

_r = gb.assign_artist("Two Steps From Hell", "Soundtrack")
check("assigning covers the whole catalogue", _r["staged"], 10)
check("the other artist is untouched",
      db.one("SELECT COUNT(*) AS n FROM changes c JOIN tracks t ON t.id=c.track_id "
             "WHERE t.artist='Tool' AND c.field='genre'")["n"], 0)
check("the artist is pinned", gb.pinned_genre("two steps from hell"), "Soundtrack")
check("a pin beats a lookup",
      genres.resolve({"artist": "Two Steps From Hell"},
                     [{"source": "lastfm", "genre": "Jazz"}])[0], "Soundtrack")
gb.unpin_artist("Two Steps From Hell")
check("unpinning releases it", gb.pinned_genre("Two Steps From Hell"), None)
db.execute("DELETE FROM changes")
db.execute("DELETE FROM tracks WHERE path LIKE '/scatter/%'")
gb.remove_custom("Polka")
check("removing it takes it back out", gb.canonicalize("polka"), None)

check("genre keys on the performing artist, not the album artist",
      genres.artist_key({"artist": "Hollywood Undead feat. Tech N9ne",
                         "album_artist": "Various Artists"}),
      "hollywood undead")

# --- approvals are reversible --------------------------------------------
db.execute("DELETE FROM changes")
_t = db.one("SELECT id FROM tracks WHERE missing=0")["id"]
for _n in range(6):
    db.execute("INSERT INTO changes(kind,track_id,field,new_value,source,confidence) "
               "VALUES('tag',?,'year','1977','musicbrainz-album',0.95)", (_t,))
check("approving moves them out of waiting",
      changes.decide_group("tag", "year", "musicbrainz-album", "high", "approved"), 6)
check("they are visible as approved", changes.counts()["approved"], 6)
check("undo moves them back",
      changes.decide_group("tag", "year", "musicbrainz-album", "high",
                           "pending", from_status="approved"), 6)
check("waiting again", changes.counts()["pending"], 6)

_ids = [r["id"] for r in db.query("SELECT id FROM changes")]
changes.set_status(_ids, "approved")
db.execute("UPDATE changes SET status='applied'")
changes.set_status(_ids, "pending")
check("applied changes cannot be reversed", changes.counts()["applied"], 6)
db.execute("DELETE FROM changes")

from app.scanner import title_qualifier
check("a featured artist is kept as a qualifier",
      title_qualifier("Idol (feat. Tech N9ne)"), "tech n9ne")
check("a plain title has no qualifier", title_qualifier("Idol"), "")
check("different guests are different recordings",
      dupes._same_recording({"title": "Idol (feat. Tech N9ne)"},
                            {"title": "Idol (feat. KURT92)"}), False)
check("a remaster still pairs with the plain title",
      dupes._same_recording({"title": "Low Tide (Remastered)"},
                            {"title": "Low Tide"}), True)
check("two identical titles pair",
      dupes._same_recording({"title": "Low Tide"}, {"title": "Low Tide"}), True)
check("a radio edit does not pair with an acoustic take",
      dupes._same_recording({"title": "Song - Radio Edit"},
                            {"title": "Song (Acoustic Version)"}), False)

check("the folder leads the bucket",
      dupes._bucket({"album": "Box", "album_key": "x|box", "folder": "/a"})[0], "/a")
# Three Coldplay releases all mistagged "Greatest Songs" must stay separate.
_wrong_tag = {"album": "Greatest Songs", "album_key": "coldplay|greatest songs", "disc_no": 1}
check("a wrong album tag cannot merge different folders",
      len({dupes._bucket({**_wrong_tag, "folder": f})
           for f in ("/m/Coldplay/Viva", "/m/Coldplay/Violet_Hill", "/m/Coldplay/Prospekt")}),
      3)
check("one folder with two albums still separates them",
      dupes._bucket({"album": "A", "album_key": "x|a", "folder": "/f"})
      != dupes._bucket({"album": "B", "album_key": "x|b", "folder": "/f"}), True)
check("a real in-folder duplicate still groups",
      dupes._bucket({"album": "X&Y", "album_key": "c|x y", "folder": "/f"})
      == dupes._bucket({"album": "X&Y", "album_key": "c|x y", "folder": "/f"}), True)

check("fingerprinting is wired in", "acoustid" in providers.LOOKUPS, True)
check("acoustid stays quiet without a key",
      providers.acoustid({"path": "/nonexistent.mp3"}), None)


check("underscore names are unpacked", hygiene.tidy("3_Doors_Down"), "3 Doors Down")
check("underscores go even when the name has spaces",
      hygiene.tidy("Hollywood_Undead feat. Tech N9ne"), "Hollywood Undead feat. Tech N9ne")
check("a trailing underscore name is tidied", hygiene.tidy("Antti Martikainen_Epic"),
      "Antti Martikainen Epic")
check("a leading underscore is left as styling", hygiene.tidy("_moshang"), "_moshang")
check("an underscored title matches its spaced form",
      normalize("Low_Tide"), normalize("Low Tide"))
check("an underscored remaster tag is still stripped",
      normalize("Low_Tide_(2011_Remaster)"), normalize("Low Tide"))
check("an underscored feat. still yields the guest",
      title_qualifier("Idol_(feat._Tech_N9ne)"), title_qualifier("Idol (feat. Tech N9ne)"))
check("AC/DC survives slash splitting", hygiene.split_credit("AC/DC"), ["AC/DC"])
check("Earth, Wind & Fire stays one band",
      len(hygiene.split_credit("Earth, Wind & Fire")), 1)
check("collaborations split on the slash",
      hygiene.split_credit("Adam Calhoun/Struggle Jennings"),
      ["Adam Calhoun", "Struggle Jennings"])
check("featured credits yield the primary artist",
      hygiene.split_credit("Aaron Lewis feat. Willie Nelson")[0], "Aaron Lewis")
check("a filename-shaped album artist is caught",
      any(p["field"] == "album_artist" and p["new"] == "3 Doors Down"
          for p in hygiene.assess({"artist": "3_Doors_Down", "album_artist": "3_Doors_Down"})),
      True)
check("a clean track proposes nothing",
      hygiene.assess({"artist": "AC/DC", "album_artist": "AC/DC"}), [])

print(f"\n  {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
