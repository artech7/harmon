/* Harmon front end. Vanilla, hash-routed, one file. */

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

const state = { status: null, config: null, codecs: null, view: 'overview' };

/* --- helpers ----------------------------------------------------------- */

async function api(path, options = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(data?.detail || res.statusText);
  return data;
}

function toast(message, bad = false) {
  const node = el('div', { class: 'toast' + (bad ? ' is-bad' : '') }, message);
  $('#toasts').append(node);
  setTimeout(() => node.remove(), 5200);
}

const bytes = (n) => {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), 4);
  return (n / 1024 ** i).toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
};

const num = (n) => (n || 0).toLocaleString();

const hours = (seconds) => {
  const h = Math.floor((seconds || 0) / 3600);
  return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : `${h}h`;
};

const CODEC_COLOR = {
  FLAC: '#6FE3C4', ALAC: '#57C8E8', AAC: '#A98BFF', MP3: '#FF9057',
  OPUS: '#F2C14E', VORBIS: '#E86A9B', WAV: '#8CE0A0', WMA: '#7A8CA8',
};
const colorFor = (codec) => CODEC_COLOR[codec] || '#6F6A80';

const basename = (p) => (p || '').split('/').pop();

/* --- routing ----------------------------------------------------------- */

const VIEWS = {
  overview:    { title: 'Overview',      sub: 'What Harmon has found in your library.' },
  duplicates:  { title: 'Duplicates',    sub: 'Copies of the same song. Ones on different albums are left alone.' },
  metadata:    { title: 'Metadata',      sub: 'Artists, albums, genres and artwork, filled in from four sources.' },
  standardize: { title: 'Format',        sub: 'Pick the codec and bitrate you want, and Harmon brings everything to it.' },
  review:      { title: 'Review',        sub: 'Nothing is written to your files until you approve it here.' },
  settings:    { title: 'Settings',      sub: 'Folders, lookup sources and how much Harmon does on its own.' },
};

function route() {
  const name = (location.hash.replace('#/', '') || 'overview').split('?')[0];
  state.view = VIEWS[name] ? name : 'overview';
  const meta = VIEWS[state.view];
  $('#view-title').textContent = meta.title;
  $('#view-sub').textContent = meta.sub;
  document.querySelectorAll('#nav a').forEach((a) =>
    a.classList.toggle('is-current', a.dataset.view === state.view));
  render();
}

async function render() {
  const host = $('#view');
  host.innerHTML = '';
  host.append(el('div', { class: 'empty' }, 'Loading…'));
  try {
    const node = await ({
      overview: viewOverview,
      duplicates: viewDuplicates,
      metadata: viewMetadata,
      standardize: viewStandardize,
      review: viewReview,
      settings: viewSettings,
    })[state.view]();
    host.innerHTML = '';
    host.append(node);
  } catch (err) {
    host.innerHTML = '';
    host.append(el('div', { class: 'card' },
      el('h2', {}, 'That screen could not load'),
      el('p', {}, err.message)));
  }
}

/* --- status polling ---------------------------------------------------- */

let lastWorkerKind = null;

async function poll() {
  try {
    state.status = await api('/status');
  } catch { return; }

  const s = state.status;
  const worker = $('#worker');
  const busy = Boolean(s.worker.kind);
  worker.classList.toggle('is-busy', busy);
  $('.worker-text', worker).textContent = busy ? s.worker.message : 'Idle';
  $('#worker-bar').style.width = busy ? `${Math.round(s.worker.progress * 100)}%` : '0%';
  $('#btn-scan').disabled = busy;
  $('#btn-pipeline').disabled = busy;

  const forgeBtn = $('.app[data-app="forge"]');
  forgeBtn.classList.toggle('is-unset', !s.shell?.forge_url);
  forgeBtn.title = s.shell?.forge_url || 'Set your Forge address in Settings';

  $('#rail-count').textContent = s.library.tracks
    ? `${num(s.library.tracks)} tracks`
    : 'No music yet';

  const pending = s.changes.pending || 0;
  $('#badge-review').textContent = pending || '';
  $('#badge-dupes').textContent = (s.duplicates.same_album || 0) + (s.duplicates.identical || 0) || '';
  $('#badge-format').textContent = s.changes.by_kind?.convert || '';

  if (lastWorkerKind && !s.worker.kind) {
    render();
    toast('Finished. Anything Harmon wants to change is waiting in Review.');
  }
  lastWorkerKind = s.worker.kind;
}

/* --- overview ---------------------------------------------------------- */

async function viewOverview() {
  const [s, activity, format] = await Promise.all([
    api('/status'), api('/activity?limit=25'), api('/standardize'),
  ]);
  state.status = s;
  const lib = s.library;
  const wrap = el('div', { class: 'grid' });

  if (!s.libraries.length) {
    wrap.append(el('div', { class: 'card' },
      el('h2', {}, 'Point Harmon at your music'),
      el('p', {}, 'Add the folder your library lives in and Harmon will read every file, group the duplicates and check the tags. It will not change anything until you say so.'),
      el('button', { class: 'btn btn-primary', onclick: () => (location.hash = '#/settings') },
        'Add a music folder')));
    return wrap;
  }

  /* Hero: the library drawn as a band of codecs, widest format first. */
  const codecs = Object.entries(format.by_codec).sort((a, b) => b[1] - a[1]);
  const totalFiles = codecs.reduce((n, [, v]) => n + v, 0) || 1;

  wrap.append(el('div', { class: 'card hero' },
    el('div', { class: 'hero-figures' },
      el('div', { class: 'figure' }, el('strong', {}, num(lib.tracks)), el('span', {}, 'tracks')),
      el('div', { class: 'figure' }, el('strong', {}, num(lib.albums)), el('span', {}, 'albums')),
      el('div', { class: 'figure' }, el('strong', {}, num(lib.artists)), el('span', {}, 'artists')),
      el('div', { class: 'figure' }, el('strong', {}, bytes(lib.bytes)), el('span', {}, 'on disk')),
      el('div', { class: 'figure' }, el('strong', {}, hours(lib.seconds)), el('span', {}, 'of music')),
    ),
    el('div', { class: 'spectrum' },
      codecs.map(([name, count]) => {
        const pct = (count / totalFiles) * 100;
        return el('span', {
          style: `width:${pct}%;background:${colorFor(name)}`,
          title: `${name}: ${num(count)} files`,
        }, pct > 7 ? name : '');
      })),
    el('div', { class: 'spectrum-key' },
      codecs.map(([name, count]) => el('span', {},
        el('i', { style: `background:${colorFor(name)}` }), `${name} ${num(count)}`))),
  ));

  const tiles = el('div', { class: 'grid grid-4' },
    el('div', { class: 'tile ' + (s.duplicates.same_album ? 'is-hot' : 'is-good') },
      el('strong', {}, num(s.duplicates.same_album + s.duplicates.identical)),
      el('span', {}, 'duplicate sets'),
      el('small', {}, s.reclaimable_bytes ? `${bytes(s.reclaimable_bytes)} to reclaim` : 'Nothing to clean up')),
    el('div', { class: 'tile ' + (format.needs_convert ? 'is-hot' : 'is-good') },
      el('strong', {}, num(format.needs_convert)),
      el('span', {}, 'files off your target format'),
      el('small', {}, `${num(format.matching)} already match`)),
    el('div', { class: 'tile' },
      el('strong', {}, num(lib.no_art)),
      el('span', {}, 'tracks with no artwork'),
      el('small', {}, 'Harmon can fetch covers for these')),
    el('div', { class: 'tile' },
      el('strong', {}, num(lib.no_genre)),
      el('span', {}, 'tracks with no genre'),
      el('small', {}, 'Filled in from Last.fm, Discogs or Spotify')),
  );
  wrap.append(tiles);

  if (s.changes.pending) {
    wrap.append(el('div', { class: 'card' },
      el('h2', {}, `${num(s.changes.pending)} changes are waiting on you`),
      el('p', {}, 'Harmon has staged these but written nothing. Look them over and approve the ones you want.'),
      el('button', { class: 'btn btn-primary', onclick: () => (location.hash = '#/review') },
        'Review changes')));
  }

  if (!s.ffmpeg) {
    wrap.append(el('div', { class: 'card' },
      el('h2', {}, 'ffmpeg is missing'),
      el('p', {}, 'Scanning, duplicate detection and metadata all work without it, but Harmon cannot convert files until ffmpeg is on the path inside the container.')));
  }

  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'Recent activity'),
    el('div', { class: 'log' },
      activity.length ? activity.map((a) => el('div', { class: 'is-' + a.level },
        el('time', {}, a.created_at.slice(5, 16)),
        el('span', {}, a.message)))
        : el('div', {}, 'Nothing has happened yet.'))));

  return wrap;
}

/* --- duplicates -------------------------------------------------------- */

let dupeTab = 'same_album';

async function viewDuplicates() {
  const groups = await api('/dupes?kind=' + dupeTab + '&limit=300');
  const wrap = el('div', { class: 'grid' });

  const tabs = el('div', { class: 'tabs' },
    [['same_album', 'Same album'], ['identical', 'Identical files'], ['cross_album', 'Across albums']]
      .map(([key, label]) => el('button', {
        class: dupeTab === key ? 'is-on' : '',
        onclick: () => { dupeTab = key; render(); },
      }, label)));

  const explain = {
    same_album: 'The same song appearing more than once inside one album. These are the real duplicates — Harmon keeps the best copy and stages the rest for removal.',
    identical: 'Byte-for-byte identical files, wherever they sit in the library. Safe to collapse down to one.',
    cross_album: 'The same song on two different albums — a studio release and a compilation, say. This is normal and Harmon will not touch it. Shown here so you can see it was checked.',
  }[dupeTab];

  const bar = el('div', { class: 'bar-actions' }, tabs);
  if (dupeTab !== 'cross_album' && groups.length) {
    bar.append(el('div', { class: 'spacer' }));
    bar.append(el('button', {
      class: 'btn btn-danger', onclick: async () => {
        const r = await api('/dupes/stage-all', { method: 'POST' });
        toast(`${r.staged} removals staged. Approve them in Review.`);
        render();
      },
    }, 'Stage every removal'));
  }

  const card = el('div', { class: 'card' },
    el('h2', {}, VIEWS.duplicates.title),
    el('p', {}, explain),
    bar);

  if (!groups.length) {
    card.append(el('div', { class: 'empty' },
      el('b', {}, 'Nothing here'),
      dupeTab === 'cross_album'
        ? 'No songs appear on more than one album.'
        : 'Harmon found no duplicates of this kind. Run a scan if you have added music recently.'));
  } else {
    const list = el('div', { class: 'rows' });
    groups.forEach((g) => list.append(dupeGroup(g)));
    card.append(list);
  }

  wrap.append(card);
  return wrap;
}

function dupeGroup(g) {
  const body = el('div', { class: 'group-body', style: 'display:none' });
  let loaded = false;

  const head = el('div', {
    class: 'group-head',
    onclick: async () => {
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : 'flex';
      if (!open && !loaded) { loaded = true; await fillGroup(g.id, body); }
    },
  },
    el('div', {},
      el('div', { class: 'row-title' }, g.title || 'Untitled'),
      el('div', { class: 'row-sub' }, g.artist || 'Unknown artist')),
    el('span', { class: 'pill' }, `${g.copies} copies`),
    el('span', { class: g.kind === 'cross_album' ? 'pill pill-cool' : 'pill pill-hot' },
      g.kind === 'cross_album' ? 'Left alone' : `${bytes(g.reclaimable)} to reclaim`),
    el('span', { class: 'pill' }, 'Show copies'),
  );

  return el('div', { class: 'group' }, head, body);
}

async function fillGroup(groupId, body) {
  body.innerHTML = '';
  const detail = await api('/dupes/' + groupId);
  const readOnly = detail.kind === 'cross_album';

  detail.members.forEach((m) => {
    const mark = el('input', {
      type: 'radio', name: 'keep-' + groupId, checked: !!m.keeper, disabled: readOnly,
      onchange: async () => {
        await api(`/dupes/${groupId}/keeper/${m.id}`, { method: 'POST' });
        await fillGroup(groupId, body);
      },
    });
    body.append(el('div', { class: 'copy' + (m.keeper ? ' is-keeper' : '') },
      readOnly ? el('span', {}) : mark,
      el('div', {},
        el('div', { class: 'row-title' },
          `${(m.codec || '?').toUpperCase()} · ${m.bitrate || '?'} kbps · ${bytes(m.size)}`),
        el('div', { class: 'copy-path' }, m.path),
        el('div', { class: 'row-sub' }, m.album || 'No album tag')),
      el('span', { class: m.keeper ? 'pill pill-good' : 'pill' }, m.reason)));
  });

  if (!readOnly) {
    body.append(el('div', { class: 'bar-actions', style: 'margin:10px 0 0' },
      el('button', {
        class: 'btn btn-sm btn-danger', onclick: async () => {
          const r = await api(`/dupes/${groupId}/stage`, { method: 'POST' });
          toast(`${r.staged} removals staged. They will not happen until approved.`);
          poll();
        },
      }, 'Stage the other copies for removal'),
      el('span', { class: 'row-sub' },
        'Removed files move to your originals folder, not the bin.')));
  }
}

/* --- metadata ---------------------------------------------------------- */

let metaOnlyProblems = true;

async function viewMetadata() {
  const tracks = await api(`/tracks?limit=150&problems=${metaOnlyProblems}`);
  const staged = await api('/changes?status=pending&kind=tag&limit=1');

  const card = el('div', { class: 'card' },
    el('h2', {}, 'Tag and artwork cleanup'),
    el('p', {}, 'Harmon asks MusicBrainz, Discogs, Last.fm and Spotify in the order you set, takes the first answer for each field, and stages what it would change. Your files stay untouched until you approve.'),
    el('div', { class: 'bar-actions' },
      el('label', { class: 'checkall' },
        el('input', {
          type: 'checkbox', checked: metaOnlyProblems,
          onchange: (e) => { metaOnlyProblems = e.target.checked; render(); },
        }),
        'Only show tracks with something missing'),
      el('div', { class: 'spacer' }),
      staged.counts.by_kind?.tag
        ? el('span', { class: 'pill pill-cool' },
            `${staged.counts.by_kind.tag} suggestions waiting in Review`)
        : null,
      el('button', {
        class: 'btn btn-primary',
        onclick: async () => {
          await api('/run/enrich', { method: 'POST', body: {} });
          toast('Looking things up. MusicBrainz limits Harmon to one track a second.');
          poll();
        },
      }, 'Look up everything not yet checked')));

  if (!tracks.length) {
    card.append(el('div', { class: 'empty' },
      el('b', {}, 'Every track has what it needs'),
      'Artist, album, genre and artwork are all filled in.'));
    return card;
  }

  const list = el('div', { class: 'rows' });
  tracks.forEach((t) => {
    const gaps = [];
    if (!t.artist) gaps.push('artist');
    if (!t.album) gaps.push('album');
    if (!t.genre) gaps.push('genre');
    if (!t.has_art) gaps.push('artwork');

    list.append(el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto' },
      el('div', {},
        el('div', { class: 'row-title' }, t.title || basename(t.path)),
        el('div', { class: 'row-sub' },
          [t.album_artist || t.artist || 'Unknown artist', t.album || 'No album'].join(' — '))),
      gaps.length
        ? el('span', { class: 'pill pill-hot' }, 'Missing ' + gaps.join(', '))
        : el('span', { class: 'pill pill-good' }, 'Complete'),
      el('button', {
        class: 'btn btn-sm',
        onclick: async (e) => {
          e.target.disabled = true;
          e.target.textContent = 'Looking up…';
          const r = await api('/run/enrich', { method: 'POST', body: { track_ids: [t.id] } });
          setTimeout(() => { toast('Suggestions staged for this track.'); poll(); }, 1500);
        },
      }, 'Look this one up')));
  });

  card.append(list);
  return card;
}

/* --- standardize ------------------------------------------------------- */

async function viewStandardize() {
  const [cfg, codecs, summary] = await Promise.all([
    api('/config'), api('/codecs'), api('/standardize'),
  ]);
  state.config = cfg;
  state.codecs = codecs;

  const wrap = el('div', { class: 'grid' });
  const target = cfg.target;
  const spec = codecs[target.codec];

  const save = async (patch) => {
    state.config = await api('/config', { method: 'PUT', body: { target: patch } });
    render();
  };

  const picker = el('div', { class: 'codecs' },
    Object.entries(codecs).map(([key, c]) => el('button', {
      class: 'codec' + (key === target.codec ? ' is-on' : ''),
      onclick: () => save({ codec: key, bitrate: c.bitrates.at(-2) || target.bitrate }),
    }, el('b', {}, c.label), el('small', {}, c.blurb))));

  const controls = el('div', { class: 'grid grid-2', style: 'margin-top:20px' });

  if (spec.bitrates.length) {
    controls.append(el('div', { class: 'field' },
      el('label', {}, 'Bitrate'),
      el('select', { onchange: (e) => save({ bitrate: Number(e.target.value) }) },
        spec.bitrates.map((b) => el('option', {
          value: b, selected: b === target.bitrate ? 'selected' : null,
        }, `${b} kbps`))),
      el('small', {}, 'Files already within about 12% of this are left alone.')));
  } else if (spec.quality?.length) {
    controls.append(el('div', { class: 'field' },
      el('label', {}, spec.quality_label || 'Quality'),
      el('select', { onchange: (e) => save({ quality: e.target.value }) },
        spec.quality.map((q) => el('option', {
          value: q, selected: q === target.quality ? 'selected' : null,
        }, `Level ${q}`)))));
  }

  controls.append(el('div', { class: 'field' },
    el('label', {}, 'Sample rate'),
    el('select', { onchange: (e) => save({ samplerate: Number(e.target.value) }) },
      [[0, 'Keep whatever the source uses'], [44100, '44.1 kHz'], [48000, '48 kHz']]
        .map(([v, label]) => el('option', {
          value: v, selected: v === target.samplerate ? 'selected' : null,
        }, label)))));

  const rules = el('div', {},
    switchRow('Convert lossless files too', 'Off by default, so your FLAC and ALAC rips are never turned into a lossy format by accident.',
      target.convert_lossless, (v) => save({ convert_lossless: v })),
    switchRow('Leave files that are already below the target bitrate', 'Re-encoding a 128 kbps file at 256 kbps cannot add back what is missing — it only makes the file bigger.',
      target.skip_if_lower_bitrate, (v) => save({ skip_if_lower_bitrate: v })),
    switchRow('Keep the original file after converting', 'Sources are moved to your originals folder instead of being deleted, so a bad conversion is one file move away from being undone.',
      target.keep_originals, (v) => save({ keep_originals: v })),
  );

  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'The format you want everything in'),
    el('p', {}, 'New music added to your library is checked against this automatically, converted, and swapped in — the same way Forge handles video.'),
    picker, controls, rules,
    el('div', { class: 'field', style: 'margin-top:8px' },
      el('label', {}, 'Originals folder'),
      el('input', {
        type: 'text', value: target.originals_path,
        onchange: (e) => save({ originals_path: e.target.value }),
      }),
      el('small', {}, 'Replaced sources and removed duplicates both land here.'))));

  wrap.append(el('div', { class: 'grid grid-3' },
    el('div', { class: 'tile is-good' },
      el('strong', {}, num(summary.matching)), el('span', {}, `already ${spec.label}`)),
    el('div', { class: 'tile ' + (summary.needs_convert ? 'is-hot' : '') },
      el('strong', {}, num(summary.needs_convert)), el('span', {}, 'need converting')),
    el('div', { class: 'tile' },
      el('strong', {}, num(summary.protected)), el('span', {}, 'protected by your rules'),
      el('small', {}, 'Lossless files, or already below target')),
  ));

  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'Queue the conversions'),
    el('p', {}, `Harmon will stage ${num(summary.needs_convert)} files. Conversions run one at a time, are checked against the source length before the swap, and never start until you approve them in Review.`),
    el('div', { class: 'bar-actions' },
      el('button', {
        class: 'btn btn-primary', disabled: !summary.needs_convert,
        onclick: async () => {
          const r = await api('/standardize/stage', { method: 'POST', body: {} });
          toast(`${r.staged} conversions staged.`);
          poll(); render();
        },
      }, 'Stage conversions'),
      el('button', {
        class: 'btn', onclick: async () => {
          await api('/run/convert', { method: 'POST', body: {} });
          toast('Running approved conversions.');
          poll();
        },
      }, 'Run approved conversions now'))));

  return wrap;
}

function switchRow(title, blurb, checked, onchange) {
  return el('label', { class: 'switch' },
    el('input', { type: 'checkbox', checked: !!checked, onchange: (e) => onchange(e.target.checked) }),
    el('span', {}, el('b', {}, title), el('small', {}, blurb)));
}

/* --- review ------------------------------------------------------------ */

let reviewKind = null;
const selected = new Set();

async function viewReview() {
  const data = await api('/changes?status=pending&limit=400' + (reviewKind ? '&kind=' + reviewKind : ''));
  const counts = data.counts;
  const items = data.items;
  selected.clear();

  const wrap = el('div', { class: 'grid' });

  const tabs = el('div', { class: 'tabs' },
    [[null, 'Everything'], ['tag', 'Tags'], ['art', 'Artwork'],
     ['delete', 'Removals'], ['convert', 'Conversions']]
      .map(([key, label]) => el('button', {
        class: reviewKind === key ? 'is-on' : '',
        onclick: () => { reviewKind = key; render(); },
      }, label + (counts.by_kind?.[key] ? ` (${counts.by_kind[key]})` : ''))));

  const card = el('div', { class: 'card' },
    el('h2', {}, `${num(counts.pending)} changes staged`),
    el('p', {}, 'This is the only screen that writes to your library. Approve what you want, then apply — everything else stays as it is.'),
    el('div', { class: 'bar-actions' }, tabs,
      el('div', { class: 'spacer' }),
      el('button', {
        class: 'btn btn-sm', disabled: !items.length,
        onclick: () => decideSelected('approved'),
      }, 'Approve ticked'),
      el('button', {
        class: 'btn btn-sm', disabled: !items.length,
        onclick: async () => {
          await api('/changes/decide-all', { method: 'POST', body: { kind: reviewKind, status: 'approved' } });
          toast('Approved. Apply them when you are ready.'); render(); poll();
        },
      }, 'Approve all shown'),
      el('button', {
        class: 'btn btn-sm', disabled: !items.length,
        onclick: async () => {
          await api('/changes/decide-all', { method: 'POST', body: { kind: reviewKind, status: 'rejected' } });
          toast('Rejected.'); render(); poll();
        },
      }, 'Reject all shown'),
      el('button', {
        class: 'btn btn-primary btn-sm', disabled: !counts.approved,
        onclick: async () => {
          await api('/run/apply', { method: 'POST', body: {} });
          toast('Writing approved changes to your files.'); poll();
        },
      }, `Apply ${num(counts.approved || 0)} approved changes`)));

  if (counts.approved) {
    card.append(el('div', { class: 'bar-actions' },
      el('span', { class: 'pill pill-good' },
        `${num(counts.approved)} approved and ready to write`),
      counts.failed ? el('span', { class: 'pill pill-hot' }, `${num(counts.failed)} failed`) : null));
  }

  if (!items.length) {
    card.append(el('div', { class: 'empty' },
      el('b', {}, 'Nothing waiting'),
      'Run a scan or a metadata lookup and anything Harmon wants to change will appear here first.'));
    wrap.append(card);
    return wrap;
  }

  const list = el('div', { class: 'rows' });
  items.forEach((c) => list.append(changeRow(c)));
  card.append(list);
  wrap.append(card);
  return wrap;
}

async function decideSelected(status) {
  if (!selected.size) {
    toast('Tick the rows you want first, or use the "all shown" buttons.');
    return;
  }
  const n = selected.size;
  await api('/changes/decide', { method: 'POST', body: { ids: [...selected], status } });
  toast(`${n} ${status === 'approved' ? 'approved' : 'skipped'}.`);
  render(); poll();
}

function changeRow(c) {
  const label = {
    tag: 'Tag', art: 'Artwork', delete: 'Remove file', convert: 'Convert',
  }[c.kind] || c.kind;

  let detail;
  if (c.kind === 'tag') {
    detail = el('div', { class: 'diff' },
      el('span', { class: 'row-sub' }, c.field.replace('_', ' ')),
      c.old_value ? el('s', {}, c.old_value) : el('span', { class: 'row-sub' }, '(empty)'),
      el('em', {}, c.new_value));
  } else if (c.kind === 'art') {
    let host = c.source || 'a cover source';
    try { host = new URL(c.new_value).hostname; } catch { /* not a URL, keep the source name */ }
    detail = el('div', { class: 'diff' }, el('em', {}, 'Embed a cover image'),
      el('span', { class: 'row-sub' }, host));
  } else if (c.kind === 'delete') {
    detail = el('div', { class: 'diff' }, el('s', {}, c.old_value));
  } else {
    detail = el('div', { class: 'diff' }, el('em', {}, c.new_value),
      el('span', { class: 'row-sub' }, c.old_value));
  }

  const kindPill = { delete: 'pill pill-hot', convert: 'pill pill-cool' }[c.kind] || 'pill';

  return el('div', { class: 'row', style: 'grid-template-columns:auto 1fr auto auto auto' },
    el('input', {
      type: 'checkbox',
      onchange: (e) => e.target.checked ? selected.add(c.id) : selected.delete(c.id),
    }),
    el('div', {},
      el('div', { class: 'row-title' }, c.title || basename(c.path)),
      el('div', { class: 'row-sub' }, [c.artist, c.album].filter(Boolean).join(' — ') || c.path),
      detail),
    el('span', { class: kindPill }, label),
    el('span', { class: 'pill mono', title: 'How sure the source is' },
      `${Math.round((c.confidence || 0) * 100)}%`),
    el('div', { class: 'row-actions' },
      el('button', {
        class: 'btn btn-sm btn-primary', onclick: async () => {
          await api('/changes/decide', { method: 'POST', body: { ids: [c.id], status: 'approved' } });
          render(); poll();
        },
      }, 'Approve'),
      el('button', {
        class: 'btn btn-sm', onclick: async () => {
          await api('/changes/decide', { method: 'POST', body: { ids: [c.id], status: 'rejected' } });
          render(); poll();
        },
      }, 'Skip')));
}

/* --- settings ---------------------------------------------------------- */

async function viewSettings() {
  const [cfg, status] = await Promise.all([api('/config'), api('/status')]);
  state.config = cfg;

  const save = async (patch, rerender = false) => {
    state.config = await api('/config', { method: 'PUT', body: patch });
    if (rerender) render();
  };

  const wrap = el('div', { class: 'grid' });

  /* Shell */
  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'Switching to Forge'),
    el('p', {}, 'The address of your Forge instance. Once this is set, the Forge button at the top of the rail switches over to it.'),
    el('div', { class: 'field' },
      el('label', {}, 'Forge address'),
      el('input', {
        type: 'text', value: cfg.shell?.forge_url || '',
        placeholder: 'https://forge.yourdomain.tld',
        onchange: (e) => save({ shell: { forge_url: e.target.value.trim() } }, true),
      }),
      el('small', {}, 'Include https:// and no trailing slash. Leave it empty to grey the button out.'))));

  /* Libraries */
  const libList = el('div', { class: 'rows' },
    status.libraries.length
      ? status.libraries.map((l) => el('div', { class: 'row', style: 'grid-template-columns:1fr auto' },
          el('div', {},
            el('div', { class: 'row-title' }, l.name),
            el('div', { class: 'row-sub' }, l.path)),
          el('button', {
            class: 'btn btn-sm', onclick: async () => {
              await api('/libraries/' + l.id, { method: 'DELETE' });
              render();
            },
          }, 'Remove')))
      : el('div', { class: 'empty' }, 'No folders yet.'));

  const pathInput = el('input', { type: 'text', placeholder: '/music' });
  const nameInput = el('input', { type: 'text', placeholder: 'Music' });

  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'Music folders'),
    el('p', {}, 'The paths as Harmon sees them inside its container, not as they look on your NAS. If you mounted your library at /music in the compose file, that is what goes here.'),
    libList,
    el('div', { class: 'grid grid-2', style: 'margin-top:16px' },
      el('div', { class: 'field' }, el('label', {}, 'Folder path'), pathInput),
      el('div', { class: 'field' }, el('label', {}, 'What to call it'), nameInput)),
    el('button', {
      class: 'btn btn-primary', onclick: async () => {
        try {
          await api('/libraries', { method: 'POST', body: { path: pathInput.value, name: nameInput.value } });
          toast('Folder added. Run a scan to read it.');
          render();
        } catch (e) { toast(e.message, true); }
      },
    }, 'Add folder')));

  /* Providers */
  const order = [...cfg.providers.order];
  const orderList = el('div', { class: 'rows' },
    order.map((name, i) => el('div', { class: 'row', style: 'grid-template-columns:auto 1fr auto' },
      el('span', { class: 'pill mono' }, i + 1),
      el('div', {},
        el('div', { class: 'row-title' }, {
          musicbrainz: 'MusicBrainz', discogs: 'Discogs', lastfm: 'Last.fm', spotify: 'Spotify',
        }[name]),
        el('div', { class: 'row-sub' }, {
          musicbrainz: 'Free and needs no key. Best for correct artist, album and track numbers.',
          discogs: 'Needs a token. Best for release years, styles and pressing detail.',
          lastfm: 'Needs a key. Best for genres people actually use.',
          spotify: 'Needs a client ID and secret. Best for high-resolution artwork.',
        }[name]),
        el('div', { style: 'margin-top:7px' },
          el('span', { class: 'probe pill' },
            name === 'musicbrainz' ? 'No key needed'
              : (name === 'spotify'
                  ? (cfg.providers.spotify_client_id && cfg.providers.spotify_client_secret)
                  : cfg.providers[name === 'discogs' ? 'discogs_token' : 'lastfm_key'])
                ? 'Key saved — untested' : 'No key yet, this one gets skipped'))),
      el('div', { class: 'row-actions' },
        el('button', {
          class: 'btn btn-sm',
          onclick: async (e) => {
            const btn = e.target;
            const cell = btn.closest('.row').querySelector('.probe');
            btn.disabled = true; cell.textContent = 'Checking…'; cell.className = 'probe pill';
            const r = await api(`/providers/${name}/test`, { method: 'POST', body: {} });
            cell.textContent = r.message;
            cell.className = 'probe pill ' + (r.ok ? 'pill-good' : 'pill-hot');
            btn.disabled = false;
          },
        }, 'Test'),
        el('button', {
          class: 'btn btn-sm', disabled: i === 0, onclick: () => {
            const next = [...order];
            [next[i - 1], next[i]] = [next[i], next[i - 1]];
            save({ providers: { order: next } }, true);
          },
        }, 'Move up'),
        el('button', {
          class: 'btn btn-sm', disabled: i === order.length - 1, onclick: () => {
            const next = [...order];
            [next[i], next[i + 1]] = [next[i + 1], next[i]];
            save({ providers: { order: next } }, true);
          },
        }, 'Move down')))));

  const keyField = (label, key, blurb, where, type = 'password') =>
    el('div', { class: 'field' },
      el('label', {}, label),
      el('input', {
        type, value: cfg.providers[key] || '',
        onchange: (e) => save({ providers: { [key]: e.target.value.trim() } }),
      }),
      el('small', {}, blurb, ' ',
        el('a', { href: where.url, target: '_blank', rel: 'noopener' }, where.label)));

  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'Where metadata comes from'),
    el('p', {}, 'Harmon asks these in order and takes the first answer it gets for each field, so a source with no key simply gets skipped and the next one fills the gap.'),
    orderList,
    el('div', { class: 'grid grid-2', style: 'margin-top:18px' },
      keyField('Discogs personal access token', 'discogs_token',
        'One long string. Not the consumer key or secret from an application — those are for signing in as other people, which Harmon never does.',
        { url: 'https://www.discogs.com/settings/developers', label: 'Generate one on Discogs' }),
      keyField('Last.fm API key', 'lastfm_key',
        'The API key from your account, not the shared secret.',
        { url: 'https://www.last.fm/api/account/create', label: 'Create a Last.fm API account' }),
      keyField('Spotify client ID', 'spotify_client_id',
        'Create an app in the dashboard, then copy its client ID.',
        { url: 'https://developer.spotify.com/dashboard', label: 'Open the Spotify dashboard' }, 'text'),
      keyField('Spotify client secret', 'spotify_client_secret',
        'Shown under the client ID once you click "View client secret".',
        { url: 'https://developer.spotify.com/dashboard', label: 'Open the Spotify dashboard' }))));

  /* Enrichment */
  const fieldToggles = el('div', {},
    Object.entries(cfg.enrich.fields).map(([f, on]) => switchRow(
      f.replace('_', ' ').replace(/^./, (c) => c.toUpperCase()),
      `Let Harmon correct the ${f.replace('_', ' ')} tag.`,
      on, (v) => save({ enrich: { fields: { [f]: v } } }))));

  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'What Harmon is allowed to correct'),
    el('p', {}, 'Turn off anything you would rather keep exactly as you tagged it.'),
    fieldToggles,
    el('div', { class: 'grid grid-2', style: 'margin-top:18px' },
      el('div', { class: 'field' },
        el('label', {}, 'Minimum confidence'),
        el('input', {
          type: 'number', min: '0.4', max: '1', step: '0.01', value: cfg.enrich.min_confidence,
          onchange: (e) => save({ enrich: { min_confidence: Number(e.target.value) } }),
        }),
        el('small', {}, 'How closely a match must line up before Harmon suggests it. 0.82 is a good balance.')),
      el('div', { class: 'field' },
        el('label', {}, 'Smallest artwork to accept'),
        el('input', {
          type: 'number', min: '200', step: '50', value: cfg.enrich.art_min_px,
          onchange: (e) => save({ enrich: { art_min_px: Number(e.target.value) } }),
        }),
        el('small', {}, 'In pixels along the shorter edge.'))),
    switchRow('Embed artwork when a track has none', 'Downloads a front cover and writes it into the file.',
      cfg.enrich.embed_art, (v) => save({ enrich: { embed_art: v } })),
    switchRow('Replace tags that are already filled in', 'Off by default: Harmon only fills blanks unless it is very sure the existing value is wrong.',
      cfg.enrich.overwrite_existing, (v) => save({ enrich: { overwrite_existing: v } }))));

  /* Duplicates */
  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'How duplicates are judged'),
    el('p', {}, 'Two files count as the same song when the artist and title match after Harmon strips things like "Remastered" and "feat.", and their lengths are close enough.'),
    el('div', { class: 'grid grid-2' },
      el('div', { class: 'field' },
        el('label', {}, 'Length may differ by'),
        el('input', {
          type: 'number', min: '0', max: '30', step: '0.5', value: cfg.dupes.duration_tolerance,
          onchange: (e) => save({ dupes: { duration_tolerance: Number(e.target.value) } }),
        }),
        el('small', {}, 'Seconds. Three is enough to cover different encoders and trimmed silence.')),
      el('div', { class: 'field' },
        el('label', {}, 'Which copy to keep'),
        el('select', { onchange: (e) => save({ dupes: { keeper_rule: e.target.value } }) },
          [['bitrate', 'Best quality — lossless first, then highest bitrate'],
           ['size', 'Largest file'],
           ['lossless', 'Lossless above all else'],
           ['newest', 'Most recently modified']].map(([v, label]) =>
            el('option', { value: v, selected: v === cfg.dupes.keeper_rule ? 'selected' : null }, label))),
        el('small', {}, 'You can override the choice on any individual set.')))));

  /* Automation */
  const a = cfg.automation;
  wrap.append(el('div', { class: 'card' },
    el('h2', {}, 'How much Harmon does on its own'),
    el('p', {}, 'Scanning, looking things up and staging are always safe — they change nothing on disk. The approval switches below are the ones that let Harmon write without asking, so turn them on only once you trust what it is suggesting.'),
    switchRow('Watch the library for new music', 'Re-scans on a timer and runs new files through the whole check.',
      a.auto_scan, (v) => save({ automation: { auto_scan: v } })),
    el('div', { class: 'field' },
      el('label', {}, 'Check every'),
      el('input', {
        type: 'number', min: '1', max: '1440', value: a.scan_interval_min,
        onchange: (e) => save({ automation: { scan_interval_min: Number(e.target.value) } }),
      }),
      el('small', {}, 'Minutes.')),
    switchRow('Look up metadata for new tracks', 'Stages suggestions automatically after each scan.',
      a.auto_enrich, (v) => save({ automation: { auto_enrich: v } })),
    switchRow('Check new tracks against your target format', 'Stages conversions for anything that does not match.',
      a.auto_standardize, (v) => save({ automation: { auto_standardize: v } })),
    el('h2', { style: 'margin-top:22px' }, 'Approve without asking'),
    switchRow('Tag and artwork changes', 'Only ones at or above your confidence setting.',
      a.auto_approve_tags, (v) => save({ automation: { auto_approve_tags: v } })),
    switchRow('Format conversions', 'Originals are still kept if that setting is on.',
      a.auto_approve_converts, (v) => save({ automation: { auto_approve_converts: v } })),
    switchRow('Duplicate removals', 'The riskiest one. Files move to your originals folder rather than being deleted.',
      a.auto_approve_deletes, (v) => save({ automation: { auto_approve_deletes: v } })),
    el('h2', { style: 'margin-top:22px' }, 'Quiet hours'),
    switchRow('Only convert between set hours', 'Scanning and lookups still run any time; only the heavy conversion work waits.',
      a.schedule_enabled, (v) => save({ automation: { schedule_enabled: v } })),
    el('div', { class: 'grid grid-2' },
      el('div', { class: 'field' }, el('label', {}, 'Start'),
        el('input', {
          type: 'time', value: a.schedule_start,
          onchange: (e) => save({ automation: { schedule_start: e.target.value } }),
        })),
      el('div', { class: 'field' }, el('label', {}, 'End'),
        el('input', {
          type: 'time', value: a.schedule_end,
          onchange: (e) => save({ automation: { schedule_end: e.target.value } }),
        })))));

  return wrap;
}

/* --- boot -------------------------------------------------------------- */

$('#btn-scan').addEventListener('click', async () => {
  await api('/run/scan', { method: 'POST', body: {} });
  toast('Scanning your library.');
  poll();
});

$('#btn-pipeline').addEventListener('click', async () => {
  await api('/run/pipeline', { method: 'POST', body: {} });
  toast('Full pass started: scan, duplicates, metadata, format check.');
  poll();
});

$('.app[data-app="forge"]').addEventListener('click', () => {
  const url = state.status?.shell?.forge_url;
  if (url) { window.location.href = url; return; }
  toast('Add your Forge address in Settings and this will switch over to it.');
  location.hash = '#/settings';
});

window.addEventListener('unhandledrejection', (e) => {
  toast(e.reason?.message || 'That did not work.', true);
  e.preventDefault();
});

window.addEventListener('hashchange', route);
route();
poll();
setInterval(poll, 2500);
