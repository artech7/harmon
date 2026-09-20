/* Harmon front end. Vanilla, tab-switched, one file. */

const $ = (sel, root = document) => root.querySelector(sel);

/* Set a property on a node that might not be there. The browser caches HTML
   and JS separately, so the two can briefly disagree after a deploy; that is
   a reason to skip an update, not to throw on every poll. */
function setProp(sel, prop, value) {
  const node = typeof sel === 'string' ? $(sel) : sel;
  if (node) node[prop] = value;
  return node;
}
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

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

const state = { status: null, config: null, codecs: null, tab: 'overview' };

/* --- helpers ----------------------------------------------------------- */

async function api(path, options = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();

  // A static-file mount answers an unknown path with the page itself. Saying
  // "Unexpected token '<'" tells you nothing; saying the build is out of date
  // tells you exactly what to do.
  if (text.trimStart().startsWith('<')) {
    throw new Error(
      `The server has no ${path} endpoint. The page and the running build are ` +
      `different versions — redeploy, then reload.`);
  }

  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`${path} returned something that is not JSON.`);
  }
  if (!res.ok) throw new Error(data?.detail || res.statusText);
  return data;
}

const _recentToasts = new Map();

/* A modal, for the one case that genuinely interrupts. Returns a close
   function so the caller decides when it goes. */
function modal(title, lede, body, footer) {
  const box = el('div', { class: 'modal' },
    el('h3', {}, title),
    lede && el('p', { class: 'lede' }, lede),
    body,
    footer && el('div', { class: 'modal-foot' }, footer));

  const back = el('div', {
    class: 'backdrop',
    onclick: (e) => { if (e.target === back) close(); },
  }, box);

  const onKey = (e) => { if (e.key === 'Escape') close(); };
  function close() {
    back.remove();
    document.removeEventListener('keydown', onKey);
  }
  document.addEventListener('keydown', onKey);
  document.body.append(back);
  return close;
}

function toast(message, bad = false) {
  const host = $('#toasts');
  if (!host) return;

  // Background polling can fail on a timer. Showing the same message every
  // few seconds buries the screen without telling you anything new.
  const last = _recentToasts.get(message);
  if (last && Date.now() - last < 20000) return;
  _recentToasts.set(message, Date.now());

  const node = el('div', { class: 'toast' + (bad ? ' bad' : '') }, message);
  host.append(node);
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

const basename = (p) => (p || '').split('/').pop();

function elapsed(startedAt) {
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

const CODEC_COLOR = {
  FLAC: '#6FE3C4', ALAC: '#57C8E8', AAC: '#A98BFF', MP3: '#F2B03D',
  OPUS: '#E878C0', VORBIS: '#FF9D42', WAV: '#8CE0A0', WMA: '#7A8CA8',
};
const colorFor = (c) => CODEC_COLOR[c] || '#6F6A80';

/* Card and stat builders, so every panel speaks the same vocabulary. */
const card = (title, lede, ...body) =>
  el('section', { class: 'glass card' },
    title && el('h3', {}, title),
    lede && el('p', { class: 'lede' }, lede),
    ...body);

const stat = (label, value, note, tone) =>
  el('div', { class: 'glass stat' + (tone ? ' ' + tone : '') },
    el('div', { class: 'k' }, label),
    el('div', { class: 'v' }, value),
    note && el('div', { class: 'n' }, note));

const chip = (text, tone) => el('span', { class: 'chip' + (tone ? ' ' + tone : '') }, text);

/* Settings write as you go, which is only trustworthy if you can see it
   happen. Every text field confirms its own save and reports its own failure. */
function flashSaved(fieldNode, text = 'Saved', ok = true) {
  let note = fieldNode.querySelector('.saved-note');
  if (!note) {
    note = el('span', { class: 'saved-note chip' });
    (fieldNode.querySelector('label') || fieldNode).after(note);
  }
  note.textContent = text;
  note.className = 'saved-note chip ' + (ok ? 'good' : 'hot');
  note.style.opacity = '1';
  clearTimeout(note._timer);
  note._timer = setTimeout(() => { note.style.opacity = '0'; }, 2400);
}

function textField({ label, value, blurb, link, type = 'text', placeholder, save }) {
  const input = el('input', { type, value: value || '', placeholder });
  const field = el('div', { class: 'field' },
    el('label', {}, label),
    input,
    blurb && el('small', {}, blurb, link ? ' ' : '',
      link && el('a', { href: link.url, target: '_blank', rel: 'noopener' }, link.label)));

  let last = value || '';
  const commit = async () => {
    const next = input.value.trim();
    if (next === last) return;
    try {
      await save(next);
      last = next;
      flashSaved(field);
    } catch (err) {
      flashSaved(field, err.message || 'Not saved', false);
    }
  };

  input.addEventListener('change', commit);   // blur, or picking from autofill
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); commit(); }
  });
  field._commit = commit;
  return field;
}

const swRow = (title, blurb, checked, onchange) =>
  el('label', { class: 'sw' },
    el('input', { type: 'checkbox', checked: !!checked, onchange: (e) => onchange(e.target.checked) }),
    el('span', {}, el('b', {}, title), el('small', {}, blurb)));

/* --- themes ------------------------------------------------------------ */

const THEMES = [
  ['nightfall', 'Nightfall', '#241a44', 'A dark listening room'],
  ['vinyl', 'Vinyl', '#3a2411', 'A record under a desk lamp'],
  ['airwave', 'Airwave', '#12354f', 'A signal coming in over the air'],
  ['velvet', 'Velvet', '#3a1338', 'A club at two in the morning'],
];

function applyTheme(name) {
  document.documentElement.setAttribute('data-theme', name || 'nightfall');
  $$('#themepick button').forEach((b) => b.classList.toggle('on', b.dataset.theme === name));
}

function buildThemePicker() {
  const host = $('#themepick');
  host.innerHTML = '';
  THEMES.forEach(([key, label, swatch, blurb]) => {
    host.append(el('button', {
      'data-theme': key, title: `${label} — ${blurb}`, 'aria-label': label,
      style: `background:${swatch}`,
      onclick: async () => {
        applyTheme(key);
        await api('/config', { method: 'PUT', body: { shell: { theme: key } } });
      },
    }));
  });
}

/* --- tabs -------------------------------------------------------------- */

const TABS = {
  overview: viewOverview,
  duplicates: viewDuplicates,
  folders: viewFolders,
  metadata: viewMetadata,
  lyrics: viewLyrics,
  review: viewReview,
  settings: viewSettings,
};

const SUBTITLE = {
  overview: 'music library',
  duplicates: 'copies of the same song',
  folders: 'browse the library as it sits on disk',
  metadata: 'artists, albums, genres, artwork',
  lyrics: 'synced .lrc files, with plain text as backup',
  review: 'nothing is written until you approve it',
  settings: 'folders, sources, automation',
};

async function switchTab(name, animate = true) {
  if (!TABS[name]) name = 'overview';
  state.tab = name;
  location.hash = '#/' + name;
  $('#subtitle').textContent = SUBTITLE[name];
  $$('.toptab').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));

  const panels = $$('.tabpanel');
  panels.forEach((p) => {
    p.classList.remove('active', 'entering');
    if (p.dataset.panel === name) p.classList.add('active', ...(animate ? ['entering'] : []));
  });
  await render();
}

async function render() {
  const panel = $(`.tabpanel[data-panel="${state.tab}"]`);
  if (!panel) return;
  if (!panel.childElementCount) panel.append(el('div', { class: 'empty' }, 'Loading…'));
  try {
    const node = await TABS[state.tab]();
    panel.innerHTML = '';
    panel.append(node);
  } catch (err) {
    panel.innerHTML = '';
    panel.append(card('That panel could not load', err.message));
  }
}

/* --- status polling ---------------------------------------------------- */

let lastWorkerKind = null;

let pollFailures = 0;

async function poll() {
  try {
    state.status = await api('/status');
    pollFailures = 0;
  } catch {
    // The server may simply be restarting. Say something only once it is
    // clearly not coming back.
    if (++pollFailures === 8) toast('Lost contact with Harmon. Is the container running?', true);
    return;
  }

  try {
    paint(state.status);
  } catch (err) {
    if (++pollFailures === 3) {
      toast('The page and the server are running different versions. A hard refresh should fix it.', true);
    }
  }
}

function paint(s) {

  const w = s.worker;
  const busy = Boolean(w.kind);
  $('#dot').classList.toggle('live', busy);

  // Say what the job is, not just where it has got to. "Album 1371 of 1695"
  // on its own could be a scan, a lookup or a conversion.
  const label = w.cancelling ? 'Stopping…'
    : busy ? (w.label || w.kind) + (w.stage ? ` · ${w.stage}` : '')
    : 'Idle';
  const detail = busy
    ? [w.message, w.started_at ? elapsed(w.started_at) : null].filter(Boolean).join('  ·  ')
    : '';
  setProp('#job-label', 'textContent', label);
  setProp('#job-detail', 'textContent', detail);
  setProp('#job-detail', 'title', busy ? w.message : '');
  setProp('#worker-text', 'textContent', busy ? w.message : 'Idle');  // older markup
  // Throttling and benched sources explain a slow pass, so say so in the
  // header rather than leaving it to be inferred from the activity log.
  const src = s.sources || {};
  const notes = [];
  if (src.mb_backoff > 0.5) {
    notes.push(`MusicBrainz throttling: one request every ${(1 + src.mb_backoff).toFixed(0)}s`);
  }
  if (src.benched?.length) {
    notes.push(`Resting: ${src.benched.join(', ')}`);
  }
  let noteEl = $('#source-note');
  if (!noteEl) {
    const anchor = $('#job-detail') || $('#worker-text');
    if (anchor) {
      noteEl = el('span', { class: 'chip wait', id: 'source-note' });
      anchor.after(noteEl);
    }
  }
  if (noteEl) {
    noteEl.textContent = notes.join(' · ');
    noteEl.hidden = !notes.length;
  }

  setProp('#btn-stop', 'hidden', !busy);
  setProp('#btn-stop', 'disabled', Boolean(w.cancelling));
  const bar = $('#worker-bar');
  if (bar) bar.style.width = busy ? `${Math.round(w.progress * 100)}%` : '0%';
  setProp('#btn-scan', 'disabled', busy);
  setProp('#btn-pipeline', 'disabled', busy);

  setProp('#badge-review', 'textContent', s.changes.pending || '');
  setProp('#badge-lyrics', 'textContent', s.changes.by_kind?.lyrics || '');
  setProp('#badge-dupes', 'textContent',
    (s.duplicates.same_album || 0) + (s.duplicates.identical || 0) || '');


  if (lastWorkerKind && !s.worker.kind) {
    render();
    toast('Finished. Anything Harmon wants to change is waiting in Review.');
  }
  lastWorkerKind = s.worker.kind;
}

/* --- overview ---------------------------------------------------------- */

async function viewOverview() {
  const [s, activity, format, cfg] = await Promise.all([
    api('/status'), api('/activity?limit=25'), api('/standardize'), api('/config'),
  ]);
  state.status = s;
  state.config = cfg;
  const lib = s.library;
  const frag = document.createDocumentFragment();

  if (!s.libraries.length) {
    frag.append(card('Point Harmon at your music',
      'Add the folder your library lives in and Harmon reads every file, groups the duplicates and checks the tags. It changes nothing until you say so.',
      el('button', { class: 'go', onclick: () => switchTab('settings') }, 'Add a music folder')));
    return frag;
  }

  frag.append(el('div', { class: 'stats' },
    stat('Tracks', num(lib.tracks)),
    stat('Albums', num(lib.albums)),
    stat('Artists', num(lib.artists)),
    stat('On disk', bytes(lib.bytes)),
    stat('Playing time', hours(lib.seconds))));

  /* The library drawn as a band of codecs, widest format first. */
  const codecs = Object.entries(format.by_codec).sort((a, b) => b[1] - a[1]);
  const total = codecs.reduce((n, [, v]) => n + v, 0) || 1;

  frag.append(card('What your library is made of', null,
    el('div', { class: 'spectrum' },
      codecs.map(([name, count]) => {
        const pct = (count / total) * 100;
        return el('span', {
          style: `width:${pct}%;background:${colorFor(name)}`,
          title: `${name}: ${num(count)} files`,
        }, pct > 7 ? name : '');
      })),
    el('div', { class: 'spectrum-key' },
      codecs.map(([name, count]) => el('span', {},
        el('i', { style: `background:${colorFor(name)}` }), `${name} ${num(count)}`)))));

  const targetLabel = (state.config?.target?.codec || '').toUpperCase();
  frag.append(el('div', { class: 'stats' },
    stat('Duplicate sets', num(s.duplicates.same_album + s.duplicates.identical),
      s.reclaimable_bytes ? `${bytes(s.reclaimable_bytes)} to reclaim` : 'Nothing to clean up',
      s.duplicates.same_album ? 'hot' : 'good'),
    stat(targetLabel ? `Already ${targetLabel}` : 'Already on target',
      num(format.matching), 'Nothing to do', 'good'),
    stat('Protected', num(format.protected),
      'Lossless, or already below target'),
    stat('No artwork', num(lib.no_art), 'Harmon can fetch covers'),
    stat('No genre', num(lib.no_genre), 'Filled from Last.fm, Discogs, Spotify')));

  if (s.changes.pending) {
    frag.append(card(`${num(s.changes.pending)} changes are waiting on you`,
      'Harmon has staged these and written nothing. Look them over and approve what you want.',
      el('button', { class: 'go', onclick: () => switchTab('review') }, 'Review changes')));
  }

  if (!s.ffmpeg) {
    frag.append(card('ffmpeg is missing',
      'Scanning, duplicates and metadata all work without it, but Harmon cannot convert anything until ffmpeg is on the path inside the container.'));
  }

  frag.append(card('Recent activity', null,
    el('div', { class: 'log' },
      activity.length
        ? activity.map((a) => el('div', { class: a.level },
            el('time', {}, a.created_at.slice(5, 16)), el('span', {}, a.message)))
        : el('div', {}, 'Nothing has happened yet.'))));

  return frag;
}

/* --- duplicates -------------------------------------------------------- */

let dupeKind = 'same_album';

async function viewDuplicates() {
  const groups = await api('/dupes?kind=' + dupeKind + '&limit=300');

  const explain = {
    same_album: 'The same song more than once inside one album. These are the real duplicates — Harmon keeps the best copy and stages the rest for removal.',
    identical: 'Byte-for-byte identical files, wherever they sit. Safe to collapse to one.',
    cross_album: 'The same song on two different albums — a studio release and a compilation, say. Normal, and Harmon will not touch it. Shown so you can see it was checked.',
  }[dupeKind];

  const bar = el('div', { class: 'bar' },
    el('div', { class: 'segs' },
      [['same_album', 'Same album'], ['identical', 'Identical files'],
       ['cross_album', 'Across albums']].map(([key, label]) =>
        el('button', {
          class: 'sm' + (dupeKind === key ? ' on' : ''),
          onclick: () => { dupeKind = key; render(); },
        }, label))));

  if (dupeKind !== 'cross_album' && groups.length) {
    bar.append(el('div', { class: 'push' }));
    bar.append(el('button', {
      class: 'warn sm', onclick: async () => {
        const r = await api('/dupes/stage-all', { method: 'POST' });
        toast(`${r.staged} removals staged. Approve them in Review.`);
        render(); poll();
      },
    }, 'Stage every removal'));
  }

  const body = groups.length
    ? el('div', { class: 'rows' }, groups.map(dupeGroup))
    : el('div', { class: 'empty' },
        el('b', {}, 'Nothing here'),
        dupeKind === 'cross_album'
          ? 'No songs appear on more than one album.'
          : 'No duplicates of this kind. Run a scan if you have added music recently.');

  return card('Duplicates', explain, bar, body);
}

function dupeGroup(g) {
  const body = el('div', { class: 'gbody', style: 'display:none' });
  let loaded = false;

  const head = el('div', {
    class: 'ghead',
    onclick: async () => {
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : 'flex';
      if (!open && !loaded) { loaded = true; await fillGroup(g.id, body); }
    },
  },
    el('div', {},
      el('div', { class: 'rname' }, g.title || 'Untitled'),
      el('div', { class: 'rmeta' }, g.artist || 'Unknown artist')),
    chip(`${g.copies} copies`),
    g.kind === 'cross_album' ? chip('Left alone') : chip(`${bytes(g.reclaimable)} to reclaim`, 'hot'),
    chip('Show copies'));

  return el('div', { class: 'group' }, head, body);
}

async function fillGroup(groupId, body) {
  body.innerHTML = '';
  const detail = await api('/dupes/' + groupId);
  const readOnly = detail.kind === 'cross_album';

  // Whether the copies share a folder is the thing worth knowing before
  // deleting one, so say it plainly rather than leaving it to be inferred
  // from the paths underneath.
  if (!readOnly) {
    body.append(el('div', { class: 'bar', style: 'margin:0 0 4px' },
      detail.same_folder
        ? chip('All copies are in one folder', 'good')
        : chip(`Spread across ${detail.folders.length} folders — check before deleting`, 'hot')));
  }

  detail.members.forEach((m) => {
    body.append(el('div', { class: 'copy' + (m.keeper ? ' keeper' : '') },
      readOnly ? el('span', {}) : el('input', {
        type: 'radio', name: 'keep-' + groupId, checked: !!m.keeper,
        onchange: async () => {
          await api(`/dupes/${groupId}/keeper/${m.id}`, { method: 'POST' });
          await fillGroup(groupId, body);
        },
      }),
      el('div', {},
        el('div', { class: 'rname' },
          `${(m.codec || '?').toUpperCase()} · ${m.bitrate || '?'} kbps · ${bytes(m.size)}`),
        el('div', { class: 'rmeta' },
          [m.album || 'No album tag',
           m.disc_no ? `disc ${m.disc_no}` : null,
           m.duration ? `${Math.round(m.duration)}s` : null].filter(Boolean).join('  ·  ')),
        el('div', { class: 'rpath' }, m.path),
        el('button', {
          class: 'jump', onclick: () => openFolderFor(m.id),
        }, 'Open this folder')),
      chip(m.reason, m.keeper ? 'good' : null)));
  });

  if (!readOnly) {
    body.append(el('div', { class: 'bar', style: 'margin:10px 0 0' },
      el('button', {
        class: 'warn sm', onclick: async () => {
          const r = await api(`/dupes/${groupId}/stage`, { method: 'POST' });
          toast(`${r.staged} removals staged. Nothing happens until approved.`);
          poll();
        },
      }, 'Stage the other copies for removal'),
      el('span', { class: 'rmeta' },
        'Removed files move to your originals folder, not the bin. Byte-identical copies are read in full and compared before anything is deleted.')));
  }
}

/* --- lyrics ------------------------------------------------------------ */

let lyricsState = 'none';

async function viewLyrics() {
  const cov = await api('/lyrics');
  const frag = document.createDocumentFragment();

  if (cov.unknown === cov.total && cov.total) {
    frag.append(card('Check what you already have',
      'Harmon has not looked for lyrics yet. This reads each track for a .lrc or .txt beside it, and for lyrics stored in the tags, so nothing already on disk gets downloaded twice.',
      el('button', {
        class: 'go', onclick: async () => {
          await api('/run/lyrics_scan', { method: 'POST', body: {} });
          toast('Checking every track for lyrics.'); poll();
        },
      }, 'Scan for lyrics')));
    return frag;
  }

  const pct = (n) => cov.total ? Math.round((n / cov.total) * 100) : 0;
  frag.append(el('div', { class: 'stats' },
    stat('Synced', num(cov.synced), `${pct(cov.synced)}% — .lrc, scrolls in time`,
      cov.synced ? 'good' : null),
    stat('Plain text only', num(cov.unsynced), `${pct(cov.unsynced)}% — a .lrc would improve these`,
      cov.unsynced ? 'hot' : null),
    stat('No lyrics', num(cov.none), `${pct(cov.none)}% of the library`,
      cov.none ? 'hot' : 'good'),
    cov.unknown ? stat('Not checked', num(cov.unknown), 'run the scan again') : null));

  frag.append(card('Fetch from LRCLIB',
    'LRCLIB is a free, open lyrics database — no key, no account. Harmon matches on artist, title, album and length, so a radio edit does not get the album version\'s timings. Files are written next to each track and nothing touches the audio itself. Anything found is staged in Review first.',
    el('div', { class: 'bar' },
      el('button', {
        class: 'go', disabled: !(cov.none + cov.unsynced),
        onclick: async () => {
          await api('/run/lyrics', { method: 'POST', body: {} });
          toast('Looking up lyrics. This runs in the background — you can leave the page.');
          poll();
        },
      }, `Look up ${num(cov.none + cov.unsynced)} tracks`),
      el('button', {
        class: 'sm', onclick: async () => {
          await api('/run/lyrics', { method: 'POST', body: { limit: 50 } });
          toast('Trying 50 tracks first.'); poll();
        },
      }, 'Try 50 first'),
      el('button', {
        class: 'sm', onclick: async () => {
          await api('/run/lyrics_scan', { method: 'POST', body: {} });
          toast('Re-checking what is on disk.'); poll();
        },
      }, 'Re-scan what I have'),
      cov.staged ? chip(`${num(cov.staged)} waiting in Review`, 'wait') : null)));

  const listHost = el('div', { class: 'rows' });
  const tabs = el('div', { class: 'segs pills' },
    [['none', 'No lyrics'], ['unsynced', 'Plain text only'], ['synced', 'Synced']]
      .map(([key, label]) => el('button', {
        class: 'sm' + (lyricsState === key ? ' on' : ''),
        onclick: () => { lyricsState = key; render(); },
      }, `${label} (${num(cov[key])})`)));

  const data = await api(`/lyrics/tracks?state=${lyricsState}&limit=100`);
  if (!data.items.length) {
    listHost.append(el('div', { class: 'empty' }, 'Nothing in this group.'));
  }
  data.items.forEach((t) => listHost.append(
    el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto' },
      el('div', {},
        el('div', { class: 'rname' }, t.title || basename(t.path)),
        el('div', { class: 'rmeta' },
          [t.album_artist || t.artist, t.album].filter(Boolean).join(' — '))),
      el('button', {
        class: 'jump', onclick: () => openFolderFor(t.id),
      }, 'Show folder'),
      el('button', {
        class: 'sm', onclick: async (e) => {
          e.target.disabled = true;
          e.target.textContent = 'Looking…';
          await api('/run/lyrics', { method: 'POST', body: { track_ids: [t.id] } });
          setTimeout(() => { toast('Checked. Anything found is in Review.'); poll(); }, 1200);
        },
      }, 'Look this one up'))));

  frag.append(card('Tracks',
    `Showing ${num(data.items.length)} of ${num(data.total)}.`,
    el('div', { class: 'bar' }, tabs), listHost));

  return frag;
}

/* --- folders ----------------------------------------------------------- */

let folderPath = null;

/* Jump to the folder a track lives in, from anywhere in the app. */
async function openFolderFor(trackId) {
  const r = await api('/browse/for-track/' + trackId);
  folderPath = r.path;
  switchTab('folders');
}

function openFolder(path) {
  folderPath = path;
  if (state.tab === 'folders') render(); else switchTab('folders');
}

async function viewFolders() {
  if (!folderPath) {
    const { roots } = await api('/browse');
    if (!roots.length) {
      return card('No folders yet',
        'Add a music folder under Settings and run a scan, then you can browse it here.');
    }
    if (roots.length === 1) { folderPath = roots[0].path; return viewFolders(); }
    return card('Your libraries', 'Pick one to look inside.',
      el('div', { class: 'rows' }, roots.map((r) =>
        el('div', { class: 'entry', onclick: () => openFolder(r.path) },
          el('span', { class: 'ico' }, '▸'),
          el('div', {}, el('div', { class: 'nm' }, r.name),
             el('div', { class: 'sub' }, r.path)),
          chip(`${num(r.tracks)} tracks`),
          el('span', { class: 'sub' }, bytes(r.bytes))))));
  }

  let r;
  try {
    r = await api('/browse?path=' + encodeURIComponent(folderPath));
  } catch (err) {
    folderPath = null;
    return card('That folder is no longer there', err.message,
      el('button', { onclick: () => { folderPath = null; render(); } }, 'Back to the top'));
  }

  const crumbs = el('div', { class: 'crumbs' });
  r.crumbs.forEach((c, i) => {
    if (i) crumbs.append(el('span', {}, '/'));
    crumbs.append(el('button', { class: 'sm', onclick: () => openFolder(c.path) }, c.name));
  });

  const rows = el('div', { class: 'rows' });

  r.folders.forEach((f) => rows.append(
    el('div', { class: 'entry', onclick: () => openFolder(f.path) },
      el('span', { class: 'ico' }, '▸'),
      el('div', {}, el('div', { class: 'nm' }, f.name)),
      chip(`${num(f.tracks)} tracks`),
      el('span', { class: 'sub' }, bytes(f.bytes)))));

  r.tracks.forEach((t) => rows.append(
    el('div', { class: 'entry file' },
      el('span', { class: 'ico' }, '♪'),
      el('div', {},
        el('div', { class: 'nm' }, t.title || t.name),
        el('div', { class: 'sub' },
          [t.name,
           [t.artist, t.album].filter(Boolean).join(' — '),
           t.year, t.genre].filter(Boolean).join('  ·  '))),
      el('span', { class: 'sub' },
        `${(t.codec || '?').toUpperCase()} ${t.bitrate || '?'}k · ${bytes(t.size)}`),
      el('div', { style: 'display:flex;gap:6px' },
        t.has_art ? null : chip('no art', 'hot'),
        t.pending ? chip(`${t.pending} staged`, 'wait') : null))));

  if (!r.folders.length && !r.tracks.length) {
    rows.append(el('div', { class: 'empty' }, 'Nothing indexed in this folder.'));
  }

  return card(null, null, crumbs,
    el('div', { class: 'bar' },
      r.parent
        ? el('button', { class: 'sm', onclick: () => openFolder(r.parent) }, 'Up one level')
        : null,
      chip(`${num(r.total_tracks)} tracks here and below`),
      chip(bytes(r.total_bytes)),
      el('div', { class: 'push' }),
      el('span', { class: 'sub' }, r.path)),
    rows);
}

/* --- metadata ---------------------------------------------------------- */

let onlyProblems = true;

const mins = (seconds) => seconds < 90 ? `${Math.round(seconds)}s`
  : seconds < 5400 ? `${Math.round(seconds / 60)} min`
  : `${(seconds / 3600).toFixed(1)} hours`;

async function viewMetadata() {
  const [tracks, staged, plan] = await Promise.all([
    api(`/tracks?limit=150&problems=${onlyProblems}`),
    api('/changes?status=pending&kind=tag&limit=1'),
    api('/albums/plan'),
  ]);

  const bar = el('div', { class: 'bar' },
    el('label', { class: 'rmeta', style: 'display:flex;gap:8px;align-items:center;cursor:pointer' },
      el('input', {
        type: 'checkbox', checked: onlyProblems,
        onchange: (e) => { onlyProblems = e.target.checked; render(); },
      }),
      'Only tracks with something missing'),
    el('div', { class: 'push' }),
    staged.counts.by_kind?.tag ? chip(`${staged.counts.by_kind.tag} waiting in review`, 'wait') : null,
    el('button', {
      class: 'go', onclick: async () => {
        await api('/run/enrich', { method: 'POST', body: {} });
        toast('Looking things up. MusicBrainz limits Harmon to one track a second.');
        poll();
      },
    }, 'Look up everything not yet checked'));

  const body = tracks.length
    ? el('div', { class: 'rows' }, tracks.map((t) => {
        const gaps = [];
        if (!t.artist) gaps.push('artist');
        if (!t.album) gaps.push('album');
        if (!t.genre) gaps.push('genre');
        if (!t.has_art) gaps.push('artwork');
        return el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto' },
          el('div', {},
            el('div', { class: 'rname' }, t.title || basename(t.path)),
            el('div', { class: 'rmeta' },
              [t.album_artist || t.artist || 'Unknown artist', t.album || 'No album'].join(' — '))),
          gaps.length ? chip('Missing ' + gaps.join(', '), 'hot') : chip('Complete', 'good'),
          el('button', {
            class: 'sm', onclick: async (e) => {
              e.target.disabled = true;
              e.target.textContent = 'Looking up…';
              await api('/run/enrich', { method: 'POST', body: { track_ids: [t.id] } });
              setTimeout(() => { toast('Suggestions staged for this track.'); poll(); }, 1500);
            },
          }, 'Look this one up'));
      }))
    : el('div', { class: 'empty' },
        el('b', {}, 'Every track has what it needs'),
        'Artist, album, genre and artwork are all filled in.');

  /* The same artist is usually scattered across several stray genres, so
     fixing one leaves the rest behind. Offer the rest — but per artist,
     since one stray genre can hold acts that belong nowhere near each other. */
  async function offerConsolidation(value, target) {
    const d = await api(`/genres/artists-elsewhere?value=${encodeURIComponent(value)}` +
                        `&target=${encodeURIComponent(target)}`);
    if (!d.artists.length) return;

    const rows = el('div', { class: 'rows' });
    const done = new Set();

    d.artists.forEach((a) => {
      const status = el('span', { class: 'rmeta' },
        a.pinned ? `already set to ${a.pinned}` : '');
      const apply = el('button', {
        class: 'go sm', onclick: async () => {
          apply.disabled = true;
          const r = await api('/genres/assign-artist', {
            method: 'POST', body: { artist: a.artist, genre: target },
          });
          done.add(a.artist);
          status.textContent = `${num(r.staged)} tracks staged as ${target}`;
          status.className = 'rmeta';
          skip.disabled = true;
          poll();
        },
      }, 'Move them');
      const skip = el('button', {
        class: 'sm', onclick: () => {
          apply.disabled = true; skip.disabled = true;
          status.textContent = 'left where they are';
        },
      }, 'Leave');

      rows.append(el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto' },
        el('div', {},
          el('div', { class: 'rname' }, a.artist),
          el('div', { class: 'rmeta', style: 'white-space:normal' },
            a.elsewhere.map((o) => `${o.genre} (${num(o.tracks)})`).join('  ·  ')),
          el('div', { class: 'rmeta' },
            `${num(a.tracks_elsewhere)} tracks under other genres`),
          status),
        apply, skip));
    });

    await new Promise((resolve) => {
      const close = modal(
        `These artists are scattered across other genres`,
        `You just sent ${value} to ${target}. The artists that were in it also have ` +
        `tracks filed under other genres. Move each one's whole catalogue to ${target}, ` +
        `or leave it — they may not all belong in the same place. Nothing is written ` +
        `until you approve it in Review.`,
        rows,
        [el('button', { class: 'go', onclick: () => { close(); resolve(); } }, 'Done'),
         el('span', { class: 'rmeta' },
           'Moving an artist also pins them, so later lookups will not change it back.')]);
    });
  }

  /* Alphabetical by default, because that is how you find a genre. Count
     order stays available, because that is how you find the long tail. */
  let genreSort = 'name';

  const genreBook = el('div', {});
  async function paintGenres() {
    genreBook.innerHTML = '';
    genreBook.append(el('div', { class: 'empty' }, 'Reading your genres…'));
    const d = await api('/genres');
    genreBook.innerHTML = '';

    genreBook.append(el('div', { class: 'stats' },
      stat('Genres in use', num(d.distinct),
        `${num(d.one_offs)} used by a single track`, d.one_offs ? 'hot' : null),
      stat('After tidying', num(d.after.length), 'from your list', 'good'),
      stat('Cannot be placed', num(d.unmapped.length),
        d.unmapped.length ? 'these need you' : 'nothing left over',
        d.unmapped.length ? 'hot' : 'good')));

    if (d.unmapped.length) {
      const rows = el('div', { class: 'rows' });
      d.unmapped.slice(0, 40).forEach((u) => {
        const pick = el('select', {},
          el('option', { value: '' }, 'Send this to…'),
          d.vocabulary.map((g) => el('option', { value: g }, g)));

        // Who carries this tag is the thing that tells you where it belongs,
        // so it has to be visible before you decide, not after.
        const inside = el('div', { class: 'gbody', style: 'display:none' });
        let loaded = false;
        const reveal = async () => {
          const open = inside.style.display !== 'none';
          inside.style.display = open ? 'none' : 'flex';
          if (open || loaded) return;
          loaded = true;
          inside.innerHTML = '';
          inside.append(el('div', { class: 'empty' }, 'Loading…'));
          const detail = await api('/genres/tracks?value=' + encodeURIComponent(u.value));
          inside.innerHTML = '';
          inside.append(el('div', { class: 'bar', style: 'margin:0 0 4px' },
            ...detail.artists.map((a) => chip(`${a.artist} · ${num(a.tracks)}`))));
          detail.tracks.forEach((t) => inside.append(
            el('div', { class: 'copy', style: 'grid-template-columns:1fr auto' },
              el('div', {},
                el('div', { class: 'rname' }, t.title || basename(t.path)),
                el('div', { class: 'rmeta' },
                  [t.album_artist || t.artist, t.album].filter(Boolean).join(' — '))),
              el('button', {
                class: 'jump', onclick: () => openFolderFor(t.id),
              }, 'Show folder'))));
          if (detail.total > detail.tracks.length) {
            inside.append(el('div', { class: 'rmeta', style: 'padding:6px 2px' },
              `${num(detail.total - detail.tracks.length)} more not shown.`));
          }
        };

        rows.append(el('div', {},
          el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto auto auto' },
          el('div', { style: 'cursor:pointer', onclick: reveal },
            el('div', { class: 'rname' }, u.value),
            el('div', { class: 'rmeta' },
              `${num(u.tracks)} tracks, ${num(u.artists)} artists — tap to see them`)),
          el('button', { class: 'sm', onclick: reveal }, 'Show'),
          pick,
          el('button', {
            class: 'sm', onclick: async () => {
              if (!pick.value) { toast('Pick a genre to send it to first.'); return; }
              const target = pick.value;
              await api('/genres/map', { method: 'POST', body: { from: u.value, to: target } });
              toast(`${u.value} now becomes ${target}.`);
              await offerConsolidation(u.value, target);
              paintGenres();
            },
          }, 'Map it'),
          el('button', {
            class: 'sm', onclick: async () => {
              await api('/genres/custom', { method: 'POST', body: { name: u.value } });
              toast(`${u.value} is now one of your genres.`);
              paintGenres();
            },
          }, 'Keep as its own')),
          inside));
      });
      genreBook.append(card('Genres Harmon cannot place',
        'These are not on the list. Send each one to a genre you already have, or keep it as a genre of its own.',
        rows));
    }

    const newGenre = el('input', { type: 'text', placeholder: 'Shoegaze Revival' });
    genreBook.append(card('Your list',
      `${num(d.vocabulary.length)} genres. Everything gets mapped onto one of these, and one genre is written per artist — writing several per track is what turns a short list into hundreds of one-offs.`,
      el('div', { class: 'bar' },
        d.after.map((a) => chip(`${a.genre} · ${num(a.tracks)}`, 'good'))),
      el('details', { style: 'margin-top:14px' },
        el('summary', { class: 'rmeta', style: 'cursor:pointer' },
          `Every value currently in your library (${num(d.items.length)})`),
        el('div', { class: 'bar', style: 'margin:12px 0 4px' },
          el('div', { class: 'segs' },
            el('button', {
              class: 'sm' + (genreSort === 'name' ? ' on' : ''),
              onclick: () => { genreSort = 'name'; paintGenres(); },
            }, 'A\u2013Z'),
            el('button', {
              class: 'sm' + (genreSort === 'tracks' ? ' on' : ''),
              onclick: () => { genreSort = 'tracks'; paintGenres(); },
            }, 'Most tracks'))),
        el('div', { class: 'rows', style: 'margin-top:8px' },
          [...d.items].sort((x, y) => genreSort === 'tracks'
            ? y.tracks - x.tracks
            : x.value.localeCompare(y.value, undefined, { sensitivity: 'base' })
          ).map((it) => {
            const inside = el('div', { class: 'gbody', style: 'display:none' });
            let loaded = false;
            const reveal = async () => {
              const open = inside.style.display !== 'none';
              inside.style.display = open ? 'none' : 'flex';
              if (open || loaded) return;
              loaded = true;
              inside.append(el('div', { class: 'empty' }, 'Loading…'));
              const detail = await api('/genres/tracks?value=' + encodeURIComponent(it.value));
              inside.innerHTML = '';
              inside.append(el('div', { class: 'bar', style: 'margin:0' },
                ...detail.artists.map((a) => chip(`${a.artist} · ${num(a.tracks)}`))));
              detail.tracks.slice(0, 20).forEach((t) => inside.append(
                el('div', { class: 'copy', style: 'grid-template-columns:1fr auto' },
                  el('div', {},
                    el('div', { class: 'rname' }, t.title || basename(t.path)),
                    el('div', { class: 'rmeta' },
                      [t.album_artist || t.artist, t.album].filter(Boolean).join(' — '))),
                  el('button', { class: 'jump', onclick: () => openFolderFor(t.id) },
                    'Show folder'))));
            };
            return el('div', {},
              el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto;cursor:pointer',
                          onclick: reveal },
                el('div', {},
                  el('div', { class: 'rname' }, it.value),
                  el('div', { class: 'rmeta' },
                    it.becomes && it.becomes !== it.value
                      ? `becomes ${it.becomes}`
                      : it.becomes ? 'already on your list' : 'cannot be placed')),
                chip(`${num(it.tracks)} tracks`),
                chip(`${num(it.artists)} artists`)),
              inside);
          }))),
      el('div', { class: 'grid2', style: 'margin-top:16px' },
        el('div', { class: 'field' },
          el('label', {}, 'Add a genre of your own'),
          newGenre,
          el('small', {}, 'Yours take priority over the built-in list.'))),
      el('button', {
        class: 'sm', onclick: async () => {
          if (!newGenre.value.trim()) return;
          await api('/genres/custom', { method: 'POST', body: { name: newGenre.value } });
          toast(`Added ${newGenre.value.trim()}.`);
          paintGenres();
        },
      }, 'Add it')));

    genreBook.append(card('Tidy up what you have',
      'Reads the genres already on your files, puts each onto your list, and stages the differences in Review. Values that cannot be placed are left alone rather than guessed at.',
      el('div', { class: 'bar' },
        el('button', {
          class: 'go', onclick: async () => {
            const r = await api('/genres/cleanup', { method: 'POST', body: {} });
            toast(`${num(r.staged)} genre changes staged` +
                  (r.unplaced ? `, ${num(r.unplaced)} left alone.` : '.'));
            paintGenres(); poll();
          },
        }, 'Stage the cleanup'),
        el('button', {
          class: 'warn sm', onclick: async () => {
            if (!confirm('Clear cached genres and any staged genre changes, so the next lookup starts fresh?')) return;
            const r = await api('/genres/reset', { method: 'POST', body: {} });
            toast(`Cleared ${num(r.staged_cleared)} staged and ${num(r.cached_cleared)} cached.`);
            paintGenres(); poll();
          },
        }, 'Start genres over'))));
  }
  paintGenres();

  const genreCard = card('Genres',
    'One genre per artist, drawn from a fixed list. Sources disagree about spelling and specificity, so everything they return gets mapped onto your list before it is written.',
    genreBook);

  const hygieneOut = el('div', {});
  let allowComma = false;

  const hygieneCard = card('Tidy up artist names',
    'Filename-style names like 3_Doors_Down, and collaborations written into the album artist, are what shatter a library into hundreds of one-album artists. This reshapes what is already in your tags — it invents nothing, and changes nothing until you approve it in Review.',
    el('div', { class: 'bar' },
      el('label', { class: 'rmeta', style: 'display:flex;gap:8px;align-items:center;cursor:pointer' },
        el('input', {
          type: 'checkbox',
          onchange: (e) => { allowComma = e.target.checked; },
        }),
        'Also treat commas as separators (risky: Earth, Wind & Fire)'),
      el('div', { class: 'push' }),
      el('button', {
        onclick: async (e) => {
          e.target.disabled = true;
          hygieneOut.innerHTML = '';
          hygieneOut.append(el('div', { class: 'empty' }, 'Checking every track…'));
          const r = await api(`/hygiene/preview?allow_comma=${allowComma}&limit=120`);
          hygieneOut.innerHTML = '';
          if (!r.total_changes) {
            hygieneOut.append(el('div', { class: 'empty' },
              el('b', {}, 'Your artist names are already tidy'),
              'Nothing to reshape.'));
            e.target.disabled = false;
            return;
          }
          hygieneOut.append(el('div', { class: 'stats' },
            stat('Artists now', num(r.artists_before)),
            stat('After cleanup', num(r.artists_after), null, 'good'),
            stat('Changes', num(r.total_changes), `across ${num(r.tracks)} tracks`)));
          hygieneOut.append(el('div', { class: 'rows' }, r.items.map((it) =>
            el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto' },
              el('div', {},
                el('div', { class: 'diff' },
                  el('span', { class: 'rmeta' }, it.field.replace('_', ' ')),
                  it.old ? el('s', {}, it.old) : el('span', { class: 'rmeta' }, '(empty)'),
                  el('em', {}, it.new)),
                el('div', { class: 'rmeta' }, it.reason)),
              chip(`${it.tracks} tracks`),
              el('span', { class: 'chip mono' }, `${Math.round(it.confidence * 100)}%`)))));
          hygieneOut.append(el('div', { class: 'bar', style: 'margin:16px 0 0' },
            el('button', {
              class: 'go', onclick: async () => {
                const res = await api('/hygiene/stage', {
                  method: 'POST', body: { allow_comma: allowComma },
                });
                toast(`${res.staged} name changes staged. Approve them in Review.`);
                poll();
              },
            }, 'Stage these for review')));
          e.target.disabled = false;
        },
      }, 'Show me what would change')),
    hygieneOut);

  const frag = document.createDocumentFragment();

  if (plan.albums || plan.loose_tracks) {
    frag.append(card('The next metadata pass',
      'Harmon looks albums up a whole tracklist at a time, which is two requests per album instead of one per track. Only files that do not belong to a recognisable album get checked individually.',
      el('div', { class: 'stats' },
        stat('Albums to check', num(plan.albums), `${num(plan.album_tracks)} tracks`),
        stat('Single tracks', num(plan.loose_tracks), 'checked one by one'),
        stat('Estimated time', mins(plan.seconds_batched),
          `one at a time would be ${mins(plan.seconds_per_track)}`, 'good')),
      plan.fingerprinting
        ? el('div', { class: 'rmeta' },
            'Fingerprinting is on, so files with unusable tags get identified by their audio.')
        : el('div', { class: 'rmeta' },
            'Add an AcoustID key in Settings to identify files whose tags are too poor to match on.')));
  }

  frag.append(genreCard);
  frag.append(hygieneCard);
  frag.append(card('Tag and artwork cleanup',
    'Harmon asks MusicBrainz, Discogs, Last.fm and Spotify in the order you set, takes the first answer for each field, and stages what it would change. Files stay untouched until you approve.',
    bar, body));
  return frag;
}

/* --- format ------------------------------------------------------------ */

/* The format settings, built here so Settings can host them. What used to be
   the Format tab was two different things wearing one hat: the settings that
   decide the target, and the status of the library against it. Those belong in
   Settings and on Overview respectively. */
async function formatSettingsCard() {
  const [cfg, codecs] = await Promise.all([api('/config'), api('/codecs')]);
  state.config = cfg;
  const target = cfg.target;
  const spec = codecs[target.codec];

  const save = async (patch) => {
    state.config = await api('/config', { method: 'PUT', body: { target: patch } });
    render();
  };

  const codecOnly = target.bitrate_mode === 'codec_only';
  const controls = el('div', { class: 'grid2', style: 'margin-top:18px' });

  if (spec.bitrates.length && !codecOnly) {
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

  return card('The format you want everything in',
    'New music is checked against this automatically, converted, and swapped in — the same way Forge handles video.',
    el('div', { class: 'codecs' },
      Object.entries(codecs).map(([key, c]) => el('button', {
        class: 'codec' + (key === target.codec ? ' on' : ''),
        onclick: () => save({ codec: key, bitrate: c.bitrates.at(-2) || target.bitrate }),
      }, el('b', {}, c.label), el('small', {}, c.blurb)))),
    spec.lossless ? null : el('div', { style: 'margin-top:20px' },
      el('h2', {}, 'What counts as needing conversion'),
      el('div', { class: 'codecs' },
        el('button', {
          class: 'codec' + (codecOnly ? '' : ' on'),
          onclick: () => save({ bitrate_mode: 'fixed' }),
        }, el('b', {}, 'Codec and bitrate'),
           el('small', {}, `Everything ends up ${spec.label} at one bitrate you choose. Files already at it are left alone; files above it are re-encoded down.`)),
        el('button', {
          class: 'codec' + (codecOnly ? ' on' : ''),
          onclick: () => save({ bitrate_mode: 'codec_only' }),
        }, el('b', {}, 'Codec only'),
           el('small', {}, `Anything already ${spec.label} is left exactly as it is, whatever its bitrate. Everything else is converted at ${spec.best_bitrate} kbps, the highest this codec goes.`))),
      codecOnly
        ? el('div', { class: 'rmeta', style: 'margin-top:12px;white-space:normal' },
            `Converting at ${spec.best_bitrate} kbps means a 128 kbps source produces a much larger file without sounding better — the bits come from the encoder, not the music. That is the trade for touching each file once and never revisiting bitrate. You can switch to the other mode later and re-run.`)
        : null),
    controls,
    swRow('Convert lossless files too',
      'Off by default, so your FLAC and ALAC rips are never turned lossy by accident.',
      target.convert_lossless, (v) => save({ convert_lossless: v })),
    swRow('Leave files already below the target bitrate',
      'Re-encoding a 128 kbps file at 256 cannot add back what is missing — it only makes the file bigger.',
      target.skip_if_lower_bitrate, (v) => save({ skip_if_lower_bitrate: v })),
    swRow('Keep the original after converting',
      'Sources move to your originals folder instead of being deleted, so a bad conversion is one file move from being undone.',
      target.keep_originals, (v) => save({ keep_originals: v })),
    textField({
      label: 'Originals folder',
      value: target.originals_path,
      blurb: 'Replaced sources and removed duplicates both land here.',
      save: async (v) => {
        state.config = await api('/config', { method: 'PUT', body: { target: { originals_path: v } } });
      },
    }));
}

/* --- review ------------------------------------------------------------ */

let reviewKind = null;
let reviewMode = 'grouped';
let reviewStatus = 'pending';
const selected = new Set();

/* A group described the way you would say it out loud, so the decision is
   about the kind of change rather than about thirteen thousand rows. */
function describeGroup(g) {
  const n = num(g.n);
  const FIELD = {
    year: 'release year', track_no: 'track number', disc_no: 'disc number',
    album_artist: 'album artist', artist: 'artist', album: 'album',
    title: 'title', genre: 'genre',
  };
  const field = FIELD[g.field] || g.field;
  const allBlank = g.filling_blanks === g.n;
  const someBlank = g.filling_blanks > 0 && !allBlank;

  if (g.kind === 'lyrics') {
    return g.field === 'synced'
      ? `Add synced lyrics (.lrc) to ${n} tracks`
      : `Add plain lyrics (.txt) to ${n} tracks that have none`;
  }
  if (g.kind === 'delete') return `Remove ${n} duplicate files`;
  if (g.kind === 'convert') return `Convert ${n} files to your target format`;
  if (g.kind === 'art') return `Embed artwork on ${n} tracks that have none`;
  if (allBlank) return `Add a ${field} to ${n} tracks that have none`;
  if (someBlank) return `Set the ${field} on ${n} tracks (${num(g.filling_blanks)} are blank)`;
  return `Change the ${field} on ${n} tracks`;
}

const SOURCE_NAME = {
  'musicbrainz-album': 'the album lookup', musicbrainz: 'MusicBrainz',
  lastfm: 'Last.fm', discogs: 'Discogs', spotify: 'Spotify', acoustid: 'AcoustID',
  coverartarchive: 'Cover Art Archive', 'name-cleanup': 'name cleanup',
  'duplicate-scan': 'the duplicate scan', standardization: 'the format check',
};

const BAND = {
  high: ['Very sure', 'good'], good: ['Fairly sure', 'wait'], low: ['Unsure', 'hot'],
};

async function viewReview() {
  return reviewMode === 'grouped' ? reviewGrouped() : reviewFlat();
}

function reviewModeBar(counts) {
  return el('div', {},
    el('div', { class: 'bar' },
      el('div', { class: 'segs pills' },
        el('button', {
          class: 'sm' + (reviewStatus === 'pending' ? ' on' : ''),
          onclick: () => { reviewStatus = 'pending'; render(); },
        }, `Waiting${counts.pending ? ` (${num(counts.pending)})` : ''}`),
        el('button', {
          class: 'sm' + (reviewStatus === 'approved' ? ' on' : ''),
          onclick: () => { reviewStatus = 'approved'; render(); },
        }, `Approved${counts.approved ? ` (${num(counts.approved)})` : ''}`),
        el('button', {
          class: 'sm' + (reviewStatus === 'rejected' ? ' on' : ''),
          onclick: () => { reviewStatus = 'rejected'; render(); },
        }, `Skipped${counts.rejected ? ` (${num(counts.rejected)})` : ''}`)),
      el('div', { class: 'push' }),
      el('div', { class: 'segs' },
        el('button', {
          class: 'sm' + (reviewMode === 'grouped' ? ' on' : ''),
          onclick: () => { reviewMode = 'grouped'; render(); },
        }, 'By kind'),
        el('button', {
          class: 'sm' + (reviewMode === 'flat' ? ' on' : ''),
          onclick: () => { reviewMode = 'flat'; render(); },
        }, 'Every change'))),
    el('div', { class: 'bar' },
      counts.failed ? chip(`${num(counts.failed)} failed`, 'hot') : null,
      el('div', { class: 'push' }),
      el('button', {
        class: 'go', disabled: !counts.approved,
        onclick: async () => {
          if (!confirm(
              `Write ${num(counts.approved)} approved changes to your files?\n\n` +
              `This is the point of no return — everything before it is reversible. ` +
              `Removed files move to your originals folder, and converted sources are ` +
              `kept there too if that setting is on.`)) return;
          await api('/run/apply', { method: 'POST', body: {} });
          toast('Writing approved changes to your files.'); poll();
        },
      }, `Apply ${num(counts.approved || 0)} approved`)));
}

/* The samples answer "what does this look like". This answers "show me every
   one", because approving 900 changes sight-unseen is not a review. */
async function showAllInGroup(g, host) {
  host.style.display = 'flex';
  host.innerHTML = '';
  host.append(el('div', { class: 'empty' }, 'Loading…'));

  const query = new URLSearchParams({ kind: g.kind, band: g.band, limit: '300' });
  if (g.field) query.set('field', g.field);
  if (g.source) query.set('source', g.source);
  const r = await api('/changes/group-items?' + query);

  host.innerHTML = '';
  host.append(el('div', { class: 'bar', style: 'margin:0 0 6px' },
    chip(`Showing ${num(r.items.length)} of ${num(r.total)}`),
    el('div', { class: 'push' }),
    el('button', {
      class: 'sm', onclick: () => { host.innerHTML = ''; host.style.display = 'none'; },
    }, 'Close')));

  r.items.forEach((it) => host.append(
    el('div', { class: 'copy', style: 'grid-template-columns:1fr auto auto' },
      el('div', {},
        el('div', { class: 'rname' }, it.title || basename(it.path)),
        el('div', { class: 'rmeta' }, [it.artist, it.album].filter(Boolean).join(' — ')),
        el('div', { class: 'diff' },
          it.old_value ? el('s', {}, it.old_value) : el('span', { class: 'rmeta' }, '(empty)'),
          el('em', {}, g.kind === 'art' ? 'cover image' : it.new_value))),
      el('button', {
        class: 'jump', title: it.folder,
        onclick: () => openFolderFor(it.track_id),
      }, 'Show folder'),
      el('span', { class: 'chip mono' }, `${Math.round(it.confidence * 100)}%`))));

  if (r.total > r.items.length) {
    host.append(el('div', { class: 'rmeta', style: 'padding:8px 2px' },
      `${num(r.total - r.items.length)} more not shown. Approving the group covers all ${num(r.total)}.`));
  }
}

/* Files that do not match the target are work waiting on a decision, which is
   what this tab is for. The settings that decide the target live in Settings. */
async function conversionCard() {
  const [format, cfg] = await Promise.all([api('/standardize'), api('/config')]);
  const label = (cfg.target.codec || '').toUpperCase();
  if (!format.needs_convert) {
    return card('Format', `Everything is ${label} or protected. Nothing to convert.`);
  }
  return card('Needs converting',
    `${num(format.needs_convert)} files are not ${label} yet. Staging them puts them in the list below with everything else, so they go through the same approval. Conversions run one at a time and are checked against the source length before the swap.`,
    el('div', { class: 'bar' },
      el('button', {
        class: 'go', onclick: async () => {
          const r = await api('/standardize/stage', { method: 'POST', body: {} });
          toast(`${r.staged} conversions staged.`);
          render(); poll();
        },
      }, `Stage ${num(format.needs_convert)} conversions`),
      el('button', {
        onclick: async () => {
          await api('/run/convert', { method: 'POST', body: {} });
          toast('Running approved conversions.');
          poll();
        },
      }, 'Run approved conversions now'),
      el('button', {
        class: 'sm', onclick: () => { settingsSection = 'format'; switchTab('settings'); },
      }, 'Change the target format')));
}

async function reviewGrouped() {
  const { counts, groups } = await api('/changes/grouped?status=' + reviewStatus);

  if (!groups.length) {
    const empty = document.createDocumentFragment();
    empty.append(await conversionCard());
    empty.append(card(
      { pending: 'Nothing waiting', approved: 'Nothing approved',
        rejected: 'Nothing skipped' }[reviewStatus],
      { pending: 'Run a scan or a metadata lookup and anything Harmon wants to change appears here first.',
        approved: 'Approve something from the Waiting tab and it collects here until you apply it.',
        rejected: 'Anything you skip collects here, in case you want it back.' }[reviewStatus],
      reviewModeBar(counts)));
    return empty;
  }

  const rows = groups.map((g) => {
    const [bandLabel, bandTone] = BAND[g.band] || BAND.good;
    const samples = el('div', { class: 'gbody', style: 'display:none' });
    let shown = false;

    const VERB = { approved: 'approved', rejected: 'skipped', pending: 'moved back to waiting' };
    const decide = async (status, ask) => {
      if (ask && !confirm(`${ask}\n\n${describeGroup(g)}\nFrom ${SOURCE_NAME[g.source] || g.source}.`)) return;
      const r = await api('/changes/decide-group', {
        method: 'POST',
        body: { kind: g.kind, field: g.field, source: g.source, band: g.band,
                status, from_status: reviewStatus },
      });
      toast(`${num(r.updated)} changes ${VERB[status]}.`);
      render(); poll();
    };

    const head = el('div', { class: 'row', style: 'grid-template-columns:1fr auto auto auto auto' },
      el('div', {},
        el('div', { class: 'rname' }, describeGroup(g)),
        el('div', { class: 'rmeta' },
          `From ${SOURCE_NAME[g.source] || g.source || 'Harmon'}`,
          g.lo === g.hi ? ` · ${Math.round(g.lo * 100)}% sure`
                        : ` · ${Math.round(g.lo * 100)}–${Math.round(g.hi * 100)}% sure`)),
      chip(bandLabel, bandTone),
      el('button', {
        class: 'sm', onclick: () => {
          shown = !shown;
          samples.style.display = shown ? 'flex' : 'none';
        },
      }, 'Examples'),
      el('button', {
        class: 'sm', onclick: () => showAllInGroup(g, samples),
      }, `View all ${num(g.n)}`),
      ...(reviewStatus === 'approved'
        ? [el('button', {
              class: 'sm', onclick: () => decide('pending'),
            }, `Undo approval (${num(g.n)})`),
           el('button', { class: 'sm', onclick: () => decide('rejected') }, 'Skip instead')]
        : reviewStatus === 'rejected'
        ? [el('button', {
              class: 'sm', onclick: () => decide('pending'),
            }, `Put back (${num(g.n)})`)]
        : [el('button', {
              class: 'go sm',
              onclick: () => decide('approved',
                `Approve ${num(g.n)} changes?\n\nNothing is written yet — you can undo this from the Approved tab until you press Apply.`),
            }, `Approve ${num(g.n)}`),
           el('button', { class: 'sm', onclick: () => decide('rejected') }, 'Skip')]));

    g.samples.forEach((sm) => samples.append(
      el('div', { class: 'copy', style: 'grid-template-columns:1fr auto auto' },
        el('div', {},
          el('div', { class: 'rname' }, sm.title || basename(sm.path)),
          el('div', { class: 'rmeta' }, [sm.artist, sm.album].filter(Boolean).join(' — ')),
          el('div', { class: 'diff' },
            sm.old_value ? el('s', {}, sm.old_value) : el('span', { class: 'rmeta' }, '(empty)'),
            el('em', {}, g.kind === 'art' ? 'cover image' : sm.new_value))),
        el('button', {
          class: 'jump', onclick: () => openFolderFor(sm.track_id),
        }, 'Show folder'),
        el('span', { class: 'chip mono' }, `${Math.round(sm.confidence * 100)}%`))));

    return el('div', {}, head, samples);
  });

  const frag = document.createDocumentFragment();
  frag.append(await conversionCard());
  const heading = {
    pending: `${num(counts.pending)} waiting, ${groups.length} decisions`,
    approved: `${num(counts.approved)} approved and not yet written`,
    rejected: `${num(counts.rejected)} skipped`,
  }[reviewStatus];
  const lede = {
    pending: 'Grouped by what they actually do, so you decide about a kind of change rather than about every row. Open Examples to see what a group contains. Nothing is written until you apply.',
    approved: 'These will be written the next time you press Apply. Until then you can undo any of it.',
    rejected: 'Skipped for now. Put any of it back if you change your mind.',
  }[reviewStatus];
  frag.append(card(heading, lede,
    reviewModeBar(counts),
    el('div', { class: 'rows' }, rows)));
  return frag;
}

async function reviewFlat() {
  const data = await api(`/changes?status=${reviewStatus}&limit=400` +
    (reviewKind ? '&kind=' + reviewKind : ''));
  const { counts, items } = data;
  selected.clear();

  const filters = el('div', { class: 'bar' },
    el('div', { class: 'segs' },
      [[null, 'Everything'], ['tag', 'Tags'], ['art', 'Artwork'],
       ['lyrics', 'Lyrics'], ['delete', 'Removals'],
       ['convert', 'Conversions']].map(([key, label]) =>
        el('button', {
          class: 'sm' + (reviewKind === key ? ' on' : ''),
          onclick: () => { reviewKind = key; render(); },
        }, label + (counts.by_kind?.[key] ? ` (${counts.by_kind[key]})` : '')))),
    el('div', { class: 'push' }),
    ...(reviewStatus === 'approved'
      ? [el('button', {
            class: 'sm', disabled: !items.length,
            onclick: () => decideSelected('pending'),
          }, 'Undo ticked'),
         el('button', {
            class: 'sm', disabled: !items.length, onclick: async () => {
              if (!confirm(`Move all ${num(counts.approved)} approved changes back to waiting?`)) return;
              await api('/changes/decide-all', { method: 'POST',
                body: { kind: reviewKind, status: 'pending', from_status: 'approved' } });
              toast('Moved back to waiting.'); render(); poll();
            },
          }, 'Undo everything approved')]
      : [el('button', {
            class: 'sm', disabled: !items.length,
            onclick: () => decideSelected('approved'),
          }, 'Approve ticked'),
         el('button', {
            class: 'sm', disabled: !items.length, onclick: async () => {
              const total = counts.by_kind?.[reviewKind] ?? counts.pending;
              if (!confirm(`Approve all ${num(total)} changes matching this filter, not just the ${items.length} shown?\n\nNothing is written yet — you can undo this from the Approved tab until you press Apply.`)) return;
              await api('/changes/decide-all', { method: 'POST',
                body: { kind: reviewKind, status: 'approved', from_status: reviewStatus } });
              toast('Approved. Apply them when you are ready.'); render(); poll();
            },
          }, 'Approve everything matching'),
         el('button', {
            class: 'sm', disabled: !items.length, onclick: async () => {
              const total = counts.by_kind?.[reviewKind] ?? counts.pending;
              if (!confirm(`Skip all ${num(total)} changes matching this filter?`)) return;
              await api('/changes/decide-all', { method: 'POST',
                body: { kind: reviewKind, status: 'rejected', from_status: reviewStatus } });
              toast('Skipped.'); render(); poll();
            },
          }, 'Skip everything matching')]));

  const body = items.length
    ? el('div', { class: 'rows' }, items.map(changeRow))
    : el('div', { class: 'empty' },
        el('b', {}, 'Nothing waiting'),
        'Run a scan or a metadata lookup and anything Harmon wants to change appears here first.');

  return card(`${num(counts.pending)} changes staged`,
    items.length >= 400
      ? `Showing the first ${items.length}. The bulk buttons act on everything matching the filter, not just what is on screen.`
      : 'Approve what you want, then apply. Everything else stays as it is.',
    reviewModeBar(counts), filters, body);
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
  const label = { tag: 'Tag', art: 'Artwork', delete: 'Remove file',
                  convert: 'Convert', lyrics: 'Lyrics' }[c.kind] || c.kind;
  let detail;

  if (c.kind === 'tag') {
    detail = el('div', { class: 'diff' },
      el('span', { class: 'rmeta' }, c.field.replace('_', ' ')),
      c.old_value ? el('s', {}, c.old_value) : el('span', { class: 'rmeta' }, '(empty)'),
      el('em', {}, c.new_value));
  } else if (c.kind === 'art') {
    let host = c.source || 'a cover source';
    try { host = new URL(c.new_value).hostname; } catch { /* not a URL */ }
    detail = el('div', { class: 'diff' },
      el('em', {}, 'Embed a cover image'), el('span', { class: 'rmeta' }, host));
  } else if (c.kind === 'lyrics') {
    detail = el('div', { class: 'diff' },
      el('em', {}, c.new_value),
      el('span', { class: 'rmeta' },
        c.old_value === 'unsynced' ? 'upgrading from plain text' : 'had none'));
  } else if (c.kind === 'delete') {
    detail = el('div', { class: 'diff' }, el('s', {}, c.old_value));
  } else {
    detail = el('div', { class: 'diff' },
      el('em', {}, c.new_value), el('span', { class: 'rmeta' }, c.old_value));
  }

  const tone = { delete: 'hot', convert: 'wait' }[c.kind] || null;

  return el('div', { class: 'row', style: 'grid-template-columns:auto 1fr auto auto auto' },
    el('input', {
      type: 'checkbox',
      onchange: (e) => e.target.checked ? selected.add(c.id) : selected.delete(c.id),
    }),
    el('div', {},
      el('div', { class: 'rname' }, c.title || basename(c.path)),
      el('div', { class: 'rmeta' }, [c.artist, c.album].filter(Boolean).join(' — ') || c.path),
      detail),
    chip(label, tone),
    el('span', { class: 'chip mono', title: 'How sure the source is' },
      `${Math.round((c.confidence || 0) * 100)}%`),
    el('div', { class: 'racts' },
      reviewStatus === 'approved'
        ? el('button', {
            class: 'sm', onclick: async () => {
              await api('/changes/decide', { method: 'POST', body: { ids: [c.id], status: 'pending' } });
              render(); poll();
            },
          }, 'Undo')
        : el('button', {
            class: 'go sm', onclick: async () => {
              await api('/changes/decide', { method: 'POST', body: { ids: [c.id], status: 'approved' } });
              render(); poll();
            },
          }, 'Approve'),
      el('button', {
        class: 'sm', onclick: async () => {
          await api('/changes/decide', { method: 'POST', body: { ids: [c.id], status: 'rejected' } });
          render(); poll();
        },
      }, 'Skip')));
}

/* --- settings ---------------------------------------------------------- */

const SETTINGS_SECTIONS = [
  ['library', 'Library'],
  ['format', 'Format'],
  ['metadata', 'Metadata'],
  ['duplicates', 'Duplicates'],
  ['automation', 'Automation'],
  ['appearance', 'Appearance'],
];
let settingsSection = 'library';

async function viewSettings() {
  const [cfg, status] = await Promise.all([api('/config'), api('/status')]);
  state.config = cfg;
  const frag = document.createDocumentFragment();

  frag.append(el('div', { class: 'bar' },
    el('div', { class: 'segs pills' },
      SETTINGS_SECTIONS.map(([key, label]) => el('button', {
        class: 'sm' + (settingsSection === key ? ' on' : ''),
        onclick: () => { settingsSection = key; render(); },
      }, label)))));

  const show = (name) => settingsSection === name;

  if (show('library')) {
    const buildLine = el('div', { class: 'rmeta' }, 'Checking…');
    api('/version').then((v) => {
      buildLine.textContent = `Harmon ${v.version}, build ${v.build}, ${v.routes.length} endpoints`;
    }).catch((e) => { buildLine.textContent = e.message; });
    frag.append(card('What is running',
      'If the page and the server disagree about which endpoints exist, the build did not fully deploy.',
      buildLine));
  }
  if (show('format')) frag.append(await formatSettingsCard());

  const save = async (patch, again = false) => {
    state.config = await api('/config', { method: 'PUT', body: patch });
    if (again) render();
  };

  /* Connection check */
  const netOut = el('div', { class: 'rows' });
  if (show('library')) frag.append(card('Check the connection',
    'If lookups fail with a name resolution error, run this. It tests one layer at a time, so the first thing that fails is the thing to fix.',
    el('div', { class: 'bar' },
      el('button', {
        onclick: async (e) => {
          e.target.disabled = true;
          netOut.innerHTML = '';
          netOut.append(el('div', { class: 'empty' }, 'Testing…'));
          const r = await api('/netcheck');
          netOut.innerHTML = '';
          r.checks.forEach((c) => netOut.append(
            el('div', { class: 'row', style: 'grid-template-columns:1fr auto' },
              el('div', {},
                el('div', { class: 'rname' }, c.name),
                el('div', { class: 'rmeta', style: 'white-space:normal' }, c.detail)),
              chip(c.ok ? 'Fine' : 'Problem', c.ok ? 'good' : 'hot'))));
          netOut.append(el('div', { class: 'row' },
            el('div', { class: 'rmeta', style: 'white-space:normal' }, r.verdict)));
          e.target.disabled = false;
        },
      }, 'Run the check')),
    netOut));

  /* Appearance */
  if (show('appearance')) frag.append(card('Appearance', 'Four palettes, one shape. Nothing about the layout changes.',
    el('div', { class: 'grid2' },
      THEMES.map(([key, label, swatch, blurb]) => el('button', {
        class: 'codec' + ((cfg.shell?.theme || 'nightfall') === key ? ' on' : ''),
        onclick: async () => {
          applyTheme(key);
          await save({ shell: { theme: key } }, true);
        },
      },
        el('b', {}, el('span', {
          style: `display:inline-block;width:11px;height:11px;border-radius:50%;background:${swatch};margin-right:8px`,
        }), label),
        el('small', {}, blurb))))));

  /* Libraries */
  const pathInput = el('input', { type: 'text', placeholder: '/music' });
  const nameInput = el('input', { type: 'text', placeholder: 'Music' });

  if (show('library')) frag.append(card('Music folders',
    'The paths as Harmon sees them inside its container, not as they look on your NAS. If you mounted your library at /music, that is what goes here.',
    el('div', { class: 'rows' },
      status.libraries.length
        ? status.libraries.map((l) => el('div', { class: 'row', style: 'grid-template-columns:1fr auto' },
            el('div', {},
              el('div', { class: 'rname' }, l.name),
              el('div', { class: 'rpath' }, l.path)),
            el('button', {
              class: 'sm', onclick: async () => {
                await api('/libraries/' + l.id, { method: 'DELETE' });
                render();
              },
            }, 'Remove')))
        : el('div', { class: 'empty' }, 'No folders yet.')),
    el('div', { class: 'grid2', style: 'margin-top:16px' },
      el('div', { class: 'field' }, el('label', {}, 'Folder path'), pathInput),
      el('div', { class: 'field' }, el('label', {}, 'What to call it'), nameInput)),
    el('button', {
      class: 'go', onclick: async () => {
        try {
          await api('/libraries', { method: 'POST', body: { path: pathInput.value, name: nameInput.value } });
          toast('Folder added. Run a scan to read it.');
          render();
        } catch (e) { toast(e.message, true); }
      },
    }, 'Add folder')));

  /* Providers */
  const order = [...cfg.providers.order];
  const NAMES = { musicbrainz: 'MusicBrainz', discogs: 'Discogs', lastfm: 'Last.fm',
                  spotify: 'Spotify', acoustid: 'AcoustID' };
  const BLURB = {
    musicbrainz: 'Free and needs no key. Best for correct artist, album and track numbers.',
    discogs: 'Needs a token. Best for release years, styles and pressing detail.',
    lastfm: 'Needs a key. Best for genres people actually use.',
    spotify: 'Needs a client ID and secret. Best for high-resolution artwork.',
    acoustid: 'Identifies a file by its audio rather than its tags, so it works where everything else fails. Free key, three lookups a second.',
  };
  const hasKey = {
    musicbrainz: true,
    discogs: Boolean(cfg.providers.discogs_token),
    lastfm: Boolean(cfg.providers.lastfm_key),
    spotify: Boolean(cfg.providers.spotify_client_id && cfg.providers.spotify_client_secret),
    acoustid: Boolean(cfg.providers.acoustid_key),
  };

  const orderRows = el('div', { class: 'rows' }, order.map((name, i) => {
    const probe = el('span', { class: 'chip' },
      name === 'musicbrainz' ? 'No key needed'
        : hasKey[name] ? 'Key saved — untested' : 'No key yet, this one gets skipped');
    return el('div', { class: 'row', style: 'grid-template-columns:auto 1fr auto' },
      el('span', { class: 'chip mono' }, i + 1),
      el('div', {},
        el('div', { class: 'rname' }, NAMES[name]),
        el('div', { class: 'rmeta', style: 'white-space:normal' }, BLURB[name]),
        el('div', { style: 'margin-top:7px' }, probe)),
      el('div', { class: 'racts' },
        el('button', {
          class: 'sm', onclick: async (e) => {
            e.target.disabled = true;
            probe.textContent = 'Checking…';
            probe.className = 'chip';
            const r = await api(`/providers/${name}/test`, { method: 'POST', body: {} });
            probe.textContent = r.message;
            probe.className = 'chip ' + (r.ok ? 'good' : 'hot');
            e.target.disabled = false;
          },
        }, 'Test'),
        el('button', {
          class: 'sm', disabled: i === 0, onclick: () => {
            const next = [...order];
            [next[i - 1], next[i]] = [next[i], next[i - 1]];
            save({ providers: { order: next } }, true);
          },
        }, 'Up'),
        el('button', {
          class: 'sm', disabled: i === order.length - 1, onclick: () => {
            const next = [...order];
            [next[i], next[i + 1]] = [next[i + 1], next[i]];
            save({ providers: { order: next } }, true);
          },
        }, 'Down')));
  }));

  const keyFields = [];
  const keyField = (label, key, blurb, where, type = 'password') => {
    const field = textField({
      label, blurb, link: where, type,
      value: cfg.providers[key] || '',
      save: (v) => save({ providers: { [key]: v } }),
    });
    keyFields.push(field);
    return field;
  };

  if (show('metadata')) frag.append(card('Where metadata comes from',
    'Harmon asks these in order and takes the first answer for each field, so a source with no key is skipped and the next one fills the gap.',
    orderRows,
    el('div', { class: 'grid2', style: 'margin-top:18px' },
      keyField('Your own MusicBrainz mirror', 'musicbrainz_url',
        'Optional. Running musicbrainz-docker on your network means no rate limit at all — Harmon drops the one-per-second wait and runs as fast as your hardware answers. Leave empty for the public server.',
        { url: 'https://github.com/metabrainz/musicbrainz-docker', label: 'How to run one' }, 'text'),
      keyField('Contact email', 'contact_email',
        'MusicBrainz asks every tool to say who is using it, and throttles the ones that do not. Nothing is sent anywhere else.',
        { url: 'https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting', label: 'Why they ask' }, 'text'),
      keyField('AcoustID application key', 'acoustid_key',
        'Fingerprints the audio and matches it against MusicBrainz, ignoring your tags entirely. This is what rescues files too badly tagged for anything else to identify.',
        { url: 'https://acoustid.org/new-application', label: 'Register an application' }, 'text'),
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
        { url: 'https://developer.spotify.com/dashboard', label: 'Open the Spotify dashboard' })),
    el('div', { class: 'bar', style: 'margin-top:4px' },
      el('button', {
        class: 'go', onclick: async (e) => {
          e.target.disabled = true;
          for (const f of keyFields) await f._commit();
          toast('Keys saved.');
          e.target.disabled = false;
        },
      }, 'Save keys'),
      el('span', { class: 'rmeta' },
        'Each field also saves on its own when you leave it or press Enter.'))));

  /* Enrichment */
  if (show('metadata')) frag.append(card('What Harmon is allowed to correct',
    'Turn off anything you would rather keep exactly as you tagged it.',
    Object.entries(cfg.enrich.fields).map(([f, on]) => swRow(
      f.replace('_', ' ').replace(/^./, (c) => c.toUpperCase()),
      `Let Harmon correct the ${f.replace('_', ' ')} tag.`,
      on, (v) => save({ enrich: { fields: { [f]: v } } }))),
    el('div', { class: 'grid2', style: 'margin-top:18px' },
      textField({
        label: 'Minimum confidence', type: 'number', value: cfg.enrich.min_confidence,
        blurb: 'How closely a match must line up before Harmon suggests it. 0.82 is a good balance.',
        save: (v) => save({ enrich: { min_confidence: Number(v) } }),
      }),
      textField({
        label: 'Smallest artwork to accept', type: 'number', value: cfg.enrich.art_min_px,
        blurb: 'In pixels along the shorter edge.',
        save: (v) => save({ enrich: { art_min_px: Number(v) } }),
      })),
    swRow('Embed artwork when a track has none',
      'Downloads a front cover and writes it into the file.',
      cfg.enrich.embed_art, (v) => save({ enrich: { embed_art: v } })),
    swRow('Replace tags that are already filled in',
      'Off by default: Harmon only fills blanks unless it is very sure the existing value is wrong.',
      cfg.enrich.overwrite_existing, (v) => save({ enrich: { overwrite_existing: v } }))));

  /* Duplicates */
  if (show('duplicates')) frag.append(card('How duplicates are judged',
    'Two files are the same song when artist and title match after Harmon strips things like "Remastered" and "feat.", and their lengths are close enough.',
    el('div', { class: 'grid2' },
      textField({
        label: 'Length may differ by', type: 'number', value: cfg.dupes.duration_tolerance,
        blurb: 'Seconds. Three covers different encoders and trimmed silence.',
        save: (v) => save({ dupes: { duration_tolerance: Number(v) } }),
      }),
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
  if (show('automation')) frag.append(card('How much Harmon does on its own',
    'Scanning, looking things up and staging are always safe — they change nothing on disk. The approval switches are the ones that let Harmon write without asking.',
    swRow('Watch the library for new music',
      'Re-scans on a timer and runs new files through the whole check.',
      a.auto_scan, (v) => save({ automation: { auto_scan: v } })),
    textField({
      label: 'Check every', type: 'number', value: a.scan_interval_min, blurb: 'Minutes.',
      save: (v) => save({ automation: { scan_interval_min: Number(v) } }),
    }),
    swRow('Look up metadata for new tracks', 'Stages suggestions automatically after each scan.',
      a.auto_enrich, (v) => save({ automation: { auto_enrich: v } })),
    swRow('Check new tracks against your target format', 'Stages conversions for anything that does not match.',
      a.auto_standardize, (v) => save({ automation: { auto_standardize: v } })),
    el('h2', {}, 'Approve without asking'),
    swRow('Tag and artwork changes', 'Only ones at or above your confidence setting.',
      a.auto_approve_tags, (v) => save({ automation: { auto_approve_tags: v } })),
    swRow('Format conversions', 'Originals are still kept if that setting is on.',
      a.auto_approve_converts, (v) => save({ automation: { auto_approve_converts: v } })),
    swRow('Duplicate removals', 'The riskiest one. Files move to your originals folder rather than being deleted.',
      a.auto_approve_deletes, (v) => save({ automation: { auto_approve_deletes: v } })),
    el('h2', {}, 'Quiet hours'),
    swRow('Only convert between set hours',
      'Scanning and lookups still run any time; only the heavy conversion work waits.',
      a.schedule_enabled, (v) => save({ automation: { schedule_enabled: v } })),
    el('div', { class: 'grid2' },
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

  /* Forge link */
  if (show('appearance')) frag.append(card('Link to Forge',
    'Harmon and Forge stay separate apps. Set an address here and a link to Forge appears in the header — nothing more clever than that.',
    textField({
      label: 'Forge address',
      value: cfg.shell?.forge_url || '',
      placeholder: 'https://forge.yourdomain.tld',
      blurb: 'Include https:// and no trailing slash. Leave it empty for no link.',
      save: (v) => save({ shell: { forge_url: v } }),
    })));

  return frag;
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

$('#btn-stop').addEventListener('click', async () => {
  await api('/worker/stop', { method: 'POST', body: {} });
  toast('Stopping after the current item. Nothing already staged is lost.');
  poll();
});

$$('.toptab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));

window.addEventListener('unhandledrejection', (e) => {
  toast(e.reason?.message || 'That did not work.', true);
  e.preventDefault();
});

window.addEventListener('hashchange', () => {
  const name = location.hash.replace('#/', '') || 'overview';
  if (name !== state.tab) switchTab(name);
});

(async function boot() {
  buildThemePicker();
  try {
    const cfg = await api('/config');
    state.config = cfg;
    applyTheme(cfg.shell?.theme || 'nightfall');
    if (cfg.shell?.forge_url) {
      $('#subtitle').after(el('a', {
        href: cfg.shell.forge_url, class: 'appjump', title: 'Open Forge',
      }, el('i', { 'aria-hidden': 'true' }), 'Forge'));
    }
  } catch { applyTheme('nightfall'); }

  await switchTab(location.hash.replace('#/', '') || 'overview', false);
  poll();
  setInterval(poll, 2500);
})();
