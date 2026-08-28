#!/usr/bin/env bash
# Patch one or more plain env vars onto the LIVE backend task def and roll, without a
# full `terraform apply` (which would also pull in ecs.tf-only vars like STRIPE_PRICE_ID).
# Copies the currently-running revision so existing env is preserved.
#
#   AWS_PROFILE=neoh bash infra/scripts/set-backend-env.sh KEY=VAL [KEY=VAL ...]
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: set-backend-env.sh KEY=VAL [KEY=VAL ...]"; exit 1; }
AWS=(aws --profile "${AWS_PROFILE:-neoh}" --region "${AWS_REGION:-us-east-1}")

LIVE=$("${AWS[@]}" ecs describe-services --cluster neoh-prod --services backend \
  --query 'services[0].taskDefinition' --output text)
echo ">> live task def: ${LIVE##*/}"
"${AWS[@]}" ecs describe-task-definition --task-definition "$LIVE" --query 'taskDefinition' --output json > /tmp/td.json

# NOTE: stdout is redirected to the file, so the JSON must be the ONLY thing printed
# to stdout — status goes to stderr, or it corrupts the task-def JSON.
PAIRS="$*" python3 - <<'PY' > /tmp/td-new.json
import json, os, sys
td = json.load(open("/tmp/td.json"))
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy","deregisteredAt"):
    td.pop(k, None)
c = td["containerDefinitions"][0]
env = {e["name"]: e["value"] for e in c.get("environment", [])}
for pair in os.environ["PAIRS"].split():
    k, _, v = pair.partition("=")
    env[k] = v
c["environment"] = [{"name": k, "value": v} for k, v in env.items()]
sys.stderr.write(">> set: " + ", ".join(os.environ["PAIRS"].split()) + "\n")
sys.stdout.write(json.dumps(td))
PY
python3 -c "import json;json.load(open('/tmp/td-new.json'))" || { echo "!! td-new.json invalid"; exit 1; }

TD=$("${AWS[@]}" ecs register-task-definition --cli-input-json file:///tmp/td-new.json --query 'taskDefinition.taskDefinitionArn' --output text)
echo ">> registered ${TD##*/}"
"${AWS[@]}" ecs update-service --cluster neoh-prod --service backend --task-definition "$TD" --force-new-deployment >/dev/null
echo ">> rolling backend (a few min)..."
"${AWS[@]}" ecs wait services-stable --cluster neoh-prod --services backend
echo "✅ backend rolled with new env."
