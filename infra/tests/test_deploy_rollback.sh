#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
LOG="$(mktemp)"
OUT="$(mktemp)"
trap 'rm -f "$LOG" "$OUT"' EXIT

set +e
PATH="$HERE/fake-bin:$PATH" \
FAKE_AWS_LOG="$LOG" \
AWS_PROFILE=test \
AWS_REGION=us-east-1 \
APP_URL=https://example.test \
bash "$ROOT/infra/scripts/deploy-update.sh" >"$OUT" 2>&1
status=$?
set -e

if [[ "$status" == "0" ]]; then
  echo "expected deployment failure did not occur" >&2
  cat "$OUT" >&2
  exit 1
fi

grep -q -- '--service backend --task-definition arn:new-task' "$LOG"
grep -q -- '--service backend --task-definition arn:old-backend' "$LOG"
grep -q -- '--service frontend --task-definition arn:old-frontend' "$LOG"
grep -q -- 'iam delete-role-policy --role-name test-role --policy-name migrate-master-secret' "$LOG"
grep -q -- 'deployment failed — rolling ECS services back' "$OUT"
grep -q -- 'rollback complete' "$OUT"

echo "deploy rollback: all assertions passed"
