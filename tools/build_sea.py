"""Build the zig~zag sea texture (7749 x 5254, 1 px per map tile).

    python3 tools/build_sea.py        ->  dist/zigzag-sea.webp   (~4 min)

First run downloads the map raster and computes the distance field into
out/sea/ and reuses them afterwards. The pipeline itself lives in
tools/sea_core.py, shared with the crop renderer, so both always agree.
"""
import os
import urllib.request
import numpy as np
from PIL import Image

import sea_core

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK = os.path.join(ROOT, "out", "sea")
MAP_URL = "https://d79e46apek3bq.cloudfront.net/maps/64/2026-11-01T22-21-31Z/map.png"
MAP_PNG = os.path.join(WORK, "map.png")
DIST_NPY = os.path.join(WORK, "dist_half.npy")
W, H = sea_core.W_MAP, sea_core.H_MAP

os.makedirs(WORK, exist_ok=True)
if not os.path.exists(MAP_PNG):
    print("downloading map raster…")
    urllib.request.urlretrieve(MAP_URL, MAP_PNG)

src = Image.open(MAP_PNG).convert("RGBA")
land = np.array(src)[:, :, 3] > 0

if not os.path.exists(DIST_NPY):
    print("distance field…")
    np.save(DIST_NPY, sea_core.edt_small(land))

print("rendering…")
d = np.load(DIST_NPY)
if d.shape != (H, W):
    d = sea_core.upscale(d, (W, H))
rgb = sea_core.render_simple(land, d)

out_png = os.path.join(WORK, "sea64.png")
Image.fromarray(rgb, "RGB").save(out_png)
Image.fromarray(rgb, "RGB").save(os.path.join(ROOT, "dist", "zigzag-sea.webp"),
                                 quality=92, method=6)
print("wrote dist/zigzag-sea.webp")
