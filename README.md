> Moving this project to another machine? Start with [HANDOFF.md](HANDOFF.md) — current state, Windows build steps, gotchas and open items.

# the source tool relief → depth map → own mesh

Derived from the source tool's shipped web client (Aug 2026 build). The relief
plate is **not** produced by an AI mesh model: the source tool's client turns the depth map
into a 2.5D plate with a Web Worker, and the server rebuilds the same geometry
for the download. So a depth map + the settings is enough to reproduce the mesh.

## Evidence

| What | Where |
| --- | --- |
| Relief settings UI (Size, Carving Mode/Depth, Remove Base, Base Thickness, Bend Mode, Mesh Resolution) | `reference-web/js/1ojpo91f_pvau.js` |
| Parameter schema `relief_params_v1`, defaults, ranges | `reference-web/js/0leysj_qdg73l.js` |
| Geometry kernel (`buildRelief`) | `reference-web/js/extrude-worker.min.js` (beautified: `extrude-worker.beauty.js`) |
| Param → worker call, bend/grid-back switch | `reference-web/js/32c54l5nl0du3.js` |
| Depth map + model download | `reference-web/js/18kcj81rj6_w5.js` (formats: `glb`, `stl`) |

Server side only stores params (`PUT /v2/tasks/{id}/relief/params`) and builds the
export (`POST /v2/tasks/{id}/relief/model/build`); `GET /v2/tasks/{id}/relief/snapshot`
returns `{depth, source}` — the depth map you can download.

## Parameter mapping

| UI label | Param | Default | Range | Effect |
| --- | --- | --- | --- | --- |
| Size | `widthMM` | 60 | 20–250, step 1 | Long edge of the plate in mm; the other edge follows the depth-map aspect ratio. |
| Carving Mode | `invertDepth` | `false` | — | `Emboss` = height ∝ depth; `Engrave` = height ∝ 1 − depth. |
| Carving Depth | `reliefHeight` | 6 (low: 3) | 0–60, step 0.1 | Millimetres between the lowest and highest point. `z = depth × reliefHeight`. |
| Remove Base | `removeBase` | `false` | — | Drops the base: the floor becomes `max(minDepth, removeThreshold) × reliefHeight`. |
| Base Thickness | `baseThickness` | 1 | 0.1–10, step 0.1 | Solid layer under the surface: floor = `minDepth × reliefHeight − baseThickness`. |
| Bend Mode | `bendMode` | `off` | off/convex/concave | Cylindrical wrap; radius = `extent / angle`. Bent plates get a full back grid. |
| Bend Axis | `bendAxis` | `xz` | xz/yz | Which axis is wrapped. |
| Bend Angle | `bendAngle` | 60° | 0–360, step 5 | Wrap angle; < 1° is treated as off. |
| Mesh Resolution | `subdivisions` | 1024 | 512–2048, step 64 | Grid nodes on the long edge; short edge is proportional. |
| — | `curve` | `linear` | linear/gamma/s-curve | Depth response curve (`γ 2.2`, smoothstep). |
| — | `closedBack` | `true` | — | Adds side walls + back so the plate is watertight. |
| — | `removeThreshold` | 0.01 | 0.01–1 | Silhouette threshold when the base is removed. |
| Color Mapping | (texture only) | — | — | Bakes the photo into UVs; no effect on geometry. |

Depth maps may be 8-bit or **16-bit** grayscale. 16-bit PNGs (the source tool's own relief
depth maps are `I;16`, values 0–65535) are decoded at full precision; converting
them through RGB clips everything above 255 to white and collapses the plate into
a binary mask.

## Geometry the kernel builds

1. Depth PNG → float `d = mean(R,G,B)/255` per pixel.
2. Optional invert, then curve.
3. Bilinear resample onto a `cols × rows` grid (`cols = subdivisions` on the long edge).
4. `z = d × reliefHeight` mm, clamped to the floor; `x = (u − 0.5) × width`, `y = (0.5 − v) × height`
   (image row 0 is the top, so the mesh is not mirrored).
5. 2 triangles per cell; side walls down to the floor; back = centre fan (flat plate)
   or full grid (bent plate).
6. Optional cylindrical bend around x or y.

## GUI

```bash
./run.sh            # opens http://127.0.0.1:8765
```

A local dark-themed studio, modelled on the source tool's panel:

- **Left panel** — drop a depth map (PNG/JPG), then adjust *Size*, *Carving mode*
  (Emboss/Engrave), *Carving depth*, and under *Advanced*: *Remove base*,
  *Base thickness*, *Depth curve*, *Bend mode/axis/angle*, *Export resolution*.
  A demo map loads on start so the viewport is never empty.
- **Viewport** — orbit / zoom / pan, wireframe, auto-rotate, and a *Texture*
  toggle that maps the source image back onto the surface UVs.
- **Preview resolution** — 128–2048 grid nodes on the long edge (default 256),
  independent of the export setting. 256 rebuilds in ~10 ms; 1024 in ~80 ms;
  2048 is 3.1 M vertices and feels heavier.
- **Export resolution** — 512–**8192** (default 1024). STL is available across
  the whole range and is written in chunks, so an 8192 grid (100.7 M triangles,
  ~5 GB) does not need a second copy in RAM. OBJ and GLB stop at 2048 because
  their text/uncompressed buffers do not scale the same way; the buttons
  disable themselves and the panel shows the estimated STL size.
- **Live preview** — every change re-runs `relief.py` on the preview grid and
  refreshes the mesh; the readout shows verts, tris, plate size, relief height
  and frame time.
- **Export** — STL, OBJ or GLB, streamed straight from the server to the browser.

The GUI is a thin layer over the same engine: `relief.py` computes the preview
and the export, so what you see is exactly what you save. It binds to
127.0.0.1 only and stores uploads in memory.

## CLI

```bash
python3 relief.py depth.png -o plate.stl \
    --width-mm 60 --relief-mm 19 --base 0.1 --resolution 2048
```

`--engrave`, `--curve gamma|s-curve`, `--bend convex|concave`, `--bend-axis yz`,
`--bend-angle 60`, `--remove-base`, `--stats` are supported. Output is binary STL
or OBJ, in millimetres, watertight.

Compare a the source tool download against a rebuild:

```bash
python3 compare_mesh.py the source tool-download.stl rebuilt.stl
```

## Validation

The port was checked against the source tool's own worker (`extrude-worker.min.js` run in a
Node worker harness, `reference-web/harness/`):

| Case | the source tool worker | This script |
| --- | --- | --- |
| 128² depth, 60 mm, 19 mm, 256 subdiv, flat | 70,637 verts / 133,110 tris | identical counts, groups, bbox; surface Δz ≤ 3.3e-5 mm |
| 128² depth, 40 mm, 8 mm, 192 subdiv, engrave + s-curve + convex 90° | 76,784 verts / 147,452 tris | identical counts, groups; bbox Δ ≤ 2e-6 mm |

## Not implemented

- Silhouette/outline extraction (`removeBackground`, segmentation mask, `phi`) — only
  reachable when the source tool cuts a shape out of the depth map.
- Edge fillets (`filletRatio`, 1 % of relief height, applied by the worker's tuning).
- Texture/UV baking for GLB colour mapping.

Neither affects the geometry of a rectangular plate produced from a depth map.

## Tests

```bash
python -m unittest discover -s tests -v
```

Twelve stdlib tests (~4 s; only numpy and pillow are needed): the 512-grid `demo-depth.png`
STL hash canary (byte-identical across platforms), 16-bit depth decode precision, the
GUI server's `/api/hello` / single-instance probing, and aborted-client handling. CI runs
them before every build.


## macOS app

A signed-for-local-use bundle is already built:

```
dist/Relief Studio.app
dist/ReliefStudio-1.0.0-macOS-arm64.dmg
```

Double-click the app (or mount the DMG and drag it out); the browser opens at
`http://127.0.0.1:8765/`. Stop it with the **Quit** button in the page header —
closing the tab alone leaves the local server running.

Rebuild after changing the code:

```bash
./packaging/build-macos.sh          # or: PYTHON=/path/to/python3 ./packaging/build-macos.sh
```

The script creates `.venv-build-mac`, installs numpy/pillow/PyInstaller, builds
`Relief Studio.app`, ad-hoc signs it, and wraps it in a compressed DMG.

Notes:

- The build is **arm64** (Apple Silicon). On an Intel Mac the same script
  produces an x86_64 bundle for that host.
- The signature is ad-hoc, not notarised. On *another* Mac, Gatekeeper may
  refuse the first launch: right-click the app → **Open**, or run
  `xattr -dr com.apple.quarantine "/Applications/Relief Studio.app"`.
- Startup failures are written to `$TMPDIR/relief-studio-error.log`.
- If port 8765 is busy the app walks 8766–8776 instead of failing.
- **One instance only.** Launching it again (double-click, or a second copy)
  probes 8765–8776 for a running Relief Studio, opens that URL in the browser
  and exits immediately, so you never end up with two rival servers. Detection
  uses `/api/hello` and also recognises older builds through `/api/defaults`.

## Windows executable

A prebuilt **Windows x64** exe is attached to every GitHub Release:
<https://github.com/Karnny/relief-studio/releases/latest> — download and run, no build
needed. (Unsigned, so SmartScreen warns once; ARM64 must be built locally.)

PyInstaller cannot cross-compile: an `.exe` has to be produced on Windows. Two
supported routes, both using the same `ReliefStudio.spec`:

**A. Build on a Windows machine (about two minutes)**

1. Copy this `relief/` folder to the PC.
2. Install Python 3.10+ from <https://www.python.org/downloads/windows/>
   (tick *Add python.exe to PATH*).
3. Double-click `packaging\build-windows.bat`.
4. It creates `.venv-build`, installs numpy/pillow/PyInstaller, and writes
   `dist\ReliefStudio.exe` — a single self-contained file (~26.5 MB on x64, ~21 MB on
   arm64; builds in ~20 s).

Run it by double-clicking: the browser opens at `http://127.0.0.1:8765/`. The Windows
build keeps a console window (the URL and Ctrl+C live there); startup errors also go to
`%TEMP%\relief-studio-error.log`. Startup takes ~1–2 s, and launching a second copy opens
the running instance and exits.

No build needed? `run-windows.bat` starts it straight from source.

**B. GitHub Actions (no Windows machine)**

`.github/workflows/build-windows.yml` builds the exe on `windows-latest`, runs the
tests, smoke-tests the executable (starts it, fetches `app.js` and the bundled
three.js), and uploads the artifact `ReliefStudio-windows-x64`.
`.github/workflows/release.yml` does the same and attaches the exe to a GitHub Release
when a `v*` tag is pushed — so users can just download a build. Both are triggered from
the Actions tab (*Run workflow*). Keep the workflow at the repository root — the first
step detects whether the project is in the root or a `relief/` subfolder.

**Windows notes**

- The shipped Windows build keeps a console window: it prints the URL and takes
  Ctrl+C. A windowed (`console=False`) variant also runs fine, including over
  SSH — it just has no visible output — so the console is kept for diagnostics.
  (If you test over SSH and see `ERROR: Input redirection is not supported`,
  that comes from `timeout.exe` in the test command, not from the app.)
  macOS stays windowed because the bundle has no console concept.
- **Architecture follows the Python that runs the build** (PyInstaller has no
  cross-compiler): an ARM64 Windows VM yields an ARM64 exe, an x64 Windows
  machine or the `windows-latest` CI runner yields an x86-64 exe. Both were
  built and verified:
  - `dist/ReliefStudio.exe` — PE32+ **Aarch64**, 20.9 MB (Windows 11 ARM64 guest,
    Python 3.12.10)
  - `dist/ReliefStudio-x64.exe` — PE32+ **x86-64**, 26.5 MB (x64 Python running
    under Windows-on-ARM emulation in the same guest)
  The same depth map and settings produced a **byte-identical STL** from the
  macOS, ARM64-Windows and x64-Windows builds.
- The exe is unsigned, so SmartScreen shows "Windows protected your PC" →
  *More info* → *Run anyway*. Some antivirus engines also flag PyInstaller
  one-file binaries heuristically; route A builds locally, so nothing is
  downloaded from a third party.
- The server binds to `127.0.0.1` only and keeps uploads in memory.
- One-file builds unpack to `%TEMP%` on start (~1–3 s first launch).

### Layout

```
relief/
├── relief.py              # geometry kernel (depth map -> mesh)
├── relief_gui.py          # local HTTP server + JSON/binary API
├── static/                # UI (app.js, style.css, vendor/three)
├── tests/                 # unittest regression tests (STL hash canary, decode, API)
├── ReliefStudio.spec      # PyInstaller recipe (Windows + macOS/Linux)
├── packaging/
│   ├── build-macos.sh     # one-click macOS .app + .dmg
│   └── build-windows.bat  # one-click Windows build
├── run-windows.bat        # run from source on Windows
├── run.sh                 # run from source on macOS/Linux
└── .github/workflows/     # CI: .exe artifact (build-windows.yml), GitHub Release (release.yml)
```
