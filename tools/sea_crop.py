"""Fast crop renderer for iterating on the sea texture.

    python3 tools/sea_crop.py     ->  out/sea/v2_crop.png   (~20-40 s)

Runs the exact pipeline from tools/sea_core.py on a 1550 x 1050 tile window,
so what you see here is what build_sea.py produces for the same area.
Edit the CROP box below to look elsewhere.
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

# window in map tiles, plus context so the distance field is correct at the edges
X0, Y0, X1, Y1 = 2800, 2150, 4350, 3200
M = 240

os.makedirs(WORK, exist_ok=True)
if not os.path.exists(MAP_PNG):
    print("downloading map raster…")
    urllib.request.urlretrieve(MAP_URL, MAP_PNG)

full = np.array(Image.open(MAP_PNG).convert("RGBA"))[:, :, 3] > 0
pad = np.zeros((full.shape[0] + 2 * M, full.shape[1] + 2 * M), dtype=bool)
pad[M:M + full.shape[0], M:M + full.shape[1]] = full
sub = pad[Y0:Y1 + M, X0:X1 + M]

print("distance…")
d_sub = sea_core.edt(sub)
d = d_sub[M:M + (Y1 - Y0), M:M + (X1 - X0)]
land = sub[M:M + (Y1 - Y0), M:M + (X1 - X0)]
del d_sub, sub, pad, full

print("rendering…")
rgb = sea_core.render(land, d)
Image.fromarray(rgb, "RGB").save(os.path.join(WORK, "v2_crop.png"))
print("wrote out/sea/v2_crop.png", rgb.shape[1], "x", rgb.shape[0])
