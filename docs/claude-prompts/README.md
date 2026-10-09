# Neoh: 20 category-specific Claude implementation prompts

> Master source-of-truth for **planning category work** across the full Neoh SaaS, on `ydngemini/oracle-cinematic`. Prepared for **one brokerage and 20 authorized agents** (plus explicitly modeled broker owner/platform operator), **all entitled pages** and **all promised product capabilities**. Prompts specify engineering tasks, not existing implementation, signed legal approval, available provider accounts or certification.

## How Claude should execute

**Use ONE category prompt per coding session/PR or a small tightly coupled vertical slice.** Do not feed 20 full prompts into a single coding model context and ask it to rewrite the whole repo. Read `CLAUDE.md`, this index, the selected prompt, its linked special-purpose specs, **current** code, migrations, tests and relevant docs; pin the base SHA. Produce a source-linked baseline before editing. Implement changes in manageable reviewed commits, using the P0 dependencies below and tests, not aspirational “feature complete” summaries.

**Primary release contract:** Brokerage is a tenant with 20 agent accounts (owner/admin inclusion determined by actual seat contract), individual client assignments, explicit record sharing, authorized team manager oversight and owner administration. The browser app's Home/Work/Neoh shell can remain quiet; **every** authorized nested screen, entity sheet, configuration workspace and external link must be discoverable, reachable, functional and thoroughly tested. Do not strip out promised features to shorten the pilot. A page that only displays a button or status ledger is not finished. Licensed/provider features require legitimate provider enrollment and must report setup needed honestly; do not spoof a connection.

**Cross-document implementation discipline:** preserve tenant and same-brokerage agent isolation, PostgreSQL RLS, source-backed evidence, licensing/privacy, consent, human approval of side effects, idempotency, source and provider status, safe spending limits and audit receipts. Neoh Space stays browser-only and reuses PlayCanvas; the avatar rig is an optional enhancement to current SVG fallback until built and verified. Reuse existing FastAPI/React/Vite/Postgres/gateway/jobs instead of multiplying architectures. Historical CI/staging and mock benchmarks are not production proof.

## Twenty independent category prompts

| Category | Implementation brief | Concrete deliverable |
|---|---|---|
| 01 | [Repository architecture & every-line source audit](01-repository-source-audit.md) | Inventory the full codebase, trace reachable code and migrations, build a reproducible issue/evidence ledger. |
| 02 | [All pages, routes, accessibility and responsive mobile](02-all-pages-mobile-ui.md) | No reduced MVP: every authorized page, nested workspace and public link must be discoverable, implemented and tested. |
| 03 | [Brokerage tenancy, teams, hybrid permissions & owner console](03-brokerage-tenancy-hybrid-access.md) | One tenant with 20 agents, assigned and shared records, team oversight, safe owner administration and real RLS. |
| 04 | [CRM, contacts, lead routing & relationships](04-crm-contact-leads-relationships.md) | Canonical customer identity, routing, consent, assignments, activities, full record lifecycle and zero false engagement. |
| 05 | [Semantic intent & contextual understanding](05-semantic-language-intent.md) | Understand goals, paraphrases, references, ambiguity, negation and constraints beyond keyword regex. |
| 06 | [Evidence retrieval, hybrid search & memory](06-evidence-retrieval-memory.md) | Authorized sources, provenance, contradictory facts, temporal validity, task-specific context and cache lifecycle. |
| 07 | [AI gateway, model routing & governed tools](07-ai-gateway-model-tool-governance.md) | Typed capabilities, budgeted tool selection, policy validation, actual receipts and safe model execution. |
| 08 | [Durable missions, task graphs & autonomy](08-durable-missions-workflow-orchestration.md) | Executable plan graph, approvals, dependencies, retries, job leasing, compensation and verified outcomes. |
| 09 | [Real-time speech, voice latency & interruption](09-realtime-voice-speech-latency.md) | Streaming voice, turn detection, grounded sidecar, barge-in, live-provider proof and accurate audio state. |
| 10 | [Communications, calendars & showings](10-communications-calendar-provider-integrations.md) | Email/SMS/carriers, signatures, consent, delivery, verified appointments and provider failover. |
| 11 | [Property, MLS, market and valuation intelligence](11-property-mls-market-intelligence.md) | Licensed property truth, source freshness, buyer matches, factual market evidence and indexable search. |
| 12 | [Neoh Space browser capture, photoreal 3D & tours](12-neoh-space-browser-capture-reconstruction.md) | All four web-only spatial workstreams, real-house field metrics, durable reconstruction and device quality. |
| 13 | [Deals, documents, contracts & compliance](13-transactions-contract-vault-compliance.md) | Transactions and milestones, secure contract vault, templates, approvals, portfolio and truthful financial status. |
| 14 | [Sites, social, paid ads, SEO & client experience](14-websites-social-ads-growth-client-portal.md) | Finish promised publishing and integration workflows; never label disconnected features live. |
| 15 | [Neoh mascot, skeletal mesh & contextual animation](15-neoh-avatar-skeletal-character-animation.md) | 3D rig, behavior controller, expressive stable transitions, voice sync, reduced-motion and SVG fallback. |
| 16 | [Loading speed, PostgreSQL indexes & cache](16-performance-indexes-mobile-cache.md) | Mobile code splitting, authorized query plans, hybrid search, tiered invalidation, real-world latency evidence. |
| 17 | [Security, privacy, compliance & retention](17-security-privacy-compliance-data-lifecycle.md) | Cross-agent RLS, PII encryption, consent, DPA/counsel, media/cache revoke, audits and incident readiness. |
| 18 | [Brokerage billing, 20 seats & business intelligence](18-brokerage-billing-seat-plans-analytics.md) | Replace solo plan assumptions; live-priced entitlements, owner usage budget and honest pilot ROI measures. |
| 19 | [Production operations, CI/CD, resilience & observability](19-infrastructure-deploy-observability-recovery.md) | Actually verify production, secrets, recovery, backups, rollback, provider alerts and realistic concurrency. |
| 20 | [Full-feature 20-agent launch certification](20-twenty-agent-full-feature-launch-e2e-evaluation.md) | All-pages matrix, E2E role/provider/device tests, launch gates, pilot rehearsal and competitive proof. |

## Coordinated build order and dependencies

1. **P0 inventory and release truth:** 01 source audit + 02 all-pages routing and capability matrix; verify 20 category coverage and current main SHA. Separate existing implemented, partially implemented, configured-but-unavailable, and not built.
2. **P0 trust/permissions:** 17 privacy/security + 03 canonical brokerage/agent/team ACL + 04 CRM identity migrations. Validate actual Postgres roles/policies and side-effect authorization before expanding retrieval or sharing.
3. **P0 business deployment and money:** 18 subscription and 20-seat contract + 19 real production infrastructure/operations. Human operator must configure Stripe, legal documents, licensed MLS, providers and support; never auto-create accounts or charge.
4. **P1 high-value intelligence:** 06 evidence/memory, 05 semantic intent, 07 tool/model gateway, 08 durable missions. These must share IDs, facts, consent, permission revision and status semantics. Test one meaningful request from intent through completed receipt.
5. **P1 revenue workflows:** 10 communications/calendars/showings, 09 live voice, 11 properties/MLS, 13 transactions, 14 sites/social/marketing. Select verified vertical slices spanning multiple prompts, avoid isolated integrations.
6. **P1/P2 experiential strength:** 16 performance and query/index optimization throughout; 12 Neoh Space real-house proof and 15 mascot skeletal/behavioral renderer with graceful fallbacks and actual browser-device tests.
7. **Release gate:** 20 full-feature test/certification must close every promised page and workflow and reconcile current production and legal provider signoffs. Iterate release artifacts, rather than one massive feature-branch merge.

## Existing specs and evidence, not to replace

- [Full platform source audit (2026-10-08)](../neoh-full-platform-source-audit-2026-10-08.md): source-backed findings; it openly states line-by-line review is unfinished.
- [Neoh Space browser-only capture/reconstruction specification](../neoh-space-web-capture-claude.md): four approved spatial workstreams; detailed capture algorithms, performance and certification contract.
- [Neoh avatar animator/Rive handoff](../neoh-avatar-rive-spec.md): existing SVG-to-Rive state and artboards; do not mistake the renderer stub for a delivered rig.
- [Brokerage go-live checklist](../brokerage-go-live-checklist.md) and [first brokerage rollout](../first-brokerage-launch.md): current operational sequences, to expand for the 20-agent full-feature goal.
- [Security launch gate](../security-launch-gate.json), [privacy data map](../privacy-data-map.md) and [capacity plan](../capacity-plan.md): dated evidence with actual limitations, not unconditional certification.
- [Launch readiness results](../launch-readiness/README.md): older snapshots requiring fresh production checks.

## Required category status ledger

Claude should maintain a structured launch ledger rather than editing each doc to claim victory. For every deliverable record category, owner, change SHA, tested files, affected routes/API/jobs, authorization matrix, current status (`not_inspected`, `source_verified`, `in_progress`, `implemented_untested`, `tests_pass`, `external_configuration_required`, `blocked`, `production_verified`), evidence links, test results, measured resource use and next task. Do not treat an empty status as pass.

All new and existing pages must be validated for **role, route, loading, empty, degraded, failure, genuine outcome, phone/mobile layout and performance**. Provider-dependent “green” requires a real authorized integration or approved test where production status is independently checked.

## Immediate instruction for Claude

Start with [02: all-pages and responsive mobile](02-all-pages-mobile-ui.md) to generate an exhaustive current route/component/API/role matrix, in parallel with read-only inspection of [03: hybrid brokerage access](03-brokerage-tenancy-hybrid-access.md), [17: security/privacy](17-security-privacy-compliance-data-lifecycle.md) and [18: brokerage billing and seats](18-brokerage-billing-seat-plans-analytics.md). **Do not make a production deployment, purchase services or contact real customers without explicit human approval.** Then implement the first safe P0 slice with tests, and update the shared ledger.
