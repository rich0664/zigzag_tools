# Heightmaps, erosion and procedural bathymetry

Research notes for the `zigzag-sea` depth texture (7749 × 5254 cells, 1 cell = 1 map tile,
~8 % land, numpy + Pillow only, a few minutes on one desktop core).

Written 2026-11-11. All timings below were measured on this machine with the repo's
existing numpy/Pillow environment, single-threaded, at the resolutions actually used by
`tools/sea_core.py` (10.2 M cells at `relief_down=2`, 2.5 M at `relief_down=4`).

---

## Summary — what to try, ranked by effort vs payoff

| # | Change | Effort | Payoff | Why |
|---|--------|--------|--------|-----|
| 1 | **Piecewise margin profile** (shelf → shelf break → slope → rise → abyss) instead of one exponential ramp | S (½ day) | ★★★★★ | The shelf break is the single strongest depth cue in every real bathymetric map; one `np.interp` over a warped distance field. |
| 2 | **Stop deriving the abyss from distance**; cross-fade to a large-scale "crustal" field (basins, ridges) beyond the slope | S–M | ★★★★★ | Removes medial-axis creases *and* gives "shallow bank vs deep basin", which distance alone can never express. |
| 3 | **Noise-perturbed coastline + coarse EDT** before the distance ramp | S | ★★★★ | Kills the parallel-offset-contour look near coasts for ~1 s of compute. |
| 4 | **Add a real drainage network at 1/4 resolution** (D8 + pointer-doubling accumulation + ~20 implicit stream-power steps), upscale the *change* only | M (2–3 days) | ★★★★★ | The only cheap family that produces true dendritic channels rather than marbling. ~2.5 M cells makes the sequential parts affordable. |
| 5 | **Make slab erosion ~8× cheaper** (replace the per-threshold dilation window with a jump-flooded distance-to-threshold) | S | ★★★ | It is the slowest stage of the current build; the freed minute pays for #4. |
| 6 | **Bathymetric feature rules**: seamounts, abyssal hills, fracture zones, one ridge axis | S–M | ★★★★ | Fixes "empty flat blue" at low zoom, where most players actually look. |

Do **not** bother with: full shallow-water / pipe simulation, Lattice-Boltzmann, or an ML
erosion model. They are 10–1000× the cost for a picture that is displayed at one pixel per
tile behind map tiles.

The rest of this document explains each claim and gives the sources.

---

## 0. What the current pipeline actually does

`tools/sea_core.py`:

1. `edt_small()` — exact separable EDT, evaluated by brute force on the vertical pass
   (`for y0 in range(sh): d[y0] = min(d[y0], (d1 + (y0-yy)²).min(0))`), at 1/2 resolution.
   Cost is **O(H²W)** rather than O(HW). Measured: 2.5 s at 1/4, so **≈20 s at 1/2**
   (and ≈160 s if ever run at full resolution).
2. A depth ramp `deep_base + deep_range·(1 - exp(-d/deep_len))`, plus 4 fBm layers.
3. `slab_erode()` — Hatchling's method: for each of `layers` thresholds, compute a
   Chebyshev distance to the nearest above/below-threshold texel inside a window
   (implemented as `w` repeated 3×3 `MaxFilter` passes), then accumulate
   `(dn - dp)/w`. At `relief_down=2`, `erosion_window=6` tiles → window = 3 px, so
   **24 layers × (3 + 3) = 144 MaxFilter passes** (≈48 s at 0.34 s/pass), plus 40 passes
   for the residual layer (≈13 s). Around **one minute of the ~3 minute build**, and it
   is a purely local morphological filter: no water is routed, no sediment is conserved.
4. Palette LUT.

Two consequences worth stating up front:

* The "carved" tendrils people like in the current output are the *residual* of a
  morphological smoothing (`r1 - e1`), i.e. distance-to-level-set contours. They are
  scale-free marbling, not a drainage network: no tributary junctions, no downstream
  widening, no concave-up long profiles.
* Because depth is a monotone function of `d`, every medial-axis crease of the EDT
  becomes a bright line in the final image. Blur radius 3 tiles and a 46-tile warp only
  soften it.

---

## 1. Erosion algorithms for heightmaps

### 1.1 The families at a glance

| Family | Idea | Cost | Visual signature | numpy feasibility | Channels? |
|---|---|---|---|---|---|
| **Droplet / particle** | Rain drops roll downhill, carry sediment by capacity, deposit on flats | O(D·L·r²): drops × path length × erosion radius² | Narrow gullies, V-valleys, alluvial fans, deltas; spiky if radius = 1 | Poor: inherently per-drop and sequential. Fine at ≤1 M cells, not at 40 M | Yes, weakly — grooves capture later drops ([Beyer 2015](http://www.firespark.de/resources/downloads/implementation%20of%20a%20methode%20for%20hydraulic%20erosion.pdf)) |
| **Shallow water / virtual pipes** | Grid water depth + 4 flux pipes + sediment; 5–7 passes per step | O(N) per step × 100–1000 steps | Wide braided channels, floodplains, sediment plains | Good vectorisation, but 40 M × 500 steps ≈ 2·10¹⁰ cell-ops ≈ minutes–tens of minutes | Yes, best-looking |
| **Stream power / fluvial (SPL)** | `∂h/∂t = U − K·A^m·S^n`; drainage area `A` from flow routing | O(N) per implicit solve, O(N log N) ordering | Dendritic networks, sharp interfluves, concave-up profiles | Good at 2.5 M; needs a topological order | **Yes — the canonical one** |
| **Thermal / talus** | Material above the talus angle slides to the neighbour | O(N) per iteration | Talus cones, straight 30–35° slopes, dunes; no valleys | Trivial | No |
| **Diffusion** | `∂h/∂t = κ∇²h` | O(N) explicit, O(N log N) implicit/FFT | Rounded, "melted", loses all detail | Trivial | No |
| **Lattice-Boltzmann** | Mesoscopic particle distributions on a lattice | O(N·q) per step, thousands of steps (CFL) | Fluid-realistic meanders, but very expensive and fiddly | Bad | Yes, overkill |
| **Slab (Hatchling)** | Per threshold, distance to the nearest texel on the other side, accumulated | O(N·L·w) with repeated dilation (naive O(N·L·w²)) | Flattened bumps, sandy slopes, wispy veins, terracing | Excellent — pure morphology | **No** |
| **River-network graph** | Build the network first (priority flood from mouths), then carve by upstream volume | O(N log N), no time steps | Explicit dendritic networks with user control | Good with `heapq` at ≤5 M | **Yes** |
| **ML (CNN/GAN erosion)** | Learned filter | O(N) inference | Convincing but uncanny at tile scale | Needs a model + framework; violates the dependency rule | Sometimes |

### 1.2 Droplet / particle hydraulic erosion

The reference implementation is Hans Theobald Beyer's bachelor thesis
([PDF](http://www.firespark.de/resources/downloads/implementation%20of%20a%20methode%20for%20hydraulic%20erosion.pdf)),
popularised by Sebastian Lague's video. Each drop has position, direction, speed, water and
sediment; it moves one cell per step along a blend of the previous direction and the
local gradient (`p_inertia`), erodes up to its capacity and deposits the surplus.

Beyer's own measurements are the useful part: 10 000 drops on a 256×256 map take 0.48 s at
`p_radius = 1` and 9.69 s at `p_radius = 8` — i.e. **cost grows with radius²**, because every
erosion step touches `r²` cells. Extrapolating one drop per cell: 40 M drops ≈ 2 000 s
before radius cost. Even at 1/8 resolution (0.64 M cells) you are looking at ~30 s and the
detail is then upscaled 8×.

Musgrave et al. already noted the same in 1989: the hydraulic model "features the global
communication necessary to create global features, but is slow despite the O(n) time
complexity", and their 100×100 patch took ~4 hours of SGI CPU
([Musgrave, Kolb & Mace 1989](https://doi.org/10.1145/74334.74337)).

Verdict for us: not viable at 40 M, and at 2.5 M it competes badly with stream power for
the same visual result.

### 1.3 Shallow water / virtual pipes

Mei, Decaudin & Hu, *Fast Hydraulic Erosion Simulation and Visualization on GPU*
([DOI](https://doi.org/10.1109/PG.2007.15),
[PDF](http://www-evasion.imag.fr/Publications/2007/MDH07/FastErosion_PG07.pdf)) is the
canonical description: terrain height `b`, water depth `d`, suspended sediment `s`,
outflow flux `f = (fL,fR,fT,fB)`, velocity `v`; seven render passes per iteration. The
virtual-pipe model itself comes from O'Brien et al. (1995) via the paper's references.
It is the most physically convincing cheap model and produces braided, wide channels
rather than single gullies.

It is a bad fit here: 40 M cells × several hundred iterations × 7 kernels is tens of
minutes in numpy, and the picture is downsampled to 1 px/tile anyway.

### 1.4 Stream power / fluvial erosion — the one that makes networks

The stream power law is

```
∂h/∂t = U − K · A^m · S^n
```

with `A` the upstream drainage area, `S` the local slope. Braun & Willett
([Geomorphology 180–181:170–179, 2013](https://doi.org/10.1016/j.geomorph.2012.10.008))
give an **O(n), implicit, parallel** solution: order nodes by elevation, then solve each
node in terms of its already-solved receiver. Implicit means you can take geological-size
timesteps instead of tiny CFL-limited ones, which is the whole trick. Their abstract
claims meshes of 10⁸ nodes on a laptop.

Two consequences for implementation:

* You need a **topological order** (downstream-to-upstream). `np.argsort` on elevation is
  the standard cheap approximation; it costs 0.14 s at 2.5 M and ~2.5 s at 10 M.
* You need **depressions resolved** or flow dead-ends in pits. Priority-Flood
  ([Barnes, Lehman & Mulla 2014](https://doi.org/10.1016/j.cageo.2013.04.024),
  [PDF](https://richard.science/sci/2014_barnes_depressions_published.pdf)) is O(n) for
  integer data / O(n log n) for float, ~20 lines of pseudocode. Cordonnier, Bovy & Braun
  ([ESurf 7:549–562, 2019](https://esurf.copernicus.org/articles/7/549/2019/)) give an
  alternative that builds a basin graph instead of raising elevations.

The CG variant that makes this practical for art direction is Cordonnier et al. 2016
([CGF 35(2):165–175](https://doi.org/10.1111/cgf.12820)): build the *stream graph* first
from an uplift map, then convert it to a DEM by blending landform kernels. High-level
control over where the dendritic structure goes — which is exactly what we want, because
we want channels on the *sea floor*, not just anywhere.

Visual signature: branching valleys that join at acute angles, ridge lines between
catchments, channel width and depth increasing downstream, concave-up longitudinal
profiles. This is the only family that reliably reads as "drainage" at a glance.

### 1.5 Thermal / talus

Musgrave et al. 1989: if `a_v − a_u > T` (the talus angle), move a fraction `c` of the
excess to the neighbour. O(N) per pass, converges in a few dozen passes. The paper's own
description of the result is "a rough approximation of sand dunes" — it *softens* ridges
and fills valleys, the opposite of carving. Useful as a post-pass to stop stream-power
channels from having vertical walls, not as a generator.

### 1.6 Diffusion

Same family, linearised. The classic use is Musgrave's "eroded fBm", where the fractal
dimension is varied with altitude to fake erosion. It removes high frequencies and
nothing else; at our scale it mainly destroys the detail we paid for.

### 1.7 Lattice-Boltzmann

Mesoscopic, O(N·q) per step with q≈9 directions, and stability requires small steps, so
thousands of iterations. Mei et al. explicitly note LBM on GPU was limited by 3D texture
support and chose the pipe model instead. No reason to consider it.

### 1.8 Slab erosion (Hatchling) — what it is and is not

Jan Mróz's write-up ([proceduralpixels.com](https://www.proceduralpixels.com/blog/terrain-hack-fastest-erosion-algorithm-ever))
describes the accident: for a threshold `t`, find the distance to the nearest texel above
`t` and to the nearest below `t` inside a small window, then `linearstep(-maxD, maxD,
d_neg - d_pos)`; sum over many thresholds. It approximates the height field with a stack of
slab functions.

Mróz is explicit about the caveats, and they matter for us:

* "The algorithm isn't based on real-life observations and relies on a math trick."
* "Works only on a heightmap — doesn't produce other outputs, like sediment transport or
  water flow velocity."
* "The highest computation cost comes from search window traversal. For the heightmap, the
  complexity is O(n²)" (n = window size).

So: it is a **morphological operator**, not a hydrological one. It flattens bumps and
mimics soil movement down slopes. It cannot produce a catchment hierarchy, because nothing
in it knows which way is downhill globally. The veins it produces are level-set contours —
which is precisely why the current output looks marbled rather than dendritic.

That is not a reason to drop it (it is cheap and it looks good), but it *is* the reason a
stream-power or river-graph pass is the only way to get real channels.

### 1.9 River-network / graph methods — the cheap middle ground

Instead of simulating time, build the network directly:

1. Seed a priority queue with every water cell adjacent to land (the river mouths).
2. Flood inland with a Dijkstra-like expansion, always taking the lowest-cost edge; this
   yields a height field *and* a downstream pointer per cell in one pass.
3. Accumulate upstream volume, then carve each cell by a function of that volume.

That is `river_network.py` in dandrino's repo
([MIT, Python](https://github.com/dandrino/terrain-erosion-3-ways/blob/master/river_network.py)),
which uses exactly this for the height field, plus
`downcut = 1/(1 + volume^1.3)` to deepen channels with upstream area. It also uses
`tanh(dist_to_mask/80)` for the coastal drop-off — the same distance-from-land idea we use.

WorldEngine ([MIT, Python](https://github.com/Mindwerks/worldengine)) does a raster version:
find flow directions by steepest descent, trace rivers from sources, A* around pits, then
carve a V-shaped valley with weights 1.0 / 0.2 / 0.05 at radii 0 / 1 / 2
([`erosion.py`](https://raw.githubusercontent.com/Mindwerks/worldengine/master/worldengine/simulations/erosion.py)).
It is slow because it is pure Python loops, but the carving rule is directly copyable and
vectorisable.

---

## 2. Procedural bathymetry / sea-floor heightmaps

### 2.1 The standard margin profile

Every real ocean margin is a sequence of four provinces, and the *breaks* between them are
what the eye reads as "bathymetry":

| Province | Depth | Width | Gradient | Source |
|---|---|---|---|---|
| Continental shelf | 0 → ~120–200 m | 88 km (passive), 31 km (active); global mean ~75 km | 0.5–1° | [Harris et al. 2014](https://doi.org/10.1016/j.margeo.2014.01.011); [SERC](https://serc.carleton.edu/s/integrate/coastlines/557) |
| Shelf break | ~120–130 m global mean (100–200 m range) | — | sharp increase | SERC; [NOAA Ocean Explorer](https://archive.oceanexplorer.noaa.gov/explorations/04etta/background/profile/profile.html) |
| Continental slope | ~200 → 1 400–3 200 m | 20–100 km; 46 km passive / 36 km active | ~4° average, 3–20° range | [Continental Slopes 1979](https://doi.org/10.2110/pec.79.27.0001); SERC |
| Continental rise | 3 000 → 4 000 m | hundreds of km | < 1° | SERC; Harris (8.24 % of seafloor) |
| Abyssal plain | 4 000 → 6 000 m | — | < 1:1 000 | [Blue Habitats](https://bluehabitats.org/?page_id=1652) |

Two numbers worth internalising: the shelf is ~9 % of ocean area, the slope ~5 %, the
abyss ~85 % and the hadal zone (< 6 000 m) < 1 %
([Harris et al., Table 5](https://doi.org/10.1016/j.margeo.2014.01.011)). So the vast
majority of our 92 % water area is *abyss*, not shelf. A pipeline that is mostly a
shore-parallel ramp spends its dynamic range on the 5 % of the picture nearest land.

**Scale check for our map.** If one tile is `s` km, the shelf should be `88/s` tiles wide.
At `s = 1 km` that is 88 tiles; at `s = 10 km`, 9 tiles. The current config's
`pool_min=2 … pool_max=12` plus `ease 18–60` is only consistent with reality if a tile is
roughly 10–40 km. Worth pinning down, because every other constant should follow from it.

### 2.2 Abyssal roughness classes (a ready-made amplitude budget)

Harris et al. classify the abyss by relief over a 25-cell radius:

* abyssal plains — relief < 300 m (27.9 % of ocean area)
* abyssal hills — 300–1 000 m (41.3 %)
* abyssal mountains — > 1 000 m (15.9 %)

Those are directly usable as fBm amplitude bands: ~150 m for plains, ~500 m for hills,
~1 500 m for the mountainous class. Seamounts are separate features; NOAA's scale graphic
puts a typical seamount ~1 800 m above the surrounding plain
([NOAA ocean floor features](https://www.noaa.gov/education/resource-collections/ocean-coasts/ocean-floor-features)).

### 2.3 Ridges, trenches and the age–depth law

Mid-ocean ridges are not decorative bumps: their depth is a function of crustal age, and
the age is a function of distance from the spreading axis. Parsons & Sclater
([JGR 82:803–827, 1977](https://doi.org/10.1029/JB082i005p00803),
[PDF](https://topex.ucsd.edu/geodynamics/parsons_sclater77.pdf)) give

```
d(t) = 2500 + 350·√t          metres, 0 < t < 70 Ma
d(t) = 6400 − 3200·exp(−t/62.8)   metres, t > 20 Ma
```

with `t` in millions of years. So: ridge crest ≈ 2 500 m, abyssal asymptote ≈ 6 400 m,
and the flank profile is a square root, not a linear ramp. If you place a ridge axis and
let `t = distance / spreading_rate`, you get the correct convex-up flank shape for free.

Trenches are the other extreme: hadal (> 6 000 m) is 0.95 % of the ocean, and the deepest
points are ~11 km
([NOAA](https://www.noaa.gov/education/resource-collections/ocean-coasts/ocean-floor-features)).
In an archipelago map the honest placement rule is "narrow elongated minimum on the outer,
ocean-facing side of an island arc", not "random deep spot".

### 2.4 How real bathymetry is assembled (and what to steal)

* **GEBCO** — 15 arc-second global grid, public domain, with a Type Identifier (TID) grid
  saying which cells are measured multibeam vs interpolated vs predicted
  ([gebco.net](https://www.gebco.net/data_and_products/gridded_bathymetry_data/)).
  The TID idea is worth copying conceptually: keep a "confidence" layer, and let
  interpolation/prediction fill the rest.
* **SRTM15+** — Tozer et al. 2019
  ([project page](https://topex.ucsd.edu/WWW_html/srtm15_plus.html),
  [PDF](http://topex.ucsd.edu/sandwell/publications/180_Tozer_SRTM15+.pdf)) merges ship
  soundings with **gravity-derived** depths: satellite altimetry measures the geoid, and
  the gravity anomaly is inverted for sea-floor topography where no ship has been. That is
  why GEBCO/SRTM15+ show plausible *fabric* (abyssal hills, fracture zones) in un-surveyed
  areas.
* **ETOPO 2022** — NOAA's 15/30/60 arc-second relief model, documented with a user guide
  ([NCEI](https://www.ncei.noaa.gov/products/etopo-global-relief-model)).

The lesson for procedural work is the *structure* of these grids, not the data: a smooth
low-frequency depth surface + a band-limited roughness field + explicit feature primitives
(ridges, fracture zones, seamounts), rather than one noise function.

### 2.5 Procedural approaches in the wild

* **Distance-from-coast + tanh/exp ramp** — what we do, and what dandrino's
  `coastal_dropoff = tanh(dist_to_mask/80)*land_mask` does. Cheap, and correct for the
  shelf; wrong for the abyss (see §3).
* **Plate/tectonic simulation** — WorldEngine assigns plates, moves them, and derives
  boundary uplift; then rivers and erosion. Heavy, but the only way to get physically
  consistent ridges and trenches.
* **Uplift map + stream graph** — Cordonnier et al. 2016. Most control per unit effort.
* **Noise + masks** — ridged noise masked by a continent-shaped falloff, the standard
  "world map" recipe in most Unity/Shadertoy tutorials and fantasy map generators.
  Produces plausible *land*; for sea floor it tends to look like clouds unless a channel
  pass is added.

---

## 3. Distance-transform artefacts, and how to avoid them

### 3.1 Why the creases exist

For a set of seeds `S`, `d(x) = min_{s∈S} |x−s|` is continuous but its gradient is
discontinuous wherever two or more seeds are equidistant. The locus of those ties is the
**medial axis**; in a strait between two islands it is the midline, and in a circular basin
it is a point. Because our depth is a monotone function of `d`, every such locus is a
bright ridge — the "straight line / halo" artefact. This is a property of the transform
itself, not of our implementation
([medial axis](https://en.wikipedia.org/wiki/Medial_axis);
[Felzenszwalb & Huttenlocher 2012](https://www.cs.brown.edu/~pff/dt/)).

Note the artefact's magnitude: a crease is a *gradient* discontinuity, so it is invisible
in flat-shaded tone but obvious once you add a hillshade or any tone mapping with a
threshold — which is why the current file has `shading=False` by default.

### 3.2 Mitigations, cheapest first

1. **Blur.** Current approach (`dist_smooth_tiles=3`, 2 passes). It widens the crease and
   rounds the "V" at the coast, so shallow water bleeds onto land unless you mask it. It
   does not remove the kink, it band-limits it.
2. **Domain warp.** Quilez's standard recipe,
   `f(p + 4·fbm(p + 4·fbm(p)))`
   ([iquilezles.org/articles/warp](https://iquilezles.org/articles/warp/)). Breaks the
   straightness into meanders. At 46 tiles of warp on a 4 000-tile basin it is a small
   perturbation; useful, not sufficient.
3. **Coarse EDT + upscale.** Sampling the EDT at 1/4 and bicubic-upscaling turns a sharp
   crease into a ~4-tile-wide soft ridge, and costs 1/64 of the full-resolution brute
   force. This is probably the best effort/result ratio for the artefact specifically.
4. **Perturb the coastline before the EDT.** Build `land' = land ⊕ noise` (morphological
   open/close with a low-frequency threshold), compute `d' = EDT(land')`, then restore
   `d'(land) = 0`. Contours near the coast stop being parallel offset curves. Cheap (~1 s)
   and attacks the cause rather than the symptom.
5. **Poisson ("screened") distance instead of Euclidean distance.** Solve

   ```
   ∇²u = −1  in water,   u = 0 on land,   ∂u/∂n = 0 at the map border
   ```

   In a straight channel of width `L` this gives `u = y(L−y)/2`, a smooth parabola — **no
   crease at the midline at all**, unlike `min(y, L−y)`. The level sets are smooth
   everywhere and the field is C¹ across the medial axis. Cost: a multigrid V-cycle on a
   1/4 grid is ~40 fine-grid sweeps; measured 100 Jacobi sweeps at 2.5 M = 0.93 s, so a
   proper V-cycle is ~1 s. Then upscale. This is the principled fix.
   (The heat method, Crane et al.,
   [project page](https://www.cs.cmu.edu/~kmcrane/Projects/HeatMethod/), is the same idea
   for geodesic distance; overkill here.)
6. **Stop using distance where it is meaningless.** Beyond the continental rise, real
   abyssal depth has nothing to do with distance from land. Cross-fading to a large-scale
   basin field from ~200–400 tiles out removes the crease by construction *and* is more
   correct. This is the recommendation in §7.2.

### 3.3 Alternatives to "distance from land" for offshore-ness

| Method | Smooth? | Cost at 40 M | Notes |
|---|---|---|---|
| Exact EDT | No (creases) | ~20 s at 1/2 (measured) | What we have |
| Blurred land mask (heat kernel) | Yes | ~0.4 s | Saturates in open water; only usable for the shelf |
| Poisson / Laplace `u` | Yes (C¹) | ~1 s at 1/4 + upscale | Best smooth "offshore-ness" |
| Coarse EDT + refine | Mostly | ~0.3 s at 1/8 | Fastest acceptable |
| Jump flooding | Approx, few-pixel errors | ~1–3 s at 1/2 (measured primitive 42 ms/pass) | Fully vectorised |
| Crustal-age / tectonic field | Yes | ~1 s | Physically the right variable for the abyss |

---

## 4. Noise for terrain

### 4.1 The primitives and what they actually look like

| Primitive | Formula / idea | Signature |
|---|---|---|
| **fBm** | `Σ gain^i · noise(lacunarity^i · x)` | Clouds / rolling hills; self-similar, no structure |
| **Ridged multifractal** | `Σ (1 − |noise|)² · weight_i`, weights fed back from previous octaves | Sharp crests, branching ridges, "craggy mountains or marble" ([libnoise RidgedMulti](http://libnoise.sourceforge.net/docs/classnoise_1_1module_1_1RidgedMulti.html)) |
| **Billow** | `|noise|` | Puffy, cumulus-like |
| **Domain warp** | `f(p + a·fbm(p + b·fbm(p)))` | Swirls, marbling, fluid distortion ([Quilez](https://iquilezles.org/articles/warp/)) |
| **Curl noise** | `v = ∇×ψ`, `ψ` = noise | Divergence-free flow; meanders and streamlines ([Bridson et al. 2007](https://doi.org/10.1145/1275808.1276435)) |
| **Worley / cellular** | F1/F2 distances to jittered points | Plates, cracks, fracture zones ([Worley 1996](https://doi.org/10.1145/237170.237267)) |

Perlin's original paper ([1985](https://doi.org/10.1145/325165.325247)) and "Improving
noise" ([2002](https://doi.org/10.1145/566654.566636)) are the canonical references;
FastNoiseLite ([MIT](https://github.com/Auburn/FastNoiseLite)) is a compact modern
implementation with `FBm`, `Ridged`, `PingPong` and both progressive and independent
domain warp, useful as a recipe reference.

### 4.2 Which combinations make dendritic patterns

This is the part most tutorials get wrong.

* **Summing octaves never makes channels.** fBm is stationary and isotropic; its level sets
  are blobs at every scale.
* **Ridged multifractal makes *ridges*, not valleys.** Invert it and you get branching
  valleys that look like a negative mountain range — closer, but the branches are
  symmetric and do not join at the acute angles real tributaries do.
* **Domain warping makes marbling.** It is the right tool for distorting an existing
  channel network (and for hiding the EDT crease), not for creating one.
* **Curl noise makes meanders.** Advecting a scalar or a mask along a curl-noise field
  gives sinuous, non-crossing channels — good for submarine canyons and contour currents.
* **Actual dendritic drainage requires routing.** Either stream power / river graphs
  (§1.4, §1.9) or a droplet sim with enough drops that grooves capture later drops.

So the correct division of labour is: **noise supplies the substrate and the roughness;
routing supplies the network.** If you want the network to follow a chosen direction, warp
the *input* height field with low-frequency noise before routing, not after.

### 4.3 Parameter recipes

| Target | Recipe |
|---|---|
| Rolling abyssal hills | fBm, 5–6 octaves, gain 0.5, lacunarity 2.0, amplitude 150–500 m, anisotropic scale ~3:1 along the ridge direction |
| Fracture zones | ridged noise, 2–3 octaves, stretched 8–15:1, amplitude 500–1 000 m, thresholded to a scarp |
| Seamount field | Worley F1 or Poisson-disk points, `exp(−(r/σ)²)` cones, σ 5–20 km, heights heavy-tailed 500–3 000 m |
| Submarine canyon | curl-noise-advected ridged noise, or a stream-power channel with base level at the shelf break |
| "Eroded" look on the shelf | ridged multifractal + one thermal pass (talus 30–35°), which is exactly Musgrave's recipe |
| Marbled background (current look) | fBm + domain warp + slab erosion residual |

Standard defaults, if you want one: `gain = 0.5`, `lacunarity = 2.0`, 4–6 octaves. libnoise
recommends lacunarity 1.5–3.5 for ridged noise specifically.

---

## 5. Existing code worth reading

| Project | Lang / licence | Algorithm | What to take |
|---|---|---|---|
| [WorldEngine](https://github.com/Mindwerks/worldengine) | Python, **MIT** | Plate sim → elevation → rivers by steepest descent + A* around pits → V-carve with 1/0.2/0.05 weights ([`erosion.py`](https://raw.githubusercontent.com/Mindwerks/worldengine/master/worldengine/simulations/erosion.py)) | The carving weights and the "river flows to sea, else lake" logic |
| [dandrino/terrain-erosion-3-ways](https://github.com/dandrino/terrain-erosion-3-ways) | Python, **MIT** | Three methods side by side: pipe/droplet simulation, Delaunay river-network with volume downcutting, and an ML model ([`river_network.py`](https://github.com/dandrino/terrain-erosion-3-ways/blob/master/river_network.py)) | The priority-queue flood + `1/(1+volume^1.3)` downcutting; the cleanest cheap network generator |
| [Landlab](https://github.com/landlab/landlab) | Python, **MIT** | Component library: `FlowAccumulator`, `FastscapeEroder`, `PriorityFloodFlowRouter`; pip-installable | Reference implementation of Braun–Willett at map scale; useful even if we copy rather than depend ([FastscapeEroder source](https://landlab.csdms.io/_modules/landlab/components/stream_power/fastscape_stream_power.html)) |
| [fastscapelib](https://fastscapelib.readthedocs.io/en/latest/api_cpp/eroder.html) | C++ / Python | O(n) implicit SPL, hillslope diffusion, marine deposition | The algorithm description and the marine-transport extension |
| [Azgaar Fantasy Map Generator](https://github.com/Azgaar/Fantasy-Map-Generator) | TypeScript, **MIT** | Heightmap + river network + hillshade rendering for a zoomable map | Map-scale conventions and how to render relief behind tiles |
| [libnoise](http://libnoise.sourceforge.net/) | C++, LGPL | Perlin, `RidgedMulti`, `Billow`, `Voronoi`, module graph | Exact ridged-multifractal formulation (credited to Musgrave's `fractal.c`) |
| [FastNoiseLite](https://github.com/Auburn/FastNoiseLite) | many, **MIT** | Perlin/OpenSimplex/Cellular + fractal and domain-warp modes | Compact reference for warp + ridged combinations |
| [GEBCO](https://www.gebco.net/data_and_products/gridded_bathymetry_data/) / [SRTM15+](https://topex.ucsd.edu/WWW_html/srtm15_plus.html) / [ETOPO](https://www.ncei.noaa.gov/products/etopo-global-relief-model) | data + tooling | Gridded global bathymetry, public domain | Ground truth for amplitude/roughness statistics; TID-style confidence layers |
| Houdini `HeightField Erode`, Gaea, World Machine | proprietary | Hydraulic/thermal solvers with art-direction masks | Parameter ranges (talus angle, capacity, deposition) if you can read their docs |

Not verified and therefore not relied on here: `terrain-erosion-rs` (the URL 404'd at the
time of writing), Wilbur (closed-source freeware).

---

## 6. Distance fields at scale

### 6.1 Measured costs on this machine

| Operation | Resolution | Time |
|---|---|---|
| `PIL ImageFilter.MaxFilter(3)` | 10.2 M px | **337 ms** |
| 8-neighbour min-combine (jump-flood inner step) | 10.2 M px | **42 ms** |
| `sea_core.box_blur` r=3, 2 passes × 2 axes | 10.2 M px | **219 ms** |
| Brute-force vertical EDT pass | 2.5 M px (1/4) | **2.47 s** → ~20 s at 1/2, ~160 s at full |
| 100 Jacobi Poisson iterations | 2.5 M px (1/4) | **0.93 s** |
| `np.argsort` float32 | 2.5 M px | **137 ms** |

Two conclusions:

* The existing EDT is **~64× more expensive than it needs to be**, because the vertical pass
  is O(H²W). Running it at 1/4 and upscaling costs 1/64 (~0.3–2.5 s) and the error is
  invisible at 1 px/tile.
* The 8-neighbour min-combine is **8× cheaper than a 3×3 MaxFilter**, which is the single
  biggest available win in `slab_erode` ((144 + 40) passes × 0.34 s ≈ 61 s).

### 6.2 Exact separable EDT

Felzenszwalb & Huttenlocher, *Distance Transforms of Sampled Functions*, Theory of
Computing 8(19), 2012 ([paper + 100-line C++](https://www.cs.brown.edu/~pff/dt/)):
the squared EDT of a sampled function is computed exactly in O(n) per dimension by taking
the lower envelope of parabolas. It is the right algorithm if you write your own; the
catch is that the 1D pass is a sequential scan, so a numpy port needs one Python loop over
rows (~2 600 iterations) — acceptable, but the vectorised alternatives are usually faster
in practice at this size.

Equivalent linear-time algorithms: Meijster et al. 2000 and Maurer et al. 2003 (both
O(N) in any dimension). `scipy.ndimage.distance_transform_edt` is an exact C
implementation ([docs](https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.distance_transform_edt.html))
and would solve this in ~1–2 s at 40 M — worth revisiting the "no scipy" rule, since it is
pip-installable and adds no compiled build step for the user.

### 6.3 Jump flooding (the numpy-friendly option)

Rong & Tan, *Jump Flooding in GPU with Applications to Voronoi Diagram and Distance
Transform*, I3D 2006 ([DOI](https://doi.org/10.1145/1111411.1111431),
[PDF](https://www.comp.nus.edu.sg/~tants/jfa/i3d06.pdf)). Propagate nearest-seed
coordinates with step sizes `n/2, n/4, …, 1`; that is `log₂ n` passes of a 3×3 neighbour
min-combine, each fully vectorisable. The paper reports errors are "hardly noticeable",
localised to a few Voronoi vertices, and that `JFA+1`/`JFA+2` (one or two extra
unit-step rounds at the end) reduce them to "just a few grid points". At 10.2 M px a bare
min-combine is 42 ms, so 12–14 rounds with coordinate payload is ~1–3 s.

This is the best fit for our constraints: no dependency, no Python loops, and the residual
errors are sub-pixel in the final 1 px/tile picture.

### 6.4 Coarse-then-refine

Because the display is nearest-neighbour at 1 px/tile, a distance error of 1–2 tiles is
invisible. So: compute the EDT at 1/4 or 1/8 (exact, cheap), bicubic-upscale, then do a
single full-resolution correction pass within ~16 tiles of the coast, where accuracy
actually matters for the shore pool. This keeps the beach exact and the abyss smooth.

---

## 7. Applicable to our pipeline

Six concrete changes, in the order I would do them. Costs are for 40.7 M cells unless
stated; all are estimates from the measurements in §6.1.

### 7.1 Piecewise margin profile instead of one exponential

Replace

```python
deep = deep_base + deep_range * (1 - exp(-dd / deep_len))
```

with a monotone LUT over warped distance, e.g. control points

```
d = 0      -> 0.000   (beach)
d = 12     -> 0.020   (shelf, gentle)
d = 40     -> 0.055   (shelf break — the visible edge)
d = 130    -> 0.300   (slope, steep)
d = 320    -> 0.480   (rise, flattening)
d = 900    -> 0.600   (abyssal)
```

with the break distances modulated ±40 % by low-frequency noise so the break is not a
perfect offset curve. Implement as `np.interp` on `dd` (or a 1 024-entry LUT built once).

* Cost: negligible (~0.2 s, dominated by the existing warp lookup).
* Effect: the shelf break is the strongest single cue in a real bathymetric image. It also
  restores dynamic range: currently the ramp spends most of its range on the first few
  hundred tiles and then saturates, which is why the open water reads as flat.

### 7.2 Cross-fade to a "crustal" field beyond the slope

```
w   = smoothstep((d - 250) / 350)
t   = mix(margin_profile(d), basin_field(x, y), w)
```

where `basin_field` is 2–3 octaves of low-frequency fBm (scales 800–3 000 tiles) plus an
optional ridged term for ridge axes, mapped to `[0.45, 0.75]`.

* Cost: ~1–2 s (two or three fBm at 40 M, plus the lerp).
* Effect: (a) the medial-axis crease disappears because `d` no longer controls the abyss;
  (b) you get "shallow bank vs deep basin" — a 300-tile-wide bank can be shallower than a
  100-tile-wide channel, which distance alone can never express; (c) large-scale colour
  variation at low zoom, where the map is actually viewed.

This is the change I would make first if only one were allowed: it fixes the artefact and
the composition at the same time.

### 7.3 Perturb the coastline before the EDT

At 1/4 resolution: `land_p = open/close(land, noise > τ)` for one or two noise octaves at
8–40 tile wavelength, then `d = EDT(land_p)`, then `d[land] = 0`.

* Cost: ~1 s at 1/4 (two morphological passes + the EDT).
* Effect: near-shore depth contours stop being parallel offsets of the coastline; the
  shore pool and the rim get natural width variation without any extra noise layer.
* Risk: straits narrower than the perturbation can close. Keep `τ` small and only apply
  beyond ~3 tiles from the true coast.

### 7.4 A real drainage network at 1/4 resolution

At `relief_down=4` (1 937 × 1 314 = 2.5 M cells):

1. D8 receiver: `argmin` over 8 shifted arrays (8 × 2.5 M comparisons ≈ 10 ms).
2. Depression handling: Priority-Flood, or simply add a tiny monotone bias toward the
   nearest ocean and skip it (channels then run to the shelf break, which is what we want).
3. Flow accumulation by **pointer doubling** instead of a topological loop. One round is
   `acc += np.bincount(recv, weights=acc, minlength=n)` followed by `recv = recv[recv]`
   (with outlets pointing at themselves); ~log₂ n = 22 rounds, each a vectorised
   `bincount`, no Python loop over cells, ~0.3 s total. `bincount` is the right primitive
   here — plain `acc[recv] += acc` is wrong because many cells share a receiver, and the
   `+=` keeps each cell's own contribution.
4. 20–40 implicit stream-power iterations in Braun–Willett order
   (`E = K·A^m·S`, solve each cell in terms of its already-solved receiver), or the much
   cheaper per-cell `downcut = 1/(1 + A^1.3)` rule from dandrino, which needs no ordering.
5. Keep only the *change* field `Δh`, upsample it, and add it to the relief.

* Cost: ~2–5 s at 2.5 M.
* Effect: tributary junctions, channels widening downstream, interfluves — the thing slab
  erosion cannot produce. Because it is added as a delta on top of the existing relief, it
  is easy to A/B and to dial to zero.

### 7.5 Make slab erosion cheap enough to keep

Two options, both local to `window_dist`:

* Replace the `w` repeated `MaxFilter` passes with a **jump-flood distance transform** of
  the threshold mask (42 ms/pass primitive vs 337 ms): an approximately Euclidean distance
  in `log₂ w` passes instead of a Chebyshev one in `w`. At `w=3` the win is small; at
  `w=8–16` it is 5–8×.
* Or compute the whole slab stack at 1/3 resolution instead of 1/2 (9× fewer cells for the
  same visual result after upscaling).

* Cost: saves ~40–70 s of the current build.
* Effect: identical picture; funds §7.4.

### 7.6 Bathymetric features by rule

Add, in this order, only where `d > shelf_break`:

1. **Abyssal hills** — anisotropic fBm (3:1 along a global "spreading" direction),
   amplitude 150–500 m equivalent, 2–3 octaves. ~0.5 s.
2. **Fracture zones** — ridged noise stretched 10:1, thresholded to a narrow scarp,
   amplitude ~0.02 in depth units. ~0.5 s.
3. **Seamounts** — threshold a low-frequency noise to get sparse points (or use a jittered
   grid), place Gaussian cones σ = 4–15 tiles, heights 0.03–0.10, and bias placement away
   from the shelf. ~0.5 s.
4. **One or two ridge axes** — a hand-placed polyline, depth from Parsons–Sclater
   `2500 + 350√t`, with `t ∝ distance/spreading_rate`, plus an axial valley (a narrow
   negative ridge). ~0.3 s.

* Effect: fixes "empty flat blue" at low zoom; gives the eye something to navigate by; and
  the ridge axis in particular makes the map read as an ocean rather than a height map with
  blue paint.

### 7.7 What not to do

* Full pipe-model or LBM simulation at 40 M: minutes to tens of minutes for a picture that
  is one pixel per tile.
* Droplet erosion at 40 M: ~2 000 s by Beyer's own scaling.
* Any ML erosion model: adds a framework dependency and produces features at the wrong
  scale for a 1 px/tile texture.

---

## References

**Erosion**

* Musgrave, Kolb & Mace, *The synthesis and rendering of eroded fractal terrains*, SIGGRAPH 1989 — <https://doi.org/10.1145/74334.74337>
* Beyer, *Implementation of a method for hydraulic erosion*, TU München 2015 — <http://www.firespark.de/resources/downloads/implementation%20of%20a%20methode%20for%20hydraulic%20erosion.pdf>
* Mei, Decaudin & Hu, *Fast Hydraulic Erosion Simulation and Visualization on GPU*, PG 2007 — <https://doi.org/10.1109/PG.2007.15>
* Braun & Willett, *A very efficient O(n), implicit and parallel method to solve the stream power equation*, Geomorphology 2013 — <https://doi.org/10.1016/j.geomorph.2012.10.008>
* Cordonnier et al., *Large scale terrain generation from tectonic uplift and fluvial erosion*, CGF 2016 — <https://doi.org/10.1111/cgf.12820>
* Cordonnier, Bovy & Braun, *A versatile, linear complexity algorithm for flow routing in topographies with depressions*, ESurf 2019 — <https://esurf.copernicus.org/articles/7/549/2019/>
* Barnes, Lehman & Mulla, *Priority-Flood*, Computers & Geosciences 2014 — <https://doi.org/10.1016/j.cageo.2013.04.024>
* Mróz, *Terrain hack: fastest erosion algorithm ever* (Hatchling's slab method) — <https://www.proceduralpixels.com/blog/terrain-hack-fastest-erosion-algorithm-ever>
* FastScapeLib eroder docs — <https://fastscapelib.readthedocs.io/en/latest/api_cpp/eroder.html>
* Landlab `FastscapeEroder` source — <https://landlab.csdms.io/_modules/landlab/components/stream_power/fastscape_stream_power.html>

**Bathymetry**

* Harris, Macmillan-Lawler, Rupp & Baker, *Geomorphology of the oceans*, Marine Geology 352:4–24, 2014 — <https://doi.org/10.1016/j.margeo.2014.01.011>
* Parsons & Sclater, *An analysis of the variation of ocean floor bathymetry and heat flow with age*, JGR 82:803–827, 1977 — <https://doi.org/10.1029/JB082i005p00803>
* *Continental Slopes*, SEPM Special Publication 27, 1979 — <https://doi.org/10.2110/pec.79.27.0001>
* Blue Habitats, *Abyssal plains* — <https://bluehabitats.org/?page_id=1652>
* NOAA, *Ocean floor features* — <https://www.noaa.gov/education/resource-collections/ocean-coasts/ocean-floor-features>
* NOAA Ocean Explorer, *The continental margin* — <https://archive.oceanexplorer.noaa.gov/explorations/04etta/background/profile/profile.html>
* SERC, *Coastal zones: the margins of continents* — <https://serc.carleton.edu/s/integrate/coastlines/557>
* GEBCO gridded bathymetry — <https://www.gebco.net/data_and_products/gridded_bathymetry_data/>
* Tozer et al., *SRTM15+*, Earth and Space Science 2019 — <https://topex.ucsd.edu/WWW_html/srtm15_plus.html>
* NOAA NCEI, *ETOPO 2022 Global Relief Model* — <https://www.ncei.noaa.gov/products/etopo-global-relief-model>

**Distance fields**

* Felzenszwalb & Huttenlocher, *Distance Transforms of Sampled Functions*, Theory of Computing 8(19), 2012 — <https://www.cs.brown.edu/~pff/dt/>
* Rong & Tan, *Jump flooding in GPU with applications to Voronoi diagram and distance transform*, I3D 2006 — <https://doi.org/10.1145/1111411.1111431>
* SciPy `distance_transform_edt` — <https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.distance_transform_edt.html>
* Crane, Weischedel & Wardetzky, *Geodesics in Heat* — <https://www.cs.cmu.edu/~kmcrane/Projects/HeatMethod/>
* Medial axis (definition) — <https://en.wikipedia.org/wiki/Medial_axis>

**Noise**

* Perlin, *An image synthesizer*, SIGGRAPH 1985 — <https://doi.org/10.1145/325165.325247>
* Perlin, *Improving noise*, SIGGRAPH 2002 — <https://doi.org/10.1145/566654.566636>
* Quilez, *Warp, noise, and fbm* — <https://iquilezles.org/articles/warp/>
* Quilez, *fBm* — <https://iquilezles.org/articles/fbm/>
* Bridson, Hourihan & Nordenstam, *Curl-noise for procedural fluid flow*, SIGGRAPH 2007 — <https://doi.org/10.1145/1275808.1276435>
* Worley, *A cellular texture basis function*, SIGGRAPH 1996 — <https://doi.org/10.1145/237170.237267>
* libnoise `RidgedMulti` — <http://libnoise.sourceforge.net/docs/classnoise_1_1module_1_1RidgedMulti.html>

**Code**

* WorldEngine (MIT) — <https://github.com/Mindwerks/worldengine>, [`erosion.py`](https://raw.githubusercontent.com/Mindwerks/worldengine/master/worldengine/simulations/erosion.py)
* dandrino/terrain-erosion-3-ways (MIT) — <https://github.com/dandrino/terrain-erosion-3-ways>
* Landlab (MIT) — <https://github.com/landlab/landlab>
* Azgaar Fantasy Map Generator (MIT) — <https://github.com/Azgaar/Fantasy-Map-Generator>
* FastNoiseLite (MIT) — <https://github.com/Auburn/FastNoiseLite>
