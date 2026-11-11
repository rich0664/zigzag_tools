"""Shared sea-bathymetry pipeline.

    tools/build_sea.py   -> full map  -> dist/zigzag-sea.webp
    tools/sea_crop.py    -> one crop  -> out/sea/v2_crop.png

Every value that affects the picture lives in CFG below. Distances are in map
tiles, noise scales in tiles; the relief is computed at `relief_down` tiles per
pixel so a crop matches the same area of the full map.

Requires numpy + Pillow.
"""
import numpy as np
from PIL import Image, ImageFilter

W_MAP, H_MAP = 7749, 5254

CFG = dict(
    # ---- distance field -------------------------------------------------
    edt_down=2,              # distance field resolution (tiles per pixel)
    dist_smooth_tiles=3,     # blur applied to the EDT (kills medial-axis kinks)
    dist_smooth_passes=2,
    dist_warp_tiles=46.0,    # domain warp on the distance field
    dist_warp_scale=700,     # ... feature size in tiles

    # ---- shore pool -----------------------------------------------------
    pool_min=2.0,            # bright terrace length, tiles
    pool_max=12.0,
    pool_noise_scale=130,    # how fast the length changes along a coast
    pool_noise2_scale=420,
    ease_min=18.0,           # ease-off length into the basin, tiles
    ease_max=60.0,
    ease_noise_scale=700,
    shore_base=0.015,        # depth value right at the beach
    shore_lift=0.02,         # how much it darkens across the pool

    # ---- open water -----------------------------------------------------
    deep_base=0.18,          # depth at the pool edge
    deep_range=0.62,         # extra depth gained with distance
    deep_len=260.0,          # tiles over which that is gained
    deep_noise=[             # (amplitude, scale in tiles, octaves, seed)
        (0.20, 2600, 3, 51),
        (0.06, 900, 3, 23),
        (0.012, 300, 4, 24),
        (0.006, 120, 4, 26),
    ],
    blur_weight=0.80,        # how much the base depth is blurred in the open
    blur_start=1.5,          # tiles from land where that blur starts
    blur_ramp=12.0,          # tiles to reach full blur

    # ---- relief ---------------------------------------------------------
    relief_down=2,           # relief/erosion resolution (tiles per pixel)
    relief_amp=0.26,         # how much relief reaches the colour
    relief_warp_tiles=20.0,
    relief_warp_scale=700,
    erosion_window=6,        # in tiles
    erosion_layers=24,
    resid_window=4,
    resid_layers=10,
    carved_weight=1.35,      # the erosion residual (the flow/tendril structure)
    second_weight=0.4,
    grain_weight=0.05,
    relief_blur_tiles=2,
    relief_near_tiles=4.0,   # bright relief reaches this close to the shore
    relief_dark_start=3.0,   # dark relief only starts this far out
    relief_dark_tiles=12.0,
    regional_min=0.7,        # regional erosion strength
    regional_max=1.6,

    # ---- shading --------------------------------------------------------
    shading=False,           # False = flat top-down, True = NW hillshade
    shade_amp=1.45,
    shade_clip=(0.76, 1.24),

    # ---- colour ---------------------------------------------------------
    land_t=None,             # tone under the land (covered by tiles in game)
    land_band=0,             # tiles of coast tone pushed INTO the land fill, so
                             # the tiles' seam never shows a dark ring
    land_band_t=None,        # None = use shore_base
    rim_levels=4,            # coast pass width in pixels (dilated from the mask)
    rim_min=2.0,             # coast pass range, tiles
    rim_max=5.0,
    rim_noise_scale=1200,
    rim_strength=0.06,       # additive strength at the very coast
    rim_rgb=(95.0, 155.0, 195.0),
)

PALETTE_STOPS = np.array([0.00, 0.03, 0.07, 0.12, 0.20, 0.32, 0.46, 0.66, 1.00],
                         dtype=np.float32)
PALETTE_COLS = np.array([
    [74, 126, 156], [58, 104, 134], [45, 86, 116], [35, 70, 98], [28, 57, 82],
    [22, 46, 67], [17, 36, 53], [12, 25, 37], [7, 13, 18],
], dtype=np.float32)


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def box_blur(a, r, passes=2):
    a = a.astype(np.float32)
    for _ in range(passes):
        for axis in (0, 1):
            n = a.shape[axis]
            pad = [(0, 0), (0, 0)]
            pad[axis] = (r + 1, r)
            q = np.pad(a, pad, mode="edge")
            c = np.cumsum(q, axis=axis)
            hi = [slice(None)] * 2; hi[axis] = slice(2 * r + 1, 2 * r + 1 + n)
            lo = [slice(None)] * 2; lo[axis] = slice(0, n)
            a = (c[tuple(hi)] - c[tuple(lo)]) / (2 * r + 1)
    return a


def fbm(sx, sy, scale_tiles, octaves=5, gain=0.55, seed=0, px_per_tile=1.0):
    r = np.random.default_rng(seed)
    out = np.zeros((sy, sx), dtype=np.float32)
    amp, total = 1.0, 0.0
    res = max(3, int(sx / (scale_tiles * px_per_tile)))
    for _ in range(octaves):
        g = r.random((res, res)).astype(np.float32)
        out += np.array(Image.fromarray(g).resize((sx, sy), Image.BICUBIC)) * amp
        total += amp
        amp *= gain
        res *= 2
    return out / total


def upscale(values, shape):
    return np.array(Image.fromarray(values.astype(np.float32), mode="F").resize(shape, Image.BICUBIC))


def edt_small(mask, down=CFG["edt_down"], cancel=None):
    """exact separable Euclidean distance transform, in tiles, at 1/down"""
    h, w = mask.shape
    sw, sh = max(1, w // down), max(1, h // down)
    small = np.array(
        Image.fromarray(mask.astype(np.uint8) * 255).resize((sw, sh), Image.NEAREST)
    ) > 0
    idx = np.arange(sw, dtype=np.int32)[None, :]
    BIG = 10 ** 6
    left = idx - np.maximum.accumulate(np.where(small, idx, -BIG), axis=1)
    right = np.minimum.accumulate(np.where(small, idx, BIG)[:, ::-1], axis=1)[:, ::-1] - idx
    dx = np.minimum(left, right).astype(np.float32)
    dx[small] = 0.0
    d1 = dx * dx
    yy = np.arange(sh, dtype=np.float32)[:, None]
    d = np.full((sh, sw), np.inf, dtype=np.float32)
    for y0 in range(sh):
        if cancel is not None and (y0 & 127) == 0:
            cancel()
        np.minimum(d[y0], (d1 + (yy - y0) ** 2).min(axis=0), out=d[y0])
    return np.sqrt(d) * down


def edt(mask, down=CFG["edt_down"], cancel=None):
    h, w = mask.shape
    return upscale(edt_small(mask, down, cancel=cancel), (w, h))


def dilate1(mask):
    img = Image.fromarray(mask.astype(np.uint8) * 255)
    return np.asarray(img.filter(ImageFilter.MaxFilter(3))) > 0


def window_dist(mask, w):
    out = np.full(mask.shape, w + 1.0, dtype=np.float32)
    cur = mask.copy()
    for k in range(1, w + 1):
        cur = dilate1(cur)
        out = np.where((out > w) & cur, np.float32(k), out)
    return out


def slab_erode(h, window=3, layers=24, cancel=None):
    """Hatchling's slab erosion (proceduralpixels "fastest erosion algorithm"):
    per threshold, distance to the nearest in/out-of-threshold texel inside a
    small window, remapped to a linear step, accumulated over layers."""
    lo, hi = float(h.min()), float(h.max())
    acc = np.zeros(h.shape, dtype=np.float32)
    for i in range(layers):
        if cancel is not None:
            cancel()
        t = lo + (hi - lo) * (i + 0.5) / layers
        above = h > t
        dp = window_dist(above, window)
        dn = window_dist(~above, window)
        acc += np.clip((dn - dp) / window, -1.0, 1.0)
    return acc / layers


def zscore(a):
    return (a - a.mean()) / (a.std() + 1e-5)


def render(land, d, cfg=CFG):
    """land: (h,w) bool, d: (h,w) distance in tiles -> rgb uint8 (h,w,3)"""
    H, W = land.shape
    p = 1.0 / cfg["relief_down"]          # relief pixels per tile

    # ---------------------------------------------------------- shore pool
    n_pool = (0.7 * fbm(W, H, cfg["pool_noise_scale"], 3, seed=14)
              + 0.3 * fbm(W, H, cfg["pool_noise2_scale"], 4, seed=16))
    L = cfg["pool_min"] + (cfg["pool_max"] - cfg["pool_min"]) * smoothstep((n_pool - 0.32) * 2.0)
    F = cfg["ease_min"] + (cfg["ease_max"] - cfg["ease_min"]) * smoothstep(
        (fbm(W, H, cfg["ease_noise_scale"], 3, seed=15) - 0.30) * 1.8)
    del n_pool

    pool = np.clip(d / L, 0, 1.0)
    t_shore = cfg["shore_base"] + cfg["shore_lift"] * smoothstep(pool)
    ease = smoothstep((d - 0.5 * L) / F)
    del pool, F

    # ---------------------------------------------------------- open water
    r = max(1, int(cfg["dist_smooth_tiles"]))
    d_s = box_blur(d.astype(np.float32), r, cfg["dist_smooth_passes"])
    w1 = (fbm(W, H, cfg["dist_warp_scale"], 3, seed=21) - 0.5) * cfg["dist_warp_tiles"]
    w2 = (fbm(W, H, cfg["dist_warp_scale"], 3, seed=22) - 0.5) * cfg["dist_warp_tiles"]
    dd = np.empty((H, W), dtype=np.float32)
    for y0 in range(0, H, 256):
        y1 = min(y0 + 256, H)
        xi = np.clip((np.arange(W, dtype=np.float32)[None, :] + w2[y0:y1]).astype(np.int32), 0, W - 1)
        yi = np.clip((np.arange(y0, y1, dtype=np.float32)[:, None] + w1[y0:y1]).astype(np.int32), 0, H - 1)
        dd[y0:y1] = d_s[yi, xi]
    del d_s, w1, w2

    deep = cfg["deep_base"] + cfg["deep_range"] * (1.0 - np.exp(-dd / cfg["deep_len"]))
    del dd
    for amp, scale, oct_, seed in cfg["deep_noise"]:
        deep += amp * (fbm(W, H, scale, oct_, seed=seed) - 0.5)

    w_blur = cfg["blur_weight"] * smoothstep((d - cfg["blur_start"]) / cfg["blur_ramp"])
    deep_b = deep * (1.0 - w_blur) + box_blur(deep, 2, 2) * w_blur
    del deep, w_blur

    # -------------------------------------------------------------- relief
    SW, SH = max(8, int(W * p)), max(8, int(H * p))
    r1 = 1.0 - np.abs(2.0 * fbm(SW, SH, 600, 5, gain=0.55, seed=31, px_per_tile=p) - 1.0)
    r1 = 0.85 * r1 + 0.15 * fbm(SW, SH, 300, 5, gain=0.55, seed=32, px_per_tile=p)
    r1 += 0.08 * fbm(SW, SH, 120, 4, gain=0.5, seed=33, px_per_tile=p)

    wv = (fbm(SW, SH, cfg["relief_warp_scale"], 3, seed=34, px_per_tile=p) - 0.5) \
        * cfg["relief_warp_tiles"] * p
    yi, xi = np.mgrid[0:SH, 0:SW]
    r1 = r1[np.clip((yi + wv).astype(np.int32), 0, SH - 1),
            np.clip((xi + wv * 0.8).astype(np.int32), 0, SW - 1)]
    del yi, xi, wv

    e1 = slab_erode(r1, window=max(2, int(round(cfg["erosion_window"] * p))),
                    layers=cfg["erosion_layers"])
    resid = r1 - e1
    e2 = slab_erode(resid * 3.0 + 0.5, window=max(2, int(round(cfg["resid_window"] * p))),
                    layers=cfg["resid_layers"])
    carved = r1 - e1

    relief = zscore(e1)
    relief += cfg["carved_weight"] * zscore(carved)
    relief += cfg["second_weight"] * zscore(e2)
    relief += cfg["grain_weight"] * zscore(fbm(SW, SH, 40, 4, gain=0.5, seed=37, px_per_tile=p))
    del e1, e2, resid, carved
    relief *= cfg["regional_min"] + (cfg["regional_max"] - cfg["regional_min"]) * smoothstep(
        (fbm(SW, SH, 900, 3, seed=35, px_per_tile=p) - 0.25) * 1.6)
    relief = np.clip(relief, -3.0, 3.0)
    rb = max(1, int(round(cfg["relief_blur_tiles"] * p)))
    relief = box_blur(relief, rb, 1)
    relief = upscale(relief, (W, H))

    # --------------------------------------------------------------- depth
    # bright relief is allowed right up to the shore, dark relief is held back:
    # masking both equally left a band with no bright wisps next to the island,
    # which read as a ring darker than the water outside it
    fx_bright = smoothstep(d / cfg["relief_near_tiles"])
    fx_dark = smoothstep((d - cfg["relief_dark_start"]) / cfg["relief_dark_tiles"])
    relief_term = np.maximum(relief, 0.0) * fx_bright + np.minimum(relief, 0.0) * fx_dark
    fx_mask = fx_dark
    t = t_shore + (deep_b + cfg["relief_amp"] * relief_term - t_shore) * ease
    del relief_term
    del deep_b, t_shore, ease, L
    if cfg["land_t"] is not None:
        t = np.where(land, cfg["land_t"], t)

    # push the shore tone a couple of tiles into the land footprint: the map's
    # tiles sit slightly inset, so a dark land fill would otherwise peek out as
    # a ring that is darker than the water beside it
    if cfg["land_band"] > 0:
        grow = ~land
        for _ in range(int(cfg["land_band"])):
            grow = dilate1(grow)
        band = grow & land
        band_t = cfg["shore_base"] if cfg["land_band_t"] is None else cfg["land_band_t"]
        t = np.where(band, band_t, t)
        del grow, band

    # invariant: open water is never brighter than the shelf, so an island edge
    # is always the highest point in the picture
    shelf_floor = cfg["shore_base"] + cfg["shore_lift"]
    t = np.where(land, t, np.maximum(t, shelf_floor))

    t = np.clip(t, 0, 1)

    # ------------------------------------------------------------ shading
    if cfg["shading"]:
        height = np.where(land, 0.0, relief) * (0.35 + 0.65 * fx_mask)
        gy, gx = np.gradient(box_blur(height, 1, 1))
        nrm = np.sqrt(gx * gx + gy * gy + 1.0)
        Lv = np.array([-0.5, -0.6, 0.62]); Lv /= np.linalg.norm(Lv)
        shade = (-gx * Lv[0] - gy * Lv[1] + Lv[2]) / nrm
        lo, hi = cfg["shade_clip"]
        shade = np.clip(1.0 + cfg["shade_amp"] * (shade - 0.62), lo, hi)
        del height, gy, gx, nrm
    else:
        shade = 1.0
    del relief

    # ------------------------------------------------------------- colour
    rgb = np.stack([np.interp(t, PALETTE_STOPS, PALETTE_COLS[:, i]) for i in range(3)], axis=-1)
    rgb = rgb * shade

    # separate coast pass: measured in PIXELS from the land mask, not from the
    # distance field, so it always starts on the first water pixel
    levels = max(1, int(cfg["rim_levels"]))
    band = np.zeros((H, W), dtype=np.float32)
    cur = land.copy()
    for k in range(1, levels + 1):
        nxt = dilate1(cur)
        band = np.where(nxt & ~cur, np.float32(k), band)
        cur = nxt
    rim = np.clip(1.0 - (band - 1.0) / max(1.0, levels - 1.0), 0.0, 1.0)
    rgb = rgb + np.array(cfg["rim_rgb"]) * (cfg["rim_strength"] * rim[:, :, None])

    return np.clip(rgb, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Minimal variant: height = distance from land, then erosion. No pool, no
# masks, no coast pass. Start here when tuning; the full pipeline is above.
CFG_SIMPLE = dict(
    deep_dist=1600.0,        # tiles from land where the floor reaches its lowest
    curve=1.0,              # >1 keeps shallows wide, <1 drops off sooner
    noise_amp=0.15,         # a little variation so the erosion has something to work on
    noise_scale=350,
    noise_octaves=4,
    noise_seed=11,
    relief_down=2,          # erosion resolution (tiles per pixel)
    erosion_window=5,       # in tiles
    erosion_layers=20,
    erosion_mix=1.0,        # 0 = raw distance height, 1 = fully eroded
    land_t=None,            # None = land keeps the top height (brightest)
)


def render_simple(land, d, cfg=CFG_SIMPLE):
    H, W = land.shape
    p = 1.0 / cfg["relief_down"]

    h = np.clip(1.0 - d / cfg["deep_dist"], 0.0, 1.0) ** cfg["curve"]
    if cfg["noise_amp"]:
        n = fbm(W, H, cfg["noise_scale"], cfg["noise_octaves"], seed=cfg["noise_seed"]) - 0.5
        h = np.clip(h * (1.0 + 2.0 * cfg["noise_amp"] * n), 0.0, 1.0)

    if cfg["erosion_mix"] > 0:
        SW, SH = max(8, int(W * p)), max(8, int(H * p))
        hs = np.array(Image.fromarray(h.astype(np.float32), mode="F").resize((SW, SH), Image.BICUBIC))
        he = slab_erode(hs, window=max(2, int(round(cfg["erosion_window"] * p))),
                        layers=cfg["erosion_layers"])
        he = upscale(he, (W, H))
        h = h * (1.0 - cfg["erosion_mix"]) + he * cfg["erosion_mix"]
        del hs, he

    t = 1.0 - np.clip(h, 0.0, 1.0)
    if cfg["land_t"] is not None:
        t = np.where(land, cfg["land_t"], t)
    t = np.clip(t, 0, 1)

    rgb = np.stack([np.interp(t, PALETTE_STOPS, PALETTE_COLS[:, i]) for i in range(3)], axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)
