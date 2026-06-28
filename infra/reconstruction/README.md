# Neoh GPU Reconstruction (AWS Batch)

Turns a property's captured photos into a walkable Gaussian splat on a SPOT GPU
that **scales to zero** — you pay only while a house reconstructs (~$0.30–1/house).

## How it fits
```
CaptureWizard → POST /api/crm/reconstruction-jobs
  → reconstruction_worker → AwsBatchProvider:
      upload photos → s3://<bucket>/recon-inputs/<job>/
      batch.submit_job (this image, SPOT g5)         ──► run.sh on the GPU:
                                                          s3 sync images
                                                          COLMAP poses (ns-process-data)
                                                          train splatfacto (Apache 3DGS)
                                                          export .ply → splat-transform → .splat
                                                          s3 cp model.splat → recon-outputs/<job>/
      poll describe_jobs → download model.splat
  → _store_splat (S3): splats/<media_id>.splat (public)  → property_media kind='splat'
  → resolver tier 3 → "Step inside" walk
```

## Files
- `Dockerfile` — CUDA + COLMAP + nerfstudio splatfacto (Apache-2.0) + PlayCanvas
  `splat-transform` (MIT) + awscli. The GPU job image.
- `run.sh` — the pipeline entrypoint (reads `INPUT_S3`, writes `OUTPUT_S3`).
- IaC: `infra/terraform/reconstruction.tf` (S3 bucket, ECR repo, Batch compute
  env/queue/job-def, IAM, app-role grants, outputs). Backend env wired in `ecs.tf`.

## Deploy
1. **Provision** (from `infra/terraform`): `terraform apply` creates the bucket,
   ECR repo, and Batch resources, and wires the backend task env
   (`RECONSTRUCTION_PROVIDER=aws_batch`, `RECON_S3_BUCKET`, `RECON_AWS_BATCH_QUEUE`,
   `RECON_AWS_BATCH_JOBDEF`, `ORACLE_SPLAT_STORAGE=s3`, `ORACLE_SPLAT_S3_BUCKET`,
   `ORACLE_SPLAT_CDN_BASE`). Note the `recon_ecr_url` output.
2. **GPU quota** — request EC2 **SPOT g5/g4dn vCPU** quota in your region (often 0
   by default): Service Quotas → EC2 → "All G and VT Spot Instance Requests".
3. **Build + push the GPU image** (on a machine with the NVIDIA toolchain, e.g. a
   GPU EC2 box or CodeBuild GPU — `tinycudann` needs nvcc):
   ```bash
   ECR=$(terraform output -raw recon_ecr_url); TAG=v1
   aws ecr get-login-password | docker login --username AWS --password-stdin "${ECR%/*}"
   docker build -t "$ECR:$TAG" infra/reconstruction
   docker push "$ECR:$TAG"
   ```
   Set `recon_image_tag = "v1"` in tfvars and re-apply (registers the job def).
4. **Roll the backend** so it picks up the new env: `aws ecs update-service
   --cluster neoh-prod --service backend --force-new-deployment`.

## Use
In the app, open a property → **Create 3D walkthrough** → follow the capture tips,
upload ~20+ overlapping photos → **Generate 3D walkthrough**. The job runs on a
SPOT GPU (one spins up, ~20–60 min, then scales to zero); when it finishes the
**Step inside** button lights up.

## Notes / hardening
- `splats/*` is **public-read** on the bucket so browsers can fetch the splat
  directly (same sensitivity as AI-rendered marketing media). For stricter setups
  front the bucket with **CloudFront (OAC)** and point `ORACLE_SPLAT_CDN_BASE` at
  the distribution; flip the bucket private.
- Capture inputs + job outputs auto-expire after 7 days (lifecycle); served
  `splats/` are kept.
- Licensing: all commercial-OK (COLMAP BSD, nerfstudio/gsplat Apache, splat-transform
  MIT). Do NOT swap in DUSt3R/INRIA-3DGS (non-commercial).
- The `Dockerfile` pins torch 2.1.2/cu121 + nerfstudio; if a future release drifts,
  pin `nerfstudio==<ver>`. Build/test it on a GPU host (no GPU in CI here).
