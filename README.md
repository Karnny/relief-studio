# Relief Studio — depth map → printable relief mesh

Turn any **grayscale depth map** into a watertight, printable relief plate — with a
live 3D preview, exact plate dimensions in millimetres, face-pose correction, a local
sculpt brush and streamed STL/OBJ/GLB export.

There is no AI mesh generation in this tool: the input is a height field (a depth image)
and the output is generated geometry. That means it works the same with depth maps from
photogrammetry, monocular depth estimators, 3D-printing tooling, painted height maps, or
commercial depth-map exporters. A depth map plus the settings below is
enough to reproduce a plate deterministically — the same input and settings always produce
the same bytes.

## Settings

| Setting | Param | Default | Range | Effect |
| --- | --- | --- | --- | --- |
| Size | `width_mm` | 60 | 5–300, step 0.5 | Long edge of the plate in mm; the other edge follows the depth-map aspect ratio. |
| Carving mode | `invert_depth` | `false` | — | `Emboss` = height ∝ depth; `Engrave` = height ∝ 1 − depth. |
| Carving depth | `relief_height` | 6 | 0–60, step 0.1 | Millimetres between the lowest and highest point: `z = depth × relief_height`. |
| Remove base | `remove_base` | `false` | — | Drops the base: the floor becomes `max(minDepth, remove_threshold) × relief_height`. |
| Base thickness | `base_thickness` | 1 | 0.1–10, step 0.1 | Solid layer under the surface: floor = `minDepth × relief_height − base_thickness`. |
| Bend mode | `bend_mode` | `off` | off/convex/concave | Cylindrical wrap; radius = `extent / angle`. Bent plates get a full back grid. |
| Bend axis | `bend_axis` | `xz` | xz/yz | Which axis is wrapped. |
| Bend angle | `bend_angle` | 60° | 0–360, step 5 | Wrap angle; < 1° is treated as off. |
| Mesh resolution | `subdivisions` | 1024 | 512–2048, step 64 | Grid nodes on the long edge; short edge is proportional. |
| Depth curve | `curve` | `linear` | linear/gamma/s-curve | Depth response curve (`γ 2.2`, smoothstep). |
| Closed back | `closed_back` | `true` | — | Adds side walls + back so the plate is watertight. |
| Remove threshold | `remove_threshold` | 0.01 | 0.01–1 | Silhouette threshold when the base is removed. |
| Texture | (viewer only) | — | — | Bakes the source image into UVs; no effect on geometry. |
| Face tilt | `tilt_deg` | 0° | −30…30, step 1 | Perspective pitch on the depth rows: positive reads as chin up, negative as chin down. |
| Face turn | `yaw_deg` | 0° | −30…30, step 1 | Perspective yaw on the columns: positive turns the nose right in the image. |
| Head roll | `roll_deg` | 0° | −30…30, step 1 | In-plane rotation of the depth map: positive = clockwise. |
| Sculpt brush | `sculpt` | — | radius 1–25 mm, strength 0.1–5 mm | Local Gaussian raise/lower strokes painted on the model; stored as UV points and applied to the depth map before meshing. |

**Face correction (this tool's extension).** A relief is a shallow height field, so rotating it
in 3D barely moves the features — the depth axis is squashed. Instead three sliders warp the
depth map directly, the way photo retouchers use perspective/rotate:

- **Face tilt** (`tilt_deg`) — 1-D projective warp of the rows: the lower face expands, the
  upper compresses; positive reads as chin up, negative as chin down.
- **Face turn** (`yaw_deg`) — the same projective warp on the columns: positive turns the nose
  toward the right of the image, negative toward the left.
- **Head roll** (`roll_deg`) — bilinear in-plane rotation around the centre; positive =
  clockwise. A rotated rectangle exposes corners, so outside samples are clamped to the edge
  (a black background stays black).

All three keep the plate extents and mesh grid untouched, run before smoothing/curves, and are
image-space approximations, not a real head re-pose: keep angles modest (±5–15° typical) and
check the preview. Signs are listed above; 0° is byte-identical to the old output.

**Local sculpt (this tool's extension).** Press **Sculpt** in the viewport toolbar and drag on
the model: the brush raises (**Raise**) or lowers (**Lower**) the depth in a soft Gaussian patch,
with **Radius** and **Strength** in millimetres. A ring cursor shows the brush footprint on the
surface (orange in Lower mode). **Strength is the maximum offset one drag can add** — repeat the
stroke to build up — and Raise may exceed the current carving depth, so a nose already at the top
of the depth map still grows instead of flattening against a ceiling (the plate gets taller; lower
*Carving depth* afterwards to compress it, or Undo). Unlike the sliders this edits only the area
you paint. The browser projects the pointer onto the plate plane and sends UV strokes;
`relief.apply_sculpt()` applies the patches to the depth map on every preview and export, so the
exported STL keeps them. **Undo stroke** / **Clear** manage the session; strokes reset when you
load another depth map. In the CLI the same strokes can be replayed with `--sculpt strokes.json`.

**Plate size.** The GUI exposes **W and H in millimetres** with a Photoshop-style
aspect-ratio chain: lock it to scale proportionally, unlock it to stretch a plate wider or
slimmer than the depth map's own aspect (handy for adjusting a face model). **Fit depth map**
restores the natural aspect. When a height is set, `width_mm` and `height_mm` are the exact
plate dimensions; without a height, the long edge is `width_mm` and the other edge follows
the depth-map aspect. The mesh grid still follows the depth-map aspect, so stretching changes
proportions, not detail.

Depth maps may be 8-bit or **16-bit** grayscale. 16-bit PNGs (some depth exporters write
`I;16` with values 0–65535) are decoded at full precision; converting them through RGB clips
everything above 255 to white and collapses the plate into a binary mask.

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

A local dark-themed studio:

- **Left panel** — drop a depth map (PNG/JPG), then adjust *Plate size* (W × H in mm with
  aspect lock and *Fit depth map*), the grouped **Face pose** section (*Face tilt*, *Face
  turn*, *Head roll*), *Carving mode* (Emboss/Engrave), *Carving depth*, and under
  *Advanced*: *Remove base*, *Base thickness*, *Depth curve*, *Bend mode/axis/angle*,
  *Export resolution*. A demo map loads on start so the viewport is never empty.
- **Viewport** — orbit / zoom / pan, wireframe, auto-rotate, a *Texture* toggle that maps the
  source image back onto the surface UVs, and a *Sculpt* tool for local raise/lower brushing
  (left-drag on the model while it is on; toggle it off to orbit again).
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

# stretch the plate to an exact 80 x 40 mm (Photoshop-style W x H)
python3 relief.py depth.png -o plate.stl --width-mm 80 --height-mm 40

# chin the face up by 8 degrees (perspective pitch correction)
python3 relief.py depth.png -o plate.stl --tilt 8

# turn it 10 degrees right and roll it 5 degrees clockwise
python3 relief.py depth.png -o plate.stl --yaw 10 --roll 5

# replay local sculpt strokes (same JSON the GUI sends)
python3 relief.py depth.png -o plate.stl --sculpt strokes.json
```

`--height-mm` (explicit plate height; omit to follow the depth-map aspect), `--tilt`
(pitch, + = chin up), `--yaw` (+ = nose turns right), `--roll` (+ = clockwise), `--sculpt`
(JSON list of `{radius_mm, strength_mm, points:[{u,v}]}` strokes), `--engrave`,
`--curve gamma|s-curve`, `--bend convex|concave`, `--bend-axis yz`, `--bend-angle 60`,
`--remove-base`, `--stats` are supported. Output is binary STL or OBJ, in millimetres,
watertight.

Compare a reference export from another tool against a rebuild (same depth map and settings):

```bash
python3 compare_mesh.py reference-export.stl rebuilt.stl
```

## Validation

The kernel is deterministic and verifiable:

- The 512-grid `demo-depth.png` export is a fixed byte hash (`130d1f33…`, 26,418,784 B);
  the test suite asserts it, so any accidental geometry change shows up immediately.
- `compare_mesh.py` checks that two meshes built from the same depth map and settings have
  matching vertex counts, groups and per-vertex surface positions.
- Cross-platform builds (macOS arm64, Windows arm64, Windows x86-64) have produced the same
  canary hash for the same input.
- The grid, side walls/back, bend and extrusion are covered by unit tests and the GUI/API
  tests in `tests/`.

## Not implemented

- Silhouette/outline extraction from a segmentation mask — rectangular plates from a depth
  map work; cutting a shape out of the depth map is not modelled.
- Edge fillets on the relief surface.
- Baked colour/texture output in GLB (the viewer can show the source image, exports carry
  geometry + UVs, not a baked material).

## Tests

```bash
python -m unittest discover -s tests -v
```

Fifty-five stdlib tests (~3.8 s; only numpy and pillow are needed): the 512-grid
`demo-depth.png` STL hash canary (byte-identical across platforms), 16-bit depth decode
precision, the plate W × H transform (auto and explicit), the face tilt/turn/roll warps, the
local sculpt brush (locality, per-stroke cap, accumulation, raise past the ceiling,
sanitising), the auto-updater (version compare, release/checksum parsing, staged download
verification, exe swap, PyInstaller-safe relaunch environment), the GUI server's
`/api/hello` / `/api/update` / single-instance probing, and aborted-client handling. CI
runs them before every build.


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

**Updates (v1.5.0+)**

The Windows build updates itself from GitHub Releases:

1. On launch (and every 6 h) it asks the release feed for the latest tag. When a newer
   version exists the header shows *"v1.6.0 available — you have 1.5.0"*; source runs on
   other platforms only get a **Release page** link.
2. **Download** fetches `ReliefStudio.exe` for the new tag, verifies it against the
   `ReliefStudio.exe.sha256` asset published in the same release and stages it under
   `%LOCALAPPDATA%\ReliefStudio\updates\<version>\`.
3. **Restart to update** closes the app, the staged exe is swapped in (the old one is
   renamed to `ReliefStudio.exe.old` and cleaned up on the following start) and the new
   build reopens. If anything fails, the old exe is restored and the app keeps running.
4. Other PCs only need this once: every installed copy checks the same feed, so no
   shared drive, installer or IT step is involved. Offline machines can keep using a
   manually downloaded exe — the checker just reports nothing.

`--check-updates` prints the feed result as JSON and exits; `--update-api URL` (with
`--update-insecure` for plain HTTP) points the checker at a mirror or a local test feed.
GitHub's unauthenticated API is rate-limited, hence the 6 h cache. Release assets and the
update path stay unsigned (SmartScreen), so signing the exe is the next trust step.

**Feed visibility:** the release feed must be readable without credentials. This repository
is public, so the default `api.github.com/.../releases/latest` check works for every copy
(verified: `/api/hello` 1.5.0, `/api/update` latest 1.5.0, no error). If the repo is ever
made private again, publish the release `ReliefStudio.exe` + `.sha256` on a public mirror and
point the app at it (`RELIEF_UPDATE_API` env var, or an `update.json` next to the exe). The
release workflow publishes both assets, so a mirror only needs to copy them.

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
├── updater.py             # GitHub-Release update check / download / staged exe swap
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
