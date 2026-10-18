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
| 01 | Repository source audit | [01](01-repository-source-audit.md) | `tests_pass` | local | `3083abe` inventory (L01-01) | — | T01.11 A01–A10 re-check; T01.05 migration crosswalk |
| 02 | All pages, routes, mobile UI | [02](02-all-pages-mobile-ui.md) | `source_verified` | staging | `90372f2` crawl | — | Per-page verdicts |
| 03 | Brokerage tenancy and hybrid access | [03](03-brokerage-tenancy-hybrid-access.md) | `source_verified` | staging | 0131 (L03-02), 0134+0137 (L03-04), 0140 (L03-05), 0142 drafts (L03-06), 0143 leads (L03-07), 0145 live frames (L03-08) all on staging (0a76a72); 0151 lead children + lead frames (L03-09); 0155 deals with no client + every deal's offers/milestones/parties/checklist (L03-10) local only; L03-11 suspension cancels queued spend (local); L03-12 Python mirrors held to the live policies (local); L03-13 suspended agent's queued video cancelled (0166, local); policy v6 | Owner: team leads (L03-03); lead assignee / listings / marketplace / pipeline counts (L03-07) | Push 0155 with approval (L03-10); listings / marketplace once the owner answers (L03-07) |
| 04 | CRM, contacts, leads, relationships | [04](04-crm-contact-leads-relationships.md) | `tests_pass` | local | `a8bff54` (L04-01); L04-02 carrier-failed texts, L04-03 replies (0158), L04-04 entity map + channel linking (local); L04-05/06 contacts added or edited by hand link too (local) | Owner: meter lead_engaged at approval, delivery or first reply (prompt 04 §7) | T04.03 dedup + AC04.02; remaining L04-04 gaps |
| 05 | Semantic intent | [05](05-semantic-language-intent.md) | `tests_pass` | local | base `2fef2e5` (L05-01, L05-02); `7b198cf` (L05-03, merged `444efe0`); `012f655` (L05-04, English + Spanish; staging-checked after `12e4c34`); `f8142c1` (L05-05, Spanish success answers + fallback drafts + EN/ES voice; on staging after `45a031d`); L05-06 Spanish call-queue cards (local, branch `claude/continue-n7jgwq`, golden `ae16549`); L05-07 a correction withdraws the previous staged request (local); L05-08 the chat turn asks the one precise question and cannot pick; L05-09 the UI offers the choices and a click answers; L05-10 an unplaced name is looked up as the caller; L05-11 review fixes (every ambiguous record held, the server's question cannot be hidden, a lookup never narrows or lifts a restriction, names kept out of logs) (local) | Owner: model-spend approval for a scored parser run per language (en / es / mixed; harness built, not run) and real parser latency; an independent Spanish held-out set. Call-queue cards now Spanish except record content and model-written suggestions/summaries (L05-06); Command Center screen and horizon labels English; real-phone voice check owed. Decided 2026-10-14: policy v7 §4 (`cb2aedd`), launch languages English + Spanish (L05-04) | Merge PR #33 with approval; scored T05.13 run once spend is approved; properties and deals looked up like people; contact as a chat context (L05-11 next) |
| 06 | Evidence retrieval and memory | [06](06-evidence-retrieval-memory.md) | `tests_pass` | local | `745f2fa`, `6c7133b`, `63ac40b` (L06-01..03) | A01, A02 | T06.03 context compiler |
| 07 | AI gateway and governed tools | [07](07-ai-gateway-model-tool-governance.md) | `tests_pass` | local | `071d65d` (L07-01); L07-02 capability registry (local); L07-03 per-actor offer names what is withheld (local) | Playwright check owed (L07-01); owner: archive_client | T08.04 + per-actor capability block |
| 08 | Durable missions and workflows | [08](08-durable-missions-workflow-orchestration.md) | `source_verified` | local | `745f2fa`; L08-02 deferral journal (0159), L08-03 feasible plans, L08-04 plans refuse unset channels by name (local) | Owner: does a mission's standing grant count as approval (C4.1)?; PR #11 needs a rebuild (L08-01 review) | T08.03 TaskGraph; rebuild PR #11's internal tasks after the owner answers |
| 09 | Real-time voice | [09](09-realtime-voice-speech-latency.md) | `source_verified` | staging | `9c3463a`; L09-05 outbound spoken opt-out, L09-06 lead-only call notes, L09-07 voice rules locked per path (local) | Phone check (L09-02); real call (T09.18) needs owner approval | T09.18; T09.10 |
| 10 | Communications and calendars | [10](10-communications-calendar-provider-integrations.md) | `source_verified` | staging | `75c6ba6` (local) | Google OAuth client; 10DLC; deploy approval; A07 fixed locally; L10-11 email Ready only after a send (0164, local); L10-12 texts and calls likewise (0165, local); 0136 + 0139 + 0145 undeployed (L10-06, L10-09, L10-10); owner: one-brand rule (L10-09) | L10-01 deploy + real connect; T10.09 retry/circuit UI + Playwright |
| 11 | Property and market intelligence (non-MLS) | [11](11-property-market-intelligence.md) | `tests_pass` | staging (L11-01), local (L11-02, L11-03) | `90372f2`; `d934b2e` (L11-02); L11-03 on branch `worktree-agent-a59f5c8799a673f68` (base `2804f47`); L11-04 source inventory (local); L11-05 heuristic valuation targets labelled, underwriter promotion gated; L11-06 review fixes (local, A08); L11-07..L11-12 unverified property matches labelled possible matches in Houses, Neoh, the AI tools, contract drafts, Property View and the client portal; L11-13 a broker owner confirms or dismisses a title finding; L11-14 a broker owner confirms a linked client's house, read by Houses; L11-15 its review fixes; L11-16 Neoh reads it too (local, T11.03 / F11.04) | Deploy 0135; live-DB test owed; staging catalog empty; L11-03 not deployed | Owner/legal: licences per source (L11-04); T11.03 |
| 12 | Neoh Space capture, 3D, tours | [12](12-neoh-space-browser-capture-reconstruction.md) | `source_verified` | local | `745f2fa`; L12-03 Capture Studio first slice + integration map (local, jsdom only); L12-04 certification state derived from fresh evidence, every label bound (local) | A03 positive certification (T12.06, T12.04); AC12.02 real-device protocol | Server upload sessions (T12.05); T12.06 room graph |
| 13 | Deals, documents, compliance | [13](13-transactions-contract-vault-compliance.md) | `source_verified` | local | `745f2fa`; L13-02 no new MLS-sourced deals; L13-03 status trail 0163 + T13.14 verified (local) | Owner: e-sign, commissions, fallen-through contracts | T13.02 remainder; T13.11 |
| 14 | Sites, social, ads, client portal | [14](14-websites-social-ads-growth-client-portal.md) | `source_verified` | local | `745f2fa`; L14-03 honest website badge; L14-04 no new IDX on sites (local) | Owner: networks, listings on sites | T14.04 + AC14.04 |
| 15 | Neoh mascot animation | [15](15-neoh-avatar-skeletal-character-animation.md) | `source_verified` | local | truth slice (L15-02) + rig spec (L15-03) local, on `2fef2e5` | Owner: Rive vs 3D, rig author and licence (L15-03); no device measurements (AC15.07) | Owner decides L15-03; then T15.04 layers and the AC15.07 device protocol |
| 16 | Performance, indexes, cache | [16](16-performance-indexes-mobile-cache.md) | `tests_pass` | staging | `745f2fa`; 0141 tenant wall (L16-01); 0113 predicates dropped + 0144 (L16-03); JIT off for request transactions (0e29f5a) — all on staging 0a76a72; L16-04 keyset deal list 0162 (local); L16-05 no ungated service-worker cache read (local) | A10 capacity on production-shaped infra; no production target | T16.06 + AC16.04 |
| 17 | Security, privacy, data lifecycle | [17](17-security-privacy-compliance-data-lifecycle.md) | `source_verified` | staging | `0739563`; ISO 27001 set (L17-06, v1.0 approved); L17-07..20 local 2026-10-14 (card redaction, unsubscribe 0146, alerts, HOOK-3, log-PII, audit drain 0148, access review, demo-login guard, hashing, dep audit, admin-read audit, DR drill in CI, two-step sign-in 0150, security.txt); L17-21 local (SMS/transcript/chat-turn sealing 0152, clients + leads design); L17-22 (email subject/body, interaction-log subject/text, AI tool receipts, belief quotes, profile summaries sealing 0154, on staging); L17-23 local (clients lookup hashes + name tokens kept current, 0156, not yet searched); L17-24 local (sealing batches bounded, 0157) | A01, A06; owner: security@/privacy@ mailboxes (L17-20); certification body, pentest, DPAs (L17-06) | Owner: name/email search semantics, then clients slice (b) (L17-23); scheduled off-host backups; log forwarding |
| 18 | Billing, seats, analytics | [18](18-brokerage-billing-seat-plans-analytics.md) | `source_verified` | local | `a8bff54` (L18-02 `tests_pass`); L18-05 seat ceiling 0161 (local); L18-06 Stripe events in order, one checkout per brokerage 0169; L18-07 payment failure keeps reads and export, stops sends; L18-08 AI token budget for Neoh chat 0170 (local) | D-2026-10-12-02 (billable stage; seat number and who counts; past_due length; AI inference when lapsed; budget numbers) | T18.07 remaining channels; AC18.03 in Stripe test mode (owner keys) |
| 19 | Production operations | [19](19-infrastructure-deploy-observability-recovery.md) | `source_verified` | staging | `745f2fa`; L19-04 property-data health, L19-05 valuation outcomes (0160), L19-06 relay health + property-data runbook (local) | No production app; A04; owner: SLO targets, on-call | Owner SLOs; AC19.06 |
| 20 | 20-agent launch certification | [20](20-twenty-agent-full-feature-launch-e2e-evaluation.md) | `source_verified` | local | `745f2fa`; launch backlog generated from the ledger (local) | Every row above; owner launch scope (T20.03) | T20.03 owner scope |

Rollup is the weakest row. A category holding `external_configuration_required` or `blocked` rows shows them under "Open blockers" instead of hiding them in an average (L09 and L10 also hold `tests_pass` rows; see below).

`production_verified` count: **0**.

## Categories and deliverables

### L01
Repository source audit. Artifacts: the file-level inventory [`docs/audit/repo-inventory-3083abe.csv`](../audit/repo-inventory-3083abe.csv), its generator and checker [`scripts/check-repo-inventory.py`](../../scripts/check-repo-inventory.py), and the lines-read record [`docs/audit/lines-read.csv`](../audit/lines-read.csv).

#### L01-01 Reproducible file inventory with evidence-based classes and lines-read accounting (T01.01, T01.13, T01.15, AC01.01)
```yaml
id: L01-01
status: tests_pass
env: local
evidence_shas: [3083abe]
files: [scripts/check-repo-inventory.py, docs/audit/repo-inventory-3083abe.csv, docs/audit/lines-read.csv]
tests_run:
  - {cmd: "python3 scripts/check-repo-inventory.py generate --sha 3083abe", result: "1867 rows (= git ls-tree blobs at 3083abe). Classes: production_entry 9, runtime_dependency 894, test 431, infrastructure 103, documentation 120, optional_integration 85, archived_or_prototype 67, orphaned 158. Files listed 1867; files read 10; lines read 1570 of 404390", date: 2026-10-16}
  - {cmd: "python3 scripts/check-repo-inventory.py check docs/audit/repo-inventory-3083abe.csv", result: "exit 0: reconciles with git ls-tree at 3083abe; every row has a class, evidence and an audit state; lines_read equals the recorded ranges", date: 2026-10-16}
  - {cmd: "same check after deleting one row and inflating lines_read on every listed row", result: "exit 1: 3708 problems (the missing tracked file; lines_read without recorded ranges, F01.06)", date: 2026-10-16}
authz_checked: "n/a: a read-only script over git objects; no runtime, data or access change."
blockers:
  - "Classes are evidence-based but still mechanical: the static import graph from the entries (backend/Dockerfile CMD server:app, which the app.yaml worker also boots; app.yaml migrate run_migrations.py; RunPod handler.py; oracle-app and observability-platform index.html -> src/main.jsx), every migration, oracle-app/public, CI/Dockerfile/app-spec/compose/package.json references, tools' own config names, quoted path literals in reachable code, a directory read by reachable code, and documentation mentions. A function-level dead-code pass is T01.02/T01.08; an import does not prove a route is used."
  - "`orphaned` is a candidate list, never a deletion licence (F01.07). Notable at 3083abe: oracle-app/src/lib/propertyImagery.js (only named in a PanoViewer comment); backend/data_integrations/state_gis/** and county_assessor/__init__ (no reachable import); backend/data_integrations/scheduler.py; backend/backfill_owner_name_normalized.py; backend/scripts/{eval_floorplan,ignite_memory,onboard_ai_agent,setup_ai_agent_mission,train_point_segmenter}.py; backend/ml_forge/cot_synthesizer.py; data/raw_scrapes/** (100 files) and data/test_deals; training_data/*.jsonl (6); performance/{analyze,resilience,scenarios}; observability-platform .env.example, nginx.conf, package.json; scripts/{audit-agent-workspace,fetch_capture_dataset,golden_capture,serve-next-static,test-personal-ai-tab,test-reel-playwright,test-vite-reel-playwright}.py; root check_models.py."
  - "oracle-app/src/components/MlsSearch.jsx is not reached from main.jsx (PropertiesTab no longer offers it, D-2026-10-12-01): consistent with the MLS gate; inventoried as legacy, not deleted."
  - "Lines read: 1570 lines in 10 files, recorded only where read in this session at an unchanged file. Line-by-line review (T01.03-T01.05) has not started; the known-uninspected list is every row with audit_state=listed (1857 files)."
  - "The generator takes ~2 minutes (it reads every blob); not wired into CI (a shallow checkout lacks the history last_revision walks)."
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T01.11 + AC01.06: re-check A01-A10 at a pinned SHA; then T01.05 migration crosswalk (CREATE FUNCTION vs GRANT EXECUTE)."
updated: 2026-10-16
```

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
next_task: "Per-page verdicts in all-pages-verdicts.json: AC02.09 asks for a signed reviewer per page, so the owner (or a named reviewer) signs them; the crawl (all-pages-crawl.json) is render evidence, not a verdict (build-all-pages-matrix.mjs:34). A fresh crawl after this branch's UI changes (Capture Studio, DealBook paging, Email card) is owed on staging once PR #33 is merged (2026-10-16: not run locally; the demo seed makes a read-only Plivo call and targets staging)."
updated: 2026-10-16
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
  - {cmd: "review after 0151 (L03-09): FKs to leads, every lead_id column and pg_policies on the local dev DB", result: "client_portals, contract_documents, property_floorplans (+ revisions), property_pano_scenes, property_view_upload_links, reconstruction_jobs, spatial_tour_variants, capture_sessions, voice_walkthrough_jobs, video_studio_jobs (source lead) and lead-only live_call_sessions now follow their lead (L03-09); email_outbox always has a client (NOT NULL). Still brokerage-wide: listings, listing-only rows and marketplace_publications (owner question in L03-07); transactions with no client and their offers/parties/milestones; lead_pipeline_counts (aggregate); lead_response_events (metrics, client/contact-linked); compliance tables by design", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: design, note: "Communications tables feed consent, opt-out and frequency checks, which must stay brokerage-wide; scoping them needs a split between 'see the message' and 'check compliance'. 0134 adds the pattern: a function that sets app.record_scope='brokerage' for its own duration and returns no PII (app_client_contact_place, app_open_contact_counts)."}
  - {type: owner, note: "Team leads (T03.02): multiple teams per agent? edit or read-only on team records?"}
next_task: "Done in L03-10 (0155): transactions with no client follow their lead, and every deal's offers, milestones, parties and checklist follow the deal. Listings and marketplace publications wait for the owner (L03-07); team leads (T03.02) wait for the owner."
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
  - {type: decided, note: "Lead frames done (L03-09, 0151): VOICE_NOTE_LOGGED, DOSSIER_MOVED, CONTRACT_EXPIRED/DANGER, SPLAT_READY and VIDEO_READY now go through ws_hub.broadcast_record(lead_id=...) — the audience of the lead's seller client (record_access.linked_audience); a prospect's or a listing's frame still reaches the whole brokerage. tests/test_realtime_fanout_scope.py::test_lead_frames_follow_the_lead and test_a_lead_frame_goes_to_the_lead_audience; the audience itself in test_lead_children_access_live.py."}
  - {type: decided, note: "Owner 2026-10-14: the platform-admin OPS firehose keeps a FULL copy of record-scoped frames (as before 0145), once per frame: the voice-session stream is not mirrored a second time, and CALL_NOTES stays unmirrored (it was broadcast_user before). ws_hub.firehose_copy; broadcast_record(mirror=False) for call notes; tests/test_realtime_fanout_scope.py::test_an_unmirrored_record_frame_never_reaches_the_firehose."}
  - {type: deploy, note: "0145 must apply before this code serves (record_access.linked_audience calls app_record_audience; without it the frame goes to the acting user only — fail closed, not open). During a rolling deploy a previous-release replica drops scoped cross-replica frames (logged as malformed) instead of delivering them."}
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "Voice-stream re-check per frame (lead frames done in L03-09)."
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

#### L03-09 The rest of a lead's rows follow the lead (D-2026-10-13-01)
```yaml
id: L03-09
status: tests_pass
env: local
evidence_shas: []  # this commit, branch worktree-agent-a27440259871e34a5, unpushed
files: [backend/db/migrations/0151_lead_children_follow_the_lead.sql, backend/record_access.py, backend/ws_hub.py, backend/client_portal.py, backend/contracts_api.py, backend/floorplan_api.py, backend/property_view_api.py, backend/tour_api.py, backend/spatial_intelligence_api.py, backend/video_studio.py, backend/video_studio_api.py, backend/voice_intel.py, backend/reconstruction_worker.py, backend/disposition_enforcer.py, backend/lead_dossier.py, backend/policy_contract.py, backend/tests/test_lead_children_access_live.py, backend/tests/test_realtime_fanout_scope.py, backend/tests/test_communications_access_matrix_live.py, backend/tests/test_lead_record_access_live.py, backend/tests/test_floorplan_provenance.py, backend/tests/test_neoh_space_routes.py, backend/tests/test_video_studio.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "schema check on the local dev DB (FKs to leads, every lead_id column, NOT NULL columns, pg_policies) + scratchpad inv.py over backend/*.py", result: "lead_id children with only the tenant wall at f510967: client_portals, contract_documents, property_floorplans (+ property_floorplan_revisions by floorplan_id), property_pano_scenes, property_view_upload_links, reconstruction_jobs, spatial_tour_variants, capture_sessions, voice_walkthrough_jobs (no FK), video_studio_jobs (lead only inside source jsonb), live_call_sessions rows with no client/contact. email_outbox needs nothing: client_id is NOT NULL, so 0140 already decides every row. Not in the brief, left out with reasons below: marketplace_publications, listings, showings, transactions, lead_response_events, outreach_consent", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q -p no:cacheprovider tests/test_lead_children_access_live.py with 0151's 48 policies dropped (BEFORE; restored by re-running 0151, ledger sha256 unchanged 286171e4…)", result: "2 failed: agent A read B's private client's portal link ('client_portals', 'a', [...]); no restrictive policies on client_portals", date: 2026-10-14}
  - {cmd: "livetest.sh migrate (0151 on the local dev DB) + pytest tests/test_lead_children_access_live.py (AFTER; real PostgreSQL, FORCE RLS, app role)", result: "2 passed: for 12 tables, agent A / B / C (view share of B's client) / owner / platform admin / brokerage pipeline / other brokerage see exactly the rows of the leads they can see (pool + prospect + own), listing-only rows and a video job with a non-uuid lead stay brokerage-wide, a pool client's call about B's house follows its client; tour reads (fetch_tour_rows) of B's house empty for A, present for C; app_linked_access read-back == policy (view for C, None for A, edit for B and the pool); C's UPDATE/DELETE of each B row = 0 rows, C's INSERT of a portal / revision / build / negotiation event = 42501; A still updates the pool's and the prospect's rows. API: create_portal_link, revoke_portal_link, edit_document, revoke_upload_link, enqueue_reconstruction, retry_reconstruction, cancel/delete_video_job, log_walkthrough answer 404 (A) / 403 (C) and change nothing. Capability URLs: open_portal_session (SECURITY DEFINER) resolves B's portal; the signed 3D-asset stream serves B's house's splat; the video worker context sees B's job and photo. Backlog count: A's RLS 0, pipeline_count 1. Frame audience for B's house = owner, B, C; pool/prospect/unknown lead = everyone. pg_policies: one permissive wall + four restrictive per table (live_call_sessions also keeps 0140's four)", date: 2026-10-14}
  - {cmd: "CI live list locally (27 files incl. the new one)", result: "55 passed, 1 skipped, 2 failed: test_communications_access_matrix_live's composition check expected only 0140's policies on live_call_sessions — now also its four 0151 lead policies (re-run with the 0143 and 0151 tests: 6 passed); test_migration_precheck fails on the shared dev DB holding another branch's 0152 (environmental, as in L03-04/05/07)", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE as oracle_app (agent, owner on oracle_app_login; platform admin on oracle_platform_login), tenant 0000 (10.46M leads), one child row per table on 5,003 leads (the 3 client-linked included) seeded inside a rolled-back transaction; interleaved A = 0151 read policy dropped in a savepoint, B = deployed, 3 runs each, jit=off (scratchpad gen_bench0151.py, bench0151.txt)", result: "medians before -> after. Agent: client_portals list of all 5,003 links (list_portal_links has no LIMIT) 4.2 -> 46.3 ms; contract_documents list LIMIT 100 (sort of 5,003) 21.3 -> 60.6 ms; by id / one lead: contract 0.07 -> 0.50, floor plan 0.08 -> 0.68, revisions 0.12 -> 1.46, 360 scenes 0.81 -> 1.17, upload links 1.65 -> 2.30, build by id 0.07 -> 0.64, latest build 0.08 -> 0.48, queued count 0.06 -> 0.52, video list LIMIT 50 0.11 -> 3.64, video by id 0.06 -> 0.59, lead-only call 0.35 -> 0.75. Owner and platform admin short-circuit: lists unchanged within noise (portals 6.5 -> 4.6 / 3.9 -> 4.4, contracts 24.1 -> 20.6 / 17.5 -> 22.7 ms), point reads +0.3-0.5 ms (the three InitPlans). The cost is one leads primary-key probe per candidate row before LIMIT; brokerages hold tens of these rows today (dev DB: 2 portals, 0 contracts, 25 builds)", date: 2026-10-14}
  - {cmd: "non-superuser rehearsal (scratchpad rehearse0151.sh): scratch DB, 0001-0150 as a throwaway superuser, REASSIGN OWNED to a NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner", result: "0151 applied (1 processed); 48 lead policies; negotiation_events' write checks re-created; scratch DB, roles and the copied migration folder dropped", date: 2026-10-14}
  - {cmd: "rls_security_review / rls_brokerage / platform_rls_test / privacy_lifecycle / rls_tenant_wall .sql on the local dev DB with 0151", result: "all exit 0", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/; bandit as CI runs it", result: "3808 passed, 49 skipped, 0 failed (the first run caught 17: route fakes that did not know the record_access read-back or the brokerage-wide backlog/quota count, and VIDEO_READY captured on ws_hub.broadcast); bandit (CI flags, baseline) exit 0", date: 2026-10-14}
authz_checked: "12 lead-child tables + negotiation_events writes under FORCE RLS as oracle_app; capability URLs (portal session, signed 3D asset, public upload link) and workers (video as brokerage-scope pipeline; reconstruction/voice as submitter; sweeps, tour-variant, contract-vault and command workers as platform admin) verified or unchanged"
blockers:
  - {type: design, note: "Left brokerage-wide, deliberately: listings and marketplace_publications (marketed and shared across brokerages; the L03-07 listings owner question covers both); showings (client_id NOT NULL, so 0131 decides); transactions with no client and their offers/parties/milestones (a transactions slice of its own); lead_response_events (metrics ledger, follows client/contact since 0140); outreach_consent (compliance)."}
  - {type: design, note: "Reconstruction and voice-walkthrough workers run as their submitter in their current role: a build whose submitter lost edit access mid-run now has its writes refused (as its media writes already were since 0143) instead of writing to a house they can no longer change (C3.4)."}
  - {type: decided, note: "Platform Use Policy v6 s2 already says a client's property record 'follows that client together with its photos and activity: you see it only while you can see the client, and changing it needs edit access'; POLICY_COVERAGE row added for 0151 on that phrase, no version bump. Optional clearer wording drafted for the owner in the session report (names tours, floor plans, videos, documents and client links)."}
audit_refs: [A01]
decision_refs: [D-2026-10-13-01]
next_task: "Owner: listings / marketplace question (L03-07); transactions with no client done in L03-10."
updated: 2026-10-14
```
How it works: 0143's lead rule, one level down. Each table gets `<t>_lead_read/_insert/_update/_delete` RESTRICTIVE policies, inline: read = `lead_id IS NULL OR EXISTS (SELECT 1 FROM leads l WHERE l.id = <t>.lead_id)` (the EXISTS runs under leads' own RLS, so 0143 decides); write = the lead's seller client editable (`app_client_access(…, true)`), or no client; platform admin, broker owner and brokerage-scope pipelines short-circuit. `video_studio_jobs` reads its lead from `source->>'lead_id'` (a non-uuid value counts as no lead); `property_floorplan_revisions` follows its floor plan; `live_call_sessions` is narrowed only when it has no client and no contact; `negotiation_events`' write checks ask about the call's client, else its lead's. The API never restates the rule: `record_access._LINKED` gained the tables (read-back of `app_linked_access`), and `require_lead_edit` / `require_linked_edit` answer 404/403 before any side effect in portal create/revoke/tour-link decision, contract draft/edit, floor-plan save, upload-link revoke, 3D build enqueue/retry, scene override, tour variant, video create/script/cancel/delete, walkthrough upload and lead-only voice sessions. Cost guards (3D and walkthrough backlog, video daily quota) count the whole brokerage through `record_access.pipeline_count` / a quota pipeline, not only the rows the caller may read. Lead frames (VOICE_NOTE_LOGGED, DOSSIER_MOVED, CONTRACT_EXPIRED/DANGER, SPLAT_READY, VIDEO_READY) go through `ws_hub.broadcast_record(lead_id=…)` → `app_record_audience(lead's seller client)`; a prospect's or a listing's still reaches everyone, and a client's house still names no address (a pool client's frame is brokerage-wide). Found and fixed on the way: (1) the signed 3D-asset stream and (2) the video worker ran as an agent identity with no user, so since 0143 a client's house's space would not stream and its finished video could not be stored (after generation was paid for) — both now brokerage-scope pipelines; (3) `log_walkthrough` queued a note on any lead id without checking it; (4) the 3D backlog, walkthrough backlog and video quota would have shrunk to the caller's own rows under the new policies.

#### L03-10 A deal with no client follows its lead; a deal's offers, milestones, parties and checklist follow the deal (D-2026-10-13-01)
```yaml
id: L03-10
status: tests_pass
env: local
evidence_shas: [20855af]
files: [backend/db/migrations/0155_transactions_follow_the_lead.sql, backend/tests/test_deal_record_access_live.py, backend/tests/test_brokerage_access_matrix_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "local PostgreSQL 16.15 built by run_migrations.py from every migration through 0154 (CI's rls-job recipe); pg_policies on transactions, transaction_offers, transaction_milestones, compliance_checklist_items, transaction_parties", result: "BEFORE: transactions had 0131's client policies only (a deal with no client was brokerage-wide); transaction_offers, transaction_milestones and compliance_checklist_items had only their tenant wall (every deal, including a colleague's private client's); transaction_parties narrowed only a party that is a client", date: 2026-10-15}
  - {cmd: "pytest -q -p no:cacheprovider tests/test_deal_record_access_live.py (real PostgreSQL, FORCE RLS, app role; 0155 NOT applied)", result: "2 failed: agent A read B's no-client deal on B's private client's house; composition found no 0155 policies", date: 2026-10-15}
  - {cmd: "python run_migrations.py (0155 applied) + the same test", result: "2 passed: 7 deals x 7 actors — A sees its own client's house, the pool's, a prospect's, the bare deal and the deal A opened on B's house; B and C (view share) see B's; owner, platform admin and a brokerage-scope pipeline see all; another brokerage none; each deal's offer, milestone, party and checklist item exactly as its deal. transaction_detail / list_offers 404 for A on B's deal. C changes nothing (UPDATE 0 / DELETE 0 on the deal and its 4 children); A and B update theirs; inserts on a deal the caller cannot edit raise 42501 (InsufficientPrivilegeError, mapped to 403 by server.py), including portfolio_api.add_milestone and opening a deal on B's house", date: 2026-10-15}
  - {cmd: "psql -f tests/rls_security_review.sql, rls_brokerage.sql, platform_rls_test.sql, privacy_lifecycle.sql, rls_tenant_wall.sql", result: "all exit 0 (rls_tenant_wall E: transactions identical with and without the twin in 7 contexts)", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests compliance_engine/tests (clean env, after the L03-10 commit)", result: "test_migration_ledger failed: 0155 declared no object run_migrations --reconcile can probe (every policy was inside the DO block). Fixed in the next commit: transactions_lead_read written out as plain SQL (the 0151 pattern; same text). Re-applied locally: the deal test, the access matrix and test_migration_ledger + test_run_migrations pass (26)", date: 2026-10-15}
  - {cmd: "pytest -q -p no:cacheprovider <every live test the CI rls job runs> tests/test_deal_record_access_live.py", result: "63 passed after test_brokerage_access_matrix_live's composition list gained transactions_lead_* and transaction_parties_deal_*. A first run also failed test_audit_fallback_drain_live once (audit chain; touches no deal table); it passed alone and in the full rerun", date: 2026-10-15}
authz_checked: "Owner, platform admin and brokerage-scope pipelines short-circuit as since 0131. Background readers checked: client_portal milestones run as a brokerage-scope pipeline; privacy_lifecycle's milestone reassignment runs as the owner; every API read of a child goes through the deal first (portfolio_api, ai_tools_read, ai_tools_gated, routes_compliance, neoh_intents). The creator arm of 0131 is kept on both levels."
blockers:
  - "Not deployed: pushing to main deploys staging (CONTRACT C4.4); owner approval owed."
  - "Not measured on production-sized data: the local database has no deals. The policies are primary-key probes (deal -> lead -> client), like 0151, whose before/after is in L03-09."
  - "Pre-existing, unchanged: portfolio_api.patch_transaction answers a view-only share with the version-conflict 409 it has used since 0131, not a 403 (the UPDATE touches 0 rows); record_access has no deal entry yet."
  - "Not narrowed by their transaction_id: contract_documents (follows its lead, 0151) and marketplace_publications (listings owner question, L03-07). A deal with no client and no lead stays the brokerage pool."
audit_refs: [A01]
decision_refs: [D-2026-10-13-01]
next_task: "Push 0155 with approval and re-run this test in CI; then a record_access deal entry so a view-only deal edit answers 403 before any write."
updated: 2026-10-15
```

How it works: `transactions_lead_read/_insert/_update/_delete` (RESTRICTIVE) leave a deal with a client to 0131 and a deal with no lead as it was; a deal with no client but a lead is visible when the lead is (an EXISTS on leads under the caller's RLS, so 0143 decides) or to the agent who opened it, and writable when the lead's seller client is editable (or it has none) or by its opener. `<t>_deal_read/_insert/_update/_delete` on transaction_offers, transaction_milestones, compliance_checklist_items and transaction_parties: read = the deal is visible (an EXISTS on transactions under the caller's RLS); write = the deal is editable (opener, editable client, or — no client — editable lead client). Inline, no function (0131). Rollback is in the migration header.

#### L03-11 Suspension cancels the agent's queued spend; what it cancels, verified (T03.16)
```yaml
id: L03-11
status: tests_pass
env: local
evidence_shas: [b8cd33d]
files: [backend/privacy_lifecycle.py, backend/tests/test_suspension_cancels_spend_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read privacy_lifecycle._PENDING / cancel_pending_side_effects and brokerage_onboarding.suspend_member at b8cd33d", result: "VERIFIED: suspension already cancels the agent's queued commands, pending approvals, automation jobs, call intents, outbox emails and OAuth states and pauses their missions and smart plans; NOT cancelled per agent: queued GPU reconstructions and voice walkthroughs (cancelled only at tenant closure although created_by records the agent), video studio jobs (no requester column), contact nurture jobs (no sender reads them yet: latent)", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_suspension_cancels_spend_live.py with privacy_lifecycle.py stashed, then restored", result: "before: 1 failed (reconstruction stayed queued); after: 1 passed (the suspended agent's queued reconstruction and voice walkthrough fail with the reason; a colleague's queued jobs and the agent's running job are untouched)", date: 2026-10-16}
  - {cmd: "test_privacy_lifecycle_live.py, test_access_review_live.py; unit test_privacy*.py, test_brokerage_onboarding*.py", result: "3 passed; 109 passed", date: 2026-10-16}
authz_checked: "Runs inside suspend_member's transaction (broker-owner or platform admin), scoped to the tenant and lower(created_by) = the suspended agent."
blockers:
  - "video_studio_jobs records no requester: a suspended agent's queued video still runs (closure cancels it). Fixed locally in L03-13 (0166)."
  - "contact_nurture_jobs have no sender today; when one is built it must skip contacts whose owner is suspended."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "T03.15 done in L03-12; owner answers for L03-07 (listings / marketplace) and team leads (T03.02)."
updated: 2026-10-16
```

#### L03-12 The Python visibility mirrors are held to the live policies (T03.15, AC03.02)
```yaml
id: L03-12
status: tests_pass
env: local
evidence_shas: [dd798b9]
files: [backend/tenancy.py, backend/tests/test_tenancy.py, backend/tests/test_tenancy_mirrors_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read tenancy.py:118-216 and pg_policies for clients / leads / listings at dd798b9; git grep the mirrors' callers", result: "client_access / contact_access are already compared with app_client_access / app_contact_access by the access-matrix live tests. can_read_private said 'clients / leads — the tenant wall' with no lead rule, but leads_linked_* (0143) narrow leads to the seller client's audience; can_write said 'any mutation (clients, leads, listings)' — true only for listings. No production decision used the drifted mirrors (only _record_access, for the wall step), so this was a drift in the stated contract, not a live leak", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_tenancy_mirrors_live.py with lead_access = the old documented rule (tenant wall => edit)", result: "1 failed, 5 mismatches: agent A reads and edits B's client's lead (the database refuses both); agent C, holding a VIEW share of B's client, would edit B's lead (the database lets C read, not write)", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_tenancy_mirrors_live.py (6 actors x 4 leads, 2 clients, 4 listings; read via SELECT and write via UPDATE as the app login)", result: "1 passed: can_read_private never admits a row the database hides; lead_access matches leads_linked_read/_update for every actor and lead; can_read_listing matches listings_read (own, private, shared pool, co-broke grant); can_write matches listings_write", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_tenancy.py", result: "16 passed (lead_access unit cases)", date: 2026-10-16}
authz_checked: "Read-only for production paths: lead_access is a new mirror for tests and future callers; no route changed. Every comparison runs as the app login through tenant_tx, so FORCE RLS decides the database side."
blockers:
  - "listings_read still admits any tenant's is_shared_mls rows (the legacy MLS pool). MLS is out of scope (D-2026-10-12-01) and category 11 gates its code; changing the policy is an owner decision (L03-07 listings / marketplace), so the mirror matches the policy as it is."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T03.03 + AC03.03 sharing primitive review; owner answers for L03-07 and T03.02."
updated: 2026-10-16
```

#### L03-13 A suspended agent's queued studio video is cancelled too (0166; T03.16, AC03.06)
```yaml
id: L03-13
status: tests_pass
env: local
evidence_shas: [1debee4]
files: [backend/db/migrations/0166_video_studio_jobs_requester.sql, backend/video_studio_api.py, backend/privacy_lifecycle.py, backend/privacy_data_map.py, backend/tests/test_suspension_cancels_spend_live.py]
tests_run:
  - {cmd: "livetest.sh pytest tests/test_suspension_cancels_spend_live.py (extended) on 1debee4 code", result: "1 failed: video_studio_jobs has no created_by column, so neither the route nor suspension can know whose video it is (L03-11 blocker)", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_suspension_cancels_spend_live.py tests/test_lead_children_access_live.py tests/test_privacy_lifecycle_live.py tests/test_migration_precheck.py (real PostgreSQL 16, app login, real suspend and create routes)", result: "12 passed: the real create_video_job route stores the requesting login; suspension cancels that agent's queued videos (seeded and route-created) with the reason; a colleague's queued video and the agent's own generating video are untouched; reconstruction and voice jobs as in L03-11", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite)", result: "5235 passed, 74 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container)", date: 2026-10-16}
authz_checked: "created_by is written from the verified request context, never from the body. The cancel runs inside the suspend route's transaction under the broker owner's RLS like the other _PENDING rows. privacy_data_map notes the new personal field (the agent's sign-in email)."
blockers:
  - "Videos created before 0166 have no recorded requester and are not guessed: they are cancelled only when the brokerage closes."
  - "A video already generating at the paid provider is not stopped by suspension (no provider cancel call); same rule as a running reconstruction."
  - "contact_nurture_jobs still have no sender (L03-11)."
  - "Not deployed (C4.4): 0166 is local."
audit_refs: []
decision_refs: []
next_task: "T03.03 + AC03.03 sharing primitive review; owner answers for L03-07 and T03.02."
updated: 2026-10-16
```

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
next_task: "Track 'responded' (inbound reply linked to the first response); then T04.01 + T04.13. (A carrier-failed text is now marked failed: L04-02.)"
updated: 2026-10-13
```
One vocabulary for every outbound store: `backend/outreach_lifecycle.py` (`Stage`, and maps for `command_executions.state`, `email_outbox.status`, `sms_messages.status`, carrier call statuses and `lead_response_events.disposition`; tests prove each map covers its CHECK). The first-response ledger now moves staged → approved (`commands_api.release_command`) → accepted (`_record_send_bookkeeping`, was `'sent'`) → delivered (Telnyx receipt in `messaging_data.record_delivery_update`, answered call in `commands_api.record_call_outcome`); a rejected draft becomes `cancelled`. Accepted and delivered are recorded only for a command whose state is `succeeded`. `routing_metrics` (`lead_routing_api.py:545`) no longer counts a staged draft as a response: it reports staged/approved/accepted/delivered/cancelled separately and measures p50/p90/under-90s to `accepted_at` (`latency_measured_to: "accepted"`); the `responded` key is gone (no caller). Not done: `responded` (a reply) is not tracked; a carrier-failed text stays `accepted`; email (SMTP) and Twilio texts never reach `delivered` (no receipt exists).

#### L04-02 A text the carrier failed is a failed first response, not an accepted one
```yaml
id: L04-02
status: tests_pass
env: local
evidence_shas: [6ca87c2]
files: [backend/outreach_lifecycle.py, backend/messaging_data.py, backend/tests/test_outreach_lifecycle.py, backend/tests/test_engagement_lifecycle_live.py]
tests_run:
  - {cmd: "read messaging_data.record_delivery_update, outreach_lifecycle (at e725553)", result: "a carrier 'failed' / 'undelivered' receipt updated sms_messages only; the first-response ledger kept 'accepted' (L04-01 'Not done'), so routing_metrics counted a text the lead never received as an accepted response and inside the speed-to-lead percentiles. The 0133 CHECK already allows disposition 'failed' and routing_metrics already counts it; nothing wrote it for a text", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_outreach_lifecycle.py tests/test_speed_to_lead.py", result: "52 passed: a failure receipt moves accepted -> failed, never meters, is terminal (a duplicate or a late 'delivered' moves nothing); it never undoes a delivery and never touches a command the provider did not take; a failure receipt that beat the acceptance is applied at acceptance (reconcile_command_delivery); FAILED moves only from approved/accepted/sent", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_engagement_lifecycle_live.py (real PostgreSQL) with the code change stashed, then applied", result: "before: failed (AssertionError: 'accepted' == 'failed'); after: 1 passed — the carrier-failed text is 'failed', a late 'delivered' receipt does not revive it, routing metrics report accepted 4 (was 5), delivered 4, cancelled 1, failed 1", date: 2026-10-15}
  - {cmd: "every CI rls-job live test; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "65 passed; unit 5196 passed, 62 skipped, 2 failed (test_media_storage + test_object_storage: container runs as root, identical without the change); bandit exit 0", date: 2026-10-15}
authz_checked: "No access change: the receipt path runs in the platform webhook context as before; the ledger write is the same RLS-scoped UPDATE with the same 'command succeeded' guard as delivered."
blockers:
  - "Metric change to note: a carrier-failed text leaves the 'accepted' count and the p50/p90/under-90s latency (it reached the provider, not the lead) and is counted under 'failed'."
  - "Email (SMTP) and Twilio texts still have no receipt, so they never reach delivered or failed (L04-01). Calls: a failed call already maps to FAILED in call_status_stage; record_call_outcome was not changed here."
  - "Not deployed (C4.4)."
audit_refs: [A05]
decision_refs: [D-2026-10-12-02]
next_task: "Done in L04-03 (replies). Then T04.01 + T04.13."
updated: 2026-10-15
```

#### L04-03 The lead's reply is recorded on its first response (T04.07 "responded")
```yaml
id: L04-03
status: tests_pass
env: local
evidence_shas: [9b851e8]
files: [backend/db/migrations/0158_first_response_replied.sql, backend/outreach_lifecycle.py, backend/messaging_data.py, backend/lead_routing_api.py, backend/tests/test_outreach_lifecycle.py, backend/tests/test_first_response_reply_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read outreach_lifecycle, messaging_data.record_inbound_message, lead_routing_api.routing_metrics, \\d lead_response_events (at d7ace66)", result: "Stage.RESPONDED existed in PROGRESSION but nothing stored a reply; the old `responded` metric (drafts) was removed in 0133; an inbound text was stored and logged on the client's timeline only. No frontend reads first_response metrics (grep oracle-app/src)", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_first_response_reply_live.py (real PostgreSQL, FORCE RLS, app role) with the code stashed (0158 applied), then with it", result: "before: failed (replied_at None); after: 1 passed — an inbound text from the client stamps replied_at and the message id on their delivered first response; disposition stays delivered; lead_engaged not metered again; a second text leaves the first stamp; an opt-out, a reply to a carrier-failed text, a reply 8 days after acceptance and a stranger's text stamp nothing; routing_metrics: replied 1, failed 1, delivered 2, no `responded` key", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_outreach_lifecycle.py", result: "37 passed (new: no client/contact = no database call; the reply statement picks one row by a scalar subquery, never changes the disposition, only from accepted/sent/delivered; the inbound path stamps in its own platform transaction after the insert commits and skips opt-outs)", date: 2026-10-15}
  - {cmd: "every CI rls-job live test + this one; SQL proofs; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "66 passed; SQL proofs exit 0; unit 5199 passed, 63 skipped, 2 failed (test_media_storage + test_object_storage: container runs as root, identical without the change); bandit exit 0", date: 2026-10-15}
authz_checked: "The stamp runs in the platform context (like delivery receipts) because the number's agent need not be the one who may edit the lead's record; it changes only replied_at / reply_sms_message_id / updated_at on one row of the message's own tenant, chosen by that tenant's client/contact ids. Who may read a row is unchanged."
blockers:
  - {type: owner, note: "Prompt 04 §7: should lead_engaged be metered at approval, at delivery or at the first reply? Unchanged: metered at delivery; a reply never meters."}
  - {type: scope, note: "Only texts: email (no inbox reader, L10-07) and inbound calls are not linked yet. The window is 7 days (outreach_lifecycle.REPLY_WINDOW_DAYS), a product choice to confirm."}
  - "Not deployed (C4.4)."
audit_refs: [A05]
decision_refs: [D-2026-10-12-02]
next_task: "T04.01 + T04.13: the entity map and 'every channel resolves to one contact' (AC04.01)."
updated: 2026-10-15
```
Stored as `replied_at` + `reply_sms_message_id` (0158), not a disposition, so a reply rides on top of the delivery: delivered counts and metering (which re-reads disposition = 'delivered') are unchanged. `outreach_lifecycle.record_reply` stamps the newest first response for the same client or contact whose disposition is accepted/sent/delivered and whose acceptance (or delivery) is within 7 days before the text, once. `routing_metrics.first_response` gains `replied` and `replied_measured_as`.

#### L04-04 Entity map; texts and calls that came before the contact join it at intake (T04.01, T04.13, AC04.01 part)
Map: [docs/crm-entity-map.md](../crm-entity-map.md).
```yaml
id: L04-04
status: tests_pass
env: local
evidence_shas: [845bbd2]
files: [docs/crm-entity-map.md, backend/contact_linking.py, backend/lead_routing_api.py, backend/tests/test_contact_linking_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read-only trace of every intake channel (Explore) + spot checks of inbound_voice.py:290/:351, messaging_data._match_contact, lead_routing_api.receive_lead, sites_api.py:755, contacts_api.py:543, crm.py:846 at 845bbd2", result: "only the lead webhook creates a contact; texts and calls only link to an existing one, so a person who texted or called first kept those rows linked to nobody after the webhook created them; no public web-form route; differing match keys and tie-breaks (map committed)", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_contact_linking_live.py with lead_routing_api.py stashed, then restored", result: "before: 1 failed (the earlier text stayed contact_id NULL); after: 1 passed", date: 2026-10-16}
  - {cmd: "test_contact_linking_live.py (real PostgreSQL; real messaging_data inbound path and a real signed receive_lead)", result: "text + call, then webhook: exactly 1 client and 1 new contact, and the earlier text and call carry its contact and client; a text from another number, a call already linked to someone else and another brokerage's text are untouched; a later text links on arrival; a replayed webhook creates nothing", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_lead_routing*.py", result: "6 passed", date: 2026-10-16}
authz_checked: "Linking runs inside the intake's own transaction after it switched to the resolved tenant (FORCE RLS), updates only rows with no contact, in that tenant, within 90 days; nothing logged but counts."
blockers:
  - "Gaps 1-6 in docs/crm-entity-map.md remain: no public web-form intake; texts/calls never create a contact; manual contact creation does not run the linking; differing match keys/tie-breaks; the call fallback's digit match; the webhook ignores contact-less clients; no unique hash constraint (AC04.02)."
  - "Linked texts do not get back-filled client_activities timeline rows; the message itself is linked."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Manual creation done in L04-05; one match key and tie-break for all channels; AC04.02 dedup under race (unique hash + merge review)."
updated: 2026-10-16
```

#### L04-05 A contact added by hand collects the earlier texts and calls from its phone (T04.13, AC04.01)
```yaml
id: L04-05
status: tests_pass
env: local
evidence_shas: [1debee4]
files: [backend/contacts_api.py, backend/tests/test_contact_linking_live.py]
tests_run:
  - {cmd: "livetest.sh pytest tests/test_contact_linking_live.py (new manual-creation test on 1debee4 code)", result: "1 failed: an agent adds 'Riley Moss' with the phone that texted and called earlier; the text and the call stay linked to nobody (entity-map gap 3)", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_contact_linking_live.py tests/test_contact_access_matrix_live.py (real PostgreSQL 16, FORCE RLS, app login, real create_contact route as an agent)", result: "4 passed: the earlier text and call join the new contact (phone typed as '(302) 555-0171' still matches the stored E.164 and hash); another number's text is untouched; a contact with no phone links nothing; the L04-04 webhook case and the contact access matrix unchanged", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite)", result: "5235 passed, 75 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container)", date: 2026-10-16}
authz_checked: "Runs in create_contact's own tenant transaction as the caller, so RLS decides which unlinked rows can be updated; only rows with no contact are touched, within the 90-day window (contact_linking). An agent's new contact is in the pool unless assigned, as before."
blockers:
  - "A phone added later by editing a contact (PATCH) does not run the linking yet. Fixed locally in L04-06."
  - "Entity-map gaps 1, 2, 4-6 remain (docs/crm-entity-map.md)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Phone edit done in L04-06; one match key and tie-break for all channels; AC04.02 dedup under race."
updated: 2026-10-16
```

#### L04-06 A phone added to a contact later collects that number's earlier texts and calls (T04.13, AC04.01)
```yaml
id: L04-06
status: tests_pass
env: local
evidence_shas: [6b38f0d]
files: [backend/contacts_api.py, backend/tests/test_contact_linking_live.py]
tests_run:
  - {cmd: "livetest.sh pytest tests/test_contact_linking_live.py (extended) on 6b38f0d code", result: "1 failed: a contact created with no phone is edited to add the number that texted earlier; the text stays linked to nobody", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_contact_linking_live.py tests/test_contact_access_matrix_live.py (real PostgreSQL 16, FORCE RLS, real update_contact route as an agent)", result: "4 passed: the new number's earlier text joins the edited contact; editing a second contact to a number whose text already belongs to someone else leaves that text where it is", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite)", result: "5235 passed, 75 skipped, 2 failed (the known root-only cases of this container)", date: 2026-10-16}
authz_checked: "Same as L04-05: update_contact's own tenant transaction, the caller's RLS, only rows with no contact, 90-day window. Runs only when the phone actually changed."
blockers:
  - "Entity-map gaps 1, 2, 4-6 remain (docs/crm-entity-map.md); two contacts may now share a phone (no unique hash, AC04.02)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "One match key and tie-break for all channels; AC04.02 dedup under race (unique hash + merge review)."
updated: 2026-10-16
```

### L05
Semantic intent.

#### L05-01 Fast-path intent hides failed searches
```yaml
id: L05-01
status: tests_pass
env: staging
evidence_shas: [745f2fa, 2fef2e5]
files: [backend/neoh_intents.py, backend/neoh_render.py, backend/search_api.py, backend/source_status.py, backend/tests/test_neoh_intents.py, backend/tests/test_search_api.py]
tests_run:
  - {cmd: "grep -n degraded backend/neoh_intents.py; sed -n 351,384p backend/search_api.py", result: "the fast path ignores degraded search legs and shows Nothing matches", date: 2026-10-12}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/test_neoh_intents.py tests/test_search_api.py", result: "35 passed, exit 0 (8 new forced-failure cases run the real search_api.search with one leg raising)", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/", result: "3527 passed, 42 skipped, exit 0", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
  - {cmd: "scope of the a7fcaab staging check for this row", result: "Deployed only: the staging smoke above did not exercise this row's behaviour (search-leg failure answer / SemanticFrame / mascot states). Evidence for the behaviour itself is the local tests listed above.", date: 2026-10-14}
authz_checked: "no access change: every leg still runs in the caller's tenant_tx; the status entries carry no exception text"
blockers: []
audit_refs: [A02]
decision_refs: []
next_task: "Extend the same per-source status to the chat model's own lookup tools (06/07)"
updated: 2026-10-14
```
AC05.07 evidence. `search_api.search` now returns `source_status: {kind: entry}` in the C6 vocabulary (`source_status.py`: `checked` with `matched`, `timed_out`, `unavailable`, `not_applicable` with reason `query_too_short`) beside the existing `degraded` list, which is unchanged for its frontend readers. The ⌘K resolvers (`neoh_intents._show_person`, `_properties_for`, `_deal_blocker`, and the address fallback) read the status of each leg they used (`_leg_status`). A failed leg returns `neoh_render.sources_failed`: "I couldn't search people for … the search timed out. This is not a “no match”", plus an `evidence` block naming each failed source, with `degraded: true` and the per-source status. It is drawn on screen, not passed to chat. A leg named in `degraded`, or one with no status at all (`status_missing`), counts as failed. "Nothing matches" now appears only beside a `checked` leg, and every search-backed answer carries `degraded` and `source_status`. Not done: the chat model's tools (`ai_tools*.py`) do not use `search_api`, and their failure reporting is not covered here.

#### L05-02 SemanticFrame v1: typed, validated, authorised as the caller
```yaml
id: L05-02
status: tests_pass
env: staging
evidence_shas: [2fef2e5]
files: [backend/semantic_frame.py, backend/tests/test_semantic_frame.py, backend/tests/test_semantic_frame_access_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/test_semantic_frame.py", result: "31 passed, exit 0 (fake gateway; no model called)", date: 2026-10-14}
  - {cmd: "livetest (real PostgreSQL, app + platform roles, FORCE RLS): pytest tests/test_semantic_frame_access_live.py", result: "1 passed: agents A/B, C with a view share, broker owner, platform admin and agent D of another brokerage each keep exactly the candidates they may open (client, contact, lead, deal); a nonexistent id is dropped for all; a brokerage-scope context is refused; revoking C's share drops B's records on re-authorisation", date: 2026-10-14}
  - {cmd: "cd backend && bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json", result: "exit 0", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
  - {cmd: "scope of the a7fcaab staging check for this row", result: "Deployed only: the staging smoke above did not exercise this row's behaviour (search-leg failure answer / SemanticFrame / mascot states). Evidence for the behaviour itself is the local tests listed above.", date: 2026-10-14}
authz_checked: "candidates re-read through record_access (app_client_access / app_contact_access / app_linked_access) and RLS on transactions, in the requesting person's own tenant_tx; brokerage_scope contexts refused"
blockers: [{type: decided, note: "Owner 2026-10-14: English + Spanish at launch (built in L05-04)"}, "a scored extraction run (AC05.08) needs model-spend approval"]
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "T05.04 + T05.07 + AC05.04: carry frame constraints into 07 tool calls and 08 mission steps; then wire extract_frame behind the fast path (T05.05)"
updated: 2026-10-14
```
AC05.02 and AC05.03 evidence. `backend/semantic_frame.py` defines `SemanticFrame` `schema_version: 1`, with goals (a closed label set, each with a minimum effect class), entity mentions with candidate ids, times with an IANA zone and offset-aware datetimes, constraints (`no_send`, `draft_only`, `approval_required`, `not_before`, `do_not_contact`), scope, required data, an effect class (`read` / `internal_edit` / `external_effect`, mapped from `ActionRisk` by `effect_for_risk`), unfilled slots, ambiguities and verbatim evidence. `validate_frame` rejects an unknown version, extra fields, a "don't send" beside a live send (F05.03), an effect that does not match the goals, a time without an offset, and dangling indexes. It also strips every server-decided field (`authorized`, candidate `access`, ambiguity choices), so authorisation is never deserialised. `admit_for_planning` re-validates the frame and requires it to be authorised with nothing left to ask. `extract_frame` makes one `llm_gateway.complete(task="fast", response_format=strict json_schema)` call, retried once. The model sees records only by number and never sees an id; it is refused for a reference outside the list, for writing ids, or for evidence not quoted from the request. `authorize_frame` re-reads every candidate as the caller, drops the ones the caller cannot see (it reports only a count), and turns 0 or 2+ survivors into one question. Not wired into any route yet: nothing calls `extract_frame` in production. No model-scored run has been made. (2026-10-14: wired behind the fast path by L05-03.)

#### L05-03 Stated constraints reach tool calls and mission steps; parser behind the fast path (ON by default)
```yaml
id: L05-03
status: tests_pass
env: local
evidence_shas: [7b198cf]
files: [backend/frame_constraints.py, backend/semantic_turn.py, backend/ai_chat_store.py, backend/ai_chat_agent.py, backend/missions/policy.py, backend/missions/executor.py, backend/missions_api.py, backend/semantic_frame.py, backend/privacy_data_map.py, docs/privacy-data-map.md, backend/tests/test_frame_constraints.py, backend/tests/test_frame_constraints_live.py, backend/tests/conftest.py, .env.example, .github/workflows/ci.yml]
tests_run:
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/test_frame_constraints.py", result: "178 passed, exit 0 (fake gateways only; includes the every-tool x every-constraint table and the utterance -> _generate -> execute_safe_tool -> ledger end-to-end turns)", date: 2026-10-14}
  - {cmd: "livetest (real PostgreSQL, app role, FORCE RLS): pytest tests/test_frame_constraints_live.py tests/test_semantic_frame_access_live.py", result: "2 passed: A's frame authorised and its discourse re-read as A (B's client never offered to A's parser); summary stored on A's own assistant row with no words; B's write to A's row changes nothing; draft_sms refused, ai_tool_operations row (user A, failed, FRAME_NO_SEND), 0 command_executions; B reads 0 of A's refusals", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/  (flag unset = ON)", result: "3984 passed, 48 skipped, exit 0", date: 2026-10-14}
  - {cmd: "ORACLE_SEMANTIC_FRAME_ENABLED=0 pytest tests/test_semantic_frame.py test_ai_chat*.py test_ai_tool_operations.py test_missions_*.py test_neoh_intents.py test_neoh_persona.py test_llm_gateway.py test_security_launch_review.py test_search_api.py test_frame_constraints.py", result: "489 passed, exit 0", date: 2026-10-14}
  - {cmd: "test_frame_constraints.py::TestTurn::test_added_latency_with_an_instant_parser (-s)", result: "parser-tier overhead with a zero-latency fake gateway: p50 0.58 ms, p95 0.83 ms, n=50, local laptop. Not a model latency.", date: 2026-10-14}
  - {cmd: "cd backend && bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json", result: "exit 0", date: 2026-10-14}
authz_checked: "authorize_frame and the discourse re-read (resolve_record) run in the requesting person's own tenant_tx; the frame summary is written to their own ai_chat_messages row (RLS: tenant + user); refusals go to ai_tool_operations as that user; a constraint is only ever NARROWED to candidates authorize_frame kept"
blockers: [{type: decided, note: "Owner 2026-10-14: Platform Use Policy v7 §4 sentence approved (cb2aedd)"}, {type: decided, note: "Owner 2026-10-14: launch languages English + Spanish (L05-04)"}, "a measured real-model parser latency needs model-spend approval"]
audit_refs: []
decision_refs: [D-2026-10-13-01]
next_task: "T05.10 asked in the chat turn since L05-08 (the fast path already asks via person_choices); F05.04 done in L05-07; then T05.13 eval suite"
updated: 2026-10-14
```
AC05.04 evidence (local). **Switch:** `ORACLE_SEMANTIC_FRAME_ENABLED`, **ON by default** (owner, 2026-10-14): unset = on; `0`/`false`/`off`/`no` = off, which is exactly the previous behaviour (no parser call, nothing stored, nothing enforced; tested). **Routing (T05.05):** the ⌘K fast path (`neoh_intents`, `/api/neoh/ask`) is unchanged and makes no model call; on a miss the text becomes a chat turn, and `ai_chat_agent._generate` (`:1131`) starts `semantic_turn.prepare_turn` (`:277`) first, concurrently with the turn's grounding reads: one `fast` structured call bounded by `ORACLE_SEMANTIC_FRAME_TIMEOUT_SECONDS` (default 3 s wall clock, retries included, capped at 8), then `authorize_frame` as the caller. No provider, a timeout or an unusable answer leaves a frame-less turn (tested with the real gateway and nothing configured, and in `ORACLE_RECOVERY_MODE=1`). Real-model latency is a target (≤ the 3 s budget), not a measurement. **Discourse (T05.04, partial):** the turn's summary (goal labels, authorised record ids, constraint kinds and times, never words) is stored on its own assistant row `ai_chat_messages.metadata.semantic_frame`; the next turn reads it from the 24 messages it already loads, re-reads earlier records as the caller to offer them to the parser by number, and keeps constraints for 30 minutes. An explicit later request to reach out ("OK, text her now") lifts the no_send / draft_only / do_not_contact it contradicts; approval_required and not_before only expire. If constraints are in force and cannot be stored, the turn fails honestly. **Enforcement (T05.07)**, one table in `frame_constraints.py` for both boundaries: `check_tool` (`:324`) at `ai_chat_store.execute_safe_tool` (`:1875`, every provider loop) and `check_mission_step` (`:384`) at `missions.policy.evaluate_action` (`:251`). no_send / do_not_contact refuse every tool that contacts a person (draft_email, draft_sms, call_contact, schedule_event, share_property_tour) and withhold mission email/sms/voice steps; draft_only allows drafts (email, text, contract) and refuses other gated requests, and a mission stages drafts but never self-releases (`:334`) or stages a call; approval_required refuses direct CRM edits and blocks mission self-release; not_before refuses effectful tools before the time and moves a mission step's due_at to it (`executor._work_one :152`, journalled once as `action_deferred`). Fail closed: a person nobody could resolve, or an unknown tool target, counts as everyone; an unreadable stored list becomes no_send + approval_required. A refusal returns `{ok:false, refused:true, constraint, error}` to the model and is written to `ai_tool_operations` (status `failed`, error_code `FRAME_<KIND>`) when the call has a call_index. Missions: `missions_api.create_mission` (`:93`) parses the objective once as its creator into the existing `missions.constraints` column (no migration); the mission JSON shows `stated_constraints`. Not done: the Bedrock loop calls the chokepoint without a call_index, so its refusals are enforced but not ledgered; a correction does not cancel a command already staged (F05.04 half); a do_not_contact in a mission objective names nobody the parser can resolve (no records are offered), so it holds every outbound step; no per-agent time zone (deployment default `ORACLE_SEMANTIC_FRAME_TIMEZONE`, America/New_York); manager-routed approval for "after the manager signs off" is not built (any approver can approve the staged request).

#### L05-04 English and Spanish at launch: parser, backstop, fast path, refusals, per-language scoring
```yaml
id: L05-04
status: tests_pass
env: staging
evidence_shas: [012f655]
files: [backend/semantic_language.py, backend/semantic_frame.py, backend/semantic_turn.py, backend/frame_constraints.py, backend/ai_chat_agent.py, backend/ai_chat_store.py, backend/missions/policy.py, backend/neoh_intents.py, backend/neoh_render.py, backend/commands_api.py, backend/semantic_eval.py, backend/scripts/eval_semantic_frame.py, backend/eval_data/semantic_frames_v1.jsonl, backend/privacy_data_map.py, docs/privacy-data-map.md, backend/tests/test_semantic_language.py, backend/tests/test_frame_constraints.py]
tests_run:
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/test_semantic_language.py", result: "989 passed, exit 0 (fake gateways only; 129 fixture utterances x backstop, verbatim quotes, detection, parser contract, correct / forgetful / translating parser turns; fast-path and command twins; refusal language; harness)", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/  (flag unset = ON)", result: "4975 passed, 48 skipped, exit 0", date: 2026-10-14}
  - {cmd: "ORACLE_SEMANTIC_FRAME_ENABLED=0 pytest tests/test_semantic_frame.py test_ai_chat*.py test_ai_tool_operations.py test_missions_*.py test_neoh_intents.py test_neoh_persona.py test_llm_gateway.py test_security_launch_review.py test_search_api.py test_frame_constraints.py test_neoh_render.py test_personal_command_parser.py", result: "571 passed, exit 0", date: 2026-10-14}
  - {cmd: "python scripts/eval_semantic_frame.py --backstop-only --split held_out  (FIRST run, before any fix)", result: "backstop alone, exact constraint kinds: en 10/10, es 14/15, mixed 6/10 (30/35); preservation en 1.0 / es 0.933 / mixed 0.6; over-restriction 0; unauthorized_actions 2 (mx-h02 'Solo un draft', mx-h05 \"Don't llames\": code-switching inside one phrase). The 5 misses were fixed and moved to dev, so held_out is now 30 cases. Written by the lexicon's author: not independent.", date: 2026-10-14}
  - {cmd: "python scripts/eval_semantic_frame.py --backstop-only --split all", result: "129 cases (dev 99, held_out 30): preservation 1.0 en / es / mixed, over-restriction 0.0, unauthorized_actions 0. dev is what the lexicon was written against: not a held-out number.", date: 2026-10-14}
  - {cmd: "python scripts/eval_semantic_frame.py --split held_out  (no --approve-spend)", result: "exit 2, refused: 'scoring the model parser spends model tokens'. The model parser has NOT been scored in any language.", date: 2026-10-14}
  - {cmd: "bench: semantic_language.cues + detect, local laptop, n=2000", result: "82 chars p50 0.24 ms / p95 0.25 ms; 576 chars p50 1.1 ms / p95 2.5 ms", date: 2026-10-14}
  - {cmd: "cd backend && bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json", result: "exit 0", date: 2026-10-14}
  - {cmd: "staging after 12e4c34 (CI run 38629541376 green), POST /api/neoh/ask as the demo agent, read-only", result: "'muéstrame propiedades para Sarah' -> intent properties_for, answered in Spanish ('Todavía no hay nada en la lista de Sarah Johnson…'); its English twin -> same intent, English answer. '¿qué está frenando 123 Main?' -> intent deal_blocker (understood) but the answer is still English — the known gap (fast-path success answers not yet translated). Neither fell through to chat.", date: 2026-10-14}
authz_checked: "no access change: the backstop only ADDS restrictions (unscoped; do_not_contact marked unresolved = everyone), never scopes one to an unchecked identity and never lifts one; a turn whose backstop added a restriction cannot also lift; check_tool / check_mission_step decide exactly as before, only the sentence changes language. No migration, no grant."
blockers: ["owner: model-spend approval to score the parser per language (scripts/eval_semantic_frame.py --approve-spend) and to measure real parser latency in Spanish", "owner/linguist: an independent Spanish (and mixed) held-out set; the current one is author-written"]
audit_refs: []
decision_refs: ["owner 2026-10-14: English + Spanish at launch (L07-01 decided note)", D-2026-10-13-01]
next_task: "Owner-approved scored run of the T05.13 harness (en/es/mixed); T05.10 done in L05-08"
updated: 2026-10-14
```
**What changed.** *Parser* (`semantic_frame.py`): the system prompt (`:571`) says requests may be English, Spanish or mixed, gives the Spanish phrasing of each constraint kind, and forbids translating a quote. Evidence is compared after NFC + casefold + typographic-quote folding (`_canon :597`, `_grounded :608`), so a decomposed "todavía" or a phone's ’ still matches; an accent-stripped, translated or paraphrased quote is still `ungrounded_evidence`. *Backstop* (`semantic_language.py`): a deterministic bilingual lexicon (`_LEXICON :242`, `cues :316`) reads the five owner examples and their English twins, with verbatim spans; a "don't send" qualified in the same clause by a time or an approval becomes that qualifier ("no le mandes nada hasta el lunes" = not_before), and a draft request with a "don't send" is draft_only. `frame_constraints.reinforce` (`:300`) adds a cue's restriction only when the parser's frame lacks it (or the parser failed); a not_before cue with no trusted time becomes the strict pair (`strict :291`). `semantic_turn.apply_backstop` (`:366`) runs it in every chat turn (`prepare_turn :348`) and mission objective. **English behaviour change:** before, a parser failure on "Don't message her yet" left a frame-less turn; now the backstop enforces no_send (the two L05-03 tests that pinned the old behaviour were changed to a neutral sentence plus a new backstop test). *Fail closed:* a request `detect` (`:162`) classes as another language (Portuguese, French, German, Italian, non-Latin script) still goes to the parser and its frame stands; with no usable frame the turn takes no_send + approval_required. *Fast path:* each of the five `INTENTS` patterns has a Spanish twin (`neoh_intents.py:233-299`, accents optional), resolved in the twin's language (`:335`); `commands_api` `/classify` (`_classify_explicit :1370`) and `_parse_intent` read Spanish channel verbs (correo / texto, mensaje de texto / llama, telefonea / agenda, programa una cita), with a Spanish "el texto de" noun guard (`:863`), "de acuerdo" not an agreement, and an accent-keeping name hint (`_name_hint_es :949`); a bare "mensaje" names no channel, like "message her". *Language of answers:* no locale field exists anywhere in the schema, so the request decides (`reply_language :190`; a too-short turn keeps the conversation's stored language; default English). Refusals (`check_tool :389`, via `ai_chat_store.py:1881`), mission step reasons (`check_mission_step :445`, via `missions/policy.py:374`), `neoh_render.sources_failed` (`:80`), verified misses (`no_match :123`), person/deal choices, the public-record shortlist failures and the empty call queue come from one en/es catalogue (`MESSAGES :362`; a test holds every key to both languages with the same slots). Machine codes (`FRAME_*`) are identical in both. The frame summary stores `lang`, `detected` and `backstop` (codes only, no words). *Harness* (`semantic_eval.py`, `scripts/eval_semantic_frame.py`): T05.13 metrics (parse/abstention, intent, constraint exact/precision/recall, preservation, over-restriction, clarification, entity, unauthorized actions, latency) reported `by_lang` en / es / mixed and overall; `--backstop-only` is free; the model run refuses without `--approve-spend`. Dataset `eval_data/semantic_frames_v1.jsonl`: 65 es, 33 mixed, 31 en (dev 99, held_out 30). **Not done:** the model parser has not been scored in any language (spend not approved); the fixture set is author-written, not independent; ~~voice input is still recognised as `en-US` (`useSpeechInput.js:89`), so spoken Spanish reaches the parser as an English transcript; success answers of the fast path (person card summary, deal headline, shortlist count) and command fallback drafts (`_fallback_draft`) are still English~~ (both closed locally by L05-05, 2026-10-15; the call-queue card text from `opportunity_engine` is still English, see L05-05); Spanish time words in `not_before` are read by the backstop only as a cue (no time is computed deterministically; it falls back to the strict pair); no languages beyond en/es are detected as such except to fail closed. Policy v7 §4 unchanged (no bump).

#### L05-05 Spanish success answers on the fast path, Spanish fallback drafts, voice input in English or Spanish
```yaml
id: L05-05
status: tests_pass
env: local
evidence_shas: [8956127, bf90efc, f8142c1]
files: [backend/semantic_language.py, backend/neoh_render.py, backend/neoh_intents.py, backend/intent_states.py, backend/belief_store.py, backend/expected_value.py, backend/commands_api.py, backend/intelligence_os_api.py, backend/ai_chat_models.py, backend/ai_chat_api.py, backend/ai_chat_agent.py, backend/semantic_turn.py, backend/tests/test_fast_path_language.py, backend/tests/fast_path_language_cases.py, backend/tests/fast_path_en_golden.json, oracle-app/src/neoh/speechLanguage.js, oracle-app/src/neoh/speechLanguage.test.js, oracle-app/src/neoh/useSpeechInput.js, oracle-app/src/neoh/useSpeechInput.test.js, oracle-app/src/neoh/useNeohComposer.js, oracle-app/src/neoh/useNeohChannel.js, oracle-app/src/neoh/NeohComposer.jsx, oracle-app/src/neoh/NeohComposer.module.css, oracle-app/src/neoh/NeohConversation.test.jsx]
tests_run:
  - {cmd: "python -m tests.fast_path_language_cases at da163ba (before any change)", result: "33 English outputs frozen in tests/fast_path_en_golden.json: 12 ask-path answers over every fast-path intent, 9 renderer branches, 12 fallback drafts (6 intents x with/without client). Committed alone as 8956127; the only later edit strips the wall-clock checked_at", date: 2026-10-15}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/test_fast_path_language.py tests/test_semantic_language.py tests/test_neoh_render.py tests/test_neoh_intents.py tests/test_personal_command_parser.py", result: "1099 passed (test_fast_path_language: 68; English byte-identical to the golden file; every Spanish twin answers in Spanish with no English product word and the same record content)", date: 2026-10-15}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 pytest -q -p no:cacheprovider tests/", result: "5066 passed, 51 skipped, exit 0 (live-DB tests skipped: no ORACLE_LIVE_DB_ADMIN_DSN; no migration in this slice)", date: 2026-10-15}
  - {cmd: "cd backend && bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json", result: "exit 0", date: 2026-10-15}
  - {cmd: "cd oracle-app && npx vitest run && npm run lint && npm run build && npm run bundle:check", result: "vitest 890 passed (90 files); eslint exit 0; build ok; within budget (largest chunk 577.7 KB gzip, unchanged)", date: 2026-10-15}
  - {cmd: "temporary harness (deleted) rendering the floating bar and the Neoh-tab bar with the real CSS modules, Playwright + Chrome at 390 and 320 px", result: "390: no horizontal overflow; EN/ES 32x44 beside the 44x44 mic; field 96 px in the fullest floating bar (harness gap 8 px; the app uses 4 px below 720 px) vs 128 px without the switch; Neoh tab field 208 px. 320: no overflow but the fullest floating bar field is 26 px (58 px before). jsdom/Chrome desktop only: no real iPhone Safari or Android check", date: 2026-10-15}
  - {cmd: "staging after 45a031d (CI green), scripts/staging-smoke.py", result: "deployed; the smoke reads are unchanged. The Spanish answers and the voice switch were not exercised on staging by this check — evidence for them is the local tests above.", date: 2026-10-15}
authz_checked: "no access change: only sentences change language. The hint is a two-letter code (normalised to en|es, anything else dropped) that never decides authorisation, constraints or routing; it is not stored except inside the existing automation_jobs payload. No migration, no grant, no RLS change."
blockers:
  - "Call-queue card text (opportunity headline / why / recommended action, built in English by opportunity_engine for the Command Center) is still English on a Spanish 'who should I call'; the spoken line, the metric label and the portfolio caveat are Spanish. Translating it needs a language through command_center.briefing and the detectors (next slice)."
  - "Frontend chrome of the answer blocks (registry.jsx labels such as counts and buttons) and all other UI copy are English: there is no frontend i18n."
  - "Real-device check of the EN/ES switch and es-* recognition on iPhone Safari / Android Chrome is owed."
audit_refs: []
decision_refs: ["owner 2026-10-14: English + Spanish at launch", "Platform Use Policy v7 §4 covers this; no policy change"]
next_task: "Spanish call-queue cards (opportunity_engine + command_center.briefing lang); then the L05-04 next task"
updated: 2026-10-15
```
**What changed.** *Fast-path success answers* (`neoh_render.py`): every product sentence of a successful answer now comes from the en/es catalogue (`semantic_language.MESSAGES`, new keys from `:502`): the call-queue spoken line (`:157`) and metric label; the person card's name fallback, subtitle enums (`_enum_word :210`; client_type / stage, stored values in English), its reading (`_intent_summary :218`, re-rendered in Spanish from the verdict and the two scores via `intent_states.reconcile_summary :271`) and its dispute questions (`_dispute_label :233`, via `belief_store.dispute_question :319`); "Unnamed"; the shortlist count, title, "Address unavailable" and beds/baths (`:331`); the deal headline in all three cases and its fallbacks (`deal_blocker :344`, "este trato" inside a Spanish sentence); the portfolio caveat (`_portfolio_caveat :436`, re-rendered from its counts via `expected_value.portfolio_caveat :277`). Timeline rows without a subject show the interaction type's Spanish name (`neoh_intents._interaction_word :355`). English: `intent_states._reconcile`, `belief_store._dispute_question` and `expected_value.portfolio` now read their English from the same catalogue; what they store and what the Command Center shows is unchanged. *Never translated:* names, addresses, milestone titles and types (free text on the row), timeline subjects, recorded belief values, match reasons, the predicate / source machine names. *Fallback drafts* (`commands_api._fallback_draft :1000`): written in the command's language (`reply_language(raw_text) :1012`); with no client name Spanish uses a bare greeting ("Hola:"). *Voice:* `useSpeechInput` takes `lang` and sets `SpeechRecognition.lang` before `start()` (`:96`), reporting the tag with the utterance (`:114`); `speechLanguage.js` picks the default (Spanish when `navigator.language` starts with "es", keeping its region, else en-US; manual Spanish = es-US), remembers the choice per device in localStorage (wrapped in try/catch) and makes the hint; `VoiceLanguageSwitch` (`NeohComposer.jsx:60`) is a two-letter toggle beside the mic with a label saying the current language and what pressing does, hidden where SpeechRecognition is missing (the disabled mic fallback is unchanged). No server-side user setting. *Hint:* only a SPOKEN turn sends `lang` (typed turns send exactly what they did): `/api/neoh/ask` accepts it (`intelligence_os_api.py:72`) but the matched pattern's language still decides there; the chat frame carries it (`ai_chat_models.py:30`, normalised) into the job payload (`ai_chat_api.py:325`) and the bundle (`ai_chat_agent.py:1367`), where `reply_language` (`:205`) uses it only when the words are not conclusive (too short, another language, an even mix), ahead of the conversation's previous language. **Defects found on the way:** (1) "contrato de cesión" drafted a seller_purchase, because only the English "assign" was checked; fixed (`_assignment_requested :994`). (2) Not fixed, English unchanged on purpose: the calendar fallback draft with no client reads "Follow-up with there"; the ⌘K metric reads `portfolio.total` while `expected_value.portfolio` returns `total_expected_value`, so the "Expected value of acting on all of it" figure is always empty on the fast path. **Other hard-coded 'en-US' in oracle-app/src:** none for speech or TTS (no speechSynthesis anywhere; the legacy useJarvisVoice no longer exists); the remaining 31 are Intl number / date formatting in 16 files (RehabEditorDrawer 7, MarketDataPanels 4, ...), out of scope. Policy v7 unchanged.

#### L05-06 The call queue's cards in Spanish (opportunity_engine)
```yaml
id: L05-06
status: tests_pass
env: local
evidence_shas: [ae16549]
files: [backend/opportunity_engine.py, backend/command_center.py, backend/neoh_intents.py, backend/buyer_matching.py, backend/semantic_language.py, backend/tests/opportunity_language_cases.py, backend/tests/opportunity_en_golden.json, backend/tests/test_opportunity_language.py, backend/tests/test_opportunity_engine.py]
tests_run:
  - {cmd: "cd backend && python -m tests.opportunity_language_cases at 26e06e1 (before any change)", result: "14 English cards frozen in tests/opportunity_en_golden.json: 3 contract-deadline, 6 next-best-action (every fixed client_ai_automation title, a model suggestion, a legacy string, an untitled action, a missing name and summary), 3 listing-buyer (0, 1 and 2 other buyers, an unparseable address, a blank name), 2 distress. Committed alone as ae16549", date: 2026-10-15}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_opportunity_language.py tests/test_opportunity_engine.py tests/test_buyer_matching.py tests/test_fast_path_language.py tests/test_semantic_language.py", result: "all passed (test_opportunity_language: 20); English (lang None, en, fr, zz) byte-identical to the golden file; every Spanish card free of the English card words; same cards, order, confidence, ids, deadlines and evidence sources in both languages", date: 2026-10-15}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests compliance_engine/tests (Python 3.13 + audioop-lts; CI uses 3.12)", result: "5187 passed, 58 skipped, 2 failed: test_media_storage::test_a_named_but_unwritable_mount_is_not_configured and test_object_storage::test_is_configured_reports_whether_a_write_could_succeed[env0-False] fail identically with the change stashed (the container runs as root, so the 'unwritable' path is writable); live-DB tests skipped (no ORACLE_LIVE_DB_ADMIN_DSN; no migration in this slice)", date: 2026-10-15}
  - {cmd: "cd backend && bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json", result: "exit 0", date: 2026-10-15}
authz_checked: "no access change: only sentences change language. The same detectors run the same tenant-scoped queries; lang is the fast path's matched-pattern language (en|es), never an authorisation, ranking or routing input. No migration, no grant, no RLS change. buyer_matching's property_buyer_matches response gains a matched_detail list (key + values of the notes it already returned)."
blockers:
  - "Record content stays as written in Spanish: client names, addresses, the intent model's own suggestion title (client_ai_automation 'model-suggestion') and its summary (model text, English today), stated needs, showing outcomes, and a recorded property type ('condo')."
  - "The Command Center screen calls briefing() without a language and stays English, as do its horizon labels (Now, Today, ...): there is no frontend i18n and no per-user language setting."
  - "Not deployed: pushing to main deploys staging (CONTRACT C4.4); owner approval owed."
audit_refs: []
decision_refs: ["owner 2026-10-14: English + Spanish at launch", "Platform Use Policy v7 §4 covers this; no policy change"]
next_task: "Push to main with approval and check a Spanish 'who should I call' on staging; then the L05-04 next task (scored T05.13 run once spend is approved)"
updated: 2026-10-15
```
**What changed.** `opportunity_engine.scan(ctx, lang=None)` hands the language to every detector (`detector(conn, ctx, lang)`); `command_center.briefing(ctx, lang=None)` passes it on; `neoh_intents._who_to_call` sends the request's language (it used to call `briefing(ctx)`). Every card sentence comes from the en/es catalogue (`semantic_language.MESSAGES`, keys `opp.*`, `fit.*`, `nba.*`, `gap.*`, `dossier.*`): the contract-deadline headline (singular/plural), why, fallback subject and address, unset status and action; the next-best-action fallbacks, evidence labels and "unscored"; the listing-match headline, why, the "N other buyers" tail, the action and evidence labels; the distress headline, why, action and evidence labels. `leads.dossier_status` values (0007 CHECK plus 'new') are named in Spanish. **Producer text, by exact match only** (`_known`): client_ai_automation's three fixed next_actions titles are translated by their code, and its five data_gaps labels (two for the property lookup) by code, but only when the stored text is exactly the catalogue's English; an edited title or a model suggestion is shown as stored. A test reads client_ai_automation's source to keep that English in step. **Matched criteria** are said from their values, not translated: buyer_matching's match Signals now carry `key` + `params` beside the English note (the English note is unchanged and a test checks each key's English template reproduces it), `property_buyer_matches` returns them as `matched_detail`, and `_fit_notes` renders Spanish from them; a note without them stays as written. buyer_matching's own "Unnamed contact" fallback reads "Contacto sin nombre". English is byte-identical for `lang` None, en and any unsupported code. **Defects found, English unchanged on purpose:** a blank buyer name gives "May fit " and "Ask Neoh about the and this home" (unreachable today: buyer_matching substitutes "Unnamed contact", which in turn gives "Ask Neoh about Unnamed and this home"); the Spanish path says "el comprador" in both cases.

#### L05-07 A correction withdraws what the previous turn staged (F05.04; T05.04 remainder)
```yaml
id: L05-07
status: tests_pass
env: local
evidence_shas: [78ccb77]
files: [backend/correction.py, backend/commands_api.py, backend/ai_chat_agent.py, backend/tests/test_correction.py]
tests_run:
  - {cmd: "read semantic_frame / semantic_turn / frame_constraints and commands_api.reject_command at 78ccb77", result: "no correction concept anywhere: 'actually, the other Sarah' left the first Sarah's staged text awaiting approval (L05-03 'Not done')", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_correction.py", result: "24 passed: 13 English/Spanish corrections recognised, 8 ordinary requests not (incl. 'What did Sarah actually say?', 'Send the other listing', 'Mejor precio...'); only the previous turn's still-awaiting-approval requests created by this agent are withdrawn (not sent ones, not a colleague's, not a reused existing request); the model is told to say so; a DB failure never breaks the turn", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_command*.py tests/test_ai_chat*.py tests/test_semantic*.py tests/test_frame*.py tests/test_approval*.py", result: "1253 passed, 4 skipped (reject route refactored into commands_api.withdraw_command, same behaviour)", date: 2026-10-16}
authz_checked: "Withdrawal runs as the agent, through the same path as their Reject button (decide_approval + cancel + first-response ledger), only for commands whose created_by is that agent and still awaiting_approval; receipts are read through the sealed-result reader under the agent's RLS."
blockers:
  - "CI caught a source-inspection test pinning Stage.CANCELLED inside reject_command (tests/test_outreach_lifecycle.py:403) after the refactor was pushed without the full suite; the test now pins reject_command -> withdraw_command -> Stage.CANCELLED. Full backend suite afterwards: 5220 passed, 2 root-only failures."
  - "Deterministic, opening-words cue only (en/es). A correction phrased later in the sentence is missed and the request stays for a human to reject (the previous behaviour). The parser does not mark corrections; T05.13's eval set should include them."
  - "No real-PostgreSQL end-to-end test of the chat turn (unit-level with the real withdraw path covered by the existing reject tests)."
  - "The UI is not told separately; the model's reply says it, and the approval disappears from the queue."
audit_refs: []
decision_refs: []
next_task: "T05.10 done in L05-08 (chat turn asks and the chokepoint refuses picking); owner-approved T05.13 scored run."
updated: 2026-10-16
```

#### L05-08 The chat turn asks the frame's one precise question and cannot pick a record instead (T05.10, AC05.03)
```yaml
id: L05-08
status: tests_pass
env: local
evidence_shas: [c7c3249]
files: [backend/semantic_turn.py, backend/frame_constraints.py, backend/semantic_language.py, backend/ai_chat_agent.py, backend/ai_chat_store.py, backend/tests/test_precise_question.py, backend/tests/test_frame_constraints.py]
tests_run:
  - {cmd: "read semantic_frame.authorize_frame, semantic_turn.prepare_turn/TurnFrame/bind, ai_chat_agent._generate and ai_chat_store.execute_safe_tool at c7c3249 (three parallel readers; a fourth hit the subagent session limit)", result: "authorize_frame already built the one precise question (an Ambiguity with the authorised choices; L05-02) and the stored summary kept needs_clarification, but nothing in production read it: _generate used only the turn's constraints, so the model was free to pick one of two Sarahs and the chokepoint let it; the fast path (neoh_intents) asks via person_choices, the chat turn did not. Voice has no speech output in the frontend (spoken is rendered as text)", date: 2026-10-17}
  - {cmd: "cd backend && git stash the implementation, keep the tests; pytest tests/test_frame_constraints.py -k 'sarahs or one_sarah'", result: "1 failed, 2 passed: 'Text Sarah about the showing' with two Sarahs the agent can open dispatched draft_sms to one of them (['draft_sms'] == []); the one-Sarah control passed before and after", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_precise_question.py tests/test_frame_constraints.py tests/test_semantic_frame.py tests/test_semantic_language.py tests/test_ai_tool_operations.py tests/test_tool_offer.py tests/test_ai_chat.py tests/test_correction.py", result: "1323 passed: two authorised records become one Question with each choice labelled from the parser's known list; one record is an answer, not a question; nobody-you-can-open is still a question with no choices and names none of the dropped; the ASK FIRST block names the choices and the rule in English and Spanish and tells the model the server refuses picking; check_pending refuses contacting or editing a record still in question and creating a new person (FRAME_CLARIFY, bilingual, naming no id), lets reads and other records through; execute_safe_tool refuses from the bound question before the dispatcher and ledgers the attempt, and behaves as before for another turn or with the switch off; end to end, a previous turn's second Sarah is offered to the parser, the model is told to ask and name both, its draft_sms to one is refused and ledgered, nothing is dispatched, and the stored summary keeps the flag and ids but no name", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ (full unit suite); livetest.sh pytest tests/test_frame_constraints_live.py tests/test_semantic_frame_access_live.py tests/test_tool_offer_live.py", result: "5299 passed, 76 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container); 3 passed", date: 2026-10-17}
authz_checked: "The question is built only from candidates authorize_frame kept as the caller (dropped ones stay a count); labels come from the known-record list the parser was already shown and live for the turn only — the stored summary is unchanged (ids, kinds, the needs_clarification flag; no words, C5.4). The refusal names the mention as the person wrote it and never a record id. Pending is bound per turn (ContextVar keyed by the assistant id) and is None with the switch off."
blockers:
  - "The model asks in prose; the UI has no structured quick-reply for the choices and no frame field to carry the chosen record back (ChatSendFrame has text + context only). The next turn re-parses the person's answer against the same known list, which is how the question gets answered today. (Buttons added in L05-09.)"
  - "Review L05-11 fixed four defects in this slice: a question the parser wrote could hide the server's; only the first ambiguous mention was held; check_pending let non-person actions through while asking; the refusal and the tool log line could carry a name."
  - "Voice: no speech output exists in the frontend; 'name the choices in voice' has no seam beyond the fast path's spoken sentence."
  - "Contacts and deals are never offered to the parser as known records (semantic_turn._known_records filters to client/lead), so a person who exists only as a contact cannot be a choice."
  - "A name not in the known list (not selected, not resolved by an earlier turn) yields no candidates and no question; the model falls back to tools (search_clients), which can ask on its own but is not enforced. (Looked up since L05-10.)"
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Choice block done in L05-09; offer contacts as known records; owner-approved T05.13 scored run."
updated: 2026-10-17
```

#### L05-09 The chat UI offers the question's choices as buttons; picking one answers with that record (T05.10, T05.04)
```yaml
id: L05-09
status: tests_pass
env: local
evidence_shas: [a1ecb4a]
files: [backend/ai_chat_agent.py, backend/ai_chat_store.py, backend/tests/test_chat_choices.py, oracle-app/src/components/AssistantMessages.jsx, oracle-app/src/components/AssistantShell.module.css, oracle-app/src/state/oracleReducer.js, oracle-app/src/neoh/NeohConversation.jsx, oracle-app/src/neoh/NeohSurfacePanel.jsx, oracle-app/src/components/AssistantMessages.test.jsx, oracle-app/src/state/oracleReducer.aiChat.test.js]
tests_run:
  - {cmd: "read ai_chat_store.list_messages / update_assistant, handle_ai_chat_job's AI_CHAT_COMPLETE frame, AssistantMessages.jsx, oracleReducer.js and useNeohChannel.send at a1ecb4a", result: "an assistant message was plain text: no field carried the question's choices and nothing in the UI could send a chosen record back (ChatSendFrame has text + context only); the fast path's comparison block only navigates", date: 2026-10-17}
  - {cmd: "git stash the implementation, keep the tests; cd backend && pytest tests/test_chat_choices.py; cd oracle-app && npx vitest run src/components/AssistantMessages.test.jsx src/state/oracleReducer.aiChat.test.js", result: "3 failed; 2 failed (16 passed)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_chat_choices.py tests/test_frame_constraints.py tests/test_ai_chat.py tests/test_pan_redaction_paths.py tests/test_tool_offer.py tests/test_ai_chat_interrupted_turn.py tests/test_precise_question.py", result: "287 passed: update_assistant writes the choices as {type, id} into metadata (no name reaches plaintext) and leaves metadata alone without choices; the turn end puts the ids on the row and the labels on AI_CHAT_COMPLETE, [] / None without a question; list_messages re-reads each choice's label as the caller and drops one they can no longer open", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ (full unit suite); livetest.sh pytest tests/test_frame_constraints_live.py tests/test_reconciliation_live.py tests/test_sealed_text_two_live.py tests/test_tool_offer_live.py", result: "5302 passed, 76 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container); 4 passed", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "928 passed (the group 'Which one did you mean?' offers each record as a button; a click calls onChoose with the choice; nothing for a turn without a question and never on the person's own message; the reducer keeps the frame's choices); eslint exit 0; within budget. NeohConversation and NeohSurfacePanel send the label with {type, id} as the next turn's context", date: 2026-10-17}
authz_checked: "The row stores record ids only (metadata is plaintext jsonb; A01). Labels are read twice as the caller: at turn end from the parser's own known list, and on listing through resolve_record under RLS, so a record the person can no longer open is not offered again. A click sends the ordinary AI_CHAT_SEND with the chosen record as context; the next turn's parser resolves the mention against it (T05.04: the clarification amends the frame)."
blockers:
  - "The click sends the choice's label as the person's message text so the transcript reads naturally; the model answers the original request only if it reads it from history (no re-run of the original intent)."
  - "No Playwright check of the buttons (jsdom only)."
  - "Contacts are named in the question and held, but are not buttons (ChatContext and wireContext carry client / lead / listing / contract only); a person who exists only as a contact is answered in words (L05-11)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Lookup of an unplaced name done in L05-10 (contacts included as candidates); owner-approved T05.13 scored run."
updated: 2026-10-17
```

#### L05-10 A name the parser could not place is looked up as the caller, so the turn resolves it or asks (T05.10, AC05.03)
```yaml
id: L05-10
status: tests_pass
env: local
evidence_shas: [1012b33]
files: [backend/semantic_turn.py, backend/search_api.py, backend/tests/test_person_lookup.py, backend/tests/test_person_lookup_live.py, backend/tests/test_frame_constraints.py, backend/tests/test_search_api.py, backend/tests/test_fast_path_language.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read semantic_turn._known_records / _parse and semantic_frame.authorize_frame at 1012b33", result: "the parser may only refer to records it was shown (the selected record and up to 5 the previous turn resolved, clients and leads only). 'Text Sarah' with no Sarah on screen left a person mention with no candidates: authorize_frame asks only when a mention HAS candidates, so there was no question and the model searched and picked on its own", date: 2026-10-17}
  - {cmd: "git stash search_api.py semantic_turn.py, keep the tests; pytest tests/test_frame_constraints.py -k nobody_mentioned; livetest.sh pytest tests/test_person_lookup_live.py", result: "before: the turn dispatched draft_sms to a Sarah the model chose (['draft_sms'] == []); live, agent A's 'Sarah' had no candidates ([] == [A's Sarah]); 16 unit tests failed in all", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_person_lookup.py tests/test_search_api.py tests/test_frame_constraints.py tests/test_precise_question.py tests/test_semantic_frame.py tests/test_semantic_language.py tests/test_fast_path_language.py tests/test_neoh_intents*.py tests/test_chat_choices.py tests/test_missions*.py tests/test_client_ai_automation.py", result: "1485 passed: an unplaced person mention runs the fast path's own people search as the caller; each hit names its record (client, a contact's legacy client, or the contact) and becomes a candidate for authorize_frame, deduplicated, non-person hits and bad ids skipped; pronouns (en/es) and non-names are never searched; a placed mention is not searched; a failed, slow (1.5 s budget) or unchecked people leg changes nothing; two hits the caller can open become the turn's question with their labels, one becomes the answer and the other is a dropped count, never named; end to end, a Sarah nobody mentioned before is looked up and asked about, the model's draft_sms is refused (FRAME_CLARIFY) and nothing is dispatched. Two existing pins updated for the new shapes (the hit's 'record' key, _parse's fifth value)", date: 2026-10-17}
  - {cmd: "livetest.sh pytest tests/test_person_lookup_live.py tests/test_semantic_frame_access_live.py tests/test_frame_constraints_live.py (real PostgreSQL 16, FORCE RLS, app login, real search_api)", result: "3 passed: two clients named Sarah, agent A's and agent B's; agent A's 'Text Sarah' finds only A's Sarah, resolves to her without asking, and B's Sarah is not a candidate, not a dropped count, not in the summary; the owner, who can open both, gets one question naming both; the stored summary carries ids, never a name", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ (full unit suite); cd oracle-app && npx vitest run", result: "5317 passed, 77 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container); 928 passed", date: 2026-10-17}
authz_checked: "The search runs in the caller's own tenant transaction (search_api legs never duplicate an RLS predicate), so a record they cannot open is never found, never a candidate and never counted; authorize_frame still re-reads every hit's access as the caller before it can resolve or be offered. Labels live for the turn; the stored summary keeps ids and kinds only (C5.4)."
blockers:
  - "Each unplaced person mention adds one people search (up to 1.5 s, outside the parser's own 3 s budget) to the turn's preparation; nothing measures that latency on real traffic yet."
  - "Only person mentions are looked up; a property or deal named but not on screen is still left to the model's tools."
  - "The search is a substring match (clients ILIKE + contacts' blind index). Since L05-11 only hits scoring at least 0.7 (exact, prefix or word-start) become candidates, and at the 5-hit cap the question says 'at least 5' and names the strongest five, not every match."
  - "A lookup that finds nobody the caller can open asks nothing (so 'create a client named Sarah Moss' still works); the model is left to its tools for that name, as before L05-10."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Owner-approved T05.13 scored run (en/es/mixed); look up properties and deals named but not on screen the same way."
updated: 2026-10-17
```

#### L05-11 Review fixes: the server's question cannot be hidden, every ambiguous record is held, a lookup never narrows or lifts a restriction, nobody is named in logs (T05.10, AC05.03)
```yaml
id: L05-11
status: tests_pass
env: local
evidence_shas: [505d846]
files: [backend/semantic_turn.py, backend/semantic_frame.py, backend/frame_constraints.py, backend/semantic_language.py, backend/ai_chat_agent.py, backend/ai_chat_store.py, oracle-app/src/components/AssistantMessages.jsx, backend/tests/test_precise_question.py, backend/tests/test_person_lookup.py, backend/tests/test_chat_choices.py, backend/tests/test_frame_constraints.py, backend/tests/test_fast_path_language.py, oracle-app/src/neoh/NeohConversation.test.jsx]
tests_run:
  - {cmd: "adversarial review workflow wf_c4a15c04-be5 over L05-08..L05-10 at 505d846 (readers + verifiers)", result: "confirmed: (1) question_from took the first ambiguity, so one the parser wrote (choices stripped) hid the server's; (2) only the first ambiguous mention was held, so 'text Sarah and call Mike' let the call through; (3) the FRAME_CLARIFY refusal quoted the mention and the 'chat tool ... error=' log line carried exception text, so a name could reach logs; (4) lookup candidates scoped a do-not-contact to the hits and could lift one while still asking. Also reported and fixed here: check_pending let non-person actions and create_client through; weak substring hits became candidates; labels went into the prompt unfenced; same-name choices were identical; the 'nobody' block claimed server enforcement; English placeholders in Spanish turns; a capped search was counted as exact; contact choices rendered as dead buttons", date: 2026-10-17}
  - {cmd: "git stash the implementation, keep the tests; cd backend && pytest tests/test_person_lookup.py tests/test_precise_question.py tests/test_chat_choices.py; cd oracle-app && npx vitest run src/neoh/NeohConversation.test.jsx", result: "test_person_lookup 17 failed, 1 passed: a do-not-contact on an unplaced Sarah was scoped to the lookup's one hit ((CHEN,) is not None), a turn still asking lifted the standing do-not-contact on Chen, and a mission objective ran a people search (1 query != []); the rest fail on the new Lookup / lang shapes. test_precise_question and test_chat_choices fail at collection on the old Pending / Choice signatures. Frontend: 1 failed (the choice button showed the type, not 'buyer · active')", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_precise_question.py tests/test_person_lookup.py tests/test_chat_choices.py tests/test_frame_constraints.py tests/test_semantic_frame.py tests/test_semantic_language.py tests/test_fast_path_language.py tests/test_ai_chat.py tests/test_killer_demo_paths.py tests/test_missions*.py", result: "1515 passed: the question is built only from ambiguities authorize_frame marked server (the flag is stripped from parser output); a parser question never hides the server's; every ambiguous mention's authorised records are held and the others are named as 'also unclear'; while choices are pending every effectful tool is refused (FRAME_CLARIFY; create_client with its own guidance) and reads run; the refusal names nobody, in English and Spanish; choices go to the model as fenced JSON data ('<' escaped) with a type / stage / email detail; a capped search says 'at least'; placeholders are localised; only hits scoring >= 0.7 are candidates; constraints and lifts are computed without lookup candidates and nothing lifts while asking; missions never search; the tool log line is 'chat tool <name> ok|refused:<code>|failed' with no error text; AI_CHAT_COMPLETE carries only clickable (client / lead) choices with their detail", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py; livetest.sh pytest tests/test_person_lookup_live.py tests/test_frame_constraints_live.py tests/test_semantic_frame_access_live.py tests/test_tool_offer_live.py", result: "5289 passed, 77 skipped, 45 deselected (the container's root-only storage cases); 4 passed", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check", result: "929 passed (new: clicking a choice sends AI_CHAT_SEND with the label as content and {type: client, id} as context, and the button shows the detail); eslint clean; built; within budget", date: 2026-10-17}
authz_checked: "Unchanged boundaries: candidates are still kept or dropped by authorize_frame as the caller, the lookup still runs in the caller's tenant transaction, and the stored summary still holds ids and kinds only. New: the hold covers every authorised record of every ambiguous mention and fails closed for any effectful tool, not just person-targeted ones; refusals and the tool log line carry a code, never a mention or exception text (A01)."
blockers:
  - "Contacts are named and held but are not buttons: ChatContext / wireContext / resolve_record accept no contact type, so supporting one needs an ai_chat_messages.context_type CHECK migration and a contact resolver."
  - "Holding every action while asking also holds an unrelated action in the same turn (e.g. a task about someone else) until the person answers; the refusal says why. Chosen deliberately (fail closed)."
  - "The STRONG_MATCH threshold (0.7) and the 5-hit cap are judgement calls, not measured on real books."
  - "No Playwright check of the buttons (jsdom only). Not deployed (C4.4)."
audit_refs: [A01]
decision_refs: []
next_task: "Owner-approved T05.13 scored run; contact as a chat context (migration + resolver); look up properties and deals named but not on screen."
updated: 2026-10-17
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

#### L06-02 Search legs: elapsed time, a wall-clock bound and a bundle verdict (T06.06, AC06.01)
```yaml
id: L06-02
status: tests_pass
env: local
evidence_shas: [aefac40, 6c7133b]
files: [backend/search_api.py, backend/tests/test_search_api.py, oracle-app/src/neoh/searchModel.js, oracle-app/src/neoh/searchModel.test.js, oracle-app/src/neoh/UniversalWorkspace.jsx, oracle-app/bundle-budget.json]
tests_run:
  - {cmd: "read search_api.search at aefac40", result: "per-leg C6 source_status already existed (L05, T05.11) but no elapsed time; a leg was bounded only by SET LOCAL statement_timeout, so a stuck pool acquire or the comms rollup (no SQL budget) could hold the box open and never report timed_out; no bundle-level verdict; the Work box read only the bare `degraded` list, so a leg with no recognisable status showed as 'Nothing matches'", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_search_api.py with search_api.py stashed, then restored", result: "before: 2 failed (the AC06.01 bundle test and the clean/not_applicable test); after: 16 passed", date: 2026-10-16}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q tests/ (env -i)", result: "5139 passed, 66 skipped, 2 failed: test_media_storage / test_object_storage unwritable-mount cases, which fail only as root in this container (same as every earlier slice; green in CI)", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "896 passed; eslint exit 0; build ok; budget re-pinned (+0.1 KB for failedKinds on top of growth earlier slices had already spent from the 2 KB tolerance: 4121.4 KB raw total, largest chunk unchanged at 2271.2 KB)", date: 2026-10-16}
authz_checked: "No access change. Every leg still runs in the caller's own tenant_tx (RLS decides); the status carries stable reason codes only, never exception text or the query."
blockers:
  - "Legs named in T06.06 that do not exist yet: property sources (11), deal timeline, calls, files. Not added here: each needs its own RLS-checked query; the five existing legs (people, properties, deals, conversations, records) carry the full contract."
  - "`degraded` (the bare list) is kept one release so a client from the previous release still shows its banner; drop it once the new client is deployed."
  - "Freshness is checked_at only: every leg reads live tables in the request, nothing is cached. When T06.08 adds caches, an entry must carry the cache's own age."
  - "Not checked in a real browser (jsdom only). Not deployed (C4.4)."
audit_refs: [A02]
decision_refs: []
next_task: "T06.02 + AC06.02 (typed EvidenceBundle with a schema version, built on this per-leg contract); 11 T11.10 reuses it for client automation."
updated: 2026-10-16
```

#### L06-03 The evidence contract: versioned EvidenceFact and EvidenceBundle (T06.02, AC06.02; T06.04 beliefs adapter)
```yaml
id: L06-03
status: tests_pass
env: local
evidence_shas: [d7e3ce6, 63ac40b]
files: [backend/evidence.py, backend/tests/test_evidence_bundle.py]
tests_run:
  - {cmd: "git grep -n 'EvidenceBundle\\|evidence_bundle\\|context_compiler' backend at d7e3ce6", result: "nothing: no evidence type existed; beliefs (belief_store) carried provenance, decay and disputes in their own shape, search legs their own C6 status", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_evidence_bundle.py", result: "20 passed (new file: every contract rule refused when broken, round trip, version refusal, beliefs adapter, failed read -> unknown)", date: 2026-10-16}
  - {cmd: "bandit -q evidence.py", result: "no findings", date: 2026-10-16}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q tests/ (env -i)", result: "5159 passed, 66 skipped, 2 failed: the same root-only unwritable-mount cases as L06-02 (green in CI)", date: 2026-10-16}
authz_checked: "No access change. evidence.py reads nothing itself; beliefs_bundle calls belief_store.beliefs_about under the caller's own RLS context and filters nothing (no duplicated RLS predicate, C3.3). `audience` names the record whose RLS decided visibility."
blockers:
  - "No consumer yet: nothing sends an EvidenceBundle to a model or a page. The context compiler (T06.03) is the first consumer; until then this is a contract with tests, not a shipped behaviour."
  - "Mapping of belief grades to C6 classes is a judgement, recorded in evidence._BELIEF_CLASS: confirmed -> fact only with a citable ref and never from a model; reported -> provisional (said, not verified); inference -> inferred; hypothesis -> provisional. Nothing maps to `predicted` yet (no forecast store is adapted). Owner/05 to confirm."
  - "Only the beliefs store is adapted. Search hits, intent_states (declared/observed) and outcome_memory still have their own shapes (T06.04 continues)."
  - "Sensitivity reuses retention_policy.RetentionCategory; `authorized audience` is the RLS-deciding record, not a role list."
audit_refs: [A02]
decision_refs: []
next_task: "T06.03 context compiler (first consumer of EvidenceBundle), then AC06.03 real-PostgreSQL test that no forbidden record reaches compiled context."
updated: 2026-10-16
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
  - {type: decided, note: "Owner 2026-10-14: semantic frame ON by default; Platform Use Policy v7 (neoh-platform-use-policy-2026-10-14-v7) s4 sentence approved as drafted (POLICY_COVERAGE row -> frame_constraints.py). Launch languages: English + Spanish (Spanish slice L05-04)."}
  - {type: test, note: "AC07.02 asks for a Playwright check; not run (no browser session here). Unit and component tests cover the four surfaces; a real-browser look at Personal AI → 'What Neoh can do in chat' on desktop and phone is still owed."}
  - {type: owner, note: "archive_client: the persona said 'Never delete or archive data' while archive_client was an offered, undoable edit. Resolved toward the tool surface ('Never delete data. Archive a client only when explicitly asked'); two tests that pinned the old sentence now pin the new one. Owner to confirm, or say to take archive_client off the offer instead."}
audit_refs: [A09]
decision_refs: [D-2026-10-12-01]
next_task: "T07.02 done in L07-02; T07.04 done in L07-03 (the nine owner-only tools are no longer offered to agents); owner: archive_client."
notes: "Over-claims found at 3bcab84 and fixed: (1) backend/NEOH_SYSTEM_KNOWLEDGE.md (in every long system prompt) claimed MLS 'via real-time feed' and MLS-sourced listing search, CourtListener, Census, Regrid, county assessor, GovInfo, BatchLeads/PropStream firehose, state underwriting models, owner history/tax records, 'PII columns encrypted via pgcrypto AES-256' (false per A01), SES/Twilio/Google delivery; rewritten as a page guide that claims no capability. (2) neoh_persona AUTHORITY 'Never delete or archive data' contradicted offered archive_client. (3) ai_chat_agent._VOICE_PROMPT told the phone model to promise lookups and 'Let me connect you … [CONNECT]' / '[END]'; nothing parses either marker and the voice path has no tools. (4) ai_chat_agent._CODEBASE_MAP (codebase_summary) presented MLS and migrations '0001-0041'. (5) 36 offered tool descriptions/schemas claimed more than their handlers: get_tenant_health (API latency, pool, cache), list_integration_status (MLS/SES/Stripe/Foundry/Regrid health), list_providers ('connected'), get_team_pipeline ('all agents'), search_listings + get_days_on_market (MLS), search_clients (stage), update_client (email/phone always refused), score_client_lead (reason ignored), estimate_arv (beds/baths ignored), list_contract_templates (filters ignored), get_audit_trail (limit ignored), get_deal_detail (contract, notes), track_deadlines (assignment), get_agent_performance (margin), check_contract_deadlines (contingencies), get_client_contact_history (meeting notes), create_deal_note (deal_id is a transaction id), calculate_mao (holding costs, floor), list_closing_checklist, get_transaction_workflow, get_job_queue, list_property_photos, suggest_client_matches; owner-only tools never said so; selected-record-only edits did not say so. (6) UI: OurAITab chat chips asked for things no tool does (approval-queue summary, multi-lead outreach sequences, cross-thread conversation review, equity reviews, public-property evidence, automation gaps) — now honest prompts or navigation; 'Social Ads … dynamic MLS'; 'record-changing actions still require review' (they apply with Undo); ProductTour 'MLS listings', 'authorized IDX sources', 'Everything Neoh can do'; PersonalAITab showed 'Public-record research: Enabled' / 'Draft preparation: Enabled' for preferences nothing reads, 'Research … can run autonomously', 'Accounts connected' for saved credentials. Guard: tests/test_ai_capabilities.py (unbacked_claims over persona layers, product knowledge, phone prompt, codebase map, every offered tool's description and parameters, usable area summaries; registry = allowlist; API per role; snapshot current) and oracle-app/src/lib/assistantCapabilities.test.js (every OurAITab chat chip, assistant copy files, MLS/IDX/RESO in ProductTour and OurAITab). Swept and NOT changed (other owners): SitePreview.jsx:212 IDX intake copy and BrokerageSetupPanel MLS setup row (14 / 11); CommsTab.jsx:307 'Queued — AI sends' for a human-composed email (10); AssistantMessages 'Anything that reaches a client waits for your approval' and OurAITab hero vs mission autopilot that releases its own approvals (missions/executor.py _release; 08); BrokerageOnboardingPanel autonomy toggles still offered though unused (03/08). Risk: test_compact_is_substantially_smaller now passes by ~20 characters (compact 3769 vs full/3 3790); a longer capability block will trip it."
updated: 2026-10-13
```

#### L07-02 Versioned ToolCapability registry, derived not restated (T07.02, AC07.01)
```yaml
id: L07-02
status: tests_pass
env: local
evidence_shas: [42f0c0a]
files: [backend/tool_capabilities.py, backend/ai_chat_store.py, backend/tests/test_tool_capabilities.py]
tests_run:
  - {cmd: "read ai_tool_policy.TOOL_RISK, ai_chat_agent.TOOLS/_tool, ai_chat_store._execute_safe_tool, ai_capabilities.tool_state at 42f0c0a", result: "each property a capability needs already had one deciding source, but no per-tool record joined them; the selected-record rule lived as an inline tuple inside _execute_safe_tool", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_tool_capabilities.py tests/test_ai_tool_policy.py tests/test_ai_capabilities.py tests/test_ai_chat.py tests/test_ai_tools_read.py", result: "254 passed (9 new: one entry per catalog tool = TOOL_RISK; purpose and schema hash from the catalog; effect/confirmation/idempotency follow the risk class; offer, owner-only and selected-record match the runtime; every tool in one area; unmeasured fields are None; version moves with any entry)", date: 2026-10-16}
authz_checked: "No access change. needs_selected_record() is the same rule _execute_safe_tool applied inline (moved, not changed); the registry reads the allowlist and owner-only set, it does not decide them."
blockers:
  - "126 tools: 102 read, 13 internal edits (undoable receipt), 11 external effects (approval); 57 offered. Latency, cost, per-tool timeout and verifier are None and listed in `unmeasured`: nothing measures them per tool yet (T07.07 budgets, T07.10 verifiers)."
  - "No endpoint exposes it. Consumed since L07-03 (tool_offer reads owner_only per turn) and L08-03/L08-04 (missions.planner.channel_feasibility reads offered); the planner is its first consumer, the chat offer its second."
  - "Record types per tool are not in the entry: the catalog does not declare them; the `area` (ai_capabilities) is the nearest derived grouping."
audit_refs: [A09]
decision_refs: []
next_task: "T07.04 done in L07-03; owner: archive_client (L07-01)."
updated: 2026-10-16
```

#### L07-03 The model is offered only what this actor can use, and is told why the rest is not (T07.04, AC07.01)
```yaml
id: L07-03
status: tests_pass
env: local
evidence_shas: [893c587]
files: [backend/tool_offer.py, backend/channel_setup.py, backend/commands_api.py, backend/ai_chat_agent.py, backend/ai_chat_store.py, backend/ai_chat_api.py, backend/ai_tools_read.py, backend/tests/test_tool_offer.py, backend/tests/test_tool_offer_live.py, backend/tests/test_channel_setup.py, backend/tests/test_ai_tool_policy.py, backend/tests/test_security_launch_review.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read ai_chat_agent offered_tool_names/_tool_config/_foundry_tools/_local_tools, ai_chat_store.execute_safe_tool, ai_tools_read.OPS_ROLE_TOOLS, handle_ai_chat_job and ai_chat_api's enqueue at 893c587 (five parallel readers, file:line evidence)", result: "the offer was role-blind and channel-blind: an agent was shown the nine broker-owner reads and refused at the handler; draft_sms / draft_email / call_contact / schedule_event were offered whatever the brokerage had connected. The chat worker ran every turn as Role.AGENT (ai_chat_agent.py:1368; the job payload carried no role), so a broker-owner asking for their own workspace report was told 'your role is agent' while /api/ai/chat/capabilities told the same owner it was available", date: 2026-10-16}
  - {cmd: "cd backend && git stash the implementation, keep the tests; pytest tests/test_tool_offer.py tests/test_tool_offer_live.py tests/test_sales_stack.py tests/test_ai_tool_policy.py -k <new tests>", result: "before: test_tool_offer.py fails to collect (no tool_offer); 6 failed (4 channel-gap tests, 2 per-actor offer tests); live test ModuleNotFoundError", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_tool_offer.py tests/test_sales_stack.py tests/test_ai_tool_policy.py tests/test_ai_chat.py tests/test_ai_capabilities.py tests/test_tool_capabilities.py tests/test_ai_tools_read.py tests/test_frame_constraints.py tests/test_semantic_language.py tests/test_neoh_persona.py tests/test_killer_demo_paths.py tests/test_llm_gateway.py tests/test_interactive_job_queue.py tests/test_ai_chat_interrupted_turn.py tests/test_missions_planner.py", result: "1595 passed: an agent's offer = the release offer minus the nine owner-only reads, each with a reason; owner and platform admin unchanged; a channel gap withholds that channel's tool only where the record unlocks it; unknown state ({} / no pool / snapshot error / 3 s timeout) withholds nothing; the prompt note lists every withheld tool and reason; execute_safe_tool refuses a withheld tool with the reason before the dispatcher; the local/gateway payload, the Foundry request and the Bedrock toolConfig drop exactly the withheld names; _generate end to end puts the note in the prompt and remembers the offer for the executor; _worker_role maps broker_owner->broker_owner, platform_admin->broker_owner, agent/None/garbage->agent; the job payload carries ctx.role.value. The 126 / 57 counts, the UI snapshot and the registry version are unchanged", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_tool_offer_live.py tests/test_frame_constraints_live.py tests/test_migration_precheck.py (real PostgreSQL 16, FORCE RLS, app login)", result: "10 passed: a fresh brokerage withholds texting / email / Neoh calls / scheduling with the setup each needs and the owner reads for an agent (not for the owner); the executor refuses draft_sms with 'no registered text number' and command_executions stays empty; a registered text number + a Twilio row validated for sms brings draft_sms back while call_contact stays withheld for voice; a valid Google row brings schedule_event back", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite)", result: "5265 passed, 76 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container)", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . (frontend unchanged)", result: "925 passed; eslint exit 0", date: 2026-10-16}
  - {cmd: "adversarial review of the pushed diff (3bd0b2f): five finders, each finding then judged by two refuters (workflow wf_17a771ee-c82)", result: "three HIGH findings confirmed by reading the executor: the first gap check mirrored the delivery page's Twilio-centric snapshot, not the command paths. (1) SMS: the default rail is Telnyx (messaging_provider.py), whose send needs an active hosted order on a voice-verified business number, never telephony_routes.sms_sender_e164. (2) Voice: the call path fails closed only on a missing verified caller ID, then follows the route's rail — Plivo (the default voice provider; env-only, provider_credentials admits no plivo row) or Twilio with the custom-call fallback. (3) Calendar: the OAuth flow labels the Google row with the connecting user's agent_id, which the owner-view snapshot never reads; and an expired access token with a refresh token is not a disconnected calendar. Also: the email reason claimed 'no mail server' for saved-but-unvalidated rows; the Foundry runtime context cut (12k) could drop the note; the worker trusted the role recorded at enqueue; the prompt asserted 'signed in as agent' for jobs without a recorded role", date: 2026-10-16}
  - {cmd: "livetest.sh python probe at 3bd0b2f (implementation stashed): a Telnyx-ready agent (verified number on Plivo, hosted order active) and an owner with a Google row under their own label", result: "sales_api.channel_setup_gaps at 3bd0b2f returned sms 'no registered text number', voice 'no Twilio voice account' and calendar 'isn't connected' for both — false on a working deployment. Fixed: channel_setup.py mirrors the executor (same reads: get_messaging_route / get_public_business_number / get_verified_caller_id / get_telephony_route, the credential loaders' own row selection via has_provider_credential / has_google_calendar without decrypting, the same env fallbacks); the reasons use the executor's words", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_channel_setup.py tests/test_tool_offer.py tests/test_security_launch_review.py + the suites above", result: "1607 passed, 2 skipped: every channel names what it needs; Telnyx needs an active hosted order on a verified number ('still being set up (processing)' while pending); Twilio SMS needs an account somewhere; calls need a verified caller ID then the route's own rail (Plivo env, Twilio row/env or the custom call URL); email: this agent's credential (own, default, shared) or the platform relay; the calendar gap follows the row the command loads, whoever asks; a failing read leaves that channel out; the loaders and the readers share one SQL each. The turn's role: an active member's live users.role wins (promotion and demotion both), a removed member runs as agent, an unreadable row leaves the recorded role (narrowed); the reason never asserts who the person is", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_tool_offer_live.py tests/test_frame_constraints_live.py tests/test_command_drafts_access_live.py tests/test_migration_precheck.py", result: "12 passed: nothing connected withholds all four channels + the owner reads for an agent; the executor refuses draft_sms with 'business number' and command_executions stays empty; a verified Plivo number with a 'processing' hosted order says so, 'active' brings draft_sms back; call_contact needs the platform Plivo env; a saved unvalidated SMTP row closes the email gap; a Google row under the owner's label closes the owner's calendar gap and not the agent's", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite, after the review fixes)", result: "5275 passed, 76 skipped, 3 failed: the 2 known root-only cases and test_security_launch_review's pin on the credential-fallback predicate, which moved into the shared _PROVIDER_ROW_SQL; the pin now checks that SQL and that both readers use it (63 passed on re-run)", date: 2026-10-16}
  - {cmd: "the review's unverified test-adequacy claims (its verify agents hit the session limit), checked by hand and answered with tests", result: "each provider path is now observed through its real seam: the Bedrock toolConfig (_offered_tool_config, incl. nothing-left -> None, and a full _generate turn with _converse captured), a _foundry_generate turn (tools filtered on every round; the executor keyed on the assistant turn id), the local/gateway payload, and the note leading Foundry's runtime context ahead of the memory block; the eviction test now starts from an empty store and pins oldest-first; test_ai_tool_policy's three-provider test calls the real seams instead of re-implementing the filter. One claim was a defect, fixed: a platform admin has no users row, so the 'missing member -> agent' rule demoted them; the recorded platform_admin role now stands, narrowed to broker_owner", date: 2026-10-17}
authz_checked: "The release offer and the handler role check are untouched; tool_offer only subtracts. The chat turn runs as the requester's CURRENT users.role (record_access.find_member, read as an agent), falling back to the role the API recorded at enqueue only when the row cannot be read, and never wider than broker_owner so the tool loop never reads across tenants under RLS. Channel truth mirrors the executor's own preconditions per actor (this agent's routes and the credential loaders' own row selection, without decrypting). Reason strings carry no number, address or secret. The remembered offer is keyed by (tenant_id, assistant turn id); a turn id is a UUID never reused."
blockers:
  - "The persona's capability block (ai_capabilities.prompt_block) is still the owner view baked at import; an agent's prompt now says the owner reads are withheld later in the same prompt. One per-actor block would be cleaner (prompt 02's one-vocabulary ask)."
  - "channel_setup.gaps costs up to 7 small reads per chat turn (one tenant transaction each); nothing caches it across turns."
  - "A member deactivated or removed between enqueue and claim still gets their turn, as an agent (the pre-existing behaviour); refusing it outright is a product decision."
  - "The review's verify phase had not finished when this was pushed (two CPUs: two agents at a time); its remaining verdicts are the next check-in's work."
  - "No Playwright or real-model run shows the model actually answering 'texting needs setup' from the note; the evidence is the prompt text and the executor refusal."
  - "Mission plans (L08-03) still judge channels by the static registry, not by this per-brokerage gap check."
  - "Not deployed (C4.4)."
audit_refs: [A09]
decision_refs: []
next_task: "Plans read the same gaps since L08-04; one per-actor capability block (02); T07.07 budgets."
updated: 2026-10-16
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
  - {cmd: "T08.01 review of PR #11 (draft, head 0b52fbf, base d9add31) against current code and a database built from every migration (2026-10-15)", result: "Not mergeable as is: (1) its 0129 migration re-creates mission_events_kind_check WITHOUT 'action_deferred' (written since L05-03), so it would re-break deferral journalling (L08-02); (2) it picks the client with lower(cl.assignee_id)=lower(m.created_by), which predates 0131 / D-2026-10-13-01: pool clients (no assignee) and edit-shared clients are refused, and the access rule is restated instead of left to RLS (C3.3); (3) under 0131 an INSERT into client_tasks for a client the owner can only view raises 42501, which _create_internal_task does not catch, so the tick errors instead of blocking the action; (4) it executes a task whenever verdict.may_release, which a standing grant can produce — the open owner question; (5) numbered 0129 although main is at 0159 (migrations README: next number above the max). Sound and worth keeping: the tenant-bound FK + partial unique index for retry safety, FOR UPDATE OF the action, the receipt read-back, no outbound command for a task", date: 2026-10-15}
authz_checked: n/a
blockers: 
  - {type: review, note: "PR #11 (55ac99c, 0b52fbf) is open, not merged."}
  - {type: owner, note: "Does a mission's standing grant count as current human approval (CONTRACT C4.1)?"}
audit_refs: []
decision_refs: []
next_task: "Owner answers the grant question; then rebuild PR #11 on current main per the review below (new migration number, includes action_deferred, RLS decides the client, 42501 -> blocked)."
updated: 2026-10-15
```

#### L08-02 A deferred mission step's journal row was refused by the database
```yaml
id: L08-02
status: tests_pass
env: local
evidence_shas: [33e80e0]
files: [backend/db/migrations/0159_mission_events_action_deferred.sql, backend/tests/test_mission_event_kinds.py, backend/tests/test_mission_deferral_journal_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "pg_get_constraintdef(mission_events_kind_check) on a database built from every migration; grep of the kinds missions_api.py and missions/*.py write", result: "the CHECK (0100) lists 19 kinds without 'action_deferred'; executor._work_one journals 'action_deferred' since L05-03; _journal logs and swallows a refused insert, so the step's due_at moved but its journal row was never stored. Every other written kind is allowed", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_mission_event_kinds.py with 0159 moved aside, then restored", result: "before: failed ('journalled but refused by mission_events_kind_check: [action_deferred]'); after: passed. The test compares every kind the code writes with the newest migration that defines the CHECK", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_mission_deferral_journal_live.py with the pre-0159 constraint restored, then 0159 re-applied", result: "before: failed (journal [] — the row was silently lost); after: 1 passed (['action_deferred'])", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_mission_event_kinds.py tests/test_migration_ledger.py tests/test_run_migrations.py tests/test_mission*.py", result: "157 passed", date: 2026-10-15}
authz_checked: "No access change: one CHECK constraint gains one allowed value."
blockers:
  - "Not deployed (C4.4). Deferral journal rows written before 0159 were never stored; they cannot be recovered (the due_at moves themselves happened)."
audit_refs: []
decision_refs: []
next_task: "See L08-01."
updated: 2026-10-15
```

#### L08-03 Plans are feasible or refused against the tool registry (T08.04, AC08.02)
```yaml
id: L08-03
status: tests_pass
env: local
evidence_shas: [6c44cbe]
files: [backend/missions/planner.py, backend/tests/test_missions_planner.py]
tests_run:
  - {cmd: "read missions/planner.py CHANNELS and missions/executor.py:178 at 6c44cbe", result: "the planner offered task alongside email/sms/voice; the executor skips every task step ('no internal task surface'), so a plan could look feasible and then do less", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_missions_planner.py with missions/planner.py stashed, then restored; + tests/test_mission*.py", result: "before: 4 failed; after: 135 passed, 1 skipped (each channel maps to its registry tool or a reason; a task step is dropped at plan time with that reason and task is not offered to the model; a task-only mission is told why and never asks; a tool missing from the release makes its channel infeasible)", date: 2026-10-16}
authz_checked: "No access change: the planner reads the registry (offered tools); approval and execution paths are unchanged."
blockers:
  - {type: owner, note: "Task steps stay infeasible until PR #11's internal tasks are rebuilt, which waits on the owner's answer about a mission's standing grant (L08-01)."}
  - "The registry maps a channel to the chat tool of the same effect (draft_email, draft_sms, call_contact); the executor stages its own commands. If a deployment disables one path but not the other, the mapping should move to an executor-side capability."
audit_refs: []
decision_refs: []
next_task: "Setup gaps done in L08-04. Owner: standing grant (L08-01); then rebuild PR #11's task steps, which makes `task` feasible."
updated: 2026-10-16
```

#### L08-04 A plan refuses channels this brokerage has not set up, naming the missing integration (T08.04, AC08.02)
```yaml
id: L08-04
status: tests_pass
env: local
evidence_shas: [56e0a77]
files: [backend/missions/planner.py, backend/tests/test_missions_planner.py]
tests_run:
  - {cmd: "read missions/planner.channel_feasibility and propose_plan at 56e0a77", result: "L08-03 judged a channel by the release only (the tool exists and is offered); every brokerage on this release saw email / sms / voice as feasible whatever it had connected, so a plan could be told to text from a brokerage with no business number and fail at the first step", date: 2026-10-16}
  - {cmd: "cd backend && git stash missions/planner.py; pytest tests/test_missions_planner.py -k TestSetupGaps", result: "3 failed, 1 passed (the one pinning that unknown state changes nothing passes by construction)", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite); livetest.sh pytest tests/test_mission_deferral_journal_live.py tests/test_tool_offer_live.py", result: "5280 passed, 76 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container); 2 passed", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_missions_planner.py tests/test_missions*.py tests/test_tool_offer.py", result: "169 passed: channel_feasibility(setup_gaps) names the gap after the release check ('sms steps cannot be carried out for this agent yet: texting isn't set up: connect and finish setting up text messages for your business number before sending.'); an empty gap and no gaps leave the release verdict; a step on an unset channel is dropped with that reason and the channel is not offered to the model; a mission whose only channel is unset is refused before any model call with the setup named in its reasoning; the brokerage's state is read for the mission's own agent (tool_offer.channel_gaps(ctx)); the zero-argument call and every L08-03 test are unchanged", date: 2026-10-16}
authz_checked: "Read-only addition: the plan reads channel_setup through tool_offer.channel_gaps as the mission's agent (the same RLS the executor's step would run under). Refused steps and the whole-mission refusal already reach the mission_events journal (executor._journal 'planned': dropped reasons, reasoning)."
blockers:
  - "The reason reaches the user only through the mission journal; no mission UI surfaces 'dropped' yet (L08-03 blocker)."
  - "Plans are judged at plan time; a channel set up or torn down afterwards is only caught when the step's command fails closed."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "T08.03 TaskGraph (typed steps with schema version); owner: standing grant (L08-01)."
updated: 2026-10-16
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
  - {cmd: "full CI live list incl. tests/test_call_notes_live.py against local real PostgreSQL (2026-10-16, base b733048)", result: "passed (72 passed in the run that added L13-03)", date: 2026-10-16}
authz_checked: frame recipients = requesting agent + client assignee; approve route runs under tenant RLS
blockers: [{type: owner, note: "Deploy approval (C4.4)."}]
audit_refs: [A01]
decision_refs: [D-2026-10-12-03]
next_task: "Deploy approval."
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
next_task: "Done in L09-05 (Twilio + Plivo, Qwen + ElevenLabs). ACS outbound and a real-phone check remain."
updated: 2026-10-15
```

#### L09-05 A spoken "stop" on an outbound AI call is honoured (T09.15, AC09.05)
```yaml
id: L09-05
status: tests_pass
env: local
evidence_shas: [c7b2a6e]
files: [backend/voice_opt_out.py, backend/elevenlabs_realtime.py, backend/qwen_omni_realtime.py, backend/commands_api.py, backend/tests/test_elevenlabs_realtime.py, backend/tests/test_outbound_voice_opt_out_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read elevenlabs_realtime._on_transcript, qwen_omni_realtime.QwenOmniRealtimeBridge._on_transcript_completed, commands_api Plivo/Twilio media handlers, plivo/twilio_call_handler outbound state (at 90a29c6)", result: "confirmed L09-04: outbound caller turns were counted for the turn limit only; nothing matched an opt-out or wrote a suppression, while the disclosure promises 'say stop and I'll end the call and won't contact you again'. Outbound call state carries tenant_id and callee on both carriers; the handlers hang up on the bridges' turn-limit exceptions", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_elevenlabs_realtime.py tests/test_voice_opt_out.py tests/*qwen* tests/*plivo*", result: "162 passed (new: through the real ElevenLabs bridge with fake sockets, 'I'm not interested, stop.' records the suppression for the call's state and raises ElevenLabsCallOptOut, a ElevenLabsCallLimitReached the Plivo handler already hangs up on; 'stop by the house on Tuesday' does nothing and nothing said is kept; an inbound 'stop' is left to the inbound finalizer; a failed suppression still ends the call; the shared Qwen hook (Twilio + Plivo bridges) does the same and skips inbound and ACS)", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_outbound_voice_opt_out_live.py (real PostgreSQL, app role)", result: "1 passed: one outreach_suppression row (channel '*', reason stop_keyword, fixed source text, no words); idempotent; the outreach gate (ConsentLedger.gather_state) then reports suppressed for voice, SMS and email in that brokerage and not in another; a call state without a callee raises OptOutNotRecorded", date: 2026-10-15}
  - {cmd: "every CI rls-job live test; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "68 passed; unit 5206 passed, 65 skipped, 2 failed (test_media_storage + test_object_storage: container runs as root, identical without the change); bandit exit 0", date: 2026-10-15}
authz_checked: "The suppression is written in the platform context of the call's own tenant (as the inbound path does) and only for the number the call dialled; it narrows contact, never widens access."
blockers:
  - "Not proven on a real phone call (T09.18 needs owner approval for a consented call). Speech-to-text quality decides what the matcher sees."
  - "ACS (Azure) bridges carry no call state on QwenOmniRealtimeBridge, so an ACS outbound 'stop' is not handled here; ACS is not the live carrier (L09-01: Plivo)."
  - "The call ends immediately on the opt-out (hang-up); the agent does not speak a goodbye first. The wording 'recorded line' is T09.16 (owner and counsel)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "T09.17 done in L09-06. Then the owner-approved real-call check (T09.18)."
updated: 2026-10-15
```

#### L09-06 Notes from a lead-only AI call are kept or the agent is told (T09.17)
```yaml
id: L09-06
status: tests_pass
env: local
evidence_shas: [73acd60]
files: [backend/commands_api.py, backend/tests/test_call_notes_live.py]
tests_run:
  - {cmd: "read commands_api.deliver_call_notes (at 3969b18)", result: "a consented call with no client_id (chk_call_anchor allows a lead-only call) logged 'call notes not attached — lead-only call' and returned None: the notes the callee agreed to share were dropped and nobody was told", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_call_notes_live.py (real PostgreSQL) with the change stashed, then applied", result: "before: failed ('a lead-only call on a client's house dropped its notes'); after: 1 passed — a lead-only call on a lead whose seller_client_id is a client puts the sealed note on that client (once); a call on a prospect with no client keeps no note and creates one task for the requesting agent ('Call notes were not kept: this lead has no client record', no note content), once across retries", date: 2026-10-15}
  - {cmd: "every CI rls-job live test; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "68 passed; unit 5206 passed, 65 skipped, 2 failed (test_media_storage + test_object_storage: container runs as root, identical without the change); bandit exit 0", date: 2026-10-15}
authz_checked: "Runs in the same platform context as before for the call's own tenant. A house lead's notes reach its seller client, whose audience 0143 already defines (requester + assignee who may still read it, broadcast_record); the not-kept task carries no customer content."
blockers:
  - "Not proven on a real call (T09.18, owner approval)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: ["D-2026-10-12-03 (review/direct note mode applies to house-lead notes as to client notes)"]
next_task: "T09.18 with owner approval."
updated: 2026-10-15
```

#### L09-07 The voice rules are locked on every carrier path and engine (T09.19, AC09.01)
```yaml
id: L09-07
status: tests_pass
env: local
evidence_shas: [b733048]
files: [backend/tests/test_voice_rules.py]
tests_run:
  - {cmd: "read voice_provider (Twilio, Plivo speak_and_stream_markup), commands_api.twilio_webhook, acs_call_handler (answer, outbound start, play-completed), qwen _system_instructions, elevenlabs outbound_instructions, inbound_voice intake at b733048", result: "every path already speaks AI_VOICE_DISCLOSURE before AI audio (ACS answers with an inactive stream and starts it on the greeting's PlayCompleted) and every prompt says never deny/hide automation; no single test held all paths", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_voice_rules.py", result: "8 passed: disclosure wording; Twilio and Plivo markup speak it before the stream; outbound Twilio webhook (Qwen and Gather) opens with it; ACS inbound and outbound play it as the greeting with streaming inactive; Qwen outbound, ElevenLabs outbound and inbound intake prompts forbid denying automation and name the recording", date: 2026-10-16}
  - {cmd: "mutations: drop 'Never deny or obscure' from the Qwen prompt; drop the Plivo <Speak> before <Stream>", result: "each: 1 failed, 7 passed; source restored", date: 2026-10-16}
authz_checked: "n/a: tests only."
blockers:
  - "The turn limit and the spoken opt-out on each engine are pinned in test_elevenlabs_realtime.py / test_outbound_voice_opt_out_live.py (L09-05); 'stores no outbound transcript without consent' is pinned by the note-consent tests (L09-05/06). ACS outbound opt-out remains open (L09-05)."
  - "'Recorded line' truth per engine (T09.16) is an owner/counsel question; this test pins the wording, not its truth."
  - "No real call (T09.18)."
audit_refs: []
decision_refs: []
next_task: "T09.18 real consented call with owner approval; T09.10 per-agent / per-brokerage concurrency bounds."
updated: 2026-10-16
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
next_task: "Fixed locally in L10-11 (2026-10-16)."
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

#### L10-11 Email reads Ready only after a real send (0164; T10.09, AC10.05) — fixes L10-08
```yaml
id: L10-11
status: tests_pass
env: local
evidence_shas: [0650144]
files: [backend/db/migrations/0164_provider_health_platform_smtp.sql, backend/provider_health.py, backend/command_providers.py, backend/sales_api.py, backend/tests/test_sales_stack.py, oracle-app/src/components/ProviderDeliveryPage.jsx, oracle-app/src/lib/emailChannel.js, oracle-app/src/lib/emailChannel.test.js]
tests_run:
  - {cmd: "read sales_api._provider_snapshot and providers() at 0650144", result: "L10-08 still true: env SMTP settings alone made channels.email true; /api/sales/providers also never returned platform_sender, so the page's 'who sends' line was always empty", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_sales_stack.py with sales_api.py, command_providers.py and provider_health.py stashed, then restored", result: "before: 6 failed; after: 15 passed (env relay not Ready until a recorded successful send; failing after a failure; not_configured without settings; a tenant's own mail server never records for the platform relay; a configuration error records credential_rejected)", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_command*.py tests/test_smtp*.py tests/test_provider*.py tests/test_component_health.py tests/test_avm*.py; live: test_component_health_live.py, test_migration_precheck.py", result: "80 passed, 2 skipped; 9 passed", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "922 passed (emailLabel: Ready / Not yet verified by a send / Last send failed / Not set up); eslint exit 0; within budget", date: 2026-10-16}
authz_checked: "provider_health is platform-global (no tenant data: a provider name, timestamps, a failure kind, a counter). The relay outcome is written only for sends that used the platform relay (no tenant credential), so one tenant's mail server never vouches for it; an uncertain send records nothing."
blockers:
  - "Every staging/production tenant reads 'Not yet verified by a send' until the relay sends once after deploy; that is the honest state. An operator test send (L10-01) would verify it."
  - "SMS and calls read Ready from an env flag (ORACLE_TWILIO_CREDENTIALS_VALIDATED); fixed locally in L10-12 (0165)."
  - "No Playwright check of the delivery page (AC10.05 asks for one)."
  - "Not deployed (C4.4): 0164 is local."
audit_refs: []
decision_refs: []
next_task: "SMS / calls done in L10-12; remaining T10.09: retries / circuit-breaker UI and a Playwright check of the delivery page."
updated: 2026-10-16
```

#### L10-12 Platform texts and calls read Ready only after a real send (0165; T10.09, AC10.05)
```yaml
id: L10-12
status: tests_pass
env: local
evidence_shas: [d15aacc]
files: [backend/db/migrations/0165_provider_health_platform_twilio.sql, backend/provider_health.py, backend/command_providers.py, backend/sales_api.py, backend/tests/test_sales_stack.py, backend/tests/test_component_health_live.py, backend/tests/test_env_documentation.py, backend/tests/test_property_source_catalog.py, oracle-app/src/lib/emailChannel.js, oracle-app/src/lib/emailChannel.test.js, oracle-app/src/components/ProviderDeliveryPage.jsx]
tests_run:
  - {cmd: "read sales_api._provider_snapshot at d15aacc; git grep ORACLE_TWILIO_CREDENTIALS_VALIDATED", result: "channels.sms / ai_call / agent_call were true from env Twilio credentials plus ORACLE_TWILIO_CREDENTIALS_VALIDATED=true; nothing in the repository ever sets or checks that flag, so it was an operator's say-so", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_sales_stack.py (new tests on d15aacc code, then with the change)", result: "before: 7 failed (Ready with no recorded send; no platform_channel_states; send_twilio_sms / place_twilio_call recorded nothing). after: 22 passed: not_yet_verified / verified_by_send / failing for texts and calls; a proven SMS account does not make calls Ready; a route without a registered sender still cannot text; a brokerage's own Twilio account never records for the platform; 401/20003 records credential_rejected; recipient refusals (21211, 21610) and a 503 record nothing", date: 2026-10-16}
  - {cmd: "livetest.sh pytest tests/test_component_health_live.py tests/test_migration_precheck.py (real PostgreSQL 16, app login)", result: "10 passed; with the 0164 CHECK restored the new test fails (1 failed): the rows could not be written before 0165", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/ (full unit suite)", result: "5234 passed, 73 skipped, 2 failed (the known root-only test_media_storage / test_object_storage cases of this container)", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "923 passed (channelLabel for the Text messages and Neoh calls cards); eslint exit 0; within budget", date: 2026-10-16}
authz_checked: "provider_health is platform-global (a provider name, timestamps, a fixed failure kind, a counter; no number, body or error text). An outcome is written only when the request used the platform account (no tenant account_sid), so one brokerage's Twilio account never vouches for the platform's; agent_call additionally keeps its verified caller ID and TwiML app requirements."
blockers:
  - "Every tenant on the platform account reads 'Not yet verified by a send' for texts and calls until one approved text and one approved call go out after deploy; that is the honest state, and it needs a real send (T10.12 / T10.15-style operator test to the operator's own number)."
  - "A successful outbound call proves the account and number, not the browser-calling TwiML app: agent_call still also needs TWILIO_TWIML_APP_SID and a verified caller ID; the browser-call path itself records no outcome."
  - "Not deployed (C4.4): 0165 is local."
audit_refs: []
decision_refs: []
next_task: "T10.09 remainder: retry / circuit-breaker UI; AC10.05 Playwright check of the delivery page."
updated: 2026-10-16
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

#### L11-04 Source inventory with code-backed licences and distinct identifiers (T11.02)
Inventory: [docs/property-source-inventory.md](../property-source-inventory.md) (generated).
```yaml
id: L11-04
status: tests_pass
env: local
evidence_shas: [e8c98bb]
files: [backend/property_source_catalog.py, backend/tests/test_property_source_catalog.py, docs/property-source-inventory.md]
tests_run:
  - {cmd: "read-only sweep of every non-MLS property/market source (Explore) + spot checks of harvesters/base.py:1180-1196, market_research_agent.py:432, data_coverage.py:69/131/155 at e8c98bb", result: "no single registry: 51 parcel harvesters (firehose.REGISTRY), 2 violation feeds, county assessor (off), RentCast/ATTOM, FRED/BLS/Treasury, 5 market-research datasets, geocoders, Census, Overpass/FEMA/Walk Score, OpenFEMA, EPA, FBI, Eviction Lab, CourtListener, Regrid, footprints, NCES, imagery, broker listings and media", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_property_source_catalog.py", result: "6 passed: every REGISTRY harvester, market dataset and valuation provider is catalogued; ids unique, kinds and statuses valid; a stated licence cites where; base-default harvesters are default_unverified and attribution strings are never a licence; no MLS/IDX/RESO source; the committed doc equals the generator", date: 2026-10-16}
authz_checked: "n/a: an inventory module and doc; nothing runtime reads it yet."
blockers:
  - {type: owner, note: "68 of 82 sources have no verified licence/terms in the repo: 54 harvesters rely on the code's unverified 'municipal-open-data' default (outreach use already off); 14 others have nothing recorded, incl. the 5 market-research files (published research CSVs, not page scraping) and FRED/BLS/Overpass/FEMA NFHL/Walk Score. Owner/legal to supply per source (T11.04 depends on it)."}
  - "Freshness is the configured cadence/TTL from code, not observed age; observed freshness per source is health (L19-04/05)."
  - "Read-only tables with no writer found (fema_flood_zones, county_market_stats, parcel_zoning, school_districts) are read by routes_market.py: a T11.04/F11.03 follow-up."
audit_refs: [A02]
decision_refs: [D-2026-10-12-01]
next_task: "Owner/legal: licences per source; then T11.03 property identity with history."
updated: 2026-10-16
```

#### L11-05 Heuristic valuation targets are labelled as such and cannot be promoted as a valuation (T11.07, audit A08)
```yaml
id: L11-05
status: tests_pass
env: local
evidence_shas: [1a8a7e3]
files: [backend/ml_forge/edge_forge/train_lora.py, backend/ml_forge/cot_synthesizer.py, backend/ml_forge/synthetic_lawyer.py, backend/models_api.py, backend/tests/test_valuation_honesty.py, backend/tests/test_valuation_honesty_live.py, backend/tests/test_model_registry_lifecycle.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "re-verify A08 at 1a8a7e3: read train_lora.py, cot_synthesizer.py, synthetic_lawyer.py, local_vllm_adapter.py, models_api.py, model_training.py, PersonalAITab.jsx; grep every caller of EdgeUnderwriter / train_lora / the county constants", result: "A08 still open and wider than the audit said. train_lora.py:207/209 still compute ARV from a fixed county $/sqft table and the 70% rule on synthetic contracts, and main() labelled that dataset kind 'licensed_state_underwriting' with a model card whose intended use was 'tone and formatting preferences only'. cot_synthesizer.py:23-29 called its own county table 'based on median sale prices' (no source) and stamped every rule-computed sample _meta.model 'meta.llama3-3-70b-instruct-v1:0'; synthetic_lawyer.py recorded the primary model even when the fallback wrote the record. models_api.activate_model promoted a state_lora underwriter on a self-attested pass flag with no per-region error and no target provenance. EdgeUnderwriter has no production caller and no inference path reads model_registry yet (test_model_registry_lifecycle pins that), so nothing serves these numbers today; the Personal AI tab can activate a model and shows a 409 detail as written (errorMessages.js:109)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_valuation_honesty.py (implementation not yet written); git stash models_api.py && livetest.sh pytest tests/test_valuation_honesty_live.py", result: "19 failed, 1 passed (test_a_style_adapter_is_not_held_to_the_valuation_gate; corrected by the L11-06 review): no target label, no per-kind card, the CoT sample claimed a model, and a state underwriter with heuristic, missing or non-JSON provenance, or with no / partial / non-numeric per-region metrics, activated. Live: a heuristic DE underwriter activated (DID NOT RAISE)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_valuation_honesty.py tests/test_model_registry_lifecycle.py tests/test_synthetic_lawyer.py tests/test_platform_completion.py", result: "57 passed: the underwriting manifest is kind synthetic_heuristic_underwriting with targets.provenance heuristic, validated_against_transactions false and the exact constants (recomputing one target from the manifest's own numbers matches _underwrite); its card says 'not a valuation', forbids presenting ARV / rehab / MAO / verdict as an appraisal, automated valuation, market value or verified deal price, and says no per-region error has been measured; the style card is unchanged; CoT samples say generator rules:cot_synthesizer, targets heuristic, no model; activation of a state_lora is refused (409) unless its card's targets are observed_transactions AND a passed, reviewed evaluation publishes per-region n, error and uncertainty (finite numbers, n > 0, every region); a style adapter is not held to it. The lifecycle fixture now carries an observed card and per-region metrics so its gate tests still exercise the same behaviour", date: 2026-10-17}
  - {cmd: "livetest.sh pytest tests/test_valuation_honesty_live.py (real PostgreSQL 16, FORCE RLS, app login, real jsonb)", result: "1 passed: of two validated DE underwriters with the same passed per-region evaluation, the heuristic one is refused and stays validated; the observed one activates", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py", result: "5309 passed, 78 skipped, 45 deselected (the container's root-only storage cases)", date: 2026-10-17}
authz_checked: "Unchanged: activation still requires BROKER_OWNER, the LOCAL_MODELS feature and the existing passed + leakage + geographic-bias gate, all in the caller's tenant transaction. The new check only adds refusals."
blockers:
  - "Provenance and per-region metrics are declared, not verified: a broker owner registering a model or recording an evaluation can still assert observed_transactions and invent numbers. Verifying them needs licensed, time-bounded transaction data and an evaluation run the platform performs itself (T11.07)."
  - "Only state_lora is treated as a valuation model; the scoring and forecast kinds have no evidence of producing valuations today and are not gated."
  - "The heuristic constants still exist (they are the only targets the trainer has); they are labelled, not replaced. No observed-transaction training set exists."
  - "Other ARV paths (distress_valuation, intelligence_engine comps, cma_generator, agent_analyst) are comp- or input-driven and were not part of A08; their UX wording is T11.07 / T11.08 work."
  - "Not deployed (C4.4)."
audit_refs: [A08]
decision_refs: []
next_task: "Review fixes in L11-06; then T11.03 property identity with history."
updated: 2026-10-17
```

#### L11-06 Review fixes for L11-05: a decided version cannot be swapped, heuristic state adapters are never served, the trained text claims no market source (T11.07, audit A08)
```yaml
id: L11-06
status: tests_pass
env: local
evidence_shas: [657777f]
files: [backend/models_api.py, backend/ml_forge/edge_forge/train_lora.py, backend/ml_forge/edge_forge/local_vllm_adapter.py, backend/ml_forge/cot_synthesizer.py, backend/ml_forge/synthetic_lawyer.py, training_data/README.md, backend/tests/test_valuation_honesty.py, backend/tests/test_valuation_honesty_live.py, backend/tests/test_platform_completion.py]
tests_run:
  - {cmd: "adversarial review workflow wf_c6d2a748-5d5 over 657777f (a bypass reviewer and a truth reviewer; one skeptic per top-4 finding; the other 14 checked by hand against the code)", result: "confirmed: (1) register_model's and _training_job's ON CONFLICT upserts replaced the artifact and card of a validated, active or fallback state_lora without resetting status or re-running the gate (an active observed underwriter could become a heuristic artifact in place; a heuristic one could inherit an evaluation it never had); (2) cot_synthesizer still wrote 'County median ARV/sqft' and county market characterisations into the completion a model learns; (3) the A08 row overstated the fix — the six tracked training_data/*.jsonl files predate it and still say 'County median' (all 600 rows) and swarm_textbook.jsonl still names a model that never wrote its rows. Refuted: rollback_model re-activating a heuristic predecessor (its target is only ever the model activate_model just demoted). Also real, fixed here: underwriting_targets omitted the age thresholds, reference year and the default age; --data with pre-built chat rows was labelled with this trainer's rules; the per-region check took negative errors, fractional or overflowing counts, and a malformed card raised 500 instead of 409; the gate keyed on model_kind only, so the heuristic card activated as 'forecast'; EdgeUnderwriter auto-mounted whatever adapter train_lora left in adapters/<state> as that state's underwriter; the reviewed-evaluation predicate and the every-region rule had no test that would fail without them; synthetic_lawyer's docstring named a fallback model it does not use; the L11-05 red-run survivor was mislabelled", date: 2026-10-17}
  - {cmd: "git stash the implementation, keep the tests; cd backend && pytest tests/test_valuation_honesty.py tests/test_platform_completion.py; livetest.sh pytest tests/test_valuation_honesty_live.py", result: "22 failed, 36 passed: re-registering a validated / canary / active / fallback / retired version succeeded; a training run with a fixed version overwrote a decided row; negative / fractional / overflowing per-region values and string or list datasets were not refused with a 409; a heuristic card activated as forecast, scoring or base; the default age and pre-built rows were misdescribed; the CoT text said 'County median'; EdgeUnderwriter mounted a state adapter with no, heuristic, unrecorded or unreadable card. Live: re-registering the active version with a heuristic artifact DID NOT RAISE. The passes are pins of behaviour L11-05 already had (every-region all(), the fallback model name, the style-card gate)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_valuation_honesty.py tests/test_model_registry_lifecycle.py tests/test_synthetic_lawyer.py tests/test_platform_completion.py", result: "88 passed: an upsert only rewrites a candidate or rejected version (409 'register the new artifact or card as a new version' otherwise; a training run that hits a decided version fails and records why, success stays one transaction); the gate applies to state_lora and to any card that declares training targets; per-region needs every region with a whole n >= 1 and non-negative finite error and uncertainty, and a malformed card or metrics is a 409 with the reason; the fake only returns metrics to a query asking for passed AND both reviews, so dropping either predicate fails the test; the manifest reports the reference year, both age thresholds and the default age, and labels pre-built chat rows 'unrecorded'; CoT samples say 'unsourced, not a market median', drop the county market lines and mark inputs synthetic; EdgeUnderwriter mounts a state adapter only with a model-card.json declaring observed_transactions (skipped and named in skipped_adapters otherwise; an explicit register raises UnverifiedTargetsError), and train_lora writes that card into the adapter directory", date: 2026-10-17}
  - {cmd: "livetest.sh pytest tests/test_valuation_honesty_live.py (real PostgreSQL 16, FORCE RLS, app login)", result: "1 passed: heuristic underwriter refused, observed one active; re-registering the active version with a heuristic artifact is a 409 and the row keeps its status, artifact and card; a per-region report on a passed but unreviewed evaluation does not count beside a reviewed pass without one", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py", result: "5340 passed, 78 skipped, 45 deselected (the container's root-only storage cases)", date: 2026-10-17}
authz_checked: "Unchanged roles and tenancy: register / evaluate / activate still require BROKER_OWNER and LOCAL_MODELS inside the caller's tenant transaction. The new checks only refuse; nothing new is readable."
blockers:
  - "The six tracked training_data/*.jsonl files are unchanged: they still carry 'County median' and (swarm_textbook.jsonl) a wrong model name. training_data/README.md says so; deleting or regenerating them is an owner decision (scripts/forge_model.py still points at the Bedrock copy in S3)."
  - "Evaluations are bound to a model_id, not an artifact checksum; the version guard makes that sufficient for now (a decided version can no longer change artifact), but binding model_evaluations to artifact_sha256 needs a migration on an append-only table."
  - "Provenance and per-region numbers remain self-declared (see L11-05)."
  - "Not deployed (C4.4)."
audit_refs: [A08]
decision_refs: []
next_task: "T11.03 property identity with history: the F11.04 / AC11.04 slices scoped in wf_c6d2a748-5d5 (AI subject resolution, possible-match labels, Property View auto-attach)."
updated: 2026-10-17
```

#### L11-07 Houses: a partial public-record match is labelled "Possible match"; an exact label needs the words to line up (T11.03, F11.04, AC11.04)
```yaml
id: L11-07
status: tests_pass
env: local
evidence_shas: [24a7b3c]
files: [backend/mls_portal.py, backend/tests/test_mls_pipeline.py, oracle-app/src/components/HouseWorkspace.jsx, oracle-app/src/components/HouseWorkspace.test.jsx, oracle-app/src/components/HouseSelection.jsx, oracle-app/src/components/HouseSelection.module.css, oracle-app/src/components/HouseSelection.test.jsx, oracle-app/bundle-budget.json]
tests_run:
  - {cmd: "scope workflow wf_c6d2a748-5d5 (three readers) + re-verify at 24a7b3c", result: "the /api/property-records payload carried match_type/match_score but no identity claim; HouseWorkspace showed every hit as 'Public record' with the owner of record as plain fact and a one-click 'Link to client'; HouseSelection auto-opened rows[0], often an owner_partial or address_partial hit; the exact branches compared text with every space and mark removed, so '15 1/2 MAIN ST' was an 'exact' address for '1512 Main St', parcel '123-45' an exact parcel for '12-345', and a query of '#' or 'ñ' made every address-less record an exact address; the detail route called any record opened by id 'record_id_exact'", date: 2026-10-17}
  - {cmd: "red runs (before each fix): pytest tests/test_mls_pipeline.py; npx vitest run HouseWorkspace.test.jsx HouseSelection.test.jsx", result: "slice: 11 failed / 7 passed (KeyError 'identity') and 4 failed / 5 passed ('Unable to find ... Possible match'); review fixes: 13 failed / 23 passed (exact_address != possible_match for '15 1/2' vs '1512', '2 22ND' vs '22 2nd', '1 21ST' vs '12 1st', 'APT 1-2' vs 'Apt 12', parcel '123-45' vs '12-345'; the empty-query exact arg bound; record_id reported exact)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_mls_pipeline.py tests/test_tour_tier_honesty.py tests/test_mls_invariants.py tests/test_mls_overlay_indexable.py tests/test_mls_portal_serialization.py tests/test_mls_refresh_durability.py tests/test_mls_switch.py tests/test_property_lookup_status.py tests/test_search_api.py", result: "124 passed: identity {status, basis, verified:false} on every search and detail row; exact_parcel / exact_address only when the query's words line up with the record's (any script: '5 大街' is not '5'; 'Calle Ñ 5' is not 'CALLE 5'), a PIN typed with dashes still matches its bare-digit record exactly unless the same digits are split two ways in the results ('12345' vs '12-345' and '123-45': both possible); a blank query or blank address is never exact; a record opened by id is not_applicable; among equal scores the word-equal row sorts first (not an argument of the count query)", date: 2026-10-17}
  - {cmd: "scratch script against local PostgreSQL 16 in a rolled-back transaction (mls_pipeline_search with real SQL)", result: "'1512 Main St' returns 1512 MAIN ST (exact_address) before the more recently refreshed 15 1/2 MAIN ST (possible_match); '12-345' exact, '123-45' possible; '12345' both possible; '19-14-209-013-0000' finds 19142090130000 as exact_parcel; '#' returns 0 rows", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check", result: "934 passed; eslint clean; built; budget raised from 4134.6 KB to 4137.1 KB total JS (+2.5 KB; largest chunk unchanged): the possible-match labels, the not-verified badges and the confirm-before-linking note (AC11.04) on top of slack earlier slices had used", date: 2026-10-17}
authz_checked: "No access change: the catalog is the shared allow-listed public-records table read in the caller's tenant transaction; the labels are derived from the match the search already made. Nothing is ever 'verified': no table records a person confirming a parcel match."
blockers:
  - "No confirmation record: verified is always false, exact matches read 'not verified'. Recording a person's confirmation (and what it was based on) needs new schema (T11.03)."
  - "Exact keys compare street or parcel text only, with no city, state or county: one query can still return several exact hits from different jurisdictions."
  - "Linking a possible match is warned about, not blocked; the link endpoint neither records nor checks the identity status. Strings are English-only like the rest of the Houses components."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "Record a person's confirmation of a property match (schema + UI), so 'verified' can mean something."
updated: 2026-10-17
```

#### L11-08 Neoh "find properties for X": owner-name hits are possible matches, never the client's shortlist (T11.03, F11.04)
```yaml
id: L11-08
status: tests_pass
env: local
evidence_shas: [24a7b3c]
files: [backend/neoh_render.py, backend/semantic_language.py, backend/client_ai_automation.py, backend/tests/test_neoh_render.py, backend/tests/test_neoh_intents.py]
tests_run:
  - {cmd: "re-verify at 24a7b3c: neoh_render.properties_for, client_ai_automation._property_candidates, neoh_intents._candidates_for", result: "every candidate the automation stores is an owner-name hit in public records with requires_review: true and match_basis 'exact owner name', yet Neoh said '1 on Sarah Chen's shortlist.' ('1 en la lista de Sarah Chen.'), with an empty option id, no basis and no review flag; the 'exact' name match actually ignores case, spaces, punctuation and letters outside a-z; the count was of the 3 shown, not the up-to-5 stored", date: 2026-10-17}
  - {cmd: "red runs: pytest tests/test_neoh_render.py -k Properties (slice); the review-fix tests on the slice commit", result: "4 failed / 2 passed ('possible match' not in \"1 on sarah chen's shortlist.\"; Spanish 'lista'; id None); then 9 failed / 41 passed (count capped at 3; 'exact' in the basis; href on a review-only record carrying an id; no city/state/ZIP; no intent-level case)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_neoh_render.py tests/test_neoh_intents*.py tests/test_fast_path_language.py tests/test_client_ai_automation.py tests/test_semantic_language.py", result: "1120 passed: review-only hits are spoken and titled as possible matches in English and Spanish, counted over every stored record ('Showing 3 of 4 here'), and at the producer's cap of 5 said as a floor ('At least 5 ... (the lookup stops at 5)'; the renderer's mirror of the cap is pinned to client_ai_automation.PROPERTY_CANDIDATE_LIMIT); the basis is described as an owner-name match ignoring case, spaces, punctuation and letters outside a-z, never 'exact'; an unconfirmed record is never linked whatever ids it carries; each option shows city, state, ZIP and parcel, so condo parcels at one street address differ; an intent-level test drives the real producer through neoh_intents; the English golden file is unchanged (it uses the confirmed-shortlist shape)", date: 2026-10-17}
authz_checked: "Rendering only: the candidates are the ones the automation already stored for a client the caller can open; nothing new is read. No name reaches a log."
blockers:
  - "The producer's own match_basis string still says 'exact owner name' (client_ai_automation); ClientDetailDrawer shows it as stored. Only Neoh's words were corrected."
  - "There is no confirm/link action from Neoh; review-only options stay unlinked until a confirmation record exists (L11-07 next_task)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Say the producer's basis truthfully at the source (client_ai_automation match_basis) and in ClientDetailDrawer."
updated: 2026-10-17
```

#### L11-09 AI property tools: the exact public-record path can match, and a fuzzy subject is labelled a possible match in every answer (T11.03, F11.04)
```yaml
id: L11-09
status: tests_pass
env: local
evidence_shas: [24a7b3c]
files: [backend/ai_tools_read.py, backend/tests/test_ai_tools_read.py]
tests_run:
  - {cmd: "re-verify at 24a7b3c: ai_tools_read._resolve_subject, _get_listing_detail, estimate_rehab / estimate_arv / comps", result: "the state-stripping regex (IGNORECASE) cut '15 Main St, Dover, DE 19901' to '15 Ma', so the exact path never matched; every lookup fell to a substring search with no state filter ordered by recency; '15 Main St' parsed as state ST and '15 Main Ct' as CT; get_listing_detail attached the row as the listing's public_record with no label; estimate_rehab stated a pre-1978 lead-paint disclosure and its sqft basis from whatever row came back", date: 2026-10-17}
  - {cmd: "red runs: pytest tests/test_ai_tools_read.py on the slice, then on the slice commit with the review tests", result: "16 failed / 64 passed (('DE', '15 Ma') != ('DE', '15 Main St'); a suffix read as a state; KeyError 'match'); then 22 new tests failed (space-stripped '12 1st St' == '1 21st St' called exact; rehab facts from a possible match; 'Apt 2B' read as the city; 'Oak Ct 19901' read as CT; a note claiming a city/ZIP disagreed when none was given; no test of the city/ZIP tie-break)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_ai_tools_read.py tests/test_ai_tool*.py", result: "185 passed: only a word-for-word street with an agreeing city or ZIP (and nothing contradicting) is exact_address, verified always false; the word-equal row is preferred before LIMIT 1 so '1 21ST ST' never hides '12 1ST ST'; unit segments stay in the street; a state needs a comma before it; the fallback stays in the parsed state; rehab, ARV and comps word a possible match's year and size as what that record says, naming it; notes name exactly what was missing or disagreed (a record with a city is never said to have none); mixed city-agrees / ZIP-disagrees is a possible match", date: 2026-10-17}
  - {cmd: "scratch script against local PostgreSQL 16 in a rolled-back transaction (_resolve_subject with real SQL)", result: "'12 1st St, Anytown, IA 50001' returns 12 1ST ST (exact_address) although 1 21ST ST was refreshed later; '15 Main St, IA 19901' against a record with a city and no ZIP is a possible match whose note says the record has no ZIP", date: 2026-10-17}
authz_checked: "Read-only tools in the caller's tenant transaction, unchanged; the change is the query's arguments, one ORDER BY term and the labels on what comes back."
blockers:
  - "A city with no comma before it ('15 Main St Dover, DE') stays part of the street; the exact path then misses and the answer is a labelled possible match (it never guesses)."
  - "The fallback search term is still the whole free-text address; comma-bearing input rarely hits search_document."
  - "AI tool payload strings are English (this module does not use semantic_language). Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Feed the possible-match label into the chat answer itself (the model now receives it in the tool payload)."
updated: 2026-10-17
```

#### L11-10 Contracts: neither assignment-contract path names the public-record owner as the Seller (T11.03, F11.04)
```yaml
id: L11-10
status: tests_pass
env: local
evidence_shas: [24a7b3c]
files: [backend/ml_forge/synthetic_lawyer.py, backend/legal_agent.py, backend/workflow_engine.py, backend/tests/test_synthetic_lawyer.py, backend/tests/test_legal_agent_seller.py, backend/tests/test_contract_seller_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "re-verify at 24a7b3c: synthetic_lawyer.fetch_assignment_transaction_for_client / normalize_assignment_payload, legal_agent.generate_legal_package, workflow_engine / real_leads", result: "with no linked seller client the drafted Assignment Contract's Seller fell back to leads.payload.owner_name (crm.py copies the public record's owner there) in both the SQL COALESCE and normalize_assignment_payload; the live LegalMatrix 'Assignment Contract' panel (legal_agent, fed by workflow_engine from real leads) set contract_fields.seller from owner_name", date: 2026-10-17}
  - {cmd: "red runs: pytest tests/test_synthetic_lawyer.py (slice), then the review tests on the slice commit", result: "2 failed / 19 passed ('Public Record Owner' is not None; \"payload->>'owner_name'\" in the seller COALESCE); then the legal_agent seller tests failed (contract_fields.seller == the owner of record) and a respelled owner fallback slipped past the string guard", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_synthetic_lawyer.py tests/test_legal_agent_seller.py tests/test_contract_draft_workspace.py tests/test_contract_template_registry.py tests/test_contract_type_expansion.py tests/test_engine_start_does_not_block.py tests/test_intelligence_platform.py ...", result: "149 passed: with only an owner of record the draft refuses with seller_name missing and never contains the owner's name; a stated seller (linked client or payload.seller_name) still fills it; the legal package's seller is a visible '[SELLER — confirm; ...]' placeholder unless a seller is stated; the guard inspects the seller expression for any owner token and any public_property_records reference, whatever the spelling", date: 2026-10-17}
  - {cmd: "livetest.sh pytest tests/test_contract_seller_live.py (real PostgreSQL 16, FORCE RLS, app login; now in the CI live list)", result: "passed: a lead whose payload carries an owner_name and no seller client yields a transaction with no seller name", date: 2026-10-17}
authz_checked: "No access change: the same tenant-scoped reads; the owner of record is simply no longer used as a party."
blockers:
  - "The owner of record is not shown in the legal package at all (labelled or not); the UNCONFIRMED_SELLER placeholder is English like the rest of legal_agent."
  - "leads.payload.seller_name is still trusted as a stated seller; no backend writer copying owner_name into it was found, but every importer was not audited."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Audit lead importers for anything writing payload.seller_name from public records."
updated: 2026-10-17
```

#### L11-11 Property View: a substring address hit is a possible match, never the auto-selected subject media attaches to (T11.03, F11.04, AC11.04)
```yaml
id: L11-11
status: tests_pass
env: local
evidence_shas: [e83e4d6]
files: [backend/property_view_api.py, backend/tests/test_property_view_resolve_scope.py, backend/tests/test_lead_record_access_live.py, oracle-app/src/components/PropertyViewTab.jsx, oracle-app/src/components/PropertyViewTab.module.css, oracle-app/src/components/PropertyViewTab.test.jsx, oracle-app/bundle-budget.json]
tests_run:
  - {cmd: "re-verify at e83e4d6 (workflow wf_ba5b6dda-f56: worktree implementer -> reviewer -> fixer; the verifier hit the subagent session limit, so the lead verified the fix and ran the live test)", result: "/crm/property-view/resolve matched address ILIKE '%typed%' with no basis; PropertyViewTab auto-selected a single hit, so typing '12 Main St' attached uploads to the lead for '112 Main St'; a resolve failure read as 'No existing record matches'; create_subject keyed new houses on sha256(address) with no state, so the same street in another state returned the other house's lead as 'Matched an existing record'", date: 2026-10-17}
  - {cmd: "red runs: pytest tests/test_property_view_resolve_scope.py; npx vitest run src/components/PropertyViewTab.test.jsx (slice, then review fixes)", result: "slice: 10 of 13 backend failed (KeyError 'identity'; a PA create reused the DE key; created True != False) and 6 of 26 frontend failed ('112 Main St' auto-selected; no 'attach media to a record' picker; resolve failure shown as no match); review fixes: '100 Main St, DE' (a state, no town) was still exact_address and auto-selected, and a picked possible match lost its label while the tour viewer was titled with the typed address (2 backend + 5 frontend failing first)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_property_view_resolve_scope.py tests/test_lead_dossier_address.py tests/test_security_launch_review.py tests/test_media_upload_offload.py tests/test_upload_decoders.py tests/test_video_mock_and_imagery.py tests/test_property_scan_ingest.py tests/test_no_blocking_io_in_async.py tests/test_client_portal*.py", result: "150 passed: every resolved lead and listing carries identity {status, basis, verified:false}; exact_address needs the same words, a town (a ZIP or a city segment) and a state the record does not contradict; everything else is a possible_match; a reused record returns its own address and identity; a key held by a visible lead in another state gets a state-qualified key, while new addresses keep the address-only key so a legacy row hidden by RLS still conflicts (409) instead of gaining a duplicate pool lead", date: 2026-10-17}
  - {cmd: "livetest.sh pytest tests/test_lead_record_access_live.py (real PostgreSQL 16, FORCE RLS, app login)", result: "2 passed (not runnable inside the worktrees; run by the lead): the reuse response carries the row's address and exact_address identity; a PA create for the same street returns created=True with its own lead", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check", result: "954 passed (auto-select only a single exact_address candidate; 'Possible match' and 'Address match · not verified' flags and a confirm-before-attaching note; a picked record keeps its address and flag on an 'Attaching media to' line with 'Change record'; the tour viewer is titled with the record's address; a resolve failure says records couldn't be checked); eslint exit 0; built; within budget after the slice re-pinned it (+1,424 B raw total JS, largest chunk unchanged)", date: 2026-10-17}
authz_checked: "Unchanged scoping: resolve and create still read leads and listings in the caller's tenant transaction under RLS; the identity label is computed from the rows the caller can already see. Nothing is ever verified."
blockers:
  - "A legacy hidden row in another state still answers 409 'already a client's property': RLS hides its state, so the conflict cannot tell the two houses apart."
  - "The resolve still takes the 10 newest substring hits; an exact record can be pushed out (auto-select then does not fire, which is the safe failure)."
  - "The town rule is textual: '100 Main St, Apt 2, DE' counts 'Apt 2' as a town segment. DossierPanel still renders pv: keys as 'Parcel …' (T11.02). jsdom only; no real-phone check."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Show pv: keys as a Property View record, not a parcel (T11.02), and record a person's confirmation of a match (L11-07 next_task)."
updated: 2026-10-17
```

#### L11-12 Client portal: the homeowner's title findings say "Possible match" / "Not verified", released liens say so, and dismissed findings and agent notes stay out (T11.03, F11.04)
```yaml
id: L11-12
status: tests_pass
env: local
evidence_shas: [e83e4d6]
files: [backend/client_portal.py, backend/tests/test_client_portal_title_summary.py, oracle-app/src/components/SecureDossierPage.jsx, oracle-app/src/components/SecureDossierPage.module.css, oracle-app/src/components/SecureDossierPage.test.jsx]
tests_run:
  - {cmd: "re-verify at e83e4d6 (workflow wf_ba5b6dda-f56: implementer -> reviewer -> fixer -> verifier)", result: "SecureDossierPage (read by the homeowner) showed every title finding as a bare type and amount ('Lien $45,000'): a possible_match or unresolved lien read as a fact about their house, a 'matched' status is only the submitter's claim (no route ever writes review_status 'confirmed'), a released lien read as outstanding, and client_portal included findings a reviewer dismissed and passed the agent's free-text notes and chain_gap to the homeowner's payload", date: 2026-10-17}
  - {cmd: "red runs: pytest tests/test_client_portal_title_summary.py; npx vitest run src/components/SecureDossierPage.test.jsx (slice, then review fixes)", result: "1 failed / 3 passed (no dismissed filter; notes in the payload) and 3 failed / 3 passed ('Possible match' missing; matched read as fact; 'lienPossible match' glued); the lead's two added pins fail under their mutations (dropping timeZone 'UTC' shows the day before in America/Los_Angeles; a fail-open review check drops 'Not verified' for a missing or unknown review_status)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_client_portal_title_summary.py tests/test_client_portal_media.py tests/test_intelligence_platform.py", result: "34 passed: the portal query excludes dismissed findings, keeps RLS as the only tenant scope (no duplicated predicate), and returns only finding_type, amount, recorded_at, released_at, match_status and review_status, built key by key, so notes and chain_gap cannot ride along", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run src/components/SecureDossierPage.test.jsx", result: "8 passed: any status other than matched / released reads 'Possible match' (missing and unknown included); matched or released reads 'Not verified' unless review_status is exactly 'confirmed' (missing, null or unknown fail closed); a released lien says 'Released <date>' with the recorded calendar date in any time zone; a note explains possible matches only when one is listed; nothing says 'verified'", date: 2026-10-17}
authz_checked: "The homeowner payload only shrinks: fewer columns and no dismissed rows; the dossier grant and RLS scoping are unchanged."
blockers:
  - "No route records a reviewer confirming or dismissing a title finding, so in practice every matched or released finding reads 'Not verified' and the dismissed filter guards rows written outside the app."
  - "'Released' shows what the submitter's record says; the release itself is not verified (the generic 'Preliminary public-record findings' caveat covers it)."
  - "No live-DB test of the new projection (plain columns from migration 0027, checked through the captured query). Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "A reviewer action that confirms or dismisses a title finding (writes review_status), with audit."
updated: 2026-10-17
```

#### L11-13 A broker owner confirms or dismisses one title finding, with a sealed reason and an audit record (T11.03, F11.04)
```yaml
id: L11-13
status: tests_pass
env: local
evidence_shas: [27475c2]
files: [backend/intelligence_api.py, backend/db/migrations/0167_title_finding_review_reason_sealed.sql, backend/portfolio_api.py, backend/client_portal.py, backend/privacy_data_map.py, backend/privacy_export.py, docs/privacy-data-map.md, backend/tests/test_title_finding_review.py, backend/tests/test_title_finding_review_live.py, backend/tests/test_portfolio_summary.py, oracle-app/src/components/PortfolioTab.jsx, oracle-app/src/components/PortfolioTab.module.css, oracle-app/src/components/PortfolioTab.test.jsx, oracle-app/src/components/SecureDossierPage.jsx, oracle-app/bundle-budget.json, .github/workflows/ci.yml]
tests_run:
  - {cmd: "re-verify at 27475c2 (workflow wf_0181441d-ced: implementer -> reviewer -> fixer -> verifier)", result: "title_findings has carried review_status / reviewed_by / reviewed_at since 0027 and the application role may UPDATE it, but no route wrote them: every matched or released finding read 'Not verified' to the homeowner (L11-12) and the portal's dismissed filter only guarded rows written outside the app", date: 2026-10-17}
  - {cmd: "red runs: pytest tests/test_title_finding_review.py tests/test_portfolio_summary.py; livetest.sh pytest tests/test_title_finding_review_live.py; npx vitest run PortfolioTab.test.jsx (slice, then review fixes)", result: "14 failed (no route), 1 failed (flags carried no finding id), the live test failed on the missing route after its app-login no-bypass check passed, 4 failed (no Confirm / Dismiss); review fixes: the reason was a new plaintext column not in the privacy map, reviewers decided without the evidence (no match status, record or amount on the flag), a platform admin's decision was audited in the admin's tenant, and the 409/404 sentence vanished on reload", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_title_finding_review.py tests/test_portfolio_summary.py tests/test_client_portal_title_summary.py tests/test_migration_ledger.py tests/test_migration_safety.py tests/test_migrations_managed_postgres.py tests/test_privacy_data_map.py tests/test_run_migrations.py tests/test_release_immutability.py tests/test_ci_security_gates.py tests/test_sealed_text.py tests/test_ci_deploy_wiring.py tests/test_spec_drift.py", result: "240 passed: POST /api/intelligence/title/findings/{id}/review takes {decision confirmed|dismissed, reason 8-500}; BROKER_OWNER only (platform admin implicit; an agent gets 403 and nothing is written); one guarded UPDATE ... WHERE id=$1 AND review_status='required' (RLS scopes it, no duplicated tenant predicate), so a second or concurrent decision is a 409 naming the status and an invisible finding a 404; the reason is stored only sealed in review_reason_ciphertext under the finding's own tenant key and the brokerage export opens it; the audit record (USER_STATE_CHANGE, in the finding's tenant) carries only the finding id and the decision; portfolio flags carry the finding id, match status, record and amount", date: 2026-10-17}
  - {cmd: "livetest.sh pytest tests/test_title_finding_review_live.py tests/test_migration_precheck.py (real PostgreSQL 16, FORCE RLS, app login; added to the CI live list)", result: "9 passed: the owner confirms a finding in their tenant; another tenant's finding is a 404 and untouched; a second decision is a 409; through the real read_scoped_dossier path the dismissed finding is gone and the confirmed one reads review_status 'confirmed'; the reason opens only with the finding tenant's key and title_findings has no plaintext reason column; an operator's decision on another tenant is sealed and audited there", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check; Chromium layout probe of the title row at 320 / 375 / 1024 px", result: "962 passed; eslint exit 0; built; budget re-pinned (+3.5 KB total JS for the review form and its copy; largest chunk unchanged). PortfolioTab shows each title flag's evidence and review state, and Confirm / Dismiss with a reason form (what the decision does to the homeowner's dossier) to a broker owner only; on a phone the actions wrap under the finding (evidence column 231 px at 320 px, was 31 px and clipped)", date: 2026-10-17}
authz_checked: "BROKER_OWNER decides (the codebase's convention for brokerage-level evidence review: contract templates, model activation); RLS alone scopes the finding; a platform admin's cross-tenant decision is sealed with and audited in the finding's tenant. The homeowner payload is unchanged except that confirmed findings lose 'Not verified' and dismissed ones disappear."
blockers:
  - "Confirming a possible_match does not change its match_status, so the homeowner still sees 'Possible match' (the form says so). Whether a review may change match_status is an owner decision."
  - "No separation of duties (title_findings has no creator column) and no reopen / undo of a decision."
  - "Migration 0167 is new and unapplied outside the local database; not deployed (C4.4)."
audit_refs: [A01]
decision_refs: []
next_task: "Record a person's confirmation of a property match (leads / public records), using this review shape (L11-07 next_task)."
updated: 2026-10-17
```

#### L11-14 A broker owner confirms, rejects or withdraws that a public record is a linked client's house; Houses reads it as verified (T11.03, F11.04, AC11.04)
```yaml
id: L11-14
status: tests_pass
env: local
evidence_shas: [4d72b3c]
files: [backend/db/migrations/0168_property_match_decisions.sql, backend/property_match_api.py, backend/mls_portal.py, backend/server.py, backend/privacy_data_map.py, backend/privacy_export.py, docs/privacy-data-map.md, backend/tests/test_property_match_decision.py, backend/tests/test_property_match_decisions_live.py, backend/tests/test_mls_pipeline.py, backend/tests/test_tour_tier_honesty.py, oracle-app/src/components/HouseWorkspace.jsx, oracle-app/src/components/HouseWorkspace.test.jsx, oracle-app/src/components/HouseSelection.jsx, oracle-app/src/components/HouseSelection.module.css, oracle-app/src/components/HouseSelection.test.jsx, oracle-app/bundle-budget.json, .github/workflows/ci.yml]
tests_run:
  - {cmd: "design workflow wf_057b333d-6d3 at 4d72b3c (three read-only scouts, two independent designs, a judge)", result: "nothing recorded that a person judged a public record to BE a client's house: the only tie is leads/listings.seller_client_id, written by link_client_house for any client type with no basis, evidence or confirmer, no unlink, and audit only as a mutable client_activities row; Property View's pick is browser state; 'verified' was false everywhere (L11-07..L11-13). Chosen design: an append-only decision chain on the pair (lead L, public record R) paired by exact parcel key, broker owner decides, verified only while the newest decision is confirmed for the client L is still linked to", date: 2026-10-17}
  - {cmd: "red runs (implementer, workflow wf_08575fb7-b90): pytest tests/test_property_match_decision.py tests/test_mls_pipeline.py tests/test_tour_tier_honesty.py tests/test_privacy_data_map.py; npx vitest run HouseWorkspace.test.jsx HouseSelection.test.jsx", result: "collection failed (no property_match_api); 14 failed / 65 passed (_public_record_json had no confirmation input; the lookup returned a bare lead id); the classification guard failed on 0168 alone until the privacy map entry landed; 11 failed / 11 passed (no confirmation line, no decision form, no 'Confirmed house' flag)", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py", result: "5547 passed, 82 skipped, 45 deselected (the container's root-only storage cases)", date: 2026-10-17}
  - {cmd: "migration 0168 applied to local PostgreSQL 16; livetest.sh pytest tests/test_property_match_decisions_live.py tests/test_migration_precheck.py (FORCE RLS, app login; run by the lead, the implementer's sandbox could not start PostgreSQL; added to the CI live list)", result: "10 passed: append-only (UPDATE refused by trigger, app role has SELECT and INSERT only), one first decision per pair and one successor per decision, the decider and time stamped from the session, RLS with the tenant wall plus restrictive read (lead visible) and insert (lead's client editable) rules, the reason opening only with the lead tenant's key", date: 2026-10-17}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check", result: "976 passed; eslint exit 0; built; within budget after the slice re-pinned it (+4.6 KB total JS for the decision form and its copy; largest chunk unchanged)", date: 2026-10-17}
authz_checked: "BROKER_OWNER decides (platform admin implicit), as in L11-13; RLS alone scopes the lead and the decision rows (no duplicated tenant predicate), with a restrictive insert floor of 'can edit the lead's client'; tenant_id and client_id come from the lead, never from the request; the reason is sealed under the lead tenant's key and never logged, audited or returned; the audit record carries ids and the decision only."
blockers:
  - "Reviewed after this commit (workflow wf_80899eb2-f4d); four findings and the verifier's leftover fixed in L11-15."
  - "Owner decisions, defaults taken: broker owner only (vs any client editor, a route-only change); a change of address/owner under the same record id does not reopen a confirmation; the evidence vocabulary; 'rejected' does not unlink and linking is not blocked by an unconfirmed record; pool leads cannot be decided; agents do not see who decided."
  - "Houses and, since L11-16, Neoh's linked-house answer read the decision; Property View, the AI address lookup, the client portal and the contract Seller fallback ask other questions and stay verified false (reasons in L11-16). Houses strings English only."
  - "The per-lead head subquery's cost under FORCE RLS is unmeasured (at most 24 leads per page). Migration 0168 is unapplied outside the local database; not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "Review fixes landed in L11-15; Neoh reads the decision since L11-16."
updated: 2026-10-17
```

#### L11-15 Review fixes for L11-14: a late decision never lands on another house, operators get no decision form, and the activity pulse leaks no record ids (T11.03, F11.04)
```yaml
id: L11-15
status: tests_pass
env: local
evidence_shas: [d551666]
files: [backend/property_match_api.py, backend/mls_portal.py, backend/portfolio_api.py, backend/tests/test_property_match_decision.py, backend/tests/test_property_match_decisions_live.py, backend/tests/test_mls_pipeline.py, backend/tests/test_portfolio_summary.py, oracle-app/src/components/HouseSelection.jsx, oracle-app/src/components/HouseSelection.module.css, oracle-app/src/components/HouseSelection.test.jsx, oracle-app/src/components/HouseWorkspace.jsx, oracle-app/src/components/HouseWorkspace.test.jsx, oracle-app/src/layoutContracts.test.js]
tests_run:
  - {cmd: "review workflow wf_80899eb2-f4d over L11-14 (a security / tenancy reviewer and a correctness / UI reviewer, a fixer, a verifier)", result: "confirmed: (1) a decision that resolved after the user switched houses patched the newly selected house, so the workspace read 'Confirmed as the linked client's house' on a house nobody decided; (2) the decision's audit metadata (lead, client, public record) reached every agent through the portfolio summary's activity pulse, past the 0143 wall that hides the decision; (3) for a platform admin Houses attached the newest lead on the parcel across all tenants and offered the decision form, so a confirmation could land in a brokerage the operator never chose; (4) the reason textarea's font reset beat the 16 px rule, so iOS zoomed on focus. The verifier then found the HTTP audit middleware's own row still naming the lead in its request path (and every staff member's client IP in metadata) on the same pulse", date: 2026-10-17}
  - {cmd: "red runs: HouseSelection / HouseWorkspace / layoutContracts vitests; pytest tests/test_property_match_decision.py tests/test_portfolio_summary.py; livetest.sh pytest tests/test_property_match_decisions_live.py (each before its fix)", result: "a captured onHouseUpdate applied after selecting house 6 gave house 6 verified true and head_id h5; the live pulse carried lead_id and public_record_id; an operator's detail view showed another tenant's lead with confirmable true; the textarea computed 14 px; the pulse returned metadata and target_id, and the middleware-shaped action read 'POST /api/crm/houses/<lead uuid>/record-match'", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/test_property_match_decision.py tests/test_mls_pipeline.py tests/test_tour_tier_honesty.py tests/test_portfolio_summary.py; livetest.sh pytest tests/test_property_match_decisions_live.py", result: "unit suites pass and the live test 2 passed: the decision's audit metadata is decision_id, decision and supersedes_id only; can_decide is broker owner only, so an operator sees decisions but gets no form (the route still admits one acting on an explicit lead); the activity pulse returns event_id, category, action and created_at only, built key by key, with UUIDs and numeric ids in a logged path masked ('POST /api/crm/houses/:id/record-match'), and no client IP", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py; cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check", result: "5552 passed, 82 skipped, 45 deselected (the container's root-only storage cases); 981 passed; eslint exit 0; built; within budget", date: 2026-10-17}
authz_checked: "Narrower than L11-14: the pulse now returns less to every agent (no metadata, no target ids, no ids in paths), the decision form is offered to broker owners only, and a late patch only touches the house it was made for. Route authorisation, RLS and sealing are unchanged."
blockers:
  - "Operators still see, read-only, the newest lead on a parcel across tenants in Houses (the pre-existing lead pick in _lead_ids_for_records); preferring the operator's tenant or labelling the lead's tenant is not done."
  - "Other pages that read audit_ledger metadata were not re-audited in this slice; the pulse was the one page every agent reads it through. The 16 px fix is pinned in jsdom, not checked on a real iPhone. Not deployed (C4.4)."
audit_refs: [A01]
decision_refs: []
next_task: "Neoh reads it since L11-16; owner decisions listed in L11-14."
updated: 2026-10-17
```

#### L11-16 Neoh's "find properties for X" names a client's linked houses and reads a broker owner's decision as Houses does; the client score stops calling a link verified (T11.03, F11.04, AC11.04)
```yaml
id: L11-16
status: tests_pass
env: local
evidence_shas: [11f37b2]
files: [backend/property_match_api.py, backend/mls_portal.py, backend/neoh_intents.py, backend/neoh_render.py, backend/semantic_language.py, backend/property_view_api.py, backend/ai_tools_gated.py, backend/client_ai_automation.py, backend/tests/test_property_match_decision.py, backend/tests/test_neoh_render.py, backend/tests/test_neoh_intents.py, backend/tests/fast_path_language_cases.py, backend/tests/test_property_match_decisions_live.py, backend/tests/test_client_ai_automation.py]
tests_run:
  - {cmd: "scout at 11f37b2 (workflow wf_392a6899-4b3: scout -> implementer -> reviewer -> fixer -> verifier)", result: "Neoh had no read of a client's linked houses: once a lead or listing is linked the producer stores no candidates (client_ai_automation.py:654, :758-762), so 'find properties for X' answered 'Nothing is on X's shortlist yet' about a client whose house is on record, and the L11-14 decision reached Houses only. Property View and the AI address lookup ask whether a record is the address typed, the contract Seller fallback asks who the seller party is (0168 does not claim the relationship kind), and the client portal shows the homeowner the lead itself with no public record: none asks whether R is this client's house, so they stay verified false, now with the reason in code. The client score's factor read 'Verified linked property' for any linked house, decision or not", date: 2026-10-18}
  - {cmd: "red runs at 11f37b2 and before each fix: pytest tests/test_property_match_decision.py tests/test_neoh_render.py tests/test_neoh_intents.py tests/test_fast_path_language.py tests/test_client_ai_automation.py; livetest tests/test_property_match_decisions_live.py", result: "84 failed / 132 passed (no shared head helper, no linked read, 10 end-to-end linked-client cases rendering the empty shortlist); live 1 failed (no _linked_houses_for). Review: a rejection of one record on a parcel was told as the whole house 'marked not their house' while an undecided sibling record sat beside it (6 unit and 1 live failure with the fix reverted). Lead: the score factor label contained 'verified' (1 failed)", date: 2026-10-18}
  - {cmd: "livetest pytest tests/test_property_match_decisions_live.py (real PostgreSQL 16, app login, FORCE RLS; already in the CI live list)", result: "3 passed: each step checked against Houses on the same data: a confirmation reads verified for the owner and an agent; another agent of the brokerage and another brokerage see no decision; a record re-keyed off the parcel, a withdrawal, a rejection with an undecided sibling, a sibling confirmation and a re-link to another client (out_of_date) all read not verified; who decided is never returned", date: 2026-10-18}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py", result: "5872 passed, 108 skipped; no frontend change", date: 2026-10-18}
authz_checked: "One shared head-decision rule (property_match_api.current_heads_sql / heads_by_record / linked_house_confirmation): Houses' statement is byte-identical after whitespace collapse, and Neoh reads the client's own leads under RLS only, no tenant predicate, never selecting decided_by, decided_role, evidence or the sealed reason. A record counts only while its (parcel_id, state) equals the lead's own; a manual listing is shown as linked but can never verify."
blockers:
  - "Neoh reads the client's own leads while Houses picks the newest visible lead per parcel; they differ only if leads_tenant_parcel_key is missing or for a platform admin. Listings stay brokerage-wide (0143), so a direct call for a client the caller cannot see returns that client's listings, never its leads or decisions; Neoh reaches it only after search resolves the client under RLS (pinned live)."
  - "Showings are not counted as linked houses (a buyer linked only by a showing still hears the empty shortlist). The score factor's stored label changes on the client's next reconcile; its code stays verified_property. Query cost at scale unmeasured (local catalog empty)."
  - {type: owner, note: "Owner decisions from L11-14 still open (who may decide, whether a changed address or owner reopens a confirmation, whether rejection unlinks). Not deployed (C4.4)."}
audit_refs: []
decision_refs: []
next_task: "Owner decisions in L11-14; then T11.04 freshness and permitted use per source (reaching comps, Neoh and Houses)."
updated: 2026-10-18
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
blockers: [{type: scope, note: "T12.09 certification state built in L12-04 (derived, nothing can be verified until the T12.06 / T12.04 producers exist). L12-04 found this row's viewer claim held at one of five TourViewer call sites; fixed there."}]
audit_refs: [A03]
decision_refs: [D-2026-10-08-01]
next_task: "Done in L12-04; owner question on publishing unverified spaces (L12-01)."
updated: 2026-10-13
```

#### L12-03 Capture Studio first slice: camera, live hints off the main thread, on-device staging until acknowledged (T12.01, T12.03, T12.05 part)
Integration map: [docs/neoh-space-integration-map.md](../neoh-space-integration-map.md).
```yaml
id: L12-03
status: tests_pass
env: local
evidence_shas: [42f0c0a]
files: [docs/neoh-space-integration-map.md, oracle-app/src/lib/clearPrivateCaches.js, oracle-app/src/lib/clearPrivateCaches.test.js, oracle-app/src/components/CaptureStudio.jsx, oracle-app/src/components/CaptureStudio.module.css, oracle-app/src/components/CaptureStudio.test.jsx, oracle-app/src/components/PropertyViewTab.jsx, oracle-app/src/components/PropertyViewTab.test.jsx, oracle-app/src/lib/capture/frameQuality.js, oracle-app/src/lib/capture/hintScheduler.js, oracle-app/src/lib/capture/qualityWorker.js, oracle-app/src/lib/capture/createQualityWorker.js, oracle-app/src/lib/capture/cameraSupport.js, oracle-app/src/lib/capture/captureStore.js, oracle-app/src/lib/capture/capture.test.js, oracle-app/bundle-budget.json]
tests_run:
  - {cmd: "read-only trace of capture entry, link, camera, upload, storage, worker, publish (spatial-stack-scout) + spot checks of property_view_api.py:918, reconstruction_worker.py:136/:1029, media_storage.py:45 at 42f0c0a", result: "no in-app camera, no live quality check, no resumable or offline upload: a reload lost every capture not yet uploaded; server gate, build, publish and viewer are real (map committed)", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run src/lib/capture src/components/CaptureStudio.test.jsx src/components/PropertyViewTab.test.jsx", result: "12 + 7 + 20 passed: gate thresholds mirrored (512 / 22 / 12 / 1.6); scheduler drops, never queues, and adapts to measured cost; camera errors -> state + sentence (no raw text); unsupported/denied -> 'Choose files instead'; recording staged on device, removed only on acknowledgement; failed upload keeps it and a later visit lists it; no-room -> 'in this page only'; no % and no 'verified'; launched from Property View, not for 360", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "917 passed; eslint exit 0; build ok, CaptureStudio (12 KB) and qualityWorker are separate lazy chunks (main chunk unchanged at 2271.2 KB); total budget re-pinned (+12.7 KB raw for the new lazy chunks)", date: 2026-10-16}
  - {cmd: "inspect dist/assets after the first build", result: "caught: the worker had been inlined as a data: URL with its import unresolved (would fail, and CSP worker-src blocks data:) because it was constructed through a variable; fixed with createQualityWorker (the literal new Worker(new URL(...)) form); rebuilt: qualityWorker-*.js emitted same-origin with frameQuality bundled", date: 2026-10-16}
authz_checked: "No server change. Uploads go through the existing POST /api/crm/property-view/media under the agent's own session and RLS. Staged originals stay in this browser's IndexedDB for this origin, keyed by the property, until acknowledged or discarded by the agent."
blockers:
  - "No real device has run this (AC12.02 open): iPhone Safari and Android Chrome, three connected rooms, scripted interruptions (tab switch, network loss, low storage). jsdom has no camera, MediaRecorder or canvas; the camera and recorder are faked in tests."
  - "Hint parity with the server gate is by construction (same kernel, edge and thresholds), not measured on real frames; luma rounding and thumbnail resampling differ slightly (stated in frameQuality.js)."
  - "Upload is still whole-file per request: no chunking, byte-acknowledged progress, hashes, receipts or duplicate detection (T12.05 remainder needs a server upload-session API). A staged file whose upload is interrupted mid-request is retried whole."
  - "Sign-out now deletes the staged-capture database with the service-worker cache (lib/clearPrivateCaches.js; test fails before, passes after); the studio says signing out discards anything not uploaded. Owner may prefer a warning before sign-out when captures are waiting."
  - "Room segments and doorway marks (T12.06) not started; owner question on publishing unverified spaces (L12-01) still open."
audit_refs: [A03]
decision_refs: [D-2026-10-08-01, D-2026-10-08-02]
next_task: "Server upload sessions (chunked, hashed, idempotent receipts, byte progress); then the AC12.02 real-device protocol; then T12.09/T12.10 (A03)."
updated: 2026-10-16
```

#### L12-04 A 3D space is certified only by fresh, measured evidence; every label reads that state (T12.09, T12.10, AC12.06; A03)
```yaml
id: L12-04
status: tests_pass
env: local
evidence_shas: [22a7f58]
files: [backend/space_certification.py, backend/tour_api.py, backend/recon_quality.py, backend/reconstruction_worker.py, backend/capture_quality.py, backend/space_status.py, backend/client_portal.py, backend/ai_tools_read.py, backend/ai_tools_gated.py, scripts/golden_capture.py, backend/tests/test_space_certification.py, backend/tests/test_space_certification_consumers.py, backend/tests/test_space_certification_live.py, backend/tests/test_recon_quality.py, backend/tests/test_tour_tier_honesty.py, backend/tests/test_neoh_space_pipeline.py, backend/tests/test_capture_pipeline.py, oracle-app/src/components/TourViewer.jsx, oracle-app/src/components/HouseWorkspace.jsx, oracle-app/src/components/PropertyViewTab.jsx, oracle-app/src/components/DossierPanel.jsx, oracle-app/src/components/SecureDossierPage.jsx, oracle-app/src/components/CaptureSessionPanel.jsx, oracle-app/src/components/CaptureSessionPanel.module.css, oracle-app/bundle-budget.json, docs/neoh-space-quality.md, docs/neoh-space.md, docs/neoh-space-integration-map.md, .github/workflows/ci.yml]
tests_run:
  - {cmd: "scout at 22a7f58 (workflow wf_187f02ec-c92: scout -> implementer -> reviewer -> fixer -> verifier; then two more fix + verify rounds, wf_30b956bc-970 and wf_5f58b8d6-079)", result: "no certification state anywhere (git grep -i certif); recon_quality.assess passed an empty report, a report without held-out PSNR/SSIM, inf and out-of-range numbers, and no registration block as not blocked; tour_api.splat_quality read only limitation keys, so [] or ['camera_poses_missing','scale_unknown'] read 'measured' / '3D Walkthrough'; the tier-3 note 'Free-roam the actual home in a photoreal 3D reconstruction' went on every captured splat; four of five TourViewer call sites (HouseWorkspace, PropertyViewTab, DossierPanel, SecureDossierPage) passed no quality label, so L12-02's 'the viewer labels the mode' held for one; the client portal, get_property_tour and share_property_tour dropped the quality state; no build recorded its input set, so a later upload could never invalidate a proof; room connectivity and device tour performance are measured nowhere", date: 2026-10-17}
  - {cmd: "red runs at 22a7f58 and before each review fix: pytest test_space_certification*.py test_recon_quality.py test_tour_tier_honesty.py test_neoh_space_pipeline.py; livetest test_space_certification_live.py; vitest TourViewer / HouseWorkspace / PropertyViewTab / SecureDossierPage / CaptureSessionPanel", result: "collection error (no space_certification); 16 failed / 51 passed; live KeyError 'evidence_fresh'; 10 vitest failed. Review rounds, each red first: AI-generated listing reels and imported street imagery counted as capture input (a reel marked the space stale and fed generated frames into rebuilds); unreadable, undecodable and unsampled (beyond the gate's 900-frame sample) captures counted as read; a one-room or inconsistent room graph passed connectivity; booleans and numeric strings passed as metrics and device frame rates; device runs were not tied to the space they measured; pending or rejected client-link uploads were capture input; the undecodable-capture record was a second best-effort write that could fail open", date: 2026-10-17}
  - {cmd: "livetest pytest tests/test_space_certification_live.py (real PostgreSQL 16, app login, FORCE RLS; added to the CI live list) and the neighbouring live suites", result: "passed: a published space built from its capture reads evidence_fresh true and stays 'incomplete' (no connectivity or device evidence exists); a new captured photo makes it stale ('pending', caveat names it) with no write; pending and rejected client uploads leave it fresh and are not gathered; approving one makes it stale", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py; cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check (integrated head with L18-08)", result: "5812 passed, 107 skipped; 1047 passed; eslint exit 0; built; budget re-pinned for this slice (+1.9 KB total JS: the quality label at five viewer call sites and the capture panel's quality line; largest chunk unchanged)", date: 2026-10-17}
authz_checked: "No new route or table and no migration: the state is derived on read from the newest build's diagnostics under the existing RLS-scoped reads. Operators (platform admin, broker owner) see each check's values and thresholds; the client portal gets the label, caveats and state only, with no check detail or job id; the AI tools get the state and a note forbidding verified / full / professional / photoreal unless verified."
blockers:
  - {type: scope, note: "'verified' is unreachable today and the tests pin that: nothing produces the room graph with expected rooms (T12.06) or real-device tour runs (T12.04 / AC12.03; storage needs its own migration). Every real space reads 'incomplete' or lower."}
  - "Thresholds are the AC12.08 pilot targets (PSNR >= 27, SSIM >= 0.88, LPIPS <= 0.12) and the provisional registration gate, not calibrated on real houses or devices; no real house or phone was run."
  - "Builds no longer reconstruct imported street imagery or AI-generated video (they were never the home); if the owner wants imported exterior frames used, that is a decision. The SPLAT_READY feed still says '3D reconstruction is live' (no quality claim). Job and build views do not read freshness (at best 'incomplete'); reconstruction_jobs.media_id has no index (no EXPLAIN at scale)."
  - {type: owner, note: "Whether an unverified space may be shared with or published to clients is still the L12-01 owner question; share_property_tour now carries the quality label but its behaviour is unchanged. Not deployed (C4.4)."}
audit_refs: [A03]
decision_refs: [D-2026-10-08-01, D-2026-10-08-02]
next_task: "T12.06 room graph with expected rooms and T12.04 device-run evidence (the producers certification reads); server upload sessions (L12-03 next)."
updated: 2026-10-17
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

#### L13-02 A new deal cannot be opened on an MLS listing while MLS is off (D-2026-10-12-01)
```yaml
id: L13-02
status: tests_pass
env: local
evidence_shas: [0d5ce47]
files: [backend/portfolio_api.py, backend/tests/test_transaction_workflow.py]
tests_run:
  - {cmd: "read portfolio_api._property_anchor and create_transaction; grep oracle-app/src for property_source (at 95c4ce5)", result: "POST /transactions accepted property_source='mls' and read oracle_mls_listings to anchor a new deal regardless of mls_switch (L13-01 finding); the only UI caller sends 'pipeline' (DossierPanel)", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_transaction_workflow.py with portfolio_api.py stashed, then restored; + tests/test_mls_switch.py", result: "before: 1 failed (an MLS-sourced deal was created with the switch off); after: 18 passed — with ORACLE_MLS_ENABLED unset the create answers 404 mls_switch.NOT_OFFERED and touches neither oracle_mls_listings nor transactions; with it on the existing MLS path is unchanged", date: 2026-10-15}
  - {cmd: "every CI rls-job live test; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "68 passed; unit 5207 passed, 65 skipped, 2 failed (test_media_storage + test_object_storage: container runs as root, identical without the change); bandit exit 0", date: 2026-10-15}
authz_checked: "No access change; one more route honours the MLS switch."
blockers:
  - "Existing MLS-sourced deals stay readable and filterable (property_source=mls on the list) — nothing is deleted (D-2026-10-12-01)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T13.02 lifecycle events (needs a migration and state machine); T13.14 tenant predicates vs RLS in portfolio_api."
updated: 2026-10-15
```

#### L13-03 Deal status changes are an immutable, database-written trail; illegal moves are refused (0163; T13.02 first slice, AC13.01) — and T13.14 verified
```yaml
id: L13-03
status: tests_pass
env: local
evidence_shas: [3e76d79]
files: [backend/db/migrations/0163_transaction_status_events.sql, backend/portfolio_api.py, backend/privacy_data_map.py, backend/tests/test_transaction_events_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read portfolio_api status writers (accept offer, close, lose, patch) and ai_chat_store deal note at 3e76d79", result: "status moved by plain UPDATEs with per-route from-state checks; no record of when, who or from what; any writer could set any status (CHECK only lists values)", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_transaction_events_live.py with 0163's triggers dropped, then re-applied", result: "before: 1 failed (no events written); after: 1 passed", date: 2026-10-16}
  - {cmd: "test_transaction_events_live.py (real PostgreSQL, request login)", result: "create + active->under_contract->closed write exactly 3 events with from/to/version/actor (a notes edit writes none); closed->active|lost|under_contract and under_contract->active refused with 23514 and nothing written; the real lose route records active->lost; GET /transactions/{id}/events returns the trail; the application can neither INSERT, UPDATE nor DELETE an event (42501) and even the owner role's UPDATE is refused by trigger; an agent cannot read the trail of a colleague's private client's deal (and gets 404 from the route); a deleted deal takes its trail", date: 2026-10-16}
  - {cmd: "full CI live list against the local migrated database", result: "72 passed (incl. privacy lifecycle erasure over the new table)", date: 2026-10-16}
  - {cmd: "found while testing", result: "default privileges (0003) grant oracle_app INSERT/UPDATE/DELETE on every new table, so GRANT SELECT alone left the trail writable; 0163 revokes them explicitly", date: 2026-10-16}
  - {cmd: "T13.14: read app_tenant_floor/ceiling (0141) and every `tenant_id=$n` in portfolio_api (39 sites)", result: "VERIFIED, no change: only a platform admin's wall spans brokerages, so for every agent, owner and brokerage-scope pipeline `tenant_id = ctx.tenant_id` equals the wall and hides no row; for a platform admin it narrows to the context brokerage, which list_transactions documents as intended. Removing it would widen platform-admin reads and writes in an agent-facing API: an owner/17 decision, not a cleanup", date: 2026-10-16}
authz_checked: "transaction_status_events: tenant wall + RESTRICTIVE read following the deal (EXISTS under the caller's RLS), SELECT-only grant, UPDATE refused by trigger for everyone; writes only by the SECURITY DEFINER trigger for the row that changed. Erasure: mapped BUSINESS_CRM / erase (privacy_erase_tenant_batch runs as owner)."
blockers:
  - {type: owner, note: "Whether a contract that falls through reopens the same deal (under_contract -> active). Refused today; nothing in code does it."}
  - "Only the five existing statuses: the canonical 12-stage lifecycle of T13.02 (offer presented, contingencies, inspection, appraisal, financing, closing scheduled, archived) and its legal audit state are not built."
  - "Existing deals get one `backfill` event stating their status at migration time, labelled as such; their earlier history is unknown and not invented."
  - "Not deployed (C4.4): 0163 is local."
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T13.02 remainder (lifecycle stages with contingencies, behind owner input); T13.11 with 11's gating slice."
updated: 2026-10-16
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

#### L14-03 "Live" for websites means a published site (T14.16)
```yaml
id: L14-03
status: tests_pass
env: local
evidence_shas: [9ac4bdb]
files: [oracle-app/src/components/OurAITab.jsx, oracle-app/src/components/OurAITab.test.jsx]
tests_run:
  - {cmd: "read OurAITab.jsx sourceState (at 13bf381)", result: "sourceState(sourceId) returns 'Live' whenever the endpoint answered; the 'Hyperlocal Website' card called it with no condition, so a brokerage with only draft sites (or none) saw 'Live' (L14-01 finding, OurAITab.jsx:338)", date: 2026-10-15}
  - {cmd: "cd oracle-app && npx vitest run src/components/OurAITab.test.jsx with OurAITab.jsx stashed, then restored", result: "before: 1 failed ('is not Live just because the sites endpoint answered'); after: 4 passed — draft-only sites show 'Setup required', a published site shows 'Live'", date: 2026-10-15}
  - {cmd: "cd oracle-app && npx vitest run && npm run lint && npm run build && npm run bundle:check", result: "892 passed; eslint exit 0; build ok; within budget", date: 2026-10-15}
authz_checked: "No access change: a label."
blockers:
  - "'Website Design' still reads Live when the endpoint answers: it describes the builder, which works without a published site. Not checked in a real browser (jsdom only)."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "T14.04 domain verification and rollback; T14.02 the other source badges."
updated: 2026-10-15
```

#### L14-04 New sites and revisions cannot add MLS/IDX; stored revisions untouched (T14.02, AC14.02)
```yaml
id: L14-04
status: tests_pass
env: local
evidence_shas: [fba5bbb]
files: [backend/sites_api.py, backend/tests/test_studio_sites.py, oracle-app/src/components/OurAITab.test.jsx]
tests_run:
  - {cmd: "grep -n -i 'idx|mls/health' OurAITab.jsx StudioTab.jsx sites_api.py at fba5bbb", result: "UI already free of the IDX poll, items and wizard step (StudioTab.jsx:124 sends authorized_idx_sources: []); the API still accepted authorized_idx_sources on SiteCreate / SiteRevisionCreate and idx_requested=true in SiteContent, so any client could add IDX to a new revision", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_studio_sites.py with sites_api.py stashed, then restored", result: "before: 1 failed (IDX sources accepted); after: 15 passed with test_public_sites.py (refused with 422 'not offered' while ORACLE_MLS_ENABLED is off; the legacy gate still decides when switched on)", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run src/components/OurAITab.test.jsx", result: "5 passed: Our AI makes no /api/mls or idx request and shows no IDX item (guard for the earlier UI removal)", date: 2026-10-16}
authz_checked: "No access change. The column and existing revisions are untouched (no migration, no data change); only new input is refused."
blockers:
  - "Row count of hyperlocal_site_revisions before/after (AC14.02) not taken on staging; no data is changed by this slice."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: [D-2026-10-12-01]
next_task: "T14.04 + T14.05 + AC14.04 domain verification and rollback."
updated: 2026-10-16
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
  - {cmd: "re-verified at 2fef2e5: git log -1 0c7fcce; git ls-files | grep -ciE '.riv$|.glb$|.gltf$'; grep -c rive oracle-app/package.json; grep -n 'layoutId=\"neoh-avatar\"' oracle-app/src/neoh/*.jsx", result: "mascot commit 0c7fcce present; 0 assets; 0 rive deps; pill (NeohSurface.jsx:173) and bar (NeohSurfacePanel.jsx:111) share layoutId neoh-avatar; Rive stub still fails over (NeohAvatarRive.jsx:34). The rig is still not started. The 3D-corner and speaking findings are addressed in L15-02", date: 2026-10-14}
authz_checked: n/a
blockers: 
  - {type: owner, note: "Who authors the rig / .riv asset, and under what licence? (evidence now in L15-03)"}
audit_refs: []
decision_refs: []
next_task: "Done in L15-02 (T15.02 + T15.05 + T15.13) and L15-03 (T15.08 evidence). The rig itself waits on the owner (L15-03)."
updated: 2026-10-14
```

#### L15-02 Neoh shows a state only when a validated event supports it; the 3D corner shows the same state
```yaml
id: L15-02
status: tests_pass
env: staging
evidence_shas: [2fef2e5]   # base; the change is the commit that adds oracle-app/src/neoh/animationEvent.js (branch worktree-agent-aa7960aed530bd89b, unpushed)
files: [oracle-app/src/neoh/animationEvent.js, oracle-app/src/neoh/avatarFacts.js, oracle-app/src/neoh/useNeohAvatarState.js, oracle-app/src/neoh/avatarPresence.js,
        oracle-app/src/neoh/cornerPose.js, oracle-app/src/neoh/pageVisibility.js, oracle-app/src/neoh/NeohCorner.jsx, oracle-app/src/neoh/NeohCorner3D.jsx,
        oracle-app/src/neoh/neohModel3d.js, oracle-app/src/neoh/NeohSurface.jsx, oracle-app/src/neoh/NeohAvatar.jsx, oracle-app/src/neoh/avatarModel.js,
        oracle-app/src/neoh/useNeohChannel.js, oracle-app/src/state/oracleReducer.js, oracle-app/src/state/useOracleWebSocket.js,
        oracle-app/src/components/AssistantContext.jsx, oracle-app/src/components/PersonalCommandComposer.jsx, oracle-app/bundle-budget.json]
tests_run:
  - {cmd: "npx vitest run src/neoh/animationEvent.test.js", result: "25 passed (AC15.02): rejects success/speaking/listening/thinking/needs_attention/disconnected/acting from a source that cannot know them, unknown sources/origins/stages, 16 coordinate/pose keys, unknown and nested fields, missing/NaN/string/negative/future timestamps, expired events, over-long success/error/thinking windows, misplaced amplitude/urgency/actionType; 3,000-case fuzz never throws and accepts only well-formed events; out-of-order and tie ordering; an ended stream refuses a late replay; a 30 ms storm never leaves idle; a 2,000-step random storm never shows a state without a live event and stays within 64 streams; barge-in; cancellation is idle with a reason", date: 2026-10-14}
  - {cmd: "npx vitest run src/neoh/useNeohAvatarState.test.jsx src/neoh/avatarFacts.test.js", result: "27 + 10 passed (AC15.03): thinking only for a fresh in-flight turn (stale history, no timestamp, cleared turn and >120 s without activity -> idle; ends with no hold); success only for an applied, not-undone receipt (chat) or the execute receipt (command), clears after SUCCESS_HOLD_MS, never on mount for an old receipt, never for 'analysis complete' or 'queued'; failures show as error with a reason (interrupted, unavailable, timeout, failed, send_refused) for ERROR_HOLD_MS; approval holds while true; listening only while the mic captures; speaking only while playback is flagged, stopping on barge-in; 40 toggles at 20 ms never flicker. Mutation check: removing the receipt and undone checks fails 2 tests", date: 2026-10-14}
  - {cmd: "npx vitest run src/neoh/NeohCorner.test.jsx src/neoh/NeohCorner3D.test.jsx src/neoh/cornerPose.test.js src/neoh/NeohSurface.test.jsx", result: "7 + 3 + 4 + 6 passed (AC15.09): the flat corner shows each of the nine states; it follows the state the surface shares and rests at idle without one; the 3D corner receives the same state for all nine, and the flat fallback after a context loss keeps it; the real NeohCorner3D reports its state and reports chunk failure and no-WebGL; cornerPose wears eyeExpression(state) for all nine; surface + corner rendered together both show thinking for a live turn; the corner stays idle when Neoh is off", date: 2026-10-14}
  - {cmd: "npx vitest run src/components/PersonalCommandComposer.test.jsx src/state/oracleReducer.aiChat.test.js", result: "4 + 4 passed: command statuses stamped (at, seq); only a confirmed execute is a receipt; an abandoned draft clears awaiting_approval on unmount; chat frames keep their local arrival time across the history re-fetch", date: 2026-10-14}
  - {cmd: "npx vitest run (oracle-app, all)", result: "88 files, 837 passed, exit 0; NeohAvatar.test.jsx unchanged and passing (AC15.01: props kept)", date: 2026-10-14}
  - {cmd: "npm run lint; npm run build; npm run bundle:check", result: "lint 0 problems; build ok; budget re-pinned with --update: total +14.1 KB raw / +5.2 KB gzip vs a base build of 2fef2e5 (4088.0 -> 4101.7 KB raw). Entry chunk +0.76 KB raw / +0.22 KB gzip (arrival and status stamps); statechart +9.2 KB raw in the lazy useNeohAvatarState chunk; NeohCorner3D lazy chunk 6,107 -> 7,723 B; largest chunk unchanged (2271.2 KB). Within budget", date: 2026-10-14}
  - {cmd: "headless Chromium 145 (Playwright, SwiftShader software GL), throwaway harness page rendering the real NeohCorner3D for each state", result: "ready, 0 runtime errors; canvas data-state matched all nine; per-state screenshots show a distinct hue and eye pose. 120 rAF intervals per state: p50 16.7-33.3 ms, p95 16.7-100.1 ms. NOT a device measurement (AC15.07 stays open)", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
  - {cmd: "scope of the a7fcaab staging check for this row", result: "Deployed only: the staging smoke above did not exercise this row's behaviour (search-leg failure answer / SemanticFrame / mascot states). Evidence for the behaviour itself is the local tests listed above.", date: 2026-10-14}
authz_checked: n/a (no data access or permission change; presents state the user already receives)
blockers:
  - {type: verification, note: "No device measurements (iPhone Safari, Android Chrome): AC15.07 open. No speaking source exists in the browser (T15.14), so speaking stays unreachable. The backend folds its 90 s turn-deadline timeout into AI_RESPONSE_UNAVAILABLE (ai_chat_agent.py), so the browser cannot yet show 'timeout' for a chat turn; the mapping is ready for a distinct code (category 07)."}
audit_refs: [A02]
decision_refs: []
next_task: "T15.04 layer blending + AC15.04 hero-sequence review; T15.11/AC15.07 device protocol on real phones; T15.10 approvals from command receipts (useCommandReceipts) as an approval_pending source."
updated: 2026-10-14
```

#### L15-03 Rig and delivery spec; Rive vs 3D evidence for the owner
```yaml
id: L15-03
status: blocked   # owner decision
env: local
evidence_shas: [2fef2e5]   # base; doc added in the same commit as L15-02
files: [docs/neoh-avatar-rig-spec.md]
tests_run:
  - {cmd: "npm pack @rive-app/canvas@2.44.2 @rive-app/canvas-lite @rive-app/react-canvas@4.36.0 into an isolated scratch dir (not installed); stat + gzip -9", result: "all MIT. canvas: rive.js 570,043 / 124,659 gz, rive.wasm 2,057,365 / 848,969 gz; canvas-lite: rive.js 489,769 / 109,204, rive.wasm 772,160 / 314,730; react-canvas wrapper 21,951 / 5,959. rive.js defaults its wasm URL to https://unpkg.com/", date: 2026-10-14}
  - {cmd: "grep Content-Security-Policy oracle-app/nginx.conf", result: "script-src has no 'wasm-unsafe-eval' and connect-src has no unpkg.com: Rive's wasm would be refused under the production CSP as configured", date: 2026-10-14}
  - {cmd: "grep deviceTypes node_modules/playcanvas/build/playcanvas/src/platform/graphics/graphics-device-create.js (playcanvas 2.21.3, MIT)", result: "PlayCanvas adds WEBGL2 then NULL: the 3D path needs WebGL2; it is already a dependency", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: owner, note: "Rive (a new ~0.98 MB gzip runtime that needs 'wasm-unsafe-eval' + self-hosted wasm) vs a glTF rig on the existing PlayCanvas engine (no new runtime). The doc recommends the PlayCanvas rig with the SVG fallback everywhere. Who authors the asset, and under what licence?"}
audit_refs: []
decision_refs: []
next_task: "Owner decision; then commission the asset against §2-§3 of docs/neoh-avatar-rig-spec.md."
updated: 2026-10-14
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

#### L16-04 The deal list pages authorized ids by key; the deal room says how much it shows (0162; T16.05, AC16.03)
```yaml
id: L16-04
status: tests_pass
env: local
evidence_shas: [f991182]
files: [backend/portfolio_api.py, backend/db/migrations/0162_transactions_page_index.sql, backend/tests/test_transaction_paging_live.py, backend/tests/test_transaction_workflow.py, oracle-app/src/components/DealBook.jsx, oracle-app/src/components/DealBook.test.jsx, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read portfolio_api.list_transactions and DealBook.jsx at f991182", result: "LIMIT/OFFSET over the full join with clients and offers for every matching deal; DealBook fetched limit=100 and labelled the chip '{total} shown' and the metric 'Total' from the loaded rows, so a brokerage with more than 100 deals was told it saw them all, with no way to load the rest", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_transaction_paging_live.py with idx_transactions_page dropped, then re-created", result: "before: 1 failed (a 4000-deal brokerage sorts every deal for one page); after: 1 passed", date: 2026-10-16}
  - {cmd: "test_transaction_paging_live.py (real PostgreSQL, request login)", result: "owner and agent page every readable deal exactly once in (updated_at, id) order, the agent without the colleague's private client's deals; a deal inserted between pages neither repeats nor shifts the next page; the page query is index-driven under the tenant wall for small and big brokerages, and the big one walks idx_transactions_page with no Sort; a malformed cursor is 422", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run src/components/DealBook.test.jsx with DealBook.jsx stashed, then restored", result: "before: 1 failed ('2 of 4 shown'); after: 2 passed (partial list says 'N of total' and '(of shown)', Show more loads by cursor and disappears at the end; a complete list says '1 shown')", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_transaction_workflow.py; cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs", result: "13 passed (the list-shape test now pins the ids-first two-query shape); 920 passed; eslint exit 0; within budget", date: 2026-10-16}
authz_checked: "Unchanged rules: both queries run in the caller's tenant_tx, so RLS decides the page (proven for an agent who cannot read a colleague's private client's deals). The explicit tenant predicate that narrows a platform admin to the context tenant is kept as before (it narrows, it does not restate)."
blockers:
  - "Found by CI after push (run 38846725668, DealBook.test.jsx:42): `load` depended on the list length, so 'Show more' re-ran the mount effect and refetched page one, wiping the loaded pages (a real browser does it on the next frame). Fixed: `load` has a stable identity (refs), background refreshes skip while extra pages are open, Refresh starts from page one; the test now also dispatches a background refresh and passed 5 runs in a row; the old component fails it."
  - "Keyset semantics: a deal updated while someone pages moves to the top and is not repeated further down; it appears on the next first-page refresh."
  - "`total` is still a count(*) per request; fine at deal volumes, revisit with T16.10 measurements."
  - "Other OFFSET lists remain: operator_api (admin-only), state_compliance/routes_mls and mls_portal (MLS, gated off, D-2026-10-12-01)."
  - "Not deployed (C4.4): 0162 is local."
audit_refs: [A10]
decision_refs: []
next_task: "T16.06 begun in L16-05; T16.06 remainder + AC16.04 Playwright triggers."
updated: 2026-10-16
```

#### L16-05 Cached splats and legal payloads leave the service worker only through the rights check (T16.06, T16.14)
```yaml
id: L16-05
status: tests_pass
env: local
evidence_shas: [228a1a7]
files: [oracle-app/src/sw-oracle.js, oracle-app/src/swOracle.test.js]
tests_run:
  - {cmd: "read oracle-app/src/sw-oracle.js and lib/splatCacheGate.js at 228a1a7; grep the app for the service worker's message types", result: "T16.14 holds on the fetch path: /api/media/{id} is served from IndexedDB only after a conditional GET the server answers under RLS (401/403/404/410 purge the copy). But two message handlers, CACHE_RETRIEVE_SPLAT and CACHE_RETRIEVE_LEGAL, posted the cached splat blob / legal payload back to any page that asked, with no check; nothing in the app sends either message", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run src/swOracle.test.js (before / after)", result: "before: 2 failed (the retrieve messages exist; 4 splat-store reads). after: 2 passed: no message hands back cached bytes or payloads; the splat store is read only by the gate, the presence query and the prefetch dedupe", date: 2026-10-16}
  - {cmd: "cd oracle-app && npx vitest run && npx eslint . && npm run build && node scripts/check-bundle-budget.mjs; grep -c CACHE_RETRIEVE dist/sw-oracle.js", result: "925 passed; eslint exit 0; within budget; 0 in the built service worker", date: 2026-10-16}
authz_checked: "The removed handlers were the only way cached bytes left IndexedDB without the server's RLS-checked 304. Sign-out still deletes the whole store (clearPrivateCaches)."
blockers:
  - "Source-pinned test only: the service worker does not load in jsdom; AC16.04 asks for Playwright per trigger (role change, reassignment, revocation, logout), not yet written."
  - "Reviewed, not changed: LeadMap's in-memory geocode cache is keyed by address (a later user hits it only for an address they already hold); the backend integration cache keys AVM answers by the property, not by a viewer. No AI context or page cache keyed by actor/permission revision exists yet to review."
  - "Not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "AC16.04 Playwright: a viewer who loses access (reassignment, revocation, role change) is refused the cached splat on the next view."
updated: 2026-10-16
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
env: staging
evidence_shas: [745f2fa, e125e51]
files: [backend/db/migrations/0132_client_note_body_sealed.sql, backend/client_note_crypto.py, backend/backfill_client_notes.py, backend/commands_api.py, backend/client_enterprise.py, backend/ai_chat_store.py, backend/client_ai_automation.py, backend/privacy_export.py, backend/privacy_data_map.py, backend/tests/test_client_note_sealing.py, backend/tests/test_client_note_sealing_live.py, backend/tests/test_call_notes_live.py, docs/privacy-data-map.md, docs/operator-scripts.md]
tests_run:
  - {cmd: "sed -n 2301,2345p backend/commands_api.py; grep -n client_notes backend/privacy_data_map.py", result: "deliver_call_notes inserts into client_notes.body, a plain text column (:2335); the data map classifies client_notes as business CRM only (:65)", date: 2026-10-12}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3240 passed, 11 skipped (baseline at ef526bd: 3229 passed, 10 skipped)", date: 2026-10-13}
  - {cmd: "livetest.sh migrate (local dev PostgreSQL, throwaway roles)", result: "applied 0132_client_note_body_sealed.sql", date: 2026-10-13}
  - {cmd: "livetest.sh python -m pytest -q tests/test_client_note_sealing_live.py tests/test_call_notes_live.py + the rest of the ci.yml live list", result: "exit 0; 12 passed (app role, FORCE RLS). tests/test_migration_precheck.py fails locally only because the shared dev DB records 0106_brokerage_setup.sql, a file in no branch", date: 2026-10-13}
  - {cmd: "worker periodic task client_note_seal (data_integrations/periodic.py) + backfill_client_notes.seal_pending; live test extended", result: "the worker seals leftover plaintext notes in bounded restart-safe passes (default 2,000 rows / 30 min) using the key already in its environment, so the backfill needs no operator and no key copy; owner go-ahead 2026-10-14 ('continue with all'); --unseal stays a manual rollback", date: 2026-10-14}
  - {cmd: "staging after fe9c62c (read-only DB check + worker log)", result: "7 client notes still plaintext, 0 sealed, no client_note_seal job rows: the worker logs 'PeriodicScheduler NOT started: ORACLE_RECOVERY_MODE is set', and staging runs in recovery mode on purpose, so NO periodic job runs there. A deployment without recovery mode seals on the first pass. Not changed: turning recovery mode off would start every periodic job on staging (harvests, nurture, sweeps) at once.", date: 2026-10-14}
  - {cmd: "staging after 6dd475d (CI run 38642567727 green): worker log; read-only counts over the staging DB window (opened and closed); GET /api/crm/clients/{id}/notes as the demo owner", result: "worker: 'PeriodicScheduler in internal-only mode … only client_note_seal, sealed_text_seal, audit_chain_verify run'; only those three were queued (22:01 and 23:01 UTC). automation_jobs: each periodic:{client_note_seal, sealed_text_seal, audit_chain_verify} succeeded twice (chain intact). Plaintext left: client_notes 0 of 7 (7 sealed), user_interactions 0 of 6 (6 sealed), sms_messages and negotiation_events empty. All 7 notes read back as text through the API (0 shown as ciphertext).", date: 2026-10-14}
authz_checked: "note keys come from the row's own tenant (client_note_crypto.open_rows), never the caller; reads and writes run as the app role under 0131 RLS in the live test"
blockers:
  - {type: decided, note: "Owner 2026-10-14: recovery mode runs internal-only periodic tasks — client_note_seal, sealed_text_seal (new, 0152 fields) and audit_chain_verify — which read and write only this database; every task that contacts a person or an outside service stays off (data_integrations/periodic.py PeriodicTask.internal_only; tests/test_periodic_recovery_mode.py). Staging at 3cfe8b4 held 7 plaintext client notes and 6 plaintext chat turns, 0 sms / 0 transcripts (read-only count)."}
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
updated: 2026-10-14
```

#### L17-08 Commercial email: unsubscribe link, RFC 8058 one-click, postal address (crosswalk T-CS-1)
```yaml
id: L17-08
status: tests_pass
env: staging
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
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "the unsubscribe writes outreach_suppression (tenant wall only, no agent narrowing, 0140) as a worker context; the live test proves agent B, who cannot see agent A's client, is blocked from emailing the same person after an unsubscribe from A's email (queued-before send blocked at delivery, new send 451, command gate denies), and that another brokerage is not bound"
blockers:
  - {type: decided, note: "Owner 2026-10-14: Platform Use Policy v6 (neoh-platform-use-policy-2026-10-14-v6) and Account Security ESA v2 (neoh-account-security-esa-2026-10-14-v2) approved as drafted: unsubscribe + postal address (s6), payment cards never asked for or kept (s4), operator views audited (s2), two-step sign-in (s1 + ESA s2). POLICY_COVERAGE rows added. No postal address = client email held (409 at queue time, refused at send)."}
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
env: staging
evidence_shas: [0a76a72]
files: [backend/pan_redaction.py, backend/audit_ledger.py, backend/server.py, backend/client_note_crypto.py, backend/commands_api.py, backend/voice_intel.py, backend/messaging_data.py, backend/outreach_compliance.py, backend/crm.py, backend/intelligence_os_api.py, backend/client_portal.py, backend/ai_chat_store.py, backend/ai_chat_agent.py, backend/memory_core/session_manager.py, backend/inbound_voice.py, backend/contacts_api.py, backend/elevenlabs_realtime.py, backend/neoh_persona.py, backend/qwen_omni_realtime.py, backend/qwen_voice_agent.py, backend/tests/test_pan_redaction.py, backend/tests/test_pan_redaction_paths.py, backend/tests/test_pan_redaction_live.py, backend/tests/test_neoh_persona.py, .github/workflows/ci.yml, docs/compliance/pci-saq-a.md]
tests_run:
  - {cmd: "grep billing.py / BillingOverlay.jsx / nginx.conf CSP at 0a76a72", result: "payment only via Stripe-hosted Checkout (stripe.checkout.Session.create → window.location.href) and the Stripe portal; no Stripe.js/Elements, CSP script-src has no js.stripe.com; billing.py stores no card fields. Nothing detected a card number arriving by call, text, email, note or chat.", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_pan_redaction.py tests/test_pan_redaction_paths.py", result: "exit 0; 91 + 19 passed. With the redactor disabled all 19 path tests fail (mutation check).", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q tests/test_pan_redaction_live.py + the ci.yml live list", result: "51 passed incl. the new live test (AI call note, inbound text, agent note under the app role; PCI_REDACTION audit rows hold counts only); tests/test_migration_precheck.py fails locally only because the shared dev DB records 0106 (no branch) and another agent's 0146", date: 2026-10-14}
  - {cmd: "bandit (CI flags: medium/medium, baseline)", result: "exit 0, no new findings", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3596 passed, 42 skipped. tests/test_neoh_persona.py::test_compact_is_substantially_smaller now measures the knowledge part (the whole-prompt ratio sat at 3.02 vs a bound of 3, so any rule both densities must carry failed it); held to 10x, measured ~20x", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "no access rule changed; the live test writes as the platform pipeline (call notes, inbound SMS) and as the app role under FORCE RLS (note route)"
blockers:
  - {type: decided, note: "Owner 2026-10-14: Platform Use Policy v6 (neoh-platform-use-policy-2026-10-14-v6) and Account Security ESA v2 (neoh-account-security-esa-2026-10-14-v2) approved as drafted: unsubscribe + postal address (s6), payment cards never asked for or kept (s4), operator views audited (s2), two-step sign-in (s1 + ESA s2). POLICY_COVERAGE rows added. No postal address = client email held (409 at queue time, refused at send)."}
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
env: staging
evidence_shas: [0a76a72]   # base; the alerts land in the commit that adds backend/security_alerts.py
files: [backend/security_alerts.py, backend/ops_alerts.py, backend/auth.py, backend/admin_ops.py, backend/server.py, backend/tests/test_security_alerts.py, .env.example, docs/privacy-data-map.md]
tests_run:
  - {cmd: "grep -n 'Successful login\\|ops_alerts\\|_notify' backend/auth.py backend/admin_ops.py (base 0a76a72)", result: "no alert on any sign-in, failure, role change or factor change; ops_alerts.py only alerted component health; the success log line printed agent_id (an email)", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_security_alerts.py", result: "9 passed (send mocked): operator sign-in alert carries id+IP+UTC time and no passphrase/code/seed; log line has actor_ref, no id/IP; 5 wrong passphrases -> 1 email (not per guess); DB platform_admin counted, agent not; rejected second factor alerts at once; 429 on the operator alerts once; role change + new owner (signup, owner invitation) alert; factor re-provisioned or switched totp->email alerts, baseline does not; secret never stored or sent; ORACLE_SECURITY_ALERTS_ENABLED=0 silences", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/ (covers L17-09..13)", result: "exit 0; 3514 passed, 43 skipped", date: 2026-10-14}
  - {cmd: "bandit -q -r backend (CI flags) -b .bandit-baseline.json; tests/rls_security_review.sql, rls_brokerage.sql, platform_rls_test.sql, privacy_lifecycle.sql, rls_tenant_wall.sql on the local dev DB with 0148", result: "bandit exit 0; all five SQL proofs exit 0", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
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
env: staging
evidence_shas: [0a76a72]   # base; fix in the commit that adds backend/webhook_auth.py
files: [backend/webhook_auth.py, backend/commands_api.py, backend/command_providers.py, backend/acs_call_handler.py, backend/log_redaction.py, backend/tests/test_webhook_auth.py, backend/tests/test_log_pii_guard.py, .env.example, docs/security-launch-review.md]
tests_run:
  - {cmd: "sed -n 130,136p backend/commands_api.py; sed -n 277,289p backend/acs_call_handler.py; sed -n 45,52p backend/command_providers.py (base)", result: "both webhooks compared ?token= with the static secret; both callback URL builders put the secret itself in the query string", date: 2026-10-14}
  - {cmd: "read uvicorn 0.49.0 protocols/websockets/websockets_impl.py:263", result: "WebSocket handshake lines go to uvicorn.error with the path as the SECOND argument; log_redaction rewrote only the third argument on uvicorn.access, so ?bridge_token= (commands_api Plivo media bridge) reached the log", date: 2026-10-14}
  - {cmd: "pytest tests/test_webhook_auth.py tests/test_recovery_security.py tests/test_acs_call_handler.py tests/test_security_launch_review.py", result: "95 passed: header token, body HMAC (body- and time-bound, 5 min), expiring path-bound signed URL (expired/other route/bad sig -> 403); Neoh's callback URLs carry no secret and no token=; old ?token= accepted with a deprecation warning that names the env var, not the secret; ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT=0 refuses it", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
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
env: staging
evidence_shas: [0a76a72]   # base; fix in the commit that adds log_redaction.actor_ref
files: [backend/log_redaction.py, backend/server.py, backend/tests/test_log_pii_guard.py, backend/audit_ledger.py, backend/auth.py, backend/tenancy.py, backend/crm.py, backend/client_enterprise.py, backend/client_portal.py, backend/agent_profile.py, backend/aws_observability.py, backend/lead_dossier.py, backend/media_api.py, backend/agent_mind/mind_service.py, docs/privacy-data-map.md]
tests_run:
  - {cmd: "AST scan of every backend logging call with agent_id/agent_identity/current_agent_id added to the PII names (base)", result: "25 sites in 11 files logged the account id (= the user's email): login success/failure, password reset/change, session registry, RLS context, CRM create/link/log, portal mint, bulk actions, AWS actions, crypto ACCESS_PII", date: 2026-10-14}
  - {cmd: "AST scan: logging calls inside `except E as x` that interpolate x", result: "201 sites in 60+ files; str() of a real asyncpg error from local PostgreSQL 16 quotes row values: 'DETAIL:  Key (email)=(jane@example.test) already exists.' and 'Failing row contains (bob@example.test, x)'", date: 2026-10-14}
  - {cmd: "pytest tests/test_log_pii_guard.py", result: "10 passed: guard now includes account ids (actor_ref() is the safe wrapper); 22 sites fixed; the PIIScrubFilter on the root and uvicorn.error handlers removes emails, phones and PG row values from messages AND tracebacks while keeping uuids, timestamps, IPs and counts; server._configure_logging installs it; WebSocket handshake query strings redacted", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
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
env: staging
evidence_shas: [0a76a72]   # base; fix in the commit that adds 0148
files: [backend/audit_ledger.py, backend/db/migrations/0148_audit_chain_verification.sql, backend/data_integrations/periodic.py, backend/server.py, backend/privacy_data_map.py, backend/tests/test_audit_fallback_lifecycle.py, backend/tests/test_audit_fallback_drain_live.py, .github/workflows/ci.yml, .env.example, docs/privacy-data-map.md]
tests_run:
  - {cmd: "grep -n 'verify_chain\\|_sqlite' backend/ (base)", result: "verify_chain had no scheduled caller; nothing read the SQLite fallback back, deleted it or bounded it (docs/privacy-data-map.md §7 'never purged')", date: 2026-10-14}
  - {cmd: "pytest tests/test_audit_fallback_lifecycle.py", result: "8 passed: no pool -> SQLite and pending; cap refuses + alerts; >30-day rows purged with an alert and the rest still verify from the stored anchor; empty fallback restarts from the anchor and VACUUMs; tampered row detected; PG failure logs the error type, not its DETAIL; audit_chain_verify registered", date: 2026-10-14}
  - {cmd: "livetest.sh sh -c 'cd /app/backend && python -m pytest -q tests/test_audit_fallback_drain_live.py' (local dev PostgreSQL, throwaway roles)", result: "1 passed: 3 SQLite rows appended to the PG chain in order with original time/content and _fallback provenance (a non-uuid tenant kept in provenance), deleted locally; re-append adds nothing; the verifier checks exactly the 3 links from a cursor, then catches a forged row once (alert once) in a rolled-back transaction; via the platform pool it is bounded and persisted; a request connection sees 0 rows and UPDATE 0", date: 2026-10-14}
  - {cmd: "verify_chain_segment(max_rows=50000) over the whole local dev chain (platform pool)", result: "38,338 rows, intact, one pass in 3.06 s (local DinD PostgreSQL 16; not a staging number)", date: 2026-10-14}
  - {cmd: "rehearse_0148.sh: scratch DB, 0001-0147 as superuser, REASSIGN OWNED to NOSUPERUSER BYPASSRLS CREATEROLE, run_migrations.py as that owner", result: "0148 applied; table owned by the owner, RLS+FORCE, policy (SELECT app_is_platform_admin()); oracle_app SELECT/UPDATE yes, DELETE no; scratch DB and roles dropped", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
  - {cmd: "staging after 6dd475d (CI run 38642567727 green): worker log; read-only counts over the staging DB window (opened and closed); GET /api/crm/clients/{id}/notes as the demo owner", result: "worker: 'PeriodicScheduler in internal-only mode … only client_note_seal, sealed_text_seal, audit_chain_verify run'; only those three were queued (22:01 and 23:01 UTC). automation_jobs: each periodic:{client_note_seal, sealed_text_seal, audit_chain_verify} succeeded twice (chain intact). Plaintext left: client_notes 0 of 7 (7 sealed), user_interactions 0 of 6 (6 sealed), sms_messages and negotiation_events empty. All 7 notes read back as text through the API (0 shown as ciphertext).", date: 2026-10-14}
authz_checked: "audit_chain_verification readable/writable only in a platform-admin context (0120 login membership); request connection proved blind in the live test"
blockers:
  - {type: decided, note: "Owner 2026-10-14: recovery mode runs internal-only periodic tasks — client_note_seal, sealed_text_seal (new, 0152 fields) and audit_chain_verify — which read and write only this database; every task that contacts a person or an outside service stays off (data_integrations/periodic.py PeriodicTask.internal_only; tests/test_periodic_recovery_mode.py). Staging at 3cfe8b4 held 7 plaintext client notes and 6 plaintext chat turns, 0 sms / 0 transcripts (read-only count)."}
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
env: staging
evidence_shas: [0a76a72]   # base; script in the commit that adds scripts/access_review.py
files: [scripts/access_review.py, backend/tests/test_access_review_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "ls scripts/ | grep -i access (base)", result: "no access-review listing existed", date: 2026-10-14}
  - {cmd: "livetest.sh sh -c 'cd /app/backend && python -m pytest -q tests/test_access_review_live.py'", result: "1 passed: one READ ONLY transaction, no INSERT/UPDATE/DELETE sent; postgres rolsuper, oracle_app neither super nor BYPASSRLS, oracle_platform_login member of platform_admin_role, no pg_* roles; broker owners per tenant (active/suspended ids) match a direct count; platform admins = env operator + users rows; operator factor reported without the seed; markdown and CSV render", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "runs on the platform login inside a read-only transaction; the output names people and is personal data (docstring)"
blockers:
  - {type: operator, note: "GitHub, DigitalOcean, Stripe, registrar and provider account listings are a manual checklist in the script's docstring; no API calls are made."}
audit_refs: []
decision_refs: []
next_task: "Run it for the first quarterly review against staging (read-only, via the db-access window) and file the result with the review record."
updated: 2026-10-14
```

#### L17-14 Demo logins refuse to boot on production (ISO A.8.9)
```yaml
id: L17-14
status: tests_pass
env: staging
evidence_shas: [5bc4494]
files: [backend/config.py, backend/auth.py, backend/tests/test_config_demo_logins.py]
tests_run:
  - {cmd: "pytest tests/test_config_demo_logins.py tests/test_config_weak_secrets.py", result: "46 passed", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "ORACLE_ENABLE_DEMO_LOGINS set (any value but empty/0/false/no/off) with ORACLE_ENV outside {dev, development, local, staging} — prod, production, unset, test, ci — refuses the boot; message names the setting, never the credential. auth.py also ignores the flag outside those envs (a process that imports auth without the lifespan)."
blockers: []
audit_refs: []
decision_refs: []
next_task: "None. Staging is unaffected: the rendered spec never sets the flag (ORACLE_ENV=staging there) and the demo brokerage logs in as ordinary users rows (scripts/demo_tenant_common.py)."
updated: 2026-10-14
```

#### L17-15 Password hashing at OWASP cost, rehash on login, common-password check (ISO A.5.17)
```yaml
id: L17-15
status: tests_pass
env: staging
evidence_shas: [5bc4494]
files: [backend/auth.py, backend/common_passwords.txt, backend/tests/test_password_hashing.py]
tests_run:
  - {cmd: "pytest tests/test_password_hashing.py + auth/password/invite suites", result: "24 passed; 116 passed", date: 2026-10-14}
  - {cmd: "timing on the dev laptop (i5-6360U, idle), median/max of 7", result: "N=2^14/8/1 (old) 56 ms; N=2^15/8/3 (new) verify 531/599 ms, hash 659/1076 ms; N=2^17/8/1 618/840 ms; 4 concurrent verifies 1.1 s wall (2-thread pool)", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "Wrong password never rewrites; rehash is compare-and-swap on the old hash; stored parameters are bounded (log2 N <= 17, 128*N*r <= 128 MiB) so a planted row cannot allocate gigabytes; a legacy-hash wrong password pays the dummy verify too, so it is not faster than an unknown account (AUTH-4)."
blockers: []
audit_refs: []
decision_refs: []
next_task: "Measure a verify on a real App Platform apps-s-1vcpu-1gb instance after deploy; if it exceeds ~0.8 s, step down to the OWASP-equivalent N=2^14/r=8/p=5 (16 MiB)."
updated: 2026-10-14
```
Parameters: N=2^15, r=8, p=3 — one of the five settings OWASP's Password Storage Cheat Sheet lists as equivalent to its N=2^17/r=8/p=1 minimum. Not 2^17 because that costs 128 MiB per hash, and a staging api instance has 1 GiB with 255-561 MiB already resident; 2^15/8/3 needs 32 MiB, and at most two hashes run at once per process (64 MiB peak). Hashing used to run on the event loop and froze every request on the worker for its duration; it now runs on a dedicated two-thread pool (scrypt releases the GIL). The hash now records its parameters (`scrypt$ln=15,r=8,p=3$salt$key`); the old three-part form still verifies as 2^14/8/1 and is rewritten after the next good login. Register, reset, change-password and accept-invite refuse the 1,000 most common passwords of 10+ characters (bundled, 12 KB, zxcvbn's list, MIT; no network).

#### L17-16 Dependency scanning on every push + Dependabot (ISO A.8.8)
```yaml
id: L17-16
status: tests_pass
env: staging
evidence_shas: [5bc4494]
files: [.github/workflows/ci.yml, .github/dependabot.yml, backend/tests/test_ci_security_gates.py]
tests_run:
  - {cmd: "npm audit --omit=dev --audit-level=high (oracle-app)", result: "found 0 vulnerabilities (19 runtime packages)", date: 2026-10-14}
  - {cmd: "npm audit (including dev)", result: "8 in the build/test toolchain (5 high: brace-expansion, browserslist, nanoid, source-map-js, undici; 3 moderate: vitest, @vitest/mocker, baseline-browser-mapping), all transitive except vitest, all fixable by npm audit fix; none ships to the browser", date: 2026-10-14}
  - {cmd: "python -m pip_audit --strict --skip-editable -r backend/requirements.txt", result: "No known vulnerabilities found", date: 2026-10-14}
  - {cmd: "pytest tests/test_ci_security_gates.py + CI wiring tests", result: "126 passed (with test_backup_restore_scripts, test_ci_deploy_wiring, test_ci_build_contexts, test_release_immutability)", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: n/a
blockers:
  - {type: owner, note: "Turn on Dependabot alerts and security updates in the GitHub repository settings; the config file cannot."}
audit_refs: []
decision_refs: []
next_task: "Fix the toolchain advisories in one PR (npm audit fix, then lint, vitest, build, bundle budget) — not done here because oracle-app/node_modules is shared with the main checkout."
updated: 2026-10-14
```
pip-audit already ran on every push (`ci.yml` backend job) before this row; the frontend job now runs `npm audit --omit=dev --audit-level=high` right after `npm ci` and fails on a HIGH or CRITICAL runtime advisory. Dependabot: pip (/backend), npm (/oracle-app; runtime and tooling groups), github-actions, docker (/backend, /oracle-app); weekly, grouped minor+patch, 1-3 open PRs per ecosystem.

#### L17-17 Platform-admin reads of tenant data are audited (ISO A.8.2, A.8.15)
```yaml
id: L17-17
status: tests_pass
env: staging
evidence_shas: [5bc4494]
files: [backend/audit_middleware.py, backend/tests/test_platform_admin_read_audit.py]
tests_run:
  - {cmd: "pytest tests/test_platform_admin_read_audit.py tests/test_security_launch_review.py", result: "72 passed", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "Only a token whose role is platform_admin; tenant users' reads are not recorded; sign-in/session/policy/ws paths skipped. Row: ADMIN_ACTION 'platform_read GET <path>', actor, route template, path, target_tenant_id (the tenant the route or ?tenant_id names, else 'all'), status, time, client IP. No query string, no response content."
blockers: []
audit_refs: []
decision_refs: []
next_task: "Route decrypts through crypto.access_pii_field (still no caller) once field encryption (A01) lands; platform-admin WebSocket subscriptions are not covered (BaseHTTPMiddleware does not see them)."
updated: 2026-10-14
```
Bounded: one row per request, never per record. The same actor reading the same path again within 60 s is coalesced into the first row (the operator console polls every 60 s); a read of a different record is a different path and is always recorded. The coalescing map is capped at 5,000 keys. Writes go through `ledger.record` via the existing tracked background task; `audit_ledger.py` is unchanged.

#### L17-18 The DR drill runs in CI (ISO A.8.13)
```yaml
id: L17-18
status: tests_pass
env: staging
evidence_shas: [5bc4494]
files: [.github/workflows/ci.yml, scripts/dr-drill.sh, backend/tests/test_ci_security_gates.py]
tests_run:
  - {cmd: "scripts/dr-drill.sh against a schema/ledger/roles export of the local dev DB (head 0146), DinD, ORACLE_DRILL_PG_IMAGE=local postgres:16-alpine + bash (DinD cannot pull postgres:16)", result: "before the readiness fix: 42 passed, 1 FAILED ('target already has  tables'); after: 43/43 twice, 41 s and 51 s, backup 2 s, restore 8-13 s, no containers left", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "Roles exported with --no-role-passwords; drill isolation is proven as oracle_app_login."
blockers: []
audit_refs: [A04]
decision_refs: []
next_task: "Confirm the first CI run of the rls job (pulls postgres:16; never run on GitHub yet). Remaining DR blockers are unchanged: scheduled, encrypted, off-host backups (docs/dr-drill-report.md)."
updated: 2026-10-14
```
New step at the end of the `rls` job: export schema, migration ledger and roles from the CI database that was just built from every migration (`pg_dump`/`pg_dumpall` from a `postgres:16` container on the host network), then run the real drill. Job timeout 15 → 20 min. Defect found: both readiness loops used a socket-only `pg_isready`, which answers during initdb's temporary server, so the next query could land in the restart gap. They now wait for TCP (`pg_isready -h 127.0.0.1`). The drill's image is now `ORACLE_DRILL_PG_IMAGE` (default `postgres:16`).

#### L17-19 Two-step sign-in (TOTP) for tenant users
```yaml
id: L17-19
status: tests_pass
env: staging
evidence_shas: [e25f3e0]
branch: worktree-agent-a25105adfe3fa71c4
files: [backend/db/migrations/0150_tenant_user_mfa.sql, backend/user_mfa.py, backend/mfa_api.py, backend/qr_code.py, backend/auth.py, backend/tenancy.py, backend/policy_acceptance.py, backend/brokerage_onboarding.py, backend/operator_api.py, backend/server.py, backend/privacy_data_map.py, backend/tests/test_tenant_user_mfa.py, backend/tests/test_tenant_user_mfa_live.py, docs/privacy-data-map.md, oracle-app/src/components/TwoStepSetup.jsx, oracle-app/src/components/TwoStepGate.jsx, oracle-app/src/components/TwoStepSettings.jsx, oracle-app/src/components/LoginVault.jsx, oracle-app/src/components/BrokerageSetupPanel.jsx, oracle-app/src/components/MyProfileTab.jsx, oracle-app/src/App.jsx, oracle-app/src/lib/apiClient.js, oracle-app/src/components/TwoStep.test.jsx, oracle-app/bundle-budget.json, scripts/demo_tenant_common.py, scripts/seed-demo-tenant.py, scripts/demo-preflight.py, scripts/crawl-all-pages.py, scripts/test-killer-demo-playwright.py, tests/golden/golden_e2e.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_tenant_user_mfa.py", result: "exit 0; 38 passed (QR decodes with OpenCV v1-v20; enrolment-only gate; claim carried through policy renewal; who must enrol; recovery codes; security.txt)", date: 2026-10-14}
  - {cmd: "livetest.sh migrate; livetest.sh python -m pytest -q tests/test_tenant_user_mfa_live.py", result: "applied 0150; exit 0; 1 passed (app + platform roles, FORCE RLS): enrolment-only session, sealed secret, brokerage context reads 0 factor rows, wrong/replayed code refused, recovery code single use, lockout 429 after 5, reset link needs the code, owner resets agent / cannot reach owner, operator resets owner, brokerage switch, audit rows", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3524 passed, 42 skipped; bandit (CI flags, baseline) exit 0", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q <the ci.yml live list, now 21 files>", result: "50 passed, 1 failed: tests/test_migration_precheck.py only, because the shared dev DB records 0106, 0146 and 0148 from other branches (environmental)", date: 2026-10-14}
  - {cmd: "cd oracle-app && npx vitest run && npm run lint && npm run build && npm run bundle:check", result: "83 files, 787 passed; lint clean; build ok; total-JS ceiling raised +17.3 KB raw / +5.9 KB gzip for the two-step UI (lazy chunks TwoStepSetup 6.2 KB, TwoStepSettings 6.4 KB)", date: 2026-10-14}
  - {cmd: "rls_security_review.sql, rls_brokerage.sql, platform_rls_test.sql, privacy_lifecycle.sql, rls_tenant_wall.sql on the local dev DB", result: "all exit 0", date: 2026-10-14}
  - {cmd: "non-superuser rehearsal: scratch DB, 0001-0145 as superuser, REASSIGN OWNED to NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner", result: "0150 applied by the NOSUPERUSER owner (rolsuper f, rolbypassrls t, rolcreaterole t); both tables RLS + FORCE, owned by it, policy app_is_platform_admin() only; oracle_app SELECT/DELETE yes, TRUNCATE no, PUBLIC none; tenants.require_mfa boolean NOT NULL DEFAULT false; scratch DB and roles dropped", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "factor tables admit only the platform login (app_is_platform_admin, 0120): a broker-owner request context reads 0 rows (live test). Owner resets agents of their own brokerage only (RLS finds the target); owner-on-owner needs a platform admin; agents cannot reset. Codes are checked only after the password matched."
blockers:
  - {type: decided, note: "Owner 2026-10-14: Platform Use Policy v6 (neoh-platform-use-policy-2026-10-14-v6) and Account Security ESA v2 (neoh-account-security-esa-2026-10-14-v2) approved as drafted: unsubscribe + postal address (s6), payment cards never asked for or kept (s4), operator views audited (s2), two-step sign-in (s1 + ESA s2). POLICY_COVERAGE rows added. No postal address = client email held (409 at queue time, refused at send)."}
  - {type: decided, note: "Owner 2026-10-14: Platform Use Policy v6 (neoh-platform-use-policy-2026-10-14-v6) and Account Security ESA v2 (neoh-account-security-esa-2026-10-14-v2) approved as drafted: unsubscribe + postal address (s6), payment cards never asked for or kept (s4), operator views audited (s2), two-step sign-in (s1 + ESA s2). POLICY_COVERAGE rows added. No postal address = client email held (409 at queue time, refused at send)."}
  - {type: owner, note: "Account Security ESA / Platform Use Policy §1 wording for two-step sign-in drafted in the round-3 report; not applied, versions not bumped (CONTRACT C5.7)."}
  - {type: scope, note: "Not deployed. After deploy, the staging owner ydngemini@gmail.com and the demo owner are asked to enrol at next sign-in (intended). The demo owner's secret is created by the first demo tool run (scripts/demo_tenant_common.py) and kept in the gitignored credentials file."}
  - {type: scope, note: "scripts/test-mission3-journeys-playwright.py (signup journey in a browser) now meets the setup screen after signup; not updated. No WebAuthn/passkeys; TOTP only."}
audit_refs: []
decision_refs: [D-2026-10-14 owner MFA decision (a)]
next_task: "Owner approves the ESA wording; deploy with approval; enrol the demo owner by running scripts/demo-preflight.py once against staging."
updated: 2026-10-14
```
Owner decision 2026-10-14 (a): authenticator-app codes for every tenant user, required for broker owners at their next sign-in, optional for agents, a brokerage-wide switch for owners (built: `PUT /api/brokerage/security`). ISO 27001 A.8.5 gap #2 (`docs/compliance/iso27001/annex-a-assessment.md`). Reuses `totp.py` (RFC 6238, ±1 step), the 0121 rule (a code is accepted once; five wrong codes retire it), the per-account login rate limit and the session epoch (0117: turning it on/off or a reset ends other sessions). An account that owes setup signs in to an enrolment-only session: every route except the policy routes, `/auth/mfa/*`, logout, CSRF and the session probe answers 403 `mfa_enrolment_required`, and every token renewal carries the claim. No bypass flag exists.

#### L17-20 Public security and privacy contacts (security.txt)
```yaml
id: L17-20
status: external_configuration_required
env: staging
evidence_shas: [e25f3e0]
branch: worktree-agent-a25105adfe3fa71c4
files: [backend/security_contact.py, backend/privacy_api.py, backend/server.py, infra/digitalocean/app.yaml, oracle-app/nginx.conf, oracle-app/vite.config.js, oracle-app/src/devProxy.test.js, .env.example, .env.prod.example, backend/tests/test_tenant_user_mfa.py]
tests_run:
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_tenant_user_mfa.py -k 'security_txt or privacy_contact or edge'", result: "exit 0; 404 until ORACLE_SECURITY_CONTACT is set; RFC 9116 fields (Contact, Expires ≤ 365 days computed per request, Preferred-Languages, Canonical only on https); a newline in the value cannot add a field; ingress routes the file to the API before the static site; nginx answers 404 rather than the SPA", date: 2026-10-14}
  - {cmd: "staging after a7fcaab (CI run 38549337342 green incl. full-history secret scan; PRE_DEPLOY applied 0146/0148/0150 as doadmin, each rehearsed first as a NOSUPERUSER BYPASSRLS owner); real API + system Chrome (Playwright) as the demo owner and agent", result: "Policy v6 + ESA v2 served and accepted. Demo owner enrolled two-step through the product routes (secret in the 0600 credentials file); a password-only sign-in then answers 401 OTP_REQUIRED, password + code signs in, and the sign-in form shows the authenticator-code step at 1440 and 390 px with no page errors (performance/out/mfa17/). Agent: two-step not required. Clients 6 / contacts 6 for both; drafts owner 2 / agent 2, approvals owner 2 / agent 0; /api/brokerage/setup commercial_email NEEDS_ACTION (no postal address yet, so client email is held); /.well-known/security.txt 404 (contact not configured); a forged unsubscribe token 404; routes 86-286 ms", date: 2026-10-14}
authz_checked: "public, read-only, no data"
blockers:
  - {type: owner, note: "Owner creates security@ and privacy@ on Neoh's domain, then sets ORACLE_SECURITY_CONTACT and ORACLE_PRIVACY_CONTACT on the api component. Until then nothing is published (404 / field omitted)."}
audit_refs: []
decision_refs: [D-2026-10-14 owner contacts decision (b)]
next_task: "After the mailboxes exist: set both env vars on staging, check https://<staging>/.well-known/security.txt."
updated: 2026-10-14
```
`ORACLE_PRIVACY_CONTACT` is returned as `privacy_contact` by `POST /api/privacy/requests`, the one place the app tells a brokerage about a data-subject request (no other privacy contact is shown anywhere in the app). No `Policy:` field: there is no disclosure page yet (ISO A.6.8 also asks for one).

#### L17-21 Field encryption of the remaining plaintext PII (A01 / ISO R01), first slice
```yaml
id: L17-21
status: tests_pass
env: staging
evidence_shas: []
branch: worktree-agent-acfd0615a1192e39c
files: [backend/db/migrations/0152_sealed_communication_text.sql, backend/sealed_text.py, backend/backfill_sealed_text.py, backend/messaging_data.py, backend/commands_api.py, backend/voice_intel.py, backend/server.py, backend/memory_core/session_manager.py, backend/privacy_requests.py, backend/privacy_export.py, backend/privacy_data_map.py, backend/tests/test_sealed_text.py, backend/tests/test_sealed_text_live.py, backend/tests/test_pan_redaction_live.py, backend/tests/test_communications_access_matrix_live.py, backend/tests/test_realtime_record_scope_live.py, backend/tests/test_env_documentation.py, .github/workflows/ci.yml, docs/privacy-data-map.md, docs/operator-scripts.md]
tests_run:
  - {cmd: "grep FROM/INTO/UPDATE/JOIN of sms_messages, negotiation_events, user_interactions (backend/**/*.py, migrations); docs/privacy-data-map.md §2", result: "list re-verified. Readers of the text: sms body = privacy_requests._access_bundle + privacy_export only (no SMS thread view reads sms_messages; the CRM timeline shows client_activities summaries); transcript = voice_intel.read_voice_telemetry + the voice WebSocket hydrate (server.py) + privacy_export + purge_expired_platform_data (0027); chat turns = SessionManager.get_user_context (AI prompt + server.py memory-sync frame) + summarize_rolling_memory + privacy_export. Writers: messaging_data inbound/outbound, commands_api.call_telemetry, voice_intel.ingest_voice_telemetry, SessionManager.record_interaction. Erasure deletes whole rows (privacy_erase_subject_rows, tenant erasure), so it needs no change. Still plaintext and outside the brief's five: email_outbox bodies, interaction_logs, ai_tool_operations.result, beliefs.source_quote, user_profiles.profile_summary", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_sealed_text.py", result: "exit 0; 16 passed (open_rows both shapes + per-tenant key + fail closed + no content in errors; key = crypto.derive_tenant_key unchanged, case-stable for the text tenant_id; AST guard: every app SQL literal naming a table and its text column names the cipher column (one test per field); export DECRYPT covers all three; each writer stores no plaintext; no key = no write; 0152 is expand-only)", date: 2026-10-14}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 3822 passed, 48 skipped. The first run failed 6 unit tests whose fakes wrote SMS / transcripts / chat turns without a master key, or unpacked the old INSERT arguments; they now set a throwaway key (autouse fixture) and unpack the key argument", date: 2026-10-14}
  - {cmd: "livetest.sh migrate; livetest.sh python -m pytest -q tests/test_sealed_text_live.py", result: "applied 0152 (dev ledger sha256 290d1d31… = file); exit 0; 1 passed as the app role under FORCE RLS: every writer stores ciphertext only; every reader (REST voice feed, WS hydrate via voice_intel.load_session_events, chat memory context, subject access bundle, brokerage export) returns sealed AND pre-0152 plaintext rows in one result; the old release's plaintext INSERT still works, a both-shapes row is refused; backfill bounded (max_rows), resumable, idempotent, --unseal restores plaintext; the 0152 functions refuse an agent and another tenant's platform context; oracle_app still cannot UPDATE user_interactions; retention purges a sealed transcript (rolled back); a sealed row without the key fails closed", date: 2026-10-14}
  - {cmd: "livetest.sh python -m pytest -q tests/test_pan_redaction_live.py tests/test_communications_access_matrix_live.py tests/test_realtime_record_scope_live.py tests/test_privacy_lifecycle_live.py tests/test_client_note_sealing_live.py tests/test_call_notes_live.py tests/test_messaging_route_race_live.py", result: "first run: 3 failed because those writers now need ORACLE_ENCRYPTION_MASTER_KEY (fail closed, like client notes) and the PAN test read sms_messages.body directly. Fixed in the tests: they set a throwaway key, and the PAN test opens body_ciphertext. Rerun: all pass except test_communications_policy_composition_cannot_widen, which fails on the shared dev DB because another branch's 0151 added live_call_sessions_lead_* policies (environmental; judge on merged main)", date: 2026-10-14}
  - {cmd: "psql -f tests/rls_security_review.sql, rls_brokerage.sql, platform_rls_test.sql, privacy_lifecycle.sql, rls_tenant_wall.sql (dev DB with 0152)", result: "all 5 exit 0", date: 2026-10-14}
  - {cmd: "bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json (backend/)", result: "exit 0, no new finding (every new SQL is a literal constant)", date: 2026-10-14}
  - {cmd: "NOSUPERUSER rehearsal (scratch DB rh_0152: 0001-0150 as a throwaway superuser, REASSIGN OWNED to a NOSUPERUSER BYPASSRLS CREATEROLE owner, run_migrations.py as that owner; dropped after)", result: "0152 applied as the owner; the two seal functions and purge_expired_platform_data owned by it, SECURITY DEFINER, EXECUTE granted to oracle_app; seal then unseal of one row through the function as that owner round-trips", date: 2026-10-14}
  - {cmd: "EXPLAIN ANALYZE before (plaintext) / after (rows sealed inside a rolled-back transaction), dev DB", result: "chat memory context (5 newest of 712 turns): same index scan, 1.1 ms -> 0.1 ms query + 4.5 ms decrypt round trip; roll-up read 712 turns: 3.9 ms -> 2.3 ms + 347 ms decrypt; voice hydrate 100 events: about 42 ms decrypt added (5,000-row bench); sms tenant newest 50: same index scan, 0.7 -> 1.7 ms query, plus about 20 ms decrypt when all 50 are sealed (0.4 ms/row; the bench's newest 50 were mostly empty-bodied, so it read 0.3 ms). No plan changed (the new columns are not indexed or filtered)", date: 2026-10-14}
  - {cmd: "pgcrypto per-row bench on 2,000-5,000 real chat turns (avg 114 chars), temp tables dropped", result: "pgp_sym_encrypt 0.49-0.64 ms/row, pgp_sym_decrypt 0.39-0.40 ms/row with the existing default options; the cost is the iterated S2K. With 's2k-mode=1' (still the same HKDF tenant key, which is already 256-bit random): encrypt 0.067 ms, decrypt 0.010 ms/row. NOT adopted: it changes the established scheme; owner/security decision (decrypt reads the mode from the ciphertext, so it would be backward compatible)", date: 2026-10-14}
  - {cmd: "leads.payload sample: 10,000 rows TABLESAMPLE SYSTEM(0.2) into a temp table; encrypt + one 1,000-row batch UPDATE on the copy; dropped", result: "payload avg 1,533 bytes (p99 2,938), ciphertext avg 1,601; encrypt 0.63 ms/row default, 0.13 ms/row s2k-mode=1; batch of 1,000 on the copy 0.66 s. leads: 10.48M rows, 23 GB heap, 14 indexes (6.8 GB), fillfactor 100, one partial index reads payload->>'schema_version'; encrypted_payload bytea exists, 0 of 200,000 rows use it", date: 2026-10-14}
  - {cmd: "clients search baseline, biggest dev tenant (21,268 clients)", result: "search_api-style full_name/email/company ILIKE '%x%': 118.6 ms; privacy_requests exact match lower(email) / phone digits: 51.8 ms (no index, regexp over every tenant row); clients.encrypted_contact bytea exists, 0 rows use it; 177 SQL statements in 59 files touch clients", date: 2026-10-14}
  - {cmd: "psql on the local dev DB, 1,000 rows of 240 bytes: pgp_sym_decrypt/encrypt with the default iterated S2K vs s2k-mode=1, same 64-hex HKDF-shaped key; then migration 0153 + every app encrypt site passes s2k-mode=1 (crypto.encrypt_pii, ai_chat_store, the sealed-text writers, both backfill scripts); scripts/rehearse-migrations-nosuperuser.sh 0153; livetest sealed_text / client_note_sealing / privacy_lifecycle / pan_redaction / call_notes", result: "decrypt 487 ms -> 9.8 ms, encrypt 537 ms -> 61 ms per 1,000 rows (~50x / ~9x). Keys are 32-byte random HKDF outputs, so the iterated S2K added no strength. Rows sealed before 0153 still open (pgp_sym_decrypt reads the mode per message; the live tests write old-mode ciphertext on purpose) and the previous release opens new rows. Rehearsal OK as NOSUPERUSER; 5 live tests passed; guard test_every_app_pgp_sym_encrypt_uses_single_pass_s2k", date: 2026-10-14}
  - {cmd: "staging after 6dd475d (CI run 38642567727 green): worker log; read-only counts over the staging DB window (opened and closed); GET /api/crm/clients/{id}/notes as the demo owner", result: "worker: 'PeriodicScheduler in internal-only mode … only client_note_seal, sealed_text_seal, audit_chain_verify run'; only those three were queued (22:01 and 23:01 UTC). automation_jobs: each periodic:{client_note_seal, sealed_text_seal, audit_chain_verify} succeeded twice (chain intact). Plaintext left: client_notes 0 of 7 (7 sealed), user_interactions 0 of 6 (6 sealed), sms_messages and negotiation_events empty. All 7 notes read back as text through the API (0 shown as ciphertext).", date: 2026-10-14}
authz_checked: "keys come from each row's own tenant (sealed_text.open_rows), never the caller; the 0152 functions run only in the platform context of the row's own tenant, at most 5,000 rows a call, and can only move a row's own text between its two columns (append-only ledgers stay append-only to oracle_app); who may read a row is unchanged (RLS untouched)"
blockers:
  - {type: decided, note: "Owner 2026-10-14: recovery mode runs internal-only periodic tasks — client_note_seal, sealed_text_seal (new, 0152 fields) and audit_chain_verify — which read and write only this database; every task that contacts a person or an outside service stays off (data_integrations/periodic.py PeriodicTask.internal_only; tests/test_periodic_recovery_mode.py). Staging at 3cfe8b4 held 7 plaintext client notes and 6 plaintext chat turns, 0 sms / 0 transcripts (read-only count)."}
  - {type: operator, note: "Rows written before the 0152 code serves stay plaintext until backend/backfill_sealed_text.py runs in that environment (docs/operator-scripts.md). It is an operator script, not a periodic job: staging runs ORACLE_RECOVERY_MODE, where no periodic job runs (L17-03). Not run on staging or production. Run it only AFTER the new code serves: the previous release cannot read a sealed row."}
  - {type: scope, note: "Expand step only. The DB does not yet refuse plaintext in these columns; the contract migration comes after the code has served everywhere and the backfill reports 0 remaining (design below). Rollback to a pre-0152 release after sealing needs --unseal first."}
  - {type: scope, note: "clients name/email/phone and leads.payload: designed below, not built. clients is read by 177 statements in 59 files and searched by substring; leads.payload is 10.48M rows read by ~100 SQL and ~114 Python references. Neither is safe in one slice."}
audit_refs: [A01]
decision_refs: []
next_task: "Owner: name-search semantics for clients (word-prefix instead of substring; no email substring), sort order for client lists. Then: contract migration (after L17-22's fields have served and been sealed too); clients slice 1 (hashes + tokens, dual write). The five plaintext fields this row's census listed (email_outbox, interaction_logs, ai_tool_operations.result, beliefs.source_quote, user_profiles.profile_summary) are sealed by L17-22 (0154). The S2K option was decided and shipped in 0153."
updated: 2026-10-15
```

**Design (2026-10-14).** One scheme for every field: `pgp_sym_encrypt` under `crypto.derive_tenant_key(tenant_id, ORACLE_ENCRYPTION_MASTER_KEY)` (HKDF-SHA-256, unchanged), the key taken from the row's own tenant, and plaintext never logged. Every rollout is expand → migrate data → contract, because migrations apply before the new code serves.

1. **Read-by-id text (built, 0152):** `sms_messages.body`, `negotiation_events.transcript_excerpt`, `user_interactions.content`.
   - Expand (this slice): add `*_ciphertext bytea` plus a CHECK that a sealed row keeps no plaintext. The new code writes ciphertext only, encrypting inline in the INSERT, so there is no extra round trip; it leaves the old column NULL (or `''` when NOT NULL). Readers open both shapes through `sealed_text.open_rows`, with one decrypt round trip per tenant. The previous release keeps writing and reading plaintext.
   - Backfill: `backfill_sealed_text.py` (operator).
   - Contract (later, its own deploy, after the backfill reports 0 remaining in that environment): `CHECK (<cipher> IS NOT NULL OR COALESCE(<plain>,'') = '') NOT VALID`, then `VALIDATE` in the next deploy. A pre-0152 release must never run against that schema again.
2. **`clients` contact fields (design only).** Reuse the `agent_contacts` scheme so the two tables match each other.
   - Columns: `email_lookup_hash` / `phone_lookup_hash` from `contact_truth.lookup_hash` (HMAC-SHA-256 under the tenant key over the normalized value). These give exact match, dedupe, privacy lookup and inbound-number matching through partial btree indexes on `(tenant_id, hash)`. `name_search_tokens text[]` from `contact_truth.name_search_tokens` (keyed word-prefix tokens), with a GIN index. `encrypted_contact` (exists, unused) for the sealed JSON.
   - Slices:
     - (a) expand + dual write of hashes and tokens (no reader change), and backfill them;
     - (b) move every search onto hashes and tokens: privacy_requests, the search_api / crm / ai_chat_store / commands_api ILIKE, and dedupe;
     - (c) move the readers to a decrypting helper, page by page, under an AST guard like F17.03;
     - (d) stop writing plaintext and seal the old rows;
     - (e) contract.
   - Behaviour change for the owner to approve: name search becomes word-prefix ("smi" finds "Smith", not "Goldsmith"), and email search becomes exact match only. Ordering by name (2 statements) needs a decision: sort the decrypted page in the app, or keep a keyed order token.
   - Measurement plan: the baselines above (118.6 ms substring search, 51.8 ms exact match on 21k clients). After each slice: `EXPLAIN ANALYZE` of the hash and token lookups (expected index scans), list-page latency including decrypt (50 rows ≈ 20 ms with default S2K, ≈ 0.5 ms with s2k-mode=1), and the dedupe hit rate before and after on the same fixtures.
3. **`leads.payload` (design only, not backfilled).** Most of `payload` is public-record property facts that are filtered and indexed (`payload->>'schema_version'`). Seal only the personal keys: owner names, mailing address, phones and emails. Move them into `encrypted_payload` (exists, unused) and leave the rest plaintext, so filters keep working.
   - Estimate from the measured sample (default options): encryption alone is 10.48M × 0.63 ms ≈ 1.8 h of one backend's CPU. Every row is also rewritten non-HOT (fillfactor 100, 14 indexes), which adds an estimated 1.5–3×, so about 3–6 h for one worker or about 1–1.5 h for 4 workers split by tenant and id range. WAL is roughly the size of a heap rewrite (≈ 25–30 GB), and the table doubles until VACUUM. That cannot run on the dev disk (4.4 GB free); staging's row count must be measured first.
   - Batching and locking:
     - `ADD COLUMN` with no default (metadata only; there is no new index).
     - Keyset by `id`, 1,000 rows per batch, each its own short transaction with `FOR UPDATE SKIP LOCKED` and `statement_timeout`.
     - Resumable on `encrypted_payload IS NULL AND id > $last`.
     - Throttled on replication lag and IO, run off-peak.
     - Plain `VACUUM` every N batches, never `VACUUM FULL`. No ACCESS EXCLUSIVE lock at any point.
   - Readers move first: every `payload` reader that touches a personal key goes through a helper, under a guard test.

#### L17-22 Field encryption of the remaining plaintext PII (A01 / ISO R01), second slice
```yaml
id: L17-22
status: tests_pass
env: staging
evidence_shas: [cef0006, 1528459, a5caaca]
branch: worktree-agent-a67b64a862bbe3928
files: [backend/db/migrations/0154_sealed_text_slice_two.sql, backend/sealed_text.py, backend/backfill_sealed_text.py, backend/data_integrations/periodic.py, backend/crm.py, backend/email_outbox.py, backend/commands_api.py, backend/voice_intel.py, backend/intelligence_os_api.py, backend/client_portal.py, backend/client_enterprise.py, backend/client_ai_automation.py, backend/lead_dossier.py, backend/neoh_intents.py, backend/admin_ops.py, backend/ai_chat_store.py, backend/belief_store.py, backend/memory_core/session_manager.py, backend/privacy_requests.py, backend/privacy_export.py, backend/privacy_data_map.py, backend/tests/test_sealed_text.py, backend/tests/test_sealed_text_two_live.py, backend/tests/test_sealed_text_live.py, backend/tests/test_crm_workflows.py, backend/tests/test_email_unsubscribe_live.py, .github/workflows/ci.yml, docs/privacy-data-map.md, docs/operator-scripts.md]
tests_run:
  - {cmd: "grep of every FROM/INTO/UPDATE/JOIN on email_outbox, interaction_logs, ai_tool_operations, beliefs, user_profiles (backend/**/*.py, migrations, SQL functions); \\d of each table on the dev DB; docs/privacy-data-map.md §2/§7; has_table_privilege(oracle_app)", result: "re-verified, all five read by id or parent record, none searched, filtered, ordered or indexed by content (no ILIKE/tsvector/index on any of the columns; the only payload->> filters in the app are on leads). Readers of the text: email_outbox subject/body = email_outbox.deliver (send time; the worker holds the key), privacy_requests._access_bundle, privacy_export, admin_ops /outbox (subject, platform admin, cross-tenant). interaction_logs subject/payload text = crm.client_interactions, crm.send_message RETURNING, crm.comms_threads (snippet), client_enterprise.comms_thread, client_ai_automation._load_bundle (AI context), lead_dossier.get_dossier, neoh_intents._person_detail (subject), admin_ops /activity (subject), privacy_export; every other reader is a count or reads behavioural keys only. ai_tool_operations.result = ai_chat_store._ledger_replay + privacy_export. beliefs.source_quote = belief_store (assert/beliefs_about/correct_belief, SELECT * / RETURNING *) + privacy_export. user_profiles.profile_summary = SessionManager.get_user_context (AI prompt + memory-sync frame) + summarize_rolling_memory + privacy_export. Writers: crm.send_message (log + outbox), commands_api._record_send_bookkeeping, voice_intel voice notes, intelligence_os_api.capture_events, client_portal activity, ai_chat_store._ledger_attach_result, belief_store.assert_belief, SessionManager.summarize_rolling_memory. Not readers: the subject access bundle never included interaction_logs/beliefs/tool receipts/profile (unchanged). Retention: no purge touches these columns (erasure deletes whole rows), so no purge function changes. Grants: oracle_app may UPDATE email_outbox, ai_tool_operations, beliefs, user_profiles; NOT interaction_logs (0008) -> storage-move function. email_outbox.body_html: no writer, no reader", date: 2026-10-15}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/test_sealed_text.py tests/test_crm_workflows.py", result: "exit 0; 52 passed. AST guard now one test per field (10), refined to name a column on ITS table (an alias-qualified ld.payload is the leads column; guard self-test added); split_json / merge keeps the payload's shape; nothing to seal needs no key, text without a key fails closed; the content keys agree across sealed_text, both 0154 SQL sites and the backfill; 0154 expand-only (7 CHECKs all '<cipher> IS NULL OR'); every table backfilled; an unopened sealed belief raises; the tool receipt is attached only sealed", date: 2026-10-15}
  - {cmd: "cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/", result: "exit 0; 5022 passed, 52 skipped (first run before the last fixes: 5013 passed; the 3 crm_workflows fakes now set a throwaway key and assert the text is in the sealed argument, not the stored payload)", date: 2026-10-15}
  - {cmd: "BACKEND_DIR=<worktree>/backend scripts/livetest-local.sh migrate; ... python -m pytest -q tests/test_sealed_text_two_live.py", result: "applied 0154; exit 0, 1 passed as the app role under FORCE RLS: every writer (CRM email + note, send bookkeeping, perception event, belief, tool receipt, profile summary) stores no plaintext of the sealed fields and the behavioural payload keys stay plain; every reader (delivery, access bundle, brokerage export, operator outbox + activity, client interactions, comms threads + thread view, client AI context, lead dossier, Neoh client card, tool replay, beliefs assert/read/pin, memory context) returns sealed AND pre-0154 plaintext rows in one result, before and after the backfill; the previous release's plaintext INSERTs still work, a both-shapes row is refused (3 CHECKs probed); a sealed email without the key is never left claimed (stays queued) and is sent once the key is back; seal_interaction_logs refuses an agent and another tenant's platform context; oracle_app still cannot UPDATE interaction_logs; backfill bounded (max_rows), idempotent, --unseal restores plaintext (payload keys merged back); a sealed row without the key fails closed (beliefs, client interactions)", date: 2026-10-15}
  - {cmd: "livetest-local.sh python -m pytest -q tests/test_sealed_text_live.py tests/test_email_unsubscribe_live.py tests/test_communications_access_matrix_live.py tests/test_pan_redaction_live.py tests/test_frame_constraints_live.py tests/test_engagement_lifecycle_live.py tests/test_lead_record_access_live.py tests/test_reconciliation_live.py tests/test_brokerage_access_matrix_live.py tests/test_privacy_lifecycle_live.py tests/test_client_note_sealing_live.py tests/test_realtime_record_scope_live.py tests/test_lead_children_access_live.py", result: "exit 0; 17 passed. test_sealed_text_live now iterates only its own three backfill fields; test_email_unsubscribe_live sets a throwaway master key (the CRM email writer now fails closed without one)", date: 2026-10-15}
  - {cmd: "scripts/rehearse-migrations-nosuperuser.sh 0154", result: "REHEARSAL OK: 0001-0153 as a throwaway superuser, owner super=false bypassrls=true, 0154 applied as that owner (seal_interaction_logs created and granted with no superuser-only SQL)", date: 2026-10-15}
  - {cmd: "psql -f tests/rls_security_review.sql, rls_brokerage.sql, platform_rls_test.sql, privacy_lifecycle.sql, rls_tenant_wall.sql (dev DB with 0154)", result: "all 5 exit 0", date: 2026-10-15}
  - {cmd: "bandit -q -r . -x ./tests,./venv,./ml_forge,./node_modules,./scripts --severity-level medium --confidence-level medium -b .bandit-baseline.json (backend/); gitleaks dir backend (CI image digest)", result: "bandit exit 0 (every new SQL is a literal); gitleaks: 11 findings, all in pre-existing test files already fingerprinted in .gitleaksignore, none in this change", date: 2026-10-15}
  - {cmd: "EXPLAIN ANALYZE, one rolled-back transaction on the dev DB: 200 clients, 5,000 interaction_logs rows (~300-char bodies, 1 in 10 a 1,500-char transcript), plaintext then sealed in place (scratch bench, dropped)", result: "comms threads (200 clients): da163ba query 194 ms; first slice-2 draft that carried the ciphertexts through the materialized CTE 1,182 ms sealed (defect, fixed before commit); shipped shape (latest touch id in the CTE, ciphertexts joined by primary key for the <=200 chosen rows, Memoize + interaction_logs_pkey) 199 ms plaintext / 180 ms sealed, plus 2.7 ms + 7.3 ms decrypt for 200 subjects + 200 contents (86 KB). Client interaction list (50 newest of one client): same plan before/after, 1.2 -> 1.7 ms, plus 2.7 ms decrypt for 25 rows. Thread view: same bitmap index plan, 0.19 -> 0.18 ms. 500 transcript-sized contents decrypt in 12.7 ms (0.025 ms/row). Sealing 5,000 rows in one statement (what seal_interaction_logs does per batch of <=5,000): 1.0-1.8 s. Single-row readers (delivery claim, tool replay, profile, a subject's beliefs) add one decrypt round trip each", date: 2026-10-15}
  - {cmd: "staging after 45a031d (CI run 38721636255 green; PRE_DEPLOY applied 0154 as doadmin); read-only counts over the staging DB window (opened and closed); scripts/staging-smoke.py", result: "migration head 0154; periodic:sealed_text_seal succeeded (last 10:49 UTC). Plaintext left: 0 in every 0154 field — ai_tool_operations.result 2 of 2 sealed by the job; email_outbox, beliefs empty; the 1 interaction_logs row and 2 user_profiles rows hold no text to seal; user_interactions 6 of 6 sealed. Smoke: both demo accounts sign in (owner with a code), lists unchanged.", date: 2026-10-15}
authz_checked: "keys come from each row's own tenant (sealed_text.open_rows; the operator outbox/activity feeds open each row under its own tenant), never the caller's; seal_interaction_logs runs only in the platform context of the row's own tenant, at most 5,000 rows a call, and can only move a row's own subject/content keys between its two shapes (interaction_logs stays append-only to oracle_app); who may read any row is unchanged (no RLS policy, grant or route changed)"
blockers:
  - {type: operator, note: "Rows written before the 0154 code serves stay plaintext until the periodic sealed_text_seal task (internal-only, runs in recovery mode) or backend/backfill_sealed_text.py seals them (docs/operator-scripts.md). Only after the new code serves: the previous release cannot read a sealed row. Not run on staging or production."}
  - {type: scope, note: "Expand step only. The DB does not yet refuse plaintext in these columns; the contract migration comes after the code has served everywhere and the backfill reports 0 remaining. Rollback to a pre-0154 release after sealing needs --unseal first."}
  - {type: scope, note: "Not sealed, by design: email_outbox.to_email (matched by lower(to_email) in the privacy request; needs a keyed lookup hash, the clients design in L17-21 §2); interaction_logs behavioural payload keys (counted and filtered); email_outbox.body_html (no writer; a sealed row must keep it NULL); interaction_logs.payload.source_filename (voice-note upload name, left plaintext — owner question below)."}
  - {type: defect, note: "Pre-existing, not fixed here: SessionManager.summarize_rolling_memory can never commit as the app role — it DELETEs the archived user_interactions rows and 0009 grants oracle_app only SELECT/INSERT, so the whole roll-up (including the profile summary upsert) rolls back. It has no caller in the app today (grep), so profile_summary has no live writer; its sealed writer is store_profile_summary (live-tested directly). Fix needs a decision: a SECURITY DEFINER archive function, or DELETE for oracle_app (ends the append-only property 0152 relies on)."}
  - {type: defect, note: "Pre-existing: crm.comms_threads scanned the materialized touches CTE once per client (2 CTE scans x 200 loops), quadratic in brokerage size. Fixed in L17-25 (one grouped pass)."}
audit_refs: [A01]
decision_refs: []
next_task: "Owner/engineering: the roll-up's DELETE grant (defect above); whether voice-note source_filename should be sealed too. Then: deploy with approval; let sealed_text_seal (or the operator script) report 0 remaining for all eight fields; one contract migration for 0152 + 0154 fields; clients slice 1 (L17-21 §2)."
updated: 2026-10-15
```
Base `da163ba` (staging at migration head 0153). Same scheme and rollout as L17-21 (pgp_sym_encrypt under `crypto.derive_tenant_key`, `s2k-mode=1` per 0153; expand → backfill → contract). New in this slice: `interaction_logs.payload` mixes content with behavioural keys, so only the text keys (`body`, `text`, `transcript`, `summary`, `extraction`) move into `content_ciphertext` as one JSON object (`sealed_text.split_json` at write, merged back by `open_rows`), and the keys that counts and filters read stay in `payload`. A writer needs the master key only when there is text to seal (`sealed_text.key_if_needed`), so a portal view or a metadata-only row still writes without one. The email send worker opens the subject and body inside its claim transaction, so a row it cannot open is never left `sending`. Policy: no wording change (encryption does not change who sees data, how people are contacted or what the AI does).

#### L17-23 Clients: keyed lookup hashes and name tokens kept current (A01, L17-21 design §2 slice a)
```yaml
id: L17-23
status: tests_pass
env: local
evidence_shas: [7574a2c]
files: [backend/db/migrations/0156_client_contact_index.sql, backend/client_contact_index.py, backend/data_integrations/periodic.py, backend/privacy_data_map.py, backend/tests/test_client_contact_index.py, backend/tests/test_client_contact_index_live.py, backend/tests/test_periodic_recovery_mode.py, .github/workflows/ci.yml, .env.example, docs/privacy-data-map.md, docs/operator-scripts.md]
tests_run:
  - {cmd: "\\d clients, \\d agent_contacts on a local PostgreSQL 16.15 built from every migration", result: "clients has trg_clients_updated (BEFORE UPDATE, set_updated_at) on every update, so index columns ON clients would bump updated_at for every client when filled (CRM list order; client_ai_automation abandons a pass when updated_at moves). agent_contacts' scheme: char(64) email/phone lookup hashes, text[] name tokens with format CHECKs, partial btree + GIN indexes. Chosen: a side table with the same columns", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_client_contact_index.py tests/test_periodic_recovery_mode.py tests/test_privacy_data_map.py tests/test_env_documentation.py", result: "42 passed: values equal contact_truth's for agent_contacts (a 'sar joh' prefix query is answerable); no plaintext in any value; tenant-separated; unnormalisable email/phone flagged, never hashed; no key fails closed; the periodic task skips without a key; the internal-only task list is now audit_chain_verify, client_contact_index, client_note_seal, sealed_text_seal", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_client_contact_index_live.py (real PostgreSQL, FORCE RLS, app role)", result: "before 0156: fails (relation does not exist). After: 1 passed — every inserted client gets a stale row; fill writes contact_truth's values and leaves clients.updated_at unchanged; an edit of notes/stage or a same-value email does not mark stale, a new email does, the next fill uses it; a fill racing an uncommitted phone edit skips that client (SKIP LOCKED) and the committed edit's stale mark stands; agent A reads index rows only of clients A can see (not B's), cannot update/delete any (0 rows) or insert (42501); an agent's own edit still marks stale (SECURITY DEFINER trigger); the row goes with its client", date: 2026-10-15}
  - {cmd: "bench: 20,000 synthetic clients in a scratch tenant, local PostgreSQL 16 (deleted after), twice", result: "bulk INSERT of 20,000: 0.70-0.74 s with both index triggers disabled, 1.42-1.79 s enabled (a first row-level INSERT trigger measured 2.33 s and was replaced by one statement-level trigger). fill: 20,000 clients in 12.5-13.0 s (~1,550/s, 500 per batch). EXPLAIN ANALYZE: email-hash lookup Index Scan on idx_client_contact_index_email 0.038 ms; two-word name-prefix lookup Bitmap Index Scan on the GIN 1.1 ms (111 rows); today's lower(email) exact match on clients 8.7 ms (filters all 20,000 rows of the tenant)", date: 2026-10-15}
  - {cmd: "psql -f tests/rls_*.sql, platform_rls_test.sql, privacy_lifecycle.sql; every live test the CI rls job runs + this one", result: "SQL proofs exit 0. Live: 63 passed, 1 failed — test_client_note_sealing_live, only when it runs after this test (alone and in CI's order it passes). Root cause is pre-existing, not in this slice: the sealing batches' `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` is planned as a nested-loop semi join on small tables and the locking subquery re-runs per outer row, so a batch of 1 sealed 3 (EXPLAIN in L17-24). Fixed in L17-24", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests compliance_engine/tests (clean env); bandit -b .bandit-baseline.json", result: "5192 passed, 61 skipped, 3 failed: test_media_storage + test_object_storage (fail identically without the change: the container runs as root) and test_migration_ledger for 0155 (fixed in the L03-10 follow-up commit). Bandit exit 0", date: 2026-10-15}
authz_checked: "Values are computed in the app under each row's own tenant key (contact_truth); the filler runs in the platform context of that tenant. Agents read index rows only of clients they can see (RLS EXISTS on clients) and cannot write any; the staleness triggers are SECURITY DEFINER, touch only the inserted/edited clients' own rows and write only `stale`. Who may read a client is unchanged."
blockers:
  - {type: owner, note: "Slice (b) moves the searches onto this index and changes behaviour: word-prefix name search ('smi' finds Smith, not Goldsmith) and exact-only email search; and name ordering needs a decision (L17-21 design). Not started without that answer."}
  - {type: operator, note: "Not deployed: pushing to main deploys staging (C4.4). After deploy the periodic task fills existing clients (5,000 per 15-minute run by default) or client_contact_index.py fills them at once."}
  - {type: scope, note: "The plaintext is not sealed yet (slices c-e). company is not indexed (agent_contacts has no equivalent)."}
audit_refs: [A01]
decision_refs: ["owner 2026-10-14: recovery mode runs internal-only periodic tasks that read and write only this database — client_contact_index is one (review point: test_periodic_recovery_mode)"]
next_task: "Owner: search semantics for slice (b). L17-24 (bounded sealing batches) done."
updated: 2026-10-15
```
Why a side table: see the first tests_run entry. Freshness: the index may be missing (stale) but never wrong — a new client's row is created stale by a statement-level AFTER INSERT trigger, an UPDATE OF full_name/email/phone that changes a value marks it stale (row-level), and the filler reads its batch `FOR SHARE OF c SKIP LOCKED`, so an edit of the same client waits for the fill to commit and then re-marks it. The filler is `backend/client_contact_index.py` (periodic task `client_contact_index`, internal-only, `ORACLE_CLIENT_CONTACT_INDEX_*`; operator use in docs/operator-scripts.md). Erasure: rows go with their client (FK cascade) and their brokerage (privacy_data_map: erase).

#### L17-24 Sealing batches really are bounded (A01 follow-up)
```yaml
id: L17-24
status: tests_pass
env: local
evidence_shas: [0944dc4]
files: [backend/db/migrations/0157_sealing_batches_bounded.sql, backend/backfill_client_notes.py, backend/backfill_sealed_text.py, backend/tests/test_sealing_batches_bounded_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "every CI rls-job live test after L17-23 (local PostgreSQL 16)", result: "test_client_note_sealing_live failed twice in a row, only after test_client_contact_index_live: seal_pending(batch_size=1, max_rows=2) sealed 3 notes in ONE batch (log: 'sealed 3 note(s)'); alone it passes", date: 2026-10-15}
  - {cmd: "EXPLAIN of backfill_client_notes._SEAL on that database", result: "Update -> Nested Loop Semi Join: outer Seq Scan on client_notes, inner Subquery Scan -> Limit -> LockRows -> Sort. The locking subquery re-runs per outer row; rows this statement already updated are skipped by LockRows, so each re-run returns the next ones and the LIMIT bounds each run, not the statement. Whether it happens depends on table statistics and scan order", date: 2026-10-15}
  - {cmd: "grep of WHERE ... IN (SELECT ... LIMIT ... FOR UPDATE SKIP LOCKED) over backend/*.py and pg_proc", result: "backfill_client_notes (2 statements), backfill_sealed_text (10), and the SECURITY DEFINER seal_negotiation_transcripts, seal_user_interactions (0153) and seal_interaction_logs (0154), whose 'at most 5,000 rows a call' bound is a stated safety property. reconciliation.py's LIMIT subqueries take no lock (a re-run reads the same snapshot), unchanged", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_sealing_batches_bounded_live.py (nested loop forced: enable_hashjoin/mergejoin/material off) BEFORE 0157", result: "failed: seal_user_interactions(tenant, key, 2) changed 12 rows (all of them); the fixed Python statements already changed exactly 2", date: 2026-10-15}
  - {cmd: "run_migrations.py (0157) + the same test; information_schema.routine_privileges", result: "1 passed: every batch changes exactly its limit (client notes seal/unseal; seal_user_interactions and seal_interaction_logs, seal and unseal); every direct statement in backfill_sealed_text picks in a MATERIALIZED CTE with one SKIP LOCKED; EXECUTE grants unchanged (oracle_app). The old-shape control is reported, not asserted: it over-runs only when the outer scan meets rows in id order", date: 2026-10-15}
  - {cmd: "every CI rls-job live test (3 runs); SQL proofs; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "65 passed x3 (test_client_note_sealing_live passes after test_client_contact_index_live); SQL proofs exit 0; unit 5193 passed, 62 skipped, 2 failed (test_media_storage + test_object_storage: the container runs as root, identical without the change); bandit exit 0", date: 2026-10-15}
authz_checked: "No access change. The three functions keep their signatures, SECURITY DEFINER, search_path, tenant check and 1..5000 limit check; bodies differ only in picking the batch in a MATERIALIZED CTE."
blockers:
  - "Not deployed: pushing to main deploys staging (C4.4). On staging the periodic sealers have already sealed everything (L17-22 counts), so the over-run had no data effect there; the fix restores the bound for future backlogs."
  - "Not run: scripts/rehearse-migrations-nosuperuser.sh (needs the owner's docker setup). 0157 only replaces three functions the same owner already owns."
audit_refs: [A01]
decision_refs: []
next_task: "Deploy with approval together with 0155-0156."
updated: 2026-10-15
```

#### L17-25 The comms rollup is one pass over the touches (L17-22 defect)
```yaml
id: L17-25
status: tests_pass
env: local
evidence_shas: [6380735]
files: [backend/crm.py, backend/tests/test_comms_threads_live.py, backend/tests/test_crm_workflows.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read crm.comms_threads at 6380735", result: "two correlated LATERAL scans of the whole touches CTE per client (newest touch, count): quadratic in brokerage size (L17-22 defect)", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_comms_threads_live.py (real PostgreSQL, request login; 300 clients x 6,000 touches, 40 clients also reached through a house-lead) with crm.py stashed, then restored, then 3 more runs", result: "before: 1 failed (new ~= old: 230 vs 255 ms); after: passed every run, old control 229-257 ms vs new 39-42 ms, and identical clients, order, counts and newest touch", date: 2026-10-16}
  - {cmd: "cd backend && pytest tests/test_crm_workflows.py and the other comms_threads unit tests + test_search_api.py", result: "27 passed (the shape test now pins the grouped LEFT JOIN that keeps untouched clients)", date: 2026-10-16}
authz_checked: "No access change: same tables, same caller tenant_tx; sealed columns are still read only for the newest touch of the <=200 returned clients and opened by sealed_text.open_fields."
blockers:
  - "The timing is a local bench (C9.3 conditions: dev PostgreSQL 16, one connection, warm cache), not a staging measurement."
  - "The rollup still groups every touch of the brokerage each call (linear now); a summary table or keyset paging is the next step if brokerages grow far past this bench."
  - "Not deployed (C4.4)."
audit_refs: [A01, A10]
decision_refs: []
next_task: "Owner: client search semantics (L17-23 slice b); the roll-up DELETE grant decision (L17-22)."
updated: 2026-10-16
```
Effect of the defect: a periodic or operator sealing pass could change more rows per statement than its batch size (up to the whole tenant's backlog in one transaction), breaking the per-run bound and holding row locks longer — never a wrong value. Fix: `WITH picked AS MATERIALIZED (SELECT id ... ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED) UPDATE ... WHERE id IN (SELECT id FROM picked)`; a CTE with a locking clause runs exactly once.

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

#### L18-05 A brokerage cannot hold more active seats than its plan (0161; T18.04, AC18.02)
```yaml
id: L18-05
status: tests_pass
env: local
evidence_shas: [3bd704d]
files: [backend/db/migrations/0161_seat_limit.sql, backend/seats.py, backend/auth.py, backend/brokerage_onboarding.py, backend/tests/test_seat_entitlements_live.py, .github/workflows/ci.yml]
tests_run:
  - {cmd: "grep -n -i seat over auth.accept_invite, brokerage_onboarding (invite, reinstate), signup at 3bd704d", result: "no seat ceiling anywhere; invitations check membership only; nothing serialised two acceptances", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_seat_entitlements_live.py with 0161's triggers dropped, then re-applied", result: "before: 1 failed (a second activation for the last seat did not wait and both committed: 21 seats); after: 1 passed", date: 2026-10-16}
  - {cmd: "test_seat_entitlements_live.py (real PostgreSQL, application role)", result: "seat_limit 20 with 19 used: two racing activations commit exactly one (the second waits on the brokerage lock, then NSEAT); the 21st accept_invite answers 409 and the invitation stays pending; suspension frees a seat which an acceptance then takes; reinstating past the limit answers 409; a profile edit is never a new seat; no limit, no ceiling; a broker-owner's UPDATE of seat_limit is refused (42501) while a platform admin's applies", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_accept_invitation.py tests/test_brokerage_onboarding*.py tests/test_migration*.py tests/test_run_migrations*.py", result: "160 passed, 1 skipped", date: 2026-10-16}
  - {cmd: "full CI live list (.github/workflows/ci.yml:257) against the local migrated database", result: "70 passed (after re-recording 0161 locally: it had been edited after a first local apply, which test_migration_precheck rightly refused)", date: 2026-10-16}
authz_checked: "Database-level: users_seat_limit (SECURITY DEFINER, search_path pinned, REVOKE ALL FROM PUBLIC) counts only the written row's brokerage; tenants_seat_limit_guard refuses seat_limit changes on any application login (member of oracle_app, not superuser) unless app_is_platform_admin(). Proven under the application role."
blockers:
  - {type: owner, note: "seat_limit is NULL for every brokerage, so nothing is enforced until the plan is decided (D-2026-10-12-02, T18.02): the number, whether the broker-owner counts (today: every active non-platform-admin user counts, owner included) and who sets it (a platform admin, today by SQL; no route)."}
  - "Lowering a limit below the active count removes no one; it blocks further activations until seats are freed."
  - "Stripe seat quantity (proration, upgrade/downgrade) is not wired: T18.04's checkout half waits on the owner's plan contract (T18.02) and Stripe test mode."
  - "Not deployed (C4.4): 0161 is local; staging needs the push."
audit_refs: []
decision_refs: [D-2026-10-12-02]
next_task: "T18.05 Stripe event ordering and replay (AC18.03), test mode only; owner: plan contract (T18.02)."
updated: 2026-10-16
```
Owner-approved workaround (2026-10-13): a staging-only subscription row for the owner's own brokerage (`staging_comp_sub_2dd07d9c`, active to 2027-10-13), written the way the demo brokerage is entitled. It also opens the spend-gated routes for that tenant on staging.

#### L18-06 Stripe events reconcile in order: a cancelled plan stays cancelled, an early deletion retries, one checkout per brokerage, a second live plan is flagged (0169; T18.05, AC18.03)
```yaml
id: L18-06
status: tests_pass
env: local
evidence_shas: [3a8f635]
files: [backend/billing.py, backend/db/migrations/0169_billing_checkout_sessions.sql, backend/privacy_data_map.py, backend/operator_api.py, backend/tests/test_stripe_subscription_state_live.py, backend/tests/test_billing_checkout.py, backend/tests/test_stripe_webhook_sdk_objects.py, backend/tests/test_operator_api.py, docs/account-offboarding-runbook.md, docs/privacy-data-map.md, oracle-app/src/components/BillingOverlay.jsx, oracle-app/src/components/BillingOverlay.test.jsx, oracle-app/src/components/admin/OperatorOverview.jsx, oracle-app/src/components/admin/OperatorOverview.test.jsx, .github/workflows/ci.yml]
tests_run:
  - {cmd: "re-verify at 3a8f635 (workflow wf_b044c614-b65: implementer -> reviewer -> fixer -> verifier)", result: "invoice.paid (billing.py:723) and customer.subscription.updated (:771) guarded only on last_event_created <=, so a same-second or later event revived 'canceled' (a dunning invoice paid weeks later reactivated the plan); customer.subscription.deleted (:787) committed its event id for an unknown subscription and answered 200, so the deletion was lost and the late checkout stored 'active'; the checkout idempotency key was a ten-minute wall-clock bucket with no customer=; a second live subscription for one brokerage was stored silently and require_active, status and portal read only the newest row", date: 2026-10-17}
  - {cmd: "red runs: livetest pytest tests/test_stripe_subscription_state_live.py; pytest tests/test_billing_checkout.py (before the slice); vitest BillingOverlay / OperatorOverview and the lock-removal checks (before the review fixes)", result: "11 failed / 1 passed live (deleted-before-checkout 200 not 503; same-second and later invoice.paid / updated and a late checkout ended 'active'; incomplete_expired revived; no anomaly for a second live plan; 402 while an older plan was active; two sessions across a ten-minute boundary; no 409 while a first payment was pending); 4 failed unit; review: an 'unpaid' / 'incomplete' / 'paused' owner was refused checkout (409) with no portal on the modal overlay (4 vitest red), both new advisory locks could be deleted with every test still green (now: 'assert 0 == 1' and 'assert 3 == 1', 3 of 3 runs each), and the duplicate alert dropped out of a 50-row anomaly list behind 60 access-denial rows", date: 2026-10-17}
  - {cmd: "migration 0169 applied to local PostgreSQL 16 and recorded (sha256 50d6740c...f813); livetest pytest tests/test_stripe_subscription_state_live.py tests/test_stripe_webhook_live.py tests/test_billing_degradation_live.py tests/test_suspension_cancels_spend_live.py tests/test_seat_entitlements_live.py tests/test_migration_precheck.py (FORCE RLS, app login; added to the CI live list)", result: "34 passed on the integrated head: 'canceled' and 'incomplete_expired' are terminal in invoice.paid, subscription.updated and the checkout upsert (a late invoice.paid is still recorded in the event ledger); a deletion before its checkout is a 503 + Retry-After and lands on redelivery; checkout hands back the brokerage's stored session under a per-brokerage advisory lock (key: brokerage, replaced session, request digest) and reuses the stored customer; any non-terminal plan blocks a second checkout with 409 naming Manage billing; a second live subscription keeps both rows, writes a high-severity second_live_subscription alert and is listed first under the operator's billing exceptions until one ends", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py; cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check (integrated head with L18-07; this slice alone built within budget at 4149.0 KB)", result: "5571 passed, 105 skipped; 1005 passed; eslint exit 0; built; budget re-pinned in L18-07", date: 2026-10-17}
authz_checked: "billing_checkout_sessions: tenant_id primary key, FORCE RLS with the indexable tenant wall, oracle_app SELECT/INSERT/UPDATE with DELETE revoked, classified capability_token / erase in the privacy data map. Checkout still requires the brokerage's own context; the webhook still verifies Stripe's signature and dedups on event id. The operator duplicate list is read-only and skips the platform tenant. Nothing is cancelled or refunded automatically."
blockers:
  - "AC18.03 asks for Stripe test mode: every event here is synthetic and signed locally, and checkout ran against an offline stand-in (customer=, customer_update, idempotent replay and expires_at are unproven against Stripe). Needs the owner's test-mode keys (L18-04)."
  - "Not fixed: the first period's end stays NULL in Stripe's real signup order (invoice.paid / subscription.updated before checkout.session.completed are retried, then dropped as stale); an event Stripe stops retrying leaves no durable trace (in-memory counters, no dead-letter table); the usage drain relies on Stripe's 24 h identifier dedup; rows left wrong by the old bugs are not reconciled against Stripe, and entitlement now trusts any live row."
  - "Other readers still take the newest subscription row (brokerage_onboarding._billing_status, operator diagnostics, privacy_lifecycle cancel-at-period-end, billing_usage customer lookup); /billing/status prefers entitled rows, then the newest, so an older 'unpaid' plan under a newer 'canceled' one reads 'canceled'. The duplicate alert has no resolved state. Checkout holds a pooled connection and the lock while Stripe answers (a second click waits up to lock_timeout, then 503)."
  - "Migration 0169 is unapplied outside the local database; not deployed (C4.4)."
audit_refs: []
decision_refs: []
next_task: "AC18.07 landed in L18-07; AC18.03 in Stripe test mode once the owner sets the keys (L18-04)."
updated: 2026-10-17
```

#### L18-07 A failed or ended payment keeps reads and export and stops new outbound sends on every path (T18.05, F18.07, AC18.07)
```yaml
id: L18-07
status: tests_pass
env: local
evidence_shas: [3a8f635]
files: [backend/billing.py, backend/crm.py, backend/email_outbox.py, backend/commands_api.py, backend/missions/executor.py, backend/automation_jobs.py, backend/tests/test_billing_degradation.py, backend/tests/test_billing_degradation_live.py, backend/tests/test_email_outbox.py, oracle-app/src/components/BillingOverlay.jsx, oracle-app/src/components/BillingOverlay.module.css, oracle-app/src/components/BillingOverlay.test.jsx, oracle-app/src/components/CrmShell.jsx, oracle-app/src/components/CrmShell.test.jsx, oracle-app/src/state/useSubscription.js, oracle-app/src/state/useSubscription.test.js, oracle-app/bundle-budget.json, .github/workflows/ci.yml]
tests_run:
  - {cmd: "re-verify at 3a8f635 (workflow wf_b044c614-b65: implementer -> reviewer -> fixer -> verifier)", result: "F18.07 held both ways: POST /api/crm/clients/{id}/messages (crm.py:1387) emailed a client with no entitlement check; email_outbox.deliver and commands_api._execute_command_job never re-read the subscription, so sends queued while paying went out after a cancellation; a mission's self-release reached release_command past the Approve route's gate; and the billing overlay was a modal with no exit for every non-entitled status, so a lapsed brokerage could neither read nor export its records", date: 2026-10-17}
  - {cmd: "red runs: livetest pytest tests/test_billing_degradation_live.py (subscription moved only by signed synthetic Stripe events, ORACLE_BILLING_ENFORCED=1); vitest BillingOverlay.test.jsx; then each review fix and leftover", result: "4 failed live (composer 201 and queued; outbox 'sent' not 'blocked'; the command reached the relay; release did not raise); 5 failed vitest (no 'records are still here', no 'Continue to your records'). Review: a paid brokerage re-checking out by bank debit read as never paid and was locked again; a platform admin's composer email returned 201 'queued' for a send the worker then blocked; the copy said sends were 'paused' while queued ones are cancelled for good. Verifier leftovers (lead): an 'incomplete' first payment was told to fix the card, a refused checkout (409) never re-read a stale status, and the copy did not say a draft awaiting approval stays a draft (3 vitest red)", date: 2026-10-17}
  - {cmd: "livetest pytest tests/test_billing_degradation_live.py tests/test_stripe_subscription_state_live.py tests/test_stripe_webhook_live.py tests/test_suspension_cancels_spend_live.py tests/test_seat_entitlements_live.py tests/test_migration_precheck.py (real PostgreSQL 16, app login; added to the CI live list)", result: "34 passed: with the subscription canceled the composer refuses outbound email (402) before any write and an admin gets the same refusal up front; a queued email becomes failed 'blocked: ...' with nothing at the relay; a queued or scheduled command fails 'Not sent' (billing.NotEntitled, job_terminal, dead-lettered); Approve and a mission's release both refuse and the command stays awaiting approval; inbound logging, notes and reads stay open; past_due still sends (current policy); /billing/status ever_entitled is true after cancel -> unpaid re-checkout -> incomplete_expired and false for a first checkout never paid", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py; cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check", result: "5571 passed, 105 skipped; 1005 passed; eslint exit 0; built; budget re-pinned (+3.3 KB total JS over the old pin for the lapsed dialog, the in-flow notice and their copy; largest chunk unchanged)", date: 2026-10-17}
authz_checked: "billing.entitlement_refusal is the one reading behind require_active_subscription (route behaviour unchanged) and now also runs in the composer, release_command and both send workers, for every caller including a platform admin; an unreadable status raises 503 (held and retried, never passed or finally blocked). Export stays owner-only on the server (_owner(ctx)); the overlay only offers it. No migration."
blockers:
  - {type: owner, note: "Not decided: whether hosted AI inference is billable or gated for a lapsed brokerage (AI chat over /ws, generate-cma, comms/draft, commands/parse are not gated), and how long past_due stays entitled (unbounded today; D-2026-10-12-02)."}
  - {type: owner, note: "Not decided: whether a lapsed brokerage may keep editing records (the server never gated edits; the copy says records stay open), and whether an unverifiable status (overlay 'error' mode) should allow continuing (it stays fully locked)."}
  - "The CRM thread still shows a composer email as 'queued' after the worker blocked it (no agent-facing read of email_outbox.status, for any block or failure), so the agent cannot see which sends to repeat. ever_entitled counts a 'canceled' row even if it never paid (reads and export of its own workspace only; the server still refuses spend). GPU, voice and video jobs queued before a cancellation are not re-checked when they run."
  - "Checked in jsdom, eslint and the build only, not on a real phone; real Stripe dunning and bank-debit timing not exercised (synthetic events). Not deployed (C4.4)."
audit_refs: []
decision_refs: [D-2026-10-12-02]
next_task: "T18.07 first channel landed in L18-08; AC18.03 in Stripe test mode once the owner sets the keys (L18-04)."
updated: 2026-10-17
```

#### L18-08 A brokerage's hosted AI tokens for Neoh chat have a cap that stops a call before it is sent, a warning, a pause and a platform kill switch (0170; T18.07, AC18.06, AC18.05)
```yaml
id: L18-08
status: tests_pass
env: local
evidence_shas: [22a7f58]
files: [backend/db/migrations/0170_ai_token_budget.sql, backend/ai_budget.py, backend/ai_budget_api.py, backend/llm_gateway.py, backend/ai_chat_agent.py, backend/ai_chat_api.py, backend/billing_usage.py, backend/server.py, backend/privacy_data_map.py, docs/privacy-data-map.md, .env.example, backend/tests/test_ai_budget.py, backend/tests/test_ai_budget_live.py, backend/tests/test_inference_metering.py, backend/tests/test_tool_offer.py, oracle-app/src/components/AiBudgetPanel.jsx, oracle-app/src/components/AiBudgetPanel.module.css, oracle-app/src/components/AiBudgetPanel.test.jsx, oracle-app/src/components/MyProfileTab.jsx, oracle-app/src/neoh/NeohConversation.jsx, oracle-app/src/neoh/NeohConversation.module.css, oracle-app/src/neoh/NeohConversation.test.jsx, oracle-app/src/neoh/animationEvent.js, oracle-app/src/neoh/avatarFacts.js, oracle-app/src/neoh/avatarFacts.test.js, oracle-app/src/neoh/useNeohAvatarState.js, oracle-app/src/state/oracleReducer.js, oracle-app/src/state/oracleReducer.aiChat.test.js, oracle-app/bundle-budget.json, .github/workflows/ci.yml]
tests_run:
  - {cmd: "scout at 22a7f58 (workflow wf_187f02ec-c92: scout -> implementer -> reviewer -> fixer -> verifier; then two more fix + verify rounds, wf_30b956bc-970 and wf_5f58b8d6-079; the last floor gap fixed by the lead)", result: "hosted model tokens had no per-tenant hard stop: llm_gateway._Counter is per-process with no tenant (T07.15 answered: it does not give per-tenant spend), only the Foundry tier wrote billing_usage_events, Bedrock's camelCase usage was not read, and tenant_action_budgets (missions cents cap) could be written by any agent's connection (AC18.05). Chosen channel: hosted model tokens spent by a Neoh chat turn, every hosted call in it (the request parse included)", date: 2026-10-17}
  - {cmd: "red runs before the slice and before each review fix: pytest tests/test_ai_budget.py tests/test_inference_metering.py; livetest tests/test_ai_budget_live.py; vitest AiBudgetPanel / avatarFacts / oracleReducer.aiChat / NeohConversation", result: "collection error (no ai_budget); 5 vitest failed. Review rounds, each red first: a photo was held at its encoded byte length (holds of over a million tokens); the direct Foundry / Fireworks / Bedrock stops could be reverted with every test green; admission and the per-call stop disagreed (the panel said 'Within budget' while every turn was refused); a cancelled thread-backed Foundry or Bedrock call released its hold while the provider kept billing, unmetered; a Bedrock PDF was held at a flat 8,000 tokens however long; the Foundry SDK kept 600 s and two retries (a call could outlive its hold); refusal and 503 copy claimed outcomes the server did not know; the admission floor left out the tool definitions, the agent's offer note, the request parse and the operator-memory block every turn carries (the last 16 unit tests and the live test fail without it)", date: 2026-10-17}
  - {cmd: "migration 0170 applied to local PostgreSQL 16 and recorded (sha256 98a6e6c4...f091); livetest pytest tests/test_ai_budget_live.py (app login, FORCE RLS; added to the CI live list)", result: "passed: an agent's write to the budget is refused by the database (42501), a broker owner's applies; the platform switch is updatable by a platform admin only; with cap = floor - 1 admission refuses AI_BUDGET_INSUFFICIENT and the smallest real turn (real _generate, real memory read under RLS) is refused at its first call before anything is sent; with cap = floor it is admitted and its reported usage settles to the brokerage's ledger", date: 2026-10-17}
  - {cmd: "cd backend && pytest tests/ --deselect tests/test_media_storage.py --deselect tests/test_object_storage.py; cd oracle-app && npx vitest run && npx eslint src && npm run build && npm run bundle:check (integrated head with L12-04)", result: "5812 passed, 107 skipped; 1047 passed; eslint exit 0; built; budget re-pinned (AiBudgetPanel is lazily loaded; the chat's warning line and the budget avatar reason are in the main bundle; largest chunk unchanged)", date: 2026-10-17}
authz_checked: "Budget routes: GET/PUT /api/billing/budgets/ai-tokens for a broker owner or platform admin (an agent gets 403), GET/PUT /api/admin/ai-spend for a platform admin only; a platform admin acts in the brokerage's own context so RLS alone scopes it. 0170's SECURITY DEFINER trigger refuses budget writes from any application login that is not a broker owner or platform admin (also closing AC18.05 for the missions cents cap); platform_spend_controls is readable by all and updatable by a platform admin only under RLS; ai_token_reservations has FORCE RLS with the indexable tenant wall. Changes are audited with old and new values; logs carry the tenant id only."
blockers:
  - {type: owner, note: "Budget numbers are owner decisions (D-2026-10-12-02): no brokerage has a cap by default and none ships in code; the 80% warning is a UI default. Whether AI chat is also gated after a lapsed payment is the L18-07 question."}
  - "One T18.07 channel only. Not built: voice seconds, carrier minutes, SMS, email, property-data requests, GPU, ads, publishing, per-agent caps, forecast, owner-approval threshold, reconciliation against provider invoices. Hosted calls outside chat turns (inbound voice replies, missions, client_ai_automation, commands /parse, comms/draft, generate-cma) are neither held nor metered."
  - "The hold is an estimate (chars / 3 plus the completion ceiling; 8,000 per image; a PDF by its text): a call whose reported usage beats it overshoots by the difference. A call with no reported usage, or cut off before it answers, is not counted; a thread-backed call is held until its thread returns but at most HOLD_SECONDS (600 s). The admission floor is a lower bound (exact for the smallest turn of an operator with an empty id and no brokerage block); a larger turn is stopped at its first call with 'too little left for this request'."
  - "The platform kill switch has an API and no UI. Not checked against a live provider; not deployed (C4.4); 0170 unapplied outside the local database."
audit_refs: []
decision_refs: [D-2026-10-12-02]
next_task: "Meter and hold the hosted calls outside chat turns (voice replies, missions, automation) under the same budget; then the next T18.07 channel (carrier minutes or SMS)."
updated: 2026-10-17
```

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
next_task: "Health done in L19-04/05; runbook done in L19-06."
updated: 2026-10-16
```

#### L19-04 Health reports the real property sources (T19.07 / AC19.07, part)
```yaml
id: L19-04
status: tests_pass
env: local
evidence_shas: [8339a3b]
files: [backend/component_health.py, backend/tests/test_component_health.py, backend/tests/test_component_health_live.py, oracle-app/src/components/admin/operatorModel.js, .github/workflows/ci.yml]
tests_run:
  - {cmd: "read component_health.py; \\d harvest_sources, state_market_stats; grep avm_client for RentCast/ATTOM (at c2eae55)", result: "health had a legacy MLS component (NOT_CONFIGURED since MLS is off) and nothing for the sources property answers come from (L19-03). harvest_sources records enabled, schedule, last success, failures and circuit state; state_market_stats carries updated_at (market_research task, daily by default); RentCast/ATTOM call outcomes are recorded nowhere", date: 2026-10-15}
  - {cmd: "cd backend && pytest -q tests/test_component_health.py tests/test_health_readiness.py", result: "16 passed: harvesters — none enabled NOT_CONFIGURED, open circuit DEGRADED, overdue (> 2x schedule + 1 h) STALE; market data — none NOT_CONFIGURED, older than 3 refresh intervals STALE; in recovery mode overdue/stale report 'paused (recovery mode)' as NOT_CONFIGURED (no page) but a tripped circuit stays DEGRADED; a valuation key alone is UNKNOWN, never HEALTHY, counts only; an UNKNOWN optional component does not degrade the overall state, a STALE one does; no database -> UNKNOWN", date: 2026-10-15}
  - {cmd: "pytest -q tests/test_component_health_live.py (real PostgreSQL, platform role)", result: "1 passed: the new queries run on the real schema; an enabled source with an open circuit makes property_harvesters DEGRADED", date: 2026-10-15}
  - {cmd: "every CI rls-job live test; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit; cd oracle-app && npx vitest run src/components/admin && npm run lint", result: "69 passed; unit 5213 passed, 66 skipped, 2 failed (test_media_storage + test_object_storage: container runs as root, identical without the change); bandit exit 0; admin vitest 12 passed; eslint exit 0", date: 2026-10-15}
authz_checked: "Aggregates only (counts and ages) in the platform health context, as the other components; no source names, error text or customer data in the detail."
blockers:
  - "AC19.07 is not met for RentCast/ATTOM: their 401/503 outcomes are not recorded, so health says UNKNOWN; recording provider outcomes (and a failure-injection test per provider) is the next step."
  - "AC19.06 (an alert per SLO that reaches a named person) is not done: these components page through the existing ops_alerts path only when STALE/DEGRADED; SLO targets are an owner decision."
  - "Not deployed (C4.4)."
audit_refs: [A04]
decision_refs: [D-2026-10-12-01]
next_task: "Done in L19-05 (valuation outcomes recorded). Then the remaining T19.07 SLOs with owner targets."
updated: 2026-10-15
```

#### L19-05 RentCast / ATTOM outcomes are recorded, so health observes them (AC19.07 for valuation)
```yaml
id: L19-05
status: tests_pass
env: local
evidence_shas: [685228b]
files: [backend/db/migrations/0160_provider_health.sql, backend/provider_health.py, backend/avm_client.py, backend/component_health.py, backend/privacy_data_map.py, backend/tests/test_component_health.py, backend/tests/test_component_health_live.py, backend/tests/test_avm_credential_cooldown.py]
tests_run:
  - {cmd: "cd backend && pytest -q tests/test_avm_credential_cooldown.py tests/test_component_health.py tests/test_health_readiness.py", result: "15 + 29 passed. Failure injection per provider through the real _value_property_uncached with a fake HTTP session: RentCast 401 -> recorded credential_rejected -> health AUTH_FAILED; RentCast 429 -> rate_limited -> RATE_LIMITED; RentCast 503 -> http_error -> HEALTHY after one, DEGRADED after three in a row; ATTOM 401 and 503 the same; an unconfigured provider records nothing. Mapping from every avm source_status entry; recording never raises without a database", date: 2026-10-15}
  - {cmd: "run_migrations.py (0160) + pytest -q tests/test_component_health_live.py (real PostgreSQL)", result: "1 passed: two credential_rejected records -> consecutive_failures 2 -> valuation_providers AUTH_FAILED; one ok -> 0, last_ok_at set -> HEALTHY", date: 2026-10-15}
  - {cmd: "every CI rls-job live test; SQL proofs; cd backend && pytest -q tests compliance_engine/tests (clean env); bandit", result: "69 passed; SQL proofs exit 0; unit: first run failed test_privacy_data_map (provider_health unclassified) — classified OPERATIONAL/global; then 44 passed for the map + migration ledger and test_privacy_lifecycle_live passed; remaining unit failures only test_media_storage + test_object_storage (container runs as root); bandit exit 0", date: 2026-10-15}
authz_checked: "provider_health is platform reference data: a provider name, timestamps, a fixed failure kind, a counter — no tenant, address or error text (privacy_data_map: OPERATIONAL, global). Written in the platform context; oracle_app may SELECT/INSERT/UPDATE it."
blockers:
  - "Outcomes are recorded only for valuation calls made through avm_client._value_property_uncached (the only RentCast/ATTOM caller found). Harvester outcomes come from harvest_sources (L19-04)."
  - "AC19.06 (each SLO alert reaching a named person) still needs owner targets and an on-call owner."
  - "Not deployed (C4.4)."
audit_refs: [A04, A02]
decision_refs: []
next_task: "Remaining T19.07 SLOs once the owner sets targets."
updated: 2026-10-15
```

#### L19-06 Property-data runbook; the platform email relay in health (T19.07 part, AC19.07)
```yaml
id: L19-06
status: tests_pass
env: local
evidence_shas: [f251145]
files: [backend/component_health.py, backend/tests/test_component_health.py, oracle-app/src/components/admin/operatorModel.js, docs/runbooks/property-data-down.md, docs/runbooks/README.md, docs/runbooks/email-provider-down.md]
tests_run:
  - {cmd: "ls docs/runbooks; grep component_health for platform_smtp at f251145", result: "only the legacy MLS runbook covered property data (L19-03); the relay outcome recorded since L10-11 was not in health", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_component_health.py with component_health.py stashed, then restored; live test_component_health_live.py", result: "before: 6 failed; after: 22 passed (relay UNKNOWN with no send, HEALTHY after one, AUTH_FAILED on a refused login, DEGRADED on other failures, NOT_CONFIGURED without settings; a RentCast row never counts for it); live 1 passed", date: 2026-10-16}
  - {cmd: "run the runbook's two SQL queries against the local migrated database", result: "both run (the first draft named columns that do not exist; corrected to harvest_sources' real ones)", date: 2026-10-16}
authz_checked: "n/a: an optional health component reading the platform-global provider_health table; runbooks."
blockers:
  - "SLO targets, alert routing and the on-call owner (T19.07 rest, AC19.06) are owner decisions; nothing here pages anyone."
  - "No failure-injection run on staging per provider (AC19.07 evidence)."
audit_refs: [A04]
decision_refs: [D-2026-10-12-01]
next_task: "Owner: SLO targets + on-call owner; then AC19.06 test alert per SLO."
updated: 2026-10-16
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

#### L20-02 One launch backlog, read from every ledger row (T20.02, AC20.02)
Backlog: [LAUNCH-BACKLOG.md](LAUNCH-BACKLOG.md) (generated).
```yaml
id: L20-02
status: tests_pass
env: local
evidence_shas: [c35af6c]
files: [scripts/build-launch-backlog.py, docs/claude-prompts/LAUNCH-BACKLOG.md, backend/tests/test_launch_backlog.py, docs/claude-prompts/LEDGER.md]
tests_run:
  - {cmd: "python3 scripts/build-launch-backlog.py; --check", result: "111 ledger rows, all open (production_verified: 0), each once under the README build order with status, env, who unblocks it (owner 70, deploy 45, engineering 38, device 17; a row can need several) and next step; A01-A10 listed", date: 2026-10-16}
  - {cmd: "found while building it", result: "two ledger defects fixed: L17-06's YAML block had no updated: line or closing fence, so L17-08 sat inside it (any reader of the ledger missed L17-08); L09-03 had a tests_run entry after authz_checked (invalid YAML), introduced earlier in this session", date: 2026-10-16}
  - {cmd: "cd backend && python -m pytest tests/test_launch_backlog.py", result: "3 passed: every heading parses and appears once; nothing short of production_verified is closed; the committed backlog is current", date: 2026-10-16}
authz_checked: "n/a: a generated reading aid; the ledger stays the only status record (C10)."
blockers:
  - "'Who unblocks' is read from each row's blockers and next step with keyword rules (owner / deploy / device / engineering); a row worded vaguely can be misfiled. The row itself is authoritative."
  - {type: owner, note: "T20.03 + AC20.03: the owner's launch scope (which rows must be production_verified for the 20-agent launch)."}
audit_refs: []
decision_refs: [D-2026-10-12-01, D-2026-10-12-02]
next_task: "Owner: launch scope (T20.03), read from LAUNCH-BACKLOG.md."
updated: 2026-10-16
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
| A01 | Customer PII and communications in plaintext | P0 | open; the audit missed the AI call notes (`aec14c4` predates its pin). 2026-10-13: client notes sealed for new writes (0132, L17-03, local); old rows await the backfill. 2026-10-14: SMS bodies, call transcript excerpts and memory-core chat turns sealed for new writes (0152, L17-21, local, expand step); old rows await `backfill_sealed_text.py`; `clients` contact fields and `leads.payload` designed in L17-21, not built. 2026-10-15: email subjects/bodies, interaction-log subjects and text, AI tool receipts, belief source quotes and profile summaries sealed for new writes (0154, L17-22, local, expand step); old rows await `sealed_text_seal` / `backfill_sealed_text.py` | `docs/privacy-data-map.md:40`, `:42`, `:48`, `:71`; `backend/commands_api.py:2335` (`deliver_call_notes` → `client_notes.body`) | 17 | L17-03, L17-21, L17-22, L09-02, L09-03 |
| A02 | Failed property lookup reads as zero matches | P0 | fixed in code at `d934b2e`, local tests only (2026-10-13, L11-02); not deployed, migration 0135 unapplied. 2026-10-13: the non-property sites listed in L11-02 (geocoders, eviction, enrich-property, market-snapshot, AVM, building footprints) fixed locally, `tests_pass` (L11-03, unpushed branch, no migration); staging check owed | was `backend/client_ai_automation.py:700`, `:704`; now `_property_candidates_bounded` returns per-source status (`:727`) and a failed lookup degrades the reconcile (`:1079`) | 11 | L11-02, L11-03, L06-01, L05-01 |
| A03 | Professional 3D certification without verified evidence | P0 | negative half fixed in code, `tests_pass` local (2026-10-17, L12-04): a derived certification state (pending / incomplete / degraded / rejected / verified) needs fresh held-out quality, registration, room connectivity and real-device evidence, and every consumer's label reads it, so nothing can say verified, professional or Full 3D without it; not deployed. Positive certification still unbuilt (no room-graph or device-run producer) | was `backend/recon_quality.py:83`, `:101`; `backend/reconstruction_worker.py:581`; badge `backend/tour_api.py:41`, `:335`; now `backend/space_certification.py` and its consumers | 12 | L12-01, L12-04 |
| A04 | Production readiness not established by staging CI | P0 | open (no production app) | `.github/workflows/ci.yml:606` (`promote` manual only) | 19 | L19-02 |
| A05 | Usage event counted before approved/delivered outreach | P1 | fixed in code (`a8bff54`), `tests_pass` local (2026-10-13), not deployed: metered only at delivered (0133); owner to confirm the billable stage | was `backend/speed_to_lead.py:387-389`; now `backend/outreach_lifecycle.py` (`BILLABLE_ENGAGEMENT_STAGE`), `backend/billing_usage.py` (`record_lead_engagement`, `BILLABLE_PREDICATE`) | 18 | L18-02, L04-01 |
| A06 | Customer names in failure logs | P1 | open. 2026-10-13: fixed with a source guard, local (L17-04); addresses added 2026-10-14; agent emails (actor_ref) and exception text (handler scrub) added 2026-10-14, local (L17-11) | `backend/client_ai_automation.py:701-703` logs `full_name` | 17 | L17-04 |
| A07 | Concurrent messaging route initialization | P1 | fixed in code at `c52b4dc`, local real-PostgreSQL race test only (2026-10-13, L10-06); not deployed, migration 0136 unapplied outside the local dev DB; 10DLC brand/campaign re-submission fixed in code at `00f94f8`, local real-PostgreSQL race test only (L10-09, 0139 unapplied outside the local dev DB); campaign number assignment fixed in code, local real-PostgreSQL race test only (L10-10, 0145 unapplied outside the local dev DB) | was `backend/messaging_data.py:108` (`ON CONFLICT … DO UPDATE SET provider_account_id`); now `ON CONFLICT … DO NOTHING` + re-read, and the hosted order goes through `provider_purchases` (`_claim_hosted_order`) | 10 | L10-06 |
| A08 | Heuristic valuation targets in model training | P1 | open in code. 2026-10-17: labelled and gated, `tests_pass` local (L11-05, review fixes L11-06): the trainer, its manifest and model card and the CoT generator say the targets are heuristic (the false "licensed" kind and the generator's model claim are gone), a state underwriter is activated only with observed-transaction targets and published per-region error and uncertainty, a decided version cannot be swapped, and EdgeUnderwriter serves no state adapter without an observed-targets card. The constants remain, provenance is self-declared, and the six tracked training_data/*.jsonl files predate the fix (README added) | `backend/ml_forge/edge_forge/train_lora.py:211` (`_COUNTY_PPSF`), `:213` (`_MAO_RULE = 0.70`); gate in `backend/models_api.py:81` (`valuation_promotion_problem`); serving guard in `backend/ml_forge/edge_forge/local_vllm_adapter.py` (`_require_observed_targets`) | 11 | L11-05, L11-06 |
| A09 | AI capability descriptions exceed executable tooling | P1 | allowlist re-verified; UI and persona wording not re-verified. **2026-10-13 (base `3bcab84`): fixed locally, `tests_pass` (L07-01)** — one registry derived from the allowlist drives the persona, the API and the Personal AI page; a guard test fails on any unbacked claim. Playwright check still owed | allowlist at `backend/ai_chat_store.py:605`, `:646`; 126 catalogued vs 57 allowlisted tools (L07-01); registry `backend/ai_capabilities.py`, guard `backend/tests/test_ai_capabilities.py` | 07 | L07-01 |
| A10 | Local capacity test is not target-production evidence | P1 | open. 2026-10-13: the tenant-isolation policies are now index conditions (0141, `tests_pass` local, L16-01; undeployed) — that removes a platform-sized scan from ordinary requests but is not capacity evidence; nothing is measured on production-shaped infrastructure | `docs/capacity-plan.md:129` (nothing measured on DigitalOcean); `backend/db/migrations/0141_indexable_tenant_wall.sql` | 16 | L16-01, L16-03, L19-02 |
