"""Build the zig~zag sea texture (7749 x 5254, 1 px per map tile).

    python3 tools/build_sea.py        ->  dist/zigzag-sea.webp   (~3 min)

First run downloads the map raster and computes the distance field into
out/sea/ and reuses them afterwards. Requires numpy + Pillow.

Pipeline, in order (all the knobs are inline below with comments):
  1. distance field d (EDT, 1/4 res, upscaled)
  2. shore "pool": length L 2-22 tiles from slow noise, plus the ease-off F
  3. open water: blur(d) -> domain warp -> exponential depth curve, + noise
  4. effect mask: blur / relief / shading fade in just off the beach
  5. relief: ridged + fbm input, two-scale slab erosion, carved residual kept
  6. colour: palette LUT + NW hillshade + separate coast pass
"""
import os
import urllib.request
import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK = os.path.join(ROOT, "out", "sea")
MAP_URL = "https://d79e46apek3bq.cloudfront.net/maps/64/2026-11-01T22-21-31Z/map.png"
MAP_PNG = os.path.join(WORK, "map.png")
DIST_NPY = os.path.join(WORK, "dist.npy")
W, H = 7749, 5254

os.makedirs(WORK, exist_ok=True)
if not os.path.exists(MAP_PNG):
    print("downloading map raster…")
    urllib.request.urlretrieve(MAP_URL, MAP_PNG)

src = Image.open(MAP_PNG).convert("RGBA")
land = np.array(src)[:, :, 3] > 0

if not os.path.exists(DIST_NPY):
    print("distance field…")
    DOWN = 4
    w, h = W // DOWN, H // DOWN
    small = np.array(
        Image.fromarray(land.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)
    ) > 0
    idx = np.arange(w, dtype=np.int32)[None, :]
    BIG = 10 ** 6
    left = idx - np.maximum.accumulate(np.where(small, idx, -BIG), axis=1)
    right = np.minimum.accumulate(np.where(small, idx, BIG)[:, ::-1], axis=1)[:, ::-1] - idx
    dx = np.minimum(left, right).astype(np.float32)
    dx[small] = 0.0
    d1 = dx * dx
    yy = np.arange(h, dtype=np.float32)[:, None]
    d = np.full((h, w), np.inf, dtype=np.float32)
    for y0 in range(h):
        np.minimum(d[y0], (d1 + (yy - y0) ** 2).min(axis=0), out=d[y0])
    np.save(DIST_NPY, np.sqrt(d) * DOWN)

print("map", W, H)

def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)

def box_blur(a, r, passes=2):
    a = a.astype(np.float32)
    for _ in range(passes):
        for axis in (0, 1):
            n = a.shape[axis]
            p = [(0, 0), (0, 0)]
            p[axis] = (r + 1, r)
            q = np.pad(a, p, mode="edge")
            c = np.cumsum(q, axis=axis)
            hi = [slice(None)] * 2; hi[axis] = slice(2 * r + 1, 2 * r + 1 + n)
            lo = [slice(None)] * 2; lo[axis] = slice(0, n)
            a = (c[tuple(hi)] - c[tuple(lo)]) / (2 * r + 1)
    return a

def fbm(sx, sy, scale_tiles, octaves=5, gain=0.55, seed=0):
    r = np.random.default_rng(seed)
    out = np.zeros((sy, sx), dtype=np.float32)
    amp, total, res = 1.0, 0.0, max(3, int(sx / scale_tiles))
    for _ in range(octaves):
        g = r.random((res, res)).astype(np.float32)
        out += np.array(Image.fromarray(g).resize((sx, sy), Image.BICUBIC)) * amp
        total += amp
        amp *= gain
        res *= 2
    return out / total

def dilate1(mask):
    img = Image.fromarray((mask.astype(np.uint8) * 255))
    return np.asarray(img.filter(ImageFilter.MaxFilter(3))) > 0

def window_dist(mask, w):
    out = np.full(mask.shape, w + 1.0, dtype=np.float32)
    cur = mask.copy()
    for k in range(1, w + 1):
        cur = dilate1(cur)
        out = np.where((out > w) & cur, np.float32(k), out)
    return out

def slab_erode(h, window=5, layers=14):
    lo, hi = float(h.min()), float(h.max())
    acc = np.zeros(h.shape, dtype=np.float32)
    for i in range(layers):
        t = lo + (hi - lo) * (i + 0.5) / layers
        above = h > t
        dp = window_dist(above, window)
        dn = window_dist(~above, window)
        acc += np.clip((dn - dp) / window, -1.0, 1.0)
    return acc / layers

print("distance…")
dist = np.load(DIST_NPY)
d = np.array(Image.fromarray(dist.astype(np.float32), mode="F").resize((W, H), Image.BICUBIC))
del dist

# --------------------------------------------------------- pool / transition
print("pool…")
n_pool = 0.7 * fbm(W, H, 130, 3, seed=14) + 0.3 * fbm(W, H, 420, 4, seed=16)
L = 2.0 + 12.0 * smoothstep((n_pool - 0.32) * 2.0)
del n_pool
F = 18.0 + 60.0 * smoothstep((fbm(W, H, 700, 3, seed=15) - 0.30) * 1.8)

pool = np.clip(d / L, 0, 1.0)
t_shore = 0.015 + 0.05 * smoothstep(pool)
del pool
# start blending back into the basin before the pool ends -> softer transition
ease = smoothstep((d - 0.5 * L) / F)
del F

# ------------------------------------------------------------- open water
# depth is distance derived again: smooth the EDT so its medial-axis kinks are
# gone, then domain-warp it so what is left reads as organic wobble rather than
# lines between islands. Noise modulates it but does not replace it.
print("deep…")
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


# effect mask reaches much closer to the shore now
w_blur = 0.80 * smoothstep((d - 1.5) / 12.0)
deep_b = deep * (1.0 - w_blur) + box_blur(deep, 2, 2) * w_blur
del deep, w_blur

# ------------------------------------------------------------------- relief
print("relief…")
SW, SH = W // 4, H // 4
r1 = 1.0 - np.abs(2.0 * fbm(SW, SH, 150, 5, gain=0.55, seed=31) - 1.0)
r1 = 0.85 * r1 + 0.15 * fbm(SW, SH, 75, 5, gain=0.55, seed=32)
r1 += 0.08 * fbm(SW, SH, 30, 4, gain=0.5, seed=33)
wv = (fbm(SW, SH, 175, 3, seed=34) - 0.5) * 5.0
yi, xi = np.mgrid[0:SH, 0:SW]
r1 = r1[np.clip((yi + wv).astype(np.int32), 0, SH - 1),
        np.clip((xi + wv * 0.8).astype(np.int32), 0, SW - 1)]
del yi, xi, wv

print("erosion…")
e1 = slab_erode(r1, window=6, layers=24)
resid = r1 - e1
e2 = slab_erode(resid * 3.0 + 0.5, window=4, layers=10)
carved = r1 - e1
relief = (e1 - e1.mean()) / (e1.std() + 1e-5)
relief += 1.35 * ((carved - carved.mean()) / (carved.std() + 1e-5))
relief += 0.4 * ((e2 - e2.mean()) / (e2.std() + 1e-5))
grain = fbm(SW, SH, 10, 4, gain=0.5, seed=37)
relief += 0.05 * ((grain - grain.mean()) / (grain.std() + 1e-5))
del grain
relief *= 0.7 + 0.9 * smoothstep((fbm(SW, SH, 225, 3, seed=35) - 0.25) * 1.6)
relief = np.clip(relief, -3.0, 3.0)
relief = box_blur(relief, 2, 2)
relief = np.array(Image.fromarray(relief.astype(np.float32), mode="F").resize((W, H), Image.BICUBIC))
del e1, e2, resid, r1

# ------------------------------------------------------------------- depth
fx_mask = smoothstep((d - 1.0) / 9.0)          # relief fades in right off the beach
t = t_shore + (deep_b + 0.26 * relief * fx_mask - t_shore) * ease
del deep_b, t_shore, ease, L
t = np.where(land, 0.58, t)
t = np.clip(t, 0, 1)
print("t p5/p50/p95 %.3f %.3f %.3f" % tuple(np.percentile(t[~land], [5, 50, 95])))

# ----------------------------------------------------------------- shading
print("shade…")
height = np.where(land, 0.0, relief) * (0.35 + 0.65 * fx_mask)
gy, gx = np.gradient(box_blur(height, 1, 1))
nrm = np.sqrt(gx * gx + gy * gy + 1.0)
Lv = np.array([-0.5, -0.6, 0.62]); Lv /= np.linalg.norm(Lv)
shade = np.clip(1.0 + 1.45 * (((-gx * Lv[0] - gy * Lv[1] + Lv[2]) / nrm) - 0.62), 0.76, 1.24)
del height, gy, gx, nrm, relief

stops = np.array([0.00, 0.03, 0.07, 0.12, 0.20, 0.32, 0.46, 0.66, 1.00], dtype=np.float32)
cols = np.array([
    [26, 54, 73], [25, 50, 69], [23, 47, 65], [20, 42, 59], [18, 38, 53],
    [16, 34, 49], [14, 29, 42], [11, 22, 33], [7, 13, 18],
], dtype=np.float32)

rgb = np.stack([np.interp(t, stops, cols[:, i]) for i in range(3)], axis=-1)
rgb = rgb * shade[:, :, None]

# separate coast pass: pure distance, max range 2-5 tiles driven by slow noise,
# added as a light additive blend so there is always a solid 1 px shoreline
r_rim = 2.0 + 3.0 * smoothstep((fbm(W, H, 1200, 3, seed=71) - 0.30) * 1.8)
rim = np.clip(1.0 - (d - 1.0) / r_rim, 0.0, 1.0) * (~land)
rgb = rgb + np.array([95.0, 155.0, 195.0]) * (0.13 * rim[:, :, None])
rgb = np.clip(rgb, 0, 255).astype(np.uint8)
del d
Image.fromarray(rgb, "RGB").save(os.path.join(WORK, "sea64.png"))
Image.open(os.path.join(WORK, "sea64.png")).save(os.path.join(ROOT, "dist", "zigzag-sea.webp"),
                                                quality=92, method=6)
print("wrote dist/zigzag-sea.webp")
