#!/usr/bin/env python3
"""Local GUI for the relief generator — sliders, live 3D preview, STL/OBJ/GLB export.

    python3 relief_gui.py            # opens http://127.0.0.1:8765

The preview is computed by relief.py itself (low resolution while dragging,
full resolution on export), so what you see is exactly what you get.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import tempfile
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import relief

FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller one-file build
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE_DIR, "static")
TEMP_DIR = tempfile.gettempdir()

# Depth maps uploaded in this session: id -> float32 array (row 0 = top)
APP_ID = "relief-studio"
APP_VERSION = "1.0.0"
SERVER: "ThreadingHTTPServer | None" = None   # set in serve(), used by /api/quit
UPLOADS: dict[str, np.ndarray] = {}
SOURCES: dict[str, bytes] = {}          # original image bytes, for the preview texture
HAS_PNG = True

PREVIEW_MAX = 2048     # browser-side cap; export is not capped
EXPORT_MAX = 8192
FORMAT_MAX = {"stl": EXPORT_MAX, "obj": 2048, "glb": 2048}

DEFAULTS = {
    "width_mm": 60.0, "relief_height": 6.0, "base_thickness": 1.0,
    "subdivisions": 1024, "invert_depth": False, "curve": "linear",
    "remove_base": False, "remove_threshold": 0.01, "closed_back": True,
    "black_point": 0.0, "smooth": 0.0,
    "bend_mode": "off", "bend_axis": "xz", "bend_angle": 60.0,
}


def params_from_json(body: dict) -> relief.ReliefParams:
    d = dict(DEFAULTS)
    d.update({k: v for k, v in body.items() if k in DEFAULTS})
    return relief.ReliefParams(
        width_mm=float(d["width_mm"]), relief_height=float(d["relief_height"]),
        base_thickness=float(d["base_thickness"]),
        subdivisions=int(round(float(d["subdivisions"]))),
        invert_depth=bool(d["invert_depth"]), curve=str(d["curve"]),
        remove_base=bool(d["remove_base"]),
        remove_threshold=float(d["remove_threshold"]), black_point=float(d["black_point"]),
        smooth=float(d["smooth"]),
        closed_back=bool(d["closed_back"]), bend_mode=str(d["bend_mode"]),
        bend_axis=str(d["bend_axis"]), bend_angle=float(d["bend_angle"]),
    )


def pack_mesh(mesh: dict) -> bytes:
    """magic | json length | json header | float32 positions | uint32 indices."""
    pos = mesh["positions"].astype(np.float32)
    uv = mesh["uvs"].astype(np.float32)
    idx = mesh["index"].astype(np.uint32)
    head = {
        "vertices": int(len(pos)), "triangles": int(len(idx) // 3),
        "cols": mesh["cols"], "rows": mesh["rows"],
        "extents": [float(mesh["extents"][0]), float(mesh["extents"][1])],
        "floor_z": float(mesh["floor_z"]),
        "z_min": float(pos[:, 2].min()) if len(pos) else 0.0,
        "z_max": float(pos[:, 2].max()) if len(pos) else 0.0,
        "groups": mesh["groups"],
    }
    hj = json.dumps(head).encode()
    return (b"RLF1" + struct.pack("<I", len(hj)) + hj
            + pos.tobytes() + uv.tobytes() + idx.tobytes())


class Handler(BaseHTTPRequestHandler):
    server_version = "relief-gui"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---- helpers ---------------------------------------------------------
    def _send(self, code: int, ctype: str, payload: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, obj: dict):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n) if n else b""

    # ---- routes ----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        if path == "/api/hello":
            return self._json(200, {"app": APP_ID, "version": APP_VERSION})
        if path == "/api/defaults":
            return self._json(200, {"defaults": DEFAULTS,
                                    "images": [{"id": k, "width": int(v.shape[1]),
                                                "height": int(v.shape[0])}
                                               for k, v in UPLOADS.items()]})
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, "text/plain", b"not found")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".png": "image/png", ".ico": "image/x-icon"}.get(
                     os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, ctype, f.read())

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/upload":
                return self._upload()
            if path == "/api/preview":
                return self._preview()
            if path == "/api/export":
                return self._export()
            if path == "/api/source":
                return self._source()
            if path == "/api/hello":
                return self._json(200, {"app": APP_ID, "version": APP_VERSION})
            if path == "/api/quit":
                return self._quit()
            self._send(404, "text/plain", b"not found")
        except Exception as exc:                      # pragma: no cover
            import traceback
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    # ---- api -------------------------------------------------------------
    def _upload(self):
        raw = self._body()
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            depth = relief.depth_from_image(img)
            bit_depth = 16 if img.mode.startswith("I;16") or img.mode == "I" else 8
        except Exception as exc:
            return self._json(400, {"error": f"cannot read image: {exc}"})
        key = uuid.uuid4().hex[:10]
        UPLOADS[key] = depth
        SOURCES[key] = raw
        self._json(200, {"id": key, "width": int(depth.shape[1]), "height": int(depth.shape[0]),
                         "min": float(depth.min()), "max": float(depth.max()),
                         "depth_bits": bit_depth,
                         "median": float(np.median(depth)), "mean": float(depth.mean())})

    def _quit(self):
        self._json(200, {"stopping": True})
        srv = SERVER
        if srv is not None:
            threading.Thread(target=srv.shutdown, daemon=True).start()

    def _preview(self):
        body = json.loads(self._body() or b"{}")
        key = body.get("image_id")
        if key not in UPLOADS:
            return self._json(400, {"error": "no image"})
        p = params_from_json(body)
        limit = int(body.get("preview_subdivisions", 256))
        p.subdivisions = max(64, min(p.subdivisions, limit, PREVIEW_MAX))
        mesh = relief.build_relief(UPLOADS[key], p)
        if not len(mesh["index"]):
            return self._json(400, {"error": "empty mesh — check Remove Base / Carving Depth"})
        self._send(200, "application/octet-stream", pack_mesh(mesh))

    def _source(self):
        body = json.loads(self._body() or b"{}")
        key = body.get("image_id")
        if key not in SOURCES:
            return self._json(404, {"error": "no image"})
        self._send(200, "image/png", SOURCES[key])

    def _export(self):
        body = json.loads(self._body() or b"{}")
        fmt = str(body.get("format", "stl")).lower()
        key = body.get("image_id")
        if key not in UPLOADS:
            return self._json(400, {"error": "no image"})
        if fmt not in {"stl", "obj", "glb"}:
            return self._json(400, {"error": "unknown format"})
        p = params_from_json(body)
        p.subdivisions = max(64, min(p.subdivisions, EXPORT_MAX))
        if p.subdivisions > FORMAT_MAX[fmt]:
            return self._json(400, {"error":
                f"{fmt.upper()} is capped at {FORMAT_MAX[fmt]} px; use STL for larger grids"})
        mesh = relief.build_relief(UPLOADS[key], p)
        if not len(mesh["index"]):
            return self._json(400, {"error": "empty mesh"})
        out = os.path.join(TEMP_DIR, f"relief-{uuid.uuid4().hex[:8]}.{fmt}")
        if fmt == "stl":
            relief.write_stl(mesh, out)
        elif fmt == "obj":
            relief.write_obj(mesh, out)
        else:
            relief.write_glb(mesh, out)
        name = f"relief-{p.width_mm:g}mm-{p.subdivisions}px.{fmt}"
        size = os.path.getsize(out)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("X-Relief-Vertices", str(len(mesh["positions"])))
        self.send_header("X-Relief-Triangles", str(len(mesh["index"]) // 3))
        self.end_headers()
        try:
            with open(out, "rb") as f:
                while True:
                    block = f.read(4 << 20)
                    if not block:
                        break
                    self.wfile.write(block)
        finally:
            try:
                os.remove(out)
            except OSError:
                pass


def info(message: str) -> None:
    """stdout is None in a windowed Windows build; never let a log line crash us."""
    stream = sys.stdout
    if stream is not None:
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass


def report_fatal(exc: BaseException) -> None:
    """Frozen GUI builds have no console: leave a log and, on Windows, a message box."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    path = os.path.join(TEMP_DIR, "relief-studio-error.log")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        path = "(could not write log)"
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, f"{exc}\n\nDetails: {path}", "Relief Studio — startup failed", 0x10)
        except Exception:
            pass
    else:
        info(text)


def probe_port(port: int) -> str | None:
    """Return the base URL if a Relief Studio answers on this port, else None."""
    import json as _json
    import urllib.request

    base = f"http://127.0.0.1:{port}/"

    def fetch(path: str):
        try:
            with urllib.request.urlopen(base + path, timeout=0.3) as resp:
                return _json.loads(resp.read().decode())
        except Exception:
            return None

    # /api/hello identifies current builds; /api/defaults also exists on older
    # ones, so a previous release is still recognised as ours.
    body = fetch("api/hello") or fetch("api/defaults")
    if not isinstance(body, dict):
        return None
    if body.get("app") == APP_ID or ("defaults" in body and "images" in body):
        return base
    return None


def find_running(port: int) -> str | None:
    """Return the URL of an already-running Relief Studio, if there is one.

    Ports are probed concurrently. Sequential probing used to cost ~10 s on
    machines where the loopback firewall drops the SYN instead of refusing it
    (Windows with a security agent): 12 ports x 2 endpoints x 0.4 s timeout.
    """
    from concurrent.futures import ThreadPoolExecutor

    candidates = list(range(port, port + 12))
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        for found in pool.map(probe_port, candidates):
            if found is not None:
                return found
    return None


def serve(port: int, open_browser: bool) -> None:
    global SERVER
    running = find_running(port)
    if running is not None:
        info(f"already running at {running} - opening that instead of starting a second copy")
        if open_browser:
            webbrowser.open(running)
        return
    last: Exception | None = None
    for candidate in range(port, port + 12):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError as exc:                      # port in use -> try the next one
            last = exc
    else:
        raise last or OSError("no free port")
    SERVER = srv
    url = f"http://127.0.0.1:{candidate}/"
    info(f"relief GUI -> {url}   (Ctrl+C or the Quit button in the page to stop)")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        info("stopped")


def main():
    ap = argparse.ArgumentParser(description="Relief GUI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        serve(args.port, not args.no_browser)
    except BaseException as exc:                    # keep a windowed build diagnosable
        report_fatal(exc)
        raise


if __name__ == "__main__":
    main()
