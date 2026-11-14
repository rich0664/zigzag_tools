# Sea Shader Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the animated sea shader behind a five-entry quality menu on the zig~zag map, with the existing texture overlay as the static fallback.

**Architecture:** Split the sea userscript into a pure, tested core (`src/sea-core.js`), the GLSL source (`src/sea-glsl.js`), and a runtime (`src/sea.js`), concatenated by `build.mjs`. Assets are baked once for the whole map by a Python tool. One renderer, two paths: a 2D canvas for `static`, a WebGL2 canvas for the four animated tiers, both reading deck's view state from React's fiber.

**Tech Stack:** JavaScript (ESM, no dependencies), WebGL2, Node 24 `node:test`, Python 3.12 + numpy/Pillow for the bake, `playwright-cli` for live checks.

**Spec:** `docs/superpowers/specs/2026-11-14-sea-shader-integration-design.md`

## Global Constraints

- Node 24 lives at `/home/dev/.local/share/node-v24.22.2-linux-x64/bin` and is
  not on the non-interactive PATH; prefix every shell step with
  `export PATH=/home/dev/.local/share/node-v24.22.2-linux-x64/bin:$PATH`.
  `npm test` = `node --test test/*.test.js`; `npm run build` = `node build.mjs`.
- No new runtime dependencies in the userscript. No bundler beyond `build.mjs`.
- `playwright-cli` is at `/home/dev/.local/share/node-v24.22.2-linux-x64/bin/playwright-cli`; the logged-in zig~zag profile is `/home/dev/.local/share/agent-browser/zigzag`; use session `-s zigzag`.
- Userscript version goes to `0.3.0` in `src/sea-header.txt`; every asset URL carries `?v=0.3.0`.
- Map is 7749 × 5254 tiles. Distance `DIST_MAX = 600`. Coarse flow is 16 tiles/texel, speed 4, aux 2.
- Baseline uniforms and curves are copied verbatim from the spec. Do not re-tune them.
- `window.__zzSea` keeps `on`, `opacity`, `error`, plus the new `mode` and `setMode`.
- Do not touch the release-filler script (`src/main.js` and friends) or its tests.
- Every entry point in `src/sea.js` is wrapped: a failure must never throw into the host page.
- Load the built script into the live page without Tampermonkey:

```bash
PW=/home/dev/.local/share/node-v24.22.2-linux-x64/bin/playwright-cli
python3 -m http.server 3007 --bind 0.0.0.0 --directory dist &   # once per session
$PW -s zigzag --raw eval "() => fetch('http://127.0.0.1:3007/zigzag-sea.user.js?t='+Date.now()).then(r=>r.text()).then(t=>new Function(t)())"
```

  The page is https and 3007 is http, which browsers allow for loopback. If
  the site's CSP blocks it, install the build in Tampermonkey instead and say
  so in the task notes.
- Page screenshots on this app can hang waiting for fonts. Prefer reading the
  canvas or `debugRender()` output; when a screenshot is the only option and it
  times out, record that and use the canvas samples instead of retrying.

## Review Focus

1. **A shader texture fails to load or CORS is refused** — the player should keep a working static sea and see a reason, never a blank/black sea.
2. **A saved mode from another version or corrupt localStorage** — the script should fall back to a valid default and keep drawing.
3. **The notifications button is missing or moved** (logged out, app redeploy) — no orphan button, no menu floating in a corner, no errors.
4. **Deck's camera has padding** (68 px left sidebar) or the window resizes — the sea must stay aligned with the map, not drift by the padding.
5. **The GL context is lost** (tab backgrounded too long, GPU reset) — the overlay falls back and recovers without a console error visible to the player.

Each of these has a test in the task that owns the behaviour.

---

### Task 1: Full-map height dump

**Files:**
- Modify: `tools/sea_full.py`
- Test: `python3 - <<'PY' ...` assertions (below)

**Interfaces:**
- Consumes: `tools/sea_full.py`'s existing strip loop and `ed.evaluate()`, which returns `ctx["h"]` (the final filtered height, float32, 0..1).
- Produces: `out/sea/height_full.npy`, shape `(5254, 7749)`, dtype `float16`, same layout as `dist_full.npy`.

- [ ] **Step 1: Add the flag and the dump**

Add `--dump-height` to the argparse in `main()`. The default is the single-pass
path (`if not args.strips:`, `tools/sea_full.py:112-130`) — that is what built
the shipped texture, so dump there, not in the strip loop: after
`ctx = ed.evaluate(...)`, take `ctx["h"]`, convert to `float16`, and
`np.save("out/sea/height_full.npy", h)`. Print `height p1/p99` over the whole
array. Leave the PNG write untouched. Also add the same dump to the strip
branch for completeness, writing 0..1 float16 directly into the strip's rows.

- [ ] **Step 2: Run it once**

Run: `python3 tools/sea_full.py interesting_v1 --dump-height`
Expected: ~11 minutes, ~5 GB peak, then `wrote out/sea/height_full.npy` and the p1/p99 line. The colour output must not change (the preset is unchanged).

- [ ] **Step 3: Verify the plane**

```bash
python3 - <<'PY'
import numpy as np
h = np.load('out/sea/height_full.npy')
assert h.shape == (5254, 7749), h.shape
assert h.dtype == np.float16
assert np.isfinite(h).all()
assert 0.0 <= h.min() and h.max() <= 1.0
lo, hi = np.percentile(h, [1, 99])
print('p1', round(float(lo), 4), 'p99', round(float(hi), 4))
PY
```
Expected: prints two values well inside 0..1 and no assertion.

- [ ] **Step 4: Commit**

```bash
git add tools/sea_full.py
git commit -m "sea: dump the full-map height field"
```

---

### Task 2: Full-map shader asset bake

**Files:**
- Create: `tools/bake_shader_assets.py`
- Create: `src/sea-consts.json`
- Test: `python3 tools/bake_shader_assets.py --check` (self-check mode, below)

**Interfaces:**
- Consumes: `out/sea/dist_full.npy`, `out/sea/currents_uv.npy`, `out/sea/height_full.npy`, `out/sea/full_interesting_v1.png`, `out/sea/map.png`, `tools/sea_core.py` (`box_blur`), `src/sea-consts.json`.
- Produces: `dist/zigzag-sea-dist.webp`, `dist/zigzag-sea-aux.webp`, `dist/zigzag-sea-flow.webp`, `dist/zigzag-sea-speed.webp`, and the frozen `alongScale` / `heightLo` / `heightHi` values in `src/sea-consts.json`.

`src/sea-consts.json` starts as:

```json
{
  "version": "0.3.0",
  "assetBase": "https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist",
  "map": { "w": 7749, "h": 5254 },
  "distMax": 600,
  "alongScale": 1.0,
  "heightLo": 0.0,
  "heightHi": 1.0,
  "auxScale": 2,
  "flowCell": 16,
  "speedCell": 4,
  "backingCap": 2560,
  "baseline": {
    "shore": 8.8, "shelf": 29, "flow": 2.31, "wave": 1.9, "grain": 0.06,
    "foam": 0.25, "deep": 1.0, "swell": 0.008, "adir": 254, "aspeed": 1.42,
    "ascale": 0.156, "bdir": 45, "bspeed": 1.16, "bscale": 0.12,
    "par": 0.814, "pom": 5, "dslow": 1.0, "win": 0.28, "wout": 0.82,
    "cycle": 7.0, "warp": 3.3, "wbias": 1.0, "deflect": 1.0,
    "deflectReach": 50.0
  },
  "curves": {
    "pc": [0, 0.057, 0.339, 0.578, 1],
    "sc": [0, 0.057, 0.274, 0.492, 0.926],
    "wc": [0, 0.13, 0.435, 0.594, 1]
  }
}
```

- [ ] **Step 1: Port the baker**

Copy the maths from `prototype/sea-shader/bake.py` into `tools/bake_shader_assets.py`, removing the crop arguments (`x0=y0=0`, `w=7749`, `h=5254`). Keep the order: distance → along vector → depth → coarse flow → detail (for speed only) → speed. All arrays are full-map; the memory peak is ~3 GB.

Deliberate changes:

- distance: `dn = sqrt(clip(d, 0, 600) / 600)`, written to `zigzag-sea-dist.webp` as **lossless** grayscale.
- along: `k_enc = alongScale` from the JSON (not `max(1, max|component|)`). Compute the raw `along_x, along_y` first, print `along-max=<value>` and the p99, and if `alongScale == 1.0` exit after printing so the value can be frozen.
- aux: at `auxScale` (2) tiles/texel, block-mean of the full-res fields; channels `R = depth`, `G = along_x/alongScale*0.5+0.5`, `B = along_y/alongScale*0.5+0.5`, `A = 255`. Depth is `(h − heightLo) / (heightHi − heightLo)`, clamped, inverted (`1 − depth`), zeroed on land.
- flow: block-average `currents_uv.npy` to 484 × 328 (`map_w / 16`), `R = u*0.5+0.5`, `G = v*0.5+0.5`, plus the coherence in `B` exactly as the prototype does.
- speed: replicate the prototype's `sp_coarse * keep * keep_g` block-averaged to 4 tiles/texel, `R` only.
- land mask: `map.png` alpha > 128, interior holes filled by flooding from every border pixel (as the prototype's baker does).

`--check` validates the written files instead of writing: decode each WebP with `createImageBitmap`-equivalent (Pillow), assert shapes, and sample 1000 random water tiles: `|sqrt(dist_webp*600) − dist_full| < 1`, `depth_webp` within 2 codes of the computed depth, `|along|` within 0.02 of 1.

- [ ] **Step 2: Freeze the along scale**

Add a `--measure` flag that computes and prints `along-max=<value>` (and p99) over the whole map, then exits without writing.

Run: `python3 tools/bake_shader_assets.py --measure`
Expected: one line. Copy the max into `src/sea-consts.json` as `alongScale` (round up to two decimals), set `heightLo`/`heightHi` to the p1/p99 from Task 1, and record the numbers in the commit message.

- [ ] **Step 3: Bake**

Run: `python3 tools/bake_shader_assets.py`
Expected: four `dist/` files written, each printed size. Total download (files + the existing 2.2 MB colour) must be ≤ 20 MB; if not, stop and report before continuing.

- [ ] **Step 4: Check**

Run: `python3 tools/bake_shader_assets.py --check`
Expected: exit 0, no assertion. Print the four sizes and the download total.

- [ ] **Step 5: Compare the distance crop with the prototype**

```bash
python3 - <<'PY'
from PIL import Image
import numpy as np
a = np.asarray(Image.open('dist/zigzag-sea-dist.webp').convert('RGB'))[700:1700, 600:2200, 0].astype(float)
b = np.asarray(Image.open('prototype/sea-shader/assets/aux.png').convert('RGBA'))[700:1700, 600:2200, 0].astype(float)
d = np.abs(a - b)
print('distance mean', round(float(d.mean()), 2), 'max', float(d.max()))
assert d.mean() <= 3 and d.max() <= 12
PY
```
Expected: mean ≤ 3 codes, max ≤ 12 (8-bit quantisation only). Depth and along are deliberately different from the prototype (full-map normalisation and a frozen scale), so they are not compared.

- [ ] **Step 6: Commit**

```bash
git add tools/bake_shader_assets.py src/sea-consts.json dist/zigzag-sea-*.webp
git commit -m "sea: full-map shader assets (dist, aux, flow, speed)"
```

---

### Task 3: Pure core and its tests

**Files:**
- Create: `src/sea-core.js`
- Create: `test/sea-core.test.js`
- Modify: `src/sea-consts.json` (no; values already there)

**Interfaces:**
- Consumes: `src/sea-consts.json` via `import seaConsts from './sea-consts.json' with { type: 'json' }`.
- Produces (all exported, no DOM, no side effects):

```js
MODE_IDS                                  // ['baseline','medium','low','extra-low','static']
modeSettings(id)  -> {shader, renderScale, uniforms}   // static: {shader:false, renderScale:1, uniforms:null}
pickMode(saved, env) -> id                 // env {webgl2, maxTextureSize, coarsePointer, narrow}
parseState(raw)   -> {mode, on, opacity, brightness, fps30}   // raw values are strings or null
renderSize(cssW, cssH, dpr, renderScale)  -> {w, h, scale}     // long edge capped at seaConsts.backingCap
cameraUniforms(vs, cssW, cssH, backingW, backingH) -> {zoomPx, centerX, centerY, viewW, viewH}
fpsGate(now, nextAt, fps30)               -> {draw, nextAt}
assetUrl(name)                            -> string           // `${assetBase}/${name}?v=${version}`
```

- [ ] **Step 1: Write the failing tests**

`test/sea-core.test.js`:

```js
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { MODE_IDS, modeSettings, pickMode, parseState, renderSize, cameraUniforms, fpsGate, assetUrl } from '../src/sea-core.js';

describe('modeSettings', () => {
  it('baseline carries the tuned values', () => {
    const s = modeSettings('baseline');
    assert.equal(s.shader, true);
    assert.equal(s.renderScale, 1);
    assert.equal(s.uniforms.par, 0.814);
    assert.equal(s.uniforms.pom, 5);
    assert.equal(s.uniforms.warp, 3.3);
    assert.equal(s.uniforms.flowMode, 1);   // LOD
    assert.equal(s.uniforms.noiseLayers, 2);
  });
  it('medium only removes parallax', () => {
    assert.equal(modeSettings('medium').uniforms.par, 0);
    assert.equal(modeSettings('medium').uniforms.pom, 1);
    assert.equal(modeSettings('medium').uniforms.warp, 3.3);
  });
  it('low drops LOD, one noise layer and warp', () => {
    const u = modeSettings('low').uniforms;
    assert.equal(u.flowMode, 0);
    assert.equal(u.noiseLayers, 1);
    assert.equal(u.warp, 0);
    assert.equal(u.par, 0);
  });
  it('extra-low is low at half scale', () => {
    assert.equal(modeSettings('extra-low').renderScale, 0.5);
    assert.deepEqual(modeSettings('extra-low').uniforms, modeSettings('low').uniforms);
  });
  it('static needs no shader', () => {
    assert.equal(modeSettings('static').shader, false);
  });
  it('rejects unknown ids', () => {
    assert.throws(() => modeSettings('nope'));
  });
});
```

Also: `pickMode` (saved wins; no WebGL2 → static; 4096 → static; coarse → static; otherwise baseline), `parseState` (nulls, `'NaN'`, `mode:'nope'`, opacity 150 → 1, brightness 20 → 0.5, fps30 `'1'` → true), `renderSize` (1920×1080 at dpr 2 → 2560×1440 and `scale ≈ 1.333`; 390×844 dpr 3 → 1170×2532; extra-low halves), `cameraUniforms` (a target at the padded centre maps to the canvas centre; `padding.left 68` shifts `centerX` by `34 × scale`; `zoomPx = 2**zoom × scale`), `fpsGate` (no gate → always true; 30 fps at 16.7 ms → false, at 33.4 ms → true, nextAt advances by 33.33), `assetUrl` (contains `?v=0.3.0`).

- [ ] **Step 2: Run the tests, watch them fail**

Run: `npm test`
Expected: FAIL, `Cannot find module '../src/sea-core.js'`.

- [ ] **Step 3: Implement `src/sea-core.js`**

- `modeSettings`: start from `{...seaConsts.baseline, flowMode:1, noiseLayers:2, travelMax:0, snap:1}`; apply the per-mode deltas from the spec table; `medium` sets `par:0, pom:1`; `low` sets `par:0, pom:1, warp:0, flowMode:0, noiseLayers:1`; `extra-low` = `low` + `renderScale:0.5`.
- `pickMode`: `MODE_IDS.includes(saved)` wins; else `!env.webgl2 || env.maxTextureSize < 8192 || env.coarsePointer || env.narrow` → `'static'`; else `'baseline'`. `narrow` is viewport width < 700.
- `parseState`: defaults `{mode:'', on:true, opacity:1, brightness:1, fps30:false}`; mode must be in `MODE_IDS` or `''`; numbers via `Number()` with `isFinite` and clamping (opacity 0..1, brightness 0.5..1.5); `fps30` true for `'1'`, `'true'`, `true`.
- `renderSize`: `k = Math.min(1, cap / Math.max(cssW*dpr, cssH*dpr))`, then `w = max(1, round(cssW*dpr*k*renderScale))`.
- `cameraUniforms`: copy the padding maths from `src/sea.js:96-101` in CSS pixels; `scale = backingW / cssW`; `zoomPx = 2**vs.zoom * scale`; `centerX = (cssW/2 + dx) * scale`; `centerY = (cssH/2 + dy) * scale`; `viewW = backingW`.
- `fpsGate`: `const interval = fps30 ? 1000/30 : 0; const draw = now >= nextAt; return {draw, nextAt: draw ? now + interval : nextAt};` with `nextAt` starting at 0.
- `assetUrl`: `\`${seaConsts.assetBase}/${name}?v=${seaConsts.version}\``.

- [ ] **Step 4: Run the tests**

Run: `npm test`
Expected: PASS, all suites including the existing filler tests.

- [ ] **Step 5: Commit**

```bash
git add src/sea-core.js test/sea-core.test.js
git commit -m "sea: pure core (modes, state, camera, fps gate)"
```

---

### Task 4: Bundle the sea modules; static path parity

**Files:**
- Modify: `build.mjs`
- Modify: `src/sea.js`
- Create: `test/sea-build.test.js`

**Interfaces:**
- Consumes: `src/sea-consts.json`, `src/sea-core.js`, `src/sea.js`.
- Produces: `dist/zigzag-sea.user.js` with `const seaConsts = {...};` inlined, then core, then runtime in one IIFE; and `window.__zzSea.mode === 'static'` at startup (no animated modes exist yet).

- [ ] **Step 1: Capture the current overlay for comparison**

The 2D canvas is tainted today (the texture is loaded without CORS), so first add `img.crossOrigin = 'anonymous'` in `src/sea.js`, rebuild, load it, and record 20 pixel samples from the sea canvas. This one-line change stays in the final commit; it is what lets the canvas be read for verification.

```bash
PW=/home/dev/.local/share/node-v24.22.2-linux-x64/bin/playwright-cli
npm run build
# (re)load the page, then run the Global Constraints load command
$PW -s zigzag --raw eval "() => { const c=document.getElementById('zz-sea'); const x=c.getContext('2d'); const pts=[[100,100],[300,200],[500,300],[700,400],[900,500],[1100,100],[200,500],[600,150],[1000,350],[400,450]]; return pts.map(([a,b]) => Array.from(x.getImageData(a,b,1,1).data)); }" > /tmp/opencode/sea-samples-before.json
```

Expected: an array of ten RGBA arrays. If the read throws `SecurityError`, `crossOrigin` is not in effect — fix that before continuing.

- [ ] **Step 2: Write the failing build test**

`test/sea-build.test.js` reads `dist/zigzag-sea.user.js` after a build and asserts:

```js
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
assert.match(src, /const seaConsts = \{/);          // JSON inlined
assert.match(src, /function modeSettings\(/);        // core present
assert.doesNotMatch(src, /^\s*export /m);            // exports stripped
assert.doesNotMatch(src, /from '\.\/sea-consts\.json'/);
assert.match(src, /__zzSea/);
```

- [ ] **Step 3: Run it, watch it fail**

Run: `npm test`
Expected: FAIL on the first assertion (today's sea bundle has no core).

- [ ] **Step 4: Change `build.mjs`**

In the sea section: read `src/sea-consts.json`, then concatenate in order `['sea-core.js', 'sea-glsl.js', 'sea.js']` (create an empty `sea-glsl.js` placeholder now so ordering exists), prefixing `const seaConsts = ${JSON.stringify(consts)};`. Strip `export ` markers as the filler does, and strip the exact JSON import line from `sea-core.js`. Keep the sea header.

- [ ] **Step 5: Rewrite `src/sea.js` onto the core**

Keep today's draw function and camera maths but source `state` from `parseState({...localStorage})`, mode from `pickMode(state.mode, {webgl2:false, ...})` for now (the real capability probe arrives in Task 8), opacity from `state.opacity`, and remove the bottom-right slider entirely (the menu is Task 7). The 2D canvas path, its z-order, `state.draws`, and `window.__zzSea` stay. Default to `static` for now so the look is unchanged.

- [ ] **Step 6: Build and verify pixel parity**

```bash
npm run build && npm test
```

Reload, load the new build the same way, and record the same ten samples:

```bash
$PW -s zigzag --raw eval "() => { const c=document.getElementById('zz-sea'); const x=c.getContext('2d'); const pts=[[100,100],[300,200],[500,300],[700,400],[900,500],[1100,100],[200,500],[600,150],[1000,350],[400,450]]; return pts.map(([a,b]) => Array.from(x.getImageData(a,b,1,1).data)); }" > /tmp/opencode/sea-samples-after.json
python3 - <<'PY'
import json
a=json.load(open('/tmp/opencode/sea-samples-before.json')); b=json.load(open('/tmp/opencode/sea-samples-after.json'))
assert len(a)==len(b)
print('max channel diff', max(abs(p-q) for x,y in zip(a,b) for p,q in zip(x,y)))
PY
```

Expected: `max channel diff 0` (the refactor does not change what is drawn). A blank canvas (all zeros) means the script did not mount — check `window.__zzSea.error`.

- [ ] **Step 7: Commit**

```bash
git add build.mjs src/sea.js src/sea-glsl.js test/sea-build.test.js dist/zigzag-sea.user.js
git commit -m "sea: bundle core modules; static path on the pure core"
```

---

### Task 5: WebGL baseline renderer

**Files:**
- Create: `src/sea-glsl.js` (replace the placeholder)
- Modify: `src/sea.js`
- Modify: `src/sea-core.js` (only if a missing helper is proven by a failing test)

**Interfaces:**
- Consumes: textures from `assetUrl(...)`, `modeSettings('baseline')`, `cameraUniforms`, `renderSize`.
- Produces: `window.__zzSea.setMode(id)` and `window.__zzSea.debugRender({tx, ty, zoom, width, height}) -> dataURL` for verification; `window.__zzSea.mode` reflects the id.

- [ ] **Step 1: Verify CORS first**

```bash
curl -sI 'https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist/zigzag-sea.webp' | grep -i 'access-control-allow-origin'
```
Expected: `access-control-allow-origin: *`. If absent, stop and report — the spec says move the assets rather than work around it.

- [ ] **Step 2: Port the GLSL**

Create `src/sea-glsl.js` exporting `VERT` and `FRAG` template strings, copied from `prototype/sea-shader/index.html` (the `VERT`/`FRAG` constants) with exactly these edits:

- Drop samplers `uLand`, `uFlowDetail`, and all arrow/debug/slice/curve-drag code paths.
- `uDist` (new sampler) replaces `uAux.r` for distance; `uAux.r` is depth; `along = texture(uAux, p/uSize).gb * 2.0 - 1.0`.
- `uCropOrigin` is removed; `uSize` is the full map.
- `uniform vec2 uCenter;` and `world = uTarget + vec2(gl_FragCoord.x - uCenter.x, uCenter.y - gl_FragCoord.y) / uZoom;`
- Replace the `uLand` composite with `outColor = vec4(col * uBrightness, 1.0);`.
- Add `uniform float uNoiseLayers, uBrightness;` — `uNoiseLayers < 1.5` uses layer A only (skip the B sample and its weight).
- `uFlowMode`: `0` = coarse only (no along blend), `1` = LOD. `uFlowDetail` sampling is deleted.
- Keep `parShift`'s `uZoomCss / 3.2` and `uSnap = 1` semantics unchanged.
- Keep the three 1D curve textures, built once from `seaConsts.curves` with 256 texels.

- [ ] **Step 3: Add the GL path to `src/sea.js`**

On `setMode` to an animated id: remove the 2D canvas, create `<canvas id="zz-sea-gl">` before deck, `getContext('webgl2', {alpha:true, premultipliedAlpha:false, antialias:false, depth:false})`, compile the program, upload `colour/dist/aux/flow/speed` as textures (`TEXTURE_WRAP_S/T = CLAMP_TO_EDGE`, `MIN_FILTER = NEAREST` for colour/aux/dist on texel-res, `LINEAR` for flow/speed), and start the loop. Errors at any point call `fallbackToStatic(reason)`.

Loop: read `viewState(deck)`; if the signature changed or time moved, render. Compute `{w,h,scale} = renderSize(cssW, cssH, dpr, renderScale)` and `cameraUniforms(vs, cssW, cssH, w, h)`. `requestAnimationFrame` is started/stopped by mode, `on`, and `document.hidden`.

`debugRender`: render one frame to an offscreen canvas of the requested size with the given camera and return `canvas.toDataURL('image/png')`, without touching the live canvas.

- [ ] **Step 4: Pin the uniform table**

Add to `test/sea-core.test.js`: the key set of `modeSettings('baseline').uniforms` equals the documented list (`shore, shelf, flow, wave, grain, foam, deep, swell, adir, aspeed, ascale, bdir, bspeed, bscale, par, pom, dslow, win, wout, cycle, warp, wbias, deflect, deflectReach, flowMode, noiseLayers, travelMax, snap`). The runtime sets uniforms by iterating this object, so this test is the only place the list lives. Run it before wiring the loop; watch it fail until `modeSettings` produces exactly that set.

- [ ] **Step 5: Render and compare with the prototype**

Render the live crop and write the PNG:

```bash
PW=/home/dev/.local/share/node-v24.22.2-linux-x64/bin/playwright-cli
$PW -s zigzag --raw eval "() => window.__zzSea.debugRender({tx:1081.5, ty:613.1, zoom:0.40, width:1600, height:1000})" > /tmp/opencode/live.b64
python3 - <<'PY'
import base64
s = open('/tmp/opencode/live.b64').read().strip().strip('"')
open('/tmp/opencode/live-baseline.png','wb').write(base64.b64decode(s.split(',',1)[1]))
PY
```

Render the prototype reference at `?variant=P&fsrc=2&t=0&tx=1081.5&ty=613.1&zoom=0.40&ui=0` and save it as `/tmp/opencode/ref-baseline.png` (use the same browser; if a page screenshot hangs on fonts, use the headless-shell pattern already used in this repo's earlier checks).

Then compare on water only:

```python
from PIL import Image; import numpy as np
live = np.asarray(Image.open('/tmp/opencode/live-baseline.png').convert('RGB'), int)
ref  = np.asarray(Image.open('/tmp/opencode/ref-baseline.png').convert('RGB'), int)
aux  = np.asarray(Image.open('prototype/sea-shader/assets/aux.png').convert('RGBA'))
water = (aux[...,0] > 2) & (live.shape[:2] == ref.shape[:2])
d = np.abs(live - ref)[water]
print('mean', float(d.mean()), 'p99', float(np.percentile(d, 99)))
assert d.mean() <= 2
```
Expected: mean ≤ 2/255. If the images are different sizes the prototype camera did not match — fix the camera before judging the shader.

- [ ] **Step 6: Commit**

```bash
git add src/sea-glsl.js src/sea.js src/sea-core.js test/sea-core.test.js dist/zigzag-sea.user.js
git commit -m "sea: webgl baseline renderer"
```

---

### Task 6: Quality tiers, 30 fps gate, brightness

**Files:**
- Modify: `src/sea.js`
- Modify: `src/sea-core.js` (only if tests demand)
- Test: `test/sea-core.test.js`

**Interfaces:**
- Consumes: `modeSettings(id)`, `fpsGate`, `renderSize`.
- Produces: `setMode` accepts all five ids; animated modes apply `renderScale`; `__zzSea.setFps30(bool)` and `__zzSea.setBrightness(0.5..1.5)`.

- [ ] **Step 1: Failing tests first**

`test/sea-core.test.js` already pins the mode table. Add:
- every animated mode's uniforms differ from baseline only in keys the spec lists (`par,pom,warp,flowMode,noiseLayers`) — assert the key sets and that all other values equal baseline;
- `modeSettings(id)` returns a fresh object each call (no shared mutation);
- `renderSize` with `renderScale 0.5` halves both axes after the cap.

- [ ] **Step 2: Implement in `src/sea.js`**

`setMode` applies `modeSettings`, sets `canvas` backing store from `renderSize`, and updates uniforms. `setFps30` stores the flag and feeds `fpsGate(performance.now(), nextAt, fps30)`. Brightness is a uniform for GL and `style.filter` for static. Opacity remains a CSS style on either canvas.

- [ ] **Step 3: Verify each tier live**

Use `debugRender` at the pinned camera for each id. Expected: `medium` differs from `baseline` only in parallax-shifted areas; `low` shows the flat-gyre flow and no warp; `extra-low` is the same image at half resolution; `static` is byte-identical to Task 4's screenshot. Save all five PNGs under `/tmp/opencode/sea-modes/`.

- [ ] **Step 4: Verify the 30 fps gate**

```bash
$PW -s zigzag --raw eval "() => { window.__zzSea.setFps30(true); return new Promise(r => { let n=0; const t0=performance.now(); const f=()=>{ n++; if (performance.now()-t0 < 1000) requestAnimationFrame(f); else r(n); }; requestAnimationFrame(f); }); }"
```
Expected: ~30 (±2). Repeat with `false` → ~60.

- [ ] **Step 5: Commit**

```bash
git add src/sea.js src/sea-core.js test/sea-core.test.js dist/zigzag-sea.user.js
git commit -m "sea: quality tiers, 30 fps gate, brightness"
```

---

### Task 7: HUD button and menu

**Files:**
- Create: `src/sea-ui.js`
- Modify: `src/sea.js`
- Modify: `build.mjs` (insert `sea-ui.js` before `sea.js`)

**Interfaces:**
- Consumes: `MODE_IDS`, `pickMode`, `parseState`; `__zzSea` state.
- Produces: `mountSeaUi({onChange, getState}) -> {destroy}`; DOM ids `zz-sea-btn`, `zz-sea-menu`.

- [ ] **Step 1: Build the anchor**

`mountSeaUi` finds `button[aria-label^="Notifications"]`, measures its rect, and creates a fixed container at `right = innerWidth - rect.right`, `top = rect.bottom + 6`. Add a `ResizeObserver` on the button's parent header and a `resize` listener; reposition on both, never per frame. If the button is absent, do nothing (Review Focus 3).

- [ ] **Step 2: Build the button and menu**

Button: 30 × 30, transparent, label on hover, title `sea`. Copy `font-family`, `font-size`, `letter-spacing`, `color` from the notifications button's computed style at mount. Click toggles the menu; `Escape` and a `pointerdown` outside close it; the button is a real `<button>` with `aria-haspopup`/`aria-expanded`.

Menu: dark panel (`#0a1016ee`, 1px `#ffffff22`, 6px radius, 12px padding), items in this order: sea on/off; quality radios for the five ids (disabled when `__zzSea.canUseShader === false`, with a note line); opacity range 0–100; brightness range 50–150; `30 fps limit` checkbox; and an empty status line `<div id="zz-sea-status">` that Task 8 fills with loading/error/retry text. Every change calls `onChange` and persists.

The menu is appended to `document.body` (the header has `contain: paint`, so a menu inside it would be clipped). It flips above the button if `rect.bottom + menuHeight > innerHeight`.

- [ ] **Step 3: Wire persistence**

`src/sea.js` reads `parseState` from `localStorage` keys `zz-sea-mode`, `zz-sea-on`, `zz-sea-opacity`, `zz-sea-brightness`, `zz-sea-fps`; writes each on change. Unknown saved mode → `pickMode('', env)` (Review Focus 2).

- [ ] **Step 4: Verify live**

At 1280 × 720 and 390 × 844 (`$PW resize 390 844`):
- button rect is `notifications.right` aligned and `top = notifications.bottom + 6`;
- the menu opens fully on screen (no clipping by the header), and does not overlap the video player at 1280 × 720;
- outside click and `Escape` close it;
- switching each mode, reloading, and checking `localStorage` restores it;
- the bottom-right slider is gone.

- [ ] **Step 5: Commit**

```bash
git add src/sea-ui.js src/sea.js build.mjs dist/zigzag-sea.user.js
git commit -m "sea: hud button and quality menu"
```

---

### Task 8: Loading, failures and context loss

**Files:**
- Modify: `src/sea.js`
- Test: `test/sea-core.test.js`

**Interfaces:**
- Consumes: `pickMode`, `assetUrl`.
- Produces: `__zzSea.canUseShader`, `__zzSea.error`, `__zzSea.retry()`, `__zzSea.bench()`.

- [ ] **Step 1: Capability probe**

On startup, create a throwaway canvas and `getContext('webgl2')`, read `MAX_TEXTURE_SIZE`, then lose the context (`WEBGL_lose_context`). `canUseShader = !!gl && maxTextureSize >= 8192`. `pickMode` uses it (Review Focus 1/3).

- [ ] **Step 2: Lazy textures and the loading state**

Static never fetches shader textures. The first animated `setMode` fetches `dist/aux/flow/speed` with `Promise.all` + `Image`/`createImageBitmap`; the button shows a 1 px underline while loading; failure → `fallbackToStatic('texture: …')`, `error` set, menu shows `retry`.

- [ ] **Step 3: Context loss**

Listen for `webglcontextlost` (preventDefault, stop the loop, fall back) and `webglcontextrestored` (rebuild if the mode is animated). Any GL exception in the loop triggers the same fallback. Never rethrow (Review Focus 5).

- [ ] **Step 4: Prove the failures live**

- Block the assets: `$PW -s zigzag route '**/zigzag-sea-aux.webp*' --status=500` (if the CLI's route verb differs, point the loader at a missing host by overriding `assetBase` in the injected build and reload). Switch to baseline; expect the static sea plus an error string in `__zzSea.error`.
- Lose the context: `$PW -s zigzag --raw eval "() => document.getElementById('zz-sea-gl').getContext('webgl2').getExtension('WEBGL_lose_context').loseContext()"`, expect static fallback and no uncaught error in `$PW console`.
- Missing anchor: hide the notifications button with CSS (`[aria-label^=Notifications]{display:none}`), reload, expect no `#zz-sea-btn` and no error.

- [ ] **Step 5: Bench hook**

Add `__zzSea.bench()`: for the current mode, render 60 frames at the current camera and size with `performance.now()`, return `{mode, ms, fps}`; for `static`, measure 60 forced draws. It must restore the live loop afterwards.

- [ ] **Step 6: Unit tests**

`pickMode` cases for each `env` combination; `parseState` unknown/corrupt values; a test that `assetUrl` for all four shader textures carries the version.

- [ ] **Step 7: Commit**

```bash
git add src/sea.js src/sea-core.js test/sea-core.test.js dist/zigzag-sea.user.js
git commit -m "sea: capability probe, lazy textures, fallbacks"
```

---

### Task 9: Docs, version and final acceptance

**Files:**
- Modify: `README.md`
- Modify: `src/sea-header.txt`
- Modify: `src/sea-consts.json` (`version`)

- [ ] **Step 1: Version and docs**

Set `0.3.0` in the header and in `sea-consts.json`. Extend the README's sea section: the five modes, the button, the 30 fps toggle, the asset list with sizes, the bake commands (`tools/bake_shader_assets.py`, `sea_full.py --dump-height`), and the phone/static default.

- [ ] **Step 2: Full test and build**

Run: `npm test && npm run build`
Expected: all tests pass; bundle contains `0.3.0` and the inlined constants.

- [ ] **Step 3: Final live sweep**

With the built script installed:
- all five modes render; `static` matches the Task 4 screenshot;
- zoom 0.4, 1, 4, 8, 16 (via deck's zoom control or wheel) keeps the sea aligned and the bed un-sheared;
- a 30 fps run shows ~30 frames over 1 s;
- `__zzSea.bench()` reports `{mode, ms, fps}` per mode (60 frames) — record the numbers in the commit message;
- off, hidden tab, and static show no rAF work;
- the menu is usable at 1280 × 720 and 390 × 844.

- [ ] **Step 4: Commit**

```bash
git add README.md src/sea-header.txt src/sea-consts.json dist/zigzag-sea.user.js
git commit -m "sea: 0.3.0 — shader modes, hud menu, docs"
```

- [ ] **Step 5: Prototype disposition**

The spec says `prototype/sea-shader/` goes once the live overlay matches. It is
untracked, so deleting it is not a git change. Do not delete it in this plan:
ask the owner after they have used the shipped build, and delete only on a yes.
