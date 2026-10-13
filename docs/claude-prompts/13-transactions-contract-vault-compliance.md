# 13 — Deals, transactions, contracts, documents and compliance

> **Scope:** Carry a brokerage deal from qualified client through offer, contract, due diligence, closing and archive, with guarded transitions, an event trail, sourced documents and honest commission reporting, and never fabricate, silently send or present legal and financial artifacts as legal advice. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03, 04, 08, 10, 11 · **Unblocks:** 20 · **Ledger:** [L13](LEDGER.md#l13) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Transactions and portfolio: `backend/portfolio_api.py`, `backend/crm.py`, `backend/marketplace_api.py`, `backend/marketplace_engine.py`
- Contracts and vault: `backend/contracts_api.py`, `backend/contract_vault.py`, `backend/media_api.py` (`synthesize_contract`), `backend/policy_contract.py`, `backend/commands_api.py`
- Compliance: `backend/state_compliance/routes_compliance.py`, `backend/compliance_engine/` (`closing.py`, `disclosures.py`)
- Privacy and holds: `backend/privacy_lifecycle.py`, `backend/privacy_data_map.py`, `docs/data-retention-policy.md`
- Migrations: `backend/db/migrations/0007_contract_lifecycle.sql`, `backend/db/migrations/0031_tenant_contract_template_registry.sql`, `backend/db/migrations/0032_contract_draft_workspaces.sql`, `backend/db/migrations/0039_transaction_workflow.sql`, `backend/db/migrations/0045_personal_ai_document_vault.sql`
- Frontend: `oracle-app/src/components/DealsTab.jsx`, `oracle-app/src/components/DealPipeline.jsx`, `oracle-app/src/components/DealBook.jsx`, `oracle-app/src/components/DealRoomPanel.jsx`, `oracle-app/src/components/ContractVaultTab.jsx`, `oracle-app/src/components/ContractDraftWorkspace.jsx`, `oracle-app/src/components/PortfolioTab.jsx`, `oracle-app/src/components/ComplianceChecklistPanel.jsx`, `oracle-app/src/components/MarketplaceBrowse.jsx`
- Tests: `backend/tests/test_transaction_workflow.py`, `backend/tests/test_transaction_lost.py`, `backend/tests/test_contract_vault.py`, `backend/tests/test_contract_approval_security.py`

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Deals has five sub-views.** `DealsTab.jsx:22-26` defines Pipeline, Transactions, Contracts, Portfolio and Marketplace; `:62-66` render `DealPipeline`, `DealBook`, `ContractVaultTab`, `PortfolioTab` and `MarketplaceBrowse`.
- **A transaction has four statuses.** `transactions` allows `active`, `under_contract`, `closed`, `cancelled` (`0039_transaction_workflow.sql:105`). There is no prospect, contingency, inspection, financing or archive stage.
- **Closing and offer acceptance guard their transitions.** `close_transaction` (`portfolio_api.py:991`) checks the version (`:1008`) and requires `under_contract` (`:1010`). `accept_offer` (`:1245`) requires an `active` transaction and rejects the other submitted offers (`:1316`).
- **No transaction event trail was found.** Transitions update `transactions` in place with a version counter (`portfolio_api.py:1021` in `close_transaction`, `:1343` in `accept_offer`). Its only inserts are transactions, parties, offers and milestones (`:864`, `:905`, `:1221`, `:1477`); a closing also calls `outcome_memory.record_outcome` and `record_usage` (`:1046`, `:1062`).
- **Transaction SQL repeats a tenant predicate.** For example `close_transaction` updates `WHERE id=$1 AND tenant_id=$2::uuid` (`portfolio_api.py:1024`).
- **Transactions still accept a legacy MLS property source.** `PropertySource` is `pipeline` or `mls` (`portfolio_api.py:37`); `_property_anchor` (`:109`) reads `oracle_mls_listings` (`:142`); the schema has `mls_listing_id` (`0039_transaction_workflow.sql:11`, `:60`). Gating it is D-2026-10-12-01.
- **Property details are copied at creation.** `_property_anchor` builds `source_provenance` (`portfolio_api.py:166`), and the address columns live on the transaction (`0039_transaction_workflow.sql:12-14`).
- **Milestones have a due time but no reminders.** `transaction_milestones.due_at` (`0027_real_estate_intelligence_platform.sql:673`); `add_milestone` (`portfolio_api.py:1459`) and `update_milestone` (`:1499`) send no reminder.
- **Contract approval is the broker-owner's, and vaulting is a gated job.** `review_document` (`contracts_api.py:2085`) requires `BROKER_OWNER` (`:2091`) and enqueues a `LEGAL_DOCUMENT` job bound to the approval (`:2107`).
- **Signatures are attestations, not verified signing.** `record_signature` (`contracts_api.py:2293`) stores `signature_verification_status='unverified'` (`:2311`). A backend grep for DocuSign, HelloSign or e-sign providers finds none.
- **Contracts are rendered from reviewed templates only.** `synthesize_contract` (`media_api.py:451`) renders an attorney-approved template (`:456`, `attorney_reviewed_by` at `:470`). `complete_draft_workspace` keeps missing values missing (`contracts_api.py:1573`).
- **The vault accepts generated PDFs only.** `contract_vault.py:46` `MAX_PDF_BYTES` (10 MB), a `%PDF-` check at `:142`, SSE-S3 encryption at `:171` and signed URLs of at most one hour at `:200`.
- **Commission accounting is not connected.** `OurAITab.jsx:390` says so, in the same sentence that calls deal reporting and approval queues "live".
- **Legal holds exist and deal tables are classified.** `active_legal_hold` (`privacy_lifecycle.py:296`); `transactions` (`privacy_data_map.py:73`) and `contract_documents` (`:153`).
- **The compliance checklist reads and writes per transaction.** `ComplianceChecklistPanel.jsx:66` calls `GET /api/compliance/checklist/{id}`; the write route is at `routes_compliance.py:102`.

## 3. Goal

An agent can run a deal from client to closing in Deals, and the broker can see the team's pipeline, deadlines and exceptions. Every transition is guarded and recorded. Every document and number traces to its source. Nothing legal or financial is sent, signed or reported as paid without a human decision and a provider receipt.

## 4. Engineering tasks

- **T13.01 Inventory the deal surface.** State machines, migration history, agent and broker roles, offers, milestones, escrow, title and lender placeholders, templates, vault, portfolio and marketplace. Mark which e-sign and accounting integrations are real and which are stubs.
- **T13.02 One canonical lifecycle with an event trail.** Prospect or active, offer drafting, offer presented, accepted or rejected, contingencies, inspection, appraisal, financing, closing scheduled, closed or lost, archived. Write an immutable event for each transition with its legal audit state; reject illegal transitions. Use an additive, reversible migration from today's four statuses.
- **T13.03 Template registry.** Jurisdiction, document version, brokerage authorization, origin and licence, human review, effective date and supersession. Model-written statutory text is never presented as an official form; consult counsel where needed.
- **T13.04 Secure document intake.** MIME sniffing, size and quota, malware screening, PDF processing, encryption, signed URLs, owner and participant ACL, watermark and audit where fitting, retention, secure export, and deletion that respects legal holds. Verify whether any intake path exists for documents other than generated PDFs.
- **T13.05 Cite and cross-check document AI.** Synthesis cites page and source, and separates extraction from inference. Amounts, parties and dates get a deterministic parse and cross-check; conflicting versions create review tasks instead of overwriting.
- **T13.06 Finish the five Deals sub-views.** Real backend state, navigable detail and approval actions, and tests for every form and button, including authorized updates and degraded states.
- **T13.07 Calendared milestones and reminders.** Due dates with time zone and jurisdiction, owner and team responsibility, escalation, reschedule, signed amendments and several open offers on one property. No duplicate reminders; calendar writes go through 10's approval path.
- **T13.08 Commission accounting.** Get the owner's choice between an internal split and payout ledger and an external accounting integration, then build approval, reconciliation, tax and compliance scope, and honest UI. Never show an unverified payout as paid, and fix the "live" wording in `OurAITab.jsx` to match the evidence.
- **T13.09 Broker visibility.** Team pipeline totals, aging, blocked deadlines, document readiness, permissioned agent detail and compliance exceptions. Keep private notes private, keep brokerages isolated, and audit every portfolio and marketplace permission.
- **T13.10 Sourced property snapshots.** Snapshot property facts from the non-MLS property sources (see 11) with source and effective date, so historical deal terms never change when a source updates.
- **T13.11 Gate the legacy MLS property source.** Stop new transactions from using `property_source='mls'`, together with 11's T11.01. Keep existing rows and columns (D-2026-10-12-01; no deletion without owner approval).
- **T13.12 Saga and outbox for external steps.** E-sign, email, calendar and payment, when connected: idempotent callbacks, failure reconciliation, and human approval for irreversible transitions.
- **T13.13 Scenario suite.** Build the scenario tests in §5 as one runnable suite, plus the end-to-end journey in AC13.10.
- **T13.14 Verify whether the tenant predicates duplicate RLS.** Check the `AND tenant_id=$2` clauses in `portfolio_api.py` against the policy as the real application role (C3.3), and remove them if they hide rows a platform admin or delegated reader should see.
- **T13.15 A truthful signature path.** Until an e-sign provider is connected, label recorded signatures "attested, not verified" everywhere. When one is connected, store the provider envelope ID and verify signed state from its callback.

## 5. Failure and adversarial cases

- **F13.01 Illegal transition.** A deal closes from `active`, a non-submitted offer is accepted, or two offers end up accepted.
- **F13.02 Concurrent edits.** Two agents edit the same deal and a stale version overwrites the newer one.
- **F13.03 Access drift.** A transferred agent keeps access, a withheld document is still downloadable, or another brokerage sees the deal.
- **F13.04 Fabricated legal content.** Model text is shown as an official form, or AI invents a party, amount or date.
- **F13.05 Silent overwrite.** A contract amendment or conflicting date replaces the earlier version with no review task.
- **F13.06 Unsafe upload.** An oversized, non-PDF or malicious file is accepted, or a signed URL outlives its policy.
- **F13.07 External step repeated or faked.** A duplicate e-sign event or an offline provider causes a second effect or a false "signed".
- **F13.08 Unverified money.** A commission payout is shown as paid without reconciliation.
- **F13.09 Snapshot drift.** Historical deal terms change when a property source updates, or MLS-sourced data enters a new deal (out of scope, D-2026-10-12-01).
- **F13.10 Hold or erasure mishandled.** Deletion ignores a legal hold, or a hold blocks lawful erasure without a record.
- **F13.11 Deadline errors.** A time-zone or holiday mistake moves a deadline, a reminder fires twice, or a rescinded offer stays open.

## 6. Acceptance criteria

- **AC13.01 Transitions are guarded and recorded.** Every legal transition writes one immutable event, and every illegal one is rejected. Evidence: state-machine tests in `backend/tests/test_transaction_events.py` (proposed) plus version-conflict tests. Covers: T13.01, T13.02, F13.01, F13.02.
- **AC13.02 Access holds under the real role.** Assigned, transferred, team-lead, owner and cross-brokerage access behave as granted, with no duplicated tenant predicate. Evidence: real-PostgreSQL RLS tests under the application role. Covers: T13.09, T13.14, F13.03.
- **AC13.03 Legal text and extracted facts are sourced.** Official forms come only from reviewed templates; extracted amounts, parties and dates cite a page and pass a cross-check; conflicts create review tasks. Evidence: template registry tests plus an extraction set with planted conflicts. Covers: T13.03, T13.05, F13.04, F13.05.
- **AC13.04 Intake is safe and respects holds.** Bad files are refused, signed URLs expire within policy, and deletion obeys legal holds with a record. Evidence: upload tests with oversized, renamed and malicious samples; hold and erasure tests. Covers: T13.04, F13.06, F13.10.
- **AC13.05 External steps happen once and are labelled truthfully.** Duplicate callbacks are idempotent, offline providers reconcile, and unverified signatures read "attested, not verified". Evidence: saga tests with duplicate and out-of-order events; UI snapshot. Covers: T13.12, T13.15, F13.07.
- **AC13.06 Commission is never shown as paid without reconciliation.** Evidence: payout ledger or integration tests per the owner's choice, plus a UI test of the not-connected state. Covers: T13.08, F13.08.
- **AC13.07 Snapshots are sourced and stable, and MLS is gated off.** New deals take facts only from non-MLS sources with an effective date; a source update leaves old deals unchanged; `property_source='mls'` is refused for new rows and old rows are untouched. Evidence: snapshot tests plus a creation test. Covers: T13.10, T13.11, F13.09.
- **AC13.08 Milestones remind once, at the right local time.** Evidence: reminder tests across DST, a holiday, a reschedule and a rescinded offer. Covers: T13.07, F13.11.
- **AC13.09 The five Deals sub-views work on desktop and phone.** Evidence: Playwright run of every form and button, incl. degraded states. Covers: T13.06.
- **AC13.10 One deal runs end to end.** Lead → offer → reviewed and signed documents → scheduled milestones → closing → broker report, with role checks, provider receipts where connected and no unreviewed legal or financial claim. Evidence: the scenario suite run, with command and results. Covers: T13.13.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS is out of scope** (D-2026-10-12-01). Do not use MLS listings as a deal's property source. Which non-MLS source replaces it for listed homes is an owner question; do not invent one.
- **No legal advice.** Neoh drafts from reviewed templates and flags issues; it never tells a party what a contract means for them. Counsel reviews jurisdiction rules.
- **No unreviewed financial claims.** Prices, commissions and payouts shown to anyone come from a source or a reconciled ledger.
- **Owner questions:** internal commission ledger or external accounting integration? Which e-sign provider, if any? Which non-MLS property source for listed homes?
- **Out of scope here:** client portal and public sites (14); calendar and email delivery mechanics (10); property data sources (11).

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the lifecycle and event-trail migration with tests;
  - the transaction access matrix (`docs/deal-access-matrix.md`, proposed);
  - the MLS-source gate on transaction creation;
  - the scenario suite and its run result.
- **First slice:** T13.02 + T13.14 with AC13.01 and AC13.02, since every later deal feature writes through these transitions. Then T13.11 with 11's gating slice.
- **Ledger:** add L13-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
