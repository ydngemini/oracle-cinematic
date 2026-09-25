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

# One-shot: inject STRIPE_PRICE_ID into the prod backend task def + roll, so Stripe
# checkout stops failing with "No such price: price_REPLACE_ME". Reads the price
# from .env (STRIPE_PRICE_ID) or $1. Also added to ecs.tf for terraform persistence.
#
#   AWS_PROFILE=neoh bash infra/scripts/fix-stripe-price.sh
set -euo pipefail
AWS=(aws --profile "${AWS_PROFILE:-neoh}" --region "${AWS_REGION:-us-east-1}")
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PRICE="${1:-$(grep -E '^STRIPE_PRICE_ID' "$ROOT/.env" | head -1 | cut -d= -f2 | tr -d '\"'"'"' ')}"
case "$PRICE" in price_*) ;; *) echo "!! no valid STRIPE_PRICE_ID (got '$PRICE')"; exit 1;; esac
echo ">> price: $PRICE"

"${AWS[@]}" ecs describe-task-definition --task-definition neoh-prod-backend --query 'taskDefinition' --output json > /tmp/td.json
PRICE="$PRICE" python3 - <<'PY' > /tmp/td-new.json
import json, os
td = json.load(open("/tmp/td.json"))
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy","deregisteredAt"):
    td.pop(k, None)
c = td["containerDefinitions"][0]
env = [e for e in c.get("environment", []) if e["name"] != "STRIPE_PRICE_ID"]
env.append({"name": "STRIPE_PRICE_ID", "value": os.environ["PRICE"]})
c["environment"] = env
json.dump(td, open("/tmp/td-new.json", "w"))
PY
TD=$("${AWS[@]}" ecs register-task-definition --cli-input-json file:///tmp/td-new.json --query 'taskDefinition.taskDefinitionArn' --output text)
echo ">> registered $TD"
"${AWS[@]}" ecs update-service --cluster neoh-prod --service backend --task-definition "$TD" --force-new-deployment >/dev/null
echo ">> rolling backend (a few min)..."
"${AWS[@]}" ecs wait services-stable --cluster neoh-prod --services backend
echo "✅ STRIPE_PRICE_ID live on backend. Retry checkout."
