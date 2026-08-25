# RunPod pods — reconstruction runbook

Everything needed to produce a real Gaussian splat, once credits are added.
Prices and account state measured live 2026-08-23.

**This is the pods path, not serverless.** RunPod *serverless* is a dead end here:
workers never left `initializing` across every image size and endpoint, the
endpoint was deleted 2026-08-14, and `RunPodProvider` in
`backend/reconstruction_providers.py` targets that surface. A pod is a rented VM
you exec into — different mechanism, and the one that has never been tried.

---

## 1. The account to fund

| | |
|---|---|
| Account | **`ydnop@ydnhft.com`** |
| Balance | **−$0.05** |
| Endpoints / pods | 0 / 0 — nothing is running or draining |
| API key | already in gitignored `.env` as `RUNPOD_API_KEY`, verified working |

Add credits at <https://www.runpod.io/console/user/billing>. Nothing else about
the account needs changing; the key authenticates fine already.

## 2. What to rent, and what it costs

Peak VRAM is the splat training step (~12–22 GB, scaling with gaussian count).
COLMAP itself is light (1–3 GB). So **24 GB is the comfortable tier** and 16 GB
is a risky floor.

Prices re-measured live 2026-08-23 against the GraphQL `gpuTypes` query. An
earlier revision of this table called the 3090 the cheapest 24 GB card; the
**A5000 undercuts it at $0.160/hr**, and `PodProvider` now tries it first.

| GPU | VRAM | $/hr | Note |
|---|---|---|---|
| RTX A5000 | 24 G | **$0.160** | cheapest 24 GB — best value here |
| RTX 3090 | 24 G | $0.220 | |
| RTX 3090 Ti | 24 G | $0.270 | |
| RTX 4090 | 24 G | $0.340 | fastest of the 24 GB tier |
| A40 | 48 G | $0.350 | headroom for very large captures |
| L4 | 24 G | $0.440 | same card the Colab run used |

**Budget:** one full reconstruction is roughly 60–90 min of wall clock
(setup ~10 min, COLMAP ~15 min, training ~30–45 min). On a 3090 that is about
**$0.25–$0.35 per property**. **$10 covers roughly 30 reconstructions** and is a
sensible first top-up.

Spot equals on-demand at these tiers right now, so there is nothing to gain by
bidding and a real risk of pre-emption mid-training — use on-demand.

## 3. Provisioning

Console → Pods → Deploy, or the API. Settings that matter:

- **GPU**: RTX 3090 (24 GB)
- **Container disk**: **40 GB minimum.** The reconstruction image is large and
  COLMAP writes a sizeable working set. This is *disk*, not VRAM — a distinction
  that has caused confusion before.
- **Image**: `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`, or your
  own `ghcr.io/ydngemini/neoh-recon-runpod:latest` (public on GHCR).
- **Volume**: optional. Only worth it to keep datasets between runs.

## 4. The one thing that will bite

**COLMAP cannot start on a headless box without help.** It links Qt and builds a
QGuiApplication even for CLI subcommands, so with no display it aborts with
SIGABRT inside `createPlatformIntegration()` *before reading a single image* —
and the failure looks like a corrupt capture, not a missing display.

Verified on a headless L4 2026-08-23: `colmap feature_extractor` exited rc=-6 in
0s. With a virtual display it processed 43 images in 27s.

Two fixes, and they are not interchangeable:

```bash
# CPU paths — enough on its own, no extra process
export QT_QPA_PLATFORM=offscreen

# GPU SIFT — needs a real GL context, so a display server is required.
# Start Xvfb DIRECTLY. Do not use xvfb-run.
apt-get install -y xvfb
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
for _ in $(seq 1 30); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 1; done
colmap feature_extractor ...
```

**`xvfb-run` does not work on this image.** It is a `/bin/sh` wrapper that dies
with `/usr/bin/xvfb-run: 184: 0: not found` *before COLMAP is reached* — a live
pod run failed there 2026-08-24. Owning the server directly removes the wrapper,
and clearing the stale lock means a recycled pod does not inherit one.

`backend/reconstruction_providers.py` does exactly the above and waits on the
X11 socket rather than sleeping a fixed interval.

## 5. Second thing that will bite

The gsplat reference trainer needs several things the docs do not mention:

1. **Install its dependencies from the cloned tag's own `examples/requirements.txt`.**
   A hand-written list is how this breaks: one omitted `cv2`, `pycolmap`,
   `imageio`, `torchmetrics`, `fused_ssim`, `sklearn`, `matplotlib` and `yaml`,
   and the job discovered that *after* 27 minutes of feature extraction,
   matching and mapping. Note `pycolmap` must be the **rmbrualla fork** pinned in
   that file — PyPI's package of the same name exposes a different API.
2. **Clone the examples at the tag matching the installed library.** Installing
   `gsplat` from pip while taking examples from `main` fails on
   `from gsplat.color_correct import ...`.
3. **`examples/datasets/` ships no `__init__.py`**, so `import datasets.colmap`
   resolves to HuggingFace's installed `datasets` package instead. Create an
   empty `__init__.py` and run from the examples directory.
4. **`--save-ply` is required, and `--save-steps` is not it.** `save_ply`
   defaults to `False` and `--save-steps` controls `.pt` checkpoints. Without it
   a run trains every step, reports its metrics, renders its trajectory video,
   exits 0 — and writes nothing the converter can read. Pass
   `--save-ply --ply-steps <max-steps>`.

Also: `--data-factor N` expects a pre-downsampled `images_N/` directory to exist.
Use `--data-factor 1` unless you have made one.

## 5b. Third thing that will bite: the `.sog` writer

The pod image ships **no node and no npm at all**, and `splat-transform` writes
`.sog` through **WebGPU**, which needs a **Vulkan loader**. Two separate live
failures, both at the final line of a job that had already paid for the GPU:

```
/workspace/run.sh: line 14: npm: command not found
```
```
Couldn't load Vulkan: libvulkan.so.1: cannot open shared object file
TypeError: Cannot read properties of null (reading 'features')
    at WebgpuGraphicsDevice.createDevice
```

```bash
apt-get install -y libvulkan1 mesa-vulkan-drivers   # llvmpipe: no NVIDIA ICD needed
curl -fsSL https://nodejs.org/dist/v22.23.2/node-v22.23.2-linux-x64.tar.xz \
  | tar -xJ -C /opt/node --strip-components=1        # apt ships node 12; needs >= 22
npm install -g @playcanvas/splat-transform@3.3.0
```

**Check the operation, not the tool.** `command -v splat-transform` was true on
the pod that failed — the binary existed, it just could not write a `.sog` on
that machine. The pipeline now converts one throwaway gaussian before any GPU
work starts: three seconds, and it exercises node, splat-transform and Vulkan
together.

## 6. Verified pipeline

Solved cleanly on an L4 — **43/43 images registered, 0.51 px mean reprojection
error** — and again on a 4090 2026-08-24 with 60 images of a real room:
**59/59 registered, 16,143 points, 0.51 px**, training to loss 0.029 / PSNR 23.16
over ~735k gaussians.

Sections 4, 5 and 5b are all folded in here. **Everything the job needs is
installed and PROVEN before the GPU work starts** — three separate live runs
died at the last line on a missing dependency, each after paying for the whole
reconstruction.

```bash
# --- everything the job needs, up front -------------------------------------
apt-get -qq update
apt-get -qq install -y colmap xvfb libvulkan1 mesa-vulkan-drivers

export QT_QPA_PLATFORM=offscreen
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x24 &                    # NOT xvfb-run; see section 4
export DISPLAY=:99
for _ in $(seq 1 30); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 1; done

pip install -q gsplat==1.5.3
git clone --depth 1 --branch v1.5.3 \
    https://github.com/nerfstudio-project/gsplat.git gs
touch gs/examples/datasets/__init__.py
pip install -q -r gs/examples/requirements.txt      # the source of truth
( cd gs/examples && python -c "import simple_trainer" ) || exit 1   # fail in 3 min, not 27

curl -fsSL https://nodejs.org/dist/v22.23.2/node-v22.23.2-linux-x64.tar.xz \
  | tar -xJ -C /opt/node --strip-components=1
export PATH=/opt/node/bin:$PATH
npm install -g @playcanvas/splat-transform@3.3.0
splat-transform smoke.ply smoke.sog || exit 1       # proves Vulkan, not just PATH

# --- now spend the GPU -------------------------------------------------------
colmap feature_extractor  --database_path db.db --image_path images \
                          --ImageReader.single_camera 1 --SiftExtraction.use_gpu 1
colmap exhaustive_matcher --database_path db.db --SiftMatching.use_gpu 1
colmap mapper             --database_path db.db --image_path images --output_path sparse
colmap model_analyzer     --path sparse/0        # registered images + reprojection error

cd gs/examples && python simple_trainer.py default \
    --data-dir /workspace --data-factor 1 --result-dir /workspace/out \
    --max-steps 7000 --save-steps 7000 \
    --save-ply --ply-steps 7000 --disable-viewer   # --save-ply is NOT optional
```

**Image count drives cost.** Exhaustive matching is O(n²): 128 images is 8,128
pairs and took >40 min when GPU matching fell back to CPU; 43 images is 903 pairs.
Subsample to 40–60 views unless the capture genuinely needs more.

## 7. Wiring it back into Oracle

**This is automatic now — `PodProvider` in `backend/reconstruction_providers.py`
does the whole lifecycle unattended:** create pod → wait for SSH → push images →
run the pipeline → pull `model.sog` → **terminate**. Set
`RECONSTRUCTION_PROVIDER=runpod_pod`. Termination is in a `finally` and is also
swept by `PodProvider.reap_stale_pods()`, because a pod bills by the hour whether
or not it computes and nothing in the product surfaces a leaked one.

Note the output is **`.sog`, not `.splat`**. splat-transform lists `.splat` as
input-only in every released version, so the `splat-transform "$PLY" model.splat`
step this runbook used to recommend fails on every real run — which is why no
reconstruction ever completed end to end. `.sog` is what the tool writes and what
the PlayCanvas engine renders.

Only for a manual one-off — pull `model.sog` off the pod and register it:

```sql
INSERT INTO property_media (id, tenant_id, lead_id, kind, url, s3_key,
                            content_type, provenance, generator)
VALUES (gen_random_uuid(), :tenant, :lead, 'splat', :url, :key,
        'application/octet-stream', 'captured', 'runpod-pod');
```

(`kind` stays `'splat'` — it names the kind of media, not the container.)

**`provenance` decides everything downstream.** The tour resolver treats only
`captured` as evidence about the property: a `captured` asset reports
`is_this_property: true`, anything else is labelled a demo and the viewer shows a
permanent "not this home" badge on that asset. Set it to `captured` **only** if
the splat was reconstructed from photographs of that address.

Note the labelling is now **per asset**, not per tour — a generated capture
sitting beside genuine 360s no longer marks the whole tour as not-this-property.

### Env

| Var | Default | Notes |
|---|---|---|
| `RECONSTRUCTION_PROVIDER` | — | set to `runpod_pod` |
| `RECON_POD_GPU_IDS` | A5000, 3090, 4090, L4, A40, A6000 | preference order; RunPod takes the first available |
| `RECON_POD_MAX_COST_USD` | `2.00` | hard per-job ceiling in dollars; the binding limit |
| `RECON_POD_MIN_BALANCE_USD` | `1.00` | `available()` refuses below this, naming the balance |
| `RECON_POD_DISK_GB` | `40` | container disk, not VRAM |
| `RECON_POD_TIMEOUT` | `7200` | wall-clock ceiling (max `14400`) |
| `RECON_POD_MIN_MBPS` | `10` | advertised link speed floor at placement; `0` omits the filter |
| `RECON_POD_CLOUD_TYPE` | `SECURE` | or `COMMUNITY` |
| `RECON_POD_TRANSPORT` | `ssh` | or `blob`, for deploys with no outbound 22 |
| `RECON_POD_IMAGE` | `runpod/pytorch:2.4.0-…` | any CUDA image; the pipeline installs what it needs |
| `RECON_POD_STEPS` | `7000` | training steps |
| `RECON_POD_VOLUME_GB` | `0` | persistent volume; only worth it to keep datasets between runs |

`available()` reads the **live balance**, so an unfunded account reports
"RunPod balance is $-0.05 … add credits" instead of failing mid-job. That is the
same distinction the Regrid fix drew between an expired credential and an outage.

### What the job returns

Three files, not one. `model.sog` is the viewer artifact; the other two exist
because `parse_ply` cannot read a byte of `.sog`:

| File | What it is |
|---|---|
| `model.sog` | the splat the tour viewer renders |
| `model.sog.points.ply` | x/y/z + opacity, for the floor plan path to measure |
| `model.sog.cameras.json` | camera centres **in the trained frame** |

**The frame matters and is checked.** gsplat's Parser recentres and rescales the
scene before training, so raw COLMAP centres are *not* interchangeable with the
delivered model. Mixing them does not raise — it returns a confident up axis
pointing somewhere else. The sidecar records its frame and the reader refuses a
mismatch.

`slicing.extract_from_reconstruction_file(path)` consumes them — it resolves the
geometry and picks up the poses on its own — and the worker calls it after every
successful reconstruction.

### The scale anchor

A reconstruction has no scale: COLMAP solves geometry up to a similarity
transform, so the cloud is the right shape and an arbitrary size. The pipeline
refuses without an anchor, because a guessed one multiplies every length and
area by a constant and looks entirely correct doing it.
`reconstruction_scale.resolve_anchor()` finds one from what the CRM already
holds, in this order and for this reason:

1. **Building footprint** (`parcel_footprint_m2`), from the address via licensed
   parcel data or OpenStreetMap. Compared against the convex hull of the
   capture's own slice band — an outline against an outline — which does not
   care how many storeys the building has.
2. **Recorded living area** (`known_total_sqft`, `leads.sqft`). Solved against
   the interior area detected, which is right for a single storey and wrong by
   roughly the storey count for anything taller. `listings` has no square
   footage column, so a listing-backed capture has only route 1.

Both fail the same way on a **partial capture** — one room measured against a
whole building reads too large — and neither can detect it, because a plan
solved against a footprint matches that footprint by construction. So the
finished plan is cross-checked against a second *independent* figure where one
exists; a disagreement past 35% is written into the provenance and halves the
confidence rather than being smoothed over.

No anchor means no plan, and that is the honest outcome.

`ORACLE_FEATURE_RECON_FLOORPLAN=0` turns the whole step off. It never fails the
job: the splat is the deliverable and has already succeeded.

### Cost control, and how it has actually failed

- The budget covers the **whole session** — upload, run and download share one
  clock that starts when the pod does. Only the training run used to be bounded,
  and a machine that took the capture at 17 KB/s would have billed for an hour
  before anything noticed.
- Staging gets a slice of that budget (`POD_UPLOAD_BUDGET_SHARE`, 25%). A pod
  that cannot receive 60 images will not train on them either; the sooner it is
  written off, the sooner a retry lands somewhere healthy.
- **Terminating retries.** Every other call here can fail for free. This one
  cannot: a single connect timeout to `rest.runpod.io` left a pod running after
  its budget guard had correctly fired. Five attempts with backoff, and a 404
  counts as success.
- **The `finally` cannot save you if the process dies.** Two pods leaked that
  way during development (one for 2h17m, about $1.90). The backend's reaper
  sweeps `neoh-recon-*` pods older than `job_ceiling * 2 + 1800`, but it only
  runs inside the worker loop — if you drive the provider from a script, run
  your own sweep. `PodProvider.reap_stale_pods()` is the same call.

```bash
# after any manual run
python -c "import sys; sys.path.insert(0,'backend'); \
  import reconstruction_providers as r; print(r.PodProvider.reap_stale_pods(0))"
```
