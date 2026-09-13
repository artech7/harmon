# Harmon

Music library cleanup and format standardization. Scans your library, finds real
duplicates, fills in artists, albums, genres and artwork from four metadata
sources, and keeps everything at the codec and bitrate you choose — the way
Forge does for video.

**Nothing is written to your files until you approve it.** Every proposed edit,
removal and conversion lands in the Review screen first.

---

## The duplicate rule

Two copies of the same song are not automatically a problem:

| Case | What Harmon does |
|---|---|
| Same song twice **inside one album** | Real duplicate. Keeps the best copy, stages the rest for removal. |
| **Byte-for-byte identical** files anywhere | Same treatment — one copy is enough. |
| Same song on **two different albums** | Left alone. A studio release and a greatest-hits appearance are both meant to be there. Shown under "Across albums" so you can see it was checked. |

Matching strips things like `(Remastered)`, `- 2019 Remaster`, `(feat. …)` and
`(1)` before comparing, then requires the durations to be within a few seconds.
Remixes, live takes and acoustic versions are deliberately *not* stripped —
those are different recordings.

## Metadata

Sources are asked in the order you set on the Settings screen. The first one
with an answer for a given field wins, so a source with no API key is simply
skipped and the next one fills the gap.

| Source | Key needed | Best at |
|---|---|---|
| MusicBrainz | No | Correct artist, album, track and disc numbers |
| Discogs | Token | Release years, styles, pressing detail |
| Last.fm | API key | Genres people actually use |
| Spotify | Client ID + secret | High-resolution artwork |

MusicBrainz allows one request per second, so a first pass over a large library
takes a while. Results are cached for 30 days.

By default Harmon only fills in blanks. Turn on "Replace tags that are already
filled in" if you want it to correct values you have already set.

## Format standardization

Pick a codec and bitrate on the Format screen. Harmon then:

1. Converts to a temp file, never in place.
2. Checks the output length against the source before swapping.
3. Moves the source to your originals folder rather than deleting it.
4. Stamps the file with the target it was converted to, so it is never
   re-encoded on the next pass. Changing the target clears this and re-queues.

Guards that are on by default: lossless files are not converted to a lossy
format, and files already below the target bitrate are left alone rather than
being re-encoded upward.

---

## Running it

```bash
git clone <your repo> harmon
cd harmon
# edit the volume paths in docker-compose.yml first
docker compose up -d
```

Then open `http://<your-nas>:8730`.

### Volumes

| Container path | What goes there |
|---|---|
| `/config` | Harmon's database and settings |
| `/music` | Your library, read and written |
| `/originals` | Replaced sources and removed duplicates |

The paths you type into the "Music folders" screen are the **container** paths.
If your compose file maps `/volume1/music:/music`, you type `/music`.

### First run

1. Settings → add `/music` as a folder.
2. Settings → paste in whichever API keys you have. MusicBrainz works with none.
3. Format → choose your codec and bitrate.
4. Header → **Run a full pass**.
5. Review → approve what you want, then apply.

Once you trust what it suggests, turn on the approval switches at the bottom of
Settings and it will run unattended.

---

## Layout

```
app/
  main.py        FastAPI routes and static hosting
  db.py          SQLite schema, settings, activity log
  config.py      Defaults, codec table, validation
  scanner.py     Folder walk, tag reading, title normalization
  dupes.py       Duplicate grouping and keeper selection
  providers.py   MusicBrainz, Discogs, Last.fm, Spotify, Cover Art Archive
  enrich.py      Turns lookups into staged changes
  changes.py     Approval store and the only code that writes to disk
  transcode.py   Format assessment and the ffmpeg pipeline
  worker.py      Job queue, automation pipeline, library watcher
web/
  index.html     Shell with the app rail
  app.css        Liquid-glass design system
  app.js         Hash-routed front end
tests/
  test_pipeline.py   End-to-end run against a generated library
```

Run the tests with `python3 tests/test_pipeline.py` — it builds a small library
with ffmpeg, then exercises scan, dedupe, staging, approval, apply and convert.

## Sitting alongside Forge

The rail header carries both apps side by side, the same way Tephra and Crucible
do — the app you are in reads bright, the other stays dim until you reach for
it. Set your Forge address under Settings → Switching to Forge, or seed it with the
`HARMON_FORGE_URL` environment variable. The button greys out until it is set.

The link only runs one way: Harmon knows where Forge is, not the reverse. To get
back, add the same rail header to Forge's own page — the markup is the `.apps`
block in `web/index.html` and the `.app` / `.orb` rules in `web/app.css`, with
the active class moved to Forge.

Nothing in `app/` assumes it is the only tenant, so folding both into one shared
shell later is a `web/` change, not a backend one.

## Notes

- The watcher polls on a timer rather than using filesystem events, which is
  the more reliable choice on NAS shares.
- Conversions run one file at a time. Distributed workers, as in Forge, are not
  wired up here.
- There is no authentication. Put it behind your reverse proxy.
