# Neoh GPU Reconstruction — RunPod Serverless

A **RunPod Serverless** GPU worker that turns a property's captured photos into a
walkable 3D **Gaussian splat** (`.splat`). It runs the same pipeline as the AWS
Batch worker (`../reconstruction/`) — **COLMAP poses → nerfstudio splatfacto →
PlayCanvas splat-transform** — but is driven by RunPod's serverless contract
instead of AWS Batch. Scales to zero; you pay per-second only while a house
reconstructs.

**Why this exists:** the AWS Batch path needs EC2 **G/VT Spot** vCPU quota
(L-3819A6DF), which has been stuck at 0 with an open support case for days. RunPod
gives us GPUs immediately, with no quota gate. This worker is a drop-in compute
backend — it writes the finished `.splat` to the **same S3 bucket** the app
already reads, so nothing downstream changes.

All licensing is commercial-OK: COLMAP (BSD), nerfstudio/gsplat/tinycudann
(Apache-2.0), splat-transform (MIT). Do **not** swap in DUSt3R / INRIA-3DGS
(non-commercial).

```
CaptureWizard → POST /api/crm/reconstruction-jobs
  → reconstruction_worker → RunPodProvider:
        upload photos → s3://<bucket>/recon-inputs/<job>/
        POST https://api.runpod.ai/v2/<endpoint>/run          ──► handler.py on the GPU:
            {"input": {image_urls, output_put_url, iters}}         pull images via signed GETs
        poll GET .../status/<id> until COMPLETED                    pipeline.sh:
                                                                      COLMAP poses (ns-process-data)
                                                                      train splatfacto (Apache 3DGS)
                                                                      export .ply → splat-transform → .splat
                                                                    upload .splat → recon-outputs/<job>/
        download model.splat from S3
  → _store_splat (S3): splats/<media_id>.splat → property_media kind='splat'
  → resolver tier 3 → "Step inside" walk
```

## Files
- `handler.py` — RunPod serverless handler. Owns bounded presigned-URL transport
  and the job contract; shells out to `pipeline.sh` for compute.
- `pipeline.sh` — the transport-free compute core (images dir → `.splat`).
- `Dockerfile` — CUDA 12.1 runtime + COLMAP + pinned nerfstudio splatfacto,
  Node 22, pinned splat-transform, and the RunPod SDK. It runs as uid 10001.
- `requirements.txt` — exact `runpod` and `requests` pins.
- `.runpod/hub.json` + `.runpod/tests.json` — RunPod **Hub** listing + CI test.
- `test_input.json` — payload for a local `python handler.py` run.

## Job contract

**Input** (`job["input"]`):

| field | type | notes |
|---|---|---|
| `image_urls` | string[] | 8–300 AWS Signature V4 presigned S3 HTTPS GET URLs. |
| `output_put_url` | string | presigned S3 HTTPS **PUT** URL to receive the `.splat`. |
| `return_splat_b64` | bool | selftest-only inline output, capped at 5 MB. |
| `iters` | int | splatfacto iterations (default `RECON_ITERS` env or 7000). |
| `selftest` | bool | skip GPU work; emit a synthetic demo room `.splat` (boot check). |

Production jobs provide `image_urls` and `output_put_url`. The worker rejects
direct S3 URIs, redirects, non-S3 hosts, oversized downloads, and unbounded
iteration counts before starting paid GPU work.

**Output:** `{"gaussians": int, "bytes": int, "splat_put"?, "splat_b64"?, "disclosure"}`.
Failures return `{"error": "..."}` (RunPod marks the job `FAILED`).

## Deploy A — RunPod Hub (from GitHub, the "Create Listing" flow)

The Hub builds and hosts your endpoint from a GitHub repo. It indexes **releases,
not commits**, so you must cut a release.

1. Push this directory to its own GitHub repo (see **Standalone repo** below).
2. In the RunPod console → **Hub → Create Listing**, connect that GitHub repo.
   The Hub reads `.runpod/hub.json` (GPU pool, disk, env inputs) and
   `.runpod/tests.json` (the `boot-selftest` CI test).
3. Create a **GitHub release** (e.g. `v1`). The Hub picks it up, builds the image,
   runs the selftest, and marks the listing `Pending → Published`.
4. Deploy an endpoint from the listing. Note the **Endpoint ID**.

## Deploy B — Manual (build + push + create endpoint)

Build with Docker (the pinned CUDA runtime dependencies use published wheels):
```bash
docker build -t <registry>/neoh-recon-runpod:v1 infra/reconstruction-runpod
docker push  <registry>/neoh-recon-runpod:v1
```
Then RunPod console → **Serverless → New Endpoint**: set the image, a 24 GB GPU
(RTX 4090 / L4 / A10), **Container Disk ≥ 40 GB**, and — critically — raise the
**Execution Timeout** (default is 10 min; a house takes 20–60 min → set ~3600 s).
Do not add AWS credentials to the endpoint. The backend supplies short-lived
presigned URLs for each job.

## Standalone repo (for the Hub)

The Hub needs its own repo. Extract this subtree without disturbing the monorepo:
```bash
git subtree split -P infra/reconstruction-runpod -b runpod-worker
git push git@github.com:<you>/neoh-recon-runpod.git runpod-worker:main
```

## Local smoke test (no GPU)
```bash
pip install -r requirements.txt
python handler.py            # auto-loads test_input.json → selftest demo splat
```
`selftest` skips COLMAP/training, so it validates the handler + transport wiring
without a GPU. A real reconstruction needs the CUDA image and 8+ photos.

## Wire it into Oracle

**Already wired.** `backend/reconstruction_providers.py` has `RunPodProvider`
registered as `_PROVIDERS["runpod"]`, and `ecs.tf` can select it through
`reconstruction_provider = "runpod"`. It uses **presigned S3 URLs**, so only the
backend touches S3. It reuses the same `recon-inputs/<job>` and
`recon-outputs/<job>/model.splat` keys as `AwsBatchProvider`; `_store_splat` and
the tour resolver remain unchanged. Both remote providers cancel timed-out jobs.

Then set the backend env and roll the service:
```
RECONSTRUCTION_PROVIDER=runpod
RUNPOD_API_KEY=...              # RunPod → Settings → API Keys
RUNPOD_ENDPOINT_ID=...          # from the deployed endpoint
RECON_S3_BUCKET=<existing recon bucket>   # reuse infra/terraform/reconstruction.tf's bucket
AWS_REGION=us-east-1
```
The RunPod endpoint receives only `image_urls` + `output_put_url`; all AWS
permissions remain on the ECS task role.

## Notes / hardening
- **Execution timeout**: RunPod's default is 10 min. The provider sends
  `policy.executionTimeout` per request; also raise the endpoint default so the
  console doesn't kill long jobs.
- **Result retention**: async `/run` results are kept only ~30 min after
  completion — fine here because the splat is persisted to S3, not carried in the
  reply.
- **GPU pool**: 24 GB (Ada `8.9` or Ampere `8.6`) is plenty for a house at 7000
  iters. The image is built for both arches.
- **Selftest** is the cheap boot check; it writes a synthetic room splat with no
  GPU/photos and is what the Hub CI test runs.
