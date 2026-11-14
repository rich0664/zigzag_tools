import { before, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

describe('sea bundle', () => {
  let src;

  before(() => {
    execFileSync(process.execPath, ['build.mjs'], { cwd: root });
    src = fs.readFileSync(path.join(root, 'dist', 'zigzag-sea.user.js'), 'utf8');
  });

  it('inlines the constants', () => {
    assert.match(src, /const seaConsts = \{/);
  });

  it('contains the pure core', () => {
    assert.match(src, /function modeSettings\(/);
  });

  it('strips exports and the JSON import', () => {
    assert.doesNotMatch(src, /^\s*export /m);
    assert.doesNotMatch(src, /from '\.\/sea-consts\.json'/);
  });

  it('mounts the runtime state', () => {
    assert.match(src, /__zzSea/);
  });
});
