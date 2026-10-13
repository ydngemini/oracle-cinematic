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
| 03 | Brokerage tenancy and hybrid access | [03](03-brokerage-tenancy-hybrid-access.md) | `source_verified` | local | 0131 (L03-02) | Owner: team leads (L03-03) | Deploy 0131; browser check; then agent_contacts |
| 04 | CRM, contacts, leads, relationships | [04](04-crm-contact-leads-relationships.md) | `source_verified` | local | `745f2fa` | A05 | L04-01 |
| 05 | Semantic intent | [05](05-semantic-language-intent.md) | `source_verified` | local | `745f2fa` | — | L05-01 |
| 06 | Evidence retrieval and memory | [06](06-evidence-retrieval-memory.md) | `source_verified` | local | `745f2fa` | A01, A02 | L06-01 |
| 07 | AI gateway and governed tools | [07](07-ai-gateway-model-tool-governance.md) | `source_verified` | local | `745f2fa` | A09 | L07-01 |
| 08 | Durable missions and workflows | [08](08-durable-missions-workflow-orchestration.md) | `source_verified` | local | `745f2fa` | PR #11 review; grant-release decision | L08-01 |
| 09 | Real-time voice | [09](09-realtime-voice-speech-latency.md) | `source_verified` | staging | `9c3463a` | Call-note broadcast (L09-03); phone check (L09-02) | L09-03 |
| 10 | Communications and calendars | [10](10-communications-calendar-provider-integrations.md) | `source_verified` | staging | `75c6ba6` (local) | Google OAuth client; 10DLC; deploy approval; A07 | L10-01 |
| 11 | Property and market intelligence (non-MLS) | [11](11-property-market-intelligence.md) | `source_verified` | staging | `90372f2` | A02; staging catalog empty | T11.10 (A02) |
| 12 | Neoh Space capture, 3D, tours | [12](12-neoh-space-browser-capture-reconstruction.md) | `source_verified` | local | `745f2fa` | A03 | Capture Studio first slice |
| 13 | Deals, documents, compliance | [13](13-transactions-contract-vault-compliance.md) | `source_verified` | local | `745f2fa` | Owner: e-sign, commissions | L13-01 |
| 14 | Sites, social, ads, client portal | [14](14-websites-social-ads-growth-client-portal.md) | `source_verified` | local | `745f2fa` | Owner: networks, listings on sites | L14-01 |
| 15 | Neoh mascot animation | [15](15-neoh-avatar-skeletal-character-animation.md) | `source_verified` | local | `745f2fa` | Owner: rig author | L15-01 |
| 16 | Performance, indexes, cache | [16](16-performance-indexes-mobile-cache.md) | `source_verified` | local | `745f2fa` | A10; no production target | L16-01 |
| 17 | Security, privacy, data lifecycle | [17](17-security-privacy-compliance-data-lifecycle.md) | `source_verified` | staging | `0739563` | A01, A06 | Read-only review with 02 |
| 18 | Billing, seats, analytics | [18](18-brokerage-billing-seat-plans-analytics.md) | `source_verified` | local | `745f2fa` | D-2026-10-12-02; A05 | Read-only review with 02 |
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
env: local
evidence_shas: [5d07552]
files: [backend/db/migrations/0131_agent_record_scope.sql, backend/record_access.py, backend/tenancy.py, backend/crm.py, backend/client_enterprise.py, backend/ai_chat_store.py, backend/privacy_lifecycle.py, backend/server.py, backend/client_portal.py, backend/client_ai_automation.py, backend/messaging_data.py, backend/inbound_voice.py, oracle-app/src/components/ClientAccess.jsx, oracle-app/src/state/useClientAccess.js, oracle-app/src/components/ClientDetailDrawer.jsx]
tests_run:
  - {cmd: "pytest tests/test_brokerage_access_matrix_live.py (real PostgreSQL, app role, local dev DB)", result: "2 passed: agent A / B / C(shared) / owner / platform admin / other brokerage / brokerage pipeline; children inherit; view share refused at the DB and as 403; edit share cannot re-share; expired share grants nothing; agent cannot hand a client to a colleague (API 403 and DB WITH CHECK); policy == app_client_access() == tenancy.client_access for every actor; pg_policies composition", date: 2026-10-13}
  - {cmd: "EXPLAIN ANALYZE as oracle_app, agent listing a 21,268-client brokerage", result: "6,262 ms with the policy as a function (per-row) -> 10.7 ms inline (index scan; share list an InitPlan)", date: 2026-10-13}
  - {cmd: "full backend suite; CI live files; rls_security_review/rls_brokerage/platform_rls_test/privacy_lifecycle.sql", result: "3226 passed, 0 failed; live 5/5 (precheck fails locally only on a stale 0106 ledger row in the dev DB, unrelated); SQL proofs PASS", date: 2026-10-13}
  - {cmd: "vitest (ClientAccess.test.jsx + full) + eslint + build + bundle budget", result: "718 passed; lint clean; within budget", date: 2026-10-13}
authz_checked: "clients + 10 child tables (notes, activities, tasks, tags, buyer_profiles, client_ai_state, transactions, transaction_parties, showings, interaction_logs) under FORCE RLS as oracle_app"
blockers: []
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "Deploy; verify in a real browser on staging (owner assigns, shares view-only, agent sees and cannot edit)."
updated: 2026-10-13
```
How it works: `assignee_user_id` (uuid) is derived from `assignee_id` by a trigger that rejects a string naming nobody (23503 → 422). Restrictive policies narrow `clients_tenant_isolation` (permissive policies OR together, F03.01). The API never re-implements the rule: it reads `app_client_access()` back for 404 vs 403. Platform admin, the broker owner and server-built pipelines marked `record_scope='brokerage'` (client portal, inbound SMS/voice matching, the client-AI steward) keep brokerage-wide access; a verified user session can never set it.

#### L03-03 What 0131 does not cover yet
```yaml
id: L03-03
status: source_verified
env: local
evidence_shas: []
files: [backend/db/migrations/0054_agent_contacts_and_intake.sql, backend/db/migrations/0001_init_tenancy.sql]
tests_run:
  - {cmd: "review of 0131 scope", result: "still brokerage-wide: agent_contacts (+ intake tables), leads, sms_messages, email_outbox, live_call_sessions, inbound_voice_calls, outreach_consent, lead_response_events, outcome_events, mission_candidates, contract_synthesis_artifacts, contact_property_relationships, media", date: 2026-10-13}
authz_checked: n/a
blockers:
  - {type: design, note: "Communications tables feed consent, opt-out and frequency checks, which must stay brokerage-wide; scoping them needs a split between 'see the message' and 'check compliance'."}
  - {type: owner, note: "Team leads (T03.02): multiple teams per agent? edit or read-only on team records?"}
next_task: "Scope agent_contacts the same way (assigned_agent_id), then the communications tables with a compliance-scope read path."
updated: 2026-10-13
```

### L04
CRM, contacts, leads and relationships.

#### L04-01 Engagement counted at the staged step
```yaml
id: L04-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/speed_to_lead.py, backend/lead_routing_api.py]
tests_run:
  - {cmd: "sed -n 380,392p backend/speed_to_lead.py; sed -n 535,552p backend/lead_routing_api.py", result: "lead_engaged metered right after staged; routing metrics count staged drafts as responded", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: [A05]
decision_refs: []
next_task: "T04.07"
updated: 2026-10-12
```

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

#### L07-01 Tool catalogue vs offered tools
```yaml
id: L07-01
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/ai_chat_agent.py, backend/ai_chat_store.py, backend/ai_tool_policy.py, backend/ai_tools_read.py, backend/neoh_persona.py]
tests_run:
  - {cmd: "grep counts over TOOLS, TOOL_RISK and the allowlists", result: "126 tools catalogued, 126 risk-classed, 57 allowlisted; unoffered tools are refused at execution", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: [A09]
decision_refs: []
next_task: "T07.13 + AC07.02 (close A09)"
updated: 2026-10-12
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
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/messaging_data.py]
tests_run:
  - {cmd: "sed -n 96,117p backend/messaging_data.py", result: "ensure_messaging_route ON CONFLICT ... DO UPDATE SET provider_account_id still present (:108)", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: [A07]
decision_refs: []
next_task: "T10.13"
updated: 2026-10-12
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
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/client_ai_automation.py]
tests_run:
  - {cmd: "sed -n 676,710p backend/client_ai_automation.py; git diff 3b63d5b 745f2fa -- backend/client_ai_automation.py", result: "_property_candidates_bounded catches every exception (:700) and returns [] (:704); reconciliation sets degraded from model_error only (:962); file unchanged since the audit pin", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: [A02]
decision_refs: []
next_task: "Prompt 11 T11.10 + AC11.03"
updated: 2026-10-12
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

#### L18-02 Engagement metered before approval or delivery
```yaml
id: L18-02
status: source_verified
env: local
evidence_shas: [745f2fa]
files: [backend/speed_to_lead.py, backend/billing_usage.py]
tests_run:
  - {cmd: "grep -n record_usage backend/speed_to_lead.py; sed -n 74,77p backend/billing_usage.py", result: "lead_engaged recorded at :387-389 right after the staged event; it is the only Stripe-reported metric (billing_usage.py:76)", date: 2026-10-12}
authz_checked: n/a
blockers: []
audit_refs: [A05]
decision_refs: []
next_task: "T18.06 + AC18.04"
updated: 2026-10-12
```

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
| D-2026-10-13-01 | 2026-10-13 | **Agents see their own clients, the brokerage pool and what is shared with them.** A client with no assignee is the brokerage pool, visible to every agent; an agent-created client is NOT auto-assigned; a share is per record and grants view or edit, chosen per share. | Owner, in session 2026-10-13 (three answers) | 03, 04, 17 | `0131_agent_record_scope.sql` (restrictive policies, `record_shares`, `assignee_user_id`), `record_access.py`, `ClientAccess.jsx` | Team leads (T03.02) or a different default for imports |
| D-2026-10-12-02 | 2026-10-12 | Pricing is a hypothesis, not decided. Code: one Stripe price at quantity 1 (`backend/billing.py:44`, `:266`), no seats. Idea: ~$1,999/month per 20-agent brokerage, ~$1,499 pilot. | Shared notes 2026-10-11 | 18 | No price change without owner approval | Owner sets pricing |

## Audit cross-reference

From [neoh-full-platform-source-audit-2026-10-08.md](../neoh-full-platform-source-audit-2026-10-08.md), pinned at `3b63d5b`. The status column records the re-check at HEAD.

| ID | Finding | Severity | Status at HEAD `745f2fa` (2026-10-12) | Evidence at HEAD | Owning category | Ledger |
|---|---|---|---|---|---|---|
| A01 | Customer PII and communications in plaintext | P0 | open; the audit missed the AI call notes (`aec14c4` predates its pin). 2026-10-13: client notes sealed for new writes (0132, L17-03, local); old rows await the backfill; the other fields stay open | `docs/privacy-data-map.md:40`, `:42`, `:48`, `:71`; `backend/commands_api.py:2335` (`deliver_call_notes` → `client_notes.body`) | 17 | L17-03, L09-02, L09-03 |
| A02 | Failed property lookup reads as zero matches | P0 | open | `backend/client_ai_automation.py:700`, `:704` (`_property_candidates_bounded`); degraded set from `model_error` only (`:962`) | 11 | L11-02, L06-01 |
| A03 | Professional 3D certification without verified evidence | P0 | partly fixed: labels bound to the verdict (L12-02); certification state not built | `backend/recon_quality.py:83`, `:101`; `backend/reconstruction_worker.py:581`; badge `backend/tour_api.py:41`, `:335` ignores the verdict | 12 | L12-01 |
| A04 | Production readiness not established by staging CI | P0 | open (no production app) | `.github/workflows/ci.yml:606` (`promote` manual only) | 19 | L19-02 |
| A05 | Usage event counted before approved/delivered outreach | P1 | open | `backend/speed_to_lead.py:387-389` (`record_usage("lead_engaged")` right after `staged`) | 18 | L18-02, L04-01 |
| A06 | Customer names in failure logs | P1 | open. 2026-10-13: fixed with a source guard, local (L17-04); addresses and agent emails not covered | `backend/client_ai_automation.py:701-703` logs `full_name` | 17 | L17-04 |
| A07 | Concurrent messaging route initialization | P1 | open | `backend/messaging_data.py:108` (`ON CONFLICT … DO UPDATE SET provider_account_id`) | 10 | L10-06 |
| A08 | Heuristic valuation targets in model training | P1 | open in code | `backend/ml_forge/edge_forge/train_lora.py:207` (`_COUNTY_PPSF`), `:209` (`_MAO_RULE = 0.70`) | 11 | — |
| A09 | AI capability descriptions exceed executable tooling | P1 | allowlist re-verified; UI and persona wording not re-verified | allowlist at `backend/ai_chat_store.py:605`, `:646`; 126 catalogued vs 57 allowlisted tools (L07-01) | 07 | L07-01 |
| A10 | Local capacity test is not target-production evidence | P1 | open | `docs/capacity-plan.md:129` (nothing measured on DigitalOcean) | 16 | L16-01, L19-02 |
