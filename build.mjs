// Tiny bundler: concatenates src modules into one Tampermonkey userscript.
// Modules are ESM for testability; the bundle strips `export` statements and
// relies on top-level function declarations (hoisted, shared scope).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const ORDER = ['discogs.js', 'youtube.js', 'match.js', 'fill.js', 'ui.js', 'main.js'];

const header = fs.readFileSync(path.join(root, 'src', 'header.txt'), 'utf8').trimEnd();
const parts = [header];
for (const f of ORDER) {
  let code = fs.readFileSync(path.join(root, 'src', f), 'utf8');
  code = code.replace(/^export\s*(\{[^}]*\};?)?\s*$/mg, '');
  parts.push(`\n/* ---- ${f} ---- */\n` + code.trim());
}
fs.mkdirSync(path.join(root, 'dist'), { recursive: true });
fs.writeFileSync(path.join(root, 'dist', 'zigzag-fill.user.js'), parts.join('\n') + '\n');
console.log('built dist/zigzag-fill.user.js');

// The sea-depth overlay is a separate userscript with its own header and asset.
// The pure core, the GLSL source and the runtime share one scope: constants are
// inlined first, then each module with its `export` markers stripped.
const seaHeader = fs.readFileSync(path.join(root, 'src', 'sea-header.txt'), 'utf8').trimEnd();
const seaConsts = JSON.parse(fs.readFileSync(path.join(root, 'src', 'sea-consts.json'), 'utf8'));
const SEA_ORDER = ['sea-core.js', 'whale-core.js', 'sea-glsl.js', 'sea-ui.js', 'sea.js'];
const seaParts = [`const seaConsts = ${JSON.stringify(seaConsts)};`];
for (const f of SEA_ORDER) {
  let code = fs.readFileSync(path.join(root, 'src', f), 'utf8');
  if (f === 'sea-core.js') {
    code = code.replace(/^import seaConsts from '\.\/sea-consts\.json' with \{ type: 'json' \};\s*$/m, '');
  }
  code = code.replace(/^export\s*\{[^}]*\};?[^\S\r\n]*$/mg, '');
  code = code.replace(/^export\s+/mg, '');
  seaParts.push(`\n/* ---- ${f} ---- */\n` + code.trim());
}
// The runtime is wrapped in one IIFE after the userscript header (which stays
// outside): a page-global `const clamp` would collide with the app or another
// userscript and throw "Identifier 'clamp' has already been declared", leaving
// the whole script unrun.
const seaBody = `(() => {\n${seaParts.join('\n')}\n})();`;
fs.writeFileSync(path.join(root, 'dist', 'zigzag-sea.user.js'), seaHeader + '\n\n' + seaBody + '\n');
console.log('built dist/zigzag-sea.user.js');
