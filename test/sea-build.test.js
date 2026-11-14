import { before, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
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

  it('contains the HUD menu before the runtime', () => {
    assert.match(src, /function mountSeaUi\(/);
    assert.match(src, /id = SEA_UI_BTN_ID/);
    assert.ok(src.indexOf('/* ---- sea-ui.js ---- */') > 0);
    assert.ok(src.indexOf('/* ---- sea-ui.js ---- */') < src.indexOf('/* ---- sea.js ---- */'));
  });

  it('no longer contains the bottom-right slider', () => {
    assert.doesNotMatch(src, /['"]zz-sea-ui['"]/);
    assert.doesNotMatch(src, /['"]zz-sea-op['"]/);
  });

  // The modules used to be concatenated at top level, so `const clamp` and
  // `const seaConsts` became page-global lexical bindings; a name already
  // declared by the app or another userscript is a SyntaxError and the script
  // never runs. The header comments stay outside the wrapper.
  it('wraps the modules in one IIFE after the header', () => {
    const lines = src.split('\n');
    const first = lines.findIndex(l => l.trim() && !l.trim().startsWith('//'));
    assert.equal(lines[first].trim(), '(() => {');
    assert.equal(lines.filter(l => l.trim()).pop().trim(), '})();');

    const open = src.indexOf('\n(() => {\n');
    const close = src.lastIndexOf('\n})();');
    assert.ok(open > 0, 'wrapper opener missing');
    assert.ok(close > open, 'wrapper footer missing');
    for (const decl of ['const seaConsts = {', 'function modeSettings(', 'function mountSeaUi(', 'const clamp =']) {
      const at = src.indexOf(decl);
      assert.ok(at > open && at < close, `${decl} is not inside the IIFE`);
    }
  });

  it('parses as a script', () => {
    // an unloadable bundle must fail the suite, not only the live page
    execFileSync(process.execPath, ['--check', path.join(root, 'dist', 'zigzag-sea.user.js')]);
  });

  // The real failure mode: a bundle injected twice (or next to another script
  // using `clamp`) must not redeclare a global lexical binding. The context has
  // no DOM, so the first statement that touches one throws ReferenceError; only
  // a top-level `const`/`function` can produce the SyntaxError instead.
  it('loads twice in one realm without a redeclaration', () => {
    const ctx = vm.createContext({});
    for (let i = 0; i < 2; i++) {
      try {
        vm.runInContext(src, ctx, { filename: 'zigzag-sea.user.js' });
      } catch (e) {
        assert.notEqual(e.name, 'SyntaxError', e.message);
      }
    }
  });

  it('keeps @version in step with seaConsts.version', () => {
    const header = fs.readFileSync(path.join(root, 'src', 'sea-header.txt'), 'utf8');
    const version = /^\s*\/\/\s*@version\s+(\S+)\s*$/m.exec(header)[1];
    const consts = JSON.parse(fs.readFileSync(path.join(root, 'src', 'sea-consts.json'), 'utf8'));
    assert.equal(version, consts.version);
  });
});
