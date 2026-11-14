// Pure core of the sea overlay: quality modes, saved-state parsing, screen ↔
// map maths, the fps gate and asset URLs. No DOM and no side effects, so
// node --test imports it directly and build.mjs inlines it into the bundle.

import seaConsts from './sea-consts.json' with { type: 'json' };

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

export {
  MODE_IDS, modeSettings, pickMode, parseState, renderSize,
  cameraUniforms, fpsGate, assetUrl,
};
