# 11 — Property and market intelligence from authorized non-MLS sources

> **Scope:** Turn the property and market data Neoh is actually authorized to use into source-backed brokerage intelligence (property view, buyer matching, valuation context, market evidence), and take the legacy MLS code out of the product. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01, 03, 06 · **Unblocks:** 13, 14, 20 · **Ledger:** [L11](LEDGER.md#l11) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- `backend/data_coverage.py`, `backend/property_view_api.py`, `backend/opportunity_engine.py`, `backend/opportunity_api.py`, `backend/property_inference.py`
- `backend/avm_client.py`, `backend/apis/market_data.py`, `backend/apis/property_data.py`, `backend/state_market_projection.py`, `backend/data_sources_api.py`
- `backend/harvesters/` (`base.py`, `county_assessor.py` and the state/county harvesters), `backend/harvest_health.py`
- `backend/client_ai_automation.py` (`_property_candidates`, `_property_candidates_bounded`)
- Legacy MLS, to be gated off:
  - backend: `backend/mls_portal.py`, `backend/mls_sync_guard.py`, `backend/mls_health.py`, `backend/mls_licensing.py`, `backend/mls_enrichment.py`, `backend/mls_feed_lock.py`, `backend/state_compliance/routes_mls.py`
  - feeds: `backend/data_integrations/listings_feed.py`, `backend/data_integrations/bridge_listings_feed.py`, `backend/data_integrations/mls_sink.py`, `backend/data_integrations/periodic.py`
  - AI tools and portfolio: `backend/ai_tools_read.py`, `backend/portfolio_api.py`
  - frontend: `oracle-app/src/components/PropertiesTab.jsx`, `oracle-app/src/components/MlsSearch.jsx`
  - tests and migrations: `backend/tests/golden_mls_flow.py`, `backend/db/migrations/0116_mls_search_order_index.sql`
- Frontend: `oracle-app/src/components/PropertiesTab.jsx`, `oracle-app/src/components/PropertyIntelligencePanel.jsx`, `oracle-app/src/components/MarketDataPanels.jsx`, `oracle-app/src/components/OurAITab.jsx`, `oracle-app/src/components/StudioTab.jsx`
- More legacy MLS surface: `backend/brokerage_onboarding.py` (MLS entitlement routes), `backend/component_health.py`, `docs/runbooks/mls-provider-down.md`, `docs/performance-targets.md`, `docs/brokerage-go-live-checklist.md`
- Docs: `docs/data-access-tiers.md`, `docs/privacy-data-map.md`; audit findings [A02 and A08](../neoh-full-platform-source-audit-2026-10-08.md)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **The Properties workspace still offers an MLS sub-tab.** `PropertiesTab.jsx:30-37` (`VIEWS`) defines six sub-views: `address`, `houses`, `listings`, `mls`, `market`, `forms`. `:34` is `{ id: 'mls', label: 'MLS' }`, and `:75` renders `<MlsSearch />` for it (lazy import at `:28`).
- **The MLS browse API is mounted in the app.** `backend/server.py:424-425` includes `mls_portal.router` (prefix `/api/mls`, `mls_portal.py:36`). `mls_portal.py:208` serves `/search` ("Retail MLS browse"), `:328` serves `/listings/{listing_id}` and `:391` serves `/health`.
- **The same router also serves the non-MLS public-records catalog.** `mls_portal.py:867` is `/public-records` and `:1044` is `/public-records/{record_id}`, both under the `/api/mls` prefix. The Houses view calls it (`HouseSelection.jsx:38` builds `/api/mls/public-records?…`), so gating MLS must keep it reachable.
- **The Listings sub-view is the brokerage's own inventory, not MLS.** `ListingsInventory.jsx:111` reads `GET /api/crm/listings`, and `:170` writes `POST /api/crm/listings`.
- **MLS sync jobs still exist in the periodic scheduler.** `data_integrations/periodic.py:540` `_new_listings_task` (RESO via `RESOListingsAggregator`, `:548`) and `:567` `_bridge_listings_task` (Bridge). Both skip with a message when their environment variables are unset (`:555`, `:580`).
- **The AI can read MLS data.** In `ai_tools_read.py`, `_search_listings` (`:402`) and `_get_days_on_market` (`:1022`) query `oracle_mls_listings`. `portfolio_api.py:142` reads it too.
- **The MLS configuration surface is still documented.** `.env.example:139-170` documents `ORACLE_RESO_*` and `ORACLE_BRIDGE_*`. The staging app spec has no MLS credentials (ledger L11-01).
- **MLS data is classified as licensed MLS data** in `privacy_data_map.py:216` (`mls_sync_status`), with retention implications for any deletion decision.
- **Brokerage MLS entitlement routes are still mounted.** `brokerage_onboarding.py:923` serves `GET /api/brokerage/mls/entitlements` and `:940` serves the POST; both use the `mls_feed_entitlements` table.
- **Two pages still advertise or probe MLS.**
  - `OurAITab.jsx:82` probes `/api/mls/health` as the source for its "MLS / IDX" row, and `:570`/`:576` list "MLS / IDX" and "WordPress IDX Plugin" capability rows.
  - `StudioTab.jsx:127` (`loadIdx`) probes `/api/mls/health` too.
- **Health, runbooks and docs still treat MLS as live.**
  - `component_health.py:38` rates an `mls` component "important" and `:171` reports it.
  - `docs/runbooks/mls-provider-down.md` is an MLS runbook.
  - `docs/performance-targets.md:31-33` and `:63` set MLS search targets.
  - `docs/brokerage-go-live-checklist.md:36` (item 8) asks for MLS authorization status.
- **Non-MLS sources in code:**
  - RentCast AVM: `avm_client.py:35` `RENTCAST_BASE`, `:107` `_rentcast_avm`.
  - An ATTOM adapter: `:165` `_attom_avm`.
  - Market data: `apis/market_data.py`.
  - Property data: `apis/property_data.py`.
  - Redfin-derived state market projection: `state_market_projection.py`.
  - About 60 harvester modules under `backend/harvesters/`.
- **A failed property lookup reads as zero matches (audit A02, open).** `client_ai_automation.py:700`, inside `_property_candidates_bounded` (`:680`), catches every exception and returns `[]`.

## 3. Goal

An agent can open any property and see only facts Neoh is authorized to show. Each fact carries its source, its freshness, and any gaps. Buyer matching is backed by stated preferences and source data, and a failed source is never presented as "no matches". Nothing in the product, the AI or the jobs presents MLS search, ingestion or display as a feature (D-2026-10-12-01).

## 4. Engineering tasks

- **T11.01 Gate legacy MLS off.**
  - Remove the `mls` sub-tab from `PropertiesTab.jsx`; keep `address`, `houses`, `listings` (non-MLS), `market` and `forms`.
  - Unmount or 404 the MLS browse routes (`/api/mls/search`, `/api/mls/listings/*`, `/api/mls/health`) and the `routes_mls.py` routes behind one explicit off switch.
  - Stop the RESO and Bridge periodic tasks.
  - Take `_search_listings` and `_get_days_on_market` out of the AI's offered tool set, or point them at non-MLS sources.
  - Unmount the brokerage MLS entitlement routes (`brokerage_onboarding.py:923`, `:940`).
  - Remove the "MLS / IDX" and "WordPress IDX Plugin" rows and the `/api/mls/health` probes from `OurAITab.jsx` and `StudioTab.jsx`.
  - Retire the `mls` health component, or mark it `not_applicable`, so a missing MLS feed never degrades overall health.
  - Mark the MLS runbook, the MLS performance targets and go-live item 8 as legacy. Replace item 8 with a property-sources check.
  - Keep `/public-records` working: move it to a non-MLS prefix with a backward-compatible alias.
  - **Delete no data, migration or table** (owner approval required; L11-01).
- **T11.02 Inventory the authorized sources.** For each source, record identifiers, licence or terms of use, freshness, and the coverage `data_coverage.py` reports:
  - public parcel and assessor records (harvesters);
  - county and state datasets;
  - RentCast and ATTOM valuations;
  - market data (`apis/market_data.py`, `state_market_projection.py`);
  - neighbourhood and walkability data;
  - environmental and hazard data;
  - broker-owned listings and property media.

  Keep property ID, parcel ID, vendor record ID and address match distinct.
- **T11.03 Resolve property identity, with history.** Match on normalized address and unit, parcel number and geographic proximity. Keep transaction and revision history, record evidence conflicts, and support merged records. Fuzzy matches stay candidates until verified.
- **T11.04 Record freshness and permitted use per source.** When a licence expires or a source must be purged, the change reaches comps, matches, caches, search, AI context, tour metadata and public sites.
- **T11.05 Index for real queries.** Cover tenant entitlement, active status, geography, beds/baths, price range, recency, bounding boxes and stable ordering. Benchmark the plans under RLS on realistic data volumes, and keep the existing index fixes.
- **T11.06 Separate facts from inference.** Every recommendation shows its sourced matches, the customer's stated preferences, missing evidence, freshness and uncertainty. No protected-characteristic scoring and no steering.
- **T11.07 Valuation honesty.** Trace comps, heuristic targets and model training. Hard-coded county averages and 70%-rule MAO examples are scaffolding, not a validated AVM (audit A08). Add source confidence, timestamps, a representative holdout, calibration and model cards; show no precision without evidence.
- **T11.08 Property View detail.** Show photos, permitted listing information, parcel and legal status, comps, market cards, source disclaimers, floor plan, tours, investment and repair context, and user actions. Room dimensions appear only if measured; reconstruction labels only if verified. An unavailable source shows its status.
- **T11.09 Finish the property sub-views** (address, houses, listings, market, forms): paging, search, filters, detail, a map where relevant, audit, permissions, export restrictions, updates to the state form library, meaningful empty and error states, and phone layout.
- **T11.10 Connect matching to CRM and showings.** Candidate retrieval returns per-source status ([C6](CONTRACT.md#c6-truth-vocabulary-use-these-words-exactly)), so a verified zero is distinct from `timed_out`, `unavailable` or no entitlement (fixes A02). Action and billable metrics count completed outcomes, not drafts.
- **T11.11 Ingestion discipline.** Deduplicate, sync incrementally, make runs idempotent, keep provenance, quarantine stale sources, check licences, bound quotas and alert on failures. Ingestion must never starve interactive search.
- **T11.12 Citation-aware property questions in Neoh.** Respect user and agent rights, resolve the exact property, state source freshness, never invent seller intent or availability, add caveats, and re-check facts before any outbound use.
- **T11.13 Benchmark search** at p50/p95 with 20 concurrent agents during ingestion. Include addresses with units, mixed locales, expired records, permission removal, a wrong parcel match, conflicting vendor rows and a source outage.

## 5. Failure and adversarial cases

- **F11.01 MLS leaks back.** An MLS UI entry, route, job or AI tool is still reachable after T11.01, or a test still expects MLS results.
- **F11.02 Public records break.** Gating MLS breaks `/public-records` or its existing callers.
- **F11.03 A failed source reads as an empty result.** A source timeout or provider error surfaces as a "checked" zero-match result.
- **F11.04 Wrong parcel.** A fuzzy match attaches the wrong owner or parcel, and a draft or AI answer then states it as fact.
- **F11.05 Revoked source lingers.** A licence is revoked or a source is purged, but the data survives in caches, search, AI context or public sites.
- **F11.06 Inferred value shown as fact.** A heuristic AVM or model output is displayed as a verified value.
- **F11.07 Ingestion starves search.** An ingestion burst pushes interactive search p95 above its target.
- **F11.08 Cross-tenant exposure.** One brokerage's broker-owned listings or notes become visible to another brokerage.

## 6. Acceptance criteria

- **AC11.01 MLS is gone from the product surface; its data is untouched.**
  - No MLS tab, capability row, health probe, route (including `/api/brokerage/mls/entitlements`), scheduled job, health component or AI tool is reachable or offered.
  - The `oracle_mls_listings` row count and the migrations are unchanged.
  - Evidence:
    - a Playwright check of the Properties sub-tabs and the Our AI capability rows;
    - a route test asserting 404 or disabled for `/api/mls/search|listings|health` and the entitlement routes;
    - a periodic-task registry test;
    - a health snapshot test;
    - an AI tool-offer snapshot;
    - the row count before and after.
  - Covers: T11.01, F11.01.
- **AC11.02 Public records keep working.** `/public-records` and `/public-records/{id}` return the same results at the new path and through the old alias. Evidence: contract test comparing both paths. Covers: T11.01, F11.02.
- **AC11.03 Lookup failure is never a verified zero.** Property-candidate lookup returns `checked` / `timed_out` / `unavailable` per source, and a timeout makes the reconciliation `degraded`. Evidence: tests for a verified zero, a timeout, a provider error, valid matches, stale results and an idempotent retry. Covers: T11.10, F11.03.
- **AC11.04 Facts are sourced; uncertain matches are labelled.** Every displayed fact links to a source record with a timestamp. Unverified identity matches are labelled "possible match", never stated as fact. Evidence: component test + an API contract test on the property detail payload. Covers: T11.02, T11.03, T11.06, T11.08, F11.04.
- **AC11.05 Revocation propagates within one sync cycle.** After a source is revoked, the data disappears from search, matching, AI context and caches within one sync cycle. Evidence: an integration test that revokes a source and checks every surface. Covers: T11.04, F11.05.
- **AC11.06 Estimates are labelled as estimates.** Valuations show source, confidence and as-of date. A heuristic estimate is labelled "estimate" and never shown as a single precise figure. Evidence: snapshot test plus a model card in docs. Covers: T11.07, F11.06.
- **AC11.07 Search speed is measured, not assumed.** Search p50/p95 is measured and recorded with its conditions, and p95 under ingestion stays within the target in `docs/performance-targets.md` (target, not measured, until this runs). Evidence: committed benchmark command and result. Covers: T11.05, T11.11, T11.13, F11.07.
- **AC11.08 No cross-brokerage access.** Cross-brokerage access to property notes and broker-owned listings is denied under the real application role. Evidence: real-PostgreSQL RLS test. Covers: F11.08, T11.09.
- **AC11.09 Property questions are cited.** Neoh's property answers cite their sources and refuse to state availability or seller intent. Evidence: evaluation set of at least 20 questions with expected citations and refusals, run and scored. Covers: T11.12.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS, IDX and RESO are out of scope** (D-2026-10-12-01). Do not build, re-enable or advertise MLS search, ingestion, display or IDX publishing. If the owner reverses the decision, it needs a new decision entry and lawful provider access first.
- **No deletion of `oracle_mls_listings` data, MLS migrations or MLS modules** without owner approval. Gate, don't delete. Record the retention question in L11-01.
- **Use only sources whose terms permit the use.** Never scrape member portals or listing sites. Web scraping stays off by default (`SPATIAL_ALLOW_WEB_SCRAPE`).
- **Fair housing:** no scoring or matching on protected characteristics; no steering.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the MLS gating change with tests;
  - the source inventory table (`docs/property-sources.md`, proposed);
  - the per-source status contract;
  - the property detail provenance contract;
  - the search benchmark result.
- **First slice:** T11.01 + AC11.01 + AC11.02, which gates MLS off without breaking public records. Then T11.10 + AC11.03, which closes A02.
- **Ledger:** update L11-01, then add L11-02 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
