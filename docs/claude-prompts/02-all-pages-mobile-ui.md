# 02 — Every page, route, mobile workflow and honest capability status

> **Scope:** Inventory every authorized page, nested workspace, entity sheet and public link in the browser app, then make each one reachable, functional, accessible, phone-ready and honest about what it can do. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01 · **Unblocks:** 14, 15, 20 · **Ledger:** [L02](LEDGER.md#l02) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Route registry and shell: `oracle-app/src/routes.js`, `oracle-app/src/App.jsx`, `oracle-app/src/components/CrmShell.jsx`, `oracle-app/src/neoh/UniversalWorkspace.jsx`
- Workspaces: `oracle-app/src/components/OurAITab.jsx`, `oracle-app/src/components/SalesWorkspace.jsx`, `oracle-app/src/components/PropertiesTab.jsx`, `oracle-app/src/components/DealsTab.jsx`
- Settings, owner and admin: `oracle-app/src/components/MyProfileTab.jsx`, `oracle-app/src/components/BrokerageSetupPanel.jsx`, `oracle-app/src/components/AdminOpsTab.jsx`, `oracle-app/src/components/BillingOverlay.jsx`
- Capability states: `backend/sales_api.py` (`CapabilityState`)
- Browser checks: `scripts/audit-neoh-production.py`, `scripts/test-crm-walkthrough-playwright.py`, `oracle-app/src/routes.test.js`, `oracle-app/scripts/check-bundle-budget.mjs`
- Targets: `docs/performance-targets.md`

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **The app has no router library; one pure module defines the address space.** `routes.js:1-20` (header comment): three views `/`, `/work`, `/neoh`, plus entity routes `/p/:id`, `/property/:key`, `/deal/:id` (`ENTITY_PREFIXES`, `:37`).
- **Work has 13 view types.** `routes.js:28-31` `WORK_TYPES`: recent, people, properties, deals, conversations, opportunities, ai, sales, social, homeowners, automations, sites, missions.
- **Old URLs and tab ids are kept as aliases.** `routes.js:42-71` `LEGACY_TAB_IDS` and `:75-95` `LEGACY_PATHS` (19 old paths).
- **Five public or capability routes render outside the auth shell.** `App.jsx:104-111`: `/reel`, `/site-preview/`, `/property-upload/`, `/vault/secure-access/` and `/accept-invite`.
- **The AI hub has eight workspaces.** `OurAITab.jsx:57-68` `WORKSPACES`: Intelligence, Cowork, Sales, Social, Homeowners, Automations, Sites, Autonomy.
- **Sales has five nested pages.** `SalesWorkspace.jsx:19-23`: Sales Agent, Power Dialer, Smart Plans, Connections, Lead Routing.
- **Deals has five sub-views.** `DealsTab.jsx:22-26`: Pipeline, Transactions, Contracts, Portfolio, Marketplace.
- **Properties has six sub-views today, one of them legacy MLS that prompt 11 gates off.** `PropertiesTab.jsx:30-37` `VIEWS`; the intended set is address, houses, listings, market and forms, and removing `mls` (`:34`, `:75`) is T11.01.
- **The AI hub still probes a legacy "MLS / IDX" capability (gated by 11).** `OurAITab.jsx:82` reads `/api/mls/health`; `:570-579` lists "MLS / IDX" and "WordPress IDX Plugin" rows.
- **OurAITab already labels partial and missing features.** Smart Plans builder partial (`:433`); commission accounting not connected (`:390`); Customer Search App not available (`:534-536`); AEO and GEO setup (`:588-597`); SEO partial (`:600-602`); social publishing disconnected (`:744`).
- **The backend capability enum has four values.** `sales_api.py:123-127` `CapabilityState`: `live`, `setup_required`, `partial`, `disabled`.
- **The Admin view is shown only when session storage says `platform_admin`.** `CrmShell.jsx:238-241` (`isPlatformAdmin`) and `:401-410` (`profileViews`: Settings, AI controls, Admin).
- **Brokerage setup lives inside personal Settings.** `MyProfileTab.jsx:7` imports `BrokerageSetupPanel`.
- **The production audit script checks only three destinations and five Work views.** `audit-neoh-production.py:47-48` (`DESTINATIONS`, `WORK_VIEWS`).

## 3. Goal

Every authorized screen loads and supports its real journey for the right roles, on desktop and phone. Each page states truthfully whether it is available, needs setup or is not built. The quiet Home / Work / Neoh shell stays, and everything else is reachable beneath it.

## 4. Engineering tasks

- **T02.01 Build the route, component, API and role matrix.** Parse the registries in §2, not the top navigation. Write one row per page to `docs/claude-prompts/ALL-PAGES-MATRIX.md` (proposed; created when 02 runs) with a JSON twin `docs/claude-prompts/all-pages.json` (proposed).
- **T02.02 Cover every page family.** Include:
  - Home; Work and its 13 types; Neoh; the three entity sheets; Studio (alias to AI);
  - People, Contacts and Opportunities; Properties sub-views address, houses, listings, market and forms;
  - Deals sub-views; the 8 AI workspaces; the 5 Sales pages;
  - Settings, AI controls, brokerage setup, invites, offboarding, billing, the owner console and operator Admin;
  - the five public routes (accept-invite, property-upload, dossier, site-preview, reel).
- **T02.03 Give each row its full contract.** Path and aliases, label, role or capability, workflow, API dependencies, provider, default/empty/loading/degraded/error/unauthorized states, performance target, desktop and phone screenshots, test IDs and acceptance proof.
- **T02.04 Keep the shell quiet but make everything findable.** Use nested navigation, search and role-relevant quick actions. Support deep links, back and forward, reload, saved position and stale legacy URLs.
- **T02.05 Resolve every partial or missing label.** For each item in OurAITab (Smart Plans, social, ads and campaigns, Customer Search App, AEO, GEO, SEO, commission accounting): build it, or keep a truthful setup state and list it as a launch blocker.
- **T02.06 Serve capability state from the backend.**
  - These are the customer-facing states a page shows. They are not ledger statuses.
  - Define them once, by extending `CapabilityState` in `backend/sales_api.py` (today `live`, `setup_required`, `partial`, `disabled`) to distinguish: `available`, `setup_required`, `awaiting_external_approval`, `unsupported`, `temporarily_degraded`, `authorized_only` and `tested`.
  - Keep the existing four values working.
  - Each state offers the next setup or recovery action.
- **T02.07 Separate the brokerage owner workspace from operator Admin.** Agents see their own authorized data; team leads see permitted team oversight; owners manage seats, sharing, policies, integrations, queues and usage; only the operator administers infrastructure. Policy comes from prompt 03.
- **T02.08 Fix focus, navigation and accessibility.** Modals, scroll restoration, keyboard paths, headings, breakpoints, reduced motion, iPhone notch and keyboard, overlays and speech controls. No overlay hides the only close or approve control.
- **T02.09 Keep the first load small.** Lazy-load per workspace; keep 3D, ads and video editors out of the shell; preload on intent with bounded network; use suspense and error boundaries.
- **T02.10 Check every button and form against a real outcome.** Create, edit, save, invite, approve, call, send, schedule, publish, export, recover and retry. Session expiry must not leave private pages in caches.
- **T02.11 Extend the production audit script into a full crawl.** Walk every matrix row, capability link, role and failure state with safe fixtures, never real sends or spend.
- **T02.12 Run visual QA across devices.** Narrow phone, tablet, desktop, offline and poor network, iOS Safari and Android Chrome, using 20-agent credentials and the role matrix.
- **T02.13 Verify whether each public route has a matching backend route and token check.** Cover the five routes in `App.jsx:104-111`.
- **T02.14 Verify whether every Admin API checks the role on the server.** The UI gate reads session storage (`CrmShell.jsx:238-241`); it is not an authorization decision.

## 5. Failure and adversarial cases

- **F02.01 Hidden page.** A page exists in a registry but no navigation, search or matrix row reaches it.
- **F02.02 False success.** A page shows a successful empty card when its API is down, or labels a disconnected integration as live.
- **F02.03 Role leak.** An owner or operator view renders for an agent, or relies on a client-side check alone.
- **F02.04 Stale private cache.** After session expiry or sign-out, a cached private page is still visible.
- **F02.05 Broken address.** A deep link, legacy URL, back or forward step, or reload lands on the wrong view or loses state.
- **F02.06 Trapped overlay.** An overlay steals focus, hides the only close or approve control, or sits under the iPhone keyboard.
- **F02.07 Runtime breakage.** An uncaught error, a broken lazy chunk or horizontal overflow at phone width.
- **F02.08 Crawl side effect.** The crawl sends, calls, publishes or spends for real.
- **F02.09 Legacy MLS resurfaces.** A legacy MLS or IDX capability row or route stays reachable after T11.01 (D-2026-10-12-01).

## 6. Acceptance criteria

- **AC02.01 The matrix covers every registry entry.** A test parses `routes.js`, `App.jsx`, `OurAITab.jsx`, `SalesWorkspace.jsx`, `DealsTab.jsx`, `PropertiesTab.jsx` and `CrmShell.jsx` and fails on a page with no matrix row. Evidence: `oracle-app/src/allPagesMatrix.test.js` (proposed), run with vitest. Covers: T02.01, T02.02, T02.03, T02.13, F02.01.
- **AC02.02 Capability states are honest.** Every capability row comes from the backend and shows its next action; none says live without a provider result. Evidence: API contract test plus component tests per state. Covers: T02.05, T02.06, F02.02, F02.09.
- **AC02.03 Roles see only their pages.** Agent, team lead, owner and platform admin each see the expected set; Admin APIs return 403 to others. Evidence: Playwright role runs plus backend 403 tests. Covers: T02.07, T02.14, F02.03.
- **AC02.04 Every address resolves.** Each `LEGACY_PATHS` entry, deep link, back, forward and reload lands correctly. Evidence: extended `routes.test.js` plus a Playwright navigation spec. Covers: T02.04, F02.05.
- **AC02.05 Pages pass accessibility and phone layout.** Automated accessibility checks, keyboard-only paths and no horizontal overflow at phone width on every matrix row. Evidence: Playwright at phone and desktop viewports, with screenshots. Covers: T02.08, T02.12, F02.06, F02.07.
- **AC02.06 The shell stays within its bundle budget.** Heavy editors load only on intent, and load p50/p95 is measured on a phone with its conditions (target, not measured, until run). Evidence: `npm run bundle:check` plus a recorded device measurement. Covers: T02.09.
- **AC02.07 Actions reach real outcomes.** Each button or form returns an authorized backend result or an honest setup state, and sign-out clears private caches. Evidence: Playwright action specs with a network log. Covers: T02.10, F02.04.
- **AC02.08 The crawl reaches every row and stays safe.** The audit script walks every matrix row, role and failure state, and a network guard blocks send and spend endpoints. Evidence: crawl report with zero blocked-call violations. Covers: T02.11, F02.08.
- **AC02.09 Each page has a signed verdict.** Each matrix row records pass, fail or setup-required with its evidence and the reviewer. Evidence: matrix diff linked from L02. Covers: T02.01, T02.12.

## 7. Out of scope, owner decisions, category-specific rules

- **The MLS sub-view is not a feature** (D-2026-10-12-01). Its removal is prompt 11's T11.01; this prompt only checks it stays gone.
- **IDX rows are out of scope** (D-2026-10-12-01): "MLS / IDX" and "WordPress IDX Plugin" in OurAITab. Owner question: remove them, or replace them with something else?
- **No router library.** Extend `routes.js`; keep every alias (`routes.js:4`).
- **Three top-level destinations stay** (Home, Work, Neoh). Depth goes underneath.
- **Neoh Space pages stay browser-only** (D-2026-10-08-01).
- **No UI libraries;** CSS Modules only ([C7.2](CONTRACT.md#c7-reuse-the-architecture)).

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - `docs/claude-prompts/ALL-PAGES-MATRIX.md` (proposed) and its JSON twin (proposed);
  - the full Playwright crawl and phone screenshots;
  - the missing-feature list inside the matrix;
  - UI and API gap fixes with tests;
  - the page-level release verdict.
- **First slice (README's starting point):** T02.01 + T02.02 + T02.03 + AC02.01, the route/component/API/role matrix. Run it alongside read-only reviews for 03, 17 and 18.
- **Ledger:** add L02-01 onward and link the matrix under L02, per [C10](CONTRACT.md#c10-ledger-update-rule).
