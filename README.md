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

