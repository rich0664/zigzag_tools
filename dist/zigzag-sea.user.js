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

function assetUrl(name, base = seaConsts.assetBase) {
  return `${base}/${name}?v=${seaConsts.version}`;
}

/* ---- sea-glsl.js ---- */
// The sea shader, ported from prototype/sea-shader/index.html.
//
// The prototype drew a 1:1 crop at 1 texel per tile and carried distance, depth
// and the along-coast vector in one aux texture. The shipped layout is the full
// map, split for memory: distance stays 1 texel per tile (uDist.r), while depth
// (uAux.r) and the along-coast vector (uAux.gb) live on a 2-tile grid spanning
// uAuxSpan, and the baked current speed lives on a 4-tile grid. Land, flow
// detail, arrows, slices and the debug/curve-drag paths are gone. The three 1D
// tuning curves stay as LUT textures built from seaConsts.curves.
const VERT = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

const FRAG = `#version 300 es
precision highp float;
precision highp sampler2D;
out vec4 outColor;

uniform sampler2D uColour, uAux, uDist, uFlow, uSpeedTex, uCurve, uSliceCurve, uWarpCurve;
uniform vec2 uTarget, uSize, uFlowSize, uSpeedSize, uAuxSpan, uCenter;
uniform float uFlowCell;   // map tiles per coarse flow texel
uniform float uZoom, uTime, uDistMax;
uniform float uShoreW, uShelf, uFlowSpeed, uGrain, uFoam, uDeep, uSwell, uSnap, uWaveSpeed, uFlowMode, uZoomCss;
uniform float uCycle;   // seconds for one advection cycle
uniform float uDeflect, uDeflectReach;   // the bake's coastal bend (bake.py)
uniform float uTravelMax;                // cap on one cycle's advection (0 = none)
uniform float uWarp, uWarpBias;          // surface distortion: amount, depth bias
uniform vec2 uDirA, uDirB;
uniform float uSpeedA, uSpeedB, uScaleA, uScaleB;
uniform float uParallax, uDeepSlow, uWaveIn, uWaveOut, uPomLayers;
uniform float uAlongScale, uBrightness, uNoiseLayers;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}
float vnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  float a = hash(i), b = hash(i + vec2(1, 0));
  float c = hash(i + vec2(0, 1)), d = hash(i + vec2(1, 1));
  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p) {
  return 0.58 * vnoise(p) + 0.30 * vnoise(p * 2.03 + 11.1)
       + 0.12 * vnoise(p * 4.01 + 23.7);
}
// Ashima / Gustavson 2D simplex noise, roughly -1..1
vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec2 mod289(vec2 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec3 permute(vec3 x) { return mod289(((x * 34.0) + 1.0) * x); }
float snoise(vec2 v) {
  const vec4 C = vec4(0.211324865405187, 0.366025403784439,
                      -0.577350269189626, 0.024390243902439);
  vec2 i  = floor(v + dot(v, C.yy));
  vec2 x0 = v - i + dot(i, C.xx);
  vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
  vec4 x12 = x0.xyxy + C.xxzz;
  x12.xy -= i1;
  i = mod289(i);
  vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0))
                 + i.x + vec3(0.0, i1.x, 1.0));
  vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.0);
  m = m * m; m = m * m;
  vec3 x = 2.0 * fract(p * C.www) - 1.0;
  vec3 h = abs(x) - 0.5;
  vec3 ox = floor(x + 0.5);
  vec3 a0 = x - ox;
  m *= 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h);
  vec3 g;
  g.x  = a0.x * x0.x + h.x * x0.y;
  g.yz = a0.yz * x12.xz + h.yz * x12.yw;
  return 130.0 * dot(m, g);
}
float parCurve(float d) {
  return texture(uCurve, vec2(clamp(d, 0.0, 1.0), 0.5)).r;
}
// where the i-th POM slice samples, as a depth fraction 0..1. Linear = even
// spacing; flattening the curve bunches the slices together there, steepening
// it spreads them out.
float sliceCurveAt(float t) {
  return texture(uSliceCurve, vec2(clamp(t, 0.0, 1.0), 0.5)).r;
}
// how much of the distortion a pixel at this depth gets
float warpCurveAt(float t) {
  return texture(uWarpCurve, vec2(clamp(t, 0.0, 1.0), 0.5)).r;
}
// parallax is a scale about the view centre: a point at depth d is sampled at
// (target + (w-target)*(1-par)). Linear in w, so there is no singularity or
// shear at the centre -- it is the flat-map equivalent of a layer further away.
vec2 parShift(vec2 w, float depth) {
  // depth is a map-space quantity: scale the parallax with zoom so the same
  // number of map tiles shifts at every zoom (3.2 is the reference zoom)
  float k = uParallax * parCurve(depth) * (uZoomCss / 3.2);
  return (uTarget - w) * k;
}
// world position to sample the base colour at, for parallax depth dep
// (0 = surface, 1 = deepest). The shift is a per-texel quantity, so snapping
// the result to a texel centre moves a whole texel rigidly instead of
// resampling the seabed smoothly at screen resolution.
vec2 parPos(vec2 p, float dep) {
  vec2 w = p + parShift(p, dep);
  return mix(w, floor(w) + 0.5, uSnap);
}

void main() {
  vec2 world = uTarget + vec2(gl_FragCoord.x - uCenter.x,
                              uCenter.y - gl_FragCoord.y) / uZoom;
  // effects live on the texture's own pixel grid (1 texel = 1 tile), not on
  // the screen grid, so the grain matches the map's grain at every zoom
  vec2 p = mix(world, floor(world) + 0.5, uSnap);
  float d = texture(uDist, p / uSize).r;
  d = d * d * uDistMax;                     // back to tiles
  vec4 aux0 = texture(uAux, p / uAuxSpan);
  float depth01 = aux0.r;                   // 0 shallow .. 1 deep, from the height map
  // coarse 16-tile field, the 1:1 bake, or an LOD that uses the coarse field
  // and rebuilds the coastal detail from the distance field (no extra bytes)
  vec4 fcoarse = texture(uFlow, p / (uFlowCell * uFlowSize));
  vec2 cdir = fcoarse.rg * 2.0 - 1.0;
  float cspd = fcoarse.b;
  vec2 flow;
  float fspeed;
  if (uFlowMode > 0.5) {
    // LOD: the raw 16-tile gyre, bent toward the along-coast direction that the
    // bake stores in aux.GB (rotate90 of the blurred distance gradient, faded
    // to zero where that gradient is ill-defined). Same inputs the 1:1 bake
    // uses, so the two agree instead of the shader re-deriving it per pixel.
    vec2 along = (texture(uAux, p / uAuxSpan).gb * 2.0 - 1.0) * uAlongScale;
    // blend the *raw* interpolated gyre, not its direction: where two cells
    // disagree its length is < 1, and the generator weighs the bend against
    // that length. Normalising first over-rotates exactly where currents meet.
    vec2 cm = cdir;
    float near = uDeflect * exp(-d / max(1.0, uDeflectReach));
    vec2 bl = mix(cm, along, near);
    float bls = length(bl);
    flow = bls > 1e-4 ? bl / bls : cm;
    // speed: the baked field itself, 4 tiles/texel over the full map.
    fspeed = texture(uSpeedTex, p / (4.0 * uSpeedSize)).r;
  } else {
    // coarse only: no along blend, the gyre's own speed
    float m = length(cdir);
    flow = m > 1e-4 ? cdir / m : vec2(0.0);
    fspeed = cspd;
  }
  float wet = step(0.0001, d);

  // ---- advection cycle (shared by the shore lap, the shelf and open water) ----
  // The current pushes the pattern, but only for one cycle's worth of travel;
  // a second copy starts half a cycle behind and the two are cross-faded, so a
  // copy always resets while its weight is zero and the displacement stays
  // bounded. Without this the offset grows forever and steady currents smear
  // the noise into long streaks.
  float ph1 = fract(uTime / uCycle);
  float ph2 = fract(uTime / uCycle + 0.5);
  float w1 = 1.0 - abs(2.0 * ph1 - 1.0);
  float w2 = 1.0 - w1;
  // one cycle's travel is capped: at large flow x cycle the displacement gets
  // long enough that the folds around a pole cross several texels and crease
  float cycleTravel = min(fspeed * uFlowSpeed * uCycle,
                          uTravelMax > 0.0 ? uTravelMax : 1e6);

  // ---- open water: two noise layers crossing, then sliding with the current ----
  // (never the depth gradient: only the shore waves use distance)
  float deep = smoothstep(0.06, 0.75, d / uDistMax);
  float shallow = 1.0 - depth01;
  vec2 q = p;                                // the noise is not parallaxed
  float slow = mix(uDeepSlow, 1.0, shallow);
  // two-phase advection: the current moves the combined noise for one cycle's
  // travel only, and the second copy half a cycle behind is cross-faded in as
  // the first fades out - so it reads as endless without ever stretching
  float travel = cycleTravel * slow;
  vec2 s1 = q - flow * (travel * ph1);
  vec2 s2 = q - flow * (travel * ph2);
  // each layer also scrolls on its own fixed heading at its own speed and
  // scale (no rotation, no stretch)
  vec2 scrollA = uDirA * (uTime * uSpeedA);
  vec2 scrollB = uDirB * (uTime * uSpeedB);
  float nA = snoise((s1 - scrollA) * uScaleA) * w1 + snoise((s2 - scrollA) * uScaleA) * w2;
  // cross-fading two copies halves the contrast at the crossover; undo it so
  // the grain does not pulse once per cycle
  float amp = inversesqrt(w1 * w1 + w2 * w2);
  float nB = 0.0;
  float n = nA * amp;                        // one layer: A alone, no B sample
  if (uNoiseLayers > 1.5) {
    nB = snoise((s1 - scrollB) * uScaleB) * w1 + snoise((s2 - scrollB) * uScaleB) * w2;
    n = (nA + nB) * 0.5 * amp;
  }

  // surface distortion: the two noise layers warp where the seabed is sampled,
  // so the bed swims under the water. The depth bias is what keeps the shallow
  // end still: weight 0 at the surface, 1 in the deep, via the warp curve.
  float wdep = mix(1.0, warpCurveAt(depth01), uWarpBias);
  vec2 warp = vec2(nA, nB) * (uWarp * wdep);
  // everything parallax-related is computed from this displaced position
  vec2 base = p + warp;

  // parallax the base colour map, anchored to the view centre: deep water is
  // pulled slightly toward the centre (smaller features), so it reads as
  // further away; the screen centre itself never moves
  int N = int(uPomLayers + 0.5);
  vec3 directCol = texture(uColour, parPos(base, depth01) / uSize).rgb;
  vec3 col = directCol;
  if (N > 1) {
    // POM: ray from the top of the height range down; the first slice below
    // the terrain is the hit, and the two slices bracketing it are mixed by
    // the crossing fraction (soft edge, still occluded)
    float hPrev = 1.0;
    vec2 uvPrev = parPos(base, 0.0);
    float terPrev = 1.0 - texture(uAux, uvPrev / uAuxSpan).r;
    bool hit = false;
    for (int i = 0; i < 12; i++) {
      if (i >= N) break;
      // the slice curve decides where this step samples along the depth
      // range: where it is flat the slices crowd together, where it is steep
      // they are sparse
      float dep = sliceCurveAt((float(i) + 1.0) / float(N));
      float hRay = 1.0 - dep;
      vec2 sp = parPos(base, dep);
      float hTer = 1.0 - texture(uAux, sp / uAuxSpan).r;
      if (hRay <= hTer) {
        float a = hPrev, b = hRay;
        for (int k = 0; k < 4; k++) {
          float hm = 0.5 * (a + b);
          vec2 uvm = parPos(base, 1.0 - hm);
          float tm = 1.0 - texture(uAux, uvm / uAuxSpan).r;
          if (hm <= tm) b = hm; else a = hm;
        }
        vec2 uvHit = parPos(base, 1.0 - b);
        float above = hPrev - terPrev;
        float below = hRay - hTer;
        float frac = clamp(above / max(above - below, 1e-6), 0.0, 1.0);
        // blend the two slice positions by the crossing fraction and take one
        // sample: continuous in frac, so no slice banding
        vec2 hitPos = mix(uvPrev, uvHit, frac);
        col = texture(uColour, hitPos / uSize).rgb;
        hit = true;
        break;
      }
      hPrev = hRay;
      uvPrev = sp;
      terPrev = hTer;
    }
    if (!hit) {
      col = texture(uColour, parPos(base, 1.0) / uSize).rgb;
    }
  }

  // ---- shore: wave crests travel in towards land ----
  // distance isolines are parallel to the coast, so advancing the phase makes
  // each crest move to a smaller d: waves breaking on the shore
  float shore = (1.0 - smoothstep(0.0, uShoreW, d)) * wet;
  float lap2 = fbm((p - flow * (cycleTravel * 0.30 * ph1)) * 0.90 + 17.0) * w1
             + fbm((p - flow * (cycleTravel * 0.30 * ph2)) * 0.90 + 17.0) * w2;
  // wobble the isolines so the crests wander instead of tracing the contours,
  // and let some stretches of coast break while others stay calm
  float wob = (fbm(p * 0.14 + 31.0) - 0.5) * 2.2;
  float breakup = smoothstep(0.05, 0.60, fbm(p * 0.05 + 5.0));
  // two trains: one runs in, one runs out, with separate wavelengths, speeds
  // and a slow phase offset along the coast so the cycles are not in lockstep
  float phIn = (fbm(p * 0.045 + 7.0) - 0.5) * 6.2832;
  float phOut = (fbm(p * 0.055 + 23.0) - 0.5) * 6.2832;
  float wIn = sin((d + wob) * 1.05 + uTime * uWaveSpeed + phIn);
  float wOut = sin((d + wob * 1.3) * 0.74 - uTime * uWaveSpeed * 0.63 + phOut);
  // keep them separate: summing the sines first leaves one moving beat, and the
  // stronger train owns the visible crest
  // inbound is a sharp crest; outbound is a broad swell, so the eye can tell
  // the two directions apart even while they interfere
  float cIn = pow(max(wIn, 0.0), 3.0);
  float cOut = smoothstep(0.10, 0.90, wOut) * 0.55;
  float crest = (cIn * uWaveIn + cOut * uWaveOut) * breakup;
  col += vec3(0.58, 0.80, 0.94) * crest * shore * uFoam;

  // thin wet line on the first texel, broken by the same noise
  float edge = (1.0 - smoothstep(0.0, 0.85, d)) * wet;
  col += vec3(0.62, 0.84, 0.98) * edge
       * (0.30 + 0.95 * smoothstep(0.34, 0.80, lap2)) * uFoam * 0.8;

  // the baked shelf glow breathes on its own, much wider than the wet line
  float shelf = (1.0 - smoothstep(0.0, uShelf, d)) * wet;
  float shelfN = fbm((p - flow * (cycleTravel * 0.40 * ph1)) * 0.12 + 3.0) * w1
               + fbm((p - flow * (cycleTravel * 0.40 * ph2)) * 0.12 + 3.0) * w2 - 0.5;
  col *= 1.0 + shelfN * shelf * uFoam * 1.1;

  float grainN = fbm(q * 0.1 + 7.0) - 0.5;
  float dither = hash(floor(q)) - 0.5;
  float onWater = wet * (1.0 - shore * 0.85);
  col *= 1.0 + (n * 1.2 + grainN * 0.16)
               * uGrain * 1.2 * onWater * (1.0 - 0.45 * deep);
  col *= 1.0 + dither * uGrain * 0.25 * onWater;

  // ---- depth: darker, slightly cooler, with a huge slow swell ----
  col *= mix(1.0, uDeep, deep * wet);
  // a cooler, slightly richer blue with depth (no blur needed)
  col = mix(col, col * vec3(0.82, 0.94, 1.12) + vec3(0.0, 0.004, 0.010),
            deep * 0.55 * wet);
  float swell = fbm(p * 0.018 + vec2(uTime * 0.015, 13.0)) - 0.5;
  col *= 1.0 + swell * uSwell * deep * wet;

  outColor = vec4(col * uBrightness, 1.0);
}`;

/* ---- sea.js ---- */
/* Sea depth overlay for the zig~zag exploration map.
 *
 * zig~zag draws its map with deck.gl into #deckgl-overlay, which clears to
 * transparent (the black page background shows through). We insert a canvas
 * directly behind it and paint an ocean into the gaps, using the same
 * OrthographicViewport maths deck uses, read live from React's viewState.
 *
 * Two renderers share one mode switch:
 *   static    - the pre-rendered bathymetry WebP, blitted as before (one texel
 *               per map tile, smoothing off).
 *   animated  - a WebGL2 full-screen triangle running the ported sea shader
 *               (src/sea-glsl.js) over the full-map distance/aux/flow/speed
 *               bakes. Started only by an explicit setMode(); the startup mode
 *               stays static until the capability probe exists.
 */
(() => {
  'use strict';

  const MAP_W = seaConsts.map.w;   // main map extent, in tiles
  const MAP_H = seaConsts.map.h;
  const OV_ID = 'zz-sea';
  const GL_ID = 'zz-sea-gl';
  const WARP_MAX = 3.0;            // tiles of distortion at warp = 1

  const assetBase = window.__ZZ_SEA_ASSET_BASE || undefined;
  const url = name => assetUrl(name, assetBase);
  const errText = e => String((e && e.message) || e);

  // Saved values live under the zz-sea-* keys; the pure core parses and clamps
  // them, and picks the mode. The capability probe arrives with Task 8.
  const saved = parseState({
    mode: localStorage.getItem('zz-sea-mode'),
    on: localStorage.getItem('zz-sea-on'),
    opacity: localStorage.getItem('zz-sea-opacity'),
    brightness: localStorage.getItem('zz-sea-brightness'),
    fps30: localStorage.getItem('zz-sea-fps'),
  });

  // Until the capability probe exists, no saved mode can render: start static
  // so the build cannot claim an animated mode it has not verified. The saved
  // value stays in `saved.mode` for Task 8 to honour.
  const state = {
    on: saved.on,
    mode: pickMode('', {
      webgl2: false, maxTextureSize: 0, coarsePointer: false, narrow: false,
    }),
    img: null, raf: 0, sig: '', draws: 0,
    opacity: saved.opacity, brightness: saved.brightness,
    glReady: false, error: null,
  };
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

  // ---- static 2D path ------------------------------------------------------

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

  function drawStatic() {
    if (state.mode !== 'static') return;
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

  function staticTick() {
    state.raf = 0;
    if (state.mode !== 'static' || !state.on || document.hidden) return;
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    state.raf = requestAnimationFrame(staticTick);
  }

  function startStatic() {
    if (state.raf || state.mode !== 'static' || !state.on || document.hidden) return;
    state.raf = requestAnimationFrame(staticTick);
  }

  function stopStatic() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
  }

  function removeStaticCanvas() {
    const ov = document.getElementById(OV_ID);
    if (ov) ov.remove();
  }

  // ---- GL path -------------------------------------------------------------

  let glc = null;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(s);
      gl.deleteShader(s);
      throw new Error('shader compile: ' + log);
    }
    return s;
  }

  function link(gl) {
    const prog = gl.createProgram();
    const vs = compile(gl, gl.VERTEX_SHADER, VERT);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(prog);
      gl.deleteProgram(prog);
      throw new Error('shader link: ' + log);
    }
    return prog;
  }

  const UNIFORM_NAMES = [
    'uColour', 'uAux', 'uDist', 'uFlow', 'uSpeedTex', 'uCurve', 'uSliceCurve', 'uWarpCurve',
    'uTarget', 'uSize', 'uFlowSize', 'uSpeedSize', 'uAuxSpan', 'uCenter', 'uFlowCell',
    'uZoom', 'uTime', 'uDistMax', 'uShoreW', 'uShelf', 'uFlowSpeed', 'uGrain', 'uFoam',
    'uDeep', 'uSwell', 'uSnap', 'uWaveSpeed', 'uFlowMode', 'uZoomCss', 'uCycle',
    'uDeflect', 'uDeflectReach', 'uTravelMax', 'uWarp', 'uWarpBias', 'uDirA', 'uDirB',
    'uSpeedA', 'uSpeedB', 'uScaleA', 'uScaleB', 'uParallax', 'uDeepSlow', 'uWaveIn',
    'uWaveOut', 'uPomLayers', 'uAlongScale', 'uBrightness', 'uNoiseLayers',
  ];

  // modeSettings().uniforms keys that set one float uniform unchanged; the
  // rest (par, pom, cycle, warp, adir, bdir) are transformed below.
  const PLAIN_UNIFORM = {
    shore: 'uShoreW', shelf: 'uShelf', flow: 'uFlowSpeed', wave: 'uWaveSpeed',
    grain: 'uGrain', foam: 'uFoam', deep: 'uDeep', swell: 'uSwell',
    aspeed: 'uSpeedA', ascale: 'uScaleA', bspeed: 'uSpeedB', bscale: 'uScaleB',
    dslow: 'uDeepSlow', win: 'uWaveIn', wout: 'uWaveOut',
    wbias: 'uWarpBias', deflect: 'uDeflect', deflectReach: 'uDeflectReach',
    flowMode: 'uFlowMode', noiseLayers: 'uNoiseLayers', travelMax: 'uTravelMax',
    snap: 'uSnap',
  };

  function setModeUniforms(gl, uni, u) {
    const rad = deg => deg * Math.PI / 180;
    // iterate the table so its key set is the single source of truth (pinned
    // by test/sea-core.test.js)
    for (const k of Object.keys(u)) {
      switch (k) {
        case 'par': gl.uniform1f(uni.uParallax, -u.par); break;
        case 'pom': gl.uniform1f(uni.uPomLayers, Math.min(12, Math.max(1, u.pom))); break;
        case 'cycle': gl.uniform1f(uni.uCycle, Math.max(0.5, u.cycle)); break;
        case 'warp': gl.uniform1f(uni.uWarp, u.warp * WARP_MAX); break;
        case 'adir': gl.uniform2f(uni.uDirA, Math.cos(rad(u.adir)), Math.sin(rad(u.adir))); break;
        case 'bdir': gl.uniform2f(uni.uDirB, Math.cos(rad(u.bdir)), Math.sin(rad(u.bdir))); break;
        default: {
          const name = PLAIN_UNIFORM[k];
          if (name) gl.uniform1f(uni[name], u[k]);
        }
      }
    }
  }

  function loadTexture(gl, src, nearest) {
    const tex = gl.createTexture();
    const f = nearest ? gl.NEAREST : gl.LINEAR;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
        resolve({ tex, w: img.width, h: img.height });
      };
      img.onerror = () => { gl.deleteTexture(tex); reject(new Error('texture failed: ' + src)); };
      img.src = src;
    });
  }

  // 5-point polyline sampled linearly at 128 texels, as np.interp would
  function curveTexture(gl, pts) {
    const lut = new Uint8Array(128 * 4);
    for (let i = 0; i < 128; i++) {
      const x = (i / 127) * (pts.length - 1);
      const k = Math.min(pts.length - 2, Math.floor(x));
      const f = x - k;
      const v = Math.round((pts[k] * (1 - f) + pts[k + 1] * f) * 255);
      lut[i * 4] = lut[i * 4 + 1] = lut[i * 4 + 2] = v;
      lut[i * 4 + 3] = 255;
    }
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 128, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, lut);
    return tex;
  }

  function buildGL() {
    const deck = deckCanvas();
    if (!deck || !deck.parentNode) throw new Error('no deck canvas');

    const canvas = document.createElement('canvas');
    canvas.id = GL_ID;
    deck.parentNode.insertBefore(canvas, deck);

    const gl = canvas.getContext('webgl2', {
      alpha: false, premultipliedAlpha: false, antialias: false, depth: false,
    });
    if (!gl) { canvas.remove(); throw new Error('webgl2 unavailable'); }

    let prog;
    try { prog = link(gl); } catch (e) { canvas.remove(); throw e; }
    const uni = {};
    for (const name of UNIFORM_NAMES) uni[name] = gl.getUniformLocation(prog, name);

    gl.useProgram(prog);
    gl.uniform1i(uni.uColour, 0);
    gl.uniform1i(uni.uAux, 1);
    gl.uniform1i(uni.uDist, 2);
    gl.uniform1i(uni.uFlow, 3);
    gl.uniform1i(uni.uSpeedTex, 4);
    gl.uniform1i(uni.uCurve, 5);
    gl.uniform1i(uni.uSliceCurve, 6);
    gl.uniform1i(uni.uWarpCurve, 7);

    const c = {
      canvas, gl, prog, uni,
      tex: {
        curve: curveTexture(gl, seaConsts.curves.pc),
        slice: curveTexture(gl, seaConsts.curves.sc),
        warp: curveTexture(gl, seaConsts.curves.wc),
      },
      size: {}, raf: 0, ready: false, css: '',
    };
    glc = c;

    // colour NEAREST (1 texel per tile); aux/flow/speed LINEAR; dist NEAREST
    // (always sampled on a texel centre because snap is on)
    const specs = [
      ['colour', 'zigzag-sea.webp', true],
      ['dist', 'zigzag-sea-dist.webp', true],
      ['aux', 'zigzag-sea-aux.webp', false],
      ['flow', 'zigzag-sea-flow.webp', false],
      ['speed', 'zigzag-sea-speed.webp', false],
    ];
    Promise.all(specs.map(([key, name, nearest]) =>
      loadTexture(gl, url(name), nearest)
        .then(r => { c.tex[key] = r.tex; c.size[key] = [r.w, r.h]; }),
    )).then(() => {
      if (glc !== c) return;
      c.ready = true;
      state.glReady = true;
      state.error = null;
      startGL();
    }).catch(e => {
      if (glc === c) fallbackToStatic(e);
    });
  }

  function releaseGL() {
    if (!glc) return;
    if (glc.raf) cancelAnimationFrame(glc.raf);
    const { gl, canvas, prog } = glc;
    glc = null;
    state.glReady = false;
    try { gl.deleteProgram(prog); } catch {}
    const lose = gl.getExtension('WEBGL_lose_context');
    if (lose) { try { lose.loseContext(); } catch {} }
    canvas.remove();
  }

  function renderFrame(c, w, h, cam, t, settings) {
    const { gl, uni, tex, size } = c;
    gl.viewport(0, 0, w, h);
    gl.useProgram(c.prog);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.uniform2f(uni.uTarget, cam.tx, cam.ty);
    gl.uniform2f(uni.uSize, MAP_W, MAP_H);
    gl.uniform2f(uni.uFlowSize, size.flow[0], size.flow[1]);
    gl.uniform1f(uni.uFlowCell, MAP_W / size.flow[0]);
    gl.uniform2f(uni.uSpeedSize, size.speed[0], size.speed[1]);
    gl.uniform2f(uni.uAuxSpan, size.aux[0] * 2, size.aux[1] * 2);
    gl.uniform2f(uni.uCenter, cam.centerX, cam.centerY);
    gl.uniform1f(uni.uZoom, cam.zoom);
    gl.uniform1f(uni.uZoomCss, cam.zoomCss);
    gl.uniform1f(uni.uTime, t);
    gl.uniform1f(uni.uDistMax, seaConsts.distMax);
    gl.uniform1f(uni.uAlongScale, seaConsts.alongScale);
    gl.uniform1f(uni.uBrightness, state.brightness);
    setModeUniforms(gl, uni, settings.uniforms);

    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex.colour);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tex.aux);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, tex.dist);
    gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, tex.flow);
    gl.activeTexture(gl.TEXTURE4); gl.bindTexture(gl.TEXTURE_2D, tex.speed);
    gl.activeTexture(gl.TEXTURE5); gl.bindTexture(gl.TEXTURE_2D, tex.curve);
    gl.activeTexture(gl.TEXTURE6); gl.bindTexture(gl.TEXTURE_2D, tex.slice);
    gl.activeTexture(gl.TEXTURE7); gl.bindTexture(gl.TEXTURE_2D, tex.warp);

    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function drawGL(t) {
    if (!glc || !glc.ready) return;
    const deck = deckCanvas();
    if (!deck) return;
    const vs = viewState(deck);
    if (!vs) return;

    const cssW = deck.clientWidth || deck.width;
    const cssH = deck.clientHeight || deck.height;
    if (!cssW || !cssH) return;

    const canvas = glc.canvas;
    if (canvas.parentNode !== deck.parentNode || canvas.nextElementSibling !== deck) {
      deck.parentNode.insertBefore(canvas, deck);
    }
    const css = (deck.getAttribute('style') || '') +
      ';pointer-events:none;opacity:' + state.opacity;
    if (glc.css !== css) {
      glc.css = css;
      canvas.style.cssText = css;
    }

    const dpr = window.devicePixelRatio || 1;
    const settings = modeSettings(state.mode);
    const { w, h, scale } = renderSize(cssW, cssH, dpr, settings.renderScale);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const cam = cameraUniforms(vs, cssW, cssH, w, h);
    renderFrame(glc, w, h, {
      tx: vs.target[0], ty: vs.target[1],
      zoom: cam.zoomPx, zoomCss: cam.zoomPx / scale,
      centerX: cam.centerX, centerY: cam.centerY,
    }, t, settings);
    state.draws++;
  }

  function glTick(now) {
    if (!glc) return;
    glc.raf = 0;
    if (state.mode === 'static' || !state.on || document.hidden) return;
    try { drawGL(now / 1000); } catch (e) { fallbackToStatic(e); return; }
    glc.raf = requestAnimationFrame(glTick);
  }

  function startGL() {
    if (!glc || glc.raf || !glc.ready) return;
    if (state.mode === 'static' || !state.on || document.hidden) return;
    glc.raf = requestAnimationFrame(glTick);
  }

  function stopGL() {
    if (glc && glc.raf) cancelAnimationFrame(glc.raf);
    if (glc) glc.raf = 0;
  }

  function fallbackToStatic(reason) {
    state.error = errText(reason);
    releaseGL();
    state.mode = 'static';
    state.sig = '';
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    startStatic();
  }

  // ---- mode switch ---------------------------------------------------------

  function setMode(id) {
    modeSettings(id);                       // validates, throws on unknown ids
    if (state.mode === id && (id === 'static' || glc)) return id;

    state.mode = id;
    state.sig = '';
    if (id === 'static') {
      releaseGL();
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
    } else {
      stopStatic();
      removeStaticCanvas();
      if (glc) startGL();
      else {
        try { buildGL(); } catch (e) { fallbackToStatic(e); }
      }
    }
    return state.mode;
  }

  // One frame at an exact size and camera, read back synchronously. Used by the
  // parity check: it is the camera, not the live viewport, that decides.
  function debugRender(opts) {
    opts = opts || {};
    if (!glc || !glc.ready) throw new Error('gl path not ready');
    const w = Math.max(1, Math.round(Number(opts.width)));
    const h = Math.max(1, Math.round(Number(opts.height)));
    const zoom = Number(opts.zoom);
    const t = opts.t === undefined ? 0 : Number(opts.t);
    const tx = Number(opts.tx);
    const ty = Number(opts.ty);
    if (![w, h, zoom, t, tx, ty].every(isFinite)) {
      throw new Error('debugRender needs finite tx, ty, width, height, zoom');
    }

    const canvas = glc.canvas;
    const prevW = canvas.width, prevH = canvas.height;
    try {
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      renderFrame(glc, w, h, {
        tx, ty,
        zoom, zoomCss: zoom, centerX: w / 2, centerY: h / 2,
      }, t, modeSettings(state.mode));

      const px = new Uint8Array(w * h * 4);
      glc.gl.readPixels(0, 0, w, h, glc.gl.RGBA, glc.gl.UNSIGNED_BYTE, px);

      // flip the bottom-up GL rows into a top-down 2D canvas
      const out = document.createElement('canvas');
      out.width = w;
      out.height = h;
      const ctx = out.getContext('2d');
      const img = ctx.createImageData(w, h);
      const row = w * 4;
      for (let y = 0; y < h; y++) {
        img.data.set(px.subarray((h - 1 - y) * row, (h - y) * row), y * row);
      }
      ctx.putImageData(img, 0, 0);
      return out.toDataURL('image/png');
    } finally {
      if (canvas.width !== prevW || canvas.height !== prevH) {
        canvas.width = prevW;
        canvas.height = prevH;
      }
      if (state.mode !== 'static' && glc && glc.ready) {
        try { drawGL(performance.now() / 1000); } catch (e) { state.error = errText(e); }
      }
    }
  }

  state.setMode = setMode;
  state.debugRender = debugRender;

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopStatic(); stopGL(); }
    else if (state.mode === 'static') startStatic();
    else startGL();
  });

  // ---- startup -------------------------------------------------------------

  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    state.img = img;
    state.sig = '';
    if (state.mode === 'static') startStatic();
  };
  img.onerror = () => { state.error = 'sea texture failed to load'; };
  img.src = url('zigzag-sea.webp');

  startStatic();
})();
