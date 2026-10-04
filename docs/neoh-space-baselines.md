# Neoh Space — measured baselines (2026-10-03)

Measured, not estimated. Measure every later run against these. Architecture
and rules: [neoh-space.md](neoh-space.md).

## Real capture run (§59/§60)

`backend/scripts/space_real_capture.py` — the production `_process` with a
recording database and a local object store (so no live DB was touched), the
real RunPod pod provider, real photographs.

| | |
|---|---|
| Source | mip-NeRF 360 `room`: 280 real photographs of a real interior, 779×519. **Not a listing and not a phone walkthrough.** |
| Upload size | 73.7 MB (280 JPEGs) → thinned to 150 by sharpness for sequential matching |
| Quality gate | passed in 7.1 s: 0 dropped, warning `soft_resolution` (median short edge 519 px), median sharpness 442 |
| Provider | `runpod_pod`, pod `guzw2wix7sczew`, RTX 4090 class at $0.74/h, 30 000 training steps (`.env`) |
| Pod phases | bootstrap 146 s (COLMAP 29 + gsplat 117) · features 20 s · matching 68 s · mapping 156 s · training 696 s |
| Provider total | 1 219 s (create → SSH → upload → pipeline → download → terminate) |
| Conversion | 0.0 s — the pod returns `.sog` (pass-through) |
| End to end | 1 229.8 s wall (20.5 min) |
| Output | `.sog` 30 681 142 bytes · 2 223 383 Gaussians · 150 camera poses · 35.6 MB point cloud |
| Cost | estimate $0.2484 (1 208 s × $0.74/h); account balance $1.658 → $1.408 after billing settled = **$0.25** (no pod running, $0/h) |
| Pod after run | `GET /v1/pods/guzw2wix7sczew` → **404**; no `neoh-recon-*` pod listed; spend/hr back to $0 |
| Stages recorded | preparing → reconstructing → converting → analyzing → atomic publish (insert + supersede + ready, one transaction) |
| Orientation | axis estimate `floor_plane`, confidence 0.52 — **15.9° off**; floor band not planar (0.134, furniture); corrected from the capture walk plane (planarity 0.26) → verticals vertical (verified in the browser, before/after screenshots) |
| Bounds | dense 4.08 × 1.23 × 1.68 units; full extent 5.8× dense on one axis → `floaters_present` caveat; viewer frames dense bounds |
| Scale | `estimated` from capture-height prior (eye height 0.84 units → 1.73 m/unit, room ≈ 7.1 × 2.1 × 2.9 m) — **not** for measuring; `scale_unknown` caveat |
| Entry view | registered camera 73, inside the room, looking at its centre |
| Floor plan | `unavailable / no_scale_anchor` — refused honestly (no parcel or recorded sq ft for a benchmark capture) |

Defects this run found and fixed before commit: full `bounds` were computed by
rotating two source-frame corners (gave min.y > max.y); the fine-tilt pass had
no answer for a cluttered floor (now falls back to the walk plane, bounded at
20°); stray geometry was not reported (now `floaters_present`).

The golden run of 2026-09-05 (7 000 steps) produced 923 158 Gaussians /
13.3 MB in 861 s. 30 000 steps gives 2.4× the Gaussians and 2.3× the bytes
for 1.4× the time — sharper, but heavier on phones (below).

## Browser (§37/§38/§58)

`scripts/test-neoh-space-playwright.py`, system Chrome 145 headless on an
Intel Iris 540 (ANGLE/OpenGL — a real, weak GPU), dev harness. Assets are
served from memory, so "ready" is decode + engine start, not network.

| | desktop 1440×900 | mid-range phone profile 390×844, touch, DPR 3, 4× CPU |
|---|---|---|
| device assessment | high (pixel ratio 1) | balanced (pixel ratio 1.5) |
| fixture (29 KB `.sog`, 9 270 G) ready | 1.56 s | 2.33 s |
| fixture frame rate | 60.2 fps | 60.4 fps |
| real capture (30.7 MB `.sog`, 2.22 M G) ready | 16.7 s | 12.5 s |
| real capture frame rate | 25.9 fps | 32.2 fps |
| JS heap after 5 open/close cycles | 85.4 → 86.5 MB (+1.1) | 85.3 → 76.3 MB (−9.1) |
| canvases left after close | 0 | 0 |
| fallback (incapable device) | card + property page intact, 0 canvases | same, no horizontal overflow |
| error (asset 404) | product-language message, property page intact | same |
| legacy `.splat` | renders via gsplat engine, no parser error | same |

Headless Chrome *without* `--enable-gpu` reports SwiftShader; the device
assessment then refuses 3D ("no graphics acceleration") — the fallback path
working as intended on a real software-rendered browser.

**Firefox / WebKit / iOS Safari: not measured.** Playwright's patched builds
are not installed and the root disk has <300 MB free.

## Reading the numbers

* A 2.2 M-Gaussian space takes 12–17 s to become usable on a weak GPU and
  runs at 26–32 fps. That is acceptable on desktop and marginal on phones;
  the governor drops pixel ratio under 24 fps. For phone-first delivery the
  next lever is fewer Gaussians (`RECON_POD_STEPS` 7 000–15 000) or SOG LOD,
  not more effects.
* Cost per space ≈ $0.25 at 30 000 steps on a 4090-class pod.
