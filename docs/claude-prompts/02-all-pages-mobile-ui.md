# Claude implementation prompt 02: Every page, route, mobile workflow and honest capability status

> Working repository: `ydngemini/oracle-cinematic` (shipping FastAPI backend and React/Vite browser app).
> Scope: all-pages, fully functional Neoh for **one brokerage + 20 participating agents** (plus explicit owner/admin seats).
> This is an INSTRUCTION and acceptance specification, not a declaration of implemented features, existing provider permissions, tested production, or code already changed.

## Claude: your assignment

Act as principal engineer, security reviewer and implementation owner. Start by reading root `CLAUDE.md`, `docs/neoh-full-platform-source-audit-2026-10-08.md`, the [master prompt index](README.md), and relevant category specifications. Inspect current HEAD and reference actual source lines before assuming older audit findings still apply. Implement in bounded, reviewable slices. **Do not stop at outlining plans** when code/test work is possible. Preserve authentic, accessible product behavior, responsible budgets, explicit user consent, no unauthorized external side effects, reproducible tests and durable provider receipts.

**Initial source paths (verify current reachability):** oracle-app/src/routes.js; oracle-app/src/App.jsx; oracle-app/src/components/CrmShell.jsx; oracle-app/src/neoh/UniversalWorkspace.jsx; oracle-app/src/components/OurAITab.jsx; oracle-app/src/components/SalesWorkspace.jsx; scripts/audit-neoh-production.py; scripts/test-crm-walkthrough-playwright.py

## Detailed engineering tasks

### Scope: FULL product, not three tabs or a reduced pilot
1. Inventory all present pages by parsing JSX route and tab registries, not by looking at top-level navigation alone. Include Home; Work (Recent, People, Properties, Deals, Conversations, Opportunities, Missions, AI, Sales, Social, Homeowners, Automations, Sites); Neoh; entity sheets; nested Sales Agent, Dialer, Smart Plans, Connections, Routing; and Studio.
2. Cover People/Contacts/Opportunities, property subviews Address/Houses/Listings/MLS/Market/Forms, Deals subviews Pipeline/Transactions/Contracts/Portfolio/Marketplace, 8 AI-hub workspaces, sales subroutes, personal/AI settings, brokerage setup/invites/offboarding, billing, company owner console, operator admin and every public/restricted/signed route (accept-invite, property-upload, dossier, site-preview, reel).
3. Establish a one-source-of-truth page registry with path/aliases, human label, role/capability, intended workflow, API dependencies, integrated provider, default/empty/loading/degraded/error/unauthorized states, performance target, desktop/mobile screenshots, test case IDs and acceptance proof.
4. Keep quiet three-destination top-level UX (Home/Work/Neoh); make ALL entitled functionality discoverable under useful nested navigation, search and role-relevant quick actions. Support deep link, back/forward, reload, saved session position and stale legacy URLs.
5. Do not disguise partial/unimplemented functions as live. OurAITab.jsx explicitly labels: Smart Plans visual builder partial; social scheduling/publishing/analytics disconnected; ads and cross-channel campaigns require external accounts; Customer Search App and WordPress IDX plugin not built; AEO/GEO are not implemented; SEO and back-office commission accounting partial. Implement each promised feature or keep a truthful configuration/setup state and identify as an all-features launch blocker.
6. Implement backend-delivered capability state; distinguish available, setup_required, awaiting_external_approval, unsupported, temporarily_degraded, authorized_only and tested. UI must offer next setup/recovery action, not empty buttons. Never bypass vendor/legal licensing for “all available”.
7. Build brokerage owner workspace that is separate from global AdminOpsTab. Agent sees own authorized data; team lead sees permitted team oversight; owner manages seats, sharing, policies, integrations, queues and usage; operator alone can administer global infrastructure.
8. Audit and fix modal, navigation and React focus behavior, scroll restoration, keyboard navigation, screen-reader headings, responsive breakpoints, reduced motion, iPhone notch/keyboard, overlays and speech interactions. No overlay captures hidden actions or obscures the only close/approve control.
9. Optimize initial load and per-workspace lazy imports; keep 3D/advertising/video editors out of the initial shell; preload on intent with bounded network use and suspense/error boundaries. Do not render successful empty cards when an API is down.
10. Audit all buttons/forms against an actual authorized backend outcome: create, edit, save, invite, approve, call, send, schedule, publish, export, recover and retry. Confirm session expiration does not leak private cached pages.
11. Broaden scripts/audit-neoh-production.py, which currently enumerates only Home/Work/Neoh and five Work views; recursively exercise every nested route, external capability-link, role and failure state. Use browser automation with safe fixtures rather than executing real sends/spend.
12. Plan visual QA at narrow phone width, tablet, desktop, offline/poor network, iOS Safari and Android Chrome. Test all-pages accessible navigation with 20-agent credentials and role matrices.

### Pass/fail contract
Every authorized screen must load and enable its declared legitimate journey, preserve tenant and agent permissions, handle expected empty/degraded/provider-required status, and avoid uncaught runtime errors, broken chunks, false-success messages, horizontal overflow and inaccessible controls. Role-protected views must be deliberately denied to others. Record true provider dependencies and their verified integration status.
### Deliver
ALL-PAGES-MATRIX.md + JSON page registry, complete Playwright crawl, mobile screenshots, missing-feature matrix, UI/API gap fixes and a signed page-level release verdict.

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
