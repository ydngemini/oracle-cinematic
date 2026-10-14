# Neoh launch ledger: the single source of status

> This file is the only place a category's status lives. Prompts carry dated baselines, never status ([CONTRACT C10](CONTRACT.md#c10-ledger-update-rule)). Update your rows **in the same commit as the change**. Evidence is append-only: add new evidence with its date, and never rewrite history.
>
> Seeded 2026-10-12 from evidence checked that day. Every unseeded deliverable is `not_inspected`. An empty or missing row is never a pass.

## How to update

1. Find your category below (`### LNN`). Add or edit its deliverable rows (`#### LNN-xx`).
2. Change `status` only when its entry criterion below is met, and say which environment (`env`) the evidence comes from.
3. Add the commit SHA, the files, and the tests you **ran** (command, result, date) to the row's YAML block.
4. Recompute the category's **rollup** in the summary table. The rollup is its weakest row, in the order below. Rows that are `external_configuration_required` or `blocked` are shown next to the rollup, not averaged away.
5. If you create an artifact (an all-pages matrix, an inventory CSV, a benchmark report), link it under the category. Do not start a second backlog anywhere else.

## Status values

Listed weakest to strongest:

| Status | Entry criterion |
|---|---|
| `not_inspected` | Nobody has checked this against the source at a pinned SHA. |
| `source_verified` | Current behaviour (or its absence) is documented with `path:line` at a SHA. No change made. |
| `in_progress` | A change is underway on a named branch. |
| `implemented_untested` | Code is committed, but its tests have not been run, are failing, or don't exist yet. |
| `tests_pass` | The named tests ran with exit 0 in the stated `env`. The command and result are recorded. |
| `external_configuration_required` | Blocked only on something an operator or customer must supply (a credential, registration, licence or account). |
| `blocked` | Blocked on anything else: a defect, an owner decision, a dependency. The blocker is named. |
| `production_verified` | Proven on a production environment with a real outcome. **Only the owner sets this.** |

**`env`** values: `local`, `ci`, `staging`, `production`. A row reading `tests_pass` / `staging` means staging only. It is never production evidence ([C12](CONTRACT.md#c12-banned-claims)).

## Category summary

| Cat | Category | Prompt | Rollup | Env high-water | Last evidence | Open blockers | Next task |
|---|---|---|---|---|---|---|---|
| 01 | Repository source audit | [01](01-repository-source-audit.md) | `not_inspected` | — | — | — | Run the 01 inventory |
| 02 | All pages, routes, mobile UI | [02](02-all-pages-mobile-ui.md) | `source_verified` | staging | `90372f2` crawl | — | Per-page verdicts |
| 03 | Brokerage tenancy and hybrid access | [03](03-brokerage-tenancy-hybrid-access.md) | `source_verified` | staging | 0131 (L03-02) staging, 0134 (L03-04) local | Owner: team leads, contact-vs-client assignee drift (L03-03, L03-04) | Deploy 0131+0134; browser check; then the communications tables |
| 04 | CRM, contacts, leads, relationships | [04](04-crm-contact-leads-relationships.md) | `tests_pass` | local | `a8bff54` (L04-01) | — | T04.01 + T04.13 (L04-01 next) |
| 05 | Semantic intent | [05](05-semantic-language-intent.md) | `source_verified` | local | `745f2fa` | — | L05-01 |
| 06 | Evidence retrieval and memory | [06](06-evidence-retrieval-memory.md) | `source_verified` | local | `745f2fa` | A01, A02 | L06-01 |
| 07 | AI gateway and governed tools | [07](07-ai-gateway-model-tool-governance.md) | `tests_pass` | local | `071d65d` (L07-01) | Playwright check owed (L07-01); owner: archive_client | T07.02 + AC07.01 |
| 08 | Durable missions and workflows | [08](08-durable-missions-workflow-orchestration.md) | `source_verified` | local | `745f2fa` | PR #11 review; grant-release decision | L08-01 |
| 09 | Real-time voice | [09](09-realtime-voice-speech-latency.md) | `source_verified` | staging | `9c3463a` | Call-note broadcast (L09-03); phone check (L09-02) | L09-03 |
| 10 | Communications and calendars | [10](10-communications-calendar-provider-integrations.md) | `source_verified` | staging | `75c6ba6` (local) | Google OAuth client; 10DLC; deploy approval; A07 fixed locally, 0136 undeployed (L10-06) | L10-01 |
| 11 | Property and market intelligence (non-MLS) | [11](11-property-market-intelligence.md) | `tests_pass` | staging (L11-01), local (L11-02) | `90372f2`; `d934b2e` (L11-02) | Deploy 0135; live-DB test owed; staging catalog empty | Apply 0135 on staging; then T11.02 provenance |
| 12 | Neoh Space capture, 3D, tours | [12](12-neoh-space-browser-capture-reconstruction.md) | `source_verified` | local | `745f2fa` | A03 | Capture Studio first slice |
| 13 | Deals, documents, compliance | [13](13-transactions-contract-vault-compliance.md) | `source_verified` | local | `745f2fa` | Owner: e-sign, commissions | L13-01 |
| 14 | Sites, social, ads, client portal | [14](14-websites-social-ads-growth-client-portal.md) | `source_verified` | local | `745f2fa` | Owner: networks, listings on sites | L14-01 |
| 15 | Neoh mascot animation | [15](15-neoh-avatar-skeletal-character-animation.md) | `source_verified` | local | `745f2fa` | Owner: rig author | L15-01 |
| 16 | Performance, indexes, cache | [16](16-performance-indexes-mobile-cache.md) | `source_verified` | local | `745f2fa` | A10; no production target | L16-01 |
| 17 | Security, privacy, data lifecycle | [17](17-security-privacy-compliance-data-lifecycle.md) | `source_verified` | staging | `0739563` | A01, A06 | Read-only review with 02 |
| 18 | Billing, seats, analytics | [18](18-brokerage-billing-seat-plans-analytics.md) | `source_verified` | local | `a8bff54` (L18-02 `tests_pass`) | D-2026-10-12-02 (billable stage) | T18.04 + AC18.02 (seats) |
| 19 | Production operations | [19](19-infrastructure-deploy-observability-recovery.md) | `source_verified` | staging | `745f2fa` | No production app; A04 | L19-03 |
| 20 | 20-agent launch certification | [20](20-twenty-agent-full-feature-launch-e2e-evaluation.md) | `source_verified` | local | `745f2fa` | Every row above | Last |

Rollup is the weakest row. A category holding `external_configuration_required` or `blocked` rows shows them under "Open blockers" instead of hiding them in an average (L09 and L10 also hold `tests_pass` rows; see below).

`production_verified` count: **0**.

## Categories and deliverables

### L01
Repository source audit. Artifacts it will own: the file-level inventory (`docs/audit/repo-inventory-<sha>.csv`, proposed). No rows yet.

### L02
All pages, routes and mobile UI. Artifacts: [ALL-PAGES-MATRIX.md](ALL-PAGES-MATRIX.md) (generated, 52 pages) with its JSON twin [all-pages.json](all-pages.json) and hand-kept verdicts in [all-pages-verdicts.json](all-pages-verdicts.json).

#### L02-01 Route and page inventory baseline
```yaml
id: L02-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [oracle-app/src/routes.js, oracle-app/src/App.jsx, oracle-app/src/components/OurAITab.jsx, oracle-app/src/components/CrmShell.jsx, scripts/audit-neoh-production.py]
tests_run:
  - {cmd: "grep/sed of the route registries", result: "3 top views, 13 Work types, 8 AI workspaces, 5 Sales, 5 Deals, 6 Properties views (incl. legacy mls), 5 public routes; the production audit script covers 3 views + 5 Work types only; the Admin tab is gated client-side only (CrmShell.jsx:238-241)", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T02.01: build ALL-PAGES-MATRIX.md"
updated: 2026-10-12
```

#### L02-02 The route, component, API and role matrix exists and cannot go stale
```yaml
id: L02-02
status: tests_pass
env: local
evidence_shas: [58b2f63]   # base; matrix in the commit that adds build-all-pages-matrix.mjs
files: [oracle-app/scripts/build-all-pages-matrix.mjs, oracle-app/src/allPagesMatrix.test.js, docs/claude-prompts/ALL-PAGES-MATRIX.md, docs/claude-prompts/all-pages.json, docs/claude-prompts/all-pages-verdicts.json]
tests_run:
  - {cmd: "npm run pages:matrix", result: "52 pages from 9 registries (3 views, 13 Work types, 3 entity sheets, 8 AI workspaces, 5 Sales, 5 Deals, 6 Properties incl. legacy mls, 3 profile views, 6 routes outside the shell); every component file resolved", date: 2026-10-13}
  - {cmd: "vitest src/allPagesMatrix.test.js", result: "4 passed; a deliberately removed row (sales:providers) made it fail, then restored", date: 2026-10-13}
authz_checked: roles recorded per page; server-side role checks not yet inspected (T02.14)
blockers: []
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T02.13/T02.14 (public-route token checks, Admin API role checks), then per-page verdicts via the crawl (T02.11)."
updated: 2026-10-13
```

#### L02-03 Public routes check their tokens; Admin APIs check the role on the server
```yaml
id: L02-03
status: tests_pass
env: staging
evidence_shas: [58b2f63]
files: [backend/admin_ops.py, backend/client_portal.py, backend/auth.py, backend/property_view_api.py, backend/public_sites_api.py]
tests_run:
  - {cmd: "GET each /api/admin/* the Admin tab calls, signed in as the demo agent and the demo owner (staging)", result: "agent: 8/8 Admin routes 403; owner: 7/8 403 and /api/admin/anomalies 200 by design (require_broker_owner_or_admin, FORCE RLS on audit_anomaly_alerts keeps it to the owner's brokerage); /api/market/coverage 200 to both (national coverage data, not tenant data)", date: 2026-10-13}
  - {cmd: "GET the public routes with a fake token or none (staging)", result: "property-upload 404 'no longer valid'; portal/session 404; portal/dossier 401 'Missing portal session'; auth/invitation 404; public sites 404: no data without a valid token", date: 2026-10-13}
authz_checked: server-side, observed on staging
blockers: [{type: scope, note: "POST variants were not exercised (to avoid side effects); covered by existing backend tests."}]
audit_refs: []
decision_refs: []
next_task: "T02.11 crawl with safe fixtures; per-page verdicts into all-pages-verdicts.json."
updated: 2026-10-13
```

#### L02-04 Ordinary browsing no longer trips the chat rate limit (found by the crawl)
```yaml
id: L02-04
status: tests_pass
env: local
evidence_shas: [129176c]   # found on staging; fix in the commit that adds _read_uses_general_bucket
files: [backend/rate_limit_middleware.py, backend/tests/test_rate_limit_buckets.py, scripts/crawl-all-pages.py]
tests_run:
  - {cmd: "scripts/crawl-all-pages.py --only <8 pages> (staging, owner+agent, desktop+phone)", result: "25 x 429 on GET /api/ai/chat/status: every page polls it and the 20/min /api/ai/chat prefix bucket counted it", date: 2026-10-13}
  - {cmd: "pytest backend/tests/test_rate_limit_buckets.py + security tests", result: "113 passed: signed-in GET/HEAD under /api/ai/chat use the 600/min general bucket; writes keep 20/min; chat turns keep the per-agent limiter in rate_limiter", date: 2026-10-13}
authz_checked: anonymous callers unchanged (IP identity, 20/min)
blockers: [{type: verification, note: "Re-crawl on staging after deploy."}]
audit_refs: []
decision_refs: []
next_task: "Full crawl (T02.11) after deploy."
updated: 2026-10-13
```

#### L02-05 First full crawl of all 52 pages, and what it found
```yaml
id: L02-05
status: tests_pass
env: staging
evidence_shas: [e219198, db4d0af, 1138dca, 90372f2]
files: [scripts/crawl-all-pages.py, docs/claude-prompts/all-pages-crawl.json, oracle-app/src/components/OurAITab.jsx, oracle-app/src/components/StateDocumentLibrary.jsx]
tests_run:
  - {cmd: "scripts/crawl-all-pages.py --base-url <staging> (owner + agent, desktop + phone, anonymous for public pages; 204 page loads)", result: "29 clean, 21 with console errors, 1 not offered (Admin, correct), 1 not run (no demo transaction for the deal sheet); 0 page errors, 0 API 5xx, 0 phone overflow, 0 writes on open, 0 rate-limit 429s after L02-04", date: 2026-10-13}
  - {cmd: "vitest OurAITab.test.jsx, StateDocumentLibrary.test.jsx + full suite", result: "713 passed", date: 2026-10-13}
  - {cmd: "re-crawl of the 20 affected pages after deploy (db4d0af), crawler scoring fixed", result: "20/20 clean; merged: 50 clean, 1 not offered (Admin), 1 not run (deal sheet, no demo transaction)", date: 2026-10-13}
  - {cmd: "crawl --only entity:deal, work:properties, properties:*, ai:sites, work:ai after 90372f2 deployed (run 20261013T091139Z), merged into the full run", result: "9/9 clean; deal sheet opened on the demo transaction 7094dfe5 (owner-approved test data); matrix 51 pages: 50 clean, 1 not offered (Admin)", date: 2026-10-13}
authz_checked: role gating observed per page (Admin hidden from owner and agent)
blockers:
  - {type: data, note: "Staging has no state_advertising_rules dataset loaded: the Forms view now says so instead of implying none apply."}
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "Per-page verdicts in all-pages-verdicts.json."
updated: 2026-10-13
```
Findings fixed from this crawl:
- AI hub, for agents: owner-only lead-routing metrics answered 403 on every visit, and the page showed "Lead routing is temporarily unavailable". A 403 source is now "not available to your role" and leaves the outage banner and the source count (`OurAITab.jsx`).
- Properties → Forms: a not-loaded advertising-rules dataset (backend 404, `dataset_not_loaded`) was shown as "No advertising rules recorded for this state", i.e. none apply. It now says the rules haven't been loaded and not to assume none apply (`StateDocumentLibrary.jsx`).

### L03
Brokerage tenancy and hybrid access.

#### L03-01 Tenancy and assignment model baseline
```yaml
id: L03-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/tenancy.py, backend/db/migrations/0001_init_tenancy.sql, backend/db/migrations/0054_agent_contacts_and_intake.sql, backend/db/migrations/0027_real_estate_intelligence_platform.sql, backend/contacts_api.py, backend/crm.py]
tests_run:
  - {cmd: "grep of policies, the Role enum and assignment columns", result: "clients/leads/agent_contacts RLS is tenant-wide; assignments are text agent ids; listing_grants is the only grant table; team_lead is unused in code", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: []
next_task: "T03.03: canonical UUID assignment"
updated: 2026-10-12
```

#### L03-02 Agent-scoped clients, the pool and per-record shares (D-2026-10-13-01)
```yaml
id: L03-02
status: tests_pass
env: staging
evidence_shas: [5d07552, ef526bd]
files: [backend/db/migrations/0131_agent_record_scope.sql, backend/record_access.py, backend/tenancy.py, backend/crm.py, backend/client_enterprise.py, backend/ai_chat_store.py, backend/privacy_lifecycle.py, backend/server.py, backend/client_portal.py, backend/client_ai_automation.py, backend/messaging_data.py, backend/inbound_voice.py, oracle-app/src/components/ClientAccess.jsx, oracle-app/src/state/useClientAccess.js, oracle-app/src/components/ClientDetailDrawer.jsx]
tests_run:
  - {cmd: "pytest tests/test_brokerage_access_matrix_live.py (real PostgreSQL, app role, local dev DB)", result: "2 passed: agent A / B / C(shared) / owner / platform admin / other brokerage / brokerage pipeline; children inherit; view share refused at the DB and as 403; edit share cannot re-share; expired share grants nothing; agent cannot hand a client to a colleague (API 403 and DB WITH CHECK); policy == app_client_access() == tenancy.client_access for every actor; pg_policies composition", date: 2026-10-13}
  - {cmd: "EXPLAIN ANALYZE as oracle_app, agent listing a 21,268-client brokerage", result: "6,262 ms with the policy as a function (per-row) -> 10.7 ms inline (index scan; share list an InitPlan)", date: 2026-10-13}
  - {cmd: "full backend suite; CI live files; rls_security_review/rls_brokerage/platform_rls_test/privacy_lifecycle.sql", result: "3226 passed, 0 failed; live 5/5 (precheck fails locally only on a stale 0106 ledger row in the dev DB, unrelated); SQL proofs PASS", date: 2026-10-13}
  - {cmd: "vitest (ClientAccess.test.jsx + full) + eslint + build + bundle budget", result: "718 passed; lint clean; within budget", date: 2026-10-13}
  - {cmd: "staging, real API, demo brokerage (owner-approved test-data change, restored afterwards)", result: "owner assigns Sarah to self -> agent list drops Sarah, GET /clients/{id} 404; owner shares view-only -> agent sees Sarah, /access = view; agent PATCH and note -> 403 'shared with you to view only'; agent re-share -> 403; share revoked + Sarah back to the pool", date: 2026-10-13}
  - {cmd: "real Chrome (Playwright, /usr/bin/google-chrome) as the demo agent on /p/{sarah}, desktop 1440 + phone 390, after ef526bd", result: "'Shared with you by Jordan Miles: view only.'; stage buttons and tag input disabled; no share form, no Take it. Screenshots performance/out/access03/. Found and fixed in ef526bd: 'Shared with you by null' on the phone, and live-looking edit controls", date: 2026-10-13}
authz_checked: "clients + 10 child tables (notes, activities, tasks, tags, buyer_profiles, client_ai_state, transactions, transaction_parties, showings, interaction_logs) under FORCE RLS as oracle_app"
blockers: []
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "L03-04 (agent_contacts)."
updated: 2026-10-13
```
How it works: `assignee_user_id` (uuid) is derived from `assignee_id` by a trigger that rejects a string naming nobody (23503 → 422). Restrictive policies narrow `clients_tenant_isolation` (permissive policies OR together, F03.01). The API never re-implements the rule: it reads `app_client_access()` back for 404 vs 403. Platform admin, the broker owner and server-built pipelines marked `record_scope='brokerage'` (client portal, inbound SMS/voice matching, the client-AI steward) keep brokerage-wide access; a verified user session can never set it.

#### L03-03 What 0131 and 0134 do not cover yet
```yaml
id: L03-03
status: source_verified
env: local
evidence_shas: []
files: [backend/db/migrations/0054_agent_contacts_and_intake.sql, backend/db/migrations/0001_init_tenancy.sql, backend/db/migrations/0058_sales_ai_stack.sql]
tests_run:
  - {cmd: "review of 0131 scope", result: "still brokerage-wide: agent_contacts (+ intake tables), leads, sms_messages, email_outbox, live_call_sessions, inbound_voice_calls, outreach_consent, lead_response_events, outcome_events, mission_candidates, contract_synthesis_artifacts, contact_property_relationships, media", date: 2026-10-13}
  - {cmd: "review after 0134 (L03-04): grep of contact_id foreign keys to agent_contacts", result: "agent_contacts + its four intake tables are now agent-scoped. Still brokerage-wide: leads, lead_intake_events, sms_messages, email_outbox, live_call_sessions, inbound_voice_calls, outreach_consent/_suppression/_attempt_log (by design: compliance), lead_response_events, outcome_events, mission_candidates, contract_synthesis_artifacts, media, and two contact children: smart_plan_enrollments, agent_call_intents (0058). Their API reads already go through an actor-scoped contact lookup (sales_api._load_contact, telephony_api prepare/outbound), so an agent cannot enroll or call a contact they cannot see; the rows themselves are still readable by id.", date: 2026-10-13}
authz_checked: n/a
blockers:
  - {type: design, note: "Communications tables feed consent, opt-out and frequency checks, which must stay brokerage-wide; scoping them needs a split between 'see the message' and 'check compliance'. 0134 adds the pattern: a function that sets app.record_scope='brokerage' for its own duration and returns no PII (app_client_contact_place, app_open_contact_counts)."}
  - {type: owner, note: "Team leads (T03.02): multiple teams per agent? edit or read-only on team records?"}
next_task: "The communications tables with a compliance-scope read path; smart_plan_enrollments and agent_call_intents inheriting the contact."
updated: 2026-10-13
```

#### L03-04 Agent-scoped contacts and intake tables (D-2026-10-13-01)
```yaml
id: L03-04
status: tests_pass
env: local
evidence_shas: [631b786]
files: [backend/db/migrations/0134_agent_contact_scope.sql, backend/record_access.py, backend/contacts_api.py, backend/tenancy.py, backend/ai_tools_gated.py, backend/lead_routing_api.py, backend/privacy_data_map.py, backend/policy_contract.py, docs/privacy-data-map.md, oracle-app/src/components/ContactDetailPanel.jsx, oracle-app/src/components/ClientAccess.jsx, oracle-app/src/state/useClientAccess.js, .github/workflows/ci.yml]
tests_run:
  - {cmd: "livetest: pytest tests/test_contact_access_matrix_live.py tests/test_brokerage_access_matrix_live.py (real PostgreSQL 16.14, app role, local dev DB)", result: "4 passed: agent A / B / C(shared) / owner / platform admin / other brokerage / brokerage pipeline / user session asking for brokerage scope; intake tables inherit; view share refused at the DB and as 403 on PATCH, intake, nurture and property routes; edit share cannot re-share or take the record (contacts AND clients, F03.02); expired share and a CLIENT share grant nothing on a contact; agent-created contact lands in the pool, a linked client's contact stays with its agent; agent cannot create or reassign to a colleague (API 403, DB 42501); unknown/other-brokerage/inactive assignee 23503; policy == app_contact_access() == tenancy.contact_access for every actor; pg_policies composition; workload counts and quiet-hours inputs stay brokerage-wide without widening the caller", date: 2026-10-13}
  - {cmd: "EXPLAIN ANALYZE as oracle_app, agent A in a throwaway 21,000-contact brokerage (7k A, 7k B, 7k pool, 50 shares), local dev DB", result: "contact list page (search_contact_rows): 1,760 ms before 0134 (tenant wall only), 280-470 ms scoped without an ordered index, 3.4 ms with idx_agent_contacts_list_order; the same rule as a function in the policy: 11,720 ms (48.6 ms with the index). Visible count 37-41 ms (seq scan, 21k rows). Child by contact_id 1.0 ms; access check 2.0 ms; app_open_contact_counts 36-41 ms. Phone-hash lookup seq scan 50-74 ms both before and after (tenant OR form, A10)", date: 2026-10-13}
  - {cmd: "rls_security_review/rls_brokerage/platform_rls_test/privacy_lifecycle.sql on the local dev DB", result: "all PASS", date: 2026-10-13}
  - {cmd: "full backend suite (ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/)", result: "3232 passed, 12 skipped, 0 failed", date: 2026-10-13}
  - {cmd: "CI live list locally (10 files)", result: "19 passed, 2 failed, both from OTHER sessions' migrations in the shared dev DB: test_migration_precheck (DB holds 0106/0132/0133/0135, not in this branch) and test_call_notes_live (0132 requires sealed call notes)", date: 2026-10-13}
  - {cmd: "vitest (ContactDetailPanel.test.jsx + ClientAccess.test.jsx + full) + eslint + vite build + bundle budget", result: "721 passed; lint clean; within budget", date: 2026-10-13}
  - {cmd: "staging PRE_DEPLOY of a62bcc2/e44aa78 (DigitalOcean managed PostgreSQL, non-superuser doadmin)", result: "FAILED at 0134 (42501 permission denied to set parameter app.record_scope): a function SET clause for a custom parameter needs superuser; local and CI migrate as superuser so neither caught it. 0132 and 0133 had applied, so staging briefly ran ef526bd code on a 0133 schema (0132 refuses that code's plaintext AI call notes). Fixed: the two brokerage-scope functions set and restore the scope with set_config() in plpgsql; verified as a NOSUPERUSER role locally (old form reproduces the exact error, new form succeeds); guard tests/test_migrations_managed_postgres.py", date: 2026-10-14}
  - {cmd: "livetest.sh migrate (0137) + pytest tests/test_contact_follows_client_live.py", result: "1 passed: owner move, return to pool and take all carry the contact; a colleague's drifted contact and an inactive assignee are skipped without failing the client update", date: 2026-10-13}
authz_checked: "agent_contacts + contact_property_relationships, contact_intake_sessions, intake_handoff_tasks, contact_nurture_jobs under FORCE RLS as oracle_app; clients reassignment guard"
blockers:
  - {type: decided, note: "Owner 2026-10-13: reassigning a client moves its linked contact. 0137 (trigger clients_carry_contact_assignee) does it in the same transaction through app_may_reassign, skipping (never failing) a contact the caller may not move or an inactive assignee; tests/test_contact_follows_client_live.py."}
  - {type: decided, note: "Owner 2026-10-13: Platform Use Policy v3 wording approved as drafted."}
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "Deploy 0131+0134 together; verify in a real browser on staging (People: owner assigns, agent shares view-only, colleague sees it read-only)."
updated: 2026-10-13
```
How it works: the 0131 pattern on `agent_contacts`. `assigned_user_id` is derived from `assigned_agent_id` by a trigger that also replaces 0059's (active members only, 23503 → 422). Four restrictive policies, inline; the intake tables inherit. `record_shares` takes `resource_kind='contact'`; `/api/crm/contacts/{id}/access|shares` mirror the client routes. An agent-created contact is not auto-assigned; one created for an assigned client stays with that client's agent, so a private client's details never land in the pool. Reads that are compliance or brokerage functions, not "seeing the contact", go through a function that sets `app.record_scope='brokerage'` only while it runs and returns no PII: quiet-hours state/timezone (`app_client_contact_place`) and lead-routing workload (`app_open_contact_counts`). Consent, opt-out and frequency (`outreach_*`) are untouched and stay brokerage-wide. Inbound SMS/voice matching and intake already ran as `brokerage_scope`, lead intake as the owner, speed-to-lead and smart-plan workers as platform admin, privacy flows as the owner; the outbound-call webhook and the smart-plan step act as the agent, so a reassigned contact is refused at execution (C3.4). Also fixed (F03.02): an edit share let its holder assign a colleague's client to themselves; `app_may_reassign()` now guards both triggers and `record_access` refuses it first.

### L04
CRM, contacts, leads and relationships.

#### L04-01 Engagement counted at the staged step (fixed: stages kept apart)
```yaml
id: L04-01
status: tests_pass
env: local
evidence_shas: [745f2fa, a8bff54]   # finding; fix (migration 0133)
files: [backend/outreach_lifecycle.py, backend/speed_to_lead.py, backend/lead_routing_api.py, backend/commands_api.py, backend/messaging_data.py, backend/email_outbox.py, backend/db/migrations/0133_engagement_lifecycle.sql, backend/tests/test_outreach_lifecycle.py, backend/tests/test_speed_to_lead.py, backend/tests/test_engagement_lifecycle_live.py]
tests_run:
  - {cmd: "sed -n 380,392p backend/speed_to_lead.py; sed -n 535,552p backend/lead_routing_api.py", result: "lead_engaged metered right after staged; routing metrics count staged drafts as responded", date: 2026-10-12}
  - {cmd: "cd backend && pytest -q -p no:cacheprovider tests/", result: "3263 passed, 11 skipped, exit 0", date: 2026-10-13}
  - {cmd: "livetest (real PostgreSQL, app + platform roles, 0133 applied): pytest tests/test_engagement_lifecycle_live.py", result: "1 passed: routing metrics count staged 1 / accepted 5 / delivered 4 / cancelled 1 apart, latency measured to accepted_at, no 'responded' key", date: 2026-10-13}
authz_checked: "live test reads the metrics as the broker owner under FORCE RLS on a throwaway brokerage; ledger moves run as the approving agent (app role) and the platform webhook context"
blockers: []
audit_refs: [A05]
decision_refs: [D-2026-10-12-02]
next_task: "Track 'responded' (inbound reply linked to the first response); mark a carrier-failed text as failed in the ledger; then T04.01 + T04.13."
updated: 2026-10-13
```
One vocabulary for every outbound store: `backend/outreach_lifecycle.py` (`Stage`, and maps for `command_executions.state`, `email_outbox.status`, `sms_messages.status`, carrier call statuses and `lead_response_events.disposition`; tests prove each map covers its CHECK). The first-response ledger now moves staged → approved (`commands_api.release_command`) → accepted (`_record_send_bookkeeping`, was `'sent'`) → delivered (Telnyx receipt in `messaging_data.record_delivery_update`, answered call in `commands_api.record_call_outcome`); a rejected draft becomes `cancelled`. Accepted and delivered are recorded only for a command whose state is `succeeded`. `routing_metrics` (`lead_routing_api.py:545`) no longer counts a staged draft as a response: it reports staged/approved/accepted/delivered/cancelled separately and measures p50/p90/under-90s to `accepted_at` (`latency_measured_to: "accepted"`); the `responded` key is gone (no caller). Not done: `responded` (a reply) is not tracked; a carrier-failed text stays `accepted`; email (SMTP) and Twilio texts never reach `delivered` (no receipt exists).

### L05
Semantic intent.

#### L05-01 Fast-path intent hides failed searches
```yaml
id: L05-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/neoh_intents.py, backend/search_api.py, oracle-app/src/neoh/useNeohComposer.js]
tests_run:
  - {cmd: "grep -n degraded backend/neoh_intents.py; sed -n 351,384p backend/search_api.py", result: "the fast path ignores degraded search legs and shows Nothing matches", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: []
next_task: "T05.11 + AC05.07"
updated: 2026-10-12
```

### L06
Evidence retrieval and memory.

#### L06-01 Retrieval legs, memory and plaintext chat history
```yaml
id: L06-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/search_api.py, backend/memory_core/session_manager.py, backend/privacy_data_map.py]
tests_run:
  - {cmd: "grep -n LEGS backend/search_api.py; git grep -i embedding", result: "5 legs with an untyped degraded list; no embeddings; chat turns stored as plaintext user_interactions (privacy_data_map.py:135)", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: [A01, A02]
decision_refs: []
next_task: "T06.06 + AC06.01"
updated: 2026-10-12
```

### L07
AI gateway and governed tools.

#### L07-01 Tool catalogue vs offered tools; capability claims agree with them (A09)
Artifact: the capability registry [`backend/ai_capabilities.py`](../../backend/ai_capabilities.py) (25 areas over all 126 catalogued tools, five states), served at `GET /api/ai/chat/capabilities` and snapshotted for the UI at [`oracle-app/src/lib/assistantCapabilities.json`](../../oracle-app/src/lib/assistantCapabilities.json) (generated; `python ai_capabilities.py --write-snapshot` from backend/).
```yaml
id: L07-01
status: tests_pass
env: local
evidence_shas: [745f2fa, 3bcab84, 071d65d]
files: [backend/ai_capabilities.py, backend/ai_chat_agent.py, backend/ai_chat_api.py, backend/ai_tools_read.py, backend/neoh_persona.py, backend/NEOH_SYSTEM_KNOWLEDGE.md, backend/tests/test_ai_capabilities.py, backend/tests/test_neoh_persona.py, backend/tests/test_ai_chat.py, oracle-app/src/components/AssistantCapabilities.jsx, oracle-app/src/components/AssistantCapabilities.module.css, oracle-app/src/components/AssistantCapabilities.test.jsx, oracle-app/src/components/PersonalAITab.jsx, oracle-app/src/components/OurAITab.jsx, oracle-app/src/components/ProductTour.jsx, oracle-app/src/lib/assistantCapabilities.js, oracle-app/src/lib/assistantCapabilities.json, oracle-app/src/lib/assistantCapabilities.test.js, oracle-app/src/lib/capabilityOrder.js]
tests_run:
  - {cmd: "grep counts over TOOLS, TOOL_RISK and the allowlists", result: "126 tools catalogued, 126 risk-classed, 57 allowlisted; unoffered tools are refused at execution", date: 2026-10-12}
  - {cmd: "re-verify at 3bcab84: each of the 57 offered tool descriptions read against its handler; persona, product knowledge, phone prompt, codebase map and UI copy swept", result: "over-claims found (see notes); 57 allowlisted confirmed = union of offered_tool_names over all contexts", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_ai_capabilities.py tests/test_neoh_persona.py tests/test_ai_tool_policy.py tests/test_ai_chat.py tests/test_ai_tools_read.py tests/test_ai_tools_spatial.py tests/test_killer_demo_paths.py", result: "314 passed", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0: 3369 passed, 10 skipped (live-DB tests skipped: no ORACLE_LIVE_DB_ADMIN_DSN)", date: 2026-10-13}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "vitest 748 passed (80 files, incl. assistantCapabilities.test.js and AssistantCapabilities.test.jsx); eslint exit 0; build ok; within budget (largest chunk 577.7 KB gzip)", date: 2026-10-13}
authz_checked: "GET /api/ai/chat/capabilities requires a session (require_context) and reads no tenant data; it reports the nine broker-owner tools (ai_tools_read.OPS_ROLE_TOOLS, checked against every _require_ops_role call) as role_limited for agents. No RLS, migration or grant change."
blockers:
  - {type: test, note: "AC07.02 asks for a Playwright check; not run (no browser session here). Unit and component tests cover the four surfaces; a real-browser look at Personal AI → 'What Neoh can do in chat' on desktop and phone is still owed."}
  - {type: owner, note: "archive_client: the persona said 'Never delete or archive data' while archive_client was an offered, undoable edit. Resolved toward the tool surface ('Never delete data. Archive a client only when explicitly asked'); two tests that pinned the old sentence now pin the new one. Owner to confirm, or say to take archive_client off the offer instead."}
audit_refs: [A09]
decision_refs: [D-2026-10-12-01]
next_task: "T07.02 + AC07.01 (ToolCapability registry per tool, built on ai_capabilities); T07.04: stop offering the nine owner-only tools to agents rather than refusing them at execution."
notes: "Over-claims found at 3bcab84 and fixed: (1) backend/NEOH_SYSTEM_KNOWLEDGE.md (in every long system prompt) claimed MLS 'via real-time feed' and MLS-sourced listing search, CourtListener, Census, Regrid, county assessor, GovInfo, BatchLeads/PropStream firehose, state underwriting models, owner history/tax records, 'PII columns encrypted via pgcrypto AES-256' (false per A01), SES/Twilio/Google delivery; rewritten as a page guide that claims no capability. (2) neoh_persona AUTHORITY 'Never delete or archive data' contradicted offered archive_client. (3) ai_chat_agent._VOICE_PROMPT told the phone model to promise lookups and 'Let me connect you … [CONNECT]' / '[END]'; nothing parses either marker and the voice path has no tools. (4) ai_chat_agent._CODEBASE_MAP (codebase_summary) presented MLS and migrations '0001-0041'. (5) 36 offered tool descriptions/schemas claimed more than their handlers: get_tenant_health (API latency, pool, cache), list_integration_status (MLS/SES/Stripe/Foundry/Regrid health), list_providers ('connected'), get_team_pipeline ('all agents'), search_listings + get_days_on_market (MLS), search_clients (stage), update_client (email/phone always refused), score_client_lead (reason ignored), estimate_arv (beds/baths ignored), list_contract_templates (filters ignored), get_audit_trail (limit ignored), get_deal_detail (contract, notes), track_deadlines (assignment), get_agent_performance (margin), check_contract_deadlines (contingencies), get_client_contact_history (meeting notes), create_deal_note (deal_id is a transaction id), calculate_mao (holding costs, floor), list_closing_checklist, get_transaction_workflow, get_job_queue, list_property_photos, suggest_client_matches; owner-only tools never said so; selected-record-only edits did not say so. (6) UI: OurAITab chat chips asked for things no tool does (approval-queue summary, multi-lead outreach sequences, cross-thread conversation review, equity reviews, public-property evidence, automation gaps) — now honest prompts or navigation; 'Social Ads … dynamic MLS'; 'record-changing actions still require review' (they apply with Undo); ProductTour 'MLS listings', 'authorized IDX sources', 'Everything Neoh can do'; PersonalAITab showed 'Public-record research: Enabled' / 'Draft preparation: Enabled' for preferences nothing reads, 'Research … can run autonomously', 'Accounts connected' for saved credentials. Guard: tests/test_ai_capabilities.py (unbacked_claims over persona layers, product knowledge, phone prompt, codebase map, every offered tool's description and parameters, usable area summaries; registry = allowlist; API per role; snapshot current) and oracle-app/src/lib/assistantCapabilities.test.js (every OurAITab chat chip, assistant copy files, MLS/IDX/RESO in ProductTour and OurAITab). Swept and NOT changed (other owners): SitePreview.jsx:212 IDX intake copy and BrokerageSetupPanel MLS setup row (14 / 11); CommsTab.jsx:307 'Queued — AI sends' for a human-composed email (10); AssistantMessages 'Anything that reaches a client waits for your approval' and OurAITab hero vs mission autopilot that releases its own approvals (missions/executor.py _release; 08); BrokerageOnboardingPanel autonomy toggles still offered though unused (03/08). Risk: test_compact_is_substantially_smaller now passes by ~20 characters (compact 3769 vs full/3 3790); a longer capability block will trip it."
updated: 2026-10-13
```

### L08
Durable missions and workflows. Open work exists: PR #11 (`feat/mission-internal-tasks-executable`, commits `55ac99c`, `0b52fbf`), not merged.

#### L08-01 Missions skip task actions; no cancel path
```yaml
id: L08-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/missions/executor.py, backend/missions/planner.py, backend/missions/policy.py, backend/missions_api.py, backend/db/migrations/0100_missions.sql]
tests_run:
  - {cmd: "sed -n 142,205p backend/missions/executor.py; grep -rn cancelled backend/missions backend/missions_api.py", result: "task actions are skipped; nothing writes cancelled; a standing grant can release a mission's own approval (policy.py:257-264)", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: review, note: "PR #11 (55ac99c, 0b52fbf) is open, not merged."}
  - {type: owner, note: "Does a mission's standing grant count as current human approval (CONTRACT C4.1)?"}
audit_refs: []
decision_refs: []
next_task: "T08.01: review PR #11"
updated: 2026-10-12
```

### L09
Real-time voice.

#### L09-01 ElevenLabs Agents as the live AI voice on Plivo calls
```yaml
id: L09-01
status: tests_pass
env: staging
evidence_shas: [7662171, 4cef36d]
files: [backend/elevenlabs_realtime.py, backend/plivo_call_handler.py, backend/commands_api.py, scripts/elevenlabs-agent.py]
tests_run:
  - {cmd: "pytest backend/tests/test_elevenlabs_realtime.py", result: "pass (CI run 37686937230)", date: 2026-10-07}
  - {cmd: "real call #5 to the operator's own number (rehearsal-10)", result: "128 s live conversation; outcome=completed + ended_at recorded via hangup_url", date: 2026-10-07}
authz_checked: signed URL only; agent auth enabled
blockers: []
audit_refs: []
decision_refs: []
next_task: "Repeat on a second real call after L09-02 to confirm notes end to end."
updated: 2026-10-12
```

#### L09-02 Consent-gated AI call notes delivered to the agent
```yaml
id: L09-02
status: implemented_untested
env: staging
evidence_shas: [aec14c4, 9c3463a]
files: [backend/elevenlabs_realtime.py, backend/commands_api.py, oracle-app/src/components/ClientNotes.jsx, oracle-app/src/state/useOracleWebSocket.js]
tests_run:
  - {cmd: "CI incl. backend/tests/test_call_notes_live.py (real PostgreSQL, FORCE RLS)", result: "pass (run 37692105722)", date: 2026-10-08}
  - {cmd: "text-mode probe against the live staging agent", result: "consent tool + 2 save_note calls observed", date: 2026-10-07}
authz_checked: brokerage tenant under FORCE RLS (live test)
blockers:
  - {type: verification, note: "Not yet proven on a real phone call."}
  - {type: decided, note: "D-2026-10-12-03: both modes, per agent; review is the default (built 2026-10-13, see L09-03)."}
audit_refs: [A01]
decision_refs: []
next_task: "One real consented call to verify the note reaches the client in review mode (T09.18)."
updated: 2026-10-12
```

#### L09-03 Call-note text went to the whole brokerage (fixed: per-user push)
```yaml
id: L09-03
status: tests_pass
env: local
evidence_shas: [745f2fa]   # finding; fix in the commit that adds 0130 (see git log)
files: [backend/commands_api.py, backend/call_note_settings.py, backend/client_enterprise.py, backend/db/migrations/0130_client_note_review.sql, oracle-app/src/components/ClientNotes.jsx, oracle-app/src/components/PersonalAITab.jsx]
tests_run:
  - {cmd: "sed -n 2350,2358p backend/commands_api.py (at 745f2fa)", result: "CALL_NOTES with note text sent via tenant-wide ws_hub.broadcast", date: 2026-10-12}
  - {cmd: "pytest backend/tests/test_call_note_review.py backend/tests/test_elevenlabs_realtime.py", result: "pass: frame only to requester + assignee; no tenant-wide broadcast; AI readers filter review_state='approved'", date: 2026-10-13}
  - {cmd: "vitest src/components/ClientNotes.test.jsx", result: "pass: proposed badge + approve", date: 2026-10-13}
authz_checked: frame recipients = requesting agent + client assignee; approve route runs under tenant RLS
blockers: [{type: verification, note: "backend/tests/test_call_notes_live.py (real PostgreSQL) runs in CI only."}]
audit_refs: [A01]
decision_refs: [D-2026-10-12-03]
next_task: "CI live test, then deploy approval."
updated: 2026-10-13
```

#### L09-04 No spoken opt-out handling on outbound AI calls
```yaml
id: L09-04
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/elevenlabs_realtime.py, backend/qwen_omni_realtime.py, backend/outreach_compliance.py]
tests_run:
  - {cmd: "grep -n opt_out backend/elevenlabs_realtime.py backend/qwen_omni_realtime.py backend/plivo_call_handler.py", result: "no matches, while the disclosure (outreach_compliance.py:105) promises no further contact", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: []
next_task: "T09.15"
updated: 2026-10-12
```

### L10
Communications and calendars.

#### L10-01 Per-agent email connect (presets, one-step connect, test send)
```yaml
id: L10-01
status: implemented_untested
env: local
evidence_shas: [75c6ba6]   # local main only, NOT pushed and NOT on this branch's base
files: [backend/sales_api.py, oracle-app/src/components/EmailConnectPanel.jsx, oracle-app/src/components/emailPresets.js, oracle-app/src/components/ProviderDeliveryPage.jsx]
tests_run:
  - {cmd: "backend pytest (full) + vitest (full) + eslint + vite build", result: "3276 passed / 688 passed / clean / built", date: 2026-10-07}
authz_checked: test-send own-or-broker only; snapshot shows own vs brokerage row
blockers:
  - {type: owner, note: "Push approval (a push to main deploys staging)."}
  - {type: verification, note: "No real mailbox connected yet; needs a real agent's app password."}
audit_refs: []
decision_refs: []
next_task: "Deploy, then one real Gmail app-password connect and test send."
updated: 2026-10-12
```

#### L10-02 Platform SMTP relay sends an approved email
```yaml
id: L10-02
status: blocked
env: staging
evidence_shas: []
files: [backend/smtp_mailer.py, backend/command_providers.py]
tests_run:
  - {cmd: "staging DB: SELECT command_type,state,count(*) FROM command_executions", result: "no EMAIL command has ever run", date: 2026-10-07}
authz_checked: n/a
blockers: [{type: verification, note: "Needs one approved send to the operator's own address."}]
audit_refs: []
decision_refs: []
next_task: "One approved email to the operator's own address."
updated: 2026-10-12
```

#### L10-03 Google Calendar connect
```yaml
id: L10-03
status: external_configuration_required
env: staging
evidence_shas: [745f2fa]
files: [backend/commands_api.py]
tests_run:
  - {cmd: "doctl apps spec get (api envs) + .env check", result: "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI absent", date: 2026-10-07}
authz_checked: calendar.events scope only; account_label = agent_id
blockers: [{type: operator, note: "Create a Google Cloud OAuth web client; redirect https://neoh-staging-ksfpn.ondigitalocean.app/api/commands/providers/google/oauth/callback"}]
audit_refs: []
decision_refs: []
next_task: "Add the client as staging secrets; connect one agent calendar."
updated: 2026-10-12
```

#### L10-04 Business texting (SMS)
```yaml
id: L10-04
status: external_configuration_required
env: staging
evidence_shas: [745f2fa]
files: [backend/messaging_api.py]
tests_run:
  - {cmd: "GET /api/messaging/business-number, /business/registration (demo owner + agent)", result: "ready=false; brand=null; campaign=null", date: 2026-10-07}
authz_checked: per-agent number
blockers: [{type: operator, note: "10DLC brand + campaign registration (paid, multi-day approval)."}]
audit_refs: []
decision_refs: []
next_task: "Register brand and campaign; assign agent numbers."
updated: 2026-10-12
```

#### L10-05 A typed-in mail server never receives the platform SMTP login
```yaml
id: L10-05
status: tests_pass
env: ci
evidence_shas: [155d474]
files: [backend/smtp_mailer.py, backend/sales_api.py]
tests_run:
  - {cmd: "CI (backend tests incl. test_smtp_mailer.py, test_sales_stack.py)", result: "pass (run 37715598311)", date: 2026-10-08}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: []
next_task: "None."
updated: 2026-10-12
```

#### L10-06 Concurrent messaging route initialization (A07)
```yaml
id: L10-06
status: tests_pass
env: local
evidence_shas: [745f2fa, c52b4dc]
files: [backend/messaging_data.py, backend/provider_purchases.py, backend/reconciliation.py, backend/privacy_data_map.py, backend/db/migrations/0136_hosted_messaging_order_ledger.sql, backend/tests/test_messaging_route_race_live.py, backend/tests/test_telnyx_messaging_data.py, .github/workflows/ci.yml, docs/provider-failure-matrix.md]
tests_run:
  - {cmd: "sed -n 96,117p backend/messaging_data.py", result: "ensure_messaging_route ON CONFLICT ... DO UPDATE SET provider_account_id still present (:108)", date: 2026-10-12}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_messaging_route_race_live.py (real PostgreSQL 16, FORCE RLS, app role; code at 3bcab84, BEFORE the fix)", result: "6 failed. (1) first route held uncommitted, 6 racers blocked behind it (pg_stat_activity): the committed route's provider_account_id ended as a racer's (acct-racer-3), not the first creator's (acct-first). (2) 6 unsynchronised first uses: one row, but callers were handed 4 different provider_account_id values. (3) 6 racing connect_messaging: 6 hosted orders submitted to the (mock) provider for one route. (4) a timed-out order left no record (a retry would submit again).", date: 2026-10-13}
  - {cmd: "livetest.sh migrate; livetest.sh python -m pytest -q -p no:cacheprovider tests/test_messaging_route_race_live.py (after the fix, 0136 applied to the local dev DB; run 4 times)", result: "6 passed each run: first committed route wins and no racer changes provider or account; all racers return the same row; 6 racing connects = 1 hosted order + 1 verification text, the others get 'already being connected'; ledger row telnyx/hosted_messaging_order/attached; timeout = unknown and blocks resubmission; confirmed-but-unattached order is reused (0 new orders); a definite rejection = failed and can be retried", date: 2026-10-13}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_reconciliation_live.py tests/test_on_conflict_targets_live.py; livetest.sh env ORACLE_AUDIT_SQLITE=/tmp/audit_logs.db python -m pytest -q tests/test_privacy_lifecycle_live.py", result: "4 passed; 1 passed (privacy lifecycle needs a writable audit sqlite path when the source is bind-mounted from a worktree; unrelated to this change)", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0: 3248 passed, 16 skipped (live-DB tests skipped without ORACLE_LIVE_DB_ADMIN_DSN)", date: 2026-10-13}
authz_checked: "No route or RLS change. ensure_messaging_route and the hosted-order claim run in the caller's tenant_tx (brokerage_scope from 0131 kept); messaging_routes and provider_purchases keep their 0108/0123 tenant policies and grants; 0136 only widens two CHECK constraints (no new function, table or column). Cross-tenant overwrite is impossible by the (tenant_id, agent_id) key; the live test uses throwaway tenants under the app role."
blockers:
  - {type: deploy, note: "Migration 0136 must apply before this code runs (connect_messaging writes provider 'telnyx' / kind 'hosted_messaging_order'). Applied only to the local dev DB."}
  - {type: provider, note: "An `unknown` hosted order has no automatic resolver: reconciliation.py turns a stale `intended` into `unknown` but does not ask Telnyx. Operator resolves it (docs/provider-failure-matrix.md row 'Hosted SMS order timeout')."}
audit_refs: [A07]
decision_refs: []
next_task: "Owner: confirm the invariant (first committed route wins; a provider/account switch only by disconnect + an explicit reconnect, which does not exist yet). Then: register_brand/register_campaign (messaging_api.py) create a NEW provider brand/campaign on every PUT, racing or not (see notes); give them the same ledger guard under T10.13/T10.14."
notes: "Invariant chosen (T10.13): first committed creation wins; nothing changes provider or provider_account_id implicitly. ensure_messaging_route is now INSERT ... ON CONFLICT DO NOTHING, then a re-read in the same transaction (messaging_data.py ensure_messaging_route). connect_messaging claims the order under the route's FOR UPDATE lock through provider_purchases (_claim_hosted_order), asks the provider once (_submit_hosted_order: definite refusal = failed, anything ambiguous = unknown), and attaches the order to the route and closes the ledger row in one transaction (_attach_hosted_order); only the attaching caller sends the verification text. reconciliation._reconcile_purchases now selects kind='phone_number' only, so a Twilio number search can never adopt a hosted order. Found, NOT changed (same audit theme, other routes): messaging_api.register_brand calls adapter.create_brand on every PUT and then overwrites provider_brand_id (ON CONFLICT DO UPDATE), so a double-click or a correction creates a second 10DLC brand at Telnyx and orphans the first; register_campaign inserts a new campaign row and provider campaign every call. assign_campaign_number has no ledger but is the same number to the same campaign. Residual: a crash between attaching the order and sending the verification text leaves 'pending_verification' with no text sent and no resend path; disconnect keeps hosted_order_id, so reconnecting after a disconnect returns the old route (pre-existing). connect_messaging's provider_account_id argument is unused and check_eligibility stores the VOICE provider name as provider_account_id (pre-existing; meaning of the column needs an owner/engineer decision)."
updated: 2026-10-13
```

#### L10-07 Nothing reads an inbox yet
```yaml
id: L10-07
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/email_outbox.py, backend/commands_api.py]
tests_run:
  - {cmd: "grep -rnE 'imaplib|IMAP4|gmail.readonly|In-Reply-To' backend", result: "no matches; the email agent in the owner's shared notes (2026-10-11) needs inbox reading", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: owner, note: "Inbox access method: IMAP with an app password, or an OAuth read scope."}
audit_refs: []
decision_refs: []
next_task: "T10.17"
updated: 2026-10-12
```

#### L10-08 Email channel shows Ready from environment variables alone
```yaml
id: L10-08
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/sales_api.py, oracle-app/src/components/ProviderDeliveryPage.jsx]
tests_run:
  - {cmd: "sed -n 385,410p backend/sales_api.py", result: "env SMTP settings count as a ready email channel (:394, :405) with no successful send", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: []
next_task: "T10.09"
updated: 2026-10-12
```

### L11
Property and market intelligence from authorized non-MLS sources.

#### L11-01 Legacy MLS code and data, gated off (no data deleted)
```yaml
id: L11-01
status: tests_pass
env: staging
evidence_shas: [745f2fa, 90372f2]
files: [backend/mls_switch.py, backend/mls_portal.py, backend/state_compliance/routes_mls.py, backend/brokerage_onboarding.py, backend/operator_api.py, backend/data_integrations/periodic.py, backend/component_health.py, backend/ai_tools_read.py, backend/ai_chat_agent.py, oracle-app/src/components/PropertiesTab.jsx, oracle-app/src/components/HouseSelection.jsx, oracle-app/src/components/OurAITab.jsx, oracle-app/src/components/StudioTab.jsx]
tests_run:
  - {cmd: "pytest backend/tests/test_mls_switch.py + full backend suite", result: "3307 passed: every /mls route (except the public-records aliases and the platform-admin privacy purge) depends on require_mls_enabled; /api/property-records is the same handler as the old alias; sync jobs skip; health NOT_CONFIGURED, never alerting", date: 2026-10-13}
  - {cmd: "vitest full suite (allPagesMatrix: no MLS page, no /api/mls/ call)", result: "713 passed; matrix now 51 pages", date: 2026-10-13}
  - {cmd: "curl staging after 90372f2 deployed (CI 38372824708 success)", result: "404 'MLS is not offered' on /api/mls/search, /api/mls/health, /api/mls/regions, /api/brokerage/mls/entitlements; 200 on /api/property-records and the /api/mls/public-records alias", date: 2026-10-13}
authz_checked: role checks run before the MLS switch on role-gated routes (non-admins get 403, not 404)
blockers:
  - {type: owner, note: "Retention/deletion of the cached MLS rows and migrations stays an owner decision; nothing was deleted."}
  - {type: data, note: "Staging's shared public property catalog is empty (total 0 for every query), so the public-records search answers correctly but returns no rows there. Harvesting into staging is not done and needs its own approval."}
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "Decide whether to harvest a public-records sample into staging so the search can be verified with rows."
updated: 2026-10-13
```

#### L11-02 A failed property lookup reads as zero matches (A02)
```yaml
id: L11-02
status: tests_pass
env: local
evidence_shas: [745f2fa, d934b2e]
files: [backend/db/migrations/0135_client_ai_source_status.sql, backend/client_ai_automation.py, backend/neoh_intents.py, backend/neoh_render.py, backend/buyer_matching.py, backend/ai_tools_read.py, backend/mls_portal.py, backend/tests/test_property_lookup_status.py, backend/tests/test_client_ai_automation.py, oracle-app/src/lib/propertyLookupStatus.js, oracle-app/src/lib/propertyLookupStatus.test.js, oracle-app/src/components/ClientDetailDrawer.jsx, oracle-app/src/components/ClientDetailDrawer.module.css]
tests_run:
  - {cmd: "sed -n 676,710p backend/client_ai_automation.py; git diff 3b63d5b 745f2fa -- backend/client_ai_automation.py", result: "_property_candidates_bounded catches every exception (:700) and returns [] (:704); reconciliation sets degraded from model_error only (:962); file unchanged since the audit pin", date: 2026-10-12}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_property_lookup_status.py tests/test_client_ai_automation.py tests/test_neoh_render.py tests/test_neoh_intents.py", result: "57 passed: verified zero = checked+matched 0; TimeoutError / asyncio.TimeoutError / SQLSTATE 57014 = timed_out; provider error = unavailable (no name in log or status); valid matches; not_applicable runs no query; reconcile with a failed lookup = degraded + PROPERTY_LOOKUP_* + data gap + timeline 'not checked'; stale candidates not re-served after a failed pass, last_checked_at kept; failed pass is retried (no 24 h shortcut), review task not duplicated; Neoh shortlist and suggest_client_matches never say 'no matches' for a failed or skipped search; public-records enrichment failure reported", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0: 3247 passed, 10 skipped (live-DB tests skipped: no ORACLE_LIVE_DB_ADMIN_DSN)", date: 2026-10-13}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "vitest 724 passed (78 files, incl. propertyLookupStatus.test.js); eslint exit 0; build ok; bundle within budget", date: 2026-10-13}
authz_checked: "no route or RLS change; client_ai_state keeps 0053's tenant policy and table-level grants (source_status needs no new grant; no new function). Buyer matching still refuses platform-admin sessions, now saying 'not_applicable' instead of an empty result."
blockers:
  - {type: deploy, note: "Migration 0135 must apply before this code runs: reconcile writes client_ai_state.source_status. Migrations run as the PRE_DEPLOY job; not applied anywhere yet."}
  - {type: test, note: "Not run against real PostgreSQL (no live-DB DSN here). The reconcile path is proven with an in-memory connection; a live test of 0135 + reconcile under FORCE RLS is still owed."}
audit_refs: [A02]
decision_refs: []
next_task: "Apply 0135 on staging with approval and run one reconcile with ORACLE_PROPERTY_CANDIDATES_TIMEOUT_MS forced low to see 'Lookup failed' in the client drawer; then the remaining non-property failure-as-zero sites found in the sweep (L11-02 note) under their owning categories."
notes: "Sites fixed at the commit: client_ai_automation._property_candidates_bounded (returns candidates + per-source status, :727) and reconcile_client (degraded on a failed required source, :1079; no 24 h shortcut after a failed lookup, :882); neoh_intents._candidates_for (:291) + neoh_render.properties_for (:190); buyer_matching.property_buyer_matches buyer_search (:402) + ai_tools_read suggest_client_matches note (:2052); mls_portal public-records search/detail report a failed official-source enrichment (degraded + enrichment[], :1081, :1156); ClientDetailDrawer shows the lookup status (:779). Also removes the customer name from the lookup-failure log (A06 site client_ai_automation.py:701-703 at 745f2fa). Swept and NOT changed (other owners or already honest): data_integrations/building_footprint.py osm/regrid footprints return [] on error and floorplan_api.py footprint-candidates says empty is a real answer (category 12); data_integrations/census_geocoder.py batch/single geocode return matched:false on upstream failure via /api/data/geocode[/batch]; /api/data/eviction matched:false for miss and outage alike; apis/property_data.py flood/walkscore None with no reason on /api/enrich-property; apis/market_data.py FRED/Treasury None on /api/market-snapshot; avm_client.py UNAVAILABLE conflates provider outage with no estimate. Already honest: ai_tools_read search/comps/market tools, ai_chat_store read-tool exceptions (ok:false), search_api degraded legs, property-records DB failure = 503."
updated: 2026-10-13
```

### L12
Neoh Space capture, reconstruction and tours. Spec: [neoh-space-web-capture-claude.md](../neoh-space-web-capture-claude.md).

#### L12-01 Unverified 3D quality still publishes with a full badge
```yaml
id: L12-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/recon_quality.py, backend/reconstruction_worker.py, backend/tour_api.py, backend/space_status.py, oracle-app/src/components/PropertyTourViewer.tsx]
tests_run:
  - {cmd: "sed/grep of recon_quality.assess, _assess_quality, _TIER_BADGE; git grep -i certif", result: "quality_unverified is not blocked and publishes; the tier-3 Full 3D Walkthrough badge (tour_api.py:41, :335) ignores the verdict; the buyer viewer never renders limitations; no certification state exists", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: owner, note: "Should a quality_unverified space publish as an agent-only draft, or with caveats as today?"}
audit_refs: [A03]
decision_refs: [D-2026-10-08-01, D-2026-10-08-02]
next_task: "Prompt 12 first slice (Capture Studio); then T12.09/T12.10 to close A03"
updated: 2026-10-12
```

#### L12-02 3D labels follow the quality verdict (no "Full" without evidence)
```yaml
id: L12-02
status: tests_pass
env: local
evidence_shas: [745f2fa]   # finding in L12-01; fix in the commit that adds splat_quality
files: [backend/tour_api.py, oracle-app/src/components/TourViewer.jsx, oracle-app/src/neoh/EntitySheet.jsx]
tests_run:
  - {cmd: "pytest backend/tests/test_tour_tier_honesty.py + tour/space tests", result: "59 passed: label per state (measured, pending, degraded, incomplete); no label says Full or claims verified", date: 2026-10-13}
  - {cmd: "vitest TourViewer, HouseWorkspace, neoh", result: "285 passed: viewer labels the mode from splat_label and shows the caveats in the disclosure", date: 2026-10-13}
authz_checked: n/a
blockers: [{type: scope, note: "T12.09 certification (verified state with held-out quality, registration, doorway and device evidence) is not built; nothing can be verified yet."}]
audit_refs: [A03]
decision_refs: [D-2026-10-08-01]
next_task: "T12.09 certification state; owner question on publishing unverified spaces."
updated: 2026-10-13
```

### L13
Deals, documents and compliance.

#### L13-01 Transaction lifecycle baseline
```yaml
id: L13-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/portfolio_api.py, backend/db/migrations/0039_transaction_workflow.sql, backend/contracts_api.py]
tests_run:
  - {cmd: "grep -n PropertySource backend/portfolio_api.py", result: "4 transaction statuses; no event trail; the transaction property source still accepts legacy mls (:37, :142)", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: owner, note: "Non-MLS source for listed homes; commission ledger vs integration; e-sign provider."}
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T13.02 + T13.14"
updated: 2026-10-12
```

### L14
Sites, social, ads and client portal.

#### L14-01 Published sites have no public route; badges read Live
```yaml
id: L14-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/sites_api.py, oracle-app/src/App.jsx, oracle-app/src/components/OurAITab.jsx, oracle-app/src/components/StudioTab.jsx]
tests_run:
  - {cmd: "grep -nE '^@router' backend/sites_api.py; sed -n 325,345p oracle-app/src/components/OurAITab.jsx", result: "publish returns /sites/{slug} but nothing serves it (only /site-preview/ is routed); sourceState reports Live whenever the endpoint answers (OurAITab.jsx:338)", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: owner, note: "Which social and ad networks are in launch scope; broker-owned listings on public sites instead of IDX?"}
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T14.02 + T14.05 + T14.16"
updated: 2026-10-12
```

#### L14-02 Published sites are served at /sites/{brokerage}/{site}
```yaml
id: L14-02
status: tests_pass
env: local
evidence_shas: [745f2fa]   # finding in L14-01; fix in the commit that adds public_sites_api.py
files: [backend/public_sites_api.py, backend/sites_api.py, backend/server.py, oracle-app/src/components/PublicSite.jsx, oracle-app/src/components/SitePreview.jsx, oracle-app/src/App.jsx]
tests_run:
  - {cmd: "pytest backend/tests/test_public_sites.py + sites tests", result: "52 passed: public fields only; no-cache + ETag/304; unpublished, archived or malformed -> 404", date: 2026-10-13}
  - {cmd: "vitest src/components/PublicSite.test.jsx", result: "2 passed: public mode hides preview bar, pending placeholders, unreviewed areas and IDX wording; unpublished says so", date: 2026-10-13}
authz_checked: anonymous read limited to status='published' + its published revision; backend/tests/test_public_sites_live.py (real PostgreSQL, runs in CI) covers draft/archived/archive-takes-effect
blockers: [{type: verification, note: "Live test runs in CI; domains (T14.04) and rollback still open."}]
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T14.04 domain verification and rollback; T14.02 + T14.16 honest badges."
updated: 2026-10-13
```

### L15
Neoh mascot animation.

#### L15-01 Mascot rig not started; Rive path is a stub
```yaml
id: L15-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [oracle-app/src/neoh/avatarModel.js, oracle-app/src/neoh/NeohAvatarRive.jsx, oracle-app/src/neoh/NeohCorner3D.jsx, oracle-app/src/neoh/NeohSurface.jsx]
tests_run:
  - {cmd: "git ls-files | grep -iE '.riv$|.glb$'; grep -n rive oracle-app/package.json", result: "no rig or .riv asset; no Rive runtime; nothing supplies speaking/audioLevel; the 3D corner receives no product state", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: owner, note: "Who authors the rig / .riv asset, and under what licence?"}
audit_refs: []
decision_refs: []
next_task: "T15.02 + T15.05 + T15.13"
updated: 2026-10-12
```

### L16
Performance, indexes and cache.

#### L16-01 Performance baseline is local only
```yaml
id: L16-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/db/connection.py, backend/crm.py, backend/search_api.py, oracle-app/src/sw-oracle.js, oracle-app/src/neoh/UniversalWorkspace.jsx, docs/capacity-plan.md]
tests_run:
  - {cmd: "grep -n AbortController oracle-app/src/neoh/UniversalWorkspace.jsx; sed -n 127,134p docs/capacity-plan.md", result: "Work search has no request cancellation; cached splats are served without a rights check until logout (sw-oracle.js:268); nothing measured on DigitalOcean", date: 2026-10-12}
authz_checked: n/a
blockers: 
  - {type: owner, note: "No production-shaped target exists yet (L19-02)."}
audit_refs: [A10]
decision_refs: []
next_task: "T16.04 + T16.05"
updated: 2026-10-12
```

#### L16-02 A prefetched splat is served only after the server re-confirms access
```yaml
id: L16-02
status: tests_pass
env: local
evidence_shas: [745f2fa]   # finding in L16-01; fix in the commit that adds splatCacheGate.js
files: [oracle-app/src/lib/splatCacheGate.js, oracle-app/src/sw-oracle.js, backend/media_api.py]
tests_run:
  - {cmd: "vitest src/lib/splatCacheGate.test.js", result: "8 passed: 304 -> cached copy; 401/403/404/410 -> purge + refuse; offline -> cached; 5xx not masked", date: 2026-10-13}
  - {cmd: "pytest backend/tests/test_media_revalidation_auth.py backend/tests/test_media_api_security.py", result: "9 passed: 304 only after an RLS visibility read (no bytes); invisible row -> 404", date: 2026-10-13}
  - {cmd: "npm run build", result: "sw-oracle.js bundles with the gate", date: 2026-10-13}
authz_checked: revalidation goes through the authenticated route and RLS
blockers: [{type: scope, note: "The browser HTTP cache still keeps media for 24 h (private, max-age=86400, immutable) without asking; per-agent RLS does not exist yet (prompt 03), so today 'losing access' means session revoked, media removed or another brokerage."}]
audit_refs: []
decision_refs: []
next_task: "T16.06 permission-keyed caches; revisit the 24 h media max-age when per-agent access lands."
updated: 2026-10-13
```

### L17
Security, privacy and data lifecycle.

#### L17-01 Staging DAST and security launch gate
```yaml
id: L17-01
status: tests_pass
env: staging
evidence_shas: [0739563]
files: [docs/security-launch-gate.json, docs/security/]
tests_run:
  - {cmd: "OWASP ZAP active scan against staging", result: "0 High; gate verdict PASS on staging", date: 2026-10-07}
authz_checked: logout revokes all sessions (epoch); non-UUID → 422
blockers: [{type: scope, note: "Staging only; production does not exist (L19-02)."}]
audit_refs: [A04]
decision_refs: []
next_task: "Repeat against production when it exists."
updated: 2026-10-12
```

#### L17-02 Database network restriction (trusted sources)
```yaml
id: L17-02
status: tests_pass
env: staging
evidence_shas: [b6d1160]
files: [scripts/db-access-window.sh, infra/digitalocean/app.yaml]
tests_run:
  - {cmd: "doctl databases firewalls list (PG + Valkey)", result: "only app:3386001b… after close", date: 2026-10-07}
authz_checked: migrations run as a PRE_DEPLOY job inside DigitalOcean
blockers: []
audit_refs: []
decision_refs: []
next_task: "Same rule for production when it exists."
updated: 2026-10-12
```

#### L17-03 AI call notes are stored as plaintext
```yaml
id: L17-03
status: tests_pass
env: local
evidence_shas: [745f2fa, e125e51]
files: [backend/db/migrations/0132_client_note_body_sealed.sql, backend/client_note_crypto.py, backend/backfill_client_notes.py, backend/commands_api.py, backend/client_enterprise.py, backend/ai_chat_store.py, backend/client_ai_automation.py, backend/privacy_export.py, backend/privacy_data_map.py, backend/tests/test_client_note_sealing.py, backend/tests/test_client_note_sealing_live.py, backend/tests/test_call_notes_live.py, docs/privacy-data-map.md, docs/operator-scripts.md]
tests_run:
  - {cmd: "sed -n 2301,2345p backend/commands_api.py; grep -n client_notes backend/privacy_data_map.py", result: "deliver_call_notes inserts into client_notes.body, a plain text column (:2335); the data map classifies client_notes as business CRM only (:65)", date: 2026-10-12}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3240 passed, 11 skipped (baseline at ef526bd: 3229 passed, 10 skipped)", date: 2026-10-13}
  - {cmd: "livetest.sh migrate (local dev PostgreSQL, throwaway roles)", result: "applied 0132_client_note_body_sealed.sql", date: 2026-10-13}
  - {cmd: "livetest.sh python -m pytest -q tests/test_client_note_sealing_live.py tests/test_call_notes_live.py + the rest of the ci.yml live list", result: "exit 0; 12 passed (app role, FORCE RLS). tests/test_migration_precheck.py fails locally only because the shared dev DB records 0106_brokerage_setup.sql, a file in no branch", date: 2026-10-13}
authz_checked: "note keys come from the row's own tenant (client_note_crypto.open_rows), never the caller; reads and writes run as the app role under 0131 RLS in the live test"
blockers:
  - {type: operator, note: "Pre-0132 notes (AI call notes since aec14c4 included) stay plaintext in each environment until backend/backfill_client_notes.py runs there (docs/operator-scripts.md). Not run on staging or production."}
  - {type: scope, note: "Only client_notes. clients name/email/phone, leads.payload, sms_messages.body, user_interactions.content and negotiation_events.transcript_excerpt are still plaintext (A01). The DB refuses a plaintext AI call note (0132 trigger); other notes are sealed by every app path but not DB-enforced, because direct SQL fixtures still insert plaintext."}
  - {type: scope, note: "The CALL_NOTES WebSocket frame and the AI prompt context carry the opened text, as before; client_activities rows carry only counts."}
audit_refs: [A01]
decision_refs: [D-2026-10-12-03]
next_task: "Run backfill_client_notes.py on staging after deploy (owner approval: push = deploy). Then T17.04 for sms_messages.body and clients contact fields."
updated: 2026-10-13
```

#### L17-04 Customer names in failure logs
```yaml
id: L17-04
status: tests_pass
env: local
evidence_shas: [745f2fa, e125e51]
files: [backend/client_ai_automation.py, backend/outreach_compliance.py, backend/commands_api.py, backend/neoh_intents.py, backend/lead_dossier.py, backend/disposition_enforcer.py, backend/crm.py, backend/auth.py, backend/brokerage_onboarding.py, backend/tests/test_log_pii_guard.py, docs/privacy-data-map.md]
tests_run:
  - {cmd: "sed -n 676,710p backend/client_ai_automation.py", result: "_property_candidates_bounded logs full_name on failure (:701-703)", date: 2026-10-12}
  - {cmd: "AST sweep of every backend logging call for name/email/phone/contact/note arguments", result: "customer PII also logged by outreach_compliance consent/opt-out/STOP (normalized phone/email), the ACS incoming-call handler (caller number), neoh_intents (request text), lead_dossier (entity name), disposition_enforcer and crm (address); account email by auth reset/signup/invite and the DEV invitation line. All now log ids or outcome codes.", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_log_pii_guard.py", result: "exit 0; 5 passed (source guard, guard-the-guard, 3 failure paths on captured log text)", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3240 passed, 11 skipped", date: 2026-10-13}
authz_checked: n/a
blockers:
  - {type: scope, note: "Not covered by the guard: property addresses in geocoder/AVM/footprint/CMA lookup logs, ctx.agent_id (the user's email) in actor fields, and text inside exception messages and tracebacks. No staging log sample has been reviewed (AC17.05 asks for one)."}
audit_refs: [A06]
decision_refs: []
next_task: "Review a staging log sample after deploy; decide whether addresses and agent_id emails in logs move to ids (T17.09)."
updated: 2026-10-13
```

#### L17-05 Platform Use Policy kept current (v2) and guarded
```yaml
id: L17-05
status: tests_pass
env: local
evidence_shas: [da4c950]
files: [backend/policy_contract.py, backend/tests/test_policy_coverage.py, oracle-app/src/components/PolicyAcceptanceGate.jsx, tests/golden/golden_e2e.py, docs/claude-prompts/CONTRACT.md]
tests_run:
  - {cmd: "pytest tests/test_policy_coverage.py + full backend suite; vitest full", result: "3229 passed; 718 passed", date: 2026-10-13}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: [D-2026-10-13-01, D-2026-10-12-03]
next_task: "After deploy every user re-accepts once (v2); re-accept for the two demo accounts before the next crawl."
updated: 2026-10-13
```
The owner asked for the policy to be kept current. v2 (wording approved by the owner 2026-10-13) adds three paragraphs: per-record visibility and sharing (§2), AI call notes need consent and wait for review (§4), and the AI-voice disclosure must not be removed (§6). `POLICY_COVERAGE` ties each to the file that proves the capability; the test fails if the words go while the capability stays. CONTRACT C5.7 makes it a standing rule.

### L18
Billing, seats and analytics.

#### L18-01 Plan model in code vs the brokerage pricing hypothesis
```yaml
id: L18-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/billing.py]
tests_run:
  - {cmd: "grep -n STRIPE_PRICE_ID backend/billing.py", result: "one price (billing.py:44 STRIPE_PRICE_ID) checked out at quantity 1 (billing.py:266 line_items); no per-seat quantity; the amount lives in Stripe, not code", date: 2026-10-12}
authz_checked: n/a
blockers: [{type: owner, note: "Pricing undecided (D-2026-10-12-02)."}]
audit_refs: [A05]
decision_refs: [D-2026-10-12-02]
next_task: "Design 20-seat entitlements and quantity on the single-price checkout (prompt 18)."
updated: 2026-10-12
```

#### L18-02 Engagement metered before approval or delivery (fixed: metered at delivery only)
```yaml
id: L18-02
status: tests_pass
env: local
evidence_shas: [745f2fa, a8bff54]   # finding; fix (migration 0133)
files: [backend/outreach_lifecycle.py, backend/billing_usage.py, backend/speed_to_lead.py, backend/commands_api.py, backend/messaging_data.py, backend/operator_api.py, backend/ai_tools_read.py, backend/db/migrations/0133_engagement_lifecycle.sql, backend/tests/test_outreach_lifecycle.py, backend/tests/test_billing_usage.py, backend/tests/test_engagement_lifecycle_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "grep -n record_usage backend/speed_to_lead.py; sed -n 74,77p backend/billing_usage.py", result: "lead_engaged recorded at :387-389 right after the staged event; it is the only Stripe-reported metric (billing_usage.py:76)", date: 2026-10-12}
  - {cmd: "cd backend && pytest -q -p no:cacheprovider tests/test_outreach_lifecycle.py tests/test_speed_to_lead.py tests/test_billing_usage.py tests/test_command_send_bookkeeping.py", result: "75 passed: staged/approved/accepted never meter; delivered meters once; failed, cancelled, blocked, manual never meter; staged-step row promoted not duplicated", date: 2026-10-13}
  - {cmd: "cd backend && pytest -q -p no:cacheprovider tests/", result: "3263 passed, 11 skipped, exit 0", date: 2026-10-13}
  - {cmd: "livetest (real PostgreSQL, throwaway app + platform roles, 0133 applied): pytest tests/test_engagement_lifecycle_live.py", result: "1 passed: Telnyx receipt meters 1 row naming its command, duplicate receipt +0, receipt-before-acceptance applied at acceptance, answered call +1, failed/cancelled +0; drain (mock Stripe) reports 3 billable, 0 on rerun, never the staged-step row; CHECK refuses a billable row without a delivered source; reconcile query flags only a staged-step row already reported", date: 2026-10-13}
  - {cmd: "livetest: pytest tests/test_on_conflict_targets_live.py tests/test_privacy_lifecycle_live.py tests/test_stripe_webhook_live.py tests/test_reconciliation_live.py tests/test_migration_precheck.py", result: "14 passed, 1 failed: test_precheck_against_a_real_migrated_database fails because the shared dev DB carries other branches' migrations (0132, 0134-0136) this worktree lacks — environmental, not 0133", date: 2026-10-13}
authz_checked: "usage summary and reconciliation run as the broker owner under FORCE RLS; delivery hooks run in the platform webhook context with explicit tenant_id filters"
blockers: [{type: owner, note: "Billable stage for email and Twilio texts (no delivery receipt exists, so they never meter today) and credit for any staged-step row already reported to Stripe — pricing contract (D-2026-10-12-02)."}]
audit_refs: [A05]
decision_refs: [D-2026-10-12-02]
next_task: "Owner: confirm delivered as the billable stage (or name one per channel). Then run billing_usage.reconcile_lead_engagements on staging after 0133 deploys; then T18.04 + AC18.02 (seats)."
updated: 2026-10-13
```
`lead_engaged` is now written only by `billing_usage.record_lead_engagement` (`billing_usage.py:206`), called from `outreach_lifecycle.meter_delivered_engagement` (`outreach_lifecycle.py:232`), which re-reads that the command `succeeded` (approved and accepted) and its first response is `delivered`. `record_usage` refuses the metric (`_STAGE_BOUND`, `billing_usage.py:82`); `speed_to_lead.py:387` meters nothing. Migration 0133 adds `lifecycle_stage`/`source_table`/`source_id` with a CHECK (a stage-bearing `lead_engaged` row must be a delivered `command_executions` row). Pre-0133 staged-step rows have NULL stage by construction (no backfill needed under FORCE RLS) and are excluded by `BILLABLE_PREDICATE` (`billing_usage.py:97`) from the Stripe drain, the sweep, `/api/billing/usage` (shown as `not_billable_events`), the operator backlog and the AI billing tools; delivery later promotes the same row (same idempotency key) instead of adding one. Prices and plans unchanged. Not done: email and Twilio-SMS engagements never meter (no receipt); no UI change in `BillingUsagePanel.jsx` for `not_billable_events`; staging has not run 0133.

#### L18-03 Billing screen shows the price checkout bills (fixed: no hard-coded plan)
```yaml
id: L18-03
status: tests_pass
env: local
evidence_shas: [745f2fa]   # finding; fix in the commit that adds GET /billing/plan
files: [backend/billing.py, backend/billing_usage.py, oracle-app/src/components/BillingOverlay.jsx, oracle-app/src/lib/planPrice.js, .env.example]
tests_run:
  - {cmd: "grep -n 199 oracle-app/src/components/BillingOverlay.jsx (at 745f2fa)", result: "UI hard-coded $199 Solo Premium / One Named User / $39 while billing_usage.py said a flat $299 plan", date: 2026-10-12}
  - {cmd: "pytest backend/tests/test_billing_plan.py + billing tests", result: "42 passed: price read from the Stripe Price, cached; misconfigured or failing Stripe -> unavailable", date: 2026-10-13}
  - {cmd: "vitest src/components/BillingOverlay.test.jsx", result: "4 passed: shows backend price; never guesses; no price/solo literal in the component", date: 2026-10-13}
authz_checked: GET /billing/plan requires a signed-in user
blockers: [{type: owner, note: "Pricing and seat terms undecided (D-2026-10-12-02); seat enforcement is T18.04."}]
audit_refs: []
decision_refs: [D-2026-10-12-02]
next_task: "T18.06 (A05) and T18.04 (seats) per prompt 18 first slice."
updated: 2026-10-13
```

#### L18-04 No new brokerage can subscribe on staging
```yaml
id: L18-04
status: external_configuration_required
env: staging
evidence_shas: [6719298]
files: [backend/billing.py, oracle-app/src/components/BillingOverlay.jsx]
tests_run:
  - {cmd: "owner signed in on staging with their own brokerage (2026-10-13)", result: "/billing/plan source 'unavailable' (no Stripe price on staging); 'Start Neoh' -> create-checkout-session fails -> 'Neoh is temporarily unavailable'; /billing/status active=false", date: 2026-10-13}
authz_checked: n/a
blockers:
  - {type: external, note: "Staging has no Stripe test-mode price/webhook, so checkout can never succeed there. The message also says 'temporarily' for a configuration that is absent."}
audit_refs: []
decision_refs: [D-2026-10-12-02]
next_task: "Configure a Stripe test-mode price + webhook on staging (owner), or make the overlay say checkout is not set up on this deployment."
updated: 2026-10-13
```
Owner-approved workaround (2026-10-13): a staging-only subscription row for the owner's own brokerage (`staging_comp_sub_2dd07d9c`, active to 2027-10-13), written the way the demo brokerage is entitled. It also opens the spend-gated routes for that tenant on staging.

### L19
Production operations.

#### L19-01 Staging, CI auto-deploy, PRE_DEPLOY migrations
```yaml
id: L19-01
status: tests_pass
env: staging
evidence_shas: [b6d1160, 745f2fa]
files: [.github/workflows/ci.yml, infra/digitalocean/app.yaml, scripts/carry-secrets.py, backend/run_migrations.py]
tests_run:
  - {cmd: "push to main → CI → DigitalOcean staging", result: "success (run 38127122126 deployed 745f2fa)", date: 2026-10-11}
authz_checked: n/a
blockers: []
audit_refs: [A04]
decision_refs: []
next_task: "None for staging."
updated: 2026-10-12
```

#### L19-02 Production environment
```yaml
id: L19-02
status: source_verified
env: production
evidence_shas: []
files: [infra/digitalocean/app.yaml]
tests_run:
  - {cmd: "doctl apps list", result: "only the staging app exists; no production app", date: 2026-10-07}
authz_checked: n/a
blockers: [{type: owner, note: "Creating production needs owner approval and budget."}]
audit_refs: [A04, A10]
decision_refs: []
next_task: "Owner decision on production creation."
updated: 2026-10-12
```

#### L19-03 Health and runbooks cover legacy MLS, not the real property providers
```yaml
id: L19-03
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/component_health.py, docs/runbooks/mls-provider-down.md, infra/digitalocean/app.yaml]
tests_run:
  - {cmd: "grep -n mls backend/component_health.py; ls docs/runbooks", result: "health has a legacy mls component and none for RentCast, ATTOM, harvesters or market data; an MLS runbook exists, no property-provider runbook", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T19.07"
updated: 2026-10-12
```

### L20
20-agent launch certification. Its gates read this ledger.

#### L20-01 Go-live checklist still requires MLS
```yaml
id: L20-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [docs/brokerage-go-live-checklist.md]
tests_run:
  - {cmd: "grep -n MLS docs/brokerage-go-live-checklist.md", result: "item 8 (:36) asked for MLS authorization status", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "Item 8 annotated as legacy in this change; replace with a property-sources check (11 T11.01)"
updated: 2026-10-12
```

## Owner decisions

Decisions are binding ([CONTRACT C2.4](CONTRACT.md#c2-scope-and-owner-decisions)). Only the owner adds, changes or retires one.

| ID | Date | Decision | Source | Categories | Effect on code | Revisit when |
|---|---|---|---|---|---|---|
| D-2026-10-08-01 | 2026-10-08 | Neoh Space is 100% browser-based. No required native app, Swift, ARKit, RoomPlan, direct LiDAR or native bridge. | root `CLAUDE.md` at `19bb3d3` (2026-10-08) | 12, 02, 16 | Feature-detect browser APIs; always keep a no-camera fallback | Owner request |
| D-2026-10-08-02 | 2026-10-08 | The four Neoh Space workstreams are approved: registration + room connectivity; mobile Capture Studio + AI guide; photoreal reconstruction + mobile tour; live readiness engine. | [spec](../neoh-space-web-capture-claude.md) | 12 | All four are first-class | Owner request |
| D-2026-10-12-01 | 2026-10-12 | **MLS is out of scope.** There is no MLS connection, and approval or cost is not available. Do not build, advertise or present MLS/IDX/RESO search, ingestion or display as a feature. | Owner, in session 2026-10-12; shared notes 2026-10-11 | 02, 04, 06, 11, 12, 13, 14, 16, 18, 19, 20 | Gate the existing MLS code off (L11-01). **No deletion of MLS data or migrations without owner approval.** | Owner changes the decision and lawful access is secured |
| D-2026-10-12-03 | 2026-10-12 | **AI call notes support both modes, chosen per agent:** `review` (default) saves the note proposed for the agent to approve, edit or discard (the AI ignores it until approved); `direct` saves it approved. | Owner, in session 2026-10-12 ("both") | 09, 17 | `client_notes.review_state` (0130), `call_note_settings.py`, approve route, Personal AI toggle | Owner request |
| D-2026-10-13-01 | 2026-10-13 | **Agents see their own clients, the brokerage pool and what is shared with them.** A client with no assignee is the brokerage pool, visible to every agent; an agent-created client is NOT auto-assigned; a share is per record and grants view or edit, chosen per share. | Owner, in session 2026-10-13 (three answers) | 03, 04, 17 | `0131_agent_record_scope.sql` (restrictive policies, `record_shares`, `assignee_user_id`), `record_access.py`, `ClientAccess.jsx`; contacts the same way in `0134_agent_contact_scope.sql` (L03-04) | Team leads (T03.02) or a different default for imports |
| D-2026-10-12-02 | 2026-10-12 | Pricing is a hypothesis, not decided. Code: one Stripe price at quantity 1 (`backend/billing.py:44`, `:266`), no seats. Idea: ~$1,999/month per 20-agent brokerage, ~$1,499 pilot. | Shared notes 2026-10-11 | 18 | No price change without owner approval | Owner sets pricing |

## Audit cross-reference

From [neoh-full-platform-source-audit-2026-10-08.md](../neoh-full-platform-source-audit-2026-10-08.md), pinned at `3b63d5b`. The status column records the re-check at HEAD.

| ID | Finding | Severity | Status at HEAD `745f2fa` (2026-10-12) | Evidence at HEAD | Owning category | Ledger |
|---|---|---|---|---|---|---|
| A01 | Customer PII and communications in plaintext | P0 | open; the audit missed the AI call notes (`aec14c4` predates its pin). 2026-10-13: client notes sealed for new writes (0132, L17-03, local); old rows await the backfill; the other fields stay open | `docs/privacy-data-map.md:40`, `:42`, `:48`, `:71`; `backend/commands_api.py:2335` (`deliver_call_notes` → `client_notes.body`) | 17 | L17-03, L09-02, L09-03 |
| A02 | Failed property lookup reads as zero matches | P0 | fixed in code at `d934b2e`, local tests only (2026-10-13, L11-02); not deployed, migration 0135 unapplied; non-property sites listed in L11-02 still open | was `backend/client_ai_automation.py:700`, `:704`; now `_property_candidates_bounded` returns per-source status (`:727`) and a failed lookup degrades the reconcile (`:1079`) | 11 | L11-02, L06-01 |
| A03 | Professional 3D certification without verified evidence | P0 | partly fixed: labels bound to the verdict (L12-02); certification state not built | `backend/recon_quality.py:83`, `:101`; `backend/reconstruction_worker.py:581`; badge `backend/tour_api.py:41`, `:335` ignores the verdict | 12 | L12-01 |
| A04 | Production readiness not established by staging CI | P0 | open (no production app) | `.github/workflows/ci.yml:606` (`promote` manual only) | 19 | L19-02 |
| A05 | Usage event counted before approved/delivered outreach | P1 | fixed in code (`a8bff54`), `tests_pass` local (2026-10-13), not deployed: metered only at delivered (0133); owner to confirm the billable stage | was `backend/speed_to_lead.py:387-389`; now `backend/outreach_lifecycle.py` (`BILLABLE_ENGAGEMENT_STAGE`), `backend/billing_usage.py` (`record_lead_engagement`, `BILLABLE_PREDICATE`) | 18 | L18-02, L04-01 |
| A06 | Customer names in failure logs | P1 | open. 2026-10-13: fixed with a source guard, local (L17-04); addresses and agent emails not covered | `backend/client_ai_automation.py:701-703` logs `full_name` | 17 | L17-04 |
| A07 | Concurrent messaging route initialization | P1 | fixed in code at `c52b4dc`, local real-PostgreSQL race test only (2026-10-13, L10-06); not deployed, migration 0136 unapplied outside the local dev DB; 10DLC brand/campaign re-submission still open | was `backend/messaging_data.py:108` (`ON CONFLICT … DO UPDATE SET provider_account_id`); now `ON CONFLICT … DO NOTHING` + re-read, and the hosted order goes through `provider_purchases` (`_claim_hosted_order`) | 10 | L10-06 |
| A08 | Heuristic valuation targets in model training | P1 | open in code | `backend/ml_forge/edge_forge/train_lora.py:207` (`_COUNTY_PPSF`), `:209` (`_MAO_RULE = 0.70`) | 11 | — |
| A09 | AI capability descriptions exceed executable tooling | P1 | allowlist re-verified; UI and persona wording not re-verified. **2026-10-13 (base `3bcab84`): fixed locally, `tests_pass` (L07-01)** — one registry derived from the allowlist drives the persona, the API and the Personal AI page; a guard test fails on any unbacked claim. Playwright check still owed | allowlist at `backend/ai_chat_store.py:605`, `:646`; 126 catalogued vs 57 allowlisted tools (L07-01); registry `backend/ai_capabilities.py`, guard `backend/tests/test_ai_capabilities.py` | 07 | L07-01 |
| A10 | Local capacity test is not target-production evidence | P1 | open | `docs/capacity-plan.md:129` (nothing measured on DigitalOcean) | 16 | L16-01, L19-02 |
