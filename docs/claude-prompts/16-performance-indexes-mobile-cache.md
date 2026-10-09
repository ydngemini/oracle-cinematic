# Claude implementation prompt 16: Performance engineering: mobile loading, PostgreSQL indexes, search, cache and real-time workloads

> Repository: `ydngemini/oracle-cinematic`. Production objective: all features and authorized pages usable by **one brokerage and 20 agents**, with separate owner/admin capabilities.
> These are engineering specifications, not claims of implementation, provider availability, cloud access, source-code certification or successful tests.

## Role and initial inspection

Claude, act as principal/staff architect, security reviewer and hands-on implementer. Read `CLAUDE.md`, category index `README.md`, current code, tests, migrations, infrastructure and older audits. Record the current SHA and real source citations. Do not guess existing service names, modify unrelated code, or perform unapproved external/production transactions. Work in atomic, reviewable vertical slices with tests.

**Inspect likely files first:** backend/db/connection.py; backend/db/migrations/0054_agent_contacts_and_intake.sql; backend/db/migrations/0059_contact_search_and_assignment_integrity.sql; backend/db/migrations/0113_app_visible_tenants.sql; backend/db/migrations/0116_mls_search_order_index.sql; backend/search_api.py; backend/crm.py; oracle-app/vite.config.js; oracle-app/bundle-budget.json; oracle-app/src/neoh/UniversalWorkspace.jsx; oracle-app/src/sw-oracle.js; docs/performance-targets.md; docs/capacity-plan.md

## Extensive work specification

### Goal
First meaningful mobile load fast, every authorized record indexed efficiently, AI context assembled under budget, voice tool calls responsive, and high-quality image/3D assets progressively delivered with privacy-preserving cache invalidation.

### Execution plan
1. Capture representative authenticated browser waterfalls for iPhone Safari and Android Chrome with clean cache, warm cache, restricted LTE/4G, slow device and typical device. Record connection negotiation, auth gate, JS/CSS, fonts, images, API waterfall, long tasks, LCP, INP, CLS, transferred/compressed KB and work done offscreen. Do not mistake bundle ceiling for actual initial bytes.
2. Inspect App.jsx initial static imports, CrmShell lazy chunks, top-level provider rerenders, nested Work/AI/Property/Deals/Sales imports, 3D/PlayCanvas and media editor boundaries. Use build visualizer/profiler; defer heavy features until intentional navigation, route-aware prefetch within network and battery budget.
3. Implement skeleton UX only where data truly loading; preserve previous data where appropriate and show stale/degraded status. Add AbortController cancellation for abandoned Work search and page transitions; avoid runaway requests and stale response overwrites.
4. At DB layer inventory exact existing indexes, query fingerprints and current RLS. Need hybrid permission queries: (tenant_id, assigned_user UUID, updated_at DESC, stable_id DESC), team membership, resource grants by principal and by target, team task queues, approvals, message timeline and calendar windows. Index *only* after duplicate review and EXPLAIN (ANALYZE, BUFFERS) under production-equivalent app DB role.
5. Adopt keyset pagination of authorized record IDs before expensive per-row summaries, preserving existing 20k-client query-shape improvements. Beware permissive RLS OR semantics and planner index mismatches like documented MLS order expression issue; benchmark after ACL enforcement.
6. Use multi-tier caching: browser assets with immutable hashes; client-safe ephemeral cache keyed actor/tenant/permission revision; Redis short-lived source-backed projections; DB read-optimized summary tables/materialized projections. Avoid blanket caching of sensitive contact/media in service worker, and invalidate immediately on role change/assignment/revocation/logout. Never share cached AI evidence across users with different rights.
7. Reuse source-index and relationship projections for semantic search and AI context compiler; protect embedding leakage; use event/outbox invalidation and bounded TTL as defense in depth. Cache is not authorization.
8. Optimize indexes for search exact, prefix/trigram or full-text and candidate re-ranking. Keep encrypted PII lookup via appropriately keyed indexes; do not index plaintext for speed. Respect licensed source entitlements and per-agent ACL before embeddings reach model.
9. Instrument p50/p95/p99 latency of request, Postgres pool wait, query time, external provider, LLM model, cache hit and job queue wait; monitor cardinality, query plan drift, dead rows, vacuum, locks, auto statistics and index write/storage cost. pg_stat_statements may contain sensitive query text; redact operational traces.
10. Capacity: tested mock jobs at 4 workers had 403s queue wait; 8 workers ~6s, but those are historical local outcomes. Re-evaluate configured worker counts, DB pool, 20 simultaneous agents, operator and provider jobs on current staging/live topology. Real model latency and DigitalOcean CPU are unknown until measured.
11. Keep 3D SOG/splat assets out of CRM shell; progressively stream mesh and texture, use LOD, chunk decompression/decoding off main thread, cleanup GPU memory, show photo fallback if WebGL fails.
12. Publish a performance budget per view and API: initial useful content p75 <2s under defined conditions as *target*, indexed CRM p95 <300–800ms depending operation, simple cached context p95 <200–300ms; measure and refine rather than pretending achieved.

### Acceptance
20-user realistic browser+API concurrency benchmark, indexed query plans under RLS, low-end mobile and warm/cold page tests, no unauthorized stale cache response, no infinite app spinner, no broken route lazy chunk, recovery from CDN/WebGL/service-worker failure, and documented before/after per optimization.

## Required implementation discipline

1. **Audit and plan with evidence:** construct source/route/API/dependency map, existing verified behavior, exact gaps, alternate design options, permission and failure analysis. A source file's existence does not establish product availability.
2. **Change real code:** integrate frontend, API, database and worker/provider as needed. Use additive migrations, backward compatibility, durability, bounded network/CPU/model/GPU cost and appropriate observability. Preserve existing React/Vite + FastAPI/Postgres/PlayCanvas architecture where useful.
3. **Prove cross-cutting security:** tenant, assigned agent, team share, owner and platform admin roles in DB RLS, APIs, tools, cache, search, media, external connectors and queue execution. Approval and provider receipts required for side effects.
4. **Test:** unit/schema/property, real PostgreSQL RLS, frontend components, Playwright browser/mobile, concurrency, mocked failure-injection and approved provider sandbox/live cases. Never run unsolicited customer communications, advertising purchases, production deployment or payment.
5. **Do not overclaim:** classify simulated vs actual provider tests, design targets vs measurements, model prediction vs verified fact, staged vs delivered, and staging CI vs production. Include source refs, p50/p95, errors, rollback, migration coverage, signed owner/legal/provider decisions.

## Your output after each implementation slice

Current baseline and affected code paths; concrete changes and migrations; tests/measurements actually run; verified role and customer journey outcomes; remaining blockers, current release verdict, linked commit(s), and the next highest-dependency implementation task. Update the master ALL-PAGES-MATRIX and category backlog.
