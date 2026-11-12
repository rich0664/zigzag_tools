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
from PIL import Image, ImageDraw, ImageFilter

import sea_core as sc

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK = os.path.join(ROOT, "out", "sea")
MAP_PNG = os.path.join(WORK, "map.png")
PRESETS = os.path.join(WORK, "presets")
SNAPSHOTS = os.path.join(WORK, "snapshots")
PORT = 3007   # override with --port

_lock = threading.Lock()          # guards STATE mutation + preview bytes
_full = None                      # whole-map land mask
_cache = {}
USE_CACHE = True        # full-map builds switch this off to bound memory                       # node id -> (signature, value)
_version = 0
_png = None
_render = {"running": False, "stage": 0, "total": 0, "node": "",
           "started": 0.0, "elapsed": 0.0, "stamp": 0, "last_ms": 0.0,
           "error": None, "auto": False, "times": {}, "ov_stamp": 0,
           "ov_running": False, "ov_error": None}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def default_state():
    return {
        "region": {"x0": 2800, "y0": 2150, "w": 1200, "h": 800, "scale": 1.0},
        "channel": "output",
        "overlay_map": False,
        "world_noise": False,
        "nodes": {
            "distance": dict(enabled=True, down=2),
            "height": dict(enabled=True, mode="profile", deep_dist=1600.0,
                           curve=1.0, shelf_t=0.10, slope_t=0.32,
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
                               invert=False),
            "open_water": dict(source="distance", tap="", scale=400, lo=40, hi=160,
                               invert=False),
            "big_noise": dict(source="noise", tap="", scale=900, lo=0.3, hi=0.7,
                              invert=False),
        },
    }


STATE_PATH = os.path.join(WORK, "editor_state.json")


STATE_KEYS = ("region", "channel", "overlay_map", "nodes", "masks", "next_mask",
              "filters", "next_filter", "world_noise")


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
    except FileNotFoundError:
        return
    except Exception as exp:
        print("state file unreadable, starting from defaults:", exp, flush=True)
        return
    base = default_state()
    for k in ("nodes", "masks"):
        if k in data and isinstance(data[k], dict):
            for nid, params in data[k].items():
                if nid in base[k] and isinstance(params, dict):
                    base[k][nid].update(params)
                else:
                    base[k][nid] = params
        STATE[k] = base[k]
    for k in ("region", "channel", "overlay_map", "next_mask", "next_filter",
              "world_noise", "filters"):
        if k in data:
            STATE[k] = data[k]
    normalise_filters(STATE.get("filters", []))
    STATE["next_filter"] = max(
        [int(f["id"][1:]) for f in walk_filters(STATE.get("filters", []))
         if f.get("id", "").startswith("f") and f["id"][1:].isdigit()] + [0]) + 1
    STATE["next_mask"] = max(
        [int(k.split("_")[1]) for k in STATE.get("masks", {})
         if k.startswith("mask_") and k.split("_")[1].isdigit()] + [0]) + 1


BLEND_MODES = ["add", "multiply", "lighter", "darker"]

OVERLAY_DEFAULTS = dict(overlay="", ov_density=26, ov_length=14,
                        ov_colour="255,205,120")

FILTER_DEFS = {
    "warp": dict(kind="value", tiles=20.0, scale=700, seed=34),
    "noise": dict(kind="value", amp=0.15, scale=350, octaves=4, seed=11,
                  blend="add"),
    "blur": dict(radius=2),
    "features": dict(kind="value", amp=0.12, scale=70, density=0.70,
                     octaves=4, seed=9, blend="add"),
    "erode": dict(window=6, layers=20, mix=1.0, auto_range=False, blend="add"),
    "drainage": dict(scale=4, steps=4, k=2.0, m=0.5, n=1.0, cap=0.15,
                     strength=1.0, blend="add"),
    "shelf": dict(gain=1.0, power=1.0, blend="add"),
    # regional base level: low-frequency field, optionally quantised into
    # terraces, added to the height so whole areas sit at different depths
    "regions": dict(kind="value", scale=2500.0, octaves=3, levels=3, soft=0.45,
                    amp=0.35, seed=7, blend="add"),
    # one floor depth per open patch: the sea is split at the sills between
    # deep points, and each patch gets its own level
    "basins": dict(threshold=60.0, spacing=120.0, levels=4, soft=40.0,
                   amp=0.4, seed=5, blend="lighter"),
    # sediment carried by the coarse gyres: shoals trail downstream of islands
    "currents": dict(source="distance", source_level=0.55, reach=140.0,
                     travel=45, settle=0.997, step=6.0, strength=1.0, soft=3.0,
                     deflect=0.30, deflect_reach=110.0, speed_bias=0.0,
                     conserve=1.0, direction=0.0, core=0.0, feedback=0.0,
                     surge=0, blend="add"),
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
    "noise.kind": "value = smooth blobs; perlin = soft gradients; worley = "
                  "cellular plates and cracks; ridged = sharp ridges (1-|2n-1|); "
                  "billow = rounded lumps (|2n-1|); white = per-pixel grain",
    "features.kind": "value = blobs; perlin = gradients; worley = cellular "
                     "plates; ridged = creases; billow = lumps; white = grain",
    "regions.kind": "value = blobs; perlin = gradients; worley = cells; "
                    "ridged = creases; billow = lumps",
    "warp.kind": "value = smooth displacement; worley = blockier; white = grain",
    "erode.auto_range": "on = normalise to this crop's own min/max (the old "
                        "behaviour, which makes the same place erode differently "
                        "in every crop); off = treat the field as 0..1",
    "mask.seed": "different noise for this mask, so two noise masks can be "
                 "independent",
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
    "regions.scale": "size of a region in tiles: 1500-3000 gives ocean-scale "
                     "platforms, smaller gives banks and shoals",
    "regions.levels": "how many distinct platform heights; 1 = one smooth field",
    "regions.soft": "width of the ramp between two shelf levels: 0 = a hard "
                    "terrace edge, 1 = no terraces at all (smooth field), 0.4-0.6 "
                    "gives platforms that slope into each other",
    "basins.threshold": "how open water must be, in tiles from land, to count as "
                        "a basin at all",
    "basins.spacing": "minimum distance between basin centres in tiles: larger "
                      "means fewer, bigger patches",
    "basins.levels": "how many distinct floor depths the patches are picked from",
    "basins.soft": "blur radius in tiles at the edges of a patch: 0 leaves a hard "
                   "step between two floors",
    "basins.amp": "the highest a patch floor can sit, in height units (1 = shore); "
                  "with blend lighter each patch is lifted to its own value, so low "
                  "values stay deep",
    "basins.seed": "different assignment of depths to patches",
    "currents.source": "distance = pick up in a band around every coast; "
                       "height = only water shallower than source level; "
                       "slope = only steep faces",
    "currents.source_level": "for source=height: the depth below which sediment "
                             "is picked up (1 = shoreline, 0.5 = mid shelf)",
    "currents.settle": "fraction of sediment that survives each step: 0.997 "
                       "settles close to the coast, 0.9995 carries it out over "
                       "the slope",
    "currents.step": "pixels travelled per advection step",
    "currents.reach": "for source=distance: how far from land sediment is "
                      "picked up, in tiles",
    "currents.travel": "how many advection steps it is carried for: more = longer "
                       "plumes downstream",
    "currents.strength": "how much the sediment moves the bottom: the shallow "
                         "source band is eroded by the same amount that is "
                         "deposited downstream, so this displaces rather than adds",
    "currents.soft": "blur radius in tiles on the plume edges",
    "currents.deflect": "how much the flow bends to follow the coast, 0 = pure "
                        "gyre field, 1 = it hugs the shoreline",
    "currents.deflect_reach": "how far the coastal bend reaches inland (tiles)",
    "currents.direction": "0 = sediment is picked up evenly all round an island, "
                          "1 = only on the flank the current runs into, so the lee "
                          "side is deposition only",
    "currents.core": "tiles of shoreline left untouched by the scour, so an island "
                     "keeps its core instead of being planed away",
    "currents.feedback": "0 = the gyres are fixed, 1 = the flow re-reads the "
                         "bottom every step and bends around the shoals it builds",
    "currents.surge": "steps between current reversals; 0 = one steady direction, "
                      "6-15 = the flow swings back and forth like a tide so the "
                      "shoal grows on both sides of an island",
    "currents.conserve": "0 = sediment only ever adds height (the sea fills in), "
                         "1 = every bit deposited is scoured from the shallows it "
                         "came from, so depths move instead of rising",
    "currents.speed_bias": "0 = deposit everywhere sediment arrives, 1 = only "
                           "deposit in fast water, so calm gyre centres stay clear",
    "regions.amp": "how far a region can sit above or below the base depth",
    "regions.octaves": "detail within the regional field",
    "regions.seed": "different arrangement of regions",
    "coast.strength": "how strongly the coastal tint is mixed in",
    "blend": "how this filter's contribution meets the height under it. Every "
             "mode is the identity when the contribution is zero. add = h + c; "
             "subtract = h - c; multiply = h x (1 + c), so it fades out where "
             "the water is already deep or shallow; screen = h + c(1-h); "
             "overlay = strongest in the mid depths; lighter = the brightening "
             "half only; darker = the deepening half only",
    "coast.r": "coastal tint red", "coast.g": "coastal tint green", "coast.b": "coastal tint blue",
    "output.land_t": "depth shown under the land tiles (0 = shallowest colour)",
    "mask.source": "what the mask reads: height (of the tapped node), distance, or noise",
    "mask.tap": "which node's height to read; empty = the height at the node using this mask",
    "mask.scale": "noise feature size when the source is noise",
    "mask.lo": "mask starts rising above this value",
    "mask.hi": "mask is fully on at this value",
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


# a render holds a dozen float32 planes the size of the region; cap the region so
# it cannot balloon the process, and refuse to start when the box is short on RAM
MAX_REGION_PX = 4_000_000        # ~2400x1600 at 1 px/tile
MIN_FREE_MB = 800


def region_pixels(r):
    """render cost is one pixel per tile whatever the display scale is"""
    try:
        return int(r["w"]) * int(r["h"])
    except Exception:
        return 0


def region_limit_msg(r):
    px = region_pixels(r)
    if px > MAX_REGION_PX:
        return (f"region is {px / 1e6:.1f} MPx; a render is capped at "
                f"{MAX_REGION_PX / 1e6:.0f} MPx, lower w/h or scale")
    return None


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


def render_guard(r):
    msg = region_limit_msg(r)
    if msg:
        return msg
    free = mem_available_mb()
    if free is not None and free < MIN_FREE_MB:
        return (f"only {free:.0f} MB RAM free, not rendering "
                f"(needs {MIN_FREE_MB} MB headroom)")
    return None


_dist_full = None


def dist_full():
    """cached exact distance field, one value per map tile (read lazily, mmap)"""
    global _dist_full
    if _dist_full is None:
        path = os.path.join(WORK, "dist_full.npy")
        if os.path.exists(path):
            try:
                _dist_full = np.load(path, mmap_mode="r")
            except Exception:
                _dist_full = False
        else:
            _dist_full = False
    return None if _dist_full is False else _dist_full


CURRENTS_CS = 16          # coarse cell size for the current field, in tiles
_currents = None


def _compute_currents():
    """one coarse gyre per basin, relaxing around land; ~3s for the whole map"""
    import heapq
    land = load_map()
    MH, MW = land.shape
    df = dist_full()
    if df is None:
        raise RuntimeError("need out/sea/dist_full.npy (tools/build_dist.py)")
    cs = CURRENTS_CS
    ch, cw = MH // cs, MW // cs
    land_c = np.array(Image.fromarray(land.astype(np.uint8) * 255)
                      .resize((cw, ch), Image.BOX)) > 127
    ds = sc.box_blur(np.asarray(df, dtype=np.float32), 30, 2)
    coarse = np.array(Image.fromarray(ds, "F").resize((cw, ch), Image.BILINEAR))
    del ds
    peak = np.array(Image.fromarray(coarse, "F").filter(ImageFilter.MaxFilter(9)))
    cand = (coarse >= peak) & (coarse > 40) & ~land_c
    cand[:5, :] = False; cand[-5:, :] = False
    cand[:, :5] = False; cand[:, -5:] = False
    ys, xs = np.nonzero(cand)
    order = np.argsort(-coarse[ys, xs])
    seeds = []
    for i in order:
        y, x = ys[i], xs[i]
        if all((y - py) ** 2 + (x - px) ** 2 > (120.0 / cs) ** 2 for py, px in seeds):
            seeds.append((y, x))

    lab = np.zeros((ch, cw), np.int32)
    heap = []
    for k, (y, x) in enumerate(seeds, start=1):
        lab[y, x] = k
        heapq.heappush(heap, (-float(coarse[y, x]), y, x))
    while heap:
        _, y, x = heapq.heappop(heap)
        k = lab[y, x]
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < ch and 0 <= nx < cw and lab[ny, nx] == 0:
                lab[ny, nx] = k
                heapq.heappush(heap, (-float(coarse[ny, nx]), ny, nx))

    sizes = np.bincount(lab.ravel())
    yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
    psi = np.zeros((ch, cw), np.float32)
    n = 0
    for k in range(1, lab.max() + 1):
        if sizes[k] * cs * cs < 40000:
            continue
        mk = lab == k
        ys_, xs_ = np.nonzero(mk)
        cy, cx = ys_.mean(), xs_.mean()
        r = np.sqrt(sizes[k] / 3.14159265)
        psi += (1.0 if n % 2 == 0 else -1.0) * np.exp(
            -((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * (0.75 * r) ** 2))
        n += 1
    for _ in range(500):
        avg = 0.25 * (np.roll(psi, 1, 0) + np.roll(psi, -1, 0)
                      + np.roll(psi, 1, 1) + np.roll(psi, -1, 1))
        psi = np.where(land_c, 0.0, avg)
    u, v = np.gradient(psi)
    v = -v
    mag = np.hypot(u, v)
    mag[mag < 1e-9] = 1
    u = (u / mag).astype(np.float32)
    v = (v / mag).astype(np.float32)
    u[land_c] = 0
    v[land_c] = 0
    return np.stack([u, v, lab.astype(np.float32)])


def currents_field():
    """(2, ch, cw) unit current vectors, built once and kept on disk"""
    global _currents
    if _currents is None:
        path = os.path.join(WORK, "currents_uv.npy")
        if os.path.exists(path):
            try:
                arr = np.load(path)
                _currents = arr if arr.shape[0] >= 3 else _compute_currents()
            except Exception:
                _currents = _compute_currents()
        else:
            _currents = _compute_currents()
            try:
                np.save(path, _currents)
            except Exception:
                pass
    return _currents


def sample_bilinear(img, ys, xs):
    h, w = img.shape
    ys = np.clip(ys, 0, h - 1.001)
    xs = np.clip(xs, 0, w - 1.001)
    y0 = ys.astype(np.int32); x0 = xs.astype(np.int32)
    fy = ys - y0; fx = xs - x0
    y1 = np.minimum(y0 + 1, h - 1); x1 = np.minimum(x0 + 1, w - 1)
    return ((1 - fy) * ((1 - fx) * img[y0, x0] + fx * img[y0, x1])
            + fy * ((1 - fx) * img[y1, x0] + fx * img[y1, x1]))


_overlay = None
_map_rgba = None


def map_rgba():
    """decoded map tile layer, kept in memory: reopening the PNG per crop meant
    a 163 MB decode for every mouse move while panning"""
    global _map_rgba
    if _map_rgba is None:
        im = Image.open(MAP_PNG).convert("RGBA")
        im.load()
        _map_rgba = im
    return _map_rgba


def snapshot_list():
    if not os.path.isdir(SNAPSHOTS):
        return []
    out = []
    for f in os.listdir(SNAPSHOTS):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(SNAPSHOTS, f)) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        out.append({"id": f[:-5], "name": meta.get("name", f[:-5]),
                    "time": meta.get("time", 0), "w": meta.get("w", 0),
                    "h": meta.get("h", 0), "channel": meta.get("channel", "")})
    out.sort(key=lambda m: m["time"])
    return out


def snapshot_save(name=""):
    """keep the settings and the picture as it is right now"""
    if _png is None:
        raise ValueError("nothing rendered yet")
    os.makedirs(SNAPSHOTS, exist_ok=True)
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + str(int(time.time() * 1000) % 1000)
    meta = {"name": name or sid, "time": time.time(),
            "channel": STATE.get("channel", "output"),
            "region": STATE.get("region"), "w": 0, "h": 0,
            "state": {k: STATE[k] for k in STATE_KEYS if k in STATE}}
    with open(os.path.join(SNAPSHOTS, sid + ".json"), "w") as f:
        json.dump(meta, f)
    with open(os.path.join(SNAPSHOTS, sid + ".png"), "wb") as f:
        f.write(_png)
    im = Image.open(io.BytesIO(_png)).convert("RGB")
    meta["w"], meta["h"] = im.size
    th = im.copy()
    th.thumbnail((240, 160), Image.LANCZOS)
    th.save(os.path.join(SNAPSHOTS, sid + "_thumb.png"))
    with open(os.path.join(SNAPSHOTS, sid + ".json"), "w") as f:
        json.dump(meta, f)
    return sid


def snapshot_load(sid):
    path = os.path.join(SNAPSHOTS, sid + ".json")
    with open(path) as f:
        data = json.load(f)
    st = data.get("state", {})
    for k in ("region", "channel", "overlay_map", "world_noise", "next_mask",
              "next_filter"):
        if k in st:
            STATE[k] = st[k]
    for k in ("nodes", "masks"):
        if k in st:
            base = default_state()[k]
            for nid, params in st[k].items():
                if nid in base and isinstance(params, dict):
                    base[nid].update(params)
                else:
                    base[nid] = params
            STATE[k] = base
    STATE["filters"] = st.get("filters", [])
    normalise_filters(STATE["filters"])
    _cache.clear()


def load_region(region, cancel=None):
    full = load_map()
    # always sampled at one pixel per tile; `scale` only downsamples the result,
    # so a half-res preview shows more area instead of a magnified corner
    x0, y0 = int(region["x0"]), int(region["y0"])
    w = max(32, int(int(region["w"])))
    h = max(32, int(int(region["h"])))
    margin = 200
    H, W = full.shape
    xs = max(0, x0 - margin)
    ys = max(0, y0 - margin)
    xe = min(W, x0 + w + margin)
    ye = min(H, y0 + h + margin)
    sub = full[ys:ye, xs:xe]
    df = dist_full()
    if df is not None and df.shape == full.shape:
        # the exact full-resolution field: previews then match the full build
        d_sub = np.array(df[ys:ye, xs:xe], dtype=np.float32)
    else:
        d_sub = sc.edt(sub, down=int(STATE["nodes"]["distance"]["down"]), cancel=cancel)
    d = d_sub[(y0 - ys):(y0 - ys) + min(h, H - y0), (x0 - xs):(x0 - xs) + min(w, W - x0)]
    land = sub[(y0 - ys):(y0 - ys) + d.shape[0], (x0 - xs):(x0 - xs) + d.shape[1]]
    return land, d


def sig(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=float).encode()).hexdigest()[:16]



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


BLEND_MODES = ["add", "subtract", "multiply", "screen", "overlay",
               "lighter", "darker"]


def blend_into(h, c, mode):
    """combine a filter's contribution with the height under it.

    Every mode is the identity when c == 0, which is what makes a filter with a
    zero contribution a no-op however it is blended."""
    if mode in ("multiply", "scale"):   # scales with the height already there
        return np.clip(h * (1.0 + c), 0, 1).astype(np.float32)
    if mode == "subtract":              # the inverse of add
        return np.clip(h - c, 0, 1).astype(np.float32)
    if mode == "screen":                # add, but saturating
        return np.clip(h + c * (1.0 - h), 0, 1).astype(np.float32)
    if mode == "overlay":               # strongest in the mid-tones
        k = np.where(h < 0.5, 2.0 * h, 2.0 * (1.0 - h))
        return np.clip(h + c * k, 0, 1).astype(np.float32)
    if mode in ("lighter", "lighten"):  # only the brightening part
        return np.clip(h + np.maximum(c, 0.0), 0, 1).astype(np.float32)
    if mode in ("darker", "darken"):    # only the darkening part
        return np.clip(h + np.minimum(c, 0.0), 0, 1).astype(np.float32)
    return np.clip(h + c, 0, 1).astype(np.float32)


def apply_curve(delta, pts):
    """shape the magnitude of a signed contribution.

    y(0) is subtracted so the curve stays continuous through zero: without it a
    curve that does not pass through (0,0) injects a step at every sign change."""
    c = sorted_curve(pts)
    if c is None:
        return delta
    xs, ys = c
    y0 = float(np.interp(0.0, xs, ys))
    shaped = np.interp(np.abs(delta), xs, ys) - y0
    return (np.sign(delta) * shaped).astype(np.float32)


def noise2d(shape, ctx, scale, octaves, seed, kind="value"):
    """fbm for a region: world-anchored when the flag is on, crop-anchored
    (the old behaviour) when it is off"""
    if STATE.get("world_noise"):
        reg = ctx.get("region") or {"x0": 0, "y0": 0, "scale": 1.0}
        return sc.fbm_world(shape, float(reg["x0"]), float(reg["y0"]), 1.0,
                            max(1.0, float(scale)), int(octaves),
                            seed=int(seed), kind=kind)
    return sc.fbm(shape[1], shape[0], max(1.0, float(scale)),
                  max(1, int(octaves)), seed=int(seed))


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
        v = noise2d(ctx["d"].shape, ctx, max(8.0, float(mask["scale"])), 4,
                    int(mask.get("seed", 3)), mask.get("kind", "value"))
    lo, hi = float(mask["lo"]), float(mask["hi"])
    t = sc.smoothstep((v - lo) / max(1e-6, hi - lo))
    return 1.0 - t if mask["invert"] else t


def walk_filters(filters):
    """every filter in the tree, parents before children"""
    for f in filters:
        yield f
        yield from walk_filters(f.get("children") or [])


def find_filter(fid, filters=None):
    for f in walk_filters(STATE["filters"] if filters is None else filters):
        if f["id"] == fid:
            return f
    return None


def filter_owner(fid, filters=None, parent=None):
    """(parent filter or None, the sibling list holding fid)"""
    lst = STATE["filters"] if filters is None else filters
    for f in lst:
        if f["id"] == fid:
            return parent, lst
        p, owner = filter_owner(fid, f.get("children") or [], f)
        if owner is not None:
            return p, owner
    return None, None


def normalise_filters(filters):
    for f in filters:
        f.setdefault("children", [])
        f.setdefault("name", "")
        f.setdefault("overlay", "")
        normalise_filters(f["children"])


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
    n = noise2d(h.shape, ctx, float(node["scale"]), int(node["octaves"]),
                int(node["seed"]), node.get("kind", "value")) - 0.5
    w = mask_weight(node, ctx) * float(node["amp"])
    c = 2.0 * w * n
    dbg = ctx.get("debug")
    if dbg is not None:
        d_ = dbg.setdefault(node["id"], {})
        d_["bumps"] = np.clip(n + 0.5, 0, 1).astype(np.float32) * 2 - 1
        d_["mask"] = np.broadcast_to(np.asarray(w, np.float32), h.shape).copy() \
            if np.isscalar(w) else np.asarray(w, np.float32)
    return np.clip(h + c, 0, 1), c


def n_features(node, ctx, prev_sig):
    h = ctx["h"]
    H, W = h.shape
    n = noise2d(h.shape, ctx, float(node["scale"]), int(node["octaves"]),
                int(node["seed"]), node.get("kind", "value"))
    bumps = np.clip((n - float(node["density"])) / max(1e-3, 1.0 - float(node["density"])), 0, 1) ** 1.6
    w = mask_weight(node, ctx) * float(node["amp"])
    c = w * bumps
    dbg = ctx.get("debug")
    if dbg is not None:
        d_ = dbg.setdefault(node["id"], {})
        d_["bumps"] = bumps.astype(np.float32)
        d_["mask"] = np.broadcast_to(np.asarray(w, np.float32), h.shape).copy() \
            if np.isscalar(w) else np.asarray(w, np.float32)
    return np.clip(h + c, 0, 1), c


def _canceller(ctx):
    """a callback the heavy loops call so a superseded render stops quickly"""
    gen = ctx.get("gen", 0)
    return (lambda: check_cancel(gen)) if gen else None


def n_erode(node, ctx, prev_sig):
    h = ctx["h"]
    p = 1.0 / 2.0
    H, W = h.shape
    SW, SH = max(8, int(W * p)), max(8, int(H * p))
    hs = np.array(Image.fromarray(h, mode="F").resize((SW, SH), Image.BICUBIC))
    auto = bool(node.get("auto_range", False))
    he = sc.slab_erode(hs, window=max(2, int(round(float(node["window"]) * p))),
                       layers=int(node["layers"]), cancel=_canceller(ctx),
                       lo=None if auto else 0.0, hi=None if auto else 1.0)
    he = sc.upscale(he, (W, H))
    mix = float(node["mix"]) * mask_weight(node, ctx)
    out = np.clip(h * (1 - mix) + he * mix, 0, 1)
    dbg = ctx.get("debug")
    if dbg is not None:
        dbg.setdefault(node["id"], {})["eroded"] = he.astype(np.float32)
    return out, out - h


def n_shelf(node, ctx, prev_sig):
    h = ctx["h"]
    w = mask_weight(node, ctx)
    gain = 1.0 + (float(node["gain"]) - 1.0) * w
    out = np.clip(h ** float(node["power"]) * gain, 0, 1)
    dbg = ctx.get("debug")
    if dbg is not None:
        dbg.setdefault(node["id"], {})["gain"] = np.asarray(gain, np.float32).copy() \
            if not np.isscalar(gain) else np.full(h.shape, float(gain), np.float32)
    return out, out - h



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
        check_cancel(ctx.get("gen", 0))
        rec, slope = flow_dirs(cur)
        dbg = ctx.get("debug")
        if dbg is not None and _ == 0:
            yy, xx = np.mgrid[0:SH, 0:SW]
            ry = np.where(rec >= 0, rec // SW, yy.ravel()).reshape(SH, SW)
            rx = np.where(rec >= 0, rec % SW, xx.ravel()).reshape(SH, SW)
            ang = np.arctan2((ry - yy) * -1.0, (rx - xx) * 1.0)
            plane = np.array(Image.fromarray(ang.astype(np.float32), "F")
                             .resize((W, H), Image.BICUBIC))
            dbg.setdefault(node["id"], {})["flow"] = plane
        order = np.argsort(-cur.ravel(), kind="stable")
        acc = flow_accum(rec, order)
        A = (acc / max(1.0, float(acc.max()))) ** float(node["m"])
        S = np.clip(slope.ravel() / max(1e-6, float(slope.max())), 0, 1) ** float(node["n"])
        step = np.clip(float(node["k"]) * A * S, 0, float(node.get("cap", 0.1))).reshape(SH, SW)
        dbg = ctx.get("debug")
        if dbg is not None and _ == 0:
            d_ = dbg.setdefault(node["id"], {})
            up = lambda a: np.array(Image.fromarray(a.astype(np.float32), "F")
                                    .resize((W, H), Image.BICUBIC))
            d_["accum"] = up((acc / max(1.0, float(acc.max()))).reshape(SH, SW))
            d_["slope"] = up(slope.reshape(SH, SW) / max(1e-6, float(slope.max())))
        cur = np.clip(cur - step, 0, 1)
        dh += step

    dh_up = np.array(Image.fromarray(dh, mode="F").resize((W, H), Image.BICUBIC))
    w = mask_weight(node, ctx) * float(node.get("strength", 1.0))
    c = -dh_up * w
    return np.clip(h + c, 0, 1), c



def f_warp(h, node, ctx):
    H, W = h.shape
    sc_t = float(node["scale"])
    kind = node.get("kind", "value")
    w1 = (noise2d(h.shape, ctx, sc_t, 3, int(node["seed"]), kind) - 0.5) * float(node["tiles"])
    w2 = (noise2d(h.shape, ctx, sc_t, 3, int(node["seed"]) + 1, kind) - 0.5) * float(node["tiles"])
    # float row/column vectors broadcast to int32 indices: same pixels as a
    # full int64 mgrid for about a third of the memory
    yf = np.arange(H, dtype=np.float32)[:, None]
    xf = np.arange(W, dtype=np.float32)[None, :]
    rows = np.clip(yf + w1, 0, H - 1).astype(np.int32)
    cols = np.clip(xf + w2, 0, W - 1).astype(np.int32)
    dbg = ctx.get("debug")
    if dbg is not None:
        d_ = dbg.setdefault(node["id"], {})
        d_["dx"] = w2.astype(np.float32)
        d_["dy"] = w1.astype(np.float32)
    out = h[rows, cols]
    del rows, cols
    # a wired mask limits where the displacement applies
    m = mask_weight(node, ctx)
    if not np.isscalar(m):
        out = h + (out - h) * m
    return out.astype(np.float32), None


def f_regions(h, node, ctx):
    H, W = h.shape
    n = noise2d(h.shape, ctx, max(8.0, float(node["scale"])),
                int(node.get("octaves", 3)), int(node.get("seed", 7)),
                node.get("kind", "value"))
    raw_field = n.copy()
    lv = max(1, int(node.get("levels", 3)))
    # soft = width of the ramp between two levels, as a fraction of a level:
    # 0 gives the cliff edge of a clean terrace, 1 gives the smooth field back
    soft = node.get("soft")
    if soft is None:                       # presets saved with the old "mix"
        soft = 1.0 - float(node.get("mix", 0.5))
    soft = min(1.0, max(0.0, float(soft)))
    if lv > 1 and soft < 0.999:            # 1.0 = leave the field smooth
        x = n * lv
        i = np.floor(x)
        f = x - i
        w = 0.5 * soft + 1e-4
        f2 = np.clip((f - 0.5) / (2.0 * w) + 0.5, 0.0, 1.0)
        f2 = f2 * f2 * (3.0 - 2.0 * f2)    # smoothstep the ramp, no creases
        n = (i + f2) / lv
    c = (n - 0.5) * float(node["amp"]) * mask_weight(node, ctx)
    dbg = ctx.get("debug")
    if dbg is not None:
        d_ = dbg.setdefault(node["id"], {})
        d_["field"] = (raw_field * 2 - 1).astype(np.float32)
        d_["levels"] = (n * 2 - 1).astype(np.float32)
    return np.clip(h + c, 0, 1).astype(np.float32), c.astype(np.float32)


def f_basins(h, node, ctx):
    """split open water into basins at its sills, one floor level each"""
    import heapq
    d = ctx["d"]
    H, W = h.shape
    # a fixed cell size on the map grid: the same place gets the same zones
    # whatever crop you are looking at
    cs = BASIN_CS
    reg = ctx.get("region") or {"x0": 0, "y0": 0}
    x0, y0 = int(reg["x0"]), int(reg["y0"])
    ch, cw = max(8, H // cs), max(8, W // cs)
    ds = sc.box_blur(d, 30, 2)
    coarse = np.array(Image.fromarray(ds, "F").resize((cw, ch), Image.BILINEAR))
    peak = np.array(Image.fromarray(coarse, "F").filter(ImageFilter.MaxFilter(9)))
    ys, xs = np.nonzero((coarse >= peak) & (coarse > float(node["threshold"])))
    order = np.argsort(-coarse[ys, xs])
    spacing = max(1.0, float(node["spacing"]) / cs)
    seeds = []
    for i in order:
        y, x = ys[i], xs[i]
        if all((y - py) ** 2 + (x - px) ** 2 > spacing ** 2 for py, px in seeds):
            seeds.append((y, x))
    if not seeds:
        return h.copy(), np.zeros_like(h)

    lab = np.zeros((ch, cw), np.int32)
    heap = []
    for k, (y, x) in enumerate(seeds, start=1):
        lab[y, x] = k
        heapq.heappush(heap, (-float(coarse[y, x]), y, x))
    # flood from the deep points upward: two basins meet at the shallow sill
    while heap:
        _, y, x = heapq.heappop(heap)
        k = lab[y, x]
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < ch and 0 <= nx < cw and lab[ny, nx] == 0:
                lab[ny, nx] = k
                heapq.heappush(heap, (-float(coarse[ny, nx]), ny, nx))

    # the value comes from the basin's position on the map, not from its order
    # in this crop, so a preview keeps its depths as you move around
    sd = int(node.get("seed", 5))
    vals = np.zeros(len(seeds) + 1, np.float32)
    for k, (y, x) in enumerate(seeds, start=1):
        hsh = ((x0 + x * cs) * 73856093) ^ (y0 + y * cs) * 19349663
        vals[k] = ((hsh ^ (sd * 83492791)) & 0xFFFFFFFF) % 100003 / 100003.0
    lv = max(1, int(node.get("levels", 4)))
    if lv > 1:
        vals = np.round(vals * lv) / lv
    # each patch gets a floor height between 0 and amp; the contribution is the
    # move needed to get there, so add sets the floor, lighter raises to it,
    # darker lowers to it
    target = (vals[lab] * float(node["amp"])).astype(np.float32)
    dbg = ctx.get("debug")
    if dbg is not None:
        d_ = dbg.setdefault(node["id"], {})
        d_["labels"] = np.array(Image.fromarray(lab.astype(np.float32), "F")
                               .resize((W, H), Image.NEAREST))
    target = np.array(Image.fromarray(target, "F").resize((W, H), Image.BILINEAR))
    r = int(max(0.0, float(node.get("soft", 40.0))))
    if r:
        target = sc.box_blur(target, r, 2)
    target[ctx["land"]] = 1.0                 # never pull land down
    if dbg is not None:
        dbg.setdefault(node["id"], {})["target"] = target.astype(np.float32)
    c = ((target - h) * mask_weight(node, ctx)).astype(np.float32)
    return np.clip(h + c, 0, 1).astype(np.float32), c


def f_currents(h, node, ctx):
    """carry near-shore sediment along the basin gyres"""
    d = ctx["d"]
    H, W = h.shape
    reg = ctx.get("region") or {"x0": 0, "y0": 0}
    uv = currents_field()
    cs = CURRENTS_CS
    xs = ((np.arange(W) + int(reg["x0"])) / float(cs)).astype(np.float32)
    ys = ((np.arange(H) + int(reg["y0"])) / float(cs)).astype(np.float32)
    gx, gy = np.meshgrid(xs, ys)
    u = sample_bilinear(uv[0], gy, gx)
    v = sample_bilinear(uv[1], gy, gx)

    # near a coast the flow turns to run along it
    dgy, dgx = np.gradient(sc.box_blur(d, 10, 2))
    near = float(node.get("deflect", 0.3)) * np.exp(
        -d / max(1.0, float(node.get("deflect_reach", 110.0))))
    u = u * (1 - near) + (-dgy) * near
    v = v * (1 - near) + dgx * near
    mag = np.hypot(u, v) + 1e-6
    speed = mag.copy()             # before normalising: how fast the water moves
    u /= mag
    v /= mag

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    # land is not sediment: d is 0 there, which would otherwise make the whole
    # island interior the strongest possible source
    # where sediment comes from. distance = a uniform band around every coast;
    # height = only water shallow enough to be stirred; slope = only steep faces
    mode = node.get("source", "distance")
    wet = (~ctx["land"]).astype(np.float32)
    if mode == "height":
        lvl = float(np.clip(float(node.get("source_level", 0.55)), 0.01, 0.99))
        src = (np.clip((h - lvl) / max(1e-6, 1.0 - lvl), 0, 1) ** 1.5) * wet
    elif mode == "slope":
        sy, sx = np.gradient(sc.box_blur(h, 2, 2))
        sl = np.hypot(sx, sy)
        ref = max(1e-3, float(np.percentile(sl[wet > 0], 95)))
        src = (np.clip(sl / ref, 0, 1) ** 1.5) * wet
    else:
        src = (np.clip(1.0 - d / float(node["reach"]), 0, 1) ** 2) * wet
    direction = float(np.clip(float(node.get("direction", 0.0)), 0, 1))
    if direction:
        # the distance gradient points away from land, so a flow with a negative
        # dot product is running *into* the coast: that is the windward flank
        nd = np.hypot(dgy, dgx) + 1e-6
        onshore = np.clip(-(u * dgy + v * dgx) / nd, 0, 1)
        src = src * (1.0 - direction + direction * onshore)
    # speed_bias weights the *deposition* by how fast the water is, so calm
    # gyre centres stay clear instead of filling up
    bias = float(node.get("speed_bias", 0.0))
    if bias:
        sn = np.clip(speed / max(1e-6, float(np.percentile(speed, 99))), 0, 1)
        weight = 1.0 - bias + bias * sn
    else:
        weight = 1.0
    feedback = float(np.clip(float(node.get("feedback", 0.0)), 0.0, 1.0))
    surge = int(node.get("surge", 0))
    settle = float(np.clip(float(node.get("settle", 0.997)), 0.9, 0.99995))
    step_px = float(np.clip(float(node.get("step", 6.0)), 0.5, 40.0))
    cur = src.copy()
    floor = h
    for i in range(max(1, int(node["travel"]))):
        uu, vv = u, v
        if feedback:
            # the current feels the bottom it is building: bend it along the
            # contours of the *current* height, not the original distance
            gy2, gx2 = np.gradient(sc.box_blur(floor, 8, 2))
            nd = np.hypot(gx2, gy2) + 1e-6
            uu = u * (1 - feedback) + (-gy2 / nd) * feedback
            vv = v * (1 - feedback) + (gx2 / nd) * feedback
            m2 = np.hypot(uu, vv) + 1e-6
            uu = uu / m2
            vv = vv / m2
        if surge > 0 and (i // surge) % 2 == 1:
            uu, vv = -uu, -vv          # the current reverses: tidal, monsoon
        step_dep = sample_bilinear(cur, yy - vv * step_px, xx - uu * step_px)
        cur = 0.5 * cur + 0.5 * step_dep
        cur = np.maximum(cur, src) * settle
        if feedback:
            floor = np.clip(floor + 0.15 * step_dep, 0, 1)
    cur = cur * weight
    dep = np.clip(cur - src, 0, 1)          # where sediment arrived
    dbg = ctx.get("debug")
    if dbg is not None:
        d_ = dbg.setdefault(node["id"], {})
        d_["u"] = u.astype(np.float32)
        d_["v"] = (-v).astype(np.float32)
        d_["speed"] = (speed / max(1e-6, float(np.percentile(speed, 99)))
                       ).astype(np.float32)
        if uv.shape[0] >= 3:
            iy = np.clip(gy.astype(np.int32), 0, uv.shape[1] - 1)
            ix = np.clip(gx.astype(np.int32), 0, uv.shape[2] - 1)
            d_["labels"] = uv[2][iy, ix].astype(np.float32)
    r = int(max(0.0, float(node.get("soft", 3.0))))
    if r:
        dep = sc.box_blur(dep, r, 2)

    # Sediment is moved, not created -- but only as much as `conserve` says.
    #   0 = pure accumulation (the water just fills in)
    #   1 = displacement (as much is scoured from the shallows as is deposited)
    # The deposit is capped by the headroom under the shoreline, so it can never
    # clip, and the scour is capped by the water actually there.
    gain = np.asarray(float(node["strength"]) * mask_weight(node, ctx),
                      dtype=np.float32)
    conserve = float(np.clip(float(node.get("conserve", 1.0)), 0.0, 1.0))

    room = np.clip(1.0 - h, 0.0, 1.0)
    dep = np.minimum(dep * gain, room)          # what actually fits
    landed = float(dep.sum())

    band = src.copy()
    core = float(node.get("core", 0.0))
    if core > 0:
        # fade the scour in over `core` tiles, so the shallows right against an
        # island are left alone and only the outer shelf is rearranged
        band = band * np.clip(d / core, 0.0, 1.0) ** 1.5
    band = band / max(1e-6, float(band.sum()))
    want = conserve * landed * band / np.maximum(gain, 1e-6) * gain
    scour = np.minimum(want, h)                 # cannot dig below zero
    if dbg is not None:
        d_["deposit"] = dep.astype(np.float32)
        d_["scour"] = scour.astype(np.float32)
    c = (dep - scour).astype(np.float32)
    return np.clip(h + c, 0, 1).astype(np.float32), c


def f_blur(h, node, ctx):
    b = sc.box_blur(h, max(1, int(round(float(node["radius"])))), 2)
    dbg = ctx.get("debug")
    if dbg is not None:
        dbg.setdefault(node["id"], {})["blurred"] = b.astype(np.float32)
    # a wired mask limits the blur to that area instead of softening everything
    m = mask_weight(node, ctx)
    if not np.isscalar(m):
        b = h + (b - h) * m
    return b.astype(np.float32), None


def apply_filter(filt, ctx, h, times=None):
    """run one filter, then its child chain over the result's contribution"""
    t0 = time.perf_counter()
    sub = dict(ctx)
    sub["h"] = h
    val, contrib = FILTER_FN[filt["type"]](h, filt, sub)
    if contrib is None:                 # warp/blur transform the field itself
        contrib = val - h
    # debug view: what this filter actually adds, before blending
    if ctx.get("debug") is not None:
        ctx["debug"].setdefault(filt["id"], {})["contribution"] = \
            np.asarray(contrib, dtype=np.float32).copy()
    pts = filt.get("out_curve")
    if pts and len(pts) >= 2:
        contrib = apply_curve(contrib, pts)
    for ch in filt.get("children") or []:
        if ch.get("enabled", True):
            contrib = apply_filter(ch, ctx, contrib, times)
    out = blend_into(h, contrib, filt.get("blend", "add"))
    if isinstance(out, np.ndarray):
        ctx.setdefault("fields", {})[filt["id"]] = out
    if times is not None:
        times[filt["id"]] = round((time.perf_counter() - t0) * 1000.0, 1)
    return out


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
    "regions": f_regions,
    "basins": f_basins,
    "currents": f_currents,
    "features": _as_filter(n_features),
    "erode": _as_filter(n_erode),
    "drainage": _as_filter(n_drainage),
    "shelf": _as_filter(n_shelf),
}

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



SIG_SKIP = ("name", "overlay", "ov_density", "ov_length", "ov_colour")


def _field_keys(node):
    """the part of a filter that can change its output (children included)"""
    if isinstance(node, dict):
        return {k: _field_keys(v) for k, v in node.items() if k not in SIG_SKIP}
    if isinstance(node, list):
        return [_field_keys(v) for v in node]
    return node


def evaluate(progress=None, stop_at=None, region=None, gen=0):
    global _cache
    region = region or STATE["region"]
    land, d = load_region(region, cancel=(lambda: check_cancel(gen)) if gen else None)
    want_debug = bool(STATE.get("_want_debug"))
    ctx = {"land": land, "d": d, "h": None, "color": None, "fields": {},
           "gen": gen, "region": region, "debug": {} if want_debug else None}
    steps = [("distance", None), ("height", None)]
    steps += [(f["id"], f) for f in STATE["filters"]]
    steps += [("palette", None), ("coast", None), ("output", None)]

    tapped = set()
    for m in STATE["masks"].values():
        t = m.get("tap")
        if t:
            tapped.add(t)

    total = len(steps)
    prev_sig = "0"
    times = {}
    for i, (nid, filt) in enumerate(steps):
        check_cancel(gen)
        if progress:
            progress(i, total, nid)
        node = filt if filt is not None else STATE["nodes"][nid]
        t_node = time.perf_counter()

        sub_masks = set()
        for n_ in walk_filters([node]):
            sub_masks.update(n_.get("masks") or [])
        chain_sig = sig(prev_sig, nid, _field_keys(node),
                        [STATE["masks"][m] for m in sorted(sub_masks)
                         if m in STATE["masks"]],
                        region.get("x0"), region.get("y0"), region.get("w"),
                        region.get("h"), region.get("scale"))
        prev_sig = chain_sig
        want_planes = bool(node.get("overlay")) or (
            STATE.get("channel", "").startswith(f"dbg:{nid}:"))
        cached = _cache.get(nid) if (USE_CACHE and not want_planes) else None
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
                    val, contrib = apply_filter(filt, ctx, ctx["h"], times), None
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
                        and nid == "height"):
                    c = sorted_curve(pts)
                    if c is not None:
                        val = np.interp(val, c[0], c[1]).astype(np.float32)
            if USE_CACHE:
                _cache[nid] = (chain_sig, val)
        times[nid] = round((time.perf_counter() - t_node) * 1000.0, 1)

        if isinstance(val, np.ndarray) and getattr(val, "ndim", 0) == 2:
            if nid == "height":
                ctx["fields"]["base"] = val      # the height field, untapped
            if nid in tapped or nid == "height":
                ctx["fields"][nid] = val
        if nid == "distance":
            ctx["d"] = val if val is not None else ctx["d"]
        elif nid == "height" or filt is not None:
            if val is not None:
                ctx["h"] = val
        elif nid == "palette":
            if val is not None:
                ctx["color"] = val
            else:   # disabled: fall back to a grey ramp of the height
                hv = ctx["h"] if ctx["h"] is not None else np.zeros(land.shape, np.float32)
                g = np.clip(hv, 0, 1) * 255.0
                ctx["color"] = np.stack([g, g, g], -1).astype(np.float32)
        elif nid == "coast":
            if val is not None:
                ctx["color"] = val
        elif nid == "output":
            ctx["out"] = val

        if stop_at and nid == stop_at:
            break

    ctx["times"] = times
    return ctx


BASIN_CS = 16          # coarse cell for basin detection, in tiles

DEBUG_LABELS = {
    "contribution": "add / remove",
    "u": "flow east", "v": "flow north", "speed": "flow speed",
    "deposit": "deposited", "scour": "scoured",
    "labels": "zones", "target": "zone floor",
    "field": "raw field", "levels": "stepped",
    "dx": "shift x", "dy": "shift y",
    "bumps": "bumps", "mask": "mask weight",
    "eroded": "eroded", "flow": "flow direction",
    "accum": "drainage area", "slope": "slope",
    "blurred": "blurred", "gain": "gain",
}


def debug_channels():
    """[(channel key, label)] for every filter that has debug planes"""
    out = []
    for f in walk_filters(STATE["filters"]):
        for key in sorted(DEBUG_KEYS.get(f["type"], ())):
            name = f.get("name") or f["type"]
            out.append((f"dbg:{f['id']}:{key}",
                        f"{f['id']} {name} · {DEBUG_LABELS.get(key, key)}"))
    return out


DEBUG_KEYS = {
    "noise": ("contribution", "mask"),
    "features": ("contribution", "bumps", "mask"),
    "blur": ("contribution", "blurred"),
    "warp": ("contribution", "dx", "dy"),
    "erode": ("contribution", "eroded"),
    "drainage": ("contribution", "flow", "accum", "slope"),
    "shelf": ("contribution", "gain"),
    "regions": ("contribution", "field", "levels"),
    "basins": ("contribution", "labels", "target"),
    "currents": ("contribution", "u", "v", "speed", "deposit", "scour"),
}


def channel_image(ctx, channel):
    if channel.startswith("dbg:"):
        _, fid, key = channel.split(":", 2)
        plane = (ctx.get("debug") or {}).get(fid, {}).get(key)
        if plane is None:
            return np.zeros((*ctx["land"].shape, 3), np.uint8)
        v = np.asarray(plane, dtype=np.float32)
        if v.ndim == 3:
            return np.clip(v, 0, 255).astype(np.uint8)
        # signed fields read as red (down) / blue (up) around zero;
        # unsigned ones are stretched like the height channel
        if key in ("labels", "levels"):
            u, inv = np.unique(v, return_inverse=True)
            if len(u) == 1:
                g = np.full(v.shape, 110, np.uint8)     # a single zone is still a view
            else:
                g = (inv.reshape(v.shape) * (255.0 / (len(u) - 1))).astype(np.uint8)
            return np.stack([g] * 3, -1)
        if v.min() < -1e-6:
            m = max(1e-6, float(np.abs(v).max()))
            t = v / m
            rgb = np.zeros((*v.shape, 3), np.float32)
            rgb[..., 0] = np.clip(t, 0, 1) * 255          # positive = red
            rgb[..., 2] = np.clip(-t, 0, 1) * 255         # negative = blue
            rgb[..., 1] = np.clip(1 - np.abs(t), 0, 1) * 40
            return rgb.astype(np.uint8)
        lo, hi = float(v.min()), float(v.max())
        if hi - lo < 1e-6:
            g = np.full(v.shape, 128, np.uint8)
            return np.stack([g] * 3, -1)
        t = (v - lo) / (hi - lo)
        return np.stack([t * 255] * 3, -1).astype(np.uint8)
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


OVERLAY_KINDS = {
    "currents": ("flow", "zones", "speed"),
    "drainage": ("flow",),
    "warp": ("vectors",),
    "basins": ("zones",),
    "regions": ("contours",),
    "noise": ("contours",),
    "features": ("contours",),
    "blur": ("contours",),
    "erode": ("contours",),
    "shelf": ("contours",),
}


def _sampler(plane):
    h, w = plane.shape
    def at(x, y):
        ix = int(min(w - 1, max(0, x)))
        iy = int(min(h - 1, max(0, y)))
        return float(plane[iy, ix])
    return at


def overlay_png(ctx):
    """a transparent PNG of whatever the filters asked to show on top"""
    active = [f for f in walk_filters(STATE["filters"]) if f.get("overlay")]
    if not active:
        return None
    H, W = ctx["land"].shape
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    for f in active:
        kind = f["overlay"]
        planes = (ctx.get("debug") or {}).get(f["id"], {})
        step = max(6, int(f.get("ov_density", 26)))
        reach = max(2, int(f.get("ov_length", 14)))
        col = tuple(int(c) for c in str(f.get("ov_colour", "255,205,120")).split(",")) + (235,)

        if kind == "flow" and "u" in planes and "v" in planes:
            su, sv = _sampler(planes["u"]), _sampler(planes["v"])
            for y in range(step // 2, H, step):
                for x in range(step // 2, W, step):
                    if ctx["land"][y, x]:
                        continue
                    px, py, pts = float(x), float(y), []
                    for _ in range(reach):
                        pts.append((px, py))
                        px += su(px, py) * 6.0
                        py -= sv(px, py) * 6.0        # v is north-up
                        if not (0 <= px < W and 0 <= py < H):
                            break
                    if len(pts) > 2:
                        dr.line(pts, fill=col, width=1)

        elif kind == "flow" and "flow" in planes:
            sa = _sampler(planes["flow"])
            for y in range(step // 2, H, step):
                for x in range(step // 2, W, step):
                    if ctx["land"][y, x]:
                        continue
                    px, py, pts = float(x), float(y), []
                    for _ in range(reach):
                        pts.append((px, py))
                        a = sa(px, py)
                        px += np.cos(a) * 6.0
                        py += np.sin(a) * 6.0
                        if not (0 <= px < W and 0 <= py < H):
                            break
                    if len(pts) > 2:
                        dr.line(pts, fill=col, width=1)

        elif kind == "vectors" and "dx" in planes and "dy" in planes:
            sdx = _sampler(planes["dx"]); sdy = _sampler(planes["dy"])
            scale = max(0.25, min(3.0, 400.0 / max(1.0, float(np.abs(planes["dx"]).max()))))
            for y in range(step // 2, H, step):
                for x in range(step // 2, W, step):
                    if ctx["land"][y, x]:
                        continue
                    dx = sdx(x, y) * scale
                    dy = sdy(x, y) * scale
                    if abs(dx) + abs(dy) < 1.0:
                        continue
                    dr.line([(x, y), (x + dx, y - dy)], fill=col, width=1)
                    dr.ellipse([x + dx - 1, y - dy - 1, x + dx + 1, y - dy + 1], fill=col)

        elif kind == "speed" and "speed" in planes:
            v = np.clip(np.asarray(planes["speed"], np.float32), 0, 1)
            lo, hi = float(np.percentile(v[~ctx["land"]], 10)), float(np.percentile(v[~ctx["land"]], 99))
            t = np.clip((v - lo) / max(1e-6, hi - lo), 0, 1) ** 1.5
            wash = np.zeros((H, W, 4), np.uint8)
            wash[..., 0] = 255
            wash[..., 1] = 150
            wash[..., 2] = 40
            wash[..., 3] = (t * 80).astype(np.uint8)
            wash[ctx["land"]] = 0
            img.alpha_composite(Image.fromarray(wash, "RGBA"))

        elif kind in ("zones", "floor") and ("labels" in planes or "label_c" in planes):
            lab = planes.get("labels", planes.get("label_c"))
            v = np.asarray(lab, np.float32)
            edge = np.zeros(v.shape, bool)
            edge[:, 1:] |= v[:, 1:] != v[:, :-1]
            edge[1:, :] |= v[1:, :] != v[:-1, :]
            # draw every boundary pixel and thicken it: the labels are coarse,
            # so a 1-px line at 16-tile spacing reads as dashes
            thick = np.asarray(Image.fromarray(edge.astype(np.uint8) * 255)
                               .filter(ImageFilter.MaxFilter(3))) > 0
            ys, xs = np.nonzero(thick & ~ctx["land"])
            for yy, xx in zip(ys, xs):
                img.putpixel((int(xx), int(yy)), col)

        elif kind == "contours":
            plane = planes.get("contribution", planes.get("levels", planes.get("field")))
            if plane is None:
                continue
            v = np.asarray(plane, np.float32)
            bands = 12
            q = np.floor((v / max(1e-6, float(np.abs(v).max()))) * bands)
            edge = np.zeros(q.shape, bool)
            edge[:, 1:] |= q[:, 1:] != q[:, :-1]
            edge[1:, :] |= q[1:, :] != q[:-1, :]
            ys, xs = np.nonzero(edge & ~ctx["land"])
            for yy, xx in zip(ys[::2], xs[::2]):
                img.putpixel((int(xx), int(yy)), col)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --- render generations: a new request supersedes the one in flight --------
_gen = 0
_gen_lock = threading.Lock()


class _Cancelled(Exception):
    pass


def is_current(gen):
    with _gen_lock:
        return gen == _gen


def check_cancel(gen):
    """raise inside a render that a newer request has replaced"""
    if gen and not is_current(gen):
        raise _Cancelled()


def _progress(gen, stage, total, node):
    if is_current(gen):
        _render.update(stage=stage, total=total, node=node,
                       elapsed=round(time.time() - _render["started"], 2))


LIMITS = {
    ("height", "deep_dist"): (1.0, 20000.0),
    ("height", "curve"): (0.01, 20.0),
    ("height", "shelf_t"): (0.0, 0.98),
    ("height", "slope_t"): (0.01, 1.0),
    ("noise", "octaves"): (1, 8),
    ("noise", "scale"): (1.0, 20000.0),
    ("features", "octaves"): (1, 8),
    ("features", "scale"): (1.0, 20000.0),
    ("erode", "layers"): (1, 200),
    ("erode", "window"): (1, 40),
    ("basins", "levels"): (1, 12),
    ("basins", "threshold"): (1.0, 5000.0),
    ("currents", "reach"): (1.0, 2000.0),
    ("currents", "travel"): (1, 400),
    ("currents", "conserve"): (0.0, 1.0),
    ("currents", "direction"): (0.0, 1.0),
    ("currents", "deflect"): (0.0, 1.0),
    ("currents", "speed_bias"): (0.0, 1.0),
    ("regions", "levels"): (1, 24),
    ("shelf", "power"): (0.05, 8.0),
    ("coast", "strength"): (0.0, 1.0),
}


def _clamp_param(nid, node, key, val):
    """keep UI-reachable values inside the range the math can survive"""
    if key == "all" and isinstance(val, dict):
        return key, {k: _clamp_param(nid, node, k, v)[1] for k, v in val.items()}
    t = nid or (node or {}).get("type")
    rng = LIMITS.get((t, key))
    if rng and isinstance(val, (int, float)) and not isinstance(val, bool):
        val = type(val)(min(rng[1], max(rng[0], val)))
    if (t, key) == ("height", "slope_t") and node is not None:
        val = max(float(val), float(node.get("shelf_t", 0.0)) + 0.01)
    return key, val


def _wants_debug():
    ch = STATE.get("channel", "")
    if ch.startswith("dbg:"):
        return True
    return any(f.get("overlay") for f in walk_filters(STATE["filters"]))


def render_now(gen=None):
    """render the current state into the in-memory PNG"""
    global _png, _overlay
    if gen is None:
        with _gen_lock:
            gen = _gen
    t0 = time.time()
    try:
        blocked = render_guard(STATE["region"])
        if blocked:
            if is_current(gen):
                _render["error"] = blocked
            print("render blocked:", blocked, flush=True)
            return False
        if not STATE["nodes"]["height"].get("enabled", True):
            if is_current(gen):
                _render["error"] = ("the height node is off, so there is nothing "
                                    "to render; switch it back on")
            return False
        ch = STATE.get("channel", "output")
        stop = STATE["masks"][ch].get("tap") if ch in STATE["masks"] else None
        region = dict(STATE["region"])
        STATE["_want_debug"] = _wants_debug()
        ctx = evaluate(lambda i, t, n: _progress(gen, i, t, n),
                       stop_at=stop or None, region=region, gen=gen)
        rgb = channel_image(ctx, ch)
        sc_ = float(STATE["region"].get("scale", 1.0))
        if abs(sc_ - 1.0) > 1e-6:
            out_w = max(32, int(round(rgb.shape[1] * sc_)))
            out_h = max(32, int(round(rgb.shape[0] * sc_)))
            rgb = np.array(Image.fromarray(rgb, "RGB").resize((out_w, out_h), Image.LANCZOS))
        # the real map is a separate overlay layer in the browser, so it can
        # follow a drag without waiting for the sea render
        buf = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(buf, "PNG")
        with _lock:
            check_cancel(gen)      # a newer request landed while encoding
            _png = buf.getvalue()
            _render["stamp"] += 1
        _render["times"] = ctx.get("times", {})
        try:
            _overlay = overlay_png(ctx) if is_current(gen) else None
            _render["ov_stamp"] += 1       # reload it, or clear it when None
        except Exception as exp:
            _overlay = None
            print("overlay failed:", str(exp) or type(exp).__name__, flush=True)
            _render["ov_stamp"] += 1
    except _Cancelled:
        return False
    except Exception as exp:
        if is_current(gen):
            _render["error"] = str(exp) or type(exp).__name__
        return False
    finally:
        if is_current(gen):
            _render.update(running=False, last_ms=int((time.time() - t0) * 1000),
                           elapsed=round(time.time() - t0, 2), node="done")
    return True


def render_overlay_now():
    """rebuild just the overlay: cached nodes are reused, so this is cheap
    unless a filter with an overlay has to run"""
    global _overlay
    _render["ov_running"] = True
    try:
        STATE["_want_debug"] = True
        ctx = evaluate(region=dict(STATE["region"]))
        ov = overlay_png(ctx)
    except _Cancelled:
        _render["ov_running"] = False
        return False
    except Exception as exp:
        _render["ov_error"] = str(exp) or type(exp).__name__
        print("overlay failed:", _render["ov_error"], flush=True)
        _render["ov_running"] = False
        return False
    finally:
        _render["ov_running"] = False
    with _lock:
        _overlay = ov
        _render["ov_stamp"] += 1
        _render["ov_error"] = None
    return True


def render_async():
    """drop whatever is rendering and start on the current state"""
    global _gen
    with _gen_lock:
        _gen += 1
        gen = _gen
    _render.update(running=True, node="queued", stage=0, total=0, error=None,
                   started=time.time(), elapsed=0.0)
    threading.Thread(target=render_now, args=(gen,), daemon=True).start()
    return True


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
#prev img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;
image-rendering:pixelated;pointer-events:none;user-select:none;-webkit-user-drag:none}
#mapov{opacity:1;transition:none}
#ovl{opacity:1}
#prev{user-select:none}
.node{padding:2px 5px;cursor:pointer;border-radius:3px;display:flex;gap:6px;align-items:center}
.node.sel{background:#24405a}.node input{cursor:pointer}
.x{margin-left:auto;color:#8fa6ba;padding:0 4px}.x:hover{color:#e08a8a}
.io{color:#6b8093;font-size:10px;margin-left:auto;white-space:nowrap}
.t{color:#7d93a6;font-size:10px;font-variant-numeric:tabular-nums;opacity:.85}
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
#roll{flex:0 0 auto;background:#0b1117;border-top:1px solid #22303d;padding:4px 6px;
max-height:118px;overflow-x:auto;overflow-y:hidden}
#rolltitle{font-size:10px;color:#7d93a6;letter-spacing:.06em;text-transform:uppercase;
margin-bottom:3px}
#rollinner{display:flex;gap:6px;align-items:flex-start}
.snap{position:relative;flex:0 0 auto;cursor:pointer;border:1px solid #2a3a4b;
border-radius:3px;overflow:hidden;background:#0f161d}
.snap.here{border-color:#4f93bb;box-shadow:0 0 0 1px #4f93bb}
.snap img{display:block;width:118px;height:72px;object-fit:cover;image-rendering:pixelated}
.snap .n{position:absolute;left:2px;bottom:1px;font-size:9px;color:#dbe7f1;
background:rgba(8,12,18,.75);padding:0 3px;border-radius:2px}
.snap .b{position:absolute;top:1px;right:1px;font-size:10px;line-height:1;
padding:1px 3px;background:rgba(8,12,18,.8);color:#cfe0ee;border:0;border-radius:2px;
cursor:pointer}
.snap .b:hover{background:#24405a}
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
    <button id=zin title="zoom in: halve the crop, centred">+</button>
    <button id=zout title="zoom out: double the crop, centred">&minus;</button>
    <select id=scale title="how many pixels per tile the preview is drawn at; the render is always 1:1"><option value=1>1 px/tile<option value=0.5>0.5 px (more area)</select>
    <select id=chan></select>
    <button id=go>render</button>
    <button id=snap title="keep the current settings and picture in the film roll">snapshot</button>
    <label style="display:inline;color:var(--t)"><input id=auto type=checkbox> auto</label>
    <label style="display:inline;color:var(--t)" title="anchor noise to the map instead of the crop: previews then match a full render, at the cost of changing how existing presets look"><input id=wnoise type=checkbox> world noise</label>
    <select id=presets style="width:auto;min-width:90px"></select>
    <button id=load>load</button><button id=save>save</button>
    <button id=del title="delete the selected save">×</button><button id=reset>reset</button>
  </div>
  <div id=barwrap><div id=bar></div><span id=prog></span></div>
  <div id=prev><img id=img draggable=false><img id=mapov draggable=false>
    <img id=ovl draggable=false></div>
  <div id=roll><div id=rolltitle>snapshots <span class=small>(click to view, load to apply)</span></div>
    <div id=rollinner></div></div>
</div>
<div id=right><h3 id=nodeTitle>select a node</h3><div id=params></div>
  <div id=curvewrap></div><h3 id=wiredH>masks wired</h3><div id=wired></div>
  <div id=ovparams></div></div>
<script>
let S=null, sel=null, lastStamp=-1, lastOv=-1, curveDrag=null, panDrag=null;
async function api(path, body){const r=await fetch(path,{method:body?'POST':'GET',
  headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.headers.get('content-type')?.includes('json')?r.json():r.text();}
let polling=false, pollAgain=false, imgBusy=false;
async function poll(){
  if(polling){pollAgain=true;return;}
  polling=true;
  try{
    let s;
    try{ s=await api('/api/status'); }
    catch(e){ setTimeout(poll,400); return; }
    const pct=s.running&&s.total?Math.round(100*s.stage/s.total):100;
    document.getElementById('bar').style.width=pct+'%';
    document.getElementById('prog').textContent=s.running
      ?`${s.node} ${s.stage}/${s.total} ${Number(s.elapsed).toFixed(1)}s`
      :(s.error?('error: '+s.error)
        :(s.ov_error?('overlay: '+s.ov_error)
          :(imgBusy?'loading image…':(s.ov_running?'overlay…'
            :(s.last_ms?`${s.last_ms} ms`:'idle')))));
    paintTimes(s.times||{});
    if((s.ov_stamp||0)!==lastOv){
      lastOv=s.ov_stamp||0;
      const ov=document.getElementById('ovl');
      if(hasOverlay()){
        ov.src='/api/overlay?t='+lastOv;
        ov.style.display='block';
      } else { ov.removeAttribute('src'); ov.style.display='none'; }
    }
    if(s.stamp!==lastStamp){
      lastStamp=s.stamp;
      const im=document.getElementById('img');
      if(viewing) return;             // stay on the snapshot being compared
      imgBusy=true;
      im.onload=()=>{imgBusy=false;poll();};
      im.onerror=()=>{imgBusy=false;};
      im.src='/api/preview?t='+s.stamp;
      im.style.transform='none';
      syncMap();
    }
    if(s.running||s.ov_running)setTimeout(poll,250);
  }finally{
    polling=false;
    if(pollAgain){pollAgain=false;setTimeout(poll,120);}
  }
}
function render(){api('/api/render',{}).then(()=>poll());}
function hasOverlay(){
  let any=false; walkF(S.filters,f=>{ if(f.overlay) any=true; }); return any;
}
function mapUrl(r){
  return `/api/map?x0=${r.x0}&y0=${r.y0}&w=${Math.round(r.w)}&h=${Math.round(r.h)}&t=${Date.now()}`;
}
let mapSoon=null;
function syncMapSoon(){
  if(mapSoon) return;
  mapSoon=setTimeout(()=>{mapSoon=null;syncMap();},90);
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
  document.getElementById('wnoise').checked=!!S.world_noise;
  syncMap();
  applyRegion(S.region);
  renderTop(); renderNodes(); renderMasks();
  const ft=document.getElementById('ftype');
  if(ft) ft.innerHTML=Object.keys(S.filter_defs||{}).map(t=>`<option>${t}</option>`).join('');
  loadPresets();
  loadRoll();
  const known=(S.filters||[]).some(f=>f.id===S.last)||S.nodes[S.last];
  select(known?S.last:((S.filters&&S.filters[0])?S.filters[0].id:'height'));
  render();
}
function renderTop(){
  const l=S.channel_labels||{};
  chan.innerHTML=S.channels.map(c=>`<option value="${c}" ${c===S.channel?'selected':''}>`+
    (l[c]||c)+`</option>`).join('');
}
let NODE_TIMES={};
function paintTimes(t){
  if(t) NODE_TIMES=t;
  for(const el of document.querySelectorAll('.node')){
    const sp=el.querySelector('.t'); if(!sp) continue;
    const ms=NODE_TIMES[el.dataset.node];
    sp.textContent = ms===undefined ? ''
      : (ms>=1000 ? (ms/1000).toFixed(2)+'s' : (ms>=10?Math.round(ms):ms.toFixed(1))+'ms');
  }
}
function walkF(list, cb, depth){
  (list||[]).forEach(f=>{ cb(f, depth||0); walkF(f.children||[], cb, (depth||0)+1); });
}
function findFilter(id){ let hit=null; walkF(S.filters,f=>{ if(f.id===id) hit=f; }); return hit; }
function fLabel(f){ return (f&&f.name) ? f.name : (f?f.type:id); }
function mLabel(id){ const m=S.masks[id]; return (m&&m.name)?m.name:id; }
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
    d.dataset.node=id;
    const tm=document.createElement('span'); tm.className='t'; d.appendChild(tm);
    const io=S.io&&S.io[id]; if(io) ioLabel(d,id,io[0],io[1],id+' takes '+io[0]+' and outputs '+io[1]);
    d.onclick=()=>select(id); el.appendChild(d);
  }
  const fe=document.getElementById('filters'); fe.innerHTML='';
  walkF(S.filters,(f,depth)=>{
    const d=document.createElement('div'); d.className='node'+(sel===f.id?' sel':'');
    d.style.paddingLeft=(5+depth*13)+'px';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=f.enabled;
    cb.onclick=e=>{e.stopPropagation(); set('filters',f.id,'enabled',cb.checked);};
    d.appendChild(cb); d.appendChild(document.createTextNode(fLabel(f)));
    d.dataset.node=f.id;
    const tm=document.createElement('span'); tm.className='t'; d.appendChild(tm);
    d.title=(depth?'sub-filter of a filter':'filter')+' #'+f.id+' ('+f.type+'): height in, height out';
    const up=document.createElement('span'); up.className='x'; up.textContent='↑';
    up.onclick=e=>{e.stopPropagation(); api('/api/filter/move',{id:f.id,dir:'up'}).then(reload);};
    const dn=document.createElement('span'); dn.className='x'; dn.textContent='↓';
    dn.onclick=e=>{e.stopPropagation(); api('/api/filter/move',{id:f.id,dir:'down'}).then(reload);};
    const x=document.createElement('span'); x.className='x'; x.textContent='×';
    x.onclick=e=>{e.stopPropagation(); api('/api/filter/del',{id:f.id}).then(reload);};
    d.append(up,dn,x);
    d.onclick=()=>select(f.id); fe.appendChild(d);
  });
  paintTimes();
}
function renderMasks(){
  const el=document.getElementById('masks'); el.innerHTML='';
  for(const id of Object.keys(S.masks)){
    const m=S.masks[id];
    const d=document.createElement('div'); d.className='node'+(sel===id?' sel':'');
    d.appendChild(document.createTextNode(mLabel(id)));
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
  const f=findFilter(id);
  document.getElementById('nodeTitle').textContent=f?fLabel(f):mLabel(id);
  renderParams(); renderNodes(); renderMasks();}
const OVERLAY_KINDS={
  currents:['flow','zones','speed'], drainage:['flow'], warp:['vectors'],
  basins:['zones'], regions:['contours'], noise:['contours'],
  features:['contours'], blur:['contours'], erode:['contours'], shelf:['contours']
};
const OVERLAY_DENSITY_KINDS=['flow','vectors'];
const CHOICES={
  'nodes.height.mode':['profile','ramp'],
  'nodes.distance.down':['1','2']
};
// enum parameters on filters, keyed type|param
const FILTER_CHOICES={
  'currents|source':['distance','height','slope'],
  'noise|kind':['value','perlin','worley','ridged','billow','white'],
  'features|kind':['value','perlin','worley','ridged','billow','white'],
  'regions|kind':['value','perlin','worley','ridged','billow','white'],
  'warp|kind':['value','perlin','worley','ridged','billow','white']
};
// a parameter nothing reads (wrong mode, wrong source, node switched off) is
// shown greyed out, so the panel says which knobs currently do anything
function inactiveParam(kind,id,k,o){
  if(!o) return false;
  if(kind==='nodes'){
    if(id==='height'){

      if(k==='shelf_t'||k==='slope_t') return o.mode!=='profile';
      return o.enabled===false;
    }
    if(id==='palette'||id==='coast') return o.enabled===false;
    if(id==='distance') return !!S.has_dist;   // the cached field ignores it
    return false;                      // distance.down still shapes the load
  }
  if(kind==='filter'){
    if(o.enabled===false) return true;
    if(o.type==='currents'&&k==='deflect_reach') return !(o.deflect>0);
    if(o.type==='currents'&&k==='reach') return o.source!=='distance';
    if(o.type==='currents'&&k==='source_level') return o.source!=='height';
    return false;
  }
  if(kind==='mask'){
    if(k==='tap') return o.source!=='height';
    if(k==='scale'||k==='seed') return o.source!=='noise';
  }
  return false;
}
const BLEND_ALL=['add','subtract','multiply','screen','overlay','lighter','darker'];
// a filter whose contribution never changes sign cannot use the split modes
const SIGN_FIXED={features:'positive',drainage:'negative',shelf:'negative',erode:'negative'};
function blendChoices(kind,id){
  if(kind!=='filter') return BLEND_ALL;
  const f=findFilter(id); if(!f) return BLEND_ALL;
  const s=SIGN_FIXED[f.type];
  return BLEND_ALL.map(m=>{
    if(s==='positive' && (m==='darker')) return {v:m,label:m+' (no effect: only adds)',off:true};
    if(s==='negative' && (m==='lighter')) return {v:m,label:m+' (no effect: only removes)',off:true};
    return m;
  });
}
function field(kind,id,k,v,opts){
  const lab=document.createElement('label'); lab.textContent=k;
  const dkey=(opts&&opts.dkey)?opts.dkey
    :((k==='blend')?'blend':((kind==='mask')?('mask.'+k):(id+'.'+k)));
  const tip=(window.DESC||{})[dkey]
    ||(k==='enabled'?'enabled: off = this node passes its input straight through':'');
  lab.title=tip;
  let inp;
  const choices=(k==='blend')?blendChoices(kind,id)
    :((opts&&opts.options)||CHOICES[kind+'.'+id+'.'+k]||null);
  if(choices){
    const opt=o=>{
      const val=(o&&typeof o==='object')?o.v:o;
      const lab=(o&&typeof o==='object')?o.label:o;
      const off=(o&&o.off)?'disabled':'';
      return `<option value="${val}" ${off} ${String(val)===String(v)?'selected':''}>${lab}</option>`;
    };
    inp=document.createElement('select');
    inp.innerHTML=choices.map(opt).join('');
    inp.onchange=()=>set(kind,id,k,typeof v==='number'?parseFloat(inp.value):inp.value);
  } else if(typeof v==='boolean'){
    inp=document.createElement('input'); inp.type='checkbox'; inp.checked=v;
    inp.onchange=()=>set(kind,id,k,inp.checked);
  } else if(typeof v==='number'){
    inp=document.createElement('input'); inp.type='number'; inp.step='any'; inp.value=v;
    inp.onchange=()=>{ const x=parseFloat(inp.value);
      if(!isFinite(x)){ inp.value=v; return; }       // blank box keeps the old value
      set(kind,id,k,x); };
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
  document.getElementById('ovparams').innerHTML='';
  const wiredEl=document.getElementById('wired'); wiredEl.innerHTML='';
  const wiredH=document.getElementById('wiredH'); if(wiredH) wiredH.style.display='none';
  const node=S.nodes[sel], mask=S.masks[sel];
  const filt=findFilter(sel);
  if(mask){
    const nl=document.createElement('label'); nl.textContent='name';
    nl.title='shown wherever this mask can be wired in';
    const ni=document.createElement('input'); ni.type='text'; ni.value=mask.name||'';
    ni.placeholder=sel; ni.onchange=()=>set('masks',sel,'name',ni.value);
    el.append(nl,ni);
    const KEYS=['source','tap','scale','seed','lo','hi','invert'];
    for(const [k,v] of Object.entries(mask)){
      if(!KEYS.includes(k)) continue;
      if(k==='tap'){
        const o=[{v:'',label:'(none)'}].concat(S.taps.map(id=>{
          if(id==='base') return {v:id,label:'height (base)'};
          const f=findFilter(id);
          return {v:id,label:(f?fLabel(f):id)+'  ·  '+id};
        }));
        el.append(...field('mask',sel,k,v,{options:o,obj:mask}));
      } else if(k==='source'){
        el.append(...field('mask',sel,k,v,{options:['height','distance','noise'],obj:mask}));
      } else {
        el.append(...field('mask',sel,k,v,{obj:mask}));
      }
    }
  } else if(filt){
    const nl=document.createElement('label'); nl.textContent='name';
    nl.title='shown in the node list and wherever this filter can be tapped';
    const ni=document.createElement('input'); ni.type='text'; ni.value=filt.name||'';
    ni.placeholder=filt.type;
    ni.onchange=()=>set('filters',filt.id,'name',ni.value);
    el.append(nl,ni);
    const defs=(S.filter_defs||{})[filt.type]||{};
    for(const [k,v] of Object.entries(defs)){
      if(k==='masks'||k==='out_curve') continue;
      el.append(...field('filter',filt.id,k,filt[k]!==undefined?filt[k]:v,
        {dkey:filt.type+'.'+k,obj:filt,
         options:FILTER_CHOICES[filt.type+'|'+k]}));
    }
    for(const [k,v] of Object.entries(filt)){
      if(k in defs||['id','type','enabled','masks','out_curve','children','name',
                     'overlay','ov_density','ov_length','ov_colour'].includes(k)) continue;
      el.append(...field('filter',filt.id,k,v,{dkey:filt.type+'.'+k,obj:filt,
        options:FILTER_CHOICES[filt.type+'|'+k]}));
    }
    const sh=document.createElement('h3'); sh.textContent='sub-filters';
    el.appendChild(sh);
    const row=document.createElement('div'); row.style.display='flex'; row.style.gap='4px';
    const ts=document.createElement('select');
    ts.innerHTML=Object.keys(S.filter_defs||{}).map(t=>`<option>${t}</option>`).join('');
    const ab=document.createElement('button'); ab.textContent='+';
    ab.title='add a filter that runs on this filter\'s output, before it is blended into the height';
    ab.onclick=()=>api('/api/filter/add',{type:ts.value,parent:filt.id}).then(reload);
    row.append(ts,ab); el.appendChild(row);
    if((filt.children||[]).length){
      const note=document.createElement('div'); note.className='small';
      note.textContent=(filt.children||[]).length+' sub-filter'+
        ((filt.children||[]).length>1?'s':'')+' — they act on this filter\'s output';
      el.appendChild(note);
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
    const wired=document.getElementById('wired');
    const wh=document.getElementById('wiredH'); if(wh) wh.style.display='block';
    for(const mid of Object.keys(S.masks)){
      const l=document.createElement('label'); const cb=document.createElement('input');
      cb.type='checkbox'; cb.checked=(filt.masks||[]).includes(mid);
      l.title='wire this mask into the filter: the effect only applies where the mask '
        + 'is on (blur and warp) or is scaled by it (noise, features, erode, drainage, shelf)';
      cb.title=l.title;
      cb.onchange=()=>{const arr=new Set(filt.masks||[]);
        cb.checked?arr.add(mid):arr.delete(mid); set('filters',filt.id,'masks',[...arr]);};
      l.appendChild(cb); l.appendChild(document.createTextNode(mLabel(mid))); wired.appendChild(l);
    }
    const box=document.getElementById('ovparams');
    const oh=document.createElement('h3'); oh.textContent='overlay';
    box.appendChild(oh);
    const orow=document.createElement('div'); orow.style.display='flex'; orow.style.gap='4px';
    const osel=document.createElement('select');
    const kinds=[''].concat(OVERLAY_KINDS[filt.type]||[]);
    osel.innerHTML=kinds.map(k=>`<option value="${k}" ${k===(filt.overlay||'')?'selected':''}>`+
      (k||'off')+`</option>`).join('');
    osel.title='draw this filter\'s debug data over the preview (on release only)';
    osel.onchange=()=>set('filters',filt.id,'overlay',osel.value);
    orow.append(osel);
    const col=document.createElement('input');
    col.type='text'; col.value=filt.ov_colour||'255,205,120'; col.style.width='110px';
    col.title='overlay colour, r,g,b';
    col.onchange=()=>set('filters',filt.id,'ov_colour',col.value);
    orow.append(col);
    box.appendChild(orow);
    const usesDensity=OVERLAY_DENSITY_KINDS.includes(filt.overlay||'');
    const drow=document.createElement('div'); drow.style.display='flex'; drow.style.gap='6px';
    for(const [k,lab] of [['ov_density','spacing'],['ov_length','length']]){
      const l=document.createElement('label'); l.textContent=lab; l.style.margin='0';
      const i=document.createElement('input'); i.type='number'; i.step='1';
      i.value=filt[k]!==undefined?filt[k]:(k==='ov_density'?26:14);
      i.style.width='64px'; i.onchange=()=>set('filters',filt.id,k,parseFloat(i.value));
      if(!usesDensity || (k==='ov_length' && filt.overlay==='zones')){ i.disabled=true;
        l.classList.add('off'); i.classList.add('off'); }
      drow.append(l,i);
    }
    box.appendChild(drow);

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
    // fixed nodes read no masks, so nothing is offered here
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
  const commit=()=>set('nodes','palette','all',{stops,cols});
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
  if(k==='overlay'||k==='ov_density'||k==='ov_length'||k==='ov_colour'){
    api('/api/overlay',{}).then(()=>setTimeout(poll,150));
  }
  // anything that changes what the panel draws (a reset curve, a new mode)
  // has to rebuild it; the curve canvas holds its own copy of the points
  if(k==='mode'||k==='source'||k==='enabled'||k==='out_curve'||k==='overlay') renderParams();
  if(k==='overlay_map') syncMap();
  if(k==='name'){ renderNodes(); renderMasks(); }
  if(S.auto) setTimeout(poll,120); else poll();
}
let viewing=null;                 // snapshot id being looked at, or null for live
async function loadRoll(){
  const r=await api('/api/snapshots');
  const box=document.getElementById('rollinner'); box.innerHTML='';
  r.snapshots.forEach((sn,i)=>{
    const d=document.createElement('div');
    d.className='snap'+(viewing===sn.id?' here':'');
    d.title=sn.name+'  ('+sn.w+'x'+sn.h+', '+sn.channel+')';
    const im=document.createElement('img');
    im.src='/api/snapshot/thumb?id='+encodeURIComponent(sn.id);
    im.onclick=()=>viewSnapshot(sn.id);
    const n=document.createElement('span'); n.className='n'; n.textContent='S'+(i+1);
    const load=document.createElement('button'); load.className='b'; load.textContent='load';
    load.title='put these settings into the editor';
    load.onclick=e=>{e.stopPropagation(); api('/api/snapshot/load',{id:sn.id})
      .then(()=>{viewing=null; return boot();});};
    const del=document.createElement('button'); del.className='b'; del.textContent='×';
    del.style.right='30px'; del.title='delete this snapshot';
    del.onclick=e=>{e.stopPropagation(); if(!confirm('delete this snapshot?'))return;
      api('/api/snapshot/del',{id:sn.id}).then(()=>{ if(viewing===sn.id) viewSnapshot(null);
        loadRoll(); });};
    d.append(im,n,load,del); box.appendChild(d);
  });
  const cur=document.createElement('div');
  cur.className='snap'+(viewing===null?' here':'');
  cur.title='back to the live render';
  cur.innerHTML='<div style="width:118px;height:72px;display:flex;align-items:center;'+
    'justify-content:center;color:#8fa6ba;font-size:11px">current</div>';
  cur.onclick=()=>viewSnapshot(null);
  box.prepend(cur);
}
function viewSnapshot(id){
  viewing=id;
  const im=document.getElementById('img');
  if(id){ im.src='/api/snapshot/image?id='+encodeURIComponent(id); }
  else { im.src='/api/preview?t='+lastStamp; }
  loadRoll();
}
async function snap(){
  await api('/api/snapshot',{});
  viewing=null;
  loadRoll();
}
document.getElementById('snap').onclick=snap;
document.getElementById('go').onclick=render;
document.getElementById('addmask').onclick=()=>api('/api/mask/add',{}).then(reload);
document.getElementById('auto').onchange=e=>api('/api/auto',{on:e.target.checked});
document.getElementById('wnoise').onchange=e=>
  api('/api/set',{id:'world_noise',k:'all',v:e.target.checked}).then(()=>{render();});
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
function applyRegion(r){rx.value=r.x0;ry.value=r.y0;rw.value=r.w;rh.value=r.h;
  scale.value=String(r.scale);}
function commitRegion(){
  // the server may refuse a region; re-read what it actually kept
  api('/api/set',{id:'region',k:'all',v:region()}).then(async()=>{
    S=await api('/api/state'); applyRegion(S.region); render();});
}
function zoomBy(f){
  const r=region(), MW=S.map.w, MH=S.map.h;
  let w=Math.max(64, Math.min(MW, Math.round(r.w*f)));
  let h=Math.max(48, Math.min(MH, Math.round(r.h*f)));
  const cap=4000000;                     // same ceiling the server enforces
  const px=w*h*(+scale.value)**2;
  if(px>cap){ const k=Math.sqrt(cap/px); w=Math.floor(w*k); h=Math.floor(h*k); }
  const x0=Math.round(r.x0+(r.w-w)/2), y0=Math.round(r.y0+(r.h-h)/2);
  applyRegion({x0:Math.max(0,Math.min(MW-w,x0)), y0:Math.max(0,Math.min(MH-h,y0)),
               w, h, scale:r.scale});
  commitRegion();
}
for(const id of ['rx','ry','rw','rh']) document.getElementById(id).onchange=commitRegion;
document.getElementById('zin').onclick=()=>zoomBy(0.5);
document.getElementById('zout').onclick=()=>zoomBy(2);
document.getElementById('scale').onchange=commitRegion;
document.getElementById('chan').onchange=()=>{const v=chan.value;
  api('/api/set',{id:'channel',k:'all',v}).then(()=>{S.channel=v;render();});};
const prev=document.getElementById('prev');
let panTimer=null;
async function pushRegion(){
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
  const xs=prev.clientWidth/(S.region.w||prev.clientWidth);
  const ys=prev.clientHeight/(S.region.h||prev.clientHeight);
  rx.value=+rx.value-Math.round(dx/xs);
  ry.value=+ry.value-Math.round(dy/ys);
  // nudge what we have immediately, then render the strip we are moving to
  const mo=document.getElementById('mapov');
  if(S.nodes.output&&S.nodes.output.overlay_map) syncMapSoon();
  // the sea is intentionally left stale until release
});
addEventListener('mouseup',()=>{if(!panDrag)return;panDrag=null;prev.style.cursor='grab';
  pushRegion();});
boot();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
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
        elif self.path.startswith("/api/overlay"):
            if _overlay is None:
                self.send_response(204); self.end_headers()
            else:
                self._png(_overlay)
        elif self.path.startswith("/api/snapshots"):
            self._json({"snapshots": snapshot_list()})
        elif self.path.startswith("/api/snapshot/thumb"):
            from urllib.parse import urlparse, parse_qs
            sid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            path = os.path.join(SNAPSHOTS, os.path.basename(sid) + "_thumb.png")
            if os.path.isfile(path):
                self._png(open(path, "rb").read())
            else:
                self.send_response(404); self.end_headers()
        elif self.path.startswith("/api/snapshot/image"):
            from urllib.parse import urlparse, parse_qs
            sid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            path = os.path.join(SNAPSHOTS, os.path.basename(sid) + ".png")
            if os.path.isfile(path):
                self._png(open(path, "rb").read())
            else:
                self.send_response(404); self.end_headers()
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
                im = map_rgba()
                MW, MH = im.size
                x0 = int(float(q.get("x0", [0])[0]))
                y0 = int(float(q.get("y0", [0])[0]))
                w = int(float(q.get("w", [1200])[0]))
                h = int(float(q.get("h", [800])[0]))
                if w < 1 or h < 1 or w > 8000 or h > 8000:
                    raise ValueError("size")
                x0 = max(0, min(MW - 1, x0))
                y0 = max(0, min(MH - 1, y0))
                tile = im.crop((x0, y0, min(MW, x0 + w), min(MH, y0 + h)))
                if tile.size != (w, h):
                    tile = tile.resize((w, h), Image.NEAREST)
                buf = io.BytesIO(); tile.save(buf, "PNG")
                self._png(buf.getvalue())
            except Exception:
                self.send_response(204)      # half-typed numbers: just show nothing
                self.end_headers()
        elif self.path == "/api/presets":
            names = sorted(f[:-5] for f in os.listdir(PRESETS)) if os.path.isdir(PRESETS) else []
            self._json({"presets": names})
        elif self.path == "/api/status":
            with _lock:
                data = dict(_render)
            self._json(data)
        elif self.path == "/api/state":
            with _lock:
                taps = ["base"] + [f["id"] for f in walk_filters(STATE["filters"])]
                st = {"region": STATE["region"], "channel": STATE["channel"],
                      "map": {"w": int(load_map().shape[1]),
                              "h": int(load_map().shape[0])},
                      "overlay_map": bool(STATE["nodes"]["output"].get("overlay_map")),
                      "has_dist": bool(dist_full() is not None),
                      "nodes": STATE["nodes"], "masks": STATE["masks"],
                      "filters": STATE["filters"], "filter_defs": FILTER_DEFS,
                      "order": ["distance", "height"] + [f["id"] for f in STATE["filters"]]
                               + ["palette", "coast", "output"],
                      "channels": (["output", "h", "d", "color"]
                                   + list(STATE["masks"].keys())
                                   + [c for c, _ in debug_channels()]),
                      "channel_labels": dict(
                          [("output", "output"), ("h", "height"), ("d", "distance"),
                           ("color", "colour")]
                          + [(m, (STATE["masks"][m].get("name") or m))
                             for m in STATE["masks"]]
                          + debug_channels()),
                      "taps": taps,
                      "desc": DESC, "io": NODE_IO,
                      "last": STATE.get("last", "erode"), "auto": _render["auto"],
                      "world_noise": bool(STATE.get("world_noise"))}
            self._json(st)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self.send_response(400); self.end_headers(); return
        try:
            self._post(body)
        except Exception as exp:
            msg = str(exp) or type(exp).__name__
            print("POST", self.path, "failed:", msg, flush=True)
            try:
                self._json({"ok": False, "error": msg}, 400)
            except Exception:
                pass

    def _post(self, body):
        with _lock:
            if self.path == "/api/filter/add":
                n = STATE.get("next_filter", 1)
                typ = body.get("type", "noise")
                new = dict(id=f"f{n}", type=typ, enabled=True, name="",
                           masks=[], out_curve=[[0, 0], [1, 1]], children=[],
                           **OVERLAY_DEFAULTS, **FILTER_DEFS[typ])
                parent = find_filter(body["parent"]) if body.get("parent") else None
                if parent is not None:
                    parent.setdefault("children", []).append(new)
                else:
                    STATE["filters"].append(new)
                STATE["next_filter"] = n + 1
            elif self.path == "/api/filter/del":
                fid = body.get("id")
                _, owner = filter_owner(fid)
                if owner is not None:
                    owner[:] = [f for f in owner if f["id"] != fid]
                for m in STATE["masks"].values():
                    if m.get("tap") == fid:
                        m["tap"] = ""
                ch = STATE.get("channel", "")
                if ch.startswith(f"dbg:{fid}:"):
                    STATE["channel"] = "output"
            elif self.path == "/api/filter/move":
                _, owner = filter_owner(body.get("id"))
                if owner is not None:
                    ids = [f["id"] for f in owner]
                    i = ids.index(body.get("id"))
                    j = i + (1 if body.get("dir") == "down" else -1)
                    if 0 <= j < len(ids):
                        owner[i], owner[j] = owner[j], owner[i]
            elif self.path == "/api/mask/add":
                n = STATE.get("next_mask", 1)
                State_mid = f"mask_{n}"
                STATE["next_mask"] = n + 1
                STATE["masks"][State_mid] = dict(
                    source="height", tap="", scale=400, seed=3, lo=0.2, hi=0.8,
                    invert=False)
            elif self.path == "/api/mask/del":
                mid = body.get("id")
                STATE["masks"].pop(mid, None)
                for node in list(STATE["nodes"].values()) + list(walk_filters(STATE["filters"])):
                    if isinstance(node.get("masks"), list) and mid in node["masks"]:
                        node["masks"].remove(mid)
                if STATE.get("channel") == mid:
                    STATE["channel"] = "output"
            elif self.path == "/api/reset":
                STATE.clear()
                STATE.update(default_state())
                _cache.clear()
                _png = None
            elif self.path == "/api/auto":
                _render["auto"] = bool(body.get("on"))
            elif self.path == "/api/set":
                if "id" not in body or "k" not in body or "v" not in body:
                    raise ValueError("set needs id, k and v")
                nid, key, val = body["id"], body["k"], body["v"]
                if isinstance(val, float) and val != val:
                    raise ValueError("that value is not a number")
                if nid == "region":
                    r = dict(val)
                    px = max(1, int(r.get("w", 0)) * int(r.get("h", 0)))
                    if px > MAX_REGION_PX:
                        k = (MAX_REGION_PX / float(px)) ** 0.5
                        w0, h0 = int(r["w"]), int(r["h"])
                        r["w"] = max(64, int(w0 * k))
                        r["h"] = max(48, int(h0 * k))
                        r["x0"] = int(r.get("x0", 0) + (w0 - r["w"]) / 2)
                        r["y0"] = int(r.get("y0", 0) + (h0 - r["h"]) / 2)
                        _render["error"] = (
                            f"region was {w0}x{h0}, capped to {r['w']}x{r['h']} "
                            f"({MAX_REGION_PX/1e6:.0f} MPx limit)")
                    else:
                        _render["error"] = None
                    w_map = load_map().shape[1]; h_map = load_map().shape[0]
                    r["x0"] = max(0, min(w_map - max(32, int(r["w"])), int(r["x0"])))
                    r["y0"] = max(0, min(h_map - max(32, int(r["h"])), int(r["y0"])))
                    STATE["region"] = r
                elif nid == "channel":
                    STATE["channel"] = val
                elif nid == "overlay":
                    STATE["overlay_map"] = bool(val)

                elif nid in STATE["nodes"]:
                    key, val = _clamp_param(nid, None, key, val)
                    if key == "all":
                        STATE["nodes"][nid].update(val)
                    else:
                        STATE["nodes"][nid][key] = val
                else:
                    f = find_filter(nid)
                    if f is not None:
                        key, val = _clamp_param(None, f, key, val)
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
            elif self.path == "/api/snapshot":
                snapshot_save(body.get("name", ""))
            elif self.path == "/api/snapshot/del":
                sid = os.path.basename(body.get("id", ""))
                for suffix in (".json", ".png", "_thumb.png"):
                    p_ = os.path.join(SNAPSHOTS, sid + suffix)
                    if os.path.isfile(p_):
                        os.remove(p_)
            elif self.path == "/api/snapshot/load":
                snapshot_load(os.path.basename(body.get("id", "")))
            elif self.path == "/api/preset/save":
                os.makedirs(PRESETS, exist_ok=True)
                name = os.path.basename(body["name"])
                path = os.path.join(PRESETS, name + ".json")
                with open(path, "w") as f:
                    json.dump({k: STATE[k] for k in STATE_KEYS if k in STATE},
                              f, indent=1)
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
                for nid, params in data.get("nodes", {}).items():
                    if nid in STATE["nodes"]:
                        STATE["nodes"][nid].update(params)
                if "masks" in data:
                    STATE["masks"] = data["masks"]
                # a preset is a snapshot: no filters really means no filters
                STATE["filters"] = data.get("filters", [])
                normalise_filters(STATE["filters"])
                for k in ("channel", "overlay_map", "world_noise"):
                    if k in data:
                        STATE[k] = data[k]
                STATE["next_filter"] = max(
                    [int(f["id"][1:]) for f in walk_filters(STATE["filters"])
                     if f.get("id", "").startswith("f") and f["id"][1:].isdigit()] + [0]) + 1
                STATE["next_mask"] = max(
                    [int(k.split("_")[1]) for k in STATE["masks"]
                     if k.startswith("mask_") and k.split("_")[1].isdigit()] + [0]) + 1
                if STATE["channel"] not in ("output", "h", "d", "color") \
                        and not STATE["channel"].startswith("dbg:"):
                    STATE["channel"] = "output"
                _cache.clear()
        if self.path != "/api/render" and self.path != "/api/select":
            save_state()
        # outside the state lock: never hold it while rendering
        if self.path == "/api/render":
            render_async()
        elif self.path == "/api/overlay":
            threading.Thread(target=render_overlay_now, daemon=True).start()
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
    ap.add_argument("--mem-mb", type=int, default=3072,
                    help="address-space cap for the editor process (MB)")
    args = ap.parse_args()
    try:
        import resource
        cap = args.mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception as exp:
        print("could not set memory cap:", exp, flush=True)
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
