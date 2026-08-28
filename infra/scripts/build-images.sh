#!/usr/bin/env bash
# Build + push Neoh container images via AWS CodeBuild. Source = a git-archive of
# the local HEAD uploaded to S3 (no GitHub push, no local disk/GPU needed, builds
# the exact committed tree incl. run_migrations.py). Run AFTER `terraform apply`.
#
#   infra/scripts/build-images.sh app     # backend + frontend (fast, ~10 min)
#   infra/scripts/build-images.sh recon   # GPU reconstruction image (slow, ~30 min)
#
# Account 151105438863 / us-east-1 / profile neoh.
set -euo pipefail
TARGET="${1:-app}"
PROFILE="${AWS_PROFILE:-neoh}"   # swarm-admin died with account 404870839825
REGION="${AWS_REGION:-us-east-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# CodeBuild consumes git-archive HEAD. Refuse a dirty workspace so production
# can never silently omit reviewed-but-uncommitted fixes (or include untracked
# local secrets through an ad-hoc archive).
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo "!! refusing production build: the workspace has uncommitted changes" >&2
  echo "!! review and commit the intended release before invoking CodeBuild" >&2
  exit 2
fi
SOURCE_REV="$(git -C "$REPO_ROOT" rev-parse HEAD)"

# Resolve resource names deterministically via aws (no local terraform needed).
ACCT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
REG="${ACCT}.dkr.ecr.${REGION}.amazonaws.com"
BE="${REG}/neoh/backend"
FE="${REG}/neoh/frontend"
RECON="${REG}/neoh/reconstruction"
OBS="${REG}/neoh/observability"
# Public app vhost. Baked into the frontend image at build time as VITE_API_BASE,
# so an image built against the wrong host calls the wrong API for its whole life
# — this hardcoded neoh.app until 2026-08-28, which is a domain that no longer
# resolves to anything we own. Must match terraform's app_base_url/cors_origins.
APP_HOST="${APP_HOST:-neohrs.com}"
# AWS observability dashboard vhost. The ALB routes this host to the dashboard,
# and its /api,/auth,/ws to the backend, so the SPA calls the API same-origin.
# Must be covered by the ACM cert, so it has to stay a subdomain of APP_HOST.
OBS_HOST="${OBS_HOST:-obs.neohrs.com}"
BUCKET="neoh-prod-recon-${ACCT}"

# ── source: git-archive HEAD → S3 (CodeBuild unpacks it as the build context) ──
ZIP="/tmp/neoh-src-$$.zip"
git -C "$REPO_ROOT" archive --format=zip -o "$ZIP" HEAD
SRCKEY="codebuild/neoh-src-${SOURCE_REV}.zip"
"${AWS[@]}" s3 cp "$ZIP" "s3://$BUCKET/$SRCKEY" >/dev/null
rm -f "$ZIP"
echo ">> source uploaded: s3://$BUCKET/$SRCKEY"
echo ">> source revision: $SOURCE_REV"

# ── shared CodeBuild service role ───────────────────────────────────────────
ROLE=neoh-codebuild
if ! "${AWS[@]}" iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo ">> creating CodeBuild role $ROLE"
  "${AWS[@]}" iam create-role --role-name "$ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  "${AWS[@]}" iam put-role-policy --role-name "$ROLE" --policy-name inline \
    --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability","ecr:CompleteLayerUpload","ecr:InitiateLayerUpload","ecr:PutImage","ecr:UploadLayerPart","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],"Resource":"*"},{"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},{"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion","s3:GetBucketAcl","s3:GetBucketLocation"],"Resource":"*"}]}'
  sleep 12 # IAM propagation
fi
ROLE_ARN=$("${AWS[@]}" iam get-role --role-name "$ROLE" --query Role.Arn --output text)

# ── pick buildspec + compute per target ─────────────────────────────────────
if [ "$TARGET" = "recon" ]; then
  PROJ=neoh-recon-build
  COMPUTE=BUILD_GENERAL1_LARGE
  read -r -d '' SPEC <<YAML || true
version: 0.2
phases:
  build:
    commands:
      - aws ecr get-login-password --region \$AWS_DEFAULT_REGION | docker login --username AWS --password-stdin \$ECR_REGISTRY
      - if [ -n "\$DOCKERHUB_TOKEN" ]; then echo "\$DOCKERHUB_TOKEN" | docker login --username "\$DOCKERHUB_USER" --password-stdin; echo "authed to Docker Hub (avoids nvidia/cuda 429)"; fi
      - docker build -t \$RECON:v1 -t \$RECON:latest infra/reconstruction
      - docker push \$RECON:v1
      - docker push \$RECON:latest
YAML
else
  PROJ=neoh-app-images
  COMPUTE=BUILD_GENERAL1_LARGE
  read -r -d '' SPEC <<YAML || true
version: 0.2
phases:
  build:
    commands:
      - aws ecr get-login-password --region \$AWS_DEFAULT_REGION | docker login --username AWS --password-stdin \$ECR_REGISTRY
      - for i in python:3.12-slim node:20-alpine nginx:1.27-alpine; do docker pull public.ecr.aws/docker/library/\$i && docker tag public.ecr.aws/docker/library/\$i \$i; done
      - docker build -t \$BE:latest backend
      - docker push \$BE:latest
      - docker build --build-arg VITE_API_BASE=https://${APP_HOST} -t \$FE:latest oracle-app
      - docker push \$FE:latest
      - docker build --build-arg VITE_API_BASE=https://${OBS_HOST} --build-arg VITE_WS_URL=wss://${OBS_HOST} -t \$OBS:latest observability-platform
      - docker push \$OBS:latest
YAML
fi

# Docker Hub auth (avoids the nvidia/cuda anonymous 429 on CodeBuild IPs).
# PREFERRED: store the PAT in Secrets Manager so it is NOT persisted as plaintext in
# the CodeBuild project env (readable by anyone with codebuild:BatchGetProjects):
#   aws secretsmanager create-secret --name neoh/dockerhub \
#     --secret-string '{"username":"<user>","token":"dckr_pat_..."}'
# CodeBuild resolves SECRETS_MANAGER-typed env vars at build time. The CodeBuild
# service role needs secretsmanager:GetSecretValue on neoh/dockerhub.
# Precedence: Secrets Manager wins whenever neoh/dockerhub is reachable — a
# correctly-configured secret must never be silently shadowed by a stray
# DOCKERHUB_TOKEN left in the operator's shell/CI env. DOCKERHUB_TOKEN is only a
# fallback for when Secrets Manager access fails (missing secret, or the build
# role lacking secretsmanager:GetSecretValue) — same "no creds" failure mode as
# before that case, just plaintext instead of a hard stop.
DH=""
if "${AWS[@]}" secretsmanager describe-secret --secret-id neoh/dockerhub >/dev/null 2>&1; then
  echo ">> Docker Hub creds via Secrets Manager (neoh/dockerhub)"
  DH=",{\"name\":\"DOCKERHUB_USER\",\"value\":\"neoh/dockerhub:username\",\"type\":\"SECRETS_MANAGER\"},{\"name\":\"DOCKERHUB_TOKEN\",\"value\":\"neoh/dockerhub:token\",\"type\":\"SECRETS_MANAGER\"}"
elif [ -n "${DOCKERHUB_TOKEN:-}" ]; then
  echo "!! WARNING: injecting Docker Hub PAT as PLAINTEXT into the CodeBuild project env (Secrets Manager unavailable)." >&2
  echo "!! Prefer Secrets Manager: aws secretsmanager create-secret --name neoh/dockerhub --secret-string '{\"username\":\"\$USER\",\"token\":\"dckr_pat_...\"}'" >&2
  DH=",{\"name\":\"DOCKERHUB_USER\",\"value\":\"${DOCKERHUB_USER:-}\"},{\"name\":\"DOCKERHUB_TOKEN\",\"value\":\"$DOCKERHUB_TOKEN\"}"
fi
ENVVARS="[{\"name\":\"ECR_REGISTRY\",\"value\":\"$REG\"},{\"name\":\"BE\",\"value\":\"$BE\"},{\"name\":\"FE\",\"value\":\"$FE\"},{\"name\":\"OBS\",\"value\":\"$OBS\"},{\"name\":\"RECON\",\"value\":\"$RECON\"},{\"name\":\"AWS_DEFAULT_REGION\",\"value\":\"$REGION\"}$DH]"

SRC="{\"type\":\"S3\",\"location\":\"$BUCKET/$SRCKEY\",\"buildspec\":$(python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))' <<<"$SPEC")}"
ENVJSON="{\"type\":\"LINUX_CONTAINER\",\"image\":\"aws/codebuild/standard:7.0\",\"computeType\":\"$COMPUTE\",\"privilegedMode\":true,\"environmentVariables\":$ENVVARS}"

if "${AWS[@]}" codebuild batch-get-projects --names "$PROJ" --query 'projects[0].name' --output text 2>/dev/null | grep -q "$PROJ"; then
  echo ">> updating project $PROJ"
  "${AWS[@]}" codebuild update-project --name "$PROJ" --source "$SRC" --environment "$ENVJSON" --service-role "$ROLE_ARN" --artifacts '{"type":"NO_ARTIFACTS"}' --timeout-in-minutes 120 >/dev/null
else
  echo ">> creating project $PROJ"
  "${AWS[@]}" codebuild create-project --name "$PROJ" --source "$SRC" --environment "$ENVJSON" --service-role "$ROLE_ARN" --artifacts '{"type":"NO_ARTIFACTS"}' --timeout-in-minutes 120 >/dev/null
fi

BID=$("${AWS[@]}" codebuild start-build --project-name "$PROJ" --query 'build.id' --output text)
echo ">> build started: $BID"
while true; do
  sleep 20
  read -r PH ST < <("${AWS[@]}" codebuild batch-get-builds --ids "$BID" --query 'builds[0].[currentPhase,buildStatus]' --output text)
  echo "   phase=$PH status=$ST"
  case "$ST" in
    SUCCEEDED) echo ">> $PROJ OK"; exit 0;;
    FAILED|FAULT|TIMED_OUT|STOPPED) echo ">> $PROJ FAILED ($ST) — logs:"; "${AWS[@]}" codebuild batch-get-builds --ids "$BID" --query 'builds[0].logs.deepLink' --output text; exit 1;;
  esac
done
