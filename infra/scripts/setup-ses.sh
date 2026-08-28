#!/usr/bin/env bash
# Enable password-reset emails (forgot-password) via Amazon SES on neohrs.com.
# Creates the SES domain identity, publishes/report the DKIM CNAMEs, grants the
# ECS task role ses:SendEmail, and requests production access (SES starts in
# sandbox — until prod access is granted you can only email VERIFIED addresses).
#
#   AWS_PROFILE=neoh infra/scripts/setup-ses.sh
#
# Idempotent. The app sends from no-reply@neohrs.com (override ORACLE_SES_SENDER).
#
# DNS: neohrs.com is NOT hosted in Route53 — its nameservers are Hostinger's
# (a.share-dns.com / b.share-dns.net). The old version of this script UPSERTed
# into hosted zone Z021772621TQAYQBIMBZR, which lived in the retired account
# 404870839825; against this account that call fails outright. So the DKIM step
# writes to Route53 only when a hosted zone for the domain actually exists here,
# and otherwise prints the records for entry at the registrar. Printing is the
# normal path today, not a fallback for an error.
set -euo pipefail
AWS=(aws --profile "${AWS_PROFILE:-neoh}" --region "${AWS_REGION:-us-east-1}")
DOMAIN="${SES_DOMAIN:-neohrs.com}"

echo ">> creating SES domain identity ${DOMAIN} (DKIM)"
"${AWS[@]}" sesv2 create-email-identity --email-identity "$DOMAIN" >/dev/null 2>&1 \
  || echo "   (identity may already exist)"
TOKENS=$("${AWS[@]}" sesv2 get-email-identity --email-identity "$DOMAIN" \
  --query 'DkimAttributes.Tokens' --output json)

# Route53 zone for this domain in THIS account, if any. Trailing dot matters.
ZONE=$("${AWS[@]}" route53 list-hosted-zones-by-name --dns-name "${DOMAIN}." \
  --query "HostedZones[?Name=='${DOMAIN}.'].Id | [0]" --output text 2>/dev/null || true)
ZONE="${ZONE##*/}"

if [ -n "$ZONE" ] && [ "$ZONE" != "None" ]; then
  echo ">> writing DKIM CNAMEs to Route53 zone $ZONE"
  python3 - "$DOMAIN" <<PY
import json,sys
domain=sys.argv[1]
toks=json.loads('''$TOKENS''')
ch=[{"Action":"UPSERT","ResourceRecordSet":{"Name":f"{t}._domainkey.{domain}","Type":"CNAME","TTL":1800,"ResourceRecords":[{"Value":f"{t}.dkim.amazonses.com"}]}} for t in toks]
json.dump({"Changes":ch}, open("/tmp/ses-dkim-batch.json","w"))
print(f"{len(ch)} records", file=sys.stderr)
PY
  "${AWS[@]}" route53 change-resource-record-sets --hosted-zone-id "$ZONE" \
    --change-batch file:///tmp/ses-dkim-batch.json --query 'ChangeInfo.Status' --output text
else
  echo ">> no Route53 zone for ${DOMAIN} in this account — add these at the registrar:"
  python3 - "$DOMAIN" <<PY
import json,sys
domain=sys.argv[1]
toks=json.loads('''$TOKENS''')
for t in toks:
    print(f"   CNAME  {t}._domainkey.{domain}  ->  {t}.dkim.amazonses.com")
if not toks:
    print("   (SES returned no DKIM tokens yet — re-run in a minute)")
PY
  echo "   SES will not verify the identity until all three CNAMEs resolve."
fi

echo ">> granting the ECS task role ses:SendEmail"
ROLE=$("${AWS[@]}" ecs describe-task-definition --task-definition neoh-prod-backend \
  --query 'taskDefinition.taskRoleArn' --output text); ROLE="${ROLE##*/}"
"${AWS[@]}" iam put-role-policy --role-name "$ROLE" --policy-name neoh-ses-send \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}]}'

echo ">> requesting SES production access (sandbox → prod, ~24h review)"
"${AWS[@]}" sesv2 put-account-details \
  --production-access-enabled \
  --mail-type TRANSACTIONAL \
  --website-url "https://${DOMAIN}" \
  --use-case-description "Transactional password-reset and account emails for Neoh real-estate CRM users." \
  --contact-language EN 2>&1 | tail -1 || echo "   (request may already be pending; check the SES console)"

echo ">> done. DKIM verifies in minutes-hours; until prod access lands, SES only"
echo "   emails VERIFIED addresses (verify yours: aws sesv2 create-email-identity --email-identity you@x.com)."
