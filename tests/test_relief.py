#!/usr/bin/env python3
"""Regression tests for the relief kernel and the local GUI server.

Run from the repository root:

    python -m unittest discover -s tests -v

The STL canary is the important one: the same depth map and settings must
produce a byte-identical STL on every platform/build (macOS arm64, Windows
arm64, Windows x64). That hash is what proved the port is deterministic.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np
from PIL import Image

import relief
import relief_gui

DEMO = os.path.join(ROOT, "demo-depth.png")

# demo-depth.png, 60 mm wide, 19 mm carving depth, 0.1 mm base, 512 grid.
CANARY_PARAMS = relief.ReliefParams(
    width_mm=60.0, relief_height=19.0, base_thickness=0.1, subdivisions=512,
)
CANARY_VERTICES = 272_365
CANARY_TRIANGLES = 528_374
CANARY_STL_BYTES = 26_418_784
CANARY_SHA256_PREFIX = "130d1f33"


def parse_mesh_head(payload: bytes) -> dict:
    """RLF1 payload -> the JSON header the GUI viewport reads."""
    import struct as _struct
    assert payload[:4] == b"RLF1", payload[:4]
    n = _struct.unpack("<I", payload[4:8])[0]
    return json.loads(payload[8:8 + n])


class CanaryTest(unittest.TestCase):
    """The cross-platform canary documented in HANDOFF.md."""

    @classmethod
    def setUpClass(cls):
        cls.depth = relief.load_depth(DEMO)
        cls.mesh = relief.build_relief(cls.depth, CANARY_PARAMS)

    def test_grid_and_topology(self):
        self.assertEqual(self.mesh["cols"], 512)
        self.assertEqual(self.mesh["rows"], 512)
        self.assertEqual(len(self.mesh["positions"]), CANARY_VERTICES)
        self.assertEqual(len(self.mesh["index"]) // 3, CANARY_TRIANGLES)

    def test_plate_extents(self):
        self.assertAlmostEqual(self.mesh["extents"][0], 60.0, places=4)
        self.assertAlmostEqual(self.mesh["extents"][1], 60.0, places=4)
        self.assertAlmostEqual(self.mesh["floor_z"], -0.1, places=6)

    def test_stl_is_byte_identical(self):
        """Guards the 'byte-identical across platforms' claim (hash canary)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "canary.stl")
            relief.write_stl(self.mesh, out)
            self.assertEqual(os.path.getsize(out), CANARY_STL_BYTES)
            with open(out, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        self.assertTrue(
            digest.startswith(CANARY_SHA256_PREFIX),
            f"STL canary drift: {digest} (expected prefix {CANARY_SHA256_PREFIX})",
        )


class PlateSizeTest(unittest.TestCase):
    """Photoshop-style W x H: stretch a plate away from the depth map aspect."""

    def _depth(self, h=50, w=100):
        # 2:1 landscape depth map with some structure
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        return (0.5 + 0.5 * np.sin(x / 8.0) * np.cos(y / 6.0)).astype(np.float32)

    def test_auto_aspect_when_height_is_unset(self):
        mesh = relief.build_relief(self._depth(), relief.ReliefParams(
            width_mm=60.0, relief_height=6.0, subdivisions=64))
        self.assertAlmostEqual(mesh["extents"][0], 60.0, places=4)
        self.assertAlmostEqual(mesh["extents"][1], 30.0, places=4)

    def test_explicit_height_stretches_the_plate(self):
        mesh = relief.build_relief(self._depth(), relief.ReliefParams(
            width_mm=90.0, height_mm=20.0, relief_height=6.0, subdivisions=64))
        self.assertAlmostEqual(mesh["extents"][0], 90.0, places=4)
        self.assertAlmostEqual(mesh["extents"][1], 20.0, places=4)
        # same grid (depth-map aspect), only the geometry is scaled
        self.assertEqual((mesh["cols"], mesh["rows"]), (64, 32))
        y = mesh["positions"][:, 1]
        self.assertAlmostEqual(float(y.min()), -10.0, places=4)
        self.assertAlmostEqual(float(y.max()), 10.0, places=4)

    def test_default_params_keep_the source tool_behaviour(self):
        params = relief.ReliefParams()
        self.assertEqual(params.height_mm, 0.0)
        self.assertEqual(relief_gui.DEFAULTS["height_mm"], 0.0)

    def test_params_from_json_parses_height(self):
        p = relief_gui.params_from_json({"width_mm": 70, "height_mm": 35})
        self.assertEqual(p.width_mm, 70.0)
        self.assertEqual(p.height_mm, 35.0)
        p2 = relief_gui.params_from_json({"width_mm": 70, "height_mm": -5})
        self.assertEqual(p2.height_mm, 0.0)          # negative -> auto


class TiltTest(unittest.TestCase):
    """Face tilt: + degrees = chin up, - = chin down (1-D projective row warp)."""

    def _ramp(self, rows=101, cols=8):
        return np.repeat((np.arange(rows, dtype=np.float32) / (rows - 1))[:, None], cols, axis=1)

    def test_zero_tilt_is_identity(self):
        depth = self._ramp()
        np.testing.assert_array_equal(relief.tilt_depth(depth, 0.0), depth)

    def test_top_and_bottom_edges_do_not_move(self):
        depth = self._ramp()
        for deg in (-30, -20, 20, 30):
            out = relief.tilt_depth(depth, deg)
            np.testing.assert_array_equal(out[0], depth[0])
            np.testing.assert_array_equal(out[-1], depth[-1])

    def test_positive_tilt_samples_lower_rows_at_the_centre(self):
        depth = self._ramp()
        mid = depth.shape[0] // 2
        up = relief.tilt_depth(depth, 20)
        down = relief.tilt_depth(depth, -20)
        # + = chin up: centre output samples a source row below centre (lower face
        # moved up), - = chin down: samples above centre.
        self.assertGreater(float(up[mid, 0]), float(depth[mid, 0]))
        self.assertLess(float(down[mid, 0]), float(depth[mid, 0]))

    def test_tilt_changes_geometry_but_not_the_grid(self):
        y, x = np.mgrid[0:80, 0:60].astype(np.float32)
        depth = (0.5 + 0.5 * np.sin(x / 7.0) * np.cos(y / 9.0)).astype(np.float32)
        base = relief.ReliefParams(width_mm=60, relief_height=6, subdivisions=64)
        tilted = relief.ReliefParams(width_mm=60, relief_height=6, subdivisions=64,
                                     tilt_deg=25)
        m0 = relief.build_relief(depth, base)
        m1 = relief.build_relief(depth, tilted)
        self.assertEqual((m1["cols"], m1["rows"]), (m0["cols"], m0["rows"]))
        self.assertEqual(len(m1["positions"]), len(m0["positions"]))
        self.assertEqual(m1["extents"], m0["extents"])
        self.assertFalse(np.allclose(m1["positions"], m0["positions"]))
        self.assertFalse(np.isnan(m1["positions"]).any())

    def test_params_from_json_parses_tilt(self):
        p = relief_gui.params_from_json({"tilt_deg": -12})
        self.assertEqual(p.tilt_deg, -12.0)
        self.assertEqual(relief_gui.DEFAULTS["tilt_deg"], 0.0)


class YawRollTest(unittest.TestCase):
    """Head turn (yaw, column warp) and head roll (2-D rotation)."""

    def _columns(self, rows=11, cols=101):
        return np.repeat(np.linspace(0, 1, cols, dtype=np.float32)[None, :], rows, axis=0)

    def test_zero_is_identity(self):
        depth = self._columns()
        np.testing.assert_array_equal(relief.yaw_depth(depth, 0.0), depth)
        np.testing.assert_array_equal(relief.roll_depth(depth, 0.0), depth)

    def test_yaw_keeps_the_edge_columns(self):
        depth = self._columns()
        for deg in (-25, 25):
            out = relief.yaw_depth(depth, deg)
            np.testing.assert_array_equal(out[:, 0], depth[:, 0])
            np.testing.assert_array_equal(out[:, -1], depth[:, -1])

    def test_yaw_direction(self):
        depth = self._columns()           # dark left -> bright right
        mid = depth.shape[1] // 2
        left = relief.yaw_depth(depth, 20)
        right = relief.yaw_depth(depth, -20)
        # + = toward the viewer's left: the centre samples source pixels to the right
        self.assertGreater(float(left[0, mid]), float(depth[0, mid]))
        self.assertLess(float(right[0, mid]), float(depth[0, mid]))

    def test_roll_is_clockwise_and_keeps_the_centre(self):
        rows, cols = 101, 101
        depth = np.zeros((rows, cols), np.float32)
        depth[50, 80] = 1.0               # a mark right of the centre
        out = relief.roll_depth(depth, 90)
        # + = clockwise: the mark swings from the right edge to the bottom
        self.assertEqual(np.unravel_index(int(np.argmax(out)), out.shape), (80, 50))

    def test_roll_180_maps_right_to_left(self):
        depth = np.zeros((101, 101), np.float32)
        depth[50, 80] = 1.0
        out = relief.roll_depth(depth, 180)
        self.assertEqual(np.unravel_index(int(np.argmax(out)), out.shape), (50, 20))

    def test_combined_warps_keep_the_grid(self):
        y, x = np.mgrid[0:80, 0:60].astype(np.float32)
        depth = (0.5 + 0.5 * np.sin(x / 7.0) * np.cos(y / 9.0)).astype(np.float32)
        base = relief.ReliefParams(width_mm=60, relief_height=6, subdivisions=64)
        warped = relief.ReliefParams(width_mm=60, relief_height=6, subdivisions=64,
                                     tilt_deg=10, yaw_deg=15, roll_deg=-8)
        m0 = relief.build_relief(depth, base)
        m1 = relief.build_relief(depth, warped)
        self.assertEqual((m1["cols"], m1["rows"]), (m0["cols"], m0["rows"]))
        self.assertEqual(len(m1["positions"]), len(m0["positions"]))
        self.assertEqual(m1["extents"], m0["extents"])
        self.assertFalse(np.allclose(m1["positions"], m0["positions"]))
        self.assertFalse(np.isnan(m1["positions"]).any())

    def test_params_from_json_parses_yaw_and_roll(self):
        p = relief_gui.params_from_json({"yaw_deg": 12, "roll_deg": -7})
        self.assertEqual(p.yaw_deg, 12.0)
        self.assertEqual(p.roll_deg, -7.0)
        self.assertEqual(relief_gui.DEFAULTS["yaw_deg"], 0.0)
        self.assertEqual(relief_gui.DEFAULTS["roll_deg"], 0.0)


class SculptTest(unittest.TestCase):
    """Local raise/lower brush strokes applied to the depth map."""

    def _depth(self, rows=61, cols=81):
        return np.full((rows, cols), 0.5, np.float32)

    def _stroke(self, u=0.5, v=0.5, radius=8, strength=1.2, n=1):
        return {"radius_mm": radius, "strength_mm": strength,
                "points": [{"u": u, "v": v}] * n}

    def test_empty_strokes_are_a_no_op(self):
        depth = self._depth()
        np.testing.assert_array_equal(
            relief.apply_sculpt(depth, [], 60.0, 60.0, 6.0), depth)

    def test_raise_and_lower_at_the_centre(self):
        depth = self._depth()
        up = relief.apply_sculpt(depth, [self._stroke(strength=1.2)], 60, 60, 6)
        down = relief.apply_sculpt(depth, [self._stroke(strength=-1.2)], 60, 60, 6)
        mid = (depth.shape[0] // 2, depth.shape[1] // 2)
        self.assertGreater(float(up[mid]), float(depth[mid]))
        self.assertLess(float(down[mid]), float(depth[mid]))
        self.assertAlmostEqual(float(up[mid]), 0.5 + 1.2 / 6.0, places=3)

    def test_brush_is_local(self):
        depth = self._depth()
        out = relief.apply_sculpt(depth, [self._stroke(u=0.5, v=0.5)], 60, 60, 6)
        self.assertAlmostEqual(float(out[2, 2]), 0.5, places=6)
        self.assertGreater(float(out[depth.shape[0] // 2, depth.shape[1] // 2]), 0.5)

    def test_several_points_and_strokes_accumulate(self):
        depth = self._depth()
        one = relief.apply_sculpt(depth, [self._stroke(strength=0.6)], 60, 60, 6)
        two = relief.apply_sculpt(depth, [self._stroke(strength=0.6),
                                          self._stroke(strength=0.6)], 60, 60, 6)
        mid = (depth.shape[0] // 2, depth.shape[1] // 2)
        self.assertAlmostEqual(float(two[mid] - 0.5), 2 * float(one[mid] - 0.5), places=3)

    def test_result_is_clamped_to_valid_depth(self):
        depth = np.ones((31, 31), np.float32)
        out = relief.apply_sculpt(depth, [self._stroke(strength=5.0)], 60, 60, 6)
        self.assertLessEqual(float(out.max()), 1.0)
        low = np.zeros((31, 31), np.float32)
        out2 = relief.apply_sculpt(low, [self._stroke(strength=-5.0)], 60, 60, 6)
        self.assertGreaterEqual(float(out2.min()), 0.0)

    def test_build_relief_applies_sculpt_but_keeps_the_grid(self):
        y, x = np.mgrid[0:80, 0:60].astype(np.float32)
        depth = (0.5 + 0.5 * np.sin(x / 7.0) * np.cos(y / 9.0)).astype(np.float32)
        base = relief.ReliefParams(width_mm=60, relief_height=6, subdivisions=64)
        sculpted = relief.ReliefParams(width_mm=60, relief_height=6, subdivisions=64,
                                       sculpt=[self._stroke(u=0.5, v=0.35, strength=2.0)])
        m0 = relief.build_relief(depth, base)
        m1 = relief.build_relief(depth, sculpted)
        self.assertEqual((m1["cols"], m1["rows"]), (m0["cols"], m0["rows"]))
        self.assertEqual(len(m1["positions"]), len(m0["positions"]))
        self.assertEqual(m1["extents"], m0["extents"])
        self.assertFalse(np.allclose(m1["positions"], m0["positions"]))
        self.assertFalse(np.isnan(m1["positions"]).any())

    def test_params_from_json_sanitizes_sculpt(self):
        p = relief_gui.params_from_json({"sculpt": [
            {"radius_mm": 5, "strength_mm": 1, "points": [{"u": 0.2, "v": 0.3}]},
            {"radius_mm": 0, "strength_mm": 1, "points": [{"u": 0.1, "v": 0.1}]},
            {"radius_mm": 5, "strength_mm": 0, "points": [{"u": 0.1, "v": 0.1}]},
            {"radius_mm": 5, "strength_mm": 1, "points": []},
            {"radius_mm": 5, "strength_mm": 1,
             "points": [{"u": -3, "v": 9}, {"u": "x", "v": 0.5}]},
            "not a stroke",
        ]})
        self.assertEqual(len(p.sculpt), 2)
        self.assertEqual(p.sculpt[0]["points"][0], {"u": 0.2, "v": 0.3})
        self.assertEqual(p.sculpt[1]["points"][0], {"u": 0.0, "v": 1.0})
        self.assertEqual(relief_gui.DEFAULTS["sculpt"], [])


class DepthDecodeTest(unittest.TestCase):
    def test_16bit_png_keeps_full_precision(self):
        """The old .convert('RGB') bug clipped everything above 255 to white."""
        import io

        values = (np.arange(256, dtype=np.uint16) * 256)[None, :]  # 0..65280
        # Pillow infers I;16 from the uint16 array; the explicit mode= argument
        # is rejected by Pillow 13 for this dtype.
        buf = io.BytesIO()
        Image.fromarray(values).save(buf, format="PNG")
        buf.seek(0)
        img = Image.open(buf)
        self.assertEqual(img.mode, "I;16")
        depth = relief.depth_from_image(img)
        self.assertLess(float(depth.min()), 0.01)
        self.assertGreater(float(depth.max()), 0.99)
        mid = depth[0, 128]                      # value 32768 -> ~0.5
        self.assertAlmostEqual(float(mid), 32768 / 65535, places=5)
        # A value of 300 must stay clearly above zero (it would clip to 1.0 via RGB,
        # and quantise to 1/255 via plain 8-bit conversion).
        self.assertGreater(float(depth[0, 1]), 255.0 / 65535)

    def test_8bit_luma_decode(self):
        arr = np.array([[0, 128, 255]], dtype=np.uint8)
        depth = relief.depth_from_image(Image.fromarray(arr, mode="L"))
        np.testing.assert_allclose(depth[0], [0.0, 128 / 255, 1.0], rtol=0, atol=1e-6)


class ServerTest(unittest.TestCase):
    """The handlers the browser and the single-instance probe rely on."""

    @classmethod
    def setUpClass(cls):
        cls.srv = relief_gui.ReliefServer(("127.0.0.1", 0), relief_gui.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def test_hello_on_get_and_post(self):
        import json
        import urllib.request

        with urllib.request.urlopen(self._url("api/hello"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode())
        self.assertEqual(body["app"], relief_gui.APP_ID)
        req = urllib.request.Request(self._url("api/hello"), data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read().decode())["version"],
                             relief_gui.APP_VERSION)

    def test_defaults_endpoint(self):
        import json
        import urllib.request

        with urllib.request.urlopen(self._url("api/defaults"), timeout=5) as resp:
            body = json.loads(resp.read().decode())
        self.assertIn("defaults", body)
        self.assertIn("images", body)
        self.assertIn("height_mm", body["defaults"])

    def test_favicon_is_quiet(self):
        import urllib.request

        with urllib.request.urlopen(self._url("favicon.ico"), timeout=5) as resp:
            self.assertEqual(resp.status, 204)     # no 404 noise in the browser console

    def _upload_bytes(self, data):
        import json
        import urllib.request

        req = urllib.request.Request(self._url("api/upload"), data=data, method="POST",
                                     headers={"Content-Type": "image/png"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["id"]

    def _upload_demo(self):
        with open(os.path.join(ROOT, "demo-depth.png"), "rb") as f:
            return self._upload_bytes(f.read())

    def _preview_head_for(self, image_id, **params):
        import json
        import urllib.request

        body = json.dumps({"image_id": image_id, "subdivisions": 128,
                           "preview_subdivisions": 128, **params}).encode()
        req = urllib.request.Request(self._url("api/preview"), data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return parse_mesh_head(resp.read())

    def _preview_head(self, **params):
        return self._preview_head_for(self._upload_demo(), **params)

    def test_preview_auto_plate_size(self):
        head = self._preview_head(width_mm=60)
        self.assertAlmostEqual(head["extents"][0], 60.0, places=3)
        self.assertAlmostEqual(head["extents"][1], 60.0, places=3)   # demo is square

    def test_preview_auto_plate_size_follows_a_non_square_depth_map(self):
        import io as _io

        from PIL import Image as _Image

        arr = np.tile(np.linspace(0, 255, 100, dtype=np.uint8), (50, 1))
        buf = _io.BytesIO()
        _Image.fromarray(arr).save(buf, format="PNG")
        image_id = self._upload_bytes(buf.getvalue())
        auto = self._preview_head_for(image_id, width_mm=60)
        self.assertAlmostEqual(auto["extents"][0], 60.0, places=3)
        self.assertAlmostEqual(auto["extents"][1], 30.0, places=3)   # 2:1 depth map
        stretched = self._preview_head_for(image_id, width_mm=90, height_mm=20)
        self.assertAlmostEqual(stretched["extents"][0], 90.0, places=3)
        self.assertAlmostEqual(stretched["extents"][1], 20.0, places=3)

    def test_preview_honours_explicit_plate_size(self):
        head = self._preview_head(width_mm=80, height_mm=40)
        self.assertAlmostEqual(head["extents"][0], 80.0, places=3)
        self.assertAlmostEqual(head["extents"][1], 40.0, places=3)

    def test_preview_accepts_face_tilt(self):
        upright = self._preview_head(width_mm=60)
        tilted = self._preview_head(width_mm=60, tilt_deg=25)
        self.assertAlmostEqual(tilted["extents"][0], upright["extents"][0], places=3)
        self.assertAlmostEqual(tilted["extents"][1], upright["extents"][1], places=3)
        self.assertEqual(tilted["vertices"], upright["vertices"])

    def test_preview_accepts_face_turn_and_roll(self):
        upright = self._preview_head(width_mm=60)
        turned = self._preview_head(width_mm=60, yaw_deg=18)
        rolled = self._preview_head(width_mm=60, roll_deg=-12)
        for head in (turned, rolled):
            self.assertAlmostEqual(head["extents"][0], upright["extents"][0], places=3)
            self.assertAlmostEqual(head["extents"][1], upright["extents"][1], places=3)
            self.assertEqual(head["vertices"], upright["vertices"])

    def test_preview_accepts_sculpt_strokes(self):
        stroke = {"radius_mm": 8, "strength_mm": 1.5,
                  "points": [{"u": 0.5, "v": 0.4}, {"u": 0.52, "v": 0.42}]}
        plain = self._preview_head(width_mm=60)
        sculpted = self._preview_head(width_mm=60, sculpt=[stroke])
        self.assertAlmostEqual(sculpted["extents"][0], plain["extents"][0], places=3)
        self.assertAlmostEqual(sculpted["extents"][1], plain["extents"][1], places=3)
        self.assertEqual(sculpted["vertices"], plain["vertices"])

    def test_find_running_detects_this_server(self):
        # GET /api/hello has to work for this: the probe used to 404 and fall
        # back to /api/defaults on every port (12 x 2 x 0.4 s of startup).
        found = relief_gui.find_running(self.port)
        self.assertIsNotNone(found)
        self.assertEqual(found, f"http://127.0.0.1:{self.port}/")

    def test_find_running_ignores_free_ports(self):
        # 8790 is expected to be free in CI and on a dev box; pick a port range
        # far from the default and probe from there.
        self.assertIsNone(relief_gui.find_running(18900))


class DisconnectTest(unittest.TestCase):
    """Closing a tab or dragging a slider aborts requests; that must stay quiet.

    The console used to fill with socketserver tracebacks (WinError 10053 on the
    preview write, then a second one from the 500 path writing to the dead socket).
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = relief_gui.ReliefServer(("127.0.0.1", 0), relief_gui.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)

    def test_connection_errors_are_not_reported(self):
        buf = io.StringIO()
        try:
            raise ConnectionResetError(10054, "forcibly closed")
        except ConnectionResetError:
            with contextlib.redirect_stderr(buf):
                self.srv.handle_error(None, ("127.0.0.1", 0))
        self.assertEqual(buf.getvalue(), "")

    def test_real_bugs_are_still_reported(self):
        buf = io.StringIO()
        try:
            raise ValueError("a bug")
        except ValueError:
            with contextlib.redirect_stderr(buf):
                self.srv.handle_error(None, ("127.0.0.1", 0))
        self.assertIn("Exception occurred during processing", buf.getvalue())

    def test_aborted_preview_requests_keep_the_server_alive_and_quiet(self):
        import json
        import socket
        import struct
        import time
        import urllib.request

        demo = os.path.join(ROOT, "demo-depth.png")
        with open(demo, "rb") as f:
            req = urllib.request.Request(self._url("api/upload"), data=f.read(), method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                image_id = json.loads(resp.read())["id"]

        body = json.dumps({"image_id": image_id, "subdivisions": 256,
                           "preview_subdivisions": 256}).encode()
        header = (f"POST /api/preview HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                  f"Connection: close\r\n\r\n").encode()

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            for _ in range(6):
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                # linger 0 -> close() sends RST instead of a clean FIN, like an aborted fetch
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                sock.sendall(header + body)
                sock.close()
            time.sleep(1.0)          # let the request threads hit the dead sockets
        self.assertNotIn("Traceback", buf.getvalue())

        # the server keeps serving after the aborts
        with urllib.request.urlopen(self._url("api/defaults"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"


if __name__ == "__main__":
    unittest.main(verbosity=2)
