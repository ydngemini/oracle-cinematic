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

**Build once. Promote the same artifact.** Two jobs in
`.github/workflows/ci.yml`, and only the first one ever builds an image.

Every push and PR runs the `backend` and `frontend` test jobs. Merging does
**not** deploy to production — ever.

### 1. `release` — build, then prove it on staging

Runs automatically on every push to `main` once the repository variable
`STAGING_ENABLED` is `true`, or by hand: Actions → CI → Run workflow on
`main`, `confirm: stage`. In the `staging` GitHub environment:

1. **Confirms the target is the staging app** — `doctl apps get` must report
   the name `neoh-staging`. Environment secrets share *names* across
   environments, so a production app id pasted into staging would otherwise
   deploy staging config over production.
2. **Builds both images, once.** The backend gets `--build-arg GIT_SHA/
   APP_VERSION/BUILD_TIMESTAMP`. The frontend is built **environment-
   agnostic**: `VITE_API_BASE` and `VITE_WS_URL` are deliberately empty, so the
   bundle calls its own origin — correct in both environments, because both
   serve web and api from one origin. Baking an API URL would make production's
   frontend call staging's API.
3. Pushes both; **resolves both digests from the registry**.
4. Writes the release manifest (`scripts/build-release-manifest.sh`).
5. **Renders the staging spec** from the one `app.yaml`
   (`scripts/render-app-spec.py`). `--check` proves staging and production
   share no app, cluster, bucket or domain, and that staging runs every backend
   component with `ORACLE_RECOVERY_MODE=1` — a staging Neoh cannot text a
   client, charge a card, or email anyone.
6. Migration precheck, then migrations **from the image by digest**.
7. `doctl apps update` with the rendered spec.
8. Smoke test — API, `/version`, **and a live worker on this release**.
9. **Only then** uploads `staging-verified-release-<sha>`. Its existence is
   the statement "this exact build ran on staging and passed."

### 2. `promote` — production, no build

Actions → CI → Run workflow on `main`, `confirm: deploy`,
`promote_sha: <full SHA of a staged commit>`. In the `production` environment,
after its required reviewer approves:

1. Validates the SHA and confirms it is on `main`.
2. **Downloads `staging-verified-release-<sha>`.** A SHA that never passed
   staging has none, and the job refuses: production is never the first place
   an artifact runs.
3. Reads the digests staging verified, and confirms both images still exist in
   the registry (garbage collection could have removed them).
4. Confirms the target is the production app (`neoh`).
5. **Captures the rollback target** — the currently-deployed spec, 90-day
   artifact.
6. Renders the production spec with **the same digests**.
7. Migration precheck, migrations from the image by digest.
8. `doctl apps update`, then the smoke test against *this* SHA.
9. Uploads `production-release-<sha>-<attempt>` — manifest plus rendered spec,
   kept 400 days. The GitHub run and its artifacts are the release record.

One production deploy runs at a time and is never cancelled; a push to `main`
mid-deploy cannot interrupt it.

> **This flow was corrected on 2026-09-25, and the previous description of
> it was false.** The app spec used to declare `dockerfile_path`, so App
> Platform built its *own* image at deploy time; the images CI pushed were
> used once for migrations and then orphaned. This document and a CI comment
> both claimed the spec "pins `api` and `worker` to that same image". Neither
> was true. See `docs/release-hardening-audit.md` for the full finding,
> including why the smoke test would have reported success anyway.

Two DigitalOcean constraints shape this, both verified against current docs:
`deploy_on_push` **cannot coexist with** `digest`, so CI drives deploys rather
than a registry push doing it. And `doctl apps update` does **not** re-pull when
the tag is unchanged; only a changed digest makes it deterministic.

## Release identity

Two things, and they answer different questions.

**`release-manifest.json`** (`scripts/build-release-manifest.sh`, uploaded by
CI) is what the release *is*: `release_id`, both image digests, the tags, the
migration head, an aggregate `migrations_sha256`, and the build run id. It is
what makes "promote the artifact that passed staging" checkable rather than
intended, and it is the input to `scripts/rollback.sh`.

**`GET /version`** is what is *actually running right now*: `git_sha`,
`app_version`, `built_at` — read from the image's own `ENV`, baked by
`--build-arg` in the build that produced it — plus `migration_head`, read
**live** from `schema_migrations` rather than baked, because migrations can be
applied independently of which image happens to be running.

> The app spec used to override `ORACLE_GIT_SHA` at runtime with
> `${_self.GIT_COMMIT_HASH}`. That made `/version` report the commit the
> platform *thought* it deployed rather than the build actually running —
> and `smoke-test.sh`, which compares exactly that, would then have agreed no
> matter which image was serving. The override is gone. An image can only
> report what it is.

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

Use `scripts/rollback.sh`. It is dry-run by default.

```sh
# what it would do, changing nothing
scripts/rollback.sh --to release-manifest-<good-sha>.json

# actually do it
DIGITALOCEAN_APP_ID=... scripts/rollback.sh --to release-manifest-<good-sha>.json --apply
```

It redeploys the previous **digest**. It never rebuilds old source — that
would produce a different artifact, which is a new release with old code in
it, not a rollback — and it never touches the database.

**It refuses when rolling back would break things.** The question is whether
the *previous* release can run against the schema the *current* one left
behind, which is not the same as "did the migration work":

| Migrations since the target | What happens |
|---|---|
| all additive | rolls the app back and **leaves the migrations in place**. No down-migration is attempted; this runner has none, by design. |
| any destructive | prints `APPLICATION ROLLBACK UNSAFE`, exits 3, and names the recovery options. Redeploying the old app onto an incompatible schema turns one broken release into a broken release *and* a broken database. |

`backend/migration_safety.py` makes that call — dropped or renamed columns,
narrowed types, new `NOT NULL` without a default, dropped policies and
functions all break a previous release. Anything it cannot classify counts as
unsafe, because the cost is asymmetric.

When it refuses, the options are a **corrective forward migration** (usually
fastest and safest), an **emergency compatibility patch** that restores what
was dropped so the old app can run, or a **point-in-time restore** — operator-
run, losing every write since, and see `docs/disaster-recovery-state-map.md`
first: DigitalOcean's PITR window is 7 days and a restore creates a *new*
cluster that has to be repointed. None of these is automated, deliberately.

Deliberately **not** used: DigitalOcean's own rollback endpoint. It *pins* the
app, blocking every subsequent deploy until someone commits or reverts the
rollback — a second incident waiting for the moment the fix-forward release is
ready and will not deploy. Applying a digest-pinned spec reaches the same
artifact and leaves the app deployable.

Afterwards, re-run `smoke-test.sh` with `EXPECTED_GIT_SHA` set to the target
release to confirm the reverted build is actually live.

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
2. **Create both apps once, by hand.** The committed `app.yaml` carries
   `__BACKEND_DIGEST__` / `__FRONTEND_DIGEST__` placeholders, so it cannot be
   applied directly — `doctl apps create --spec infra/digitalocean/app.yaml`
   will fail, deliberately. Bootstrap:
   ```sh
   doctl registry login
   docker build -t registry.digitalocean.com/<reg>/neoh-backend:bootstrap -f backend/Dockerfile .
   docker build -t registry.digitalocean.com/<reg>/neoh-frontend:bootstrap -f oracle-app/Dockerfile oracle-app
   docker push …/neoh-backend:bootstrap && docker push …/neoh-frontend:bootstrap
   # read each digest back:  docker inspect --format '{{index .RepoDigests 0}}' <image>
   for env in staging production; do
     scripts/render-app-spec.py --env $env --backend-digest sha256:… --frontend-digest sha256:… > app.$env.yaml
     doctl apps create --spec app.$env.yaml
   done
   ```
   Every deploy after that is `doctl apps update`, which CI does. Create the
   staging database, Valkey and Spaces bucket first — the staging spec names
   `neoh-postgres-staging`, `neoh-redis-staging` and `neoh-media-staging`.
3. Set the GitHub Actions secrets listed in §Secrets.
4. Attach your domain to the `api` and `web` components in the DO control
   panel; DO issues TLS automatically once DNS resolves.
5. Register the live Twilio/Stripe webhook URLs in each provider's
   dashboard.
6. Decide on `ORACLE_MISSIONS_ENABLED` — left `0` in `app.yaml` on purpose;
   flip it once the Missions AI-agent feature has been reviewed for this
   tenant (see `SYPHER_VAULT/10_Active_Builds/Neoh_AI_Real_Estate_Agent.md`).
7. **Staging is not optional.** Production promotes only what passed
   staging, so until staging exists nothing can reach production. Create the
   `staging` and `production` GitHub environments (the production one with a
   required reviewer), put each environment's secrets on it rather than on the
   repository, then set the repository variable `STAGING_ENABLED=true`. See
   `docs/release-checklist.md`.
8. **The Google Maps key must allow both origins.** It is referrer-locked and
   compiled into the one frontend bundle that both environments serve.

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
