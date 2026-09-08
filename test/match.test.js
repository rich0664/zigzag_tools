import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { normalize, similarity, stripArtistPrefix, matchTracksToVideos } from '../src/match.js';

describe('normalize/similarity', () => {
  it('ignores case, punctuation and parentheticals', () => {
    assert.equal(normalize('Pile! No Pile! Pile! (Official Video)'), 'pile no pile pile');
    assert.ok(similarity('Pile! No Pile! Pile!', 'pile no pile pile (lyrics)') > 0.99);
  });

  it('scores disjoint titles near zero', () => {
    assert.ok(similarity('abcdefgh', 'xyz 123 456') < 0.2);
  });
});

describe('stripArtistPrefix', () => {
  it('removes a leading artist name', () => {
    assert.equal(
      stripArtistPrefix('The Brave Little Abacus - pile! no pile! pile!', ['The Brave Little Abacus']),
      'pile! no pile! pile!',
    );
  });

  it('leaves unrelated titles alone', () => {
    assert.equal(stripArtistPrefix('pile! no pile! pile!', ['The Brave Little Abacus']), 'pile! no pile! pile!');
  });
});

describe('matchTracksToVideos', () => {
  const videos = [
    { id: 'aaaaaaaaaaa', title: 'The Brave Little Abacus - pile! no pile! pile!', url: 'u1' },
    { id: 'bbbbbbbbbbb', title: 'The Brave Little Abacus - buginfested floorboards (official)', url: 'u2' },
    { id: 'ccccccccccc', title: 'Some unrelated cooking tutorial', url: 'u3' },
  ];
  const artists = ['The Brave Little Abacus'];

  it('matches tracks to the right videos', () => {
    const res = matchTracksToVideos(['Pile! No Pile! Pile!', 'Bug-Infested Floorboards'], videos, artists);
    assert.equal(res[0].video.id, 'aaaaaaaaaaa');
    assert.equal(res[1].video.id, 'bbbbbbbbbbb');
  });

  it('leaves unmatched tracks null without stealing', () => {
    const res = matchTracksToVideos(['Pile! No Pile! Pile!', 'Totally Unknown Song'], videos, artists);
    assert.equal(res[0].video.id, 'aaaaaaaaaaa');
    assert.equal(res[1].video, null);
  });

  it('assigns 1:1 even when one video fits two tracks', () => {
    const res = matchTracksToVideos(['Pile', 'Pile! No Pile! Pile!'], videos.slice(0, 1), artists);
    const assigned = res.map((r) => r.video && r.video.id);
    assert.equal(new Set(assigned.filter(Boolean)).size, assigned.filter(Boolean).length);
  });
});
