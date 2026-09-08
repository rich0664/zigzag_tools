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

export {
  extractYtInitialData,
  parsePlaylistVideos,
  parseWatchVideo,
  playlistIdFromUrl,
  videoIdFromUrl,
  canonicalPlaylistUrl,
  cleanYoutubeUrl,
};
