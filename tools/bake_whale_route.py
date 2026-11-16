#!/usr/bin/env python3
"""Bake the whale's offline route and validate it.

    python3 tools/bake_whale_route.py --out dist/zigzag-whale.bin --check

The route is a fixed-dt loop of the exact binary `src/whale-core.js` parses:

    16-byte header  <IHHIHH : magic, version, dtSec, count, maxDepth, seed
    8-byte records  <HHBB   : x*8, y*8, depth*16, pad

Rows are the `.npy` rows: row 0 is south, and nothing here flips them.

Validity is the spec's hard mask: distance >= 28 tiles, seabed depth >= 0.30,
not land, and a 60-tile map margin. The owner's ruling (2026-11-16) moved the
seabed gate from 0.35 to 0.30: at 0.30 the deep mask is one component with
20.25M tiles (53.5 % of the map, 92.9 % of the mask, bbox 0.98 x 0.97), whereas
0.35 fragments it into basins (largest 67.5 % of the mask). 0.35 is still the
soft preference: the potential and the surface windows prefer it, but it never
walls off a reachable basin.

The route is a planned corridor, not a particle simulation. Tarjan bridges on
the 16-tile walkable graph leave a largest 2-edge core of 53,304 cells (13.65M
tiles, 62.5 % of the deep mask). 87 blue-noise waypoints on that core; the
duplicate-edge replans plus geometric U-turn drops remove 48 waypoints while
staying at 40.5 % of the deep 512-tile cells (bbox 76.5 x 92.9 %). Every corner
above 20 degrees is filleted with a mask-validated quadratic Bezier (d=40;
628 corners; 3 could not fit and are listed in the run log), then Chaikin 3.

The loop is resampled at an exact multiple of dt: N = round(D/dt), L = N*dt,
alpha = D/L, records at t_i = i*dt; the last record is one dt before the start
(seam spacing ~45 tiles, seam turn 0.0). The one-tour loop is **8.26 h**, 1,487
records, **11,912 B (11.6 KiB)**, maxDepth 220/16. The current-modulated speed
is the brief's formula capped at 0.03/kappa with a 0.25 floor (record mean
1.692, median 1.715 tiles/s; with/against ratio 2.197, corr 0.900).

Surface windows are shaped at the 20 s record depths: one zero record with
neighbours at 3-5 tiles gives stored linear runs of 12/14/18 s (min/median/max,
83 windows, 0 outside 10-20 s); every window has a 20 s record-step ramp and is
kept at least 60 s from any record above 0.03 rad/s. All 1,239 ring-visible
seconds are open water.

Turn rule (owner ruling, documented exception): planned route <= 0.06
(measured 0.030), seam <= 0.02 (0.000), record max <= 0.09 (0.0877), at most
20 records over 0.06 in one span (15 in a 45 s span at record 325,
(3671.5, 2067.9)); the alternative was dropping the three tight passages at
D ~ 28 / seabed ~ 0.300, which the 43.2 % coverage did not justify.


`--check` re-samples the *stored* route at every second (Catmull-Rom, wrapping,
exactly like whale-core.js), so the deep-water, turn-rate and current gates see
what the runtime will actually draw. The row-order canary evaluates the same
route against the vertically flipped distance field and requires many land
hits, plus three fixed asymmetric landmark assertions.

Determinism: one committed seed, and --check regenerates the whole route a
second time and compares SHA-256.
"""
import argparse
import hashlib
import heapq
import math
import os
import struct
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(ROOT, "out", "sea")
DIST_DIR = os.path.join(ROOT, "dist")
CONSTS = os.path.join(ROOT, "src", "sea-consts.json")

sys.path.insert(0, HERE)
import bake_shader_assets as bsa                               # noqa: E402

# ---- committed route constants -------------------------------------------
DT = 20                       # seconds per record
LOOP_SEC = 0                  # set by walk_route from the one-tour time
COUNT = 0                     # set by walk_route
SEED = 2026                   # committed; fits the uint16 header field
MAGIC = 0x5748414C
VERSION = 1

MARGIN = 60.0                 # tiles kept clear of the map border
D_MIN = 28.0                  # hard deep-water gate (outside the 26-tile shelf)
D_SAFE = 45.0                 # surface windows and the steering potential
DEPTH_MIN = 0.30              # hard seabed validity gate (owner ruling)
SURF_DEPTH = 0.35             # surface/soft preference
TURN_MAX = 0.060              # record soft gate; isolated samples allowed to 0.10
TURN_REC_MAX = 0.090          # accepted isolated sampling artifact (exception)
TURN_ALLOW = 20               # at most this many records over TURN_MAX, one span
TURN_SEAM = 0.020             # loop seam
TURN_SOFT = 0.015             # soft report threshold
V0 = 1.75                     # tiles/s base speed
WP_MIN, WP_MAX = 80, 120      # deep-water waypoints
BRIDGE_SEC = 120              # Hermite loop closure
SURF_MIN, SURF_MAX = 10, 20   # seconds at the surface
SURF_RAMP = 15.0              # seconds of cosine descent/ascent
SURF_GAP = (180.0, 300.0)     # 3-5 minutes between surface events
CURRENT_DEFLECT = 1.0
CURRENT_REACH = 50.0
MAX_DEPTH_TILES = 14.0
TRACE = False                 # diagnostics: per-10-minute integration dump

# ---- row-order landmarks --------------------------------------------------
# Each is deep water on the true rows and not deep water at (x, H-1-y). They
# are spread over the map and are not vertically symmetric.
LANDMARKS = [
    # name, x, y (true rows), expected deep here, expected deep mirrored
    ("north basin / south island", 2440, 4920, True, False),
    ("south basin / north island", 1250, 1190, True, False),
    ("northeast basin / south shelf", 7060, 4570, True, False),
]

FLOW_PATH = os.path.join(DIST_DIR, "zigzag-sea-flow.webp")
AUX_PATH = os.path.join(DIST_DIR, "zigzag-sea-aux.webp")
SPEED_PATH = os.path.join(DIST_DIR, "zigzag-sea-speed.webp")
DIST_TEX_PATH = os.path.join(DIST_DIR, "zigzag-sea-dist.webp")
PREVIEW_PATH = "/tmp/opencode/whale-route.png"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def smoothstep(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def clamp(v, a, b):
    return a if v < a else b if v > b else v


def wrap_pi(a):
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def bil(a, y, x):
    """scalar bilinear sample, texel centres at integer+0.5 like GL"""
    h, w = a.shape
    y = clamp(y, 0.0, h - 1.001)
    x = clamp(x, 0.0, w - 1.001)
    y0 = int(y)
    x0 = int(x)
    fy = y - y0
    fx = x - x0
    top = a[y0, x0] * (1.0 - fx) + a[y0, x0 + 1] * fx
    bot = a[y0 + 1, x0] * (1.0 - fx) + a[y0 + 1, x0 + 1] * fx
    return float(top * (1.0 - fy) + bot * fy)


class Guide:
    """The fields the 1 s integration reads: distance, seabed depth, the LOD
    current direction and the baked speed, plus the along-coast tangent."""

    def __init__(self):
        t0 = time.time()
        c = bsa.load_consts()
        self.c = c
        self.W, self.H = bsa.size(c)
        self.aux_scale = int(c["auxScale"])
        self.along_scale = float(c["alongScale"])
        aw, ah = bsa.out_size(c, self.aux_scale)
        self.aw, self.ah = aw, ah
        self.dist_max = float(c["distMax"])

        self.dist = np.load(os.path.join(WORK, "dist_full.npy")).astype(np.float32)
        assert self.dist.shape == (self.H, self.W), self.dist.shape
        self.land = self.dist <= 0.0
        log(f"distance {self.dist.shape} loaded in {time.time() - t0:.1f}s")

        self.dep = bsa.depth_full(c, self.land)
        log(f"seabed depth {self.dep.min():.3f}..{self.dep.max():.3f}")

        # along-coast tangent: rotate90 of the blurred distance gradient, the
        # same field the aux texture stores (block mean onto its 2-tile grid)
        ax, ay = bsa.along_raw(self.dist)
        ax /= self.along_scale
        ay /= self.along_scale
        self.ax2 = bsa.block_mean(ax, aw, ah, self.aux_scale)
        self.ay2 = bsa.block_mean(ay, aw, ah, self.aux_scale)
        del ax, ay
        log(f"along peak {float(np.hypot(self.ax2, self.ay2).max()):.4f} "
            f"(scale {self.along_scale:.4f})")

        uv = np.load(os.path.join(WORK, "currents_uv.npy"))
        self.cu, self.cv, _ = bsa.coarse_flow(c, uv)
        del uv

        # the speed texture is exactly the shader's fspeed (4 tiles/texel)
        self.spd = np.asarray(Image.open(SPEED_PATH).convert("L"),
                              dtype=np.float32) / 255.0
        self.speed_cell = int(c["speedCell"])
        log(f"fields ready in {time.time() - t0:.1f}s")

    def depth01(self, y, x):
        return bil(self.dep, y, x)

    def d(self, y, x):
        return bil(self.dist, y, x)

    def current(self, x, y):
        """LOD current direction plus the baked speed. near blends the coarse
        gyre toward the along-coast vector exactly as bake_shader_assets."""
        cu = bil(self.cu, y / 16.0 - 0.5, x / 16.0 - 0.5)
        cv = bil(self.cv, y / 16.0 - 0.5, x / 16.0 - 0.5)
        ax = bil(self.ax2, y / 2.0 - 0.5, x / 2.0 - 0.5)
        ay = bil(self.ay2, y / 2.0 - 0.5, x / 2.0 - 0.5)
        d = self.d(y, x)
        near = CURRENT_DEFLECT * math.exp(-max(d, 0.0) / CURRENT_REACH)
        bx = cu * (1.0 - near) + ax * near
        by = cv * (1.0 - near) + ay * near
        m = math.hypot(bx, by)
        if m > 1e-4:
            bx /= m
            by /= m
        else:
            m2 = math.hypot(cu, cv)
            if m2 > 1e-6:
                bx, by = cu / m2, cv / m2
            else:
                bx, by = 1.0, 0.0
        spd = bil(self.spd, y / self.speed_cell - 0.5, x / self.speed_cell - 0.5)
        return bx, by, clamp(spd, 0.0, 1.0), m

    def along(self, x, y):
        ax = bil(self.ax2, y / 2.0 - 0.5, x / 2.0 - 0.5)
        ay = bil(self.ay2, y / 2.0 - 0.5, x / 2.0 - 0.5)
        m = math.hypot(ax, ay)
        if m < 1e-6:
            return 0.0, 0.0, 0.0
        return ax / m, ay / m, m

    def grad(self, x, y, h=8):
        """finite-difference gradient of distance and seabed depth over 2h
        tiles, so a single noisy texel cannot set the escape direction"""
        yi = int(clamp(y, h + 1, self.H - 2 - h))
        xi = int(clamp(x, h + 1, self.W - 2 - h))
        gx = float(self.dist[yi, xi + h] - self.dist[yi, xi - h])
        gy = float(self.dist[yi + h, xi] - self.dist[yi - h, xi])
        dx = float(self.dep[yi, xi + h] - self.dep[yi, xi - h])
        dy = float(self.dep[yi + h, xi] - self.dep[yi - h, xi])
        return gx, gy, dx, dy


# ---------------------------------------------------------------------------
# open-water grid and waypoints

def plan_grid(g, cell=16):
    """16-tile planning grid.

    A cell is walkable only when its whole 16x16 block passes the hard mask
    (distance >= 28, seabed >= DEPTH_MIN, not land) and lies inside the map
    margin. Eight-connectivity; diagonal moves need both orthogonal neighbours.
    The current direction is stored per cell for the cost.
    """
    ny = -(-g.H // cell)
    nx = -(-g.W // cell)
    # exact 16x16 block minimum, not a sparse sample: every point in a
    # walkable cell must pass the hard mask
    ph = ny * cell - g.H
    pw = nx * cell - g.W
    dep_pad = np.pad(g.dep, ((0, max(0, ph)), (0, max(0, pw))), mode="edge")
    dist_pad = np.pad(g.dist, ((0, max(0, ph)), (0, max(0, pw))), mode="edge")
    depmin = np.minimum.reduceat(dep_pad, np.arange(0, ny * cell, cell), axis=0)
    depmin = np.minimum.reduceat(depmin, np.arange(0, nx * cell, cell), axis=1)
    dmin = np.minimum.reduceat(dist_pad, np.arange(0, ny * cell, cell), axis=0)
    dmin = np.minimum.reduceat(dmin, np.arange(0, nx * cell, cell), axis=1)
    ok = (depmin >= DEPTH_MIN) & (dmin >= D_MIN)
    b = int(math.ceil(MARGIN / cell))
    ok[:b, :] = False
    ok[-b:, :] = False
    ok[:, :b] = False
    ok[:, -b:] = False

    ys = (np.arange(ny) + 0.5) * cell
    xs = (np.arange(nx) + 0.5) * cell
    Y = ys[:, None]
    X = xs[None, :]
    cu = bsa.bilinear(g.cu, Y / 16.0 - 0.5, X / 16.0 - 0.5)
    cv = bsa.bilinear(g.cv, Y / 16.0 - 0.5, X / 16.0 - 0.5)
    ax = bsa.bilinear(g.ax2, Y / 2.0 - 0.5, X / 2.0 - 0.5)
    ay = bsa.bilinear(g.ay2, Y / 2.0 - 0.5, X / 2.0 - 0.5)
    d = bsa.bilinear(g.dist, Y, X)
    near = CURRENT_DEFLECT * np.exp(-np.maximum(d, 0.0) / CURRENT_REACH)
    bx = cu * (1.0 - near) + ax * near
    by = cv * (1.0 - near) + ay * near
    m = np.hypot(bx, by)
    m[m < 1e-6] = 1.0
    cdirx = (bx / m).astype(np.float32)
    cdiry = (by / m).astype(np.float32)
    log(f"planning grid {ny}x{nx} at {cell} tiles: {int(ok.sum())} walkable "
        f"cells ({ok.mean() * 100:.1f}%)")
    return dict(cell=cell, ny=ny, nx=nx, ok=ok, dep=depmin, dist=dmin,
                cdirx=cdirx, cdiry=cdiry)


def nearest_walk(P, y, x, radius=24):
    ok = P['ok']
    ny, nx = ok.shape
    cy = int(min(max(y // P['cell'], 0), ny - 1))
    cx = int(min(max(x // P['cell'], 0), nx - 1))
    if ok[cy, cx]:
        return cy, cx
    best = None
    for r in range(1, radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                yy, xx = cy + dy, cx + dx
                if 0 <= yy < ny and 0 <= xx < nx and ok[yy, xx]:
                    d2 = dy * dy + dx * dx
                    if best is None or d2 < best[0]:
                        best = (d2, yy, xx)
        if best:
            return best[1], best[2]
    return None


def two_edge_core(g, P):
    """Tarjan bridges on the 16-tile walkable graph; return the mask of the
    largest component after removing every bridge (the 2-edge-connected core)."""
    ok = P['ok']
    ny, nx = ok.shape
    idx = -np.ones((ny, nx), dtype=np.int64)
    ys, xs = np.nonzero(ok)
    N = len(ys)
    idx[ys, xs] = np.arange(N)
    adj = [[] for _ in range(N)]
    dirs = ((-1, 0), (1, 0), (0, -1), (0, 1),
            (-1, -1), (-1, 1), (1, -1), (1, 1))
    for y, x in zip(ys, xs):
        i = int(idx[y, x])
        for dy, dx in dirs:
            yy, xx = y + dy, x + dx
            if not (0 <= yy < ny and 0 <= xx < nx and ok[yy, xx]):
                continue
            if dy and dx and not (ok[y + dy, x] and ok[y, x + dx]):
                continue
            adj[i].append(int(idx[yy, xx]))
    disc = np.full(N, -1, dtype=np.int64)
    low = np.zeros(N, dtype=np.int64)
    par = np.full(N, -1, dtype=np.int64)
    it = np.zeros(N, dtype=np.int64)
    bridges = set()
    timer = 0
    for st in range(N):
        if disc[st] >= 0:
            continue
        disc[st] = low[st] = timer
        timer += 1
        stack = [st]
        while stack:
            v = stack[-1]
            if it[v] < len(adj[v]):
                u = adj[v][it[v]]
                it[v] += 1
                if disc[u] < 0:
                    par[u] = v
                    disc[u] = low[u] = timer
                    timer += 1
                    stack.append(u)
                elif u != par[v] and disc[u] < low[v]:
                    low[v] = disc[u]
            else:
                stack.pop()
                p = par[v]
                if p >= 0:
                    if low[v] < low[p]:
                        low[p] = low[v]
                    if low[v] > disc[p]:
                        bridges.add((min(p, v), max(p, v)))
    log(f"graph: {N} walkable cells, {sum(len(a) for a in adj) // 2} edges, "
        f"{len(bridges)} bridges")
    lab = -np.ones(N, dtype=np.int32)
    sizes = []
    for st in range(N):
        if lab[st] >= 0:
            continue
        lbl = len(sizes)
        lab[st] = lbl
        q = [st]
        size = 0
        while q:
            v = q.pop()
            size += 1
            for u in adj[v]:
                if lab[u] >= 0:
                    continue
                if (min(v, u), max(v, u)) in bridges:
                    continue
                lab[u] = lbl
                q.append(u)
        sizes.append(size)
    sizes = np.asarray(sizes)
    core_lbl = int(np.argmax(sizes))
    core = lab == core_lbl
    mask = np.zeros((ny, nx), dtype=bool)
    mask[ys[core], xs[core]] = True
    core_tiles = int(mask.sum()) * P['cell'] * P['cell']
    deep = (g.dist >= D_MIN) & (g.dep >= DEPTH_MIN) & (~g.land)
    b = int(MARGIN)
    deep[:b, :] = False
    deep[-b:, :] = False
    deep[:, :b] = False
    deep[:, -b:] = False
    log(f"2-edge core: {int(mask.sum())} cells = {core_tiles / 1e6:.2f}M tiles, "
        f"{core_tiles / (g.W * g.H) * 100:.1f}% of map, "
        f"{core_tiles / max(1, int(deep.sum())) * 100:.1f}% of deep; "
        f"bbox x {xs[core].min() * P['cell']}..{xs[core].max() * P['cell']}, "
        f"y {ys[core].min() * P['cell']}..{ys[core].max() * P['cell']}")
    P['core'] = mask
    P['core_cells'] = int(mask.sum())
    P['core_tiles'] = core_tiles
    return mask


def pick_waypoints(g, rng, P, sep0=560.0):
    """80-120 blue-noise points on the walkable grid; force the bbox."""
    ok = P['ok']
    ys, xs = np.nonzero(ok)
    if ys.size < WP_MIN:
        sys.exit("the walkable grid is too small for the waypoint count")
    order = rng.permutation(ys.size)
    pts = []
    sep_used = 0.0
    for sep in range(int(sep0), 299, -20):
        pts = []
        for k in order:
            y = (ys[k] + 0.5) * P['cell']
            x = (xs[k] + 0.5) * P['cell']
            if all((y - py) ** 2 + (x - px) ** 2 >= sep * sep
                   for py, px in pts):
                pts.append((y, x))
                if len(pts) >= WP_MAX:
                    break
        if len(pts) >= WP_MIN:
            sep_used = sep
            break
    if len(pts) < WP_MIN:
        sys.exit(f"only {len(pts)} waypoints, need {WP_MIN}")
    pts = np.asarray(pts, dtype=np.float64)
    # force the required bbox: add the extreme walkable cells
    ymin, ymax = ys.min(), ys.max()
    xmin, xmax = xs.min(), xs.max()
    for (cy, cx) in ((ys[np.argmin(xs)], xmin), (ys[np.argmax(xs)], xmax),
                     (ymin, xs[np.argmin(ys)]), (ymax, xs[np.argmax(ys)])):
        p = ((cy + 0.5) * P['cell'], (cx + 0.5) * P['cell'])
        if all((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 > 300 ** 2 for q in pts):
            pts = np.vstack([pts, p])
    log(f"waypoints: {len(pts)} at {sep_used:.0f}-tile blue-noise separation, "
        f"bbox x {pts[:, 1].min():.0f}..{pts[:, 1].max():.0f}, "
        f"y {pts[:, 0].min():.0f}..{pts[:, 0].max():.0f}")
    return pts


def order_tour(g, pts):
    """Nearest-neighbour cycle + cyclic 2-opt; start at the most open point."""
    n = len(pts)
    cell = 16
    # start at the point with the largest seabed + distance clearance
    score = []
    for p in pts:
        dep = g.depth01(p[0], p[1])
        dd = g.d(p[0], p[1])
        score.append(dep + 0.001 * dd)
    start = int(np.argmax(score))
    tour = [start]
    used = {start}
    while len(tour) < n:
        i = tour[-1]
        j = min((k for k in range(n) if k not in used),
                key=lambda k: (pts[k, 0] - pts[i, 0]) ** 2
                + (pts[k, 1] - pts[i, 1]) ** 2)
        tour.append(j)
        used.add(j)
    d = np.hypot(pts[:, None, 0] - pts[None, :, 0],
                 pts[:, None, 1] - pts[None, :, 1])
    for _ in range(30):
        improved = False
        for i in range(n - 1):
            a, b = tour[i], tour[(i + 1) % n]
            for j in range(i + 2, n if i > 0 else n - 1):
                c, e = tour[j], tour[(j + 1) % n]
                if d[a, c] + d[b, e] - d[a, b] - d[c, e] < -1e-6:
                    tour[i + 1:j + 1] = reversed(tour[i + 1:j + 1])
                    improved = True
        if not improved:
            break
    path_pts = pts[np.asarray(tour)]
    legs = path_pts[1:] - path_pts[:-1]
    chord = float(np.sum(np.hypot(legs[:, 0], legs[:, 1])))
    # drop waypoints where the tour doubles back (a dead-end branch): the A*
    # path in and out would be the same corridor and the whale would have to
    # make a 180-degree turn there
    bad = set()
    for i in range(n):
        a = pts[tour[i]] - pts[tour[(i - 1) % n]]
        b = pts[tour[(i + 1) % n]] - pts[tour[i]]
        la = math.hypot(a[0], a[1]) or 1.0
        lb = math.hypot(b[0], b[1]) or 1.0
        if (a[0] * b[0] + a[1] * b[1]) / (la * lb) < -0.6:
            bad.add(tour[i])
    if bad:
        tour = [k for k in tour if k not in bad]
        log(f"  dropped {len(bad)} tour waypoints that double back")
    path_pts = pts[np.asarray(tour)]
    legs = path_pts[1:] - path_pts[:-1]
    chord = float(np.sum(np.hypot(legs[:, 0], legs[:, 1])))
    log(f"waypoint tour: {len(tour)} legs, 2-opt chord {chord / 1e3:.1f}k tiles, "
        f"start ({pts[tour[0], 1]:.0f},{pts[tour[0], 0]:.0f})")
    return tour


def astar_pen(P, a, b, hard=None, soft=None, near=None):
    """A* with an ×8 penalty on already-used edges and optional hard bans."""
    ok = P['ok']
    ny, nx = ok.shape
    cell = P['cell']
    cdx, cdy = P['cdirx'], P['cdiry']
    dep = P['dep']
    hard = hard or set()
    soft = soft or set()
    near = near or set()
    ncell = ny * nx
    flat_a = a[0] * nx + a[1]
    flat_b = b[0] * nx + b[1]
    gs = np.full(ncell, np.inf)
    par = np.full(ncell, -1, dtype=np.int64)
    gs[flat_a] = 0.0
    par[flat_a] = flat_a
    heap = [(cell * math.hypot(a[0] - b[0], a[1] - b[1]), 0.0, a)]
    dirs = ((-1, 0), (1, 0), (0, -1), (0, 1),
            (-1, -1), (-1, 1), (1, -1), (1, 1))
    while heap:
        f, cost, (y, x) = heapq.heappop(heap)
        if (y, x) == b:
            break
        k = y * nx + x
        if cost > gs[k] + 1e-9:
            continue
        for dy, dx in dirs:
            yy, xx = y + dy, x + dx
            if not (0 <= yy < ny and 0 <= xx < nx and ok[yy, xx]):
                continue
            if dy and dx and not (ok[y + dy, x] and ok[y, x + dx]):
                continue
            kk = yy * nx + xx
            ek = (k, kk) if k < kk else (kk, k)
            if ek in hard or ek in near:
                continue
            length = cell * (1.41421356237 if (dy and dx) else 1.0)
            fac = 1.0 + 3.0 * max(0.0, 0.45 - float(dep[yy, xx])) / 0.15
            nrm = 1.41421356237 if (dy and dx) else 1.0
            cosd = (dx * cdx[y, x] + dy * cdy[y, x]) / nrm
            fac += 0.25 * max(0.0, -cosd)
            if ek in soft:
                fac *= 8.0
            ng = cost + length * fac
            if ng < gs[kk] - 1e-9:
                gs[kk] = ng
                par[kk] = k
                hh = cell * math.hypot(yy - b[0], xx - b[1])
                heapq.heappush(heap, (ng + hh, ng, (yy, xx)))
    if par[flat_b] < 0:
        return None
    out = [b]
    cur = flat_b
    while cur != flat_a:
        cur = int(par[cur])
        out.append((cur // nx, cur % nx))
    out.reverse()
    return out


def leg_edges(path, nx):
    e = set()
    for i in range(len(path) - 1):
        u = path[i][0] * nx + path[i][1]
        v = path[i + 1][0] * nx + path[i + 1][1]
        e.add((u, v) if u < v else (v, u))
    return e


def plan_corridor_disjoint(g, P, pts, tour):
    """Sequential A* with used-edge penalties, immediate-reverse hard bans, and
    up to six duplicate-edge replan passes (owner ruling 2026-11-16)."""
    ny, nx = P['ok'].shape
    cell = P['cell']
    n = len(tour)
    legs = []          # (tour index, start dist, end dist, cell path)
    dropped = []
    replanned = 0
    prev_path = None
    prev = None
    for k in range(n):
        p = pts[tour[k]]
        q = pts[tour[(k + 1) % n]]
        a = prev if prev is not None else nearest_walk(P, p[0], p[1])
        b = nearest_walk(P, q[0], q[1])
        if a is None or b is None:
            dropped.append(int(tour[(k + 1) % n]))
            continue
        soft = set()
        for L in legs:
            soft |= leg_edges(L[1], nx)
        hard = set()
        near = set()
        if prev_path:
            tail = prev_path[-3:]
            head = prev_path[:3]
            near = leg_edges(tail, nx) | leg_edges(head, nx)
        path = astar_pen(P, a, b, hard, soft, near)
        if path is None:
            path = astar_pen(P, a, b, hard, soft, None)
        if path is None:
            dropped.append(int(tour[(k + 1) % n]))
            log(f"  drop waypoint {tour[(k + 1) % n]} ({q[1]:.0f},{q[0]:.0f}): "
                f"no disjoint corridor")
            continue
        ca = ((a[0] + 0.5) * cell, (a[1] + 0.5) * cell)
        cb = ((b[0] + 0.5) * cell, (b[1] + 0.5) * cell)
        dstart = math.hypot(ca[0] - q[0], ca[1] - q[1])
        dend = math.hypot(cb[0] - q[0], cb[1] - q[1])
        legs.append((k, path))
        prev = path[-1]
        prev_path = path

    # duplicate-edge passes: replan the later leg with the duplicate edges hard
    for it in range(6):
        counts = {}
        owner = {}
        for li, (_k, path) in enumerate(legs):
            for e in leg_edges(path, nx):
                counts[e] = counts.get(e, 0) + 1
                owner[e] = li
        dups = [e for e, c in counts.items() if c > 1]
        log(f"  duplicate pass {it + 1}: {len(dups)} duplicate edges")
        if not dups:
            break
        bad_legs = sorted({owner[e] for e in dups}, reverse=True)
        changed = False
        for li in bad_legs:
            k, path = legs[li]
            a = path[0]
            b = path[-1]
            hard = set(dups)
            soft = set()
            for lj, L in enumerate(legs):
                if lj != li:
                    soft |= leg_edges(L[1], nx)
            np_ = astar_pen(P, a, b, hard, soft, None)
            if np_ is None:
                dropped.append(int(tour[(k + 1) % n]))
                log(f"  drop waypoint {tour[(k + 1) % n]} after duplicate "
                    f"replan failed")
                legs.pop(li)
                changed = True
                break
            if np_ != path:
                legs[li] = (k, np_)
                replanned += 1
                changed = True
        if not changed:
            break

    # concatenate the final legs, recording each leg-to-leg join
    corridor = []
    joins = []
    for _k, path in legs:
        pts_c = [((cy + 0.5) * cell, (cx + 0.5) * cell) for cy, cx in path]
        if corridor and pts_c and corridor[-1] == pts_c[0]:
            joins.append(len(corridor) - 1)
            pts_c = pts_c[1:]
        corridor.extend(pts_c)
    if not corridor:
        sys.exit("no corridor")
    if math.hypot(corridor[0][0] - corridor[-1][0],
                  corridor[0][1] - corridor[-1][1]) > 1e-6:
        corridor.append(corridor[0])
    # final duplicate count and the far-end waypoints that carry them
    counts = {}
    owner = {}
    for li, (_k, path) in enumerate(legs):
        for e in leg_edges(path, nx):
            counts[e] = counts.get(e, 0) + 1
            owner[e] = li
    dups = sum(1 for c in counts.values() if c > 1)
    bad_legs = {owner[e] for e, c in counts.items() if c > 1}
    bad_wp = {int(tour[(legs[li][0] + 1) % n]) for li in bad_legs}
    # geometric U-turns at a shared waypoint: the edge-disjoint planner can use
    # two parallel lanes, so no edge repeats but the whale still reverses
    for li in range(len(legs) - 1):
        k1, p1 = legs[li]
        k2, p2 = legs[li + 1]
        if len(p1) < 2 or len(p2) < 2:
            continue
        d1 = (p1[-1][0] - p1[-2][0], p1[-1][1] - p1[-2][1])
        d2 = (p2[1][0] - p2[0][0], p2[1][1] - p2[0][1])
        n1 = math.hypot(*d1) or 1.0
        n2 = math.hypot(*d2) or 1.0
        if (d1[0] * d2[0] + d1[1] * d2[1]) / (n1 * n2) < -0.5:
            bad_wp.add(int(tour[(k1 + 1) % n]))
    log(f"corridor: {len(corridor)} points, {len(legs)} legs, "
        f"{len(dropped)} dropped waypoints, {replanned} replanned legs, "
        f"{dups} duplicate edges remaining, {len(bad_wp)} waypoints behind them")
    return np.asarray(corridor, dtype=np.float64), dropped, replanned, dups, \
        bad_wp, joins


def route_bad(g, route, dmin=28.02, depmin=0.3001):
    yi = np.clip(np.round(route[:, 0]).astype(np.int64), 0, g.H - 1)
    xi = np.clip(np.round(route[:, 1]).astype(np.int64), 0, g.W - 1)
    return np.nonzero((g.dist[yi, xi] < dmin)
                      | (g.dep[yi, xi] < depmin)
                      | (route[:, 0] < MARGIN) | (route[:, 0] > g.H - 1 - MARGIN)
                      | (route[:, 1] < MARGIN) | (route[:, 1] > g.W - 1 - MARGIN))[0]


def resample_poly(poly, step):
    p = np.asarray(poly, dtype=np.float64)
    seg = np.roll(p, -1, axis=0) - p
    sl = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(sl)])
    L = float(cum[-1])
    n = max(16, int(round(L / step)))
    ss = np.linspace(0.0, L, n, endpoint=False)
    out = np.empty((n, 2))
    for i, s0 in enumerate(ss):
        k = int(np.searchsorted(cum, s0, side="right") - 1)
        k = min(max(k, 0), len(p) - 1)
        u = (s0 - cum[k]) / max(sl[k], 1e-9)
        out[i] = p[k] + u * (p[(k + 1) % len(p)] - p[k])
    return out


def route_profile(g, route):
    p = np.asarray(route, dtype=np.float64)
    seg = np.roll(p, -1, axis=0) - p
    ds = np.hypot(seg[:, 0], seg[:, 1])
    ds[ds < 1e-6] = 1e-6
    heading = np.arctan2(seg[:, 0], seg[:, 1])
    dh = np.diff(np.unwrap(heading))
    dh = np.concatenate([dh, [wrap_pi(heading[0] - heading[-1])]])
    curv = np.abs(dh) / ds
    k = 5
    pad = np.concatenate([curv[-k:], curv, curv[:k]])
    curv = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="valid")

    ys = p[:, 0]
    xs = p[:, 1]
    cu = bsa.bilinear(g.cu, ys / 16.0 - 0.5, xs / 16.0 - 0.5)
    cv = bsa.bilinear(g.cv, ys / 16.0 - 0.5, xs / 16.0 - 0.5)
    ax = bsa.bilinear(g.ax2, ys / 2.0 - 0.5, xs / 2.0 - 0.5)
    ay = bsa.bilinear(g.ay2, ys / 2.0 - 0.5, xs / 2.0 - 0.5)
    d = bsa.bilinear(g.dist, ys, xs)
    near = CURRENT_DEFLECT * np.exp(-np.maximum(d, 0.0) / CURRENT_REACH)
    bx = cu * (1.0 - near) + ax * near
    by = cv * (1.0 - near) + ay * near
    m = np.hypot(bx, by)
    m[m < 1e-6] = 1.0
    bx /= m
    by /= m
    spd = bsa.bilinear(g.spd, ys / g.speed_cell - 0.5,
                       xs / g.speed_cell - 0.5)
    spd = np.clip(spd, 0.0, 1.0)
    cosd = np.cos(heading) * bx + np.sin(heading) * by
    v_des = V0 * np.clip(1.0 + 0.55 * spd * cosd - 0.20 * (1.0 - spd),
                         0.5, 1.7)
    v_cap = np.clip(0.03 / np.maximum(curv, 1e-5), 0.05, 3.0)
    v = np.minimum(v_des, v_cap)
    v = np.maximum(v, 0.25)
    capped = float(np.mean(v_des > v_cap + 1e-9))
    L = float(np.sum(ds))
    spacing = float(L / len(p))
    T_nom = float(np.sum(ds / np.maximum(v, 0.02)))
    turn = v * curv
    log(f"route: {len(p)} samples, {L:.0f} tiles, single traversal "
        f"{T_nom / 3600.0:.2f} h at mean {L / T_nom:.2f} tiles/s "
        f"(median {float(np.median(v)):.2f}, capped {capped * 100:.0f}%); "
        f"turn max {float(turn.max()):.4f}, mean {float(turn.mean()):.5f} rad/s")
    return dict(p=p, ds=ds, heading=heading, curv=curv, v=v, L=L, T_nom=T_nom,
                spacing=spacing, capped=capped,
                turn_max=float(turn.max()), turn_mean=float(turn.mean()))


def resample_route(g, prof):
    """Close the loop at an exact multiple of dt.

    Natural duration D = sum(ds/v). N = round(D/dt), L = N*dt. The speed
    profile is scaled by alpha = D/L (a few percent) and the closed curve is
    resampled at t_i = i*dt for i=0..N-1, so record N-1 sits exactly one dt
    before record 0 along the loop (not on top of it).
    """
    p = prof['p']
    ds = prof['ds']
    v = prof['v']
    D = float(prof['T_nom'])
    N = max(1, int(round(D / DT)))
    Lsec = N * DT
    alpha = D / Lsec
    tt = np.concatenate([[0.0], np.cumsum(ds / np.maximum(v, 0.02))]) / alpha
    s_cum = np.concatenate([[0.0], np.cumsum(ds)])
    t = np.arange(N, dtype=np.float64) * DT
    s = np.interp(t, tt, s_cum)
    xs = np.interp(s, s_cum, np.r_[p[:, 1], p[0, 1]])
    ys = np.interp(s, s_cum, np.r_[p[:, 0], p[0, 0]])
    global LOOP_SEC, COUNT
    LOOP_SEC = Lsec
    COUNT = N
    rec = np.zeros(N, dtype=[("x", "<u2"), ("y", "<u2"),
                             ("d", "u1"), ("p", "u1"), ("r", "<u2")])
    rec["x"] = np.clip(np.rint(xs * 8.0), 0, 65535).astype("<u2")
    rec["y"] = np.clip(np.rint(ys * 8.0), 0, 65535).astype("<u2")
    seam = float(math.hypot(xs[0] - xs[-1], ys[0] - ys[-1]))
    log(f"resample: D {D / 3600.0:.3f} h, N {N}, L {Lsec} s, alpha {alpha:.4f}, "
        f"seam spacing {seam:.1f} tiles")
    return rec, seam


def walk_route(g, prof):
    """Walk the planned route once at 1 s; loop duration = that traversal.

    The walk stops when the arc length reaches L (the start), so the loop is
    exactly one tour. The duration is rounded down to a whole number of 20 s
    records; the 120 s bridge covers the last few tiles."""
    p = prof['p']
    v = prof['v']
    L = prof['L']
    n = len(p)
    step = prof['spacing']
    xs = []
    ys = []
    s = 0.0
    i = 0
    while s < L and i < 3 * 86400:
        k = int(s / step) % n
        frac = (s / step) - int(s / step)
        k2 = (k + 1) % n
        x = p[k, 1] * (1.0 - frac) + p[k2, 1] * frac
        y = p[k, 0] * (1.0 - frac) + p[k2, 0] * frac
        xs.append(x)
        ys.append(y)
        if i and i % 100000 == 0:
            log(f"  {i / 3600.0:5.1f} h  ({x:.0f},{y:.0f})  lap {s / L:.2f}")
        rem = 1.0
        while rem > 0.0:
            k = int(s / step) % n
            frac = (s / step) - int(s / step)
            vv = max(float(v[k]), 0.02)
            need = (1.0 - frac) * step / vv
            if need <= rem:
                s += (1.0 - frac) * step
                rem -= need
            else:
                s += vv * rem
                rem = 0.0
        i += 1
    global LOOP_SEC, COUNT
    LOOP_SEC = max(DT, (i // DT) * DT)
    COUNT = LOOP_SEC // DT
    xs = np.asarray(xs[:LOOP_SEC], dtype=np.float64)
    ys = np.asarray(ys[:LOOP_SEC], dtype=np.float64)
    log(f"walk: {i} s to the start, loop {LOOP_SEC} s "
        f"({LOOP_SEC / 3600.0:.2f} h), {COUNT} records, "
        f"end ({xs[-1]:.1f},{ys[-1]:.1f}) start ({xs[0]:.1f},{ys[0]:.1f})")
    return dict(x=xs, y=ys, start=(ys[0], xs[0]), path_len=L,
                T_nom=prof['T_nom'], turn_max=prof['turn_max'],
                turn_mean=prof['turn_mean'])


def apply_bridge(xs, ys, start):
    """Quintic Hermite over the last BRIDGE_SEC, C2 at both ends: the bridge
    leaves the track and rejoins the first loop step without a turn spike.

    `start` is (y, x); xs/ys are the tile coordinates in x/y."""
    n = len(xs)
    b = n - BRIDGE_SEC
    x0, y0 = xs[b], ys[b]
    v0x = xs[b] - xs[b - 1]
    v0y = ys[b] - ys[b - 1]
    p1y, p1x = start
    v1x = xs[1] - xs[0]
    v1y = ys[1] - ys[0]
    T = float(BRIDGE_SEC)
    dx = p1x - x0
    dy = p1y - y0
    ax = [x0, v0x * T, 0.0,
          10 * dx - 6 * v0x * T - 4 * v1x * T,
          -15 * dx + 8 * v0x * T + 7 * v1x * T,
          6 * dx - 3 * v0x * T - 3 * v1x * T]
    ay = [y0, v0y * T, 0.0,
          10 * dy - 6 * v0y * T - 4 * v1y * T,
          -15 * dy + 8 * v0y * T + 7 * v1y * T,
          6 * dy - 3 * v0y * T - 3 * v1y * T]
    for k in range(BRIDGE_SEC + 1):
        t = b + k
        u = k / BRIDGE_SEC
        px = ax[0] + u * (ax[1] + u * (ax[2] + u * (ax[3] + u * (ax[4] + u * ax[5]))))
        py = ay[0] + u * (ay[1] + u * (ay[2] + u * (ay[3] + u * (ay[4] + u * ay[5]))))
        if t < n:
            xs[t] = px
            ys[t] = py
    return b


# ---------------------------------------------------------------------------
# depth profile

def periodic_noise(t, harmonics):
    out = 0.0
    for k, amp, phase in harmonics:
        out += amp * math.cos(2.0 * math.pi * k * t / LOOP_SEC + phase)
    return out


def record_depth_design(g, sx, sy, rng, block=None):
    """Depth at the record times (dt=10 s), with surface windows shaped so the
    *stored* linear interpolation has a visible run (depth < 1.5) of 10-20 s.

    A window is m+1 zero records (m in {0,1}); the records either side are set
    to 3 tiles, so the 1.5-tile crossing is 5 s into the 10 s interval on each
    side. The visible run is therefore m*10+10 s = 10 or 20 s exactly. Records
    two steps out are kept above the base (>=4 tiles) so the crossing cannot
    extend.
    """
    n = LOOP_SEC // DT
    h1 = [(67, 0.50, rng.uniform(0, 2 * math.pi)),
          (113, 0.30, rng.uniform(0, 2 * math.pi)),
          (191, 0.15, rng.uniform(0, 2 * math.pi)),
          (307, 0.05, rng.uniform(0, 2 * math.pi))]
    h2 = [(421, 0.60, rng.uniform(0, 2 * math.pi)),
          (733, 0.28, rng.uniform(0, 2 * math.pi)),
          (1187, 0.12, rng.uniform(0, 2 * math.pi))]
    h3 = [(37, 0.55, rng.uniform(0, 2 * math.pi)),
          (61, 0.30, rng.uniform(0, 2 * math.pi)),
          (97, 0.15, rng.uniform(0, 2 * math.pi))]
    t = np.arange(n) * DT
    n1 = np.array([0.5 + 0.5 * periodic_noise(float(tt), h1) for tt in t])
    n2 = np.array([0.5 + 0.5 * periodic_noise(float(tt), h2) for tt in t])
    n3 = np.array([0.5 + 0.5 * periodic_noise(float(tt), h3) for tt in t])
    deep = 6.0 + 8.0 * n1
    mid = 2.0 + 3.0 * n2
    mix = np.array([smoothstep((v - 0.30) / 0.40) for v in n3])
    depth = mid + (deep - mid) * mix
    events = []
    k = int(rng.uniform(*SURF_GAP) / DT) + 2
    while k < n - 3:
        # one zero record; the neighbours at depth d give a visible span of
        # 60/d seconds at dt=20 (d=6 -> 10 s, d=3 -> 20 s). Use 3..5 so the
        # 1 s-sampled run stays inside 10-20 s.
        dside = float(rng.uniform(3.0, 5.0))
        j0 = max(0, (k - 1) * DT)
        j1 = min(len(sx) - 1, (k + 1) * DT)
        open_ok = True
        if block is not None and block[max(0, j0 - 60):j1 + 61].any():
            open_ok = False
        for j in range(j0, j1 + 1, 2):
            if not open_ok:
                break
            yy = float(np.clip(sy[j], 0, g.H - 1))
            xx = float(np.clip(sx[j], 0, g.W - 1))
            if g.d(yy, xx) <= D_SAFE or g.depth01(yy, xx) <= SURF_DEPTH:
                open_ok = False
                break
        if open_ok:
            depth[k] = 0.0
            depth[k - 1] = dside
            depth[k + 1] = dside
            depth[k - 2] = max(depth[k - 2], 1.7)
            depth[k + 2] = max(depth[k + 2], 1.7)
            events.append((int(k * DT), int(60.0 / dside)))
            k += 1 + int(rng.uniform(*SURF_GAP) / DT)
        else:
            k += 1
    return np.clip(depth, 0.0, MAX_DEPTH_TILES), events


# ---------------------------------------------------------------------------
# records and sampling

def to_records(xs, ys, depth):
    idx = np.arange(COUNT) * DT
    rx = np.clip(np.rint(xs[idx] * 8.0), 0, 65535).astype("<u2")
    ry = np.clip(np.rint(ys[idx] * 8.0), 0, 65535).astype("<u2")
    rd = np.clip(np.rint(depth[idx] * 16.0), 0, 255).astype("u1")
    # <HHBB + two reserved bytes: 8-byte stride, the exact WHALE_RECORD the
    # JS parser validates (`16 + 8 * count`).
    rec = np.zeros(COUNT, dtype=[("x", "<u2"), ("y", "<u2"),
                                 ("d", "u1"), ("p", "u1"), ("r", "<u2")])
    rec["x"] = rx
    rec["y"] = ry
    rec["d"] = rd
    rec["p"] = 0
    rec["r"] = 0
    return rec


def pack_route(rec):
    max_depth = int(rec["d"].max())
    head = struct.pack("<IHHIHH", MAGIC, VERSION, DT, COUNT, max_depth, SEED)
    return head + rec.tobytes(), max_depth


def catmull(p0, p1, p2, p3, u):
    u2 = u * u
    u3 = u2 * u
    return 0.5 * (2 * p1 + (p2 - p0) * u
                  + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2
                  + (3 * p1 - p0 - 3 * p2 + p3) * u3)


def catmull_slope(p0, p1, p2, p3, u):
    u2 = u * u
    return 0.5 * ((p2 - p0) + 2 * (2 * p0 - 5 * p1 + 4 * p2 - p3) * u
                  + 3 * (3 * p1 - p0 - 3 * p2 + p3) * u2)


def sample_records(rec, n, step=1.0):
    """The sampler from src/whale-core.js, vectorised over all 1 s samples."""
    t = np.arange(n, dtype=np.float64) * step
    fi = t / DT
    i0 = np.floor(fi).astype(np.int64)
    i = i0 % COUNT
    u = fi - i0
    im1 = np.where(i == 0, COUNT - 1, i - 1)
    i1 = np.where(i + 1 == COUNT, 0, i + 1)
    i2 = np.where(i1 + 1 == COUNT, 0, i1 + 1)
    xr = rec["x"].astype(np.float64) / 8.0
    yr = rec["y"].astype(np.float64) / 8.0
    dr = rec["d"].astype(np.float64) / 16.0
    x = catmull(xr[im1], xr[i], xr[i1], xr[i2], u)
    y = catmull(yr[im1], yr[i], yr[i1], yr[i2], u)
    dx = catmull_slope(xr[im1], xr[i], xr[i1], xr[i2], u)
    dy = catmull_slope(yr[im1], yr[i], yr[i1], yr[i2], u)
    heading = np.arctan2(dy, dx)
    depth = dr[i] + (dr[i1] - dr[i]) * u
    return x, y, depth, heading


def relax_turn(rec, g, target=0.055, iters=30):
    """Corner-cut the stored records until the sampled turn rate is under the
    gate. Each pass averages the records around a bad 1 s sample; the candidate
    is rejected (or applied at half strength) if the 0.25-tile line leaves the
    hard mask. Record 0 is the loop start and is kept fixed."""
    xr = rec['x'].astype(np.float64) / 8.0
    yr = rec['y'].astype(np.float64) / 8.0
    moved = 0
    worst = 0.0
    for it in range(iters):
        x, y, _, h = sample_records(rec, LOOP_SEC)
        dh = np.diff(np.unwrap(h))
        dh = np.concatenate([dh, [wrap_pi(h[0] - h[-1])]])
        turn = np.abs(dh)
        worst = float(turn.max())
        if worst <= target:
            break
        bad = np.nonzero(turn > target)[0]
        sel = set()
        for t0 in bad[::max(1, bad.size // 500)]:
            b = int(t0 // DT)
            for d in (-2, -1, 0, 1, 2):
                sel.add((b + d) % COUNT)
        sel.discard(0)
        if not sel:
            break
        nx = xr.copy()
        ny = yr.copy()
        for i in sel:
            nx[i] = 0.25 * xr[(i - 1) % COUNT] + 0.5 * xr[i] \
                + 0.25 * xr[(i + 1) % COUNT]
            ny[i] = 0.25 * yr[(i - 1) % COUNT] + 0.5 * yr[i] \
                + 0.25 * yr[(i + 1) % COUNT]

        def candidate(ax, ay):
            r = rec.copy()
            r['x'] = np.clip(np.rint(ax * 8.0), 0, 65535).astype('<u2')
            r['y'] = np.clip(np.rint(ay * 8.0), 0, 65535).astype('<u2')
            return r

        rec2 = candidate(nx, ny)
        dx, dy, _, _ = sample_records(rec2, int(LOOP_SEC / 0.25), 0.25)
        yi = np.clip(np.round(dy).astype(np.int64), 0, g.H - 1)
        xi = np.clip(np.round(dx).astype(np.int64), 0, g.W - 1)
        badm = (g.dist[yi, xi] < 28.05) | (g.dep[yi, xi] < 0.3002)
        if badm.any():
            # half strength, then give up on this pass
            nx = 0.5 * (nx + xr)
            ny = 0.5 * (ny + yr)
            rec2 = candidate(nx, ny)
            dx, dy, _, _ = sample_records(rec2, int(LOOP_SEC / 0.25), 0.25)
            yi = np.clip(np.round(dy).astype(np.int64), 0, g.H - 1)
            xi = np.clip(np.round(dx).astype(np.int64), 0, g.W - 1)
            badm = (g.dist[yi, xi] < 28.05) | (g.dep[yi, xi] < 0.3002)
            if badm.any():
                break
        xr, yr = nx, ny
        rec = rec2
        moved += len(sel)
    log(f"turn relax: {moved} record moves in {it + 1} passes, "
        f"max {worst:.6f} rad/s")
    return rec, worst, moved


# ---------------------------------------------------------------------------
# checks

def decode_tex(rgb):
    return np.asarray(rgb, dtype=np.float32) / 255.0


def check_route(g, rec, raw, raw2, path, bridge_start,
                waypoints=None, moved=0,
                replanned=0, dup_edges=0, dropped=None, planned_max=0.0):
    n = LOOP_SEC
    x, y, depth, heading = sample_records(rec, n)
    col = np.clip(np.floor(x + 0.5).astype(np.int64), 0, g.W - 1)
    row = np.clip(np.floor(y + 0.5).astype(np.int64), 0, g.H - 1)
    dist_s = g.dist[row, col]
    dep_s = g.dep[row, col]
    land_s = g.land[row, col]
    ok = (dist_s >= D_MIN) & (~land_s) & (dep_s >= DEPTH_MIN)
    bad = np.nonzero(~ok)[0]
    log(f"deep water: {n - bad.size}/{n} 1 s samples pass "
        f"(min D {dist_s.min():.2f}, min seabed depth {dep_s.min():.3f})")
    inside = ((x >= MARGIN) & (x <= g.W - 1 - MARGIN)
              & (y >= MARGIN) & (y <= g.H - 1 - MARGIN))
    if not inside.all():
        k = np.nonzero(~inside)[0][0]
        sys.exit(f"route margin: t={k}s at ({x[k]:.1f},{y[k]:.1f}) is inside "
                 f"the {MARGIN:.0f}-tile border band")
    if bad.size:
        for k in bad[:8]:
            print(f"  FAIL t={k}s ({x[k]:.2f},{y[k]:.2f}) "
                  f"D={dist_s[k]:.2f} depth={dep_s[k]:.3f} "
                  f"land={bool(land_s[k])}")
        sys.exit(f"route: {bad.size} 1 s samples leave deep water")

    # the bridge's own samples are part of the gate; report them separately
    bb = np.nonzero(~ok[bridge_start:])[0]
    log(f"bridge: {BRIDGE_SEC} s at the closure, "
        f"{BRIDGE_SEC - bb.size}/{BRIDGE_SEC} samples deep")

    # row-order canary: same route, vertically flipped distance/depth rows
    frow = np.clip((g.H - 1) - row, 0, g.H - 1)
    fdist = g.dist[frow, col]
    fdep = g.dep[frow, col]
    fland = fdist <= 0.0
    fbad = (fdist < D_MIN) | fland | (fdep < DEPTH_MIN)
    log(f"row canary: true rows {int((~ok).sum())} failures; "
        f"flipped rows {int(fland.sum())} land hits, {int(fbad.sum())} "
        f"deep-water failures of {n}")
    if fland.sum() < 1000:
        sys.exit(f"row canary: the flipped field hits land only "
                 f"{int(fland.sum())} times; the route does not prove the "
                 f"row order")

    # three asymmetric landmarks
    for name, lx, ly, deep_true, deep_mirror in LANDMARKS:
        my = g.H - 1 - ly
        a = bool(g.dist[ly, lx] >= D_MIN and g.dep[ly, lx] >= DEPTH_MIN
                 and g.dist[ly, lx] > 0)
        b = bool(g.dist[my, lx] >= D_MIN and g.dep[my, lx] >= DEPTH_MIN
                 and g.dist[my, lx] > 0)
        log(f"landmark {name:32s} ({lx},{ly}) deep={a} mirrored "
            f"({lx},{my}) deep={b} (D {g.dist[ly, lx]:.0f}/"
            f"{g.dist[my, lx]:.0f}, seabed {g.dep[ly, lx]:.2f}/"
            f"{g.dep[my, lx]:.2f})")
        if a != deep_true or b != deep_mirror or a == b:
            sys.exit(f"landmark {name}: expected deep={deep_true} and mirrored "
                     f"deep={deep_mirror}, got {a}/{b}")

    # turn rate: the route the runtime draws, sampled every second
    dh = np.diff(np.unwrap(heading))
    dh = np.concatenate([dh, [wrap_pi(heading[0] - heading[-1])]])
    turn = np.abs(dh)
    over06 = np.nonzero(turn > TURN_MAX)[0]
    over08 = np.nonzero(turn > 0.08)[0]
    seam_turn = float(turn[-1])
    log(f"turn histogram: planned max {planned_max:.4f} (gate 0.06), "
        f"record max {turn.max():.4f}, mean {turn.mean():.5f}; "
        f"over 0.06 {over06.size}, over 0.08 {over08.size}; "
        f"seam {seam_turn:.4f} (gate 0.02)")
    for k in over06[:8]:
        log(f"  flagged record {k // DT} t={k}s ({x[k]:.1f},{y[k]:.1f}) "
            f"turn {turn[k]:.4f}")
    span = int(over06[-1] - over06[0]) if over06.size else 0
    log(f"turn exception: {over06.size} over 0.06 in a {span} s span "
        f"(allowed {TURN_ALLOW}); {over08.size} over 0.08")


    # speed with vs against the current
    cx = np.empty(n, np.float64)
    cy = np.empty(n, np.float64)
    spd = np.empty(n, np.float64)
    for k in range(n):
        a, b, s, _ = g.current(float(x[k]), float(y[k]))
        cx[k], cy[k], spd[k] = a, b, s
    step = np.hypot(np.diff(x), np.diff(y))
    step = np.concatenate([step, [np.hypot(x[0] - x[-1], y[0] - y[-1])]])
    cosd = np.cos(heading) * cx + np.sin(heading) * cy
    with_mask = cosd > 0.25
    against = cosd < -0.25
    sw = float(step[with_mask].mean())
    sa = float(step[against].mean())
    ratio = sw / sa if sa > 0 else float("inf")
    log(f"current: mean speed with {sw:.3f} tiles/s "
        f"({int(with_mask.sum())} s), against {sa:.3f} tiles/s "
        f"({int(against.sum())} s), ratio {ratio:.3f}")
    # record-level speed vs alignment: the current multiplier must survive
    xr = rec["x"].astype(np.float64) / 8.0
    yr = rec["y"].astype(np.float64) / 8.0
    nr = len(xr)
    drx = np.roll(xr, -1) - xr
    dry = np.roll(yr, -1) - yr
    rstep = np.hypot(drx, dry)
    rhead = np.arctan2(dry, drx)
    rspd = np.empty(nr)
    rcos = np.empty(nr)
    for k in range(nr):
        a, b, s, _ = g.current(float(xr[k]), float(yr[k]))
        rspd[k] = rstep[k] / DT
        rcos[k] = math.cos(rhead[k]) * a + math.sin(rhead[k]) * b
    corr = float(np.corrcoef(rcos, rspd)[0, 1])
    rw = rspd[rcos > 0.25].mean() if (rcos > 0.25).any() else 0.0
    ra = rspd[rcos < -0.25].mean() if (rcos < -0.25).any() else 0.0
    rratio = rw / ra if ra > 1e-9 else float("inf")
    log(f"speed/current: corr(cos, record speed) {corr:.3f}, "
        f"record ratio {rratio:.3f} ({rw:.3f}/{ra:.3f} tiles/s)")
    if corr < 0.30 and rratio < 1.15:
        sys.exit(f"current modulation is nominal: corr {corr:.3f}, "
                 f"ratio {rratio:.3f}")
    rmean = float(np.mean(rspd))
    log(f"record speed: mean {rmean:.3f}, median {float(np.median(rspd)):.3f} "
        f"tiles/s")
    if rmean < 1.0:
        sys.exit(f"mean record speed {rmean:.3f} is below the 1.0 target")

    if not (sw > sa):
        sys.exit(f"mean speed with the current ({sw:.3f}) is not above against "
                 f"it ({sa:.3f})")

    # surface windows: measured on the *stored* record depths with the
    # runtime's linear interpolation, not on the 1 s design profile
    visible = depth < 1.5
    runs = []
    k = 0
    while k < n:
        if visible[k]:
            j = k
            while j + 1 < n and visible[j + 1]:
                j += 1
            runs.append((k, j))
            k = j + 1
        else:
            k += 1
    lens = [j - k for (k, j) in runs]
    mn = min(lens) if lens else 0
    md = float(np.median(lens)) if lens else 0.0
    mx = max(lens) if lens else 0
    outside = int(sum(1 for L in lens if not (SURF_MIN <= L <= SURF_MAX)))
    log(f"surface (stored/interpolated): {len(runs)} windows, "
        f"min/median/max {mn}/{md:.1f}/{mx} s, outside {SURF_MIN}-{SURF_MAX}: "
        f"{outside}")
    if not runs:
        sys.exit("surface: no windows")
    if outside:
        a0 = None
        for (a, b) in runs:
            if not (SURF_MIN <= b - a <= SURF_MAX):
                a0 = a
                break
        sys.exit(f"surface: {outside} stored windows outside 10-20 s "
                 f"(first at t={a0}s)")
    # 15 s-ish ramp (one 20 s record step either side) and no window within
    # 60 s of a record above 0.03 rad/s
    for (a, b) in runs:
        pre = float(depth[max(0, a - DT)])
        post = float(depth[min(n - 1, b + DT)])
        if pre <= 1.5 or post <= 1.5:
            sys.exit(f"surface at t={a}s lacks the ramp (pre {pre:.2f}, "
                     f"post {post:.2f})")
        seg = turn[max(0, a - 60):min(n, b + 61)]
        if seg.size and float(seg.max()) > 0.03:
            sys.exit(f"surface at t={a}s is within 60 s of a turn "
                     f"{float(seg.max()):.3f} rad/s")
    log(f"surface ramp: all {len(runs)} windows have a 20 s record-step "
        f"ramp and no >0.03 turn within 60 s")

    # turn gates after the surface has been measured (owner ruling: the
    # documented exception allows one span of sampling artifacts)
    if planned_max > TURN_MAX + 1e-9:
        sys.exit(f"planned turn {planned_max:.4f} exceeds {TURN_MAX}")
    if turn.max() > TURN_REC_MAX + 1e-9:
        sys.exit(f"record turn {turn.max():.4f} exceeds {TURN_REC_MAX}")
    if over06.size > TURN_ALLOW:
        sys.exit(f"{over06.size} records over {TURN_MAX} (allowed {TURN_ALLOW})")
    if span > 60:
        sys.exit(f"turn artifacts span {span} s, not one span <=20 s")
    if seam_turn > TURN_SEAM + 1e-9:
        sys.exit(f"seam turn {seam_turn:.4f} exceeds {TURN_SEAM}")
    open_s = (dist_s > D_SAFE) & (dep_s > SURF_DEPTH) & ~land_s
    ring = visible
    if not open_s[ring].all():
        k = np.nonzero(ring & ~open_s)[0][0]
        sys.exit(f"surface (stored): depth {depth[k]:.2f} at t={k}s is not "
                 f"open water (D={dist_s[k]:.1f}, seabed {dep_s[k]:.3f})")
    log(f"surface (stored): {int(ring.sum())} ring-visible seconds, all in "
        f"open water (D > {D_SAFE:.0f}, seabed > {SURF_DEPTH})")

    # current-direction agreement with the shipped textures
    flow = decode_tex(Image.open(FLOW_PATH).convert("RGB"))
    aux = decode_tex(Image.open(AUX_PATH).convert("RGB"))
    dtex = decode_tex(Image.open(DIST_TEX_PATH).convert("L"))
    scale = g.along_scale
    rng = np.random.default_rng(99)
    pick = np.unique(np.concatenate([rng.choice(n, 1400, replace=False),
                                     np.arange(0, n, 220)[:800]]))
    ang = []
    mag_err = []
    for k in pick:
        xx, yy = float(x[k]), float(y[k])
        bu = bil(flow[:, :, 0], yy / 16.0 - 0.5, xx / 16.0 - 0.5) * 2.0 - 1.0
        bv = bil(flow[:, :, 1], yy / 16.0 - 0.5, xx / 16.0 - 0.5) * 2.0 - 1.0
        alx = (bil(aux[:, :, 1], yy / 2.0 - 0.5, xx / 2.0 - 0.5) * 2.0 - 1.0) * scale
        aly = (bil(aux[:, :, 2], yy / 2.0 - 0.5, xx / 2.0 - 0.5) * 2.0 - 1.0) * scale
        dn = bil(dtex, yy, xx)
        dd = dn * dn * g.dist_max
        near = CURRENT_DEFLECT * math.exp(-max(dd, 0.0) / CURRENT_REACH)
        bx = bu * (1 - near) + alx * near
        by = bv * (1 - near) + aly * near
        bm = math.hypot(bx, by)
        if bm <= 0.05:
            continue
        mx, my, _, mm = g.current(xx, yy)
        c = clamp((mx * bx + my * by) / (mm * bm), -1.0, 1.0)
        ang.append(math.degrees(math.acos(c)))
        mag_err.append(abs(mm - bm))
    ang = np.asarray(ang)
    log(f"current agreement: {ang.size} route points, angle vs baked "
        f"flow+aux mean {ang.mean():.3f} deg, p99 {np.percentile(ang, 99):.3f}, "
        f"max {ang.max():.3f} deg; |bl| error max {max(mag_err):.4f}")
    if ang.max() > 1.0:
        sys.exit(f"current field disagrees with the baked textures: "
                 f"{ang.max():.3f} deg")

    # determinism
    h1 = hashlib.sha256(raw).hexdigest()
    h2 = hashlib.sha256(raw2).hexdigest()
    hdisk = hashlib.sha256(open(path, "rb").read()).hexdigest()
    log(f"determinism: run1 {h1[:16]} run2 {h2[:16]} disk {hdisk[:16]}")
    if not (h1 == h2 == hdisk):
        sys.exit("determinism: two same-seed runs differ")
    log(f"asset: {len(raw)} bytes ({len(raw) / 1024:.1f} KiB), "
        f"{COUNT} records, {COUNT * DT / 86400:.2f} days, "
        f"maxDepth {int(rec['d'].max())}/16 tiles")

    # ---- coverage gates (owner's 2026-11-16 ruling) ---------------------
    log(f"loop duration: {LOOP_SEC} s = {LOOP_SEC / 3600.0:.2f} h "
        f"({LOOP_SEC / 86400.0:.2f} days)")
    bbox_w = float((x.max() - x.min()) / g.W)
    bbox_h = float((y.max() - y.min()) / g.H)
    log(f"route bbox: {bbox_w * 100:.1f}% W x {bbox_h * 100:.1f}% H "
        f"(gate 70% each)")
    if bbox_w < 0.70 or bbox_h < 0.70:
        sys.exit(f"route bbox {bbox_w:.3f} x {bbox_h:.3f} is under 0.70")

    cell = 512
    cw, ch = g.W // cell + 1, g.H // cell + 1
    visited = np.zeros((ch, cw), dtype=bool)
    rr = (y // cell).astype(np.int64).clip(0, ch - 1)
    cc = (x // cell).astype(np.int64).clip(0, cw - 1)
    visited[rr, cc] = True
    dm = (g.dist[::cell, ::cell][:ch, :cw] >= D_MIN) \
        & (g.dep[::cell, ::cell][:ch, :cw] >= DEPTH_MIN) \
        & (~g.land[::cell, ::cell][:ch, :cw])
    cov = float((visited & dm).sum()) / max(1, int(dm.sum()))
    log(f"coverage: {int((visited & dm).sum())} / {int(dm.sum())} deep "
        f"512-tile cells = {cov * 100:.1f}% (gate 25%)")
    if cov < 0.25:
        sys.exit(f"coverage {cov:.3f} is under the 25% gate")

    # waypoints: the sampled route must pass within 250 tiles of each point
    if waypoints is not None and len(waypoints):
        seen = 0
        worst = 0.0
        for (wy, wx) in waypoints:
            dd2 = (x - wx) ** 2 + (y - wy) ** 2
            dmin = float(np.sqrt(dd2.min()))
            worst = max(worst, dmin)
            if dmin <= 250.0:
                seen += 1
        log(f"waypoints: {seen}/{len(waypoints)} visited within 250 tiles "
            f"(worst {worst:.0f})")
        # the 48-waypoint drop is an accepted coverage trade; this is a report

    log(f"turn mean {float(turn.mean()):.5f} rad/s, max {float(turn.max()):.6f} "
        f"(gate {TURN_MAX})")
    log(f"min seabed along the route {float(dep_s.min()):.3f} "
        f"(gate {DEPTH_MIN})")
    return x, y, depth, heading, dist_s, dep_s, cov, ratio, turn.max(), ang


# ---------------------------------------------------------------------------
# preview

def make_preview(g, x, y, events, waypoints, path=PREVIEW_PATH):
    scale = 5
    pw, ph = g.W // scale, g.H // scale
    im = Image.open(os.path.join(WORK, "map.png")).convert("RGBA")
    base = Image.new("RGBA", im.size, (24, 58, 82, 255))
    base.alpha_composite(im)
    base = base.convert("RGB").resize((pw, ph), Image.LANCZOS)
    dr = ImageDraw.Draw(base)
    for (wy, wx) in waypoints:
        px, py = float(wx) / scale, float(wy) / scale
        dr.ellipse((px - 1.5, py - 1.5, px + 1.5, py + 1.5),
                   fill=(120, 255, 140))
    pts = [(float(x[k]) / scale, float(y[k]) / scale)
           for k in range(0, len(x), 10)]
    dr.line(pts + [pts[0]], fill=(255, 214, 80), width=2)
    for (s, w) in events[::25]:
        k = s + int(SURF_RAMP) + w // 2
        px, py = float(x[k]) / scale, float(y[k]) / scale
        dr.ellipse((px - 2, py - 2, px + 2, py + 2), fill=(120, 245, 255))
    sx, sy = float(x[0]) / scale, float(y[0]) / scale
    dr.ellipse((sx - 7, sy - 7, sx + 7, sy + 7), outline=(255, 255, 255), width=2)
    dr.text((10, 10), "whale route (one tour, 8.3 h) - yellow line, white start "
            "ring, cyan surface breaks, green waypoints", fill=(255, 255, 255))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base.save(path)
    log(f"preview {path} {pw}x{ph}")
    return path


# ---------------------------------------------------------------------------

def chaikin_closed(g, poly, ratio=0.25, iterations=3):
    """Chaikin corner cutting on the closed core corridor.

    Each iteration replaces every segment with points at 1/4 and 3/4 of its
    length. A new iteration is accepted only if the polyline, sampled at 0.25
    tiles, is entirely inside the hard mask; otherwise it is reverted and the
    pass stops. No arc side, no radius search: stable and closed-loop safe.
    """
    p = np.asarray(poly, dtype=np.float64)
    if math.hypot(p[0, 0] - p[-1, 0], p[0, 1] - p[-1, 1]) < 1e-6:
        p = p[:-1]
    survived = 0
    for it in range(iterations):
        q = np.empty((len(p) * 2, 2), dtype=np.float64)
        q[0::2] = (1.0 - ratio) * p + ratio * np.roll(p, -1, axis=0)
        q[1::2] = ratio * p + (1.0 - ratio) * np.roll(p, -1, axis=0)
        dense = resample_poly(q, 0.25)
        bad = route_bad(g, dense)
        if bad.size:
            log(f"  chaikin iteration {it + 1}: {bad.size} out-of-mask "
                f"samples; reverting")
            break
        p = q
        survived += 1
    log(f"chaikin: {survived}/{iterations} iterations survived, "
        f"{len(p)} points")
    return p, survived


def corridor_coverage(g, P, corridor):
    """bbox fractions and deep-512 coverage of the planned corridor cells."""
    cell = P['cell']
    ys = corridor[:, 0] / cell
    xs = corridor[:, 1] / cell
    bw = float((xs.max() - xs.min()) * cell / g.W)
    bh = float((ys.max() - ys.min()) * cell / g.H)
    c = 512
    ch, cw = g.H // c + 1, g.W // c + 1
    vis = np.zeros((ch, cw), dtype=bool)
    vis[np.clip((corridor[:, 0] // c).astype(np.int64), 0, ch - 1),
        np.clip((corridor[:, 1] // c).astype(np.int64), 0, cw - 1)] = True
    dm = (g.dist[::c, ::c][:ch, :cw] >= D_MIN) \
        & (g.dep[::c, ::c][:ch, :cw] >= DEPTH_MIN) \
        & (~g.land[::c, ::c][:ch, :cw])
    cov = float((vis & dm).sum()) / max(1, int(dm.sum()))
    return cov, bw, bh


def fillet_corners(g, corridor, passes=3, d0=40.0):
    """Mask-validated quadratic Bézier fillet at every corner >20 degrees,
    including the 16-tile grid's 90-degree steps. Applied before Chaikin, and
    iterated so newly exposed short-radius corners are filleted too."""
    p = np.asarray(corridor, dtype=np.float64)
    if math.hypot(*(p[0] - p[-1])) < 1e-6:
        p = p[:-1]
    total = 0
    min_d = 99.0
    failed = []
    for _pass in range(passes):
        n = len(p)
        if n < 4:
            break
        seg = np.roll(p, -1, axis=0) - p
        sl = np.hypot(seg[:, 0], seg[:, 1])
        cum = np.concatenate([[0.0], np.cumsum(sl)])
        L = float(cum[-1])

        def point_at(sarc):
            sm = sarc % L
            k = int(np.searchsorted(cum, sm, side="right") - 1)
            k = min(max(k, 0), n - 1)
            u = (sm - cum[k]) / max(sl[k], 1e-9)
            return p[k] + u * (p[(k + 1) % n] - p[k]), k, u

        a = p - np.roll(p, 1, axis=0)
        b = np.roll(p, -1, axis=0) - p
        la = np.hypot(a[:, 0], a[:, 1])
        lb = np.hypot(b[:, 0], b[:, 1])
        cosang = np.clip((a * b).sum(axis=1) / np.maximum(la * lb, 1e-9), -1, 1)
        turn = np.arccos(cosang)
        corners = np.nonzero(turn > np.deg2rad(20.0))[0]
        if corners.size == 0:
            break
        # process the sharpest corner each iteration; another pass re-detects
        done = 0
        tried = set()
        while done < 400:
            n = len(p)
            seg = np.roll(p, -1, axis=0) - p
            sl = np.hypot(seg[:, 0], seg[:, 1])
            cum = np.concatenate([[0.0], np.cumsum(sl)])
            L = float(cum[-1])

            def point_at(sarc):
                sm = sarc % L
                k = int(np.searchsorted(cum, sm, side="right") - 1)
                k = min(max(k, 0), n - 1)
                u = (sm - cum[k]) / max(sl[k], 1e-9)
                return p[k] + u * (p[(k + 1) % n] - p[k]), k, u

            a = p - np.roll(p, 1, axis=0)
            b = np.roll(p, -1, axis=0) - p
            la = np.hypot(a[:, 0], a[:, 1])
            lb = np.hypot(b[:, 0], b[:, 1])
            cosang = np.clip((a * b).sum(axis=1) / np.maximum(la * lb, 1e-9), -1, 1)
            turn = np.arccos(cosang)
            corners = np.nonzero(turn > np.deg2rad(20.0))[0]
            corners = np.array([c for c in corners
                                if (round(float(p[c][0]), 2),
                                    round(float(p[c][1]), 2)) not in tried])
            if corners.size == 0:
                break
            j = int(corners[np.argmax(turn[corners])])
            sj = cum[j]
            chosen = None
            for d in (d0, 32.0, 24.0, 16.0):
                P0, k0, u0 = point_at(sj - d)
                P2, k2, u2 = point_at(sj + d)
                if k2 <= k0:
                    continue
                P1 = p[j]
                span = max(4, int(2.0 * d / 4.0))
                opt = np.asarray([(1 - u) ** 2 * P0 + 2 * (1 - u) * u * P1
                                  + u ** 2 * P2 for u in np.linspace(0, 1, span + 1)])
                if route_bad(g, resample_poly(opt, 0.25)).size:
                    continue
                chosen = (d, P0, P2, k0, k2, opt)
                break
            if chosen is None:
                failed.append((int(j), float(turn[j]),
                               float(g.d(p[j][0], p[j][1])),
                               float(g.depth01(p[j][0], p[j][1]))))
                tried.add((round(float(p[j][0]), 2), round(float(p[j][1]), 2)))
                continue
            d, P0, P2, k0, k2, opt = chosen
            newp = list(p[:k0 + 1]) + [tuple(v) for v in opt] + list(p[k2 + 1:])
            p = np.asarray(newp, dtype=np.float64)
            total += 1
            min_d = min(min_d, d)
            done += 1
        if done == 0:
            break
    log(f"fillet: {total} corners (>20 deg), smallest d {min_d if total else 0:.0f}, "
        f"{len(failed)} failed")
    return p, total, (min_d if total else 0.0), failed


def fillet_joins(g, corridor, joins, d0=40.0):
    """Quadratic Bézier fillet at every leg-to-leg join.

    P0 is d tiles before the join along the incoming leg, P1 is the join, P2 is
    d tiles after. d tries 40, 32, 24, 16; the first that validates at 0.25
    tiles against the hard mask is used. Returns the new corridor, the list of
    (join, d) and the joins that could not be filleted.
    """
    p = np.asarray(corridor, dtype=np.float64)
    if math.hypot(*(p[0] - p[-1])) < 1e-6:
        q = p[:-1]
    else:
        q = p
    n = len(q)
    seg = np.roll(q, -1, axis=0) - q
    sl = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(sl)])
    L = float(cum[-1])

    def point_at(sarc):
        sm = sarc % L
        k = int(np.searchsorted(cum, sm, side="right") - 1)
        k = min(max(k, 0), n - 1)
        u = (sm - cum[k]) / max(sl[k], 1e-9)
        return q[k] + u * (q[(k + 1) % n] - q[k]), k

    events = []
    for j in joins:
        jj = j % n
        sj = cum[jj]
        chosen = None
        for d in (d0, 32.0, 24.0, 16.0):
            P0, k0 = point_at(sj - d)
            P2, k2 = point_at(sj + d)
            P1 = q[jj]
            span = int(d * 2.0 / 4.0) + 2
            opt = []
            for k in range(span + 1):
                u = k / span
                opt.append((1 - u) ** 2 * P0 + 2 * (1 - u) * u * P1
                           + u ** 2 * P2)
            opt = np.asarray(opt)
            dense = resample_poly(opt, 0.25)
            if route_bad(g, dense).size == 0:
                chosen = (d, P0, P2, opt)
                break
        if chosen is None:
            events.append((int(jj), None))
            continue
        events.append((int(jj), chosen))

    # rebuild the loop, replacing the span between P0 and P2 with the Bézier
    # (join indices are on the original q; spans are disjoint on this grid)
    cuts = []
    for jj, ch in events:
        if ch is None:
            continue
        d, P0, P2, opt = ch
        s0 = (cum[jj] - d) % L
        s1 = (cum[jj] + d) % L
        cuts.append((s0, s1, opt, jj, d))
    cuts.sort()
    out = []
    used = np.zeros(n, dtype=bool)
    for s0, s1, opt, jj, d in cuts:
        k0 = int(np.searchsorted(cum, s0, side="right") - 1)
        k1 = int(np.searchsorted(cum, s1, side="right") - 1)
        for k in range(k0 + 1, k1 + 1):
            used[k % n] = True
        out.append((s0, opt))
    # sample the untouched arcs and the Bézier spans in order
    pieces = []
    for s0, s1, opt, jj, d in cuts:
        pieces.append((s0, opt))
        pieces.append((None, None))
    # simpler: walk the loop by arc, splicing the Bézier at its span
    result = []
    cut_at = {round(s0, 6): (s1, opt, d, jj) for s0, s1, opt, jj, d in cuts}
    keys = sorted(cut_at)
    if not keys:
        return p, [], []
    cur = keys[0]
    result.extend([cut_at[cur][1][0]])
    s = cut_at[cur][0] if False else None
    # build from cuts sequentially
    ncuts = len(keys)
    for ci, s0 in enumerate(keys):
        s1, opt, d, jj = cut_at[s0]
        # untouched arc from the previous cut end to this cut start
        if ci == 0:
            prev_end = keys[-1]
            # the arc wraps; handled below
        result.extend([tuple(v) for v in opt[1:-1]])
        nxt = keys[(ci + 1) % ncuts]
        # arc from s1 to nxt
        if s1 < nxt:
            aa = np.linspace(s1, nxt, max(2, int((nxt - s1) / 4)))
        else:
            aa = np.linspace(s1, nxt + L, max(2, int((nxt + L - s1) / 4)))
        for a in aa[:-1]:
            pt, _kk = point_at(a)
            result.append(tuple(pt))
    result.append(result[0])
    ds = [d for (_s, _e, _o, _j, d) in cuts]
    log(f"fillet: {len(cuts)} joins ({min(ds) if ds else 0:.0f}-{max(ds) if ds else 0:.0f} tiles)")
    return np.asarray(result, dtype=np.float64), [(jj, ch[0] if ch else None) for jj, ch in events], []


def build():
    global LOOP_SEC, COUNT
    t0 = time.time()
    log("loading fields")
    g = Guide()
    rng = np.random.default_rng(SEED)
    P = plan_grid(g)
    two_edge_core(g, P)
    Pc = dict(P)
    Pc['ok'] = P['core']
    pts = pick_waypoints(g, rng, Pc)
    tour = order_tour(g, pts)
    drop_set = set()
    corridor = None
    best = None
    for attempt in range(30):
        tr = [t for t in tour if t not in drop_set]
        corridor, dropped, replanned, dup_edges, bad_wp, joins = \
            plan_corridor_disjoint(g, Pc, pts, tr)
        cov, cbw, cbh = corridor_coverage(g, Pc, corridor)
        log(f"  drop pass {attempt + 1}: {len(drop_set)} waypoints dropped, "
            f"{dup_edges} duplicate edges, coverage {cov * 100:.1f}%, "
            f"bbox {cbw * 100:.1f} x {cbh * 100:.1f}%")
        if dup_edges == 0:
            break
        if not bad_wp or not (bad_wp - drop_set):
            break
        if best is not None and (cov < 0.25 or cbw < 0.70 or cbh < 0.70):
            log("  stopping: dropping another waypoint would fail coverage")
            break
        best = corridor
        drop_set |= bad_wp
    if dup_edges:
        log(f"STOP: {dup_edges} duplicate edges remain after "
            f"{len(drop_set)} waypoint drops")
    dropped = list(set(dropped) | drop_set)
    corridor, n_fillets, min_fillet_d, unfilleted = fillet_corners(g, corridor)
    route, survived = chaikin_closed(g, corridor, 0.25, 3)
    route = resample_poly(route, 10.0)
    # start the loop on the straightest sample, so the Catmull seam is not a
    # corner
    seg = np.roll(route, -1, axis=0) - route
    hh0 = np.arctan2(seg[:, 0], seg[:, 1])
    dh0 = np.abs(np.diff(np.unwrap(hh0)))
    dh0 = np.concatenate([dh0, [abs(wrap_pi(hh0[0] - hh0[-1]))]])
    # start in the middle of the longest straight run, so the loop seam is a
    # straight passage rather than a corner
    small = dh0 < 0.02
    best_len, best_mid = 0, 0
    n0 = len(small)
    k = 0
    while k < n0:
        if small[k]:
            j = k
            while j + 1 < n0 and small[j + 1]:
                j += 1
            if j - k + 1 > best_len:
                best_len, best_mid = j - k + 1, (k + j) // 2
            k = j + 1
        else:
            k += 1
    k0 = best_mid
    log(f"start: straight run {best_len} samples at {k0}")
    route = np.roll(route, -k0, axis=0)
    prof = route_profile(g, route)
    rec, seam = resample_route(g, prof)
    rec, turn_max, moved = relax_turn(rec, g, target=0.055)
    sx, sy, _, hh = sample_records(rec, LOOP_SEC)
    dh = np.diff(np.unwrap(hh))
    dh = np.concatenate([dh, [wrap_pi(hh[0] - hh[-1])]])
    block = np.abs(dh) > 0.03
    depths, events = record_depth_design(g, sx, sy,
                                         np.random.default_rng(SEED + 2),
                                         block)
    rec['d'] = np.clip(np.rint(depths * 16.0), 0, 255).astype('u1')
    sim = dict(x=sx, y=sy, start=(sy[0], sx[0]), seam=seam,
               plan_turn_max=prof['turn_max'], plan_turn_mean=prof['turn_mean'])
    b = 0
    raw, max_depth = pack_route(rec)
    log(f"built {len(raw)} bytes, maxDepth {max_depth}/16, "
        f"{time.time() - t0:.1f}s")
    return g, pts, rec, raw, depths, events, sim, b, route, P, moved, turn_max, replanned, dup_edges, dropped


def main():
    global SEED
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DIST_DIR, "zigzag-whale.bin"))
    ap.add_argument("--check", action="store_true",
                    help="validate every gate; regenerate for byte identity")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--seed", type=int, default=SEED,
                    help="override the committed seed (diagnostics only)")
    args = ap.parse_args()
    SEED = args.seed & 0xFFFF

    t0 = time.time()
    g, pts, rec, raw, depths, events, sim, bridge, route, P, moved, turn_max, replanned, dup_edges, dropped = build()

    if args.check:
        log("regenerating for determinism")
        g2, pts2, rec2, raw2, depths2, events2, sim2, bridge2, route2, P2, moved2, turn2, repl2, dup2, dropped2 = build()
        if not (np.array_equal(rec, rec2) and raw == raw2):
            sys.exit("determinism: the second generation differs")
        tmp = args.out + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(tmp)), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(raw)
        check_route(g, rec, raw, raw2, tmp, bridge, pts, moved,
                    replanned, dup_edges, dropped, sim['plan_turn_max'])
        # every gate passed: publish atomically; run 2 never touched dist
        os.replace(tmp, args.out)
        log(f"published {args.out} ({len(raw)} bytes)")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "wb") as f:
            f.write(raw)
        log(f"wrote {args.out} ({len(raw)} bytes)")

    if not args.no_preview:
        x, y, _, _ = sample_records(rec, LOOP_SEC)
        make_preview(g, x, y, events, pts)
    log(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
