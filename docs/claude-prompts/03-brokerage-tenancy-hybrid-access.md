# 03 — Brokerage tenancy, teams, hybrid sharing and owner administration

> **Scope:** Turn the brokerage-wide tenant wall into the hybrid model in [C3.1](CONTRACT.md#c3-tenancy-roles-and-authorization): 20 agents, per-record assignment, explicit sharing, scoped team oversight and owner administration, enforced the same way in every layer. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01 · **Unblocks:** 04, 06, 07, 08, 10, 11, 12, 13, 17, 18, 20 · **Ledger:** [L03](LEDGER.md#l03) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Identity and context: `backend/tenancy.py` (`Role`, `TenantContext`, `apply_rls_context`, `require_role`), `backend/auth.py`
- Brokerage administration: `backend/brokerage_onboarding.py`, `backend/privacy_api.py` (`offboard`), `oracle-app/src/components/BrokerageSetupPanel.jsx`
- Records and routing: `backend/contacts_api.py`, `backend/crm.py`, `backend/lead_routing_api.py`, `backend/search_api.py`, `backend/ai_chat_store.py`
- Migrations: `0001_init_tenancy.sql`, `0002_listing_grants.sql`, `0019_client_enterprise.sql`, `0027_real_estate_intelligence_platform.sql`, `0036_private_ai_chat.sql`, `0054_agent_contacts_and_intake.sql`, `0059_contact_search_and_assignment_integrity.sql`, `0113_app_visible_tenants.sql`, `0120_platform_admin_is_a_role.sql`
- RLS tests: `backend/tests/rls_brokerage.sql`, `backend/tests/test_tenancy.py`, `backend/tests/test_rls_enforcement_guard.py`

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **There are three roles and no team-lead role.** `tenancy.py:71-74` `Role`: `platform_admin`, `broker_owner`, `agent`; `users.role` has the same CHECK (`0001_init_tenancy.sql:65`).
- **A request context carries tenant, role and a text agent id.** `tenancy.py:76-95` `TenantContext` (`agent_id: str`, `user_id` optional).
- **RLS context is three session settings.** `tenancy.py:167-196` `apply_rls_context` sets `app.current_tenant`, `app.current_role` and `app.current_agent`, or calls `app_begin_session` when the session must be re-checked.
- **Platform admin needs both the role setting and a database role membership.** `0120_platform_admin_is_a_role.sql:46-48` `app_is_platform_admin()` (`pg_has_role`).
- **Clients and leads are walled per brokerage, not per agent.** `0001_init_tenancy.sql:134-140` `clients_tenant_isolation` and `leads_tenant_isolation`: `app_is_platform_admin() OR tenant_id = app_current_tenant()`. `tenancy.py:113-115` `can_read_private` mirrors it.
- **Contacts and their intake tables use the same tenant-wide policy.** `0054_agent_contacts_and_intake.sql:299-319` (DO loop over `agent_contacts` and four related tables, FORCE RLS).
- **AI chat messages already use an agent-scoped policy.** `0036_private_ai_chat.sql:126-132` `ai_chat_messages_tenant_isolation` adds `user_id = app_current_agent()`.
- **Assignments are text agent ids, not user UUIDs.** `agent_contacts.assigned_agent_id text` (`0054_agent_contacts_and_intake.sql:12`); `clients.assignee_id text` (`0019_client_enterprise.sql:17`); `users.id` is the UUID and `users.agent_id` the text key (`0001_init_tenancy.sql:62-64`).
- **Assignment input is matched case-insensitively to `users.agent_id`.** `contacts_api.py:161-175` `_canonical_assignee` (`lower(agent_id)=lower($2)`).
- **The client list filters by assignee only when the caller asks.** `crm.py:702-715` `list_clients` (optional `assignee` filter); contact search has no assignment predicate (`contacts_api.py:117-141` `search_contact_rows`).
- **Teams are one free-text name per user.** `0027_real_estate_intelligence_platform.sql:610-628` `team_memberships`: `team_name text`, `member_role` in agent/team_lead/broker, `UNIQUE (tenant_id,user_id)`. No backend Python outside tests mentions `team_lead` (`grep -rn team_lead backend --include=*.py | grep -v tests`).
- **The only grant table is tenant-to-tenant listing sharing.** `0002_listing_grants.sql:14` `listing_grants`; no per-user or per-record share table exists (grep of `CREATE TABLE` names in migrations).
- **Owner actions are gated to broker owner or platform admin.** `brokerage_onboarding.py:659-765` (invitations) and `:812`, `:885` (suspend, reinstate) call `require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)`.
- **Suspension deactivates the account and bumps its session epoch.** `brokerage_onboarding.py:812-830` `suspend_member` (docstring: records stay with the brokerage).
- **Most policies still use the OR form, which defeats tenant indexes.** `0113_app_visible_tenants.sql:1-21` (comment: "Rewriting 123 isolation predicates" left for review) and `app_visible_tenants()` (`:24-27`).
- **CI runs the brokerage RLS test as the application role.** `.github/workflows/ci.yml:233` runs `rls_brokerage.sql`, which does `SET LOCAL ROLE oracle_app` (`rls_brokerage.sql:35`).

## 3. Goal

One brokerage with 20 named agents works the way [C3.1](CONTRACT.md#c3-tenancy-roles-and-authorization) describes. Each agent sees assigned records, records explicitly shared with them, and approved brokerage knowledge. Team leads see their team. The owner administers the brokerage and its aggregate reporting without reading agents' private memories. Every layer makes the same decision.

## 4. Engineering tasks

- **T03.01 Inventory roles and identifiers.** Map `Role`, `team_memberships`, `users.id` versus `users.agent_id`, and every column, tool and job that stores an agent reference. Do not add a competing user or team store.
- **T03.02 Add stable team IDs and memberships.** Keep migration compatibility with `team_name`. Decide (owner question) whether a user may belong to more than one team.
- **T03.03 Canonicalize assignments onto the user UUID.** Cover `agent_contacts.assigned_agent_id`, `clients.assignee_id`, AI tools and lead routing. Add backward-compatible adapters and reject string or UUID mismatches.
- **T03.04 Build one policy decision service.** Inputs: actor, tenant, active user, action, resource kind and id, permission revision, membership, assignment, team scope, explicit share, field sensitivity and consent. Output: allow or deny plus a private reason code for audit. Unknown scope is denied.
- **T03.05 Add resource grants.** Per-user and per-team read, edit, collaborate and manage rights, with expiry, revocation, grantor, delegated authority, resource id, version and immutable audit. A grantor cannot exceed their own rights or make a private record brokerage-wide by mistake.
- **T03.06 Enforce agent scope in RLS together with API guards.** Cover `agent_contacts`, `clients` and their notes, communications, deals and media. Test policy composition as the application role with live session settings.
- **T03.07 Apply the same decision everywhere.** Search, AI chat store and tool resolver, documents and media, signed URLs, export, caches and embeddings, outbox workers, telephony, calendar and notifications. A denial never reveals that the record exists.
- **T03.08 Make reassignment and suspension durable.** Check the successor's membership and consent; change responsibility in one transaction; emit outbox events; invalidate caches; revoke grants as policy says; re-authorize or cancel pending calls, messages and missions; keep history. Suspension blocks live sessions and unsent work.
- **T03.09 Build the owner workspace for 20 agents.** Batch invitations and acceptance, teams and leads, assignment workload, agent status, sharing, seats and billing (with 18), autonomy permissions, integration health, audit and support.
- **T03.10 Add exceptional owner access to private data.** Short-lived, reason-coded, visibly audited, and only where lawful.
- **T03.11 Separate brokerage knowledge from agent-private memory.** Approved policies, templates, listings and integrations are shared; agent preferences and transcripts stay private. No universally visible vector store.
- **T03.12 Review privacy and consent for sharing paths.** Caller ID, downloaded client files, shared notes and agent offboarding. Imported contacts are never promoted to brokerage-wide silently.
- **T03.13 Load-test the access model.** Assignment changes, many grants, pagination, overlapping teams, and RLS query plans with no tenant-wide sequential scans.
- **T03.14 Verify whether any API layer limits an agent to assigned records today.** Start from `list_clients`, `search_contact_rows`, search and AI tools.
- **T03.15 Verify whether `can_read_private` and the other Python mirrors still match the policies.** `tenancy.py:106-109` says they must stay in lockstep.
- **T03.16 Verify whether suspension cancels queued work.** Calls, messages, missions and nurture jobs owned by the suspended user.

## 5. Failure and adversarial cases

- **F03.01 Permissive policies OR together.** A narrower new policy is added, but the old tenant-wide policy still grants the row.
- **F03.02 Grant escalation.** A grantor shares more than they hold, or turns a private record brokerage-wide.
- **F03.03 Identity mismatch.** Case or format differences between text agent ids and user UUIDs route a record to the wrong person.
- **F03.04 Existence leak.** A deny response, count or timing reveals that a record exists.
- **F03.05 Suspended user keeps acting.** A suspended or offboarded agent keeps a live session, or their queued call, message or mission still runs.
- **F03.06 Stale cache.** A cache, search index or embedding still serves a record after revocation or reassignment.
- **F03.07 Reassignment race.** Reassignment happens while a side effect is being delivered.
- **F03.08 Owner overreach.** An owner reads private agent memory or transcripts without reason-coded access.
- **F03.09 Expired authority honoured.** An expired grant or a revoked provider credential still works.
- **F03.10 Tenant-wide scans.** An agent-scoped query falls back to scanning every tenant's rows.

## 6. Acceptance criteria

- **AC03.01 The actor matrix passes as the application role.** Actors: agent A (assigned), agent B (unassigned), agent C (shared), team lead inside and outside the team, owner, platform admin, another brokerage. Operations: read, write, search, AI, tool, media, export, cached response, queued action. Evidence: `backend/tests/test_brokerage_access_matrix_live.py` (proposed) on real PostgreSQL. Covers: T03.04, T03.06, T03.07, T03.14, F03.01, F03.04.
- **AC03.02 Policy composition is proven.** A test reads `pg_policies` for each scoped table and asserts no permissive policy is wider than intended; the Python mirrors match. Evidence: SQL test run by CI next to `rls_brokerage.sql`. Covers: T03.06, T03.15, F03.01.
- **AC03.03 There is one canonical identity.** Assignment columns reference `users.id`; mismatched strings are rejected; adapters keep old callers working. Evidence: migration plus contract tests. Covers: T03.01, T03.03, F03.03.
- **AC03.04 Teams have stable IDs and scoped leads.** A team lead sees only their team's records. Evidence: live RLS test. Covers: T03.02.
- **AC03.05 Grants cannot escalate or outlive expiry.** Evidence: negative tests for escalation, expiry, revocation and audit. Covers: T03.05, F03.02, F03.09.
- **AC03.06 Reassignment and suspension are durable.** Pending work is re-authorized or cancelled, caches are invalidated, and a race with delivery does not double-send. Evidence: concurrency and failure-injection tests. Covers: T03.08, T03.16, F03.05, F03.06, F03.07.
- **AC03.07 The owner can run 20 agents.** 20 real invitations (target, not measured) are issued and accepted; exceptional access is reason-coded and visible to the agent. Evidence: Playwright owner journey plus audit-row assertions. Covers: T03.09, T03.10, F03.08.
- **AC03.08 Private memory stays private.** Brokerage retrieval never returns agent-private memories or transcripts, and imports are not made brokerage-wide. Evidence: retrieval tests with private and shared fixtures. Covers: T03.11, T03.12.
- **AC03.09 Plans stay indexed.** `EXPLAIN` as the application role shows index use and no tenant-wide sequential scan at realistic volume. Evidence: committed plan check with its data volume. Covers: T03.13, F03.10.

## 7. Out of scope, owner decisions, category-specific rules

- **Permissive policies OR together.** Adding a narrower policy does not narrow access while a broad policy remains. Replace or restrict the broad policy, then prove it.
- **Default deny.** An unknown resource kind or scope is denied, never allowed.
- **No second identity system.** Extend `users` and `team_memberships`; do not add a parallel user or team database.
- **Legacy MLS entitlement routes are out of scope** (D-2026-10-12-01): `brokerage_onboarding.py:923-985`. Gating them belongs to prompt 11.
- **Owner questions:** may an agent belong to several teams; may a team lead edit, or only read, team records.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the policy decision service with its reason codes;
  - migrations for team IDs, grants and UUID assignment, with rollback notes;
  - the actor matrix test and the policy-composition test;
  - the owner workspace;
  - the security and test matrix, linked from L03.
- **First slice:** read-only T03.01 + T03.14 + T03.15 alongside 02's matrix (README). Then T03.03 + AC03.03, which every sharing feature builds on.
- **Ledger:** add L03-01 onward per slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
