import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  parsePlaylistVideos,
  playlistIdFromUrl,
  videoIdFromUrl,
  canonicalPlaylistUrl,
  cleanYoutubeUrl,
} from '../src/youtube.js';

const fixture = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'playlist-initial-data.json'),
  'utf8',
);
// Re-wrap as page HTML the way the parser expects it.
const pageHtml = `<html><head></head><body><script>var ytInitialData = ${fixture};</script></body></html>`;

describe('parsePlaylistVideos (real playlist fixture)', () => {
  it('finds all videos in playlist order', () => {
    const vids = parsePlaylistVideos(pageHtml);
    assert.equal(vids.length, 12);
    assert.equal(vids[0].id, 'ajH2PUTq6Jc');
    assert.equal(vids[0].title, 'The Brave Little Abacus - pile! no pile! pile!');
    assert.equal(vids[0].url, 'https://www.youtube.com/watch?v=ajH2PUTq6Jc');
  });

  it('every entry has an 11-char id, non-empty title, clean url', () => {
    for (const v of parsePlaylistVideos(pageHtml)) {
      assert.equal(v.id.length, 11);
      assert.ok(v.title.length > 0);
      assert.equal(v.url, cleanYoutubeUrl(v.id));
    }
  });

  it('returns [] when no ytInitialData present', () => {
    assert.deepEqual(parsePlaylistVideos('<html></html>'), []);
  });
});

describe('url helpers', () => {
  it('extracts list + video ids', () => {
    const u = 'https://www.youtube.com/watch?v=ajH2PUTq6Jc&list=PL9XGRoJZm6dctwa72_7mftIG5wRIhNShF';
    assert.equal(playlistIdFromUrl(u), 'PL9XGRoJZm6dctwa72_7mftIG5wRIhNShF');
    assert.equal(videoIdFromUrl(u), 'ajH2PUTq6Jc');
  });

  it('canonicalizes watch+list urls to the playlist url', () => {
    assert.equal(
      canonicalPlaylistUrl('https://www.youtube.com/watch?v=ajH2PUTq6Jc&list=PL9XGRoJZm6dctwa72_7mftIG5wRIhNShF'),
      'https://www.youtube.com/playlist?list=PL9XGRoJZm6dctwa72_7mftIG5wRIhNShF',
    );
    assert.equal(canonicalPlaylistUrl('https://www.youtube.com/watch?v=ajH2PUTq6Jc'), null);
  });
});
