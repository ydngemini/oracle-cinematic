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

# GPU SIFT — needs a real GL context, so xvfb is required
apt-get install -y xvfb
xvfb-run -a --server-args="-screen 0 1024x768x24" colmap feature_extractor ...
```

`backend/reconstruction_providers.py` now sets `QT_QPA_PLATFORM=offscreen` on all
three COLMAP calls (commit `40aa275`). A pod image that enables
`SiftExtraction.use_gpu` must additionally run under `xvfb-run`.

## 5. Second thing that will bite

The gsplat reference trainer needs three things the docs do not mention:

1. **`viser` and `nerfview` are mandatory even headless** — `simple_trainer.py`
   imports them at module scope, before `--disable-viewer` is parsed.
2. **Clone the examples at the tag matching the installed library.** Installing
   `gsplat` from pip while taking examples from `main` fails on
   `from gsplat.color_correct import ...`.
3. **`examples/datasets/` ships no `__init__.py`**, so `import datasets.colmap`
   resolves to HuggingFace's installed `datasets` package instead. Create an
   empty `__init__.py` and run from the examples directory.

Also: `--data-factor N` expects a pre-downsampled `images_N/` directory to exist.
Use `--data-factor 1` unless you have made one.

## 6. Verified pipeline

This exact sequence solved cleanly on an L4 — **43/43 images registered, 0.51 px
mean reprojection error**:

```bash
apt-get -qq update && apt-get -qq install -y colmap xvfb
X="xvfb-run -a --server-args=-screen 0 1024x768x24"

$X colmap feature_extractor  --database_path db.db --image_path images \
                             --ImageReader.single_camera 1 --SiftExtraction.use_gpu 1
$X colmap exhaustive_matcher --database_path db.db --SiftMatching.use_gpu 1
$X colmap mapper             --database_path db.db --image_path images --output_path sparse
$X colmap model_analyzer     --path sparse/0        # registered images + reprojection error

pip install gsplat viser nerfview splines jaxtyping tensorboard tyro
git clone --depth 1 --branch v$(python -c "import gsplat;print(gsplat.__version__.split('+')[0])") \
    https://github.com/nerfstudio-project/gsplat.git gs
touch gs/examples/datasets/__init__.py
cd gs/examples && python simple_trainer.py default \
    --data-dir /scene --data-factor 1 --result-dir /out \
    --max-steps 7000 --save-steps 7000 --disable-viewer
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
| `RECON_POD_GPU_IDS` | A5000, 3090, 4090, A40 | preference order; RunPod takes the first available |
| `RECON_POD_MAX_COST_USD` | `2.00` | hard per-job ceiling; the pod is killed when it is reached |
| `RECON_POD_MIN_BALANCE_USD` | `1.00` | `available()` refuses below this, naming the balance |
| `RECON_POD_DISK_GB` | `40` | container disk, not VRAM |
| `RECON_POD_TIMEOUT` | `5400` | wall-clock ceiling |

`available()` reads the **live balance**, so an unfunded account reports
"RunPod balance is $-0.05 … add credits" instead of failing mid-job. That is the
same distinction the Regrid fix drew between an expired credential and an outage.
