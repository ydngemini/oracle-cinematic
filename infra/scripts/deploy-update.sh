#!/usr/bin/env bash
# Ship the latest built images + any new migrations to prod. Run AFTER
# `infra/scripts/build-images.sh app` has pushed fresh backend/frontend images.
#
#   AWS_PROFILE=swarm-admin infra/scripts/deploy-update.sh
#
# Idempotent: migrations are IF-NOT-EXISTS; the rollout is a force-new-deployment.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-swarm-admin}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")

echo "═══ 1/3  migrations (applies any new 00NN_*.sql; idempotent) ═══"
bash "$HERE/run-migrations.sh"

echo "═══ 2/3  roll backend + frontend onto the new images ═══"
"${AWS[@]}" ecs update-service --cluster neoh-prod --service backend  --task-definition neoh-prod-backend --force-new-deployment >/dev/null
"${AWS[@]}" ecs update-service --cluster neoh-prod --service frontend --force-new-deployment >/dev/null
echo "   waiting for stability (a few min)..."
"${AWS[@]}" ecs wait services-stable --cluster neoh-prod --services backend frontend

echo "═══ 3/3  smoke test ═══"
bash "$HERE/prod-smoke.sh" || echo "   (smoke reported issues — see above)"
echo "✅ update live → https://neoh.app"
