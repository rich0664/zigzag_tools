import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { seaUiPlacement } from '../src/sea-ui.js';

// The menu is positioned from the notifications button's live rect: same right
// edge, six px under its bottom, flipped above when it would cross the bottom.
describe('sea ui placement', () => {
  it('anchors to the notifications button right edge and bottom', () => {
    const p = seaUiPlacement({ right: 938, bottom: 44 }, { width: 1280, height: 720 }, 250);
    assert.equal(p.right, 342);
    assert.equal(p.top, 50);
    assert.equal(p.flipped, false);
  });

  it('uses the same rule at a phone-width viewport', () => {
    const p = seaUiPlacement({ right: 360, bottom: 44 }, { width: 390, height: 844 }, 250);
    assert.equal(p.right, 30);
    assert.equal(p.top, 50);
    assert.equal(p.flipped, false);
  });

  it('flips above when the menu would cross the viewport bottom', () => {
    const p = seaUiPlacement({ right: 938, bottom: 44 }, { width: 1280, height: 300 }, 250);
    assert.equal(p.flipped, true);
  });

  it('does not flip when the menu ends exactly at the viewport bottom', () => {
    // 50 + 30 + 6 + 214 = 300
    const p = seaUiPlacement({ right: 938, bottom: 44 }, { width: 1280, height: 300 }, 214);
    assert.equal(p.flipped, false);
  });
});
