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

print(f"\n  {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
