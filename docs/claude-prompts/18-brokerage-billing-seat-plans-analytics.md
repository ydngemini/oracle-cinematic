# 18 — Brokerage billing, seat entitlements, usage accounting and analytics

> **Scope:** Give one brokerage subscription 20 working agent seats plus the owner, count usage only from durable completed events, cap provider spend, and give the owner honest cost and performance reporting, without changing any price the owner has not approved. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03, 17 · **Unblocks:** 19, 20 · **Ledger:** [L18](LEDGER.md#l18) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Billing: `backend/billing.py`, `backend/billing_usage.py`, `backend/db/migrations/0067_speed_to_lead_and_usage_metering.sql`
- Seats and team: `backend/brokerage_onboarding.py` (`create_invitations`, `suspend_member`, `reinstate_member`)
- Usage sources: `backend/speed_to_lead.py` (`_speed_to_lead_job`), `backend/outcome_memory.py`, `backend/operator_api.py` (`pilot_metrics`)
- Frontend: `oracle-app/src/components/BillingOverlay.jsx`, `oracle-app/src/components/BillingUsagePanel.jsx`, `oracle-app/src/components/BrokerageSetupPanel.jsx`
- Docs: `docs/pilot-metrics.md`, `docs/brokerage-go-live-checklist.md`
- Audit finding [A05](../neoh-full-platform-source-audit-2026-10-08.md); ledger rows L18-01 (plan model) and L18-02 (A05); decision D-2026-10-12-02 in [LEDGER.md](LEDGER.md#owner-decisions)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Checkout sells one price at quantity 1; there is no seat model.** `billing.py:44` `STRIPE_PRICE_ID`; `:266` `line_items = [{"price": STRIPE_PRICE_ID, "quantity": 1}]` in `create_checkout_session` (`:223`). Ledger L18-01.
- **The code says so itself.** `billing_usage.py:4-6` (module docstring): "there is no seat count to grow and no usage line to attach".
- **Pricing is a hypothesis, not a decision.** D-2026-10-12-02: about $1,999/month per 20-agent brokerage, about $1,499 for a pilot, undecided. The amount lives in Stripe, not in code (L18-01).
- **The billing screen hard-codes a solo plan.** `BillingOverlay.jsx:18` `PLAN_PRICE_USD = 199`, `:28` "One Named User", `:44` "Neoh Solo Premium", `:289` "Extra named users are $39/month".
- **Another code comment assumes a different price.** `billing_usage.py:66` refers to "a flat $299 plan".
- **Invitations check membership, not seats.** `brokerage_onboarding.py:513` `create_invitations` skips existing members (`:535-541`); the file has no seat limit (`grep -n -i seat` finds nothing).
- **The server gates spend and contact on subscription status.** `billing.py:125` `require_active_subscription` refuses outbound and spend routes for a closing tenant or a non-entitled status; `:113` `_ENTITLED_STATUSES` includes `past_due`. Enforcement is on except in development (`billing_enforced`, `:116`).
- **Stripe webhooks are deduplicated by event id.** `billing.py:526` inserts into `stripe_webhook_events` with `ON CONFLICT (event_id) DO NOTHING`, inside `stripe_webhook` (`:458`).
- **Usage is counted when outreach is only staged (A05).** In `_speed_to_lead_job` (`speed_to_lead.py:244`), the staged event is recorded at `:379-382`, then `record_usage(metric="lead_engaged")` runs at `:387-389`; the result still says `"requires_approval": True` (`:405`).
- **That is the one metric wired to Stripe.** `billing_usage.py:76` `_STRIPE_REPORTED = frozenset({"lead_engaged"})`; the full metric list is `METRICS` (`:56`).
- **Seven pilot metrics are defined, with an honesty rule for revenue.** `docs/pilot-metrics.md:12-18`; the operator endpoint is `operator_api.py:1580` `pilot_metrics`; attribution is last-touch (`pilot-metrics.md:22-24`, `outcome_memory.py:209` `_last_accepted_decision`).

## 3. Goal

The owner activates one brokerage plan, invites 20 agents, and sees what they pay, what each channel costs and what Neoh achieved, with estimates labelled as estimates. Seats are enforced on the server. No charge happens twice, no usage is billed for work that was never approved or delivered, and no price changes without the owner.

## 4. Engineering tasks

- **T18.01 Trace the billing path.** Stripe product and price configuration, customer-to-tenant mapping, statuses (active, trialing, past_due, canceled), invitation limits, the billing overlay, provider and AI credits, the usage ledger, and webhook dedup and ordering.
  - Verify whether any route lets an agent, owner or expired plan bypass the server entitlement check.
- **T18.02 Draft the brokerage plan contract for the owner.** Seat count (owner included or not), included provider features, optional teams, billing period, add-ons, usage meter names, spend caps, trial, proration, upgrade, downgrade and cancellation. Present it as an owner question; create or change no Stripe price without the owner's commercial approval (D-2026-10-12-02).
- **T18.03 Make the backend the entitlement authority.** The UI reads the active plan, seat count and price from the backend. Remove the hard-coded $199 Solo Premium, "One Named User" and $39 copy from `BillingOverlay.jsx`, and fix the $299 comment.
- **T18.04 Enforce 20 seats transactionally.** Check the seat count on invitation acceptance and membership activation in one transaction, so two concurrent acceptances cannot exceed it. Define suspension and deactivation so they free or hold a seat without an accidental extra charge.
  - Design seat quantity on the single-price checkout (L18-01 next task), behind the owner's decision.
- **T18.05 Reconcile Stripe events in order.** Verify signatures, handle duplicates and out-of-order events, and reconcile subscription state chronologically. On payment failure keep read and export plus a human-supported recovery path, and block new billable outgoing actions as the contract says.
- **T18.06 Count usage at the contracted stage (A05).** Separate requested, drafted, staged, approved, provider accepted, delivered, responded and closed (C6). Move `lead_engaged` off the staged step, or mark it non-billable until approval and delivery, and correct any existing events if they were ever used for invoicing.
- **T18.07 Add cost guardrails.** Per tenant and per agent: model tokens, real-time voice seconds, carrier minutes, SMS, email, property data provider requests (see 11), GPU reconstruction, ads and web publishing. Each has a soft warning, hard stop, kill switch, forecast and owner-approval threshold, reconciled against provider data.
- **T18.08 Build the owner billing dashboard.** Seats, active agents, subscription and paid state, channel spend, usage by feature, failed and pending job cost, provider connection status, budgets and forecasts. Label estimated vs invoiced values; agents see only their own permitted figures.
- **T18.09 Implement the seven pilot metrics** from `docs/pilot-metrics.md`, counting completed actions only, and keep the last-touch caveat. Never claim causality.
- **T18.10 Model cost per outcome.** Per-provider cost per successful workflow and marginal inference cost. Track fixed hosting and data licensing separately. Never mix gross deal value with brokerage commission or Neoh subscription revenue. Support credits, disputes and adjustments in a ledger.
- **T18.11 Run the billing failure suite** with synthetic data: seat races, concurrent acceptance, duplicate callbacks, trial expiry, Stripe downtime, price mismatch, revoked card, partial refund, spend cap, cancelled and reinstated brokerage, and legal export.
- **T18.12 Prepare tax, accounting and legal statements for review.** Counsel and finance verify them. Implement no regulated financial decision and promise no ROI from AI heuristics.
- **T18.13 Build the owner activation flow.** The owner activates the plan and invites 20 agents, with an accurate description of included features and an understandable cost breakdown.

## 5. Failure and adversarial cases

- **F18.01 Seat race.** Two agents accept the last seat at once and both get in.
- **F18.02 Duplicate or out-of-order webhook.** A replayed or late Stripe event charges twice or reverts a newer status.
- **F18.03 Billed for staged work.** A rejected, expired, duplicate or never-delivered outreach counts as billable engagement.
- **F18.04 Price drift.** The UI shows a price that differs from the Stripe price the backend uses.
- **F18.05 Agent edits pricing.** An agent changes plan, price, budget or another agent's caps.
- **F18.06 Spend cap ignored.** A runaway conversation, call or GPU job passes the hard stop.
- **F18.07 Payment failure locks data.** A failed payment blocks read or export, or still allows new billable sends.
- **F18.08 Overclaimed value.** A dashboard presents revenue influenced as revenue caused, or an estimate as an invoice.

## 6. Acceptance criteria

- **AC18.01 The plan shown is the plan billed.** The UI reads plan, seats and price from the backend; no hard-coded price remains. Evidence: component test with two backend plans; grep shows no price literal in `BillingOverlay.jsx`. Covers: T18.01, T18.03, F18.04.
- **AC18.02 Exactly 20 seats can be used.** The 21st activation is refused, including under concurrency. Evidence: real-PostgreSQL concurrency test, `backend/tests/test_seat_entitlements.py` (proposed). Covers: T18.04, F18.01.
- **AC18.03 No duplicate or reverted charge.** Evidence: webhook tests for replay, reorder and concurrent duplicates against Stripe test mode. Covers: T18.05, F18.02.
- **AC18.04 Usage links to durable completed events.** Every billable usage row references an approved and delivered event. Evidence: tests for rejected, expired, duplicate and undelivered outreach, plus a reconciliation query. Covers: T18.06, F18.03.
- **AC18.05 Agents cannot change pricing or budgets.** Evidence: role tests on every billing and budget route. Covers: T18.08, F18.05.
- **AC18.06 Spend caps stop work.** Evidence: failure-injection test per capped channel showing warn, hard stop and kill switch. Covers: T18.07, F18.06.
- **AC18.07 Payment failure degrades safely.** Read and export still work; new billable actions are blocked. Evidence: test with a past_due and a canceled subscription. Covers: T18.05, F18.07.
- **AC18.08 Owner reporting is labelled honestly.** Estimates, invoices and attribution are labelled, and pilot metrics count completed actions only. Evidence: API contract test plus a dashboard snapshot. Covers: T18.08, T18.09, T18.10, F18.08.
- **AC18.09 The billing failure suite runs green.** Evidence: committed suite command, exit code and counts (C9.2). Covers: T18.11.
- **AC18.10 The owner approves the plan contract before any Stripe change.** Evidence: owner decision recorded in the ledger. Covers: T18.02, T18.12.
- **AC18.11 The owner can activate and invite 20 agents in one flow.** Evidence: Playwright test on desktop and phone. Covers: T18.13.
- **AC18.12 Live Stripe is checked in production before money moves.** Evidence: production configuration check in 19, signed by the owner. Covers: T18.01, T18.05.

## 7. Out of scope, owner decisions, category-specific rules

- **No price change without owner approval** (D-2026-10-12-02). Use Stripe test mode only; never run a checkout or charge with a live key.
- **MLS data vendor costs are out of scope** (D-2026-10-12-01). Do not meter, budget or price MLS feeds.
- **Tax, accounting and legal wording** needs counsel or finance review; Claude drafts, humans sign.
- **Attribution is association, not causation.** Keep the last-touch caveat wherever revenue influenced appears.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the plan contract draft for the owner (`docs/brokerage-plan-contract.md`, proposed);
  - seat entitlement enforcement with tests;
  - the usage lifecycle fix for A05;
  - spend caps and the owner dashboard;
  - the billing failure suite.
- **First slice:** T18.06 + AC18.04, which closes A05 without a price change. Then T18.04 + AC18.02, which enforces seats.
- **Ledger:** update L18-01, then add L18-02 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
