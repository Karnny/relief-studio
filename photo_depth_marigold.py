#!/usr/bin/env python3
"""Photo -> 16-bit depth map with Marigold (diffusion-based, fine detail).

    python photo_depth_marigold.py photo.jpg -o depth.png [--steps 4] [--ensemble 1]

Downloads prs-eth/marigold-depth-v1-1 (OpenRAIL++) on first run. Runs on Apple
Silicon MPS when available, otherwise CPU.
"""
from __future__ import annotations

import argparse
import time

import numpy as np
import torch
from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--steps", type=int, default=4, help="denoising steps (1-10)")
    ap.add_argument("--ensemble", type=int, default=1, help="ensemble size (paper uses 10)")
    ap.add_argument("--model", default="prs-eth/marigold-depth-v1-1")
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    from diffusers import MarigoldDepthPipeline

    device = "cpu" if args.cpu or not torch.backends.mps.is_available() else "mps"
    dtype = torch.float32 if device == "cpu" else torch.float16
    print(f"device={device} dtype={dtype} steps={args.steps} ensemble={args.ensemble}", flush=True)

    t0 = time.time()
    pipe = MarigoldDepthPipeline.from_pretrained(args.model, variant="fp16" if device == "mps" else None,
                                                torch_dtype=dtype)
    pipe = pipe.to(device)
    pipe.set_progress_bar_config(disable=True)
    print(f"model loaded in {time.time()-t0:.1f}s", flush=True)

    img = Image.open(args.image).convert("RGB")
    # Marigold works best at the model resolution; keep it moderate for speed.
    scale = 768 / max(img.size)
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.BICUBIC)

    t1 = time.time()
    with torch.no_grad():
        out = pipe(img, num_inference_steps=args.steps, ensemble_size=args.ensemble,
                   processing_resolution=768, match_input_resolution=True, output_type="np")
    print(f"inference {time.time()-t1:.1f}s", flush=True)

    depth = np.asarray(out.prediction, dtype=np.float32)
    d = depth.min(axis=(0, 1), keepdims=True), depth.max(axis=(0, 1), keepdims=True)
    d = (depth - d[0]) / (d[1] - d[0] + 1e-8)
    d = np.squeeze(d)

    # Marigold's prediction has larger values for farther surfaces; the relief
    # builder wants white = near, so flip it.
    d = 1.0 - d

    d_img = Image.fromarray(d, mode="F").resize(Image.open(args.image).size, Image.BICUBIC)
    d = np.asarray(d_img, dtype=np.float32)
    Image.fromarray((np.clip(d, 0, 1) * 65535).astype(np.uint16)).save(args.out)
    print(f"wrote {args.out}  range {d.min():.3f}-{d.max():.3f} mean {d.mean():.3f}")


if __name__ == "__main__":
    main()
