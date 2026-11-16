# Sea shader prototype — session log, 2026-11-13/14

Work done on `prototype/sea-shader/` (the throwaway WebGL2 prototype that draws an
animated sea over a 1:1 crop of the map, meant to become a Tampermonkey mod).

Scope: everything below was driven from the prototype, with the sea editor and
generator (`tools/sea_editor.py`, `tools/sea_core.py`) treated as read-only
inputs. The bake step (`prototype/sea-shader/bake.py`) is ours.

---

## 1. What the effect is, in one paragraph

Per pixel it reads a distance field and a depth field from `aux.png`, a base
colour from `colour.png`, a flow direction from one of three sources, and draws
the seabed with a parallax shift (with an optional POM ray march), shore waves
driven by distance isolines, a shelf glow, and two advected simplex noise layers
that modulate the seabed brightness ("grain"). Land is composited on top from
`land.png`. Everything is evaluated on the texture's own pixel grid
(`texel-res`), 1 texel = 1 map tile.

---

## 2. Feature work, in order

### 2.1 Advection: two-phase cycling (replaced unbounded offset)

Was: `noise(p - flow(p) * speed * t)`. Because the displacement grows without
bound, its Jacobian `I - t·∇flow` grows too, so wherever the current varies the
noise shears into longer and longer streaks — the "stretching one texture
forever" symptom.

Now: two copies of the field, the second half a cycle behind, each displaced by
at most `flow(p) * flowSpeed * cycle` map tiles and cross-faded by
`1 - |2·fract(t/cycle) - 1|`. A copy resets while its weight is zero, so the
cycle is invisible. Contrast is divided by `sqrt(w1²+w2²)` so the grain does not
pulse (measured: mean luma 120.6–120.9 over a cycle, std 45.6–51.0, ±6%).

Two numbers matter: `cycle` (slider, 0.5–60 s) sets the travel budget; the
shore-lap and shelf-breath noises use the same cycle at 30%/40% of the travel.

### 2.2 Slider limits

`a speed`/`b speed` → 0–10, `wave` → 0–20, `flow` → 0–6, `cycle` → 0.5–60.
Defaults unchanged; handles sit left of centre on the widened tracks.

### 2.3 Shareable state (`save`)

`save` writes every slider, the variant, the camera, the flow source, arrows /
slices / texel-res, **and all three curves** (`pc`, `sc`, `wc`, five
comma-separated points each) into the URL, copies it, and leaves it in the
address bar. Snapshot semantics: later tweaks are not in the link until you
press save again.

This surfaced a pre-existing bug: `flow` was doing double duty as the speed
slider *and* the flow-source index, so `?flow=1.2` also selected the 1:1 mode.
Source index is now `fsrc` (old `flow=` links still work as before).

### 2.4 Presets / baseline

Variant `P` ("baseline") is the default for a bare URL; `A`/`B`/`C` remain as
comparisons. Each baseline save was verified by rendering the bare URL and the
saved link at the same `t` and diffing: the last one is **0 differing pixels**.
The camera defaults are part of the preset, because parallax and its warping
scale with zoom.

### 2.5 POM slice spacing curve

The POM's slice heights came from a fixed `1/N` step. They now come from a third
LUT curve (`sc`): `dep = S((i+1)/N)`, so flattening the curve crowds slices into
that depth range and steepening spreads them out. Drag enforces non-decreasing,
or the ray would step uphill. Linear reproduces the old behaviour exactly.

### 2.6 Texel-res parallax

The parallax sample was built from the raw screen position, so it resampled the
bed bilinearly regardless of `texel-res`. All colour samples now go through
`parPos()`, which snaps the *shifted* position to a texel centre:

```glsl
vec2 w = p + parShift(p, dep);
return mix(w, floor(w) + 0.5, uSnap);
```

Verified by rendering snap on/off at zoom 16: with snap the bed moves in whole
texels, without it it slides smoothly.

### 2.7 Flow field: what the modes actually are

- `flow.png` — the **raw** 16-tile gyre over the whole map (~0.27 MB). No
  coastal bend: a 16-tile cell can only take the gradient at cell scale, which
  smears small islands, so its bend is not a faithful low-res version of the 1:1
  field and cannot be reconstructed from.
- `flow_detail.png` — the gyre blended with the editor's deflection
  `deflect·exp(-d/reach)` (currently 1.0 / 50 tiles) at one texel per 2 tiles.
- LOD — the raw gyre plus the *along-coast vector*, reconstructed from data the
  effect already ships, so it costs no per-tile flow texture.

Consequences measured: at a coastal camera 16-vs-1:1 differ on 8.4% of pixels;
LOD now matches 1:1 to 0.87° mean / 0.41° median / 1.85° p90 direction, and the
rendered water to 1.8/255 mean.

### 2.8 Fold ("pole") suppression

A linear blend of the gyre and the along-coast direction cancels where they are
near-antiparallel (`|blend|` p1 = 0.000 there; the two differ by a median of
157° inside the band). The normalised direction then flips up to 254° between
adjacent pixels, and advecting through that squashes the noise into a crease.

Three things were tried; only the third plus the turn-rate budget worked:

1. **Damp the speed by `|blend|`** over a box-blurred patch (`SEA_CALM_TILES`,
   default off). Folded area 0.47% → 0.10%, squashed 2.16% → 0.78%. Alone it was
   not enough — the surviving creases are where the direction simply turns fast,
   not where the blend cancels.
2. **Turn-rate budget** (the one that works): `keep = 1/(1 + (|∇θ|/6)^3)` with
   `|∇θ|` in deg/tile, blurred over 4 tiles. Full speed below ~4 deg/tile, then
   falls away. Folded 0.16% → 0.08%, squashed 0.67% → 0.40%, median speed
   unchanged, ~10% of the crop slowed at all. `SEA_GRAD_G0` / `SEA_GRAD_POW` /
   `SEA_GRAD_TILES`.
3. **Clamp the travel** (`clamp`, default **off**): caps one cycle at 12 tiles.
   It bounds the worst case the sliders allow but also slows the whole sea,
   which made the remaining poles stand out — hence off by default.

The values were picked from a rendered matrix (grad 3/4.5/6/9 × calm off/12/30,
each cell annotated with folded / squashed / slowed) rather than by eye.

### 2.9 Storage / resolution

- Coarse stays at 16 tiles: that is already the resolution of the gyre; halving
  it changed direction by 1.7° mean but 23.7° at p99 where currents cancel.
- Detail ships at **2 tiles/texel**: 0.5° mean direction change, and the rendered
  1:1 mode differs from the 1-tile bake by 0.09/255. Halves the download, quarters
  the VRAM.
- Both detail bakes are shipped so they can be compared live (`detail` button,
  `?detail=hi|lo`, `d`).
- `bake.py` takes `SEA_FLOW_CELL` / `SEA_DETAIL_STEP` and writes the resolutions
  into `assets/meta.json`; the page reads them back plus the real texture sizes,
  so a re-bake at another scale needs no code change.

### 2.10 Surface warp (water distortion)

The two simplex layers double as a warp field (`vec2(nA, nB)`) — no extra noise.
Applied as the **base of the whole parallax pass**:

```glsl
vec2 base = p + warp;          // depth lookup, slice selection, terrain test
parPos(base, dep)              // ...and the colour fetch all run from here
```

Parameters: `warp` (0–10, 3 tiles per unit → up to 30 for stress testing),
`wbias` (0 = distort everywhere, 1 = only deep water), and the **warp depth**
curve. Applying it to the colour fetch only (the earlier attempt) translated the
sampled colours while the slice-crossing edges stayed where the un-warped depth
field put them, so POM's harsh edges did not move with the distortion.

### 2.11 Arrow overlay

Rebuilt per view rather than once for the whole crop: spacing ≈20 screen px,
length 55–100% of an 8 px tick, rebuilt 220 ms after the camera settles. Two
problems this solved: at low zoom the fixed 4-tile grid plus 17 px arrows merged
into continuous stalks that read as horizontal lines, and a 0→100% length map
made standstill water draw a zero-length arrow, cutting holes whose edges looked
like creases.

---

## 3. Bugs found (each with the symptom that exposed it)

| symptom | cause | evidence |
|---|---|---|
| LOD grain jittered tile to tile ("turbulence") | LOD rebuilt the along vector per pixel from an 8-bit blurred-distance gradient | replaced with the vector baked into `aux.BA`; direction error vs 1:1 dropped to 0.87° |
| LOD's swirls displaced, ~90° off | the vector was stored as `(dbx, -dby)` — the gradient, not rotate90 of it | after the swap: median 1.3° → 0.3° |
| LOD had a still line the 1:1 didn't | shader re-derived the speed (turn of the *coast* vector + a `|blend|` term the bake lacks) | replaced with a baked `flow_speed.png` (4 tiles/texel, 0.04 MB); `|diff|` 0.0048 mean |
| a whole band of LOD arrows pointed the wrong way | `aux[...,3] = 255` left over *after* the along vector was written into A, so the shader read `(along_x, 1.0)` | removed; aux.A now matches the bake to 0.004 |
| arrow overlay punched a rectangular hole | `imgData()` decoded PNGs through a 2D canvas, which is **premultiplied**; `aux.png` carries data in its alpha channel, and where that alpha hits 0 the canvas zeroes the RGB, so `aux.R` read as 0 → "land" | page read `[0,0,0,0]`, file `[164,179,134,0]`; 486 lattice points skipped instead of 129. Fixed by decoding via `texImage2D` + `readPixels` (straight alpha) |
| 1:1 mode "glitchy/doubled" at half res | detail UV divisor was the *texture's* pixel size, not the crop's tile size, so UV ran 0..2 and wrapped | reverted to `p / uSize`; 1:1 vs the 1-tile bake 0.09/255 |
| shore lap ran *against* the current | `lap2` used `+flow*t` where the others used `-flow*t`, and ignored the flow-speed slider | fixed signs and speed factors |
| specks / invalid premultiplied pixels on filled land | `alpha = wet + land.a` could reach 2 | `max(wet, land.a)`; `aux.R` zeroed on land |
| open-sea inlets labelled land | flood fill seeded only from the four corners | floods from every border pixel (holes 2220 → 1515) |
| 1:1 mode deleted every standstill | `flow_detail.B` was `|unit vector|`, i.e. constant | now samples the coarse coherence |
| LOD invented a south-east direction in slack cells | `normalize(cdir + 1e-6)` | guarded |
| `?flow=1.2` also switched the flow source | one param for two things | split into `flow` (speed) and `fsrc` (source) |

---

## 4. What did not work

- **Layer-Gaussian POM compositing**: soft, but deep layers show *through* shallow
  ones — no occlusion.
- **5-tap / adaptive-spread kernel around the POM crossing**: widened with the
  depth jump, so it either did nothing (small jumps) or smeared normal slopes;
  and at max `par` the visible bands are the base texture's own depth contours,
  which no kernel can remove.
- **Blending angles instead of vectors** (capped at 75°): *worse* — the sign of
  the rotation flips across the antiparallel locus, giving a 150° jump. The
  obstruction is topological.
- **Box-smoothing the blend vector where `|blend|` is small**: no better; the
  cancellation is a curve, blurring relocates it.
- **Fading `near` by gradient coherence** (to escape medial-axis flips in LOD):
  created a *new* seam where it fell back to the gyre, and mismatched the bake.
  The real fix was shipping the vector instead.
- **Residual-correction LOD** (bake the bend into the coarse field, add only the
  "rest" in the shader): cannot work — the cell's bend was computed from a
  cell-scale gradient, so the missing part is not recoverable.
- **Curl/streamfunction advection**: divergence-free does not mean fold-free; a
  rotational vortex folds the same way.
- **Shader-side Jacobian damping**: four extra texture fetches per pixel to
  reproduce what the bake already knows.

---

## 5. How the work was verified

- **Pixel diffs** between a saved link and the bare URL at the same `t` — the
  test that the preset really is what was saved (last: 0 differing pixels).
- **Field-level numpy comparisons** against the baked assets: direction error,
  speed error, fold/squash statistics computed from the Jacobian of
  `q → q − flow·travel`, so "does it look better" had a number behind it.
- **In-shader diagnostics**: `debug=3` paints the flow field; `debug=11` paints
  the angle between the LOD reconstruction and the 1:1 bake (black = agreement),
  which is how the LOD/speed mismatch was tracked down.
- **Staged A/B builds**: for anything structural (warp ordering, along encoding)
  the old variant was patched in, rendered at the same `t`, and restored — so
  comparisons were like-for-like instead of "before I remember it".
- **Parameter matrices** rendered as one grid with per-cell metrics, used to
  pick the turn-damping knee.
- **Independent subagents** (read-only) for the shader maths, the runtime
  visuals, the baked data, and the flow-pole research. They found the `aux`
  premultiply bug, the `|blend|` cancellation mechanism, and the median 157°
  angle between the gyre and the coast direction — things that were invisible
  from the render alone.
- `?ui=0` hides the panel for screenshots; `?t=` freezes time; `?snap=0` turns
  off texel-res.

---

## 6. Current knobs

| knob | where | default |
|---|---|---|
| `SEA_FLOW_CELL` / `SEA_DETAIL_STEP` | `bake.py` env | 16 tiles / 2 tiles |
| `SEA_DEFLECT` / `SEA_DEFLECT_REACH` | `bake.py` env | 1.0 / 50 tiles |
| `SEA_CALM_TILES` | `bake.py` env | 0 (off) |
| `SEA_GRAD_G0` / `SEA_GRAD_POW` / `SEA_GRAD_TILES` | `bake.py` env | 6 / 3 / 4 |
| `warp` / `wbias` / warp-depth curve | UI + saved link | 3.3 / 1.0 |
| `cycle` / `clamp` / `flow` | UI | 7 s / off / 2.31 |
| `par` / `pom` / depth curves | UI + saved link | 0.814 / 5 |
| `detail` (1× or 2× bake) | UI, `?detail=hi|lo` | lo |
| `debug` | `?debug=N` | 0 |
| `ui` | `?ui=0` | shown |

Asset sizes for the crop (1600×1000): `flow.png` 0.27 MB, `flow_detail.png`
0.36 MB, `flow_detail_hi.png` 1.02 MB, `flow_speed.png` 0.04 MB, `aux.png`
1.63 MB, `colour.png` 0.83 MB, `land.png` 0.25 MB.

---

## 7. Known limitations

- The coarse 16-tile mode carries no coastal bend by design; it is the flat-gyre
  mode.
- The LOD speed texture is 4 tiles/texel and the detail bake is 2, so the two
  modes differ by ~0.005 in speed (0.07 at p99).
- The bake's `near` uses a 2×2 block mean of the distance; the shader uses the
  point distance — about half a tile of difference.
- `aux.BA` is 8-bit: ~0.45° of direction quantisation.
- The `|blend|` contraction still exists in the data (it is topology), it is only
  damped; the arrows will always curl around a genuine pole.
- Full-map VRAM is dominated by `aux` at 1:1 (~163 MB), not by the flow textures;
  if that has to come down, the distance/depth packing is the place to look.
