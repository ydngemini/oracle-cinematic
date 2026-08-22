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

## Cloud-service bindings

Every AWS integration is now opt-in behind a variable that defaults to its Azure
counterpart, so an unset environment cannot silently reach for AWS. The AWS SDK
imports are all lazy — an Azure-only deployment never loads `boto3`.

| Concern | Variable | Azure default | Legacy AWS value |
|---|---|---|---|
| Database auth | `ORACLE_DB_AUTH` | `azure-entra` (Entra token, `AZURE_CLIENT_ID` selects the identity) | `aws-iam` |
| DB trust anchor | `ORACLE_DB_CA_BUNDLE` | unset — system trust store verifies Flexible Server | RDS global bundle |
| DB TLS floor | `ORACLE_DB_TLS_MIN` | `1.2` (Flexible Server may negotiate either) | `1.3` |
| Migration secret | `ORACLE_KEY_VAULT_URI` + `ORACLE_DB_ADMIN_SECRET` | Key Vault via managed identity | `DB_MASTER_SECRET_ARN` |
| Transactional email | `ORACLE_EMAIL_PROVIDER` | `smtp` — an operator-named server, see `docs/email-dns-setup.md` (`acs` still available) | `ses` |
| Object storage | `ORACLE_STORAGE_BACKEND` | `azure-files` on `/mnt/neoh` | `s3` |
| Splat output | `ORACLE_SPLAT_STORAGE` | `fs`, with `ORACLE_SPLAT_DIR=/mnt/neoh/splats` | `s3` |
| Assistant inference | `ORACLE_AI_CHAT_PROVIDER` | `azure-foundry` | `bedrock` |
| Bedrock fallback rung | `ORACLE_AI_BEDROCK_FALLBACK` | `0` (ladder is Foundry → local) | `1` |

Two storage notes:

- `azure-files` cannot mint expiring links — a mounted share has no public URL.
  Those bytes are served through the app's authenticated media endpoint. The
  contract vault *does* hand out expiring links, so a deployment that uses it
  must set `ORACLE_STORAGE_BACKEND=azure-blob`; it fails loudly otherwise rather
  than returning an unusable link.
- Blob links are user-delegation SAS signed by `neoh-app-id`, so they inherit
  the identity's RBAC and can be revoked centrally. An account key is used only
  when `AZURE_STORAGE_CONNECTION_STRING` carries one.

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

**`neohrs.com` is NOT delegated to Azure DNS, and per the 2026-08-07 decision it
will not be.** DNS lives in the registrar's panel (share-dns:
`A8.SHARE-DNS.COM` / `B8.SHARE-DNS.NET`). The Azure DNS zone in this resource
group is unused — do not add records to it expecting them to resolve.

Email authentication records (SPF, DKIM, DMARC, MX) and the SMTP sender setup
are documented in **`docs/email-dns-setup.md`**, which is the authority for
anything DNS-related.

Container Apps managed certificates require public DNS to resolve the app
records first; those go in the registrar panel too.

Historical note: this section previously instructed delegating to
`ns1-03.azure-dns.com` and its three siblings. That was never carried out.

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

## Inference provider (Fireworks AI)

Fireworks is the hosted inference tier. Azure Foundry and Bedrock are both
unreachable — Foundry has no endpoint configured, and the Bedrock credentials
return `UnrecognizedClientException` — so without this the only model answering
is the local llama.cpp server, which no Container Apps revision runs. Every AI
feature then fails closed with "The assistant is temporarily unavailable."

The `neoh-api` container needs three settings:

- `ORACLE_AI_CHAT_PROVIDER=fireworks` — a plain value, not a secret.
- `ORACLE_FIREWORKS_API_KEY` uses the Container Apps secret reference
  `fireworks-api-key`, backed by the versionless Key Vault secret
  `fireworks-api-key` through the `neoh-app-id` managed identity.
- `ORACLE_FIREWORKS_MODEL` names the model, e.g.
  `accounts/fireworks/models/kimi-k2p7-code`.

Run `infra/scripts/set-fireworks-secret.sh` to create the Key Vault secret and
bind it. It cannot run while the subscription is suspended: Key Vault still
answers metadata calls, so `az keyvault secret list` returns every name, but the
data plane is Forbidden and reading or writing a secret *value* fails with
`(Forbidden) The subscription associated with this vault has been disabled`.
A working `secret list` is not evidence the vault is usable.

Never place the Fireworks key in source, an image layer, `docker-compose.yml`,
or a literal Container Apps environment value — local dev reads it from the
gitignored `.env` instead.

Two settings are load-bearing and should not be lowered:
`ORACLE_FIREWORKS_MAX_TOKENS` (chat, default 4000) and
`ORACLE_FIREWORKS_MIN_TOKENS` (`ml_forge/bedrock_client.py`, default 2048).
The default model is a reasoning model that spends its budget on
`reasoning_content` before emitting any `content`; below those floors it returns
an empty string with `finish_reason: "length"`, which reads as a broken
integration rather than a truncated one.

Set `ORACLE_ENABLE_WEBHOOKS=true` only after both
`ORACLE_ACS_WEBHOOK_SECRET` and `ORACLE_CUSTOM_CALL_WEBHOOK_SECRET` are backed
by Key Vault references. The migration job must run the newly built API image
before that image receives production traffic; forward migration
`0046_azure_security_forward_fixes.sql` repairs databases that already recorded
the earlier 0036 and 0043 filenames.

### Video Marketing Studio (Sora 2)

The video studio generates property reels through the Azure OpenAI Sora 2
deployment (`sora-2-estate` on the `neoh-resource` account) via the standard
`openai.azure.com` endpoint. Keep `ORACLE_FEATURE_VIDEO_STUDIO=false` until
the Azure OpenAI account is healthy and the operator has sized the quota.

Enablement checklist:

1. `ORACLE_FEATURE_VIDEO_STUDIO=true` and `ORACLE_AZURE_OPENAI_ENDPOINT`
   (e.g. `https://neoh-resource.openai.azure.com`) on the `neoh-api` container.
2. `ORACLE_SORA_DEPLOYMENT=sora-2-estate` (deployment exists on the account).
3. Auth: the `neoh-app-id` managed identity must hold the Cognitive Services
   OpenAI User role on the `neoh-resource` account. No API key is required in
   prod — `DefaultAzureCredential` mints the bearer token. For non-prod
   testing, an API key may be set via a Key Vault secret reference bound as
   `ORACLE_AZURE_OPENAI_API_KEY`.
4. Size the quota (`ORACLE_VIDEO_DAILY_QUOTA`, default 120s/tenant/day) —
   video generation bills per second. The worker enforces a 2-pending-job cap
   per resource and the job row stores consumed seconds.
5. Migration `0063_video_studio.sql` must be applied by the migration job
   (adds `video_studio_jobs`, widens `chk_media_kind` to `video`).
6. `av` (PyAV) is a new backend requirement — rebuild the API image so the
   stitching dependency is present.

Jobs are durable rows claimed by the in-app worker (`FOR UPDATE SKIP LOCKED`),
so a container restart recovers queued jobs instead of stranding them. Finished
MP4s are stored as tenant-scoped `property_media` rows (kind `video`) with
bytes in `media_blobs`, delivered through the authenticated `/api/media/{id}`.

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

For Twilio, set `ORACLE_TWILIO_ACCOUNT_TIER=trial` while the project is on the
Voice Trial. Trial outbound calls accept only Twilio's predefined templates
and strip `<Stream>`, so CRM AI calls fail before dialing with an actionable
error. After upgrading, set the tier to `full` and set
`ORACLE_TWILIO_QWEN_REALTIME_ENABLED=true`.
The TwiML webhook then plays the disclosure before `<Connect><Stream>`. The
media WebSocket validates Twilio's signed handshake and binds the start frame
to live Redis state; signed status callbacks remove state when the call ends.

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
