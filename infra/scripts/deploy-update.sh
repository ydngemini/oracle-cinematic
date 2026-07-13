#!/usr/bin/env bash
# Ship the latest built images + migrations to prod. Run AFTER
# `infra/scripts/build-images.sh app` has pushed fresh backend/frontend/observability
# images.
#
#   AWS_PROFILE=swarm-admin infra/scripts/deploy-update.sh
#
# Registers a DIGEST-PINNED task-def revision per service from the current :latest
# image, so ECS runs the exact freshly-built image (never a cached :latest digest),
# then migrates on the new backend image and rolls both services. Idempotent.
set -Eeuo pipefail
export AWS_PROFILE="${AWS_PROFILE:-swarm-admin}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")
ACCT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
CLUSTER=neoh-prod
APP_URL="${APP_URL:-https://neoh.app}"

OLD_BE_TD=$("${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services backend --query 'services[0].taskDefinition' --output text)
OLD_FE_TD=$("${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services frontend --query 'services[0].taskDefinition' --output text)
OLD_OBS_TD=""
ROLLBACK_ARMED=0

rollback_on_error () {
  local code="$1"
  trap - ERR
  set +e
  if [[ "$ROLLBACK_ARMED" == "1" ]]; then
    echo "!! deployment failed — rolling ECS services back to their prior task definitions"
    "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service backend --task-definition "$OLD_BE_TD" --force-new-deployment >/dev/null
    "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service frontend --task-definition "$OLD_FE_TD" --force-new-deployment >/dev/null
    local rollback_services=(backend frontend)
    if [[ -n "$OLD_OBS_TD" ]]; then
      "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service observability --task-definition "$OLD_OBS_TD" --force-new-deployment >/dev/null
      rollback_services+=(observability)
    fi
    "${AWS[@]}" ecs wait services-stable --cluster "$CLUSTER" --services "${rollback_services[@]}"
    echo "!! rollback complete; additive migrations remain in place for forward compatibility"
  fi
  exit "$code"
}
trap 'rollback_on_error $?' ERR

backend_stage_smoke () {
  local health policy
  health=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$APP_URL/health")
  policy=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$APP_URL/api/intelligence/policy")
  [[ "$health" == "200" ]] || { echo "!! staged backend health returned $health"; return 1; }
  case "$policy" in
    200|401|403) ;;
    *) echo "!! staged intelligence policy route returned $policy"; return 1;;
  esac
  echo "   staged backend health=200 intelligence-policy=$policy"
}

pin () {  # $1=family  $2=repo  → echoes a new digest-pinned task-def ARN
  local family="$1" repo="$2" digest img
  digest=$("${AWS[@]}" ecr describe-images --repository-name "$repo" --image-ids imageTag=latest \
    --query 'imageDetails[0].imageDigest' --output text)
  img="${ACCT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${repo}@${digest}"
  "${AWS[@]}" ecs describe-task-definition --task-definition "$family" --query 'taskDefinition' --output json > /tmp/td-src.json
  IMG="$img" python3 - <<'PY' > /tmp/td-new.json
import json, os
td = json.load(open("/tmp/td-src.json"))
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy","deregisteredAt"):
    td.pop(k, None)
td["containerDefinitions"][0]["image"] = os.environ["IMG"]
json.dump(td, open("/tmp/td-new.json","w"))
PY
  "${AWS[@]}" ecs register-task-definition --cli-input-json file:///tmp/td-new.json --query 'taskDefinition.taskDefinitionArn' --output text
}

echo "═══ pin freshly-built images by digest ═══"
BE_TD=$(pin neoh-prod-backend  neoh/backend);  echo "   backend  → ${BE_TD##*/}"
FE_TD=$(pin neoh-prod-frontend neoh/frontend); echo "   frontend → ${FE_TD##*/}"
OBS_TD=""
OBS_STATUS=$("${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services observability \
  --query 'services[0].status' --output text 2>/dev/null || true)
if [[ "$OBS_STATUS" == "ACTIVE" ]]; then
  OLD_OBS_TD=$("${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services observability --query 'services[0].taskDefinition' --output text)
  OBS_TD=$(pin neoh-prod-observability neoh/observability)
  echo "   observability → ${OBS_TD##*/}"
fi

echo "═══ 1/3  migrations (on the new backend image) ═══"
MIGRATION_TASK_DEF="$BE_TD" bash "$HERE/run-migrations.sh"

echo "═══ 2/3  staged rollout onto pinned revisions ═══"
ROLLBACK_ARMED=1
"${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service backend  --task-definition "$BE_TD" --force-new-deployment >/dev/null
echo "   waiting for backend stability..."
"${AWS[@]}" ecs wait services-stable --cluster "$CLUSTER" --services backend
backend_stage_smoke

"${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service frontend --task-definition "$FE_TD" --force-new-deployment >/dev/null
echo "   waiting for frontend stability..."
"${AWS[@]}" ecs wait services-stable --cluster "$CLUSTER" --services frontend
SERVICES=(backend frontend)
if [[ -n "$OBS_TD" ]]; then
  "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service observability --task-definition "$OBS_TD" --force-new-deployment >/dev/null
  SERVICES+=(observability)
fi
echo "   waiting for complete service stability..."
"${AWS[@]}" ecs wait services-stable --cluster "$CLUSTER" --services "${SERVICES[@]}"

echo "═══ 3/3  smoke test ═══"
bash "$HERE/prod-smoke.sh"
ROLLBACK_ARMED=0
trap - ERR
echo "✅ update live → https://neoh.app"
