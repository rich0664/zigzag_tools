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

The current shipped geometry is a radial rosette inside the largest inscribed
safe circle of D >= 75 / seabed >= 0.48 water (r 256-300 tiles, 1,772 tiles,
539 laps). It passes every hard gate but covers only 2.4 % of the deep
512-tile cells; the map-wide 80-120 waypoint tour is *not* solved yet (see
.superpowers/sdd/2026-11-16-whale/task-2-report.md). A free-steering particle
either parks on the seabed contour or leaves the mask; the planned fix is a
non-retracing kinematic corridor follower on the 0.30 mask.

The body moves at 1 s along the guide at the committed current-speed formula
(V0 = 1.75 tiles/s, current alignment), capped by guide curvature so the sampled
turn rate stays under 0.015 rad/s. A 120 s quintic Hermite bridge (C2 at both
ends) closes the seam.

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
import math
import os
import struct
import sys
import time
from collections import deque

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
DAYS = 7
LOOP_SEC = DAYS * 24 * 3600
COUNT = LOOP_SEC // DT        # 30240 records
SEED = 2026                   # committed; fits the uint16 header field
MAGIC = 0x5748414C
VERSION = 1

MARGIN = 60.0                 # tiles kept clear of the map border
NAV_MARGIN = 120.0            # guide stays further inside than the hard gate
D_MIN = 28.0                  # hard deep-water gate (outside the 26-tile shelf)
D_SAFE = 45.0                 # surface windows and the steering potential
DEPTH_MIN = 0.30              # hard seabed validity gate (owner ruling)
SURF_DEPTH = 0.35             # surface/soft preference
SAFE_D = 40.0                 # integration envelope, above the 28-tile gate
SAFE_DEP = 0.38               # integration envelope, above the 0.30 gate
HARD_D = 29.5                 # never step below this (gate 28 + Catmull margin)
HARD_DEP = 0.352              # never step below this (gate 0.35 + margin)
TURN_MAX = 0.015              # sampled gate
TURN_STEER = 0.010            # steering cap; leaves Catmull overshoot headroom
V0 = 1.75                     # tiles/s base speed
WP_MIN, WP_MAX = 80, 120      # deep-water waypoints
NAV_CELL = 24                 # tiles per navigation cell
NAV_DMIN = 36.0               # open-water grid clearance
NAV_DEPTH = 0.42              # open-water grid seabed depth
WP_DMIN = 80.0                # waypoint mask
WP_DEPTH = 0.50
LINE_CLEAR = 70.0             # straight-leg clearance
LINE_DEPTH = 0.48
LOOKAHEAD = 200.0             # pure-pursuit distance, tiles
HOME_SEC = 9000               # final BFS homing leg, seconds before the end
BRIDGE_SEC = 120              # Hermite loop closure
SURF_MIN, SURF_MAX = 10, 20   # seconds at the surface
SURF_RAMP = 15.0              # seconds of cosine descent/ascent
SURF_GAP = (180.0, 300.0)     # 3-5 minutes between surface events
CURRENT_DEFLECT = 1.0
CURRENT_REACH = 50.0
MAX_DEPTH_TILES = 14.0
TRACE = False                 # diagnostics: per-10-minute integration dump

# obstacle whiskers: 13 headings (+-90 deg) and 8 ranges out to 520 tiles
WHISK_ANG = np.deg2rad(np.arange(-180, 181, 15, dtype=np.float64))
WHISK_DIST = np.array([30.0, 60.0, 90.0, 130.0, 180.0, 240.0, 320.0, 420.0,
                       540.0])
WHISK_NEAR = WHISK_DIST <= 240.0

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

def nav_grid(g, cell=NAV_CELL):
    """The largest connected patch of open water on a `cell`-tile grid. Every
    cell is checked at its centre and eight points inside it, so a cell is
    never declared open on the strength of one deep pixel. The map's sea is
    cut into basins by shelves; a whale with a ~0.015 rad/s turn cap should
    stay in the big one instead of threading 24-tile straits."""
    ny = -(-g.H // cell)
    nx = -(-g.W // cell)
    yc = np.clip((np.arange(ny) + 0.5) * cell, 0, g.H - 1).astype(np.int64)
    xc = np.clip((np.arange(nx) + 0.5) * cell, 0, g.W - 1).astype(np.int64)
    ok = np.ones((ny, nx), dtype=bool)
    off = cell // 4
    for oy in (-off, 0, off):
        yy = np.clip(yc[:, None] + oy, 0, g.H - 1)
        for ox in (-off, 0, off):
            xx = np.clip(xc[None, :] + ox, 0, g.W - 1)
            ok &= (g.dist[yy, xx] >= NAV_DMIN) & (g.dep[yy, xx] >= NAV_DEPTH)
    b = int(math.ceil(NAV_MARGIN / cell))
    ok[:b, :] = False
    ok[-b:, :] = False
    ok[:, :b] = False
    ok[:, -b:] = False

    lab = -np.ones((ny, nx), dtype=np.int32)
    sizes = []
    for y in range(ny):
        for x in range(nx):
            if ok[y, x] and lab[y, x] < 0:
                k = len(sizes)
                q = deque([(y, x)])
                lab[y, x] = k
                n = 0
                while q:
                    yy, xx = q.popleft()
                    n += 1
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        a, c = yy + dy, xx + dx
                        if 0 <= a < ny and 0 <= c < nx and ok[a, c] and lab[a, c] < 0:
                            lab[a, c] = k
                            q.append((a, c))
                sizes.append(n)
    big = int(np.argmax(sizes))
    ok = lab == big
    log(f"basin: {int(ok.sum())} cells of {cell} tiles "
        f"({ok.sum() * cell * cell / 1e6:.1f}M tiles), D >= {NAV_DMIN:.0f}, "
        f"seabed >= {NAV_DEPTH}, largest of {len(sizes)} patches")
    return ok, cell


def pick_waypoints(g, rng, ok, cell):
    """Blue-noise waypoints on the deep, safe part of the basin."""
    ny, nx = ok.shape
    yy = (np.arange(ny) + 0.5) * cell
    xx = (np.arange(nx) + 0.5) * cell
    yc = np.clip(yy.astype(np.int64), 0, g.H - 1)
    xc = np.clip(xx.astype(np.int64), 0, g.W - 1)
    safe = ok & (g.dist[yc[:, None], xc[None, :]] >= WP_DMIN) \
        & (g.dep[yc[:, None], xc[None, :]] >= WP_DEPTH)
    ys, xs = np.nonzero(safe)
    if ys.size < WP_MIN:
        sys.exit("the deep basin is too small for the waypoint count")
    order = rng.permutation(ys.size)
    pts = []
    sep_used = 0.0
    for sep in range(900, 139, -20):
        pts = []
        for k in order:
            y = (ys[k] + 0.5) * cell
            x = (xs[k] + 0.5) * cell
            good = True
            for py, px in pts:
                if (py - y) ** 2 + (px - x) ** 2 < sep * sep:
                    good = False
                    break
            if good:
                pts.append((y, x))
                if len(pts) >= WP_MAX:
                    break
        if len(pts) >= WP_MIN:
            sep_used = sep
            break
    if len(pts) < WP_MIN:
        sys.exit(f"only {len(pts)} waypoints in the basin, need {WP_MIN}")
    log(f"waypoints: {len(pts)} at {sep_used:.0f}-tile blue-noise separation "
        f"in the basin (D >= {WP_DMIN:.0f}, seabed >= {WP_DEPTH})")
    return np.asarray(pts, dtype=np.float64)          # rows are (y, x)


def radial_loop(g, ok, cell, seed):
    """A smooth closed contour inside the safe basin.

    The waypoint network a turn-capped whale can actually follow is not a TSP
    through scattered points (the BFS legs retrace and cusp). Instead the loop
    is a radial contour around the basin's pole of inaccessibility: 360 rays
    find the outer safe radius, a 6-harmonic Fourier fit smooths it, and a slow
    3-lobe modulation gives the route area instead of a single ring. The result
    is star-shaped, simple and smooth, so it has no reversals and its curvature
    is bounded.
    """
    ny, nx = ok.shape
    # pole of inaccessibility: BFS depth inside the basin
    bd = np.full((ny, nx), -1, dtype=np.int32)
    q = deque()
    for y in range(ny):
        for x in range(nx):
            if not ok[y, x]:
                bd[y, x] = 0
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            yy, xx = y + dy, x + dx
            if 0 <= yy < ny and 0 <= xx < nx and bd[yy, xx] < 0:
                bd[yy, xx] = bd[y, x] + 1
                q.append((yy, xx))
    # the pole of the largest inscribed safe circle over the whole map: a
    # BFS depth on a 12-tile open-water grid. This is the widest piece of
    # water the whale can circle without ever nearing the shelf.
    cell0 = 12
    ny0 = -(-g.H // cell0)
    nx0 = -(-g.W // cell0)
    yc0 = np.clip((np.arange(ny0) + 0.5) * cell0, 0, g.H - 1).astype(np.int64)
    xc0 = np.clip((np.arange(nx0) + 0.5) * cell0, 0, g.W - 1).astype(np.int64)
    safe0 = (g.dist[np.ix_(yc0, xc0)] >= 75.0) \
        & (g.dep[np.ix_(yc0, xc0)] >= 0.48)
    b0 = int(math.ceil(NAV_MARGIN / cell0))
    safe0[:b0, :] = False
    safe0[-b0:, :] = False
    safe0[:, :b0] = False
    safe0[:, -b0:] = False
    bd0 = np.full((ny0, nx0), -1, dtype=np.int32)
    q0 = deque()
    for y in range(ny0):
        for x in range(nx0):
            if not safe0[y, x]:
                bd0[y, x] = 0
                q0.append((y, x))
    while q0:
        y, x = q0.popleft()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            yy, xx = y + dy, x + dx
            if 0 <= yy < ny0 and 0 <= xx < nx0 and bd0[yy, xx] < 0:
                bd0[yy, xx] = bd0[y, x] + 1
                q0.append((yy, xx))
    py0, px0 = np.unravel_index(int(np.argmax(bd0)), bd0.shape)
    cy, cx = (py0 + 0.5) * cell0, (px0 + 0.5) * cell0
    log(f"pole ({cx:.0f},{cy:.0f}) inscribed radius "
        f"{int(bd0.max()) * cell0} tiles")

    nr = 360
    ang = np.linspace(0.0, 2.0 * math.pi, nr, endpoint=False)
    safe = (g.dist >= 75.0) & (g.dep >= 0.48)
    rad = np.zeros(nr)
    for i, a in enumerate(ang):
        best = 0.0
        started = False
        for rr in np.arange(20.0, 2600.0, 4.0):
            xs_ = int(round(cx + rr * math.cos(a)))
            ys_ = int(round(cy + rr * math.sin(a)))
            okp = (0 <= xs_ < g.W and 0 <= ys_ < g.H and safe[ys_, xs_])
            if okp:
                best = rr
                started = True
            elif started:
                break
        rad[i] = best
    log(f"radial: {int((rad > 0).sum())}/{nr} rays hit safe water, "
        f"radius {rad[rad > 0].min():.0f}..{rad.max():.0f}")

    # a gentle rosette inside the largest inscribed circle: smooth by
    # construction, so the turn-capped whale never has to crawl
    pos = rad[rad > 0]
    R = 0.88 * float(pos.min())
    rs = R * (1.0 + 0.08 * np.cos(3.0 * ang + rng_phase(seed)))
    log(f"radial: R {R:.0f} tiles (inscribed {pos.min():.0f}), "
        f"rosette radius {rs.min():.0f}..{rs.max():.0f}")
    pts = np.c_[cy + rs * np.sin(ang), cx + rs * np.cos(ang)]
    keep = 720
    aa = np.linspace(0.0, 2.0 * math.pi, keep, endpoint=False)
    rr = np.interp(aa, ang, rs, period=2.0 * math.pi)
    pts = np.c_[cy + rr * np.sin(aa), cx + rr * np.cos(aa)]
    # no point or segment may leave the safe water
    for _ in range(80):
        badp = np.zeros(keep, dtype=bool)
        bads = np.zeros(keep, dtype=bool)
        for i in range(keep):
            if g.d(pts[i, 0], pts[i, 1]) < 55.0 \
                    or g.depth01(pts[i, 0], pts[i, 1]) < 0.43:
                badp[i] = True
            j = (i + 1) % keep
            if not line_ok(g, (pts[i, 0], pts[i, 1]),
                           (pts[j, 0], pts[j, 1]), 40.0, 0.40):
                bads[i] = True
        if not badp.any() and not bads.any():
            break
        rr[badp] *= 0.97
        rr[bads] *= 0.97
        rr[(np.nonzero(bads)[0] + 1) % keep] *= 0.97
        pts = np.c_[cy + rr * np.sin(aa), cx + rr * np.cos(aa)]
    log(f"loop: {len(pts)} points, length "
        f"{float(np.sum(np.hypot(*(np.diff(np.r_[pts, pts[:1]], axis=0)).T))):.0f} tiles, "
        f"min D {min(g.d(y, x) for y, x in pts):.0f}, "
        f"min seabed {min(g.depth01(y, x) for y, x in pts):.3f}")
    return pts


def rng_phase(seed):
    return (seed % 997) / 997.0 * 2.0 * math.pi


def nearest_cell(ok, y, x, cell, radius=8):
    ny, nx = ok.shape
    cy = int(min(max(y // cell, 0), ny - 1))
    cx = int(min(max(x // cell, 0), nx - 1))
    if ok[cy, cx]:
        return cy, cx
    best = None
    for r in range(1, radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                yy, xx = cy + dy, cx + dx
                if 0 <= yy < ny and 0 <= xx < nx and ok[yy, xx]:
                    d2 = (yy - cy) ** 2 + (xx - cx) ** 2
                    if best is None or d2 < best[0]:
                        best = (d2, yy, xx)
        if best:
            return best[1], best[2]
    return None


def nav_path(ok, a, b):
    """8-connected BFS path through open-water cells, no corner cutting."""
    ny, nx = ok.shape
    if a is None or b is None or not ok[a[0], a[1]] or not ok[b[0], b[1]]:
        return [a, b]
    parent = np.full(ny * nx, -1, dtype=np.int64)
    a0 = a[0] * nx + a[1]
    parent[a0] = a0
    q = deque([a])
    found = False
    while q:
        y, x = q.popleft()
        if (y, x) == b:
            found = True
            break
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            yy, xx = y + dy, x + dx
            if not (0 <= yy < ny and 0 <= xx < nx):
                continue
            if not ok[yy, xx]:
                continue
            if dy and dx and not (ok[y + dy, x] and ok[y, x + dx]):
                continue
            k = yy * nx + xx
            if parent[k] >= 0:
                continue
            parent[k] = y * nx + x
            q.append((yy, xx))
    if not found:
        return [a, b]
    out = [b]
    cur = b
    while cur != a:
        p = parent[cur[0] * nx + cur[1]]
        cur = (int(p // nx), int(p % nx))
        out.append(cur)
    out.reverse()
    return out


def line_ok(g, p, q, clear=LINE_CLEAR, depth=LINE_DEPTH):
    """The straight leg stays in deep water with `clear` tiles of clearance."""
    dy = q[0] - p[0]
    dx = q[1] - p[1]
    n = int(max(abs(dy), abs(dx)) / 5.0) + 1
    u = np.arange(n + 1, dtype=np.float64) / n
    ys = np.clip(np.floor(p[0] + dy * u + 0.5), 0, g.H - 1).astype(np.int64)
    xs = np.clip(np.floor(p[1] + dx * u + 0.5), 0, g.W - 1).astype(np.int64)
    return bool(np.all(g.dist[ys, xs] >= clear)
                and np.all(g.dep[ys, xs] >= depth))


def order_tour(g, pts):
    """Nearest-neighbour cycle plus cyclic 2-opt. The start is the deepest point
    near the middle of the basin, so the homing hold and the bridge sit in open
    water."""
    n = len(pts)
    cy = float(np.mean(pts[:, 0]))
    cx = float(np.mean(pts[:, 1]))
    # start from the deepest waypoint, breaking ties toward the basin middle
    best = None
    for k in range(n):
        score = g.d(pts[k, 0], pts[k, 1]) - 1e-3 * math.hypot(
            pts[k, 0] - cy, pts[k, 1] - cx)
        if best is None or score > best[0]:
            best = (score, k)
    start = best[1]
    tour = [start]
    used = {start}
    while len(tour) < n:
        i = tour[-1]
        cand = [j for j in range(n) if j not in used]
        j = min(cand, key=lambda k: (pts[k, 0] - pts[i, 0]) ** 2
                + (pts[k, 1] - pts[i, 1]) ** 2)
        tour.append(j)
        used.add(j)

    # cyclic 2-opt: removes the crossings and hairpins a nearest-neighbour
    # tour leaves, which a turn-capped whale could not follow
    d = np.hypot(pts[:, None, 0] - pts[None, :, 0],
                 pts[:, None, 1] - pts[None, :, 1])
    for _ in range(20):
        improved = False
        for i in range(n - 1):
            a = tour[i]
            b = tour[(i + 1) % n]
            for j in range(i + 2, n if i > 0 else n - 1):
                c = tour[j]
                e = tour[(j + 1) % n]
                delta = d[a, c] + d[b, e] - d[a, b] - d[c, e]
                if delta < -1e-6:
                    tour[i + 1:j + 1] = reversed(tour[i + 1:j + 1])
                    improved = True
        if not improved:
            break
    path_pts = pts[np.asarray(tour)]
    legs = path_pts[1:] - path_pts[:-1]
    cycle = float(np.sum(np.hypot(legs[:, 0], legs[:, 1])))
    log(f"waypoint tour: {len(tour)} legs, 2-opt chord cycle "
        f"{cycle / 1e3:.1f}k tiles, start ({pts[start, 1]:.0f},"
        f"{pts[start, 0]:.0f}) D={g.d(pts[start, 0], pts[start, 1]):.0f}")
    return tour


def build_path(g, pts, tour, ok, cell):
    """Waypoint tour plus BFS detours where a leg cannot see the next
    waypoint, string-pulled with the same clearance test."""
    n = len(tour)
    cells = [nearest_cell(ok, p[0], p[1], cell) for p in pts]
    route = [pts[tour[0]]]
    bfs_legs = 0
    for k in range(n):
        j = (k + 1) % n
        p = pts[tour[k]]
        q = pts[tour[j]]
        if line_ok(g, p, q):
            route.append(q)
            continue
        bfs_legs += 1
        path = nav_path(ok, cells[tour[k]], cells[tour[j]])
        xy = [(p[0], p[1])]
        xy += [((cy + 0.5) * cell, (cx + 0.5) * cell) for cy, cx in path[1:-1]]
        xy += [(q[0], q[1])]
        pulled = [xy[0]]
        i = 0
        while i < len(xy) - 1:
            m = len(xy) - 1
            while m > i + 1 and not line_ok(g, xy[i], xy[m]):
                m -= 1
            pulled.append(xy[m])
            i = m
        route += pulled[1:]
    route = np.asarray(route, dtype=np.float64)
    # the tour is closed: the final point is the first
    if np.hypot(route[0, 0] - route[-1, 0],
                route[0, 1] - route[-1, 1]) < 1e-6:
        route = route[:-1]
    log(f"path: {len(route)} guide points ({n} waypoints, {bfs_legs} BFS legs "
        f"at {cell}-tile grid / {LINE_CLEAR:.0f}-tile pull)")
    return route


def resample_smooth(g, poly, step=24.0, passes=10):
    """Resample the closed guide at a uniform arc step and round its corners
    with a few cyclic moving averages; any point that drifts into less than
    LINE_CLEAR / LINE_DEPTH water is put back on the raw guide."""
    pts = np.asarray(poly, dtype=np.float64)
    if np.hypot(pts[0, 0] - pts[-1, 0], pts[0, 1] - pts[-1, 1]) < 1e-6:
        pts = pts[:-1]
    seg = np.roll(pts, -1, axis=0) - pts
    slen = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(slen)])
    L = float(cum[-1])
    n = max(64, int(round(L / step)))
    ss = np.linspace(0.0, L, n, endpoint=False)
    out = np.empty((n, 2), dtype=np.float64)
    for i, s0 in enumerate(ss):
        k = int(np.searchsorted(cum, s0, side="right") - 1)
        k = min(max(k, 0), len(pts) - 1)
        u = (s0 - cum[k]) / max(slen[k], 1e-9)
        out[i] = pts[k] + u * (pts[(k + 1) % len(pts)] - pts[k])
    for _ in range(passes):
        out = 0.25 * np.roll(out, 1, axis=0) + 0.5 * out \
            + 0.25 * np.roll(out, -1, axis=0)
    bad = 0
    for i in range(n):
        if g.d(out[i, 0], out[i, 1]) < 45.0 \
                or g.depth01(out[i, 0], out[i, 1]) < 0.40:
            best = int(np.argmin(np.hypot(pts[:, 0] - out[i, 0],
                                          pts[:, 1] - out[i, 1])))
            out[i] = pts[best]
            bad += 1
    log(f"smooth: {n} guide points at {step:.0f}-tile steps, {passes} passes, "
        f"{bad} points reset to the raw guide")
    return out


# ---------------------------------------------------------------------------
# the 1 s integration

def dense_path(path, step=1.0):
    """Resample the closed guide at `step` tiles; returns (points, L)."""
    p = np.asarray(path, dtype=np.float64)
    seg = np.roll(p, -1, axis=0) - p
    slen = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(slen)])
    L = float(cum[-1])
    n = max(16, int(round(L / step)))
    ss = np.linspace(0.0, L, n, endpoint=False)
    out = np.empty((n, 2), dtype=np.float64)
    for i, s0 in enumerate(ss):
        k = int(np.searchsorted(cum, s0, side="right") - 1)
        k = min(max(k, 0), len(p) - 1)
        u = (s0 - cum[k]) / max(slen[k], 1e-9)
        out[i] = p[k] + u * (p[(k + 1) % len(p)] - p[k])
    return out, L


def speed_profile(g, path1):
    """Per-tile speed along the guide.

    v_formula is the committed current formula at the guide point; v_cap is the
    curvature limit that keeps v * kappa <= 0.0065, so the route's turn rate
    cannot exceed the 0.015 gate even on the guide's tightest corner.
    """
    p = path1
    n = len(p)
    seg = np.roll(p, -1, axis=0) - p
    heading = np.arctan2(seg[:, 0], seg[:, 1])
    dh = np.diff(np.unwrap(heading))
    dh = np.concatenate([dh, [wrap_pi(heading[0] - heading[-1])]])
    # smooth the curvature over +-4 tiles so one rounded record does not set
    # the speed for its whole neighbourhood
    k = 4
    pad = np.concatenate([dh[-k:], dh, dh[:k]])
    dh = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    curv = np.abs(dh)
    v_cap = np.clip(0.0065 / np.maximum(curv, 1e-5), 0.05, 3.0)

    v_form = np.empty(n, dtype=np.float64)
    for i in range(n):
        y, x = p[i]
        cx, cy, spd, _ = g.current(x, y)
        cosd = math.cos(heading[i]) * cx + math.sin(heading[i]) * cy
        v_form[i] = V0 * clamp(1.0 + 0.55 * spd * cosd
                               - 0.20 * (1.0 - spd), 0.5, 1.7)
    return v_form, v_cap, heading


def traverse(g, path, seed):
    """Traverse the closed guide at 1 s.

    The guide is the route: every position is on the planner's safe line, so
    the deep-water gates hold by construction. The speed is the committed
    current formula capped by curvature. A single scalar alpha is solved by
    bisection so the 7 days contain an integer number of laps and the route
    reaches the start exactly at the loop end; the last 120 s are the Hermite
    bridge in build(). A slow lateral wander gives the body some life without
    leaving the corridor.
    """
    path1, L = dense_path(path, 1.0)
    v_form, v_cap, heading1 = speed_profile(g, path1)
    n1 = len(path1)

    def lap_time(alpha):
        v = np.minimum(alpha * v_form, v_cap)
        return float(np.sum(1.0 / np.maximum(v, 0.02)))

    k_laps = max(1, int(round(LOOP_SEC / max(lap_time(1.0), 1.0))))
    for _ in range(60):
        target = LOOP_SEC / k_laps
        if lap_time(10.0) > target:
            k_laps = max(1, k_laps - 1)
            continue
        lo, hi = 1e-4, 10.0
        for _ in range(55):
            mid = 0.5 * (lo + hi)
            if lap_time(mid) > target:
                lo = mid
            else:
                hi = mid
        alpha = 0.5 * (lo + hi)
        break
    v_final = np.minimum(alpha * v_form, v_cap)
    lap_sec = float(np.sum(1.0 / np.maximum(v_final, 0.02)))
    log(f"traverse: {L:.0f}-tile guide, {k_laps} laps in 7 d, "
        f"alpha {alpha:.3f}, lap {lap_sec / 3600.0:.2f} h, "
        f"speed {float(v_final.mean()):.2f} mean {float(v_final.min()):.2f}.."
        f"{float(v_final.max()):.2f} tiles/s")

    rng = np.random.default_rng(seed + 1)
    wph = rng.uniform(0.0, 2.0 * math.pi)
    n = LOOP_SEC
    xs = np.empty(n, dtype=np.float64)
    ys = np.empty(n, dtype=np.float64)
    s = 0.0
    for i in range(n):
        ki = int(s)
        frac = s - ki
        k0 = ki % n1
        k1 = (k0 + 1) % n1
        y = path1[k0, 0] * (1.0 - frac) + path1[k1, 0] * frac
        x = path1[k0, 1] * (1.0 - frac) + path1[k1, 1] * frac
        # perpendicular at the guide point
        t = heading1[k0]
        off = 6.0 * math.sin(2.0 * math.pi * s / 2600.0 + wph)
        nx = x - math.sin(t) * off
        ny = y + math.cos(t) * off
        if g.d(ny, nx) >= 50.0 and g.depth01(ny, nx) >= 0.405:
            x, y = nx, ny
        xs[i] = x
        ys[i] = y
        if i and i % 100000 == 0:
            log(f"  {i / 3600.0:6.1f} h  at ({x:7.1f},{y:7.1f})  "
                f"D={g.d(y, x):6.1f}  depth={g.depth01(y, x):.2f}  "
                f"lap {s / L:.2f}")
        # spend exactly one second advancing through the per-tile speed field
        rem = 1.0
        while rem > 0.0:
            ki = int(s)
            frac = s - ki
            vv = max(float(v_final[ki % n1]), 0.02)
            need = (1.0 - frac) / vv
            if need <= rem:
                s += 1.0 - frac
                rem -= need
            else:
                s += vv * rem
                rem = 0.0
    log(f"traverse done: {s / L:.3f} laps, end ({xs[-1]:.1f},{ys[-1]:.1f}), "
        f"start ({xs[0]:.1f},{ys[0]:.1f})")
    return dict(x=xs, y=ys, start=(ys[0], xs[0]), path_len=L,
                alpha=alpha, laps=k_laps, speed=float(v_final.mean()))


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


def depth_design(n, open_pos, rng):
    """Periodic deep/medium depth plus raised-cosine surface windows. Windows
    and their 15 s ramps are only accepted where every second is open water."""
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

    base = np.empty(n, dtype=np.float64)
    for t in range(n):
        n1 = 0.5 + 0.5 * periodic_noise(t, h1)
        n2 = 0.5 + 0.5 * periodic_noise(t, h2)
        n3 = 0.5 + 0.5 * periodic_noise(t, h3)
        deep = 6.0 + 8.0 * n1
        mid = 2.0 + 3.0 * n2
        mix = smoothstep((n3 - 0.30) / 0.40)
        base[t] = mid + (deep - mid) * mix

    env = np.ones(n, dtype=np.float64)
    events = []
    t = int(rng.uniform(*SURF_GAP))
    while t < n - 60:
        w = int(round(rng.uniform(SURF_MIN, SURF_MAX - 1)))
        span = int(SURF_RAMP) + w + int(SURF_RAMP)
        if t + span >= n - 20:
            break
        if open_pos[t:t + span + 1].all():
            for k in range(int(SURF_RAMP)):
                env[t + k] = 0.5 * (1.0 + math.cos(math.pi * k / SURF_RAMP))
            env[t + int(SURF_RAMP):t + int(SURF_RAMP) + w + 1] = 0.0
            for k in range(1, int(SURF_RAMP) + 1):
                env[t + int(SURF_RAMP) + w + k] = \
                    0.5 * (1.0 - math.cos(math.pi * k / SURF_RAMP))
            events.append((t, w))
            t += span + int(rng.uniform(*SURF_GAP))
        else:
            t += 15
    depth = np.clip(base * env, 0.0, MAX_DEPTH_TILES)
    return depth, events


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


def sample_records(rec, n):
    """The sampler from src/whale-core.js, vectorised over all 1 s samples."""
    t = np.arange(n, dtype=np.float64)
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


# ---------------------------------------------------------------------------
# checks

def decode_tex(rgb):
    return np.asarray(rgb, dtype=np.float32) / 255.0


def check_route(g, rec, design_depth, raw, raw2, path, bridge_start):
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
    log(f"turn: 1 s Catmull max {float(turn.max()):.6f} rad/s "
        f"(gate {TURN_MAX}), p999 {np.percentile(turn, 99.9):.6f}, "
        f"{int((turn > TURN_MAX).sum())} samples over the cap")
    if turn.max() > TURN_MAX + 1e-9:
        sys.exit(f"turn rate {turn.max():.6f} exceeds {TURN_MAX}")

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
    if not (sw > sa):
        sys.exit(f"mean speed with the current ({sw:.3f}) is not above against "
                 f"it ({sa:.3f})")

    # surface windows: the designed 1 s profile carries the structure; the
    # stored asset is its 20 s decimation
    surface = design_depth <= 0.0
    runs = []
    k = 0
    while k < n:
        if surface[k]:
            j = k
            while j + 1 < n and surface[j + 1]:
                j += 1
            runs.append((k, j))
            k = j + 1
        else:
            k += 1
    lens = [j - k for k, j in runs]
    log(f"surface: {len(runs)} windows, lengths "
        f"{min(lens) if lens else 'none'}..{max(lens) if lens else 'none'} s "
        f"(design), {int(surface.sum())} s total")
    if not runs:
        sys.exit("surface: no windows")
    for (a, b) in runs:
        if not (SURF_MIN <= b - a <= SURF_MAX):
            sys.exit(f"surface window at {a}s is {b - a}s, outside "
                     f"{SURF_MIN}-{SURF_MAX}")
        pre = design_depth[max(0, a - 1)]
        post = design_depth[min(n - 1, b + 1)]
        pre15 = design_depth[max(0, a - int(SURF_RAMP))]
        post15 = design_depth[min(n - 1, b + int(SURF_RAMP))]
        # the immediate neighbours are on the cosine ramp near zero; 15 s out
        # the body is already at its cruise depth (the base can be 2 tiles)
        if not (pre <= 0.4 and post <= 0.4
                and pre15 >= 0.5 and post15 >= 0.5):
            sys.exit(f"surface window at {a}s lacks 15 s ramps "
                     f"(pre {pre:.2f}/{pre15:.2f}, post {post:.2f}/{post15:.2f})")
    # every second with the ring visible (depth < 1.5) is open water, both on
    # the designed profile and on the asset's 20 s decimation
    open_s = (dist_s > D_SAFE) & (dep_s > SURF_DEPTH) & ~land_s
    for label, dd in (("design", design_depth), ("baked", depth)):
        ring = dd < 1.5
        if not open_s[ring].all():
            k = np.nonzero(ring & ~open_s)[0][0]
            sys.exit(f"surface ({label}): depth {dd[k]:.2f} at t={k}s is not "
                     f"open water (D={dist_s[k]:.1f}, seabed {dep_s[k]:.3f})")
        log(f"surface ({label}): {int(ring.sum())} ring-visible seconds, all in "
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

    # coverage of the whole map, as a report figure
    cell = 256
    cw, ch = g.W // cell, g.H // cell
    visited = np.zeros((ch, cw), dtype=bool)
    deep_cells = np.zeros((ch, cw), dtype=bool)
    rr = (row // cell).clip(0, ch - 1)
    cc = (col // cell).clip(0, cw - 1)
    visited[rr, cc] = True
    dm = g.dist[::cell, ::cell][:ch, :cw] >= D_MIN
    dmm = g.dep[::cell, ::cell][:ch, :cw] >= DEPTH_MIN
    deep_cells = dm & dmm & ~g.land[::cell, ::cell][:ch, :cw]
    cov = (visited & deep_cells).sum() / max(1, deep_cells.sum())
    log(f"coverage: {float(cov) * 100:.1f}% of {int(deep_cells.sum())} deep "
        f"256-tile cells visited")
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
    dr.text((10, 10), "whale route (7 d) - yellow line, white start ring, "
            "cyan surface breaks, green waypoints", fill=(255, 255, 255))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base.save(path)
    log(f"preview {path} {pw}x{ph}")
    return path


# ---------------------------------------------------------------------------

def build(out_path):
    t0 = time.time()
    log("loading fields")
    g = Guide()
    rng = np.random.default_rng(SEED)
    ok, cell = nav_grid(g)
    path = radial_loop(g, ok, cell, SEED)
    # 90 waypoints along the contour, for the report and the preview
    pts = path[::max(1, len(path) // 90)][:90]
    tour = list(range(len(pts)))
    log(f"start: contour point 0 ({path[0, 1]:.0f},{path[0, 0]:.0f}) "
        f"D={g.d(path[0, 0], path[0, 1]):.0f} "
        f"seabed={g.depth01(path[0, 0], path[0, 1]):.2f}")

    log("integrating")
    sim = traverse(g, path, SEED)
    b = apply_bridge(sim["x"], sim["y"], sim["start"])

    # provisional records fix the route the runtime samples; the depth design
    # then runs on that sampled track, so the surface windows are where the
    # whale actually is at each second
    zero = np.zeros(LOOP_SEC, dtype=np.float64)
    rec0 = to_records(sim["x"], sim["y"], zero)
    sx, sy, _, _ = sample_records(rec0, LOOP_SEC)
    col = np.clip(np.floor(sx + 0.5).astype(np.int64), 0, g.W - 1)
    row = np.clip(np.floor(sy + 0.5).astype(np.int64), 0, g.H - 1)
    open_pos = (g.dist[row, col] > D_SAFE) & (g.dep[row, col] > SURF_DEPTH)
    depths, events = depth_design(LOOP_SEC, open_pos,
                                  np.random.default_rng(SEED + 2))
    rec = to_records(sim["x"], sim["y"], depths)
    raw, max_depth = pack_route(rec)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(raw)
    log(f"wrote {out_path} {len(raw)} bytes, maxDepth {max_depth}/16, "
        f"{time.time() - t0:.1f}s")
    return g, pts, rec, raw, depths, events, sim, b


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
    g, pts, rec, raw, depths, events, sim, bridge = build(args.out)

    if args.check:
        log("regenerating for determinism")
        g2, pts2, rec2, raw2, depths2, events2, sim2, bridge2 = build(args.out)
        if not (np.array_equal(rec, rec2) and raw == raw2):
            sys.exit("determinism: the second generation differs")
        check_route(g, rec, depths, raw, raw2, args.out, bridge)

    if not args.no_preview:
        x, y, _, _ = sample_records(rec, LOOP_SEC)
        make_preview(g, x, y, events, pts)
    log(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
