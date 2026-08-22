#!/usr/bin/env bash
# Deliver the Fireworks API key to the deployed containers.
#
# Follows the pattern already used for admin-passphrase / redis-url / stripe:
# versionless Key Vault secret -> Container Apps secret reference bound through
# the neoh-app-id managed identity -> env var. The key never touches source,
# an image layer, or a literal Container Apps environment value.
#
# Usage:
#   export FIREWORKS_API_KEY=fw_...      # or leave unset to read from ~/.bashrc
#   ./infra/scripts/set-fireworks-secret.sh
#
# Requires: az login, and a subscription that is NOT disabled.
#
# While the subscription is suspended NOTHING here works, including step 1.
# Key Vault still answers metadata calls -- `az keyvault secret list` returns
# every name -- but the data plane is Forbidden, so reading or writing a secret
# VALUE fails:
#   ERROR: (Forbidden) The subscription associated with this vault has been disabled.
# A working `secret list` is therefore not evidence that this script can run.

set -euo pipefail

VAULT="neoh-kv-120ea104"
RG="neoh"
SECRET_NAME="fireworks-api-key"
IDENTITY="/subscriptions/120ea104-5498-44f6-8e86-5654a1f4419b/resourcegroups/neoh/providers/Microsoft.ManagedIdentity/userAssignedIdentities/neoh-app-id"
# Container Apps that run backend code and therefore need inference.
APPS=(neoh-api)
MODEL="${ORACLE_FIREWORKS_MODEL:-accounts/fireworks/models/kimi-k2p7-code}"

key="${FIREWORKS_API_KEY:-}"
if [[ -z "$key" ]]; then
  key="$(grep -hoP 'FIREWORKS_API_KEY=["'"'"']?\K[^"'"'"']+' "$HOME/.bashrc" 2>/dev/null | tail -1 || true)"
fi
if [[ -z "$key" ]]; then
  echo "FIREWORKS_API_KEY is not set and none was found in ~/.bashrc" >&2
  exit 1
fi

echo "==> Writing $SECRET_NAME to Key Vault $VAULT"
az keyvault secret set \
  --vault-name "$VAULT" \
  --name "$SECRET_NAME" \
  --value "$key" \
  --output none
echo "    stored."

# Versionless URI: Container Apps then picks up a rotated value without a
# revision edit, matching how redis-url and admin-passphrase are bound.
SECRET_URI="https://${VAULT}.vault.azure.net/secrets/${SECRET_NAME}"

for app in "${APPS[@]}"; do
  echo "==> Binding secret reference on $app"
  az containerapp secret set \
    --name "$app" --resource-group "$RG" \
    --secrets "${SECRET_NAME}=keyvaultref:${SECRET_URI},identityref:${IDENTITY}" \
    --output none

  echo "==> Setting env vars on $app"
  az containerapp update \
    --name "$app" --resource-group "$RG" \
    --set-env-vars \
      "ORACLE_AI_CHAT_PROVIDER=fireworks" \
      "ORACLE_FIREWORKS_API_KEY=secretref:${SECRET_NAME}" \
      "ORACLE_FIREWORKS_MODEL=${MODEL}" \
    --output none
  echo "    $app updated."
done

echo
echo "Done. Verify with:"
echo "  az containerapp show -n neoh-api -g $RG \\"
echo "    --query \"properties.template.containers[0].env[?starts_with(name,'ORACLE_FIREWORKS') || name=='ORACLE_AI_CHAT_PROVIDER']\" -o table"
echo
echo "The value must render as secretRef, never as a literal."
