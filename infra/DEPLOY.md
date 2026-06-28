# Neoh — AWS Production Deploy Runbook

Full AWS deployment matching `HARDENING.md`: Aurora PostgreSQL (Serverless v2,
IAM auth) + Fargate (backend + nginx frontend) behind an ALB with WAF, Secrets
Manager, KMS, CloudWatch, all in a zero-trust VPC.

Terraform lives in `infra/terraform/` and is **validated** (`terraform validate`
passes). You run `terraform apply` with **your** AWS credentials — it provisions
real, billable infrastructure.

---

## 0. Prerequisites (one-time)

- AWS account + credentials with admin (or scoped infra) access in your shell.
- An **ACM certificate** (ISSUED) in your target region covering your domain.
  `aws acm request-certificate --domain-name app.neoh.example --validation-method DNS`
  then add the DNS validation CNAME and wait for status ISSUED. Copy the ARN.
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
terraform apply        # VPC, Aurora, ALB, WAF, ECS, IAM, Secrets, CloudWatch
```

Note the outputs: `alb_dns_name`, `db_writer_endpoint`, `db_master_secret_arn`,
`app_secret_name`. The backend service will NOT be healthy yet — it can't connect
until the secrets are real, the param group is loaded, and migrations have run.

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

Migrations need network to Aurora (private subnet). Easiest: a one-off task in the
cluster, or run from a bastion via SSM. Pull the master creds from the RDS-managed
secret, then apply `0001 → 0002 → 0003 → … → 0022` in order (0001/0003 create the
`oracle_app_login` IAM role + RLS the app depends on):

```bash
aws secretsmanager get-secret-value --secret-id "$(terraform output -raw db_master_secret_arn)" \
  --query SecretString --output text   # {username,password}
```

There is no migration-runner script — apply the raw SQL files in **filename
order** (they are numbered `0001`…`0022`; `0001`/`0003` create the `oracle_app_login`
IAM role + the RLS the app relies on). From a host with network to Aurora
(SSM bastion or a one-off ECS task), over TLS verify-full against the RDS CA:

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

## 8. DNS + smoke test

- Point `app.neoh.example` at the ALB (Route53 ALIAS to `alb_dns_name`/`alb_zone_id`,
  or a CNAME).
- Smoke:
  ```bash
  curl -fsS https://app.neoh.example/health          # backend 200
  curl -fsS https://app.neoh.example/ | head         # SPA shell
  ```
- Stripe: add the live webhook endpoint `https://app.neoh.example/billing/webhook`
  in the Stripe dashboard, copy the signing secret into `STRIPE_WEBHOOK_SECRET`,
  and re-run step 7.

## 9. Day-2

- New release: build+push a new `image_tag`, register a new task-def revision,
  `update-service`. (The service ignores `task_definition` drift in TF so CI owns it.)
- Audit trail: pgaudit → `/aws/rds/cluster/neoh-prod-aurora/postgresql` (KMS,
  90-day retention). Add a log-group resource policy denying delete for immutability.
- **Still TODO (not in this TF):** per-tenant API throttling (add API Gateway in
  front, or in-app rate caps) — see HARDENING.md §3.

## Teardown

`deletion_protection=true` on Aurora and a final snapshot guard data. To destroy:
disable deletion protection, then `terraform destroy` (the master/app secrets have
a 7-day recovery window).
