# 16 — Performance: mobile loading, PostgreSQL indexes, search, cache and real-time load

> **Scope:** Make first mobile load fast, index every authorized record query, keep AI context and voice tool calls inside budget, deliver images and 3D progressively, and cache without ever leaking across rights, all proven by reproducible measurements on production-shaped infrastructure. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01 · **Unblocks:** 20 · **Ledger:** [L16](LEDGER.md#l16) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Database: `backend/db/connection.py`, `backend/db/migrations/0054_agent_contacts_and_intake.sql`, `backend/db/migrations/0059_contact_search_and_assignment_integrity.sql`, `backend/db/migrations/0113_app_visible_tenants.sql`
- Legacy lesson only (D-2026-10-12-01): `backend/db/migrations/0116_mls_search_order_index.sql`
- Queries: `backend/crm.py` (`list_clients`), `backend/search_api.py`, `backend/server.py` (leads page)
- Frontend: `oracle-app/src/App.jsx`, `oracle-app/vite.config.js`, `oracle-app/bundle-budget.json`, `oracle-app/scripts/check-bundle-budget.mjs`, `oracle-app/src/neoh/UniversalWorkspace.jsx`, `oracle-app/src/sw-oracle.js`, `oracle-app/src/lib/clearPrivateCaches.js`
- Load harness: `performance/README.md`, `performance/run.sh`, `performance/scenarios/mixed.js`, `performance/scenarios/read_load.js`, `performance/topology/up.sh`
- Docs: `docs/performance-targets.md`, `docs/capacity-plan.md`, `docs/performance-capacity-runbook.md`; audit A10 in [LEDGER.md](LEDGER.md#audit-cross-reference)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **The request pool defaults to 2–10 connections plus one reserved listener.** `connection.py:114-115` read `ORACLE_DB_POOL_MIN`/`ORACLE_DB_POOL_MAX` (defaults 2 and 10); `:121` `LISTENER_RESERVED_CONNECTIONS = 1` is added for the `ws_hub` LISTEN.
- **Every pooled query has a 30 s ceiling.** `connection.py:384` `command_timeout=30` in `init_pool`.
- **Tenant queries can get an index condition without duplicating RLS.** `0113_app_visible_tenants.sql:24` defines `app_visible_tenants()`; `crm.py:702` (`list_clients`) and `server.py:942` add `tenant_id = ANY (app_visible_tenants())`.
- **The Work list picks its page before decorating rows.** `crm.py:739` `WITH page AS MATERIALIZED (… LIMIT 500)` runs the per-client laterals only on the chosen page.
- **Contact lookups use keyed hashes, not plaintext.** `0054_agent_contacts_and_intake.sql:86` and `:89` index `email_lookup_hash` and `phone_lookup_hash`; `0059_contact_search_and_assignment_integrity.sql:17` adds a GIN index on HMAC `name_search_tokens`.
- **An assigned-agent index exists.** `0054_agent_contacts_and_intake.sql:92` `idx_agent_contacts_agent` on `(tenant_id, assigned_agent_id, updated_at DESC)`.
- **Universal search times out per leg and reports degraded legs.** `search_api.py:73` `LEG_TIMEOUT_MS = 2000`; `:374` appends the kind to `degraded`.
- **Work search drops stale responses but does not cancel requests.** `UniversalWorkspace.jsx:218-221` ignores a response whose `requestId` is old; the file has no `AbortController`.
- **The app shell is mostly static.** `App.jsx:5` and `:11` statically import `CrmShell` and `SitePreview`; only three pages are lazy (`:16`, `:17`, `:20`).
- **The bundle budget is a ceiling, not a target.** `bundle-budget.json` records a largest chunk of 2,325,752 bytes and 4,168,097 bytes in total; `package.json:14` `bundle:check` enforces it.
- **The service worker serves cached splats without a network check.** `sw-oracle.js:268` `serveSplat` returns the IndexedDB copy for `/api/media/*` (`:287`) when present. Logout clears it: `clearPrivateCaches.js:15` posts `CLEAR_PRIVATE_CACHE` (`sw-oracle.js:170`) and deletes every Cache Storage entry (`clearPrivateCaches.js:29-31`).
- **Capacity figures are local, not DigitalOcean.** `capacity-plan.md:15` says "Not DigitalOcean"; `:39` records 403 s oldest job wait with 4 workers and `:40` 6 s with 8 workers; `:129` lists everything on DigitalOcean as unmeasured. No production app exists (ledger L19-02).
- **The HTTP targets still include legacy MLS rows (out of scope, D-2026-10-12-01).** `performance-targets.md:31-33` and `:63` cite MLS search, deep page, listing detail and ingestion.
- **The legacy MLS index shows a planner trap.** `0116_mls_search_order_index.sql:1-16` records that `NULLS LAST` on NOT NULL columns kept a `DESC` index from matching the `ORDER BY`. This is a lesson about index and expression matching, not a feature to optimize.

## 3. Goal

An agent on a mid-range phone over LTE reaches useful content quickly. Every authorized list, search and timeline query uses an index under the real application role and RLS. Caches are keyed by actor and rights and are invalidated when rights change. Every number is a measurement with stated conditions, or is labelled a target.

## 4. Engineering tasks

- **T16.01 Capture real browser waterfalls.** iPhone Safari and Android Chrome; clean and warm cache; throttled LTE/4G; slow and typical devices. Record connection setup, auth gate, JS/CSS, fonts, images, API waterfall, long tasks, LCP, INP, CLS, transferred KB and offscreen work. The bundle ceiling is not the initial byte count.
- **T16.02 Shrink the initial load.** Inspect `App.jsx` static imports, `CrmShell` lazy chunks, provider re-renders, nested Work/AI/Property/Deals/Sales imports, and the PlayCanvas and media-editor boundaries. Use a build visualizer and profiler. Defer heavy features until navigation; prefetch by route within network and battery budgets.
- **T16.03 Load honestly and cancel abandoned work.** Skeletons only while data is really loading; keep previous data where it helps, with stale or degraded status. Add `AbortController` cancellation to Work search and page transitions.
- **T16.04 Inventory indexes and queries before adding any.** List existing indexes, query fingerprints and RLS policies. Candidates: `(tenant_id, assigned_user, updated_at DESC, stable_id DESC)`, team membership, resource grants by principal and by target, team task queues, approvals, message timeline and calendar windows. Add an index only after a duplicate review and `EXPLAIN (ANALYZE, BUFFERS)` under the application role.
- **T16.05 Keyset-paginate authorized IDs first.** Page over authorized record IDs before expensive per-row summaries, keeping the `crm.py` page-first shape. Watch for the RLS `OR` problem and for planner mismatches like the legacy 0116 `ORDER BY` case. Benchmark after access checks.
- **T16.06 Cache in tiers, keyed by rights.** Immutable hashed browser assets; client cache keyed by actor, tenant and permission revision; short-lived Valkey projections; read-optimized summary tables. Never blanket-cache sensitive contact data or media in the service worker. Invalidate on role change, assignment, revocation and logout. Never share cached AI evidence across users with different rights.
- **T16.07 Reuse projections for semantic search and AI context.** Build on the source index and relationship projections; prevent embedding leakage; invalidate by event or outbox with a bounded TTL as backup. Cache is not authorization.
- **T16.08 Index search by kind.** Exact, prefix, trigram or full-text, then re-rank. Keep encrypted PII on keyed lookup indexes; never index plaintext for speed. Apply source entitlements and per-agent access before anything reaches the model.
- **T16.09 Instrument the hot paths.** p50/p95/p99 for request, pool wait, query, provider, model, cache hit and queue wait. Watch cardinality, plan drift, dead rows, vacuum, locks, statistics and index write cost. Redact `pg_stat_statements` text in traces.
- **T16.10 Measure capacity on production-shaped infrastructure (audit A10).** The 4-worker 403 s and 8-worker 6 s results are local history. Re-measure worker count, pool size, 20 concurrent agents, operator and provider jobs on infrastructure shaped like the target. Production does not exist (L19-02), so first write the protocol and run it on staging; label the result "staging", never "production".
- **T16.11 Keep 3D out of the CRM shell.** Stream SOG/splat mesh and textures progressively with LOD, decode off the main thread, free GPU memory, and show a photo fallback when WebGL fails.
- **T16.12 Publish a budget per view and API.** Keep these as targets, not measured: initial useful content p75 < 2 s under defined conditions; indexed CRM p95 < 300–800 ms by operation; simple cached context p95 < 200–300 ms. Measure and refine.
- **T16.13 Retire legacy MLS performance targets (D-2026-10-12-01).** Propose removing or marking legacy the MLS rows in `performance-targets.md` and the `mls_search.js` scenario from launch gates; delete nothing without owner approval.
- **T16.14 Verify whether cached media survives a rights change.** Check whether a splat cached by `serveSplat` is still served after the viewer loses access without logging out, and fix it if so.

## 5. Failure and adversarial cases

- **F16.01 Unauthorized stale cache.** After a role change, reassignment or revocation, a cached page, projection, AI context or splat is still served.
- **F16.02 Infinite spinner.** A failed or slow request leaves a view loading forever.
- **F16.03 Broken lazy chunk.** A deploy removes a chunk that an open tab still requests.
- **F16.04 CDN, WebGL or service-worker failure.** The app does not recover when one of these fails.
- **F16.05 Index unmatched by the planner.** A new index is skipped because of RLS `OR`, a bound-parameter predicate or a sort-direction mismatch.
- **F16.06 Stale response overwrite.** An older response replaces a newer one, or abandoned requests pile up.
- **F16.07 Local numbers reported as production.** A local or mocked benchmark is cited as production capacity.
- **F16.08 Plaintext PII index.** A speed fix indexes decrypted names, emails or phones.

## 6. Acceptance criteria

- **AC16.01 Mobile load is measured.** Cold and warm waterfalls for both phone browsers are recorded with device, network and bytes, with before and after for each change. Evidence: committed measurement protocol and results file `docs/performance/mobile-waterfalls.md` (proposed). Covers: T16.01, T16.02, T16.12.
- **AC16.02 Loading states end.** Every view shows data, an empty state or an error within its timeout, and abandoned searches are cancelled. Evidence: Playwright tests with throttled and failing APIs; a unit test asserting `AbortController.abort` on query change. Covers: T16.03, F16.02, F16.06.
- **AC16.03 Indexed plans under RLS.** Each hot query uses an index under the application role, with no duplicated RLS predicate. Evidence: committed `EXPLAIN (ANALYZE, BUFFERS)` plans plus a test that fails on a sequential scan. Covers: T16.04, T16.05, T16.08, F16.05, F16.08.
- **AC16.04 No unauthorized cache hit.** After role change, reassignment, revocation or logout, no cached page, projection, AI context or splat is served to the old viewer. Evidence: integration and Playwright tests for each trigger. Covers: T16.06, T16.07, T16.14, F16.01.
- **AC16.05 Failures recover.** A missing chunk reloads cleanly; WebGL, CDN and service-worker failures show fallbacks. Evidence: Playwright failure-injection tests. Covers: T16.11, F16.03, F16.04.
- **AC16.06 20-agent concurrency is measured with conditions.** A browser plus API benchmark with 20 agents reports p50/p95/p99, errors, pool wait and queue wait, with environment named. Evidence: committed `performance/run.sh` command and result. Covers: T16.09, T16.10, F16.07.
- **AC16.07 Production-shaped capacity is evidenced (A10).** A run on target-shaped infrastructure is recorded with its topology, or A10 stays open with the blocker named. Evidence: run log and ledger row. Covers: T16.10, F16.07.
- **AC16.08 Legacy MLS targets no longer gate launch.** The target list and gates exclude MLS rows, and nothing is deleted without approval. Evidence: diff of the targets doc and gate config, reviewed by the owner. Covers: T16.13.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS is out of scope** (D-2026-10-12-01). Do not optimize MLS search, ingestion or listing detail. The 0116 migration is cited only as a planner lesson.
- **Browser only** (D-2026-10-08-01): mobile work targets phone browsers, not a native app.
- **Index only after a duplicate review and a plan under the application role.** Never patch performance by duplicating an RLS predicate ([C3.3](CONTRACT.md#c3-tenancy-roles-and-authorization)).
- **Targets stay labelled as targets** until a measurement with conditions replaces them ([C9.3](CONTRACT.md#c9-testing-and-measurement)).
- **Owner question:** what infrastructure counts as production-shaped for A10 while no production app exists, and is staging an acceptable proxy?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - mobile waterfalls before and after;
  - the index inventory with committed plans;
  - the rights-keyed cache design and invalidation tests;
  - the per-view and per-API budget;
  - the 20-agent benchmark and the A10 capacity record.
- **First slice:** T16.04 + T16.05 + AC16.03 (index inventory and plans under RLS). Then T16.06 + T16.14 + AC16.04.
- **Ledger:** add L16-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
