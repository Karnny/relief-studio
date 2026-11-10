#!/usr/bin/env python3
"""Decode a Marigold/Depth-Anything colormap visualisation back into a 16-bit depth map.

Useful because GPU-remote demos (e.g. the Marigold V2 HF Space) hand you a
colourised PNG/WebP instead of the raw .npy. This fits the image against the
common matplotlib colormaps, picks the closest, and inverts it to the
white = near convention relief.py expects.

    python decode_spectral_depth.py marigold-v2-generated.webp -o depth.png --smooth 1.5
"""
from __future__ import annotations

import argparse
import json

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

CANDIDATES = ["Spectral_r", "Spectral", "RdYlBu_r", "RdYlBu", "turbo", "jet", "viridis",
              "plasma", "inferno", "magma", "cividis", "coolwarm", "seismic"]


def pick_colormap(img: np.ndarray) -> tuple[str, float]:
    import matplotlib

    flat = img.reshape(-1, 3)
    rng = np.random.default_rng(0)
    sample = flat[rng.choice(len(flat), min(20000, len(flat)), replace=False)]
    best = (None, 9.9)
    for name in CANDIDATES:
        try:
            lut = matplotlib.colormaps[name](np.linspace(0, 1, 512))[:, :3].astype(np.float32)
        except KeyError:
            continue
        d = ((sample[:, None, :] - lut[None, :, :]) ** 2).sum(-1)
        err = float(np.sqrt(d.min(1)).mean())
        if err < best[1]:
            best = (name, err)
    return best


def decode(img: np.ndarray, name: str) -> np.ndarray:
    import matplotlib

    lut = matplotlib.colormaps[name](np.linspace(0, 1, 1024))[:, :3].astype(np.float32)
    tree = cKDTree(lut)
    flat = img.reshape(-1, 3)
    t = np.empty(len(flat), np.float32)
    for i in range(0, len(flat), 500_000):
        _, idx = tree.query(flat[i:i + 500_000], workers=-1)
        t[i:i + 500_000] = idx / (len(lut) - 1)
    return t.reshape(img.shape[:2])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--colormap", default="auto")
    ap.add_argument("--smooth", type=float, default=1.2, help="gaussian sigma in px (0 = off)")
    ap.add_argument("--invert", dest="invert", action="store_true", default=None,
                    help="force the near=white flip on")
    ap.add_argument("--no-invert", dest="invert", action="store_false",
                    help="force it off (visualisation already near=white)")
    ap.add_argument("--max-side", type=int, default=0)
    args = ap.parse_args()

    rgb = np.asarray(Image.open(args.image).convert("RGB"), np.float32) / 255.0
    if args.colormap == "auto":
        name, err = pick_colormap(rgb)
        if name is None:
            raise SystemExit("could not match a known colormap")
        print(f"colormap: {name} (mean colour error {err:.4f})")
    else:
        name = args.colormap

    t = decode(rgb, name)
    if args.smooth > 0:
        t = gaussian_filter(t, args.smooth)

    # Most demos paint near surfaces warm. In a reversed colormap ("*_r") that is
    # the high end of t, so no flip is needed; in a base colormap it is t=0.
    flip = args.invert if args.invert is not None else (not name.endswith("_r"))
    d = (1.0 - t) if flip else t
    print(f"orientation: {'flipped' if flip else 'as-is'}")
    d = np.clip((d - d.min()) / (d.max() - d.min() + 1e-8), 0, 1)

    out = Image.fromarray(d.astype(np.float32))
    if args.max_side and max(out.size) > args.max_side:
        s = args.max_side / max(out.size)
        out = out.resize((int(out.width * s), int(out.height * s)), Image.BICUBIC)
    arr = np.asarray(out, np.float32)
    Image.fromarray((arr * 65535).astype(np.uint16)).save(args.out)
    print(f"wrote {args.out}  {arr.shape[1]}x{arr.shape[0]}  range {arr.min():.3f}-{arr.max():.3f}")


if __name__ == "__main__":
    main()
