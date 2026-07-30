#!/bin/bash
# Purchase neohr.app domain using Cloudflare API
# Cloudflare sells domains at-cost (no markup)

set -e

DOMAIN="neohr.app"

echo "=== Purchasing $DOMAIN via Cloudflare Registrar ==="
echo ""
echo "Cloudflare sells domains at-cost (no markup)"
echo ".app domains: ~$12/year"
echo ""
echo "Prerequisites:"
echo "1. Go to https://dash.cloudflare.com/sign-up"
echo "2. Create account and verify email"
echo "3. Go to https://dash.cloudflare.com/?to=/:account/api-keys"
echo "4. Create API token with 'All zones' permissions"
echo "5. Add payment method in Cloudflare dashboard"
echo ""

read -p "Enter your Cloudflare API token: " CF_API_TOKEN
read -p "Enter your Cloudflare account email: " CF_EMAIL
read -p "Enter contact email for domain: " CONTACT_EMAIL

# Get account ID
echo ""
echo "Fetching Cloudflare account..."
ACCOUNTS=$(curl -s -X GET "https://api.cloudflare.com/client/v4/accounts" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  -H "Content-Type: application/json")

ACCOUNT_ID=$(echo "$ACCOUNTS" | jq -r '.result[0].id 2>/dev/null || echo "error"')

if [ "$ACCOUNT_ID" = "error" ] || [ -z "$ACCOUNT_ID" ]; then
  echo "Failed to get account ID. Check your API token."
  exit 1
fi

echo "Account ID: $ACCOUNT_ID"
echo ""
echo "Checking domain availability..."

# Check availability (note: Cloudflare API requires additional endpoints)
# Direct purchase needs to go through dashboard for regulatory compliance

echo ""
echo "=== IMPORTANT ==="
echo "Cloudflare requires domain purchase through their dashboard for ICANN compliance."
echo "You cannot purchase domains via API directly."
echo ""
echo " QUICK PURCHASE STEPS:"
echo ""
echo "1. Open: https://domains.cloudflare.com/?domain=neohr.app"
echo "2. Click 'Register'"
echo "3. Enter contact details"
echo "4. Payment: ~$12 for 1 year"
echo "5. After purchase, go to: https://dash.cloudflare.com"
echo "6. Navigate to: $DOMAIN > Overview > Nameservers"
echo "7. Select 'Use my own nameservers'"
echo "8. Add Azure nameservers:"
echo "   ns1-03.azure-dns.com"
echo "   ns2-03.azure-dns.net"
echo "   ns3-03.azure-dns.org"
echo "   ns4-03.azure-dns.info"
echo ""
echo "=== ALTERNATIVE: Quick Link ==="
echo ""
echo "https://dash.cloudflare.com/?to=/:account/domains/register?q=neohr.app"
