# Claude implementation prompt 03: One brokerage, twenty agents, sharing, teams and owner administration

> Working repository: `ydngemini/oracle-cinematic` (shipping FastAPI backend and React/Vite browser app).
> Scope: all-pages, fully functional Neoh for **one brokerage + 20 participating agents** (plus explicit owner/admin seats).
> This is an INSTRUCTION and acceptance specification, not a declaration of implemented features, existing provider permissions, tested production, or code already changed.

## Claude: your assignment

Act as principal engineer, security reviewer and implementation owner. Start by reading root `CLAUDE.md`, `docs/neoh-full-platform-source-audit-2026-10-08.md`, the [master prompt index](README.md), and relevant category specifications. Inspect current HEAD and reference actual source lines before assuming older audit findings still apply. Implement in bounded, reviewable slices. **Do not stop at outlining plans** when code/test work is possible. Preserve authentic, accessible product behavior, responsible budgets, explicit user consent, no unauthorized external side effects, reproducible tests and durable provider receipts.

**Initial source paths (verify current reachability):** backend/tenancy.py; backend/auth.py; backend/brokerage_onboarding.py; backend/contacts_api.py; backend/crm.py; backend/lead_routing_api.py; backend/db/migrations/0027_real_estate_intelligence_platform.sql; backend/db/migrations/0054_agent_contacts_and_intake.sql; backend/db/migrations/0059_contact_search_and_assignment_integrity.sql; oracle-app/src/components/BrokerageSetupPanel.jsx

## Detailed engineering tasks

### Contract
One brokerage tenant; 20 named agent accounts and a separately counted owner seat when needed. User sees assigned clients, explicitly shared resources and approved organization listings/knowledge. Team leads have scoped oversight. Owner gets organization controls and aggregate reporting without unrestricted private agent memories. Platform admin is different from owner.

### Implement in order
1. Inventory actual auth roles (agent, broker_owner, platform_admin) and team_memberships semantics, and existing users.id versus agent_id identifiers. Do not bolt on a competing user/team database. Introduce stable team ID and membership relations with existing migration compatibility.
2. Canonicalize assignments onto immutable user UUID. Current agent_contacts.assigned_agent_id and clients.assignee_id plus AI tools and lead routing may use different representations. Add backwards compatible adapters and prohibit string/UUID mismatch.
3. Build a policy decision service for actor, tenant, active user, action, resource kind/id, permission revision, membership, assignment, team scope, explicit share, field sensitivity and consent. Result must include allow/deny and private reason code for audit. Default deny for unknown scope.
4. Resource grants: per-user/per-team read/edit/collaborate/manage rights, expiration, revocation, grantor, delegated authority, resource ID, version, immutable audit. Grantor cannot exceed own rights or convert private data to brokerage-wide by mistake. Protect privilege escalation/race cases.
5. Enforce Postgres RLS on agent_contacts and clients and related notes/communications/deals/media in conjunction with API guards. **IMPORTANT:** old policies grant tenant-level access and permissive RLS policies OR together; do not assume adding a narrower policy restricts broad old SELECT. Test policy composition with app DB role and live current session variables.
6. Apply same authorization to search_api, ai_chat_store/tool resolver, documents/media, signed URLs, export, caches/embeddings, outbox workers, telephony, calendar and notifications. API must return safe inaccessible status without leaking record existence.
7. Reassignment: validate successor membership and consent, transactionally change authoritative responsibility, produce outbox events, invalidate caches, revoke grants as policy requires, reauthorize or cancel pending calls/messages/missions, retain audit and history. Suspension must block live sessions and unsent work.
8. Owner workspace for 20 agents: invitation batch and acceptance, teams and team leads, assignment workload, agent status, explicit sharing, billing/seats, autonomy permissions, integration health, audit and support. Owner-level exceptional private access requires short-lived reason-coded privilege and visible audit where lawful.
9. Brokerage-shared knowledge includes approved policies, templates, listings and integrations; agent-private preferences and transcripts remain separately scoped. Do not combine memories into a universally visible vector store.
10. Run privacy/consent reviews for caller ID, downloaded client files, shared notes and agent offboarding. Never silently promote imported contacts to brokerage-public if legacy ACL semantics were broad.
11. Load-test assignment changes, many grants, pagination, overlapping team membership and RLS query plans to prevent tenant-wide sequential scans.

### Required tests
Agent A assigned; Agent B unassigned; Agent C explicitly shared; team lead in/outside team; owner; platform admin; separate brokerage. Exercise read/write/search/AI/tool/media/export/cached response/queued action. Negative tests for grant privilege escalation, suspended user, expired grants, revoked provider credential, stale cache and race with side-effect delivery. RLS runs as app DB role. No unintentional cross-agent/cross-tenant disclosures.

### Definition of done
20 real invitations supported, owner controls working, clear assignee responsibilities, all cross-layer access decisions match, reassignment is durable, no broad RLS bypass, no duplicated canonical identity and a reproducible security/test matrix.

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
