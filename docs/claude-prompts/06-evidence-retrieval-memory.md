# Claude implementation prompt 06: Evidence-based knowledge retrieval, hybrid search and memory

> Repository: `ydngemini/oracle-cinematic` shipping FastAPI + React/Vite. Goal: full-feature browser SaaS, one brokerage and **20 agents**, with a separately accounted owner, not a cut-down MVP.
> This is a specification and task prompt, NOT evidence of completed code, production deployment, provider licensing or tested results.

## Claude: implementation assignment

Act as principal architect and hands-on engineering owner. Read `CLAUDE.md`, `README.md` in this prompt folder, the existing platform audit, and the actual current source. Pin main SHA and inspect path:line, tests, migrations, configurations and provider state before coding. Prioritize verifiable full vertical slices rather than replacing entire systems with unproven frameworks.

**Inspect first:** backend/belief_store.py; backend/intent_states.py; backend/outcome_memory.py; backend/memory_core/session_manager.py; backend/search_api.py; backend/client_ai_automation.py; backend/data_coverage.py; backend/agent_twin.py

## Work program

### Product objective
Reliable, high-quality context for every agent and customer journey. Find the correct records, connect data across systems, distinguish facts from claims and suggestions, and feed only relevant, authorized, fresh evidence into Neoh AI and dashboards.

### Implementation program
1. Inspect all authoritative customer, lead, listing, transaction, communication and event sources; build a source catalog with schema, identifiers, tenant/agent ACL, staleness, licensing, retention, confidence and legal restrictions. Classify facts as verified, reported, inferred, disputed, stale, unavailable, unsupported. Do not use a general number as an undifferentiated “confidence.”
2. Introduce a shared versioned EvidenceFact / EvidenceBundle contract: immutable fact ID, canonical subject, predicate, typed value/unit, source IDs, source observation/event time, valid time, expiration/refresh policy, epistemic status, conflicts, sensitivity, authorized audience, schema revision. Preserve source spans/citations accessible to authorized roles.
3. Centralize the context compiler: given authenticated actor, semantic goal, allowed actions, time budget, tool budget and requested context, identify required fact types; resolve record identities; request minimal reads in parallel; reconcile conflicts; return bounded, privacy-filtered evidence with explicit unknowns. No prompt dumping of arbitrary entire CRM histories.
4. Preserve and improve belief_store.py temporal confidence/source/contradiction machinery and intent_states.py distinction between declared, observed and inferred intent. Design adapters, not a duplicate living-graph implementation.
5. Use search tiers: exact identifiers and tenant-safe blind indexes for protected data; full-text/structured queries for appropriate records; embedding vectors for licensed/permitted semantic data; relationship traversals via indexed foreign keys. Apply permissions before returning/search-exporting content or sending retrieved records to third-party LLM.
6. Define named retrieval legs (clients, properties, deal timeline, calls, files, MLS etc.), each returning checked/partial/timeout/disabled/not_authorized/not_configured with elapsed/freshness metadata. Client automation currently can confuse lookup error with zero matches; test and correct this.
7. Establish memory tiers: ephemeral working context, episodic events with receipts, source-backed semantic facts, authorized agent preferences and procedural settings, outcome memory without false causality. Each with retention and erasure.
8. Build source-version-aware cache projections and invalidation on record edit, assignment/share/revocation, provider sync, permission changes, retention deletion and bad-source retractions. Model-generated summaries are derived artifacts, not authoritative truth.
9. Add provenance-aware answer composition: cite customer-visible source references; clearly label missing or contradictory data; abstain rather than invent listing status, financial eligibility, deadline or room scale. License/MLS restrictions preserved in derived summaries.
10. Instrument retrieval latency, hit/miss, authorization denials, stale fact rate, source conflict, hallucination/unsupported claim rate, context tokens, summarization drift, vendor cost and index amplification.
11. Add offline regression suite across difficult questions: pronouns, conflicting client notes, two properties with same street name, time-zone mismatches, provider outage, RLS sharing/revocation, stale caches, unsupported owner data, ad-hoc CSV uploads containing prompt injection.
12. Optimize retrieval for live voice with pre-authorized contextual snapshots, bounded deadlines, freshness checking and asynchronous read-only sidecar. No shared cache key missing tenant/user/permission revision.

### Acceptance
For each task family, evaluate exact-record precision/recall, source faithfulness, conflict visibility, correct unknown responses, zero inaccessible documents in embeddings/model context, p50/p95 evidence assembly, token cost, and regression tests covering lifecycle invalidation. Provide reproducible gold tasks and explainable summaries.
### Outputs
Source catalog, schema/contract, context compiler integration, hybrid search benchmarks, memory lifecycle implementation, permission-safe cache, evaluation set, trace dashboards and tested production rollout.

## Global acceptance discipline

- **All pages in scope**, including nested Work, Deals, Properties, Sales, AI, settings and public capability routes. Available page does not imply working external integration; implement or truthfully surface setup required, no false “live.”
- Enforce tenant/agent/team/owner permissions at database, API, AI context, jobs, media, search and caches. Do not treat the model as authorization, or legal/financial/property inference as verified evidence.
- Preserve current human approval before external sends, calls, publishing, calendar writes, financial and legal decisions; verify final provider events; use durable idempotency for effectful actions.
- Preserve product tech decisions: PostgreSQL RLS, FastAPI, React/Vite, PlayCanvas/gsplat for tours, browser-only spatial capture, existing workflows and endpoint names unless planned backwards-compatible migration is tested.
- Require tests: unit/contracts, real Postgres/RLS, UI/Playwright, load/failure-injection, provider sandbox/live *only with approved synthetic destinations*, cross-brokerage access, 20-account end-to-end. No unapproved production deployment, charges, sending or real customer calls.
- Distinguish proposal vs code change, mocked vs real benchmark, verified vs inferred, staged vs delivered, deployment staging vs production. Add p50/p95 and cost evidence where relevant.
- Emit source-linked baseline, concrete implementation sequence, tested commits, schema/rollout plan, role/privacy matrix, outcome receipts, blockers and verification reports. Never report completion without executing the relevant tests.

## Final response expected from Claude

Files changed with exact references; tests and their outputs; measured performance where possible; residual known failures; release gate status; recommended next vertical slice. Update the category tracking matrix and existing docs, avoiding parallel contradictory sources of truth.
