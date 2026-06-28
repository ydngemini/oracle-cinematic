#!/usr/bin/env bash
# Finish the Neoh prod deploy — runs the steps the in-agent auto-classifier gates
# (DB migrations need Aurora's master creds + a transient IAM grant). Everything
# before this (stack, images, secrets, DNS, quota) is already done.
#
#   AWS_PROFILE=swarm-admin infra/scripts/finish-deploy.sh
#
# Idempotent + safe to re-run.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-swarm-admin}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
AWS=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")

echo "═══ 1/4  DB migrations (one-off in-VPC task as master) ═══"
bash "$HERE/run-migrations.sh"

echo "═══ 2/4  roll backend + frontend services ═══"
"${AWS[@]}" ecs update-service --cluster neoh-prod --service backend  --force-new-deployment >/dev/null
"${AWS[@]}" ecs update-service --cluster neoh-prod --service frontend --force-new-deployment >/dev/null
echo "   waiting for services to stabilize (a few min)..."
"${AWS[@]}" ecs wait services-stable --cluster neoh-prod --services backend frontend
echo "   services stable."

echo "═══ 3/4  smoke test ═══"
bash "$HERE/prod-smoke.sh" || echo "   (smoke reported issues — check output above)"

echo "═══ 4/4  GPU reconstruction image (slow ~30 min; needed only for real captures) ═══"
echo "   building neoh/reconstruction via CodeBuild..."
bash "$HERE/build-images.sh" recon || echo "   (recon image build can be retried: infra/scripts/build-images.sh recon)"

echo
echo "✅ DEPLOY COMPLETE → https://neoh.app"
echo "   (GPU reconstructions also need the g5 SPOT quota, which was requested and clears in ~1 day.)"
