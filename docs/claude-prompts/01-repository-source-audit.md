# Claude implementation prompt 01: Repo-wide source audit, dependency map and refactoring

> Working repository: `ydngemini/oracle-cinematic` (shipping FastAPI backend and React/Vite browser app).
> Scope: all-pages, fully functional Neoh for **one brokerage + 20 participating agents** (plus explicit owner/admin seats).
> This is an INSTRUCTION and acceptance specification, not a declaration of implemented features, existing provider permissions, tested production, or code already changed.

## Claude: your assignment

Act as principal engineer, security reviewer and implementation owner. Start by reading root `CLAUDE.md`, `docs/neoh-full-platform-source-audit-2026-10-08.md`, the [master prompt index](README.md), and relevant category specifications. Inspect current HEAD and reference actual source lines before assuming older audit findings still apply. Implement in bounded, reviewable slices. **Do not stop at outlining plans** when code/test work is possible. Preserve authentic, accessible product behavior, responsible budgets, explicit user consent, no unauthorized external side effects, reproducible tests and durable provider receipts.

**Initial source paths (verify current reachability):** backend/; oracle-app/; backend/db/migrations/; scripts/; .github/workflows/; infra/digitalocean/; tests/; README.md; docs/neoh-full-platform-source-audit-2026-10-08.md

## Detailed engineering tasks

### Deliver a reproducible architectural inventory
1. Pin the base SHA and enumerate every tracked file, extension, path, approximate lines, owner/domain, size, last revision and reachability. Read code, not filenames alone. Mark production-entry, runtime dependency, test, infrastructure, documentation, optional integration, archived/prototype and orphaned. A previous audit counted 1,637 files; remeasure. Do NOT report all lines read until verifiably inspected.
2. Build a dependency graph of frontend route -> component -> state hook -> REST/WebSocket -> backend router -> domain service -> storage/queue -> callback -> customer-visible state. Include non-HTTP jobs, incoming webhooks, cron/scheduled tasks and public capability-link routes. Distinguish potential code paths from exercised ones.
3. Examine Python service modules line by line in *bounded, logged batches*: inputs, imports, ownership, lifetime, tenant/agent context, error handling, data validation, SQL, provider SDK, timeouts, circuit breakers, retries, idempotency, audit, PII, logging, event loop safety and race conditions. Record exact path:line and tests, avoid regex-only assertions. Do the same for JSX/TS components, hooks, CSS and feature/route entries.
4. Review every SQL migration in sequence for compatibility, privilege grants, RLS interactions, indexes, constraints, encryption, outbox, irreversible operations and cold-start restore. Cross-check app runtime role with SECURITY DEFINER functions and session claims. Generate a migration-to-code usage crosswalk.
5. Evaluate API-to-UI contract mismatches: list pagination vs totals, legacy and canonical client IDs, status enumeration, date/time-zone semantics, blank strings versus missing facts, billing event states and provider acceptance versus actual delivery.
6. Review complex files such as backend/commands_api.py, backend/ai_chat_store.py, backend/crm.py, backend/server.py, backend/reconstruction_worker.py and backend/auth.py, but resist rewriting them solely due to length. Extract bounded domains behind versioned public interfaces and regression tests.
7. Inspect JS bundle/import graph, dead prototype import risks and route state. Historical src/ Next.js is not automatically deployed; inspect shipping README and Vite entry. Isolate high-cost libraries, unreachable panels, obsolete docs and unused provider code only after call graph evidence.
8. Compare current tests to reachable behavior and risk. Map unit, integration, real PostgreSQL/RLS, React, Playwright, provider sandbox, load, backup and real-device testing; report actual run environment and whether tests executed or only exist.
9. Maintain category labels CONFIRMED_DEFECT, VERIFIED_GAP, UNPROVEN_RISK, PERFORMANCE_HYPOTHESIS, INTEGRATION_MISMATCH and COMPETITIVE_ENHANCEMENT. Rate severity independently of customer impact. Revalidate 2026-10-08 findings against current SHA, including plaintext data, failed property lookups, billing event meaning and quality labels.
10. Produce an actionable engineering matrix: path:line, current behavior, reproduction, fix, dependencies, risk, targeted test, benchmark, owner/role, done/blocked, source SHA and associated commit. Incrementally commit fixes with passing suites; do not post a one-shot rewrite.

### Adversarial reviews
- Same-brokerage cross-agent data access through search, details, AI context, export, caches and signed media; cross-brokerage denials.
- Double tool invocation, repeated callbacks, crash between provider acceptance and DB commit, retries after credential revocation.
- Prompt injection in notes/uploads/URLs; log and analytics exposure; stale memory from changed permissions.
- 20 concurrent users and a manager: auth, permissions, pool contention, pagination, stuck background jobs.
- Source reported complete when a required provider timed out; missing metrics treated as professional success.

### Completion artifacts
REPO-AUDIT-LEDGER.md plus CSV/JSON ledger, service dependency map, security findings with reproducible tests, deduped backlog, safe refactor PRs, line coverage accounting and known-uninspected lists.

## Required engineering workflow

1. Map concrete sources and call graph; document code paths, schema, external connections, permissions, failure states, test setup and observed baseline. Keep a table of **existing verified**, **proposed**, **implemented**, **tested** and **blocked**; never merge categories.
2. Implement one vertical slice including frontend, API, storage/RLS, provider/worker, customer-visible error/retry, tests and telemetry where appropriate. Use additive migrations and backwards-compatible API changes; preserve former route aliases and workflows.
3. Add unit and contract tests plus real PostgreSQL/RLS, React component, Playwright, concurrency, permission or live/sandbox provider testing as applicable. Do not contact real customers, send external communications, purchase services, deploy live or execute real financial transactions without explicit authorized approvals.
4. Run and report commands and exit codes, separate simulated and actual provider tests, measure p50/p95 where applicable, inspect logs for PII, and verify no regressed authorized page.
5. Commit focused code changes with changelog, migration notes, rollback/recovery instructions, exact acceptance evidence and remaining issues. Never use “done” or “A+” for untested paths; record external contractual, legal, licensing, operational or device blockers.

## Cross-cutting rules

- **All pages remain in launch scope**; honest setup-required gates are allowed when a customer must grant an external license/credential, but absent advertised functionality remains unfinished.
- **Hybrid brokerage access:** tenant boundary, agent assignments, explicit shares and team privileges must apply to DB, API, search, AI, jobs and browser caches.
- **Truthfulness:** missing facts, timed-out sources, non-verified 3D geometry and provisional model estimates are not verified outcomes.
- **Approval:** model suggestions are never authorization. Legal instruments, outreach, publishing, calendar writes, spend and high-impact changes require their established approvals.
- **Efficiency:** reuse FastAPI/Postgres/React/PlayCanvas and current job/tool architecture. Avoid duplicate frameworks or microservices unless benchmarks prove benefit.
- **Launch:** 20 agents concurrently plus owner/operator, actual iOS/Android browser tests, production-shaped deployment, cost and reliability measurement.

## Output format

Return a concise source-linked baseline, chosen vertical slice, completed files and migrations, tests run with results, measurable before/after where available, next dependent tasks, and blockers requiring human approvals. Update the master category matrix rather than duplicating contradictory backlogs.
