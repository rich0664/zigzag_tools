// ==UserScript==
// @name         zig~zag sea depth
// @namespace    zigzagTools
// @version      0.3.0
// @description  Draws a bathymetric sea between the zig-zag.fm archipelagos: depth = distance from land, terrain = layered noise, rendered tile-aligned behind the map.
// @match        https://www.zig-zag.fm/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(() => {
const seaConsts = {"version":"0.3.0","assetBase":"https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist","map":{"w":7749,"h":5254},"distMax":600,"alongScale":1.03,"heightLo":0.1606,"heightHi":1,"auxScale":2,"flowCell":16,"speedCell":4,"backingCap":2560,"baseline":{"shore":8.8,"shelf":29,"flow":2.31,"wave":1.9,"grain":0.06,"foam":0.25,"deep":1,"swell":0.008,"adir":254,"aspeed":1.42,"ascale":0.156,"bdir":45,"bspeed":1.16,"bscale":0.12,"par":0.814,"pom":5,"dslow":1,"win":0.28,"wout":0.82,"cycle":7,"warp":3.3,"wbias":1,"deflect":1,"deflectReach":50},"curves":{"pc":[0,0.057,0.339,0.578,1],"sc":[0,0.057,0.274,0.492,0.926],"wc":[0,0.13,0.435,0.594,1]}};

/* ---- sea-core.js ---- */
// Pure core of the sea overlay: quality modes, saved-state parsing, screen ↔
// map maths, the fps gate and asset URLs. No DOM and no side effects, so
// node --test imports it directly and build.mjs inlines it into the bundle.


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
  }
  return { shader: true, renderScale: id === 'extra-low' ? 0.5 : 1, uniforms };
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

// nextAt starts at 0; without the 30 fps gate every frame draws. The 1 ms
// tolerance keeps exact 60 Hz steps (2 × 16.6667 ms) admitted: without it the
// sum lands a hair below nextAt and the gate slips to every third frame.
function fpsGate(now, nextAt, fps30) {
  if (!fps30) return { draw: true, nextAt };
  const draw = now >= nextAt - 1;
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
// the pixel is inside the map raster; outside it the overlay must stay clear
float within01(vec2 uv) {
  return step(0.0, uv.x) * step(uv.x, 1.0) * step(0.0, uv.y) * step(uv.y, 1.0);
}

void main() {
  // y is bottom-origin, like gl_FragCoord: the sea's texture rows run south at
  // row 0 to north at the last row, so world y grows upward on screen. (The
  // prototype's y-down world was mirrored against the real map; R17.)
  vec2 world = uTarget + vec2(gl_FragCoord.x - uCenter.x,
                              gl_FragCoord.y - uCenter.y) / uZoom;
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

  outColor = vec4(col * uBrightness, within01(p / uSize));
}`;

/* ---- sea-ui.js ---- */
/* Sea quality menu for the zig~zag HUD.
 *
 * A 30 px button under the notifications button (same right edge) opens a dark
 * panel with the sea on/off switch, the five quality ids, opacity, brightness
 * and the 30 fps limit. The menu owns no state: it paints getState() and
 * reports every change through onChange(patch). The runtime in sea.js applies
 * the change, persists it and returns the state to paint.
 *
 * The header has `contain: paint` and is 36 px tall, so the container lives on
 * document.body and is positioned `fixed` from the notifications button's live
 * rect. Its position is refreshed on window resize and by a ResizeObserver on
 * the header, never per frame.
 *
 * The userscript runs at document-idle, before this SPA has rendered the map
 * header. Mounting is therefore lazy: a MutationObserver on body (plus a 30 s
 * polling fallback) waits for the notifications button, and the same watcher
 * remounts the HUD if React later replaces or removes it.
 */

const SEA_UI_BTN_ID = 'zz-sea-btn';
const SEA_UI_MENU_ID = 'zz-sea-menu';
const SEA_UI_WRAP_ID = 'zz-sea-hud';
const SEA_UI_GAP = 6;             // px between the notifications button and ours
const SEA_UI_BTN = 30;            // both buttons are 30 x 30
const SEA_UI_MOUNT_MS = 30000;    // fallback polling stops this long after mount

// right/top of the fixed container and whether the panel must open above the
// button. Pure: node --test pins the anchor rule without a DOM.
function seaUiPlacement(rect, viewport, menuHeight, gap) {
  const g = gap === undefined ? SEA_UI_GAP : gap;
  const top = rect.bottom + g;
  return {
    right: viewport.width - rect.right,
    top,
    flipped: top + SEA_UI_BTN + g + menuHeight > viewport.height,
  };
}

function seaUiEl(tag, css) {
  const el = document.createElement(tag);
  if (css) el.style.cssText = css;
  return el;
}

function mountSeaUi(opts) {
  const getState = () => (opts && opts.getState ? opts.getState() : null);
  const onChange = patch => (opts && opts.onChange ? opts.onChange(patch) : null);

  let ui = null;                 // the mounted HUD, when the header exists
  let anchor = null;             // the notifications button it is anchored to

  function teardown() {
    if (ui) { ui.destroy(); ui = null; }
    anchor = null;
  }

  // Find / lose the notifications button. Runs on DOM mutations, the bounded
  // poll, and once at startup; it destroys a dead container before rebuilding.
  function tick() {
    if (ui && anchor && anchor.isConnected) return;
    teardown();
    const live = document.querySelector('button[aria-label^="Notifications"]');
    if (!live) return;
    const stale = document.getElementById(SEA_UI_WRAP_ID);
    if (stale) stale.remove();     // never leave two containers behind
    anchor = live;
    ui = build(live);
  }

  function refresh(state) {
    const next = state || getState();
    if (ui && next) ui.sync(next);
  }

  function build(liveAnchor) {
    // The app's Tailwind classes are purged, so copy the live HUD text style
    // from the notifications button once, at mount.
    const hud = getComputedStyle(liveAnchor);
    const applyFont = el => {
      el.style.fontFamily = hud.fontFamily;
      el.style.fontSize = hud.fontSize;
      el.style.letterSpacing = hud.letterSpacing;
      el.style.color = hud.color;
    };

    const wrap = seaUiEl('div',
      'position:fixed;width:30px;height:30px;pointer-events:none;z-index:21');
    wrap.id = SEA_UI_WRAP_ID;
    applyFont(wrap);

    // ---- button ------------------------------------------------------------

    const button = seaUiEl('button', [
      'position:relative', 'width:30px', 'height:30px', 'margin:0', 'padding:0',
      'background:transparent', 'border:0', 'cursor:pointer', 'opacity:.6',
      'display:inline-flex', 'align-items:center', 'justify-content:center',
      'pointer-events:auto', 'transition:opacity .2s',
    ].join(';'));
    button.id = SEA_UI_BTN_ID;
    button.type = 'button';
    button.title = 'sea';
    button.setAttribute('aria-label', 'sea options');
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    applyFont(button);

    const icon = seaUiEl('span', 'font-size:15px;line-height:1;pointer-events:none');
    icon.textContent = '≈';
    icon.setAttribute('aria-hidden', 'true');

    const label = seaUiEl('span', [
      'position:absolute', 'right:34px', 'top:50%', 'transform:translateY(-50%)',
      'white-space:nowrap', 'opacity:0', 'transition:opacity .2s',
      'pointer-events:none',
    ].join(';'));
    label.className = 'zz-sea-label';
    label.textContent = 'sea';
    applyFont(label);

    // 1 px underline while a lazy texture load is in flight; the menu's status
    // line carries the words.
    const loadingMark = seaUiEl('span', [
      'position:absolute', 'left:4px', 'right:4px', 'bottom:2px', 'height:1px',
      'background:currentColor', 'opacity:.8', 'pointer-events:none',
      'display:none',
    ].join(';'));
    loadingMark.id = 'zz-sea-loading';

    button.append(icon, label, loadingMark);

    // ---- menu --------------------------------------------------------------

    const rowCss = 'display:flex;align-items:center;gap:6px;margin:0 0 5px;' +
      'line-height:1.4;cursor:pointer';
    const groupCss = 'margin:7px 0 5px;border:0;padding:0';
    const titleCss = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;' +
      'opacity:.55;margin:0 0 4px';

    function row(input, text) {
      const el = seaUiEl('label', rowCss);
      el.append(input, document.createTextNode(text));
      return el;
    }

    const menu = seaUiEl('div', [
      'position:absolute', 'right:0', 'top:36px', 'width:190px',
      'box-sizing:border-box', 'background:#0a1016ee',
      'border:1px solid #ffffff22', 'border-radius:6px', 'padding:12px',
      'overflow-y:auto', 'pointer-events:auto',
    ].join(';'));
    menu.id = SEA_UI_MENU_ID;
    menu.hidden = true;
    menu.setAttribute('role', 'dialog');
    menu.setAttribute('aria-label', 'sea settings');
    applyFont(menu);

    const onBox = document.createElement('input');
    onBox.type = 'checkbox';
    onBox.style.margin = '0';
    onBox.style.accentColor = '#fff';
    const onRow = row(onBox, 'sea on/off');
    onRow.style.marginBottom = '6px';

    const quality = seaUiEl('fieldset', groupCss);
    const qualityTitle = seaUiEl('legend', titleCss);
    qualityTitle.textContent = 'quality';
    quality.append(qualityTitle);
    const radios = [];
    const labels = { 'extra-low': 'extra low' };
    for (const id of MODE_IDS) {
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'zz-sea-mode';
      input.value = id;
      input.style.margin = '0';
      input.style.accentColor = '#fff';
      radios.push(input);
      quality.append(row(input, labels[id] || id));
    }
    // canUseShader === false is the menu's honest report of a failed probe:
    // the animated entries are disabled and the note explains why.
    const note = seaUiEl('div', 'font-size:11px;line-height:1.35;opacity:.6;margin:2px 0 0');
    note.textContent = 'Animated modes need WebGL2 and an 8192 px texture limit.';
    note.hidden = true;
    quality.append(note);

    function rangeRow(text, min, max) {
      const el = seaUiEl('label', rowCss);
      const name = seaUiEl('span', 'flex:0 0 auto');
      name.textContent = text;
      const input = document.createElement('input');
      input.type = 'range';
      input.min = String(min);
      input.max = String(max);
      input.step = '1';
      input.style.flex = '1';
      input.style.minWidth = '0';
      input.style.margin = '0';
      input.style.accentColor = '#fff';
      const out = seaUiEl('span',
        'flex:0 0 34px;text-align:right;opacity:.7;font-variant-numeric:tabular-nums');
      el.append(name, input, out);
      return { el, input, out };
    }

    const opacity = rangeRow('opacity', 0, 100);
    const brightness = rangeRow('brightness', 50, 150);

    const fpsBox = document.createElement('input');
    fpsBox.type = 'checkbox';
    fpsBox.style.margin = '0';
    fpsBox.style.accentColor = '#fff';
    const fpsRow = row(fpsBox, '30 fps limit');
    fpsRow.style.marginBottom = '0';

    const status = document.createElement('div');
    status.id = 'zz-sea-status';
    status.style.cssText = 'font-size:11px;line-height:1.35;opacity:.75;margin-top:2px';

    const retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.id = 'zz-sea-retry';
    retryBtn.textContent = 'retry';
    retryBtn.style.cssText = 'display:none;margin-top:5px;padding:2px 8px;' +
      'background:transparent;border:1px solid #ffffff44;border-radius:3px;' +
      'color:inherit;font:inherit;font-size:11px;cursor:pointer';

    menu.append(onRow, quality, opacity.el, brightness.el, fpsRow, status, retryBtn);

    wrap.append(button, menu);
    document.body.appendChild(wrap);

    // ---- painting ----------------------------------------------------------

    function sync(s) {
      if (!s) return;
      onBox.checked = !!s.on;
      const shaderOff = s.canUseShader === false;
      for (const input of radios) {
        input.checked = input.value === s.mode;
        if (input.value !== 'static') input.disabled = shaderOff;
      }
      note.hidden = !shaderOff;
      const op = Math.round((Number.isFinite(s.opacity) ? s.opacity : 1) * 100);
      opacity.input.value = String(op);
      opacity.out.textContent = String(op);
      const br = Math.round((Number.isFinite(s.brightness) ? s.brightness : 1) * 100);
      brightness.input.value = String(br);
      brightness.out.textContent = `${br}%`;
      fpsBox.checked = !!s.fps30;

      // The status line carries lazy-load progress and the fallback reason;
      // retry is only offered where a retry can plausibly help.
      const loading = !!s.loading;
      loadingMark.style.display = loading ? 'block' : 'none';
      const err = s.error ? String(s.error) : '';
      status.textContent = loading ? 'loading sea…' : err;
      status.title = err;
      retryBtn.style.display =
        err && !loading && s.canUseShader !== false ? 'inline-block' : 'none';
    }

    function emit(patch) {
      const next = onChange(patch);
      if (next) sync(next);
    }

    // ---- position ----------------------------------------------------------

    function reposition() {
      const r = liveAnchor.getBoundingClientRect();
      if (!r.width && !r.height) return;
      let menuHeight = 0;
      if (!menu.hidden) {
        menu.style.maxHeight = 'none';
        menuHeight = menu.getBoundingClientRect().height;
      }
      const place = seaUiPlacement(
        { right: r.right, bottom: r.bottom },
        { width: window.innerWidth, height: window.innerHeight },
        menuHeight,
      );
      wrap.style.right = `${place.right}px`;
      wrap.style.top = `${place.top}px`;
      if (menu.hidden) return;

      // Keep the panel inside the viewport even when neither side has room for
      // its full height: the panel scrolls instead of being clipped.
      const below = Math.max(0, window.innerHeight - (place.top + 36) - 8);
      // flipped: the panel's bottom sits `gap` above the button's top
      const above = Math.max(0, place.top - SEA_UI_GAP - 8);
      menu.style.top = place.flipped ? 'auto' : '36px';
      menu.style.bottom = place.flipped ? '36px' : 'auto';
      menu.style.maxHeight = `${place.flipped ? above : below}px`;
    }

    // ---- open / close ------------------------------------------------------

    let open = false;

    function setOpen(next) {
      open = !!next;
      menu.hidden = !open;
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        sync(getState());          // pick up async runtime changes on open
        reposition();
      }
    }

    function showLabel(on) {
      label.style.opacity = on ? '.6' : '0';
      button.style.opacity = on ? '1' : '.6';
    }

    button.addEventListener('click', () => setOpen(!open));
    button.addEventListener('mouseenter', () => showLabel(true));
    button.addEventListener('mouseleave', () => showLabel(false));
    button.addEventListener('focus', () => showLabel(true));
    button.addEventListener('blur', () => showLabel(false));

    onBox.addEventListener('change', () => emit({ on: onBox.checked }));
    for (const input of radios) {
      input.addEventListener('change', () => {
        if (input.checked) emit({ mode: input.value });
      });
    }
    opacity.input.addEventListener('input', () => {
      opacity.out.textContent = opacity.input.value;
      emit({ opacity: Number(opacity.input.value) / 100 });
    });
    // brightness is shown in percent; the setter takes the 0.5..1.5 multiplier
    brightness.input.addEventListener('input', () => {
      brightness.out.textContent = `${brightness.input.value}%`;
      emit({ brightness: Number(brightness.input.value) / 100 });
    });
    fpsBox.addEventListener('change', () => emit({ fps30: fpsBox.checked }));
    retryBtn.addEventListener('click', () => emit({ retry: true }));

    const onResize = () => reposition();

    const onPointerDown = e => {
      if (open && !wrap.contains(e.target)) setOpen(false);
    };

    const onKeyDown = e => {
      if (open && e.key === 'Escape') {
        setOpen(false);
        button.focus();
      }
    };

    window.addEventListener('resize', onResize);
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);

    const header = liveAnchor.closest('header') || liveAnchor.parentElement;
    const observer = new ResizeObserver(() => reposition());
    observer.observe(header);

    sync(getState());
    reposition();

    function destroy() {
      observer.disconnect();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
      wrap.remove();
    }

    return { sync, destroy };
  }

  // ---- lifecycle -----------------------------------------------------------

  const target = document.body || document.documentElement;
  const observer = new MutationObserver(tick);
  observer.observe(target, { childList: true, subtree: true });

  const started = Date.now();
  const interval = setInterval(() => {
    tick();
    if (ui || Date.now() - started >= SEA_UI_MOUNT_MS) clearInterval(interval);
  }, 500);
  tick();

  return {
    refresh,
    destroy() {
      clearInterval(interval);
      observer.disconnect();
      teardown();
    },
  };
}

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
 *               bakes. Textures load on the first animated request, never for
 *               static. A throwaway context probes WebGL2 and MAX_TEXTURE_SIZE
 *               first; failures (probe, texture load/upload, context loss) fall
 *               back to static with a reason in the menu and a retry.
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

  // ---- capability probe ----------------------------------------------------
  // A throwaway context reads the texture limit, then is released so the page
  // keeps its context budget. Nothing is fetched for the shader before this.
  function probeShader() {
    try {
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl2');
      if (!gl) return { webgl2: false, maxTextureSize: 0 };
      const maxTextureSize = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || 0;
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) { try { lose.loseContext(); } catch {} }
      return { webgl2: true, maxTextureSize };
    } catch {
      return { webgl2: false, maxTextureSize: 0 };
    }
  }

  const probe = probeShader();
  const canUseShader = shaderCapable(probe.webgl2, probe.maxTextureSize);

  // Saved values live under the zz-sea-* keys; the pure core parses and clamps
  // them.
  const saved = parseState({
    mode: localStorage.getItem('zz-sea-mode'),
    on: localStorage.getItem('zz-sea-on'),
    opacity: localStorage.getItem('zz-sea-opacity'),
    brightness: localStorage.getItem('zz-sea-brightness'),
    fps30: localStorage.getItem('zz-sea-fps'),
  });

  const env = {
    webgl2: probe.webgl2, maxTextureSize: probe.maxTextureSize,
    coarsePointer: !!(window.matchMedia && matchMedia('(pointer: coarse)').matches),
    narrow: window.innerWidth < 700,
  };
  // A saved mode still wins over the weak-device default, but the probe's hard
  // limits beat everything: without a usable shader the overlay starts static
  // and the animated entries are disabled in the menu.
  const startupMode = canUseShader ? pickMode(saved.mode, env) : 'static';

  const state = {
    on: saved.on,
    mode: 'static',                // the live mode; the saved one is applied when ready
    img: null, raf: 0, sig: '', draws: 0,
    opacity: saved.opacity, brightness: saved.brightness, fps30: saved.fps30,
    glReady: false, error: null, loading: false,
    canUseShader, maxTextureSize: probe.maxTextureSize,
  };
  window.__zzSea = state;

  // The requested mode (menu choice / saved preference), kept across a
  // fallback so context restore and retry know what to rebuild.
  let wantedMode = startupMode;

  // The HUD is mounted lazily (the SPA header may not exist yet); the runtime
  // pushes state changes it makes outside the menu back into it.
  let seaUi = null;
  function refreshUi() {
    if (seaUi) seaUi.refresh(uiState());
  }

  // An animated mode requested before the deck canvas exists. It is applied by
  // the watcher below; it is never turned into `static` just because the app is
  // still rendering.
  let pendingMode = null;
  let deckObserver = null;
  let deckTimer = 0;
  let deckWatchStart = 0;

  // The first animated build takes seconds. Until its first GL frame the 2D
  // canvas stays visible (and keeps repainting with the camera) so the map is
  // never left blank.
  let holdStatic = false;
  const staticVisible = () => state.mode === 'static' || holdStatic;

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const deckCanvas = () => document.getElementById('deckgl-overlay');

  // The app hands its Deck instance to the map canvas through an imperative
  // handle, not through props, so it has to be found in the fiber tree: the
  // fiber's stateNode may carry it, otherwise a hook's current ref does. Only a
  // Deck whose own canvas is this canvas will do; the find is cached because
  // the fibers are walked on every frame.
  let deckInstance = null;
  function findDeck(canvas) {
    if (deckInstance && deckInstance.canvas === canvas) return deckInstance;
    const key = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
    if (!key) return null;
    const isDeck = v => !!(v && v.canvas === canvas && v.viewManager);
    for (let f = canvas[key]; f; f = f.return) {
      if (isDeck(f.stateNode && f.stateNode.deck)) return (deckInstance = f.stateNode.deck);
      for (let hook = f.memoizedState; hook; hook = hook.next) {
        if (isDeck(hook.memoizedState && hook.memoizedState.current && hook.memoizedState.current.deck)) {
          return (deckInstance = hook.memoizedState.current.deck);
        }
      }
    }
    return null;
  }

  // The controller updates deck.viewManager.viewState synchronously, while the
  // app's React state (the viewState prop) commits a frame or more later.
  // Reading the prop made the sea trail the map while panning, so the live
  // manager state wins and the prop is only a fallback. Every camera consumer
  // (2D draw, GL draw, bench, debugRender's live default) goes through here.
  function viewState(canvas) {
    const deck = findDeck(canvas);
    const propsState = () => {
      const key = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
      if (key) {
        for (let f = canvas[key]; f; f = f.return) {
          const vs = f.memoizedProps && f.memoizedProps.viewState;
          if (vs) return vs;
        }
      }
      return (deck && deck.props && deck.props.viewState) || null;
    };
    const live = deck && deck.viewManager && deck.viewManager.viewState;
    // the controller always publishes a complete viewState: take it as is and
    // leave the React fibers alone
    if (live && live.target && isFinite(live.zoom) && live.padding) {
      return { source: 'viewManager', target: live.target, zoom: live.zoom, padding: live.padding };
    }
    const fallback = propsState() || {};
    // a partial live update still wins for the fields it carries
    if (live && (live.target || isFinite(live.zoom))) {
      return {
        source: 'viewManager',
        target: live.target || fallback.target,
        zoom: isFinite(live.zoom) ? live.zoom : fallback.zoom,
        padding: live.padding || fallback.padding,
      };
    }
    return fallback.target
      ? { source: 'props', target: fallback.target, zoom: fallback.zoom, padding: fallback.padding }
      : null;
  }

  function liveViewState() {
    const c = deckCanvas();
    const vs = c && viewState(c);
    if (!vs) return null;
    return { source: vs.source, target: vs.target, zoom: vs.zoom, padding: vs.padding };
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
    // cssText replaced every style above, including the hide applied by
    // setOn(false); re-apply it so a rebuild while off cannot reveal the sea.
    ov.style.display = state.on ? '' : 'none';
    ov.style.opacity = String(state.opacity);
    ov.style.filter = `brightness(${state.brightness})`;
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

  function drawStatic(force) {
    if (!state.on || !staticVisible()) return;
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
    if (!force && sig === state.sig) return;
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

    // A lost WebGL canvas keeps its last composited frame. Hide (never remove)
    // it only now that the 2D replacement has painted, so there is never a
    // frame with no visible sea layer; webglcontextrestored still finds it.
    if (glc && glc.lost && glc.canvas && glc.canvas.style.display !== 'none') {
      glc.canvas.style.display = 'none';
    }
  }

  function staticTick() {
    state.raf = 0;
    if (!state.on || document.hidden || !staticVisible()) return;
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    state.raf = requestAnimationFrame(staticTick);
  }

  function startStatic() {
    if (state.raf || !state.on || document.hidden || !staticVisible()) return;
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

  // The colour map is the only texture static needs. It also feeds the GL
  // colour texture, so it is decoded once; releasing it while animated keeps a
  // 40 MPx bitmap out of memory next to the GPU copy.
  let colourPromise = null;

  function loadColour() {
    if (state.img) return Promise.resolve(state.img);
    if (colourPromise) return colourPromise;
    colourPromise = new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        colourPromise = null;
        state.img = img;
        state.sig = '';
        if (staticVisible()) startStatic();
        // A ready animated renderer already has the colour pixels in its
        // texture; a pending static/fallback decode must not leave the bitmap
        // resident after an animated mode resumes.
        if (glc && glc.ready && !glc.lost && state.mode !== 'static') state.img = null;
        resolve(img);
      };
      img.onerror = () => {
        colourPromise = null;
        state.error = 'texture: failed to load ' + url('zigzag-sea.webp');
        refreshUi();
        reject(new Error(state.error));
      };
      img.src = url('zigzag-sea.webp');
    });
    return colourPromise;
  }

  // ---- GL path -------------------------------------------------------------

  let glc = null;
  let nextAt = 0;                 // fps gate clock, ms; 0 admits the first frame

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
    let vs = null;
    let fs = null;
    try {
      vs = compile(gl, gl.VERTEX_SHADER, VERT);
      fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
      gl.attachShader(prog, vs);
      gl.attachShader(prog, fs);
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        throw new Error('shader link: ' + gl.getProgramInfoLog(prog));
      }
      gl.deleteShader(vs);
      gl.deleteShader(fs);
      return prog;
    } catch (e) {
      if (vs) gl.deleteShader(vs);
      if (fs) gl.deleteShader(fs);
      gl.deleteProgram(prog);
      throw e;
    }
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

  // Fetch + decode off the main thread where the browser supports it. The four
  // shader textures are 40 MPx; Image decoding on the main thread would stall
  // the page during the first animated switch.
  function decodeTexture(src) {
    if (typeof createImageBitmap === 'function') {
      return fetch(src, { mode: 'cors' })
        .then(r => {
          if (!r.ok) throw new Error(`texture: ${src} HTTP ${r.status}`);
          return r.blob();
        })
        .then(b => createImageBitmap(b));
    }
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('texture: failed to load ' + src));
      img.src = src;
    });
  }

  // The WebP bakes decode to RGBA, but only the channels the shader reads need
  // to reach the GPU. A sized internal format stores exactly those: RGBA8 for
  // the colour map, R8 for the two scalar fields, RGB8 for the multi-channel
  // ones. (Plain RGBA was ~377 MB; this is ~237 MB with the colour map.)
  const TEX_FORMAT = {
    colour: ['RGBA8', 'RGBA'],
    dist: ['R8', 'RED'],            // shader reads .r
    aux: ['RGB8', 'RGB'],           // .r depth, .gb along
    flow: ['RGB8', 'RGB'],          // .rg direction, .b the coarse-only speed
    speed: ['R8', 'RED'],           // shader reads .r
  };

  function uploadTexture(gl, source, nearest, key) {
    const [internal, format] = TEX_FORMAT[key];
    const tex = gl.createTexture();
    const f = nearest ? gl.NEAREST : gl.LINEAR;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, f);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, f);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);   // required for R8/RGB8 rows
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl[internal], gl[format], gl.UNSIGNED_BYTE, source);
    // A browser that rejects this internalformat/source pair must fail loudly:
    // silently uploading RGBA instead would keep 377 MB of VRAM.
    const err = gl.getError();
    const w = source.width;
    const h = source.height;
    if (typeof source.close === 'function') source.close();
    if (err !== gl.NO_ERROR) {
      gl.deleteTexture(tex);
      throw new Error(`texture: ${key} upload GL error 0x${err.toString(16)}`);
    }
    return { tex, w, h };
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

  function drainErrors(gl) {
    for (let i = 0; i < 16 && gl.getError() !== gl.NO_ERROR; i++) { /* clear sticky errors */ }
  }

  function buildGL() {
    const deck = deckCanvas();
    if (!deck || !deck.parentNode) throw new Error('no deck canvas');
    if (!canUseShader) {
      throw new Error('shader unavailable: MAX_TEXTURE_SIZE ' + probe.maxTextureSize);
    }

    // A lost context's canvas is kept for webglcontextrestored; a rebuild owns
    // the slot again and the old one goes.
    const stale = document.getElementById(GL_ID);
    if (stale) stale.remove();

    const canvas = document.createElement('canvas');
    canvas.id = GL_ID;
    if (!state.on) canvas.style.display = 'none';
    deck.parentNode.insertBefore(canvas, deck);

    const gl = canvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: false, antialias: false, depth: false,
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
      size: {}, raf: 0, ready: false, css: '', lost: false, first: false,
      onLost: null, onRestored: null,
    };
    glc = c;
    c.onLost = e => onContextLost(c, e);
    c.onRestored = () => onContextRestored(c);
    canvas.addEventListener('webglcontextlost', c.onLost);
    canvas.addEventListener('webglcontextrestored', c.onRestored);

    drainErrors(gl);
    state.loading = true;
    refreshUi();

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
      // The colour decode is shared with the 2D path; a second fetch of the
      // same 40 MPx WebP is both slow and memory.
      (key === 'colour' ? loadColour() : decodeTexture(url(name)))
        .then(source => {
          if (glc !== c || c.lost) {
            if (typeof source.close === 'function') source.close();
            return;
          }
          const up = uploadTexture(gl, source, nearest, key);
          c.tex[key] = up.tex;
          c.size[key] = [up.w, up.h];
        }),
    )).then(() => {
      if (glc !== c || c.lost) return;
      // No silent ready: an upload error or an oversized texture means the
      // device cannot hold this mode, even though WebGL2 exists.
      const glErr = gl.getError();
      if (glErr !== gl.NO_ERROR) {
        throw new Error('texture: GL error 0x' + glErr.toString(16));
      }
      const max = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || probe.maxTextureSize;
      for (const key of ['colour', 'dist', 'aux', 'flow', 'speed']) {
        const [w, h] = c.size[key] || [0, 0];
        if (!w || !h || w > max || h > max) {
          throw new Error(`texture: ${key} ${w}x${h} > MAX_TEXTURE_SIZE ${max}`);
        }
      }
      c.ready = true;
      state.glReady = true;
      state.loading = false;
      state.error = null;
      startGL();
      refreshUi();
    }).catch(e => {
      state.loading = false;
      if (glc === c && !c.lost) fallbackToStatic(e);
      else refreshUi();
    });
  }

  // Paint the 2D path over a lost context. drawStatic hides the lost canvas
  // only after it has actually painted; until then the retained GL frame is
  // the visible sea.
  function paintStaticOverLost() {
    state.sig = '';
    try { drawStatic(); } catch (e) { state.error = errText(e); }
    startStatic();
  }

  // A lost context keeps its canvas and listeners so the browser can restore
  // it. The live mode falls back immediately; the requested animated mode is
  // remembered for the restored event.
  function onContextLost(c, e) {
    if (e && e.preventDefault) e.preventDefault();
    if (glc !== c || c.lost) return;
    c.lost = true;
    c.ready = false;
    stopGL();
    state.glReady = false;
    state.loading = false;
    state.mode = 'static';
    state.sig = '';
    holdStatic = false;
    state.error = 'context lost' +
      (e && e.statusMessage ? ': ' + e.statusMessage : '');
    // The lost canvas still shows its last composited frame. Keep it visible
    // until the 2D replacement paints; drawStatic hides it after that.
    if (state.img) {
      paintStaticOverLost();
    } else {
      loadColour().then(() => {
        if (glc !== c || !c.lost) return;
        paintStaticOverLost();
      }).catch(err => {
        if (glc === c) { state.error = errText(err); refreshUi(); }
      });
    }
    refreshUi();
  }

  function onContextRestored(c) {
    if (glc !== c) return;
    const canvas = c.canvas;
    glc = null;
    c.ready = false;
    state.glReady = false;
    if (canvas) {
      canvas.removeEventListener('webglcontextlost', c.onLost);
      canvas.removeEventListener('webglcontextrestored', c.onRestored);
      canvas.remove();
    }
    if (!canUseShader) { refreshUi(); return; }
    if (!wantedMode || wantedMode === 'static') {
      state.error = null;
      refreshUi();
      return;
    }
    try { setMode(wantedMode); } catch (e) { fallbackToStatic(e); }
  }

  function releaseGL() {
    if (!glc) return;
    const c = glc;
    glc = null;
    if (c.raf) cancelAnimationFrame(c.raf);
    state.glReady = false;
    if (c.canvas) {
      if (c.onLost) c.canvas.removeEventListener('webglcontextlost', c.onLost);
      if (c.onRestored) c.canvas.removeEventListener('webglcontextrestored', c.onRestored);
    }
    try { c.gl.deleteProgram(c.prog); } catch {}
    for (const key of Object.keys(c.tex || {})) {
      try { c.gl.deleteTexture(c.tex[key]); } catch {}
    }
    if (!c.lost) {
      const lose = c.gl.getExtension('WEBGL_lose_context');
      if (lose) { try { lose.loseContext(); } catch {} }
    }
    if (c.canvas) c.canvas.remove();
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

  // The backing store follows the deck CSS size and the tier's render scale.
  // Only the drawing buffer changes on a tier switch; the program and every
  // texture survive, so the next frame redraws with new uniforms alone.
  function sizeGL(renderScale) {
    if (!glc) return null;
    const deck = deckCanvas();
    if (!deck) return null;
    const cssW = deck.clientWidth || deck.width;
    const cssH = deck.clientHeight || deck.height;
    if (!cssW || !cssH) return null;

    const { w, h, scale } = renderSize(
      cssW, cssH, window.devicePixelRatio || 1, renderScale,
    );
    const canvas = glc.canvas;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    return { cssW, cssH, w, h, scale };
  }

  // deck's target[1] is the map's page row (larger = further down the page)
  // and the bakes store the map with south at row 0, so the shader's texture
  // row is MAP_H - 1 - target.y. x is a pixel centre, as the 2D path uses.
  // debugRender takes texture rows directly and never goes through this.
  const seaTarget = vs => [vs.target[0] + 0.5, MAP_H - 1 - vs.target[1]];

  function drawGL(t) {
    if (!glc || !glc.ready || glc.lost) return;
    const deck = deckCanvas();
    if (!deck) return;
    const vs = viewState(deck);
    if (!vs) return;

    const settings = modeSettings(state.mode);
    const sized = sizeGL(settings.renderScale);
    if (!sized) return;
    const { cssW, cssH, w, h, scale } = sized;

    const canvas = glc.canvas;
    if (canvas.parentNode !== deck.parentNode || canvas.nextElementSibling !== deck) {
      deck.parentNode.insertBefore(canvas, deck);
    }
    const css = (deck.getAttribute('style') || '') +
      ';pointer-events:none;opacity:' + state.opacity +
      (state.on ? '' : ';display:none');
    if (glc.css !== css) {
      glc.css = css;
      canvas.style.cssText = css;
    }

    const cam = cameraUniforms(vs, cssW, cssH, w, h);
    const target = seaTarget(vs);
    renderFrame(glc, w, h, {
      tx: target[0], ty: target[1],
      zoom: cam.zoomPx, zoomCss: cam.zoomPx / scale,
      centerX: cam.centerX, centerY: cam.centerY,
    }, t, settings);
    state.draws++;

    // The first real frame means the 2D canvas can go: the opaque-enough water
    // is already in the drawing buffer, so removing it cannot show a blank.
    if (!glc.first && state.on) {
      glc.first = true;
      holdStatic = false;
      removeStaticCanvas();
    }
    // Any live animated frame owns the colour pixels; holding the decoded
    // bitmap as well keeps ~163 MB resident. This also covers a fast
    // static -> animated round trip where a pending static decode resolves
    // after the animated loop has resumed.
    if (state.img && state.mode !== 'static') state.img = null;
  }

  function glTick(now) {
    if (!glc) return;
    glc.raf = 0;
    if (glc.lost || state.mode === 'static' || !state.on || document.hidden) return;
    try {
      // the loop still runs every rAF; the gate decides which frames draw
      const gate = fpsGate(now, nextAt, state.fps30);
      nextAt = gate.nextAt;
      if (gate.draw) drawGL(now / 1000);
    } catch (e) {
      // A GL exception (including a lost context surfacing as one) is a
      // fallback, never a page error.
      fallbackToStatic(e);
      return;
    }
    if (glc) glc.raf = requestAnimationFrame(glTick);
  }

  function startGL() {
    if (!glc || glc.raf || !glc.ready || glc.lost) return;
    if (state.mode === 'static' || !state.on || document.hidden) return;
    glc.raf = requestAnimationFrame(glTick);
  }

  function stopGL() {
    if (glc && glc.raf) cancelAnimationFrame(glc.raf);
    if (glc) glc.raf = 0;
  }

  // The animated renderer is gone; paint the 2D path again. While an
  // animated build was loading, its canvases and the decoded colour image are
  // still around, so this repaints without a gap. Otherwise the colour decode
  // is requested and the dead canvas is dropped once it can be replaced.
  function fallbackToStatic(reason) {
    state.error = errText(reason);
    state.loading = false;
    pendingMode = null;
    state.mode = 'static';
    state.sig = '';
    holdStatic = false;
    if (glc && glc.lost) {
      // keep the lost canvas alive for webglcontextrestored; it stays visible
      // until the 2D path paints over it
      stopGL();
      if (state.img) {
        paintStaticOverLost();
      } else {
        loadColour().then(() => {
          if (glc && glc.lost) paintStaticOverLost();
        }).catch(e => {
          state.error = errText(e);
          refreshUi();
        });
      }
      refreshUi();
      return;
    }
    if (state.img) {
      releaseGL();
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      refreshUi();
      return;
    }
    // The colour image was released while animated: keep the last GL frame up
    // until the cached WebP decodes, then swap the 2D canvas in. A token drops
    // the swap if a newer mode change has taken over.
    const token = ++staticToken;
    stopGL();
    loadColour().then(() => {
      if (token !== staticToken || state.mode !== 'static') return;
      releaseGL();
      state.sig = '';
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      refreshUi();
    }).catch(e => {
      if (token !== staticToken) return;
      releaseGL();
      state.error = errText(e);
      refreshUi();
    });
  }

  // Switching to static from a running animated mode: the decoded colour image
  // is gone, so the GL canvas stays up until the cached image can replace it.
  let staticToken = 0;

  function enterStatic() {
    const token = ++staticToken;
    stopStatic();
    state.sig = '';
    holdStatic = false;
    if (state.img) {
      releaseGL();
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      return;
    }
    stopGL();
    loadColour().then(() => {
      if (token !== staticToken || state.mode !== 'static') return;
      releaseGL();
      state.sig = '';
      try { drawStatic(); } catch (e) { state.error = errText(e); }
      startStatic();
      refreshUi();
    }).catch(e => {
      if (token !== staticToken) return;
      releaseGL();
      state.error = errText(e);
      refreshUi();
    });
  }

  // ---- mode switch ---------------------------------------------------------

  function setMode(id) {
    modeSettings(id);                       // validates, throws on unknown ids
    if (id !== 'static' && !canUseShader) {
      wantedMode = id;
      state.error = 'shader unavailable: MAX_TEXTURE_SIZE ' + probe.maxTextureSize;
      state.loading = false;
      state.mode = 'static';
      state.sig = '';
      holdStatic = false;
      if (state.img) {
        try { drawStatic(); } catch (e) { state.error = errText(e); }
        startStatic();
      }
      refreshUi();
      return state.mode;
    }

    wantedMode = id;
    if (state.mode === id && id !== 'static' && glc && glc.ready && !glc.lost) return id;

    state.mode = id;
    state.sig = '';
    if (id === 'static') {
      state.error = null;
      state.loading = false;
      enterStatic();
    } else {
      state.error = null;
      stopStatic();
      // Tier switches reuse the program and textures; the first build keeps
      // the 2D canvas visible until a GL frame exists.
      if (glc && glc.ready && !glc.lost) {
        try {
          sizeGL(modeSettings(id).renderScale);
          nextAt = 0;                       // paint on this tick, not the next
          drawGL(performance.now() / 1000);
        } catch (e) {
          fallbackToStatic(e);
          return state.mode;
        }
        startGL();
      } else if (glc && !glc.lost) {
        // a build is already in flight; it will render with the current mode
        startStatic();
        startGL();
      } else {
        holdStatic = true;
        try { buildGL(); } catch (e) { fallbackToStatic(e); }
        // keep the 2D overlay following the camera while the textures load
        startStatic();
      }
    }
    refreshUi();
    return state.mode;
  }

  // ---- deferred mode and the deck watcher ---------------------------------
  // The userscript can start before the SPA has rendered #deckgl-overlay. An
  // animated mode requested then waits here; it is applied as soon as the deck
  // appears, and the saved preference is never rewritten to static for it.

  function stopDeckWatch() {
    if (deckObserver) { deckObserver.disconnect(); deckObserver = null; }
    if (deckTimer) { clearInterval(deckTimer); deckTimer = 0; }
  }

  function applyPendingMode() {
    if (!pendingMode || !state.on) return false;
    if (pendingMode !== 'static' && !deckCanvas()) return false;
    const id = pendingMode;
    pendingMode = null;
    stopDeckWatch();
    try { setMode(id); } catch (e) { fallbackToStatic(e); }
    return true;
  }

  function watchForDeck() {
    if (deckObserver) return;
    deckWatchStart = Date.now();
    const target = document.body || document.documentElement;
    deckObserver = new MutationObserver(() => {
      if (applyPendingMode()) stopDeckWatch();
    });
    deckObserver.observe(target, { childList: true, subtree: true });
    deckTimer = setInterval(() => {
      if (applyPendingMode() || Date.now() - deckWatchStart >= 30000) {
        if (deckTimer) { clearInterval(deckTimer); deckTimer = 0; }
      }
    }, 500);
  }

  // The menu's mode choice: persist the choice, not whatever the live mode
  // settles on if the app is not ready yet.
  function requestMode(id) {
    modeSettings(id);                       // validates, throws on unknown ids
    wantedMode = id;
    if (id !== 'static' && !deckCanvas()) {
      pendingMode = id;
      watchForDeck();
      applyPendingMode();                   // in case it arrived just now
      refreshUi();
      return state.mode;
    }
    pendingMode = null;
    stopDeckWatch();
    try { return setMode(id); } catch (e) { fallbackToStatic(e); return state.mode; }
  }

  // Retry the animated mode that failed. The saved/requested id was left
  // intact by fallbackToStatic, so a texture or context failure can be tried
  // again after the network or GPU recovers.
  function retry() {
    if (!canUseShader) {
      state.error = 'shader unavailable: MAX_TEXTURE_SIZE ' + probe.maxTextureSize;
      refreshUi();
      return state.mode;
    }
    const target = wantedMode && wantedMode !== 'static'
      ? wantedMode
      : pickMode('', env);
    return requestMode(target);
  }

  // One frame at an exact size and camera, read back synchronously. Used by the
  // parity check: it is the camera, not the live viewport, that decides.
  function debugRender(opts) {
    opts = opts || {};
    if (!glc || !glc.ready || glc.lost) throw new Error('gl path not ready');
    // no tx/ty: render the live view through the same camera the loop uses
    let live = null;
    if (opts.tx === undefined && opts.ty === undefined) {
      const c = deckCanvas();
      const vs = c && viewState(c);
      if (!vs) throw new Error('debugRender has no live camera');
      live = seaTarget(vs);
      opts = {
        ...opts,
        zoom: opts.zoom === undefined ? 2 ** vs.zoom : opts.zoom,
        width: opts.width === undefined ? (c.clientWidth || c.width) : opts.width,
        height: opts.height === undefined ? (c.clientHeight || c.height) : opts.height,
      };
    }
    const w = Math.max(1, Math.round(Number(opts.width)));
    const h = Math.max(1, Math.round(Number(opts.height)));
    const zoom = Number(opts.zoom);
    const t = opts.t === undefined ? 0 : Number(opts.t);
    const tx = live ? live[0] : Number(opts.tx);
    const ty = live ? live[1] : Number(opts.ty);
    if (![w, h, zoom, t, tx, ty].every(isFinite)) {
      throw new Error('debugRender needs finite tx, ty, width, height, zoom');
    }

    const canvas = glc.canvas;
    const prevW = canvas.width, prevH = canvas.height;
    // width/height are the camera extent in CSS-style pixels; extra-low renders
    // the same view into half the backing store, as the live tier does
    const settings = modeSettings(state.mode);
    const scale = settings.renderScale;
    const bw = Math.max(1, Math.round(w * scale));
    const bh = Math.max(1, Math.round(h * scale));
    try {
      if (canvas.width !== bw || canvas.height !== bh) {
        canvas.width = bw;
        canvas.height = bh;
      }
      renderFrame(glc, bw, bh, {
        tx, ty,
        zoom: zoom * scale, zoomCss: zoom,
        centerX: bw / 2, centerY: bh / 2,
      }, t, settings);

      const px = new Uint8Array(bw * bh * 4);
      glc.gl.readPixels(0, 0, bw, bh, glc.gl.RGBA, glc.gl.UNSIGNED_BYTE, px);

      // flip the bottom-up GL rows into a top-down 2D canvas. readPixels
      // returns the straight-alpha bytes the shader wrote (the context is not
      // premultiplied); putImageData takes straight alpha, so toDataURL keeps
      // the transparent margin transparent instead of flattening it.
      const out = document.createElement('canvas');
      out.width = bw;
      out.height = bh;
      const ctx = out.getContext('2d');
      const img = ctx.createImageData(bw, bh);
      const row = bw * 4;
      for (let y = 0; y < bh; y++) {
        img.data.set(px.subarray((bh - 1 - y) * row, (bh - y) * row), y * row);
      }
      ctx.putImageData(img, 0, 0);
      return out.toDataURL('image/png');
    } finally {
      if (canvas.width !== prevW || canvas.height !== prevH) {
        canvas.width = prevW;
        canvas.height = prevH;
      }
      if (state.mode !== 'static' && glc && glc.ready && !glc.lost) {
        try { drawGL(performance.now() / 1000); } catch (e) { state.error = errText(e); }
      }
    }
  }

  // gl.finish() is a no-op on some drivers (headless SwiftShader included), so
  // a one-pixel readback is the portable way to wait for queued frames.
  function syncGL(gl) {
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
  }

  // 60 frames at the live camera and backing size, then hand the frame back to
  // the loop. Static has no animation clock, so it measures 60 forced 2D
  // blits of the current view instead.
  function bench() {
    const mode = state.mode;
    try {
      if (mode === 'static') {
        if (!state.img) throw new Error('static not ready');
        const t0 = performance.now();
        for (let i = 0; i < 60; i++) drawStatic(true);
        const ms = performance.now() - t0;
        startStatic();
        return { mode, ms, fps: ms > 0 ? 60000 / ms : 0 };
      }

      if (!glc || !glc.ready || glc.lost) throw new Error('gl path not ready');
      const deck = deckCanvas();
      const vs = deck && viewState(deck);
      if (!vs) throw new Error('no camera');
      const settings = modeSettings(mode);
      const sized = sizeGL(settings.renderScale);
      if (!sized) throw new Error('no viewport');

      const cam = cameraUniforms(vs, sized.cssW, sized.cssH, sized.w, sized.h);
      const target = seaTarget(vs);
      const frame = {
        tx: target[0], ty: target[1],
        zoom: cam.zoomPx, zoomCss: cam.zoomPx / sized.scale,
        centerX: cam.centerX, centerY: cam.centerY,
      };
      const start = performance.now();
      let ms;
      try {
        // Warm up, then wait for the GPU so the clock measures work instead of
        // command submission.
        renderFrame(glc, sized.w, sized.h, frame, start / 1000, settings);
        syncGL(glc.gl);
        const t0 = performance.now();
        for (let i = 0; i < 60; i++) {
          renderFrame(glc, sized.w, sized.h, frame, start / 1000 + i / 60, settings);
        }
        syncGL(glc.gl);
        ms = performance.now() - t0;
      } finally {
        // Restore the live frame and loop whatever happened during the run.
        nextAt = 0;
        try { drawGL(performance.now() / 1000); } catch (e) { fallbackToStatic(e); }
        startGL();
      }
      return { mode: state.mode, ms, fps: ms > 0 ? 60000 / ms : 0 };
    } catch (e) {
      state.error = errText(e);
      refreshUi();
      return { mode: state.mode, ms: 0, fps: 0, error: state.error };
    }
  }

  // ---- menu-facing setters -------------------------------------------------
  // Brightness is the 0.5..1.5 multiplier, the same unit parseState returns and
  // the shader/CSS use; the menu passes its percentage divided by 100.
  function setBrightness(multiplier) {
    const v = Number(multiplier);
    state.brightness = clamp(isFinite(v) ? v : 1, 0.5, 1.5);
    const ov = document.getElementById(OV_ID);
    if (ov) ov.style.filter = `brightness(${state.brightness})`;
    refreshUi();
    return state.brightness;
  }

  // Opacity is a CSS style on whichever canvas is live; the GL loop rewrites
  // its inline style from `glc.css` each frame, so clear the cache too.
  function setOpacity(value) {
    const v = Number(value);
    state.opacity = clamp(isFinite(v) ? v : 1, 0, 1);
    const ov = document.getElementById(OV_ID);
    if (ov) ov.style.opacity = String(state.opacity);
    if (glc) {
      glc.css = '';
      glc.canvas.style.opacity = String(state.opacity);
    }
    refreshUi();
    return state.opacity;
  }

  function setFps30(on) {
    state.fps30 = !!on;
    nextAt = 0;                    // the next rAF draws immediately
    refreshUi();
    return state.fps30;
  }

  // Toggling the sea off stops both loops and hides the live canvas; toggling
  // it back on reuses the current mode (a deferred animated build happens here
  // when startup was off, so a sea that is off never loads the textures).
  function setOn(on) {
    state.on = !!on;
    for (const id of [OV_ID, GL_ID]) {
      const c = document.getElementById(id);
      if (c) c.style.display = state.on ? '' : 'none';
    }
    if (!state.on) {
      stopStatic();
      stopGL();
      refreshUi();
      return state.on;
    }
    if (pendingMode && applyPendingMode()) return state.on;
    if (state.mode === 'static') {
      if (!state.img) loadColour().catch(() => {});
      startStatic();
    } else if (glc && glc.ready && !glc.lost) {
      startGL();
    } else {
      setMode(state.mode);
    }
    refreshUi();
    return state.on;
  }

  state.setMode = setMode;
  state.debugRender = debugRender;
  state.liveViewState = liveViewState;
  state.bench = bench;
  state.retry = retry;
  state.setFps30 = setFps30;
  state.setBrightness = setBrightness;
  state.setOpacity = setOpacity;
  state.setOn = setOn;

  // ---- menu wiring and persistence ----------------------------------------
  // The menu reports a patch; this applies it, writes the zz-sea-* key and
  // returns the state the menu should paint.
  function uiState() {
    return {
      on: state.on, mode: state.mode, opacity: state.opacity,
      brightness: state.brightness, fps30: state.fps30,
      canUseShader: state.canUseShader, maxTextureSize: state.maxTextureSize,
      loading: state.loading, error: state.error,
    };
  }

  function applyChange(patch) {
    if (!patch) return uiState();
    if ('on' in patch) {
      localStorage.setItem('zz-sea-on', setOn(patch.on) ? '1' : '0');
    }
    if ('mode' in patch) {
      requestMode(patch.mode);       // may wait for the deck instead of demoting
      localStorage.setItem('zz-sea-mode', patch.mode);
    }
    if ('retry' in patch) {
      retry();
    }
    if ('opacity' in patch) {
      localStorage.setItem('zz-sea-opacity', String(setOpacity(patch.opacity)));
    }
    if ('brightness' in patch) {
      const v = setBrightness(patch.brightness);
      localStorage.setItem('zz-sea-brightness', String(Math.round(v * 100)));
    }
    if ('fps30' in patch) {
      localStorage.setItem('zz-sea-fps', setFps30(patch.fps30) ? '1' : '0');
    }
    return uiState();
  }

  seaUi = mountSeaUi({ getState: uiState, onChange: applyChange });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopStatic(); stopGL(); }
    else if (state.mode === 'static') {
      if (!state.img) loadColour().catch(() => {});
      startStatic();
    } else {
      if (holdStatic) startStatic();
      startGL();
    }
  });

  // ---- startup -------------------------------------------------------------

  // Static needs the colour map immediately; the animated build reuses this
  // decode instead of fetching the same 40 MPx WebP again.
  loadColour().catch(() => {});

  // The saved mode is a preference, not a claim that the app is ready. An
  // animated saved mode waits for #deckgl-overlay (this SPA renders it after
  // document-idle); only a real build failure calls fallbackToStatic. The
  // stored mode is never rewritten just because the app was still rendering.
  if (startupMode !== 'static') {
    pendingMode = startupMode;
    watchForDeck();
    applyPendingMode();            // no-op until the deck exists / the sea is on
  } else if (state.on) {
    startStatic();
  }
})();
})();
