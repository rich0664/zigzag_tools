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

It loads `dist/zigzag-sea.webp` (2.2 MB, 7749 × 5254 — one pixel per map tile).

## Use

Open the exploration map. To toggle it from the console:

```js
window.__zzSea.on = false   // off
window.__zzSea.on = true    // on
```

## Texture generator

`tools/build_sea.py` rebuilds `dist/zigzag-sea.webp` (7749 × 5254, one pixel per
map tile). The pipeline itself lives in `tools/sea_core.py`, shared with the crop
renderer so the two can never drift. Needs numpy + Pillow:

```bash
python3 tools/build_sea.py     # ~3 min, writes dist/zigzag-sea.webp
python3 tools/sea_crop.py      # ~20 s, 1550 x 1050 crop in out/sea/v2_crop.png
```

The first run downloads the map raster into `out/sea/` and caches the distance
field there; later runs reuse them. Every knob is inline with comments — the
ones worth touching first:

| knob | where | effect |
|---|---|---|
| `L` | pool length | how far the bright shallow terrace reaches (2–22 tiles) |
| `F` | ease-off | how long it takes to blend into the basin |
| deep curve | `0.18 + 0.62 * (1 - exp(-dd / 260))` | overall depth range |
| `slab_erode(...)` | erosion | window + layer count; more layers = smoother dunes |
| `carved` weight | `relief += 1.35 * ...` | the flow/tendril structure |
| relief into `t` | `0.26 * relief` | how much the seabed drives colour vs shading |
| palette `cols` | bottom | colour ramp |
| coast pass | `rgb + (95,155,195) * 0.13 * rim` | shoreline strength |
