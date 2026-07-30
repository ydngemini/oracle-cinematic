# NEOH Azure production deployment

This deployment keeps application secrets out of source and images. Azure
Container Apps uses the `neoh-app-id` user-assigned identity to pull from ACR,
read versionless Key Vault references, and invoke the Foundry project.

## Production resources

- Subscription: `120ea104-5498-44f6-8e86-5654a1f4419b`
- Resource group: `neoh`
- Runtime region: North Central US
- Database region: Central US (private cross-region VNet peering)
- Foundry project: `https://neoh-resource.services.ai.azure.com/api/projects/neoh`
- Prompt agent: `neoh-kimi-k2-6`, version 2, model deployment `Kimi-K2.6`
- Registry: `neoh120ea104.azurecr.io`
- Key Vault: `neoh-kv-120ea104`
- Managed Redis: `neoh-redis-120ea104` (`Balanced_B0`, TLS 1.2, HA,
  public access disabled)
- Redis private endpoint: `neoh-redis-private-endpoint` on
  `redis-private-endpoints`, resolved through `privatelink.redis.azure.net`
- Shared Azure Files mount: `/mnt/neoh`
- Managed identity: `neoh-app-id`
- PostgreSQL: `neoh-db-120ea104.postgres.database.azure.com`, private access only
- Runtime VNet: `neoh-app-vnet`, peered to database VNet `neoh-prod-vnet`
- Public DNS zone: `neohrs.com`

The app connects as the non-owner `oracle_app_login` role so PostgreSQL FORCE
RLS remains active. Schema migrations run separately with an administrator
credential referenced from Key Vault.

## Direct MLS policy

NEOH combines public county/assessor records with licensed MLS data only through
direct RESO Web API endpoints authorized by the operator:

- Put `ORACLE_RESO_FEEDS_JSON` in Key Vault or use secret-backed token
  environment variables referenced by that JSON.
- Set `ORACLE_RESO_ALLOWED_HOSTS` to the exact reviewed MLS endpoint hosts.
- Never configure consumer portals or listing aggregators as MLS feeds.
- The browse, health, transaction, and lead-overlay queries quarantine legacy
  `mls_id='rentcast'` rows without deleting historical data.
- Each board has its own database cursor and failure boundary, so one failed MLS
  produces a partial run instead of blocking every direct feed.

Generated contracts, uploaded audio staging, and splat assets use subfolders
under the shared `/mnt/neoh` Azure Files mount so every backend replica sees the
same durable files. Database records and encrypted document revisions remain in
PostgreSQL.

## Domain delegation

The Azure DNS zone is authoritative after the registrar delegates `neohrs.com`
to all four nameservers below:

- `ns1-03.azure-dns.com.`
- `ns2-03.azure-dns.net.`
- `ns3-03.azure-dns.org.`
- `ns4-03.azure-dns.info.`

Do not remove the trailing provider assignment at the registrar until the
Container Apps records have been populated and verified. Managed certificates
can be issued only after public DNS resolves those records.

## Runtime configuration

The backend uses `ORACLE_AI_CHAT_PROVIDER=azure-foundry`, stateless Foundry
responses (`store=False`), and a local allowlist for reversible CRM tools.
Uploads are scanned by the ClamAV sidecar over loopback before processing.
External communication, deletion, legal execution, money movement, price
changes, and role changes are not exposed as agent tools.

Production operator authentication keeps demo logins disabled and requires both
of these settings on the `neoh-api` container:

- `ORACLE_ADMIN_ID` contains the operator login identifier.
- `ORACLE_ADMIN_PASSPHRASE` uses the Container Apps secret reference
  `admin-passphrase`, backed by the versionless Key Vault secret
  `oracle-admin-passphrase` through the `neoh-app-id` managed identity.

Never place the operator passphrase in source, an image layer, or a literal
Container Apps environment value.

ACS callbacks and distributed rate limits require Azure Managed Redis. The
versionless Key Vault secret `redis-url` contains the TLS `rediss://` connection
URI; the `neoh-api` Container App exposes it only through the `redis-url`
secret reference as `REDIS_URL`. Production also sets
`ORACLE_REQUIRE_REDIS=true`, so a revision fails startup instead of accepting
an ACS call it cannot manage across replicas. Redis traffic stays on the
Container Apps virtual network through the private endpoint and private DNS
zone; public network access is disabled.

Set `ORACLE_ENABLE_WEBHOOKS=true` only after both
`ORACLE_ACS_WEBHOOK_SECRET` and `ORACLE_CUSTOM_CALL_WEBHOOK_SECRET` are backed
by Key Vault references. The migration job must run the newly built API image
before that image receives production traffic; forward migration
`0046_azure_security_forward_fixes.sql` repairs databases that already recorded
the earlier 0036 and 0043 filenames.

### Qwen Omni realtime calls

Store `DASHSCOPE_API_KEY` in Key Vault and expose it to the API container only
through a managed-identity secret reference. Configure
`DASHSCOPE_WORKSPACE_ID`, `DASHSCOPE_REGION`, `QWEN_REALTIME_MODEL`,
`QWEN_REALTIME_VOICE`, and the full Azure Communication Services resource ID
as `ORACLE_ACS_RESOURCE_ID`.

Keep `ORACLE_QWEN_REALTIME_ENABLED=false` until the candidate revision is
healthy. The media WebSocket accepts only the signed bearer JWT supplied by
ACS and binds `x-ms-call-connection-id` to live Redis state; it never accepts a
DashScope key or reusable webhook secret from the client.

### Twilio migration recovery verification

On 2026-07-27, production migration metadata showed that
`0043_acs_provider.sql` was applied at `2026-07-19T16:32:54Z`. An isolated
Azure PostgreSQL point-in-time restore at `2026-07-19T16:32:53Z` confirmed
there were zero Twilio rows immediately before the migration; production also
contained zero Twilio rows afterward. No tenant Twilio credentials were lost,
so no secret reconstruction or production-row merge was required. The
corrected 0043 migration and forward migration 0046 retain Twilio in the
provider constraint and never delete its rows.

The active Foundry metadata and seed dataset are under
`backend/.foundry/`. The registered dataset URI is recorded in
`backend/.foundry/datasets/manifest.json`.
