# Neoh — DigitalOcean Production Deploy Runbook

Target platform as of 2026-09-21. Supersedes `infra/DEPLOY.md` (AWS) and
`infra/azure/README.md` (Azure, already retired before this migration
started) as the production deploy path. Neither AWS nor Azure support code
was removed — see "What stays" below — but DigitalOcean is now what this
document, `infra/digitalocean/app.yaml`, and `.github/workflows/ci.yml`'s
`deploy` job actually stand up.

## Architecture

**DigitalOcean App Platform**, not a Droplet + docker-compose. Chosen because
the repository's actual requirements already fit App Platform's component
model with almost no code change:

| Neoh component | DigitalOcean product | Why |
|---|---|---|
| Backend API | App Platform **Service**, `instance_count: 2+` | `ORACLE_PROCESS_ROLE=web` (new) makes it safe to scale horizontally — see below |
| Background jobs / scheduler | App Platform **Worker**, `instance_count: 1` | Same image, `ORACLE_PROCESS_ROLE=worker` — pinned to one instance, see below |
| Frontend SPA | App Platform **Static Site** | Built from `oracle-app/Dockerfile`, served from DO's CDN — cheaper than an always-on container for static files |
| PostgreSQL | **Managed PostgreSQL** | `db/connection.py`'s generic password+TLS path already works unmodified |
| Redis | **Managed Valkey** | Every Redis call site parses `REDIS_URL` via `redis.asyncio.from_url()`, which natively supports `rediss://` (TLS) — zero code change |
| Object storage | **Spaces** | `object_storage.py`'s `s3` backend now accepts `ORACLE_S3_ENDPOINT_URL` (added this migration) — Spaces speaks the S3 API |
| GPU reconstruction | **Unchanged: RunPod** | Already external via `RECON_POD_*`/`RUNPOD_*` env vars; DO has no comparable GPU product and none is needed |

This was **not** a coin-flip between App Platform and a Droplet. The
deciding facts, from auditing the actual code:

- WebSocket cross-replica fan-out (`ws_hub.py`) rides **Postgres
  LISTEN/NOTIFY**, not Redis pub/sub or sticky sessions — horizontal API
  scaling needs nothing extra.
- The durable job queue (`automation_jobs.py`) claims work with
  `FOR UPDATE SKIP LOCKED` and idempotency-keyed enqueue — already safe
  across replicas.
- **But** `voice_intel.py` and `reconstruction_worker.py` hold their job
  queues in an in-process `asyncio.Queue` with **no** cross-replica
  coordination — a job submitted to one API replica is invisible to
  another. Scaling the API naively (as App Platform's whole pitch is) would
  have silently dropped voice/reconstruction work.

That last fact is why `ORACLE_PROCESS_ROLE` exists now (`backend/config.py`,
`backend/server.py`): `web` starts the API only; `worker` (or the old
default, `all`) starts the API plus the scheduler, the durable job queue,
and the voice/reconstruction/video-studio in-process workers. The worker
component is pinned at `instance_count: 1` for exactly the reason those two
subsystems are unsafe to duplicate.

## What stays

Per the audit, nothing here is "AWS" or "Azure" anymore in the sense the
brief assumed — the live production target *before* this migration was
already **AWS** (`docs/production-blockers.md`, `infra/terraform/`), not
Azure; Azure Container Apps was retired earlier and is legacy documentation
only (`infra/azure/`, `backend/NEOH_AZURE_DEPLOYMENT.md`). Both AWS and
Azure code paths remain in the repository as **optional provider support**,
selected by env var, never required:

- `ORACLE_DB_AUTH=aws-iam|azure-entra` — DigitalOcean uses neither; it sets
  neither var and instead uses the pre-existing generic `ORACLE_DB_PASSWORD`
  path.
- `ORACLE_STORAGE_BACKEND=azure-blob|azure-files` remain valid values; DO
  sets `s3` (pointed at Spaces).
- `ORACLE_AI_CHAT_PROVIDER=bedrock|azure-foundry|fireworks|local` — DO
  deployment uses `fireworks` (no cloud-specific dependency); Bedrock/Foundry
  remain available if a tenant's key is configured.
- `infra/terraform/` (AWS) and `infra/azure/` (Azure) are untouched. They
  are no longer *the* deploy path but remain usable for a hybrid/multi-cloud
  future without being rebuilt from scratch.

## Database

1. Create a DigitalOcean Managed PostgreSQL cluster (version 16, matching
   `docker-compose.yml`'s `postgres:16-alpine`). Enable connection pooling
   (DO's built-in PgBouncer) if concurrent connections approach the plan's
   ceiling — `db/connection.py`'s own `asyncpg` pool (`ORACLE_DB_POOL_MIN`/
   `MAX`, default 2/10 per replica) already pools app-side; PgBouncer is an
   additional layer, not a replacement. Note the cluster's DO-assigned name
   (`doctl databases list`) — `infra/digitalocean/app.yaml`'s top-level
   `databases:` block attaches to it by `cluster_name` (with
   `production: true`), which is what makes the `${neoh-postgres.HOSTNAME}`-
   style bindable variables in the `api`/`worker` components resolve to real
   connection info; update `cluster_name` there to match before applying.
   Same pattern for Valkey below (`neoh-redis`).
2. Set `ORACLE_DB_HOST`, `ORACLE_DB_PORT`, `ORACLE_DB_NAME`,
   `ORACLE_DB_USER` (a non-owner role, `oracle_app_login`, so `FORCE ROW
   LEVEL SECURITY` actually applies), `ORACLE_DB_PASSWORD`,
   `ORACLE_DB_SSLMODE=require`. Leave `ORACLE_DB_AUTH` unset.
3. Leave `ORACLE_DB_CA_BUNDLE` unset — DO Managed Postgres presents a
   publicly-trusted certificate chain, and `db/connection.py`'s TLS context
   falls back to the system trust store when no bundle is given (the same
   fallback that already verifies Azure's cert). Set it explicitly only if
   you need `verify-full` against DO's own downloadable CA certificate.
4. Migrations: `backend/run_migrations.py`'s `_admin_credentials()` already
   has a plain-env-var path that needs no cloud SDK —
   `ORACLE_DB_ADMIN_USER`/`ORACLE_DB_ADMIN_PASSWORD` (the cluster's admin
   role and its password from the DO control panel). The `deploy` CI job
   runs this against the freshly-built image before updating App Platform.
5. **Fails closed**: `server.py`'s lifespan aborts boot in production if the
   pool cannot be established — no silent degraded mode.
6. **Backup/restore**: DO Managed PostgreSQL takes automatic daily backups
   with point-in-time recovery on paid plans (7-day retention on the
   cheapest tier at time of writing — confirm current retention in the DO
   control panel before relying on a specific window). Restore creates a
   new cluster from a backup; repoint `ORACLE_DB_HOST` at it and redeploy.
   This is a DO control-plane operation, not something this repo automates.

## Redis / Valkey

Used for: AI-chat rate limiting/de-dup (`rate_limiter.py`), general API rate
limiting with in-memory fallback (`rate_limit_middleware.py`), and an
optional L1 cache in front of the durable Postgres `di_cache` L2
(`data_integrations/cache.py`). **Not** used for WebSocket fan-out (that's
Postgres) or as a job queue (that's `automation_jobs.py`, Postgres-backed).

1. Create a DigitalOcean Managed Valkey cluster.
2. Set `REDIS_URL` to the `rediss://...` connection string DO provides — the
   `s` matters, it's what turns TLS on; every call site passes the URL
   straight to `redis.asyncio.from_url()` unmodified.
3. `ORACLE_REQUIRE_REDIS` (`server.py:106-108`) controls fail-open vs.
   fail-closed: unset (default) means Redis is optional and each consumer
   degrades honestly (rate limiting falls back to in-memory or DB-only,
   `data_integrations/cache.py` falls back to Postgres-only). Set it to `1`
   only once you've decided the degraded modes are unacceptable for your
   traffic pattern — it makes a Redis outage a boot-time failure.

## Object storage (Spaces)

1. Create a Spaces bucket (e.g. `neoh-media`, region `nyc3` or your chosen
   region) and a Spaces access key/secret (Spaces credentials are separate
   from your DO API token).
2. Set:
   ```
   ORACLE_STORAGE_BACKEND=s3
   ORACLE_S3_BUCKET=neoh-media
   ORACLE_S3_REGION=nyc3
   ORACLE_S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com
   ORACLE_S3_ACCESS_KEY_ID=<spaces key>
   ORACLE_S3_SECRET_ACCESS_KEY=<spaces secret>
   ```
   `object_storage.py`'s `_s3_client()` also forces virtual-hosted-style
   addressing (`Config(s3={"addressing_style": "virtual"})`) whenever a
   custom endpoint is configured — DigitalOcean's own documented requirement
   for Spaces — without changing boto3's default behaviour against real AWS
   S3, where no endpoint override is set.
3. **Leave the bucket private** (Spaces defaults to private; do not enable
   "File Listing" or a public CDN endpoint pointed at the whole bucket).
   Every write path audited (`media_storage.py`, `property_view_api.py`,
   `reconstruction_worker.py`, `video_studio.py`, `contract_vault.py`)
   already serves bytes either through an authenticated app route or a
   time-limited `signed_url()`/`presigned_put_url()` — this was true before
   Spaces support existed and is unchanged by it. `object_storage.py`'s S3
   client (`_s3_client()`) generates standard boto3 presigned URLs, which
   work against Spaces exactly as they do against real S3.
4. Contracts, client documents, and reconstruction artifacts get no special
   treatment beyond "goes through the same private backend" — there is no
   separate public-media pipeline for them (`contract_vault.py`'s own
   docstring says as much).

## Secrets

Set as App Platform `SECRET`-type env vars (encrypted at rest, never logged),
sourced as follows:

| Secret | Source |
|---|---|
| `ORACLE_SECRET_KEY`, `ORACLE_ENCRYPTION_MASTER_KEY` | Generate once (`openssl rand -hex 32`), store nowhere else. Rotating either invalidates existing sessions/encrypted data — see `config.py`'s weak-secret checks for the minimum entropy this must clear. |
| `ORACLE_ADMIN_ID`, `ORACLE_ADMIN_PASSPHRASE` | The operator/platform-admin login — choose, don't reuse a personal password. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio console. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe dashboard — the webhook secret is per-endpoint, generated when you register the DO backend's `/billing/webhook` URL in Stripe. |
| `ORACLE_BRIDGE_ACCESS_TOKEN` | Bridge Interactive / RESO MLS data provider. |
| `ORACLE_SMTP_HOST/USERNAME/PASSWORD` | Your transactional-mail provider (Gmail app password, SendGrid SMTP, etc. — see `backend/smtp_mailer.py`). |
| `ORACLE_FIREWORKS_API_KEY` (or Bedrock/Foundry keys, if used instead) | The chosen `ORACLE_AI_CHAT_PROVIDER`'s console. |
| `ORACLE_DB_PASSWORD`, `ORACLE_DB_ADMIN_PASSWORD` | DO Managed PostgreSQL control panel. |
| `REDIS_URL` | DO Managed Valkey control panel (full connection string). |
| `ORACLE_S3_ACCESS_KEY_ID/SECRET` | DO Spaces access keys (Account → API → Spaces Keys). |
| `VITE_GOOGLE_MAPS_KEY`, `VITE_GOOGLE_MAP_ID` | These are **browser-public** despite living in Actions secrets — the Maps key is referrer-restricted in the Google console, not actually secret. They ship in the JS bundle regardless of where they're stored in CI. |
| GitHub Actions: `DIGITALOCEAN_ACCESS_TOKEN`, `DIGITALOCEAN_REGISTRY`, `DIGITALOCEAN_APP_ID` | DO API token (Account → API), your Container Registry name, and the App Platform app's ID (`doctl apps list` after the first manual `doctl apps create`). |

**Never** commit a real value to `.env.example`/`.env.prod.example` (both
stay templates), and never expose a backend-only secret through a `VITE_*`
var — only the two Maps vars above are legitimately public; everything else
in the table stays server-side.

## Domain / TLS

App Platform provisions and renews TLS automatically for any domain you
attach to a component (Let's Encrypt under the hood — no cert management in
this repo). Point your registrar's CNAME/A record at the App Platform
component per DO's instructions, then:

- `ORACLE_PUBLIC_BASE_URL` (backend) — used to build Twilio/Stripe
  callback URLs, OAuth redirect URIs, and client-facing links
  (`commands_api.py`, `telephony_api.py`, `config.py`). Set to the API's
  final public HTTPS URL.
- `VITE_API_BASE` / `VITE_WS_URL` (frontend build args) — the app spec
  wires these to `${api.PUBLIC_URL}` automatically via App Platform's
  cross-component variable binding, so they stay correct across deploys
  without manual editing once the domain is attached.
- Twilio webhooks (`/api/commands/webhooks/twilio*`,
  `/api/telephony/webhooks/twilio/*`) and the Stripe webhook
  (`/billing/webhook`) are registered in each provider's own dashboard
  against the API's public URL — signature verification
  (`RequestValidator`/`stripe.Webhook.construct_event`) already gates both,
  unchanged by the platform move.
- `infra/digitalocean/app.yaml`'s top-level `ingress.rules` explicitly routes
  every non-`/api` prefix the backend actually serves (`/auth`, `/billing` —
  including the Stripe webhook, `/admin`, `/ws`, `/health`, `/version`,
  `/docs`) to the `api` component, with `/` as the final catch-all to `web`.
  A naive two-rule `/api` → api, `/` → web split (the obvious first attempt)
  would silently 404 all of those on the static site instead of reaching
  the backend — verified against the real route table in `server.py` before
  writing the spec, not assumed.

## Deployment flow

`push`/merge to `main` runs tests only — `backend`/`frontend` in
`.github/workflows/ci.yml` (existing, unchanged): pytest +
`pip_audit --strict`, eslint + typecheck + vitest + build + bundle budget.
**Nothing deploys automatically.** This repo stays code-only against
DigitalOcean until you deliberately trigger it — no DO secrets need to exist
in GitHub Actions before then, and no CI run touches DigitalOcean by
accident.

To actually deploy, on deployment day: GitHub → Actions → CI → "Run
workflow", branch `main`, type `deploy` into the `confirm` input (any other
value, or leaving it blank, runs tests only and stops). Or from the CLI:
`gh workflow run ci.yml --ref main -f confirm=deploy`. That runs the `deploy`
job, after both test jobs pass:
   - Build the backend image with `--build-arg GIT_SHA/APP_VERSION/
     BUILD_TIMESTAMP` (see `backend/Dockerfile`) and the frontend image with
     its `VITE_*` build args.
   - Push both to DO Container Registry, tagged with the immutable commit
     SHA (and `:latest` for convenience only — nothing deploys `:latest`).
   - Run migrations against the SHA-tagged image (`run_migrations.py`).
   - `doctl apps update` from `infra/digitalocean/app.yaml`, which pins
     `api` and `worker` to that same image.
   - Run `infra/digitalocean/smoke-test.sh` against the live URL, asserting
     `GET /version`'s `git_sha` matches what was just deployed.

There is intentionally no separate staging→production promotion step in
this first pass — see "Remaining manual setup" below for adding one behind
a second App Platform app + a manual-approval GitHub Environment.

## Release manifest

`GET /version` (new, `server.py`) answers with `git_sha`, `app_version`,
`built_at` (all baked into the image at build time — a running container
otherwise cannot see its own build history) and `migration_head` (read
**live** from the `schema_migrations` table, not baked, since migrations can
be applied independently of which image happens to be running). A release
is therefore always identifiable by four facts, never by `:latest`.

## Worker architecture recap

| Subsystem | Where it runs | Cross-replica safe? |
|---|---|---|
| API routes, WebSockets | `api` service (N replicas) | Yes — Postgres LISTEN/NOTIFY |
| Periodic scheduler (MLS/Bridge sync, mission ticks, outcome attribution, etc.) | `worker` (1 replica) | Yes on its own (idempotency-keyed), but centralized anyway — no reason to run it per web replica |
| Durable job queue (`automation_jobs.py`) | `worker` (1 replica) | Yes (`FOR UPDATE SKIP LOCKED`), centralized for the same reason |
| Voice workers | `worker` (1 replica) | **No** — in-process `asyncio.Queue` |
| Reconstruction workers | `worker` (1 replica) | **No** — in-process `asyncio.Queue` |
| Video-studio workers | `worker` (1 replica) | Yes (DB-claimed + advisory lock), centralized for consistency |
| GPU reconstruction compute | RunPod (external) | N/A — not a DO component |

If load ever requires more than one worker instance, voice_intel.py and
reconstruction_worker.py need the same DB-claim treatment video_studio.py
already has before `worker`'s `instance_count` can safely go above 1 — that
is future work, not done in this migration.

## Rollback

1. Identify the last known-good SHA — either from `GET /version` on a
   healthy deploy, or from the CI run history.
2. Re-run the `deploy` job's later steps against that SHA (`doctl apps
   update` accepts an explicit image digest — re-point `app.yaml`'s image
   references, or use `doctl apps create-deployment` with the prior
   deployment ID via `doctl apps list-deployments`).
3. If the failed release included a migration, a plain image rollback does
   **not** undo it — check `GET /version`'s `migration_head` before and
   after; a forward-only migration that broke something needs its own
   corrective migration, not a schema rollback (this codebase's migration
   runner has no down-migrations, matching `backend/db/migrations/README.md`).
4. Re-run `smoke-test.sh` after rollback to confirm the reverted SHA is
   actually live.

## Tests

New/updated for this migration, all passing:

- `backend/tests/test_object_storage.py` — Spaces endpoint/credential
  passthrough, AWS-S3-unchanged-by-default, `RECON_S3_BUCKET` back-compat.
- `backend/tests/test_process_role.py` — `ORACLE_PROCESS_ROLE` resolution
  (`all`/`web`/`worker`, fail-safe on an unrecognised value) and a
  structural proof that every background starter/stopper in `server.py`'s
  lifespan is actually gated by it.
- `backend/tests/test_version_endpoint.py` — baked release identity,
  honest `"unknown"` defaults, live (not baked) migration head.
- Full suite: 1933 backend tests passing after this migration (up from
  1930 before it — 3 net-positive test files, no regressions across the
  1900+ pre-existing tests that were not touched). 2017 including
  `compliance_engine/tests`, matching CI's exact `deploy` job precondition
  (`pytest tests compliance_engine/tests`, the same command the `backend`
  job runs).
- `infra/digitalocean/app.yaml` and `.github/workflows/ci.yml` validated as
  syntactically correct YAML; `app.yaml`'s bindable-variable and ingress
  syntax cross-checked against DigitalOcean's own current App Spec
  reference and Spaces boto3 example docs (not assumed from prior
  knowledge) — two real gaps were found and fixed this way: a missing
  top-level `databases:` block (without which `${neoh-postgres.HOSTNAME}`-
  style variables would never resolve) and the deprecated per-component
  `routes:` field (replaced with `ingress.rules`, explicitly listing every
  non-`/api` prefix the backend serves).

## Remaining manual DigitalOcean setup

Nothing in this repo can create your actual DigitalOcean resources — that
needs your DO account and API token:

0. **Do this one well before deployment day, not on it**: check the
   account's resource limit tier (Settings → Account → Limits, or
   `doctl account get`). A fresh/individual account defaults to a low tier
   (e.g. Tier 1 = 3 Droplets) — App Platform components, Managed Database
   clusters, and Spaces buckets have their own tiered ceilings too, and
   requesting an increase goes through DO support, which is not
   instant. Confirm the account can actually hold 1 App (3 components) + 2
   managed database clusters + 1+ Spaces bucket before day-of. Also set up
   spend alerts (Billing → Spend Alerts) now — they're percentage-threshold
   email notifications, not a hard spending cap, so they only help if
   someone's actually watching for the email.
1. Create the DO project, Container Registry, and the three managed
   resources (PostgreSQL, Valkey, Spaces bucket + keys) — confirmed
   available together in `nyc3` (App Platform, Managed PostgreSQL, Managed
   Valkey, and Spaces all list `nyc3` in DO's regional availability docs);
   pick a different region only if you have a specific reason to.
2. `doctl apps create --spec infra/digitalocean/app.yaml` once, by hand, to
   get the initial `app-id` — every deploy after that is `doctl apps
   update` (which the CI job does).
3. Set the GitHub Actions secrets listed in §Secrets.
4. Attach your domain to the `api` and `web` components in the DO control
   panel; DO issues TLS automatically once DNS resolves.
5. Register the live Twilio/Stripe webhook URLs in each provider's
   dashboard.
6. Decide on `ORACLE_MISSIONS_ENABLED` — left `0` in `app.yaml` on purpose;
   flip it once the Missions AI-agent feature has been reviewed for this
   tenant (see `SYPHER_VAULT/10_Active_Builds/Neoh_AI_Real_Estate_Agent.md`).
7. Optional: a staging App Platform app + a GitHub Environment required-
   reviewer rule on top of the manual `confirm=deploy` trigger, if
   "an authorized person clicks Run workflow" isn't a strong enough gate
   once real customers are on it.

## Estimated minimum infrastructure for the first 10 customers

Rough DO list-price floor, current at time of writing — verify against DO's
own pricing page before budgeting:

| Resource | Tier | Approx. monthly |
|---|---|---|
| `api` service | 2× `apps-s-2vcpu-4gb` | ~$50 |
| `worker` | 1× `apps-s-2vcpu-4gb` | ~$25 |
| `web` static site | Free tier (App Platform static sites are free up to a bandwidth ceiling) | $0 |
| Managed PostgreSQL | smallest production tier (1 vCPU / 1GB, with daily backups) | ~$15 |
| Managed Valkey | smallest tier | ~$15 |
| Spaces | 250GB + CDN | $5 |
| **Total** | | **~$110/month** |

Twilio, Stripe, Bridge/RESO, Fireworks, and RunPod GPU usage are all
usage-billed separately and not included — they scale with actual customer
activity, not with the base infrastructure. This is a floor for "the app is
up and correct," not a capacity plan; revisit `api`/`worker` sizing once
real traffic is observed.
