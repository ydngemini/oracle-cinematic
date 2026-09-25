#!/usr/bin/env bash
# ── LEGACY: AWS — NOT Neoh's production deploy ─────────────────────────────
# Neoh production runs on DigitalOcean App Platform (docs/deploy-digitalocean.md
# since 2026-09-21). This script targets the RETIRED AWS ECS stack. Its own
# header may still say "prod"; that was true once and is not now.
#
# It refuses to run unless you state you mean AWS, so nobody runs it believing
# it deploys Neoh. Scripts that call each other inherit the variable, so the
# legacy path still works end to end for anyone who genuinely needs it.
if [ "${NEOH_LEGACY_AWS:-}" != "1" ]; then
  cat >&2 <<'LEGACY_AWS'

  REFUSING: this is the retired AWS deploy path, not Neoh production.

  Neoh production is DigitalOcean App Platform:
    - deploy:    GitHub Actions -> CI -> Run workflow on main, confirm=deploy
    - rollback:  scripts/rollback.sh --to <release-manifest.json>
    - runbook:   docs/deploy-digitalocean.md
    - checklist: docs/release-checklist.md

  If you really do mean the legacy AWS stack, re-run with NEOH_LEGACY_AWS=1.

LEGACY_AWS
  exit 64
fi

# Apply DB migrations to prod Aurora via a one-off in-VPC ECS task (Aurora is
# private). Grants the ECS task role a least-privilege read on ONLY the Aurora
# master secret, then runs backend/run_migrations.py (baked into the image) as a
# one-off task on the backend task def. Idempotent migrations → safe to re-run.
#
#   AWS_PROFILE=neoh infra/scripts/run-migrations.sh
#
# Requires: the backend image already pushed to ECR (infra/scripts/build-images.sh app).
set -euo pipefail
AWS=(aws --profile "${AWS_PROFILE:-neoh}" --region "${AWS_REGION:-us-east-1}")
CLUSTER=neoh-prod
ROLE=""
GRANT_CREATED=0

cleanup_migration_grant() {
  if [[ "$GRANT_CREATED" == "1" && -n "$ROLE" ]]; then
    echo ">> revoking temporary migration secret grant"
    "${AWS[@]}" iam delete-role-policy \
      --role-name "$ROLE" --policy-name migrate-master-secret >/dev/null 2>&1 || true
    GRANT_CREATED=0
  fi
}
trap cleanup_migration_grant EXIT

echo ">> resolving backend service network + task def"
NET=$("${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services backend \
  --query 'services[0].{td:taskDefinition,subnets:networkConfiguration.awsvpcConfiguration.subnets,sgs:networkConfiguration.awsvpcConfiguration.securityGroups}' --output json)
TD=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["td"])' <<<"$NET")
# Allow the caller (deploy-update.sh) to pin a specific (digest-pinned) revision so
# the migration runs the freshly-built image, not a cached :latest digest.
[ -n "${MIGRATION_TASK_DEF:-}" ] && TD="$MIGRATION_TASK_DEF"
SUBNETS=$(python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["subnets"]))' <<<"$NET")
SGS=$(python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["sgs"]))' <<<"$NET")
ROLE_ARN=$("${AWS[@]}" ecs describe-task-definition --task-definition "$TD" --query 'taskDefinition.taskRoleArn' --output text)
ROLE="${ROLE_ARN##*/}"
MASTER_ARN=$("${AWS[@]}" rds describe-db-clusters --db-cluster-identifier neoh-prod-aurora \
  --query 'DBClusters[0].MasterUserSecret.SecretArn' --output text)
echo "   task-def=$TD role=$ROLE"
echo "   master-secret=$MASTER_ARN"

echo ">> granting task role least-privilege read on the master secret (+ KMS decrypt via Secrets Manager)"
"${AWS[@]}" iam put-role-policy --role-name "$ROLE" --policy-name migrate-master-secret \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"$MASTER_ARN\"},{\"Effect\":\"Allow\",\"Action\":\"kms:Decrypt\",\"Resource\":\"*\",\"Condition\":{\"StringEquals\":{\"kms:ViaService\":\"secretsmanager.${AWS_REGION:-us-east-1}.amazonaws.com\"}}}]}"
GRANT_CREATED=1
sleep 10 # IAM propagation

echo ">> launching one-off migration task"
OVERRIDES=$(cat <<JSON
{"containerOverrides":[{"name":"backend","command":["python","/app/run_migrations.py"],
  "environment":[{"name":"DB_MASTER_SECRET_ARN","value":"$MASTER_ARN"}]}]}
JSON
)
NETCFG="awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SGS],assignPublicIp=DISABLED}"
ARN=$("${AWS[@]}" ecs run-task --cluster "$CLUSTER" --task-definition "$TD" --launch-type FARGATE \
  --network-configuration "$NETCFG" --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)
echo "   task: $ARN"
echo ">> waiting for migration task to stop..."
"${AWS[@]}" ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$ARN"
CODE=$("${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
REASON=$("${AWS[@]}" ecs describe-tasks --cluster "$CLUSTER" --tasks "$ARN" \
  --query 'tasks[0].stoppedReason' --output text)
echo ">> migration task exitCode=$CODE ($REASON)"
[ "$CODE" = "0" ] || { echo "!! migrations FAILED — check CloudWatch logs for the task"; exit 1; }
echo ">> migrations applied."
cleanup_migration_grant
echo ">> done"
