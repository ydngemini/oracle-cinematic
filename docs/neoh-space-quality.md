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
| Masks | torchvision Mask R-CNN (COCO): person, cat, dog, bird, tv, laptop, cell phone; score ≥ 0.6; 12 px dilation. Written as `masks/<image>.png`, where 0 means ignore | Moving objects and screens become translucent ghosts. Masks are applied to COLMAP features (`--ImageReader.mask_path`) and to training |
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

## Known limits

- **Mirrors, windows and glass are not masked.** No COCO class covers them. They still reconstruct as "rooms behind the glass".
- **Masks are skipped under patch-crop training** (`patch_size`), because random crops cannot be replayed onto a mask. The default config doesn't patch-crop.
- **Held-out PSNR on masked frames is slightly optimistic,** since masked pixels are black on both sides.
- **The round-trip runs on up to 12 held-out views,** not all of them.
- **The thresholds are provisional** (see above).
