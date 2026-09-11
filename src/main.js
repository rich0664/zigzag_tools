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
  const plan = {
    release: { title: d.title, year: d.year, country: countryName(d.country), genres: [] },
    coverBlob,
    coverName: 'cover.jpg',
    tracks,
    support: d.supportUrl ? [d.supportUrl] : [],
    artists: d.artists || [],
    label: (d.labels || [])[0] || null,
    notes: null, // extra notes stay clear for the user
  };
  zzLog('filling… (genres/countries left for manual pick when unsure)');
  await fillAll(plan, zzLog);
}

async function onSync() {
  const d = ZZ.state.discogs;
  if (!d || !d.tracks.length) {
    zzLog('fetch a Discogs release + playlist first');
    return;
  }
  const rename = !!document.querySelector('[data-testid="zz-rename"]')?.checked;
  zzLog('reading existing rows…');
  const existing = await readExistingTracks(zzLog);
  zzLog(`form has ${existing.length} row(s)`);
  // Never offer videos that are already linked — avoids the duplicate check.
  const usedVideoIds = new Set(existing.map((e) => videoIdFromUrl(e.link)).filter(Boolean));
  const freshVideos = ZZ.state.videos.filter((v) => !usedVideoIds.has(v.id));
  const matches = matchTracksToVideos(d.tracks, freshVideos, d.artists);
  const videoFor = (ti) => {
    const m = matches[ti];
    return (m && m.video) || null;
  };
  const plan = planMerge(existing, d.tracks);
  const present = plan.matchedIdx.filter((x) => x >= 0).length;
  zzLog(`${present} already present, ${plan.fills.length} empty slot(s), ${plan.appends.length} to append`);
  for (const f of plan.fills) {
    const v = videoFor(f.track);
    await fillTab(f.tab, d.tracks[f.track], v ? v.url : null, zzLog);
  }
  for (const ti of plan.appends) {
    const idx = Math.max(...trackTabIndices(), -1) + 1;
    if ((await ensureTab(idx, zzLog)) < 0) {
      zzLog('stopping, rest manual');
      break;
    }
    const v = videoFor(ti);
    await fillTab(idx, d.tracks[ti], v ? v.url : null, zzLog);
  }
  if (rename) {
    let renamed = 0;
    for (let i = 0; i < existing.length; i++) {
      const ti = plan.matchedIdx[i];
      if (ti >= 0 && existing[i].title !== d.tracks[ti]) {
        await fillTab(i, d.tracks[ti], null, zzLog);
        renamed++;
      }
    }
    zzLog(`renamed ${renamed} row(s) to Discogs titles`);
  }
  await sleep(600);
  const remaining = remainingChecklist();
  log(remaining.length ? `still needed (${remaining.length}): ${remaining.join(' | ')}` : 'checklist clear — review and submit manually');
}

// Re-read the form (e.g. after drag-reordering tabs) and rebuild the
// preview in current tab order, keeping existing video assignments.
async function onReread() {
  if (!zzRoot()) {
    zzLog('form not open');
    return;
  }
  const existing = await readExistingTracks(zzLog);
  const byVideo = new Map(ZZ.state.videos.map((v) => [v.id, v]));
  const dTracks = ZZ.state.discogs?.tracks || [];
  ZZ.state.matches = existing.map((e, i) => {
    const video = byVideo.get(videoIdFromUrl(e.link) || '');
    if (video) return { track: e.title || video.title, video, score: 1 };
    let best = null;
    let bestScore = -1;
    dTracks.forEach((t, j) => {
      const s = similarity(e.title, t);
      if (s > bestScore) {
        bestScore = s;
        best = { track: t, video: null, score: s };
      }
    });
    if (best && bestScore >= 0.4) return best;
    return { track: e.title || `(empty slot ${i + 1})`, video: null, score: 0 };
  });
  zzLog(`re-read ${existing.length} row(s) in current order`);
  renderPreview();
}

function boot() {
  const panelPresent = () => !!document.querySelector('[data-testid="zz-panel"]');
  const tick = () => {
    if (document.querySelector('[data-testid="contribute-track-form"]')) {
      if (!panelPresent()) mountPanel({ onFetch, onFill, onSync, onReread });
    } else if (panelPresent()) {
      // SPA-navigated away: drop the panel, keep fetched state.
      document.querySelector('[data-testid="zz-panel"]').remove();
    }
  };
  tick();
  const obs = new MutationObserver(tick);
  obs.observe(document.documentElement, { childList: true, subtree: true });
}

boot();
