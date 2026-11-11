#!/usr/bin/env python3
"""Sea texture editor.

    python3 tools/sea_editor.py           -> http://127.0.0.1:3007

A small local editor for the sea depth texture. The pipeline is a fixed chain
of nodes, each with its own parameters, an enable toggle and a cached output;
mask nodes can be wired into any effect node to modulate it by distance,
height or noise.

Change a parameter and only that node (and the ones downstream) recompute, so
iterating on a late stage does not redo the expensive distance/erosion work.
"""
import hashlib
import io
import argparse
import json
import os
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

import sea_core as sc

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK = os.path.join(ROOT, "out", "sea")
MAP_PNG = os.path.join(WORK, "map.png")
PRESETS = os.path.join(WORK, "presets")
PORT = 3007   # override with --port

_lock = threading.Lock()          # guards STATE mutation + preview bytes
_render_lock = threading.Lock()   # one render at a time
_full = None                      # whole-map land mask
_cache = {}                       # node id -> (signature, value)
_version = 0
_png = None
_render = {"running": False, "stage": 0, "total": 0, "node": "",
           "started": 0.0, "elapsed": 0.0, "stamp": 0, "last_ms": 0.0,
           "error": None, "auto": False, "pending": False}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def default_state():
    return {
        "region": {"x0": 2800, "y0": 2150, "w": 1200, "h": 800, "scale": 1.0},
        "channel": "output",
        "overlay_map": False,
        "fast": False,
        "nodes": {
            "distance": dict(enabled=True, down=2),
            "height": dict(enabled=True, mode="profile", deep_dist=1600.0,
                           curve=1.0, shelf_t=0.10, slope_t=0.32, abyss_t=1.0,
                           out_curve=[[0, 0], [1, 1]]),
            "palette": dict(enabled=True,
                            stops=[0.0, 0.03, 0.07, 0.12, 0.20, 0.32, 0.46, 0.66, 1.0],
                            cols=[[74, 126, 156], [58, 104, 134], [45, 86, 116],
                                  [35, 70, 98], [28, 57, 82], [22, 46, 67],
                                  [17, 36, 53], [12, 25, 37], [7, 13, 18]]),
            "coast": dict(enabled=False, levels=4, strength=0.13,
                          r=95, g=155, b=195),
            "output": dict(enabled=True, land_t=0.58, overlay_map=False),
        },
        "filters": [
            dict(id="f1", type="erode", enabled=True, masks=[],
                 out_curve=[[0, 0], [1, 1]],
                 **FILTER_DEFS["erode"]),
        ],
        "next_filter": 2,
        "masks": {
            "depth_band": dict(source="height", tap="", scale=400, lo=0.15, hi=0.70,
                               feather=0.1, invert=False),
            "open_water": dict(source="distance", tap="", scale=400, lo=40, hi=160,
                               feather=1.0, invert=False),
            "big_noise": dict(source="noise", tap="", scale=900, lo=0.3, hi=0.7,
                              feather=1.0, invert=False),
        },
    }


STATE_PATH = os.path.join(WORK, "editor_state.json")


STATE_KEYS = ("region", "channel", "overlay_map", "nodes", "masks", "next_mask",
              "filters", "next_filter")


def save_state():
    # the filter chain is part of the state; and write through a temp file so a
    # restart during a save cannot truncate it
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: STATE[k] for k in STATE_KEYS if k in STATE}, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        for k in STATE_KEYS:
            if k in data:
                STATE[k] = data[k]
        STATE.setdefault("next_filter", len(STATE.get("filters", [])) + 1)
    except Exception:
        pass


BLEND_MODES = ["add", "multiply", "lighter", "darker"]

FILTER_DEFS = {
    "warp": dict(tiles=20.0, scale=700, seed=34),
    "noise": dict(amp=0.15, scale=350, octaves=4, seed=11, blend="add"),
    "blur": dict(radius=2),
    "features": dict(amp=0.12, scale=70, density=0.70, octaves=4, seed=9,
                     blend="add"),
    "erode": dict(window=6, layers=20, mix=1.0, blend="add"),
    "drainage": dict(scale=4, steps=4, k=2.0, m=0.5, n=1.0, cap=0.15,
                     strength=1.0, blend="add"),
    "shelf": dict(gain=1.0, power=1.0, blend="add"),
}

DESC = {
    "distance.down": "distance-field resolution: 1 = one pixel per tile, 2 = two tiles per pixel (cheaper)",
    "warp.tiles": "how far the distance field is displaced, in tiles",
    "warp.scale": "feature size of the warp, in tiles (larger = broader bends)",
    "height.mode": "ramp = one exponential falloff; profile = shelf, shelf break, slope, abyss",
    "height.deep_dist": "distance in tiles at which the floor reaches its lowest point",
    "height.curve": "exponent on the falloff, both modes: >1 deepens sooner (narrow "
                     "shallows), <1 keeps shallow water wide and drops off later",
    "height.shelf_t": "profile mode: fraction of deep_dist that is flat shelf",
    "height.slope_t": "profile mode: fraction of deep_dist where the shelf break happens",
    "height.abyss_t": "profile mode: unused end marker for the abyssal plain",
    "noise.amp": "how much the noise adds to the height (0.1 is subtle, 0.4 is strong)",
    "noise.scale": "noise feature size in tiles: small = fine grain, large = broad swells",
    "noise.octaves": "number of noise octaves; each adds finer detail",
    "noise.seed": "random seed; change for a different pattern at the same scale",
    "noise.blend": "add = offset the height, multiply = scale it by the noise",
    "features.amp": "height of the added seamounts/bumps",
    "features.scale": "size of individual features, in tiles",
    "features.density": "threshold; higher = fewer, more isolated bumps",
    "features.octaves": "noise octaves used to place the features",
    "features.seed": "random seed for feature placement",
    "erode.window": "search window in tiles; larger = longer, smoother slopes",
    "erode.layers": "number of threshold passes; more = smoother and slower",
    "erode.mix": "0 = untouched height, 1 = fully eroded",
    "drainage.scale": "resolution divisor for the drainage solve (4 = quarter res)",
    "drainage.steps": "how many incision passes; more = deeper, more developed channels",
    "drainage.k": "incision strength per pass",
    "drainage.m": "area exponent: low spreads erosion, high concentrates it into trunks",
    "drainage.n": "slope exponent: high favours steep faces",
    "drainage.cap": "maximum incision per pass, keeps channels from exploding",
    "drainage.strength": "overall multiplier on the carved depth",
    "shelf.gain": "scales the height, pushing shallow water up or down",
    "shelf.power": "exponent on the height: >1 darkens mid-depths, <1 lifts them",
    "shade.amp": "hillshade strength (0 = flat, 1.5 = strong relief)",
    "shade.lo": "lower clamp of the shade multiplier",
    "shade.hi": "upper clamp of the shade multiplier",
    "palette.enabled": "maps the height to colour",
    "coast.levels": "width of the coastal pass in pixels",
    "coast.strength": "how strongly the coastal tint is mixed in",
    "blend": "how this filter's contribution meets the height under it: "
             "add = height + contribution; multiply = height x (1 + contribution), "
             "so it fades out where the water is already deep or shallow; "
             "lighter = only the brightening part; darker = only the deepening part",
    "coast.r": "coastal tint red", "coast.g": "coastal tint green", "coast.b": "coastal tint blue",
    "output.land_t": "depth shown under the land tiles (0 = shallowest colour)",
    "mask.source": "what the mask reads: height (of the tapped node), distance, or noise",
    "mask.tap": "which node's height to read; empty = the height at the node using this mask",
    "mask.scale": "noise feature size when the source is noise",
    "mask.lo": "mask starts rising above this value",
    "mask.hi": "mask is fully on at this value",
    "mask.feather": "not used yet: the edge softness comes from lo/hi",
    "mask.invert": "flip the mask",
}

STATE = default_state()


# --------------------------------------------------------------------------
# region + fields
# --------------------------------------------------------------------------
def load_map():
    global _full
    if _full is None:
        if not os.path.exists(MAP_PNG):
            raise SystemExit("map raster missing: run tools/build_sea.py once")
        _full = np.array(Image.open(MAP_PNG).convert("RGBA"))[:, :, 3] > 0
    return _full


def load_region(region):
    full = load_map()
    s = float(region.get("scale", 1.0))
    x0, y0 = int(region["x0"]), int(region["y0"])
    w = max(32, int(int(region["w"]) * s))
    h = max(32, int(int(region["h"]) * s))
    margin = 200
    H, W = full.shape
    xs = max(0, x0 - margin)
    ys = max(0, y0 - margin)
    xe = min(W, x0 + w + margin)
    ye = min(H, y0 + h + margin)
    sub = full[ys:ye, xs:xe]
    d_sub = sc.edt(sub, down=int(STATE["nodes"]["distance"]["down"]))
    d = d_sub[(y0 - ys):(y0 - ys) + min(h, H - y0), (x0 - xs):(x0 - xs) + min(w, W - x0)]
    land = sub[(y0 - ys):(y0 - ys) + d.shape[0], (x0 - xs):(x0 - xs) + d.shape[1]]
    return land, d


def sig(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=float).encode()).hexdigest()[:16]


def box2(a):
    return sc.box_blur(a, 1, 1)


def sorted_curve(pts):
    """control points as strictly increasing x in [0,1]; None if unusable"""
    p = sorted(((float(a), float(b)) for a, b in pts), key=lambda t: t[0])
    n = len(p)
    if n < 2:
        return None
    gap = min(0.01, 0.9 / (n - 1))
    xs = [min(1.0, max(0.0, x)) for x, _ in p]
    ys = [y for _, y in p]
    # duplicates (two points on x=1 is easy to make by dragging) must not drop a
    # point: spread them out, then pull the whole run back inside [0,1]
    for i in range(1, n):
        xs[i] = max(xs[i], xs[i - 1] + gap)
    for i in range(n - 1, -1, -1):
        xs[i] = min(xs[i], 1.0 - (n - 1 - i) * gap)
    return np.array(xs, dtype=np.float32), np.array(ys, dtype=np.float32)


def blend_into(h, c, mode):
    """combine a filter's contribution with the height under it"""
    if mode == "multiply":     # scales with the current height, sign preserved
        return np.clip(h * (1.0 + c), 0, 1).astype(np.float32)
    if mode == "lighter":      # only the brightening part
        return np.clip(h + np.maximum(c, 0.0), 0, 1).astype(np.float32)
    if mode == "darker":       # only the darkening part
        return np.clip(h + np.minimum(c, 0.0), 0, 1).astype(np.float32)
    return np.clip(h + c, 0, 1).astype(np.float32)


def apply_curve(delta, pts):
    c = sorted_curve(pts)
    if c is None:
        return delta
    xs, ys = c
    return np.sign(delta) * np.interp(np.abs(delta), xs, ys).astype(np.float32)


def mask_field(mask, ctx, fallback=None):
    src = mask["source"]
    if src == "distance":
        v = ctx["d"]
    elif src == "height":
        tap = mask.get("tap") or ""
        v = ctx.get("fields", {}).get(tap)
        if v is None:
            v = fallback if fallback is not None else ctx["h"]
    else:
        v = sc.fbm(ctx["d"].shape[1], ctx["d"].shape[0], max(8.0, float(mask["scale"])),
                   4, seed=int(mask.get("seed", 3)))
    lo, hi = float(mask["lo"]), float(mask["hi"])
    t = sc.smoothstep((v - lo) / max(1e-6, hi - lo))
    return 1.0 - t if mask["invert"] else t


def mask_weight(node, ctx, nid=None):
    ids = node.get("masks") or []
    if not ids:
        return 1.0
    w = 1.0
    for mid in ids:
        m = STATE["masks"].get(mid)
        if m:
            w = w * mask_field(m, ctx, ctx.get("h"))
    return w


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------
def n_distance(node, ctx, prev_sig):
    return ctx["d"], None


def n_warp(node, ctx, prev_sig):
    d = ctx["d"]
    H, W = d.shape
    w1 = (sc.fbm(W, H, float(node["scale"]), 3, seed=21) - 0.5) * float(node["tiles"])
    w2 = (sc.fbm(W, H, float(node["scale"]), 3, seed=22) - 0.5) * float(node["tiles"])
    yi, xi = np.mgrid[0:H, 0:W]
    out = d[np.clip((yi + w1).astype(np.int32), 0, H - 1),
            np.clip((xi + w2).astype(np.int32), 0, W - 1)]
    return out.astype(np.float32), None


def n_height(node, ctx, prev_sig):
    d = ctx["d"]
    g = max(1e-3, float(node["curve"]))
    dmax = float(node["deep_dist"])
    if node.get("mode", "profile") == "ramp":
        h = np.clip(1.0 - d / dmax, 0, 1) ** g
    else:
        # piecewise margin profile: flat shelf, a fast shelf break, a slope,
        # then the abyssal plain
        xs = np.array([0.0, float(node["shelf_t"]) * dmax,
                       float(node["slope_t"]) * dmax, dmax], dtype=np.float32)
        ys = np.array([1.0, 0.93, 0.45, 0.0], dtype=np.float32)
        h = np.interp(np.clip(d, 0, dmax), xs, ys) ** g
    return h.astype(np.float32), None


def n_noise(node, ctx, prev_sig):
    h = ctx["h"]
    H, W = h.shape
    n = sc.fbm(W, H, float(node["scale"]), int(node["octaves"]), seed=int(node["seed"])) - 0.5
    w = mask_weight(node, ctx) * float(node["amp"])
    c = 2.0 * w * n
    return np.clip(h + c, 0, 1), c


def n_features(node, ctx, prev_sig):
    h = ctx["h"]
    H, W = h.shape
    n = sc.fbm(W, H, float(node["scale"]), int(node["octaves"]), seed=int(node["seed"]))
    bumps = np.clip((n - float(node["density"])) / max(1e-3, 1.0 - float(node["density"])), 0, 1) ** 1.6
    w = mask_weight(node, ctx) * float(node["amp"])
    c = w * bumps
    return np.clip(h + c, 0, 1), c


def n_erode(node, ctx, prev_sig):
    h = ctx["h"]
    p = 1.0 / 2.0
    H, W = h.shape
    SW, SH = max(8, int(W * p)), max(8, int(H * p))
    hs = np.array(Image.fromarray(h, mode="F").resize((SW, SH), Image.BICUBIC))
    he = sc.slab_erode(hs, window=max(2, int(round(float(node["window"]) * p))),
                       layers=int(node["layers"]))
    he = sc.upscale(he, (W, H))
    mix = float(node["mix"]) * mask_weight(node, ctx)
    out = np.clip(h * (1 - mix) + he * mix, 0, 1)
    return out, out - h


def n_shelf(node, ctx, prev_sig):
    h = ctx["h"]
    w = mask_weight(node, ctx)
    gain = 1.0 + (float(node["gain"]) - 1.0) * w
    out = np.clip(h ** float(node["power"]) * gain, 0, 1)
    return out, out - h


def n_shade(node, ctx, prev_sig):
    h = ctx["h"]
    hh = box2(h)
    gy, gx = np.gradient(hh)
    nrm = np.sqrt(gx * gx + gy * gy + 1.0)
    Lv = np.array([-0.5, -0.6, 0.62]); Lv /= np.linalg.norm(Lv)
    sh = (-gx * Lv[0] - gy * Lv[1] + Lv[2]) / nrm
    ctx["shade"] = np.clip(1.0 + float(node["amp"]) * (sh - 0.62),
                           float(node["lo"]), float(node["hi"]))
    return h, None


def palette_arrays(node):
    stops = np.array(node["stops"], dtype=np.float32)
    cols = np.array(node["cols"], dtype=np.float32)
    order = np.argsort(stops)
    return stops[order], cols[order]


def n_palette(node, ctx, prev_sig):
    t = np.clip(1.0 - ctx["h"], 0, 1)
    stops, cols = palette_arrays(node)
    rgb = np.stack([np.interp(t, stops, cols[:, i]) for i in range(3)], -1)
    sh = ctx.get("shade")
    if sh is not None:
        rgb = rgb * sh[:, :, None]
    return rgb, None


def n_coast(node, ctx, prev_sig):
    rgb = ctx["color"]
    levels = max(1, int(node["levels"]))
    band = np.zeros(ctx["land"].shape, dtype=np.float32)
    cur = ctx["land"].copy()
    for k in range(1, levels + 1):
        nxt = sc.dilate1(cur)
        band = np.where(nxt & ~cur, np.float32(k), band)
        cur = nxt
    # band is 0 everywhere outside the coated rings; without this test the
    # formula turns that 0 into a rim of 1, tinting the whole ocean
    rim = np.where(band > 0,
                   np.clip(1.0 - (band - 1.0) / max(1.0, levels - 1.0), 0, 1),
                   0.0).astype(np.float32)
    tint = np.array([float(node["r"]), float(node["g"]), float(node["b"])], dtype=np.float32)
    a = (float(node["strength"]) * rim)[:, :, None]
    out = rgb + (tint - rgb) * a        # lighten-only: dark pixels move, bright stay
    return np.maximum(rgb, out), None


def n_output(node, ctx, prev_sig):
    rgb = ctx["color"].copy()
    land = ctx["land"]
    lt = node.get("land_t")
    if lt is not None:
        stops, cols = palette_arrays(STATE["nodes"]["palette"])
        fill = np.array([np.interp(float(lt), stops, cols[:, i]) for i in range(3)],
                        dtype=np.float32)
        rgb = np.where(land[:, :, None], fill, rgb)
    return np.clip(rgb, 0, 255).astype(np.uint8), None



# --------------------------------------------------------------------------
# stream-power drainage
# --------------------------------------------------------------------------
def flow_dirs(h):
    """D8 steepest descent. Returns (receiver flat index or -1, slope)."""
    H, W = h.shape
    idx = np.arange(H * W, dtype=np.int64).reshape(H, W)
    best = np.full((H, W), -1, dtype=np.int64)
    bestdrop = np.zeros((H, W), dtype=np.float32)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            y0, y1 = max(0, -dy), H - max(0, dy)
            x0, x1 = max(0, -dx), W - max(0, dx)
            nb = h[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
            ni = idx[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
            dist = 1.41421356 if (dy and dx) else 1.0
            slope = (h[y0:y1, x0:x1] - nb) / dist
            b = slope > bestdrop[y0:y1, x0:x1]
            bestdrop[y0:y1, x0:x1] = np.where(b, slope, bestdrop[y0:y1, x0:x1])
            best[y0:y1, x0:x1] = np.where(b, ni, best[y0:y1, x0:x1])
    return best.ravel(), bestdrop


def flow_accum(rec, order):
    """upstream cell count, accumulated downhill (order = high to low)"""
    acc = np.ones(rec.size, dtype=np.float32)
    for i in order:
        r = rec[i]
        if r >= 0:
            acc[r] += acc[i]
    return acc


def n_drainage(node, ctx, prev_sig):
    """stream-power incision: carve where drainage area and slope are high"""
    h = ctx["h"]
    H, W = h.shape
    sc_ = max(1, int(node["scale"]))
    SW, SH = max(8, W // sc_), max(8, H // sc_)
    cur = np.array(Image.fromarray(h, mode="F").resize((SW, SH), Image.BICUBIC))
    dh = np.zeros((SH, SW), dtype=np.float32)

    for _ in range(max(1, int(node["steps"]))):
        rec, slope = flow_dirs(cur)
        order = np.argsort(-cur.ravel(), kind="stable")
        acc = flow_accum(rec, order)
        A = (acc / max(1.0, float(acc.max()))) ** float(node["m"])
        S = np.clip(slope.ravel() / max(1e-6, float(slope.max())), 0, 1) ** float(node["n"])
        step = np.clip(float(node["k"]) * A * S, 0, float(node.get("cap", 0.1))).reshape(SH, SW)
        cur = np.clip(cur - step, 0, 1)
        dh += step

    dh_up = np.array(Image.fromarray(dh, mode="F").resize((W, H), Image.BICUBIC))
    w = mask_weight(node, ctx) * float(node.get("strength", 1.0))
    c = -dh_up * w
    return np.clip(h + c, 0, 1), c



def f_warp(h, node, ctx):
    H, W = h.shape
    sc_t = float(node["scale"])
    w1 = (sc.fbm(W, H, sc_t, 3, seed=int(node["seed"])) - 0.5) * float(node["tiles"])
    w2 = (sc.fbm(W, H, sc_t, 3, seed=int(node["seed"]) + 1) - 0.5) * float(node["tiles"])
    yi, xi = np.mgrid[0:H, 0:W]
    out = h[np.clip((yi + w1).astype(np.int32), 0, H - 1),
            np.clip((xi + w2).astype(np.int32), 0, W - 1)]
    return out.astype(np.float32), None


def f_blur(h, node, ctx):
    return sc.box_blur(h, max(1, int(round(float(node["radius"])))), 2), None


def _as_filter(fn):
    def run(h, node, ctx):
        c2 = dict(ctx)
        c2["h"] = h
        return fn(node, c2, "")
    return run


FILTER_FN = {
    "warp": f_warp,
    "noise": _as_filter(n_noise),
    "blur": f_blur,
    "features": _as_filter(n_features),
    "erode": _as_filter(n_erode),
    "drainage": _as_filter(n_drainage),
    "shelf": _as_filter(n_shelf),
}

SCALAR_NODES = {"height", "noise", "features", "erode", "drainage", "shelf"}

# what each node consumes and produces, shown in the node list
NODE_IO = {
    "distance": ("land", "distance"),
    "warp": ("distance", "distance"),
    "height": ("distance", "height"),
    "noise": ("height", "height"),
    "features": ("height", "height"),
    "erode": ("height", "height"),
    "drainage": ("height", "height"),
    "shelf": ("height", "height"),
    "shade": ("height", "shade"),
    "palette": ("height + shade", "colour"),
    "coast": ("colour", "colour"),
    "output": ("colour", "image"),
}



def evaluate(progress=None, stop_at=None, region=None):
    global _cache
    region = region or STATE["region"]
    land, d = load_region(region)
    ctx = {"land": land, "d": d, "h": None, "color": None, "fields": {}}
    steps = [("distance", None), ("height", None)]
    steps += [(f["id"], f) for f in STATE["filters"]]
    steps += [("palette", None), ("coast", None), ("output", None)]

    total = len(steps)
    prev_sig = "0"
    for i, (nid, filt) in enumerate(steps):
        if progress:
            progress(i, total, nid)
        node = filt if filt is not None else STATE["nodes"][nid]

        chain_sig = sig(prev_sig, nid, node,
                        [STATE["masks"][m] for m in node.get("masks", [])],
                        region.get("x0"), region.get("y0"), region.get("w"),
                        region.get("h"), region.get("scale"))
        prev_sig = chain_sig
        cached = _cache.get(nid)
        if cached and cached[0] == chain_sig:
            val = cached[1]
        else:
            val, contrib = None, None
            if node.get("enabled", True):
                if nid == "distance":
                    val = d
                elif nid == "height":
                    val, contrib = n_height(node, ctx, chain_sig)
                elif filt is not None:
                    val, contrib = FILTER_FN[filt["type"]](ctx["h"], node, ctx)
                elif nid == "palette":
                    val, contrib = n_palette(node, ctx, chain_sig)
                elif nid == "coast":
                    val, contrib = n_coast(node, ctx, chain_sig)
                elif nid == "output":
                    val, contrib = n_output(node, ctx, chain_sig)
                pts = node.get("out_curve")
                has_curve = bool(pts) and len(pts) >= 2
                if (isinstance(val, np.ndarray) and contrib is not None
                        and ctx.get("h") is not None):
                    c = apply_curve(contrib, pts) if has_curve else contrib
                    val = blend_into(ctx["h"], c, node.get("blend", "add"))
                elif (isinstance(val, np.ndarray) and has_curve
                        and (nid in ("height",) or filt is not None)):
                    c = sorted_curve(pts)
                    if c is not None:
                        val = np.interp(val, c[0], c[1]).astype(np.float32)
            _cache[nid] = (chain_sig, val)

        if isinstance(val, np.ndarray) and getattr(val, "ndim", 0) == 2:
            ctx["fields"][nid] = val
        if nid == "distance":
            ctx["d"] = val if val is not None else ctx["d"]
        elif nid == "height" or filt is not None:
            if val is not None:
                ctx["h"] = val
        elif nid == "palette":
            ctx["color"] = val if val is not None else np.zeros((*land.shape, 3), np.float32)
        elif nid == "coast":
            if val is not None:
                ctx["color"] = val
        elif nid == "output":
            ctx["out"] = val

        if stop_at and nid == stop_at:
            break

    return ctx


def channel_image(ctx, channel):
    if channel in STATE["masks"]:
        v = mask_field(STATE["masks"][channel], ctx, ctx.get("h"))
        v = np.asarray(v, dtype=np.float32)
        return np.stack([v * 255] * 3, -1).astype(np.uint8)
    if channel == "output":
        rgb = ctx.get("out")
        if rgb is None:
            rgb = np.clip(ctx["color"], 0, 255).astype(np.uint8)
        return rgb

    val = ctx.get(channel)
    if val is None:
        if channel == "d":
            val = ctx["d"]
        else:
            val = ctx.get("h")
    if val is None:
        val = np.zeros(ctx["land"].shape, np.float32)
    v = np.asarray(val, dtype=np.float32)
    if channel != "h":          # height is kept raw so curves are visible
        lo, hi = float(v.min()), float(v.max())
        v = (v - lo) / max(1e-6, hi - lo)
    return np.stack([v * 255] * 3, -1).astype(np.uint8)


def _progress(stage, total, node):
    _render.update(stage=stage, total=total, node=node,
                   elapsed=round(time.time() - _render["started"], 2))


def render_now(auto=False):
    """render once into the in-memory PNG (serialised by _render_lock)"""
    global _png
    with _render_lock:
        _render.update(running=True, stage=0, total=2 + len(STATE["filters"]) + 3, node="start",
                       started=time.time(), elapsed=0.0, error=None)
        t0 = time.time()
        try:
            ch = STATE.get("channel", "output")
            stop = STATE["masks"][ch].get("tap") if ch in STATE["masks"] else None
            region = dict(STATE["region"])
            fast = bool(STATE.get("fast"))
            if fast:
                region["scale"] = float(region.get("scale", 1.0)) * 0.5
            ctx = evaluate(_progress, stop_at=stop or None, region=region)
            rgb = channel_image(ctx, ch)
            if fast:      # keep the display size stable
                out_w, out_h = int(STATE["region"]["w"]), int(STATE["region"]["h"])
                rgb = np.array(Image.fromarray(rgb, "RGB").resize((out_w, out_h), Image.NEAREST))
            # the real map is a separate overlay layer in the browser, so it can
            # follow a drag without waiting for the sea render
            buf = io.BytesIO()
            Image.fromarray(rgb, "RGB").save(buf, "PNG")
            with _lock:
                _png = buf.getvalue()
                _render["stamp"] += 1
        except Exception as exp:
            _render["error"] = str(exp)
        finally:
            _render.update(running=False, last_ms=int((time.time() - t0) * 1000),
                           elapsed=round(time.time() - t0, 2), node="done")
    return True


_queue_lock = threading.Lock()
_render_thread = None


def render_async():
    """queue a render; asking while one is running renders once more after it"""
    global _render_thread
    with _queue_lock:
        _render["pending"] = True
        if _render["running"] or (_render_thread is not None
                                  and _render_thread.is_alive()):
            return True
        # report "running" before the worker starts, so a poll fired right after
        # the request does not conclude that the render is already finished
        _render.update(running=True, node="queued", stage=0, total=0,
                       started=time.time(), elapsed=0.0)
        _render_thread = threading.Thread(target=_render_loop, daemon=True)
        _render_thread.start()
    return True


def _render_loop():
    global _render_thread
    while True:
        with _queue_lock:
            if not _render.get("pending"):
                _render_thread = None
                return
            _render["pending"] = False
        render_now()


_auto_timer = None


def schedule_render(delay=0.35):
    """auto mode: debounce a burst of edits into one queued render"""
    global _auto_timer
    if _auto_timer is not None:
        _auto_timer.cancel()
    _auto_timer = threading.Timer(delay, render_async)
    _auto_timer.daemon = True
    _auto_timer.start()


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><meta charset=utf-8><title>sea editor</title>
<style>
:root{--b:#1c2530;--p:#0d141b;--t:#c9d6e2;--a:#3f7fa6}
*{box-sizing:border-box} body{margin:0;background:var(--p);color:var(--t);
font:12px/1.4 ui-monospace,monospace;display:flex;height:100vh;overflow:hidden}
#left,#right{width:260px;background:var(--b);overflow:auto;padding:8px;flex:0 0 auto}
#mid{flex:1;display:flex;flex-direction:column;min-width:0}
#top{display:flex;gap:6px;padding:6px;background:var(--b);align-items:center;flex-wrap:wrap}
#top input[type=number]{width:62px}
#top select{width:auto}
#prev{flex:1;overflow:hidden;position:relative;cursor:grab;display:flex;
align-items:center;justify-content:center;background:#0a1016}
#prev img{position:absolute;inset:0;margin:auto;image-rendering:pixelated;
max-width:100%;max-height:100%;pointer-events:none;user-select:none;-webkit-user-drag:none}
#mapov{opacity:1;transition:none}
#prev{user-select:none}
.node{padding:2px 5px;cursor:pointer;border-radius:3px;display:flex;gap:6px;align-items:center}
.node.sel{background:#24405a}.node input{cursor:pointer}
.x{margin-left:auto;color:#8fa6ba;padding:0 4px}.x:hover{color:#e08a8a}
.io{color:#6b8093;font-size:10px;margin-left:auto;white-space:nowrap}
.tap{color:#7fa9c4;font-size:10px}
label{display:block;margin:5px 0 2px;color:#8fa6ba}
input[type=number],input[type=text],select{width:100%;background:#0b1117;color:var(--t);
border:1px solid #2a3a4b;padding:2px 4px}
button{background:#24405a;color:var(--t);border:0;padding:4px 8px;cursor:pointer;border-radius:3px}
h3{margin:8px 0 4px;font-size:11px;color:#8fa6ba;text-transform:uppercase;letter-spacing:.08em}
#barwrap{height:18px;background:#0a1016;position:relative;flex:0 0 auto}
#bar{height:100%;width:0;background:#2f6a8c;transition:width .12s linear}
#prog{position:absolute;left:8px;top:0;line-height:18px;font-size:11px;color:#cfe0ee}
canvas.curve{background:#0b1117;border:1px solid #2a3a4b;display:block;margin-top:4px}
label.off{opacity:.38}
input.off,select.off{opacity:.38}
.small{color:#7d93a6}
</style>
<div id=left><h3>pipeline</h3><div id=nodes></div>
  <h3>filters <select id=ftype style="width:auto"></select>
    <button id=addfilter style="padding:1px 6px">+</button></h3>
  <div id=filters></div>
  <h3>masks <button id=addmask style="padding:1px 6px">+</button></h3><div id=masks></div></div>
<div id=mid>
  <div id=top>
    x<input id=rx type=number size=5> y<input id=ry type=number size=5>
    w<input id=rw type=number size=5> h<input id=rh type=number size=5>
    <select id=scale><option value=1>1 px/tile<option value=0.5>0.5 px/tile</select>
    <select id=chan></select>
    <button id=go>render</button>
    <label style="display:inline;color:var(--t)"><input id=auto type=checkbox> auto</label>
    <select id=presets style="width:auto;min-width:90px"></select>
    <button id=load>load</button><button id=save>save</button>
    <button id=del title="delete the selected save">×</button><button id=reset>reset</button>
  </div>
  <div id=barwrap><div id=bar></div><span id=prog></span></div>
  <div id=prev><img id=img draggable=false><img id=mapov draggable=false></div>
</div>
<div id=right><h3 id=nodeTitle>select a node</h3><div id=params></div>
  <div id=curvewrap></div><h3>masks wired</h3><div id=wired></div></div>
<script>
let S=null, sel=null, lastStamp=-1, curveDrag=null, panDrag=null;
async function api(path, body){const r=await fetch(path,{method:body?'POST':'GET',
  headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.headers.get('content-type')?.includes('json')?r.json():r.text();}
let polling=false, pollAgain=false, imgBusy=false;
async function poll(){
  if(polling){pollAgain=true;return;}
  polling=true;
  try{
    const s=await api('/api/status');
    const pct=s.running&&s.total?Math.round(100*s.stage/s.total):100;
    document.getElementById('bar').style.width=pct+'%';
    document.getElementById('prog').textContent=s.running
      ?`${s.node} ${s.stage}/${s.total} ${Number(s.elapsed).toFixed(1)}s`
      :(s.error?('error: '+s.error)
        :(imgBusy?'loading image…':(s.last_ms?`${s.last_ms} ms`:'idle')));
    if(s.stamp!==lastStamp){
      lastStamp=s.stamp;
      const im=document.getElementById('img');
      imgBusy=true;
      im.onload=()=>{imgBusy=false;poll();};
      im.onerror=()=>{imgBusy=false;};
      im.src='/api/preview?t='+s.stamp;
      im.style.transform='none';
      syncMap();
    }
    if(s.running)setTimeout(poll,250);
  }finally{
    polling=false;
    if(pollAgain){pollAgain=false;setTimeout(poll,120);}
  }
}
function render(){api('/api/render',{}).then(()=>poll());}
function mapUrl(r){
  return `/api/map?x0=${r.x0}&y0=${r.y0}&w=${Math.round(r.w)}&h=${Math.round(r.h)}&t=${Date.now()}`;
}
function syncMap(){
  const mo=document.getElementById('mapov');
  const on=!!(S.nodes.output&&S.nodes.output.overlay_map);
  mo.style.display=on?'block':'none';
  if(on) mo.src=mapUrl(region());
}
async function boot(){
  S=await api('/api/state'); window.DESC=S.desc||{};
  document.getElementById('auto').checked=!!S.auto;
  syncMap();
  rx.value=S.region.x0; ry.value=S.region.y0; rw.value=S.region.w; rh.value=S.region.h;
  scale.value=String(S.region.scale);
  renderTop(); renderNodes(); renderMasks();
  const ft=document.getElementById('ftype');
  if(ft) ft.innerHTML=Object.keys(S.filter_defs||{}).map(t=>`<option>${t}</option>`).join('');
  loadPresets();
  const known=(S.filters||[]).some(f=>f.id===S.last)||S.nodes[S.last];
  select(known?S.last:((S.filters&&S.filters[0])?S.filters[0].id:'height'));
  render();
}
function renderTop(){
  chan.innerHTML=S.channels.map(c=>`<option ${c===S.channel?'selected':''}>${c}</option>`).join('');
}
function ioLabel(d,id,inp,out,title){
  const sp=document.createElement('span'); sp.className='io'; sp.textContent=inp+' → '+out;
  sp.title=title||''; d.appendChild(sp);
}
function renderNodes(){
  const el=document.getElementById('nodes'); el.innerHTML='';
  for(const id of ['distance','height','palette','coast','output']){
    const n=S.nodes[id]; if(!n) continue;
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=n.enabled;
    cb.onclick=e=>{e.stopPropagation(); set('nodes',id,'enabled',cb.checked);};
    d.appendChild(cb); d.appendChild(document.createTextNode(id));
    const io=S.io&&S.io[id]; if(io) ioLabel(d,id,io[0],io[1],id+' takes '+io[0]+' and outputs '+io[1]);
    d.onclick=()=>select(id); el.appendChild(d);
  }
  const fe=document.getElementById('filters'); fe.innerHTML='';
  S.filters.forEach((f,i)=>{
    const d=document.createElement('div'); d.className='node'+(sel===f.id?' sel':'');
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=f.enabled;
    cb.onclick=e=>{e.stopPropagation(); set('filters',f.id,'enabled',cb.checked);};
    d.appendChild(cb); d.appendChild(document.createTextNode(f.type));
    ioLabel(d,f.id,'height','height','filter #'+f.id+' ('+f.type+')');
    const up=document.createElement('span'); up.className='x'; up.textContent='↑';
    up.onclick=e=>{e.stopPropagation(); api('/api/filter/move',{id:f.id,dir:'up'}).then(reload);};
    const dn=document.createElement('span'); dn.className='x'; dn.textContent='↓';
    dn.onclick=e=>{e.stopPropagation(); api('/api/filter/move',{id:f.id,dir:'down'}).then(reload);};
    const x=document.createElement('span'); x.className='x'; x.textContent='×';
    x.onclick=e=>{e.stopPropagation(); api('/api/filter/del',{id:f.id}).then(reload);};
    d.append(up,dn,x);
    d.onclick=()=>select(f.id); fe.appendChild(d);
  });
}
function renderMasks(){
  const el=document.getElementById('masks'); el.innerHTML='';
  for(const id of Object.keys(S.masks)){
    const m=S.masks[id];
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    d.appendChild(document.createTextNode(id));
    const sp=document.createElement('span'); sp.className='io tap';
    sp.textContent=(m.source==='height'?(m.tap||'this node'):m.source)+' → mask';
    sp.title='reads '+(m.source==='height'?('height at '+(m.tap||'the consuming node')):m.source)+
      ' and outputs a mask';
    d.appendChild(sp);
    const x=document.createElement('span'); x.className='x'; x.textContent='×';
    x.onclick=e=>{e.stopPropagation(); api('/api/mask/del',{id}).then(reload);};
    d.appendChild(x); d.onclick=()=>select(id); el.appendChild(d);
  }
}
async function loadPresets(){
  const r=await api('/api/presets');
  document.getElementById('presets').innerHTML=r.presets.map(n=>`<option>${n}</option>`).join('');
}
async function reload(){S=await api('/api/state'); window.DESC=S.desc||{}; renderTop(); renderNodes();
  renderMasks(); renderParams();}
async function select(id){sel=id; await api('/api/select',{id});
  document.getElementById('nodeTitle').textContent=id;
  renderParams(); renderNodes(); renderMasks();}
const CHOICES={
  'nodes.height.mode':['profile','ramp'],
  'nodes.distance.down':['1','2']
};
// a parameter nothing reads (wrong mode, wrong source, node switched off) is
// shown greyed out, so the panel says which knobs currently do anything
function inactiveParam(kind,id,k,o){
  if(!o) return false;
  if(kind==='nodes'){
    if(id==='height'){
      if(k==='abyss_t') return true;          // no code path reads it
      if(k==='shelf_t'||k==='slope_t') return o.mode!=='profile';
      return o.enabled===false;
    }
    if(id==='palette'||id==='coast') return o.enabled===false;
    return false;                      // distance.down still shapes the load
  }
  if(kind==='filter') return o.enabled===false;
  if(kind==='mask'){
    if(k==='feather') return true;            // not mapped into the mask weight
    if(k==='tap') return o.source!=='height';
    if(k==='scale') return o.source!=='noise';
  }
  return false;
}
function field(kind,id,k,v,opts){
  const lab=document.createElement('label'); lab.textContent=k;
  const dkey=(opts&&opts.dkey)?opts.dkey
    :((k==='blend')?'blend':((kind==='mask')?('mask.'+k):(id+'.'+k)));
  const tip=(window.DESC||{})[dkey]
    ||(k==='enabled'?'enabled: off = this node passes its input straight through':'');
  lab.title=tip;
  let inp;
  const choices=(k==='blend')?['add','multiply','lighter','darker']
    :((opts&&opts.options)||CHOICES[kind+'.'+id+'.'+k]||null);
  if(choices){
    inp=document.createElement('select');
    inp.innerHTML=choices.map(o=>`<option ${String(o)===String(v)?'selected':''}>${o}</option>`).join('');
    inp.onchange=()=>set(kind,id,k,typeof v==='number'?parseFloat(inp.value):inp.value);
  } else if(typeof v==='boolean'){
    inp=document.createElement('input'); inp.type='checkbox'; inp.checked=v;
    inp.onchange=()=>set(kind,id,k,inp.checked);
  } else if(typeof v==='number'){
    inp=document.createElement('input'); inp.type='number'; inp.step='any'; inp.value=v;
    inp.onchange=()=>set(kind,id,k,parseFloat(inp.value));
  } else {
    inp=document.createElement('input'); inp.type='text'; inp.value=v;
    inp.onchange=()=>set(kind,id,k,inp.value);
  }
  inp.title=lab.title;
  if(opts&&opts.obj&&inactiveParam(kind,id,k,opts.obj)){
    lab.classList.add('off'); inp.classList.add('off'); inp.disabled=true;
  }
  return [lab,inp];
}
function renderParams(){
  const el=document.getElementById('params'); el.innerHTML='';
  document.getElementById('curvewrap').innerHTML='';
  const node=S.nodes[sel], mask=S.masks[sel];
  const filt=(S.filters||[]).find(f=>f.id===sel);
  if(mask){
    const KEYS=['source','tap','scale','lo','hi','feather','invert'];
    for(const [k,v] of Object.entries(mask)){
      if(!KEYS.includes(k)) continue;
      if(k==='tap'){
        const o=[''].concat(S.taps);
        el.append(...field('mask',sel,k,v,{options:o,obj:mask}));
      } else if(k==='source'){
        el.append(...field('mask',sel,k,v,{options:['height','distance','noise'],obj:mask}));
      } else {
        el.append(...field('mask',sel,k,v,{obj:mask}));
      }
    }
  } else if(filt){
    const defs=(S.filter_defs||{})[filt.type]||{};
    for(const [k,v] of Object.entries(defs)){
      if(k==='masks'||k==='out_curve') continue;
      el.append(...field('filter',filt.id,k,filt[k]!==undefined?filt[k]:v,
        {dkey:filt.type+'.'+k,obj:filt}));
    }
    for(const [k,v] of Object.entries(filt)){
      if(k in defs||['id','type','enabled','masks','out_curve'].includes(k)) continue;
      el.append(...field('filter',filt.id,k,v,{dkey:filt.type+'.'+k,obj:filt}));
    }
    if(filt.out_curve){
      const w=document.getElementById('curvewrap');
      const h=document.createElement('h3'); h.textContent='output curve';
      const rs=document.createElement('button'); rs.textContent='reset';
      rs.title='reset this curve to identity';
      rs.onclick=()=>set('filters',sel,'out_curve',[[0,0],[1,1]]);
      h.appendChild(document.createTextNode(' ')); h.appendChild(rs);
      w.appendChild(h);
      w.appendChild(curveUI(filt.out_curve, pts=>set('filters',sel,'out_curve',pts)));
    }
    const wired=document.getElementById('wired'); wired.innerHTML='';
    for(const mid of Object.keys(S.masks)){
      const l=document.createElement('label'); const cb=document.createElement('input');
      cb.type='checkbox'; cb.checked=(filt.masks||[]).includes(mid);
      l.title='wire this mask into the filter: its contribution is multiplied by it';
      cb.title=l.title;
      cb.onchange=()=>{const arr=new Set(filt.masks||[]);
        cb.checked?arr.add(mid):arr.delete(mid); set('filters',sel,'masks',[...arr]);};
      l.appendChild(cb); l.appendChild(document.createTextNode(mid)); wired.appendChild(l);
    }
  } else if(sel==='palette' && node){
    paletteEditor(node);
    return;
  } else if(node){
    for(const [k,v] of Object.entries(node)){
      if(k==='masks'||k==='out_curve'||k==='enabled') continue;
      if(sel==='coast'&&['r','g','b'].includes(k)) continue;
      el.append(...field('nodes',sel,k,v,{obj:node}));
    }
    if(sel==='coast'){
      const lab=document.createElement('label'); lab.textContent='tint';
      lab.title='coastal tint colour, mixed in only where it lightens';
      const c=document.createElement('input'); c.type='color';
      c.value=hex([node.r,node.g,node.b]);
      c.title='coastal tint colour, mixed in only where it lightens';
      c.onchange=()=>{const [r,g,b]=unhex(c.value); set('nodes','coast','all',{r,g,b});};
      el.append(lab,c);
    }
    if(node.out_curve){
      const w=document.getElementById('curvewrap');
      const h=document.createElement('h3'); h.textContent='output curve';
      const rs=document.createElement('button'); rs.textContent='reset';
      rs.title='reset this curve to identity';
      rs.onclick=()=>set('nodes',sel,'out_curve',[[0,0],[1,1]]);
      h.appendChild(document.createTextNode(' ')); h.appendChild(rs);
      w.appendChild(h); w.appendChild(curveUI(node.out_curve,
        pts=>set('nodes',sel,'out_curve',pts)));
    }
    const wired=document.getElementById('wired'); wired.innerHTML='';
    for(const mid of Object.keys(S.masks)){
      const l=document.createElement('label'); const cb=document.createElement('input');
      cb.type='checkbox'; cb.checked=(node.masks||[]).includes(mid);
      l.title='wire this mask into '+sel+': the effect is multiplied by it';
      cb.title=l.title;
      cb.onchange=()=>{const arr=new Set(node.masks||[]);
        cb.checked?arr.add(mid):arr.delete(mid); set('nodes',sel,'masks',[...arr]);};
      l.appendChild(cb); l.appendChild(document.createTextNode(mid)); wired.appendChild(l);
    }
  } else { document.getElementById('wired').innerHTML=''; }
}
function hex(c){return '#'+c.map(v=>Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,'0')).join('');}
function unhex(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}
function paletteEditor(node){
  const box=document.getElementById('params');
  const stops=[...node.stops], cols=node.cols.map(c=>[...c]);
  const cv=document.createElement('canvas'); cv.width=230; cv.height=26; cv.className='curve';
  cv.title='resulting colour ramp';
  const g=cv.getContext('2d');
  const S2=[...stops].map((x,i)=>[x,i]).sort((a,b)=>a[0]-b[0]);
  for(let x=0;x<230;x++){
    const t=x/230; let c=cols[S2[0][1]];
    for(let k=1;k<S2.length;k++){
      const [x0,i0]=S2[k-1],[x1,i1]=S2[k];
      if(t<=x1||k===S2.length-1){
        const f=Math.max(0,Math.min(1,(t-x0)/Math.max(1e-6,x1-x0)));
        c=[0,1,2].map(j=>cols[i0][j]*(1-f)+cols[i1][j]*f); break;
      }
    }
    g.fillStyle=hex(c); g.fillRect(x,0,1,26);
  }
  box.appendChild(cv);
  const commit=()=>set('nodes','palette',{stops,cols});
  stops.forEach((st,i)=>{
    const row=document.createElement('div'); row.className='node';
    const p=document.createElement('input'); p.type='number'; p.step='0.01'; p.value=st;
    p.style.width='64px'; p.title='depth position of this colour stop (0 = shallowest)';
    p.onchange=()=>{stops[i]=parseFloat(p.value);commit();};
    const c=document.createElement('input'); c.type='color'; c.value=hex(cols[i]);
    c.style.width='40px'; c.title='colour at this depth';
    c.onchange=()=>{cols[i]=unhex(c.value);commit();};
    const x=document.createElement('span'); x.className='x'; x.textContent='×';
    x.onclick=()=>{ if(stops.length<=2)return; stops.splice(i,1); cols.splice(i,1); commit(); };
    row.append(p,c,x); box.appendChild(row);
  });
  const add=document.createElement('button'); add.textContent='add stop';
  add.onclick=()=>{stops.push(0.5);cols.push([40,70,100]);commit();};
  box.appendChild(add);
}
function curveUI(pts, cb){
  const size=180, c=document.createElement('canvas'); c.width=c.height=size; c.className='curve';
  const g=c.getContext('2d');
  function draw(){
    g.fillStyle='#0b1117'; g.fillRect(0,0,size,size);
    g.strokeStyle='#22303d'; g.lineWidth=1;
    for(let i=1;i<4;i++){g.beginPath();g.moveTo(i*size/4,0);g.lineTo(i*size/4,size);
      g.moveTo(0,i*size/4);g.lineTo(size,i*size/4);g.stroke();}
    const P=[...pts].sort((a,b)=>a[0]-b[0]);
    g.strokeStyle='#4f93bb'; g.lineWidth=2; g.beginPath();
    P.forEach(([x,y],i)=>{const px=x*size,py=(1-y)*size;i?g.lineTo(px,py):g.moveTo(px,py);});
    g.stroke(); g.fillStyle='#dbe7f1';
    P.forEach(([x,y])=>{g.beginPath();g.arc(x*size,(1-y)*size,4,0,7);g.fill();});
  }
  const pos=e=>{const r=c.getBoundingClientRect();
    return [(e.clientX-r.left)/size,1-(e.clientY-r.top)/size];};
  c.onmousedown=e=>{ if(e.button!==0) return; const [x,y]=pos(e);
    let best=-1,bd=1e9; pts.forEach((p,i)=>{const d=(p[0]-x)**2+(p[1]-y)**2; if(d<bd){bd=d;best=i;}});
    const i=(bd<0.02)?best:(pts.push([x,y])-1);
    // one shared drag record: the window handlers below were previously added
    // per canvas and never removed, so every curve ever drawn received the drag
    curveDrag={pts,cb,canvas:c,draw,i};
    draw(); };
  c.oncontextmenu=e=>{e.preventDefault(); const [x,y]=pos(e);
    let best=-1,bd=1e9; pts.forEach((p,i)=>{const d=(p[0]-x)**2+(p[1]-y)**2; if(d<bd){bd=d;best=i;}});
    if(bd<0.02&&pts.length>2){pts.splice(best,1);draw();cb(pts);}};
  draw(); return c;
}
addEventListener('mousemove',e=>{ if(!curveDrag) return;
  const {pts,canvas,draw,i}=curveDrag; const r=canvas.getBoundingClientRect();
  if(!r.width) return;
  const x=(e.clientX-r.left)/r.width, y=1-(e.clientY-r.top)/r.height;
  pts[i]=[Math.max(0,Math.min(1,x)),Math.max(0,Math.min(1,y))]; draw(); });
addEventListener('mouseup',()=>{ if(!curveDrag) return;
  const {pts,cb,draw}=curveDrag; curveDrag=null;
  pts.sort((a,b)=>a[0]-b[0]);
  const g=Math.min(0.01,0.9/(pts.length-1));
  for(let i=1;i<pts.length;i++) pts[i][0]=Math.max(pts[i][0],pts[i-1][0]+g);
  for(let i=pts.length-1;i>=0;i--) pts[i][0]=Math.min(pts[i][0],1-(pts.length-1-i)*g);
  draw(); cb(pts); });
async function set(kind,id,k,v){
  await api('/api/set',{id,k,v}); S=await api('/api/state'); window.DESC=S.desc||{};
  if(k==='mode'||k==='source'||k==='enabled') renderParams();
  if(S.auto) setTimeout(poll,120); else poll();
}
document.getElementById('go').onclick=render;
document.getElementById('addmask').onclick=()=>api('/api/mask/add',{}).then(reload);
document.getElementById('auto').onchange=e=>api('/api/auto',{on:e.target.checked});
document.getElementById('addfilter').onclick=()=>{
  api('/api/filter/add',{type:document.getElementById('ftype').value}).then(reload);};
document.getElementById('reset').onclick=async()=>{ if(!confirm('reset to defaults?'))return;
  await api('/api/reset',{}); await boot(); };
const presets=document.getElementById('presets');
document.getElementById('save').onclick=()=>{const n=prompt('save preset as',
  presets.value||'preset'); if(n) api('/api/preset/save',{name:n}).then(loadPresets);};
document.getElementById('load').onclick=async()=>{const n=presets.value;
  if(n){await api('/api/preset/load',{name:n}); await boot();}};
document.getElementById('del').onclick=async()=>{const n=presets.value;
  if(!n || !confirm('delete save "'+n+'"?')) return;
  await api('/api/preset/del',{name:n}); await loadPresets();};
const region=()=>({x0:+rx.value,y0:+ry.value,w:+rw.value,h:+rh.value,scale:+scale.value});
for(const id of ['rx','ry','rw','rh']) document.getElementById(id).onchange=()=>{
  const r=region(); api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;
    if(S.auto)render();else poll();});};
document.getElementById('scale').onchange=()=>{const r=region();
  api('/api/set',{id:'region',k:'all',v:r}).then(()=>{S.region=r;
    if(S.auto)render();else poll();});};
document.getElementById('chan').onchange=()=>{const v=chan.value;
  api('/api/set',{id:'channel',k:'all',v}).then(()=>{S.channel=v;render();});};
const prev=document.getElementById('prev');
let panTimer=null;
async function pushRegion(fast){
  await api('/api/set',{id:'fast',k:'all',v:fast});
  const r=region();
  await api('/api/set',{id:'region',k:'all',v:r});
  S.region=r;
  await api('/api/render',{});
  setTimeout(poll,120);
}
prev.onmousedown=e=>{panDrag={x:e.clientX,y:e.clientY};
  prev.style.cursor='grabbing';};
addEventListener('mousemove',e=>{if(!panDrag)return;
  const dx=e.clientX-panDrag.x, dy=e.clientY-panDrag.y;
  panDrag.x=e.clientX; panDrag.y=e.clientY;
  rx.value=+rx.value-Math.round(dx/S.region.scale);
  ry.value=+ry.value-Math.round(dy/S.region.scale);
  // nudge what we have immediately, then render the strip we are moving to
  const mo=document.getElementById('mapov');
  if(S.nodes.output&&S.nodes.output.overlay_map) mo.src=mapUrl(region());   // cheap: it is just a crop
  // the sea is intentionally left stale until release
});
addEventListener('mouseup',()=>{if(!panDrag)return;panDrag=null;prev.style.cursor='grab';
  pushRegion(false);});
boot();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _png(self, data):
        self.send_response(200)
        self.send_header("content-type", "image/png")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/preview"):
            if _png is None:
                self.send_response(204)
                self.end_headers()
            else:
                self._png(_png)
        elif self.path.startswith("/api/map"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                x0 = int(float(q.get("x0", [0])[0])); y0 = int(float(q.get("y0", [0])[0]))
                w = int(float(q.get("w", [1200])[0])); h = int(float(q.get("h", [800])[0]))
                tile = Image.open(MAP_PNG).convert("RGBA").crop((x0, y0, x0 + w, y0 + h))
                if tile.size != (w, h):
                    tile = tile.resize((w, h), Image.NEAREST)
                buf = io.BytesIO(); tile.save(buf, "PNG")
                self._png(buf.getvalue())
            except Exception:
                self.send_response(400); self.end_headers()
        elif self.path == "/api/presets":
            names = sorted(f[:-5] for f in os.listdir(PRESETS)) if os.path.isdir(PRESETS) else []
            self._json({"presets": names})
        elif self.path == "/api/status":
            with _lock:
                data = dict(_render)
                data["has_image"] = _png is not None
            self._json(data)
        elif self.path == "/api/state":
            with _lock:
                taps = ["base"] + [f["id"] for f in STATE["filters"]]
                st = {"region": STATE["region"], "channel": STATE["channel"],
                      "overlay_map": bool(STATE["nodes"]["output"].get("overlay_map")),
                      "nodes": STATE["nodes"], "masks": STATE["masks"],
                      "filters": STATE["filters"], "filter_defs": FILTER_DEFS,
                      "order": ["distance", "height"] + [f["id"] for f in STATE["filters"]]
                               + ["palette", "coast", "output"],
                      "channels": ["output", "h", "d", "color"] + list(STATE["masks"].keys()),
                      "taps": taps,
                      "desc": DESC, "io": NODE_IO,
                      "last": STATE.get("last", "erode"), "auto": _render["auto"]}
            self._json(st)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with _lock:
            if self.path == "/api/filter/add":
                n = STATE.get("next_filter", 1)
                typ = body.get("type", "noise")
                STATE["filters"].append(dict(id=f"f{n}", type=typ, enabled=True,
                                             masks=[], out_curve=[[0, 0], [1, 1]],
                                             **FILTER_DEFS[typ]))
                STATE["next_filter"] = n + 1
            elif self.path == "/api/filter/del":
                STATE["filters"] = [f for f in STATE["filters"] if f["id"] != body.get("id")]
            elif self.path == "/api/filter/move":
                ids = [f["id"] for f in STATE["filters"]]
                i = ids.index(body.get("id"))
                j = i + (1 if body.get("dir") == "down" else -1)
                if 0 <= j < len(ids):
                    STATE["filters"][i], STATE["filters"][j] = STATE["filters"][j], STATE["filters"][i]
            elif self.path == "/api/mask/add":
                n = STATE.get("next_mask", 1)
                State_mid = f"mask_{n}"
                STATE["next_mask"] = n + 1
                STATE["masks"][State_mid] = dict(
                    source="height", tap="", scale=400, lo=0.2, hi=0.8,
                    feather=1.0, invert=False)
            elif self.path == "/api/mask/del":
                mid = body.get("id")
                STATE["masks"].pop(mid, None)
                for node in STATE["nodes"].values():
                    if isinstance(node.get("masks"), list) and mid in node["masks"]:
                        node["masks"].remove(mid)
            elif self.path == "/api/reset":
                STATE.clear()
                STATE.update(default_state())
                _cache.clear()
                _png = None
            elif self.path == "/api/auto":
                _render["auto"] = bool(body.get("on"))
            elif self.path == "/api/set":
                nid, key, val = body["id"], body["k"], body["v"]
                if nid == "region":
                    STATE["region"] = val
                elif nid == "channel":
                    STATE["channel"] = val
                elif nid == "overlay":
                    STATE["overlay_map"] = bool(val)
                elif nid == "fast":
                    STATE["fast"] = bool(val)
                elif nid in STATE["nodes"]:
                    if key == "all":
                        STATE["nodes"][nid].update(val)
                    else:
                        STATE["nodes"][nid][key] = val
                elif any(f["id"] == nid for f in STATE["filters"]):
                    f = next(x for x in STATE["filters"] if x["id"] == nid)
                    if key == "all":
                        f.update(val)
                    else:
                        f[key] = val
                elif nid in STATE["masks"]:
                    if key == "all":
                        STATE["masks"][nid].update(val)
                    else:
                        STATE["masks"][nid][key] = val
            elif self.path == "/api/render":
                pass          # started outside the lock, below
            elif self.path == "/api/select":
                STATE["last"] = body.get("id")
            elif self.path == "/api/preset/save":
                os.makedirs(PRESETS, exist_ok=True)
                name = os.path.basename(body["name"])
                path = os.path.join(PRESETS, name + ".json")
                with open(path, "w") as f:
                    json.dump({"region": STATE["region"], "nodes": STATE["nodes"],
                               "masks": STATE["masks"], "filters": STATE["filters"],
                               "next_filter": STATE.get("next_filter", 1)}, f, indent=1)
            elif self.path == "/api/restore":
                # put a whole state back, e.g. from the browser copy after a restart
                body = {k: v for k, v in body.items() if k in STATE_KEYS}
                for k in ("nodes", "masks"):
                    for nid, params in body.get(k, {}).items():
                        if nid in STATE[k]:
                            STATE[k][nid].update(params)
                if "filters" in body:
                    STATE["filters"] = body["filters"]
                for k in ("region",):
                    if k in body:
                        STATE[k].update(body[k])
                for k in ("channel", "overlay_map", "next_mask", "next_filter"):
                    if k in body:
                        STATE[k] = body[k]
                _cache.clear()
            elif self.path == "/api/preset/del":
                name = os.path.basename(body.get("name", ""))
                path = os.path.join(PRESETS, name + ".json")
                if name and os.path.isfile(path):
                    os.remove(path)
            elif self.path == "/api/preset/load":
                path = os.path.join(PRESETS, body["name"] + ".json")
                with open(path) as f:
                    data = json.load(f)
                STATE["region"] = data["region"]
                for k in ("nodes", "masks"):
                    for nid, params in data.get(k, {}).items():
                        if nid in STATE[k]:
                            STATE[k][nid].update(params)
                if "filters" in data:
                    STATE["filters"] = data["filters"]
                STATE["next_filter"] = data.get("next_filter", len(STATE["filters"]) + 1)
                _cache.clear()
        if self.path != "/api/render" and self.path != "/api/select":
            save_state()
        # outside the state lock: never hold it while rendering
        if self.path == "/api/render":
            render_async()
        elif self.path == "/api/set" and _render["auto"]:
            schedule_render()
        self._json({"ok": True})


def main():
    global PORT, STATE_PATH
    ap = argparse.ArgumentParser(description="sea texture editor")
    ap.add_argument("--port", type=int, default=PORT,
                    help="port to serve on (default 3007); use a second port to "
                         "keep an agent's experiments off your own session")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    PORT = args.port
    if PORT != 3007:
        # a second port must not overwrite the state of the session you drive
        STATE_PATH = os.path.join(WORK, f"editor_state_{PORT}.json")
    load_map()
    load_state()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"sea editor on http://127.0.0.1:{PORT}  (ctrl-c to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
