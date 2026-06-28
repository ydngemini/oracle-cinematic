#!/usr/bin/env bash
# Build + push Neoh container images via AWS CodeBuild. Source = a git-archive of
# the local HEAD uploaded to S3 (no GitHub push, no local disk/GPU needed, builds
# the exact committed tree incl. run_migrations.py). Run AFTER `terraform apply`.
#
#   infra/scripts/build-images.sh app     # backend + frontend (fast, ~10 min)
#   infra/scripts/build-images.sh recon   # GPU reconstruction image (slow, ~30 min)
#
# Account 404870839825 / us-east-1 / profile swarm-admin.
set -euo pipefail
TARGET="${1:-app}"
PROFILE="${AWS_PROFILE:-swarm-admin}"
REGION="${AWS_REGION:-us-east-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

cd "$(dirname "$0")/../terraform"
BE=$(terraform output -raw ecr_backend_url)
FE=$(terraform output -raw ecr_frontend_url)
RECON=$(terraform output -raw recon_ecr_url)
BUCKET=$(terraform output -raw recon_s3_bucket)
REG="${BE%%/*}"

# ── source: git-archive HEAD → S3 (CodeBuild unpacks it as the build context) ──
ZIP="/tmp/neoh-src-$$.zip"
git -C "$REPO_ROOT" archive --format=zip -o "$ZIP" HEAD
SRCKEY="codebuild/neoh-src.zip"
"${AWS[@]}" s3 cp "$ZIP" "s3://$BUCKET/$SRCKEY" >/dev/null
rm -f "$ZIP"
echo ">> source uploaded: s3://$BUCKET/$SRCKEY"

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
      - docker build -t \$BE:latest backend
      - docker push \$BE:latest
      - docker build --build-arg VITE_API_BASE=https://neoh.app -t \$FE:latest oracle-app
      - docker push \$FE:latest
YAML
fi

ENVVARS="[{\"name\":\"ECR_REGISTRY\",\"value\":\"$REG\"},{\"name\":\"BE\",\"value\":\"$BE\"},{\"name\":\"FE\",\"value\":\"$FE\"},{\"name\":\"RECON\",\"value\":\"$RECON\"},{\"name\":\"AWS_DEFAULT_REGION\",\"value\":\"$REGION\"}]"

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
