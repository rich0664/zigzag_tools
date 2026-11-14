# zigzag filler

Tampermonkey userscript that fills the **zig-zag.fm** "Add a release" form
from a **Discogs release link** + a **YouTube playlist link**.
It fills fields only — it never submits anything.

## Install

1. Install [Tampermonkey](https://www.tampermonkey.net/) in your browser.
2. Open
   [`dist/zigzag-fill.user.js`](https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist/zigzag-fill.user.js)
   — Tampermonkey will offer to install it (with auto-updates).

## Use

Open **Add a release** on zig-zag.fm, paste both links into the panel,
**Fetch & preview**, review the track matches, then **Fill form**.

---

# zig~zag sea depth

A second, unrelated userscript: it paints the empty space between the map's
archipelagos as a **bathymetric sea**. Depth is distance from the nearest land
(shelf → slope → abyssal plain), with regional variation, ridge/fracture
noise, and a faint north-west hillshade. It draws behind deck.gl's transparent
canvas and follows pan and zoom, one texel per map tile.

## Install

1. Install [Tampermonkey](https://www.tampermonkey.net/).
2. Open
   [`dist/zigzag-sea.user.js`](https://raw.githubusercontent.com/rich0664/zigzag_tools/master/dist/zigzag-sea.user.js)
   — Tampermonkey will offer to install it.

## Use

Open the exploration map. A **≈** button appears directly under the header's
notifications button; click it for the sea settings panel (outside click or
`Escape` closes it). To toggle it from the console:

```js
window.__zzSea.on = false   // off
window.__zzSea.on = true    // on
```

The panel holds, in order: sea on/off; quality; opacity (0–100); brightness
(50–150%); and a **30 fps limit** toggle. Every choice is remembered in
`localStorage`.

### Quality modes

All animated modes run the same shader; a lower mode is the same picture with
work removed.

| mode | what it removes |
|---|---|
| `baseline` | nothing — LOD flow, two open-water noise layers, parallax/POM, depth-biased warp |
| `medium` | parallax/POM (`par 0`, `pom 1`) |
| `low` | also the warp, the analytic bend and the second noise layer: coarse 16-tile gyre only |
| `extra low` | same as `low`, rendered at half the backing-store resolution |
| `static` | the shader entirely: the colour map blitted by the 2D path, one draw per camera change |

`static` is the fallback when WebGL2 is missing or the GPU's texture limit is
below 8192 px. With nothing saved the mode is `baseline` on a desktop browser
and `static` on a coarse-pointer (phone/tablet) or narrow viewport; a saved
mode always wins. Phones are not a performance target for this version. The
animated textures are loaded lazily on the first animated switch; `static`
never fetches them.

The **30 fps limit** caps only the animated modes (it is off by default and
does nothing on `static`). Opacity fades the whole overlay; brightness
multiplies the colour — in the shader when animated, as CSS
`filter: brightness()` on `static`.

### Assets

It loads the colour map plus, for animated modes, four baked shader textures
(total 12.75 MB):

| file | size (px) | size |
|---|---|---:|
| `dist/zigzag-sea.webp` | 7749 × 5254 | 2.03 MB |
| `dist/zigzag-sea-dist.webp` | 7749 × 5254 | 3.20 MB |
| `dist/zigzag-sea-aux.webp` | 3875 × 2627 | 6.77 MB |
| `dist/zigzag-sea-flow.webp` | 484 × 328 | 0.19 MB |
| `dist/zigzag-sea-speed.webp` | 1937 × 1313 | 0.55 MB |

`dist.webp` is √(distance / 600), `aux.webp` is depth + the along-coast
vector, `flow.webp` the 16-tile gyre field and `speed.webp` the baked speed
field. All four are lossless, full-map and versioned with the userscript.

## Texture generator

The shipped colour texture is built from a **saved editor preset**, over the
whole map at one pixel per tile (7749 × 5254), in a single pass:

```bash
python3 tools/build_dist.py                    # once: exact distance field, ~35 s
python3 tools/sea_full.py <preset> --single    # ~80 s, writes out/sea/full_<preset>.png
python3 -c "from PIL import Image; \
  Image.open('out/sea/full_<preset>.png').convert('RGB').save('dist/zigzag-sea.webp', quality=92, method=6)"

# once, for the shader textures: dump the preset's final height field
python3 tools/sea_full.py <preset> --dump-height   # writes out/sea/height_full.npy

# the four animated-mode textures
python3 tools/bake_shader_assets.py --measure  # along max/p99; writes nothing
python3 tools/bake_shader_assets.py            # dist, aux, flow, speed
python3 tools/bake_shader_assets.py --check    # validate what is on disk
```

`--strips` still exists but bands at the seams: every filter is anchored to its
crop, so a strip sees different noise, erosion and drainage than its neighbour.
Use `--single`.

`bake_shader_assets.py` reads the exact distance field, currents, the height
dump and the map raster from `out/sea/`, and the shared values (`distMax`,
`alongScale`, `heightLo/Hi`, `auxScale`, cell sizes) from
`src/sea-consts.json`, so the baker and the shader cannot disagree. `--measure`
prints the full-map along-coast maximum for freezing `alongScale`; `--check`
recomputes a thousand water tiles against the files on disk.

`tools/build_sea.py` and `tools/sea_crop.py` are the older standalone pipeline
(`sea_core.render_simple`) kept for quick look tests; they are **not** how the
shipped texture is made.

The first run downloads the map raster into `out/sea/` and caches the distance
field there; later runs reuse them. Every value that affects the picture is in
the `CFG` block at the top of `tools/sea_core.py`, grouped as: distance field,
shore pool, open water, relief/erosion, shading, colour. `CFG["shading"] =
False` renders it flat top-down (relief shows only as tone); `True` adds the
NW hillshade. The shader's own baseline uniforms and curves live in
`src/sea-consts.json`, not in `CFG`.
