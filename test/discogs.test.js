import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { discogsIdFromUrl, parseDiscogs, parseReleaseApi, parseMasterApi, splitArtistTitle, countryName, apiUrlFor } from '../src/discogs.js';

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

const API_RELEASE = {
  title: 'Just Got Back From The Discomfort—We’re Alright',
  artists: [{ name: 'The Brave Little Abacus' }],
  year: 2010,
  country: 'US',
  labels: [{ name: 'Greater Than Collective' }],
  formats: [{ name: 'CD', descriptions: ['Album'] }],
  genres: ['Rock'],
  styles: ['Emo', 'Math Rock'],
  images: [{ type: 'primary', uri: 'https://api-img.discogs.com/full.jpg', uri150: 'https://api-img.discogs.com/small.jpg' }],
  tracklist: [
    { position: '1', title: 'Pile! No Pile! Pile!', duration: '2:30' },
    { position: '', title: 'B', type_: 'heading' },
    { position: '2', title: 'Bug-Infested Floorboards', duration: '3:00' },
  ],
};

describe('parseReleaseApi', () => {
  it('extracts everything, skips headings, strips nothing needed', () => {
    const d = parseReleaseApi(API_RELEASE, 'https://www.discogs.com/release/7456635-x');
    assert.equal(d.title, 'Just Got Back From The Discomfort—We’re Alright');
    assert.deepEqual(d.artists, ['The Brave Little Abacus']);
    assert.equal(d.year, '2010');
    assert.equal(d.country, 'US');
    assert.deepEqual(d.labels, ['Greater Than Collective']);
    assert.deepEqual(d.formats, ['CD']);
    assert.deepEqual(d.genres, ['Rock']);
    assert.deepEqual(d.styles, ['Emo', 'Math Rock']);
    assert.equal(d.coverUrl, 'https://api-img.discogs.com/full.jpg');
    assert.deepEqual(d.tracks, ['Pile! No Pile! Pile!', 'Bug-Infested Floorboards']);
    assert.deepEqual(d.debug.strategies, ['api:release']);
  });

  it('strips disambiguation numbers and falls back to released date', () => {
    const d = parseReleaseApi({ artists: [{ name: 'Foo (2)' }], released: '1999-05-01', tracklist: [] }, 'https://www.discogs.com/release/1-x');
    assert.deepEqual(d.artists, ['Foo']);
    assert.equal(d.year, '1999');
  });
});

describe('countryName', () => {
  it('maps common codes, passes names through', () => {
    assert.equal(countryName('US'), 'United States');
    assert.equal(countryName('JP'), 'Japan');
    assert.equal(countryName('United States'), 'United States');
    assert.equal(countryName(null), null);
  });
});

describe('apiUrlFor', () => {
  it('builds release/master endpoints', () => {
    assert.equal(apiUrlFor({ kind: 'release', id: '7456635' }), 'https://api.discogs.com/releases/7456635');
    assert.equal(apiUrlFor({ kind: 'master', id: '123' }), 'https://api.discogs.com/masters/123');
    assert.equal(apiUrlFor(null), null);
  });
});
