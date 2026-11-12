# Sea editor audit — 2026-11-12

Full read-only audit of `tools/sea_editor.py`, `tools/sea_core.py` and `tools/sea_full.py`
by a subagent, 58 findings. This file is the raw report; the summary is in chat.

## Summary

- Palette editing is completely dead: `paletteEditor` calls `set('nodes','palette',{stops,cols})`
  (the only 3-arg call), so `do_POST` raises `KeyError 'v'` and closes the connection.
- `scale = 0.5` crops the region instead of resampling: 0.5 renders the NW quarter, magnified.
- Every fbm-based filter is anchored to the crop, not the map: noise/features/regions/warp
  patterns change when you pan, and a preview can never predict a 7,749 px build.
- `slab_erode` normalises to each crop's min/max, so the same place differs by up to 0.47
  between crop sizes, and a shallow crop is driven to black.
- `basins` derives its coarse cell size and seed grid from the crop, so zones differ per crop.
- Cache holes: child-filter masks are not in the parent signature; child overlays work once.
- Blend is only honest for 6 of 10 filter types (matrix below).
- `tap="base"` is not the base height; there is no `"height"` tap.
- Masks wired into the five fixed nodes are inert but offered.
- The "masks wired" panel is not cleared between selections and writes stray keys —
  `out/sea/editor_state.json` contains `"masks": []` inside `depth_band` and `mask_2`.

## Blend-mode matrix

| filter (offered?) | add | multiply | lighter | darker |
|---|---|---|---|---|
| warp (no) | meaningful | degenerate | harmful | harmful |
| blur (no) | meaningful | degenerate | harmful | harmful |
| noise (yes) | meaningful | meaningful | meaningful | meaningful |
| features (yes) | meaningful | meaningful | **no-op** | **no-op** |
| erode (yes) | meaningful | degenerate | ≈no-op | ≈add |
| drainage (yes) | meaningful | odd | **no-op** | = add |
| shelf (yes) | meaningful | odd | no-op at defaults | darkening half |
| regions (yes) | meaningful | can black out | meaningful | meaningful |
| basins (yes) | meaningful | degenerate | partial | lowers only |
| currents (yes) | meaningful | odd | defeats conserve | meaningful |

## Findings

F1  blocker  Palette editor does nothing (wrong `set()` arity).
F2  major    `blend` no-ops offered as meaningful (features/darker, drainage/lighter, shelf).
F3  major    warp/blur return a delta and are blended; only `add` reconstructs them.
F4  major    Sub-filters run on the parent's delta; the UI calls it "output".
F5  major    "Output curve" means three different things (height / signed delta / child delta).
F6  major    Filter curves are discontinuous at zero when f(0) != 0.
F7  minor    `multiply` is `h*(1+c)` and can clip to black.
F8  minor    Debug "contribution" ignores blend and curve.
F9  blocker  Editing a nested child's mask does not invalidate the parent cache.
F10 major    Child overlays / `dbg:<child>` channels work once, then vanish.
F11 major    Mask channels with a `tap` set `stop_at`, cutting off later overlays.
F12 major    Cache is keyed only by node id and shared between threads.
F13 minor    Debug planes are always built, even with no overlay (~100 MB at 4 MP).
F14 minor    Rename / overlay colour changes recompute the whole chain.
F15 major    `tap="base"` is not the base height.
F16 major    Tapping a nested child silently falls back.
F17 major    `mask_weight` on a child measures the parent's delta.
F18 major    Masks wired into the fixed nodes are inert but offered.
F19 major    Stale "masks wired" panel writes junk keys into masks (confirmed in state file).
F20 major    Disabling the palette blacks out the ocean; tooltip says "passes through".
F21 major    `distance.enabled` / `distance.down` are inert when `dist_full.npy` exists.
F22 minor    `abyss_t` and `feather` are dead; `mask.seed` is live but hidden.
F23 minor    `currents` sub-params are conditional but not greyed.
F24 minor    `coast.strength < 0` and `shelf.power < 0` are silently degenerate.
F25 blocker  `scale` crops instead of resampling.
F26 blocker  fbm is crop-anchored.
F27 blocker  Erode is normalised per crop.
F28 major    Basins is crop- and resolution-dependent.
F29 major    Drainage and currents normalise by crop statistics.
F30 major    `sea_full` strip mode bands; only `--single` is coherent.
F31 major    `sea_full` has no memory guard; `--scale` is not a downsample.
F32 major    Presets lose channel/next_mask/overlay_map and can collide ids.
F33 major    Presets without `filters` mean "use the default chain" rather than "none".
F34 major    `load_state` replaces node dicts wholesale and swallows failures.
F35 minor    `sea_full` skips `normalise_filters` and type validation.
F36 major    Unhandled POST exceptions close the connection with no message.
F37 major    A cleared number box becomes `null` and poisons the persisted state.
F38 major    Overlay-only refresh is a race; the client usually misses it.
F39 major    Clearing an overlay on a normal render never tells the client.
F40 major    The overlay thread cannot be cancelled and can overwrite a newer overlay.
F41 major    State mutation and rendering share no lock.
F42 minor    `_lock`/`_gen_lock` ordering is safe (no deadlock).
F43 major    Memory caps can fail with an empty error string.
F44 major    Several UI-reachable values produce NaN / flat fields.
F45 minor    Dead code that looks live (`n_warp`, `n_shade`, `/api/restore`, ...).
F46 minor    Discrete labels are handled correctly.
F47 major    The channel dropdown gets stuck on a deleted channel.
F48 major    The map overlay toggle does nothing until a render.
F49 minor    Panning is not 1:1 with the cursor.
F50 minor    Dead/duplicate overlay options (`accum`, `floor`); density ignored by some kinds.
F51 minor    The params panel duplicates the overlay controls.
F52 minor    Palette/mask panels leave stale lower sections.
F53 minor    Region edits cannot escape a too-big box one field at a time.
F54 minor    Out-of-bounds regions error as "cannot write empty image" and still report success.
F55 cosmetic Tooltip errors (`warp.tiles`, `ramp`, `currents.strength`, missing entries).
F56 cosmetic `has_image` / 204 handling.
F57 major    README documents `build_sea.py`, but the shipped texture comes from `sea_full.py`.
F58 minor    `sea_full` docstring says dist_half but uses dist_full.

## Proposed fix pass

### Batch A — before the next texture
A1  Palette commit arity + JSON errors on POST (F1, F36)
A2  Remove `0.5 px/tile` or make it a true resample (F25)
A3  World-anchored fbm, gated behind a flag so old presets can be compared (F26)
A4  Erode: explicit lo/hi for top-level filters (F27)
A5  Basins: constant coarse cell size and global grid (F28)
A6  Cache: transitive masks in the signature; subtree overlay/dbg; no stop_at with overlays (F9-F11)
A7  `tap="base"` stores the real base height (F15)
A8  Overlay refresh: busy flag + always bump ov_stamp (F38, F39)
A9  Disable no-op blend options; force add for whole-field transforms (F2, F3)
A10 Ignore empty/NaN numbers in the UI and validate server-side (F37)

### Batch B — should fix
B1  Zero-continuous curves + document the three meanings (F5, F6, F8)
B2  Missing filters means none; normalise/validate on preset load (F33, F35)
B3  Preset round-trip: channel, next_mask, overlay_map, id counters (F32)
B4  Deep-merge state on load, report failures (F34)
B5  Wrap POST in try/except, surface errors, make poll() retry (F36, F41)
B6  Concurrency: bump generation on mutating POSTs, snapshot state, generation for overlay (F38-F41)
B7  Build debug planes only when needed; strip non-field keys from the signature (F12-F14)
B8  Clean taps/channels when deleting filters or masks (F47)
B9  Clear the wired panel every selection; hide it for fixed nodes; capture filt not sel (F18, F19, F52)
B10 Disable inert distance controls; palette-off shows height not black (F20, F21)
B11 Clamp degenerate params and validate slope_t > shelf_t (F44, F24)
B12 sea_full: default --single, auto margin, memory guard, document --scale (F30, F31)
B13 Remove dead overlay options; grey density where ignored; skip overlay keys in the extras loop (F50, F51)
B14 Map overlay refresh + pan maths (F48, F49)
B15 Region clamping, 400 on rejection, render_now returns False on error (F53, F54, F43)

### Batch C — nice to have
C1  Remove dead code (F45)
C2  DESC pass + README/build docs (F55, F57)
C3  Remove abyss_t/feather; expose mask.seed (F22)
C4  Grey currents sub-params at trigger 0 (F23)
C5  Normalise drainage/currents from whole-field stats (F29)
C6  Palette >= 2 stops; basins levels off-by-one
C7  Cosmetic: "cached" instead of 0.0 ms, density label, label colours per crop

---

## Status after the fix pass (2026-11-12)

All of A, B and C implemented, plus:

- `scale` is now a true downsample: the region is always rendered at 1 px/tile
  and the image is resized for display, so 0.5 shows twice the area instead of
  a magnified corner. Radii in tiles stay meaningful.
- `world_noise` flag in the top bar: anchors every fbm to the map grid, so a
  preview matches a full render and panning does not shift the pattern.
- noise types on noise / features / regions / warp / masks:
  `value`, `perlin`, `worley`, `ridged`, `billow`, `white`.
- blend modes: `add`, `subtract`, `multiply`, `screen`, `overlay`, `lighter`,
  `darker`; every mode is the identity at c = 0, and the no-op options are
  disabled per filter type.

Verified after the pass:

- blend identity at c = 0 for all seven modes
- `apply_curve` continuous through zero (0 at 0, ±0.0005 either side)
- child-mask edit changes the parent's output (0.08) — cache invalidation fixed
- erode crop stability improved from 0.47 to 0.039 (explicit 0..1 range)
- world noise: two different crops of the same tiles agree exactly (0.0)
- palette commit applies (colour changed on the server)
- oversized region clamped (7000x5000 -> 2366x1690) with an explanatory message
- invalid POST returns 400 JSON instead of dropping the connection
- half-res preview doubles the visible area at half the pixels (420x300 -> 210x150)
