// ==UserScript==
// @name         zig~zag sea depth
// @namespace    zigzagTools
// @version      0.2.15
// @description  Draws a bathymetric sea between the zig-zag.fm archipelagos: depth = distance from land, terrain = layered noise, rendered tile-aligned behind the map.
// @match        https://www.zig-zag.fm/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
const seaConsts = {"version":"0.3.0","assetBase":"https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist","map":{"w":7749,"h":5254},"distMax":600,"alongScale":1.03,"heightLo":0.1606,"heightHi":1,"auxScale":2,"flowCell":16,"speedCell":4,"backingCap":2560,"baseline":{"shore":8.8,"shelf":29,"flow":2.31,"wave":1.9,"grain":0.06,"foam":0.25,"deep":1,"swell":0.008,"adir":254,"aspeed":1.42,"ascale":0.156,"bdir":45,"bspeed":1.16,"bscale":0.12,"par":0.814,"pom":5,"dslow":1,"win":0.28,"wout":0.82,"cycle":7,"warp":3.3,"wbias":1,"deflect":1,"deflectReach":50},"curves":{"pc":[0,0.057,0.339,0.578,1],"sc":[0,0.057,0.274,0.492,0.926],"wc":[0,0.13,0.435,0.594,1]}};

/* ---- sea-core.js ---- */
// Pure core of the sea overlay: quality modes, saved-state parsing, screen ↔
// map maths, the fps gate and asset URLs. No DOM and no side effects, so
// node --test imports it directly and build.mjs inlines it into the bundle.


const MODE_IDS = ['baseline', 'medium', 'low', 'extra-low', 'static'];

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// Saved values are strings or null; missing and unparseable ones use the
// documented default. (Number(null) is 0, so check for null first.)
function num(raw, fallback) {
  if (raw === null || raw === undefined || raw === '') return fallback;
  const v = Number(raw);
  return isFinite(v) ? v : fallback;
}

function modeSettings(id) {
  if (!MODE_IDS.includes(id)) throw new Error(`unknown sea mode: ${id}`);
  if (id === 'static') return { shader: false, renderScale: 1, uniforms: null };

  const uniforms = {
    ...seaConsts.baseline,
    flowMode: 1,        // 1 = LOD flow, 0 = coarse only
    noiseLayers: 2,
    travelMax: 0,
    snap: 1,
  };
  if (id !== 'baseline') {
    uniforms.par = 0;
    uniforms.pom = 1;
  }
  if (id === 'low' || id === 'extra-low') {
    uniforms.warp = 0;
    uniforms.flowMode = 0;
    uniforms.noiseLayers = 1;
  }
  return { shader: true, renderScale: id === 'extra-low' ? 0.5 : 1, uniforms };
}

// saved mode wins; weak devices (no WebGL2, small max texture, touch, narrow
// viewport) stay on the free 2D overlay
function pickMode(saved, env) {
  if (MODE_IDS.includes(saved)) return saved;
  return !env.webgl2 || env.maxTextureSize < 8192 || env.coarsePointer || env.narrow
    ? 'static'
    : 'baseline';
}

function parseState(raw) {
  const r = raw || {};
  return {
    mode: MODE_IDS.includes(r.mode) ? r.mode : '',
    on: !(r.on === false || r.on === '0' || r.on === 'false'),
    opacity: clamp(num(r.opacity, 1), 0, 1),
    // saved as a percentage (menu 50–150), used as a multiplier
    brightness: clamp(num(r.brightness, 100) / 100, 0.5, 1.5),
    fps30: r.fps30 === true || r.fps30 === '1' || r.fps30 === 'true',
  };
}

function renderSize(cssW, cssH, dpr, renderScale) {
  const k = Math.min(1, seaConsts.backingCap / Math.max(cssW * dpr, cssH * dpr));
  const w = Math.max(1, Math.round(cssW * dpr * k * renderScale));
  const h = Math.max(1, Math.round(cssH * dpr * k * renderScale));
  return { w, h, scale: w / cssW };
}

// Same padding maths as the 2D overlay, in CSS pixels, then scaled to the
// backing store. deck centres the view on target inside the padded area.
function cameraUniforms(vs, cssW, cssH, backingW, backingH) {
  const pad = vs.padding || {};
  const dx = clamp(((pad.left || 0) + cssW - (pad.right || 0)) / 2, 0, cssW) - cssW / 2;
  const dy = clamp(((pad.top || 0) + cssH - (pad.bottom || 0)) / 2, 0, cssH) - cssH / 2;
  const scale = backingW / cssW;
  return {
    zoomPx: 2 ** vs.zoom * scale,
    centerX: (cssW / 2 + dx) * scale,
    centerY: (cssH / 2 + dy) * scale,
    viewW: backingW,
    viewH: backingH,
  };
}

// nextAt starts at 0; without the 30 fps gate every frame draws
function fpsGate(now, nextAt, fps30) {
  if (!fps30) return { draw: true, nextAt };
  const draw = now >= nextAt;
  return { draw, nextAt: draw ? now + 1000 / 30 : nextAt };
}

function assetUrl(name) {
  return `${seaConsts.assetBase}/${name}?v=${seaConsts.version}`;
}

/* ---- sea-glsl.js ---- */
// Placeholder so the sea bundle keeps a fixed module order (core, glsl, runtime).
// Task 5 replaces it with the VERT and FRAG shader sources.

/* ---- sea.js ---- */
/* Sea depth overlay for the zig~zag exploration map.
 *
 * zig~zag draws its map with deck.gl into #deckgl-overlay, which clears to
 * transparent (the black page background shows through). We insert a canvas
 * directly behind it and paint an ocean into the gaps, using the same
 * OrthographicViewport maths deck uses, read live from React's viewState.
 *
 * The water itself is a pre-rendered bathymetry: distance-from-land with a
 * shelf/slope/abyss profile, regional depth variation, ridge/fracture noise
 * and a subtle NW hillshade. It is 1 px per map tile and drawn with smoothing
 * off, so one texel == one map cell and the grain matches the map's own tiles.
 */
(() => {
  'use strict';

  const SEA_URL = window.__ZZ_SEA_URL ||
    'https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist/zigzag-sea.webp?v=0.2.15';
  const MAP_W = 7749;          // main map extent, in tiles
  const MAP_H = 5254;
  const OV_ID = 'zz-sea';

  // Saved values live under the zz-sea-* keys; the pure core parses and clamps
  // them, and picks the mode. The capability probe arrives with the GL path.
  const saved = parseState({
    mode: localStorage.getItem('zz-sea-mode'),
    on: localStorage.getItem('zz-sea-on'),
    opacity: localStorage.getItem('zz-sea-opacity'),
    brightness: localStorage.getItem('zz-sea-brightness'),
    fps30: localStorage.getItem('zz-sea-fps'),
  });

  const state = { on: saved.on, mode: pickMode(saved.mode, {
                    webgl2: false, maxTextureSize: 0,
                    coarsePointer: false, narrow: false,
                  }),
                  img: null, raf: 0, sig: '', draws: 0,
                  opacity: saved.opacity, brightness: saved.brightness };
  window.__zzSea = state;

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const deckCanvas = () => document.getElementById('deckgl-overlay');

  // deck's view state lives on the React fiber of its canvas
  function viewState(deck) {
    const key = Object.keys(deck).find(k => k.startsWith('__reactFiber'));
    if (!key) return null;
    let f = deck[key];
    while (f && !(f.memoizedProps && f.memoizedProps.viewState)) f = f.return;
    return f ? f.memoizedProps.viewState : null;
  }

  function overlay(deck) {
    let ov = document.getElementById(OV_ID);
    if (!ov) {
      ov = document.createElement('canvas');
      ov.id = OV_ID;
    }
    if (ov.parentNode !== deck.parentNode || ov.nextElementSibling !== deck) {
      deck.parentNode.insertBefore(ov, deck);
    }
    ov.style.cssText = (deck.getAttribute('style') || '') + ';pointer-events:none';
    ov.style.opacity = String(state.opacity);
    // work in CSS pixels: quality settings and devicePixelRatio change the
    // canvas backing store, which would otherwise shift the projection
    const cw = deck.clientWidth || deck.width;
    const ch = deck.clientHeight || deck.height;
    if (ov.width !== cw || ov.height !== ch) {
      ov.width = cw;
      ov.height = ch;
    }
    return ov;
  }

  function draw() {
    const deck = deckCanvas();
    const img = state.img;
    if (!deck || !img) return;

    const vs = viewState(deck);
    if (!vs) return;

    const pad = vs.padding || {};
    const z = Math.pow(2, vs.zoom);
    const W = deck.clientWidth || deck.width;
    const H = deck.clientHeight || deck.height;

    const sig = [vs.zoom, vs.target[0], vs.target[1], pad.left, pad.right, W, H].join('|');
    if (sig === state.sig) return;
    state.sig = sig;

    const ov = overlay(deck);
    const c = ov.getContext('2d');

    // deck centres the view on target inside the padded area
    const dx = clamp(((pad.left || 0) + W - (pad.right || 0)) / 2, 0, W) - W / 2;
    const dy = clamp(((pad.top || 0) + H - (pad.bottom || 0)) / 2, 0, H) - H / 2;

    const pxt = img.width / MAP_W;                 // image px per tile
    const s = z / pxt;                             // screen px per image px
    const tx = vs.target[0] + 0.5, ty = vs.target[1] + 0.5;
    const ox = W / 2 + dx - tx * z;
    const oy = H / 2 + dy + (MAP_H - 0.5 - ty) * z;   // raster runs top-down

    // only the visible slice of the texture
    const wx0 = (0 - W / 2 - dx) / z + vs.target[0];
    const wx1 = (W - W / 2 - dx) / z + vs.target[0];
    const wy0 = (0 - H / 2 - dy) / z + vs.target[1];
    const wy1 = (H - H / 2 - dy) / z + vs.target[1];

    const sx0 = clamp(Math.floor((wx0 + 0.5) * pxt) - 1, 0, img.width);
    const sx1 = clamp(Math.ceil((wx1 + 0.5) * pxt) + 1, 0, img.width);
    const sy0 = clamp(Math.floor((MAP_H - 0.5 - wy1) * pxt) - 1, 0, img.height);
    const sy1 = clamp(Math.ceil((MAP_H - 0.5 - wy0) * pxt) + 1, 0, img.height);
    if (sx1 <= sx0 || sy1 <= sy0) return;

    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, W, H);
    c.imageSmoothingEnabled = false;               // one texel, one map cell
    c.setTransform(s, 0, 0, -s, ox, oy);
    c.drawImage(img, sx0, sy0, sx1 - sx0, sy1 - sy0, sx0, sy0, sx1 - sx0, sy1 - sy0);

    state.draws++;
  }

  function tick() {
    if (!state.on) { state.raf = 0; return; }
    try { draw(); } catch (e) { state.error = String(e); }
    state.raf = requestAnimationFrame(tick);
  }

  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => { state.img = img; state.sig = ''; if (!state.raf) tick(); };
  img.onerror = () => { state.error = 'sea texture failed to load'; };
  img.src = SEA_URL;
})();
