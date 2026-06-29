#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Neoh GPU reconstruction image — one-shot AWS CodeBuild build + push driver.
#
# WHAT IT DOES (CPU CodeBuild, privileged Docker — NO GPU host needed; nvcc in the
# Dockerfile cross-compiles for SM 8.6):
#   1. reads recon_ecr_url + recon_s3_bucket from `terraform output` (infra/terraform)
#   2. zips this dir's contents (Dockerfile + run.sh + buildspec.yml) -> S3 source
#   3. creates/updates an IAM service role for CodeBuild (ECR push/pull, S3 get, Logs)
#   4. creates/updates a CodeBuild project (standard:7.0, LARGE, privilegedMode)
#   5. starts the build and polls until it SUCCEEDS (or exits nonzero on failure)
#
# WHEN TO RUN:  AFTER `terraform apply` in infra/terraform/ (the ECR repo + S3
# bucket the outputs point at must already exist). This script is idempotent and
# safe to re-run; it overwrites the source zip, role policy, and project each time.
#
# USAGE:
#   cd infra/reconstruction
#   ./codebuild-setup.sh                # builds + pushes tag v1 (and latest)
#   IMAGE_TAG=v2 ./codebuild-setup.sh   # build a new tag
#
# Defaults (override via env):
#   AWS_PROFILE_NAME=swarm-admin  AWS_REGION=us-east-1  IMAGE_TAG=v1
#   CB_PROJECT=neoh-recon-build   CB_ROLE=neoh-recon-codebuild
#   TERRAFORM=terraform           (set to your dockerized terraform invocation if needed)
#
# Requires: aws CLI v2, zip, and terraform (or a TERRAFORM override) on PATH.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

AWS_PROFILE_NAME="${AWS_PROFILE_NAME:-swarm-admin}"
AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
CB_PROJECT="${CB_PROJECT:-neoh-recon-build}"
CB_ROLE="${CB_ROLE:-neoh-recon-codebuild}"
TERRAFORM="${TERRAFORM:-terraform}"
SRC_KEY="codebuild/reconstruction-src.zip"

# Pin every AWS call to the operator's profile + region.
AWS=(aws --profile "$AWS_PROFILE_NAME" --region "$AWS_REGION")

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Resolve paths relative to this script (works from any CWD).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECON_DIR="$SCRIPT_DIR"
TF_DIR="$(cd "$SCRIPT_DIR/../terraform" && pwd)" || die "cannot locate infra/terraform"

# ── 0. Preflight ─────────────────────────────────────────────────────────────
command -v aws  >/dev/null 2>&1 || die "aws CLI not found on PATH"
command -v zip  >/dev/null 2>&1 || die "zip not found on PATH"
command -v "${TERRAFORM%% *}" >/dev/null 2>&1 || die "terraform ('$TERRAFORM') not found on PATH"
for f in Dockerfile run.sh buildspec.yml; do
  [ -f "$RECON_DIR/$f" ] || die "missing $RECON_DIR/$f"
done

log "Verifying AWS identity (profile $AWS_PROFILE_NAME, region $AWS_REGION)"
ACCOUNT_ID="$("${AWS[@]}" sts get-caller-identity --query Account --output text)" \
  || die "could not call STS — check the '$AWS_PROFILE_NAME' profile"
echo "    account $ACCOUNT_ID"

# ── 1. Read Terraform outputs (must exist => apply already ran) ───────────────
log "Reading Terraform outputs from $TF_DIR"
ECR_REPO_URI="$( ( cd "$TF_DIR" && $TERRAFORM output -raw recon_ecr_url ) 2>/dev/null )" \
  || die "terraform output recon_ecr_url failed — run 'terraform apply' in infra/terraform first"
RECON_BUCKET="$( ( cd "$TF_DIR" && $TERRAFORM output -raw recon_s3_bucket ) 2>/dev/null )" \
  || die "terraform output recon_s3_bucket failed — run 'terraform apply' in infra/terraform first"
[ -n "$ECR_REPO_URI" ] || die "recon_ecr_url output is empty"
[ -n "$RECON_BUCKET" ] || die "recon_s3_bucket output is empty"
REPO_PATH="${ECR_REPO_URI#*/}"   # e.g. neoh/reconstruction
echo "    ECR repo   $ECR_REPO_URI"
echo "    S3 bucket  $RECON_BUCKET"

# ── 2. Zip the build source and upload to S3 ─────────────────────────────────
log "Packaging build source -> s3://$RECON_BUCKET/$SRC_KEY"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
SRC_ZIP="$TMP_DIR/reconstruction-src.zip"
( cd "$RECON_DIR" && zip -q -X "$SRC_ZIP" Dockerfile run.sh buildspec.yml )
"${AWS[@]}" s3 cp "$SRC_ZIP" "s3://$RECON_BUCKET/$SRC_KEY"

# ── 3. IAM role for CodeBuild (create or refresh policy) ──────────────────────
TRUST_DOC="$TMP_DIR/trust.json"
cat > "$TRUST_DOC" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Principal": { "Service": "codebuild.amazonaws.com" },
      "Action": "sts:AssumeRole" }
  ]
}
JSON

POLICY_DOC="$TMP_DIR/policy.json"
cat > "$POLICY_DOC" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*" },
    { "Sid": "EcrPushPull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/${REPO_PATH}" },
    { "Sid": "S3SourceRead",
      "Effect": "Allow",
      "Action": [ "s3:GetObject", "s3:GetObjectVersion" ],
      "Resource": "arn:aws:s3:::${RECON_BUCKET}/${SRC_KEY}" },
    { "Sid": "S3SourceList",
      "Effect": "Allow",
      "Action": [ "s3:GetBucketAcl", "s3:GetBucketLocation", "s3:ListBucket" ],
      "Resource": "arn:aws:s3:::${RECON_BUCKET}" },
    { "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [ "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents" ],
      "Resource": [
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/codebuild/${CB_PROJECT}",
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/codebuild/${CB_PROJECT}:*"
      ] }
  ]
}
JSON

ROLE_FRESH=0
if "${AWS[@]}" iam get-role --role-name "$CB_ROLE" >/dev/null 2>&1; then
  log "IAM role $CB_ROLE exists — refreshing trust + inline policy"
  "${AWS[@]}" iam update-assume-role-policy --role-name "$CB_ROLE" \
    --policy-document "file://$TRUST_DOC"
else
  log "Creating IAM role $CB_ROLE"
  "${AWS[@]}" iam create-role --role-name "$CB_ROLE" \
    --description "CodeBuild service role for the Neoh reconstruction GPU image build" \
    --assume-role-policy-document "file://$TRUST_DOC" >/dev/null
  ROLE_FRESH=1
fi
"${AWS[@]}" iam put-role-policy --role-name "$CB_ROLE" \
  --policy-name "${CB_ROLE}-inline" --policy-document "file://$POLICY_DOC"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${CB_ROLE}"
echo "    role $ROLE_ARN"

if [ "$ROLE_FRESH" = "1" ]; then
  log "Waiting 15s for new IAM role to propagate before CodeBuild can assume it"
  sleep 15
fi

# ── 4. CodeBuild project (create or update) ──────────────────────────────────
PROJECT_JSON="$TMP_DIR/project.json"
cat > "$PROJECT_JSON" <<JSON
{
  "name": "${CB_PROJECT}",
  "description": "Build + push the Neoh GPU reconstruction image (CUDA/COLMAP/nerfstudio) to ECR. No GPU needed to build.",
  "source": {
    "type": "S3",
    "location": "${RECON_BUCKET}/${SRC_KEY}",
    "buildspec": "buildspec.yml"
  },
  "artifacts": { "type": "NO_ARTIFACTS" },
  "environment": {
    "type": "LINUX_CONTAINER",
    "image": "aws/codebuild/standard:7.0",
    "computeType": "BUILD_GENERAL1_LARGE",
    "privilegedMode": true,
    "environmentVariables": [
      { "name": "ECR_REPO_URI",       "value": "${ECR_REPO_URI}", "type": "PLAINTEXT" },
      { "name": "AWS_DEFAULT_REGION", "value": "${AWS_REGION}",   "type": "PLAINTEXT" },
      { "name": "IMAGE_TAG",          "value": "${IMAGE_TAG}",    "type": "PLAINTEXT" }
    ]
  },
  "serviceRole": "${ROLE_ARN}",
  "timeoutInMinutes": 120
}
JSON

if "${AWS[@]}" codebuild batch-get-projects --names "$CB_PROJECT" \
     --query 'projects[0].name' --output text 2>/dev/null | grep -qx "$CB_PROJECT"; then
  log "Updating CodeBuild project $CB_PROJECT"
  "${AWS[@]}" codebuild update-project --cli-input-json "file://$PROJECT_JSON" >/dev/null
else
  log "Creating CodeBuild project $CB_PROJECT"
  "${AWS[@]}" codebuild create-project --cli-input-json "file://$PROJECT_JSON" >/dev/null
fi

# ── 5. Start the build and poll to completion ────────────────────────────────
log "Starting build (image tag $IMAGE_TAG + latest)"
BUILD_ID="$("${AWS[@]}" codebuild start-build --project-name "$CB_PROJECT" \
  --query 'build.id' --output text)" || die "start-build failed"
echo "    build id $BUILD_ID"
echo "    logs: https://${AWS_REGION}.console.aws.amazon.com/codesuite/codebuild/${ACCOUNT_ID}/projects/${CB_PROJECT}/build/${BUILD_ID}"

log "Polling build status (heavy CUDA image — expect 20-40 min) ..."
LAST_PHASE=""
while true; do
  read -r STATUS PHASE < <("${AWS[@]}" codebuild batch-get-builds --ids "$BUILD_ID" \
    --query 'builds[0].[buildStatus,currentPhase]' --output text)
  if [ "$PHASE" != "$LAST_PHASE" ]; then
    printf '    %s  phase=%s  status=%s\n' "$(date +%H:%M:%S)" "$PHASE" "$STATUS"
    LAST_PHASE="$PHASE"
  fi
  case "$STATUS" in
    SUCCEEDED)
      log "BUILD SUCCEEDED — pushed ${ECR_REPO_URI}:${IMAGE_TAG} and :latest"
      echo "Next: ensure var.recon_image_tag = \"${IMAGE_TAG}\" in tfvars, re-apply to register the Batch job def,"
      echo "then force-new-deployment on the backend service."
      exit 0 ;;
    FAILED|FAULT|TIMED_OUT|STOPPED)
      die "build $STATUS (phase $PHASE) — see CodeBuild console logs above" ;;
    IN_PROGRESS)
      sleep 15 ;;
    *)
      printf '    unexpected status: %s\n' "$STATUS"; sleep 15 ;;
  esac
done
