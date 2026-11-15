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
uniform float uReveal;          // 0..1 depth-masked intro (deep water first)

// ---- map border -------------------------------------------------------------
// Everything here is in map texels (one texel per cell), so the trim scales
// with zoom like the rest of the sea: the water bleeds BLEED_TILES past the
// rectangle, then a 9-texel band: black, line, black, 3-texel zigzag, black,
// line, black. The band is a solid opaque-black backing, the zigzag's unpainted
// cells included; only past it does the page show through.
const float BLEED_TILES = 6.0;
const float BORDER_BLACK_IN = 1.0;
const float BORDER_LINE1 = 1.0;
const float BORDER_BLACK1 = 1.0;
const float BORDER_ZIG = 3.0;
const float BORDER_BLACK2 = 1.0;
const float BORDER_LINE2 = 1.0;
const float BORDER_BLACK_OUT = 1.0;

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

  col *= uBrightness;   // sea colour only: the border below overrides it

  // ---- map border ----
  // The border lives on the texel grid: p is snapped to texel centres, so every
  // cell below is exactly one map texel wide and the edges stay crisp at any
  // zoom. The sea runs BLEED_TILES past the rectangle (the bakes clamp), then
  // the 9-texel band starts; outside it the page shows through. The band is an
  // opaque black backing with white rules on it, drawn after the sea, so it
  // takes no brightness, parallax or warp.
  // the sea content fades in by depth: quantise to the depth texel's 8-bit
  // code so the soft edge follows the contours, deep water first and the shore
  // last. The trim below is not masked.
  float dq = floor(depth01 * 255.0 + 0.5) / 255.0;
  float thr = 1.25 - 1.55 * uReveal;
  float seaA = smoothstep(thr - 0.02, thr + 0.02, dq);
  vec2 overTiles = max(vec2(0.0), max(-p, p - uSize));  // tiles out, >= 0
  vec2 outIdx = floor(overTiles);            // 0-based texel index outside
  float outT = max(outIdx.x, outIdx.y);      // Chebyshev: nearest side wins
  if (outT >= BLEED_TILES) {
    float b0 = BLEED_TILES;                  // black band starts here
    float b1 = b0 + BORDER_BLACK_IN;         // inner white line
    float b2 = b1 + BORDER_LINE1;            // black
    float b3 = b2 + BORDER_BLACK1;           // 3-texel zigzag
    float b4 = b3 + BORDER_ZIG;              // black
    float b5 = b4 + BORDER_BLACK2;           // outer white line
    float b6 = b5 + BORDER_LINE2;            // black
    float b7 = b6 + BORDER_BLACK_OUT;        // end of the band
    // every cell in the band is opaque; only past b7 is the page visible
    seaA = outT < b7 ? 1.0 : 0.0;
    col = vec3(0.0);                         // black backing
    if (outT >= b1 && outT < b2) {           // inner white line
      col = vec3(1.0);
    } else if (outT >= b3 && outT < b4) {    // 3-texel zigzag
      // along the nearest side: vertical position for left/right, horizontal
      // for top/bottom. The painted column steps 0,1,2,1 every 4 texels, so
      // the chain reads as a diagonal staircase.
      float along = floor(overTiles.x >= overTiles.y ? p.y : p.x);
      float across = outT - b3;              // 0..2 within the sub-band
      float k = mod(along, 4.0);
      float zigCol = k < 0.5 ? 0.0 : (k < 1.5 ? 1.0 : (k < 2.5 ? 2.0 : 1.0));
      if (abs(across - zigCol) < 0.5) {
        col = vec3(1.0);
      }
    } else if (outT >= b5 && outT < b6) {    // outer white line
      col = vec3(1.0);
    }
  }

  outColor = vec4(col, seaA);
}`;

export { VERT, FRAG };
