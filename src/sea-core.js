// Pure core of the sea overlay: quality modes, saved-state parsing, screen ↔
// map maths, the fps gate and asset URLs. No DOM and no side effects, so
// node --test imports it directly and build.mjs inlines it into the bundle.

import seaConsts from './sea-consts.json' with { type: 'json' };

const MODE_IDS = ['baseline', 'medium', 'low', 'extra-low', 'static'];

// The colour map is 7749 px wide, so a device below this cannot upload the
// shader's base texture at all. Shared by the startup probe (sea.js) and
// pickMode so the hard limit is stated once.
const MIN_TEXTURE_SIZE = 8192;

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function shaderCapable(webgl2, maxTextureSize) {
  return !!webgl2 && Number(maxTextureSize) >= MIN_TEXTURE_SIZE;
}

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
    // no warp: a little more open-water grain keeps the surface from reading flat
    uniforms.grain = 0.05;
  }
  return { shader: true, renderScale: id === 'extra-low' ? 0.5 : 1, uniforms };
}

// Bytes per texel of each uploaded bake under the sized internal formats the
// GL path uses, plus the three 1D curve LUTs (3 x 128 RGBA8 texels). Kept next
// to the mode table so the VRAM readout and its test share one source.
const TEX_BYTES = { colour: 4, dist: 1, aux: 3, flow: 3, speed: 1 };
const CURVE_BYTES = 3 * 128 * 4;

// GPU bytes for the shader's textures from the decoded sizes the runtime
// tracks (`{ colour: [w, h], ... }`). A size that is not uploaded yet is not
// counted; the curve LUTs always are.
function vramBytes(sizes) {
  let bytes = CURVE_BYTES;
  for (const [key, perTexel] of Object.entries(TEX_BYTES)) {
    const s = sizes && sizes[key];
    if (!s) continue;
    bytes += Number(s[0]) * Number(s[1]) * perTexel;
  }
  return bytes;
}

// saved mode wins; weak devices (no WebGL2, small max texture, touch, narrow
// viewport) stay on the free 2D overlay
function pickMode(saved, env) {
  if (MODE_IDS.includes(saved)) return saved;
  return shaderCapable(env.webgl2, env.maxTextureSize) &&
    !env.coarsePointer && !env.narrow
    ? 'baseline'
    : 'static';
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
    // gl_FragCoord is bottom-origin, so the padded centre (measured from the
    // top, as deck measures padding) flips into the shader's y
    centerY: backingH - (cssH / 2 + dy) * scale,
    viewW: backingW,
    viewH: backingH,
  };
}

// nextAt starts at 0; without the adaptive gate every frame draws. While the
// camera moves the gate is open (and resets nextAt, so the first still frame
// draws immediately); while it is still the 1 ms tolerance keeps exact 60 Hz
// steps (2 × 16.6667 ms) admitted instead of slipping to every third frame.
function fpsGate(now, nextAt, adaptive, moving) {
  if (!adaptive) return { draw: true, nextAt };
  if (moving) return { draw: true, nextAt: 0 };
  const draw = now >= nextAt - 1;
  return { draw, nextAt: draw ? now + 1000 / 30 : nextAt };
}

function assetUrl(name, base = seaConsts.assetBase) {
  return `${base}/${name}?v=${seaConsts.version}`;
}

export {
  MODE_IDS, modeSettings, pickMode, parseState, renderSize,
  cameraUniforms, fpsGate, assetUrl, shaderCapable, vramBytes,
};
