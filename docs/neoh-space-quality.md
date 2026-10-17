# Neoh Space — reconstruction quality

Before this, a Space was published as soon as training wrote a non-empty `.sog`. Now every pod run measures itself, and the worker publishes only what passes those measurements. Code lives in three places:

- `backend/recon_pod_tools.py` runs on the pod. It is embedded in the pod script by `reconstruction_providers.render_pod_pipeline`.
- `backend/recon_quality.py` is the gate.
- `reconstruction_worker._assess_quality` applies the gate. Tests: `tests/test_recon_pod_tools.py`, `tests/test_recon_quality.py`, and `tests/test_neoh_space_pipeline.py` (the quality-gate section).

## What runs on the pod, in order

Every step in this table is optional. If one fails, the run skips that measurement or improvement and carries on. A quality tool is never the reason a reconstruction is lost.

| Step | What | Why |
|---|---|---|
| Self-tests, before COLMAP | Render a 1-Gaussian PLY through gsplat; read a `.sog` back to PLY; load the segmenter | Proves the toolchain cheaply. All five early pod failures were found at the last line |
| Masks | torchvision Mask R-CNN (COCO): person, cat, dog, bird; screens (tv, laptop, cell phone) only with `RECON_MASK_SCREENS=1`; score ≥ 0.6; 12 px dilation. Written as `masks/<image>.png`, where 0 means ignore | Moving objects become translucent ghosts. Masks are applied to COLMAP features (`--ImageReader.mask_path`) and to training. Screens are opt-in because the golden capture's switched-off TV was masked in 113 of 150 frames, and a static object with no supervision becomes a hole |
| Lens groups | One COLMAP camera per (image size, EXIF make/model/lens/focal). Groups under 3 frames fold into the main group | Phones switch lenses mid-walk. One forced camera model gives a compromise lens and soft geometry. Video frames have no EXIF and stay one group |
| Training | gsplat `simple_trainer default`, **30 000 steps** (one value everywhere), `--antialiased`, `--use-bilateral-grid`, held-out evaluation at the final step | The bilateral grid normalises exposure and white balance per frame, which removes the foggy look. Antialiasing stops thin detail shimmering and popping |
| Training fallback | If that run fails, the plain proven trainer runs instead (no masks, no new flags) | An improvement can never cost the job |
| Tag | `comment antialiased 1` added to the PLY, so the `.sog` carries `"model": "antialiased"` | gsplat writes no tag, and an antialiased scene drawn classically renders small splats too opaque |
| Floaters | `splat-transform --filter-nan --filter-floaters 0.05,0.1,0.004`. Rejected if it removes more than 25 % | Uses splat-transform's own voxel filter. A filter removing a quarter of the scene isn't removing floaters |
| Round-trip | Render gsplat's held-out views from the raw PLY, the pruned PLY, and the `.sog` decoded back, through one renderer | Separates the three causes of smear (see the next table). If pruning lowers held-out PSNR by more than 0.3 dB, the unpruned scene is delivered |
| Report | `quality.json`, holding only what was measured | Read by the worker |

### What the round-trip comparisons tell you

| Comparison | Measures |
|---|---|
| `raw_vs_photo` | Capture + registration + training |
| `pruned_vs_raw` | What floater removal cost |
| `sog_vs_pruned` | What compression cost. A smeared `.sog` from a sharp PLY means the converter, not the camera |

## The gate (`recon_quality.assess`)

**The thresholds are provisional.** They rest on a handful of real runs: the golden capture at 7k steps gave PSNR 23.16 / SSIM 0.807, and real phone walkthroughs registered 18–37 % of frames. That isn't a sample anyone should calibrate against.

So the **block** lines sit far out, at results nobody would publish. Everything in between becomes a customer caveat plus a recorded number. Tighten the lines only against more measured runs, and log each run's numbers in `docs/neoh-space-baselines.md`.

| Measure | Block (not published) | Caveat (published, labelled) |
|---|---|---|
| Held-out PSNR (colour-corrected when the exposure model is on) | < 16 dB | raw < 22 dB → `quality_low` |
| Held-out SSIM | < 0.55 | < 0.72 → `quality_low` |
| Registration ratio (needs ≥ 20 frames) | < 20 % | < 60 % → `partial_registration` |
| Mean reprojection error | > 4 px | > 1.5 px → `pose_error_high` |
| `.sog` vs PLY on held-out views | — | < 28 dB → `compression_loss` |
| No report, or no held-out metrics | — | `quality_unverified` |

**When a Space is blocked:**
- the job ends `failed_quality_gate` with `quality_gate='reconstruction'`;
- the customer sees recapture guidance: walk slowly, cover doorways, keep people, pets and screens out of shot, turn the lights on;
- the raw output is preserved (`raw_output_key`), because the GPU time is already spent.

**Every number lands in two places:** the job's diagnostics (stage `quality`), and `scene.json` (`quality`, plus `renderModel`, which the viewer uses to switch PlayCanvas's `scene.gsplat.antiAlias`). The delivered `.sog`'s own `meta.json` decides `renderModel`, with the pod report as the fallback.

**Impossible numbers are not measurements.** A value that is not a finite number inside `recon_quality.VALID_RANGES` (PSNR 0–100 dB, SSIM −1–1, LPIPS and registration ratio 0–1, reprojection error and frame count ≥ 0) is listed under `metrics.invalid` and never used: it neither passes nor blocks, and it never reaches the diagnostics (jsonb refuses `Infinity` and `NaN`, which used to lose the whole `quality` stage). A report whose held-out figures are all invalid is `quality_unverified`. `input_images` is recorded beside the ratio.

## Certification (T12.09)

The gate decides what may be **published**. Certification decides what a published space may be **called**, and is separate from the job's READY stage ("ready to view"). Code: `backend/space_certification.py`; tests: `tests/test_space_certification.py`, `tests/test_space_certification_consumers.py`, `tests/test_space_certification_live.py` (real PostgreSQL, FORCE RLS).

The state is **derived on every read** from evidence already stored, never stored itself: the newest build that produced the space (its `diagnostics.limitations`, `diagnostics.quality` and `diagnostics.capture.input_media_ids`) and the property's current photos and videos. A late upload therefore invalidates an old proof without anything being rewritten. No migration was needed.

**Four required checks.** `verified` needs every one to pass, on fresh evidence. A missing metric is missing evidence (CONTRACT C6), never a pass.

| Check | Reads | passed | Otherwise |
|---|---|---|---|
| Held-out image quality | `held_out_psnr`, `held_out_ssim`, `held_out_lpips` (raw); colour-corrected for the block line | all three present, PSNR ≥ 27 dB, SSIM ≥ 0.88, LPIPS ≤ 0.12 (AC12.08 pilot **targets**, not measured on houses) | `failed` under the block lines; `limited` under the caveat lines (`quality_low`, `compression_loss`); `acceptable` in between or with a figure missing; `missing`; `invalid` |
| Registration | `registration_ratio`, `mean_reprojection_error_px`, `input_images` | all three present, ≥ 20 frames, ratio ≥ 60 %, error ≤ 1.5 px (the provisional gate lines; raising them is a calibration and owner question) | `failed` (< 20 % or > 4 px); `coverage_gap` (< 60 %, `partial_registration`); `limited` (> 1.5 px, `pose_error_high`); `missing`; `invalid` (out of range, or < 20 frames) |
| Room connectivity | `quality.room_graph = {rooms, components, edges, verified_edges, expected_rooms}`; `expected_rooms` is the home's own room count (floor plan or declared rooms) | one connected component, every room edge verified, and at least `expected_rooms` rooms captured (a one-room home passes with one room) | `coverage_gap` (> 1 component, an unverified edge, or fewer rooms than the home has); `missing` (no graph, or no `expected_rooms` to compare with: one well-connected room is not the whole home); `invalid` (an impossible graph, e.g. fewer edges than rooms − components, or a non-integer count). **Nothing writes it yet (T12.06), so this is `missing` for every space** |
| Device tour performance | real-device runs per platform | iOS Safari **and** Android Chrome, real devices, steady ≥ 30 fps (spec §7 target) | `limited` below 30 fps; `missing` (emulated or headless runs do not count, nor does a run that does not name this space's `media_id`: one measured on an older build's space says nothing about this one); `invalid` (a frame rate that is not a JSON number, e.g. `"41"` or `true`, or is negative or non-finite). **No producer stores these yet (T12.04, AC12.03), so this is `missing` for every space** |

A published caveat is measured evidence even when its numbers were not kept, so it sets the least-severe status its check may have (`quality_low` → held-out `limited`, `partial_registration` → registration `coverage_gap`); it never raises a status.

**Freshness** (`tour_api._EVIDENCE_COLUMNS`): `true` when the build's recorded input set holds every current capture photo/video of the property (the same predicate as the build's gather, `reconstruction_worker.capture_input_sql`); `false` when one exists that the build never read; for a legacy build with no recorded set, `false` only when a capture row is newer than the build's start, otherwise unknown (`NULL`). Comparing ids rather than times also catches an upload whose transaction began before the gather but committed after it. Unknown is never assumed fresh.

**Capture input** is a photo or video of the property itself: `provenance = 'captured'` (migration 0071), `review_status = 'approved'` (migration 0066) and stored bytes. A generated listing reel (`ai_generated`, video studio) or third-party street imagery (`imported`) is neither reconstructed nor counted for freshness, and neither is a client-link upload an agent has not approved (`pending`) or has `rejected`: it is untrusted and may not be this house. Approving one makes it capture input, so a space built without it reads stale. Rows the build considered but could not use (bytes that failed to load, a video with no frames, bytes the capture gate could not decode as an image, or a row none of whose frames fell inside the gate's sample, see "Known limits") are recorded as `diagnostics.capture.skipped_media_ids`, in the capture stage's one write after the gate. That write is required: if it fails, the build stops before any GPU and publishes nothing; the tour counts the current ones as `evidence_unread`, and any unread capture keeps the space from `verified` ("Some photos or video couldn't be read, so this space was built without them"). Stored measurements are re-validated on every read: a boolean or a numeric string is invalid, not a number.

**States, in the order they are decided** (the first that applies wins):

| # | State | When | Tour badge |
|---|---|---|---|
| 1 | `pending` | no build produced it (phone scan), or its build is running | 3D Walkthrough · quality not verified |
| 2 | `rejected` | the gate refused the build, or a check measured a failure | 3D Walkthrough · failed quality checks |
| 3 | `pending` | the build failed for another reason | — (nothing published) |
| 4 | `incomplete` | a coverage gap (part of the capture or of the house does not connect) | Partial 3D Walkthrough |
| 5 | `degraded` | a measured quality caveat | 3D Walkthrough · reduced quality |
| 6 | `pending` | stale evidence, or image quality never measured | 3D Walkthrough · quality not verified |
| 7 | `incomplete` | a required check missing, invalid or below target, capture the build could not read, or freshness unknown | 3D Walkthrough · quality not verified |
| 8 | `verified` | every check passed on fresh evidence, all of it read | Full 3D Walkthrough |

Measured negatives are said ahead of missing evidence; the reasons list carries everything that applies (for example "Partial" with "photos were added after this space was built"). Only `verified` may say "Full", "verified" or "photoreal … the actual home"; every other note says the quality has not been verified. **With today's stored evidence `verified` is unreachable by construction**, and the tests pin that: the best a space can be today is `incomplete` ("Whether every room was captured and connects to the next hasn't been checked"; "It hasn't been tested on real phones yet").

The job view (`GET /api/crm/reconstruction-jobs/{id}`) reports `certification` beside the stage. It does not read freshness, so a build view is never `verified`, and it does not claim freshness cannot be told (the published space's reading tells it). `GET /api/crm/space` reports the published space's certification, which the capture panel re-reads once a build is ready. On both, operators additionally get each check's status, the values used and the lines applied.

## Proof runs

| Date | Capture | Result |
|---|---|---|
| 2026-10-06 | mip-NeRF 360 `room`, 280 photos at 779×519 → 150, 30k steps, all steps on | **Failed on its last line, $0.30.** `phase ""` returned 1 under `set -e` after every quality step had run (fixed; `tests/test_pod_pipeline_dry_run.py` now executes the whole script). Measured: COLMAP 150/150, 0.50 px; held-out PSNR **29.56 dB** / SSIM **0.914** / LPIPS **0.072** (colour-corrected 30.63 dB); 1.85 M Gaussians; 1 lens group; masks hit 113/150 frames (the static TV, which is why screens are now opt-in). The `.sog` was built but never downloaded |
| 2026-10-06 (run 2) | same capture, same settings, screens not masked | **Published end to end, $0.29**, pod verified gone. Held-out PSNR **28.82 dB** / SSIM **0.890** / LPIPS **0.086** (colour-corrected 29.90 / 0.905); COLMAP 150/150 at 0.51 px; `.sog` vs PLY on held-out views **41.97 dB** (compression is not the softness); floater filter removed 858 Gaussians at 60.3 dB vs raw (no visible cost); 1.90 M Gaussians, 26.7 MB `.sog` (30.7 MB at baseline). The 0.7 dB drop from run 1 is run 1's masked TV inflating its score. Found: splat-transform's filters drop the antialiased tag, so the `.sog` was untagged and the worker wrote `renderModel: classic`. Fixed: the pruned PLY is re-tagged, and untagged now means unknown, with the pod report deciding. Browser (Chrome, Iris 540): ready in 13.1 s desktop / 9.5 s mobile, 28–33 fps; walls built in 292 ms from 1.9 M splats (887 of 120×76 cells solid); walking into a wall stops at it, and with walls off the same walk passes into the void |

**What still softens this scene:** the photos rarely look up, so the **ceiling is never observed** and renders as fog. Neither the floater filter nor the gate can fix unobserved surfaces; the capture has to cover them. The capture itself is 779×519 (`soft_resolution`). The same room exists at 1557×1038 in `neoh-capture-room-hires`, which is the next lever.

## Known limits

- **Mirrors, windows and glass are not masked.** No COCO class covers them. They still reconstruct as "rooms behind the glass".
- **Masks are skipped under patch-crop training** (`patch_size`), because random crops cannot be replayed onto a mask. The default config doesn't patch-crop.
- **Held-out PSNR on masked frames is slightly optimistic,** since masked pixels are black on both sides.
- **The round-trip runs on up to 12 held-out views,** not all of them.
- **The thresholds are provisional** (see above).
- **Nothing can be certified yet.** Room connectivity and real-device tour performance have no producer, so no space reaches `verified` (see "Certification").
- **The capture gate looks at no more than 900 frames** (`capture_quality.MAX_ANALYSED`), evenly spaced, and only frames it looked at go to reconstruction. A capture can exceed that (the gather has no total cap: for example four 240-frame videos), and the frames outside the sample are never decoded or reconstructed. A photo or video none of whose frames fell inside the sample is recorded as unread (`skipped_media_ids`), so a space whose input includes one cannot be `verified`. The sampling itself is unchanged, and an operator sees the gap as `quality_gate.submitted` above `quality_gate.analysed`.
- **Walls stop you close.** The body radius is 0.15 nominal m so that 80 cm doorways stay open, so walking into a wall ends very near it (a blurry close-up). A larger radius would stop further back but risks closing doorways.
- **Unobserved surfaces (usually ceilings) render as fog.** Capture guidance, not processing, fixes them.
