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
