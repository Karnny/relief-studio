import * as THREE from 'three';
import { OrbitControls } from '/vendor/OrbitControls.js';

const $ = (id) => document.getElementById(id);

// Surface failures in the UI instead of a silent blank viewport.
function reportError(msg) {
  document.title = 'ERR ' + msg;
  const s = document.getElementById('stats');
  if (s) s.textContent = '⚠ ' + msg;
}
window.addEventListener('error', (e) => reportError(e.message || String(e.error)));
window.addEventListener('unhandledrejection', (e) => reportError(e.reason?.message || String(e.reason)));

const state = {
  imageId: null,
  wireframe: false,
  spin: false,
  textured: true,
  previewSubdiv: 256,
  lastMeshMs: 0,
  aspectLock: true,      // link W and H like Photoshop's transform chain
  imageAspect: 1,        // depth map width / height
  ratio: 1,              // locked W / H ratio
  sculptMode: false,
  sculptStrokes: [],     // [{radius_mm, strength_mm, points:[{u,v}]}] applied to the depth map
  surface: null,         // {cols, rows, positions} of the latest preview surface
  plateOffset: { x: 0, y: 0 },   // centring translation applied to the preview geometry
};

// ---------------------------------------------------------------- three.js
const canvas = $('view');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e0f11);

const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 20000);
camera.position.set(0, 0, 140);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 5;
controls.maxDistance = 4000;

scene.add(new THREE.HemisphereLight(0xffffff, 0x14171a, 0.75));
const key = new THREE.DirectionalLight(0xfff6ec, 2.4);
key.position.set(70, 90, 160);
scene.add(key);
const fill = new THREE.DirectionalLight(0x9fb6ff, 0.55);
fill.position.set(-120, -60, 80);
scene.add(fill);
const rim = new THREE.DirectionalLight(0xffffff, 0.9);
rim.position.set(-40, 120, -120);
scene.add(rim);

const material = new THREE.MeshStandardMaterial({
  color: 0xd9d0c4, roughness: 0.68, metalness: 0.04, side: THREE.DoubleSide,
});
const mesh = new THREE.Mesh(new THREE.BufferGeometry(), material);
mesh.visible = false;
scene.add(mesh);

let texture = null;

// Debug handle (handy in DevTools and for automated checks).
window.__relief = { scene, camera, controls, mesh, material, state };
window.__relief.three = THREE;
window.__relief.refresh = (...a) => refresh(...a);

function resize() {
  const w = canvas.clientWidth || 1;
  const h = canvas.clientHeight || 1;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas);
resize();

function frameCamera(head) {
  const [extX, extY] = head.extents;
  const depth = Math.max(0.001, head.z_max - head.z_min);
  const size = Math.max(extX, extY, depth);
  const dist = size / (2 * Math.tan((camera.fov * Math.PI) / 360)) * 1.35;
  camera.position.set(dist * 0.12, dist * 0.10, dist);
  controls.target.set(0, 0, 0);
  controls.update();
}

// ---------------------------------------------------------------- preview
function readControls() {
  return {
    width_mm: +$('width_mm').value,
    height_mm: +$('height_mm').value,
    relief_height: +$('relief_height').value,
    base_thickness: +$('base_thickness').value,
    subdivisions: +$('subdivisions').value,
    invert_depth: mode === 'engrave',
    curve,
    black_point: +$('black_point').value,
    smooth: +$('smooth').value,
    remove_base: $('remove_base').checked,
    remove_threshold: 0.01,
    closed_back: true,
    bend_mode: bend,
    bend_axis: axis,
    bend_angle: +$('bend_angle').value,
    tilt_deg: +$('tilt_deg').value,
    yaw_deg: +$('yaw_deg').value,
    roll_deg: +$('roll_deg').value,
    sculpt: sculptStrokesPayload(),
  };
}

let mode = 'emboss';
let curve = 'linear';
let bend = 'off';
let axis = 'xz';

function parseMesh(buffer) {
  const magic = new TextDecoder().decode(new Uint8Array(buffer, 0, 4));
  if (magic !== 'RLF1') throw new Error('bad payload');
  const dv = new DataView(buffer);
  const jsonLen = dv.getUint32(4, true);
  const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 8, jsonLen)));
  let off = 8 + jsonLen;
  const n = head.vertices, m = head.triangles * 3;
  const positions = new Float32Array(buffer.slice(off, off + n * 12)); off += n * 12;
  const uvs = new Float32Array(buffer.slice(off, off + n * 8)); off += n * 8;
  const indices = new Uint32Array(buffer.slice(off, off + m * 4));
  return { head, positions, uvs, indices };
}

let inFlight = null;
let pending = null;

async function refresh({ frame = false } = {}) {
  if (!state.imageId) return;
  const body = {
    image_id: state.imageId, ...readControls(),
    subdivisions: state.previewSubdiv,        // preview grid, not the export slider
    preview_subdivisions: state.previewSubdiv,
  };
  const t0 = performance.now();
  $('busy').hidden = false;
  try {
    if (inFlight) inFlight.abort();
    const ctl = new AbortController();
    inFlight = ctl;
    const res = await fetch('/api/preview', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body), signal: ctl.signal,
    });
    if (inFlight === ctl) inFlight = null;
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      $('stats').textContent = '⚠ ' + (err.error || res.statusText);
      return;
    }
    const { head, positions, uvs, indices } = parseMesh(await res.arrayBuffer());
    state.lastMeshMs = performance.now() - t0;

    // Build a fresh geometry: reusing one while buffer sizes change leaves
    // stale GPU buffers behind and renders stale triangles.
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
    geo.setIndex(new THREE.BufferAttribute(indices, 1));
    geo.computeVertexNormals();
    geo.computeBoundingBox();
    geo.computeBoundingSphere();

    // centre the plate so orbiting feels natural
    const c = geo.boundingBox.getCenter(new THREE.Vector3());
    geo.translate(-c.x, -c.y, 0);
    state.plateOffset = { x: c.x, y: c.y };
    state.surface = { cols: head.cols, rows: head.rows, positions };
    state.lastExtents = head.extents;

    const old = mesh.geometry;
    mesh.geometry = geo;
    if (old) old.dispose();
    mesh.visible = true;

    if (frame) frameCamera(head);

    $('stats').textContent =
      `${head.vertices.toLocaleString()} verts · ${head.triangles.toLocaleString()} tris\n` +
      `${head.extents[0].toFixed(1)} × ${head.extents[1].toFixed(1)} mm · ` +
      `height ${(head.z_max - head.z_min).toFixed(2)} mm\n` +
      `grid ${head.cols} × ${head.rows} · ${Math.round(state.lastMeshMs)} ms`;
    $('exportInfo').textContent =
      `export ${(+$('subdivisions').value).toLocaleString()} px`;
  } catch (err) {
    if (err.name !== 'AbortError') console.error(err);
  } finally {
    if (!inFlight) $('busy').hidden = true;
  }
}

function schedule({ frame = false } = {}) {
  clearTimeout(pending);
  pending = setTimeout(() => refresh({ frame }), 70);
}

// ---------------------------------------------------------------- controls
function bindPair(numId, rangeId, onChange) {
  const num = $(numId), range = $(rangeId);
  const sync = (v, from) => {
    const val = Math.min(+num.max, Math.max(+num.min, +v || 0));
    num.value = val; range.value = val;
    onChange && onChange(val, from);
  };
  num.addEventListener('input', (e) => sync(e.target.value, 'num'));
  range.addEventListener('input', (e) => sync(e.target.value, 'range'));
  // keep the two roughly in sync on change (avoid fighting during typing)
  range.addEventListener('change', () => schedule());
  num.addEventListener('change', () => schedule());
}

['relief_height', 'base_thickness', 'subdivisions', 'bend_angle', 'preview_res', 'black_point', 'smooth', 'tilt_deg', 'yaw_deg', 'roll_deg'].forEach((id) =>
  bindPair(id, id + '_r'));

['relief_height', 'base_thickness', 'subdivisions', 'bend_angle', 'tilt_deg', 'yaw_deg', 'roll_deg'].forEach((id) =>
  $(id + '_r').addEventListener('input', () => schedule()));

// ------------------------------------------------- plate W × H (Photoshop-style)
// the source tool's "Size" only scaled the long edge and let the other edge follow the
// depth-map aspect. Here W and H are independent plate dimensions (mm) with an
// optional chain lock, so a face can be stretched wider or slimmer.
const WH_MIN = 5, WH_MAX = 300;
const clampWH = (v) => Math.min(WH_MAX, Math.max(WH_MIN, Math.round((+v || 0) * 10) / 10));
const fmtWH = (v) => (Math.round(v * 10) / 10).toString();

function naturalSize(longEdge = 60) {
  const a = state.imageAspect || 1;
  return a >= 1 ? [longEdge, longEdge / a] : [longEdge * a, longEdge];
}
window.__relief.naturalSize = naturalSize;   // debug handle for automated checks

function updateRatioInfo() {
  const w = +$('width_mm').value || 1;
  const h = +$('height_mm').value || 1;
  const same = state.imageAspect > 0 && Math.abs(w / h - state.imageAspect) < 0.005;
  $('ratioInfo').textContent =
    `aspect ${(w / h).toFixed(3)}${state.aspectLock ? ' · linked' : ' · free'}` +
    (same ? ' · depth-map aspect' : '');
}

function applySize(w, h, from) {
  w = clampWH(w); h = clampWH(h);
  if (state.aspectLock) {
    if (from === 'w') h = clampWH(w / state.ratio);
    else if (from === 'h') w = clampWH(h * state.ratio);
  }
  if (!state.aspectLock) state.ratio = w / h;
  $('width_mm').value = fmtWH(w);
  $('height_mm').value = fmtWH(h);
  updateRatioInfo();
}

function sizeEdited(from) {
  if (state.aspectLock) {
    const w = +$('width_mm').value, h = +$('height_mm').value;
    if (from === 'w' && w > 0) $('height_mm').value = fmtWH(clampWH(w / state.ratio));
    if (from === 'h' && h > 0) $('width_mm').value = fmtWH(clampWH(h * state.ratio));
  } else {
    state.ratio = (+$('width_mm').value || 1) / (+$('height_mm').value || 1);
  }
  updateRatioInfo();
}

$('width_mm').addEventListener('input', () => sizeEdited('w'));
$('height_mm').addEventListener('input', () => sizeEdited('h'));
['width_mm', 'height_mm'].forEach((id) => $(id).addEventListener('change', () => {
  applySize($('width_mm').value, $('height_mm').value, 'fit');
  schedule();
}));

$('ratio_lock').addEventListener('click', (e) => {
  state.aspectLock = !state.aspectLock;
  e.currentTarget.classList.toggle('on', state.aspectLock);
  e.currentTarget.textContent = state.aspectLock ? '\u{1F517}' : '\u{1F513}';
  e.currentTarget.setAttribute('aria-label',
    state.aspectLock ? 'Unlock aspect ratio' : 'Lock aspect ratio');
  if (state.aspectLock) {
    state.ratio = (+$('width_mm').value || 1) / (+$('height_mm').value || 1);
  }
  updateRatioInfo();
});

$('fit_aspect').addEventListener('click', () => {
  const longEdge = Math.max(+$('width_mm').value, +$('height_mm').value) || 60;
  const [w, h] = naturalSize(longEdge);
  applySize(w, h, 'fit');
  state.ratio = w / h;
  updateRatioInfo();
  schedule({ frame: true });
});

// preview resolution only rebuilds the viewport mesh (no export impact)
const applyPreviewRes = (v) => {
  const r = Math.max(128, Math.min(2048, Math.round(+v || 256)));
  state.previewSubdiv = r;
  $('preview_res').value = r;
  $('preview_res_r').value = r;
  $('previewHint').textContent = r > 1024
    ? `Grid nodes on the long edge — ${r} px is very heavy (≈${(r * r * 0.75 / 1e6).toFixed(1)} M verts).`
    : r > 512
      ? `Grid nodes on the long edge — ${r} px may feel sluggish while dragging.`
      : 'Grid nodes on the long edge. Browser-side; export uses the value below.';
  schedule();
};
$('preview_res').addEventListener('input', (e) => applyPreviewRes(e.target.value));
$('preview_res_r').addEventListener('input', (e) => applyPreviewRes(e.target.value));

// export resolution: warn about size, and constrain formats that cannot scale
function updateExportHint() {
  const sub = +$('subdivisions').value;
  const approxTris = 2 * sub * sub * 0.75;
  const bytes = approxTris * 50;
  const gb = bytes / 1073741824;
  const big = sub > 2048;
  $('exportHint').textContent = sub >= 4096
    ? `STL ≈ ${gb.toFixed(1)} GB — OBJ/GLB unavailable above 2048.`
    : `STL up to 8192 · OBJ/GLB up to 2048.${sub > 2048 ? ' Above 2048 only STL is offered.' : ''}`;
  document.querySelectorAll('#export button').forEach((b) => {
    if (b.dataset.fmt === 'stl') return;
    b.disabled = big;
    b.title = big ? 'Use STL above 2048 px' : '';
  });
  $('exportInfo').textContent = `export ${sub.toLocaleString()} px`;
}
$('subdivisions').addEventListener('change', updateExportHint);
$('subdivisions').addEventListener('input', updateExportHint);
updateExportHint();

$('remove_base').addEventListener('change', () => {
  $('baseField').style.opacity = $('remove_base').checked ? 0.4 : 1;
  schedule();
});

function segment(container, attr, cb) {
  container.querySelectorAll('button').forEach((btn) => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('button').forEach((b) => b.classList.remove('on'));
      btn.classList.add('on');
      cb(btn.dataset[attr]);
      schedule();
    });
  });
}
segment($('mode'), 'mode', (v) => { mode = v; });
segment($('curve'), 'curve', (v) => { curve = v; });
segment($('bend'), 'bend', (v) => {
  bend = v;
  $('bendExtra').hidden = v === 'off';
});
segment($('axis'), 'axis', (v) => { axis = v; });

$('wire').addEventListener('click', (e) => {
  state.wireframe = !state.wireframe;
  material.wireframe = state.wireframe;
  e.currentTarget.classList.toggle('on', state.wireframe);
});
$('spin').addEventListener('click', (e) => {
  state.spin = !state.spin;
  controls.autoRotate = state.spin;
  controls.autoRotateSpeed = 1.6;
  e.currentTarget.classList.toggle('on', state.spin);
});
$('tex').addEventListener('click', (e) => {
  state.textured = !state.textured;
  material.map = state.textured ? texture : null;
  material.needsUpdate = true;
  e.currentTarget.classList.toggle('on', state.textured);
});

// ---------------------------------------------------------------- sculpt brush
// Local raise/lower brush on the depth map: raycast the preview, collect UV
// points along the drag, and let the server apply Gaussian falloff patches.
const raycaster = new THREE.Raycaster();
const pointerNdc = new THREE.Vector2();
const planeZ0 = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
const planeHit = new THREE.Vector3();
let brush = 'raise';
let activeStroke = null;
let lastStrokePoint = null;
let sculptTimer = null;
let lastSculptPaint = 0;

// World-sized ring that follows the pointer in sculpt mode (like ZBrush/Blender).
const brushRing = new THREE.Mesh(
  new THREE.RingGeometry(0.93, 1.0, 64),
  new THREE.MeshBasicMaterial({
    color: 0xd6ff3f, side: THREE.DoubleSide, transparent: true,
    opacity: 0.9, depthTest: false, depthWrite: false,
  }),
);
brushRing.visible = false;
brushRing.renderOrder = 999;
scene.add(brushRing);

function sculptStrokesPayload() {
  return activeStroke ? state.sculptStrokes.concat([activeStroke]) : state.sculptStrokes;
}

function updateSculptInfo() {
  const n = state.sculptStrokes.length;
  const pts = state.sculptStrokes.reduce((sum, s) => sum + s.points.length, 0);
  const editing = $('sculpt').classList.contains('on');
  if (n) {
    $('sculptInfo').textContent =
      `${n} stroke${n === 1 ? '' : 's'} · ${pts} points — ` +
      (editing ? 'drag on the model to keep sculpting.' : 'press Sculpt to edit again.');
  } else {
    const hint = '0 strokes — press Sculpt (top right of the viewport), then drag on the model.';
    $('sculptInfo').textContent = hint;
  }
}

function surfaceZ(u, v) {
  const s = state.surface;
  if (!s) return 0;
  const fx = Math.min(s.cols - 1, Math.max(0, u * (s.cols - 1)));
  const fy = Math.min(s.rows - 1, Math.max(0, v * (s.rows - 1)));
  const x0 = Math.floor(fx), y0 = Math.floor(fy);
  const x1 = Math.min(s.cols - 1, x0 + 1), y1 = Math.min(s.rows - 1, y0 + 1);
  const tx = fx - x0, ty = fy - y0;
  const zp = s.positions;
  const at = (row, col) => zp[(row * s.cols + col) * 3 + 2];
  const top = at(y0, x0) * (1 - tx) + at(y0, x1) * tx;
  const bot = at(y1, x0) * (1 - tx) + at(y1, x1) * tx;
  return top * (1 - ty) + bot * ty;
}

// Map the pointer onto the plate plane and back to depth-map UV. The plane
// projection keeps this O(1) even for multi-million-triangle previews (a mesh
// raycast would walk every triangle on each mouse move).
function brushCursor(ev) {
  const rect = canvas.getBoundingClientRect();
  pointerNdc.set(((ev.clientX - rect.left) / rect.width) * 2 - 1,
                 -((ev.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(pointerNdc, camera);
  if (!raycaster.ray.intersectPlane(planeZ0, planeHit)) return null;
  const off = state.plateOffset;
  const ext = state.lastExtents || [60, 60];
  const u = (planeHit.x + off.x) / ext[0] + 0.5;
  const v = 0.5 - (planeHit.y + off.y) / ext[1];
  if (u < 0 || u > 1 || v < 0 || v > 1) return null;
  return { u, v, point: new THREE.Vector3(planeHit.x, planeHit.y, surfaceZ(u, v)) };
}

function updateBrushRing(cursor) {
  if (!state.sculptMode || !cursor) {
    brushRing.visible = false;
    return;
  }
  brushRing.visible = true;
  brushRing.position.copy(cursor.point);
  brushRing.quaternion.copy(camera.quaternion);      // always readable from the camera
  brushRing.scale.setScalar(+$('brush_radius').value || 6);
  brushRing.material.color.set(brush === 'lower' ? 0xff9a5a : 0xd6ff3f);
  brushRing.material.opacity = activeStroke ? 1.0 : 0.85;
}

function sculptRefreshSoon() {
  const now = performance.now();
  const wait = Math.max(0, 90 - (now - lastSculptPaint));
  if (wait === 0) {
    lastSculptPaint = now;
    refresh();
    return;
  }
  if (!sculptTimer) {
    sculptTimer = setTimeout(() => {
      sculptTimer = null;
      lastSculptPaint = performance.now();
      refresh();
    }, wait);
  }
}

function endStroke() {
  if (!activeStroke) return;
  if (activeStroke.points.length) state.sculptStrokes.push(activeStroke);
  activeStroke = null;
  lastStrokePoint = null;
  updateSculptInfo();
  sculptRefreshSoon();
}

$('sculpt').addEventListener('click', (e) => {
  state.sculptMode = !state.sculptMode;
  e.currentTarget.classList.toggle('on', state.sculptMode);
  controls.enabled = !state.sculptMode;   // left-drag paints instead of orbiting
  canvas.style.cursor = state.sculptMode ? 'crosshair' : '';
  if (!state.sculptMode) endStroke();
  brushRing.visible = false;
  updateSculptInfo();
});

canvas.addEventListener('pointerdown', (ev) => {
  if (!state.sculptMode || ev.button !== 0 || !mesh.visible) return;
  const cursor = brushCursor(ev);
  if (!cursor) return;
  ev.preventDefault();
  try { canvas.setPointerCapture(ev.pointerId); } catch (_) {}
  activeStroke = {
    radius_mm: +$('brush_radius').value || 6,
    strength_mm: (brush === 'lower' ? -1 : 1) * (+$('brush_strength').value || 1),
    points: [{ u: cursor.u, v: cursor.v }],
  };
  lastStrokePoint = { u: cursor.u, v: cursor.v };
  updateBrushRing(cursor);
  updateSculptInfo();
  sculptRefreshSoon();
});

canvas.addEventListener('pointermove', (ev) => {
  if (!state.sculptMode) return;
  const cursor = brushCursor(ev);
  updateBrushRing(cursor);
  if (!cursor || !activeStroke) return;
  const moved = lastStrokePoint
    ? Math.hypot(cursor.u - lastStrokePoint.u, cursor.v - lastStrokePoint.v) : Infinity;
  if (moved >= 0.006 && activeStroke.points.length < 600) {
    activeStroke.points.push({ u: cursor.u, v: cursor.v });
    lastStrokePoint = { u: cursor.u, v: cursor.v };
    updateSculptInfo();
    sculptRefreshSoon();
  }
});

canvas.addEventListener('pointerleave', () => { brushRing.visible = false; });

['pointerup', 'pointercancel'].forEach((name) =>
  canvas.addEventListener(name, () => endStroke()));

function bindBrushPair(id) {
  const num = $(id), range = $(id + '_r');
  const sync = (v) => {
    const val = Math.min(+num.max, Math.max(+num.min, +v || +num.min));
    num.value = val; range.value = val;
  };
  num.addEventListener('input', () => sync(num.value));
  range.addEventListener('input', () => sync(range.value));
}
bindBrushPair('brush_radius');
bindBrushPair('brush_strength');
segment($('brush_mode'), 'brush', (v) => { brush = v; });

$('sculpt_undo').addEventListener('click', () => {
  if (!state.sculptStrokes.length) return;
  state.sculptStrokes.pop();
  activeStroke = null;
  updateSculptInfo();
  schedule();
});

$('sculpt_clear').addEventListener('click', () => {
  if (!state.sculptStrokes.length && !activeStroke) return;
  state.sculptStrokes = [];
  activeStroke = null;
  updateSculptInfo();
  schedule();
});

function resetSculpt() {
  state.sculptStrokes = [];
  activeStroke = null;
  updateSculptInfo();
}

window.__relief.sculpt = {
  strokes: () => state.sculptStrokes.map((s) => ({ ...s, points: s.points.length })),
  mode: () => state.sculptMode,
  brush: () => brush,
  reset: resetSculpt,
  ring: brushRing,
};

$('reset').addEventListener('click', () => {
  const d = { relief_height: 6, base_thickness: 1, subdivisions: 1024, bend_angle: 60, black_point: 0, smooth: 0 };
  for (const [k, v] of Object.entries(d)) { $(k).value = v; $(k + '_r').value = v; }
  // plate size resets to the depth map's own aspect, 60 mm on the long edge
  state.aspectLock = true;
  state.ratio = state.imageAspect || 1;
  const [nw, nh] = naturalSize(60);
  applySize(nw, nh, 'fit');
  resetSculpt();
  $('ratio_lock').classList.add('on');
  $('ratio_lock').textContent = '\u{1F517}';
  mode = 'emboss'; curve = 'linear'; bend = 'off'; axis = 'xz';
  $('remove_base').checked = false;
  $('bendExtra').hidden = true;
  $('baseField').style.opacity = 1;
  const set = (id, val) => $(id).querySelectorAll('button').forEach((b) =>
    b.classList.toggle('on', b.dataset.mode === val || b.dataset.curve === val ||
      b.dataset.bend === val || b.dataset.axis === val));
  set('mode', 'emboss'); set('curve', 'linear'); set('bend', 'off'); set('axis', 'xz');
  schedule({ frame: true });
});

// ---------------------------------------------------------------- image
async function useFile(file) {
  const buf = await file.arrayBuffer();
  const res = await fetch('/api/upload', { method: 'POST', body: buf });
  const info = await res.json();
  if (info.error) { alert(info.error); return; }
  state.imageId = info.id;
  state.imageAspect = info.width / info.height;
  resetSculpt();                     // brush strokes belong to the previous depth map
  const longEdge = Math.max(+$('width_mm').value || 60, +$('height_mm').value || 60);
  const [nw, nh] = naturalSize(longEdge);
  applySize(nw, nh, 'fit');
  state.ratio = nw / nh;
  updateRatioInfo();
  $('imgInfo').textContent = `${info.width} × ${info.height} px · depth ${info.min.toFixed(2)}–${info.max.toFixed(2)}`;
  const thumb = $('thumb');
  thumb.src = URL.createObjectURL(file);
  thumb.hidden = false;
  $('dropText').hidden = true;

  texture = new THREE.TextureLoader().load(URL.createObjectURL(file));
  texture.colorSpace = THREE.SRGBColorSpace;
  material.map = state.textured ? texture : null;
  material.needsUpdate = true;

  refresh({ frame: true });
}

const drop = $('drop');
$('file').addEventListener('change', (e) => e.target.files[0] && useFile(e.target.files[0]));
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove('over');
}));
drop.addEventListener('drop', (e) => {
  const f = e.dataTransfer.files[0];
  if (f) useFile(f);
});

// ---------------------------------------------------------------- export
document.querySelectorAll('#export button').forEach((btn) => {
  btn.addEventListener('click', async () => {
    if (!state.imageId) return;
    const fmt = btn.dataset.fmt;
    const btns = document.querySelectorAll('#export button');
    btns.forEach((b) => (b.disabled = true));
    const old = btn.textContent;
    btn.textContent = 'Building…';
    try {
      const res = await fetch('/api/export', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ image_id: state.imageId, format: fmt, ...readControls() }),
      });
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      const blob = await res.blob();
      const name = (res.headers.get('content-disposition') || '').match(/filename="([^"]+)"/)?.[1]
        || `relief.${fmt}`;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      btn.textContent = 'Saved ✓';
      setTimeout(() => (btn.textContent = old), 1200);
    } catch (err) {
      alert('Export failed: ' + err.message);
      btn.textContent = old;
    } finally {
      btns.forEach((b) => (b.disabled = false));
      updateExportHint();
    }
  });
});

// ---------------------------------------------------------------- quit
$('quit').addEventListener('click', async (e) => {
  if (!confirm('Stop Relief Studio? The local server will shut down.')) return;
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  try { await fetch('/api/quit', { method: 'POST' }); } catch (_) {}
  await new Promise((r) => setTimeout(r, 400));
  document.body.innerHTML =
    '<div class="bye"><div><strong>Relief Studio stopped</strong>' +
    'You can close this tab. Relaunch the app to start it again.</div></div>';
  controls.dispose();
});

// ---------------------------------------------------------------- updates
function showUpdate(info) {
  window.__relief.update = info;
  $('updateText').textContent = `v${info.latest} available — you have ${info.current}`;
  const btn = $('updateAction');
  if (info.can_install) {
    btn.textContent = 'Download';
    btn.onclick = downloadUpdate;
  } else {
    btn.textContent = 'Release page';
    btn.onclick = () => window.open(
      info.page_url || 'https://github.com/Karnny/relief-studio/releases', '_blank');
  }
  btn.disabled = false;
  $('updateBar').hidden = false;
}

async function checkForUpdates() {
  try {
    const r = await fetch('/api/update');
    if (!r.ok) return;
    const info = await r.json();
    window.__relief.update = info;
    if (info.update_available) showUpdate(info);
  } catch (_) { /* offline is fine — try again next launch */ }
}

async function downloadUpdate() {
  const btn = $('updateAction');
  btn.disabled = true;
  btn.textContent = 'Downloading…';
  try {
    const r = await fetch('/api/update/download', { method: 'POST' });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'download failed');
    btn.textContent = 'Restart to update';
    btn.disabled = false;
    btn.onclick = restartForUpdate;
    $('updateText').textContent = `v${body.staged} downloaded — restart to apply`;
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Download';
    $('updateText').textContent = `Update failed: ${err.message}`;
  }
}

async function restartForUpdate() {
  const btn = $('updateAction');
  btn.disabled = true;
  btn.textContent = 'Restarting…';
  try { await fetch('/api/restart', { method: 'POST' }); } catch (_) {}
  await new Promise((r) => setTimeout(r, 700));
  document.body.innerHTML =
    '<div class="bye"><div><strong>Restarting…</strong>' +
    'Relief Studio will reopen with the new version.</div></div>';
  controls.dispose();
}

$('updateDismiss').addEventListener('click', () => { $('updateBar').hidden = true; });
setTimeout(checkForUpdates, 1500);
setInterval(checkForUpdates, 6 * 60 * 60 * 1000);

// ---------------------------------------------------------------- demo map
function demoDepth() {
  const N = 768, c = document.createElement('canvas');
  c.width = c.height = N;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(N * 0.5, N * 0.42, N * 0.02, N * 0.5, N * 0.42, N * 0.55);
  grd.addColorStop(0, '#ffffff');
  grd.addColorStop(0.55, '#b9b9b9');
  grd.addColorStop(1, '#000000');
  g.fillStyle = grd; g.fillRect(0, 0, N, N);
  // a few "carved" features so the preview reads as a relief
  g.globalAlpha = 0.85; g.fillStyle = '#ffffff';
  g.beginPath(); g.ellipse(N * 0.36, N * 0.45, N * 0.07, N * 0.10, 0, 0, 7); g.fill();
  g.beginPath(); g.ellipse(N * 0.64, N * 0.45, N * 0.07, N * 0.10, 0, 0, 7); g.fill();
  g.beginPath(); g.ellipse(N * 0.50, N * 0.66, N * 0.10, N * 0.05, 0, 0, 7); g.fill();
  g.globalAlpha = 1; g.fillStyle = '#000';
  g.fillRect(0, N * 0.92, N, N * 0.08);
  return new Promise((res) => c.toBlob((b) => res(new File([b], 'demo-depth.png', { type: 'image/png' })), 'image/png'));
}

// ---------------------------------------------------------------- loop
function tick() {
  requestAnimationFrame(tick);
  controls.update();
  renderer.render(scene, camera);
}
tick();

(async function boot() {
  await fetch('/api/defaults').then((r) => r.json()).catch(() => ({}));
  useFile(await demoDepth());
})();
