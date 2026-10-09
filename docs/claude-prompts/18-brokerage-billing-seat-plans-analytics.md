# Claude implementation prompt 18: 20-agent billing, seat entitlements, usage accounting and brokerage analytics

> Repository: `ydngemini/oracle-cinematic`. Production objective: all features and authorized pages usable by **one brokerage and 20 agents**, with separate owner/admin capabilities.
> These are engineering specifications, not claims of implementation, provider availability, cloud access, source-code certification or successful tests.

## Role and initial inspection

Claude, act as principal/staff architect, security reviewer and hands-on implementer. Read `CLAUDE.md`, category index `README.md`, current code, tests, migrations, infrastructure and older audits. Record the current SHA and real source citations. Do not guess existing service names, modify unrelated code, or perform unapproved external/production transactions. Work in atomic, reviewable vertical slices with tests.

**Inspect likely files first:** backend/billing.py; backend/billing_usage.py; backend/brokerage_onboarding.py; backend/outcome_memory.py; backend/speed_to_lead.py; oracle-app/src/components/BillingOverlay.jsx; oracle-app/src/components/BillingUsagePanel.jsx; oracle-app/src/components/BrokerageSetupPanel.jsx; docs/pilot-metrics.md; docs/brokerage-go-live-checklist.md

## Extensive work specification

### Goal
One brokerage subscription that correctly accommodates 20 participating agent seats plus clear owner status, provider spend and fair usage with trusted financial/operational reporting. Replace the current Solo Premium “One Named User” UI/plan mismatch, but never silently change live Stripe pricing or charge customers.

### Steps
1. Trace Stripe product/price configuration, customer/tenant mapping, active/trial/past_due/canceled status, seat invitation limits, billing overlay, provider and AI credits, billing ledger and webhook event dedup/ordering. Confirm no global premium feature bypass for agent/owner/expired plan.
2. Propose explicit brokerage plan contract: seat count (owner included/excluded by verified business decision), allowed provider features, optional agents/teams, billing period, add-ons, usage meter names, spend caps, permitted trial, proration/upgrade/downgrade and cancellation. Obtain owner's commercial authorization before creating/changing Stripe prices.
3. Backend is entitlement authority; UI must request active plan/seat price from backend, never hardcode $199 Solo Premium if billed differently. Seats enforced transactionally on invite acceptance and membership activation; suspension/deactivation semantics defined without accidental extra charges.
4. Use durable Stripe webhook verification, chronological reconciliation, duplicate/out-of-order handling and subscription state changes. On payment failure, preserve safe read/export and human-supported recovery path while blocking new billable outgoing actions per contract.
5. Distinguish requested/drafted/staged/approved/provider accepted/delivered/responded/closed. Correct existing lead_engaged events created when outreach only staged, especially if ever used for usage-based invoicing. Billing must match written policy.
6. Cost guardrails: per-tenant and per-agent model tokens, real-time voice seconds, carrier call minutes, SMS, email, MLS/data vendor requests, GPU splat reconstruction, ads and web publishing. Set soft warn, hard stop, kill switch, forecast and owner approval thresholds; provider data required for accurate reconciliation.
7. Build owner dashboard: current seats, active agents, subscription, paid/unpaid state, channel spend, usage by feature, failed/pending job cost, provider connection status, allocations/budgets, forecasts and clearly labeled estimated versus invoiced values. Agents see only permitted personal information.
8. Brokerage performance metrics: seven pilot measures in docs/pilot-metrics.md, including weekly active agents, Neoh conversations and *completed* actions, communications initiated, matches acted on, measurable time saved and revenue influenced. Retain last-touch association caveat; do not overclaim causality.
9. Financial model: per-provider cost per successful workflow and marginal inference cost; track fixed hosting and data licensing separately; do not mix gross deal value with brokerage commission or Neoh subscription revenue. Support credits, disputes and adjustment ledgers.
10. Run synthetic seat races, concurrent acceptance, duplicate billing callbacks, trial expiration, Stripe downtime, price mismatch, revoked card, partial refund, provider spend cap, canceled/reinstated brokerage and legal export tests.
11. Verify tax/accounting/legal statements with counsel/finance; do not implement regulated financial decisions or guarantee ROI from AI heuristics.
12. Add onboarding experience for owner to activate plan and invite 20 agents with supported full-feature capability description and understandable cost breakdown.

### Acceptance
Correct plan and billing shown, 20 seats usable, no duplicate/unapproved charge, all usage links to durable events, owner has transparent budgets, agents cannot edit owner pricing, spend caps tested, and live provider/Stripe configuration verified in production before money moves.

## Required implementation discipline

1. **Audit and plan with evidence:** construct source/route/API/dependency map, existing verified behavior, exact gaps, alternate design options, permission and failure analysis. A source file's existence does not establish product availability.
2. **Change real code:** integrate frontend, API, database and worker/provider as needed. Use additive migrations, backward compatibility, durability, bounded network/CPU/model/GPU cost and appropriate observability. Preserve existing React/Vite + FastAPI/Postgres/PlayCanvas architecture where useful.
3. **Prove cross-cutting security:** tenant, assigned agent, team share, owner and platform admin roles in DB RLS, APIs, tools, cache, search, media, external connectors and queue execution. Approval and provider receipts required for side effects.
4. **Test:** unit/schema/property, real PostgreSQL RLS, frontend components, Playwright browser/mobile, concurrency, mocked failure-injection and approved provider sandbox/live cases. Never run unsolicited customer communications, advertising purchases, production deployment or payment.
5. **Do not overclaim:** classify simulated vs actual provider tests, design targets vs measurements, model prediction vs verified fact, staged vs delivered, and staging CI vs production. Include source refs, p50/p95, errors, rollback, migration coverage, signed owner/legal/provider decisions.

## Your output after each implementation slice

Current baseline and affected code paths; concrete changes and migrations; tests/measurements actually run; verified role and customer journey outcomes; remaining blockers, current release verdict, linked commit(s), and the next highest-dependency implementation task. Update the master ALL-PAGES-MATRIX and category backlog.
