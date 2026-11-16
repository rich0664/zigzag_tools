# Whale — design

2026-11-16

## Intent

A single whale roams the zig-zag map as an easter egg. It is always somewhere,
usually deep and faint, occasionally rising to break the surface. Everyone sees
the same whale in the same place at the same moment, with no server: the
position is a pure function of wall-clock time.

The prototype draws the whale as a shaded ellipsoid. A later, separate piece of
work replaces it with an animated, depth-bearing body. Nothing here assumes the
ellipsoid beyond the first pass.

## What exists to build on

- The animated sea shader (`src/sea-glsl.js`, `src/sea.js`) already runs in the
  four GL modes and draws behind deck's map tiles, so tiles occlude anything the
  sea canvas paints on land.
- The full-map fields the whale needs already exist and are shipped:
  exact distance to land (`dist_full.npy` / `zigzag-sea-dist.webp`), the final
  height field (`height_full.npy`), and the current field (`currents_uv.npy`,
  plus the baked flow/speed/aux textures).
- **Row order.** The bakes store the map with **south at row 0**, north at the
  last row (the 2D path draws with a negative y scale, and the live camera maps
  deck's page row with `MAP_H - 1 - y`). The route generator reads the `.npy`
  rows directly and the shader samples the same rows, so the whale's `y` is a
  texture row. A vertical-flip mistake is silent and self-consistent; the tests
  in this design exist to make it impossible.
- The sea's world is texture-row tiles; `p` is the snapped texel-centre sample,
  `world` the continuous position. The whale **moves on `world`** (continuous,
  smooth) and is **rendered on a texel grid anchored to itself**: its local body
  coordinates are quantised to ~1-tile cells, so it is a 5-6 × 2-3 block 3D
  creature whose blocks travel with it. It is **not** snapped to the map grid —
  that would step one tile at a time. The ring uses the same local cell grid.

## Goals

- One whale, deterministic and synced from the clock alone.
- Deep water only: never over land, never across the shallow shelf.
- Smooth arcs through the channels between archipelagos; roams the whole map
  over the loop, not a tour or a crossing.
- Feels the currents: faster running with the flow, slower against and in slack
  water.
- Depth varies: long deep stretches (faint silhouette), shallower spells
  (readable), surface breaks every few minutes that throw the coast's wave ring.
- Texel-sized: scales with zoom like the seabed.
- Invisible cost: one object, no extra passes or draw calls.

## Non-goals

- The animated model, sound, interactions, any UI or marker.
- Static mode (the 2D path has no shader); out of scope for now.
- Multiple whales, population, sightings, rewards.

## Settled by the owner

- Easter-egg feel: hard to notice, always roaming.
- A baked multi-day loop is fine; not a 20 MB asset.
- Deep water only, no islands, no very shallow water.
- Smooth arcs, roaming everywhere between islands; not a lap or a crossing.
- Follow the currents; speed varies with them.
- Surface breaks use the same wave effect as the coast.
- Body ~5-6 texels long, 2.5-3 across; final body is depth-bearing
  (a 3D mesh, not a sprite); the prototype is an ellipsoid stand-in with the same contract.

## Time and sync

- The clock is derived from a network reading when possible, with the local
  clock as the fallback. At startup a same-origin `HEAD` to a **cheap static
  asset** (`/logos/logoicon.webp`, not the SPA route — the app's TTFB is
  2-9 s, which would always blow the timeout) reads the `Date` header; with
  `performance.now()` timestamps around the request, the server's "now" is the
  header value plus half the round trip. The `Date` header has 1 s resolution,
  so the total error is under a second — under two tiles of travel at full
  speed. No third-party API, no CORS, one tiny request.
- Cross-client acceptance: for the same `t`, the sampler is deterministic
  (pinned tests); across clients the phase is bounded by the `Date`
  resolution (~1 s ≈ 1.6 tiles at cruising speed). That is accepted for an
  easter egg; a sub-second source would be a different decision.
- The offset is anchored once: `wallOffsetSec = serverNow - performance.now()/1000`,
  and the live whale time is `wallOffsetSec + performance.now()/1000`. Motion is
  monotonic and rAF-smooth; the offset keeps every client on the same wall
  clock. Re-anchor on `visibilitychange` when returning to a background tab.
- Policy: the fetch runs in parallel with the route and textures; the whale is
  not shown before the sea is up, so it normally lands first. If it fails or
  times out (~1.5 s), use the local clock. A late correction under ~1 s is
  ignored; a larger one is eased in over a second or two rather than
  teleporting the whale. `stats()` reports the source (`network` / `local`).
- **Unix seconds never enter GLSL.** `float32` at 1.79e9 has a 128-second ULP;
  passing wall time to a shader would freeze the animation. JS does
  `t = wallClock mod loopDuration` and the route sampling in doubles, and passes
  only the evaluated pose to the shader. The shader's own `uTime` stays the
  existing seconds-since-load (or `debugRender`'s `t`), used by the wave ring.
- No server, no storage, no per-user seed. A wrong local clock behind a failed
  fetch moves the whale by the clock error — acceptable.

## Route asset

A route is baked once by `tools/bake_whale_route.py` from the shipped fields,
then shipped as one small binary.

### Format (little-endian)

Header, 16 bytes:

| field | type | meaning |
|---|---|---|
| magic | uint32 | `0x5748414C` ("WHAL") |
| version | uint16 | 1 |
| dtSec | uint16 | seconds per record, e.g. 20 |
| count | uint32 | records; loop duration = count × dtSec |
| maxDepth | uint16 | deepest point, in 1/16 tile units |
| seed | uint16 | generation seed |

Records, 8 bytes, **fixed Δt** (no timestamp):

| field | type | meaning |
|---|---|---|
| x | uint16 | texture column × 8 (1/8 tile) |
| y | uint16 | texture row × 8 (row 0 = south) |
| depth | uint8 | tiles below the waterline at the body centre × 16 |
| pad | uint8 | reserved (0); keeps the stride aligned |

Fixed Δt means sampling is `O(1)` — `i = floor(t / dtSec)`, `u = fract` — with
no binary search, uniform Catmull-Rom is `C1` by construction, and wrapping the
four indices modulo `count` makes the loop seam `C1` in position and tangent.
A 7-day loop at 20 s is 30,240 records, 242 KB raw (well under 150 KB over the
wire after compression), and 1/8-tile x/y does not staircase a slowly moving
body.

### Sampling

`t = wallClock mod (count × dtSec)`; `i = floor(t/dt)`, `u = (t mod dt)/dt`;
Catmull-Rom through records `(i-1, i, i+1, i+2) mod count` for position; linear
for depth. Heading is the tangent, `atan2(dy, dx)` in texture-row space. Depth
is in tiles; at 0 the body centre is on the waterline and the top half breaches.

- The loop duration is in the asset and never changes at runtime.
- The route is fetched with the GL assets on the first animated mode; static
  never fetches it.

### Generation

A steering particle is integrated offline at 1 s resolution with these fields:

- **Deep water.** Safe if distance-to-land ≥ 28 tiles (outside shelf 26) and
  seabed depth ≥ 0.35; map margin 60 tiles. A soft potential
  `U = max(0, 1 - D/45)^3 + max(0, 1 - H/0.35)^3 + margin` pushes away, and
  inside 32 tiles of land the desired direction blends toward the **along-coast
  tangent** (sign chosen by the travel direction) so the whale follows channels
  instead of bouncing off coasts.
- **Currents.** The route uses the same deflected current field the shader's LOD
  samples. Heading blends toward the current with inertia; base speed ~1.5-2
  tiles/s, modulated by alignment:
  `v = v0 * clamp(1 + 0.55*spd*cos(Δθ) - 0.20*(1-spd), 0.5, 1.7)`.
- **Coverage.** Pure current-following gets trapped in a gyre. A sequence of
  80-120 deep-water waypoints (blue-noise on `dist > 60 & depth > 0.35`),
  ordered through navigable channels, pulls the particle with a 15-25 % weight
  and advances when it passes within ~250 tiles. The rest is the current, a
  low-frequency wander, and the coastal tangent.
- **Smoothness.** Turn rate capped at 0.015 rad/s (~0.85°/s) so the path is arcs
  and Catmull-Rom stays within a fraction of a tile of the integrated track.
- **Loop closure.** No naive position blend: the waypoint cycle returns to the
  starting basin ~15 min before the end, a homing pull aims at
  `start + v_start*(t - T_loop)`, then a Hermite bridge over ~120 s connects
  position and velocity to the start. Every 1 s sample of the bridge must pass
  the deep-water check.
- **Depth.** Periodic 1D FBM mapped to 6-14 tiles, with medium spells of 2-5
  tiles, and raised-cosine surface windows of 10-20 s with 15 s descent/ascent
  ramps, only where distance > 45 and seabed depth > 0.35. Depth 0 is the
  window; the asset needs no extra field, and the ring fade derives from the
  ramp.
- **Determinism.** One committed seed; regenerating with the same fields and
  seed reproduces the asset byte-for-byte.

## Rendering

Drawn inside the sea fragment shader, after the sea's shaded colour and **before
`col *= uBrightness` and the trim**, so brightness, the intro reveal and the
trim all apply consistently and tiles occlude it on land.

### Local frame and body

- Origin at the body centre; `+X` forward along `heading`, `+Y` across,
  `+Z` up toward the sky in tiles with the waterline at world Z = 0. A body point
  at local height `z` sits at `Zworld = z - whaleDepth`.
- Prototype geometry: a triaxial ellipsoid, half-length 2.75, half-width 1.35,
  vertical radius 1.35 tiles (5.5 × 2.7 × 2.7). Two `vec4` uniforms:
  `uWhalePose = (x, y, heading, depth)` and
  `uWhaleShape = (halfLen, halfWid, surf, enabled)`.
- Height over the body: `zTop = Rz*sqrt(1 - r²)`, so the waterline is the exact
  contour where `zTop = whaleDepth`. A single line of GLSL more than a sphere,
  and it validates heading and the oblong ring immediately.
- **Depth-scaled water coupling:** sample the body's position with
  `wq = world + clamp(warp * depth01, vec2(-3.0), vec2(3.0))` (`depth01 = clamp(depth / 14.0, 0.0, 1.0)`),
  so the breach at depth 0 is anchored to the water surface with no warp, while
  a deep body refracts with the surface waves. Do **not** apply `parShift` to
  the whale: `parShift` is a view-centre radial scale (`(uTarget - world) * k`)
  for the continuous seabed that shifts an off-centre object by tens of tiles,
  clips it against the 12-tile bounding-box gate, and distorts its size with
  zoom.
- **Whale-anchored cell grid (smooth motion, blocky 3D shading):** the body's
  local coordinates are quantised to a cell grid anchored to the whale
  (`lq = (floor(local / cell) + 0.5) * cell`, cell ≈ 1 tile); the ellipse,
  `zTop`, a per-cell 3D normal and the hard mask are evaluated from `lq`. The
  pose itself stays continuous, so the whole block pattern translates smoothly
  instead of stepping with the map grid. Each ~1-tile cell has its own normal
  and Lambert + small-specular shading, so the body reads as a chunky but shaded
  3D creature, not two flat colours per cell; the waterline and fog weights are
  per cell. The ring uses `lq` too.

### Fog and the ring

- Submerged body (`Zworld < 0`) fades toward the already-shaded `col`:
  `mix(body, col, 1 - exp(-submerged*0.22))`, and the surface grain is applied
  over it, so it reads as under the water, not pasted on glass.
- Breached body (`Zworld > 0`) and the waterline sit above the grain; brightness
  and the reveal mask still apply.
- The ring reuses the shore wave **outbound** logic only — never the inbound
  crest, shelf glow or full wobble. `dWhale` is the signed distance from the
  body contour in tiles; `wob` is scaled to 0.25 so it cannot tear a 2-tile
  ring; the wet-line foam is `1 - smoothstep(0, 0.75, |Zworld|)` on the
  waterline. Everything is multiplied by `surf` (from the depth ramp), so it
  fades in/out and never appears while diving in open water.
- Bounding-box early exit: the whole block is skipped unless
  `max(|p - whalePos|) < 12` tiles, which is a coherent branch on screen.

### Debug seams

`__zzSea.whale({ t })` pins the whale clock, `__zzSea.whale(false)` disables it;
`debugRender` accepts `whale` / `whaleT` and defaults the whale **off** when
`whale` is omitted, so existing sea parity frames stay bit-identical. An absent
or failed route leaves the whale off.

### Model path (later)

The final body is a **3D mesh**, not a sprite: render the whale into a small
colour+depth target centred on its screen position and composite it in the sea
shader, with local height packed into the alpha channel (no second depth texture
needed). The target is rendered at a **low resolution matched to the body's
texel size** and sampled nearest, so the pixels are texel blocks while the mesh
translates smoothly. The sea-side logic — local frame, waterline cut, submerged
fog, ring distance, reveal, trim — is identical whether `zTop` comes from the
ellipsoid stand-in today or from the mesh target tomorrow, so the swap changes
only the draw.

No sprite sheet or texture atlas: the low-res look comes from the render
resolution, not from flattening the body.

**Prototype mirror.** The whale is developed in `src/sea-glsl.js` only. The
standalone prototype page (`prototype/sea-shader/index.html`) copies that block
verbatim — same uniforms, constants and shading — once it is final, adding only
sliders and a time control so the look can be watched in motion. It never
develops its own whale; tuned values are ported back into the shipped
constants.

## Performance and robustness

- Two `vec4` uniforms per frame; no per-frame GL state beyond them.
- The route lives in a module-level typed array, independent of GL: it is not
  part of `buildGL`'s `Promise.all` and is cached across mode switches and
  context restores. **A failed route load must never fall back the sea to
  static** — it only leaves the whale disabled.
- Per frame: one `O(1)` index, one Catmull-Rom evaluation in doubles.
- The whale block is a bbox-gated branch over ~12×12 tiles of a 7749×5254 map.

## Code shape

- `src/whale-core.js` (pure ESM): `parseWhaleRoute(buf)` with magic/version/
  count validation, `sampleWhale(route, unixSec)` returning
  `{x, y, depth, heading}`, and `whaleUniforms(pose, shape)`. No DOM, no GL;
  concatenated by `build.mjs` after `sea-core.js` and tested directly by
  `node --test`.
- `src/sea.js`: wall-clock anchor, route fetch (own version), uniform setting,
  debug hooks.
- `src/sea-glsl.js`: the ellipsoid, waterline, ring and fog block.
- `tools/bake_whale_route.py`: the baker and its `--check`.

## Testing

- **Sampler (node):** header validation and corrupt-buffer rejection; `O(1)`
  indexing at boundaries; Catmull-Rom continuity at every record; loop-seam
  position *and* tangent continuity; depth range; the same `t` gives identical
  results across runs and between Node and the browser.
- **Route (generator, `--check`):** every 1 s sample (not just control points)
  passes distance ≥ 28, not land, seabed depth ≥ 0.35; turn rate under cap;
  surface windows 10-20 s and only in deep/open water; loop bridge passes;
  speed faster with than against the current on average; regenerating with the
  same seed matches byte-for-byte.
- **Row-order canary:** three asymmetric landmarks (deep south vs land north and
  vice versa); assert every route sample is deep water on the true rows and that
  the **same route fails on the vertically flipped field** (hits land many
  times). A browser canary renders a pinned whale over a known deep point and
  asserts the pixel difference is centred there.
- **Visual:** pinned deep, shallow and breaching frames at a fixed camera,
  compared with reference crops; `whale: false` must be pixel-identical to the
  pre-whale sea.
- **Live:** two independent contexts at the same timestamp sample identical
  positions; the whale appears with the sea's intro reveal and not in static.

## Rollout

- Ship the route beside the textures with its **own** `whaleVersion` query
  (`assetUrl(name, base, ver)` gains an optional version), so changing the whale
  never re-downloads the 40 MPx sea textures.
- The route downloads only with the animated modes; a failure disables the whale
  silently and leaves the sea untouched.

## Open items for the owner

None blocking. The builder's calls: the ellipsoid proportions, the surface
cadence (10-20 s every 3-5 min), the base speed (owner liked ~2 tiles/s; AGY
suggested a statelier 0.35) and the deep-water threshold (28 tiles, just outside
the 26-tile shelf).
