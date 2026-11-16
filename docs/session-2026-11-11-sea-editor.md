# Sea editor — session log, 11–12 Nov 2026

A record of what was built, what broke, what was discovered, and how it was
verified. The subject is the bathymetry editor (`tools/sea_editor.py`) and the
generator that ships the texture (`tools/sea_full.py`), plus the userscript
that draws it on zig-zag.fm.

The session started from a working but untrustworthy editor and ended with a
shipped texture, a filter system with nesting, current-driven sediment, debug
overlays, snapshots, and a measured performance pass.

---

## 1. The system, as it ended up

```
tools/sea_editor.py     server + embedded HTML/JS editor + render pipeline
tools/sea_core.py       numpy primitives (noise, blur, erosion, distance)
tools/sea_full.py       full-map render of a saved preset
tools/build_dist.py     one-off exact distance field (out/sea/dist_full.npy)
out/sea/currents_uv.npy cached coarse current field (u, v, basin labels)
out/sea/presets/*.json  saved editor states
out/sea/snapshots/*     snapshot state + picture + thumbnail
dist/zigzag-sea.webp    the shipped texture (7749 x 5254, 1 px per tile)
```

Pipeline order is fixed at the ends and free in the middle:

```
distance -> height -> [filters, each with optional children] -> palette -> coast -> output
```

Each filter is `f(h, node, ctx) -> (value, contribution_or_None)`:

- effect filters return `(h + c, c)` — a signed contribution
- whole-field transforms (warp, blur) return `(transformed, None)`; the caller
  derives `c = value - h`

`apply_filter` runs the filter, applies the node's output curve to the
contribution, recurses into children (which see the contribution, not the
height), then blends with `blend_into`.

Cache: one entry per node, keyed by a **chained** signature — node params,
the masks of the whole subtree, the region, and the upstream node's signature.
Debug planes are stored in the entry alongside the value.

Rendering is generation-based: a new request bumps a counter, the running
render notices between nodes and inside the long loops and gives up, and only
the newest generation is allowed to publish an image.

---

## 2. Timeline, and the bug that drove each step

The order matters: almost every feature was a reaction to something observed
in the previous one.

1. **Panning.** Dragging moved the crop, but the sea only updated on release,
   and the *old* image was nudged by a CSS transform, which drifted from the
   map. Split the map into its own layer (`/api/map`), let it follow the drag
   at ~11 Hz, and keep the sea stale until mouseup. The map is a plain crop of
   a cached raster, so following a drag costs one crop.

2. **Filters as a list.** The chain was fixed; the user wanted `warp` after
   `drainage`, blur anywhere, etc. Turned the middle into an ordered list you
   can add to, remove from and reorder, while distance/height and
   palette/coast/output stay put because moving them is meaningless.

3. **A one-word bug that made every filter inert.**
   `elif nid in FILTER_FN` looked up the filter *id* (`f1`) in a table keyed by
   *type* (`erode`), so no branch matched and every filter passed its input
   through untouched. Found it by pixel-diffing `erode.mix 0` against `1`:
   the images were identical. After the fix: max diff 69.

4. **Cache signatures had to chain.** With the dispatch fixed, editing
   `height` still left the filters serving stale values, because each node's
   signature ignored its upstream. Added `prev_sig`.

5. **Curves.** Three separate failures:
   - `np.interp` needs increasing `xp`; dragging a point past its neighbour
     produced garbage. Points are sorted and de-duplicated before every lookup.
   - Two points at `x = 1` (easy to make by dragging) collapsed the curve to a
     constant, because the right-edge duplicate was dropped rather than
     separated. Points are now *fitted* into `[0,1]` with a minimum gap.
   - The curve editor leaked: every redraw added another `window` listener, so
     after visiting four nodes a single drag mutated all four curves and the
     oldest handler won the mouseup. Now one shared drag record.
   - Reset did not redraw, because `set()` only rebuilt the panel for a few
     keys; `out_curve` was not among them.

6. **The height `curve` did nothing.** It was only read on the `ramp` path;
   the default (and what was in use) is `profile`. Now a gamma on both.

7. **Renders were dropped.** `render_async` started a thread that returned
   immediately if a render was already running, and outside auto mode nothing
   retried. A second edit during a render vanished; a page refresh re-requested
   one, which is why refreshing "fixed" it. Replaced with a queue, then with
   preemption: a new request cancels the running render via a generation
   counter.

8. **Cancellation at node boundaries was not enough.** On a 4-core box an 8.6 s
   erode held the GIL through PIL filters, so the new render waited. Cancel
   checks went *inside* the long loops (erode layers, drainage steps, EDT
   rows). Superseding an 8.6 s render then took 9.0 s total instead of 16.7,
   with exactly one image published.

9. **The preview could freeze the server.** `/api/map` re-decoded the whole
   7749x5254 PNG for every crop request; panning fired one per mousemove.
   Six connections filled with 163 MB decodes, the listen backlog filled and
   `/api/status` could not get a thread: 1.4 GB resident, 0% CPU, editor
   frozen. The decoded layer is now cached once.

10. **Memory.** A render holds a dozen planes the size of the region, and the
    region was user-controlled. Added a 4 MPx cap, a free-RAM floor before
    starting, and `RLIMIT_AS` on the process so a runaway allocation becomes a
    `MemoryError` the render reports instead of an OOM that takes the machine.

11. **Per-node timings** in the node list, so it is obvious where a frame goes
    (`0.0ms` = cache hit).

12. **Coast tinted the whole ocean.** `rim = 1 - (band-1)/(levels-1)` and `band`
    is 0 everywhere outside the rings, so `0` became `rim = 1`. Masked to
    `band > 0`. Test: 276 of 3600 pixels change with a red tint — exactly the
    4 px ring.

13. **Blend modes and masks.** Added `add/multiply/lighter/darker`, then later
    `subtract/screen/overlay`. Discovered that masks were ignored entirely by
    `blur` and `warp` (they return no contribution, so they skipped the mask
    path) — they now lerp toward their input by the mask weight.

14. **Nested filters, names, debug views, overlays, snapshots.** Each added in
    response to a workflow need: filter the output of a filter; tell two
    features apart; see what a node actually does; keep a picture to compare
    against.

---

## 3. Findings catalogue

### 3.1 The dangerous class: silent failures

Almost every serious bug in this session failed *quietly* — the UI accepted the
input, the server returned `{"ok": true}`, and nothing changed. Worth listing,
because they share a cause: a dispatch that did not match, and a response that
did not report it.

| symptom | cause |
|---|---|
| every filter did nothing | id looked up in a type-keyed table |
| filters after an edit used stale values | cache signature did not chain |
| child filters ignored params and enable | `/api/set` searched only top-level filters |
| mask on blur/warp did nothing | those filters never read `masks` |
| palette editing did nothing | `set()` called with 3 args, `body["v"]` raised inside `do_POST` which then dropped the connection |
| "masks wired" wrote junk into mask dicts | panel not cleared between selections; handler read the global `sel` — found in `editor_state.json` as `"masks": []` inside two masks |
| a bad payload killed the connection | no `try/except` around `do_POST` |
| output curve appeared to do nothing | a point parked on `x=1` collapsed the curve |

Fix pattern used throughout: make the failure visible (400 + JSON error,
progress line), and treat "no change" as suspicious rather than acceptable.

### 3.2 Numerical correctness

- **Exact distance field.** The cached field was computed at 2 tiles/px with
  NEAREST downsampling, which *deleted* small islands: 2.3% of the map was more
  than 2 tiles off, 0.66% more than 10, worst 36. Implemented the
  Felzenszwalb–Huttenlocher transform (`edt_exact`), O(n) instead of O(h²w):
  the whole map in 33 s instead of hours. Verified against brute force
  (max error 4.5e-7) and against the old algorithm at `down=1` (bit-identical).
- **Erode normalised per crop.** `slab_erode` used `h.min()..h.max()` as its
  threshold range, so the same place eroded differently in every crop (up to
  0.47 difference) and a shallow crop was driven to black. Now takes an
  explicit range; `auto_range` keeps the old behaviour for comparison.
  Crop stability 0.47 → 0.039.
- **Curves discontinuous at zero.** `sign(d) * interp(|d|)` jumps at every
  sign change when `f(0) != 0` (measured: a `(0, 0.5)` point injected a ±0.5
  step). Now subtracts `y(0)`, so the curve is continuous and zero maps to zero.
- **Blend identity.** Every mode is now the identity at `c = 0`; before,
  `multiply` was `h*(1+c)` (fine) but there was no way to express "no change"
  for whole-field transforms other than `add`.
- **Sediment mass.** Accumulation was unbounded: 40% of a crop saturated at
  height 1. Two causes — land was used as the strongest possible sediment
  source (`d = 0` there), and deposit was not capped by headroom, so the part
  that could not fit was silently clipped while the scour side remained.
  Now: `conserve` 0..1 (0 = accumulate, 1 = displace), deposit capped by
  `1-h`, and the scour matches what actually landed. At `conserve = 1`, mass
  change is exactly 0.

### 3.3 Interaction and feedback

- **Progress was optimistic.** The bar advanced to a node's stage *before*
  running it, and the PNG encode/transfer happened after the last stage, so it
  showed "done" while the browser still had the old frame. Now it reports the
  real node and stays on `loading image…` until the image decodes.
- **Polling died on an early return.** Two separate cases: the poll stopped as
  soon as a render finished (fixed by optimistic `running` and a single poll
  loop), and viewing a snapshot used `return` inside `poll()`, skipping the
  reschedule — so switching back to `current` could show a stale picture.
- **Auto mode could never show one edit.** It polled at 120 ms while the
  debounce was 350 ms; the poll saw "not running" and stopped. Now polls after
  the debounce.
- **Overlay refresh raced.** The overlay had no stamp of its own, so changing
  an overlay setting did nothing until a full render. Now: own stamp, own
  request, busy flag, and it is always announced (including "there is none",
  which clears it).

### 3.4 Determinism: preview vs build

The most consequential discovery of the session. Every fbm-based filter was
anchored to the **crop**: `fbm()` derived its grid resolution from the crop
width and stretched a small random grid over it. So:

- panning changed the pattern (measured same-place differences 0.02–0.22)
- a 1200-wide preview could never predict a 7749-wide build
- strip renders banded at the seams, because each strip saw different noise,
  different erosion ranges and different drainage statistics

Fixed with `fbm_world`: value/perlin/worley noise sampled at map coordinates,
so the same tiles look the same in any crop (verified: two overlapping crops
agree exactly, 0.0). It is behind a `world noise` flag, off by default, because
turning it on changes the look of every existing preset.

`basins` had the same class of problem (coarse cell size derived from the crop
area). Its cell is now fixed at 16 tiles and its values are hashed from map
position; the seed grid is still crop-local, which remains a known difference.

### 3.5 Performance

Measured on `something1` at 2400x1600 (3.84 MPx): **5,816 → 3,676 ms (−37%)**
in the quick-win pass, and the map overlay request 495 ms → 0.7 ms cached.

| fix | before | after |
|---|---:|---:|
| `dilate1` (numpy instead of PIL MaxFilter) | 117 ms/pass | 3 ms |
| `flow_accum` (C arrays, chunked lists) | 1,505 ms | 460 ms |
| PNG `compress_level=1` | 798 ms | 241 ms |
| identity curves skipped | 277 ms | 0 |
| `/api/map` repeat request | 495 ms | 0.7 ms |
| erode node | 3,665 ms | 344 ms |
| coast node | 718 ms | 127 ms |

Also: `sample_bilinear` was computing in float64 because `float32 − int32`
promotes (−23% on currents after fixing); `currents` transport now runs on a
half-resolution grid (`scale`, default 2) for a measured 3.2–4.1×; debug planes
are cached with their node so an overlay no longer forces a recompute (751 →
70 ms for a palette edit); and the long loops are cancellable.

Measured, not assumed: `sig`, `save_state`, `load_region` and `/api/state` are
*not* costs (0.01–3.8 ms), and the exact EDT is off the interactive path
entirely when `dist_full.npy` exists.

---

## 4. What did not work, and what replaced it

| attempt | why it failed | what replaced it |
|---|---|---|
| Parallel strips for full renders | every filter is crop-anchored, so strips banded at the seams (0.1–0.22 height steps) | single pass over 40.7 MPx; strips remain opt-in with a warning |
| Cached distance at 2 tiles/px | dropped small islands; 2.3% of the map >2 tiles wrong | exact O(n) EDT, 33 s once |
| Centred basin offsets | `lighter` only lifted the above-average half at half strength, so it looked inert | per-patch *floor* value, contribution = `target - h` |
| Pure noise current field | each island got its own little gyre; islands did not share a current | basin gyres from a streamfunction relaxed around land |
| Connectivity flood for basins | the sea is one connected body: 1 basin at any useful threshold | watershed from deep points, split at sills |
| `speed_bias` as a post-multiplier | sediment was already correlated with speed; the knob only removed material | left as a sharpening control, documented as such |
| Slope as a pick-up source on a clean ramp | a distance-based height is piecewise linear, so its slope is piecewise constant — the source saw polygonal artefacts of the ramp | kept, but only meaningful once the field has detail |
| `feedback` accumulating the floor | `floor += 0.15 × plume` hit 1.0 within a few steps; the gradient went flat and the weight stopped mattering above ~0.5 | floor recomputed as `h + 0.6 × plume` each step |
| Full-res distance via the old EDT | O(h²w) — hours at 40 MPx | the parabola envelope |

---

## 5. How the work was approached

The method that made progress possible, in the order it mattered:

1. **Reproduce with a number before touching code.** Nearly every fix started
   with a pixel diff, a timing, or a correlation: `erode.mix 0` vs `1` (0.0
   difference → the dispatch bug), identity vs inverted curve (255), two crops
   of the same tiles (0.22 → crop anchoring), mass change at `conserve = 1`
   (exactly 0).
2. **Distrust "it works".** A control that appears to do nothing is a bug until
   measured. That is how the palette, the mask panel, the blend modes and the
   child-filter settings were all caught.
3. **Check the invariants, not the picture.** Mass conservation, identity at
   zero contribution, continuity through zero, crop independence. Each is a
   one-line assertion that catches a whole class of regression.
4. **Restart, then verify the state survived.** The editor keeps state on disk;
   every restart was followed by a read of `/api/state` to confirm filters,
   masks, names and region came back.
5. **Measure where the time goes, then fix the top item only.** The per-node
   timing column and one `cProfile` run replaced guesswork; the performance
   pass changed four functions, not forty.
6. **Keep the docs honest as they are touched.** Tooltips, `DESC` entries and
   the README were corrected in the same commit as the behaviour, including the
   admission that the README had been documenting a build path that was not the
   one used to ship.

### Two things that went wrong operationally

- **The editor state was lost once.** Both ports shared one state file, the
  file only persisted `region/channel/nodes/masks` (not the filter chain), a
  test on 3008 overwrote the live 3007 state, and a kill truncated it to zero
  bytes. Since then: per-port state files, filters included, atomic writes
  through a temp file, and an `/api/restore` endpoint to put a browser's copy
  back. The user's working state was lost in that incident and had to be
  rebuilt; it is the one unrecoverable cost of the session.
- **A shipped texture was built at different settings than the editor showed**
  (`travel 150` vs `120`) because the render was started before the setting
  changed. Not fatal — the texture is a snapshot by definition — but it is
  worth remembering that a full build is a 2–11 minute job and edits made
  during it do not affect the output.

---

## 6. Measurements worth keeping

| quantity | value |
|---|---|
| full map | 7749 x 5254 tiles (40.7 MPx) |
| max distance from land | 1297.6 tiles (corner), 1205.9 ignoring a 64 px border |
| distance percentiles over water | p50 122, p90 378, p99 810 |
| exact distance field build | 33 s |
| full render, light preset (`something1`) | 79 s, 3.9 GB peak |
| full render, currents-heavy (`interesting_v1`, travel 150) | 683 s, 4.86 GB peak |
| editor frame, 4 MPx, `something1` | 5,816 → 3,676 ms after the perf pass |
| editor frame, 1 MPx, late-node edit | 195–223 ms |
| currents node, 4 MPx, travel 45 | 1,853 ms at scale 1, 580 ms at scale 2 |
| currents node, heavy (travel 150, feedback 0.95) | 10.8 s at scale 1, 2.6 s at scale 2 |
| shipped texture (`0.2.15`) | 2.03 MB WebP, quality 92 |
| WebP vs PNG at 1:1 | PSNR 44.5 dB, max 34/255, visible only as edge ringing |

---

## 7. Open items

- `basins` seed grid is still crop-local (the cell size and value hash are
  global) — a basin near a crop edge can differ from the full map.
- `world noise` costs 8× (value) to 44× (worley) more than crop-anchored noise;
  it is off by default and the cost is not shown in the UI.
- Two-phase preview (quarter-res first, then full) was designed and not built;
  it is the only thing that would cut click-to-first-pixel.
- Full-map memory is 4.9–5.3 GB; float32 colour and freeing untapped fields
  would roughly halve it.
- `feedback` still compresses near 1 (a blend of unit directions), so the top
  10% of the knob changes little; a non-linear remap would fill the range.
- The shipped texture was built at `travel 150`; the editor now sits at 120.
