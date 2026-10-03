# Creating `neoh-staging` on DigitalOcean

Staging is not optional. The `promote` job deploys to production only an
artifact that already passed staging, so **until staging runs, nothing can
reach production.** It is also where the security DAST and the resilience
drills must run (`docs/launch-state.md`).

This page is the exact command sequence. Steps marked **[owner]** need the
account owner: they spend money, accept terms, or handle a credential no tool
should see. Everything else is mechanical. Run from the repository root with
`doctl` and `gh` authenticated.

Check where you are at any point (read-only, prints no secret):

```sh
python3 scripts/neoh-launch-readiness.py --env staging
```

## State on 2026-10-03, ~20:00 UTC

The owner is creating staging now, so re-run the command above for the current
state. Readiness output saved at that point: [`launch-readiness/staging.md`](launch-readiness/staging.md).

| Resource | State |
|---|---|
| Container registry `neoh-registry` (basic, nyc3) | exists. Bootstrap images being pushed |
| `neoh-postgres-staging` (pg 16.15, `db-s-1vcpu-2gb`, nyc3, 1 node) | **online**. Database `oracle` and DO-managed user `oracle_app_login` exist. **All 123 migrations applied over verified TLS**. **0 trusted sources** |
| `neoh-redis-staging` (valkey 8, `db-s-1vcpu-1gb`, nyc3) | **online**. **0 trusted sources** |
| Spaces bucket `neoh-media-staging` | created, private, with a bucket-scoped readwrite key (doctl cannot see buckets) |
| App `neoh-staging` | being created with the reduced sizes |
| GitHub environment `staging` | exists (branch policy set, no reviewers). **None of the 10 CI secrets set yet**. `STAGING_ENABLED` unset. `NEOH_DOMAIN` unset |

### Verified on real DigitalOcean (first bring-up, 2026-10-03)

Each of these broke the first real bring-up. None was visible to the offline
tests until then. Each is now fixed, and a test or CI step keeps it fixed.

| # | What broke | Fix | Kept fixed by |
|---|---|---|---|
| 1 | Backend refuses to boot: `ORACLE_JWT_ISSUER` / `ORACLE_JWT_AUDIENCE` were not in the spec (required outside dev). The worker would have crash-looped | api + worker carry the pair: production `neoh`, staging `neoh-staging` | `backend/tests/test_rendered_spec_boots.py` runs `validate_or_die` on every rendered component; the renderer refuses a component without the pair, and refuses staging sharing production's pair |
| 2 | App Platform rejected the whole spec: `unknown field "drain_seconds"` on the worker (service-only field) | removed; `grace_period_seconds: 300` kept (worker max 600) | renderer refuses service-only fields on workers; CI runs `doctl apps spec validate` before migrations |
| 3 | `CERTIFICATE_VERIFY_FAILED`: DO signs Postgres certificates with the cluster's own project CA, which is not publicly trusted | `ORACLE_DB_CA_CERT: ${neoh-postgres.CA_CERT}` on api + worker; CI migrations fetch the CA with `doctl databases get-ca`; `db/connection.py` and `run_migrations.py` load it (lead's fix on main) | spec + CI step |
| 4 | `run_migrations.py` connected to maintenance database `postgres`, which does not exist on DO (it is `defaultdb`) | probes the target database first, then falls back `postgres` → `defaultdb` (lead's fix on main) | — |
| 5 | App Platform refuses `instance_count: 2` for `apps-s-1vcpu-0.5gb` (production `web`) | production web 2× `apps-s-1vcpu-1gb`; staging 1× 0.5gb | renderer refuses scaling a single-instance size |

Also verified: on DO Managed Postgres, `doadmin` has **BYPASSRLS** (it is not a
superuser), and a `doctl databases user create` user (`oracle_app_login`) has
no superuser, BYPASSRLS, CREATEROLE or CREATEDB. So SECURITY DEFINER erasure
owned by the migration role sees every row. **`doadmin` must only ever run
migrations, never be the app login.**

## What staging is

`scripts/render-app-spec.py --env staging` renders it from the one
`infra/digitalocean/app.yaml`. It differs from production **only** in:

| | production | staging |
|---|---|---|
| app | `neoh` | `neoh-staging` |
| Postgres / Valkey | `neoh-postgres` / `neoh-redis` | `neoh-postgres-staging` / `neoh-redis-staging` |
| Spaces bucket | `neoh-media` | `neoh-media-staging` |
| `ORACLE_ENV` | `prod` | `staging` |
| `ORACLE_RECOVERY_MODE` | absent | `1` on api and worker |
| api | 2× `apps-s-2vcpu-4gb` | 2× `apps-s-1vcpu-1gb` |
| worker | 1× `apps-s-2vcpu-4gb` | 1× `apps-s-1vcpu-2gb` |
| web | 2× `apps-s-1vcpu-1gb` | 1× `apps-s-1vcpu-0.5gb` |
| DB pools (api / worker) | 10+3 / 15+6 | 6+3 / 8+4 (33 of 47 connections) |

The render **refuses** a staging spec that lacks recovery mode on any backend
component, sets `ORACLE_ALLOW_LIVE_STRIPE`, shares any app, cluster, bucket or
domain with production, runs the api below 2 instances, or opens more Postgres
connections than a 2 GiB cluster leaves headroom for.
`backend/tests/test_render_app_spec.py` pins the permitted differences.

### Side-effect safety, and what it costs you

`ORACLE_RECOVERY_MODE=1` blocks every provider egress method (calls, texts),
SMTP, both Stripe mutations and the scheduler. A second layer is independent:
with `ORACLE_ENV=staging`, a live `sk_live_` Stripe key refuses to boot
(`billing.py`). So staging **cannot** contact a real person or charge a real
card, whatever credentials it is given.

The trade is that staging also **cannot** prove outbound communications,
email delivery, alert emails or the scheduler (MLS sync, usage drain). Those
are proven on production with the owner's own contacts. See the T-1 step in
`docs/first-brokerage-launch.md`. Staging proves deploy, migrations, the API,
the worker on the new release, cross-replica fan-out, auth, RLS and the UI.

## Cost

Approximate DigitalOcean list prices, verified 2026-10-03 (check the pricing page before budgeting):

| Resource | Reduced staging (**chosen**) | Production-parity staging |
|---|---|---|
| api | 2× `apps-s-1vcpu-1gb` @ $12 = $24 | 2× `apps-s-2vcpu-4gb` @ $50 = $100 |
| worker | 1× `apps-s-1vcpu-2gb` = $25 | 1× `apps-s-2vcpu-4gb` = $50 |
| web | 1× `apps-s-1vcpu-0.5gb` = $5 | 2× `apps-s-1vcpu-1gb` @ $12 = $24 |
| Postgres | `db-s-1vcpu-2gb` = $30 | `db-s-2vcpu-4gb` = $60 |
| Valkey | `db-s-1vcpu-1gb` = $15 | $15 |
| Container registry | basic = $5 (one registry per account, **shared with production**) | $5 |
| Spaces | $5 | $5 |
| **Total** | **≈ $109/month** | **≈ $259/month** |

Reduced staging is enough to test correctness and release safety. It is **not**
a capacity test: capacity numbers come from `docs/capacity-plan.md` (measured
locally) and must be re-measured on production-sized resources before anyone
quotes them for DigitalOcean. Measured backend RSS is 255–561 MiB per replica,
which fits in 1 GiB.

## The sequence

```sh
REPO=ydngemini/oracle-cinematic
REGION=nyc3
```

### 1. Account limits and spend alerts **[owner]**

```sh
doctl account get          # tier/limits; App + 2 clusters + bucket must fit
```

In the console: Billing → Spend Alerts. This is an email notice, not a spending cap.

### 2. Registry **[owner — $5/month]** (done)

```sh
doctl registry create neoh-registry --subscription-tier basic --region $REGION
doctl registry login
```

### 3. Postgres **[owner — $30/month]** (done)

```sh
doctl databases create neoh-postgres-staging --engine pg --version 16 \
  --region $REGION --size db-s-1vcpu-2gb --num-nodes 1 --wait
PG_ID=$(doctl databases list --format ID,Name --no-header | awk '$2=="neoh-postgres-staging"{print $1}')
doctl databases db create   "$PG_ID" oracle            # the spec's db_name
doctl databases user create "$PG_ID" oracle_app_login  # the spec's db_user — DO generates its password
```

Create `oracle_app_login` **through DigitalOcean, not SQL**. The spec binds
`ORACLE_DB_PASSWORD` to `${neoh-postgres.PASSWORD}`, which DO can resolve only
for a user it manages. Migration 0001's `CREATE ROLE` is guarded by
`IF NOT EXISTS` and then grants `oracle_app` to it. Never set
`ORACLE_DB_APP_PASSWORD` for DigitalOcean: it would overwrite the password
DO hands the app.

### 4. Valkey **[owner — $15/month]** (done)

```sh
doctl databases create neoh-redis-staging --engine valkey --version 8 \
  --region $REGION --size db-s-1vcpu-1gb --num-nodes 1 --wait
RD_ID=$(doctl databases list --format ID,Name --no-header | awk '$2=="neoh-redis-staging"{print $1}')
```

### 5. Spaces bucket and a key scoped to it **[owner — $5/month]**

Create the bucket `neoh-media-staging` in `nyc3` in the console (Spaces → Create).
Keep it **private** and turn **File Listing** off. Then create a key that can
reach only that bucket:

```sh
doctl spaces keys create neoh-staging-app \
  --grants 'bucket=neoh-media-staging;permission=readwrite' -o json > /dev/shm/spaces-staging.json
chmod 600 /dev/shm/spaces-staging.json   # holds the secret; delete it after step 9
```

### 6. Bootstrap images (first time only)

CI builds every later release. The first app needs one image of each to exist:

```sh
REG=registry.digitalocean.com/neoh-registry
SHA=$(git rev-parse HEAD)
docker build -t $REG/neoh-backend:bootstrap \
  --build-arg GIT_SHA=$SHA --build-arg APP_VERSION=$SHA \
  --build-arg BUILD_TIMESTAMP=$(date -u +%FT%TZ) -f backend/Dockerfile backend   # context backend/, as compose
docker build -t $REG/neoh-frontend:bootstrap \
  --build-arg VITE_API_BASE= --build-arg VITE_WS_URL= -f oracle-app/Dockerfile oracle-app
docker push $REG/neoh-backend:bootstrap && docker push $REG/neoh-frontend:bootstrap
BD=$(docker inspect --format '{{index .RepoDigests 0}}' $REG/neoh-backend:bootstrap | cut -d@ -f2)
FD=$(docker inspect --format '{{index .RepoDigests 0}}' $REG/neoh-frontend:bootstrap | cut -d@ -f2)
```

### 7. Migrate the empty database (first time only)

CI does this on every release. For the bootstrap, run the same command
from the same image. The admin password goes through stdin into an env var and is never echoed:

```sh
PGHOST=$(doctl databases connection "$PG_ID" --format Host --no-header)
read -rs ADMINPW < <(doctl databases connection "$PG_ID" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["password"])')
PLATFORMPW=$(openssl rand -hex 24)        # keep for steps 8 and 9, then unset
ORACLE_DB_CA_CERT=$(doctl databases get-ca "$PG_ID" -o json | python3 -c 'import base64,json,sys;print(base64.b64decode(json.load(sys.stdin)["certificate"]).decode())')
docker run --rm -e ORACLE_DB_HOST=$PGHOST -e ORACLE_DB_PORT=25060 -e ORACLE_DB_NAME=oracle \
  -e ORACLE_DB_ADMIN_USER=doadmin -e ORACLE_DB_ADMIN_PASSWORD="$ADMINPW" \
  -e ORACLE_DB_PLATFORM_PASSWORD="$PLATFORMPW" -e ORACLE_DB_CA_CERT="$ORACLE_DB_CA_CERT" \
  -e ORACLE_DB_SSL=require \
  $REG/neoh-backend@$BD python run_migrations.py
```

The image must contain the lead's fixes from main: CA loading, and the
`postgres` → `defaultdb` maintenance fallback. Without them, see findings 3
and 4 above. On 2026-10-03 all 123 migrations applied this way to
`neoh-postgres-staging`.

### 8. Render the spec, fill the secrets locally, create the app **[owner — app ≈ $54/month]**

```sh
python3 scripts/render-app-spec.py --check
python3 scripts/render-app-spec.py --env staging --backend-digest "$BD" --frontend-digest "$FD" \
  > /dev/shm/app.staging.yaml
```

**Run `doctl apps spec validate <rendered spec>` before every `doctl apps
create` or `update`.** It is read-only and catches what the renderer cannot
know (findings 2 and 5). CI runs it after rendering, in both the release and
promote jobs.

Every `type: SECRET` entry in that file is blank. Fill the values into the
**local copy only** (in `/dev/shm`, never in the repo), then create the app.
`ORACLE_DB_PASSWORD` and `REDIS_URL` are bound by DigitalOcean and need no
value. The values to supply:

| Key | Staging value |
|---|---|
| `ORACLE_SECRET_KEY`, `ORACLE_ENCRYPTION_MASTER_KEY` | `openssl rand -hex 32` each. **Staging's own**, never production's |
| `ORACLE_ADMIN_ID`, `ORACLE_ADMIN_PASSPHRASE` | a staging operator login |
| `ORACLE_ADMIN_TOTP_SECRET` | `python3 scripts/generate-operator-totp.py` |
| `ORACLE_DB_PLATFORM_PASSWORD` | `$PLATFORMPW` from step 7 |
| `ORACLE_S3_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | from `/dev/shm/spaces-staging.json` |
| `STRIPE_SECRET_KEY` | a **test-mode** `sk_test_…` key (a live key refuses to boot on staging) |
| `STRIPE_WEBHOOK_SECRET` | from a **test-mode** webhook endpoint pointed at staging's `/billing/webhook` |
| `STRIPE_PRICE_ID` | the **test-mode** price of the plan |
| `TWILIO_ACCOUNT_SID` / `_AUTH_TOKEN` | Twilio **test credentials** (recovery mode blocks egress anyway) |
| `ORACLE_SMTP_*` | any SMTP account. Recovery mode blocks sending, so a sandbox is enough |
| `ORACLE_FIREWORKS_API_KEY` | a separate, spend-capped key, so staging AI calls are visible on their own |
| `ORACLE_BRIDGE_ACCESS_TOKEN` | a test or sandbox MLS token, or leave blank (MLS is a WARN) |
| `ORACLE_ALERT_EMAIL`, `ORACLE_ADMIN_OTP_EMAIL` | an ops address (staging cannot send; alerts land in logs and `ops_alerts`) |

```sh
python3 - /dev/shm/app.staging.yaml <<'PY'   # fills blanks from the environment; prints names only
import os, sys, yaml
p = sys.argv[1]; spec = yaml.safe_load(open(p))
for comp in spec.get("services", []) + spec.get("workers", []):
    for e in comp.get("envs", []):
        if e.get("type") == "SECRET" and not e.get("value") and os.environ.get(e["key"]):
            e["value"] = os.environ[e["key"]]
        elif e.get("type") == "SECRET" and not e.get("value"):
            print("still blank:", comp["name"], e["key"])
open(p, "w").write(yaml.safe_dump(spec, sort_keys=False))
PY
doctl apps spec validate /dev/shm/app.staging.yaml > /dev/null   # ALWAYS before create/update
doctl apps create --spec /dev/shm/app.staging.yaml --wait
shred -u /dev/shm/app.staging.yaml
APP_ID=$(doctl apps list --format ID,Spec.Name --no-header | awk '$2=="neoh-staging"{print $1}')
doctl apps get "$APP_ID" --format DefaultIngress --no-header     # the staging URL
```

### 9. Lock the databases to the app

```sh
doctl databases firewalls append "$PG_ID" --rule app:$APP_ID
doctl databases firewalls append "$RD_ID" --rule app:$APP_ID
```

> **Decision needed [owner]:** CI runs migrations from a GitHub-hosted runner
> (`ci.yml` "Run database migrations"). Those runners have no fixed IP, so a
> Postgres restricted to `app:$APP_ID` **refuses the CI migration step.**
> The options are: leave Postgres open to the internet, protected by password
> and TLS (the security gate lists trusted sources as an open manual control);
> move migrations into an App Platform `PRE_DEPLOY` job that runs inside the
> trusted boundary (a CI change); or use a self-hosted runner with a fixed IP.
> Until you decide, append only the Valkey rule.

### 10. GitHub environment `staging` and its secrets

The environment already exists. Set its 10 secrets by piping each value in, so none
appears in shell history or output:

```sh
s() { gh secret set "$1" --env staging --repo $REPO; }   # value on stdin
doctl registry get --format Name --no-header        | s DIGITALOCEAN_REGISTRY
printf %s "$APP_ID"                                  | s DIGITALOCEAN_APP_ID
printf %s "$PGHOST"                                  | s ORACLE_DB_HOST
printf 25060                                         | s ORACLE_DB_PORT
printf oracle                                        | s ORACLE_DB_NAME
printf doadmin                                       | s ORACLE_DB_ADMIN_USER
printf %s "$ADMINPW"                                 | s ORACLE_DB_ADMIN_PASSWORD
printf %s "$PLATFORMPW"                              | s ORACLE_DB_PLATFORM_PASSWORD
printf 'https://%s' "$(doctl apps get "$APP_ID" --format DefaultIngress --no-header | sed 's#https://##')" \
                                                     | s NEOH_PUBLIC_API_BASE
gh secret set DIGITALOCEAN_ACCESS_TOKEN --env staging --repo $REPO   # [owner] prompts; use a token scoped to this team
unset ADMINPW PLATFORMPW
rm -f /dev/shm/spaces-staging.json
```

Optional build-time keys go on the environment too: `VITE_GOOGLE_MAPS_KEY`
and `VITE_GOOGLE_MAP_ID`. They are browser-public and referrer-locked, and the
key must allow the staging origin.

### 11. Domain (optional for staging)

```sh
gh variable set NEOH_DOMAIN --env staging --repo $REPO --body staging.<your-domain>
```

Add the CNAME at your DNS provider, pointing at the app's default ingress. The next deploy
writes the domain into the spec, and DO issues TLS. Never attach a domain in the
DO console: the next `doctl apps update` detaches it, and `spec-drift.py`
blocks the release. Leave `NEOH_DOMAIN` unset to use the `ondigitalocean.app` URL.

### 12. Turn on automatic staging

```sh
gh variable set STAGING_ENABLED --repo $REPO --body true
```

From now on every push to `main` builds once, deploys to staging and
smoke-tests. To deploy without a push: Actions → CI → Run workflow →
`confirm: stage`.

### 13. Verify that a CI deploy preserves the encrypted secrets **[owner — empirical]**

The committed spec carries `type: SECRET` keys with **no value**, and CI
deploys it with `doctl apps update --spec`. App Platform must keep the
encrypted values already set on the app (step 8) rather than wipe them. Nobody has
observed this yet. Prove it on staging before production depends on it:

```sh
doctl apps spec get "$APP_ID" --format json \
  | python3 -c 'import json,sys;s=json.load(sys.stdin);print(sorted(e["key"] for c in s.get("services",[])+s.get("workers",[]) for e in c.get("envs",[]) if e.get("type")=="SECRET" and e.get("value")))' \
  > /tmp/secrets-before.txt
# trigger one CI staging deploy (Actions → CI → confirm: stage), wait for it, then:
doctl apps spec get "$APP_ID" --format json | python3 -c '…same…' > /tmp/secrets-after.txt
diff /tmp/secrets-before.txt /tmp/secrets-after.txt && echo "secrets preserved"
python3 scripts/neoh-launch-readiness.py --env staging   # core_secrets must still PASS
```

Both lists hold key **names** only. If any name disappears, App Platform wiped it.
In that case the CI deploy design needs a fix before production: the
deploy would have to merge the running spec's `EV[…]` values into the rendered
spec before `doctl apps update` (or `spec-drift.py` would have to refuse a
blank-over-encrypted change). Do **not** promote to production until this is
settled.

### 14. Confirm

```sh
APP_URL=$(gh secret list --env staging --repo $REPO >/dev/null; doctl apps get "$APP_ID" --format DefaultIngress --no-header)
APP_URL=$APP_URL ./infra/digitalocean/smoke-test.sh
python3 scripts/neoh-launch-readiness.py --env staging --json docs/launch-readiness/staging.json
```

Then run the open launch work that needs staging, in this order:

1. OWASP ZAP baseline against staging. This is the security gate's
   `staging_dast_no_blocker`, so update `docs/security-launch-gate.json`.
2. The resilience drills (`performance/resilience/run_drill.sh`). This is the
   owner gate `resilience_on_staging`.
3. The live privacy erasure test against the DO cluster. This is the owner gate
   `privacy_erasure_role_on_do`.

## Production

Production is the same sequence with `--env production`, the names `neoh`,
`neoh-postgres` (`db-s-2vcpu-4gb`), `neoh-redis` and `neoh-media`, and **live** Stripe
values. Use the GitHub environment `production`, which already exists with
required reviewers. There are two differences. You do not build images: `promote`
deploys the digests staging verified, and bootstrap uses the same digests as
staging. And `STAGING_ENABLED` does not apply: production is only ever deployed by
dispatching `confirm: deploy` with a `promote_sha`.
