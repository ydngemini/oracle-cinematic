# Claude implementation prompt 13: Transaction lifecycle, deal rooms, contracts, compliance, portfolio and commission controls

> Repository `ydngemini/oracle-cinematic`; one brokerage, **20 authorized agents** and an explicitly modeled owner role; *every* entitled web page and advertised workflow is in the final release scope.
> This is an engineering implementation assignment. It does not establish any asset, service, API connection, third-party license, production deployment or benchmark as shipped.

## Your role and preflight

Act as principal engineer, domain specialist and security reviewer. Read root `CLAUDE.md`, this folder's `README.md` and relevant existing audit/implementation specs. Pin current commit and inspect true source paths/line references. Implement focused code changes and passing tests, not just aspirational prose. Preserve existing FastAPI/Postgres/React/Vite, tenant RLS, permission/approval mechanisms, provider contracts and browser-only capture.

**Likely sources to inspect:** backend/crm.py; backend/commands_api.py; backend/privacy_lifecycle.py; oracle-app/src/components/DealsTab.jsx; oracle-app/src/components/DealPipeline.jsx; oracle-app/src/components/DealBook.jsx; oracle-app/src/components/DealRoomPanel.jsx; oracle-app/src/components/ContractVaultTab.jsx; oracle-app/src/components/ContractDraftWorkspace.jsx; oracle-app/src/components/PortfolioTab.jsx; oracle-app/src/components/ComplianceChecklistPanel.jsx

## Implementation work

### Goal
Full brokerage transaction workflows from qualified client to showing, offer, contract, due diligence, financing, closing, archived records and evidence-backed reporting. Legal and financial artifacts must never be fabricated, silently sent or accepted as legal advice.

### Plan
1. Inventory transaction/deal state machines, schema migration history, agent and broker roles, offers, milestones, escrow/title/lender placeholders, contract templates, document vault, portfolio and marketplace surfaces. Identify implemented versus stubbed external signature/accounting integrations.
2. Implement canonical Deal and Transaction lifecycle: prospect/active, offer drafting, offer presented/accepted/rejected, contingencies, inspection/appraisal/financing, closing scheduled, closed/lost and archival. Provide immutable event trail and legal audit state for every transition; guard illegal transitions.
3. Template registry: state/jurisdiction, document version, brokerage authorization and origin/licensing, human review, effective date and supersession; no model-created statutory text falsely presented as official form. Consult counsel where needed.
4. Secure document intake: MIME sniffing, size/quota, malware screening, PDF processing, encryption, signed URLs, owner/participant ACL, watermark/audit if appropriate, retention policies, secure export and deletion/hold logic.
5. AI synthesis from documents must cite page/source and separate extraction from inference. Numerical fields, parties, amounts and dates require deterministic parse/cross-check; conflicting contract versions must generate review tasks, not silent overwrites.
6. Provide complete Deals subpages Pipeline, Transactions, Contracts, Portfolio and Marketplace with real backend state and navigable detail/approval actions. All forms/buttons need tests including authorized updates and degraded states.
7. Add verified calendared milestones and reminders: due dates with time zone/jurisdiction, agent/team responsibility, escalation, reschedule, signed contract amendments and multiple open offers on a property. Avoid duplicate reminders.
8. Audit commission accounting limitation: current UI reports commission accounting not connected. For full-feature release decide authoritative brokerage splits and payout ledger versus external accounting integration, approval, reconciliation, taxes/compliance scope and UI honesty. Never display unverified payout as paid.
9. Broker visibility: team pipeline totals, aging, blocked deadlines, document readiness, permissioned agent detail, compliance exceptions; preserve private notes and cross-brokerage isolation. Audit all portfolio and marketplace permissions.
10. Integrate listing/property snapshots so historical deal terms do not silently mutate on new MLS updates; retain sourced snapshots and effective dates.
11. Use saga/outbox with external e-sign, emails, calendar and payment when connected; idempotent callbacks, failure reconciliation and human approval for irreversible transitions.
12. Build scenario tests: two agents same deal, transferred agent, contract amendment, rescinded offer, time-zone/holiday issue, withheld document permission, provider offline, duplicate e-sign event, deletion hold and conflicting dates.

### Done
End-to-end test from lead -> offer -> signed/reviewed docs -> scheduled milestones -> closing -> broker report, with role checks, actual provider receipts and no unreviewed legal/financial assertions.

## Universal constraints, tests and deliverables

- Verify every associated page, workflow, API and worker; no false capability badges. Customer-required credentials/licensing may show a complete, truthful setup-required flow; genuinely missing advertised functionality must be implemented before calling an all-features release finished.
- Access rights: authorized assigned/shared agent records, team lead only in delegated scope, brokerage owner administration separate from platform admin; revalidate before external side effects and respect consent/PII.
- Evidence quality: distinguish source-backed facts, inferred predictions, provider timeouts, drafts, accepted jobs, delivered messages and actual success. Document source timestamps and failure states.
- Test units, schema/contracts, real PostgreSQL/RLS and migration chain, React UI/Playwright, concurrency and device scenarios as applicable. Use sandbox accounts for providers and synthetic data; never send to actual clients, spend or deploy live without authorized human approval.
- Measure actual latency/memory/cost, specify test conditions and p50/p95; mocked provider timings are not production results.
- Report source-linked baseline and gaps, architecture diagram or sequence, implementation plan with P0/P1/P2, file changes and migrations, tests actually executed, failure/recovery proofs, human/external blockers and next dependent slice. Keep category acceptance ledger updated; never claim “fully operational” from a mock alone.
