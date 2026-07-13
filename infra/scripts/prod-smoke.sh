#!/usr/bin/env bash
#
# prod-smoke.sh — Post-deploy smoke test for the Neoh production stack.
#
# Mostly read-only. Verifies, with clear PASS/FAIL output:
#   1. The public app health endpoint (https://neoh.app/health), with the ALB
#      DNS as a direct fallback so you can tell "app down" from "DNS/cert gap".
#   2. The API is live by hitting the tour resolver (/api/crm/property-tour) and
#      the public data health route (/api/data/health). Both sit behind the
#      tenant-auth gate, so anonymous calls return 401/403/422 — which still
#      proves the backend router is mounted and serving (404/5xx/no-response do
#      NOT). See infra/terraform/alb.tf: /api/* + /health route to the backend.
#   3. ECS services running == desired with a single stable deployment.
#   4. The splat S3 bucket exists and grants public read on splats/*.
#   5. The contract-vault S3 bucket exists, blocks public access, and encrypts.
#
# Identifiers are read from `terraform output` when terraform/state is reachable,
# otherwise they fall back to the deterministic prod values
# (project=neoh, environment=prod, account 404870839825, region us-east-1).
#
# Usage:
#   ./prod-smoke.sh
#
# Env overrides (any may be set to skip terraform):
#   PROFILE         (default swarm-admin)
#   REGION          (default us-east-1)
#   APP_URL         (default https://neoh.app)
#   ALB_DNS         (default: terraform output alb_dns_name)
#   ECS_CLUSTER     (default: terraform output ecs_cluster -> neoh-prod)
#   SPLAT_BUCKET    (default: terraform output recon_s3_bucket -> neoh-prod-recon-<acct>)
#   SPLAT_CDN_BASE  (default: terraform output recon_splat_cdn_base)
#   SPLAT_TEST_KEY  (optional: a key UNDER splats/ to anonymously HEAD for 200)
#   CONTRACT_VAULT_BUCKET (default: terraform output contract_vault_bucket)
#   TF_DIR          (default: ../terraform relative to this script)
#
# Exit code: 0 if all hard checks pass, 1 if any hard check fails.
#
# Requires: curl, aws CLI v2 (profile swarm-admin). terraform optional.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${TF_DIR:-$SCRIPT_DIR/../terraform}"

tf_out() { terraform -chdir="$TF_DIR" output -raw "$1" 2>/dev/null || true; }

# ── config / resolution (env > terraform > deterministic default) ────────────
PROFILE="${PROFILE:-swarm-admin}"
REGION="${REGION:-us-east-1}"
ACCOUNT_ID="404870839825"
APP_URL="${APP_URL:-https://neoh.app}"

ALB_DNS="${ALB_DNS:-$(tf_out alb_dns_name)}"

ECS_CLUSTER="${ECS_CLUSTER:-$(tf_out ecs_cluster)}"
ECS_CLUSTER="${ECS_CLUSTER:-neoh-prod}"

SPLAT_BUCKET="${SPLAT_BUCKET:-$(tf_out recon_s3_bucket)}"
SPLAT_BUCKET="${SPLAT_BUCKET:-neoh-prod-recon-$ACCOUNT_ID}"

SPLAT_CDN_BASE="${SPLAT_CDN_BASE:-$(tf_out recon_splat_cdn_base)}"
SPLAT_CDN_BASE="${SPLAT_CDN_BASE:-https://$SPLAT_BUCKET.s3.$REGION.amazonaws.com}"

CONTRACT_VAULT_BUCKET="${CONTRACT_VAULT_BUCKET:-$(tf_out contract_vault_bucket)}"
CONTRACT_VAULT_BUCKET="${CONTRACT_VAULT_BUCKET:-neoh-prod-contract-vault-$ACCOUNT_ID}"

# ECS service names are fixed in infra/terraform/ecs.tf and observability.tf.
SERVICES=(backend frontend observability)

# host header for the ALB-direct fallback (cert is for the app domain, not the ALB)
APP_HOST="${APP_URL#*://}"; APP_HOST="${APP_HOST%%/*}"

PASS=0; WARN=0; FAIL=0
pass() { echo "[PASS] $*"; PASS=$((PASS+1)); }
warn() { echo "[WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "[INFO] $*"; }

# curl wrapper: prints HTTP status code (000 on no response). Extra curl opts
# after the URL.
get_code() {
  local url="$1"; shift
  curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$@" "$url" 2>/dev/null || true
}

# ── preflight ────────────────────────────────────────────────────────────────
command -v curl >/dev/null 2>&1 || { echo "[FAIL] curl not found on PATH." >&2; exit 2; }
command -v aws  >/dev/null 2>&1 || { echo "[FAIL] aws CLI not found on PATH." >&2; exit 2; }
AWS=(aws --profile "$PROFILE" --region "$REGION")

echo "════════ Neoh prod smoke test ════════"
info "Account $ACCOUNT_ID | Region $REGION | Profile $PROFILE"
info "App URL:      $APP_URL"
info "ALB DNS:      ${ALB_DNS:-<unresolved>}"
info "ECS cluster:  $ECS_CLUSTER  (services: ${SERVICES[*]})"
info "Splat bucket: $SPLAT_BUCKET"
info "Vault bucket: $CONTRACT_VAULT_BUCKET"
echo

# ── 1. public app health (+ ALB fallback) ────────────────────────────────────
echo "── 1. public app health ──"
code="$(get_code "$APP_URL/health")"
if [[ "$code" == "200" ]]; then
  pass "GET $APP_URL/health -> 200"
else
  warn "GET $APP_URL/health -> $code"
  if [[ -n "$ALB_DNS" ]]; then
    acode="$(get_code "https://$ALB_DNS/health" -k -H "Host: $APP_HOST")"
    if [[ "$acode" == "200" ]]; then
      fail "Public $APP_URL/health is $code but ALB-direct is 200 -> app is UP; DNS/ACM/Route53 for $APP_HOST is the gap"
    else
      fail "Neither $APP_URL/health ($code) nor ALB-direct https://$ALB_DNS/health ($acode) returned 200 -> app appears DOWN"
    fi
  else
    fail "GET $APP_URL/health -> $code and no ALB DNS available to fall back to"
  fi
fi
echo

# ── 2. API liveness (tour resolver + public data health) ─────────────────────
echo "── 2. API liveness ──"
classify_api() {
  # $1 = label, $2 = path. Hard PASS on 200/401/403/422 (router mounted),
  # hard FAIL on 404 / 5xx / no-response.
  local label="$1" path="$2" c
  c="$(get_code "$APP_URL$path")"
  case "$c" in
    200|401|403|422) pass "$label: GET $path -> $c (router mounted, gate responding)";;
    404)             fail "$label: GET $path -> 404 (router not mounted / wrong build deployed)";;
    000)             fail "$label: GET $path -> no response (backend unreachable)";;
    5*)              fail "$label: GET $path -> $c (backend error)";;
    *)               warn "$label: GET $path -> $c (unexpected — investigate)";;
  esac
}
classify_api "tour resolver" "/api/crm/property-tour"
classify_api "data health"   "/api/data/health"
classify_api "commands"      "/api/commands"
classify_api "intelligence"  "/api/intelligence/policy"
classify_api "portfolio"     "/api/portfolio"
classify_api "marketplace"   "/api/marketplace"
classify_api "models"        "/api/models"
classify_api "harvests"      "/api/harvests"
classify_api "contracts"     "/api/contracts/policy"
echo

# ── 3. ECS services running == desired, stable ───────────────────────────────
echo "── 3. ECS services ──"
ecs_out="$("${AWS[@]}" ecs describe-services \
  --cluster "$ECS_CLUSTER" --services "${SERVICES[@]}" \
  --query 'services[].[serviceName,runningCount,desiredCount,length(deployments),deployments[0].rolloutState]' \
  --output text 2>/dev/null)"
if [[ -z "$ecs_out" ]]; then
  fail "ecs describe-services returned nothing for cluster '$ECS_CLUSTER' (cluster/service missing or no AWS access)"
else
  got=0
  while IFS=$'\t' read -r name running desired ndeploy rollout; do
    [[ -z "$name" ]] && continue
    got=$((got+1))
    if [[ "$running" == "$desired" ]] && [[ "${desired:-0}" =~ ^[0-9]+$ ]] && (( desired > 0 )); then
      if [[ "$ndeploy" == "1" ]]; then
        pass "ECS $name: running=$running/desired=$desired, single stable deployment (rollout=$rollout)"
      else
        fail "ECS $name: running=$running/desired=$desired but $ndeploy active deployments — rollout in progress (rollout=$rollout)"
      fi
    else
      fail "ECS $name: running=$running != desired=$desired (rollout=$rollout)"
    fi
  done <<< "$ecs_out"
  if (( got < ${#SERVICES[@]} )); then
    fail "ECS: expected ${#SERVICES[@]} services (${SERVICES[*]}) in '$ECS_CLUSTER', only $got reported"
  fi
fi
echo

# ── 3b. staged feature flags are explicit in the backend task definition ────
echo "── 3b. staged platform flags ──"
backend_td="$("${AWS[@]}" ecs describe-services --cluster "$ECS_CLUSTER" --services backend \
  --query 'services[0].taskDefinition' --output text 2>/dev/null || true)"
feature_names=(
  ORACLE_FEATURE_AUTOMATION
  ORACLE_FEATURE_MUNICIPAL_HARVESTS
  ORACLE_FEATURE_PREDICTIVE_INTELLIGENCE
  ORACLE_FEATURE_MARKETPLACE
  ORACLE_FEATURE_LOCAL_MODELS
  ORACLE_FEATURE_SPATIAL_TOURS
  ORACLE_FEATURE_CONTRACTS
)
if [[ -z "$backend_td" || "$backend_td" == "None" ]]; then
  fail "Could not resolve the backend task definition for feature-flag audit"
else
  feature_env="$("${AWS[@]}" ecs describe-task-definition --task-definition "$backend_td" \
    --query 'taskDefinition.containerDefinitions[0].environment' --output json 2>/dev/null || true)"
  for feature_name in "${feature_names[@]}"; do
    feature_value="$(python3 -c 'import json,sys; n=sys.argv[1]; rows=json.load(sys.stdin); print(next((str(x.get("value", "")).lower() for x in rows if x.get("name")==n), ""))' "$feature_name" <<<"${feature_env:-[]}" 2>/dev/null || true)"
    case "$feature_value" in
      true|false|1|0) pass "$feature_name is explicit ($feature_value)";;
      *) fail "$feature_name is absent or invalid in the backend task definition";;
    esac
  done
fi
echo

# ── 3c. database audit preload + platform alarms ────────────────────────────
echo "── 3c. audit and platform alarms ──"
cluster_pg="$("${AWS[@]}" rds describe-db-clusters --db-cluster-identifier neoh-prod-aurora \
  --query 'DBClusters[0].DBClusterParameterGroup' --output text 2>/dev/null || true)"
preload=""
if [[ -n "$cluster_pg" && "$cluster_pg" != "None" ]]; then
  preload="$("${AWS[@]}" rds describe-db-cluster-parameters --db-cluster-parameter-group-name "$cluster_pg" \
    --query 'Parameters[?ParameterName==`shared_preload_libraries`].ParameterValue | [0]' --output text 2>/dev/null || true)"
fi
if [[ "$preload" == *pgaudit* ]]; then
  pass "Aurora shared_preload_libraries includes pgaudit"
else
  fail "Aurora pgaudit preload is absent (parameter group=${cluster_pg:-unresolved})"
fi

platform_alarms=(
  neoh-prod-automation-job-failures
  neoh-prod-harvest-source-failures
  neoh-prod-stale-harvest-sources
)
alarm_rows="$("${AWS[@]}" cloudwatch describe-alarms --alarm-names "${platform_alarms[@]}" \
  --query 'MetricAlarms[].[AlarmName,StateValue]' --output text 2>/dev/null || true)"
for alarm_name in "${platform_alarms[@]}"; do
  alarm_state="$(awk -v name="$alarm_name" '$1==name {print $2}' <<<"$alarm_rows")"
  case "$alarm_state" in
    OK) pass "CloudWatch $alarm_name is OK";;
    INSUFFICIENT_DATA) warn "CloudWatch $alarm_name has insufficient data after deployment";;
    ALARM) fail "CloudWatch $alarm_name is ALARM";;
    *) fail "CloudWatch alarm missing: $alarm_name";;
  esac
done
echo

# ── 4. splat S3 bucket exists + splats/* public-read ─────────────────────────
echo "── 4. splat S3 bucket ──"
if "${AWS[@]}" s3api head-bucket --bucket "$SPLAT_BUCKET" >/dev/null 2>&1; then
  pass "S3 splat bucket exists: $SPLAT_BUCKET"

  pol="$("${AWS[@]}" s3api get-bucket-policy --bucket "$SPLAT_BUCKET" \
    --query 'Policy' --output text 2>/dev/null || true)"
  if printf '%s' "$pol" | grep -q 'splats/\*' && printf '%s' "$pol" | grep -q 's3:GetObject'; then
    pass "Bucket policy grants public read on splats/* (s3:GetObject)"
  else
    warn "Could not confirm a splats/* public-read (s3:GetObject) statement in the bucket policy"
  fi

  if [[ -n "${SPLAT_TEST_KEY:-}" ]]; then
    scode="$(get_code "$SPLAT_CDN_BASE/splats/${SPLAT_TEST_KEY#splats/}" -I)"
    if [[ "$scode" == "200" ]]; then
      pass "Anonymous HEAD splats/${SPLAT_TEST_KEY#splats/} -> 200 (public read works)"
    else
      fail "Anonymous HEAD splats/${SPLAT_TEST_KEY#splats/} -> $scode (expected 200)"
    fi
  else
    info "Skipping anonymous splat-object HEAD (set SPLAT_TEST_KEY=<path under splats/> to test a real object)"
  fi
else
  fail "S3 splat bucket NOT found or no access: $SPLAT_BUCKET"
fi
echo

# ── 5. contract vault bucket private + encrypted ────────────────────────────
echo "── 5. contract vault S3 bucket ──"
if "${AWS[@]}" s3api head-bucket --bucket "$CONTRACT_VAULT_BUCKET" >/dev/null 2>&1; then
  pass "S3 contract vault bucket exists: $CONTRACT_VAULT_BUCKET"

  pab="$("${AWS[@]}" s3api get-public-access-block --bucket "$CONTRACT_VAULT_BUCKET" \
    --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' \
    --output text 2>/dev/null || true)"
  if [[ "$pab" == $'True\tTrue\tTrue\tTrue' ]]; then
    pass "Contract vault blocks all public ACLs and public policies"
  else
    fail "Contract vault public-access block is not fully enabled: ${pab:-<missing>}"
  fi

  enc="$("${AWS[@]}" s3api get-bucket-encryption --bucket "$CONTRACT_VAULT_BUCKET" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
    --output text 2>/dev/null || true)"
  if [[ "$enc" == "AES256" ]]; then
    pass "Contract vault default encryption is AES256"
  else
    fail "Contract vault default encryption is ${enc:-<missing>} (expected AES256)"
  fi

  vstat="$("${AWS[@]}" s3api get-bucket-versioning --bucket "$CONTRACT_VAULT_BUCKET" \
    --query 'Status' --output text 2>/dev/null || true)"
  if [[ "$vstat" == "Enabled" ]]; then
    pass "Contract vault versioning is enabled"
  else
    warn "Contract vault versioning status is ${vstat:-<unset>} (expected Enabled)"
  fi

  vpol="$("${AWS[@]}" s3api get-bucket-policy --bucket "$CONTRACT_VAULT_BUCKET" \
    --query 'Policy' --output text 2>/dev/null || true)"
  if printf '%s' "$vpol" | grep -q 'DenyInsecureTransport'; then
    pass "Contract vault bucket policy denies non-TLS transport"
  else
    fail "Contract vault bucket policy does not show DenyInsecureTransport"
  fi
else
  fail "S3 contract vault bucket NOT found or no access: $CONTRACT_VAULT_BUCKET"
fi
echo

# ── summary ──────────────────────────────────────────────────────────────────
echo "════════ summary ════════"
echo "PASS=$PASS  WARN=$WARN  FAIL=$FAIL"
if (( FAIL > 0 )); then
  note=""; (( WARN > 0 )) && note=" ($WARN warning(s))"
  echo "RESULT: FAIL — $FAIL hard check(s) failed${note}."
  exit 1
fi
note=""; (( WARN > 0 )) && note=" with $WARN warning(s)"
echo "RESULT: PASS — all hard checks green${note}."
exit 0
