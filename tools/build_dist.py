#!/usr/bin/env python3
"""Build the exact full-resolution distance field for the map.

    python3 tools/build_dist.py [--down 1]

Writes out/sea/dist_full.npy (float32, one value per map tile). The editor and
sea_full prefer it when it exists; the old dist_half.npy stays as a fallback.
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sea_core as sc                                          # noqa: E402
import sea_editor as ed                                        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--down", type=int, default=1,
                    help="1 = exact at one value per tile (default)")
    ap.add_argument("--out", default=os.path.join(ed.WORK, "dist_full.npy"))
    args = ap.parse_args()

    land = ed.load_map()
    t = time.time()
    if args.down == 1:
        d = sc.edt_exact(land)
    else:
        d = sc.edt(land, down=args.down)
    np.save(args.out, d.astype(np.float32))
    print(f"wrote {args.out} {d.shape} max {d.max():.1f} tiles "
          f"in {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
