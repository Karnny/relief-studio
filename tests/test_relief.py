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
