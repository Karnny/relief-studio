#!/usr/bin/env python3
"""Rebuild a the source tool-style relief plate from a depth map.

The geometry follows the source tool's client-side relief core (`/resource/relief/
extrude-worker.min.js`, `buildRelief` / `ze`): a bilinear-resampled
height field is lifted to `depth * reliefHeight` millimetres over a
`widthMM`-wide grid, then closed with side walls and a flat back.

Usage:
    relief.py depth.png -o plate.stl --width-mm 60 --relief-mm 19 \
        --resolution 2048 --base 0.1

    relief.py depth.png -o plate.glb --engrave --bend convex --bend-angle 60
"""
from __future__ import annotations

import argparse
import json
import math
import struct
from dataclasses import dataclass, asdict, field

import numpy as np
from PIL import Image


@dataclass
class ReliefParams:
    width_mm: float = 60.0          # the source tool "Size"          (20..250); exact plate width when height_mm is set
    height_mm: float = 0.0          # explicit plate height in mm (0 = follow the depth-map aspect)
    relief_height: float = 6.0      # "Carving Depth"       (0..60)
    base_thickness: float = 1.0     # "Base Thickness"      (0.1..10)
    subdivisions: int = 1024        # "Mesh Resolution"     (512..2048)
    invert_depth: bool = False      # "Engrave" instead of "Emboss"
    curve: str = "linear"           # linear | gamma | s-curve
    black_point: float = 0.0        # depths below this are treated as background (0)
    smooth: float = 0.0             # gaussian sigma in depth-map pixels (0 = off)
    remove_base: bool = False       # "Remove Base" (silhouette cut-out)
    remove_threshold: float = 0.01
    closed_back: bool = True        # always true for a printable plate
    bend_mode: str = "off"          # off | convex | concave
    bend_axis: str = "xz"           # xz | yz
    bend_angle: float = 60.0        # degrees (0..360)
    tilt_deg: float = 0.0           # apparent head pitch; + = chin up, - = chin down
    yaw_deg: float = 0.0            # apparent head turn; + = nose turns right in the image
    roll_deg: float = 0.0           # in-plane head roll; + = clockwise
    sculpt: list = field(default_factory=list)   # local raise/lower brush strokes


def tilt_depth(depth: np.ndarray, degrees: float) -> np.ndarray:
    """Pitch the head in the depth map (+ = chin up, - = chin down).

    The relief is a shallow height field, so rotating it in 3D barely moves the
    features (the depth axis is squashed). Instead this applies a 1-D projective
    warp to the rows - the classic "perspective / pitch" correction: the lower
    face expands, the upper face compresses, and the top/bottom edges stay put,
    so the face reads as looking further up (or down) without cropping.
    """
    if abs(degrees) < 0.05:
        return depth
    # positive = chin up: sample lower source rows into the centre, so the lower
    # face expands and the upper face compresses (photo perspective correction)
    k = -math.tan(math.radians(max(-45.0, min(45.0, degrees))))
    if abs(k) > 0.8:
        k = math.copysign(0.8, k)
    rows = depth.shape[0]
    v_out = np.linspace(0.0, 1.0, rows, dtype=np.float32)   # row 0 = top
    u_out = 1.0 - 2.0 * v_out                                # +1 top, -1 bottom
    u_src = (u_out + k) / (1.0 + k * u_out)                  # endpoints stay fixed
    src = (1.0 - u_src) * 0.5 * (rows - 1)
    src = np.clip(src, 0.0, rows - 1.0)
    i0 = np.floor(src).astype(np.int64)
    i1 = np.minimum(i0 + 1, rows - 1)
    frac = (src - i0).astype(np.float32)[:, None]
    warped = depth[i0] * (1.0 - frac) + depth[i1] * frac
    return warped.astype(np.float32)


def yaw_depth(depth: np.ndarray, degrees: float) -> np.ndarray:
    """Turn the head in the depth map (+ = nose turns toward the right of the image).

    Same trick as tilt_depth, but on the columns: a 1-D projective warp keeps the
    left/right edges fixed while expanding one cheek and compressing the other,
    which reads as the head being turned left/right.
    """
    if abs(degrees) < 0.05:
        return depth
    k = math.tan(math.radians(max(-45.0, min(45.0, degrees))))
    if abs(k) > 0.8:
        k = math.copysign(0.8, k)
    cols = depth.shape[1]
    x_out = np.linspace(-1.0, 1.0, cols, dtype=np.float32)   # -1 left, +1 right
    x_src = (x_out + k) / (1.0 + k * x_out)                  # endpoints stay fixed
    src = (x_src + 1.0) * 0.5 * (cols - 1)
    src = np.clip(src, 0.0, cols - 1.0)
    i0 = np.floor(src).astype(np.int64)
    i1 = np.minimum(i0 + 1, cols - 1)
    frac = (src - i0).astype(np.float32)[None, :]
    warped = depth[:, i0] * (1.0 - frac) + depth[:, i1] * frac
    return warped.astype(np.float32)


def roll_depth(depth: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate the head in the depth image (+ = clockwise), bilinear sampled.

    Rotating a rectangle exposes empty corners; outside samples are clamped to the
    nearest edge so a black background stays black. Processed in row chunks so an
    8192-grid depth map does not allocate full-frame coordinate arrays.
    """
    if abs(degrees) < 0.05:
        return depth
    theta = math.radians(max(-180.0, min(180.0, degrees)))
    ca, sa = math.cos(theta), math.sin(theta)
    rows, cols = depth.shape
    cx = (cols - 1) * 0.5
    cy = (rows - 1) * 0.5
    chunk = max(1, 4_000_000 // max(cols, 1))
    xs = np.arange(cols, dtype=np.float32) - cx
    out = np.empty_like(depth)
    for y0 in range(0, rows, chunk):
        y1 = min(rows, y0 + chunk)
        ys = np.arange(y0, y1, dtype=np.float32)[:, None] - cy
        dx = xs[None, :]
        # inverse rotation: source = R(-theta) * (dx, dy)
        sx = np.clip(dx * ca + ys * sa + cx, 0.0, cols - 1.0)
        sy = np.clip(-dx * sa + ys * ca + cy, 0.0, rows - 1.0)
        x0 = np.floor(sx).astype(np.int64)
        x1 = np.minimum(x0 + 1, cols - 1)
        r0 = np.floor(sy).astype(np.int64)
        r1 = np.minimum(r0 + 1, rows - 1)
        fx = (sx - x0).astype(np.float32)
        fy = (sy - r0).astype(np.float32)
        top = depth[r0, x0] * (1.0 - fx) + depth[r0, x1] * fx
        bot = depth[r1, x0] * (1.0 - fx) + depth[r1, x1] * fx
        out[y0:y1] = top * (1.0 - fy) + bot * fy
    return out.astype(np.float32)


def depth_from_image(img: "Image.Image") -> np.ndarray:
    """Depth image -> float32 [0,1] (row 0 = top).

    16-bit grayscale PNGs are common for depth maps; converting them through
    RGB clips everything above 255 to white and turns the plate into a binary
    mask, so decode those at full precision (the source tool does the same).
    """
    mode = img.mode
    if mode.startswith("I;16") or mode == "I":
        a = np.asarray(img).astype(np.float32)
        peak = float(a.max()) if a.size else 0.0
        return np.clip(a / (65535.0 if peak > 255 else 255.0), 0.0, 1.0).astype(np.float32)
    if mode == "F":
        a = np.asarray(img).astype(np.float32)
        peak = float(a.max()) if a.size else 0.0
        if peak <= 1.0 + 1e-6:
            return np.clip(a, 0.0, 1.0).astype(np.float32)
        return np.clip(a / (65535.0 if peak > 255 else 255.0), 0.0, 1.0).astype(np.float32)
    if mode in ("L", "LA"):
        a = np.asarray(img)
        if a.ndim == 3:
            a = a[..., 0]
        return (a.astype(np.float32) / 255.0).astype(np.float32)
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    return ((rgb[..., 0] + rgb[..., 1] + rgb[..., 2]) / 3.0 / 255.0).astype(np.float32)


def load_depth(path: str) -> np.ndarray:
    """Load a depth map file, preserving 16-bit precision when present."""
    return depth_from_image(Image.open(path))


def gaussian_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable gaussian blur (pure numpy, edge-clamped)."""
    if sigma <= 0:
        return a
    radius = max(1, int(round(3.0 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    k /= k.sum()

    def conv1(src: np.ndarray, axis: int) -> np.ndarray:
        pad = [(radius, radius) if i == axis else (0, 0) for i in range(src.ndim)]
        padded = np.pad(src, pad, mode="edge")
        out = np.zeros_like(src, dtype=np.float32)
        for i, w in enumerate(k):
            sl = [slice(None)] * src.ndim
            sl[axis] = slice(i, i + src.shape[axis])
            out += w * padded[tuple(sl)]
        return out

    return conv1(conv1(a.astype(np.float32), 0), 1)


def apply_depth_curve(depth: np.ndarray, invert: bool, curve: str,
                      black_point: float = 0.0) -> np.ndarray:
    if black_point > 0:
        depth = np.clip((depth - black_point) / (1.0 - black_point), 0.0, 1.0)
    d = 1.0 - depth if invert else depth
    if curve == "gamma":
        d = np.power(d, 2.2)
    elif curve == "s-curve":
        d = d * d * (3.0 - 2.0 * d)
    return d.astype(np.float32)


def apply_sculpt(depth: np.ndarray, strokes: list, width_mm: float, height_mm: float,
                 relief_height: float) -> np.ndarray:
    """Apply local raise/lower brush strokes to the depth map.

    Each stroke is ``{"radius_mm": r, "strength_mm": s, "points": [{"u": .., "v": ..}]}``
    with u/v in 0..1 (v = 0 at the top of the image). ``strength_mm`` is the
    maximum depth offset a single stroke may add at the brush centre (negative
    lowers); repeating the brush over an area builds it up further. The Gaussian
    falloff is sized in millimetres on the plate, so the brush feels the same at
    any plate size.

    The result is *not* clamped to the source depth range: raising a nose that is
    already at the top of the depth map must actually make it protrude instead of
    flattening against a 0..1 ceiling (the plate simply gets taller; the carving
    depth can be lowered afterwards to compensate). A per-stroke cap keeps one
    drag from stacking dozens of overlapping dabs into a spike.
    """
    if not strokes or not (width_mm > 0) or not (height_mm > 0) or not (relief_height > 0):
        return depth
    rows, cols = depth.shape
    if rows < 2 or cols < 2:
        return depth
    delta = np.zeros((rows, cols), dtype=np.float32)
    for stroke in strokes:
        if not isinstance(stroke, dict):
            continue
        try:
            radius_mm = float(stroke.get("radius_mm", 0.0))
            strength_mm = float(stroke.get("strength_mm", 0.0))
        except (TypeError, ValueError):
            continue
        if radius_mm <= 0.0 or strength_mm == 0.0 or not math.isfinite(strength_mm):
            continue
        points = stroke.get("points") or []
        if not isinstance(points, list):
            continue
        su = radius_mm / width_mm * (cols - 1)      # sigma in x pixels
        sv = radius_mm / height_mm * (rows - 1)     # sigma in y pixels
        if su <= 0.0 or sv <= 0.0:
            continue
        amp = strength_mm / relief_height
        cap = abs(amp)
        rx = max(1, int(math.ceil(3.2 * su)))
        ry = max(1, int(math.ceil(3.2 * sv)))
        stroke_delta = np.zeros((rows, cols), dtype=np.float32)
        for pt in points[:20000]:
            if not isinstance(pt, dict):
                continue
            try:
                u = float(pt.get("u"))
                v = float(pt.get("v"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(u) and math.isfinite(v)):
                continue
            cx = u * (cols - 1)
            cy = v * (rows - 1)
            x0 = max(0, int(math.floor(cx)) - rx)
            x1 = min(cols, int(math.ceil(cx)) + rx + 1)
            y0 = max(0, int(math.floor(cy)) - ry)
            y1 = min(rows, int(math.ceil(cy)) + ry + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            dx = (np.arange(x0, x1, dtype=np.float32) - cx) / su
            dy = (np.arange(y0, y1, dtype=np.float32) - cy) / sv
            falloff = np.exp(-0.5 * (dx[None, :] ** 2 + dy[:, None] ** 2))
            stroke_delta[y0:y1, x0:x1] += amp * falloff.astype(np.float32)
        if cap > 0.0:
            stroke_delta = np.clip(stroke_delta, -cap, cap)
        delta += stroke_delta
    if not delta.any():
        return depth
    return (depth + delta).astype(np.float32)


def bilinear_grid(depth: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """Sample the depth map at the grid nodes, exactly as the mesh builder does."""
    h, w = depth.shape
    xs = np.arange(cols, dtype=np.float32) / (cols - 1) * (w - 1)
    ys = np.arange(rows, dtype=np.float32) / (rows - 1) * (h - 1)
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (xs - x0)[None, :]
    fy = (ys - y0)[:, None]
    top = depth[np.ix_(y0, x0)] * (1 - fx) + depth[np.ix_(y0, x1)] * fx
    bot = depth[np.ix_(y1, x0)] * (1 - fx) + depth[np.ix_(y1, x1)] * fx
    return (top * (1 - fy) + bot * fy).astype(np.float32)


def bend(positions: np.ndarray, mode: str, axis: str, angle_deg: float, extent: float) -> None:
    """Cylindrically wrap the plate around the chosen axis (in place)."""
    if mode == "off" or angle_deg < 1.0:
        return
    a = 0 if axis == "xz" else 1          # 0 -> x column, 1 -> y column
    if angle_deg <= 0:
        return
    theta = angle_deg * math.pi / 180.0
    radius = extent / theta
    c = positions[:, a]
    z = positions[:, 2]
    phi = c / radius
    if mode == "convex":
        positions[:, a] = (radius + z) * np.sin(phi)
        positions[:, 2] = (radius + z) * np.cos(phi) - radius
    else:  # concave
        positions[:, a] = (radius - z) * np.sin(phi)
        positions[:, 2] = radius - (radius - z) * np.cos(phi)


def build_relief(depth: np.ndarray, p: ReliefParams) -> dict:
    """Return {positions, uvs, index, groups, extents, floor_z, counts}."""
    if p.tilt_deg:
        depth = tilt_depth(depth, p.tilt_deg)
    if p.yaw_deg:
        depth = yaw_depth(depth, p.yaw_deg)
    if p.roll_deg:
        depth = roll_depth(depth, p.roll_deg)
    h, w = depth.shape
    aspect = w / h

    if p.height_mm and p.height_mm > 0.0:
        # Photoshop-style transform: W and H are the actual plate dimensions, so
        # the depth map can be stretched wider or slimmer than its own aspect.
        width_mm = float(p.width_mm)
        height_mm = float(p.height_mm)
    else:
        # the source tool "Size": the long edge is width_mm and the other edge follows the
        # depth-map aspect (the original behaviour, still the default).
        width_mm = p.width_mm if aspect >= 1 else p.width_mm * aspect
        height_mm = p.width_mm / aspect if aspect >= 1 else p.width_mm

    if p.smooth > 0:
        depth = gaussian_blur(depth, p.smooth)
    depth = apply_depth_curve(depth, p.invert_depth, p.curve, p.black_point)
    if p.sculpt:
        depth = apply_sculpt(depth, p.sculpt, width_mm, height_mm, p.relief_height)

    subdiv = max(2, int(round(p.subdivisions)))
    cols = subdiv if aspect >= 1 else max(2, int(round(subdiv * aspect)))
    rows = subdiv if aspect < 1 else max(2, int(round(subdiv / aspect)))
    dx = width_mm / (cols - 1)
    dy = height_mm / (rows - 1)

    grid = bilinear_grid(depth, cols, rows)                 # [rows, cols]
    z = grid.astype(np.float32) * np.float32(p.relief_height)

    min_depth = float(depth.min()) if depth.size else 0.0
    if p.remove_base:
        floor_z = max(min_depth, p.remove_threshold) * p.relief_height
    else:
        floor_z = min_depth * p.relief_height - p.base_thickness

    if p.remove_base and not (p.relief_height > 0):
        return {"positions": np.zeros((0, 3), np.float32), "uvs": np.zeros((0, 2), np.float32),
                "index": np.zeros(0, np.uint32), "groups": [], "extents": (width_mm, height_mm),
                "floor_z": floor_z, "cols": cols, "rows": rows, "surface_quads": 0}

    z = np.maximum(z, np.float32(floor_z))

    # --- surface vertices, row-major (row 0 is the top of the image) --------
    u = np.arange(cols, dtype=np.float32) / (cols - 1)
    v = np.arange(rows, dtype=np.float32) / (rows - 1)
    uu, vv = np.meshgrid(u, v)
    verts = [np.stack([(uu - 0.5) * width_mm,
                       (0.5 - vv) * height_mm,
                       z], axis=-1).reshape(-1, 3).astype(np.float32)]
    uvs = [np.stack([uu, 1.0 - vv], axis=-1).reshape(-1, 2).astype(np.float32)]

    idx = (np.arange(rows - 1)[:, None] * cols + np.arange(cols - 1)[None, :]).ravel()
    tris = np.concatenate([
        np.stack([idx, idx + cols, idx + cols + 1], axis=1),
        np.stack([idx, idx + cols + 1, idx + 1], axis=1),
    ]).astype(np.uint32)
    n_surface = len(verts[0])
    indices = [tris.reshape(-1)]

    # --- side walls down to the floor --------------------------------------
    wall_verts = []
    wall_tris = []
    if p.closed_back:
        bottom = np.float32(floor_z)
        edge_pairs = []
        for c in range(cols - 1):                     # top edge, left -> right
            edge_pairs.append((c, c + 1))
        for r in range(rows - 1):                     # right edge, top -> bottom
            edge_pairs.append((r * cols + cols - 1, (r + 1) * cols + cols - 1))
        for c in range(cols - 1, 0, -1):              # bottom edge, right -> left
            edge_pairs.append(((rows - 1) * cols + c, (rows - 1) * cols + c - 1))
        for r in range(rows - 1, 0, -1):              # left edge, bottom -> top
            edge_pairs.append((r * cols, (r - 1) * cols))

        base = n_surface
        for (a, b) in edge_pairs:
            pa, pb = verts[0][a], verts[0][b]
            quad = np.array([
                [pa[0], pa[1], pa[2]], [pb[0], pb[1], pb[2]],
                [pb[0], pb[1], bottom], [pa[0], pa[1], bottom],
            ], dtype=np.float32)
            start = base + len(wall_verts)
            wall_verts.extend(quad)
            wall_tris.extend([[start, start + 1, start + 2], [start, start + 2, start + 3]])
        wall_verts = np.asarray(wall_verts, np.float32).reshape(-1, 3)
        wall_uvs = np.zeros((len(wall_verts), 2), np.float32)
        if len(wall_verts):
            verts.append(wall_verts)
            uvs.append(wall_uvs)
            indices.append(np.asarray(wall_tris, np.uint32).reshape(-1))

    # --- flat back --------------------------------------------------------
    # A bent plate uses a full back grid, because a single fan would fold
    # through the curvature; a flat plate uses a cheap fan from its centre.
    grid_back = p.closed_back and p.bend_mode != "off" and p.bend_angle >= 1.0
    if grid_back:
        n_before_cap = sum(len(x) for x in verts)
        back = np.stack([(uu - 0.5) * width_mm,
                         (0.5 - vv) * height_mm,
                         np.full_like(uu, np.float32(floor_z))], axis=-1).reshape(-1, 3).astype(np.float32)
        verts.append(back)
        uvs.append(np.zeros((len(back), 2), np.float32) + 0.5)
        gi = (np.arange(rows - 1)[:, None] * cols + np.arange(cols - 1)[None, :]).ravel() + n_before_cap
        back_tris = np.concatenate([
            np.stack([gi, gi + 1, gi + cols], axis=1),
            np.stack([gi, gi + cols + 1, gi + 1], axis=1),
        ]).astype(np.uint32)
        indices.append(back_tris.reshape(-1))
    elif p.closed_back:
        n_before_cap = sum(len(x) for x in verts)
        ring = []
        for c in range(cols):                          # top row
            ring.append(verts[0][c])
        for r in range(1, rows):                       # right column
            ring.append(verts[0][r * cols + cols - 1])
        for c in range(cols - 2, -1, -1):              # bottom row
            ring.append(verts[0][(rows - 1) * cols + c])
        for r in range(rows - 2, 0, -1):               # left column
            ring.append(verts[0][r * cols])
        ring_v = np.array([[q[0], q[1], floor_z] for q in ring], np.float32)
        centre = np.array([[0.0, 0.0, floor_z]], np.float32)
        cap = np.vstack([centre, ring_v])
        verts.append(cap)
        uvs.append(np.zeros((len(cap), 2), np.float32) + 0.5)
        ci = n_before_cap
        n = len(ring_v)
        cap_tris = np.array([[ci, ci + 1 + i, ci + 1 + ((i + 1) % n)] for i in range(n)], np.uint32)
        indices.append(cap_tris.reshape(-1))

    positions = np.vstack(verts).astype(np.float32)
    uv = np.vstack(uvs).astype(np.float32)
    index = np.concatenate(indices).astype(np.uint32)

    # --- groups: surface+walls first, back second --------------------------
    groups = []
    if len(index):
        surface_tris = len(tris.reshape(-1)) // 3
        groups.append({"start": 0, "count": surface_tris * 3, "material_index": 0})
        if len(index) // 3 > surface_tris:
            groups.append({"start": surface_tris * 3,
                           "count": len(index) - surface_tris * 3,
                           "material_index": 1})

    bend(positions, p.bend_mode, p.bend_axis, p.bend_angle,
         width_mm if p.bend_axis == "xz" else height_mm)

    return {"positions": positions, "uvs": uv, "index": index, "groups": groups,
            "extents": (width_mm, height_mm), "floor_z": floor_z,
            "cols": cols, "rows": rows, "surface_quads": (cols - 1) * (rows - 1)}


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------
def write_stl(mesh: dict, path: str, name: str = "relief", chunk: int = 1_500_000) -> None:
    """Binary STL, written in chunks so 8000+ grids do not need a huge copy."""
    pos, idx = mesh["positions"], mesh["index"].reshape(-1, 3)
    n = len(idx)
    header = f"REA relief {name}".encode()[:80].ljust(80, b" ")
    dtype = np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")])
    with open(path, "wb") as f:
        f.write(header)
        f.write(struct.pack("<I", n))
        for start in range(0, n, chunk):
            tri = pos[idx[start:start + chunk]]
            normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
            ln = np.linalg.norm(normals, axis=1, keepdims=True)
            normals = np.divide(normals, ln, out=np.zeros_like(normals), where=ln > 0)
            rec = np.zeros(len(tri), dtype=dtype)
            rec["n"] = normals.astype(np.float32)
            rec["v"] = tri.reshape(len(tri), 9).astype(np.float32)
            f.write(rec.tobytes())


def compute_normals(positions: np.ndarray, index: np.ndarray) -> np.ndarray:
    """Area-weighted vertex normals (same accumulation as the source tool's kernel)."""
    tri = index.reshape(-1, 3)
    p0, p1, p2 = positions[tri[:, 0]], positions[tri[:, 1]], positions[tri[:, 2]]
    fn = np.cross(p1 - p0, p2 - p0)
    n = np.zeros_like(positions)
    for k in range(3):
        np.add.at(n, tri[:, k], fn)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return np.divide(n, ln, out=np.zeros_like(n), where=ln > 0).astype(np.float32)


def write_glb(mesh: dict, path: str) -> None:
    """Minimal glTF 2.0 binary: one mesh, positions/normals/indices, no texture."""
    pos = mesh["positions"].astype(np.float32)
    nrm = compute_normals(pos, mesh["index"])
    idx = mesh["index"].reshape(-1).astype(np.uint32)
    pos_b, nrm_b, idx_b = pos.tobytes(), nrm.tobytes(), idx.tobytes()
    pad = lambda b: b + b"\0" * ((4 - len(b) % 4) % 4)
    pos_b, nrm_b, idx_b = pad(pos_b), pad(nrm_b), pad(idx_b)

    off_pos = 0
    off_nrm = len(pos_b)
    off_idx = off_nrm + len(nrm_b)
    bin_blob = pos_b + nrm_b + idx_b
    n_tris = len(idx) // 3

    doc = {
        "asset": {"version": "2.0", "generator": "relief.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "relief"}],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1},
            "indices": 2, "material": 0}], "name": "relief"}],
        "materials": [{"name": "relief", "doubleSided": True, "pbrMetallicRoughness": {
            "baseColorFactor": [0.86, 0.82, 0.76, 1.0],
            "metallicFactor": 0.0, "roughnessFactor": 0.65}}],
        "buffers": [{"byteLength": len(bin_blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": off_pos, "byteLength": len(pos_b), "target": 34962},
            {"buffer": 0, "byteOffset": off_nrm, "byteLength": len(nrm_b), "target": 34962},
            {"buffer": 0, "byteOffset": off_idx, "byteLength": len(idx_b), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
             "min": pos.min(0).tolist(), "max": pos.max(0).tolist()},
            {"bufferView": 1, "componentType": 5126, "count": len(nrm), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125, "count": len(idx), "type": "SCALAR"},
        ],
    }
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(bin_blob), 0x004E4942) + bin_blob)
    _ = n_tris


def write_obj(mesh: dict, path: str) -> None:
    pos, uv, idx = mesh["positions"], mesh["uvs"], mesh["index"].reshape(-1, 3)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# relief plate (the source tool-compatible relief parameters)\n")
        for p in pos:
            f.write(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n")
        for t in uv:
            f.write(f"vt {t[0]:.6f} {t[1]:.6f}\n")
        for a, b, c in idx:
            f.write(f"f {a + 1}/{a + 1} {b + 1}/{b + 1} {c + 1}/{c + 1}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Depth map -> relief mesh (the source tool-compatible settings)")
    ap.add_argument("depth", help="depth map PNG (white = high)")
    ap.add_argument("-o", "--out", required=True, help="output .stl, .obj or .glb")
    ap.add_argument("--width-mm", type=float, default=60.0,
                    help="plate width in mm; with --height-mm it is the exact width")
    ap.add_argument("--height-mm", type=float, default=0.0,
                    help="explicit plate height in mm (default: follow the depth-map aspect)")
    ap.add_argument("--relief-mm", type=float, default=6.0)
    ap.add_argument("--base", type=float, default=1.0)
    ap.add_argument("--resolution", type=int, default=1024)
    ap.add_argument("--engrave", action="store_true")
    ap.add_argument("--curve", choices=["linear", "gamma", "s-curve"], default="linear")
    ap.add_argument("--black-point", type=float, default=0.0,
                    help="treat depths below this (0-1) as background, like the source tool's depth map")
    ap.add_argument("--smooth", type=float, default=0.0,
                    help="gaussian blur sigma in depth-map pixels (2-3 matches the source tool's smoothness)")
    ap.add_argument("--remove-base", action="store_true")
    ap.add_argument("--remove-threshold", type=float, default=0.01)
    ap.add_argument("--bend", choices=["off", "convex", "concave"], default="off")
    ap.add_argument("--bend-axis", choices=["xz", "yz"], default="xz")
    ap.add_argument("--bend-angle", type=float, default=60.0)
    ap.add_argument("--tilt", type=float, default=0.0,
                    help="apparent head pitch in degrees (+ = chin up, - = chin down)")
    ap.add_argument("--yaw", type=float, default=0.0,
                    help="apparent head turn in degrees (+ = nose turns right in the image)")
    ap.add_argument("--roll", type=float, default=0.0,
                    help="in-plane head roll in degrees (+ = clockwise)")
    ap.add_argument("--sculpt", metavar="FILE",
                    help="JSON list of local sculpt strokes (radius_mm, strength_mm, points)")
    ap.add_argument("--stats", action="store_true", help="print mesh statistics as JSON")
    args = ap.parse_args()

    sculpt_strokes = []
    if args.sculpt:
        try:
            with open(args.sculpt, encoding="utf-8") as f:
                sculpt_strokes = json.load(f)
        except OSError as exc:
            raise SystemExit(f"cannot read --sculpt file: {exc}")
        if not isinstance(sculpt_strokes, list):
            raise SystemExit("--sculpt JSON must be a list of stroke objects")

    params = ReliefParams(
        width_mm=args.width_mm, height_mm=args.height_mm,
        relief_height=args.relief_mm, base_thickness=args.base,
        subdivisions=args.resolution, invert_depth=args.engrave, curve=args.curve,
        black_point=args.black_point, smooth=args.smooth,
        remove_base=args.remove_base, remove_threshold=args.remove_threshold,
        bend_mode=args.bend, bend_axis=args.bend_axis, bend_angle=args.bend_angle,
        tilt_deg=args.tilt, yaw_deg=args.yaw, roll_deg=args.roll,
        sculpt=sculpt_strokes,
    )
    mesh = build_relief(load_depth(args.depth), params)

    if args.out.lower().endswith(".stl"):
        write_stl(mesh, args.out)
    elif args.out.lower().endswith(".obj"):
        write_obj(mesh, args.out)
    elif args.out.lower().endswith(".glb"):
        write_glb(mesh, args.out)
    else:
        raise SystemExit("output must end in .stl, .obj or .glb")

    if args.stats:
        pos = mesh["positions"]
        print(json.dumps({
            "params": asdict(params), "grid": [mesh["cols"], mesh["rows"]],
            "vertices": int(len(pos)), "triangles": int(len(mesh["index"]) // 3),
            "extents_mm": [round(mesh["extents"][0], 4), round(mesh["extents"][1], 4)],
            "floor_z": round(float(mesh["floor_z"]), 4),
            "z_range": [round(float(pos[:, 2].min()), 4), round(float(pos[:, 2].max()), 4)],
        }, indent=2))


if __name__ == "__main__":
    main()
