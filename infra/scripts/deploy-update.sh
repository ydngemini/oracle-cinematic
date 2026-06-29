#!/usr/bin/env bash
# Ship the latest built images + migrations to prod. Run AFTER
# `infra/scripts/build-images.sh app` has pushed fresh backend/frontend images.
#
#   AWS_PROFILE=swarm-admin infra/scripts/deploy-update.sh
#
# Registers a DIGEST-PINNED task-def revision per service from the current :latest
# image, so ECS runs the exact freshly-built image (never a cached :latest digest),
# then migrates on the new backend image and rolls both services. Idempotent.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-swarm-admin}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")
ACCT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
CLUSTER=neoh-prod

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

echo "═══ 1/3  migrations (on the new backend image) ═══"
MIGRATION_TASK_DEF="$BE_TD" bash "$HERE/run-migrations.sh"

echo "═══ 2/3  roll services onto the pinned revisions ═══"
"${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service backend  --task-definition "$BE_TD" --force-new-deployment >/dev/null
"${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service frontend --task-definition "$FE_TD" --force-new-deployment >/dev/null
echo "   waiting for stability (a few min)..."
"${AWS[@]}" ecs wait services-stable --cluster "$CLUSTER" --services backend frontend

echo "═══ 3/3  smoke test ═══"
bash "$HERE/prod-smoke.sh" || echo "   (smoke reported issues — see above)"
echo "✅ update live → https://neoh.app"
