# NEOH Azure Deployment Context

## Current Deployment (As of 2026-07-27)

### Resources
| Resource | Name | Details |
|----------|------|--------|
| Subscription | Azure subscription 1 | `120ea104-5498-44f6-8e86-5654a1f4419b` |
| Resource Group | neoh | eastus (metadata), North Central US (runtime) |
| Container App (API) | neoh-api | 1.0 CPU / 2Gi, port 8000, user-assigned identity neoh-app-id |
| Container App (Web) | neoh-web | 0.5 CPU / 1Gi, port 8080, nginx static SPA |
| Sidecar (Scanning) | clamav | clamav/clamav:stable, 1.0 CPU / 2Gi |
| Container Registry | neoh120ea104 | `neoh120ea104.azurecr.io` |
| Key Vault | neoh-kv-120ea104 | `neoh-kv-120ea104.vault.azure.net` |
| PostgreSQL | neoh-db-120ea104 | Private VNet, Central US, cross-region peering |
| Environment | neoh-prod-env | North Central US, static IP 65.52.10.94 |

### Managed Identity
- Name: `neoh-app-id`
- Client ID: `584c137f-9aeb-4883-a49e-8127440a53e4`
- Roles: AcrPull, Cognitive Services User (neoh-resource), Key Vault Secrets User

### AI Services / Foundry
- Provider: `ORACLE_AI_CHAT_PROVIDER=azure-foundry`
- Project: `https://neoh-resource.services.ai.azure.com/api/projects/neoh`
- Agent: `neoh-kimi-k2-6`, version 2
- Model: `Kimi-K2.6`
- AIServices accounts: `neoh` (eastus), `neoh-resource` (eastus2)

### Key Vault Secrets
| Secret | Env Variable | Purpose |
|--------|-------------|---------|
| oracle-jwt-signing-key | ORACLE_SECRET_KEY | JWT signing (HS256) |
| oracle-encryption-master-key | ORACLE_ENCRYPTION_MASTER_KEY | PII encryption |
| oracle-db-app-password | ORACLE_DB_PASSWORD | oracle_app_login password |
| oracle-db-admin-password | ORACLE_DB_ADMIN_PASSWORD | DB admin credential |
| oracle-admin-passphrase | ORACLE_ADMIN_PASSPHRASE | Platform admin login |
| stripe-secret-key | STRIPE_SECRET_KEY | Stripe Live sk_live_* |
| stripe-webhook-secret | STRIPE_WEBHOOK_SECRET | Stripe webhook signature |
| stripe-price-id | STRIPE_PRICE_ID | Subscription price key |
| tavily-api-key | TAVILY_API_KEY | Web search API |

### Feature Flags
| Feature | Status | Env Var |
|---------|--------|---------|
| AI Chat | ✅ Enabled | ORACLE_FEATURE_AI_CHAT=true |
| Contracts | ✅ Enabled | ORACLE_FEATURE_CONTRACTS=true |
| Automation (email/call/calendar) | ✅ Enabled | ORACLE_FEATURE_AUTOMATION=true |
| Local Models (LoRA/vLLM) | ❌ Disabled | ORACLE_FEATURE_LOCAL_MODELS=false |
| Municipal Harvests | ✅ Enabled | ORACLE_FEATURE_MUNICIPAL_HARVESTS=true |
| Predictive Intelligence | ❌ Disabled | ORACLE_FEATURE_PREDICTIVE_INTELLIGENCE=false |
| Marketplace | ❌ Disabled | ORACLE_FEATURE_MARKETPLACE=false |
| Spatial Tours | ❌ Disabled | ORACLE_FEATURE_SPATIAL_TOURS=false |

### Database
- Host: `neoh-db-120ea104.postgres.database.azure.com:5432`
- Database: `oracle`
- App User: `oracle_app_login` (non-owner, FORCE RLS active)
- SSL Mode: `require`
- Pool: min=1, max=8
- Migrations: 0001-0049 applied; migration execution
  `neoh-db-migrate-06paofm` succeeded on 2026-07-27.

### URLs
- Web (SPA): `https://neoh-web.livelypebble-f08762d5.northcentralus.azurecontainerapps.io`
- API: `https://neoh-api.livelypebble-f08762d5.northcentralus.azurecontainerapps.io`
- Base URL: `https://neohrs.com` (DNS not yet delegated to Azure — currently at share-dns.com)
- JWT Issuer: `https://neohrs.com`
- JWT Audience: `neoh-web`

### Network
- VNet: `neoh-app-vnet` (runtime) peered to `neoh-prod-vnet` (database, Central US)
- Shared Azure Files mount: `/mnt/neoh` (contracts, audio, splats)
- ClamAV sidecar: loopback TCP 3310
- Outbound IPs: 20.25.244.13, 20.80.41.110, 20.80.41.109, 52.159.122.36, 20.241.96.28

### Scale
- neoh-api: 1-3 replicas
- neoh-web: 1-3 replicas
- Memory: neoh-api 2Gi, clamav 2Gi, neoh-web 1Gi (total pod: 2.0 CPU / 4Gi)

### Known Issues
- Local model path `/media/ydn/SYPHER_CORE/models/qwen-1.5b-valuation-lora.gguf` does not exist in container
- Custom domain `neohrs.com` DNS delegation pending (share-dns.com → Azure DNS)
- Direct MLS health is intentionally `empty` until licensed board credentials
  and `ORACLE_RESO_ALLOWED_HOSTS` are configured through Key Vault/Container Apps.

### Direct MLS and public-record pipeline

- API revision: `neoh-api--directmlsutv3` (100%, Healthy)
- Web revision: `neoh-web--nomlsui1` (100%, Healthy)
- API image: `neoh120ea104.azurecr.io/neoh/api:20260727-direct-mls-ut-v3`
- Web image: `neoh120ea104.azurecr.io/neoh-frontend:20260727-no-mls-ui-v1`
- Public property acquisition uses the bounded 50-state + DC municipal
  firehose with per-source checkpoints and health.
- Utah uses Utah County's official `TaxParcelAll` ArcGIS assessor layer; the
  retired `opendata.utah.gov` Socrata source is no longer called.
- MLS acquisition accepts multiple direct, licensed RESO Web API Property
  resources. Production requires an exact host allowlist; third-party
  aggregators and consumer portal scraping are prohibited.
- MLS facts remain a separate lead overlay. Matching requires parcel plus
  location evidence, or normalized address plus exact ZIP. Coordinates alone
  never establish identity.

### Recent Fixes (2026-07-18)
- Login fixed: SPA now calls neoh-api cross-origin (VITE_API_BASE + VITE_WS_URL baked in)
- Billing fixed: STRIPE_PRICE_ID added from Key Vault
- ClamAV fixed: memory increased 1Gi→2Gi, clamd now starts
- Memory Core fixed: platform admin profile seeded in user_profiles
- Frontend image: neoh-frontend:202607181013
- Backend image: neoh-backend:202607180216
