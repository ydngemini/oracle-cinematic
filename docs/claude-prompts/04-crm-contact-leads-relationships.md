# 04 — CRM identity, lead intake, assignment, consent and lifecycle

> **Scope:** Give each customer one canonical identity across web intake, calls, texts, leads, deals and property links, with signed intake, honest routing, consent, stage truth and a full record lifecycle, inside brokerage-private scope. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01, 03 · **Unblocks:** 05, 06, 10, 13, 20 · **Ledger:** [L04](LEDGER.md#l04) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Backend: `backend/contacts_api.py`, `backend/contact_truth.py`, `backend/crm.py`, `backend/lead_routing_api.py`, `backend/speed_to_lead.py`, `backend/client_ai_automation.py`, `backend/billing_usage.py`
- Frontend: `oracle-app/src/components/PeopleTab.jsx`, `oracle-app/src/components/ClientCrmTab.jsx`, `oracle-app/src/components/ContactIntakePanel.jsx`, `oracle-app/src/components/ClientTimeline.jsx`, `oracle-app/src/components/LeadRoutingPage.jsx`
- Migrations: `0001_init_tenancy.sql`, `0018_harvest_idempotency_and_force_rls.sql`, `0019_client_enterprise.sql`, `0027_real_estate_intelligence_platform.sql`, `0054_agent_contacts_and_intake.sql`, `0055_inbound_voice_routes.sql`, `0061_lead_intake_and_routing.sql`, `0067_speed_to_lead_and_usage_metering.sql`, `0108_telnyx_messaging.sql`
- Prompt [03](03-brokerage-tenancy-hybrid-access.md) (assignment identity) and prompt [11](11-property-market-intelligence.md) (property sources); audit A01, A02, A05 in [LEDGER.md § Audit cross-reference](LEDGER.md#audit-cross-reference)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Customers live in several overlapping tables.** `clients` (`0001_init_tenancy.sql:74-82`), `leads` keyed by parcel (`:85-95`), `agent_contacts` (`0054_agent_contacts_and_intake.sql:9-30`), and `transaction_parties` (`0027_real_estate_intelligence_platform.sql:597-608`).
- **Clients and contacts are linked both ways.** `agent_contacts.legacy_client_id` (`0054_agent_contacts_and_intake.sql:26`) and `clients.contact_id` (`:104`).
- **Calls, texts and lead events point at `agent_contacts`.** Inbound voice (`0055_inbound_voice_routes.sql:109`), `sms_messages` (`0108_telnyx_messaging.sql:237`) and `lead_intake_events` (`0061_lead_intake_and_routing.sql:75`).
- **`clients` stores name, email and phone in plain columns.** `0001_init_tenancy.sql:77-79` (`full_name`, `email`, `phone`); `agent_contacts` stores `pii_ciphertext` plus lookup hashes (`0054_agent_contacts_and_intake.sql:13-15`). Audit A01 is owned by 17.
- **Contacts carry consent and suppression per channel.** `0054_agent_contacts_and_intake.sql:20-23` (`consent`, `suppression` JSONB defaults: all false).
- **Phone and email are normalized and hashed per tenant.** `contact_truth.py:66` `normalize_email`, `:83-84` `normalize_phone` (E.164-shaped), `:114` `lookup_hash`.
- **Duplicate lookups are indexed but not unique.** `0054_agent_contacts_and_intake.sql:86-91` (`idx_agent_contacts_email_lookup`, `idx_agent_contacts_phone_lookup`, plain `CREATE INDEX`). No merge endpoint exists in the backend (`grep -rn "merge_contacts\|/merge" backend --include=*.py`).
- **Property links already carry a reference kind.** `0054_agent_contacts_and_intake.sql:119-145` `contact_property_relationships`: `property_ref_kind` in lead, listing, public_record, manual; `relationship_type` in owner, seller, buyer, occupant, other.
- **Leads are unique per tenant and parcel.** `0018_harvest_idempotency_and_force_rls.sql:56` `leads_tenant_parcel_key`.
- **Public lead intake is HMAC-signed with a five-minute replay window.** `lead_routing_api.py:53-67` `_verify_webhook_signature`, `:40` `_SIGNATURE_WINDOW_SECONDS`; events are unique per connector and external id (`0061_lead_intake_and_routing.sql:71`) and hashed (`lead_routing_api.py:691` `payload_digest`).
- **Intake events can end as `unassigned`.** `0061_lead_intake_and_routing.sql:62` (`received`, `routed`, `unassigned`, `failed`).
- **Routing joins contacts to users on the text agent id.** `lead_routing_api.py:409` (`c.assigned_agent_id=u.agent_id`).
- **Speed-to-lead meters `lead_engaged` right after a staged response (audit A05).** `speed_to_lead.py:380-392` in `_speed_to_lead_job` (`disposition="staged"`, then `record_usage(metric="lead_engaged")`).
- **Routing metrics count staged drafts as responses.** `lead_routing_api.py:535-552` (`disposition IN ('staged','sent')` for `responded`, p50, p90 and under-90 s).
- **A failed property lookup returns an empty list and logs the customer's name (audits A02, A06).** `client_ai_automation.py:695-704` `_property_candidates_bounded` (`except Exception` → `return []`).

## 3. Goal

One relationship per customer runs from intake → identity match → qualification → assignment → communications → preferences → showing → deal → result. Every step keeps its evidence, consent and brokerage-private scope. A call or form never creates a second customer, a draft is never counted as an engagement, and a failed source is never read as "no matches".

## 4. Engineering tasks

- **T04.01 Map the overlapping entities.** Cover `agent_contacts`, `clients`, `leads`, site leads, SMS senders, inbound callers, deal participants and parcel or owner links from public records. Diagram canonical IDs, provider IDs and legacy adapters.
- **T04.02 Model one identity with many roles.** Buyer, seller, homeowner, transaction party, owning agent, team, household or group, preferred channel, verified alias, opt-out state, current assignment and relationship history.
- **T04.03 Write explicit dedup rules.** Use stable event and source identity; normalize phone (E.164 when valid), email (with care for its semantics), address and external CRM IDs. Uncertain collisions stay separate until a human approves a merge. Merges are reversible and audited.
- **T04.04 Harden lead ingestion.** Check signature, timestamp, replay window, IP and rate limits, idempotency key, schema, duplicate callbacks, source licence and tenant routing. Reject unsigned production webhooks without leaking data.
- **T04.05 Separate the parts of a lead score.** Observed, declared and inferred interest; completeness; source freshness; actionability; consent. A high-scored lead with no usable address is not an actionable property candidate.
- **T04.06 Route by real constraints.** Brokerage and team membership, capacity, ZIP or service area, verified skills and availability, explicit owner assignment, fairness, suspensions and reassignments. When routing is impossible, the lead goes to a visible review queue, never to a silent default agent.
- **T04.07 Keep speed-to-lead stages honest.** Stage messages with real consent and approval; keep staged, approved, accepted, delivered and responded apart in storage, UI and metrics. Do not meter `lead_engaged` on a draft without a matching pricing contract (A05, with 18).
- **T04.08 Finish the CRM screens.** Intake, add and edit, import with dry run and field mapping, assignment, share and revoke, duplicates, notes, timeline, tasks, segments, buyer and seller match, state changes, optimistic concurrency and stale-data markers.
- **T04.09 Propagate deletion, export and closure.** Reach AI memory, search projections, vector records, queued jobs, analytics and provider connectors, while keeping legal retention.
- **T04.10 Report degraded reconciliation.** Use the per-source status from prompt 11 (T11.10) so a timeout is `degraded` or `unknown`, never zero matches.
- **T04.11 Keep a CRM operation ledger.** Who changed a fact, whether it was verified, its source and time, undo or compensation where possible, and the exact outcome of each external action.
- **T04.12 Block cross-agent access in every path.** List pagination, term search, guessed client IDs, deep-linked sheets, caller route matching and background jobs.
- **T04.13 Verify whether site leads, texts and calls all resolve to an existing contact.** Check `hyperlocal_site_attribution_events` (`0056_studio_sites_and_contract_types.sql:133`), `sms_messages` and inbound calls.
- **T04.14 Verify whether new code still writes plaintext to `clients`.** Report to 17 (A01).
- **T04.15 Verify whether `unassigned` intake events reach a visible queue.** Check `LeadRoutingPage.jsx` and the routing API.

## 5. Failure and adversarial cases

- **F04.01 Duplicate customer.** A call, text or web form creates a new contact for a known person.
- **F04.02 False merge.** Two people who share an email, phone or household are merged without approval.
- **F04.03 Bad webhook accepted.** An unsigned, stale, replayed or duplicate event is accepted, or a rejection reveals data.
- **F04.04 Silent default agent.** Routing fails and the lead lands with an arbitrary agent or nowhere visible.
- **F04.05 Draft counted as engagement.** A staged draft is counted as a response, a delivered message or a billable event.
- **F04.06 Opt-out ignored.** A contact opts out between drafting and sending, and the send still happens.
- **F04.07 Deleted contact lingers.** A deleted or exported-and-closed contact survives in AI memory, search, vectors, jobs, analytics or connectors.
- **F04.08 Timeout read as zero.** A property lookup times out and reconciliation reports no matches.
- **F04.09 Cross-agent access.** Agent B reaches agent A's contact through pagination, search, an ID guess, a deep link, caller matching or a job.
- **F04.10 Wrong owner or parcel.** A fuzzy public-record match links the wrong owner and a draft states it as fact.
- **F04.11 Lost update.** Two concurrent edits overwrite each other with no conflict reported.

## 6. Acceptance criteria

- **AC04.01 Every channel resolves to one contact.** The entity map exists, and tests send the same person through web form, call, text and lead webhook and get one contact. Evidence: `docs/crm-entity-map.md` (proposed) plus integration tests. Covers: T04.01, T04.02, T04.13, F04.01.
- **AC04.02 Dedup holds under race and stays reversible.** N concurrent identical intakes create one contact; uncertain collisions wait for approval; merge and unmerge keep audit. Evidence: real-PostgreSQL concurrency test. Covers: T04.03, F04.02.
- **AC04.03 Intake rejects bad events safely.** Tests cover bad signature, stale timestamp, replay, duplicate external id, oversize body, wrong tenant and rate limit; rejections reveal nothing. Evidence: `backend/tests/test_lead_routing.py` extended, run with exit 0. Covers: T04.04, F04.03.
- **AC04.04 Scoring and routing are explainable.** Unactionable leads are labelled; routing honours membership, capacity and suspension; impossible cases appear in the review queue. Evidence: routing unit tests plus a Playwright check of the queue. Covers: T04.05, T04.06, T04.15, F04.04.
- **AC04.05 Stages are never merged.** Metrics and billing count only the stage they name; staged drafts are excluded from responses and from `lead_engaged` unless 18's pricing contract says otherwise. Evidence: tests on the metrics query and on usage metering. Covers: T04.07, F04.05.
- **AC04.06 Consent is checked at send time.** An opt-out recorded after drafting blocks the send. Evidence: test that flips suppression between stage and execute. Covers: F04.06.
- **AC04.07 Lifecycle actions reach every copy.** Delete, export and closure reach the derived stores listed in T04.09. Evidence: lifecycle integration test with an orphan scan. Covers: T04.09, T04.14, F04.07.
- **AC04.08 Reconciliation is degraded, not empty, on failure.** A timeout or provider error yields `degraded`; an unverified parcel match is labelled "possible match". Evidence: tests for timeout, error, verified zero and fuzzy match. Covers: T04.10, F04.08, F04.10.
- **AC04.09 Every CRM write is ledgered and conflict-safe.** Each write records actor, source, time and verification; a stale write gets a conflict response. Evidence: API tests plus component tests for the conflict state. Covers: T04.08, T04.11, F04.11.
- **AC04.10 Isolation holds at scale.** One brokerage, 20 agents, overlapping client names and several million synthetic event references (target, not measured): no duplicates under race, no unauthorized routing, no cross-agent reads. Evidence: seeded live test run as the application role. Covers: T04.12, F04.09.
- **AC04.11 The funnel is measured with drafts excluded.** p50/p95 for signed lead to routing, to agent acknowledgement, to approved delivery and to response; plus showing attendance, missing-source rate, dedup false positives, fairness and time saved. Evidence: committed `scripts/measure-lead-funnel.py` (proposed) and its recorded output. Covers: T04.06, T04.07.

## 7. Out of scope, owner decisions, category-specific rules

- **Property links come from public records and other sources owned by 11.** The old MLS-owner linkage is out of scope (D-2026-10-12-01).
- **Email is not always a person.** Shared and role addresses never merge contacts on their own.
- **No unsigned production webhooks.** Test fixtures may skip signatures only outside production.
- **Provider acceptance is not delivery.** Use the C6 stage words in data, UI and metrics.
- **Owner question:** should `lead_engaged` be metered at approval, at delivery, or at the customer's first reply?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the entity map and canonical identity model;
  - dedup and merge rules with reversible merge;
  - ingestion hardening and routing review queue;
  - stage-true metrics and metering (with 18);
  - CRM screens, operation ledger and lifecycle propagation;
  - the funnel measurement script and its result.
- **First slice:** T04.01 + T04.13 + AC04.01, after 03's T03.03 settles the assignment identity. Then T04.07 + AC04.05, which closes the draft-counting gap.
- **Ledger:** add L04-01 onward per slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
