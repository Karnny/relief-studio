#!/usr/bin/env python3
"""Compare a reference relief export with a locally rebuilt plate.

Both meshes are expected to come from the same depth map and settings, so
every surface vertex of the export should land on the rebuilt grid at the
same (x, y) with the same z.

    compare_mesh.py reference.stl rebuilt.stl
"""
from __future__ import annotations

import argparse
import struct
import sys

import numpy as np


def read_stl(path: str) -> np.ndarray:
    with open(path, "rb") as f:
        head = f.read(84)
        if len(head) < 84:
            raise SystemExit(f"{path}: too short for a binary STL")
        n = struct.unpack("<I", head[80:84])[0]
        data = f.read(n * 50)
    if len(data) != n * 50:
        raise SystemExit(f"{path}: truncated binary STL")
    rec = np.frombuffer(data, dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]))
    return rec["v"].reshape(-1, 3, 3).astype(np.float32)


def read_obj(path: str) -> np.ndarray:
    verts = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                verts.append([float(x) for x in line.split()[1:4]])
    return np.asarray(verts, np.float32).reshape(-1, 3)


def surface_points(tris: np.ndarray) -> np.ndarray:
    """Unique vertices of the mesh, deduplicated on a 1e-3 mm grid."""
    pts = tris.reshape(-1, 3)
    key = np.round(pts, 3)
    _, idx = np.unique(key, axis=0, return_index=True)
    return pts[idx]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference", help="reference export (.stl or .obj)")
    ap.add_argument("rebuilt", help="mesh from relief.py")
    ap.add_argument("--tolerance", type=float, default=0.05, help="mm; default 0.05")
    args = ap.parse_args()

    load = read_stl if args.reference.lower().endswith(".stl") else read_obj
    ref = surface_points(load(args.reference))
    load2 = read_stl if args.rebuilt.lower().endswith(".stl") else read_obj
    mine = surface_points(load2(args.rebuilt))

    # Index the rebuilt mesh by (x, y); a column can hold several heights
    # (surface plus the side wall that shares the same footprint).
    table: dict[tuple[float, float], list[float]] = {}
    for x, y, z in mine:
        table.setdefault((round(float(x), 3), round(float(y), 3)), []).append(float(z))

    matched, dev = 0, []
    for x, y, z in ref:
        k = (round(float(x), 3), round(float(y), 3))
        column = table.get(k)
        if column is not None:
            matched += 1
            dev.append(min(abs(float(z) - c) for c in column))

    dev = np.asarray(dev, dtype=np.float64) if dev else np.zeros(0)
    ref_keys = {(round(float(x), 3), round(float(y), 3)) for x, y, _ in ref}
    reverse = sum(1 for x, y, _ in mine if (round(float(x), 3), round(float(y), 3)) in ref_keys)
    total = len(ref)
    print(f"reference vertices : {total}")
    print(f"rebuilt vertices   : {len(mine)}")
    print(f"matched on (x, y)  : {matched} ({matched / max(total, 1):.1%})")
    print(f"reverse coverage   : {reverse / max(len(mine), 1):.1%} (low => rebuilt grid is finer)")
    if len(dev):
        print(f"z deviation  mean  : {dev.mean():.6f} mm")
        print(f"             p99   : {np.percentile(dev, 99):.6f} mm")
        print(f"             max   : {dev.max():.6f} mm")
        print(f"within {args.tolerance} mm : {(dev <= args.tolerance).mean():.2%}")
        if dev.max() > args.tolerance:
            print("=> mismatch: check widthMM, reliefHeight, engrave flag, base thickness,")
            print("   depth-map orientation (v flip) and whether the export used a bent back.")
            sys.exit(2)
        print("=> geometry matches the rebuilt plate within tolerance.")
    else:
        print("=> no vertices matched; grids differ (different resolution, size, or crop).")
        sys.exit(2)


if __name__ == "__main__":
    main()
