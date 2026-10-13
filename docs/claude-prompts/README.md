# Neoh: 20 category engineering prompts

> **What this is:** the master index for building Neoh, the AI operating system for a real-estate brokerage, on `ydngemini/oracle-cinematic`. The target release is **one brokerage with 20 agent seats**, plus an explicitly modelled broker owner and platform operator, with **every** entitled page and promised capability working.
>
> The prompts are engineering assignments. They do not claim that a feature is implemented, a provider account exists, a licence is signed, or anything is certified.

## Start here

1. **Read, in this order:** root `CLAUDE.md` → this README → [CONTRACT.md](CONTRACT.md) (binding rules for every category) → [LEDGER.md](LEDGER.md) (the only place status lives).
2. **Pick the category:**
   - **No category assigned:** begin with **[02 — all pages and mobile UI](02-all-pages-mobile-ui.md)**. Build the route, component, API and role matrix (`ALL-PAGES-MATRIX.md`, proposed). At the same time, do a **read-only** review of [03 tenancy](03-brokerage-tenancy-hybrid-access.md), [17 security and privacy](17-security-privacy-compliance-data-lifecycle.md) and [18 billing and seats](18-brokerage-billing-seat-plans-analytics.md). Then implement the first safe P0 slice with tests.
   - **Task already given:** use the matching category prompt from the table below. If your task touches several categories, take the one whose ledger rows it changes most.
   - **Next category after that:** choose the highest-dependency category in the [ledger summary](LEDGER.md#category-summary) whose prerequisites are met and whose rollup is weakest.
     - "Depends on" means "uses that category's outputs". A prerequisite is met when the rows you rely on are at least `source_verified`. It does not have to be finished.
     - The start rule above takes precedence: 02 builds the route inventory it needs from 01 itself.
3. **Work the session to the contract:** one category per session ([C1](CONTRACT.md#c1-session-protocol)). End by updating the ledger ([C10](CONTRACT.md#c10-ledger-update-rule)) and reporting in the [C11](CONTRACT.md#c11-report-format-end-of-every-slice) format.
4. **Never without owner approval:** deploy to production, purchase services, contact real customers, or push unrelated commits. **Pushing to `main` deploys staging** ([C4](CONTRACT.md#c4-external-actions-and-approvals)).

## Owner decisions in force

These bind every category. The full register, with sources, is in [LEDGER.md § Owner decisions](LEDGER.md#owner-decisions).

- **D-2026-10-12-01: MLS is out of scope.** No MLS/IDX/RESO search, ingestion or display. The legacy MLS code is gated off by [11](11-property-market-intelligence.md) T11.01, and no MLS data is deleted without owner approval.
- **D-2026-10-08-01: Neoh Space is 100% browser-based.** The four Neoh Space workstreams are approved (D-2026-10-08-02).
- **D-2026-10-12-02: pricing is a hypothesis,** about $1,999/month per 20-agent brokerage. It is not a decision.

## The 20 categories

Each prompt has the same eight sections (read first, current verified state, goal, tasks, failure cases, acceptance criteria, scope and decisions, deliverables), plus a meta line giving its dependencies and its ledger row. In §2, a bare file name such as `PropertiesTab.jsx:34` refers to the full path listed in that prompt's §1. Line numbers are pinned to the SHA in the §2 heading, and the symbol named next to them survives drift.

| # | Prompt | Deliverable in one line | Depends on |
|---|---|---|---|
| 01 | [Repository source audit](01-repository-source-audit.md) | Reproducible inventory, call graph and issue ledger at a pinned SHA | — |
| 02 | [All pages, routes, accessibility, mobile](02-all-pages-mobile-ui.md) | Every authorized page discoverable, working and tested on desktop and phone | 01 |
| 03 | [Brokerage tenancy and hybrid access](03-brokerage-tenancy-hybrid-access.md) | One tenant, 20 agents, assignments, shares, team oversight, owner console, real RLS | 01 |
| 04 | [CRM, contacts, leads, relationships](04-crm-contact-leads-relationships.md) | Canonical customer identity, routing, consent and record lifecycle | 01, 03 |
| 05 | [Semantic intent](05-semantic-language-intent.md) | Goals, references, ambiguity and constraints understood beyond keywords | 04 |
| 06 | [Evidence retrieval and memory](06-evidence-retrieval-memory.md) | Authorized sources, provenance, temporal validity, task-specific context | 03, 04, 05 |
| 07 | [AI gateway and governed tools](07-ai-gateway-model-tool-governance.md) | Typed capabilities, budgets, policy validation, real receipts | 03, 05, 06 |
| 08 | [Durable missions and workflows](08-durable-missions-workflow-orchestration.md) | Executable plan graphs with approvals, retries and verified outcomes | 03, 05, 06, 07 |
| 09 | [Real-time voice](09-realtime-voice-speech-latency.md) | Streaming voice, barge-in, grounded calls, consented notes | 06, 07 |
| 10 | [Communications and calendars](10-communications-calendar-provider-integrations.md) | Email, SMS, calendars and showings with consent and delivery receipts | 03, 04, 07, 08, 09 |
| 11 | [Property and market intelligence (non-MLS)](11-property-market-intelligence.md) | Source-backed property and market data from authorized sources; legacy MLS gated off | 01, 03, 06 |
| 12 | [Neoh Space capture, 3D, tours](12-neoh-space-browser-capture-reconstruction.md) | The four browser-only spatial workstreams, proven on real houses and real phones | 03 |
| 13 | [Deals, documents, compliance](13-transactions-contract-vault-compliance.md) | Transactions, milestones, contract vault, approvals, truthful financial status | 03, 04, 08, 10, 11 |
| 14 | [Sites, social, ads, client portal](14-websites-social-ads-growth-client-portal.md) | Promised publishing workflows finished; nothing disconnected labelled live | 02, 07, 08, 10, 11 |
| 15 | [Neoh mascot animation](15-neoh-avatar-skeletal-character-animation.md) | Rig and behaviour controller over the SVG fallback, with reduced motion | 02 |
| 16 | [Performance, indexes, cache](16-performance-indexes-mobile-cache.md) | Measured mobile speed, matched query plans, safe caching | 01 |
| 17 | [Security, privacy, data lifecycle](17-security-privacy-compliance-data-lifecycle.md) | Cross-agent RLS, PII encryption, consent, retention, incident readiness | 01, 03 |
| 18 | [Billing, seats, analytics](18-brokerage-billing-seat-plans-analytics.md) | 20-seat entitlements, usage budgets, honest pilot ROI | 03, 17 |
| 19 | [Production operations](19-infrastructure-deploy-observability-recovery.md) | Production environment, secrets, backups, rollback, alerts, realistic load | 17, 18 |
| 20 | [20-agent launch certification](20-twenty-agent-full-feature-launch-e2e-evaluation.md) | All-pages matrix, end-to-end role, provider and device tests, launch gates | 01–19 |

## Build order

The order is set by the dependencies above, and grouped by priority:

1. **P0 release truth:** 01 source audit + 02 all-pages matrix. Separate what is implemented, partly implemented, configured but unavailable, and not built.
2. **P0 trust and permissions:** 17 privacy and security + 03 brokerage access + 04 CRM identity. Prove the real Postgres roles and policies, and the authorization of every side effect, before expanding retrieval or sharing.
3. **P0 business and operations:** 18 seats and billing + 19 production infrastructure. A human operator configures Stripe, legal documents, providers and support. Sessions never create accounts or charge cards.
4. **P1 shared intelligence:** 05 intent, 06 evidence and memory, 07 gateway and tools, 08 missions. These four share IDs, facts, consent, permission revisions and the [C6](CONTRACT.md#c6-truth-vocabulary-use-these-words-exactly) status vocabulary. Prove one real request end to end, from intent to receipt.
5. **P1 revenue workflows:** follow the owner's sequence:
   - 09 AI calls → 10 SMS → 10 email → follow-ups → long-term relationships;
   - alongside those, 11 property data, 13 transactions and 14 sites and marketing.

   Build vertical slices that span prompts, not isolated integrations.
6. **P1/P2 experience:** 16 performance throughout; 12 Neoh Space real-house proof; 15 mascot with graceful fallbacks, tested in real browsers and on real devices.
7. **Release gate:** 20 closes every promised page and workflow against the ledger and the production, legal and provider sign-offs. Ship in iterations, not one giant merge.

## Existing specs and evidence

Use these; don't replace them. Each is dated evidence with stated limits, not certification:

- [Platform source audit (2026-10-08)](../neoh-full-platform-source-audit-2026-10-08.md): findings A01–A10. Their status at HEAD is tracked in the [ledger](LEDGER.md#audit-cross-reference).
- [Neoh Space browser-only specification](../neoh-space-web-capture-claude.md): the primary spec for category 12.
- [Neoh avatar / Rive handoff](../neoh-avatar-rive-spec.md): the current SVG-to-Rive state. A renderer stub is not a delivered rig.
- [Brokerage go-live checklist](../brokerage-go-live-checklist.md) and [first brokerage rollout](../first-brokerage-launch.md): operational sequences.
- [Security launch gate](../security-launch-gate.json), [privacy data map](../privacy-data-map.md), [capacity plan](../capacity-plan.md), [launch readiness results](../launch-readiness/README.md): older snapshots. Re-check against current production evidence before relying on them.

## Verifying a page or workflow

Every page, new or existing, is validated for: role, route, loading, empty, degraded and failure states, a genuine outcome, phone layout and performance ([C9](CONTRACT.md#c9-testing-and-measurement)). A provider-dependent "green" requires a real authorized integration, or an approved test whose production status is checked independently.
