# Infrastructure status: what deploys Neoh, and what does not

**How do I deploy Neoh?** Follow [`deploy-digitalocean.md`](deploy-digitalocean.md).
There is one path: DigitalOcean App Platform, driven by CI. Nothing below
marked LEGACY or ARCHIVED deploys Neoh, and running it will not ship a
release.

| Class | Meaning |
|---|---|
| **ACTIVE PRODUCTION** | Part of how Neoh is built, deployed, verified or operated today |
| **OPTIONAL PROVIDER SUPPORT** | Working code for a provider Neoh *can* use when configured, but does not require |
| **LEGACY** | Describes or drives a retired platform (AWS ECS, Azure Container Apps, RunPod Serverless). Kept as history and a starting point, never run for a release |
| **ARCHIVED** | One-off helpers whose job is done. Kept for the record |

Classified 2026-10-03. When you add an infrastructure file, add a row here.

## ACTIVE PRODUCTION

| Path | What it is |
|---|---|
| `infra/digitalocean/app.yaml` | The one App Platform spec template (api, web, worker, Postgres + Valkey attachment, ingress) |
| `scripts/render-app-spec.py` | Renders `app.yaml` for `production` / `staging`, pinned to digests, and refuses unsafe specs |
| `infra/digitalocean/smoke-test.sh` | Post-deploy check: health, release SHA, worker on this release, callbacks, headers |
| `infra/digitalocean/callback-routes.txt` | Provider webhook URLs the smoke test probes |
| `.github/workflows/ci.yml` | Tests, then `release` (build once, deploy to staging) and `promote` (production, no build) |
| `scripts/build-release-manifest.sh`, `scripts/migration-precheck.sh`, `scripts/spec-drift.py`, `scripts/rollback.sh` | Release identity, migration safety, console-drift refusal, digest rollback |
| `scripts/neoh-launch-readiness.py` | One non-destructive command: PASS / WARN / BLOCKED for every launch requirement |
| `scripts/audit-neoh-production.py` | Authenticated, non-destructive sweep of the current UI (Home / Work / Neoh / Admin) |
| `scripts/backup-postgres.sh`, `restore-postgres.sh`, `verify-restore.sh`, `dr-drill.sh`, `dr-drill-redis.sh`, `reapply-erasures.py` | Disaster recovery ([`disaster-recovery-state-map.md`](disaster-recovery-state-map.md)) |
| `scripts/generate-operator-totp.py`, `scripts/privacy-orphan-audit.py` | Operator second factor; privacy orphan audit |
| `scripts/dev-start.sh` | Local stack (not a deploy) |
| `backend/Dockerfile`, `oracle-app/Dockerfile`, `oracle-app/nginx.conf`, `docker-compose.yml` | The two images, and the local stack |
| `docs/deploy-digitalocean.md`, `docs/staging-setup.md`, `docs/release-checklist.md`, `docs/launch-state.md`, `docs/runbooks/` | How to deploy, set up staging, release, and respond |

## OPTIONAL PROVIDER SUPPORT

| Path / setting | What it is |
|---|---|
| `ORACLE_DB_AUTH=aws-iam\|azure-entra` (`backend/db/connection.py`) | Cloud-IAM database auth. DigitalOcean uses neither (password + project CA) |
| `ORACLE_STORAGE_BACKEND=azure-blob\|azure-files` | Alternate object storage. Production sets `s3` (Spaces) |
| `ORACLE_AI_CHAT_PROVIDER=bedrock\|azure-foundry\|local` | Alternate AI providers. Production uses Fireworks |
| Telnyx (`TELNYX_*`, [`telnyx-hosted-sms-runbook.md`](telnyx-hosted-sms-runbook.md)) | Hosted SMS. Not in the spec until used |
| Plivo (`PLIVO_*`) | Alternate voice carrier. Not in the spec until used |
| Google OAuth (`GOOGLE_CLIENT_*`) | Calendar / Google sign-in. Not in the spec until used |
| RunPod pods (`RUNPOD_API_KEY`, `RECON_POD_*`, [`runpod-pods-runbook.md`](runpod-pods-runbook.md)) | GPU 3D reconstruction. External to DigitalOcean |
| DashScope / Qwen realtime (`DASHSCOPE_*`) | Realtime voice model. **Singapore region by default**: read [`subprocessor-inventory.md`](subprocessor-inventory.md) before enabling |

## LEGACY — retired platforms; do not run for a release

| Path | Was | Banner |
|---|---|---|
| `infra/DEPLOY.md` | AWS ECS deploy runbook | yes |
| `infra/scripts/*` | AWS deploy scripts. They refuse to run without `NEOH_LEGACY_AWS=1` | README yes |
| `infra/terraform/*.tf`, `terraform.tfvars.example` | AWS IaC (Aurora, ECS, ALB, WAF, Route 53) | — (directory covered here) |
| `infra/iam/rds-connect-policy.json` | AWS RDS IAM policy | — |
| `infra/HARDENING.md` | AWS-era hardening map. Its SQL controls still apply | yes |
| `infra/reconstruction/` | AWS Batch GPU worker | yes |
| `infra/reconstruction-runpod/` | RunPod **Serverless** worker (serverless never left `initializing`; pods are used instead) | yes |
| `infra/tests/test_deploy_rollback.sh`, `infra/tests/fake-bin/` | Tests of the legacy AWS deploy scripts | in-file |
| `infra/azure/` | Azure Container Apps deploy (subscription disabled 2026-08-09) | yes |
| `docs/production-blockers.md` | AWS-era blocker list (superseded by [`launch-state.md`](launch-state.md)) | yes |
| `docs/aws-domain-support-case.md` | AWS Route 53 domain support case | yes |
| `observability-platform/` (incl. its `DEPLOY.md`) | AWS observability dashboard subproject | — (outside infra; not deployed) |
| `scripts/bedrock_uplink.py`, `forge_model.py`, `oracle_query.py`, `s3_uplink.py` | AWS Bedrock / S3 training experiments | yes |
| `scripts/qa_local_crm.py` | QA script for the retired five-tab UI | yes |

## ARCHIVED — one-off helpers

| Path | Was | Banner |
|---|---|---|
| `scripts/purchase-domain-cloudflare.sh`, `scripts/purchase-neohr-domain.sh` | Domain purchase helpers. Their DNS step now points at DigitalOcean, not the retired Azure DNS zone | yes |
| `scripts/migrate-docker-root.sh` | Local Docker data-root move | — |

## Local Terraform state, outside git

The main checkout holds **untracked, gitignored** AWS Terraform files from
the retired stack: `infra/terraform/terraform.tfvars`, `infra/terraform/tfplan`,
`infra/terraform/.terraform-root-owned-old/terraform.tfstate`, and
`infra/.tf-root-owned-20260827-072849/terraform.tfstate{,.backup}`. They were
**never committed**: `.gitignore` lines 45–66 cover them, and `git log --all`
shows none.

They were checked on 2026-10-03 for credentials, by key name and pattern only; no values were read out.

- No AWS access keys, Stripe keys or private keys.
- The RDS cluster used `manage_master_user_password`, so no database password is stored.
- The one `aws_secretsmanager_secret_version.secret_string` (in the state backup and inside `tfplan`) holds
  `ORACLE_ADMIN_PASSPHRASE`, `ORACLE_ENCRYPTION_MASTER_KEY`, `ORACLE_SECRET_KEY`,
  `RENTCAST_API_KEY`, `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`, **all
  still placeholder values**.
- They do contain AWS account ids, ARNs and KMS key ids.

Recommendation: delete them from the working tree. They describe a retired
account and have no use. No credential rotation is needed for them. They are
not in git history, so no history rewrite is needed either.
