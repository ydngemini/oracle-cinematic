# 19 — Production infrastructure, deploys, observability and recovery

> **Scope:** Turn the working staging pipeline into a production environment that an owner can approve: separate secrets, safe migrations and rollback, alerts a person answers, tested backups, and capacity measured on the real platform. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 17, 18 · **Unblocks:** 20 · **Ledger:** [L19](LEDGER.md#l19) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Platform: `infra/digitalocean/app.yaml`, `infra/digitalocean/`, `.github/workflows/ci.yml`, `scripts/carry-secrets.py`, `scripts/db-access-window.sh`
- Runtime: `backend/server.py`, `backend/db/connection.py`, `backend/automation_jobs.py`, `backend/run_migrations.py`
- Health and alerts: `backend/component_health.py`, `backend/ops_alerts.py`, `docs/runbooks/README.md`, `docs/runbooks/`
- Property providers used today (see 11): `backend/avm_client.py`, `backend/apis/market_data.py`, `backend/harvest_health.py`, `backend/harvesters/`
- Readiness: `scripts/neoh-launch-readiness.py`, `docs/launch-readiness/production.json`, `docs/launch-readiness/staging.json`, `docs/launch-state.md`
- Capacity and support: `docs/capacity-plan.md`, `docs/support-model.md`
- Audit finding [A04](../neoh-full-platform-source-audit-2026-10-08.md); ledger rows L19-01, L19-02, L17-02

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **A staging app exists and every push to main deploys it.** `ci.yml:250` job `release` runs on a push to main when `vars.STAGING_ENABLED == 'true'` (`:256`). Ledger L19-01: run 38127122126 deployed `745f2fa` on 2026-10-11 to https://neoh-staging-ksfpn.ondigitalocean.app.
- **No production app exists.** Ledger L19-02: `doctl apps list` on 2026-10-07 showed only staging (A04).
- **A production promote job exists but has never had a target.** `ci.yml:606` job `promote` runs only on a manual dispatch with `confirm == 'deploy'` on main (`:609-611`) and the `production` environment; it deploys digests staging already verified (`:596-599`).
- **Migrations run as a PRE_DEPLOY job.** `app.yaml:559-560` job `migrate`, `kind: PRE_DEPLOY`, runs `run_migrations.py --precheck-then-migrate`; a failed job cancels the deployment (`:553-558`). Ledger L17-02 (`b6d1160`).
- **Staging databases accept only the app.** Operators open a window with `scripts/db-access-window.sh` (`open|close|status`, `:12-14`). Ledger L17-02.
- **CI checks that staging and production share no resources.** `ci.yml:480-483` (comment) and `:487` run `scripts/render-app-spec.py --check`: no shared app, cluster, bucket or domain, and staging runs with `ORACLE_RECOVERY_MODE=1`.
- **The app spec sizes two API and two web instances and one worker.** `app.yaml:90-91` (api, 2 × `apps-s-2vcpu-4gb`), `:352-353` (web, 2 × `apps-s-1vcpu-1gb`), `:368` (worker, 1 instance).
- **The last production readiness report is old and BLOCKED.** `docs/launch-readiness/production.json:8` `generated_at` 2026-10-03, `:10` `"verdict": "BLOCKED"` (5 PASS, 9 WARN, 30 BLOCKED), `:6` `app_id: null`.
- **The readiness script is read-only.** `scripts/neoh-launch-readiness.py:11-13` (docstring) says it never creates, deploys or migrates; `--env` is `production` or `staging` (`:870`).
- **Alerts go to one email, or only to logs.** `app.yaml:222-227` `ORACLE_ALERT_EMAIL` ("Unset, alerts reach only the logs"); evaluator `ops_alerts.py:42` `evaluate_once`.
- **Health covers a legacy MLS component but no property provider.** `component_health.py:216` `_snapshot` lists database, realtime_fanout, valkey, workers, scheduler, job_queue, `mls` (legacy, D-2026-10-12-01), ai, outbound_side_effects and email_outbox (`:225`); nothing for RentCast, ATTOM, harvesters or market data.
- **RentCast and ATTOM already pause after a rejected key.** `avm_client.py:98` `_mark_credential_rejected` starts a cooldown that `_credential_down` (`:94`) checks.
- **Capacity numbers are local and mocked.** `capacity-plan.md:12-16` (one 4-core host, "Not DigitalOcean"), `:20` (all providers mocked), `:88` (50 chats, 16 slots, first-token p95 4.7 s), `:48` (8 job workers, peak wait 6 s), `:129` ("Everything on DigitalOcean" still unknown).
- **The support and on-call contacts are placeholders.** `docs/support-model.md:11-14`.

## 3. Goal

The owner can approve a dated production go/no-go record. Production runs the same verified digests as staging, with its own secrets, a tested restore and rollback, alerts that reach a named person, and capacity measured on DigitalOcean with real models. Staging success is never reported as production readiness (C6, C12).

## 4. Engineering tasks

- **T19.01 Inventory the platform without printing secrets.** Pin the SHA, then list DigitalOcean apps, API, web and worker replicas, the PostgreSQL cluster, Valkey, object storage, DNS, domain and TLS, registry images, secrets by name, backup status, trusted-source rules and each production provider credential's presence.
- **T19.02 Re-run readiness fresh.** Run `scripts/neoh-launch-readiness.py` for staging and for production, record the full dated result, and never treat the 2026-10-03 JSON as today's state. Make no production change without the operator's explicit approval and an approved change plan.
- **T19.03 Draft the production creation plan for the owner.** Size, cost, regions, trusted sources and the steps to create the production app and databases (L19-02 blocker). Present it as an owner question; create nothing.
- **T19.04 Verify which CI gates exist and add the missing ones.** Backend lint, types and tests; frontend lint, types, tests and build; migrations on a fresh and an upgraded database; real-PostgreSQL RLS; dependency and secret scanning; authenticated DAST; smoke tests; schema compatibility; a browser route crawl; and performance budgets.
- **T19.05 Separate staging and production configuration.** Separate secrets, webhook URLs and Stripe test and live modes. Missing secrets fail safe, a rollback never points live calls at staging, images build reproducibly by digest, and the deployment reports its running SHA.
- **T19.06 Run database changes safely.** Expand first, backfill in bounded batches with checkpoints, use dual reads only when needed, and contract explicitly after rollout. Prove schema upgrade and cold restore, and match pool sizes, replica counts and worker slots to the database connection limit.
- **T19.07 Define SLOs and alerts.** Cover login and auth, client list and search, AI first token, voice first audio and call drops, outbox depth and age, webhook loss, property data freshness and provider errors (RentCast, ATTOM, harvesters, market data; see 11), billing anomalies, privacy request handling, provider spend, database CPU, locks and replication, WebSocket and frontend crashes.
  - Add property-provider components to the health snapshot.
- **T19.08 Trace requests end to end.** Correlate request, workflow, tenant and user IDs, with PII safeguards, across browser, API, PostgreSQL, the model gateway, workers, provider callbacks and the operator dashboard. Name an alert owner and an on-call route; replace the placeholder contacts (owner decision).
- **T19.09 Run resilience drills on the target platform.** Database stop or failover, cache loss, a worker killed with a job pending, AI provider 429 or hang, property provider 401/503 and harvester outage, telephony media disconnect, duplicate webhooks, object-storage errors, a provider-specific network block, frontend chunk invalidation and restore from backup. No hidden manual reconciliation.
- **T19.10 Measure capacity for one brokerage and 20 agents.** Concurrent page loads, CRM reads and writes, 10–20 simultaneous Neoh chats, representative calls, media uploads, background jobs, provider outages and bursts. Re-measure the mocked figures (first-token p95 4.7 s at 50 chats with 16 slots; peak queue wait 6 s at 8 workers; target, not measured on DigitalOcean) with real models (A10 is owned by 16).
- **T19.11 Rehearse rollback and incidents.** Roll back to the previous digest, keep the database compatible across rollback, sequence provider rollouts, use recovery and egress safe mode, and record owner approvals in the audit trail. Run a deployment-freeze and incident rehearsal before the 20-agent launch.
- **T19.12 Check privacy on the infrastructure.** Verify erasure after a backup restore and signed-media expiry independently, and add cross-region processing to the data-flow map (see 17).
- **T19.13 Publish one release evidence report.** Include readiness, owner gates, the security gate, automated QA, deployment health and the on-call plan, versioned and dated. Never fold WARN or BLOCKED into a green summary.

## 5. Failure and adversarial cases

- **F19.01 Staging reported as production.** A staging pass or old JSON is presented as production readiness.
- **F19.02 Secrets cross environments.** A staging credential reaches production, or a rollback sends live calls to staging.
- **F19.03 Migration breaks rollback.** A contract step runs before rollout ends, and the previous digest cannot read the schema.
- **F19.04 Silent provider failure.** A property provider returns 401 or 503 and no alert fires, or the UI shows stale data as current.
- **F19.05 Pool exhaustion.** Replicas and workers open more connections than the database allows under load.
- **F19.06 Alert nobody reads.** An alert goes only to logs or to a placeholder address.
- **F19.07 Restore brings back erased data** or loses the erasure directives.
- **F19.08 Manual fix hidden.** A drill passes only because someone reconciled state by hand, and the report does not say so.

## 6. Acceptance criteria

- **AC19.01 Production is inventoried and its readiness is fresh.** Evidence: dated `neoh-launch-readiness.py --env production` JSON at the current SHA, and the inventory with no secret values. Covers: T19.01, T19.02, F19.01.
- **AC19.02 The owner approves production creation before it exists.** Evidence: owner decision recorded in the ledger (L19-02). Covers: T19.03.
- **AC19.03 CI blocks a bad release.** Each gate fails the pipeline when broken. Evidence: one deliberately failing case per gate, run in CI. Covers: T19.04.
- **AC19.04 Environments cannot cross.** Evidence: `scripts/render-app-spec.py --check` extended to compare secret names and webhook URLs per environment, plus a rollback drill showing live webhooks stay on production. Covers: T19.05, F19.02.
- **AC19.05 Upgrade, rollback and cold restore work.** Evidence: drill log of an expand, backfill, rollback to the previous digest and a cold restore, with times. Covers: T19.06, T19.11, F19.03.
- **AC19.06 Every SLO has an alert that reaches a named person.** Evidence: a fired test alert per SLO, received by the on-call owner. Covers: T19.07, T19.08, F19.06.
- **AC19.07 Provider failures are visible and honest.** A property provider 401/503 shows as degraded in health and in the UI. Evidence: failure-injection test per provider. Covers: T19.07, T19.09, F19.04.
- **AC19.08 Drills run on the target platform with no hidden manual steps.** Evidence: drill report listing each fault, the result and every manual action. Covers: T19.09, F19.08.
- **AC19.09 Capacity is measured on DigitalOcean with real models.** Evidence: committed load command with conditions (C9.3) and p50/p95, connection peaks and cost. Covers: T19.10, F19.05.
- **AC19.10 Erasure survives restore on the target platform.** Evidence: restore drill plus `scripts/reapply-erasures.py` output. Covers: T19.12, F19.07.
- **AC19.11 A signed, dated go/no-go record exists.** It shows zero unresolved release-blocking checks and keeps every WARN and BLOCKED visible. Evidence: the T19.13 report signed by the owner. Covers: T19.13, F19.01.

## 7. Out of scope, owner decisions, category-specific rules

- **Pushing to main deploys staging** (C4.4). Treat every push as a deploy; get approval first.
- **Production creation needs owner approval and budget** (L19-02). Do not create apps, databases or DNS records without it.
- **MLS ingest health and MLS chaos cases are out of scope** (D-2026-10-12-01). The legacy `mls` health component and `docs/runbooks/mls-provider-down.md` are gated with the rest of legacy MLS in 11.
- **Never print secret values** in logs, reports or the ledger. Read names and presence only.
- **A10 (local capacity is not production evidence) is owned by 16;** this category supplies the target platform it runs on.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the platform inventory and fresh readiness reports;
  - the production creation plan for the owner;
  - SLOs, alerts and runbooks for the property providers;
  - drill, restore and capacity reports;
  - the release evidence report (`docs/launch-readiness/release-evidence.md`, proposed).
- **First slice:** T19.01 + T19.02 + AC19.01, a read-only refresh that replaces the 2026-10-03 snapshot. Then T19.03 + AC19.02, the owner decision on production.
- **Ledger:** update L19-01 and L19-02, then add L19-03 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
