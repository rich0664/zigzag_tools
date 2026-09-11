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

// Merge plan for topping up an existing release.
// existing: [{title, link}] in current tab order (may contain empties).
// discogsTracks: full tracklist in order.
// Returns {matchedIdx, fills, appends}:
// - matchedIdx[i] = discogs index already present at tab i, or -1.
// - fills = [{tab, track}] empty slots to fill (positional when possible).
// - appends = [discogsIdx] songs needing brand-new tabs.
// Re-running on an already-synced form yields no fills/appends (idempotent).
function planMerge(existing, discogsTracks, threshold = 0.4) {
  const used = new Set();
  const matchedIdx = existing.map((e) => {
    if (!e.title.trim()) return -1;
    let best = -1;
    let bestScore = -1;
    discogsTracks.forEach((t, j) => {
      if (used.has(j)) return;
      const s = similarity(e.title, t);
      if (s > bestScore) {
        bestScore = s;
        best = j;
      }
    });
    if (best >= 0 && bestScore >= threshold) {
      used.add(best);
      return best;
    }
    return -1;
  });
  const firstUnmatched = () => discogsTracks.findIndex((_, j) => !used.has(j));
  const fills = [];
  existing.forEach((e, i) => {
    if (e.title.trim() || (e.link || '').trim()) return; // only truly empty slots
    let track = -1;
    if (i < discogsTracks.length && !used.has(i)) track = i; // positional
    else track = firstUnmatched();
    if (track >= 0) {
      used.add(track);
      fills.push({ tab: i, track });
    }
  });
  const appends = discogsTracks.map((_, j) => j).filter((j) => !used.has(j));
  return { matchedIdx, fills, appends };
}
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

export { normalize, similarity, stripArtistPrefix, matchTracksToVideos, planMerge };
