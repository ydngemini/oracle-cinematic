# Neoh Space

**Neoh Space** is the 3D capability inside a Property: capture a home with a
phone, and Neoh builds a space people can walk through. It is not a separate
app or a top-level product — it lives in Property View (capture) and the
property's tour (viewing), next to photos, the floor plan and listing data,
which always remain available.

This document is the architecture of record (Mission 4, Part I, 2026-10-03):
what exists, the states a build moves through, formats, the honesty rules, and
what is still not proven. Measurements are in [§Baselines](#baselines).

---

## 1. Pipeline

```
Property View ── upload originals (per file, resumable) ──► property_media (photo/video)
   │                                                          (originals: never deleted by 3D)
   └─ "Build 3D space" ── POST /api/crm/reconstruction-jobs ──► reconstruction_jobs row (queued)
                          (cost guard, idempotent)                   │  the row IS the queue
                                                                     ▼
worker process: _claim_next (FOR UPDATE SKIP LOCKED, attempts+1) ─► _process
   preparing     gather originals → video frames (ffmpeg 2 fps)
                 capture_quality.assess: refuse bad captures BEFORE any GPU
                 (too few / blurred / dark / duplicate / tiny / unreadable),
                 drop duplicates + worst-blurred (bounded), warnings
   reconstructing PodProvider (RunPod pod): sharpest-per-bucket thin to 150 →
                 one tar over SSH → COLMAP (sequential matcher) → gsplat →
                 .sog + cameras.json (trained frame) + points.ply → terminate.
                 Pod id persisted the moment it exists; cost recorded.
   converting    .sog passes through; PLY/SPZ → splat-transform 3.3.0 → .sog.
                 On failure the RAW output is preserved for a no-GPU retry.
   analyzing     scene.json v2 (world up, fine tilt, scale, bounds, entry view,
                 navigation), stored beside the asset; floor plan derivation
                 (never fails the build)
   ready         ATOMIC publish: insert media row + supersede previous space +
                 job ready, in ONE transaction
```

Modules: `reconstruction_worker.py` (orchestration), `reconstruction_providers.py`
(providers; `PodProvider` is production), `capture_quality.py` (gate),
`frame_selection.py` (sharpness), `capture_sidecars.py` (poses + point cloud
contract), `scene_manifest.py` (scene.json), `space_status.py` (state machine +
customer language), `space_assets.py` (signed delivery, deletion),
`tour_api.py` (routes + tour resolver), `floorplan_pipeline/` + `floorplan_api.py`.

Frontend: `CaptureSessionPanel.jsx` (capture guide, build, stage list, resume,
retry), `PropertyViewTab.jsx` (per-file upload + retry), `TourViewer.jsx`
(asset composition, device gate, data gate, error boundary, fallback),
`PropertyTourViewer.tsx` (PlayCanvas, quality governor, context loss),
`WalkableSplatViewer.jsx` (gsplat engine, legacy `.splat`), `lib/tour/*`.

## 2. States

One state machine (`space_status.py`), stored as `reconstruction_jobs.stage`.
`status` stays the coarse claim key and is kept consistent by one writer.

| stage | customer label | notes |
|---|---|---|
| uploading | Uploading | client-side only (per-file upload) |
| queued | Waiting to start | durable row; survives restarts |
| preparing | Processing capture | gather, quality gate, frame selection |
| reconstructing | Building your space | the GPU step; 15–40 min typical |
| converting | Preparing walkthrough | delivery format |
| analyzing | Finishing touches | orientation, scale, entry view, floor plan |
| ready | Ready | published atomically |
| needs_attention | Needs attention | e.g. conversion failed; raw output kept |
| failed | Couldn't build this space | with category + guidance |

**Progress honesty.** No provider exposes a real percentage, so none is shown:
the API returns `steps` (done / current / pending) and a label. `progress` is
no longer returned to customers.

**Customer vs operator.** `GET /api/crm/reconstruction-jobs/{id}` returns the
public view — no provider name, pod id, raw error or toolchain words — for
agents; brokerage owners and platform admins additionally get provider, cost,
pipeline version and diagnostics (`/diagnostics` is operator-only).

`stage_of()` treats `status` as authoritative whenever it is not `running`, so
a sweep that fails a row by status alone (reconciliation's stalled pass) never
leaves the stage claiming "building".

## 3. Durability and recovery

| situation | what happens |
|---|---|
| worker restart mid-build | startup sweep: the recorded pod is terminated **now** (`provider_job_id`), then the job is re-queued while `attempts < RECON_MAX_ATTEMPTS` (2), else `failed / orphaned` |
| lost provider / stalled job | heartbeat every 60 s keeps `updated_at` fresh; reconciliation fails rows with no heartbeat for `ORACLE_RECON_STALL_HOURS` |
| leaked pod | `reap_stale_pods` at startup + every 30 min (age > 2×timeout+30 min) |
| provider failure | `failed / provider`, originals intact, "Try again" |
| conversion failure | `needs_attention / conversion`, raw output preserved under `splats/{tenant}/raw/`, `POST …/{id}/retry` reconverts with **no GPU** |
| floor plan failure | space still READY; `floorplan_unavailable` caveat |
| 3D failure | photos, floor plan and listing untouched; retry / recapture offered |

The job row is the queue (`FOR UPDATE SKIP LOCKED`); nothing critical lives
only in asyncio memory. One worker process runs builds (see the ASSUMPTION in
`fail_orphaned_jobs`).

## 4. Cost guard and concurrency

`POST /api/crm/reconstruction-jobs`, in order:

1. active subscription (dependency);
2. `idempotency_key` (query) — same key, same job;
3. one active build per property (route check + partial unique index, 0126) —
   a second tap returns the first job;
4. replacing a published space needs `confirm_rebuild=true`; the current space
   stays visible until the new one is ready;
5. `RECON_RERUN_LIMIT_PER_DAY` (3) full rebuilds per property per 24 h → 429
   (quality-gate refusals and conversion retries are free and not counted);
6. bounded queues: `RECON_QUEUE_MAX` per brokerage (RLS count) and
   `RECON_GLOBAL_QUEUE_MAX` platform-wide → 503 with Retry-After.

Concurrency on the GPU is `RECON_WORKER_COUNT` (1). The worker runs in the
worker process role, so interactive Neoh/CRM traffic is not on its event loop.
Per job: `RECON_POD_MAX_COST_USD` and `RECON_POD_TIMEOUT` bound one run, and
upload/train/download share one budget clock.

Recorded per job (operators only): provider, `provider_job_id`, `gpu_seconds`,
`cost_estimate_usd` (quoted hourly × create-to-terminate wall clock — an
estimate, labelled so), `output_bytes`, `pipeline_version`, per-stage diagnostics.

## 5. Formats

* **Delivery: `.sog`.** splat-transform cannot write `.splat` in any released
  version; `.sog` is ~10× smaller than PLY and PlayCanvas renders it.
* **Legacy `.splat`** (pre-`.sog` spaces, the dev stub's demo room) renders in
  the **gsplat** engine: PlayCanvas 2.21 has no `.splat` parser ("No parser
  found for resource") — found by the browser harness and fixed in
  `TourViewer` by routing `.splat` to `WalkableSplatViewer`.
* **PLY** is training output and is refused by the viewer.
* Companions stored under the asset key: `.cameras.json` (poses, trained
  frame), `.points.ply` (x/y/z/opacity), `.scene.json` (v2), `{id}.json`
  (provenance + AI disclosure + pipeline version).
* `PIPELINE_VERSION` (e.g. `space-2026.10.1+st3.3.0+scene2`) is stamped on the
  job, scene.json and the provenance manifest. Bump it when gating, frame
  selection, conversion, scene schema or delivery change.

## 6. scene.json v2 — honesty rules

| field | rule |
|---|---|
| `worldUp`, `worldUpSource`, `worldUpConfidence` | from floor/ceiling mass + camera path (`estimate_up_axis`); `assumed` when no estimate |
| `tiltCorrection` | bounded refinement. Floor plane first: applied only if planar, 0.5°–5°, and not contradicted by a level capture walk. If the floor cannot be fitted (clutter — the normal case), the **capture walk plane** (planarity ≤ 0.3, ≥ 20 poses) corrects up to 20°. Every refusal has a reason (`already_level`, `exceeds_bound`, `camera_path_disagrees`, `camera_tilt_exceeds_bound`, …). On the first real v2 capture the axis estimate was 15.9° off and the walk plane fixed it |
| `bounds` / `denseBounds` | full extent (taken from the rotated points — rotating two corners was wrong) vs 2%-trimmed extent; the viewer frames and clamps to dense bounds, so one floater 400 m away does not set the scale; `strayExtentRatio` > 2 adds the `floaters_present` caveat |
| `entryCamera` | a registered capture position inside the space with room ahead, mid-sequence preferred; `entryCameraSource` says how; curated override via `PUT /crm/space/{media_id}/scene-override` |
| `navigation.eyeHeight` | the capture's own camera height in scene units — the viewer walks at this, never an assumed 1.6 "m" |
| `scale` | `metric` **only** from a measured source (device AR/LiDAR metadata — none captured today — or an operator calibration with a stated basis); `estimated` from the capture-height prior (navigation only, `measurementsAllowed: false`); otherwise `unknown`. The viewer shows "Measurements unavailable — scale unknown" / "Approximate scale — not for measuring" |
| `rooms`, `roomGraph` | `null` — never guessed from a point cloud. Room names come from a floor plan when one exists (`floors[].rooms`) |
| `limitations` | deterministic caveats: `camera_poses_missing`, `orientation_uncertain`, `scale_unknown`, `entry_view_default`, `floaters_present`, plus `floorplan_unavailable` from the worker |

v1 manifests are still read, marked `legacy`, with scale reported `unknown` —
not silently reinterpreted. Missing poses never produce a silent READY: the
space is READY *with* the stated caveat "Starting view and floor plan
unavailable for this capture".

**Quality is evidence, not a score.** Warnings from the gate (`thin_capture`,
`soft_resolution`, `dark`, `blurry`, `duplicates`, `mixed_orientation`,
`unreadable`) and limitations from analysis become plain caveats on a READY
space. There is no uncalibrated "93/100".

**Floor plans** keep their own measured / estimated / inferred provenance. A
plan derived from a reconstruction is anchored (parcel footprint or recorded
sq ft), recorded as `estimated_from_anchor`, and halved in confidence when the
cross-check disagrees. With no anchor the plan is refused, honestly.

## 7. Capture and upload

The capture guide (`lib/tour/captureGuide.ts`) is five plain steps — Start,
Move through the property, Cover everything, Finish, Upload — with no tool or
format words. Uploads go **one file per request, in order**: completed files
stay uploaded, failed files are listed by name with the reason and retried on
their own. Nothing heavy runs on the phone during capture.

Recapture guidance on refusal is concise and specific (walk slower, more
light, cover transitions, full loop, full resolution, keep moving).

## 8. Delivery, devices and fallback

* **Signed streaming.** The resolver returns `splat_stream_url`: a 10-minute
  HMAC URL for one media id + tenant (`space_assets.py`), served by
  `GET /api/space/assets/{id}` with Range support and `Cache-Control: private`.
  PlayCanvas fetches it directly (real progress, HTTP cache, no full copy in
  JS memory). Without it, the JWT-protected `/api/media/{id}` path is used.
  Nothing lists a bucket or exposes a storage URL.
* **Device assessment** (`lib/tour/deviceCapability.ts`) before any engine
  loads: no WebGL or a software rasteriser → fallback card; low memory/cores →
  `performance`; touch/≤4 GB/WebGL1 → `balanced`; otherwise `high`. Pixel
  ratio caps 1 / 1.5 / 2.
* **Data gate.** On Data Saver or a 3G-class connection the space is not
  downloaded until the person taps "Load the 3D space".
* **Frame-rate governor** (`lib/tour/renderQuality.ts`) lowers the pixel ratio
  after 2.5 s under 24 fps; recovers only to the starting level after 12 s
  above 55 fps.
* **Context loss** is caught; if the GPU context is not restored in 5 s the
  viewer reports `onUnavailable` and the fallback card appears.
* **Error boundary** around every renderer and lazy chunk: a crash shows
  "3D view unavailable on this device." with the photo count, room list and
  "Back to property" — the Property page is never taken down.
* **Teardown** releases assets, destroys the PlayCanvas app and loses the
  WebGL context explicitly (measured: no heap growth across open/close cycles).
* 3D stays lazy: Home never loads PlayCanvas or gsplat (`bundle:check`).

## 9. Navigation and collision

Orbit (default) and Walk (`F`/`O`, buttons), drag to look, WASD/arrows,
pinch/scroll zoom, touch joystick. The camera is clamped to dense bounds
(+ margin), Walk holds the eye at the capture height above the floor, and the
Orbit eye never sinks below the floor (`minEyeAboveFloor`).

### Walls (Walk mode) — built from the splat, never from the floor plan

Derived floor-plan walls are unreliable on real captures (gaps make rooms
leak, clutter reads as wall), so colliding against them would block real
doorways and let people through real walls. Walls come from the **splat
itself**: where the reconstruction put dense, opaque Gaussians.
Code: `lib/tour/collision.ts` (pure, no engine), `lib/tour/splatSamples.ts`
(reads centers + opacity out of PlayCanvas / gsplat.js by duck typing).

**Grid.** A 2D occupancy grid in the canonical floor plane. Sizes are in
"nominal metres" derived from scene.json `navigation.eyeHeight` (taken as
1.55 m), never from an assumed scale:

| setting | value | why |
|---|---|---|
| cell | 0.10 | an 80 cm doorway stays ~8 cells open |
| body band | 0.30–1.80 above `floorHeight` | above floor noise, rugs, thresholds; below door headers |
| body radius | 0.15 | small on purpose — a wide body closes real doorways |
| opacity | ≥ 0.3 | haze and floaters are translucent |
| solid cell | opacity-weighted count ≥ max(3, 2% × p95 of occupied cells) | relative because density varies 20× between captures; 2% because a textured bookcase gets 10–20× the Gaussians of a plain white wall, and the wall must still count |
| height spread | weighted std of heights ≥ 0.05 | a wall/sofa back has vertical extent; a thin horizontal sheet (residual floor tilt, sagging ceiling, haze layer) does not |
| area opening | 8-connected solid blobs < 4 cells dropped | a stray clump is not an invisible wall (no erosion: it would delete 1-cell walls) |
| plausibility | > 50% of the dense footprint solid → no grid | what a wrong up axis looks like |

Splats are transformed by `canonicalTransform` (centers are in the source
frame). For `.sog`, opacity is the sh0 texture's alpha, read back from the GPU
once (async PBO readback) and decoded exactly as PlayCanvas does (v2: a/255;
v1: sigmoid of the quantised logit). If the readback fails, the grid is built
from centers alone and says `opacity none`. Legacy `.splat` (gsplat engine)
uses the RGBA alpha; that engine walks in the source frame, so it queries the
grid through the same transform.

**Movement.** The proposed step is cut into ≤ half-cell sub-steps (no
tunnelling on a slow frame), the body circle is tested against the grid, and a
blocked sub-step is replaced by the free one making the most progress (axis
slides, then ±30°/±60° projected slides) — sliding along walls rather than
stopping dead. **Never trapped:** if the body already overlaps solid cells
(spawned inside furniture, bad data), any position no deeper in is allowed, so
it can always walk out. Outside the grid nothing is solid. Orbit mode and
other floors (`floors[].y` ≠ `floorHeight`) are unconstrained.

**When.** Built after the Space is already interactive (750 ms after
`ready`), in 65 536-splat slices that yield to the event loop; walking works
unconstrained until the grid arrives. Measured: 2.22 M synthetic splats in
77 ms (one go) / 98 ms wall in 18 slices, longest slice 4.8 ms, on an i5-6360U
(Node 20 / V8, same engine as Chrome); the 9 270-splat fixture in 25–65 ms
including the GPU readback, in headless Chrome on Iris 540. The viewer exposes
`data-space-collision` (`none` / `building` / `on` / `unavailable`) and
`data-space-collision-detail` (timings, splat counts, solid cells, reason).

**Fallback.** No `floorHeight` or no `eyeHeight` (no scene.json, v1, missing
poses) → `none`: nothing is invented, the old clamp-only walk applies. No
splat centers, fewer than 500 band splats, a grid that would need cells > 2×
nominal, nothing solid, or an implausible grid → `unavailable`, same walk.

**Known limits (stated plainly).**
* Not yet tried on a real phone capture or the 2.2 M-Gaussian real run — no
  real `.sog` was available locally; thresholds are reasoned + synthetic, not
  calibrated. Check `data-space-collision-detail` on the first real spaces.
* Glass, mirrors, windows and screens: reconstructed as either nothing (glass
  → walk through) or a "room" behind the mirror (walls appear where none are,
  or the mirror plane is missing).
* Sparse walls in poor captures (plain white walls with few large Gaussians,
  dark areas, unvisited sides) can fall under the threshold → gaps you can
  walk through. Centers only — a Gaussian's footprint (scale) is not used.
* Low, flat things fail the height-spread rule: a table top, a bed's flat
  top, a low coffee table can be walked through; sloped attic ceilings
  likewise do not block.
* Floaters with real vertical extent and ≥ 4 cells survive the opening and
  can block; residual tilt over large floors is assumed handled by
  scene.json's tilt correction.
* One floor only (the scene's `floorHeight`); stairs are not modelled.

### Render model

scene.json `renderModel` (`"antialiased"` | `"classic"`, absent = classic)
sets PlayCanvas's scene-wide `app.scene.gsplat.antiAlias` before any splat is
added (`applySceneRenderModel`): scenes trained with gsplat `--antialiased`
drawn in classic mode render small splats too opaque.

## 10. Privacy and access

* Spaces are tenant-scoped (RLS) and served only through authenticated or
  signed routes. Public/client sharing goes through `client_portal` scopes
  (`tour` scope) and the same resolver, so superseded spaces are never shared.
* `DELETE /api/crm/space?…&confirm=true` deletes every version of a property's
  space and all derived objects (asset, poses, point cloud, scene, provenance,
  preserved raw outputs). Originals are **not** touched there: source media is
  deleted only by media deletion or the privacy lifecycle. Tenant erasure
  already removes the whole `splats/{tenant}/` prefix.
* Captures can contain people, family photos, documents and plates. The
  quality gate computes only luminance statistics and a 16×16 thumbnail —
  nothing about faces or content is computed or kept. Automatic blurring is
  **not** implemented (see Gaps); the capture guide asks for nobody in frame.

## 11. Testing

* Backend: `tests/test_neoh_space_pipeline.py` (worker `_process` end to end
  on generated JPEGs: gate, stages, atomic publish, provider id, cost,
  conversion-failure → no-GPU retry), `test_neoh_space_routes.py` (cost guard,
  idempotency, customer-safe status, retry, delete, signed URLs + Range),
  `test_neoh_space_scene.py` (tilt, scale honesty, v1 legacy, overrides),
  `test_reconstruction_orphans.py` (resume / release).
* Frontend (vitest): `TourViewer.test.jsx` (fallback, error boundary, context
  loss, streaming, data gate, legacy `.splat` routing),
  `CaptureSessionPanel.test.jsx`, `PropertyViewTab.test.jsx` (per-file upload
  retry), `lib/tour/neohSpace.test.ts` (device, governor, units, floor guard),
  `lib/tour/collision.test.ts` (splat-built walls: doorway room, sliding,
  floaters, haze, thin sheets, escape from inside solid, empty input,
  frames, async build; .sog opacity decode; render model).
* Browser: `scripts/test-neoh-space-playwright.py` against the dev-only
  `oracle-app/neoh-space-harness.html` (real TourViewer + PlayCanvas, fixture
  `scripts/fixtures/neoh-space-demo-room.sog`, generated by our own stub; no
  GPU job). Desktop and a mid-range phone profile (390×844, touch, 4× CPU).
* Real capture: `backend/scripts/space_real_capture.py` runs the production
  `_process` against real photos and the real provider with a recording DB
  and local object store, then verifies pod termination through the RunPod API.

## Baselines

See `docs/neoh-space-baselines.md` for the measured numbers of the real
capture run and the browser harness.

## Gaps (not done, stated plainly)

* **No phone walkthrough of a real house has been reconstructed.** The real
  run (2026-10-03, baselines doc) used mip-NeRF 360 `room` photographs — a real
  interior, not a listing — and succeeded end to end for $0.25. COLMAP
  registered only 18–37% of earlier real walkthrough frames; that is a
  capture-quality gap, not a code gap.
* **Heavy spaces on phones.** 30 000 training steps → 2.2 M Gaussians /
  30.7 MB, 12–17 s to usable and 26–32 fps on a weak GPU. No LOD / progressive
  SOG yet.
* **Metric scale from the capture itself** (ARKit/ARCore/LiDAR metadata) — the
  hook exists (`measured_scale`), no capture app sends it yet.
* **Room semantics / room graph** — not inferred; only floor-plan rooms are
  listed. `focus_room`-style Neoh camera actions are therefore not offered.
* **Guided tour over a splat** — the guided route exists for 360 scenes only.
* **Face / plate / document blurring** — not implemented.
* **Cross-browser** — Chrome measured; Firefox and WebKit need Playwright's
  patched browsers, which are not installed (root disk full); Safari/iOS
  untested on hardware.
* **GPU memory** is not directly measurable from the page; JS heap and live
  canvas count are.
* `property_view_api` scan ingest inserts a splat row without superseding; the
  resolver now opens the newest current capture, so this is cosmetic.
