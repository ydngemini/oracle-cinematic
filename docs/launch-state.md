# Neoh launch state

**Could Brokerage #1 use Neoh tomorrow? No.** Production does not exist yet.
Staging came up for the first time on 2026-10-03, and the open blockers are
ranked below.

This page is the one authoritative view of launch state. It summarises the
detailed documents and does not replace them. Anything machine-checkable is
also checked live by:

```sh
python3 scripts/neoh-launch-readiness.py --env production   # or --env staging
```

The latest saved runs are in [`launch-readiness/`](launch-readiness/README.md).
**State as of 2026-10-03 ~20:00 UTC.** Migration head is `0124_job_claim_aging.sql`.
Branch commits referenced below are on the Mission 3 launch-operations branch.

## State by area

| Area | State | Evidence / detail |
|---|---|---|
| **Production** | **Does not exist.** No app `neoh`, no `neoh-postgres` / `neoh-redis`, no production CI secrets, no `NEOH_DOMAIN`. The GitHub `production` environment exists with required reviewers | readiness run: 5 PASS / 9 WARN / 30 BLOCKED. [`deploy-digitalocean.md`](deploy-digitalocean.md) |
| **Staging** | **Up for the first time** (`neoh-staging`, app `3386001b…`, reduced size ≈ $109/mo). api 2× / worker 1× / web 1× ACTIVE; `/health` 200; worker alive; recovery mode on; 123 migrations applied over verified TLS. It runs a hand-built bootstrap image (`git_sha unknown`). Stripe, SMTP, AI and voice keys are not set yet. 9 of the 10 GitHub `staging` secrets are set (`DIGITALOCEAN_ACCESS_TOKEN` is not); `STAGING_ENABLED` unset | readiness run: 25 PASS / 17 WARN / 4 BLOCKED. [`staging-setup.md`](staging-setup.md) |
| **Release pipeline** | Built and tested offline. It has **never run end to end**: no CI secrets yet. Nine defects were found and fixed by the first real bring-up (below) | [`release-checklist.md`](release-checklist.md), `.github/workflows/ci.yml` |
| **Security gate** | **FAIL** (2026-10-01) on one condition: `staging_dast_no_blocker` (no staging existed, so OWASP ZAP was never run). The other 20 of 21 conditions PASS, including RLS, IDOR, JWT, CSRF/CORS, webhooks, SSRF and uploads. Four controls need a person: GitHub environment protection on both environments; DO Postgres/Valkey trusted sources (both **0 rules** on staging today); Spaces bucket private; `ORACLE_DB_PLATFORM_PASSWORD` plus an operator second factor set in production | [`security-launch-gate.json`](security-launch-gate.json), [`security-launch-review.md`](security-launch-review.md) |
| **Privacy** (Mission 10) | **FAIL for real brokerage data.** Consumer Gmail SMTP has no DPA. DashScope realtime voice defaults to Singapore. ElevenLabs trains on content unless opted out (not in the production spec). Counsel sign-off pending. Several PII columns are plaintext (`clients`, `leads.payload`, `sms_messages`, `user_interactions`). **Resolved 2026-10-03:** on DO, `doadmin` has BYPASSRLS (not superuser), so the DEFINER erasure functions see every row. `doadmin` must run migrations only, never the app | [`subprocessor-inventory.md`](subprocessor-inventory.md), [`privacy-data-map.md`](privacy-data-map.md), [`privacy-counsel-checklist.md`](privacy-counsel-checklist.md), owner gates |
| **Resilience** (Mission 2) | **FAIL for pilot.** Every drill passed, but against a local topology and a provider mock. None has run on DigitalOcean staging or against provider sandboxes. Backlog drain throughput is unexplained (29.7/s vs 49.3/s) | [`resilience-drill-report.md`](resilience-drill-report.md) |
| **Billing** | Single plan. Checkout needs `STRIPE_PRICE_ID`, which the spec never carried until today. The worker needs the Stripe key too (erasure stops renewal there). Live-mode key and live webhook endpoint are unverifiable remotely, so the owner confirms them in Stripe | `billing.py`, [`brokerage-go-live-checklist.md`](brokerage-go-live-checklist.md) |
| **Communications** | Twilio is in the spec. Telnyx (hosted SMS), Plivo and Google OAuth are optional and absent. The Twilio account's number purchase was blocked by KYC (Trust Hub) at last check | [`telnyx-hosted-sms-runbook.md`](telnyx-hosted-sms-runbook.md), [`provider-failure-matrix.md`](provider-failure-matrix.md) |
| **AI** | Fireworks is the production provider. Staging reuses the existing Fireworks key (owner decision, 2026-10-04); readiness `ai_provider` PASS. The spec now sets `ORACLE_AI_CHAT_PROVIDER=fireworks` — without it the chat path defaulted to the retired `azure-foundry` and Neoh never answered (`bdc90ab`) | [`runbooks/ai-provider-down.md`](runbooks/ai-provider-down.md) |
| **MLS** | No licensed production feed. Bridge/RESO credentials are absent from staging. MLS is a WARN, not a blocker, until a brokerage requires it | [`mls-production-runbook.md`](mls-production-runbook.md) |
| **Alerts and incidents** | `ORACLE_ALERT_EMAIL` is now in the spec (it was missing, so production alerts would have reached only logs). Staging cannot send alert email (recovery mode); alerts land in logs and `ops_alerts` | [`runbooks/README.md`](runbooks/README.md) |
| **Backups / DR** | Staging Postgres has a daily backup (first one taken). DO retention is 7 days, backups cannot be downloaded, and restore goes only to a NEW cluster | [`disaster-recovery-state-map.md`](disaster-recovery-state-map.md) |
| **Support** | Model defined. Support address and urgent route are **not decided** (placeholders) | [`support-model.md`](support-model.md) |
| **Capacity** | Measured locally (Mission 8). Never measured on DigitalOcean. Reduced staging is not a capacity test | [`capacity-plan.md`](capacity-plan.md) |

## Found by the first real staging bring-up (2026-10-03), now fixed

None of these were visible to the offline test suite. Each would have stopped
the first deploy. Each is now kept fixed by a test or a CI step.

| # | Defect | Effect | Fix | Kept fixed by |
|---|---|---|---|---|
| 1 | `ORACLE_JWT_ISSUER` / `ORACLE_JWT_AUDIENCE` not in the spec (required outside dev) | backend refuses to boot; worker crash-loop | `6f4de80`: production `neoh`, staging `neoh-staging` | `test_rendered_spec_boots.py` runs `validate_or_die` per rendered component; renderer refuses a missing or shared pair |
| 2 | Worker `termination.drain_seconds` (service-only field) | App Platform rejects the whole spec | `6f4de80` | renderer refuses service-only worker fields; CI `doctl apps spec validate` |
| 3 | DO Postgres certificate is signed by the cluster's own project CA | TLS `CERTIFICATE_VERIFY_FAILED` for app and migrations | `6f4de80` (spec `ORACLE_DB_CA_CERT`, CI `get-ca`) + main (`db/connection.py`, `run_migrations.py`) | spec + CI step |
| 4 | Migrations connected to maintenance DB `postgres` (DO has `defaultdb`) | migrations fail | main (`run_migrations.py` fallback) | — |
| 5 | Production `web` 2× `apps-s-1vcpu-0.5gb` (that size allows 1 instance) | App Platform rejects the production spec | `6f4de80`: 2× `apps-s-1vcpu-1gb` | renderer refuses scaling a single-instance size |
| 6 | CI built the backend with the repo root as context | first release fails at build (no root `requirements.txt`); `COPY . .` would copy the monorepo | `a571fee`: context `backend/` | `test_ci_build_contexts.py` |
| 7 | Ingress rules stripped the path prefix (no `preserve_path_prefix`) | every API route 404 while the SPA worked | `59bcf7f` | test: every api rule preserves its prefix |
| 8 | **`doctl apps update` with a blank SECRET wipes it** (proven twice on staging; DO auto-rolled back). CI and `rollback.sh` applied blank-secret specs | every deploy after the first, and every rollback, would fail and silently roll back | `6ab519b`: `scripts/carry-secrets.py` carries the ACTIVE deployment's `EV[…]` values, refuses before migrations if a required secret is missing everywhere. **Resubmission PROVEN 2026-10-04** by the first CI release (run 37236009143): `apps update` accepts carried `EV[…]` values; `apps spec validate` rejects them, so CI validates the rendered spec (`c0cdc46`) | `test_carry_secrets.py`, `test_ci_deploy_wiring.py`, rollback test |
| 9 | CI steps after the first read `DIGITALOCEAN_APP_ID`, `NEOH_PUBLIC_API_BASE` and the migration DB variables **empty** | first `doctl apps get ""` fails; migrations get no host | `6ab519b` | `test_ci_deploy_wiring.py` |

Found while building the readiness check, before any deploy:

| Defect | Effect | Fix |
|---|---|---|
| `STRIPE_PRICE_ID` absent from the spec | checkout refuses to run | `e77118b` |
| Worker lacked `STRIPE_SECRET_KEY` | erasure reported `stripe_not_configured`, which it does not count as a failure, so a **closed brokerage would have kept being billed** | `e77118b`, plus `STRIPE_WEBHOOK_SECRET` (`6f4de80`; `validate_or_die` requires it once the key is set) |
| `ORACLE_ALERT_EMAIL` absent from the spec | production alerts only in logs | `e77118b` |
| Smoke test failed on the SPA's 200 for `/openapi.json`, and on DO's 503→504 rewrite of unconfigured webhooks | every release would fail, or report a broken webhook falsely | `0daa205` |

Found after the merge (2026-10-04), now fixed:

| Defect | Effect | Fix |
|---|---|---|
| `ORACLE_AI_CHAT_PROVIDER` absent from the spec | Neoh never answered in staging or production, even with a valid Fireworks key | `bdc90ab` |
| Deleting a photo deleted only its database row | the file (and a 3D space's companions) stayed in the bucket forever — a privacy defect | `417899a`: files removed first; failure → 503, nothing deleted |
| Neoh's 3D-build and listing-video tools staged approvals no code ever executed | the agent was told "approving it starts the job"; nothing ever happened | `c341e9c`: hand-off to the real confirm-with-cost paths |
| `/api/admin/users` listed only demo identities and profiles | real users without a profile were invisible to operators | `42d93e0` (also: every admin read bounded to 5 s) |
| gitleaks flagged three fake test fixtures; bandit flagged constant-built SQL | CI security job red | `.gitleaksignore` fingerprints; literal SQL |

First CI release to staging (2026-10-04, run 37236009143) — staging now runs a CI-built, digest-pinned release (`252a58d`). Getting there found:

| Defect | Fix |
|---|---|
| Release-manifest step read `DIGITALOCEAN_REGISTRY` it was never given → `registry.digitalocean.com//neoh-backend` | `76335d5` (job-level identifier; wiring test extended) |
| `doctl apps spec validate` refuses carried `EV[…]` secret values | `c0cdc46`: validate the rendered spec; `apps update` (which accepts them) gets the carried one |
| The staging database carried no environment stamp; the migration precheck refused (as designed) | stamped `neoh-environment=staging`; step added to `staging-setup.md` |

## Found on staging 2026-10-06 (real calls, real database, DAST), now fixed

| Defect | Effect | Fix |
|---|---|---|
| Plivo's call-create response carries no request id on this account (only `api_id` + `message`) | Every approved call was refused at answer as "unmanaged". The phone rang and the caller heard "cannot be connected safely" | `6cab880`: Neoh's own reference is stored before the call and carried in the callback URLs. The signature is verified over the URL including its query |
| `ON CONFLICT (command_id)` vs a **partial** unique index on `live_call_sessions` | Every approved call failed to record after it rang, and was left as "needs review". 0 call sessions on staging, ever | `21ae08e`, plus `test_on_conflict_targets_live.py`: all 86 ON CONFLICT targets checked against the real catalog in CI |
| `finish_automation_job_attempt` duplicated half the RLS predicate | No job attempt ever finalized, and per-attempt errors were lost | migration `0128`. Live test uses a brokerage tenant (verified: 16/16 attempts now finalize) |
| A text refused for missing setup was retried 5 times | Could send a "not sent" text later, unannounced | Configuration errors are terminal on attempt 1 (`NOT_CONFIGURED`), verified on staging |
| CSP `media-src https:` and the retired `neohrs.com` in `connect-src` | Media from any origin. A lapsed domain stays trusted | `media-src 'self' blob:`, plus `test_csp_policy.py` |
| **The 3D Space never loaded on staging.** The viewer fetches a `blob:` URL that `connect-src` forbade, in **both** the nginx header and the `<meta>` copy from `vite.config.js` (the browser enforces both). The demo check missed it: its regex didn't match "could not be loaded" | Every rehearsal since 2026-10-04 "passed" over an error box | `f831a91` + `3a4cb84`: `blob:` in both policies, which are now held equal by test. The demo test reads the viewer's own `data-space-status`. Rehearsal 6: Space ready, **0 console errors** (was 18) |
| `AnimatePresence mode="popLayout"` injected a `<style>` that `style-src 'self'` blocks | 8 console errors per run. The exiting pill was never pinned, so the layout jumped | The exit sets `position: absolute` inline (allowed by `style-src-attr`) |

**Proof status:**
- **Calls:** a real call was matched and spoke the AI disclosure. The acknowledgement fix is proven by replaying the write on staging in a rolled-back transaction, **not yet by a real call**: the 3-call budget is used.
- **DAST:** OWASP ZAP baseline gave 0 FAIL / 10 WARN / 57 PASS (`docs/security/zap-baseline-staging-2026-10-06.md`). The authenticated active scan is still open, so the gate stays FAIL.

## Launch blockers, ranked

Each one unblocks the next. Rows 1–4 need the owner.

1. **Finish staging.** Set the last GitHub `staging` secret (`DIGITALOCEAN_ACCESS_TOKEN`). Re-apply the app's secret values: the app spec is currently the blank-secret one, and the serving deployment is DO's automatic rollback. Add Fireworks, Stripe **test**, SMTP and Twilio test values. **Prove that App Platform accepts resubmitted `EV[…]` values** ([`staging-setup.md`](staging-setup.md) step 13). If it does not, switch CI to plaintext injection from GitHub environment secrets. Only then set `STAGING_ENABLED` and run a CI-built release (`confirm: stage`).
2. **Decide trusted sources vs CI migrations.** GitHub-hosted runners have no fixed IP, so locking Postgres to the app breaks CI migrations. Options are in [`staging-setup.md`](staging-setup.md) §9.
3. **Run the staging-only launch work:** OWASP ZAP (closes the security gate), the resilience drills on DO staging and provider sandboxes, and the live erasure test on the DO cluster.
4. **Privacy:** an email provider with a DPA, the DashScope region decision, and counsel sign-off.
5. **Create production** with the same sequence: `neoh-postgres` `db-s-2vcpu-4gb`, `neoh-redis`, `neoh-media`, live Stripe, `NEOH_DOMAIN`, and production GitHub secrets. Promote a staged SHA.
6. **Support contact and urgent route** decided ([`support-model.md`](support-model.md)).
7. Run the readiness check and the UI audit against production. Then work through [`first-brokerage-launch.md`](first-brokerage-launch.md) from T-7.

These are not blockers: they are WARNs in the readiness report. MLS licensing (until a brokerage needs MLS), Telnyx/Plivo/Google, 3D reconstruction, spend alerts, and a staging domain.

## Open decisions for the owner

- **Trusted sources vs CI migrations** (above).
- **Production web size:** 2× `apps-s-1vcpu-1gb` ($24, survives an instance failure) vs 1× `apps-s-1vcpu-0.5gb` ($5).
- **`REQUIRE_CALLBACK_PROVIDERS`** (repository variable). Set it to `stripe`, plus `telnyx` if used, once production bills. The production smoke test then fails if those webhooks are not configured.
- **Support address and urgent route.**
- **Delete the local, untracked AWS Terraform state files** ([`infrastructure-status.md`](infrastructure-status.md#local-terraform-state-outside-git)). They hold placeholders only, so no rotation is needed.
