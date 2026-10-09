# Claude implementation prompt 20: All-feature 20-agent acceptance, field testing, competition and release certification

> Repository: `ydngemini/oracle-cinematic`. Production objective: all features and authorized pages usable by **one brokerage and 20 agents**, with separate owner/admin capabilities.
> These are engineering specifications, not claims of implementation, provider availability, cloud access, source-code certification or successful tests.

## Role and initial inspection

Claude, act as principal/staff architect, security reviewer and hands-on implementer. Read `CLAUDE.md`, category index `README.md`, current code, tests, migrations, infrastructure and older audits. Record the current SHA and real source citations. Do not guess existing service names, modify unrelated code, or perform unapproved external/production transactions. Work in atomic, reviewable vertical slices with tests.

**Inspect likely files first:** docs/first-brokerage-launch.md; docs/brokerage-go-live-checklist.md; docs/pilot-metrics.md; docs/pilot-feedback.md; docs/neoh-full-platform-source-audit-2026-10-08.md; docs/neoh-space-web-capture-claude.md; scripts/audit-neoh-production.py; scripts/test-crm-walkthrough-playwright.py; oracle-app/src/routes.js; oracle-app/src/components/OurAITab.jsx; docs/security-launch-gate.json

## Extensive work specification

### Unambiguous release objective
All authorized Neoh surfaces and promised capabilities available for **one brokerage with 20 agent seats** (plus owner), not a 10-user MVP. Honest external-provider “needs credentials/license” only when complete integration/setup workflow exists; internal placeholder functionality is a launch gap. Never claim production-ready from source inspection or passing staging CI.

### Comprehensive launch certification
1. Maintain single master ALL-PAGES-MATRIX with every route/alias and nested view: Home; Work 13 types; Neoh; Contacts/People; conversations; Properties 6 subviews; Deals 5 subviews; Opportunity/Agent Twin; missions; Neoh tools 8 nested workspaces; Sales 5 subpages; Sites/Video; Profile/AI/Broker setup; dedicated broker management; platform admin; all public capability-link routes. Each: code component, API, permission, external dependency, UI state, end-to-end test, observed result, screenshot, timestamp and release status.
2. Import category 01–19 implementation gaps into one dependency-aware backlog; dedupe issues with the platform audit. Avoid declaring “working” based solely on a React tab and placeholder ledger status.
3. Define all-feature scope with owner: MLS entitlements, actual supported carrier(s), SMS registration, email/calendar vendors, Stripe pricing, live advertising connectors, customer search portal, WordPress IDX, SEO/AEO/GEO, commission accounting, browser-only space reconstruction, skeletal mascot and premium studio. Confirm which are included in customer contract; no hidden removal or feature misrepresentation.
4. Validate owner plus 20 agents, at least two team leads, mixed assignment/sharing, one extra adversarial brokerage. Verify accounts, seat billing, owner/team management, one person's complete CRM lifecycle, communications, property data, deal, approval, export and offboarding under both positive and negative rights.
5. Upgrade scripts/audit-neoh-production.py to crawl **every route and nested workspace** in Chromium and WebKit/mobile browsers, valid and invalid permission roles, configured and unconfigured providers, offline/stale/error states and deep links; no uncaught runtime errors, button dead ends, broken imports, horizontal overflow or unexpected 4xx/5xx.
6. Separate visibility tests from functional tests: one nav test per page, one real authorization test per role, one form/action test per advertised workflow, one failure/degraded test per external provider, and one 20-user performance/soak test. Use sandbox/test accounts and human-approved destinations only.
7. End-to-end scenarios: new buyer -> signed lead -> dedup -> assignment -> approved contact -> real carrier receipt -> showing -> CRM timeline -> deal/contract milestones -> brokerage report; buyer asks complex semantic question -> evidence-backed tools -> verified result; homeowner receives share link; mobile browser scans room -> upload resume -> reconstruction -> certified only when proved; marketing site draft -> approve -> publish -> lead -> CRM; owner revokes access during queued AI action.
8. Persona-level acceptance: agent sees assigned plus granted, manager team permitted data, owner brokerage oversight with controlled private detail, operator platform infrastructure; unauthenticated customer capabilities only with valid purpose-limited tokens.
9. Quality targets specified separately from measured: mobile useful Home p75 under 2s on defined devices, indexed CRM p95 target <800ms, AI first token target <10s on representative concurrency, safe and interruptible speech, actual property media quality/field certification and animation frame stability. Record sample, percentile, run environment and failures.
10. Competition benchmark: compare real flows against current Follow Up Boss, BoldTrail, Lofty, CINC and Matterport; use current documented features and direct user-journey tests rather than invented rival limitations. Look for verifiable strengths: trustworthy end-to-end AI outcomes, brokerage intelligence, integrated calls and tours, permission-safe knowledge, low-latency user experience and measurable workflows.
11. Production gates: real production deployment, 20 seats, provider DPAs and counsel decisions, working consent rules, messaging registration, source licensing, security/DAST and within-broker RLS, backups/restore, rollbacks, on-call support, spend caps, load and incident drills. Re-run readiness; previous 2026-10-03 snapshot cannot prove present state.
12. Run prelaunch synthetic rehearsal, owner walkthrough and staged 5 -> 20 agent invitation only when all-pages contract is satisfied; monitor usage, failed jobs, wrong-recipient risk, AI hallucination, refunds and support. Day 1 and Day 7 reviews use pilot metrics; distinguish “revenue influenced” association from causation.
13. Issue a release go/no-go dossier: per-page GREEN/YELLOW/RED with evidence, owner/external provider tasks, migration compatibility, security exceptions signed off, test run links and approved plan. RED advertised core functions stop the “fully available” claim.

### Definition of done
Every promised authorized page is reachable and functional, not merely visible. Real cloud+provider validation, protected records, 20-agent workload, honest AI, verified external outcomes, recoverable failures and owner acceptance are demonstrated and linked. Unverified/untested parts remain plainly marked incomplete.

## Required implementation discipline

1. **Audit and plan with evidence:** construct source/route/API/dependency map, existing verified behavior, exact gaps, alternate design options, permission and failure analysis. A source file's existence does not establish product availability.
2. **Change real code:** integrate frontend, API, database and worker/provider as needed. Use additive migrations, backward compatibility, durability, bounded network/CPU/model/GPU cost and appropriate observability. Preserve existing React/Vite + FastAPI/Postgres/PlayCanvas architecture where useful.
3. **Prove cross-cutting security:** tenant, assigned agent, team share, owner and platform admin roles in DB RLS, APIs, tools, cache, search, media, external connectors and queue execution. Approval and provider receipts required for side effects.
4. **Test:** unit/schema/property, real PostgreSQL RLS, frontend components, Playwright browser/mobile, concurrency, mocked failure-injection and approved provider sandbox/live cases. Never run unsolicited customer communications, advertising purchases, production deployment or payment.
5. **Do not overclaim:** classify simulated vs actual provider tests, design targets vs measurements, model prediction vs verified fact, staged vs delivered, and staging CI vs production. Include source refs, p50/p95, errors, rollback, migration coverage, signed owner/legal/provider decisions.

## Your output after each implementation slice

Current baseline and affected code paths; concrete changes and migrations; tests/measurements actually run; verified role and customer journey outcomes; remaining blockers, current release verdict, linked commit(s), and the next highest-dependency implementation task. Update the master ALL-PAGES-MATRIX and category backlog.
