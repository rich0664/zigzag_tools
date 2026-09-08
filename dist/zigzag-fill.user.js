// ==UserScript==
// @name         zig-zag release filler
// @namespace    zigzagTools
// @version      0.4.0
// @description  Fill the zig-zag.fm Add-a-release form from a Discogs link + YouTube playlist link. Never submits.
// @match        https://www.zig-zag.fm/contributors*
// @grant        GM_xmlhttpRequest
// @connect      discogs.com
// @connect      www.discogs.com
// @connect      api-img.discogs.com
// @connect      api.discogs.com
// @connect      www.youtube.com
// @run-at       document-idle
// ==/UserScript==

/* ---- discogs.js ---- */
// Pure Discogs helpers: parse release-page HTML into release data.
// Runs in the user's browser (Cloudflare blocks datacenter fetches), so the
// parser must be defensive: ld+json first, meta tags, then DOM fallbacks.
// Every result carries `debug` (which strategies hit) for iteration.

function discogsIdFromUrl(url) {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/\/(release|master)\/(\d+)/);
    return m ? { kind: m[1], id: m[2] } : null;
  } catch {
    return null;
  }
}

function ldJsonBlocks(html) {
  const out = [];
  const re = /<script type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(html))) {
    try {
      const parsed = JSON.parse(m[1]);
      if (Array.isArray(parsed)) out.push(...parsed);
      else out.push(parsed);
    } catch {
      // ignore unparseable blocks
    }
  }
  return out;
}

function asArray(x) {
  if (!x) return [];
  return Array.isArray(x) ? x : [x];
}

function artistNamesOf(node) {
  return asArray(node.byArtist || node.artist)
    .map((a) => (typeof a === 'string' ? a : a?.name))
    .filter(Boolean);
}

function tracksOf(node) {
  const items = asArray(node.track?.itemListElement || node.tracks || node.track);
  return items
    .map((t) => {
      if (typeof t === 'string') return t;
      const item = t.item || t;
      return item?.name || item?.title || null;
    })
    .filter(Boolean);
}

function metaContent(html, prop) {
  const m = html.match(new RegExp(`<meta[^>]+property="${prop}"[^>]+content="([^"]+)"`))
    || html.match(new RegExp(`<meta[^>]+content="([^"]+)"[^>]+property="${prop}"`));
  return m ? m[1] : null;
}

function h1Title(html) {
  const m = html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/);
  if (!m) return null;
  return m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() || null;
}

// Discogs h1 / og:title are usually "Artist – Release Title".
function splitArtistTitle(s) {
  if (!s) return { artists: [], title: null };
  const parts = s.split(/\s+[–—-]\s+/);
  if (parts.length >= 2) {
    const title = parts.pop().trim();
    const artistBit = parts.join(' & ').trim();
    const artists = artistBit.split(/\s*[,&×x]\s*|\s+and\s+/i).map((a) => a.trim()).filter(Boolean);
    return { artists, title };
  }
  return { artists: [], title: s.trim() || null };
}

// Classic Discogs tracklist markup fallback.
function domTracklist(html) {
  const titles = [];
  const re = /class="[^"]*tracklist_track_title[^"]*"[^>]*>([\s\S]*?)<\/(?:td|span|div)>/g;
  let m;
  while ((m = re.exec(html))) {
    const t = m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    if (t) titles.push(t);
  }
  return titles;
}

function parseDiscogs(html, url) {
  const debug = { strategies: [], id: discogsIdFromUrl(url) };
  const data = {
    title: null, artists: [], year: null, country: null,
    formats: [], genres: [], styles: [], labels: [],
    coverUrl: null, tracks: [], supportUrl: url, debug,
  };

  for (const block of ldJsonBlocks(html)) {
    const nodes = asArray(block['@graph'] || block);
    for (const node of nodes) {
      const type = node['@type'];
      if (type !== 'MusicAlbum' && type !== 'MusicRelease') continue;
      debug.strategies.push('ld+json:' + type);
      data.title = data.title || node.name || null;
      const an = artistNamesOf(node);
      if (an.length && !data.artists.length) data.artists = an;
      const tr = tracksOf(node);
      if (tr.length && !data.tracks.length) data.tracks = tr;
      if (!data.coverUrl && node.image) {
        data.coverUrl = typeof node.image === 'string' ? node.image : node.image.url || asArray(node.image)[0];
      }
      if (!data.year && node.datePublished) {
        const y = String(node.datePublished).match(/\d{4}/);
        data.year = y ? y[0] : null;
      }
      const genres = asArray(node.genre).filter((g) => typeof g === 'string');
      if (genres.length && !data.genres.length) data.genres = genres;
    }
    if (data.title && data.tracks.length) break;
  }

  if (!data.title || !data.artists.length) {
    const og = metaContent(html, 'og:title');
    if (og) {
      debug.strategies.push('og:title');
      const split = splitArtistTitle(og.replace(/&amp;/g, '&'));
      if (!data.title) data.title = split.title;
      if (!data.artists.length) data.artists = split.artists;
    }
  }
  if (!data.title) {
    const h1 = h1Title(html);
    if (h1) {
      debug.strategies.push('h1');
      const split = splitArtistTitle(h1);
      data.title = split.title;
      if (!data.artists.length) data.artists = split.artists;
    }
  }
  if (!data.coverUrl) {
    const ogImg = metaContent(html, 'og:image');
    if (ogImg) {
      debug.strategies.push('og:image');
      data.coverUrl = ogImg;
    }
  }
  if (!data.tracks.length) {
    const dom = domTracklist(html);
    if (dom.length) {
      debug.strategies.push('dom:tracklist_track_title');
      data.tracks = dom;
    }
  }
  return data;
}

function apiUrlFor(id) {
  if (!id) return null;
  return `https://api.discogs.com/${id.kind === 'master' ? 'masters' : 'releases'}/${id.id}`;
}

// Discogs API names carry disambiguation numbers: "Name (2)". Strip them.
function cleanApiName(s) {
  return String(s || '').replace(/\s+\(\d+\)$/, '').trim();
}

function apiArtists(j) {
  return (j.artists || []).map((a) => cleanApiName(a.name)).filter(Boolean);
}

function apiYear(j) {
  if (j.year) return String(j.year);
  const m = String(j.released || '').match(/\d{4}/);
  return m ? m[0] : null;
}

function apiTracks(j) {
  return (j.tracklist || [])
    .filter((t) => t && t.title && (!t.type_ || t.type_ === 'track'))
    .map((t) => String(t.title).trim())
    .filter(Boolean);
}

function apiCover(j) {
  const imgs = j.images || [];
  const primary = imgs.find((i) => i.type === 'primary') || imgs[0];
  return (primary && (primary.uri || primary.uri150)) || null;
}

function parseReleaseApi(j, url) {
  return {
    title: j.title || null,
    artists: apiArtists(j),
    year: apiYear(j),
    country: j.country || null,
    formats: (j.formats || []).map((f) => f.name).filter(Boolean),
    genres: (j.genres || []).filter((g) => typeof g === 'string'),
    styles: (j.styles || []).filter((s) => typeof s === 'string'),
    labels: (j.labels || []).map((l) => cleanApiName(l.name)).filter(Boolean),
    coverUrl: apiCover(j),
    tracks: apiTracks(j),
    supportUrl: url,
    debug: { strategies: ['api:release'], id: discogsIdFromUrl(url) },
  };
}

function parseMasterApi(j, url) {
  const d = parseReleaseApi(
    { ...j, labels: [], country: j.country || null, formats: j.formats || [] },
    url,
  );
  d.debug.strategies = ['api:master'];
  d.mainReleaseId = j.main_release || null;
  return d;
}

// Minimal ISO-code -> English-name map for the country chip field.
// Unmapped codes fall through to manual pick (logged).
const COUNTRY_NAMES = {
  US: 'United States', UK: 'United Kingdom', GB: 'United Kingdom',
  DE: 'Germany', FR: 'France', JP: 'Japan', CA: 'Canada', AU: 'Australia',
  IT: 'Italy', ES: 'Spain', NL: 'Netherlands', BE: 'Belgium', CH: 'Switzerland',
  AT: 'Austria', SE: 'Sweden', NO: 'Norway', DK: 'Denmark', FI: 'Finland',
  IE: 'Ireland', PT: 'Portugal', GR: 'Greece', PL: 'Poland', CZ: 'Czech Republic',
  HU: 'Hungary', RO: 'Romania', BG: 'Bulgaria', HR: 'Croatia', RS: 'Serbia',
  UA: 'Ukraine', RU: 'Russia', BY: 'Belarus', BR: 'Brazil', AR: 'Argentina',
  MX: 'Mexico', CL: 'Chile', CO: 'Colombia', PE: 'Peru', KR: 'South Korea',
  CN: 'China', TW: 'Taiwan', IN: 'India', NZ: 'New Zealand', ZA: 'South Africa',
  IL: 'Israel', TR: 'Turkey', IS: 'Iceland', LU: 'Luxembourg', EE: 'Estonia',
  LV: 'Latvia', LT: 'Lithuania', SK: 'Slovakia', SI: 'Slovenia',
};

function countryName(code) {
  if (!code) return null;
  const c = String(code).trim();
  if (c.length !== 2) return c; // already a name
  return COUNTRY_NAMES[c.toUpperCase()] || c;
}

/* ---- youtube.js ---- */
// Pure YouTube helpers: no browser APIs, testable in Node.
// Fetching happens in main.js (GM_xmlhttpRequest); parsing lives here.

function extractYtInitialData(html) {
  const m = html.match(/var ytInitialData\s*=\s*(\{.*?\});<\/script>/s);
  if (!m) return null;
  try {
    return JSON.parse(m[1]);
  } catch {
    return null;
  }
}

function walkNodes(root, onDict) {
  const stack = [root];
  while (stack.length) {
    const o = stack.pop();
    if (Array.isArray(o)) {
      for (let i = o.length - 1; i >= 0; i--) stack.push(o[i]);
    } else if (o && typeof o === 'object') {
      onDict(o, stack);
      const vals = Object.values(o);
      for (let i = vals.length - 1; i >= 0; i--) stack.push(vals[i]);
    }
  }
}

function cleanYoutubeUrl(id) {
  return `https://www.youtube.com/watch?v=${id}`;
}

function videosFromInitialData(data) {
  const out = [];
  if (!data) return out;
  // Modern renderer: lockupViewModel nodes (playlist page, 2025+)
  walkNodes(data, (o) => {
    const lockup = o.lockupViewModel;
    if (!lockup || typeof lockup !== 'object') return;
    if (lockup.contentType && lockup.contentType !== 'LOCKUP_CONTENT_TYPE_VIDEO') return;
    const id = lockup.contentId;
    const title = lockup.metadata?.lockupMetadataViewModel?.title?.content;
    if (typeof id === 'string' && id.length === 11 && typeof title === 'string' && title) {
      out.push({ id, title, url: cleanYoutubeUrl(id) });
    }
  });
  if (out.length) return out;
  // Legacy renderer fallback
  walkNodes(data, (o) => {
    const r = o.playlistVideoRenderer;
    if (!r || typeof r !== 'object') return;
    const id = r.videoId;
    const title = r.title?.runs?.[0]?.text || r.title?.simpleText;
    if (typeof id === 'string' && id.length === 11 && typeof title === 'string' && title) {
      out.push({ id, title, url: cleanYoutubeUrl(id) });
    }
  });
  return out;
}

// Parse a playlist page's HTML into ordered [{id, title, url}].
function parsePlaylistVideos(html) {
  return videosFromInitialData(extractYtInitialData(html));
}

// Parse a single watch page's HTML into {id, title} via og tags.
function parseWatchVideo(html, url) {
  const id = videoIdFromUrl(url);
  if (!id) return null;
  const m = html.match(/<meta property="og:title" content="([^"]+)"/);
  return { id, title: m ? m[1] : id, url: cleanYoutubeUrl(id) };
}

function playlistIdFromUrl(url) {
  try {
    return new URL(url).searchParams.get('list');
  } catch {
    return null;
  }
}

function videoIdFromUrl(url) {
  try {
    const u = new URL(url);
    if (u.hostname === 'youtu.be') return u.pathname.slice(1) || null;
    return u.searchParams.get('v');
  } catch {
    return null;
  }
}

// Normalize user input: playlist URL -> canonical playlist URL,
// bare watch URL -> itself (single-video mode handled by caller).
function canonicalPlaylistUrl(url) {
  const list = playlistIdFromUrl(url);
  return list ? `https://www.youtube.com/playlist?list=${list}` : null;
}

/* ---- match.js ---- */
// Fuzzy matching of Discogs track titles to YouTube video titles.
// Pure functions, testable in Node.

function normalize(s) {
  return (s || '')
    .toLowerCase()
    .replace(/\(.*?\)|\[.*?\]/g, ' ') // drop (official video), [ lyrics ], …
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function tokens(s) {
  return new Set(normalize(s).split(' ').filter(Boolean));
}

// Jaccard similarity over token sets, 0..1, with a joined-words fallback:
// "bug-infested" vs "buginfested" share no tokens but are clearly the same.
function similarity(a, b) {
  const ta = tokens(a);
  const tb = tokens(b);
  if (!ta.size || !tb.size) return 0;
  let inter = 0;
  for (const t of ta) if (tb.has(t)) inter++;
  const base = inter / (ta.size + tb.size - inter);
  const sa = [...ta].join('');
  const sb = [...tb].join('');
  if (sa.length >= 6 && sb.length >= 6 && (sa.includes(sb) || sb.includes(sa))) {
    return Math.max(base, 0.5);
  }
  return base;
}

// Strip a leading "Artist - " prefix from a video title when it matches the
// release artist, so "Artist - Track" compares equal to Discogs "Track".
function stripArtistPrefix(videoTitle, artistNames) {
  const norm = normalize(videoTitle);
  for (const artist of artistNames || []) {
    const na = normalize(artist);
    if (na && norm.startsWith(na + ' ')) {
      return videoTitle.slice(videoTitle.toLowerCase().indexOf(artist.toLowerCase()) + artist.length)
        .replace(/^[\s\-–—:]+/, '');
    }
  }
  return videoTitle;
}

// Greedy 1:1 assignment: for each track (in order), pick the best unused video
// above `threshold`. Returns [{track, video|null, score}].
function matchTracksToVideos(tracks, videos, artistNames, threshold = 0.4) {
  const used = new Set();
  return tracks.map((track) => {
    let best = null;
    let bestScore = -1;
    videos.forEach((video, i) => {
      if (used.has(i)) return;
      const s = similarity(track, stripArtistPrefix(video.title, artistNames));
      if (s > bestScore) {
        bestScore = s;
        best = { video, index: i };
      }
    });
    if (best && bestScore >= threshold) {
      used.add(best.index);
      return { track, video: best.video, score: bestScore };
    }
    return { track, video: null, score: bestScore < 0 ? 0 : bestScore };
  });
}

/* ---- fill.js ---- */
// Browser-only: fill the zig-zag "Add a release" form.
// Selectors use stable data-testid / placeholder / aria-label hooks only.
// React-generated ids (_r_41_…) are NEVER used.
// This module NEVER clicks Submit for review / Save draft / Discard.

function zzRoot() {
  return document.querySelector('[data-testid="contribute-track-form"]');
}

function setNativeValue(el, value) {
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

function setReactText(el, value) {
  el.focus();
  setNativeValue(el, value == null ? '' : String(value));
  el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
  el.blur();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function sectionFor(testid) {
  return zzRoot()?.querySelector(`[data-testid="${testid}"]`) || null;
}

function inputByPlaceholder(section, placeholder) {
  return section?.querySelector(`input[placeholder="${placeholder}"]`) || null;
}

// Search-style inputs (country/format/genre): type, wait for the chip button
// with exact text, click it. Returns true when a chip was clicked.
async function pickChip(sectionTestid, ariaLabel, wanted, log) {
  const section = sectionFor(sectionTestid);
  if (!section) return false;
  const input = section.querySelector(`input[aria-label="${ariaLabel}"]`);
  if (!input) {
    log(`chip input missing: ${ariaLabel}`);
    return false;
  }
  setReactText(input, wanted);
  await sleep(900);
  const chips = [...section.querySelectorAll('button')].filter(
    (b) => b.textContent.trim().toLowerCase() === wanted.trim().toLowerCase(),
  );
  if (!chips.length) {
    log(`no chip for "${wanted}" (${ariaLabel}) — left for manual pick`);
    return false;
  }
  chips[0].click();
  await sleep(400);
  return true;
}

async function fillRelease(release, log) {
  const section = sectionFor('contribute-release-section');
  if (!section) {
    log('release section not found');
    return false;
  }
  const title = inputByPlaceholder(section, 'Release title');
  if (title && release.title) {
    setReactText(title, release.title);
    await sleep(250);
  }
  const year = inputByPlaceholder(section, 'Missing year');
  if (year && release.year) {
    setReactText(year, release.year);
    await sleep(250);
  }
  if (release.country) await pickChip('contribute-release-section', 'Search countries', release.country, log);
  if (release.format) await pickChip('contribute-release-section', 'Search formats', release.format, log);
  if (release.genres && release.genres.length) {
    // Order slightly affects map weight: fill in given order.
    for (const g of release.genres) await pickChip('contribute-release-section', 'Search genres', g, log);
  }
  return true;
}

async function fillCover(blob, filename, log) {
  const input = document.querySelector('input[data-testid="image-upload-input"]');
  if (!input) {
    log('cover file input not found');
    return false;
  }
  if (!blob) {
    log('no cover blob — pick manually via Choose an image');
    return false;
  }
  const file = new File([blob], filename || 'cover.jpg', { type: blob.type || 'image/jpeg' });
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await sleep(500);
  return true;
}

function trackSection() {
  return sectionFor('contribute-tracklist-section');
}

// Ordered inputs, independent of row wrapper testids.
function trackTitleInputs() {
  return [...(trackSection()?.querySelectorAll('input[placeholder="Track title"]') || [])];
}

function trackLinkInputs() {
  return [...(trackSection()?.querySelectorAll('input') || [])].filter((el) =>
    (el.placeholder || '').startsWith('https://www.youtube.com/watch?v='),
  );
}

function trackRows() {
  return [...(zzRoot()?.querySelectorAll('[data-testid^="track-row-"]') || [])];
}

async function waitForTrue(cond, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 3000);
  while (Date.now() < deadline) {
    if (cond()) return true;
    await sleep(250);
  }
  return !!cond();
}

async function waitForCount(getCount, target, timeoutMs) {
  return waitForTrue(() => getCount() >= target, timeoutMs);
}

function trackTab(i) {
  return zzRoot()?.querySelector(`[data-testid="track-tab-${i}"]`) || null;
}

function trackRow(i) {
  return zzRoot()?.querySelector(`[data-testid="track-row-${i}"]`) || null;
}

// The form renders ONLY the active tab's row (other rows unmount, data stays
// in React state). So: select tab i (creating it first if needed), wait for
// its row, fill it, repeat.
async function fillTracks(tracks, log) {
  if (!tracks.length) {
    log('no tracks parsed — nothing to fill (see debug dump)');
    return true;
  }
  const add = zzRoot()?.querySelector('[data-testid="add-track"]');
  if (!add) {
    log('add-track button not found');
    return false;
  }
  let filled = 0;
  for (let i = 0; i < tracks.length; i++) {
    if (!trackTab(i)) {
      add.click();
      if (!(await waitForTrue(() => !!trackTab(i), 4000))) {
        log(`tab ${i + 1} never appeared — stopping, rest manual`);
        break;
      }
    }
    trackTab(i).click();
    if (!(await waitForTrue(() => !!trackRow(i), 3000))) {
      log(`row ${i + 1} never rendered — stopping, rest manual`);
      break;
    }
    const row = trackRow(i);
    const title = row.querySelector('input[placeholder="Track title"]');
    const link = [...row.querySelectorAll('input')].find((el) =>
      (el.placeholder || '').startsWith('https://www.youtube.com/watch?v='),
    );
    // Discogs title wins on mismatch (per zig-zag mods).
    if (tracks[i].title && title) setReactText(title, tracks[i].title);
    if (tracks[i].url && link) setReactText(link, tracks[i].url);
    else if (tracks[i].url && !link) log(`row ${i + 1}: link input missing`);
    filled++;
    await sleep(300);
  }
  log(`filled ${filled}/${tracks.length} track row(s)`);
  if (filled < tracks.length) log('remaining tracks left manual — paste the debug dump back');
  return true;
}

async function fillSupport(urls, log) {
  const section = sectionFor('contribute-support-section');
  const input = section?.querySelector('input[placeholder="https://…"]');
  if (!input) {
    log('support link input not found');
    return false;
  }
  if (urls && urls.length) {
    setReactText(input, urls[0]);
    await sleep(250);
    if (urls.length > 1) log('extra support links ignored (form takes one): ' + urls.slice(1).join(', '));
  }
  return true;
}

// Combobox inputs (artists/labels): type the name and try to pick an exact
// dropdown option; otherwise leave the typed text for manual pick.
async function pickCombobox(sectionTestid, placeholder, wanted, log) {
  const section = sectionFor(sectionTestid);
  const input = section && inputByPlaceholder(section, placeholder);
  if (!input) {
    log(`combobox missing: ${placeholder}`);
    return false;
  }
  setReactText(input, wanted);
  await sleep(1000);
  const opts = [...(section.querySelectorAll('[role="option"]') || [])];
  const hit = opts.find((o) => o.textContent.trim().toLowerCase() === wanted.trim().toLowerCase())
    || opts.find((o) => o.textContent.trim().toLowerCase().includes(wanted.trim().toLowerCase()));
  if (hit) {
    hit.click();
    await sleep(400);
    return true;
  }
  log(`no dropdown hit for "${wanted}" — left typed for manual pick`);
  return false;
}

async function fillArtists(names, log) {
  const add = zzRoot()?.querySelector('[data-testid="add-artist"]');
  for (let i = 0; i < names.length; i++) {
    if (i > 0) {
      if (!add) {
        log('add-artist button missing');
        break;
      }
      add.click();
      await sleep(450);
    }
    await pickCombobox('contribute-artists-section', 'Search existing artists', names[i], log);
  }
  return true;
}

async function fillLabel(name, log) {
  if (!name) return true;
  return pickCombobox('contribute-label-section', 'Search existing labels', name, log);
}

async function fillNotes(text, log) {
  const ta = zzRoot()?.querySelector('textarea[placeholder^="Anything a moderator"]');
  if (!ta) {
    log('notes textarea not found');
    return false;
  }
  if (text) setReactText(ta, text);
  return true;
}

function remainingChecklist() {
  return [...(zzRoot()?.querySelectorAll('[data-testid="checklist-unmet"]') || [])].map((li) =>
    li.textContent.replace(/\s+/g, ' ').trim(),
  );
}

// plan: {release, coverBlob, coverName, tracks:[{title,url}], support:[], artists:[], label, notes}
// onLog(msg) receives progress lines. Returns {ok, remaining}.
async function fillAll(plan, onLog) {
  const log = onLog || (() => {});
  if (!zzRoot()) {
    log('add-release form not open (open it via Add a release first)');
    return { ok: false, remaining: [] };
  }
  await fillRelease(plan.release || {}, log);
  await fillCover(plan.coverBlob, plan.coverName, log);
  await fillTracks(plan.tracks || [], log);
  await fillSupport(plan.support || [], log);
  await fillArtists(plan.artists || [], log);
  await fillLabel(plan.label, log);
  if (plan.notes) await fillNotes(plan.notes, log);
  await sleep(600);
  const remaining = remainingChecklist();
  log(remaining.length ? `still needed (${remaining.length}): ${remaining.join(' | ')}` : 'checklist clear — review and submit manually');
  return { ok: true, remaining };
}

/* ---- ui.js ---- */
// Browser-only: floating panel + preview/match review + debug dump.
const ZZ = (globalThis.ZZ ??= {});
ZZ.state = ZZ.state || { discogs: null, videos: [], matches: [], log: [] };

function zzLog(msg) {
  ZZ.state.log.push(msg);
  const el = document.querySelector('[data-testid="zz-log"]');
  if (el) {
    const div = document.createElement('div');
    div.textContent = msg;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }
}

function panelHtml() {
  return `
  <div data-testid="zz-panel" style="position:fixed;top:8px;right:338px;z-index:99999;width:380px;max-height:92vh;overflow:auto;background:#111;color:#eee;border:1px solid #555;border-radius:8px;padding:10px;font:12px sans-serif;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <b>zig-zag filler (never submits)</b>
      <button data-testid="zz-collapse" style="background:#333;color:#eee;border:1px solid #555;border-radius:4px;">–</button>
    </div>
    <div data-testid="zz-body">
      <input data-testid="zz-discogs" placeholder="Discogs release URL" style="width:100%;margin-bottom:4px;background:#222;color:#eee;border:1px solid #555;border-radius:4px;padding:4px;" />
      <input data-testid="zz-playlist" placeholder="YouTube playlist URL" style="width:100%;margin-bottom:4px;background:#222;color:#eee;border:1px solid #555;border-radius:4px;padding:4px;" />
      <div style="display:flex;gap:4px;margin-bottom:6px;">
        <button data-testid="zz-fetch" style="flex:1;background:#234;color:#fff;border:1px solid #555;border-radius:4px;padding:5px;">Fetch &amp; preview</button>
        <button data-testid="zz-fill" disabled style="flex:1;background:#333;color:#888;border:1px solid #555;border-radius:4px;padding:5px;">Fill form</button>
      </div>
      <div data-testid="zz-preview" style="margin-bottom:6px;"></div>
      <div data-testid="zz-log" style="max-height:120px;overflow:auto;background:#000;border:1px solid #333;border-radius:4px;padding:4px;margin-bottom:6px;"></div>
      <button data-testid="zz-dump" style="width:100%;background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:4px;">Copy debug dump</button>
    </div>
  </div>`;
}

function renderPreview() {
  const { discogs, matches } = ZZ.state;
  const box = document.querySelector('[data-testid="zz-preview"]');
  if (!box || !discogs) return;
  const rows = matches.map((m, i) => {
    const opts = ZZ.state.videos
      .map((v) => `<option value="${v.id}" ${m.video && m.video.id === v.id ? 'selected' : ''}>${escapeHtml(v.title)}</option>`)
      .join('');
    return `<div style="display:flex;gap:4px;align-items:center;margin-bottom:2px;">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(m.track)}">${i + 1}. ${escapeHtml(m.track)}</span>
      <select data-testid="zz-match-${i}" style="flex:1;background:#222;color:#eee;border:1px solid #555;">${opts}</select>
      <span style="color:${m.video ? '#8f8' : '#f88'}">${m.video ? m.score.toFixed(2) : '—'}</span>
    </div>`;
  }).join('');
  box.innerHTML = `<div style="margin-bottom:4px;"><b>${escapeHtml(discogs.title || '?')}</b> · ${escapeHtml((discogs.artists || []).join(', '))} · ${escapeHtml(discogs.year || '?')}</div>${rows}`;
  const fill = document.querySelector('[data-testid="zz-fill"]');
  if (fill) {
    fill.disabled = false;
    fill.style.background = '#263';
    fill.style.color = '#fff';
  }
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function selectorCheck() {
  const ids = [
    'contribute-track-form', 'contribute-release-section', 'contribute-tracklist-section',
    'contribute-support-section', 'contribute-artists-section', 'contribute-label-section',
    'contribute-submit-section', 'add-track', 'add-artist', 'image-upload-input', 'draft-checklist',
  ];
  const out = {};
  for (const id of ids) out[id] = !!document.querySelector(`[data-testid="${id}"]`);
  return out;
}

function copyDebugDump() {
  const dump = {
    url: location.href,
    selectors: selectorCheck(),
    discogs: ZZ.state.discogs && { ...ZZ.state.discogs, tracks: ZZ.state.discogs.tracks?.length },
    videos: ZZ.state.videos.length,
    matches: ZZ.state.matches.map((m) => ({ track: m.track, video: m.video?.id || null, score: m.score })),
    htmlSnippet: ZZ.state.discogsHtmlSnippet || null,
    log: ZZ.state.log,
  };
  const text = JSON.stringify(dump, null, 2);
  (navigator.clipboard?.writeText(text) || Promise.reject()).then(
    () => zzLog('debug dump copied — paste it back to the dev'),
    () => {
      window.prompt('Copy debug dump:', text);
    },
  );
}

function mountPanel(handlers) {
  if (document.querySelector('[data-testid="zz-panel"]')) return;
  const wrap = document.createElement('div');
  wrap.innerHTML = panelHtml();
  document.body.appendChild(wrap);
  wrap.querySelector('[data-testid="zz-fetch"]').addEventListener('click', handlers.onFetch);
  wrap.querySelector('[data-testid="zz-fill"]').addEventListener('click', handlers.onFill);
  wrap.querySelector('[data-testid="zz-dump"]').addEventListener('click', copyDebugDump);
  const body = wrap.querySelector('[data-testid="zz-body"]');
  const btn = wrap.querySelector('[data-testid="zz-collapse"]');
  btn.addEventListener('click', () => {
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    btn.textContent = hidden ? '–' : '+';
  });
  for (const line of ZZ.state.log) zzLog(line);
}

/* ---- main.js ---- */
// Browser-only wiring: fetching (GM_xmlhttpRequest), orchestration, boot.
// Depends on globals from discogs.js / youtube.js / match.js / fill.js / ui.js
// (concatenated scope in the built userscript).

function gmGet(url, responseType) {
  return new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method: 'GET',
      url,
      responseType: responseType || 'text',
      onload: (res) => (res.status >= 200 && res.status < 300 ? resolve(res) : reject(new Error(`HTTP ${res.status} for ${url}`))),
      onerror: (e) => reject(new Error(`network error for ${url}: ${e && e.error}`)),
      ontimeout: () => reject(new Error(`timeout for ${url}`)),
    });
  });
}

async function fetchDiscogsData(dUrl) {
  const id = discogsIdFromUrl(dUrl);
  if (id) {
    try {
      const apiRes = await gmGet(apiUrlFor(id));
      const j = JSON.parse(apiRes.responseText);
      if (id.kind === 'master' && j.main_release) {
        const master = parseMasterApi(j, dUrl);
        try {
          // Main release carries the definitive country/labels/formats.
          const mainRes = await gmGet(`https://api.discogs.com/releases/${j.main_release}`);
          const main = parseReleaseApi(JSON.parse(mainRes.responseText), dUrl);
          master.country = master.country || main.country;
          master.labels = main.labels;
          if (!master.formats.length) master.formats = main.formats;
          if (!master.coverUrl) master.coverUrl = main.coverUrl;
          master.debug.strategies.push('api:main-release');
        } catch (e) {
          master.debug.strategies.push('api:main-release-failed');
        }
        return { data: master, html: null };
      }
      return { data: parseReleaseApi(j, dUrl), html: null };
    } catch (e) {
      zzLog('discogs API failed, falling back to page scrape: ' + e.message);
    }
  }
  const dRes = await gmGet(dUrl);
  return { data: parseDiscogs(dRes.responseText, dUrl), html: dRes.responseText };
}

// First 1500 chars around the first "tracklist" mention in raw HTML —
// included in the debug dump so the scraper fallback can be hardened.
function tracklistSnippet(html) {
  if (!html) return null;
  const i = html.toLowerCase().indexOf('tracklist');
  if (i < 0) return '(no "tracklist" in raw html)';
  return html.slice(Math.max(0, i - 300), i + 1200);
}

async function onFetch() {
  const dUrl = document.querySelector('[data-testid="zz-discogs"]')?.value.trim();
  const pUrl = document.querySelector('[data-testid="zz-playlist"]')?.value.trim();
  if (!dUrl || !pUrl) {
    zzLog('paste both URLs first');
    return;
  }
  try {
    zzLog('fetching Discogs…');
    const { data, html } = await fetchDiscogsData(dUrl);
    ZZ.state.discogs = data;
    ZZ.state.discogsHtmlSnippet = tracklistSnippet(html);
    const d = ZZ.state.discogs;
    zzLog(`discogs: "${d.title}" — ${d.artists.join(', ')} (${d.year || 'no year'}), ${d.tracks.length} tracks [${d.debug.strategies.join(', ')}]`);
  } catch (e) {
    zzLog('discogs fetch failed: ' + e.message);
    return;
  }
  try {
    zzLog('fetching YouTube…');
    const listUrl = canonicalPlaylistUrl(pUrl);
    if (listUrl) {
      const yRes = await gmGet(listUrl);
      ZZ.state.videos = parsePlaylistVideos(yRes.responseText);
    } else {
      const vId = videoIdFromUrl(pUrl);
      if (!vId) {
        zzLog('could not read playlist or video id from YouTube URL');
        return;
      }
      const wRes = await gmGet(cleanYoutubeUrl(vId));
      const one = parseWatchVideo(wRes.responseText, pUrl);
      ZZ.state.videos = one ? [one] : [];
    }
    zzLog(`youtube: ${ZZ.state.videos.length} video(s)`);
  } catch (e) {
    zzLog('youtube fetch failed: ' + e.message);
    return;
  }
  ZZ.state.matches = matchTracksToVideos(ZZ.state.discogs.tracks, ZZ.state.videos, ZZ.state.discogs.artists);
  const auto = ZZ.state.matches.filter((m) => m.video).length;
  zzLog(`matched ${auto}/${ZZ.state.matches.length} tracks — review the table, then Fill form`);
  renderPreview();
}

async function onFill() {
  const d = ZZ.state.discogs;
  if (!d) {
    zzLog('nothing fetched yet');
    return;
  }
  // Read user overrides from the preview table.
  const tracks = ZZ.state.matches.map((m, i) => {
    const sel = document.querySelector(`[data-testid="zz-match-${i}"]`);
    const video = sel ? ZZ.state.videos.find((v) => v.id === sel.value) : m.video;
    return { title: m.track, url: video ? video.url : null };
  });
  zzLog(`filling ${tracks.length} track(s)…`);
  let coverBlob = null;
  if (d.coverUrl) {
    try {
      zzLog('fetching cover…');
      const cRes = await gmGet(d.coverUrl, 'blob');
      coverBlob = cRes.response;
    } catch (e) {
      zzLog('cover fetch failed (pick manually): ' + e.message);
    }
  }
  const genreNotes = [...(d.genres || []), ...(d.styles || [])].filter(Boolean);
  const plan = {
    release: { title: d.title, year: d.year, country: countryName(d.country), genres: [] },
    coverBlob,
    coverName: 'cover.jpg',
    tracks,
    support: d.supportUrl ? [d.supportUrl] : [],
    artists: d.artists || [],
    label: (d.labels || [])[0] || null,
    notes: genreNotes.length ? `Discogs genres/styles: ${genreNotes.join(', ')} — closest picked manually.` : null,
  };
  zzLog('filling… (genres/countries left for manual pick when unsure)');
  await fillAll(plan, zzLog);
}

function boot() {
  const maybeMount = () => {
    if (document.querySelector('[data-testid="contribute-track-form"]')) {
      mountPanel({ onFetch, onFill });
      return true;
    }
    return false;
  };
  if (maybeMount()) return;
  const obs = new MutationObserver(() => {
    if (maybeMount()) obs.disconnect();
  });
  obs.observe(document.documentElement, { childList: true, subtree: true });
}

boot();
