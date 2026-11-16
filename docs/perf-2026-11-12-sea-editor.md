# Sea editor performance review — 2026-11-12

Read-only review by a subagent. Environment: 4-core, 15.7 GB, numpy 2.5.3.
Measured on `out/sea/presets/something1.json` at 2400x1600 (3.84 MPx) and
1200x800 (0.96 MPx), medians of 3.

## Where the frame goes — `something1` at 3.84 MPx (5 816 ms)

| cost | ms | share | cause |
|---|---:|---:|---|
| drainage (`f5`) | 2 068 | 35.6% | `flow_accum` Python loop, 1 505 ms |
| PNG encode (level 6) | 798 | 13.7% | `Image.save` default |
| coast | 718 | 12.3% | 5x `dilate1` at 117 ms each (PIL MaxFilter) |
| noise / fbm (12 calls) | ~1 170 | 20% | recomputed per consumer |
| noise `f11`+`f15` | 369 | 6.3% | |
| warp `f8` | 283 | 4.9% | |
| basins `f14` | 282 | 4.9% | |
| `apply_curve` (identity curves) | 277 | 4.8% | runs `np.interp` anyway |
| features `f10`, warp `f2` | 470 | 8% | |
| palette (float64) | 122 | 2.1% | |
| `load_region`, `sig`, `save_state` | < 5 | 0% | not costs |

`f_currents` (in the live state, not this preset): **14.6 s at 3.84 MPx** with
travel 45, and **55-83 s** with travel 150 / feedback 0.95.

## Top findings

### F1 · currents transport is O(travel × pixels) — 95% of the frame
`sea_editor.py:1125-1143`. Full-plane bilinear gather + two blends + max per
step, plus `box_blur`/`gradient` per feedback step. The gyre field is 16
tiles/cell, so the transport is inherently low-frequency.
Fix: run the transport on `h[::s,::s]` and upscale the deposit (parameter
`scale`, default 2). Measured at 1200x800: travel 45 2 389 -> 539 ms (4.4x),
travel 150/feedback 14.4 -> 3.2 s (4.6x); mean contribution error 0.9%.

### F2 · `dilate1` is a PIL round-trip per pass — 30-36x slower than numpy
`sea_core.py:334`. 3.84 MPx: 116.9 ms vs 3.2 ms numpy, identical output.
Knock-on: erode 3 665 -> 344 ms (default state frame 4 159 -> 849 ms), coast
718 -> ~60 ms, full-map coast -6 s.

### F3 · `flow_accum` is a pure-Python loop over every cell
`sea_editor.py:884`. 1 505 ms at 3.84 MPx; 17.2 s of the full-map drainage node.
Fix: lists + chunked `.tolist()`: 475 -> 234 ms per step, identical result.

### F4 · PNG encode at default compression is 14% of the frame
2400x1600: level 6 798 ms / 2.15 MB; level 1 239 ms / 2.72 MB.
`compress_level=1` costs +26% bytes on loopback.

### F5 · `/api/map` re-encodes a PNG per request and is never cached
66 ms (1200x800) to 1 069 ms (4800x3200) per crop, refreshed on every stamp and
every 90 ms while dragging. Fix: memoise encoded crops, drop `Date.now()`, and
only refresh on mouseup.

### F6 · Superseded renders can't be cancelled inside the long loops
A currents render kept running 14.2 s after being superseded. Add `check_cancel`
inside the travel loop, `flow_accum` chunks, the basins flood and the overlay
drawing loops.

### F7 · Any overlay forces debug planes for every node
30 planes / 461 MB at 4 MPx, and `want_planes` bypasses the cache so an overlay
makes late edits 3.4x slower (751 vs 223 ms). Only build planes for the nodes
whose overlay is on.

### F8 · `sample_bilinear` silently computes in float64
`fy = ys - y0` promotes float32 - int32 to float64; everything downstream
doubles. Fix: `y0.astype(np.float32)`. Currents -23%.

### F9 · Colour chain is float64
palette and coast cache entries are 92 MB each instead of 46 MB. Cast to
float32 (or a 256-entry LUT: 122 -> 62 ms, error 1.4/255).

### F10 · Full-map build peaks at 5.3 GB, 1.5x per pixel vs the editor
87 s clean, 169 s under load; 24.7 s of `sys` time from retained planes.
float32 colour (-976 MB), free untapped `fields`, buffer reuse.

### F11 · Auto mode can never show a single edit's result
`set()` polls after 120 ms but the auto debounce is 350 ms, so the poll sees
`running == false` and stops. Poll after the debounce instead.

## Quick wins, measured together

A1 numpy `dilate1` + A2 `flow_accum` + A3 PNG level 1 + A5 skip identity curves:
**5 832 -> 3 822 ms (-34%)** on the 4 MPx preset frame, and the default-state
frame 4 159 -> 849 ms.

## Not worth changing (measured)

- `sig` 0.01 ms/node, `save_state` 0.4 ms, `load_region` 3.8 ms, `/api/state` 0.13 ms.
- `edt_exact` 2 872 ms at 2400x1600 — but off the interactive path when
  `dist_full.npy` exists; only used once by `build_dist.py`.
- `argsort`, the basins heap, `MaxFilter(9)`, `channel_image` min/max: each 1-2%.
- HTTP keep-alive: ~1 ms on loopback.
