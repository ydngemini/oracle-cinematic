#!/bin/bash
# Purchase neohr.app domain on Porkbun
# Requires Porkbun API key from https://porkbun.com/account/api

set -e

DOMAIN="neohr.app"
TLD="app"

echo "=== Purchasing $DOMAIN on Porkbun ==="
echo ""
echo "Prerequisites:"
echo "1. Go to https://porkbun.com/account/api"
echo "2. Generate API key and Secret key"
echo "3. Add funds to your account (balance needed: ~$12)"
echo ""

read -p "Enter your Porkbun API key: " API_KEY
read -p "Enter your Porkbun Secret key: " SECRET_KEY
read -p "Enter contact email for domain: " CONTACT_EMAIL

# Porkbun API endpoint
API_URL="https://api.porkbun.com/api/json/v3"

echo ""
echo "Checking domain availability..."

# Check availability
RESULT=$(curl -s -X POST "$API_URL/domain/checkAvailability" \
  -H "Content-Type: application/json" \
  -d "{
    \"apikey\": \"$API_KEY\",
    \"secretapikey\": \"$SECRET_KEY\",
    \"domain\": \"$DOMAIN\"
  }")

if echo "$RESULT" | grep -q '"status":"SUCCESS"'; then
  echo "Domain $DOMAIN is available!"
else
  echo "Domain check result: $RESULT"
  echo ""
  read -p "Continue with purchase? [y/N] " CONFIRM
  if [[ "$CONFIRM" != "y" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

echo ""
echo "Purchasing $DOMAIN..."

# Purchase domain
PURCHASE=$(curl -s -X POST "$API_URL/domain/register" \
  -H "Content-Type: application/json" \
  -d "{
    \"apikey\": \"$API_KEY\",
    \"secretapikey\": \"$SECRET_KEY\",
    \"domain\": \"$DOMAIN\",
    \"period\": 1,
    \"auto_renew\": true,
    \"admin_email\": \"$CONTACT_EMAIL\",
    \"whois_privacy\": true
  }")

if echo "$PURCHASE" | grep -q '"status":"SUCCESS"'; then
  echo ""
  echo "SUCCESS! Domain purchased: $DOMAIN"
  echo ""
  echo "Next steps:"
  echo "1. Go to https://porkbun.com/account/domains"
  echo "2. Click on $DOMAIN"
  echo "3. Go to 'Nameservers' and select 'Use porkbun nameservers'"
  echo "4. Then update nameservers to Azure DNS:"
  echo "   - ns1-03.azure-dns.com"
  echo "   - ns2-03.azure-dns.net"
  echo "   - ns3-03.azure-dns.org"
  echo "   - ns4-03.azure-dns.info"
else
  echo "Purchase failed: $PURCHASE"
  exit 1
fi
