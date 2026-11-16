#!/usr/bin/env python3
"""Photo -> 16-bit depth map, using Depth Anything V2 (Small, ONNX, Apache-2.0).

This is a free depth-estimation step: a monocular depth estimator turns a photo into a
depth estimator turns the photo into a height field, which relief.py then turns
into the printable plate.

    python3 photo_depth.py photo.jpg -o depth.png
    python3 photo_depth.py photo.jpg -o depth.png --model models/depth-anything-v2-small/onnx_model.onnx

Output is 16-bit grayscale, white = near (the convention the relief builder
expects for "emboss").
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
SIZE = 518
DIVISOR = 14


def load_model(path: str):
    import onnxruntime as ort

    providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    try:
        sess = ort.InferenceSession(path, providers=providers)
    except Exception:
        sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return sess


def prepare(img: Image.Image) -> tuple[np.ndarray, tuple[int, int]]:
    w, h = img.size
    scale = SIZE / min(w, h)
    nw = max(DIVISOR, int(round(w * scale / DIVISOR)) * DIVISOR)
    nh = max(DIVISOR, int(round(h * scale / DIVISOR)) * DIVISOR)
    small = img.convert("RGB").resize((nw, nh), Image.BICUBIC)
    x = np.asarray(small, dtype=np.float32) / 255.0
    x = (x - MEAN) / STD
    return x.transpose(2, 0, 1)[None].astype(np.float32), (w, h)


def infer(sess, tensor: np.ndarray) -> np.ndarray:
    name = sess.get_inputs()[0].name
    out = sess.run(None, {name: tensor})[0]
    depth = np.asarray(out, dtype=np.float32).reshape(out.shape[-2], out.shape[-1])
    return depth


def normalise(depth: np.ndarray, invert: bool = False) -> np.ndarray:
    lo, hi = float(depth.min()), float(depth.max())
    d = (depth - lo) / (hi - lo) if hi > lo else np.zeros_like(depth)
    d = 1.0 - d if invert else d          # DA V2 output: larger = nearer
    return np.clip(d, 0.0, 1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Photo -> 16-bit depth map (Depth Anything V2)")
    ap.add_argument("image")
    ap.add_argument("-o", "--out", required=True, help="output 16-bit PNG depth map")
    ap.add_argument("--model", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "models",
        "depth-anything-v2-small", "onnx_model.onnx"))
    ap.add_argument("--invert", action="store_true", help="near = dark instead of white")
    ap.add_argument("--smooth", type=float, default=0.0, help="optional blur radius in pixels")
    args = ap.parse_args()

    if not os.path.isfile(args.model):
        sys.exit(f"model not found: {args.model}\n"
                 "download it with:\n"
                 "  curl -L -o onnx_model.onnx https://huggingface.co/onnx-community/"
                 "depth-anything-v2-small/resolve/main/onnx/model.onnx")

    img = Image.open(args.image)
    tensor, (w, h) = prepare(img)
    sess = load_model(args.model)
    depth = infer(sess, tensor)
    d = normalise(depth, args.invert)

    # back to the original aspect/size
    d_img = Image.fromarray(d, mode="F").resize((w, h), Image.BICUBIC)
    if args.smooth > 0:
        from PIL import ImageFilter
        d_img = d_img.filter(ImageFilter.GaussianBlur(args.smooth))
    d = np.asarray(d_img, dtype=np.float32)

    out16 = (np.clip(d, 0, 1) * 65535.0).astype(np.uint16)
    Image.fromarray(out16).save(args.out)   # uint16 -> mode I;16 (Pillow 13 rejects the explicit mode)
    print(f"wrote {args.out}  ({w}x{h}, depth {d.min():.3f}-{d.max():.3f}, mean {d.mean():.3f})")


if __name__ == "__main__":
    main()
