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
| 03 | Brokerage tenancy and hybrid access | [03](03-brokerage-tenancy-hybrid-access.md) | `source_verified` | staging | 0131 (L03-02), 0134+0137 (L03-04) staging; 0140 (L03-05) local; 0142 drafts (L03-06), 0143 leads (L03-07), 0145 live frames (L03-08) local | Owner: team leads (L03-03); Platform Use Policy wording for 0143 (L03-07); lead assignee / listings (L03-07) | Owner approves the 0143 policy wording; then the remaining lead_id children (L03-03) |
| 04 | CRM, contacts, leads, relationships | [04](04-crm-contact-leads-relationships.md) | `tests_pass` | local | `a8bff54` (L04-01) | — | T04.01 + T04.13 (L04-01 next) |
| 05 | Semantic intent | [05](05-semantic-language-intent.md) | `source_verified` | local | `745f2fa` | — | L05-01 |
| 06 | Evidence retrieval and memory | [06](06-evidence-retrieval-memory.md) | `source_verified` | local | `745f2fa` | A01, A02 | L06-01 |
| 07 | AI gateway and governed tools | [07](07-ai-gateway-model-tool-governance.md) | `tests_pass` | local | `071d65d` (L07-01) | Playwright check owed (L07-01); owner: archive_client | T07.02 + AC07.01 |
| 08 | Durable missions and workflows | [08](08-durable-missions-workflow-orchestration.md) | `source_verified` | local | `745f2fa` | PR #11 review; grant-release decision | L08-01 |
| 09 | Real-time voice | [09](09-realtime-voice-speech-latency.md) | `source_verified` | staging | `9c3463a` | Call-note broadcast (L09-03); phone check (L09-02) | L09-03 |
| 10 | Communications and calendars | [10](10-communications-calendar-provider-integrations.md) | `source_verified` | staging | `75c6ba6` (local) | Google OAuth client; 10DLC; deploy approval; A07 fixed locally, 0136 + 0139 + 0145 undeployed (L10-06, L10-09, L10-10); owner: one-brand rule (L10-09) | L10-01 |
| 11 | Property and market intelligence (non-MLS) | [11](11-property-market-intelligence.md) | `tests_pass` | staging (L11-01), local (L11-02, L11-03) | `90372f2`; `d934b2e` (L11-02); L11-03 on branch `worktree-agent-a59f5c8799a673f68` (base `2804f47`) | Deploy 0135; live-DB test owed; staging catalog empty; L11-03 not deployed | Apply 0135 on staging; push L11-03 with approval; then T11.02 provenance |
| 12 | Neoh Space capture, 3D, tours | [12](12-neoh-space-browser-capture-reconstruction.md) | `source_verified` | local | `745f2fa` | A03 | Capture Studio first slice |
| 13 | Deals, documents, compliance | [13](13-transactions-contract-vault-compliance.md) | `source_verified` | local | `745f2fa` | Owner: e-sign, commissions | L13-01 |
| 14 | Sites, social, ads, client portal | [14](14-websites-social-ads-growth-client-portal.md) | `source_verified` | local | `745f2fa` | Owner: networks, listings on sites | L14-01 |
| 15 | Neoh mascot animation | [15](15-neoh-avatar-skeletal-character-animation.md) | `source_verified` | local | `745f2fa` | Owner: rig author | L15-01 |
| 16 | Performance, indexes, cache | [16](16-performance-indexes-mobile-cache.md) | `tests_pass` | local | `745f2fa`; 0141 tenant wall (L16-01, 2026-10-13); 0113 predicates dropped + 0144 (L16-03, 2026-10-14) | A10 capacity on production-shaped infra; no production target; 0141/0144 undeployed | JIT on request queries (L16-03); then T16.05 |
| 17 | Security, privacy, data lifecycle | [17](17-security-privacy-compliance-data-lifecycle.md) | `source_verified` | staging | `0739563`; card-number redaction / SAQ A (L17-07, local, 2026-10-14) | A01, A06; owner: policy wording for the AI payment-card rule (L17-07) | Read-only review with 02 |
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
  - {cmd: "review after 0142 (L03-06)", result: "command_executions + action_approvals now follow their client/contact (L03-06). Still brokerage-wide: leads, lead_intake_events, media, interaction_logs rows linked only by lead_id; compliance tables by design; approvals not about a command (contract documents: draft_payload has no target) stay unlinked.", date: 2026-10-14}
  - {cmd: "review after 0140 (L03-05): grep of every reader/writer of the communications tables (scratchpad inv.py over backend/*.py, excluding tests)", result: "sms_messages, email_outbox, live_call_sessions (+ negotiation_events), inbound_voice_calls, contract_synthesis_artifacts, smart_plan_enrollments (+ smart_plan_step_runs), agent_call_intents, lead_response_events, outcome_events and mission_candidates now follow their client/contact (L03-05). Still brokerage-wide: leads, lead_intake_events, media, interaction_logs rows linked only by lead_id; outreach_consent/_suppression/_attempt_log and suppression_tombstones (by design: compliance); and command_executions + action_approvals — GET /api/commands (commands_api.commands) lists every agent's staged drafts (bodies and targets) to any agent in the brokerage (action_approvals' list is filtered to the requester in approval_service, its rows are not); the client/contact ids live only inside the target jsonb, so scoping them needs link columns first.", date: 2026-10-14}
  - {cmd: "review after 0143 (L03-07): grep of every reader/writer of leads, lead_intake_events, property_media, interaction_logs (scratchpad inv.py over backend/*.py) and of the tables with a lead_id foreign key", result: "leads (seller_client_id), lead_intake_events (contact_id), interaction_logs and property_media rows on a lead now follow their client/contact (L03-07); harvested prospects stay the pool (D-2026-10-13-01). Still brokerage-wide: listings and listing-only media (owner question in L03-07); the other lead_id children — client_portals, contract_documents, property_floorplans, property_view_upload_links, reconstruction_jobs, spatial_tour_variants, marketplace_publications, capture_sessions, property_pano_scenes, video_studio_jobs — and email_outbox/live_call_sessions rows linked ONLY by lead_id (0140 follows their client/contact links); lead_pipeline_counts (aggregate); media_blobs has no RLS by design (0022) and every reader joins it through property_media (media_api, media_storage, video_studio, reconstruction_worker); compliance tables by design; command_executions/action_approvals (parallel slice, 0142)", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: design, note: "Communications tables feed consent, opt-out and frequency checks, which must stay brokerage-wide; scoping them needs a split between 'see the message' and 'check compliance'. 0134 adds the pattern: a function that sets app.record_scope='brokerage' for its own duration and returns no PII (app_client_contact_place, app_open_contact_counts)."}
  - {type: owner, note: "Team leads (T03.02): multiple teams per agent? edit or read-only on team records?"}
next_task: "The remaining lead_id children (client_portals, property_floorplans, tours/reconstruction, upload links): follow the lead as 0143 does for media; then listings once the owner answers (L03-07)."
updated: 2026-10-14
```

#### L03-04 Agent-scoped contacts and intake tables (D-2026-10-13-01)
```yaml
id: L03-04
status: tests_pass
env: staging
evidence_shas: [631b786, 86c9cb7, e99446f, b3518eb]
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
  - {cmd: "staging after b3518eb (migration head 0137), real API as the demo owner and agent", result: "demo contacts were seeded assigned to the owner while their clients are in the pool (the drift 0137 now prevents); moved to the pool with their clients (owner PATCH, 6 contacts) so the agent sees them again. Owner assigns Sarah to self -> her contact follows (0137) -> agent GET 404; owner shares the contact view-only -> agent sees it, /access = view, PATCH -> 403 'shared with you to view only'; share revoked, Sarah and contact back in the pool", date: 2026-10-14}
  - {cmd: "real Chrome (Playwright) as the demo agent on /work?type=people, desktop 1440 + phone 390, Sarah's contact shared view-only by the owner (restored afterwards)", result: "'Shared with you by Jordan Miles: view only.'; no share form; no Save; fields readOnly (now styled as text, they looked editable). Screenshots performance/out/access03/agent-contact-view-only-*.png", date: 2026-10-14}
authz_checked: "agent_contacts + contact_property_relationships, contact_intake_sessions, intake_handoff_tasks, contact_nurture_jobs under FORCE RLS as oracle_app; clients reassignment guard"
blockers:
  - {type: decided, note: "Owner 2026-10-13: reassigning a client moves its linked contact. 0137 (trigger clients_carry_contact_assignee) does it in the same transaction through app_may_reassign, skipping (never failing) a contact the caller may not move or an inactive assignee; tests/test_contact_follows_client_live.py."}
  - {type: decided, note: "Owner 2026-10-13: Platform Use Policy v3 wording approved as drafted."}
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "Communications tables (L03-05, in progress); then leads (L03-03)."
updated: 2026-10-13
```
How it works: the 0131 pattern on `agent_contacts`. `assigned_user_id` is derived from `assigned_agent_id` by a trigger that also replaces 0059's (active members only, 23503 → 422). Four restrictive policies, inline; the intake tables inherit. `record_shares` takes `resource_kind='contact'`; `/api/crm/contacts/{id}/access|shares` mirror the client routes. An agent-created contact is not auto-assigned; one created for an assigned client stays with that client's agent, so a private client's details never land in the pool. Reads that are compliance or brokerage functions, not "seeing the contact", go through a function that sets `app.record_scope='brokerage'` only while it runs and returns no PII: quiet-hours state/timezone (`app_client_contact_place`) and lead-routing workload (`app_open_contact_counts`). Consent, opt-out and frequency (`outreach_*`) are untouched and stay brokerage-wide. Inbound SMS/voice matching and intake already ran as `brokerage_scope`, lead intake as the owner, speed-to-lead and smart-plan workers as platform admin, privacy flows as the owner; the outbound-call webhook and the smart-plan step act as the agent, so a reassigned contact is refused at execution (C3.4). Also fixed (F03.02): an edit share let its holder assign a colleague's client to themselves; `app_may_reassign()` now guards both triggers and `record_access` refuses it first.

#### L03-05 Communications follow their client or contact; compliance stays brokerage-wide (D-2026-10-13-01)
```yaml
id: L03-05
status: tests_pass
env: local
evidence_shas: [12fe9de]  # branch worktree-agent-af76b40ede58acfb0, unpushed
files: [backend/db/migrations/0140_communications_follow_the_record.sql, backend/record_access.py, backend/commands_api.py, backend/voice_intel.py, backend/media_api.py, backend/sales_api.py, backend/telephony_api.py, backend/missions/executor.py, backend/inbound_voice.py, backend/tests/test_communications_access_matrix_live.py, backend/tests/test_inbound_voice.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "livetest.sh migrate (0140 on the local dev DB) + pytest tests/test_communications_access_matrix_live.py (real PostgreSQL 16.14, app role)", result: "2 passed: for all 12 tables agent A / B / C(view share) / owner / platform admin / brokerage pipeline / other brokerage see exactly the rows of the records they can see (+ the unlinked text); policy == app_linked_access() for every actor and row, view and edit; a view share cannot update a text or add a call-transcript event (42501), an agent cannot log a text on a colleague's client (42501), a view share may add a ledger fact (outcome_events) but not about an invisible client; API: call consent/telemetry, voice session, contract list/download, enrollment pause, agent-call prepare answer 404 when invisible and 403 when view-only; inbound call on A's own line from B's client is B's; the real inbound-SMS path (messaging_data.record_inbound_message as brokerage scope) still links client+contact; A cannot read B's STOP thread and is still blocked by it (only blocker: opt-out list); B's 3 AI calls make A's 4th 'frequency cap reached (3/3 calls in 24h)'; a spoken opt-out (inbound_voice._record_voice_opt_out) blocks A on sms and voice; pg_policies: one permissive tenant wall + four restrictive per table, compliance tables unchanged", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE as oracle_app, agent A, throwaway brokerage of 21,000 clients + 21,000 linked contacts (7k A / 7k B / 7k pool, 100 view shares) and 63,000 linked texts, rolled back (scratchpad bench0140.sql)", result: "newest 50 texts: 0.13 ms tenant wall only -> 60 ms with 0140 (hashed sub-plans over the caller's 14,050 visible clients and contacts); one client's timeline 0.9 ms (own) / 1.0 ms (colleague's, 0 rows); app_linked_access read-back for one row 7 ms; owner 0.6 ms (short-circuit); visible count over all 63k texts 153 ms with jit=off but 1.2-1.5 s with the server default jit=on (the plan's estimated cost crosses jit_above_cost; tenant wall alone 80 ms) — no agent route counts or scans sms_messages today", date: 2026-10-14}
  - {cmd: "rls_security_review / rls_brokerage / platform_rls_test / privacy_lifecycle .sql on the local dev DB", result: "all PASS (exit 0)", date: 2026-10-14}
  - {cmd: "full backend suite (ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/)", result: "3421 passed, 23 skipped, 0 failed", date: 2026-10-14}
  - {cmd: "CI live list locally (15 files incl. the new one)", result: "31 passed, 1 failed: test_migration_precheck (the shared dev DB holds other sessions' 0139/0141, not in this branch) — environmental, as in L03-04", date: 2026-10-14}
  - {cmd: "owner decisions 2026-10-14", result: "Platform Use Policy v4 paragraph approved as drafted (neoh-platform-use-policy-2026-10-14-v4, POLICY_COVERAGE row for 0140); messages on an agent's own line from a colleague's client stay hidden from the line owner (consistent with the record rule)", date: 2026-10-14}
authz_checked: "sms_messages, email_outbox, live_call_sessions, negotiation_events, inbound_voice_calls, contract_synthesis_artifacts, smart_plan_enrollments, smart_plan_step_runs, agent_call_intents, lead_response_events, outcome_events, mission_candidates under FORCE RLS as oracle_app; outreach_consent/_suppression/_attempt_log/suppression_tombstones verified unchanged"
blockers:
  - {type: owner, note: "Platform Use Policy wording for 0140 drafted in the session report, not applied (CONTRACT C5.7 asks for it in the same change; the owner approves wording and the version bump)."}
  - {type: owner, note: "Calls and texts that arrive on an agent's own line from a colleague's client are now the colleague's record and hidden from the line's owner (consistent with 0131's child tables). Keep, or let an agent always see their own line?"}
  - {type: design, note: "Visible-count queries over large text volumes cross jit_above_cost (1.2 s with JIT vs 153 ms without). No agent route does this today; if one is added, page it or set jit off for the app role."}
audit_refs: [A01]
decision_refs: [D-2026-10-13-01]
next_task: "Owner approves the policy paragraph; then command_executions/action_approvals (L03-03)."
updated: 2026-10-14
```
How it works: restrictive `<t>_linked_read/_insert/_update/_delete` policies, inline. Read: the linked client is visible AND the linked contact is visible (each EXISTS runs under that table's own RLS, so 0131/0134 decide); a NULL link does not restrict. Write: the same, editable — except the ledgers (lead_response_events, outcome_events, mission_candidates), where a fact about a visible record may always be recorded. negotiation_events and smart_plan_step_runs inherit their parent row. Admin, owner and brokerage-scope pipelines short-circuit. `app_linked_access()` is the read-back for 404 vs 403 (`record_access.require_linked_edit`). Compliance is untouched by design: `ConsentLedger.gather_state` reads only `outreach_*`/`suppression_tombstones`, never the scoped tables. Found and fixed on the way: (1) a spoken "stop calling me" on an inbound call set only that contact's `suppression` jsonb, which the outreach gate never reads — every other send path to the number went on (now also `outreach_suppression` '*', in the finalize transaction, `inbound_voice._record_voice_opt_out`); (2) any agent could download any client's generated contract by id (now 404); (3) the contract listing answered 500 for any stored artifact (jsonb read as text, `media_api._metadata`); (4) `mission_candidates.client_id` was never written (backfilled; `missions/executor._client_uuid`); (5) the Twilio agent-call status receipt now records as a brokerage pipeline so a reassignment mid-call cannot drop it (C4.5). Outreach (calls, enrollments, contracts, call consent) now needs edit, not a view share.

#### L03-06 Staged drafts and their approvals follow the record (D-2026-10-13-01)
```yaml
id: L03-06
status: tests_pass
env: staging
evidence_shas: []  # this slice's commit on branch worktree-agent-a7df430df0144db4c, unpushed (SHA in the session report)
files: [backend/db/migrations/0142_drafts_follow_the_record.sql, backend/commands_api.py, backend/approval_service.py, backend/record_access.py, backend/policy_contract.py, backend/privacy_data_map.py, docs/privacy-data-map.md, backend/tests/test_command_drafts_access_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "livetest.sh migrate (0142) + pytest tests/test_command_drafts_access_live.py (real PostgreSQL 16.14, app role, local dev DB)", result: "2 passed: links derived on insert by the trigger for both tables (API path, and a previous-release INSERT without the columns; record scope back to 'actor' after it; a writer cannot null a link; another brokerage's client links nothing); GET /api/commands and approval rows by id: A / B / C(view share) / owner / admin / pipeline / other brokerage see exactly the drafts about records they can see (+ unlinked ones); app_linked_access read-back == policy for every actor and draft, view and edit; detail 404 invisible / 200 view share; edit, approve, reject 404 invisible / 403 view share; a colleague's draft about a POOL client is readable but edit/approve/reject answer 403 (not the requester); staging about an invisible record 404, view share 403; retargeting your own draft at a colleague's client 404; the DB refuses the view-share insert (42501) and update (UPDATE 0); the requester may reject their own; execution re-checks the requester (pool client reassigned after staging -> the worker stops before the outreach gate, state failed 'Not sent: ...', no provider reference; a non-member system requester is checked as an agent: pool only); deleting a client unlinks its drafts (FK SET NULL with the trigger); pg_policies: one permissive tenant wall + four restrictive per table, the trigger on both. Mutation check: removing the edit-decider rule fails the test (200 != 403)", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE as oracle_app, throwaway brokerage of 21,000 clients (7k A / 7k B / 7k pool), 21,000 drafts + 21,000 approvals, rolled back (scratchpad s0142/bench*.sql)", result: "GET /api/commands newest 100, agent A: 63 ms before 0142 (sees all 21,000, top-N sort); with 0142 and the old ORDER BY created_at 1.7-2.4 s (policy sub-plans costed per row -> JIT 1.5-2.3 s); with idx_command_executions_tenant_created + ORDER BY tenant_id, created_at DESC 22-54 ms (owner 0.7 ms); one draft by id 0.5 ms. Agent's own approvals (~5% of 21,000): 323 ms before 0142; 3.2 s with JIT; 154 ms JIT off; 103 ms JIT off + expression statistics on lower(requested_by) (both lists now SET LOCAL jit = off). Platform count of failed/succeeded drafts (operator_api shape): 20 ms JIT off, 150-210 ms with JIT on (cost 458k)", date: 2026-10-14}
  - {cmd: "non-superuser rehearsal (scratchpad s0142/rehearse0142.sh): scratch DB, 0001-0141 as a throwaway superuser, one linked draft + approval seeded, REASSIGN OWNED to a NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner", result: "0142 applied; backfill linked the draft and its approval; updated_at unchanged; app_link_draft_target owned by the owner, proconfig search_path only, oracle_app EXECUTE; 8 policies. Scratch DB and roles dropped", date: 2026-10-14}
  - {cmd: "previous release (e3ea107 backend exported) live tests on the 0142 schema: engagement_lifecycle, privacy_lifecycle, reconciliation, communications/brokerage/contact matrices", result: "8 passed, 1 failed: the brokerage matrix policy composition sees interaction_logs_lead_* policies from ANOTHER session's 0143 in the shared dev DB (environmental)", date: 2026-10-14}
  - {cmd: "full backend suite (ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/)", result: "3471 passed, 32 skipped, 0 failed", date: 2026-10-14}
  - {cmd: "CI live list locally (17 files incl. the new one)", result: "39 passed, 2 failed, both environmental: test_migration_precheck (the dev DB holds other sessions' 0143-0145) and the brokerage matrix composition (0143's interaction_logs lead policies)", date: 2026-10-14}
  - {cmd: "rls_security_review / rls_brokerage / platform_rls_test / privacy_lifecycle / rls_tenant_wall .sql on the local dev DB; bandit with the CI flags and baseline", result: "all PASS (exit 0); bandit exit 0", date: 2026-10-14}
  - {cmd: "staging after 0a76a72 (CI run 38535851793 green; PRE_DEPLOY applied 0142-0145 as doadmin; rehearsed first as a NOSUPERUSER BYPASSRLS owner), real API as the demo owner and agent (scratchpad smoke.py + smoke_r3.py)", result: "Policy v5 served and accepted; clients 6 / contacts 6 (pool) for both; GET /api/commands owner 2, agent 2 (pool drafts readable); /api/commands/approvals owner 2, agent 0 (own requests only); property records 200; MLS 404; each route 157-185 ms", date: 2026-10-14}
authz_checked: "command_executions + action_approvals under FORCE RLS as oracle_app; commands_api detail/list/edit/approve/reject/stage; approval_service.list_approvals; the command worker's execution-time requester check"
blockers:
  - {type: design, note: "JIT: PostgreSQL costs the 0140/0142 EXISTS policies as a per-row lookup (the correlated alternative of the hashed sub-plan it then runs), ~22-34 cost units per row, so a scan of a few thousand rows on these tables crosses jit_above_cost and compiles for 0.1-2.3 s. The two agent lists set jit off locally; operator/health counts over command_executions (operator_api, component_health) and 0140's tables are still exposed. Taken at merge: tenancy.apply_rls_context sets jit off for every request transaction (set_config is_local, both context paths; tests/test_tenancy.py::test_rls_context_turns_jit_off_for_the_transaction)."}
  - {type: decided, note: "Owner 2026-10-14: keep; drafts expire as before. A pending draft whose client is reassigned keeps its requester's approval: the new assignee can see it but not decide it, the old requester can no longer see it, and it expires (24 h default) unless the broker owner acts. Cancel such drafts on reassignment instead?"}
audit_refs: [A01]
decision_refs: [D-2026-10-13-01]
next_task: "leads (L03-07)"
updated: 2026-10-14
```
How it works: `client_id`/`contact_id` (tenant-bound FKs, ON DELETE SET NULL of the link only) on both tables, set by `trg_command_executions_link` / `trg_action_approvals_link` from `target` / `draft_payload->target` on every insert and on any update of the target or the link columns, so the previous release (which never writes them) and this one both link every row, and no writer can disagree with the target. The lookup runs with `app.record_scope='brokerage'` for its own duration (set_config in plpgsql, restored), because it must find records the caller cannot see. The 0140 restrictive policies, inline: read = linked client AND contact visible, write = editable; a NULL link does not restrict; admin/owner/brokerage scope short-circuit. The API never restates the rule: `record_access.require_linked_edit(conn, 'command_executions', id)` answers 404/403 before edit, approve and reject (and before the locking read, which would hide a view-only row as 404); staging checks the target's client/contact with `require_client_edit`/`require_contact_edit`. Who decides is unchanged (the requester or a broker owner, `approval_service._require_decider`), and editing now needs the same right (`require_decider`), because an edit re-requests the approval as the editor. At execution (C3.4) the worker re-checks `app_linked_access` as the approval's requester in their own role. Found and fixed on the way: (1) any agent who could see a draft could edit it, which re-created its approval as their own request, and then approve it (now 403); (2) both lists sorted every visible row, and with the new policies JIT compiled for 1.5-3 s (now tenant-led order + index + jit off); (3) nothing re-checked at execution that the requester could still contact the record (C3.4).

#### L03-08 Live frames follow the record (CONTRACT C3.6)
```yaml
id: L03-08
status: tests_pass
env: staging
evidence_shas: [91b7e38]  # branch worktree-agent-a5563e55e5fb840d8, unpushed
files: [backend/db/migrations/0145_realtime_audience_and_number_assignment.sql, backend/ws_hub.py, backend/record_access.py, backend/commands_api.py, backend/voice_intel.py, backend/tests/test_realtime_fanout_scope.py, backend/tests/test_realtime_record_scope_live.py, backend/tests/test_call_note_review.py, backend/tests/test_call_notes_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "grep/AST inventory of every ws_hub / WebSocket send in backend/ (tests/test_realtime_fanout_scope.py::test_every_tenant_wide_fanout_is_classified)", result: "client/contact content sent tenant-wide at e3ea107: NEGOTIATION_TELEMETRY (commands_api.call_telemetry: counter offer, ARV, rehab, MAO), CALL_CONSENT (record_call_consent), VOICE_SESSION and VOICE_TELEMETRY (voice_intel: the transcript line, offer math, objection draft) — each also mirrored IN FULL to the platform firehose, and the voice-session group mirrored there too. Already scoped: CALL_NOTES (broadcast_user to requester + assignee), AI chat and JOB_PROGRESS of ai_chat jobs (broadcast_user). Tenant-wide by classification (8 call sites pinned with reasons): lead frames VOICE_NOTE_LOGGED, DOSSIER_MOVED, CONTRACT_EXPIRED/DANGER (leads are brokerage-wide, L03-03), SPLAT_READY and VIDEO_READY (property media), JOB_PROGRESS (ids + stage text), workflow-engine ambient frames (public records, simulated transcript). Platform-admin-only sockets (audit trail, surge, AWS observability) are global by design. No SSE endpoint exists.", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_realtime_record_scope_live.py with call_telemetry temporarily back on ws_hub.broadcast (BEFORE)", result: "1 failed: 'agent B received a colleague's private-client call' (NEGOTIATION_TELEMETRY with the counter offer)", date: 2026-10-14}
  - {cmd: "livetest.sh migrate (0145 on the local dev DB) + livetest.sh python -m pytest -q -p no:cacheprovider tests/test_realtime_record_scope_live.py tests/test_call_notes_live.py (AFTER; real PostgreSQL, FORCE RLS, app role)", result: "2 passed. app_record_audience == app_linked_access(read) for every member (owner, assignee A, B, view share C, contact edit share D, revoked share E, expired share F) x 16 client/contact combinations; another brokerage's agent never listed; unknown record = nobody; same answer under a platform-admin caller. Real routes + real hub: A's private client -> owner, A and C get NEGOTIATION_TELEMETRY, CALL_CONSENT, VOICE_TELEMETRY; B and the other brokerage's same-id socket get nothing; the firehose gets only {type, scoped, source_tenant}. Pool client -> owner, A, B, C all get all three. B posting to A's call: 404 and no frame anywhere. The voice-session stream still gets its frame and is never mirrored. CALL_NOTES: requester with an edit share + assignee; a requester who cannot read the client gets nothing", date: 2026-10-14}
  - {cmd: "pytest tests/test_realtime_fanout_scope.py tests/test_call_note_review.py (unit)", result: "PASS: exact delivery, case-insensitive agent ids, only-narrows-never-widens, audience failure -> actor only, empty audience -> nobody, one NOTIFY per frame, v2 envelope has no 'payload' key (a previous-release replica drops it instead of fanning out tenant-wide), malformed v2 envelopes ignored", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE SELECT app_record_audience(client, NULL) as oracle_app, brokerage scope, local dev DB: largest brokerage by members (100 users, 21,268 clients), rolled back (scratchpad bench0145.sql)", result: "assigned client: 29 ms cold (first call of the function in the session), 4.1-4.8 ms warm; pool client 3.8-4.0 ms. One query per frame, not per socket", date: 2026-10-14}
  - {cmd: "non-superuser rehearsal: scratch DB, migrations <= 0141 as a throwaway superuser, REASSIGN OWNED to a NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner (scratchpad rehearse0145.sh; DB and roles dropped)", result: "0145 applied (1 processed); function owned by the owner, EXECUTE granted to oracle_app, proconfig only search_path; kind CHECK widened", date: 2026-10-14}
  - {cmd: "rls_security_review / rls_brokerage / platform_rls_test / privacy_lifecycle / rls_tenant_wall .sql on the local dev DB", result: "all exit 0", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "3481 passed, 35 skipped, 0 failed (live-DB tests skipped without ORACLE_LIVE_DB_ADMIN_DSN); bandit (CI flags, baseline) exit 0", date: 2026-10-14}
  - {cmd: "staging after 0a76a72 (CI run 38535851793 green; PRE_DEPLOY applied 0142-0145 as doadmin; rehearsed first as a NOSUPERUSER BYPASSRLS owner), real API as the demo owner and agent (scratchpad smoke.py + smoke_r3.py)", result: "Policy v5 served and accepted; clients 6 / contacts 6 (pool) for both; GET /api/commands owner 2, agent 2 (pool drafts readable); /api/commands/approvals owner 2, agent 0 (own requests only); property records 200; MLS 404; each route 157-185 ms", date: 2026-10-14}
authz_checked: "Delivery rule = app_linked_access for read, per member (0140), computed by app_record_audience in a brokerage-scope pipeline context of the record's tenant (SECURITY INVOKER, explicit GRANT to oracle_app, no app.* SET clause). Verified under FORCE RLS as oracle_app."
blockers:
  - {type: design, note: "Voice-session stream sockets (/ws/voice-telemetry/{id}) are admitted under RLS at connect and are not re-checked per frame: a share revoked mid-call keeps receiving that one call's stream until the socket closes. The main /ws frames are re-checked per frame."}
  - {type: design, note: "Lead frames (VOICE_NOTE_LOGGED, DOSSIER_MOVED, CONTRACT_*) stay tenant-wide because leads are brokerage-wide (L03-03). When leads are agent-scoped, move them to broadcast_record with a lead link."}
  - {type: decided, note: "Owner 2026-10-14: the platform-admin OPS firehose keeps a FULL copy of record-scoped frames (as before 0145), once per frame: the voice-session stream is not mirrored a second time, and CALL_NOTES stays unmirrored (it was broadcast_user before). ws_hub.firehose_copy; broadcast_record(mirror=False) for call notes; tests/test_realtime_fanout_scope.py::test_an_unmirrored_record_frame_never_reaches_the_firehose."}
  - {type: deploy, note: "0145 must apply before this code serves (record_access.linked_audience calls app_record_audience; without it the frame goes to the acting user only — fail closed, not open). During a rolling deploy a previous-release replica drops scoped cross-replica frames (logged as malformed) instead of delivering them."}
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "Voice-stream re-check per frame; lead frames once leads are scoped (L03-03)."
updated: 2026-10-14
```
How it works: `ws_hub.broadcast_record(tenant, frame, client_id=, contact_id=, actor=, only=)` asks `record_access.linked_audience()` once per frame → `app_record_audience()` (0145): NULL = every member (no link, or both links in the pool: delivered exactly as `broadcast()` did), else the agent ids of active broker owners + members who pass `app_linked_access(…, false)`. `broadcast_users()` delivers to those agents' sockets on this replica and sends ONE NOTIFY (v2 envelope: `scoped_payload` + `user_ids`, no `payload` key) for the others; the platform firehose gets `{type, scoped: true, source_tenant}`. If the audience cannot be computed the frame goes to the acting user only. CALL_NOTES reuses the same path with `only=` (requester + assignee ∩ who may still read the client) instead of its own `broadcast_user` loop; the voice-session group now uses `broadcast_group` (never mirrored). The Platform Use Policy v4 already says calls, call notes and transcripts "follow that record: you see them only while you can see the record" — no new wording.

#### L03-07 A lead that is a client's house follows the client; harvested prospects stay the pool (D-2026-10-13-01)
```yaml
id: L03-07
status: tests_pass
env: staging
evidence_shas: []  # this commit, branch worktree-agent-aff8b0bd0b8d151a9, unpushed
files: [backend/db/migrations/0143_leads_follow_the_record.sql, backend/record_access.py, backend/lead_dossier.py, backend/disposition_enforcer.py, backend/media_api.py, backend/property_view_api.py, backend/tour_api.py, backend/crm.py, backend/ai_chat_store.py, backend/admin_ops.py, backend/db/connection.py, oracle-app/src/state/useOracleWebSocket.js, backend/tests/test_lead_record_access_live.py, backend/tests/test_brokerage_access_matrix_live.py, backend/tests/test_media_delete.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "facts re-verified on the local dev DB (\\d leads, counts)", result: "leads has no assignee column; seller_client_id (0012, FK clients ON DELETE SET NULL, idx_leads_seller_client); 10,462,293 leads across the dev tenants, 3 with seller_client_id; no contact column (a contact links to a lead only through contact_property_relationships, already the contact's since 0134). lead_intake_events.contact_id; property_media.lead_id/listing_id; interaction_logs.lead_id/client_id. Harvest (harvesters/base.persist_leads), disposition_enforcer, reconstruction/video/client-upload workers and lead intake write as platform admin or broker owner", date: 2026-10-14}
  - {cmd: "livetest.sh migrate (0143 on the local dev DB) + pytest tests/test_lead_record_access_live.py (real PostgreSQL 16.14, app role)", result: "2 passed: agent A / B / C (view share of B's client) / owner / platform admin / brokerage pipeline / other brokerage see exactly the leads of clients they can see plus the pool's and the harvested prospect; interaction_logs and property_media rows on a lead follow it; listing-only media unchanged; lead_intake_events follow their contact; the pipeline page predicate (app_visible_tenants) lists pool + harvested + own only; policy == app_linked_access() == record_access.linked_access_level for every actor and lead; a view share cannot update the house, delete its photo (0 rows) or add a log row / photo (42501); an agent cannot attach a house to a colleague's client (INSERT or UPDATE of seller_client_id, 42501); the harvest upsert (persist_leads' statement, platform admin) still refreshes a client's house and keeps its link; API: move_dossier, delete_media, create_upload_link 404 (invisible) / 403 (view-only), no file removed; create_subject on a colleague's client's address 409 (was a 500 from the hidden duplicate), the owner of it gets the existing lead; DOSSIER_MOVED for a client's house names no address/parcel; pg_policies: one permissive tenant wall + four restrictive per table", date: 2026-10-14}
  - {cmd: "CI live list locally (17 files incl. the new one)", result: "40 passed, 1 failed: test_migration_precheck (the shared dev DB holds other sessions' 0106/0142/0144/0145, not in this branch) — environmental, as in L03-04/L03-05. test_brokerage_access_matrix_live's composition check now expects interaction_logs' four 0143 lead policies besides 0131's client ones", date: 2026-10-14}
  - {cmd: "rls_security_review / rls_brokerage / platform_rls_test / privacy_lifecycle / rls_tenant_wall .sql on the local dev DB with 0143", result: "all exit 0. rls_tenant_wall A: 126 policies use the wall; D: 124 tables x 7 contexts identical (leads compared on the seeded brokerages, whose leads have no client); no change to the proof needed — the restrictive policies are the same in both of D's phases", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE as oracle_app (agent, owner on oracle_app_login; platform admin on oracle_platform_login), tenant 0000 = 10.46M leads; interleaved A/B, three runs each: A = 0143's read policy dropped inside a rolled-back transaction, B = deployed (scratchpad l0307/gen_ab.py)", result: "with the server default jit=on every lead query got slower — pipeline page 160 -> 1,222 ms (agent), 133 -> 1,354 ms (admin), capped search count 2.4 -> 3.7 s — and the plan was unchanged: the time was JIT compilation (1,099 ms of a 1,324 ms page; 48 functions), not execution. With jit=off (now the app's connection setting): pipeline page agent 46.7 -> 42.6 ms, owner 29.9 -> 26.3, admin 7.0 -> 7.1; page + search 'smith' 451 -> 382 ms; capped search count 1.93 -> 2.12 s (agent), 1.50 -> 1.73 (owner), 1.64 -> 1.46 (admin); state+hot count 1.4 -> 2.0 ms; lead by id 0.1 -> 0.7 ms; graph seed 0.3 -> 0.6 ms; map (5k-lead brokerage) 0.4-0.6 -> 0.5-0.7 ms; client_interactions 0.7 -> 1.7 ms; media for a lead 0.1 -> 0.9 ms. The tenant wall stays the index condition (idx_leads_pipeline_rank / idx_leads_tenant / tenant_rank). Harvest ON CONFLICT DO UPDATE of 500 parcels as admin: 1.0-1.9 s both (trigger-dominated)", date: 2026-10-14}
  - {cmd: "same, the one index-only path: admin_ops /overview count(*) FROM leads per tenant", result: "0143 makes leads queries read seller_client_id, so a count can no longer be index-only: 35.3 s -> 48.7 s for the 10.46M-lead tenant — but the route has a 5 s statement_timeout, so it already answered 503 before 0143 (pre-existing). Now reads lead_pipeline_counts (the 0038 rollup the pipeline and AI tools already treat as the authority): 0.2 ms", date: 2026-10-14}
  - {cmd: "non-superuser rehearsal (scratchpad l0307/rehearse0143.sh): scratch DB, 0001-0141 as a throwaway superuser, REASSIGN OWNED to a NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner", result: "0143 applied; 4 policies on each of leads, lead_intake_events, interaction_logs, property_media; scratch DB and roles dropped", date: 2026-10-14}
  - {cmd: "full backend suite; bandit as CI runs it; vitest + eslint + build", result: "backend 3473 passed, 32 skipped, 0 failed (the first run caught 13: test_migration_ledger wanted a probeable object, so leads' four policies are now literal CREATE POLICY; and route fakes that only knew the old existence check); bandit (CI flags, baseline) clean; vitest 768 passed; eslint clean; vite build ok; bundle within budget", date: 2026-10-14}
  - {cmd: "staging after 0a76a72 (CI run 38535851793 green; PRE_DEPLOY applied 0142-0145 as doadmin; rehearsed first as a NOSUPERUSER BYPASSRLS owner), real API as the demo owner and agent (scratchpad smoke.py + smoke_r3.py)", result: "Policy v5 served and accepted; clients 6 / contacts 6 (pool) for both; GET /api/commands owner 2, agent 2 (pool drafts readable); /api/commands/approvals owner 2, agent 0 (own requests only); property records 200; MLS 404; each route 157-185 ms", date: 2026-10-14}
authz_checked: "leads, lead_intake_events, interaction_logs (lead link), property_media (lead link) under FORCE RLS as oracle_app; listings, listing-only media and lead_pipeline_counts verified unchanged"
blockers:
  - {type: decided, note: "Owner 2026-10-14: Platform Use Policy v5 wording approved (neoh-platform-use-policy-2026-10-14-v5; section 2 paragraph on property records linked to a client and on drafts; POLICY_COVERAGE rows for 0142 and 0143)."}
  - {type: owner, note: "Leads have no assignee: every harvested prospect is the brokerage pool. Should an agent be able to claim a prospect (a new assignee concept on leads), or does a lead become private only by being linked to a client? (Not built — a new owner decision.)"}
  - {type: owner, note: "Listings keep brokerage-wide visibility even when their seller is a private client (a listing is marketed and shared across brokerages through listing_grants); media attached only to a listing follows the listing. Keep, or should a listing follow its seller client like its lead?"}
  - {type: owner, note: "lead_pipeline_counts (per tenant/state/stage rollup, no row identity) still counts a private client's house, so a colleague's totals can exceed the rows they can open by those houses. Keep?"}
audit_refs: [A01]
decision_refs: [D-2026-10-13-01]
next_task: "Owner answers; then the remaining lead children (L03-03)."
updated: 2026-10-14
```
How it works: the 0140 pattern, restrictive inline policies. `leads`: read when `seller_client_id` is NULL or that client is visible (the EXISTS runs under clients' own RLS); write when NULL or editable. `interaction_logs` (beside 0131's client policies) and `property_media`: a row with a `lead_id` follows that lead (read: the lead is visible; write: its client is editable). `lead_intake_events` follow `contact_id`. A NULL link never restricts, so harvested prospects, their photos and log rows read exactly as before; admin, owner and brokerage-scope pipelines short-circuit. For an agent the InitPlans and `seller_client_id IS NULL` settle every harvested row before the EXISTS (a clients primary-key probe) is reached. `app_linked_access(seller_client_id, NULL, …)` is the read-back (`record_access.require_lead_edit`). Deliberately unchanged: a contact's link to a lead (contact_property_relationships, already the contact's), listings, `lead_pipeline_counts`. Found and fixed on the way: (1) with RLS on the leads table the default `jit=on` compiled ~50 functions per lead query — about 1.1 s on every pipeline page — so the app's pools now set `jit=off` (`db/connection._SERVER_SETTINGS`; 0140 saw the same on texts); (2) `DELETE /api/crm/media/{id}` and `DELETE /api/crm/space` removed the stored files BEFORE the row delete that RLS refuses for a view-only share — now 404/403 first, nothing removed; (3) DOSSIER_MOVED / CONTRACT_DANGER / CONTRACT_EXPIRED are sent to every session in the brokerage and named the house — for a client's house they now name none (feed: "a client's property"); (4) `/admin/overview` counted 10M leads per tenant against a 5 s timeout (503 before this change) — now the rollup; (5) typing the address of a colleague's client's house in Property View hit the hidden duplicate (500) — now 409, and linking a public record whose lead is a colleague's client's house still answers 409 (the hidden copy would otherwise have read as 404).

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
next_task: "Owner: confirm the invariant (first committed route wins; a provider/account switch only by disconnect + an explicit reconnect, which does not exist yet). The brand/campaign duplicate found here is fixed locally in L10-09."
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

#### L10-09 10DLC brand and campaign registration never creates a duplicate (A07 follow-up)
```yaml
id: L10-09
status: tests_pass
env: local
evidence_shas: [2804f47, 00f94f8]
files: [backend/messaging_data.py, backend/messaging_api.py, backend/provider_purchases.py, backend/privacy_data_map.py, backend/db/migrations/0139_10dlc_registration_ledger.sql, backend/tests/test_messaging_registration_race_live.py, .github/workflows/ci.yml, docs/provider-failure-matrix.md]
tests_run:
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_messaging_registration_race_live.py (real PostgreSQL 16, FORCE RLS, app role; code at 2804f47, BEFORE the fix; mock adapter that counts calls)", result: "7 failed. 6 racing brand PUTs from 2 broker owners of one brokerage = 6 brands registered at the (mock) provider for one tenant_messaging_brands row (5 orphaned); 6 racing campaign PUTs = 6 campaigns; a timed-out brand left no record and the next PUT submitted again; a rejected brand left no failure on the row; the ledger could not hold a 10DLC kind (CHECK)", date: 2026-10-13}
  - {cmd: "livetest.sh migrate; livetest.sh python -m pytest -q -p no:cacheprovider tests/test_messaging_registration_race_live.py tests/test_messaging_route_race_live.py (after the fix, 0139 applied to the local dev DB; registration test run 4 times)", result: "13 passed; registration test 7 passed each run: 6 racing brand PUTs = 1 provider call, 1 x 200 + 5 x 409, ledger telnyx/10dlc_brand/attached; a later correction = 409 'A brand is already registered for this number; contact support to change it.' with 0 calls; timeout = unknown, the next PUT is 409 with 0 calls; a definite rejection = brand row failed, a corrected retry registers once; a confirmed-but-unrecorded brand is attached with 0 calls; the same for campaigns (1 call for 6 racers, 409 afterwards, unknown blocks, rejection retries)", date: 2026-10-13}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_reconciliation_live.py tests/test_on_conflict_targets_live.py tests/test_privacy_lifecycle_live.py", result: "5 passed", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0: 3421 passed, 28 skipped (live-DB tests skipped without ORACLE_LIVE_DB_ADMIN_DSN)", date: 2026-10-13}
authz_checked: "No route, role or RLS change: both PUTs still require BROKER_OWNER and an active subscription; the claim, the provider-outcome writes and the attach run in the caller's tenant_tx; tenant_messaging_brands/_campaigns and provider_purchases keep their 0108/0123 tenant policies and grants. 0139 only widens provider_purchases' kind CHECK (no new function, table or column, nothing superuser-only). The live test uses throwaway tenants under the app role and removes them (0 left)."
blockers:
  - {type: owner, note: "Decision to confirm (safe default chosen, not an owner decision): one 10DLC brand and one campaign per brokerage at the provider, ever. Once a provider id exists, any further PUT is 409 'contact support' — including a correction, a brand that failed vetting and a campaign the carriers rejected — because no adapter can update or resubmit in place. Alternatives: add Telnyx brand update/revet and campaign update/appeal to the adapter (needs a sandbox check), or allow a new campaign after a rejection."}
  - {type: deploy, note: "Migration 0139 must apply before this code runs (it writes kinds 10dlc_brand / 10dlc_campaign). Applied only to the local dev DB."}
  - {type: provider, note: "An `unknown` brand/campaign has no automatic resolver: reconciliation.py turns a stale `intended` into `unknown` but does not ask Telnyx. Operator resolves it (docs/provider-failure-matrix.md row '10DLC brand/campaign timeout'). No real Telnyx call was made."}
audit_refs: [A07]
decision_refs: []
next_task: "Owner: confirm or change the one-brand/one-campaign rule (blocker above). Then, with approval, deploy 0136 + 0139 and the code together."
notes: "Found in L10-06. Before: register_brand called adapter.create_brand on every PUT and then overwrote provider_brand_id (ON CONFLICT DO UPDATE); register_campaign inserted a new row and a new provider campaign on every call. Now (messaging_data.register_messaging_brand / register_messaging_campaign): the brand row (UNIQUE tenant_id) is created if missing and locked FOR UPDATE, which serialises every broker owner of the brokerage; under it an existing provider id is a 409, an open ledger row of any agent ('intended'/'unknown') is a 409, a 'confirmed' one is reused, otherwise the intent is inserted (the brand fields are written in the same transaction, before the provider is asked). The provider is asked once outside the transaction: a definite refusal (ProviderRejectedError, configuration, recovery mode, NotImplemented) = ledger failed (+ brand row status failed with the reason), retry allowed; anything else (timeout, lost or malformed answer, ProviderRequestError such as 'no brand id returned') = unknown, 502, blocks. The attach writes provider_brand_id (only if still NULL) or inserts the campaign row (only if none has a provider id) and closes the ledger row in one transaction. The brand/campaign status is stored only if the schema knows it, else 'pending' (before, an unrecognised Telnyx identity_status would have violated the CHECK after the brand was created, orphaning it). GET /business/registration adds open_submissions (in_progress / unconfirmed). Not changed: assign_campaign_number has no ledger (re-assigning the same number to the same campaign); the per-agent in-flight index is not brokerage-wide on its own (the brand-row lock is the brokerage-wide guard). No UI calls these routes yet (grep oracle-app/src)."
updated: 2026-10-13
```

#### L10-10 Assigning the number to the 10DLC campaign reaches the carrier once (A07 follow-up)
```yaml
id: L10-10
status: tests_pass
env: staging
evidence_shas: [91b7e38]  # branch worktree-agent-a5563e55e5fb840d8, unpushed
files: [backend/messaging_data.py, backend/messaging_api.py, backend/provider_purchases.py, backend/db/migrations/0145_realtime_audience_and_number_assignment.sql, backend/tests/test_messaging_registration_race_live.py, docs/provider-failure-matrix.md]
tests_run:
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_messaging_registration_race_live.py -k racing_number with messaging_api.py at e3ea107 (BEFORE; mock adapter that counts calls)", result: "1 failed: 6 concurrent assign-number POSTs from one broker owner = 6 assign_number_to_campaign calls at the (mock) provider for one number and one campaign", date: 2026-10-14}
  - {cmd: "livetest.sh migrate (0145) + livetest.sh python -m pytest -q -p no:cacheprovider tests/test_messaging_registration_race_live.py (AFTER; real PostgreSQL, FORCE RLS, app role)", result: "11 passed (7 L10-09 + 4 new): 6 racing assignments = 1 provider call, 1 x 200 + 5 x 409, ledger telnyx/10dlc_number_assignment/attached, route.campaign_id set; a later click = 200 already_assigned with 0 calls and no new ledger row; timeout = 502 'until an operator has checked it', ledger unknown, the next click 409 (operator) with 0 calls, route untouched, GET /business/registration open_submissions = number_assignment/unconfirmed; a definite rejection = failed, the retry assigns once; a confirmed-but-unrecorded assignment is attached with 0 calls", date: 2026-10-14}
  - {cmd: "staging after 0a76a72 (CI run 38535851793 green; PRE_DEPLOY applied 0142-0145 as doadmin; rehearsed first as a NOSUPERUSER BYPASSRLS owner), real API as the demo owner and agent (scratchpad smoke.py + smoke_r3.py)", result: "Policy v5 served and accepted; clients 6 / contacts 6 (pool) for both; GET /api/commands owner 2, agent 2 (pool drafts readable); /api/commands/approvals owner 2, agent 0 (own requests only); property records 200; MLS 404; each route 157-185 ms", date: 2026-10-14}
authz_checked: "No route, role or RLS change: still BROKER_OWNER + active subscription; the claim, outcome and attach run in the caller's tenant_tx; provider_purchases keeps its 0123 tenant policy and grants; 0145 only widens its kind CHECK. No real provider was called (mock adapter)."
blockers:
  - {type: deploy, note: "0145 must apply before this code runs (it writes kind 10dlc_number_assignment). Applied only to the local dev DB."}
  - {type: provider, note: "An `unknown` assignment has no automatic resolver (reconciliation.py turns a stale `intended` into `unknown` but does not ask Telnyx). Operator procedure: docs/provider-failure-matrix.md row '10DLC number assignment timeout'."}
  - {type: design, note: "'already_assigned' is answered from the ledger; an assignment removed at Telnyx outside Neoh is not detected, and moving a number to a different campaign is a new (number, campaign) pair that is submitted."}
audit_refs: [A07]
decision_refs: []
next_task: "With approval, deploy 0136 + 0139 + 0145 and the code together; then L10-01."
updated: 2026-10-14
```
How it works (`messaging_data.assign_campaign_number`): under the brokerage's latest approved campaign row `FOR UPDATE` (serialises every broker owner), the ledger rows for this number + provider campaign id decide: `unknown` → 409 (operator), `intended` → 409 (in flight), `attached` → done (no provider call), `confirmed` → reused; otherwise an `intended` row is inserted (detail: campaign id + the brokerage's own business number, as kind `phone_number` already stores) and committed before the provider is asked once outside the transaction (`_ask_registration`: definite refusal = failed, anything else = unknown + 502). The route's campaign_id and the ledger's `attached` are written in one transaction. Before: every POST called Telnyx.

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

#### L11-03 The remaining failure-as-empty sites: geocoding, eviction, enrichment, market rates, AVM, footprints (A02)
```yaml
id: L11-03
status: tests_pass
env: local
evidence_shas: [2804f47]
branch: worktree-agent-a59f5c8799a673f68
files: [backend/source_status.py, backend/data_integrations/census_geocoder.py, backend/data_integrations/eviction_lab.py, backend/data_sources_api.py, backend/apis/geocoding.py, backend/server.py, backend/apis/property_data.py, backend/apis/market_data.py, backend/avm_client.py, backend/agents/agent_analyst.py, backend/data_integrations/building_footprint.py, backend/floorplan_api.py, backend/tests/test_source_status_a02.py, oracle-app/src/lib/sourceStatus.js, oracle-app/src/lib/sourceStatus.test.js, oracle-app/src/components/PropertyViewTab.jsx, oracle-app/src/components/PropertyViewTab.test.jsx, oracle-app/src/components/RehabEditorDrawer.jsx, oracle-app/src/components/RehabEditorDrawer.test.jsx, oracle-app/bundle-budget.json]
tests_run:
  - {cmd: "re-read each site listed in the L11-02 note at 2804f47", result: "all still open at HEAD; plus three found on the way: census/eviction fetch() returned None on error, which DataSource.get caches as a verified empty (EMPTY_RESULT_TTL 1 h); FEMA ArcGIS HTTP-200 {error}, Overpass `remark` timeouts and Walk Score status!=1 were cached for up to 30 days as 'unmapped' / '0 POIs' / 'no score'; auto-dimensions built CascadingGeocoder() with no cache, so it raised before any request, OSM was never asked and 'no footprint found' was reported without a lookup; /api/geocode (Property View) answered 404 'address not found' for a Nominatim outage", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_source_status_a02.py", result: "48 passed: per site, an injected timeout / HTTP error / upstream error body is timed_out|unavailable with a stable reason code, the response is degraded or 502, nothing failed is cached (second call reaches the source again), a real miss stays checked; no log line carries the address; FRED key not logged", date: 2026-10-13}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0: 3469 passed, 21 skipped (live-DB tests skipped: no ORACLE_LIVE_DB_ADMIN_DSN)", date: 2026-10-13}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "vitest 766 passed (82 files); eslint exit 0; build ok; budget first failed by +2.7 KB raw (total 4087.6 KB vs 4085.0), raised with --update for the per-source wording, then within budget", date: 2026-10-13}
authz_checked: "no route, role or RLS change; no migration; no new table or column (privacy data map unchanged). Routes keep require_context; footprint routes still check the subject under tenant_tx before any lookup."
blockers:
  - {type: deploy, note: "Not pushed or deployed (pushing to main deploys staging, C4.4). No migration needed."}
  - {type: test, note: "No real provider was called (all mocked). Staging check owed: force a source down (e.g. unset WALKSCORE_API_KEY, expired REGRID token) and confirm Property View and the footprint picker read 'Couldn't check' / 'Not configured'."}
audit_refs: [A02]
decision_refs: [D-2026-10-12-01]
next_task: "Push with approval and verify on staging; then T11.02 provenance. Owner: should data_integrations/geocoder.py (CascadingGeocoder, Census→Nominatim, now without importers) be deleted?"
notes: "Shared vocabulary backend/source_status.py (C6 statuses + stable reason codes; SourceFailure is raised inside cache fetchers so a failure is never stored). Response fields are ADDED, none renamed: lookup_status/lookup_reason on rows and envelopes, source_status per source, degraded. Sites: census_geocoder.geocode_checked (:269) + fetch raises (:238) + batch rows unverified when any pass failed (:408, :508); /api/data/geocode 502 on outage, 200 checked miss (data_sources_api.py:188); /api/data/geocode/batch unchecked/unmatched_checked counts, 502 when nothing was checked (:219); eviction_lab.by_fips_checked (:230) + /api/data/eviction 502 vs not_applicable vs checked (data_sources_api.py:289); apis/geocoding.geocode_checked (:40) + /api/geocode 502 vs 404 (server.py:772); property_data _nearby_pois_checked total_pois None not 0 and `error` is a reason code, not exception text (:64), _flood_zone_checked (:142), _walkscore_checked (:241), enrich_property source_status/degraded (:310); market_data _fred_latest_checked (:58), _treasury_rates_checked (:297), get_market_snapshot source_status/degraded (:342); AVMResult.lookup_status/source_status/degraded (avm_client.py:81), only HTTP 404 or a zero price is a verified 'no estimate' (:125), analyst status line says which (agents/agent_analyst.py); building_footprint *_checked + footprint_lookup found|none_found|incomplete|not_checked (:211, :276, :375, :406); floorplan footprint-candidates lookup/source_status (floorplan_api.py:649), auto-dimensions geocodes through the Census geocoder + IntegrationCache and reports geocode_status and a failed-geocode OSM as unavailable (:700, :748). UI: PropertyViewTab reads `flood` (it read a non-existent `flood_zone`, so the flood cell was always '—') and `pois.total_pois` (it read `pois.length`), and shows Couldn't check / Not configured / Not mapped by FEMA / Not checked; geocoder outage is not 'could not be located'; RehabEditorDrawer footprint picker and auto-fill notice say 'couldn't check' for not_checked/incomplete. Also removed addresses from the touched log lines (census fetch, Nominatim, AVM HTTP errors, footprint resolver, auto-dimensions). Not changed: state_compliance/routes_market flood fallback (already says 'could not be determined', but still treats a FEMA outage like 'unmapped'); reconstruction_scale.py logs a truncated address on footprint failure (category 12); data_integrations/geocoder.py keeps its swallow-and-cache fetch (no importer left)."
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
status: tests_pass   # the 0141 tenant-wall slice, local only; A10 production-shaped capacity stays open (blocker below)
env: local
evidence_shas: [745f2fa]   # finding; the fix is the commit that adds backend/db/migrations/0141_indexable_tenant_wall.sql (branch worktree-agent-a6f0278f657dfc229, unpushed)
files: [backend/db/connection.py, backend/crm.py, backend/search_api.py, oracle-app/src/sw-oracle.js, oracle-app/src/neoh/UniversalWorkspace.jsx, docs/capacity-plan.md,
        backend/db/migrations/0141_indexable_tenant_wall.sql, backend/tests/rls_tenant_wall.sql, backend/tests/privacy_lifecycle.sql, performance/rls_tenant_wall_plans.sh, .github/workflows/ci.yml]
tests_run:
  - {cmd: "grep -n AbortController oracle-app/src/neoh/UniversalWorkspace.jsx; sed -n 127,134p docs/capacity-plan.md", result: "Work search has no request cancellation; cached splats are served without a rights check until logout (sw-oracle.js:268); nothing measured on DigitalOcean", date: 2026-10-12}
  - {cmd: "livetest.sh migrate (shared local dev DB)", result: "0141 applied: 128 permissive tenant policies rewritten in place (names kept) to USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling()); WITH CHECK kept verbatim; 3 left on the OR form (audit_ledger: nullable tenant_id; user_interactions/user_profiles: text tenant_id); policies of other shapes untouched", date: 2026-10-13}
  - {cmd: "psql -U postgres -d oracle -v ON_ERROR_STOP=1 -f tests/rls_tenant_wall.sql (local dev DB, 52+2 tenants, 9.05M leads)", result: "exit 0, twice. A: 128 policies, one canonical expression, all on uuid NOT NULL, write checks unchanged, no eligible table left on the OR form, bounds STABLE invoker plpgsql granted to oracle_app only. B: 0 mismatches old vs new over 57 tenant ids (incl. an id with no tenant row and both uuid extremes) in 7 contexts: agent A, owner A, agent B, platform admin on oracle_platform_login, forged platform_admin GUC on oracle_app_login (sees 1 tenant, as before), agent role on the platform login, no context. C: client count, client page, phone-hash lookup use an index (custom + forced generic) for agent, owner and forged-GUC contexts. D: 124 tables x 7 contexts identical visible rows (count + ctid hash) under the new policies and, flipped back in the same REPEATABLE READ transaction, the old ones (leads restricted to the seeded brokerages)", date: 2026-10-13}
  - {cmd: "psql -f tests/rls_security_review.sql; rls_brokerage.sql; platform_rls_test.sql; privacy_lifecycle.sql (local dev DB, 0141 applied)", result: "all exit 0. privacy_lifecycle's RLS-coverage check now also accepts the exact 0141 wall (it matched the text app_is_platform_admin()); it passes before and after 0141", date: 2026-10-13}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider <the 14 live files in ci.yml>", result: "29 passed, 1 failed: test_migration_precheck refuses the shared dev DB because it records 0106 (abandoned), 0139 and 0140 that this branch does not contain — environmental, unrelated to 0141; access matrices (brokerage + contact) pass", date: 2026-10-13}
  - {cmd: "python -m pytest -q -p no:cacheprovider tests/ (backend, ORACLE_SKIP_DOTENV=1)", result: "3421 passed, 21 skipped, exit 0", date: 2026-10-13}
  - {cmd: "performance/rls_tenant_wall_plans.sh (local dev DB; EXPLAIN ANALYZE as oracle_app_login / oracle_platform_login; throwaway 21,000-contact brokerage beside a 253-client one; one run, single machine shared with other work, no concurrency)", result: "pre-0141 -> 0141. Small agent (253 clients): client count 14.8 -> 2.1 ms (seq scan of 35,961 -> bitmap index); client page 14.4 -> 0.9 ms; phone-hash lookup 3.0 -> 0.1 ms, generic plan 54.8 -> 0.2 ms (seq -> index); leads pipeline page >5 s (cancelled) -> 1.3 ms; generic client count 30.9 -> 0.8 ms. Big owner (21,268 clients): count 65.0 -> 15.4 ms, page 56.0 -> 15.9 ms, generic count 62.7 -> 14.9 ms. Forged admin GUC: same as the small agent (count 22.0 -> 1.1 ms; leads >5 s -> 0.2 ms). Platform admin: count 57.1 -> 22.2 ms. Unchanged: child rows by client_id 0.3-0.5 ms, client by id 0.1-0.2 ms. Regressions: big agent/owner leads page 0.2-0.6 -> 2.5-6.4 ms (tenant index + sort instead of the global ranking index; bounded by the brokerage's size, not the platform's); per-row policy cost on a forced seq scan 43-58 -> 115-120 ms for 36k rows (two plpgsql calls, ~2 us/row) for owner/admin, unchanged for agents", date: 2026-10-13}
  - {cmd: "EXPLAIN ANALYZE x3 of crm.list_clients page selection with/without the 0113 predicate, pre-0141 vs 0141", result: "with the predicate (current code): big owner 58-80 -> 28-42 ms, small agent 3.2-4.0 -> 3.7-4.9 ms; without it on 0141: 27-30 ms and 1.6-1.8 ms — the restated predicate is now redundant and costs ~2-3 ms", date: 2026-10-13}
authz_checked: "tenant isolation proven equivalent per policy and context (B, D above), incl. a forged platform_admin GUC on the request login; restrictive agent-scope policies (0131/0134) unchanged and still AND"
blockers:
  - {type: owner, note: "No production-shaped target exists yet (L19-02). A10 (capacity on production-shaped infrastructure) stays open: these are local single-run plans, not capacity evidence."}
  - {type: owner, note: "0141 is local only: unpushed branch; pushing to main deploys staging (CONTRACT C4.4). On staging it runs as doadmin (table owner), one transaction, lock_timeout 5 s, ACCESS EXCLUSIVE per table briefly."}
  - {type: scope, note: "Not rewritten: audit_ledger (nullable tenant_id), user_interactions/user_profiles (text tenant_id), the 5 tenant+user policies (ai_chat_messages, ai_chat_actions, ai_tool_operations, agent_decisions, autonomy_preferences) and other non-canonical shapes. tenant_id = ANY (app_visible_tenants()) (0113, still used by crm.list_clients and server.push_deal_pipeline) hides rows whose tenant row is gone from a platform admin; the dev DB has such rows (15 lead_pipeline_counts, 120 tenant_contract_template_registrations)."}
audit_refs: [A10]
decision_refs: []
next_task: "Done in L16-03 (0113 predicates dropped, plan proof, 0144, T16.04 inventory). Remaining here: T16.10 protocol on staging once 0141/0144 are deployed."
updated: 2026-10-14
```

#### L16-03 The tenant wall is not restated; duplicate tenant policies are gone; hot-query index inventory
```yaml
id: L16-03
status: tests_pass   # local only; unpushed branch. 0144 undeployed
env: staging
evidence_shas: []   # the commit that adds backend/db/migrations/0144_drop_duplicate_tenant_policies.sql and tests/test_tenant_wall_plans_live.py
files: [backend/crm.py, backend/server.py, backend/db/migrations/0144_drop_duplicate_tenant_policies.sql, backend/tests/test_tenant_wall_plans_live.py,
        backend/tests/test_client_list_query_shape.py, backend/tests/test_migration_ledger.py, backend/tests/rls_tenant_wall.sql, .github/workflows/ci.yml]
tests_run:
  - {cmd: "pg_policies on the local dev DB at 0141; fresh-chain rehearsal DB through 0141", result: "agent_licenses carries tenant_isolation (0013/0017) AND agent_licenses_tenant_isolation (0027); transactions carries tenant_isolation AND transactions_tenant_isolation (0039) on the dev DB only — it recorded 0017 after 0039 (2026-08-18); a fresh chain (= staging, CI) has only the 0039 one on transactions. Each pair is IDENTICAL: PERMISSIVE, ALL, PUBLIC, USING the 0141 wall, WITH CHECK the old OR form. Neither grants anything extra; no other table has a duplicate permissive policy", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE x3 (median) of the real crm.list_clients page selection (sort recent / score) and server.push_deal_pipeline page + capped count (priority=hot), with vs without `tenant_id = ANY (app_visible_tenants())`, 0141 applied, as oracle_app_login / oracle_platform_login, custom | forced generic plan (local dev DB, one machine shared with other agents, no concurrency)", result: "small agent (253 clients, 50 leads): client page 2.0 -> 1.2 | 2.0 -> 1.8 ms, pipeline page 4.5 -> 2.8 | 3.5 -> 1.8 ms, capped count 0.9 -> 0.2 | 0.7 -> 0.3 ms. Big owner (21,268 clients, 5,000 leads): client page 53.5 -> 24.6 | 28.7 -> 32.5 ms, pipeline page 6.6 -> 5.6 | 8.3 -> 6.6 ms. Big agent: client page 19.7 -> 18.0 | 15.5 -> 17.1 ms, pipeline page 6.5 -> 9.2 | 7.2 -> 9.9 ms (both on tenant indexes; within run-to-run noise). Platform admin: client page 67.3 -> 33.6 | 40.8 -> 58.7 ms; pipeline page 1371 -> 1247 ms custom, > 20 s generic either way (see blockers). Every non-admin plan stays on a tenant index; small contexts consistently faster, large ones within noise", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_tenant_wall_plans_live.py", result: "2 passed: SQL captured from the real handlers (list_clients recent/score, push_deal_pipeline page, page + hot filter, capped count) planned custom + generic for agent, owner and forged-admin-GUC contexts of a 250-client/50-lead brokerage beside a 20,000/20,000 one: target table reached through a tenant index whose Index Cond is the 0141 wall, no seq scan, no non-tenant index; client sets with and without the old predicate identical in 4 contexts, admin's a superset", date: 2026-10-14}
  - {cmd: "rehearsal: scratch DB, migrations through 0141 as a throwaway superuser, REASSIGN OWNED to a NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner (scratch DB and roles dropped)", result: "0144 applied as the non-superuser owner; agent_licenses.tenant_isolation dropped, transactions untouched (had no twin); 1 processed", date: 2026-10-14}
  - {cmd: "psql -U postgres -d oracle -v ON_ERROR_STOP=1 -f tests/rls_tenant_wall.sql (local dev DB, 0144 applied)", result: "exit 0 (first two attempts deadlocked against another agent's long UPDATE on the shared dev DB in D's ALTER POLICY; environmental). A: 126 policies (128 - the 2 dropped twins), new check: no table has two identical permissive policies (verified it reports agent_licenses x2 when a twin is recreated). B, C, D unchanged and passing. New E: recreating the twin on agent_licenses and transactions changes no visible row (count + ctid hash) in the 7 contexts", date: 2026-10-14}
  - {cmd: "psql -f tests/rls_security_review.sql; rls_brokerage.sql; platform_rls_test.sql; privacy_lifecycle.sql (local dev DB, 0144 applied)", result: "all exit 0", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider <the 17 live files in ci.yml>", result: "39 passed, 2 failed, both environmental on the shared dev DB: test_migration_precheck (foreign ledger rows 0106/0142/0143/0145) and test_brokerage_access_matrix_live::test_policy_composition_cannot_widen (interaction_logs_lead_* policies from another branch's 0143); judge on merged main", date: 2026-10-14}
  - {cmd: "python -m pytest -q -p no:cacheprovider tests/ (backend, ORACLE_SKIP_DOTENV=1); bandit as in ci.yml", result: "3472 passed, 32 skipped; bandit exit 0 against .bandit-baseline.json", date: 2026-10-14}
  - {cmd: "T16.04 inventory: EXPLAIN (ANALYZE, BUFFERS) as oracle_app_login of client detail (notes, tasks, timeline, last touch), client page, task queue, approvals (agent own / owner pending / all), latest SMS (local dev DB; small tables: approvals 21k rows over 8 tenants, tasks 4.4k, sms 37k, record_shares 0)", result: "Client detail reads are child-by-client_id index scans, 0.3-2.0 ms. Every hot list uses a tenant index. Findings, no index added: (1) JIT. jit is on (jit_above_cost 100000) and RLS-inflated estimates cross it on small results: an agent's own approvals (3 rows) 3,022 ms of which ~2.8 s JIT compile and 98 ms execution (seq scan of a 203 MB, 268k-dead-tuple dev table); owner approvals 389-466 ms; the platform-admin pipeline page ~1.2 s of JIT (its ORDER BY updated_at does not match idx_leads_pipeline_rank's created_at, so the estimate is a 10.9M-row sort). EXPLAIN with COSTS OFF hides the JIT section. (2) Child-table read policies (0131) for a broker owner hash every client of the brokerage: task queue 135 ms for 2,536 tasks of a 21,268-client brokerage (17 ms building the hash, rest per row) — the owner/brokerage short-circuit that 0140's linked_read policies have is missing there. (3) record_shares is seq-scanned inside every agent-scope policy; it is empty here, idx_record_shares_grantee exists and is chosen once seq scan is off — re-check when shares exist", date: 2026-10-14}
  - {cmd: "staging after 0a76a72 (CI run 38535851793 green; PRE_DEPLOY applied 0142-0145 as doadmin; rehearsed first as a NOSUPERUSER BYPASSRLS owner), real API as the demo owner and agent (scratchpad smoke.py + smoke_r3.py)", result: "Policy v5 served and accepted; clients 6 / contacts 6 (pool) for both; GET /api/commands owner 2, agent 2 (pool drafts readable); /api/commands/approvals owner 2, agent 0 (own requests only); property records 200; MLS 404; each route 157-185 ms", date: 2026-10-14}
authz_checked: "visibility unchanged for every non-admin context (live test + D/E); the platform admin may additionally see rows whose tenant row is gone, as under the policy itself; write checks untouched (WITH CHECK identical on the dropped twins)"
blockers:
  - {type: owner, note: "Local only: unpushed branch; pushing to main deploys staging (CONTRACT C4.4). 0144 on staging (doadmin, fresh chain) drops agent_licenses.tenant_isolation only; ACCESS EXCLUSIVE on that table briefly."}
  - {type: scope, note: "Left in place on purpose (business or licence predicates, not a restated wall): agent_twin.py (one person's decision policy), autonomy.py (own settings), command_center.py (digest of the requester's brokerage), mls_enrichment/mls_health (per-brokerage MLS licence; MLS gated off), push_deal_pipeline's lead_pipeline_counts total and harvest_sources lookup (`tenant_id = $1` = ctx.tenant_id: for a platform admin the page lists every brokerage's leads but the total counts only the admin's own tenant — a pre-existing mismatch, not changed here)."}
  - {type: scope, note: "JIT and the child-policy owner short-circuit are measured findings, not fixed: turning JIT off for the request logins (connection server_settings or ALTER ROLE ... SET jit) changes every query and needs its own before/after; the policy change belongs with prompt 03."}
audit_refs: [A10]
decision_refs: []
next_task: "Measure jit=off for oracle_app_login/oracle_platform_login (approvals, admin pipeline, hot pages; before/after) and decide; add the owner/brokerage short-circuit to the 0131 child read policies with prompt 03; then T16.05."
updated: 2026-10-14
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
  - {cmd: "worker periodic task client_note_seal (data_integrations/periodic.py) + backfill_client_notes.seal_pending; live test extended", result: "the worker seals leftover plaintext notes in bounded restart-safe passes (default 2,000 rows / 30 min) using the key already in its environment, so the backfill needs no operator and no key copy; owner go-ahead 2026-10-14 ('continue with all'); --unseal stays a manual rollback", date: 2026-10-14}
  - {cmd: "staging after fe9c62c (read-only DB check + worker log)", result: "7 client notes still plaintext, 0 sealed, no client_note_seal job rows: the worker logs 'PeriodicScheduler NOT started: ORACLE_RECOVERY_MODE is set', and staging runs in recovery mode on purpose, so NO periodic job runs there. A deployment without recovery mode seals on the first pass. Not changed: turning recovery mode off would start every periodic job on staging (harvests, nurture, sweeps) at once.", date: 2026-10-14}
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
  - {cmd: "tests/test_log_pii_guard.py with address names added (address, street, situs_address, ...)", result: "10 log lines printed property addresses (geocoders, AVM, building footprints, floor-plan auto-dimensions, reconstruction scale); now outcome + error type only; guard 5 passed", date: 2026-10-14}
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

#### L17-06 ISO 27001 ISMS documentation, Annex A assessment and framework crosswalk (draft)
```yaml
id: L17-06
status: source_verified
env: local
evidence_shas: [d02e87d, b719fef, b693fbe]
files: [docs/compliance/README.md, docs/compliance/iso27001/01-scope-and-context.md, docs/compliance/iso27001/02-leadership-and-policy.md, docs/compliance/iso27001/03-risk-methodology-and-register.md, docs/compliance/iso27001/04-objectives-and-support.md, docs/compliance/iso27001/05-operation-evaluation-improvement.md, docs/compliance/iso27001/annex-a-assessment.md, docs/compliance/iso27001/statement-of-applicability.md, docs/compliance/framework-crosswalk.md, docs/compliance/certification-roadmap.md, docs/compliance/saas-checklist-map.md]
tests_run:
  - {cmd: "grep/read of every cited path:line at e3ea107 (three parallel reviews); lsblk; .env key-prefix count only", result: "Annex A 93 controls: 16 implemented, 43 partial, 5 gap, 14 inherited, 2 not_applicable, 13 owner_action. Risk register R01-R31 (5 critical: plaintext PII, single operator + unescrowed master key, scheduled controls off on staging (recovery mode), unencrypted workstation, sub-processors without DPAs). Applicability: PCI SAQ A (Stripe-hosted Checkout/Portal), HIPAA n/a, GDPR conditional (US-only hosting), CCPA/state laws conditional (leads-data role needs counsel)", date: 2026-10-14}
  - {cmd: "openssl s_client -tls1|-tls1_1|-tls1_2|-tls1_3 against staging; curl -I", result: "TLS 1.0/1.1 refused; 1.2 ECDHE-ECDSA-AES128-GCM-SHA256 and 1.3 TLS_AES_256_GCM_SHA384 accepted; HSTS max-age=31536000; includeSubDomains; strict CSP", date: 2026-10-14}
  - {cmd: "chmod 600 on the workstation's .env, three .env backups and oracle-app/.env (were 0644/0664; .env holds a live Stripe key — counted by prefix, never printed)", result: "owner-only; docker compose reads them as the same user", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: owner, note: "Certification is issued only by an accredited certification body after Stage 1 + Stage 2 audits of an ISMS that has operated with records (about 3 months minimum). Nothing in the repo is, or may be described as, ISO 27001 / SOC 2 compliant or certified (C12)."}
  - {type: decided, note: "Owner 2026-10-14: approved as v1.0 the scope (01), the information security policy and roles (02), the risk methodology and acceptance criteria (03) and the SoA; this starts the ISMS record period. Declined item 6 (full-disk encryption; moving dev off the live Stripe key): R18 retained at 15 until go-live, review by 2027-01-12. The approved criteria require treating it before production go-live. Production: not created yet; the owner asked about cost; staging components are about $105/month at list price (estimate). Create production about 3-4 months before Stage 2."}
  - {type: owner, note: "Create production (L19-02) about 3-4 months before Stage 2; escrow the master key; MFA + screenshots on GitHub/DO/Stripe/registrar; DPAs and replacing consumer Gmail SMTP; workstation full-disk encryption; rotate the live Stripe key out of dev; counsel (privacy notice, breach matrix, leads-data role); external pentest; certification-body quotes. Order and estimates: docs/compliance/certification-roadmap.md."}
audit_refs: [A01, A04, A06]
decision_refs: []
next_task: "Code gaps in flight: L17-07 card-number redaction, L17-08 CAN-SPAM unsubscribe, L17-09..13 privileged-activity alerts / HOOK-3 / log-PII / audit fallback + verify_chain / access review, L17-14..18 demo-login boot guard / password hashing / dependency audit + Dependabot / admin read logging / DR drill in CI. Then: tenant-user MFA (owner decision), field encryption of remaining PII (A01), scheduled off-host backups, log forwarding."

#### L17-08 Commercial email: unsubscribe link, RFC 8058 one-click, postal address (crosswalk T-CS-1)
```yaml
id: L17-08
status: tests_pass
env: local
evidence_shas: [475f40c]   # baseline; the fix is the commit that adds backend/commercial_email.py
files: [backend/commercial_email.py, backend/email_unsubscribe_api.py, backend/db/migrations/0146_brokerage_postal_address.sql, backend/email_outbox.py, backend/commands_api.py, backend/command_providers.py, backend/smtp_mailer.py, backend/brokerage_onboarding.py, backend/server.py, backend/csrf_middleware.py, backend/rate_limit_middleware.py, backend/log_redaction.py, backend/privacy_lifecycle.py, backend/privacy_data_map.py, backend/tests/test_commercial_email.py, backend/tests/test_email_unsubscribe_live.py, backend/tests/test_email_outbox.py, backend/tests/test_brokerage_onboarding.py, backend/tests/test_env_documentation.py, oracle-app/src/components/BrokerageSetupPanel.jsx, oracle-app/src/components/BrokerageSetupPanel.module.css, oracle-app/src/components/BrokerageSetupPanel.test.jsx, docs/privacy-data-map.md, docs/compliance/framework-crosswalk.md, .env.example, .github/workflows/ci.yml]
tests_run:
  - {cmd: "grep -rni 'unsubscribe|list-unsub|postal' backend/*.py; read email_outbox.deliver, commands_api._execute_command_job (EMAIL), smtp_mailer.build_message at 475f40c", result: "baseline confirmed: no unsubscribe link, no List-Unsubscribe header, no postal address; tenants has no address column; email_outbox.deliver did NOT re-check the opt-out list at send time (only crm.send_message at queue time)", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3520 passed, 42 skipped", date: 2026-10-14}
  - {cmd: "pytest tests/test_commercial_email.py tests/test_email_outbox.py", result: "29 + 8 passed: token round trip / tamper / foreign key / no PII in URL; raw-bytes header format; footer with and without address; every direct smtp_mailer.send caller classified; GET changes nothing, POST without login or CSRF token records, forged token 404, failed write 503; outbox blocked at send time after an opt-out", date: 2026-10-14}
  - {cmd: "livetest.sh migrate; livetest.sh python -m pytest -q tests/test_email_unsubscribe_live.py + the ci.yml live list", result: "applied 0146; 50 passed, 1 failed: test_migration_precheck (environmental: the shared dev DB records 0106_brokerage_setup.sql, a file in no branch)", date: 2026-10-14}
  - {cmd: "psql -f tests/{rls_security_review,rls_brokerage,platform_rls_test,privacy_lifecycle,rls_tenant_wall}.sql", result: "all 5 PASS", date: 2026-10-14}
  - {cmd: "non-superuser rehearsal: scratch DB, 0001-0145 as superuser, REASSIGN OWNED + DB owner to a NOSUPERUSER BYPASSRLS CREATEROLE role, run_migrations.py as that role", result: "applied 0146, exit 0; scratch DB and roles dropped", date: 2026-10-14}
  - {cmd: "bandit (CI flags) -b .bandit-baseline.json; npx vitest run; npm run lint; npm run build", result: "bandit exit 0; vitest 771 passed (82 files); lint clean; build ok", date: 2026-10-14}
authz_checked: "the unsubscribe writes outreach_suppression (tenant wall only, no agent narrowing, 0140) as a worker context; the live test proves agent B, who cannot see agent A's client, is blocked from emailing the same person after an unsubscribe from A's email (queued-before send blocked at delivery, new send 451, command gate denies), and that another brokerage is not bound"
blockers:
  - {type: owner, note: "Should client email be held while the brokerage has no postal address? Today it sends with the unsubscribe link and no address, and setup shows 'setup required' (commercial_email block of /api/brokerage/setup). No address is inferred; the 10DLC brand address (0108) is not reused without the owner's say."}
  - {type: owner, note: "Every client/lead email is treated as commercial (footer + headers), including deal updates; an email unsubscribe therefore also stops relationship email to an active client from every agent. Confirm, or name a transactional class."}
  - {type: operator, note: "Gmail/Yahoo require the List-Unsubscribe headers to be covered by the sender's DKIM signature; that is the SMTP relay's configuration (platform or brokerage), not verified. ydnhft.com SPF/DMARC were already missing (memory: Neoh Email & DNS)."}
  - {type: scope, note: "Rotating ORACLE_SECRET_KEY invalidates every unsubscribe link already sent (documented in .env.example); the page then asks the person to reply. The approval preview does not yet show the footer that is appended after the approved body."}
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "Owner answers the two questions; show the footer in the approval preview; after deploy, send one test email to the operator's own address and check the headers and DKIM in Gmail's 'show original'."
updated: 2026-10-14
```

#### L17-07 PCI DSS SAQ A scope: no card data outside Stripe
```yaml
id: L17-07
status: tests_pass
env: local
evidence_shas: [0a76a72]
files: [backend/pan_redaction.py, backend/audit_ledger.py, backend/server.py, backend/client_note_crypto.py, backend/commands_api.py, backend/voice_intel.py, backend/messaging_data.py, backend/outreach_compliance.py, backend/crm.py, backend/intelligence_os_api.py, backend/client_portal.py, backend/ai_chat_store.py, backend/ai_chat_agent.py, backend/memory_core/session_manager.py, backend/inbound_voice.py, backend/contacts_api.py, backend/elevenlabs_realtime.py, backend/neoh_persona.py, backend/qwen_omni_realtime.py, backend/qwen_voice_agent.py, backend/tests/test_pan_redaction.py, backend/tests/test_pan_redaction_paths.py, backend/tests/test_pan_redaction_live.py, backend/tests/test_neoh_persona.py, .github/workflows/ci.yml, docs/compliance/pci-saq-a.md]
tests_run:
  - {cmd: "grep billing.py / BillingOverlay.jsx / nginx.conf CSP at 0a76a72", result: "payment only via Stripe-hosted Checkout (stripe.checkout.Session.create → window.location.href) and the Stripe portal; no Stripe.js/Elements, CSP script-src has no js.stripe.com; billing.py stores no card fields. Nothing detected a card number arriving by call, text, email, note or chat.", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_pan_redaction.py tests/test_pan_redaction_paths.py", result: "exit 0; 91 + 19 passed. With the redactor disabled all 19 path tests fail (mutation check).", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q tests/test_pan_redaction_live.py + the ci.yml live list", result: "51 passed incl. the new live test (AI call note, inbound text, agent note under the app role; PCI_REDACTION audit rows hold counts only); tests/test_migration_precheck.py fails locally only because the shared dev DB records 0106 (no branch) and another agent's 0146", date: 2026-10-14}
  - {cmd: "bandit (CI flags: medium/medium, baseline)", result: "exit 0, no new findings", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3596 passed, 42 skipped. tests/test_neoh_persona.py::test_compact_is_substantially_smaller now measures the knowledge part (the whole-prompt ratio sat at 3.02 vs a bound of 3, so any rule both densities must carry failed it); held to 10x, measured ~20x", date: 2026-10-14}
authz_checked: "no access rule changed; the live test writes as the platform pipeline (call notes, inbound SMS) and as the app role under FORCE RLS (note route)"
blockers:
  - {type: scope, note: "Not redacted: uploaded files/images/PDFs and chat attachments, call audio (recording is off), free text outside the docs/compliance/pci-saq-a.md table (tasks, deal/showing notes, documents, leads.payload), rows written before this change (no backfill), and provider-side copies (Telnyx, ElevenLabs/ASR)."}
  - {type: scope, note: "Detection limits: spoken forms other than single digit words, dictated 13/14/19-digit cards, a card split across messages or turns. Known false positive: two phone numbers dictated digit by digit with nothing between (~1-2%, test bound < 3%)."}
  - {type: owner, note: "Platform Use Policy wording for the AI payment-card rule (drafted in the L17-07 report; policy not edited, version not bumped)."}
  - {type: operator, note: "The SAQ A itself is completed and signed by the operator; no QSA or acquirer review."}
audit_refs: [A01]
decision_refs: []
next_task: "Owner approves the policy sentence (then v-bump + POLICY_COVERAGE row for pan_redaction.PAYMENT_CARD_RULE); decide whether to scan existing rows (notes, sms_messages, transcripts) for historical card numbers."
updated: 2026-10-14
```
A card number arriving through any channel other than Stripe (a caller reading it to the AI, a text, an email, a note, a chat message) would put Neoh in SAQ D scope once stored, even encrypted. `pan_redaction.scrub` removes card numbers and security codes on every listed write path before storage, the model or a live frame. Logs are filtered on every handler. Each removal is recorded only as surface plus counts. Every voice and chat persona carries `PAYMENT_CARD_RULE`. Scope boundary, what keeps us in SAQ A and what would break it: [docs/compliance/pci-saq-a.md](../compliance/pci-saq-a.md).

#### L17-09 Security alerts for privileged activity (SoD compensation)
```yaml
id: L17-09
status: tests_pass
env: local
evidence_shas: [0a76a72]   # base; the alerts land in the commit that adds backend/security_alerts.py
files: [backend/security_alerts.py, backend/ops_alerts.py, backend/auth.py, backend/admin_ops.py, backend/server.py, backend/tests/test_security_alerts.py, .env.example, docs/privacy-data-map.md]
tests_run:
  - {cmd: "grep -n 'Successful login\\|ops_alerts\\|_notify' backend/auth.py backend/admin_ops.py (base 0a76a72)", result: "no alert on any sign-in, failure, role change or factor change; ops_alerts.py only alerted component health; the success log line printed agent_id (an email)", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_security_alerts.py", result: "9 passed (send mocked): operator sign-in alert carries id+IP+UTC time and no passphrase/code/seed; log line has actor_ref, no id/IP; 5 wrong passphrases -> 1 email (not per guess); DB platform_admin counted, agent not; rejected second factor alerts at once; 429 on the operator alerts once; role change + new owner (signup, owner invitation) alert; factor re-provisioned or switched totp->email alerts, baseline does not; secret never stored or sent; ORACLE_SECURITY_ALERTS_ENABLED=0 silences", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/ (covers L17-09..13)", result: "exit 0; 3514 passed, 43 skipped", date: 2026-10-14}
  - {cmd: "bandit -q -r backend (CI flags) -b .bandit-baseline.json; tests/rls_security_review.sql, rls_brokerage.sql, platform_rls_test.sql, privacy_lifecycle.sql, rls_tenant_wall.sql on the local dev DB with 0148", result: "bandit exit 0; all five SQL proofs exit 0", date: 2026-10-14}
authz_checked: "alerts are side outputs only; no authorization decision reads them"
blockers:
  - {type: owner, note: "Self-serve signups create a broker owner and now alert too (one email per new brokerage). Keep, or alert only on role changes inside an existing brokerage?"}
  - {type: scope, note: "Failure counting is per process (no shared store): N replicas allow N x threshold guesses before the first alert. No user MFA exists (AUTH-12: broker-owner MFA open), so 'MFA reset' covers only the operator's env-provisioned factor; a future reset route must call security_alerts. Platform-admin rows can be created only by SQL (no API) and are listed by scripts/access_review.py, not alerted."}
  - {type: operator, note: "No alert is delivered until ORACLE_SECURITY_ALERT_EMAIL or ORACLE_ALERT_EMAIL is set and SMTP works in that environment; not exercised on staging."}
audit_refs: []
decision_refs: []
next_task: "Set ORACLE_SECURITY_ALERT_EMAIL on staging after deploy and sign in once as the operator to see one real email."
updated: 2026-10-14
```

#### L17-10 HOOK-3: no static webhook secret in callback URLs
```yaml
id: L17-10
status: tests_pass
env: local
evidence_shas: [0a76a72]   # base; fix in the commit that adds backend/webhook_auth.py
files: [backend/webhook_auth.py, backend/commands_api.py, backend/command_providers.py, backend/acs_call_handler.py, backend/log_redaction.py, backend/tests/test_webhook_auth.py, backend/tests/test_log_pii_guard.py, .env.example, docs/security-launch-review.md]
tests_run:
  - {cmd: "sed -n 130,136p backend/commands_api.py; sed -n 277,289p backend/acs_call_handler.py; sed -n 45,52p backend/command_providers.py (base)", result: "both webhooks compared ?token= with the static secret; both callback URL builders put the secret itself in the query string", date: 2026-10-14}
  - {cmd: "read uvicorn 0.49.0 protocols/websockets/websockets_impl.py:263", result: "WebSocket handshake lines go to uvicorn.error with the path as the SECOND argument; log_redaction rewrote only the third argument on uvicorn.access, so ?bridge_token= (commands_api Plivo media bridge) reached the log", date: 2026-10-14}
  - {cmd: "pytest tests/test_webhook_auth.py tests/test_recovery_security.py tests/test_acs_call_handler.py tests/test_security_launch_review.py", result: "95 passed: header token, body HMAC (body- and time-bound, 5 min), expiring path-bound signed URL (expired/other route/bad sig -> 403); Neoh's callback URLs carry no secret and no token=; old ?token= accepted with a deprecation warning that names the env var, not the secret; ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT=0 refuses it", date: 2026-10-14}
authz_checked: "no secret -> 503 as before; any failed method -> 403"
blockers:
  - {type: operator, note: "Before the compat path is removed: point the ACS Event Grid subscription at the X-Neoh-Webhook-Token delivery header (or rely on the signed URL Neoh now hands ACS per call) and any custom-call provider at the header or X-Neoh-Signature. Calls placed by the previous release keep their ?token= URL until they end."}
  - {type: scope, note: "The Plivo media bridge still takes a per-call bridge_token in the WebSocket query (a per-call capability, not the static secret); it is now redacted from both uvicorn loggers. Edge/CDN logs outside the app are not controlled here."}
audit_refs: []
decision_refs: []
next_task: "Next release: delete the ?token= branch in webhook_auth.verify_webhook and ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT."
updated: 2026-10-14
```

#### L17-11 Log-PII guard: account ids and exception text
```yaml
id: L17-11
status: tests_pass
env: local
evidence_shas: [0a76a72]   # base; fix in the commit that adds log_redaction.actor_ref
files: [backend/log_redaction.py, backend/server.py, backend/tests/test_log_pii_guard.py, backend/audit_ledger.py, backend/auth.py, backend/tenancy.py, backend/crm.py, backend/client_enterprise.py, backend/client_portal.py, backend/agent_profile.py, backend/aws_observability.py, backend/lead_dossier.py, backend/media_api.py, backend/agent_mind/mind_service.py, docs/privacy-data-map.md]
tests_run:
  - {cmd: "AST scan of every backend logging call with agent_id/agent_identity/current_agent_id added to the PII names (base)", result: "25 sites in 11 files logged the account id (= the user's email): login success/failure, password reset/change, session registry, RLS context, CRM create/link/log, portal mint, bulk actions, AWS actions, crypto ACCESS_PII", date: 2026-10-14}
  - {cmd: "AST scan: logging calls inside `except E as x` that interpolate x", result: "201 sites in 60+ files; str() of a real asyncpg error from local PostgreSQL 16 quotes row values: 'DETAIL:  Key (email)=(jane@example.test) already exists.' and 'Failing row contains (bob@example.test, x)'", date: 2026-10-14}
  - {cmd: "pytest tests/test_log_pii_guard.py", result: "10 passed: guard now includes account ids (actor_ref() is the safe wrapper); 22 sites fixed; the PIIScrubFilter on the root and uvicorn.error handlers removes emails, phones and PG row values from messages AND tracebacks while keeping uuids, timestamps, IPs and counts; server._configure_logging installs it; WebSocket handshake query strings redacted", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: scope, note: "crypto.py's three agent_id sites (pii_context, access_pii_field) are exempted by name in the guard (_ACCOUNT_ID_DEFERRED) because a parallel slice owns crypto.py today; remove the entry when that slice merges. The scrub is pattern-based: a name or street address inside an exception message is not recognised. The 201 exception sites were not rewritten one by one."}
audit_refs: [A06]
decision_refs: []
next_task: "Drop the crypto.py deferral at merge; review a staging log sample (AC17.05)."
updated: 2026-10-14
```

#### L17-12 Audit ledger: fallback drained and capped, chain verified on a schedule
```yaml
id: L17-12
status: tests_pass
env: local
evidence_shas: [0a76a72]   # base; fix in the commit that adds 0148
files: [backend/audit_ledger.py, backend/db/migrations/0148_audit_chain_verification.sql, backend/data_integrations/periodic.py, backend/server.py, backend/privacy_data_map.py, backend/tests/test_audit_fallback_lifecycle.py, backend/tests/test_audit_fallback_drain_live.py, .github/workflows/ci.yml, .env.example, docs/privacy-data-map.md]
tests_run:
  - {cmd: "grep -n 'verify_chain\\|_sqlite' backend/ (base)", result: "verify_chain had no scheduled caller; nothing read the SQLite fallback back, deleted it or bounded it (docs/privacy-data-map.md §7 'never purged')", date: 2026-10-14}
  - {cmd: "pytest tests/test_audit_fallback_lifecycle.py", result: "8 passed: no pool -> SQLite and pending; cap refuses + alerts; >30-day rows purged with an alert and the rest still verify from the stored anchor; empty fallback restarts from the anchor and VACUUMs; tampered row detected; PG failure logs the error type, not its DETAIL; audit_chain_verify registered", date: 2026-10-14}
  - {cmd: "livetest.sh sh -c 'cd /app/backend && python -m pytest -q tests/test_audit_fallback_drain_live.py' (local dev PostgreSQL, throwaway roles)", result: "1 passed: 3 SQLite rows appended to the PG chain in order with original time/content and _fallback provenance (a non-uuid tenant kept in provenance), deleted locally; re-append adds nothing; the verifier checks exactly the 3 links from a cursor, then catches a forged row once (alert once) in a rolled-back transaction; via the platform pool it is bounded and persisted; a request connection sees 0 rows and UPDATE 0", date: 2026-10-14}
  - {cmd: "verify_chain_segment(max_rows=50000) over the whole local dev chain (platform pool)", result: "38,338 rows, intact, one pass in 3.06 s (local DinD PostgreSQL 16; not a staging number)", date: 2026-10-14}
  - {cmd: "rehearse_0148.sh: scratch DB, 0001-0147 as superuser, REASSIGN OWNED to NOSUPERUSER BYPASSRLS CREATEROLE, run_migrations.py as that owner", result: "0148 applied; table owned by the owner, RLS+FORCE, policy (SELECT app_is_platform_admin()); oracle_app SELECT/UPDATE yes, DELETE no; scratch DB and roles dropped", date: 2026-10-14}
authz_checked: "audit_chain_verification readable/writable only in a platform-admin context (0120 login membership); request connection proved blind in the live test"
blockers:
  - {type: scope, note: "Staging runs ORACLE_RECOVERY_MODE, so no periodic job (this verifier included) runs there (see L17-03). The drain runs in every process at boot and on the first good write regardless. A container that dies before Postgres returns still loses its fallback rows (ephemeral disk)."}
audit_refs: []
decision_refs: []
next_task: "Run the verifier once on staging by hand (or enable periodic jobs) after deploy; record the pass time on real data."
updated: 2026-10-14
```

#### L17-13 Quarterly access-review export
```yaml
id: L17-13
status: tests_pass
env: local
evidence_shas: [0a76a72]   # base; script in the commit that adds scripts/access_review.py
files: [scripts/access_review.py, backend/tests/test_access_review_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "ls scripts/ | grep -i access (base)", result: "no access-review listing existed", date: 2026-10-14}
  - {cmd: "livetest.sh sh -c 'cd /app/backend && python -m pytest -q tests/test_access_review_live.py'", result: "1 passed: one READ ONLY transaction, no INSERT/UPDATE/DELETE sent; postgres rolsuper, oracle_app neither super nor BYPASSRLS, oracle_platform_login member of platform_admin_role, no pg_* roles; broker owners per tenant (active/suspended ids) match a direct count; platform admins = env operator + users rows; operator factor reported without the seed; markdown and CSV render", date: 2026-10-14}
authz_checked: "runs on the platform login inside a read-only transaction; the output names people and is personal data (docstring)"
blockers:
  - {type: operator, note: "GitHub, DigitalOcean, Stripe, registrar and provider account listings are a manual checklist in the script's docstring; no API calls are made."}
audit_refs: []
decision_refs: []
next_task: "Run it for the first quarterly review against staging (read-only, via the db-access window) and file the result with the review record."
updated: 2026-10-14
```

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
  - {cmd: "billing._stripe_plan reason field + BillingOverlay (tests/test_billing_plan.py, BillingOverlay.test.jsx)", result: "/billing/plan now says why it is unavailable: not_configured (no key/price: the overlay says billing isn't set up on this deployment and disables Start, never 'temporarily unavailable') vs lookup_failed (Stripe did not answer: checkout stays available)", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: external, note: "Root cause (staging api boot log, 2026-10-14): STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_ID are named in the app spec but have NO values. Needs the owner's Stripe test-mode secret key, a test-mode recurring Price and the webhook signing secret set on the app."}
audit_refs: []
decision_refs: [D-2026-10-12-02]
next_task: "Owner: set the Stripe test-mode secret key, Price id and webhook secret on the staging app; then run one test-mode checkout."
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
next_task: "Owner 2026-10-14: production deferred (\"wait later\"); staging stays as is. Create production about 3-4 months before the ISO Stage 2 audit (L17-06). A copy sized like staging is about $105/month at list price (estimate)."
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
| A02 | Failed property lookup reads as zero matches | P0 | fixed in code at `d934b2e`, local tests only (2026-10-13, L11-02); not deployed, migration 0135 unapplied. 2026-10-13: the non-property sites listed in L11-02 (geocoders, eviction, enrich-property, market-snapshot, AVM, building footprints) fixed locally, `tests_pass` (L11-03, unpushed branch, no migration); staging check owed | was `backend/client_ai_automation.py:700`, `:704`; now `_property_candidates_bounded` returns per-source status (`:727`) and a failed lookup degrades the reconcile (`:1079`) | 11 | L11-02, L11-03, L06-01 |
| A03 | Professional 3D certification without verified evidence | P0 | partly fixed: labels bound to the verdict (L12-02); certification state not built | `backend/recon_quality.py:83`, `:101`; `backend/reconstruction_worker.py:581`; badge `backend/tour_api.py:41`, `:335` ignores the verdict | 12 | L12-01 |
| A04 | Production readiness not established by staging CI | P0 | open (no production app) | `.github/workflows/ci.yml:606` (`promote` manual only) | 19 | L19-02 |
| A05 | Usage event counted before approved/delivered outreach | P1 | fixed in code (`a8bff54`), `tests_pass` local (2026-10-13), not deployed: metered only at delivered (0133); owner to confirm the billable stage | was `backend/speed_to_lead.py:387-389`; now `backend/outreach_lifecycle.py` (`BILLABLE_ENGAGEMENT_STAGE`), `backend/billing_usage.py` (`record_lead_engagement`, `BILLABLE_PREDICATE`) | 18 | L18-02, L04-01 |
| A06 | Customer names in failure logs | P1 | open. 2026-10-13: fixed with a source guard, local (L17-04); addresses added 2026-10-14; agent emails (actor_ref) and exception text (handler scrub) added 2026-10-14, local (L17-11) | `backend/client_ai_automation.py:701-703` logs `full_name` | 17 | L17-04 |
| A07 | Concurrent messaging route initialization | P1 | fixed in code at `c52b4dc`, local real-PostgreSQL race test only (2026-10-13, L10-06); not deployed, migration 0136 unapplied outside the local dev DB; 10DLC brand/campaign re-submission fixed in code at `00f94f8`, local real-PostgreSQL race test only (L10-09, 0139 unapplied outside the local dev DB); campaign number assignment fixed in code, local real-PostgreSQL race test only (L10-10, 0145 unapplied outside the local dev DB) | was `backend/messaging_data.py:108` (`ON CONFLICT … DO UPDATE SET provider_account_id`); now `ON CONFLICT … DO NOTHING` + re-read, and the hosted order goes through `provider_purchases` (`_claim_hosted_order`) | 10 | L10-06 |
| A08 | Heuristic valuation targets in model training | P1 | open in code | `backend/ml_forge/edge_forge/train_lora.py:207` (`_COUNTY_PPSF`), `:209` (`_MAO_RULE = 0.70`) | 11 | — |
| A09 | AI capability descriptions exceed executable tooling | P1 | allowlist re-verified; UI and persona wording not re-verified. **2026-10-13 (base `3bcab84`): fixed locally, `tests_pass` (L07-01)** — one registry derived from the allowlist drives the persona, the API and the Personal AI page; a guard test fails on any unbacked claim. Playwright check still owed | allowlist at `backend/ai_chat_store.py:605`, `:646`; 126 catalogued vs 57 allowlisted tools (L07-01); registry `backend/ai_capabilities.py`, guard `backend/tests/test_ai_capabilities.py` | 07 | L07-01 |
| A10 | Local capacity test is not target-production evidence | P1 | open. 2026-10-13: the tenant-isolation policies are now index conditions (0141, `tests_pass` local, L16-01; undeployed) — that removes a platform-sized scan from ordinary requests but is not capacity evidence; nothing is measured on production-shaped infrastructure | `docs/capacity-plan.md:129` (nothing measured on DigitalOcean); `backend/db/migrations/0141_indexable_tenant_wall.sql` | 16 | L16-01, L16-03, L19-02 |
