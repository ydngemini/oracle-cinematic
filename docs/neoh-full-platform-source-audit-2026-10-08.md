# Neoh full-platform source audit and engineering backlog

> Snapshot: 2026-10-08; `ydngemini/oracle-cinematic`, branch `main`, examined commit [`3b63d5b`](https://github.com/ydngemini/oracle-cinematic/commit/3b63d5b49e426bd613cc1dc2b9e073a5efc2c5db).
>
> This is a documented **audit and remediation backlog**, not an assertion that the work below has been implemented or that every source line has been verified. Reopen current code and run tests before making changes. Check this document against newer commits before using line numbers.
>
> Related agent entry point: [`CLAUDE.md`](../CLAUDE.md). This platform-wide backlog supplements, but does not replace, the four approved web-only Neoh Space workstreams in [`neoh-space-web-capture-claude.md`](neoh-space-web-capture-claude.md).
>
> **Live status (added 2026-10-12).** The findings below now carry stable IDs A01–A10. Their current status is tracked only in the [ledger's audit cross-reference](claude-prompts/LEDGER.md#audit-cross-reference), and the remediation work lives in the [category prompts](claude-prompts/README.md). The original evidence links stay pinned at `3b63d5b`; each finding adds one dated "Status at HEAD" line and nothing else is rewritten.

## 1. Scope, coverage, and reproducibility

The initial repository inventory identified **1,636 tracked files**, including **626 Python** and **190 JSX** files at the snapshot. Those are inventory counts, **not** 1,636 fully audited files. The review examined the Git tree, substantial implementation paths, selected tests, migrations, infrastructure/documentation, and CI status. A literal every-line review, all SQL migrations, dependency/model-license audit, real provider calls, full-scale production tests, and a production penetration test remain outstanding.

The cited [GitHub Actions run](https://github.com/ydngemini/oracle-cinematic/actions/runs/37839584771) reported passing backend tests, frontend lint/type/tests/build, real-PostgreSQL migration/security checks, and staging deployment/smoke tests; **production promotion was skipped**. This establishes passing checks **in those environments**, not live production safety, real carrier delivery, real brokerage outcomes, or 3D field quality. Revalidate the run and head commit on each audit continuation.

### Shipping vs. historical or specialized surfaces

| Area | Review focus |
| --- | --- |
| `backend/` | Primary FastAPI application, auth, domain services, provider integrations, jobs, AI, billing |
| `oracle-app/` | Primary React/Vite product, customer flows, auth, approvals, performance |
| `backend/db/migrations/` | PostgreSQL schema, RLS, privileges, encrypted columns, migration ordering |
| `infra/digitalocean/` | Target deployment, secrets, availability, rollback, scale |
| `src/` | Historical Next.js cinematic prototype; establish reachability before working on it |
| `observability-platform/` | Separate AWS-oriented dashboard; check whether assumptions are obsolete |
| `legal_sentinel/` | Specialized legal/retrieval component; validate integration, rights, and evidence |
| `training_data/` | Datasets; validate provenance, privacy, leakage, licensing and holdouts |

Reference: [repository README](https://github.com/ydngemini/oracle-cinematic/blob/main/README.md).

For every file, answer **(1) is the implementation correct and safe? (2) is it reachable from a shipping route, task, UI, or deployment?** Avoid spending launch effort on disconnected experiments.

## 2. Source-backed findings, priority and acceptance checks

Severity reflects possible impact, not proof of exploitation or customer harm.

### A01 · P0/High: customer PII and communications in plaintext

**Evidence:** [`docs/privacy-data-map.md`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/docs/privacy-data-map.md#L38-L79) documents plaintext `clients` contact data and notes, `leads.payload` owner names, `sms_messages` bodies/numbers, `negotiation_events.transcript_excerpt`, `user_interactions.content`, and additional AI/tool outputs. Some alternative encrypted columns exist but are unused. Do **not** assume all records are encrypted just because an encrypted path exists.

**Remediate:** inventory columns, access paths and retention; classify sensitive values; minimize at source; design field-level encryption and HMAC lookup where appropriate; migrate historical rows in controlled batches; dual-read/write safely during transition; preserve tenant RLS and deletion semantics; assess logs, backups, exports, search indexes and provider copies. Decide how lookup, search and analytics operate without reopening plaintext exposure.

**Acceptance:** tests prove sensitive write paths do not create new plaintext rows, old rows migrate correctly, tenant boundaries remain effective, required search still works, erasure covers derived copies, and rollback/recovery are documented. Treat production data migration as a staged, reversible operation.

**Status at HEAD `745f2fa` (2026-10-12):** open — `docs/privacy-data-map.md:40`, `:42`, `:48` and `:71` still record plaintext `clients` contact data, `leads.payload`, `sms_messages` and `user_interactions.content`, and `:142` lists the column-encryption migration as open. Not in the original evidence: AI call notes (commit `aec14c4`, which predates the `3b63d5b` pin) are written in plaintext. `backend/commands_api.py:2335`, inside `deliver_call_notes` (`:2301`), inserts the notes `body` into `client_notes`, whose `body` is a plain `text` column (`backend/db/migrations/0019_client_enterprise.sql:51`). `backend/privacy_data_map.py:65` classifies `client_notes` as business CRM data. Ledger row L09-02 cites A01.

### A02 · P0/High: distinguish failed property lookup from zero property matches

**Evidence:** [`backend/client_ai_automation.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/client_ai_automation.py#L680-L704) catches lookup errors and returns `[]`. [Reconciliation code](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/client_ai_automation.py#L935-L1016) sets `complete` versus `degraded` according to `model_error`, not property lookup failure. [Tests](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/tests/test_client_ai_automation.py#L192-L246) explicitly expect empty results on lookup timeout. Thus a missing/failed property search can masquerade as a successful zero-match check.

**Remediate:** return structured status per source, e.g. `checked`, `unavailable`, `timed_out`, `not_applicable`, alongside candidates and timestamps. Propagate required-source failures into overall `degraded` and customer-visible status. Preserve the bounded timeout and job durability; do not turn this back into an unrecoverable reconciliation failure.

**Acceptance:** tests separately cover verified zero matches, timeout, provider error, valid matches, stale results, idempotent retries and displayed status. No source failure is presented as checked-empty.

**Status at HEAD `745f2fa` (2026-10-12):** open — `backend/client_ai_automation.py:700`, inside `_property_candidates_bounded` (`:680`), catches every exception and returns `[]` (`:704`). Reconciliation still sets `degraded` from `model_error` only (`:962`). The file is unchanged since `3b63d5b`.

### A03 · P0/High: professional 3D certification without verified evidence

**Evidence:** [`backend/recon_quality.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/recon_quality.py#L79-L160) records `quality_unverified` when held-out quality metrics are absent, without necessarily setting `blocked`. Audit the consumer(s) of this verdict to ensure a professional-grade badge, publication claim, or quality certification **cannot** be inferred from a non-blocked verdict alone.

**Remediate:** distinguish provisional, failed, degraded and independently verified certification; require measured held-out image quality, camera registration, doorway/room connectivity, and device-specific mobile tour performance for high-confidence claims. Treat a missing metric as missing evidence, not a passing score.

**Acceptance:** missing or invalid metrics, disconnected rooms, unregistered captures and stale quality records cannot produce a “verified/professional” claim; 2D/photo fallback remains usable. Align with the [web-only Neoh Space specification](neoh-space-web-capture-claude.md).

**Status at HEAD `745f2fa` (2026-10-12):** open — `backend/recon_quality.py:79` `assess` adds `quality_unverified` with `blocked=False` for a missing report (`:83`) or missing held-out metrics (`:101`). `backend/reconstruction_worker.py:581` stops publication only when `verdict.blocked`. One consumer infers quality from asset existence alone: `backend/tour_api.py:335` labels every captured splat "Full 3D walkthrough" with `walkable: True` (`:334`), and `:41` `_TIER_BADGE[3]` is "Full 3D Walkthrough". `tour_api.py` never reads the verdict's `limitations`. No explicit certification state exists anywhere in the backend or frontend (`git grep -i certif`). Caveats are shown in the capture panel (`oracle-app/src/components/CaptureSessionPanel.jsx:219`) but not in the buyer tour viewer. Remediation: [prompt 12](claude-prompts/12-neoh-space-browser-capture-reconstruction.md) T12.09–T12.11.

### A04 · P0/High: production readiness is not established by staging CI

**Evidence:** the reviewed [successful CI workflow](https://github.com/ydngemini/oracle-cinematic/actions/runs/37839584771) includes staging checks but no completed production promotion. Check [`scripts/neoh-launch-readiness.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/scripts/neoh-launch-readiness.py), live provider configuration, privacy approvals, operations and support.

**Remediate:** independently validate credentials/secrets, live telephony/SMS/email/payment sandbox-to-live mapping, consent and disclosure, backup restoration, monitoring, incident response, rate and cost limits, rollback, account offboarding, and production deployment. Do not let outdated launch reports override current CI evidence.

**Acceptance:** explicit production go/no-go checklist with dated evidence and owners. Clearly distinguish `staging passed`, `production configured`, `production exercised`, and `customer outcome proven`.

**Status at HEAD `745f2fa` (2026-10-12):** open — `.github/workflows/ci.yml:606` defines a `promote` job that runs only on a manual "deploy" dispatch. The ledger (L19-02, `doctl apps list` on 2026-10-07, not re-run here) records that no production app exists.

### A05 · P1/Medium: usage event counted before approved/delivered outreach

**Evidence:** [`backend/speed_to_lead.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/speed_to_lead.py#L378-L405) records `lead_engaged` when an outreach command is **staged**, before human approval and provider delivery. Current flat-rate plans may not charge against this immediately; it is a billing-semantics risk if event-based billing is used.

**Remediate:** formalize `lead_received → lead_qualified → outreach_staged → outreach_approved → provider_accepted → delivered → lead_responded`, with stable IDs, event timestamps, retries and reconciliation. Make the billable stage contractually explicit. Preserve correct idempotency across retries and webhooks.

**Acceptance:** rejected, expired, duplicate and never-delivered commands cannot silently count as delivered or billable engagement unless the agreement expressly bills for staging; Stripe usage reconciles to independently checkable source events.

**Status at HEAD `745f2fa` (2026-10-12):** open — `backend/speed_to_lead.py:387` calls `record_usage(metric="lead_engaged")` inside `_speed_to_lead_job` (`:244`). It runs right after the command is recorded as `staged` (`:379`) and before approval or delivery. The file is unchanged since `3b63d5b`.

### A06 · P1/Medium: customer names in failure logs

**Evidence:** [`backend/client_ai_automation.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/client_ai_automation.py#L695-L704) logs `full_name` when property-candidate lookup fails.

**Remediate:** log tenant-safe stable IDs, outcome codes and metrics rather than customer names, CRM text, provider payloads or model prompts. Review neighboring warning/error code paths.

**Acceptance:** a failure-path test and representative log sample contain no names, phone numbers, message bodies or leaked cross-tenant identifiers.

**Status at HEAD `745f2fa` (2026-10-12):** open — `backend/client_ai_automation.py:701`-`:703` still logs `full_name` with `%r` on property-candidate lookup failure. Neighbouring warning paths were not re-checked.

### A07 · P1/Medium: concurrent messaging route initialization

**Evidence:** [`backend/messaging_data.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/messaging_data.py#L96-L116) checks for an existing route before insert; its `ON CONFLICT` clause can update `provider_account_id` although the function says existing routes remain unchanged.

**Remediate:** choose a single explicit invariant for initial creation versus authorized provider migration; use atomic insert/read semantics or guarded transitions. Preserve route ownership and provider consistency under racing requests.

**Acceptance:** deterministic concurrency tests show no implicit provider account switch, cross-tenant overwrite, or route/credential mismatch.

**Status at HEAD `745f2fa` (2026-10-12):** open — `backend/messaging_data.py:96` `ensure_messaging_route` still checks before inserting (`:100`). Its docstring says an existing row's provider is never overwritten, but `:108` runs `ON CONFLICT (tenant_id,agent_id) DO UPDATE SET provider_account_id=EXCLUDED.provider_account_id`. The file is unchanged since `3b63d5b`.

### A08 · P1/Medium: heuristic valuation targets in model training

**Evidence:** [`backend/ml_forge/edge_forge/train_lora.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/ml_forge/edge_forge/train_lora.py#L200-L209) contains fixed Delaware county price-per-square-foot assumptions and a 70% rule for synthetic underwriting examples.

**Remediate:** label such targets as synthetic/heuristic; separate production financial guidance from training experiments; validate with correctly licensed, time-bounded real transactions and genuinely held-out outcomes, calibration and drift checks.

**Acceptance:** no model or UX markets synthetic county constants as live appraisal or a verified deal price. Publish per-region error and uncertainty before promotion.

**Status at HEAD `745f2fa` (2026-10-12):** open — in code: `backend/ml_forge/edge_forge/train_lora.py:207` `_COUNTY_PPSF` still hard-codes Delaware county $/sqft values, and `:209` `_MAO_RULE = 0.70`. Whether any model or UX presents these as live valuations was not re-verified.

### A09 · P1/Medium: AI capability descriptions can exceed executable tooling

**Evidence:** [`backend/ai_chat_store.py`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/backend/ai_chat_store.py#L594-L648) deliberately maintains a constrained actual tool allowlist distinct from roadmap/catalog prose.

**Remediate:** show whether each capability is live, approval-gated, degraded, beta, or unavailable; do not advertise execution for tools without verified handlers, licensed data or safe side-effect paths.

**Acceptance:** UI, API metadata and model-exposed tools agree; unsupported actions cannot be asserted as completed.

**Status at HEAD `745f2fa` (2026-10-12):** partly re-verified — the constrained allowlist is still present (126 tools catalogued, 57 allowlisted; ledger L07-01) at `backend/ai_chat_store.py:605` `_AGENT_AVAILABLE_TOOLS` and `:646` `is_agent_tool_available`. Whether UI copy and API metadata agree with it was not checked.

### A10 · P1/Medium: local capacity test is not target-production evidence

**Evidence:** [`docs/capacity-plan.md`](https://github.com/ydngemini/oracle-cinematic/blob/3b63d5b/docs/capacity-plan.md#L127-L135) describes local testing limitations.

**Remediate:** test representative DigitalOcean infrastructure and real provider quotas with realistic concurrency and privacy-safe data. Measure queue latency, rate limits, cost and provider error handling, not only request throughput.

**Acceptance:** defined per-tier budgets and SLOs backed by reproducible target-environment tests, alert thresholds and graceful degradation.

**Status at HEAD `745f2fa` (2026-10-12):** open — `docs/capacity-plan.md:129` still says "Everything on DigitalOcean. No figure above was measured there." No production environment exists to measure (ledger L19-02).

## 3. Existing architecture worth preserving

- **Tenant isolation and DB security:** PostgreSQL migration/RLS/privilege tests ran with real PostgreSQL in CI. Keep enforcing tenant scope at storage and business-layer boundaries; test cross-tenant denial.
- **Human approval boundaries:** outbound communications, calling, legal instruments and monetary actions are deliberately gated. Do not bypass gates for “autonomous” demos.
- **Durability and idempotency:** job leases, command IDs, provider correlation, webhooks, retries and execution receipts matter at brokerage scale. Continue designing for at-least-once delivery.
- **Source provenance and uncertainty:** MLS licensing, data freshness and refusal to invent unsupported property matches are intentional. Propagate that discipline into every status/result. *(2026-10-12: MLS is out of scope (D-2026-10-12-01); the provenance and freshness discipline applies to the authorized non-MLS sources in [prompt 11](claude-prompts/11-property-market-intelligence.md).)*
- **Web-only Neoh Space decision:** honor the separate Capture Studio spec and its four approved workstreams; do not require native iOS, ARKit, RoomPlan or raw LiDAR.

## 4. End-to-end journeys required before production confidence

Each journey must assert **UI outcome + API response + persisted state + audit receipt + authorization + no unintended side effects**, including retries and partial failure.

1. **Brokerage onboarding:** tenant, owner, agents, role/permissions, billing and provider setup stay consistent.
2. **Lead to first contact:** signed intake, dedup, owner routing, consent/suppression, approval, provider delivery and accurately staged/recorded billing events.
3. **Inbound AI call:** correct called number/tenant, disclosures, verified identity where needed, conversation, transfer, callback, recording rules and durable CRM outcome.
4. **Multi-tool AI session:** grounded data, correct permission checks, staged approval, partial failures, undo/receipts, duplicate-safe external actions.
5. **Showing:** buyer intent, availability, booking/confirmation, CRM event, cancellation/reminder, outcome and race handling.
6. **Transaction:** offer/deal, legally reviewed documents, signatures/approvals where supported, deadline tracking, audit and closure.
7. **Erasure/offboarding:** records, derived AI content, embeddings, uploaded objects, queued operations, credentials, suppression retention and provider-specific copies.
8. **Agent or brokerage changes:** ownership reassignment, session revocation, provider/phone routes, historical audit preservation and strict tenant boundaries.
9. **Provider/database outage:** transparent `degraded` state, retries, queue recovery, no duplicate send/charge and no silent success.
10. **Whole-house capture:** original media retained, room/door connectivity verified, actual GPU budget, resumable uploads, measured iPhone/Android browser performance, usable mobile tour and photo fallback.

A successful HTTP 200 is **not** sufficient evidence that the client received an SMS, the right agent answered, the provider billed once, or a spatial tour works on a real phone.

## 5. Maintainability and module decomposition

High-coupling hotspots from the review (approximate line counts at the snapshot, not independent quality grades):

| Module | Approx. lines | Extract stable boundaries around |
| --- | ---: | --- |
| `backend/commands_api.py` | 3,504 | command creation, authorization, approvals, execution, provider callback, receipts |
| `backend/ai_chat_store.py` | 1,973 | persistence, tool registry/dispatch, domain mutations |
| `backend/crm.py` | 1,892 | client, lead, listing, property and transaction services |
| `backend/telephony_api.py` | 1,782 | provider webhooks, number setup, inbound/outbound routing |
| `backend/reconstruction_worker.py` | 1,574 | processing phases, independent quality checks, publishing |
| `backend/server.py` | 1,505 | composition/root wiring and lifecycle |
| `backend/auth.py` | 1,495 | identity, session, credential and recovery boundaries |
| `backend/privacy_lifecycle.py` | 1,389 | erasure, offboarding, revocation and retention receipts |

Prefer **incremental, contract-preserving extraction under integration tests**, not sweeping rewrites. Keep domain data shapes consistent between backend and Home/Work/Neoh frontend surfaces. Eliminate stale prototypes and obsolete instructions only after verifying they are unused.

## 6. Ordered Claude engineering backlog

*Owning prompts added 2026-10-12. Live status: [ledger](claude-prompts/LEDGER.md#audit-cross-reference).*

- [ ] **P0 — Sensitive plaintext:** map and remediate customer PII, messages, AI derived text and associated backups/logs; prove safe migration. *(A01, A06 → [prompt 17](claude-prompts/17-security-privacy-compliance-data-lifecycle.md))*
- [ ] **P0 — Production gate:** re-run staging checks and separately demonstrate live-service readiness, compliance, support, rollback, backup restore and provider configuration. *(A04 → [prompt 19](claude-prompts/19-infrastructure-deploy-observability-recovery.md))*
- [ ] **P0 — Truthful AI/spatial outcomes:** propagate unavailable/timed-out checks; prevent `complete`, `verified` or `professional` claims without the corresponding evidence. *(A02 → [prompt 11](claude-prompts/11-property-market-intelligence.md); A03 → [prompt 12](claude-prompts/12-neoh-space-browser-capture-reconstruction.md); A09 → [prompt 07](claude-prompts/07-ai-gateway-model-tool-governance.md))*
- [ ] **P1 — Canonical business event lifecycle:** separate received, requested, staged, approved, attempted, accepted, delivered, responded, completed and failed. Decide billing at the contracted event. *(A05 → [prompt 18](claude-prompts/18-brokerage-billing-seat-plans-analytics.md))*
- [ ] **P1 — Full customer-journey integration tests:** begin with lead intake → approval → provider delivery → CRM/audit/billing; expand to every journey in `4. *(no audit ID; the end-to-end journey is [prompt 20](claude-prompts/20-twenty-agent-full-feature-launch-e2e-evaluation.md))*
- [ ] **P1 — Provider/route race tests:** telephony, SMS, email and webhook reconciliation, account switching, duplicate delivery and outage recovery. *(A07 → [prompt 10](claude-prompts/10-communications-calendar-provider-integrations.md))*
- [ ] **P1 — Enforceable cost budgets:** tenant-level LLM tokens, telephony minutes, provider API usage, media generation and 3D GPU work; rate limiting and alerts. *(A10 → [prompt 16](claude-prompts/16-performance-indexes-mobile-cache.md) for capacity evidence; budgets themselves have no audit ID)*
- [ ] **P1 — AI/model evaluation:** grounded lead reasoning, valuations, hallucination/refusal rates, consent-safe behavior, voice call quality, real-data holdouts and license controls. *(A08 → [prompt 11](claude-prompts/11-property-market-intelligence.md); A09 → [prompt 07](claude-prompts/07-ai-gateway-model-tool-governance.md))*
- [ ] **P2 — Shipping vs experiment cleanup:** document reachability, assign owners, audit obsolete configs, prune dead code only with regression evidence. *(no audit ID; repository inventory is [prompt 01](claude-prompts/01-repository-source-audit.md))*
- [ ] **P2 — Incremental domain refactoring:** break hotspots along stable ownership boundaries after coverage exists. *(no audit ID; repository inventory is [prompt 01](claude-prompts/01-repository-source-audit.md))*

### “Definition of done” for every remediation

1. Trace code and configuration to the current commit; link **file:line** evidence and reproduction steps.
2. Record user/business impact, severity, affected tenants/data, implementation plan and operational/migration risk.
3. Add a **red test** for the defect and a positive/negative/edge test suite, including auth and replay/race behavior when relevant.
4. Implement the smallest safe change; preserve approval, RLS, idempotency, consent and rollback.
5. Pass targeted unit/integration checks plus appropriate real-Postgres/frontend/provider sandbox suites.
6. Check logs/metrics, migrations, runbooks, product copy and customer-facing status; document residual risks.
7. Verify production separately. No automatic promotion or claims of A+ based only on a green CI run.

## 7. Protocol for the unfinished every-line audit

To satisfy the original request to inspect **every line of every file**, process source in reviewable batches anchored to **one commit SHA**. Build a manifest of all tracked paths with size, language, purpose, shipping/reachable status and audit state (`unreviewed` / `read` / `verified by tests` / `requires runtime`). Record actual line ranges covered, reviewer findings, linked tests, owners and evidence. Read implementations, tests, migrations, infra and data/model artifacts, not just documentation. Reconcile line-level coverage to the manifest; do not use a Git-tree inventory as a substitute for line review. Recheck changed files and prior findings when branch head advances.

**Coverage not yet demonstrated:** 100% line-level examination, independent full-suite execution, every SQL migration, third-party dependency and model-weight provenance, real call/SMS/provider delivery, production security/load tests, full residential 3D field trial, and external compliance/legal review.

## 8. Recommended first implementation slice

Start with **AI property-lookup status fidelity** (A02, A06) because the failure mode is narrow and testable: add a typed checked/failed result, propagate `degraded`, sanitize warnings, and cover timeout/zero-match differentiation. In parallel, design the **PII migration plan** (A01) before making irreversible storage changes. For launch, prioritize the **production go/no-go evidence checklist** (A04) and one complete lead-to-delivery journey.

All findings require reconfirmation against the active branch before modification. The goal is truthful, testable outcomes across boundaries rather than another isolated feature or speculative readiness grade.
