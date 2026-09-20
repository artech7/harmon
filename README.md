# Harmon

Music library cleanup and format standardization. Scans your library, finds real
duplicates, fills in artists, albums, genres and artwork from four metadata
sources, and keeps everything at the codec and bitrate you choose — the way
Forge does for video.

**Nothing is written to your files until you approve it.** Every proposed edit,
removal and conversion lands in the Review screen first.

Review has three states — Waiting, Approved and Skipped — and everything moves
freely between them. Approving is not a commitment: it collects work for the
next Apply, and until you press Apply you can undo any of it, a whole group at
a time or row by row. Approving a group asks first, and so does Apply, which is
the only step that touches your files. Once something is applied it is final
and cannot be moved back.

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

### What stops it deleting the wrong thing

The album tag alone is not trusted, because trusting it causes real damage:

- **The folder has to agree, not just the tag.** Tags are the least reliable
  thing in a library: three different Coldplay releases can all carry the album
  name "Greatest Songs" because one bad tagger got at them, and trusting that
  groups a single, a standard album and a deluxe edition into one pile with two
  of them marked for deletion. A removal needs the folder *and* the album tag
  to agree. Genuinely redundant copies across folders are still caught by the
  byte-identical check, which verifies contents in full.
- **Different discs never group.** Disc 2's "Intro" is not a duplicate of
  disc 1's "Intro".
- **Title qualifiers are compared, not discarded.** Stripping `(feat. …)` is
  what lets "Low Tide" match "Low Tide (Remastered)", but it also makes
  "Idol (feat. Tech N9ne)" and "Idol (feat. KURT92)" look identical when they
  are different recordings. If two titles both carry a qualifier and the
  qualifiers disagree, they are not duplicates. If only one has a qualifier,
  they can still pair.
- **Byte-identical copies are read in full and compared before deletion.**
  Grouping uses a cheap fingerprint (size plus head and tail); that is fine
  for finding candidates and not good enough to delete on. If the full
  contents differ, the deletion is refused and recorded as failed.
- **Copies spread across folders are staged at lower confidence** and flagged
  in the UI, so they land in a different band in Review from copies sitting
  together in one folder.
- **Nothing is deleted.** Removed files move to your originals folder.

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

Lookups go album-first: Harmon asks MusicBrainz for the release, which returns
the whole tracklist in one response. That is two requests per album instead of
one per track — on a library organised into albums, roughly a tenfold saving,
and better matches, since a full tracklist resolves ambiguity a lone title
cannot. Only files that belong to no recognisable album are checked one by one.

AcoustID is the last resort and the most useful one for badly tagged files: it
fingerprints the audio with `fpcalc` and matches that against MusicBrainz, so
it does not read your tags at all. Free key, three lookups a second. The
Metadata screen estimates what the next pass will cost before you start it.

MusicBrainz allows one request per second and offers no token or paid tier that
raises it — throttling is by User-Agent, by IP, and by how busy their servers
are overall, so a 503 can arrive however well-behaved you are.

When that happens Harmon doubles the delay between requests, up to thirty
seconds, and decays it back down as they recover. If throttling persists it
stops the pass rather than crawling: eight consecutive rate limits and the
source rests for half an hour, then picks up exactly where it left off. A hard
error benches after three. Any source that starts answering again clears its
own strikes. The header shows the current delay and any resting source, so a
slow pass explains itself. Results cache for 30 days.

If a first pass over a large library is too slow, run your own copy with
[musicbrainz-docker](https://github.com/metabrainz/musicbrainz-docker) and put
its address in Settings. Harmon then drops the one-per-second wait entirely,
since it is your hardware answering your own queries. Expect to give it a few
hundred GB and a few hours to import.

### Genres

One genre per artist, drawn from a fixed vocabulary of about 70, with
everything mapped onto it. Three things cause a library to sprout hundreds of
one-off genres, and all three are handled here:

- Sources spell things differently: `Nu Metal`, `nu-metal`, `NUMETAL`.
- They disagree about specificity: `Djent` lands on `Progressive Metal`,
  `Melodic Death Metal` on `Death Metal`.
- Writing several genres per track multiplies the distinct values — three from
  a pool of forty gives thousands of possible strings. So Harmon writes one.

The Metadata screen shows every genre value in your library, how many are
one-offs, and what each would become. Anything the vocabulary cannot place is
listed separately, never guessed at: send it to a genre you already have, or
keep it as a genre of its own. Your genres and mappings take priority over the
built-in ones.

**Tidy up what you have** reads the genres already on your files and stages the
differences in Review. After that, everything Harmon proposes passes through
the same list, so it stays tidy.

Genre is keyed on the *performing* artist with guests dropped, not the album
artist — on a compilation the album artist is "Various Artists", and keying on
that would hand every track on the record one genre.

The rest of how genre is decided: Harmon filters out tags that are not genres (`seen live`, `00s`,
`american`, `female vocalists`), decides by agreement between the sources
rather than taking whichever answered first, and settles it once per artist so
a band cannot end up spread across four genres. A single unsupported answer
stays at low confidence and lands in the low band in Review.

Discogs results are matched against the artist and album before anything is
read from them. Taking the top search result unchecked is how a metal band
ends up tagged Jazz.

If genres were staged before this, clear them from the Metadata screen and run
the lookup again.

By default Harmon only fills in blanks. Turn on "Replace tags that are already
filled in" if you want it to correct values you have already set.

## Artist name hygiene

A shattered artist list — hundreds of one-album artists, most without photos —
is usually two problems, neither of which is missing artwork:

- Names that came from filenames: `3_Doors_Down`, `A_Perfect_Circle`.
- Collaborations written into `album_artist`, so every guest gets their own
  artist entry.

Metadata for Jellyfin, Plex and Navidrome follows one convention: `artist`
holds the full credit, `album_artist` holds the primary artist alone. Harmon's
Metadata screen proposes exactly that, plus mechanical name cleanup.

It never changes casing — no algorithm gets `AC/DC` or `will.i.am` right, so
casing is left to MusicBrainz. Slash splitting is guarded so `AC/DC` survives,
and comma splitting is off by default because `Earth, Wind & Fire` is one band.

Fixing the names usually fixes the missing artist images on its own: media
servers can match `3 Doors Down` against their own metadata sources, but not
`3_Doors_Down`.

## Lyrics

Synced lyrics (`.lrc`, with timestamps) are what make a player scroll along
with the song, and what Jellyfin, Navidrome and Subsonic clients look for.
Plain text is a fallback worth keeping but not worth preferring.

The Lyrics screen splits the library three ways: tracks with synced lyrics,
tracks with plain text only, and tracks with none. Run the scan first — it
looks for a `.lrc` or `.txt` beside each track *and* for lyrics stored in the
audio tags, so anything you already have is not fetched again.

Fetching uses [LRCLIB](https://lrclib.net), which is free and needs no key.
They ask for sequential requests with a 200–500ms gap, so Harmon sends one at
a time and waits 300ms after each response before the next — a real gap rather
than one overlapping the request's own duration. A 429 is honoured, including
`Retry-After`, and twenty consecutive failures stop the pass rather than
grinding on.
Matching is on artist, title, album and duration, so a radio edit does not
inherit the album version's timings.

What gets written:

- A `.lrc` whenever synced lyrics exist, including for tracks that already
  have a `.txt` — that is an upgrade.
- A `.txt` only when no synced version exists *and* the track has no lyrics at
  all. Plain text is never written over plain text.
- Sidecar files only. Harmon reads embedded lyrics but never writes them:
  rewriting every audio file's tags means a full rescan in your media server,
  and embedded synced lyrics are poorly supported anyway. A sidecar is
  reversible by deleting it.
- Existing `.txt` files are left where they are. Players prefer the `.lrc`
  when both sit beside a track.

Everything is staged in Review like any other change.

## Format standardization

Pick a codec and bitrate on the Format screen. Harmon then:

1. Converts to a temp file, never in place.
2. Checks the output length against the source before swapping.
3. Moves the source to your originals folder rather than deleting it.
4. Stamps the file with the target it was converted to, so it is never
   re-encoded on the next pass. Changing the target clears this and re-queues.

Two modes, on the Format screen:

- **Codec and bitrate** — everything ends up at one codec and one bitrate.
  Files above the target are re-encoded down.
- **Codec only** — anything already in the target codec is left exactly as it
  is, whatever its bitrate. Everything else converts at the codec's ceiling
  (320 kbps for AAC and MP3, 256 for Opus). Each file is touched once and
  bitrate never triggers a conversion again.

Codec-only means a 128 kbps source becomes a much larger file without sounding
better — the extra bits come from the encoder, not the music. That is the trade
for never revisiting bitrate. Switching modes later re-queues accordingly.

Guards that are on by default: lossless files are not converted to a lossy
format, and files already below the target bitrate are left alone rather than
being re-encoded upward.

## Browsing by folder

The Folders tab shows the library the way it sits on disk — built from the
index, not by walking the share, so it is quick on a NAS. Each file shows its
codec, bitrate, size and whether it has changes staged. Review's grouped view
links straight to a track's folder, so you can see what you are working on
before approving anything.

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
it. Set your Forge address under Settings → Link to Forge, or seed it with the
`HARMON_FORGE_URL` environment variable.

Harmon borrows Forge's design system — liquid glass with the masked rim, top
tabs, background art painted per theme — with its own palettes (nightfall,
vinyl, airwave, velvet), its own type, and its own mark. The variable names
match Forge's, so a component moved between the two re-skins itself.

Nothing in `app/` assumes it is the only tenant, so folding both into one shared
shell later is a `web/` change, not a backend one.

## If metadata lookups fail to resolve

`Temporary failure in name resolution` means the container has no working DNS.
No API key will help — nothing is reaching the internet at all. Both compose
files set `dns:` explicitly, which fixes it in most Synology setups.

To confirm from inside the container:

```bash
docker exec harmon python3 -c "import socket; print(socket.gethostbyname('musicbrainz.org'))"
```

An address means DNS works. An error means it does not, and the `dns:` entries
are either missing or pointing somewhere unreachable from the NAS.

## Caching

The shell is served with `no-store` and the stylesheet and script are requested
with a version derived from their modification times. A browser therefore
cannot hold a stale `app.js` alongside a fresh `index.html`, which is the
mismatch that produces an error on every status poll. No hard refresh needed
after a deploy.

## Notes

- The watcher polls on a timer rather than using filesystem events, which is
  the more reliable choice on NAS shares.
- Conversions run one file at a time. Distributed workers, as in Forge, are not
  wired up here.
- There is no authentication. Put it behind your reverse proxy.
