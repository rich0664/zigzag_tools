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

export { discogsIdFromUrl, parseDiscogs, splitArtistTitle };
