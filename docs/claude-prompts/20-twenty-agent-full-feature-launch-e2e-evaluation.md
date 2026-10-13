# 20 — Twenty-agent launch: end-to-end acceptance and release certification

> **Scope:** Certify, from the ledger and from end-to-end runs, whether one brokerage with an owner and 20 agents can use every authorized Neoh page and promised capability, and produce the go/no-go dossier the owner signs. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19 · **Unblocks:** — · **Ledger:** [L20](LEDGER.md#l20) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Status: [LEDGER.md](LEDGER.md) (every category row and the audit cross-reference) and [README.md](README.md)
- Launch docs: `docs/first-brokerage-launch.md`, `docs/brokerage-go-live-checklist.md`, `docs/pilot-metrics.md`, `docs/pilot-feedback.md`, `docs/performance-targets.md`
- Audits and specs: `docs/neoh-full-platform-source-audit-2026-10-08.md`, `docs/neoh-space-web-capture-claude.md`, `docs/security-launch-gate.json`
- Journey tooling: `scripts/audit-neoh-production.py`, `scripts/test-crm-walkthrough-playwright.py`, `scripts/neoh-launch-readiness.py`
- Routes and surfaces: `oracle-app/src/routes.js`, `oracle-app/src/components/OurAITab.jsx`, `oracle-app/src/components/PropertiesTab.jsx`, `oracle-app/src/components/DealsTab.jsx`
- The all-pages matrix from 02: `docs/claude-prompts/ALL-PAGES-MATRIX.md` (proposed), created when 02 runs

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **No category is production verified.** `docs/claude-prompts/LEDGER.md` § Category summary: "`production_verified` count: **0**". Its row for 20 lists "Every row above" as the open blocker.
- **No production app exists.** Ledger L19-02 (`doctl apps list`, 2026-10-07); A04 is open.
- **The security gate passed on staging only.** Ledger L17-01 (`0739563`); `docs/security-launch-gate.json:7` `"verdict": "PASS"`.
- **The all-pages matrix does not exist yet.** `ls` of the matrix path (proposed) fails; ledger L02 assigns it to 02.
- **The app has three top views and 13 Work types.** `routes.js:22` `VIEWS` (home, work, neoh); `:28-31` `WORK_TYPES`; `:99-106` `SALES_ROUTES` (six Sales paths).
- **Properties has six sub-views, one of them legacy MLS.** `PropertiesTab.jsx:31-36`; `:34` is the `mls` tab (legacy, gated by 11 under D-2026-10-12-01). Deals has five (`DealsTab.jsx:22-26`).
- **The UI sweep covers only part of the app, in one browser by default.** `scripts/audit-neoh-production.py:7-11` (docstring) visits Home, the Work views, Neoh and Admin; `--browser` defaults to `chromium` (`:342`); it sends, approves and uploads nothing (`:25-27`).
- **The go-live checklist still has a legacy MLS item.** `docs/brokerage-go-live-checklist.md:36` (item 8, "MLS authorization status"). It is now annotated as legacy (D-2026-10-12-01) and still needs a property-sources replacement (11 T11.01).
- **Invitations go out in batches of up to 25.** `brokerage-go-live-checklist.md:31` (item 3, `POST /api/brokerage/invitations`).
- **The launch runs from T-7 to T+7.** `docs/first-brokerage-launch.md:12` (T-7), `:51` (go-live, owner first), `:62` (T+1 review), `:72` (T+7 pilot review).
- **Performance targets exist as targets.** `docs/performance-targets.md:30` (work list < 800 ms) and `:48` (Neoh first token p95 < 10 s with the mock model).

## 3. Goal

The owner signs a dated go/no-go dossier that shows, page by page and journey by journey, what works for one brokerage with an owner and 20 agents, with evidence from the ledger and from real runs. A page that is only visible, a staging pass or a ledger row without evidence never counts as a launch pass (C12).

## 4. Engineering tasks

- **T20.01 Use the all-pages matrix as the launch list.** Every route, alias and nested view from 02: Home; Work and its 13 types; Neoh; Contacts; conversations; Properties sub-views; Deals sub-views; opportunities; missions; the Neoh tool workspaces; Sales pages; Sites and video; profile, AI and broker setup; broker management; platform admin; every public capability link.
  - Each row has component, API, permission, external dependency, UI state, end-to-end test, observed result, screenshot, timestamp and ledger row.
- **T20.02 Build one dependency-ordered launch backlog.** Pull every open gap from the L01–L19 ledger rows and the audit cross-reference (A01–A10), and remove duplicates. A React tab or a placeholder ledger status is never "working".
- **T20.03 Confirm the launch scope with the owner.** List: supported carriers, SMS registration, email and calendar vendors, Stripe pricing (D-2026-10-12-02), live ad connectors, the customer search portal, SEO/AEO/GEO, commission accounting, browser-only Neoh Space (D-2026-10-08-01), the skeletal mascot and the premium studio.
  - Record which are in the customer contract. Hide or remove nothing silently, and present no out-of-scope feature (D-2026-10-12-01).
- **T20.04 Set up the launch cast.** An owner plus 20 agents, at least two team leads, mixed assignment and sharing, and one adversarial second brokerage. Check accounts, seat billing, owner and team management, one person's full CRM lifecycle, communications, property data, a deal, approvals, export and offboarding, with both allowed and denied rights.
- **T20.05 Extend the UI sweep to every route.** Upgrade `scripts/audit-neoh-production.py` to crawl every matrix row in Chromium and WebKit, on desktop and phone, for valid and invalid roles, configured and unconfigured providers, offline, stale and error states, and deep links. Fail on runtime errors, dead-end buttons, broken imports, horizontal overflow and unexpected 4xx/5xx.
- **T20.06 Separate visibility from function.** One navigation test per page, one real authorization test per role, one form or action test per advertised workflow, one degraded test per external provider, and one 20-user performance and soak test. Use sandbox accounts and approved destinations only (C4.3).
- **T20.07 Run the end-to-end journeys:**
  - new buyer → signed lead → dedup → assignment → approved contact → real carrier receipt → showing → CRM timeline → deal and contract milestones → brokerage report;
  - a complex buyer question → evidence-backed tools → a verified answer;
  - a homeowner receives a share link;
  - a phone browser scans a room → upload resumes → reconstruction → certified only when proven;
  - a marketing site draft → approval → publish → lead → CRM;
  - the owner revokes access while an AI action is queued.
- **T20.08 Accept each persona.** An agent sees assigned and granted records; a manager sees permitted team data; the owner has brokerage oversight with controlled private detail; the operator sees platform infrastructure; an unauthenticated customer acts only with a valid purpose-limited token.
- **T20.09 Measure quality against stated targets.** Home useful on mobile p75 under 2 s on defined devices, indexed CRM p95 under 800 ms, AI first token under 10 s at representative concurrency (all targets, not measured), safe and interruptible speech, property media quality and animation frame stability. Record sample, percentile, environment and failures.
- **T20.10 Benchmark against competitors honestly.** Compare real flows with current Follow Up Boss, BoldTrail, Lofty, CINC and Matterport, using their current documented features and direct journey tests, never invented rival limits.
- **T20.11 Check the production gates from the ledger.** Production deployment (L19), 20 seats (L18), provider DPAs and counsel decisions (L17), consent rules, messaging registration (L10), source licensing (L11), security and within-brokerage RLS (L17, L03), backup and restore, rollback, on-call support, spend caps, load and incident drills (L19). Re-run readiness; the 2026-10-03 snapshot proves nothing today.
- **T20.12 Rehearse, then invite in stages.** Run a synthetic rehearsal and an owner walkthrough, then invite 5 agents, then 20, only once the matrix is satisfied. Monitor usage, failed jobs, wrong-recipient risk, AI hallucination, refunds and support. The T+1 and T+7 reviews use the pilot metrics and keep "revenue influenced" separate from causation.
- **T20.13 Issue the go/no-go dossier.** Per-page GREEN, YELLOW or RED with evidence, owner and provider tasks, migration compatibility, signed security exceptions, test-run links and the approved plan. Any RED advertised core function blocks any claim that the platform is available in full.
- **T20.14 Propose the go-live checklist fix.** Replace item 8 (legacy MLS authorization) with the property sources from 11, as a change for the owner to approve (D-2026-10-12-01).

## 5. Failure and adversarial cases

- **F20.01 Visible but broken.** A page renders, but its main action fails or dead-ends.
- **F20.02 Ledger read as proof.** A row marked `tests_pass` on staging, or a historical CI run, is counted as a production pass.
- **F20.03 Cross-account leak in the cast.** An agent, team lead or the second brokerage sees a record it should not.
- **F20.04 Wrong recipient.** A journey sends to a real customer or a non-approved destination.
- **F20.05 Revocation race.** An AI action queued before revocation still runs after it.
- **F20.06 Fabricated quality number.** A missing measurement is reported as a pass, or a mock timing as production.
- **F20.07 Out-of-scope feature presented.** The dossier, the checklist or a page presents a feature outside the owner's scope, such as legacy MLS (D-2026-10-12-01).
- **F20.08 Invented competitor claim.** The benchmark states a rival limitation with no current source.

## 6. Acceptance criteria

- **AC20.01 Every matrix row has evidence.** Each row has an observed result, a screenshot and a timestamp, and every row's action works, not only its view. Evidence: the matrix plus the extended sweep output in two browsers and two viewports. Covers: T20.01, T20.05, T20.06, F20.01.
- **AC20.02 One backlog, read from the ledger.** Every open ledger row and audit ID appears once, with an owner and an order. Evidence: the backlog file linked from L20. Covers: T20.02, F20.02.
- **AC20.03 The owner signs the launch scope.** Evidence: owner decision recorded in the ledger. Covers: T20.03, T20.14, F20.07.
- **AC20.04 The cast is isolated both ways.** Evidence: real-PostgreSQL and API tests across all 20 agents, two team leads and the second brokerage. Covers: T20.04, T20.08, F20.03.
- **AC20.05 Every journey completes with real receipts.** Each journey ends with provider receipts to approved destinations only, and the revocation journey shows no action after revocation. Evidence: journey run logs with receipts. Covers: T20.07, F20.04, F20.05.
- **AC20.06 Quality figures are measured, with conditions.** Each target shows a measured value with sample, percentile and environment, or says "not measured". Evidence: committed commands and results (C9.3). Covers: T20.09, F20.06.
- **AC20.07 The competitor benchmark cites current sources.** Evidence: dated source links for every rival claim, plus the journey runs. Covers: T20.10, F20.08.
- **AC20.08 Every production gate is met in the ledger.** Each named row is at the required status in `production`, and the owner records `production_verified`. Evidence: the ledger rows and a fresh readiness run. Covers: T20.11, F20.02.
- **AC20.09 Staged rollout and pilot reviews happen as planned.** Evidence: the 5-agent and 20-agent invitation dates, and the T+1 and T+7 reviews with pilot metrics. Covers: T20.12.
- **AC20.10 A signed go/no-go dossier exists.** RED core functions block launch, and the dossier marks unverified parts as incomplete. Evidence: the dossier signed by the owner. Covers: T20.13.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS, IDX and RESO are out of scope** (D-2026-10-12-01). There are no MLS entitlements and no WordPress IDX in the launch scope. Do not test, certify or advertise them.
- **This category certifies; it does not build.** Gaps go back to their owning category's ledger rows.
- **Not a 10-user MVP.** The launch is one brokerage, an owner and 20 agents (C2.5).
- **Only the owner sets `production_verified`** and signs the dossier (C10.3).

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the launch backlog (`docs/claude-prompts/LAUNCH-BACKLOG.md`, proposed);
  - the extended UI sweep and journey runs;
  - the quality and competitor reports;
  - the go/no-go dossier (`docs/launch-readiness/go-no-go.md`, proposed).
- **First slice:** T20.02 + AC20.02, which reads every ledger row into one backlog. Then T20.03 + AC20.03, the owner's launch scope.
- **Ledger:** add L20-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
