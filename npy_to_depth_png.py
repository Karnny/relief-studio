#!/usr/bin/env python3
"""Convert a Marigold V2 prediction (.npy) into the 16-bit depth map relief.py expects.

Marigold V2's depth checkpoints emit affine-invariant *log depth* (larger = farther)
as float32 [H, W]. The relief builder wants white = near, so this inverts by default.

    python npy_to_depth_png.py predictions_npy/photo.npy -o depth.png [--black-point 0.30]
"""
from __future__ import annotations

import argparse

import numpy as np
from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("npy")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--max-side", type=int, default=0, help="optional downscale")
    ap.add_argument("--black-point", type=float, default=0.0,
                    help="clamp this fraction of the near range to 0 (background cut)")
    ap.add_argument("--no-invert", action="store_true",
                    help="keep the raw orientation (use for disparity outputs)")
    args = ap.parse_args()

    a = np.load(args.npy).astype(np.float32)
    if a.ndim == 3:                       # [3,H,W] or [H,W,3] safety
        a = a[0] if a.shape[0] == 3 else a[..., 0]
    if not np.isfinite(a).all():
        a = np.nan_to_num(a, nan=float(np.nanmedian(a)))

    lo, hi = np.percentile(a, (0.5, 99.5))
    d = np.clip((a - lo) / (hi - lo + 1e-8), 0, 1)
    if not args.no_invert:
        d = 1.0 - d
    if args.black_point > 0:
        t = float(args.black_point)
        d = np.clip((d - t) / (1.0 - t), 0.0, 1.0)

    img = Image.fromarray(d)
    if args.max_side and max(img.size) > args.max_side:
        s = args.max_side / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)), Image.BICUBIC)
    arr = np.asarray(img, np.float32)
    Image.fromarray((np.clip(arr, 0, 1) * 65535).astype(np.uint16)).save(args.out)
    print(f"wrote {args.out}  {arr.shape[1]}x{arr.shape[0]}  range {arr.min():.3f}-{arr.max():.3f}")


if __name__ == "__main__":
    main()
