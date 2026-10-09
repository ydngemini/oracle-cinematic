# Claude implementation prompt 11: Property intelligence, MLS/IDX data, valuations and market evidence

> Repository `ydngemini/oracle-cinematic`; one brokerage, **20 authorized agents** and an explicitly modeled owner role; *every* entitled web page and advertised workflow is in the final release scope.
> This is an engineering implementation assignment. It does not establish any asset, service, API connection, third-party license, production deployment or benchmark as shipped.

## Your role and preflight

Act as principal engineer, domain specialist and security reviewer. Read root `CLAUDE.md`, this folder's `README.md` and relevant existing audit/implementation specs. Pin current commit and inspect true source paths/line references. Implement focused code changes and passing tests, not just aspirational prose. Preserve existing FastAPI/Postgres/React/Vite, tenant RLS, permission/approval mechanisms, provider contracts and browser-only capture.

**Likely sources to inspect:** backend/opportunity_engine.py; backend/data_coverage.py; backend/mls_portal.py; backend/recon_quality.py; backend/client_ai_automation.py; oracle-app/src/components/PropertiesTab.jsx; oracle-app/src/components/PropertyIntelligencePanel.jsx; oracle-app/src/components/MlsSearch.jsx; oracle-app/src/components/MarketDataPanels.jsx

## Implementation work

### Goal
Turn diverse property data into source-backed brokerage intelligence without inventing listing availability, ownership, price, legal right, ROI, valuation or geometry. Provide all six property workspace subviews and buyer/seller matching.

### Engineering specification
1. Inventory authoritative source tables and API adapters: public parcel records, MLS/RESO listings, licensed subscriptions, sales/transaction records, tax, deeds, neighborhoods, government reference, environmental/hazard datasets, property media and broker-owned listings. Distinguish property ID, parcel ID, MLS listing ID, address match and vendor source record ID.
2. Build property identity resolution and temporal lineage: normalized address and unit, parcel numbers, geospatial proximity, MLS IDs, transaction dates, listing revisions, evidence conflicts and merged property records. Don't treat fuzzy matching as canonical truth without verification.
3. Record freshness and legal usage per source. Enforce per-brokerage MLS licensing and IDX display rules before query or content exposure; legal expiration/purge must propagate to derived comps, matches, caches, search, AI context, tour metadata and public sites.
4. Strengthen query indexes for tenant/license entitlement, active listing, geography, beds/baths, price range, recency, bounding boxes and correct ordering; benchmark live RLS/entitlement-aware plans with realistic high-cardinality data. Existing index migration fixes should be preserved.
5. Separate factual attributes from inferred suitability and model predictions. Every recommendation should show sourced matches, declared customer preferences, missing evidence, freshness and uncertainty. Avoid protected-characteristic scoring and housing steering.
6. Property valuation: trace current comps, heuristic targets and model training; hardcoded county averages/70%-MAO synthetic examples are training scaffolds, not validated AVMs. Add licensed sale comparables, condition/source confidence, timestamp, geographically representative holdout data, calibration and model cards; do not present precision without evidence.
7. Implement trustworthy Property View detail: photos, licensed listings, parcel/legal status, comps, market cards, source disclaimers, floor plan, tours, investment/repair context and user actions. Only show measured room dimensions and verified reconstruction labels. Externally unavailable sources get explicit status.
8. Complete Houses / Listings / MLS / Market / Forms subpages: paging, search, filters, property detail, map where relevant, audit, permission, export restrictions, state form library updates, meaningful empty/error states, responsive UI.
9. Connect buyer/property matching to CRM and showing workflows. Candidate retrieval must return verified zero results distinctly from timeout/no entitlement; action and billable metrics are based on completed outcomes, not drafts.
10. Build source ingestion with dedup, incremental sync, idempotency, provenance, stale-source quarantine, legal license checks, bounding quotas and alerting. Prevent ingest from monopolizing interactive search.
11. Add citation-aware Neoh property question answering with user/agent rights, exact property subject resolution, source freshness, no fabricated seller intent/availability, caveats and recheck-before-outbound policy.
12. Benchmark search p50/p95 under concurrent agents and MLS ingest; test mixed locales, addresses with unit numbers, expired listings, MLS permission removal, incorrect parcel association, conflicting vendor rows and unavailability.

### Definition of done
All property screens usable with authorized source data, source completeness explicit, source-permission revocation honored, buyer matching evidence-backed, benchmarked query plans, no invented factual attributes or third-party content reuse outside its license.

## Universal constraints, tests and deliverables

- Verify every associated page, workflow, API and worker; no false capability badges. Customer-required credentials/licensing may show a complete, truthful setup-required flow; genuinely missing advertised functionality must be implemented before calling an all-features release finished.
- Access rights: authorized assigned/shared agent records, team lead only in delegated scope, brokerage owner administration separate from platform admin; revalidate before external side effects and respect consent/PII.
- Evidence quality: distinguish source-backed facts, inferred predictions, provider timeouts, drafts, accepted jobs, delivered messages and actual success. Document source timestamps and failure states.
- Test units, schema/contracts, real PostgreSQL/RLS and migration chain, React UI/Playwright, concurrency and device scenarios as applicable. Use sandbox accounts for providers and synthetic data; never send to actual clients, spend or deploy live without authorized human approval.
- Measure actual latency/memory/cost, specify test conditions and p50/p95; mocked provider timings are not production results.
- Report source-linked baseline and gaps, architecture diagram or sequence, implementation plan with P0/P1/P2, file changes and migrations, tests actually executed, failure/recovery proofs, human/external blockers and next dependent slice. Keep category acceptance ledger updated; never claim “fully operational” from a mock alone.
