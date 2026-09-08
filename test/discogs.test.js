import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { discogsIdFromUrl, parseDiscogs, splitArtistTitle } from '../src/discogs.js';

const LD_PAGE = `<html><head>
<meta property="og:title" content="The Brave Little Abacus – Just Got Back From The Discomfort—We're Alright" />
<meta property="og:image" content="https://api-img.discogs.com/cover.jpg" />
<script type="application/ld+json">${JSON.stringify({
  '@type': 'MusicAlbum',
  name: 'Just Got Back From The Discomfort—We’re Alright',
  byArtist: { '@type': 'MusicGroup', name: 'The Brave Little Abacus' },
  datePublished: '2010',
  genre: ['Rock'],
  image: 'https://api-img.discogs.com/cover.jpg',
  track: {
    '@type': 'ItemList',
    itemListElement: [
      { '@type': 'ListItem', position: 1, item: { '@type': 'MusicRecording', name: 'Pile! No Pile! Pile!' } },
      { '@type': 'ListItem', position: 2, item: { '@type': 'MusicRecording', name: 'Bug-Infested Floorboards' } },
    ],
  },
})}</script>
</head><body></body></html>`;

describe('parseDiscogs (synthetic ld+json page)', () => {
  it('extracts title, artist, year, genre, cover, tracks', () => {
    const d = parseDiscogs(LD_PAGE, 'https://www.discogs.com/release/7456635-x');
    assert.equal(d.title, 'Just Got Back From The Discomfort—We’re Alright');
    assert.deepEqual(d.artists, ['The Brave Little Abacus']);
    assert.equal(d.year, '2010');
    assert.deepEqual(d.genres, ['Rock']);
    assert.equal(d.coverUrl, 'https://api-img.discogs.com/cover.jpg');
    assert.deepEqual(d.tracks, ['Pile! No Pile! Pile!', 'Bug-Infested Floorboards']);
    assert.ok(d.debug.strategies.some((s) => s.startsWith('ld+json')));
  });
});

describe('parseDiscogs fallbacks (no ld+json)', () => {
  it('uses og:title split + og:image', () => {
    const html = `<html><head>
      <meta property="og:title" content="Artist Name – Some Title" />
      <meta property="og:image" content="https://img/cover.png" />
      </head><body></body></html>`;
    const d = parseDiscogs(html, 'https://www.discogs.com/release/1-x');
    assert.equal(d.title, 'Some Title');
    assert.deepEqual(d.artists, ['Artist Name']);
    assert.equal(d.coverUrl, 'https://img/cover.png');
  });

  it('uses classic tracklist markup', () => {
    const html = `<table><tr><td class="track tracklist_track_title"><span>A1 Song</span></td></tr></table>`;
    const d = parseDiscogs(html, 'https://www.discogs.com/release/1-x');
    assert.deepEqual(d.tracks, ['A1 Song']);
  });
});

describe('discogsIdFromUrl', () => {
  it('parses release and master urls', () => {
    assert.deepEqual(discogsIdFromUrl('https://www.discogs.com/release/7456635-The-Brave-Little-Abacus-x'), { kind: 'release', id: '7456635' });
    assert.deepEqual(discogsIdFromUrl('https://www.discogs.com/master/123-foo'), { kind: 'master', id: '123' });
    assert.equal(discogsIdFromUrl('https://example.com/'), null);
  });
});

describe('splitArtistTitle', () => {
  it('splits on dashes', () => {
    assert.deepEqual(splitArtistTitle('A – B'), { artists: ['A'], title: 'B' });
    assert.deepEqual(splitArtistTitle('Just A Title'), { artists: [], title: 'Just A Title' });
  });
});
