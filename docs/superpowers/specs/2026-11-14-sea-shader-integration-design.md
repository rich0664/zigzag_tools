# Sea shader integration — design

2026-11-14

## Intent

Replace the pre-rendered bathymetry overlay in the zig~zag userscript with the
animated WebGL shader proven in `prototype/sea-shader/`, and give the player a
small quality menu on the map.

The prototype settled the look; this work is about shipping it: the baseline
look exactly as tuned, a ladder of cheaper modes, and a control that belongs to
the game's own HUD instead of a floating debug slider.

## What exists today

- `src/sea.js` (167 lines) inserts a 2D canvas behind `#deckgl-overlay`, blits
  `dist/zigzag-sea.webp` (7749 × 5254, one texel per map tile) and re-blits
  when deck's view state changes. It reads the camera from deck's React fiber,
  which is the same contract the shader needs.
- A fixed bottom-right opacity slider, stored in `zz-sea-opacity`.
- `window.__zzSea` exposes `on`, `img`, `opacity`, `error`, `draws`.
- `prototype/sea-shader/` is a standalone WebGL2 page over a 1600 × 1000 crop,
  with the full effect, all its uniforms, three tuning curves and a bench.
  Its README documents every effect and the measurements behind it.

## Settled by the owner

- Animated modes use the **analytic LOD** flow: the 16-tile gyre texture plus
  the coastal bend rebuilt in the shader from the along-coast vector. The
  2-tile and 1-tile baked LOD textures are a later experiment, not shipped.
- **Static** is literally the main map texture, blitted as today — not the
  shader with time frozen.
- Five entries in the menu: `baseline`, `medium`, `low`, `extra low`, `static`.
- The old bottom-right slider goes away. In its place, a button **under the
  notifications button** at the top right of the map, opening a small menu with
  the quality modes, opacity, brightness and a 30 fps toggle.
- One asset set for all modes for now. Splitting it by mode is a later
  decision. Phones are not a target for this version; tiling the textures is
  the later path to them.
- Later, explicitly out of scope: the shader's own look controls, an asset
  updater for map changes, other maps, flow arrows, debug views, and splitting
  the map textures into tiles (the path to phones and to lower VRAM).

## Goals

- The baseline shader look runs on the live map, camera-correct at every zoom,
  with the tuned defaults in `prototype/sea-shader` (variant P).
- Four animated tiers and one texture-only tier, selectable and remembered.
- Turning the sea off or hiding the tab stops all GPU work.
- The menu matches the game HUD and never covers the floating video player or
  steals map input.
- Without WebGL2 it draws the same static texture as today, so the change is
  never worse than what is shipping.

## Non-goals

- ARIA-perfect accessibility of the menu beyond ordinary keyboard use.
- Per-device benchmark-driven auto-tuning.
- Changing the map texture, the bake pipeline's look, or the editor.

## Anti-goals

- A mode that looks *different* rather than *cheaper*. Every tier is the
  baseline minus work.
- A UI that fights React for the header's DOM every frame.
- Loading 10–20 MB of shader assets for a player who stays on Static.
- Silent failure: if the shader cannot start, fall back to the texture and say
  so in the menu.

## Modes

All animated modes share one program and one set of uniforms; a mode only
changes uniform values and the render scale. Baseline values are the tuned
prototype defaults.

| | baseline | medium | low | extra low |
|---|---|---|---|---|
| flow | LOD (16-tile gyre + analytic bend) | LOD | 16-tile gyre only | 16-tile gyre only |
| open-water noise | two layers averaged | two layers | one layer | one layer |
| parallax / POM | on, `par 0.814`, `pom 5` | off (`par 0`, `pom 1`) | off | off |
| warp | `3.3`, fully depth-biased | `3.3` | `0` | `0` |
| render scale | 1.0 | 1.0 | 1.0 | 0.5 |

`static` renders nothing through WebGL: the shipped colour WebP is blitted by
the existing 2D path (today's `src/sea.js`), so it costs one decode and one
draw per camera change.

Baseline uniform values to ship as constants (from the tuned prototype URL):

```
shore 8.8   shelf 29    flow 2.31   wave 1.9    grain 0.06  foam 0.25
deep 1.0    swell 0.008 win 0.28    wout 0.82   cycle 7     dslow 1.0
par 0.814   pom 5       warp 3.3    wbias 1.0
adir 254    aspeed 1.42 ascale 0.156
bdir 45     bspeed 1.16 bscale 0.12
curves: pc 0,0.057,0.339,0.578,1
        sc 0,0.057,0.274,0.492,0.926
        wc 0,0.13,0.435,0.594,1
clamp off, calm 0, texel-res on
```

Texel-res stays on: one effect value per texture pixel, snapped to the tile
grid, as in the prototype. Extra low renders at half the canvas backing store
and upscales with linear filtering; texel snapping is unaffected.

### When the shader cannot run

Two hard limits, checked before any texture is fetched:

- WebGL2 absent;
- `MAX_TEXTURE_SIZE < 8192` — the colour map is 7749 px wide and cannot be
  uploaded at all below that.

Either one disables the animated entries with a one-line note in the menu, and
the mode falls back to `static`. Nothing else changes.

A device that passes those two can still run out of memory holding ~240 MB of
textures; that shows up as a context loss, which also falls back to `static`
and is reported in the menu. Making animated water work on such a device would
need the map split into tiles, which is later work.

### Frame limit

A `30 fps` toggle in the menu caps the animated modes at 30 frames per second
(time-gated in the rAF loop, so it behaves the same on 60 and 120 Hz displays);
it is off by default and remembered. `static` is unaffected — it does not
animate at all.

### Default mode

With nothing saved: `static` when the probe fails, otherwise `static` on a
coarse-pointer or narrow viewport and `baseline` everywhere else. A saved mode
always wins. No `auto` entry is shown in the menu.

Phones are **not a target** for the first version: a coarse-pointer device
defaults to `static`, the animated modes stay selectable, and nothing is tuned
or accepted against phone performance. Making animated water work there needs
the map split into tiles, which is explicitly later.

## Assets

New full-map bakes under `dist/`, loaded from the same GitHub raw URL scheme as
the current texture and versioned in the same way.

| file | size (px) | channels | contents |
|---|---|---|---|
| `zigzag-sea.webp` | 7749 × 5254 | RGB | colour map (exists, unchanged) |
| `zigzag-sea-dist.webp` | 7749 × 5254 | R | **√(distance / 600), clamped, zeroed on land/holes**, lossless |
| `zigzag-sea-aux.webp` | 3875 × 2627 | RGB | R depth (1 − height, zeroed on land/holes), G/B along-coast vector, A = 255, lossless |
| `zigzag-sea-flow.webp` | 484 × 328 | RG | 16-tile gyre u/v (the prototype's `flow.png`) |
| `zigzag-sea-speed.webp` | 1937 × 1313 | R | baked speed field (4 tiles/texel), lossless |

Rationale for splitting the prototype's single `aux`:

- Distance must stay at one texel per tile — the 2–5 tile shore band is the
  whole point, and 8-bit quantisation at 2 tiles/texel would band the coast.
- Depth and the along vector vary over 10–50 tiles. The prototype measured a
  2-tile bake as 0.09/255 from the 1-tile version for the bend, and depth only
  feeds a five-step ray. Half resolution is invisible and costs a quarter.
- A single full-map RGBA aux would be 163 MB of VRAM; the split is ~71 MB and
  the colour map (already decoded today) dominates at 163 MB.

Encoding and precision:

- Distance is stored as **√(d / 600)**, as the prototype does, not linearly.
  Near the coast that is ~0.01 tiles per code step, 0.6 at `shore 8.8` and
  ~1.0 at `shelf 29` — the same quantisation the tuned look was built on. A
  linear /600 encoding (2.35 tiles per step) would staircase the shore and
  could not match variant P.
- The along vector's 8-bit range is set by one **global** scale constant, not
  by the maximum in whatever region was baked (the prototype's `k_enc` was
  per-crop). The full-map maximum is measured once, frozen in the shared
  constants, and used by both the baker and the shader. Magnitude matters:
  the LOD blend is deliberately unnormalised.
- Depth is `1 − height` from the preset's **final** height field, after its
  filters — that is what the prototype's `bake_depth.py` reads (`ctx["h"]`),
  not the bare height node. There is no full-map height plane on disk, so
  `tools/sea_full.py` gains a `--dump-height` flag that writes
  `out/sea/height_full.npy` during one full render (~11 min, ~5 GB), once.
  Normalisation is the percentile stretch `(h − p1) / (p99 − p1)`, computed
  over the whole map (`p1 = 0.1606, p99 = 1.0`) and frozen in the constants.
  Note that the prototype's `depth.png` came from `bake_depth.py` running the
  editor's crop preview (whose distance, currents and noise are crop-anchored),
  not a slice of the full-map height render (`MAE ≈ 31` codes over the crop,
  unchanged by crop `lo/hi` re-normalisation); keeping `height_full.npy` with
  full-map `p1/p99` is authoritative because it is the exact height field that
  produced the shipped colour texture.
- Both distance and depth are zeroed on land using the map raster's alpha
  (`alpha > 128`), interior holes (lakes) filled from the border, exactly as
  the prototype's baker (`prototype/sea-shader/bake.py:120-121`) does.
- Speed and depth use fixed 0–255 ranges; no other scale is baked in.

Baking:

- The baker is promoted to `tools/bake_shader_assets.py` and runs over the
  whole map. It takes `out/sea/dist_full.npy` (exact EDT),
  `out/sea/currents_uv.npy`, `out/sea/full_interesting_v1.png`,
  `out/sea/height_full.npy` and `out/sea/map.png`, and writes the five
  `dist/` files above. The prototype's `bake.py` stays as the crop reference
  until parity is verified, then goes.
- Every field is computed from full-map arrays, never per crop. The 16-tile
  flow is a block-average of `currents_uv.npy`; the detail blend is computed
  at 2 tiles/texel only because the speed field is derived from it, and the
  1-tile detail texture is never produced or shipped.
- Constants shared by the baker and the shader (`DIST_MAX`, the along scale,
  the height node's numbers, texture resolutions) live in one
  `src/sea-consts.json`, read by the Python baker and inlined by `build.mjs`.
  No runtime metadata fetch: one fewer asset, one fewer failure mode.
- **No land texture.** The game's own tiles cover land, so the prototype's
  `land.png` compositing is dropped; the sea canvas is opaque and the tiles
  draw over it, as the 2D overlay does today.

Estimated download: colour 2.2 MB + distance 3–6 MB + aux ~6 MB + flow 0.27 MB
+ speed ~1 MB. **The real numbers are measured during implementation**; if the
total exceeds ~20 MB, stop and revisit before shipping.

Loading is lazy: `static` loads only the colour map. The first switch to an
animated mode fetches the four shader textures, showing a small "loading sea…"
state on the button; the browser caches them for later. A failed load leaves
the sea in texture mode and offers a retry in the menu.

Hosting is unchanged (GitHub raw, `@updateURL`/`@downloadURL` already in the
header). `raw.githubusercontent.com` must send CORS headers for WebGL uploads;
this is verified first, and if it does not, the assets move to a host that does.

## Renderer

One WebGL2 canvas, inserted exactly where the 2D canvas goes today
(`deck.parentNode.insertBefore(canvas, deck)`), so the map still draws on top
of it. The overlay fills the deck canvas's CSS box; the backing store is
`css × dpr × renderScale`, with the long edge **capped at 2560 px**. On a
dpr-2 1920 × 1080 display that renders 2560 × 1440, not 3840 × 2160; the sea
carries no fine detail beyond one value per map tile, so the cap costs almost
nothing visually and keeps the frame budget in one place. Extra low applies
its 0.5 after the cap.

- Camera: reuse the existing React-fiber `viewState` read and the
  orthographic maths from `src/sea.js`. The shader receives view size, target,
  zoom (device px), map size and crop origin `(0, 0)`.
- Composition: full-screen triangle, colour texture sampled with nearest
  filtering on texel-res, shader in the prototype's `index.html` reused nearly
  verbatim, with the crop bounds replaced by the full map and the two aux
  samplers rewired to the shipped layout (`uDist.r` → distance `t * t * 600`,
  `uAux.r` → depth, `(uAux.gb * 2 − 1) * uAlongScale` → along, with `uAux`
  sampled as a 3875 × 2627 2-tile grid over the 7749 × 5254 map). No other
  GLSL logic changes.
- Animation: rAF loop only while mode is animated, `on` is true, the document
  is visible, and the camera/texture state is ready; the 30 fps toggle
  time-gates it. `static` draws on camera changes only.
- Upload: each texture is uploaded once. `aux`/`dist`/`flow`/`speed` are
  decoded through `createImageBitmap` so the main thread is not blocked by a
  40 MPx decode where the browser supports it.
- Context loss: on `webglcontextlost`, prevent default, drop all GL state,
  and rebuild on restore; on failure fall back to texture mode.
- Nothing in this path may throw into the host page; every entry point is
  wrapped and reports through `window.__zzSea.error`.

## Control and menu

Anchor: the notifications button, found in the live DOM by its accessible name
(`button[aria-label^="Notifications"]`), inside `header` → `div.pointer-events-auto.ml-auto`.

Observed on 2026-11-14 at 1280 × 720: button 30 × 30 at (908, 14); header
`absolute inset-x-0 top-[8px] z-20`, `pointer-events: none`, `contain: paint`;
a floating YouTube player occupies the top right (953, 6, 324 × 223).

Because of `contain: paint`, the button and menu cannot live inside the header.
Instead:

- A fixed-position container is appended to `document.body`, positioned from
  the notifications button's live rect: same right edge, `top = button.bottom + 6`.
- Its position is refreshed by a `ResizeObserver` on the header, a window
  `resize`, and one rAF after any scroll or camera change has settled — never
  per frame.
- If the button is not found (logged out, different shell), the container is
  hidden and no UI is injected.

Button and menu:

- A 30 × 30 button, same right alignment as notifications, transparent
  background, HUD font/colour copied from the notifications button's computed
  style at mount (the app's Tailwind classes are purged, so do not rely on
  using them). Label appears on hover like the notifications label does.
- Click toggles the menu; outside click and `Escape` close it; focus stays in
  the document's normal order.
- Menu contents, in order:
  1. `sea on / off`
  2. quality: `baseline · medium · low · extra low · static` (disabled entries
     explained when the shader cannot run; the current one marked)
  3. opacity (0–100)
  4. brightness (50–150%, default 100)
  5. `30 fps limit` toggle (off by default)
- The menu is styled as a dark translucent panel with a thin border, matching
  the footer and settings surfaces. It opens downward, left-aligned to the
  button's right edge, and flips above if it would cross the viewport bottom.
- No layout shift: the button is fixed, and the map's own HUD is untouched.

Persistence (`localStorage`): `zz-sea-mode`, `zz-sea-on`, `zz-sea-opacity`
(existing key, kept), `zz-sea-brightness`, `zz-sea-fps`. The old
`window.__zzSea.on` stays and mirrors `zz-sea-on`.

Brightness multiplies the final colour in the shader; in `static` it is a CSS
`filter: brightness()` on the canvas, so both modes dim the same way to the eye.

## Code shape

`src/sea.js` keeps its single-IIFE shipping form, but the pure parts move to
`src/sea-core.js` so they can be tested without a DOM:

- the mode table (mode → uniforms, render scale, needs-shader)
- default-mode choice from `navigator`/`matchMedia`
- persistence parsing and clamping
- screen ↔ map tile maths used for the uniforms
- the asset URL builder, including the version suffix

`build.mjs` concatenates `sea-core.js` then `sea.js` behind the sea header,
stripping `export`s the same way the filler bundle does. `node --test` imports
`sea-core.js` directly.

## Verification

Unit (`node --test`):

- every mode maps to the documented uniforms; only `static` needs no shader
- extra low is the only tier with render scale 0.5
- missing/corrupt/out-of-range saved settings fall back to the documented
  defaults
- camera maths round-trips: a map tile at the view centre maps to the canvas
  centre at the current zoom, including deck's padding offset
- the 30 fps gate admits a frame every 33.3 ms on both a 60 Hz and a 120 Hz
  clock, and never on consecutive ticks
- asset URLs carry the script version

Live, in the logged-in profile with `playwright-cli`:

- the button appears under notifications at 1280 × 720 and 390 × 844, does not
  overlap the video player, and the menu never gets clipped
- `static` is pixel-identical to the pre-change overlay at the pinned camera
  (crop-local `tx 800, ty 500`, full-map `tx 1400, ty 1200`, `zoom 1` px/tile
  over crop origin `(600, 700)`)
- Shader parity at variant P (`fsrc=2` LOD, `t=0`, `1600 × 1000`, prototype
  crop-local `(800, 500, zoom 1)` vs full-map `(1400, 1200, zoom 1)`)
  is verified in three layered gates:
  1. **colour-only** (all shader effects zeroed) vs the colour texture:
     mean |Δ| ≤ 1.0/255 (measured `0.755`, the lossy-WebP floor — proves
     camera projection, texel snapping and sampling).
  2. **medium** (parallax/POM off) vs the prototype at `fsrc=2`:
     mean |Δ| ≤ 2.5/255 (measured `2.096`, `p99 15`).
  3. **baseline** (parallax/POM on) vs the prototype at `fsrc=2`:
     mean |Δ| ≤ 4.0/255 (measured `3.064`, `p99 21`) and visual inspection of
     the rendered image confirms no structural defects (orientation, offset,
     black regions, or POM slice banding).
  4. **Difference budget** (asset- and coordinate-side, not shader port):
     lossy WebP (`~0.75/255`) + continuous full-map vs crop-anchored simplex
     noise (`~0.66/255`) + global `alongScale`/2-tile aux LOD (`~0.35/255`) +
     full-map `height_full.npy` vs crop-preview `depth.png` in POM (`~1.3/255`).
- each lower tier visibly removes only its documented work, checked as a
  before/after pair at the same camera
- parallax and warp are stable across zoom (0.4, 1, 4, 8, 16): both are
  tile-space effects and grow on screen with zoom, so the baseline is only
  proven if the high end does not shear the bed
- switching mode never leaves the map blank; a failed texture load or a lost
  context falls back to `static` with the message visible
- off, hidden tab and `static` show no rAF activity in the performance panel

Performance acceptance is on the owner's machine, not headless SwiftShader:
baseline holds 60 fps with a 2560 × 1440 backing store at typical map zoom;
extra low holds it where baseline cannot; the 30 fps toggle holds 30 on a 60 Hz
display; static costs nothing while the camera is still.
`window.__zzSea.bench()` times 60 frames per mode for this.

Phones are outside the acceptance set for this version: they default to
`static`, the capability probe still applies, and the animated tiers are
offered but not tuned or measured there. Tiling the textures is the later path
to making them work.

## Rollout

- Bump the userscript version and the asset `?v=` in the same commit as the
  new bakes.
- Ship with `static` as the fallback everywhere the shader cannot start, so the
  change is a strict visual improvement, never a blank map.
- `prototype/sea-shader/` stays in the tree until the live overlay matches it;
  it is the reference for every mode's look.

## Open items for the owner

None blocking — phones, the 30 fps toggle and tiling were settled in review.

Three calls the builder made, for review: brightness is a multiplier with a
50–150% range; the coarse-pointer default is `static` for this version; and the
full-map VRAM cost is accepted (~240 MB, colour map included) rather than
downsampling the colour texture.
