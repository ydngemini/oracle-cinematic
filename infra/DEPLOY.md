# Neoh — AWS Production Deploy Runbook

Full AWS deployment matching `HARDENING.md`: Aurora PostgreSQL (Serverless v2,
IAM auth) + Fargate (backend + nginx frontend) behind an ALB with WAF, Secrets
Manager, KMS, CloudWatch, private S3 contract-vault storage, all in a zero-trust
VPC.

Terraform lives in `infra/terraform/` and is **validated** (`terraform validate`
passes). You run `terraform apply` with **your** AWS credentials — it provisions
real, billable infrastructure.

---

## 0. Prerequisites (one-time)

- AWS account + credentials with admin (or scoped infra) access in your shell.
- An **ACM certificate** (ISSUED) in your target region covering your domain.
  `aws acm request-certificate --domain-name app.neoh.example --validation-method DNS`
  then add the DNS validation CNAME and wait for status ISSUED. Copy the ARN.
  The CNAME must be published in the zone the domain's **live nameservers** serve,
  not merely in a zone you control — ACM resolves it the way the internet does.
- Terraform >= 1.6, Docker, and the AWS CLI installed.
- (Recommended) Create the remote-state S3 bucket + DynamoDB lock table, then
  uncomment the `backend "s3"` block in `versions.tf`. State holds secret ARNs.

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in: acm_certificate_arn, cors_origins, app_base_url, region
```

## 1. Create ECR repos first (so you can push images Terraform will reference)

The task definitions reference `…:{image_tag}`. Create just the repos, then push,
then apply the rest:

```bash
terraform init
terraform apply -target=aws_ecr_repository.repo      # creates neoh/backend + neoh/frontend
```

## 2. Build + push the images

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
TAG=v1   # must equal var.image_tag
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# backend
docker build -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/neoh/backend:$TAG ../../backend
docker push       $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/neoh/backend:$TAG

# frontend (browser-PUBLIC build args; VITE_API_BASE empty = same-origin behind ALB)
docker build \
  --build-arg VITE_API_BASE="" \
  --build-arg VITE_GOOGLE_MAPS_KEY="<referrer-restricted-key>" \
  -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/neoh/frontend:$TAG ../../oracle-app
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/neoh/frontend:$TAG
```

## 3. Provision everything

```bash
terraform apply        # VPC, Aurora, ALB, WAF, ECS, IAM, Secrets, CloudWatch, S3 vaults
```

Note the outputs: `alb_dns_name`, `db_writer_endpoint`, `db_master_secret_arn`,
`app_secret_name`, `contract_vault_bucket`. The backend service will NOT be
healthy yet — it can't connect until the secrets are real, the param group is
loaded, and migrations have run.

Terraform also provisions the private contract vault bucket and wires
`CONTRACT_VAULT_BUCKET` into the backend task definition. The bucket blocks
public access, defaults to SSE-S3 (`AES256`), denies non-TLS access, and the app
task role can only read/write `clients/*/contracts/*.pdf` objects.

## 4. Populate the application secrets (real values, out-of-band)

`config.validate_or_die()` refuses to boot without `ORACLE_SECRET_KEY` +
`ORACLE_ENCRYPTION_MASTER_KEY`. Generate strong values and put them in the secret
Terraform created (it ignores changes, so this is never clobbered):

```bash
aws secretsmanager put-secret-value --secret-id "$(terraform output -raw app_secret_name)" \
  --secret-string "$(jq -n \
    --arg sk "$(openssl rand -hex 48)" \
    --arg ek "$(openssl rand -hex 48)" \
    --arg ap "$(openssl rand -hex 24)" \
    --arg ss "sk_live_…" --arg ws "whsec_…" --arg rc "<rentcast-key>" \
    '{ORACLE_SECRET_KEY:$sk, ORACLE_ENCRYPTION_MASTER_KEY:$ek, ORACLE_ADMIN_PASSPHRASE:$ap, STRIPE_SECRET_KEY:$ss, STRIPE_WEBHOOK_SECRET:$ws, RENTCAST_API_KEY:$rc}')"
```

> ⚠️ **Stripe is a LIVE key → real charges.** Keep a test key in a staging
> deploy; only put `sk_live_…` here when you intend to bill real cards.
> ⚠️ **Rotating `ORACLE_ENCRYPTION_MASTER_KEY` after data exists loses that data.**

## 5. Aurora param group → reboot (HARDENING.md §1)

`shared_preload_libraries=pgaudit` + `password_encryption=scram` are static; the
cluster must reboot to load them before `CREATE EXTENSION pgaudit` works:

```bash
aws rds reboot-db-instance --db-instance-identifier neoh-prod-aurora-0
# (also reboot -1 if you keep two instances)
```

## 6. Run migrations (as the master user, then the app uses IAM)

Migrations need network to private Aurora. The production runner launches a
one-off private-subnet ECS task on the new digest-pinned backend image,
temporarily grants access to only the RDS-managed master secret, applies every
pending numbered migration, and revokes that grant on success or failure:

```bash
AWS_PROFILE=neoh infra/scripts/run-migrations.sh
```

For break-glass recovery, apply the raw SQL files in **filename order** (`0001`
through the latest migration; `0001`/`0003` create the `oracle_app_login` role
and RLS). From a host with network to Aurora, use TLS verify-full against the RDS
CA:

```bash
export PGSSLMODE=verify-full PGSSLROOTCERT=/path/to/rds-global-bundle.pem
for f in $(ls backend/db/migrations/*.sql | sort); do
  echo ">> $f"; psql "host=<db_writer_endpoint> dbname=oracle user=oracle_admin password=<master>" \
    -v ON_ERROR_STOP=1 -f "$f"
done
```

## 7. Roll the backend service

Once secrets + migrations are in, force a fresh deployment so tasks pick them up:

```bash
aws ecs update-service --cluster "$(terraform output -raw ecs_cluster)" \
  --service backend --force-new-deployment
```

Watch the target group go healthy; tail logs in `/ecs/neoh-prod/backend`.

For normal releases use `infra/scripts/deploy-update.sh`. It runs migrations
first, rolls and checks the backend before the frontend, runs the complete smoke
suite, and automatically restores the prior ECS task definitions if stability or
any smoke assertion fails. Additive migrations remain in place and are forward-
compatible with the prior service revision.

## 7b. Staged platform capabilities

The new platform capabilities are explicit Terraform booleans and default to
`false` in production: municipal harvests, predictive intelligence, marketplace,
contracts, spatial generation, local models, and autonomous commands. Enable one
reviewed cohort at a time in `terraform.tfvars`, apply Terraform to register the
task definition, and then run the digest-pinned release:

```bash
terraform -chdir=infra/terraform apply
AWS_PROFILE=neoh infra/scripts/build-images.sh app
AWS_PROFILE=neoh infra/scripts/deploy-update.sh
```

Keep `feature_automation` last: EMAIL/CALL/CALENDAR execution additionally needs
provider credentials, consent/approval testing, and webhook-signature validation.

## 8. DNS + smoke test

DNS for `neohrs.com` is managed in this account (`infra/terraform/dns.tf`) — the
zone was moved off the registrar's own nameservers on 2026-08-28 precisely so that
ACM validation and the ALB alias stop being manual steps. The apply creates the
apex, `www` and observability ALIAS records; nothing here needs a console visit.

The one thing Terraform cannot do is the **NS delegation**, which lives at the
registrar:

```bash
terraform output route53_name_servers   # set these four at the registrar
dig +short NS neohrs.com                # confirm the delegation went live
```

Until those nameservers are live, every record in the zone is invisible to the
internet — including the ACM validation CNAME, which is what keeps a certificate
in `PENDING_VALIDATION`.

- For a domain whose DNS you do NOT hold here, point it at the ALB by hand
  (Route53 ALIAS to `alb_dns_name`/`alb_zone_id`, or a CNAME).
- Smoke:
  ```bash
  curl -fsS https://app.neoh.example/health          # backend 200
  curl -fsS https://app.neoh.example/ | head         # SPA shell
  infra/scripts/prod-smoke.sh                        # ECS + S3 vault checks
  ```
- Stripe: add the live webhook endpoint `https://app.neoh.example/billing/webhook`
  in the Stripe dashboard, copy the signing secret into `STRIPE_WEBHOOK_SECRET`,
  and re-run step 7.

## 9. GPU reconstruction (walkable 3D tours)

`reconstruction.tf` provisions the walk-inside splat storage: an S3 bucket
(capture inputs/outputs + public `splats/`) plus the default **AWS Batch** SPOT g5
GPU compute environment that **scales to zero** (pay per job, ~$0.30–1/house).
`terraform apply` also wires the backend task env (`RECONSTRUCTION_PROVIDER`,
`RECON_*`, `ORACLE_SPLAT_STORAGE=s3`, `ORACLE_SPLAT_S3_BUCKET`, `ORACLE_SPLAT_CDN_BASE`).

After apply: (1) request EC2 **SPOT g5/g4dn vCPU** quota in-region; (2) build+push
the GPU job image to the `recon_ecr_url` output and set `recon_image_tag`; (3) roll
the backend. Until configured, the enqueue endpoint honestly returns 503 (never a
fake splat). Full steps + the capture flow: **`infra/reconstruction/README.md`**.

If AWS GPU quota is blocked, set `reconstruction_provider = "runpod"` and
`runpod_endpoint_id` in `terraform.tfvars`, add `RUNPOD_API_KEY` to the app
Secrets Manager JSON, and deploy the worker in **`infra/reconstruction-runpod/`**.

## 9b. Day-2

- New release: start from a reviewed, clean commit (the build script refuses a
  dirty tree), build/push, then run `deploy-update.sh`. The service ignores
  `task_definition` drift in TF so the digest-pinned release script owns rollout.
- Audit trail: pgaudit → `/aws/rds/cluster/neoh-prod-aurora/postgresql` (KMS,
  90-day retention). Add a log-group resource policy denying delete for immutability.
- **Still TODO (not in this TF):** per-tenant API throttling (add API Gateway in
  front, or in-app rate caps) — see HARDENING.md §3.

## Teardown

`deletion_protection=true` on Aurora and a final snapshot guard data. To destroy:
disable deletion protection, then `terraform destroy` (the master/app secrets have
a 7-day recovery window).
