# Whale — design

2026-11-16

## Intent

A single whale roams the zig-zag map as an easter egg. It is always somewhere,
usually deep and faint, occasionally rising to break the surface. Everyone sees
the same whale in the same place at the same moment, with no server: the
position is a pure function of Unix time.

The prototype draws the whale as a shaded sphere. A later, separate piece of
work replaces it with an animated whale sprite. Nothing in this design assumes
the sphere beyond the first pass.

## What exists to build on

- The animated sea shader (`src/sea-glsl.js`, `src/sea.js`) already runs in the
  four GL modes and draws behind deck's map tiles, so tiles occlude anything the
  sea canvas paints on land.
- The full-map fields the whale needs already exist and are shipped for the
  shader: exact distance to land (`dist_full.npy` / `zigzag-sea-dist.webp`), the
  final height field (`height_full.npy`), and the current field
  (`currents_uv.npy`).
- The shader's world is texture-row space: `p` runs 0..map size with **south at
  row 0** (the row order of the shipped fields), and one unit is one map tile (a
  texel). The route generator reads the `.npy` fields directly, so its `y` is
  the same row index the shader samples; `heading` is `atan2(dy, dx)` in that
  same space. The whale lives in the same space.
- The whale is evaluated at the **unsnapped** world position (before the sea's
  texel snap), so a 5-6 texel body moves smoothly instead of stepping one tile
  at a time; the seabed texture keeps its snap.

## Goals

- One whale, deterministic and synced from the clock alone.
- It only travels deep water: never over land, never across the shallow shelf.
- Smooth, long arcs through the channels between archipelagos; it roams the
  whole map over the loop rather than touring or crossing it.
- It feels the currents: faster running with the flow, slower against it and in
  slack water.
- Depth varies: long deep stretches where only a faint silhouette shows,
  shallower spells where it reads clearly, and surface breaks every few
  minutes, each throwing the coast's wave/foam ring.
- Everything is texel-sized, so it scales with zoom like the seabed.
- Its cost is invisible: one object, no extra draw calls, no measurable frame
  time.

## Non-goals

- The animated whale model, sound, interactions, or any UI. No marker, no map
  pin, no notification.
- Marking or rewarding a sighting.
- Static mode. The 2D texture path has no shader to draw the whale; it is out of
  scope for now.
- Multiple whales or a population.

## Settled by the owner

- Easter-egg feel: hard to notice, always roaming.
- A baked multi-day loop is fine; whichever is cheaper. Not a 20 MB asset.
- Deep water only, no islands and no very shallow water.
- Smooth arcs, roaming everywhere between islands; not a lap or a crossing.
- Follow the currents; speed varies with them.
- Surface breaks use the same wave effect as the coast.
- The whale is small: ~5-6 texels long, 2.5-3 across.
- The final body is depth-bearing (an animated mesh, or pixel art with depth) so
  the waterline cuts it; the prototype is a sphere.

## Route

A route is baked once by `tools/bake_whale_route.py` from the same fields the
sea uses, then shipped as a small binary asset.

**Format.** Control points in texture-row space:

| field | type | meaning |
|---|---|---|
| x | uint16 | map column, tiles |
| y | uint16 | texture row, tiles (**row 0 = south**, the same rows as the shipped fields) |
| t | uint32 | seconds since the start of the loop |
| depth | uint8 | tiles below the waterline at the body centre: 0 = centre at the surface, max in the header |

Plus a short header: magic, version, point count, loop duration in seconds, and
a seed. A week of route at one control point every ~30 s is ~20k points, ~140
KB; one point every 60 s halves it. The exact density is chosen so Catmull-Rom
between points stays within a tile of the underlying steering path.

**Generation.** A steering particle is simulated offline with the shipped
fields:

- **Deep water:** reject positions where the seabed is shallower than a
  threshold (a fraction of the shelf profile) or inside the map margin; steer
  along the distance field's gradient when approaching the threshold.
- **Currents:** add the local current vector to the heading with inertia, so the
  route naturally follows streamlines; speed is the base speed modulated by the
  current's alignment (with the flow faster, against it slower, slack slower).
- **Smoothness:** cap the turn rate so the path is arcs, not jitter; low-
  frequency noise supplies the wander so it does not settle into a gyre.
- **Coverage:** drive the wander over the whole map across the loop (e.g., a
  slowly rotating bias), while never leaving deep water.
- **Loop:** simulate a closed loop — the steering field is periodic over the
  loop duration and the particle's end state is blended back to its start over
  the last stretch, so the seam has no visible jump or turn. The seam is
  ordinary travel, not a lap along the edge.
- **Depth:** a second, slower noise along the route yields the depth profile:
  mostly 6-14 tiles down (a faint silhouette), shallower spells of 2-5 tiles
  where it reads clearly, and surface windows of 10-20 s every few minutes.
  Depth is metres-below-surface at the body centre in tiles; at 0 the centre is
  on the waterline and the top half breaches. The window is simply depth 0 —
  the asset needs no extra field, and the breach/ring fade derives from the
  depth ramp either side of it. Windows are placed only where the seabed is
  deep (open water), so a breach never happens over a shelf or an island.
- **Determinism:** one committed seed; regenerating with the same fields and
  seed reproduces the asset byte-for-byte.

**Sampling.** At runtime, `t = (unixSeconds mod loopDuration)`; binary-search
the point timestamps; Catmull-Rom through the four surrounding points for
position, linear for depth. Heading is the curve's tangent. All of it is plain
double arithmetic in JS; the shader only receives the result.

- The loop duration is part of the asset and never changes at runtime.
- A clock that is wrong by seconds moves the whale by seconds — acceptable.
- The route is loaded with the GL assets on the first animated mode; static
  never fetches it.

## Rendering

Drawn inside the sea fragment shader, immediately after the sea's colour and
before the trim, so the existing alpha and intro reveal mask it and the map
tiles occlude it naturally.

**Position and size.** New uniforms: whale position (vec2, texture-row tiles),
depth (tiles below the waterline at the body centre, `0..maxDepth` from the
asset header), heading (radians in texture-row space), body half-length and
half-width in tiles, and a surface factor derived from depth for the ring fade.
The whale is small: roughly **5-6 texels along its length and 2.5-3 across**, so
it is a handful of screen pixels at map zoom and a proper animal when zoomed in.
The prototype is a sphere of radius ~2.2-2.5 tiles; the later model replaces the
sphere but keeps the same uniforms, position and waterline logic.

**Model path (later).** The final animal is a depth-bearing render (animated
mesh, or pixel art with a depth channel), not a flat sprite, because the surface
break has to cut the body at the waterline. The intended pipeline is to render
the whale into a small offscreen colour+depth target centred on its screen
position and composite that in the sea shader, so the waterline, fog, reveal and
tile occlusion all apply per pixel. The prototype sphere can be drawn
procedurally in the shader; the position/depth/heading/scale contract must be
the one the offscreen pass will use, so swapping the body later changes only the
draw, not the sea integration.

**Underwater.** The body is tinted toward the deep-sea colour and dimmed with
depth (at the deepest it is a barely-there silhouette); a cheap normal-shaded
highlight keeps it readable when shallow.

**Surface break.** When the depth profile is in a surface window, the top of the
body rises above the waterline. The waterline is drawn where the body crosses
depth 0, and a foam/wave ring is added around the body using the **same wave
function the shore uses**, with the whale's radius as the "shore" and a small
shore width (roughly 1.5-2.5 tiles), animated by the same clock. The ring is
only drawn in open water (the route guarantees it) and fades in/out over the
surface window so it does not pop.

**Masking.** The whale's alpha is multiplied by the sea reveal so it appears
with the water during the intro. In static mode there is no whale.

**Debug seams.** `__zzSea.whale({ t })` pins the whale clock for a frame and
`__zzSea.whale(false)` turns the whale off; `debugRender` accepts the same
`whale` / `whaleT` options. The existing sea parity and reference frames pin it
off, so adding the whale cannot silently invalidate the sea's only visual
regression check. An unknown or failed route simply leaves the whale off.

## Sync

- Time source: `Date.now() / 1000`, modulo the loop duration.
- No server, no storage, no per-user seed.
- Determinism check: given a fixed `t`, the sampler returns bit-identical values
  in Node and in the browser; the shader's float32 conversion is below a
  thousandth of a tile and cannot visibly matter.

## Performance

- One distance-to-whale per sea pixel, branched to nothing when far; no extra
  passes, textures or draw calls.
- The route asset is ~50-150 KB, parsed once into typed arrays.
- The per-frame JS is one binary search and a Catmull-Rom evaluation.

## Testing

- **Sampler (node):** timestamps monotonic; position continuous at every control
  point; loop seam continuity in position and velocity; Catmull-Rom stays within
  a tile of the control polygon; depth in range; the same `t` gives the same
  result across runs.
- **Route (generator):** every point in deep water and inside the margin; turn
  rate under the cap; speeds vary with the current (faster with than against on
  average); surface windows only over deep water and 10-20 s long; loop duration
  and point count recorded; regenerating with the same seed matches.
- **Visual:** a debug hook `__zzSea.whale({t})` pins the clock for a frame;
  render a deep, a shallow and a surface moment at a pinned camera and compare
  with the reference images.
- **Live:** two independent surfaces (e.g., two browsers) at the same wall clock
  report the same sampled position; with the whale time pinned, the shader
  output matches the reference within float tolerance.

## Rollout

- Ship the route asset beside the other textures. It carries its **own version
  query** (a `routeVersion` constant, not the userscript version), so changing
  the whale does not force every client to re-download the 40 MPx sea textures.
- The whale is invisible until the route is loaded; a failed route load disables
  the whale and leaves the sea untouched (no error state in the menu).
- The route only downloads with the animated modes.

## Open items for the owner

None blocking. The builder's calls for review: the sphere's size (radius ~2.2-2.5
tiles for a 5-6 texel body), the surface cadence (10-20 s every 3-5 min), and
the deep-water threshold (the shelf's outer edge rather than a fixed tile
distance).
