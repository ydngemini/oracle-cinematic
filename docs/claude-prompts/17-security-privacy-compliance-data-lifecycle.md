# Claude implementation prompt 17: Security, privacy, regulatory compliance, data retention and incident response

> Repository: `ydngemini/oracle-cinematic`. Production objective: all features and authorized pages usable by **one brokerage and 20 agents**, with separate owner/admin capabilities.
> These are engineering specifications, not claims of implementation, provider availability, cloud access, source-code certification or successful tests.

## Role and initial inspection

Claude, act as principal/staff architect, security reviewer and hands-on implementer. Read `CLAUDE.md`, category index `README.md`, current code, tests, migrations, infrastructure and older audits. Record the current SHA and real source citations. Do not guess existing service names, modify unrelated code, or perform unapproved external/production transactions. Work in atomic, reviewable vertical slices with tests.

**Inspect likely files first:** backend/auth.py; backend/tenancy.py; backend/privacy_lifecycle.py; backend/ai_tool_policy.py; backend/ai_chat_store.py; backend/db/migrations/; oracle-app/src/lib/clearPrivateCaches.js; oracle-app/src/sw-oracle.js; docs/security-launch-gate.json; docs/security-launch-review.md; docs/privacy-data-map.md; docs/privacy-counsel-checklist.md; docs/support-model.md

## Extensive work specification

### Mission
Protect brokerage and customer information across all pages, AI context, voice, media, backups and providers; execute real 20-agent release security gates. GitHub CI/security PASS on staging is not evidence of production authorization correctness.

### Hardening plan
1. Re-read current security review and 2026-10-06 gate, verify current commit/infra differs from older local/staging snapshots. Preserve verified security fixes (session revocation, signed webhooks, RLS, role changes, operator MFA, recovery/egress guards). Retest after ACL schema modifications.
2. Enforce cross-agent *within same brokerage* isolation in addition to cross-tenant isolation. Tenant-level RLS on contacts and clients is insufficient for unshared personal records; audit grants and row access on all dependent tables, export, search, AI tools, media, background jobs and privileged functions. Do not expose existence in unauthorized errors.
3. Inventory every sensitive field, including docs/privacy-data-map.md notes on plaintext clients, leads.payload, sms_messages, transcripts and model outputs. Implement staged, migration-safe encryption with searchable blind indexes as needed, dual read/write transition, safe legacy backfill, validation, DR compatibility and erasure. Evaluate memory/storage/log duplicates.
4. Update service-worker IndexedDB, browser persistence, prefetch caches, signed links and AI vector summaries on session change, agent reassignment, deleted records and revocation; public capability links must have expiration and audience/purpose binding. Force bounded access invalidation.
5. Authentication/session: login rate limits, token freshness, MFA for broker owners when required, privilege elevation audit, invitation expiry/single use, password reset security, policy acceptance and fresh permission state on WebSockets. Operator-only functions never reachable through brokerage owner route.
6. Privacy/lawyer gate: brokerage controller versus platform processor terms, DPA/subprocessor list, marketing consent/TCPA and internal DNC, recording disclosure, cross-border model processing (DashScope default region noted as Singapore), model training opt-outs and data retention schedules require counsel/contract review. No pretend legal sign-off.
7. Data subject lifecycle: access/export/deletion/offboard/hold, user attribution, derived caches/embeddings, real-time agent context, backups after restore, orphan reconstruction files and provider copies. Test live restore re-application of erasures in safe environment.
8. Secure third-party and AI use: scoped keys/secrets, encrypted at rest, no prompt/log PII, model/training license provenance, prompt injection boundaries, no unauthorized calls/emails/financial actions. Provider breach and outage response/runbooks with incident severity.
9. API/infra: CSRF, CORS/CSP, request limits, SSRF, webhooks, signed uploads, SQL injection, file MIME validation/malware scanning, WebSocket auth, rate limits/budgets, dependencies/SBOM/SAST/DAST and deploy secret gates. Full authenticated OWASP dynamic scan in staging and compare findings with known accepted residuals.
10. Build formal RBAC/ABAC security matrix against 20 brokerage accounts (assigned/shared/team manager/owner/suspended) and a second brokerage; include concurrency, SQL-level claims, cache leaks, stale sockets, tool execution and signed media.
11. Produce auditable privacy compliance and incident readiness report with external/legal signoffs clearly labeled as human-owned; risk acceptance requires responsible signatory and impact.
12. Train owner/operator workflows: support channel, urgent route, incident notification, revocation/offboarding, consent explanation, billing disputes and data export. Support address cannot remain a placeholder.

### Release gate
Zero known unresolved critical/high unauthorized-access issues, legal/data-processor decisions documented, completed application and DB cross-agent RLS tests, PII remediation or formally approved risk decision, production secrets and backups verified, DAST with justified risks, support/incident routing live. No claim of “guaranteed secure.”

## Required implementation discipline

1. **Audit and plan with evidence:** construct source/route/API/dependency map, existing verified behavior, exact gaps, alternate design options, permission and failure analysis. A source file's existence does not establish product availability.
2. **Change real code:** integrate frontend, API, database and worker/provider as needed. Use additive migrations, backward compatibility, durability, bounded network/CPU/model/GPU cost and appropriate observability. Preserve existing React/Vite + FastAPI/Postgres/PlayCanvas architecture where useful.
3. **Prove cross-cutting security:** tenant, assigned agent, team share, owner and platform admin roles in DB RLS, APIs, tools, cache, search, media, external connectors and queue execution. Approval and provider receipts required for side effects.
4. **Test:** unit/schema/property, real PostgreSQL RLS, frontend components, Playwright browser/mobile, concurrency, mocked failure-injection and approved provider sandbox/live cases. Never run unsolicited customer communications, advertising purchases, production deployment or payment.
5. **Do not overclaim:** classify simulated vs actual provider tests, design targets vs measurements, model prediction vs verified fact, staged vs delivered, and staging CI vs production. Include source refs, p50/p95, errors, rollback, migration coverage, signed owner/legal/provider decisions.

## Your output after each implementation slice

Current baseline and affected code paths; concrete changes and migrations; tests/measurements actually run; verified role and customer journey outcomes; remaining blockers, current release verdict, linked commit(s), and the next highest-dependency implementation task. Update the master ALL-PAGES-MATRIX and category backlog.
