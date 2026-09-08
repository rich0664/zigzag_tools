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

export { normalize, similarity, stripArtistPrefix, matchTracksToVideos };
