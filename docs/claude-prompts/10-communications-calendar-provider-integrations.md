# Claude implementation prompt 10: Communications, calendars, showings, delivery and provider adapters

> Repository: `ydngemini/oracle-cinematic` shipping FastAPI + React/Vite. Goal: full-feature browser SaaS, one brokerage and **20 agents**, with a separately accounted owner, not a cut-down MVP.
> This is a specification and task prompt, NOT evidence of completed code, production deployment, provider licensing or tested results.

## Claude: implementation assignment

Act as principal architect and hands-on engineering owner. Read `CLAUDE.md`, `README.md` in this prompt folder, the existing platform audit, and the actual current source. Pin main SHA and inspect path:line, tests, migrations, configurations and provider state before coding. Prioritize verifiable full vertical slices rather than replacing entire systems with unproven frameworks.

**Inspect first:** backend/telephony_api.py; backend/voice_provider.py; backend/messaging_data.py; backend/command_providers.py; backend/ai_tools_gated.py; backend/speed_to_lead.py; backend/lead_routing_api.py; oracle-app/src/components/CommsTab.jsx; oracle-app/src/components/CommsComposer.jsx; oracle-app/src/components/ShowingLogger.jsx; oracle-app/src/components/ProviderDeliveryPage.jsx

## Work program

### Goal
Complete, correctly attributed email, SMS, phone, appointment and showing workflows for authorized agents. Provider-specific acceptance and final customer-visible status must be separate; consent, opt-out and carrier regulations are nonnegotiable.

### Steps
1. Audit every provider integration, external callback, signature verifier, sender/phone ownership, token encryption, registration and connected-provider health. Keep carrier-neutral abstractions; test isolation with same platform-level provider account.
2. Implement consistent communication event lifecycle: requested, drafted, staged, owner/human approved, provider-accepted, in-transit, delivered/connected, replied/completed, bounced/failed, delivery-unknown, canceled and expired. Design idempotent mapping for out-of-order, duplicated callbacks. Don't label “delivered” from a 200 send request.
3. Audit incoming voice routing to correct brokerage and assigned/responsible agent with explicit failovers, business-hours rules, tenant-scoped preferences, verified inbound number, agent suspension and escalation. Ensure no cross-tenant caller ID/number collision.
4. Harden SMS: verified number, A2P/10DLC approval where required, opt-out keywords including STOP, quiet hours, recipient verification, consent evidence and human authorization. Keep SMS off until externally registered.
5. Harden email: verified domain/sender identity, SPF/DKIM/DMARC, secure provider DPA, unsubscribe and bounce states, PII minimization, templates, correct reply threading, never unchecked free-text recipient rewriting.
6. Implement calendar free/busy and showing reservation with connector permission scopes, time zones/DST, working hours, per-agent/calendar selection, double-booking protection, tentative versus confirmed status, cancellation and reschedule, provider confirmed event ID and compensation.
7. Create a broker-wide communication inbox that honors agent assignment/grants: agents own customer threads; team leads access permitted team records; owner sees organizational diagnostics with controlled content disclosure. Search and AI summaries use same ACL.
8. Reconcile communication timeline, operations ledger and billing events from a canonical event type. Avoid speed_to_lead “lead_engaged” counting merely staged messages unless expressly contracted.
9. Implement per-tenant provider availability, capability probing, retry policy and circuit-breaker UI, plus actionable “requires setup” and registration states. Never claim connected because SDK is imported.
10. Confirm outbound campaigns have explicit rate/budget limits, customer consent, intelligent personalization from verified CRM, agent/broker approval and delivery receipts. Audit quiet hours with customer's location/time zone and applicable laws.
11. Support onboarding and offboarding: each agent may have their own verified business number or approved routing identity; moving agent cannot retain use of brokerage numbers/threads/secrets improperly.
12. Create provider-sandbox end-to-end harness plus human-controlled test calls/texts/email/calendar. Test wrong number, retries, partial outage, webhook replay and event ordering, consent changes, provider credentials revoked mid-job, unavailable calendar, double booking, carrier costs and false success.

### Acceptance
One broker + 20 authorized agents can configure supported connections, handle calls/messages, draft/approve and verify actual delivery, schedule/reschedule showings, inspect accurate timelines and preserve privacy and legal rules. Honest degraded states and support escalation for unconfigured external providers.

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
