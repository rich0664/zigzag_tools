"""Shared sea-bathymetry pipeline.

Both entry points call `render()` on a region:
  tools/build_sea.py   -> render(0, 0, 7749, 5254)  -> dist/zigzag-sea.webp
  tools/sea_crop.py    -> render(x0, y0, w, h)      -> out/sea/v2_crop.png

Everything here is resolution-independent: distances are in map tiles, noise
scales are in tiles, and the relief is always computed at RELIEF_DOWN tiles
per pixel, so a crop is the same picture as the corresponding part of the
full map.

Requires numpy + Pillow.
"""
import numpy as np
from PIL import Image, ImageFilter

W_MAP, H_MAP = 7749, 5254

EDT_DOWN = 2        # distance field: 2 tiles per pixel
RELIEF_DOWN = 2     # relief/erosion: 2 tiles per pixel


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


def edt_small(mask, down=EDT_DOWN):
    """exact separable Euclidean distance transform, in map tiles, at 1/down"""
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
        np.minimum(d[y0], (d1 + (yy - y0) ** 2).min(axis=0), out=d[y0])
    return np.sqrt(d) * down


def edt(mask, down=EDT_DOWN):
    h, w = mask.shape
    return upscale(edt_small(mask, down), (w, h))


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


def slab_erode(h, window=3, layers=24):
    """Hatchling's slab erosion (proceduralpixels "fastest erosion algorithm"):
    per threshold, distance to the nearest in/out-of-threshold texel inside a
    small window, remapped to a linear step, accumulated over layers."""
    lo, hi = float(h.min()), float(h.max())
    acc = np.zeros(h.shape, dtype=np.float32)
    for i in range(layers):
        t = lo + (hi - lo) * (i + 0.5) / layers
        above = h > t
        dp = window_dist(above, window)
        dn = window_dist(~above, window)
        acc += np.clip((dn - dp) / window, -1.0, 1.0)
    return acc / layers


PALETTE_STOPS = np.array([0.00, 0.03, 0.07, 0.12, 0.20, 0.32, 0.46, 0.66, 1.00],
                         dtype=np.float32)
PALETTE_COLS = np.array([
    [26, 54, 73], [25, 50, 69], [23, 47, 65], [20, 42, 59], [18, 38, 53],
    [16, 34, 49], [14, 29, 42], [11, 22, 33], [7, 13, 18],
], dtype=np.float32)


def render(land, d, seed=41):
    """land: (h,w) bool, d: (h,w) distance in tiles -> rgb uint8 (h,w,3)"""
    H, W = land.shape
    p = 1.0 / RELIEF_DOWN          # px per tile used by the relief grid

    # ---------------------------------------------------------- shore pool
    n_pool = 0.7 * fbm(W, H, 130, 3, seed=14) + 0.3 * fbm(W, H, 420, 4, seed=16)
    L = 2.0 + 12.0 * smoothstep((n_pool - 0.32) * 2.0)
    F = 18.0 + 60.0 * smoothstep((fbm(W, H, 700, 3, seed=15) - 0.30) * 1.8)
    del n_pool

    pool = np.clip(d / L, 0, 1.0)
    t_shore = 0.015 + 0.05 * smoothstep(pool)
    ease = smoothstep((d - 0.5 * L) / F)
    del pool, F

    # ---------------------------------------------------------- open water
    # depth is distance derived: smooth the EDT, domain-warp it, then curve it
    d_s = box_blur(d.astype(np.float32), 3, 2)
    w1 = (fbm(W, H, 700, 3, seed=21) - 0.5) * 46.0
    w2 = (fbm(W, H, 700, 3, seed=22) - 0.5) * 46.0
    dd = np.empty((H, W), dtype=np.float32)
    for y0 in range(0, H, 256):
        y1 = min(y0 + 256, H)
        xi = np.clip((np.arange(W, dtype=np.float32)[None, :] + w2[y0:y1]).astype(np.int32), 0, W - 1)
        yi = np.clip((np.arange(y0, y1, dtype=np.float32)[:, None] + w1[y0:y1]).astype(np.int32), 0, H - 1)
        dd[y0:y1] = d_s[yi, xi]
    del d_s, w1, w2

    deep = 0.18 + 0.62 * (1.0 - np.exp(-dd / 260.0))
    del dd
    deep += 0.20 * (fbm(W, H, 2600, 3, seed=51) - 0.5)
    deep += 0.06 * (fbm(W, H, 900, 3, seed=23) - 0.5)
    deep += 0.012 * (fbm(W, H, 300, 4, seed=24) - 0.5)
    deep += 0.006 * (fbm(W, H, 120, 4, seed=26) - 0.5)

    w_blur = 0.80 * smoothstep((d - 1.5) / 12.0)
    deep_b = deep * (1.0 - w_blur) + box_blur(deep, 2, 2) * w_blur
    del deep, w_blur

    # -------------------------------------------------------------- relief
    SW, SH = max(8, int(W * p)), max(8, int(H * p))
    r1 = 1.0 - np.abs(2.0 * fbm(SW, SH, 600, 5, gain=0.55, seed=31, px_per_tile=p) - 1.0)
    r1 = 0.85 * r1 + 0.15 * fbm(SW, SH, 300, 5, gain=0.55, seed=32, px_per_tile=p)
    r1 += 0.08 * fbm(SW, SH, 120, 4, gain=0.5, seed=33, px_per_tile=p)
    wv = (fbm(SW, SH, 700, 3, seed=34, px_per_tile=p) - 0.5) * 20.0 * p
    yi, xi = np.mgrid[0:SH, 0:SW]
    r1 = r1[np.clip((yi + wv).astype(np.int32), 0, SH - 1),
            np.clip((xi + wv * 0.8).astype(np.int32), 0, SW - 1)]
    del yi, xi, wv

    e1 = slab_erode(r1, window=max(2, int(6 * p)), layers=24)
    resid = r1 - e1
    e2 = slab_erode(resid * 3.0 + 0.5, window=max(2, int(4 * p)), layers=10)
    carved = r1 - e1
    relief = (e1 - e1.mean()) / (e1.std() + 1e-5)
    relief += 1.35 * ((carved - carved.mean()) / (carved.std() + 1e-5))
    relief += 0.4 * ((e2 - e2.mean()) / (e2.std() + 1e-5))
    grain = fbm(SW, SH, 40, 4, gain=0.5, seed=37, px_per_tile=p)
    relief += 0.05 * ((grain - grain.mean()) / (grain.std() + 1e-5))
    del grain, e1, e2, resid, carved
    relief *= 0.7 + 0.9 * smoothstep((fbm(SW, SH, 900, 3, seed=35, px_per_tile=p) - 0.25) * 1.6)
    relief = np.clip(relief, -3.0, 3.0)
    relief = box_blur(relief, 1, 1)
    relief = np.array(Image.fromarray(relief, mode="F").resize((W, H), Image.BICUBIC))

    # --------------------------------------------------------------- depth
    fx_mask = smoothstep((d - 1.0) / 9.0)
    t = t_shore + (deep_b + 0.26 * relief * fx_mask - t_shore) * ease
    del deep_b, t_shore, ease, L
    t = np.where(land, 0.58, t)
    t = np.clip(t, 0, 1)

    # ------------------------------------------------------------ shading
    height = np.where(land, 0.0, relief) * (0.35 + 0.65 * fx_mask)
    gy, gx = np.gradient(box_blur(height, 1, 1))
    nrm = np.sqrt(gx * gx + gy * gy + 1.0)
    Lv = np.array([-0.5, -0.6, 0.62]); Lv /= np.linalg.norm(Lv)
    shade = np.clip(1.0 + 1.45 * (((-gx * Lv[0] - gy * Lv[1] + Lv[2]) / nrm) - 0.62), 0.76, 1.24)
    del height, gy, gx, nrm, relief

    rgb = np.stack([np.interp(t, PALETTE_STOPS, PALETTE_COLS[:, i]) for i in range(3)], axis=-1)
    rgb = rgb * shade[:, :, None]

    # separate coast pass: pure distance, 2-5 tile range, additive
    r_rim = 2.0 + 3.0 * smoothstep((fbm(W, H, 1200, 3, seed=71) - 0.30) * 1.8)
    rim = np.clip(1.0 - (d - 1.0) / r_rim, 0.0, 1.0) * (~land)
    rgb = rgb + np.array([95.0, 155.0, 195.0]) * (0.13 * rim[:, :, None])

    return np.clip(rgb, 0, 255).astype(np.uint8)
