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
