"""Sea bathymetry v2 - prototype renderer.

Architecture (per the re-plan):
  1. distance field d  -> the 2-5 tile shore band comes straight out of the
     curve of d (no mask dilation, no hard override).
  2. d is also a MASK: every "character" effect (blur, noise, erosion) is
     multiplied by smoothstep((d - band)/blend), so nothing touches the shore.
  3. open-water depth comes from a blurred land-influence field, not from d,
     so there are no medial-axis creases in the deep.
  4. relief is Hatchling's slab erosion (proceduralpixels "fastest erosion"),
     vectorised: per threshold, distance to nearest in/out-of-threshold texel
     in a window, remapped to a linear step; accumulate over layers.

Renders a single crop by default so iterations are cheap.
"""
import os
import urllib.request
import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK = os.path.join(ROOT, "out", "sea")
MAP_URL = "https://d79e46apek3bq.cloudfront.net/maps/64/2026-11-01T22-21-31Z/map.png"
MAP_PNG = os.path.join(WORK, "map.png")

os.makedirs(WORK, exist_ok=True)
if not os.path.exists(MAP_PNG):
    print("downloading map raster…")
    urllib.request.urlretrieve(MAP_URL, MAP_PNG)

# crop in map tiles, plus context for the distance field
X0, Y0, X1, Y1 = 2800, 2150, 4350, 3200
M = 240
Wc, Hc = X1 - X0, Y1 - Y0

src = Image.open(MAP_PNG).convert("RGBA")
full = np.array(src)[:, :, 3] > 0
pad = np.zeros((full.shape[0] + 2 * M, full.shape[1] + 2 * M), dtype=bool)
pad[M:M + full.shape[0], M:M + full.shape[1]] = full
local_land = pad[Y0:Y1 + M, X0:X1 + M]
sub = pad[Y0:Y1 + M, X0:X1 + M]

def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)

def box_blur(a, r, passes=3):
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

def edt(mask, down=2):
    """exact separable Euclidean distance transform, in tiles"""
    h, w = mask.shape
    sw, sh = w // down, h // down
    small = np.array(Image.fromarray(mask.astype(np.uint8) * 255).resize((sw, sh), Image.NEAREST)) > 0
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
    dist = np.sqrt(d) * down
    return np.array(Image.fromarray(dist, mode="F").resize((w, h), Image.BICUBIC))

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
    """distance (1..w, or w+1 = none) to the nearest True texel in a window"""
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
d_full = edt(sub, 2)
d = d_full[M:M + Hc, M:M + Wc]
land = local_land[M:M + Hc, M:M + Wc]

print("fields…")
# how far the bright "swimming pool" reaches before easing into the dark:
# 2-22 tiles, varying along a coast, so some shores are wide and shallow and
# others drop away almost immediately
n_pool = 0.7 * fbm(Wc, Hc, 130, 3, seed=14) + 0.3 * fbm(Wc, Hc, 420, 4, seed=16)
L = 2.0 + 12.0 * smoothstep((n_pool - 0.32) * 2.0)
F = 7.0 + 45.0 * smoothstep((fbm(Wc, Hc, 700, 3, seed=15) - 0.30) * 1.8)

pool = np.clip(d / L, 0, 1.0)
t_shore = 0.015 + 0.05 * smoothstep(pool)     # nearly flat across the pool

# 3: open water from a blurred land influence (no medial axes)
# depth still comes from distance, but the distance field is smoothed and then
# domain-warped so its medial-axis kinks turn into organic wobble instead of
# lines between islands
d_s = box_blur(d.astype(np.float32), 3, 2)
w1 = (fbm(Wc, Hc, 700, 3, seed=21) - 0.5) * 46.0
w2 = (fbm(Wc, Hc, 700, 3, seed=22) - 0.5) * 46.0
yy2, xx2 = np.mgrid[0:Hc, 0:Wc]
dd = d_s[np.clip((yy2 + w1).astype(np.int32), 0, Hc - 1),
         np.clip((xx2 + w2).astype(np.int32), 0, Wc - 1)]
del d_s, w1, w2, yy2, xx2

deep = 0.18 + 0.62 * (1.0 - np.exp(-dd / 260.0))
del dd
# some oceans are much deeper than others
deep += 0.20 * (fbm(Wc, Hc, 2600, 3, seed=51) - 0.5)
deep += 0.06 * (fbm(Wc, Hc, 900, 3, seed=23) - 0.5)
deep += 0.012 * (fbm(Wc, Hc, 300, 4, seed=24) - 0.5)
deep += 0.006 * (fbm(Wc, Hc, 120, 4, seed=26) - 0.5)

# tidal-flat style channels: anisotropic, warped ridged noise, sharpened so it
# reads as meandering bars rather than clouds


# 4: eroded relief
print("relief…")
# layered input: ridged systems + broad fbm + grain, then domain-warped so the
# ridges are not axis aligned
r1 = 1.0 - np.abs(2.0 * fbm(Wc, Hc, 600, 5, gain=0.55, seed=31) - 1.0)
r1 = 0.85 * r1 + 0.15 * fbm(Wc, Hc, 300, 5, gain=0.55, seed=32)
r1 += 0.08 * fbm(Wc, Hc, 120, 4, gain=0.5, seed=33)

w = (fbm(Wc, Hc, 700, 3, seed=34) - 0.5) * 20.0
yi, xi = np.mgrid[0:Hc, 0:Wc]
r1 = r1[np.clip((yi + w).astype(np.int32), 0, Hc - 1),
        np.clip((xi + w * 0.8).astype(np.int32), 0, Wc - 1)]

print("erosion…")
# two scales: broad sediment slopes, then fine channels from the residual
e1 = slab_erode(r1, window=6, layers=24)
resid = r1 - e1
e2 = slab_erode(resid * 3.0 + 0.5, window=4, layers=10)

# keep both the smoothed slopes and the material erosion carved out of them
carved = r1 - e1
grain = fbm(Wc, Hc, 40, 4, gain=0.5, seed=37)
relief = (e1 - e1.mean()) / (e1.std() + 1e-5)
relief += 1.35 * ((carved - carved.mean()) / (carved.std() + 1e-5))
relief += 0.4 * ((e2 - e2.mean()) / (e2.std() + 1e-5))
relief += 0.05 * ((grain - grain.mean()) / (grain.std() + 1e-5))
relief *= 0.7 + 0.9 * smoothstep((fbm(Wc, Hc, 900, 3, seed=35) - 0.25) * 1.6)
relief = np.clip(relief, -3.0, 3.0)
# soften the relief texture without losing the tendril shapes
relief = box_blur(relief, 2, 2)

# mask: effects fade out towards the shore (distance used as an effect mask)
ease = smoothstep((d - L) / F)

# warp the deep field a little so no contour survives as a clean line
warp = (fbm(Wc, Hc, 500, 3, seed=41) - 0.5) * 7.0
yi, xi = np.mgrid[0:Hc, 0:Wc]
deep = deep[np.clip((yi + warp).astype(np.int32), 0, Hc - 1),
            np.clip((xi + warp * 0.7).astype(np.int32), 0, Wc - 1)]

# non-uniform blur on the BASE depth only, so the seabed relief survives while
# the big contours still get softened away from the shore
w_blur = 0.80 * ease
deep_b = deep * (1.0 - w_blur) + box_blur(deep, 2, 2) * w_blur
t = t_shore + (deep_b + 0.26 * relief - t_shore) * ease

t = np.where(land, 0.58, t)
t = np.clip(t, 0, 1)

print('relief std %.3f  t p5/p50/p95 %.3f %.3f %.3f' % (relief.std(), *np.percentile(t[~land], [5,50,95])))

# ---------------------------------------------------------------- shading
height = np.where(land, 0.0, relief * 1.0) * (0.35 + 0.65 * ease)
gy, gx = np.gradient(box_blur(height, 1, 1))
nrm = np.sqrt(gx * gx + gy * gy + 1.0)
L = np.array([-0.5, -0.6, 0.62]); L /= np.linalg.norm(L)
shade = np.clip(1.0 + 1.45 * (((-gx * L[0] - gy * L[1] + L[2]) / nrm) - 0.62), 0.76, 1.24)

stops = np.array([0.00, 0.03, 0.07, 0.12, 0.20, 0.32, 0.46, 0.66, 1.00], dtype=np.float32)
cols = np.array([
    [26, 54, 73], [25, 50, 69], [23, 47, 65], [20, 42, 59], [18, 38, 53],
    [16, 34, 49], [14, 29, 42], [11, 22, 33], [7, 13, 18],
], dtype=np.float32)

rgb = np.stack([np.interp(t, stops, cols[:, i]) for i in range(3)], axis=-1)
rgb = rgb * shade[:, :, None]

# separate coast pass: pure distance, max range 2-5 tiles driven by slow noise,
# added as a light additive blend so there is always a solid 1 px shoreline
r_rim = 2.0 + 3.0 * smoothstep((fbm(Wc, Hc, 1200, 3, seed=71) - 0.30) * 1.8)
rim = np.clip(1.0 - (d - 1.0) / r_rim, 0.0, 1.0) * (~land)
rgb = rgb + np.array([95.0, 155.0, 195.0]) * (0.13 * rim[:, :, None])
rgb = np.clip(rgb, 0, 255).astype(np.uint8)
Image.fromarray(rgb, "RGB").save(os.path.join(WORK, "v2_crop.png"))
print("wrote out/sea/v2_crop.png", rgb.shape[1], "x", rgb.shape[0])
