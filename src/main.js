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
