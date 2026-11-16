#!/usr/bin/env python3
"""Bake the full-map shader assets.

    python3 tools/bake_shader_assets.py --measure   # print the along max, write nothing
    python3 tools/bake_shader_assets.py             # write the four textures
    python3 tools/bake_shader_assets.py --check     # validate what is on disk

The maths is the prototype's (prototype/sea-shader/bake.py) with the crop
removed: distance -> along vector -> depth -> coarse flow -> detail (for the
speed only) -> speed. Every field is computed over the whole map.

Outputs, all under dist/:

    zigzag-sea-dist.webp   7749 x 5254   L   sqrt(clip(d, 0, 600) / 600)
    zigzag-sea-aux.webp    3875 x 2627   RGBA R depth, G/B along vector, A 255
    zigzag-sea-flow.webp    484 x 328    RGBA 16-tile gyre u/v, B coherence
    zigzag-sea-speed.webp  1937 x 1313   L   sp_coarse * keep_g, 4 tiles/texel

The along vector is stored as along / alongScale * 0.5 + 0.5, where alongScale
is frozen in src/sea-consts.json from `--measure` (the full-map maximum). The
shader must decode with the same number; a bake with the default 1.0 would
ship a clipped vector, so it refuses unless --allow-default-scale is passed.
"""
import argparse
import json
import os
import resource
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(ROOT, "out", "sea")

# The four canonical inputs (map raster, height, currents, reference render)
# live in data/ and are tracked with Git LFS, so a fresh clone builds without
# re-deriving them. A bare working copy still falls back to out/sea/.
DATA = os.path.join(ROOT, "data")


def data_path(name):
    p = os.path.join(DATA, name)
    return p if os.path.exists(p) else os.path.join(WORK, name)

DIST_DIR = os.path.join(ROOT, "dist")
CONSTS = os.path.join(ROOT, "src", "sea-consts.json")

sys.path.insert(0, HERE)
import sea_core as sc                                          # noqa: E402

DETAIL_STEP = 2          # tiles per texel for the detail pass (speed only)
COH_CELLS = 3.0          # coherence window, in 16-tile cells
SPD_CELLS = 2.0          # speed smoothing window, in 16-tile cells
ALONG_BLUR = 10          # distance blur (texels) for the along gradient
ALONG_PASSES = 2
GRAD_G0 = 6.0            # turn-rate knee, deg/tile
GRAD_POW = 3.0
GRAD_TILES = 4.0         # turn-rate blur, tiles

DIST_PATH = os.path.join(DIST_DIR, "zigzag-sea-dist.webp")
AUX_PATH = os.path.join(DIST_DIR, "zigzag-sea-aux.webp")
FLOW_PATH = os.path.join(DIST_DIR, "zigzag-sea-flow.webp")
SPEED_PATH = os.path.join(DIST_DIR, "zigzag-sea-speed.webp")
COLOUR_PATH = os.path.join(DIST_DIR, "zigzag-sea.webp")


def load_consts():
    with open(CONSTS) as f:
        return json.load(f)


def size(c):
    return int(c["map"]["w"]), int(c["map"]["h"])


def out_size(c, cell):
    """ceil(w/cell), ceil(h/cell) -- the aux grid rounds the odd width up."""
    w, h = size(c)
    return -(-w // cell), -(-h // cell)


def floor_size(c, cell):
    """floor(w/cell), floor(h/cell) -- the prototype's cw/SW for flow, speed."""
    w, h = size(c)
    return w // cell, h // cell


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def box_mean(a, r):
    """The prototype's edge-padded box mean (used on the coarse fields)."""
    k = 2 * r + 1
    p = np.pad(a, ((r, r), (r, r)), mode="edge")
    ii = p.cumsum(0).cumsum(1)
    ii = np.pad(ii, ((1, 0), (1, 0)))
    return (ii[k:, k:] - ii[:-k, k:] - ii[k:, :-k] + ii[:-k, :-k]) / (k * k)


def block_mean(a, ow, oh, step):
    """step x step block mean, replicating the last column/row if the map does
    not divide evenly (only the aux width does not: 7749 -> 3875)."""
    pad_h = oh * step - a.shape[0]
    pad_w = ow * step - a.shape[1]
    if pad_h > 0 or pad_w > 0:
        a = np.pad(a, ((0, max(0, pad_h)), (0, max(0, pad_w))), mode="edge")
    return a[:oh * step, :ow * step].reshape(oh, step, ow, step).mean(
        axis=(1, 3), dtype=np.float32)


def enc01(x):
    """0..1 float -> 8-bit, the prototype's rounding (truncate x*255+0.5)."""
    return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


def save_webp(arr, path, method=6):
    Image.fromarray(arr).save(path, lossless=True, quality=100, method=method)
    log(f"{os.path.basename(path)} {arr.shape[1]}x{arr.shape[0]} "
        f"{os.path.getsize(path) / 1e6:.2f} MB")


def land_mask(w, h):
    """map.png alpha > 128, interior holes filled by flooding from the border."""
    a = np.asarray(Image.open(data_path("map.png")).convert("RGBA"))[..., 3]
    land_raw = a > 128
    m8 = np.zeros(land_raw.shape, np.uint8)
    m8[land_raw] = 255
    m = Image.fromarray(m8, "L")
    dr = ImageDraw.Draw(m)
    border = ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
              + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)])
    for seed in border:
        if m.getpixel(seed) == 0:
            ImageDraw.floodfill(m, seed, 128)
    holes = np.asarray(m) == 0
    log(f"land holes filled: {int(holes.sum())}")
    return land_raw | holes


def along_raw(dist):
    """Rotate90 of the blurred distance gradient: (-d/dy, d/dx)."""
    db = sc.box_blur(dist, ALONG_BLUR, ALONG_PASSES)
    dby, dbx = np.gradient(db)
    del db
    np.negative(dby, out=dby)
    return dby, dbx


def depth_full(c, land):
    """1 - clamp((h - lo) / (hi - lo)), zeroed on land."""
    h = np.load(data_path("height_full.npy")).astype(np.float32)
    lo, hi = float(c["heightLo"]), float(c["heightHi"])
    d = 1.0 - np.clip((h - lo) / (hi - lo), 0.0, 1.0)
    del h
    d[land] = 0.0
    return d


def bilinear(a, ys, xs):
    ys = np.clip(ys, 0, a.shape[0] - 1.001)
    xs = np.clip(xs, 0, a.shape[1] - 1.001)
    y0_ = ys.astype(np.int32); x0_ = xs.astype(np.int32)
    fy = ys - y0_; fx = xs - x0_
    y1_ = np.minimum(y0_ + 1, a.shape[0] - 1)
    x1_ = np.minimum(x0_ + 1, a.shape[1] - 1)
    return ((1 - fy) * ((1 - fx) * a[y0_, x0_] + fx * a[y0_, x1_])
            + fy * ((1 - fx) * a[y1_, x0_] + fx * a[y1_, x1_]))


def coarse_flow(c, uv):
    """The raw 16-tile gyre plus the coherence that stands in for its speed."""
    fw, fh = floor_size(c, c["flowCell"])
    cu, cv = uv[0], uv[1]
    if (cu.shape[1], cu.shape[0]) != (fw, fh):
        cu = np.asarray(Image.fromarray(cu, "F").resize((fw, fh), Image.BILINEAR))
        cv = np.asarray(Image.fromarray(cv, "F").resize((fw, fh), Image.BILINEAR))
        m = np.hypot(cu, cv)
        m[m < 1e-6] = 1
        cu = cu / m; cv = cv / m
    coh_r = max(1, int(round(COH_CELLS)))          # the flow cell is native
    sp_r = max(1, int(round(SPD_CELLS)))
    coh = np.hypot(box_mean(cu, coh_r), box_mean(cv, coh_r))
    speed = np.clip((coh - 0.35) / (0.92 - 0.35), 0.0, 1.0)
    speed = box_mean(speed, sp_r)
    speed *= np.clip(np.hypot(cu, cv) * 4.0, 0.0, 1.0)
    log(f"coarse speed mean {float(speed.mean()):.2f} "
        f"standstill {float((speed < 0.1).mean()):.3f}")
    return cu.astype(np.float32), cv.astype(np.float32), speed.astype(np.float32)


def measure(c):
    dist = np.load(os.path.join(WORK, "dist_full.npy")).astype(np.float32)
    ax, ay = along_raw(dist)
    del dist
    amax = np.maximum(np.abs(ax), np.abs(ay))
    hi = float(amax.max())
    p99 = float(np.percentile(amax, 99))
    print(f"along-max={hi:.4f} along-p99={p99:.4f}", flush=True)


def bake(c, allow_default_scale):
    w, h = size(c)
    aux_scale = int(c["auxScale"])
    dist_max = float(c["distMax"])
    along_scale = float(c["alongScale"])
    deflect = float(c["baseline"]["deflect"])
    deflect_reach = float(c["baseline"]["deflectReach"])
    aw, ah = out_size(c, aux_scale)
    fw, fh = floor_size(c, c["flowCell"])
    sw, sh = floor_size(c, c["speedCell"])

    if along_scale <= 1.0 and not allow_default_scale:
        sys.exit("refusing to bake zigzag-sea-aux.webp: alongScale is still the "
                 "default 1.0 and the along vector would be clipped. Run "
                 "`python3 tools/bake_shader_assets.py --measure`, freeze the "
                 "printed max in src/sea-consts.json as alongScale, then bake. "
                 "(--allow-default-scale overrides.)")

    # ---- distance -------------------------------------------------------
    # zeroed on land, exactly as the prototype's aux.R was: the map raster's
    # alpha marks lakes as water, but the baker fills them into the land mass,
    # and leaving d > 0 there puts a shore ring inside the tiles.
    land = land_mask(w, h)
    dist = np.load(os.path.join(WORK, "dist_full.npy")).astype(np.float32)
    dist_tex = dist.copy()
    dist_tex[land] = 0.0
    dn = np.sqrt(np.clip(dist_tex, 0.0, dist_max) / dist_max)
    save_webp(enc01(dn), DIST_PATH, method=6)
    del dn, dist_tex

    # ---- along vector ---------------------------------------------------
    ax, ay = along_raw(dist)
    amax = np.maximum(np.abs(ax), np.abs(ay))
    log(f"along max {float(amax.max()):.4f} p99 {float(np.percentile(amax, 99)):.4f} "
        f"encoded with alongScale {along_scale}")
    del amax
    ax /= along_scale
    ay /= along_scale
    ax2 = block_mean(ax, aw, ah, aux_scale)
    ay2 = block_mean(ay, aw, ah, aux_scale)
    del ax, ay

    # ---- depth ----------------------------------------------------------
    dep = depth_full(c, land)
    dep2 = block_mean(dep, aw, ah, aux_scale)
    del dep

    aux = np.empty((ah, aw, 4), np.uint8)
    aux[..., 0] = enc01(dep2)
    aux[..., 1] = enc01(ax2 * 0.5 + 0.5)
    aux[..., 2] = enc01(ay2 * 0.5 + 0.5)
    aux[..., 3] = 255
    del dep2
    save_webp(aux, AUX_PATH)

    # the deflection blend uses exactly what the shader will decode
    alx = (aux[..., 1].astype(np.float32) / 255.0 * 2.0 - 1.0) * along_scale
    aly = (aux[..., 2].astype(np.float32) / 255.0 * 2.0 - 1.0) * along_scale

    # ---- coarse flow ----------------------------------------------------
    uv = np.load(data_path("currents_uv.npy"))
    cu, cv, csp = coarse_flow(c, uv)
    flow = np.empty((fh, fw, 4), np.uint8)
    flow[..., 0] = enc01(cu * 0.5 + 0.5)
    flow[..., 1] = enc01(cv * 0.5 + 0.5)
    flow[..., 2] = enc01(csp)
    flow[..., 3] = 255
    save_webp(flow, FLOW_PATH)

    # ---- detail, for the speed only -------------------------------------
    dw, dh = w // DETAIL_STEP, h // DETAIL_STEP
    log(f"detail {dw}x{dh} for the speed field")
    dist_s = block_mean(dist, dw, dh, DETAIL_STEP)
    near = deflect * np.exp(-dist_s / max(1.0, deflect_reach))
    del dist_s, dist

    gx = (np.arange(dw, dtype=np.float32) + 0.5) * DETAIL_STEP / float(c["flowCell"]) - 0.5
    gy = (np.arange(dh, dtype=np.float32) + 0.5) * DETAIL_STEP / float(c["flowCell"]) - 0.5
    du = bilinear(uv[0], gy[:, None], gx[None, :])
    dv = bilinear(uv[1], gy[:, None], gx[None, :])
    du2 = du * (1.0 - near) + alx[:, :dw] * near
    dv2 = dv * (1.0 - near) + aly[:, :dw] * near
    del du, dv, alx, aly, near, aux, uv

    sp = np.hypot(du2, dv2)
    mag = sp + 1e-6
    nu = du2 / mag
    nv = dv2 / mag
    del du2, dv2, mag, sp

    # a direction that turns fast shears the pattern even where the blend is
    # healthy, so bound travel * |grad theta|
    e = (nu + 1j * nv).astype(np.complex64)
    del nu, nv
    g1 = np.gradient(e, axis=1)
    g0 = np.gradient(e, axis=0)
    del e
    turn = np.hypot(np.abs(g1), np.abs(g0)).astype(np.float32)
    del g1, g0
    turn *= (180.0 / np.pi) / max(1, DETAIL_STEP)              # deg per tile
    turn = sc.box_blur(turn, max(1, int(round(GRAD_TILES / DETAIL_STEP))), 2)
    keep_g = 1.0 / (1.0 + (turn / max(1e-3, GRAD_G0)) ** GRAD_POW)
    del turn

    sp_coarse = bilinear(csp, gy[:, None], gx[None, :])
    spd = sp_coarse * keep_g                       # keep = 1 (calm patch off)
    del sp_coarse, keep_g

    # ---- speed at 4 tiles/texel -----------------------------------------
    k = max(1, c["speedCell"] // DETAIL_STEP)
    spd = block_mean(spd, sw, sh, k)
    save_webp(enc01(spd), SPEED_PATH, method=4)
    log(f"speed mean {float(spd.mean()):.3f} frac<0.5 {float((spd < 0.5).mean()):.3f}")

    total = os.path.getsize(COLOUR_PATH)
    for p in (DIST_PATH, AUX_PATH, FLOW_PATH, SPEED_PATH):
        total += os.path.getsize(p)
    log(f"download total {total / 1e6:.2f} MB "
        f"(colour {os.path.getsize(COLOUR_PATH) / 1e6:.2f} MB)")


def check(c):
    w, h = size(c)
    aux_scale = int(c["auxScale"])
    dist_max = float(c["distMax"])
    along_scale = float(c["alongScale"])
    aw, ah = out_size(c, aux_scale)
    fw, fh = floor_size(c, c["flowCell"])
    sw, sh = floor_size(c, c["speedCell"])

    dimg = Image.open(DIST_PATH)
    aimg = Image.open(AUX_PATH)
    fimg = Image.open(FLOW_PATH)
    simg = Image.open(SPEED_PATH)
    assert dimg.size == (w, h), (dimg.size, (w, h))
    assert aimg.size == (aw, ah), (aimg.size, (aw, ah))
    assert fimg.size == (fw, fh), (fimg.size, (fw, fh))
    assert simg.size == (sw, sh), (simg.size, (sw, sh))
    log(f"shapes ok: dist {dimg.size} aux {aimg.size} flow {fimg.size} speed {simg.size}")

    dist = np.load(os.path.join(WORK, "dist_full.npy")).astype(np.float32)
    land = land_mask(w, h)
    dist_tex = dist.copy()
    dist_tex[land] = 0.0
    dn_exact = np.sqrt(np.clip(dist_tex, 0.0, dist_max) / dist_max)
    del dist_tex
    dist8 = np.asarray(dimg.convert("L"))

    ax, ay = along_raw(dist)
    ax /= along_scale
    ay /= along_scale
    ax2 = block_mean(ax, aw, ah, aux_scale)
    ay2 = block_mean(ay, aw, ah, aux_scale)
    del ax, ay

    dep2 = block_mean(depth_full(c, land), aw, ah, aux_scale)

    aux = np.asarray(aimg.convert("RGBA"))
    rng = np.random.default_rng(20261114)
    ys = np.empty(0, np.int64); xs = np.empty(0, np.int64)
    while ys.size < 1000:
        y = rng.integers(0, h, 4096); x = rng.integers(0, w, 4096)
        ok = ~land[y, x]
        ys = np.concatenate([ys, y[ok]]); xs = np.concatenate([xs, x[ok]])
    ys, xs = ys[:1000], xs[:1000]

    # distance: the brief writes |sqrt(dist_webp*600) - dist_full| < 1, which is
    # dimensionally off (the texture stores dn = sqrt(d/600); the shader decodes
    # d = dn*dn*600). The one-code assertion in the stored space is the same
    # gate; the tile error is reported beside it (bound 2.36 at d = 600).
    dec = dist8[ys, xs].astype(np.float64) / 255.0
    code_err = np.abs(dec - dn_exact[ys, xs]) * 255.0
    tile_err = np.abs(dec * dec * dist_max - np.minimum(dist[ys, xs], dist_max))
    print(f"dist: 1000 water tiles, error mean {code_err.mean():.3f} "
          f"max {code_err.max():.3f} codes; "
          f"tiles mean {tile_err.mean():.3f} max {tile_err.max():.3f}")
    assert code_err.max() <= 1.0 + 1e-6, code_err.max()

    ayi = np.minimum(ys // aux_scale, ah - 1)
    axi = np.minimum(xs // aux_scale, aw - 1)
    ddec = aux[ayi, axi, 0].astype(np.float64)
    derr = np.abs(ddec - dep2[ayi, axi].astype(np.float64) * 255.0)
    print(f"depth: error mean {derr.mean():.3f} max {derr.max():.3f} codes")
    assert derr.max() <= 2.0 + 1e-6, derr.max()

    # ax2/ay2 are the block means of the *scaled* along vector, i.e. the values
    # the baker encoded. Decode to the same units and compare; report the raw
    # magnitude (decoded * alongScale) beside it.
    alx = aux[ayi, axi, 1].astype(np.float64) / 255.0 * 2.0 - 1.0
    aly = aux[ayi, axi, 2].astype(np.float64) / 255.0 * 2.0 - 1.0
    aerr = np.hypot(alx - ax2[ayi, axi], aly - ay2[ayi, axi])
    mag = np.hypot(alx, aly) * along_scale
    print(f"along: error mean {aerr.mean():.4f} max {aerr.max():.4f}; "
          f"raw |along| p1 {np.percentile(mag, 1):.3f} "
          f"median {np.median(mag):.3f} p99 {np.percentile(mag, 99):.3f}")
    assert aerr.max() <= 0.02, aerr.max()

    total = os.path.getsize(COLOUR_PATH)
    for p in (DIST_PATH, AUX_PATH, FLOW_PATH, SPEED_PATH):
        print(f"  {os.path.basename(p):24s} {os.path.getsize(p) / 1e6:.2f} MB")
        total += os.path.getsize(p)
    print(f"download total {total / 1e6:.2f} MB "
          f"(colour {os.path.getsize(COLOUR_PATH) / 1e6:.2f} MB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true",
                    help="print the full-map along max and p99, write nothing")
    ap.add_argument("--check", action="store_true",
                    help="validate the written textures instead of writing")
    ap.add_argument("--allow-default-scale", action="store_true",
                    help="bake with alongScale 1.0 (clips the along vector)")
    args = ap.parse_args()

    c = load_consts()
    t = time.time()
    if args.measure:
        measure(c)
        return
    if args.check:
        check(c)
    else:
        bake(c, args.allow_default_scale)
    log(f"done in {time.time() - t:.0f}s "
        f"peak rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")


if __name__ == "__main__":
    main()
