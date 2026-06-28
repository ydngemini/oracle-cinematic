#!/usr/bin/env bash
# Enable password-reset emails (forgot-password) via Amazon SES on neoh.app.
# Creates the SES domain identity + DKIM records in Route53, grants the ECS task
# role ses:SendEmail, and requests production access (SES starts in sandbox —
# until prod access is granted you can only email VERIFIED addresses).
#
#   AWS_PROFILE=swarm-admin infra/scripts/setup-ses.sh
#
# Idempotent. The app sends from no-reply@neoh.app (override ORACLE_SES_SENDER).
set -euo pipefail
AWS=(aws --profile "${AWS_PROFILE:-swarm-admin}" --region "${AWS_REGION:-us-east-1}")
ZONE=Z021772621TQAYQBIMBZR   # neoh.app hosted zone

echo ">> creating SES domain identity neoh.app (DKIM)"
"${AWS[@]}" sesv2 create-email-identity --email-identity neoh.app >/dev/null 2>&1 || echo "   (identity may already exist)"
TOKENS=$("${AWS[@]}" sesv2 get-email-identity --email-identity neoh.app --query 'DkimAttributes.Tokens' --output json)

echo ">> writing DKIM CNAMEs to Route53"
python3 - "$ZONE" <<PY > /tmp/ses-dkim-batch.json
import json,sys
toks=json.loads('''$TOKENS''')
ch=[{"Action":"UPSERT","ResourceRecordSet":{"Name":f"{t}._domainkey.neoh.app","Type":"CNAME","TTL":1800,"ResourceRecords":[{"Value":f"{t}.dkim.amazonses.com"}]}} for t in toks]
json.dump({"Changes":ch}, open("/tmp/ses-dkim-batch.json","w"))
print(f"{len(ch)} records", file=sys.stderr)
PY
"${AWS[@]}" route53 change-resource-record-sets --hosted-zone-id "$ZONE" --change-batch file:///tmp/ses-dkim-batch.json --query 'ChangeInfo.Status' --output text

echo ">> granting the ECS task role ses:SendEmail"
ROLE=$("${AWS[@]}" ecs describe-task-definition --task-definition neoh-prod-backend \
  --query 'taskDefinition.taskRoleArn' --output text); ROLE="${ROLE##*/}"
"${AWS[@]}" iam put-role-policy --role-name "$ROLE" --policy-name neoh-ses-send \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}]}'

echo ">> requesting SES production access (sandbox → prod, ~24h review)"
"${AWS[@]}" sesv2 put-account-details \
  --production-access-enabled \
  --mail-type TRANSACTIONAL \
  --website-url https://neoh.app \
  --use-case-description "Transactional password-reset and account emails for Neoh real-estate CRM users." \
  --contact-language EN 2>&1 | tail -1 || echo "   (request may already be pending; check the SES console)"

echo ">> done. DKIM verifies in minutes-hours; until prod access lands, SES only"
echo "   emails VERIFIED addresses (verify yours: aws sesv2 create-email-identity --email-identity you@x.com)."
