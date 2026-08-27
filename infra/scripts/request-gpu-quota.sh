#!/usr/bin/env bash
#
# request-gpu-quota.sh — Raise the EC2 SPOT G/VT vCPU Service Quota for the
# Neoh GPU reconstruction pipeline (AWS Batch SPOT g5/g4dn — see
# infra/terraform/reconstruction.tf).
#
# Why this exists:
#   The "All G and VT Spot Instance Requests" quota (service ec2, quota code
#   L-3819A6DF) defaults to 0 vCPUs on new AWS accounts. At 0, the Batch SPOT
#   GPU compute environment can NEVER launch an instance, so every COLMAP/3DGS
#   reconstruction job sits RUNNABLE forever with no error. You must raise this
#   quota before the reconstruction pipeline can do real work.
#
# What it does:
#   1. Reads the current applied quota.
#   2. If it is already >= the target, exits 0 (nothing to do).
#   3. If a PENDING / CASE_OPENED increase already exists, exits 0 (no duplicate).
#   4. Otherwise submits an increase to the target and prints the request id.
#
# Usage:
#   ./request-gpu-quota.sh [TARGET_VCPUS]      # default target = 8 vCPUs
#
# Examples:
#   ./request-gpu-quota.sh          # request 8 vCPUs (2x g5.xlarge @ 4 vCPU each)
#   ./request-gpu-quota.sh 16       # request 16 vCPUs
#
# Env overrides:
#   PROFILE   (default swarm-admin)   AWS CLI profile
#   REGION    (default us-east-1)     AWS region
#
# Idempotent and safe to re-run. Only the request-service-quota-increase call
# mutates anything; everything else is read-only.
#
# Requires: aws CLI v2 with the swarm-admin profile (service-quotas:*).
# Account: 151105438863   Region: us-east-1   Profile: neoh
# Approval: AWS typically grants G/VT Spot quota increases in ~1 day.
#
set -uo pipefail

PROFILE="${PROFILE:-swarm-admin}"
REGION="${REGION:-us-east-1}"
SERVICE_CODE="ec2"
QUOTA_CODE="L-3819A6DF"   # "All G and VT Spot Instance Requests"
TARGET="${1:-8}"

# ── sanity ──────────────────────────────────────────────────────────────────
if ! command -v aws >/dev/null 2>&1; then
  echo "[FAIL] aws CLI not found on PATH." >&2
  exit 2
fi
if ! [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  echo "[FAIL] TARGET_VCPUS must be a positive integer (got '$TARGET')." >&2
  exit 2
fi

AWS=(aws --profile "$PROFILE" --region "$REGION")

echo "── Neoh GPU Spot quota request ────────────────────────────────────────"
echo "Account 151105438863 | Region $REGION | Profile $PROFILE"
echo "Quota:  $SERVICE_CODE / $QUOTA_CODE (All G and VT Spot Instance Requests)"
echo "Target: $TARGET vCPUs"
echo

# ── 1. current applied quota ────────────────────────────────────────────────
current="$("${AWS[@]}" service-quotas get-service-quota \
  --service-code "$SERVICE_CODE" --quota-code "$QUOTA_CODE" \
  --query 'Quota.Value' --output text 2>/dev/null)"

# Fall back to the AWS-published default if the applied value can't be read.
if [[ -z "$current" || "$current" == "None" ]]; then
  current="$("${AWS[@]}" service-quotas get-aws-default-service-quota \
    --service-code "$SERVICE_CODE" --quota-code "$QUOTA_CODE" \
    --query 'Quota.Value' --output text 2>/dev/null)"
fi
if [[ -z "$current" || "$current" == "None" ]]; then
  echo "[WARN] Could not read the current quota value; assuming 0." >&2
  current="0"
fi
echo "Current applied quota: $current vCPUs"

# ── 2. already satisfied? ───────────────────────────────────────────────────
if awk -v c="$current" -v t="$TARGET" 'BEGIN{exit !(c+0 >= t+0)}'; then
  echo "[OK] Current quota ($current) already >= target ($TARGET). Nothing to do."
  exit 0
fi

# ── 3. existing pending request? ────────────────────────────────────────────
history="$("${AWS[@]}" service-quotas list-requested-service-quota-change-history-by-quota \
  --service-code "$SERVICE_CODE" --quota-code "$QUOTA_CODE" \
  --query 'RequestedQuotas[].[Status,Id,DesiredValue]' --output text 2>/dev/null)"

pending_line="$(printf '%s\n' "$history" | grep -E '^(PENDING|CASE_OPENED)[[:space:]]' | head -n1)"
if [[ -n "$pending_line" ]]; then
  p_status="$(printf '%s' "$pending_line" | awk '{print $1}')"
  p_id="$(printf '%s' "$pending_line" | awk '{print $2}')"
  p_val="$(printf '%s' "$pending_line" | awk '{print $3}')"
  echo "[OK] A quota increase is already in flight (status=$p_status, desired=$p_val vCPUs)."
  echo "     Request id: $p_id"
  echo "     Not submitting a duplicate. Approval is typically ~1 day."
  exit 0
fi

# ── 4. submit the increase ──────────────────────────────────────────────────
echo "Submitting increase request to $TARGET vCPUs ..."
req_id="$("${AWS[@]}" service-quotas request-service-quota-increase \
  --service-code "$SERVICE_CODE" --quota-code "$QUOTA_CODE" \
  --desired-value "$TARGET" \
  --query 'RequestedQuota.Id' --output text 2>/dev/null)"

if [[ -z "$req_id" || "$req_id" == "None" ]]; then
  echo "[FAIL] Request submission did not return a request id. Check IAM perms / console." >&2
  exit 1
fi

echo "[OK] Requested $TARGET vCPUs for $SERVICE_CODE/$QUOTA_CODE."
echo "     Request id: $req_id"
echo "     Approval is typically ~1 day. Track it with:"
echo "       aws --profile $PROFILE --region $REGION service-quotas \\"
echo "         get-requested-service-quota-change --request-id $req_id"
exit 0
