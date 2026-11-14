#!/usr/bin/env python3
"""Render a saved editor preset over the whole map, in strips.

    python3 tools/sea_full.py something_maybe [--out out/sea/full.png] [--scale 1]

The editor caps a region at 4 MPx, so a full map (7749x5254) is rendered as
overlapping horizontal strips and stitched. Two things make the strips line up
with each other:

- the distance field is the global one (dist_half.npy), not the per-crop EDT
  the editor uses for previews
- every strip is rendered with a margin and the edges are cropped off, so
  filters with reach (warp, blur, drainage) see real neighbours
"""
import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sea_core as sc                                          # noqa: E402
import sea_editor as ed                                        # noqa: E402

WORK = os.path.join(os.path.dirname(HERE), "out", "sea")
PRESETS = os.path.join(WORK, "presets")

STRIP_H = 1200          # rows of finished image per pass
MARGIN = 300            # rows of context on each side, cropped away after


def _peak_mb():
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


HEIGHT_PATH = os.path.join(WORK, "height_full.npy")


def _save_height(path, h):
    """Write the final filtered height as 0..1 float16, one value per tile."""
    np.save(path, h)
    lo, hi = np.percentile(np.asarray(h, dtype=np.float32), [1, 99])
    print(f"height p1 {lo:.4f} p99 {hi:.4f} "
          f"{h.shape[0]}x{h.shape[1]} float16", flush=True)
    print("wrote", path, flush=True)


def apply_preset(name):
    path = os.path.join(PRESETS, name + ".json")
    with open(path) as f:
        data = json.load(f)
    for nid, params in data.get("nodes", {}).items():
        if nid in ed.STATE["nodes"]:
            ed.STATE["nodes"][nid].update(params)
    if "masks" in data:
        ed.STATE["masks"] = data["masks"]
    ed.STATE["filters"] = data.get("filters", [])
    ed.normalise_filters(ed.STATE["filters"])
    for f in ed.walk_filters(ed.STATE["filters"]):
        if f.get("type") not in ed.FILTER_FN:
            raise SystemExit(f"preset uses unknown filter type {f.get('type')!r}")
    if data.get("world_noise") is not None:
        ed.STATE["world_noise"] = data["world_noise"]
    ed.STATE["next_filter"] = data.get("next_filter", 1)
    ed.STATE.setdefault("channel", "output")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("preset")
    ap.add_argument("--out", default=None)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="1 = one pixel per map tile")
    ap.add_argument("--strip", type=int, default=STRIP_H)
    ap.add_argument("--margin", type=int, default=MARGIN)
    ap.add_argument("--strips", action="store_true",
                    help="render in strips instead of one pass. Faster to "
                         "interrupt, but every filter is anchored to its crop, "
                         "so noise, erosion and drainage band at the seams")
    ap.add_argument("--min-free-mb", type=int, default=900)
    ap.add_argument("--dump-height", action="store_true",
                    help="also save the final height to "
                         "out/sea/height_full.npy")
    args = ap.parse_args()

    apply_preset(args.preset)
    ed.USE_CACHE = False        # one pass needs each node once
    free = ed.mem_available_mb()
    if free is not None and free < args.min_free_mb:
        raise SystemExit(f"only {free:.0f} MB free, need {args.min_free_mb} MB; "
                         f"close something and retry")
    land_full = ed.load_map()
    H, W = land_full.shape
    out_w, out_h = int(round(W * args.scale)), int(round(H * args.scale))

    # global distance, resampled to the working resolution
    full_path = os.path.join(WORK, "dist_full.npy")
    if os.path.exists(full_path):
        dist = np.load(full_path, mmap_mode="r")
        if dist.shape != (out_h, out_w):
            dist = sc.upscale(dist, (out_w, out_h))
        d_full = np.asarray(dist, dtype=np.float32)
        print("distance: dist_full.npy (exact, 1 per tile)", flush=True)
    else:
        d_half = np.load(os.path.join(WORK, "dist_half.npy"))
        d_full = sc.upscale(d_half, (out_w, out_h)).astype(np.float32)
        print("distance: dist_half.npy (2 tiles per value)", flush=True)
    if args.scale != 1.0:
        land_full = np.array(Image.fromarray(land_full).resize((out_w, out_h),
                                                               Image.NEAREST))

    def load_region(region, cancel=None):
        x0, y0 = int(region["x0"]), int(region["y0"])
        w, h = int(region["w"]), int(region["h"])
        y1, x1 = min(y0 + h, out_h), min(x0 + w, out_w)
        x0, y0 = max(0, x0), max(0, y0)
        return land_full[y0:y1, x0:x1], d_full[y0:y1, x0:x1]

    ed.load_region = load_region
    ed.STATE["region"] = {"x0": 0, "y0": 0, "w": out_w, "h": out_h, "scale": 1.0}

    if not args.strips:
        t0 = time.time()
        last = [t0]

        def step(i, total, node):
            now = time.time()
            print(f"  {node:10s} {i + 1}/{total} {now - last[0]:6.1f}s "
                  f"rss {_peak_mb():.0f} MB", flush=True)
            last[0] = now

        ctx = ed.evaluate(progress=step,
                          region={"x0": 0, "y0": 0, "w": out_w, "h": out_h,
                                  "scale": 1.0})
        canvas = ed.channel_image(ctx, "output")
        out = args.out or os.path.join(WORK, f"full_{args.preset}.png")
        Image.fromarray(canvas, "RGB").save(out)
        print("wrote", out, canvas.shape, f"in {time.time() - t0:.0f}s",
              f"peak rss {_peak_mb():.0f} MB")
        if args.dump_height:
            _save_height(HEIGHT_PATH, np.asarray(ctx["h"], dtype=np.float16))
        return

    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
    h_plane = (np.zeros((out_h, out_w), dtype=np.float16)
               if args.dump_height else None)
    total = (out_h + args.strip - 1) // args.strip
    t_all = time.time()
    for i, y in enumerate(range(0, out_h, args.strip)):
        y1 = min(y + args.strip, out_h)
        ry0 = max(0, y - args.margin)
        ry1 = min(out_h, y1 + args.margin)
        region = {"x0": 0, "y0": ry0, "w": out_w, "h": ry1 - ry0, "scale": 1.0}
        ed._cache.clear()
        ctx = ed.evaluate(region=region)
        rgb = ed.channel_image(ctx, "output")
        canvas[y:y1] = rgb[y - ry0:y - ry0 + (y1 - y)]
        if h_plane is not None:
            h16 = np.asarray(ctx["h"], dtype=np.float16)
            h_plane[y:y1] = h16[y - ry0:y - ry0 + (y1 - y)]
        print(f"strip {i + 1}/{total} rows {y}-{y1} "
              f"({time.time() - t_all:.0f}s)", flush=True)

    out = args.out or os.path.join(WORK, f"full_{args.preset}.png")
    Image.fromarray(canvas, "RGB").save(out)
    print("wrote", out, canvas.shape, f"in {time.time() - t_all:.0f}s",
          f"peak rss {_peak_mb():.0f} MB")
    if h_plane is not None:
        _save_height(HEIGHT_PATH, h_plane)


if __name__ == "__main__":
    main()
