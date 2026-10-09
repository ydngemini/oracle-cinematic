# Claude implementation prompt 04: CRM identity, lead intake, assignment, consent and lifecycle

> Working repository: `ydngemini/oracle-cinematic` (shipping FastAPI backend and React/Vite browser app).
> Scope: all-pages, fully functional Neoh for **one brokerage + 20 participating agents** (plus explicit owner/admin seats).
> This is an INSTRUCTION and acceptance specification, not a declaration of implemented features, existing provider permissions, tested production, or code already changed.

## Claude: your assignment

Act as principal engineer, security reviewer and implementation owner. Start by reading root `CLAUDE.md`, `docs/neoh-full-platform-source-audit-2026-10-08.md`, the [master prompt index](README.md), and relevant category specifications. Inspect current HEAD and reference actual source lines before assuming older audit findings still apply. Implement in bounded, reviewable slices. **Do not stop at outlining plans** when code/test work is possible. Preserve authentic, accessible product behavior, responsible budgets, explicit user consent, no unauthorized external side effects, reproducible tests and durable provider receipts.

**Initial source paths (verify current reachability):** backend/contacts_api.py; backend/contact_truth.py; backend/crm.py; backend/lead_routing_api.py; backend/speed_to_lead.py; backend/client_ai_automation.py; oracle-app/src/components/PeopleTab.jsx; oracle-app/src/components/ClientCrmTab.jsx; oracle-app/src/components/ContactIntakePanel.jsx; oracle-app/src/components/ClientTimeline.jsx; oracle-app/src/components/LeadRoutingPage.jsx

## Detailed engineering tasks

### Goal
One reliable customer relationship spanning website intake -> identity match -> lead qualification -> team assignment -> communications -> buyer/seller preferences -> showing -> deal -> results, with evidence, consent and brokerage-private scope.

### Steps
1. Inventory overlapping entities agent_contacts, clients, leads, site leads, SMS callers, deal participants and MLS-owner/property linkages. Diagram canonical IDs, external-provider IDs and legacy adapters; avoid creating a new customer each time a call or web form arrives.
2. Model canonical contact identity plus many roles and relationships: buyer, seller, homeowner, transaction party, agent owner, team, household/group, preferred channel, verified alias, opt-out status, current assignment and relationship history.
3. Implement explicit dedup rules with stable event/source identity: normalize phone (E.164 when valid), email (careful about semantics), address and external CRM IDs; allow uncertain collisions to remain separate pending human merge approval. Document reversible merge and preserved audit.
4. Validate lead ingestion signatures, timestamps, replay windows, IP/rate limit, idempotency key, input schema, duplicate callbacks, source licensing and tenant routing. Do not accept unsigned production webhooks or leak data in rejection responses.
5. Lead scoring separates observed/declared/inferred interest, data completeness, source freshness, actionability and contact consent. Historical high-scored leads without usable addresses are not actionable property candidates.
6. Routing must honor brokerage and team membership, capacity, ZIP/service region, skills/availability (when verified), explicit owner assignments, fairness constraints, current suspensions and reassignments. Fail to a visible review queue when impossible, not an invisible default agent.
7. Speed-to-lead: stage recommendations/communications with real consent and approval. Distinguish staged, approved, accepted by provider, delivered and responded. Do not bill a “lead engaged” on draft creation without a matching pricing contract.
8. Provide complete UI: contact intake, add/edit, import with dry-run and field mapping, assignment, share/revoke, duplicates, notes, timeline, tasks, segments, seller/buyer match, state changes, optimistic concurrency and explicit stale-data indicators.
9. Make contact deletion/export/account closure propagate into AI memory, search projections, vector records, queued jobs, analytics and provider connectors with preservation of legal retention requirements.
10. Audit background client_ai_automation.py: timeouts that return [] must be distinguished from real zero matches; reconciliation must report degraded/unknown when sources were unavailable.
11. Build CRM operation ledger: who changed a fact, whether verified, what source and time, undo/compensation support where possible, and exact outcome of external actions.
12. Protect against cross-agent access through list pagination, term search, client ID guessing, deep-linked sheets, caller route matching and background processing.

### Measurement
Time from first signed lead event to authorized routing, time to agent acknowledgment, human-approved message delivery, client responses, showing attendance, missing source rate, dedup false positives, assignment fairness and manually measured time saved. Report p50/p95; exclude drafts from “completed actions”.

### Acceptance
Seed one brokerage with 20 agents, overlapping client names and several million *synthetic* event references across tests. Verify no duplicates under race, no unauthorized routing, correct opt-out/consent, no provider acceptance mistaken for delivery, and all authorized CRM pages functional end-to-end.

## Required engineering workflow

1. Map concrete sources and call graph; document code paths, schema, external connections, permissions, failure states, test setup and observed baseline. Keep a table of **existing verified**, **proposed**, **implemented**, **tested** and **blocked**; never merge categories.
2. Implement one vertical slice including frontend, API, storage/RLS, provider/worker, customer-visible error/retry, tests and telemetry where appropriate. Use additive migrations and backwards-compatible API changes; preserve former route aliases and workflows.
3. Add unit and contract tests plus real PostgreSQL/RLS, React component, Playwright, concurrency, permission or live/sandbox provider testing as applicable. Do not contact real customers, send external communications, purchase services, deploy live or execute real financial transactions without explicit authorized approvals.
4. Run and report commands and exit codes, separate simulated and actual provider tests, measure p50/p95 where applicable, inspect logs for PII, and verify no regressed authorized page.
5. Commit focused code changes with changelog, migration notes, rollback/recovery instructions, exact acceptance evidence and remaining issues. Never use “done” or “A+” for untested paths; record external contractual, legal, licensing, operational or device blockers.

## Cross-cutting rules

- **All pages remain in launch scope**; honest setup-required gates are allowed when a customer must grant an external license/credential, but absent advertised functionality remains unfinished.
- **Hybrid brokerage access:** tenant boundary, agent assignments, explicit shares and team privileges must apply to DB, API, search, AI, jobs and browser caches.
- **Truthfulness:** missing facts, timed-out sources, non-verified 3D geometry and provisional model estimates are not verified outcomes.
- **Approval:** model suggestions are never authorization. Legal instruments, outreach, publishing, calendar writes, spend and high-impact changes require their established approvals.
- **Efficiency:** reuse FastAPI/Postgres/React/PlayCanvas and current job/tool architecture. Avoid duplicate frameworks or microservices unless benchmarks prove benefit.
- **Launch:** 20 agents concurrently plus owner/operator, actual iOS/Android browser tests, production-shaped deployment, cost and reliability measurement.

## Output format

Return a concise source-linked baseline, chosen vertical slice, completed files and migrations, tests run with results, measurable before/after where available, next dependent tasks, and blockers requiring human approvals. Update the master category matrix rather than duplicating contradictory backlogs.
