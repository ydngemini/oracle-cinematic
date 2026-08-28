# Deploying the Neoh AWS Observability Dashboard

This is the **Neoh-embedded** AWS observability dashboard (JWT auth against the Neoh
backend). It is a *second, separate* deployment from the canonical `observatory`
repo (Cognito/Cloudflare) — the two are independent.

## Architecture

```
                          ALB (shared with the main app)
  obs.neohrs.com/  ─────────►  observability ECS service (nginx :8080, this app)
  obs.neohrs.com/api/*  ─┐
  obs.neohrs.com/auth/* ─┼───►  backend ECS service (existing alb.tf `api` rule,
  obs.neohrs.com/ws      ┘       priority 10 — no host condition, matches every host)
```

The dashboard signs in via `POST /auth/login` and streams `/api/aws/ws` using an
`oracle.jwt` WebSocket subprotocol,
both **same-origin** on `obs.neohrs.com` → routed to the backend → **no CORS**. The
backend's `/api/aws/ws` is auth-gated to `platform_admin`/`broker_owner`.
Blank `VITE_API_BASE` and `VITE_WS_URL` values select this same-origin behavior.

## What's in code (this branch)

- `observability-platform/Dockerfile`, `nginx.conf`, `.dockerignore` — the image.
- `infra/scripts/build-images.sh` — builds/pushes `neoh/observability` (4th image),
  baking `VITE_API_BASE=https://$OBS_HOST` + `VITE_WS_URL=wss://$OBS_HOST`.
- `infra/terraform/` — `ecr.tf` (repo), `observability.tf` (log group, target group,
  task def, service, host listener rule, vars, output), `ecs.tf`
  (`AWS_OBSERVABILITY_ENABLED`), `iam.tf` (`AwsObservabilityReadOnly` task-role grant).
- All Terraform is gated on `var.observability_enabled` (default `true`).

## Operator steps (run against your AWS account — nothing here is auto-applied)

1. **Commit** the untracked files: `observability-platform/`, `backend/aws_observability.py`.
   `build-images.sh` archives `HEAD`, so it must be committed to build.

2. **Cert**: ensure `var.acm_certificate_arn` covers `obs.neohrs.com`. A `*.neohrs.com`
   wildcard already covers it; otherwise add it as a SAN and re-validate.

3. **Terraform**:
   ```bash
   cd infra/terraform
   terraform init && terraform validate
   terraform plan   # review: new ECR repo, TG, task def, service, 1 listener rule,
                    #         the AwsObservabilityReadOnly grant, AWS_OBSERVABILITY_ENABLED
   terraform apply
   ```
   Override the host if needed: `-var 'observability_host=obs.example.com'`.
   To NOT grant AWS read / not deploy: `-var 'observability_enabled=false'`.

4. **DNS**: point `obs.neohrs.com` at the ALB (`terraform output` → `aws_lb.main.dns_name`;
   Route53 ALIAS or a CNAME).

5. **Build + push images** (backend must be rebuilt so the auth-gated `/api/aws/ws`
   router ships; frontend image is the new dashboard):
   ```bash
   OBS_HOST=obs.neohrs.com infra/scripts/build-images.sh app
   ```

6. **Roll the services** to the new images:
   ```bash
   aws ecs update-service --cluster neoh-prod --service backend       --force-new-deployment
   aws ecs update-service --cluster neoh-prod --service observability --force-new-deployment
   ```

7. **Sign in**: open `https://obs.neohrs.com`, log in as a `platform_admin`
   (e.g. `ydnop@ydnhft.com`). No self-serve signup exists.

## Security note — the IAM grant

`iam.tf::AwsObservabilityReadOnly` gives the backend **task role** broad read-only
AWS access (EC2/RDS/Lambda/ELB/CloudWatch/Cost Explorer/Security Hub/IAM list/S3
list). It is read/describe/list only (no mutation), gated at runtime by
`AWS_OBSERVABILITY_ENABLED`, and removable by `-var 'observability_enabled=false'`.
Review it before apply — it is the single most privileged change in this feature.
