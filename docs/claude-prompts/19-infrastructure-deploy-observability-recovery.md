# Claude implementation prompt 19: Production infrastructure, CI/CD, monitoring, backup, resilience and safe releases

> Repository: `ydngemini/oracle-cinematic`. Production objective: all features and authorized pages usable by **one brokerage and 20 agents**, with separate owner/admin capabilities.
> These are engineering specifications, not claims of implementation, provider availability, cloud access, source-code certification or successful tests.

## Role and initial inspection

Claude, act as principal/staff architect, security reviewer and hands-on implementer. Read `CLAUDE.md`, category index `README.md`, current code, tests, migrations, infrastructure and older audits. Record the current SHA and real source citations. Do not guess existing service names, modify unrelated code, or perform unapproved external/production transactions. Work in atomic, reviewable vertical slices with tests.

**Inspect likely files first:** infra/digitalocean/; .github/workflows/; backend/server.py; backend/db/connection.py; backend/automation_jobs.py; scripts/neoh-launch-readiness.py; docs/launch-readiness/production.json; docs/launch-readiness/staging.json; docs/capacity-plan.md; docs/launch-state.md; docs/support-model.md; docs/runbooks/README.md

## Extensive work specification

### Goal
An actually deployed, observable, recoverable SaaS for 20 agents with production secrets and validated provider contracts. Distinguish verified staging CI from production reality; historical 2026-10-03 production snapshot was BLOCKED with no app, while later CI staging/security passed. Re-run fresh checks.

### Engineering instructions
1. Pin current SHA, enumerate DigitalOcean staging/production apps, API/web/background replicas, Postgres cluster, Valkey, object storage, DNS/domain/TLS, registry images, identity/secrets, backup status, firewall/trusted-source decisions and each required production provider credential without printing secret values.
2. Run production readiness script against current target and record full fresh result, status/date/evidence; do not treat historical JSON as proof of today's state. No production changes without explicit operator authorization and an approved change plan.
3. Ensure CI gates: backend lint/type/tests, frontend lint/type/tests/build, migrations on fresh and upgrade DB, real Postgres RLS security, dependency/secret scanning, authenticated DAST, smoke tests, schema compatibility, browser route crawl and performance budget checks.
4. Separate staging and production secrets, webhook URLs and Stripe test/live modes. Fail safely on missing configured secrets; rollback should not accidentally point live calls to staging. Build images reproducibly by digest/SHA; report running SHA from deployment.
5. DB operations: expansion migrations first, backfill with bounded batches and checkpoints, dual reads only when needed, explicit contract shrink after rollout; prove schema upgrade and cold restore. Ensure connection pools, replica count and worker slots match DB connection limits.
6. Set SLO/alerts for login/auth, client list/search, AI first token, voice first audio and call drops, outbox queue depth/age, webhook loss, stale MLS ingest, billing anomalies, DPA privacy event handling, provider spend, DB CPU/locks/replication, WebSocket and frontend crashes.
7. Correlated traces (request/workflow/tenant/user IDs with sensitive-data safeguards) across browser, API, Postgres, model gateways, background workers, provider callbacks and operator dashboard. Establish alert owner and on-call route; no unresolved “TODO support email.”
8. Resilience and chaos testing on target infrastructure: DB stop/failover, cache loss, worker kill while job pending, AI provider 429/hang, MLS 401/503, telephony media disconnect, webhook duplicates, S3 error, provider-specific network block, frontend chunk invalidation and restore from backups. No hidden manual reconciliation.
9. Capacity for one brokerage plus 20 agents: concurrent page loads, CRM reads/writes, 10–20 simultaneous Neoh chats, representative calls, media uploads, background jobs, provider outages and unexpected burst. Prior mock benchmarks with first-token p95 4.7s at 50 chat VUs with 16 slots and queue 6s at 8 workers must be remeasured with real models and DigitalOcean.
10. Establish clear rollback deployment (previous digest), database compatibility across rollbacks, provider rollout sequencing, recovery/egress safe mode, owner approvals and audit trail. Run real deployment-freeze and incident rehearsal ahead of 20-user launch.
11. Independently verify privacy deletion after backup restore and signed media expiration. Consider cross-region processing in infrastructure/data flow mapping.
12. Publish launch readiness, owner-gate status, security gate, automated QA evidence, actual deployment health and on-call/escalation plan in one versioned release evidence report. Never merge all WARN and BLOCKED into green summary.

### Done
A signed, dated production go/no-go record with zero unresolved release-blocking checks, deployed current SHA, real provider and worker health, tested backup restore and rollback, bounded load/providercost, and operational support accountable to a person.

## Required implementation discipline

1. **Audit and plan with evidence:** construct source/route/API/dependency map, existing verified behavior, exact gaps, alternate design options, permission and failure analysis. A source file's existence does not establish product availability.
2. **Change real code:** integrate frontend, API, database and worker/provider as needed. Use additive migrations, backward compatibility, durability, bounded network/CPU/model/GPU cost and appropriate observability. Preserve existing React/Vite + FastAPI/Postgres/PlayCanvas architecture where useful.
3. **Prove cross-cutting security:** tenant, assigned agent, team share, owner and platform admin roles in DB RLS, APIs, tools, cache, search, media, external connectors and queue execution. Approval and provider receipts required for side effects.
4. **Test:** unit/schema/property, real PostgreSQL RLS, frontend components, Playwright browser/mobile, concurrency, mocked failure-injection and approved provider sandbox/live cases. Never run unsolicited customer communications, advertising purchases, production deployment or payment.
5. **Do not overclaim:** classify simulated vs actual provider tests, design targets vs measurements, model prediction vs verified fact, staged vs delivered, and staging CI vs production. Include source refs, p50/p95, errors, rollback, migration coverage, signed owner/legal/provider decisions.

## Your output after each implementation slice

Current baseline and affected code paths; concrete changes and migrations; tests/measurements actually run; verified role and customer journey outcomes; remaining blockers, current release verdict, linked commit(s), and the next highest-dependency implementation task. Update the master ALL-PAGES-MATRIX and category backlog.
