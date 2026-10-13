# Neoh engineering contract (binding for every category prompt)

> Every prompt in this folder says **"Contract: binding"**. This file is that contract. It holds the rules that apply to all 20 categories, stated once. A prompt's own §7 adds category-specific rules; it never weakens these. Where a prompt and this contract disagree, this contract wins and the prompt is a defect to fix.
>
> Clause IDs (C1–C12) are stable. Cite them in reports and reviews ("violates C4.2").

## C1. Session protocol

1. **One category per session, PR or tightly coupled vertical slice.** Never load all 20 prompts into one context or rewrite the repository in one pass.
   - A **read-only review** of another category (such as the 03/17/18 review that starts every un-briefed session) does not break this rule.
   - Record its findings as `source_verified` rows in that category's ledger section. Don't change that category's code.
2. **Read order before editing:** root `CLAUDE.md` → [README.md](README.md) → this contract → your category prompt → its ledger rows in [LEDGER.md](LEDGER.md) → the specs the prompt links → the **current** code, migrations, tests and docs it names.
3. **Pin the base.** Record `git rev-parse HEAD` and the date at the start. Every claim about existing behaviour cites `path:line` at that SHA and names the symbol (function, component, migration), so it survives line drift.
4. **Baseline before change.** Write a short source-linked baseline: what exists, what the prompt assumes that you could not find, and what the ledger says. A file existing does not mean the feature is available to a user.
5. **Re-verify, don't inherit.** Older audits, prompts, ledgers and memory notes are leads, not facts. Re-check any finding at your SHA before acting on it or reporting it.
6. **Do the work.** Where code and tests are possible, change code and run tests. A plan or outline alone does not complete a slice.
7. **Know when to stop.** A slice ends when:
   - its named acceptance criteria have evidence;
   - its ledger rows are updated;
   - the C11 report is written.

   Then stop and propose the next slice. Don't start it unasked.

## C2. Scope and owner decisions

1. **Every authorized page is in launch scope.** That covers nested Work, Deals, Properties, Sales, AI and settings screens, entity sheets, configuration workspaces and public capability links. A page that only shows a button or a status badge is not finished.
2. **Honest setup gates are allowed.** If a feature needs a credential, licence or registration that only the customer or operator can supply, a complete and truthful "setup required" flow is acceptable. Missing advertised functionality is not: it stays unfinished until built.
3. **Never spoof a connection.** Never label a disconnected integration "live", "connected" or "ready". "Ready" needs a successful probe or send through that channel. An endpoint that answers, an environment variable that is set, or a credential that is saved is not enough.
4. **Owner decisions are binding.** The register lives in [LEDGER.md § Owner decisions](LEDGER.md#owner-decisions). Do not re-scope, downgrade or re-add anything a decision settles. If one blocks your work, record an owner question; don't route around it. In force today:
   - **D-2026-10-12-01: MLS is out of scope.**
   - **D-2026-10-08-01: Neoh Space is 100% browser-based.**
5. **Don't shrink the release to fit.** Do not drop promised features to shorten the pilot, and do not invent features the owner has not asked for.

## C3. Tenancy, roles and authorization

1. **One model.** The brokerage is the tenant. It has 20 agent seats, individual client assignments, explicit record sharing, team-manager oversight within delegated scope, and a broker-owner administration that is separate from platform admin.
2. **Enforce at every layer.** Apply it in PostgreSQL RLS, the API, AI/tool context, background jobs, search, signed media, exports and caches. A cache or a model answer is never an authorization decision.
3. **Never duplicate an RLS predicate in application SQL.** For example, `AND tenant_id = app_current_tenant()` reproduces only half of a policy. Run the database as the real application role (not a superuser) when proving isolation.
4. **Revalidate before every side effect,** including permission, assignment, consent, opt-out and credential state at execution time, not only when the work was drafted.
5. **Prove isolation both ways.** Same-brokerage cross-agent denial and cross-brokerage denial are each tested, not assumed.
6. **Real-time pushes are a layer too.** A WebSocket frame that carries customer content goes to the users entitled to it (`ws_hub.broadcast_user`), never to the whole tenant.
7. **Caches are keyed by actor and permission.** Every cache key includes tenant, user and permission state. Role changes, reassignment, revocation and logout invalidate it.
8. **Permissive RLS policies combine with OR.** Adding a narrower policy restricts nothing while a broad one remains, so replace the broad policy. A client-side role check (e.g. a `sessionStorage` role) is never authorization.
9. **Untrusted content is data, never instructions.** Records, notes, transcripts, emails, web pages and uploads may contain text aimed at the model; it never changes tools, scope or approvals.

## C4. External actions and approvals

1. **No external side effect without explicit, current human approval.** Side effects include outreach (email, SMS, calls), publishing, calendar writes, spend, purchases, charges, legal instruments and high-impact data changes. A model suggestion is never authorization.
2. **Approval is bound to the exact content.** The approved payload is immutable and hashed. If the content changes after approval, approve again. Record approvals, rejections, executions and outcomes.
3. **Sessions do not contact real customers, send to real people, buy services, charge cards, deploy to production or create provider accounts without the owner's explicit approval in that session.** Use sandbox accounts, synthetic data and the operator's own allow-listed destinations.
4. **Pushing to `main` deploys staging.** While the repository variable `STAGING_ENABLED` is `true` (it is today), CI builds and releases every push to `main` to the DigitalOcean staging app (`.github/workflows/ci.yml`, `release` job). Treat a push as a deploy: get approval, and never push unrelated unreviewed commits along with yours.
5. **Durable idempotency for every effectful action.** A retry, a duplicate callback or a crash between provider acceptance and the DB commit must not repeat the effect. Store provider receipts.
6. **Destinations come from records, never from the model.** A phone number, email address or URL a model produces is never used as a send target.

## C5. Privacy, consent and data lifecycle

1. **Minimize personal data at source.** Classify every new table or column in the privacy data map (`docs/privacy-data-map.md`, `backend/privacy_data_map.py`). Do not add new plaintext PII or communication content when an encrypted path exists or is required (see audit A01).
2. **Consent and opt-out are checked at execution,** recorded with their basis, and honored at once.
3. **Retention, export, correction and erasure** must reach derived copies: caches, search indexes, AI context, provider copies and backups, as documented.
4. **No PII or secrets in logs, analytics, error messages, reports or the ledger.** Inspect the logs your change produces. Check a secret's presence and name, never print its value.
5. **Never index plaintext PII for speed.** Use keyed lookup hashes (the `agent_contacts` pattern).
6. **Fair housing.** No scoring, targeting, matching or steering on protected characteristics.

## C6. Truth vocabulary (use these words exactly)

| Distinguish | From |
|---|---|
| fact (source-backed, cited) | inferred / predicted / provisional |
| staged (drafted) → approved → accepted (by provider) → delivered → responded | any earlier stage reported as a later one |
| simulated / mocked | live / sandbox provider result |
| target / design goal | measured result (with conditions) |
| staging passed | production configured → production exercised → customer outcome proven |
| verified zero results | unavailable / timed out / no entitlement |

**Use the same words in the UI and the timeline:** "sent" or "delivered" only after the provider event that proves it. Before that, say "accepted by the server". A label derived from an asset existing (a "Full 3D walkthrough" badge, a "Live" chip) must also read the quality or health verdict behind it.

**Per-source status values:** `checked`, `unavailable`, `timed_out`, `not_applicable`. A required source that is not `checked` makes the overall result `degraded`. A missing metric is missing evidence, never a passing score. Never fabricate geometry, scale, confidence, percentages or property facts.

## C7. Reuse the architecture

1. **Keep the stack.** That is FastAPI + asyncpg + PostgreSQL (FORCE RLS), React/Vite with CSS Modules, PlayCanvas/gsplat for tours, the existing AI gateway (`llm_gateway.py`), the automation job queue, the approval service and the provider-ledger patterns.
2. **No UI component libraries** (no MUI, Bootstrap or Tailwind): CSS Modules only. No Three.js in the main Oracle bundle. No new frameworks or microservices unless a benchmark shows a need.
3. **Preserve existing endpoint names, route aliases and workflows.** Change them only through a tested, backward-compatible migration.
4. **Check licence, size and browser support** before adopting any package or pretrained model.

## C8. Engineering workflow

1. **One vertical slice at a time,** covering whichever of frontend, API, storage/RLS, worker/provider, customer-visible error and retry, telemetry and tests the slice needs.
2. **Additive, ordered migrations** with explicit grants. Every new function needs an explicit `GRANT`; 0003 revokes PUBLIC. Document rollback and recovery, and treat production data migrations as staged and reversible.
3. **Bound cost:** network, CPU, model tokens, GPU, voice minutes. Use bounded timeouts, retries and queues. Degrade honestly instead of failing silently.
4. **Refactor only behind regression tests.** Extract bounded domains from large modules; don't rewrite a file just because it is long.
5. **Focused commits,** each with a change note, migration notes, rollback, the exact acceptance evidence and the remaining issues.

## C9. Testing and measurement

1. **Test at the right level:**
   - unit, schema and contract tests;
   - real PostgreSQL with RLS and the full migration chain;
   - React component tests and Playwright (desktop and phone viewport);
   - concurrency and failure injection;
   - provider sandbox, or live only to approved synthetic destinations;
   - cross-brokerage access;
   - the 20-account end-to-end journey;
   - real-device tests where the category needs them.
2. **Report tests as run, not as existing.** Give the command, exit code, counts and environment (`local`, `ci`, `staging`). A test that exists but did not run proves nothing.
3. **Measurements state their conditions:** device or browser, data volume, concurrency, p50/p95 and cost. Mocked provider timings and local load tests are not production results.
4. **Every performance or quality claim is reproducible** from a committed command or protocol. Every acceptance gate states its measurement protocol in one line: device or browser, data, method.
5. **Never weaken a passing test or control** to make a slice pass. Fix the code or record the failure.

## C10. Ledger update rule

1. **Status lives only in [LEDGER.md](LEDGER.md).** Prompts carry dated baselines, not status.
2. **Update the ledger in the same commit as the change.** Edit your category's `LNN-xx` rows: status, env, evidence SHAs, files, tests run (command, result, date), blockers and next task. Evidence is append-only.
3. **Use only the status values defined in the ledger**, and move a row only when its entry criterion is met. Never mark `production_verified`; only the owner records that.
4. **Name artifacts in the ledger.** If you create one (for example `ALL-PAGES-MATRIX.md` or an inventory CSV), link it from the ledger. Don't start a parallel backlog.

## C11. Report format (end of every slice)

Report, in this order:
1. Base SHA and source-linked baseline.
2. The slice chosen and why it is the highest-dependency next step.
3. Files and migrations changed (`path:line`).
4. Tests and measurements actually run, with commands and results.
5. Role and customer-journey outcomes verified.
6. Remaining failures, risks and blockers that need humans (owner, legal, provider, device).
7. Ledger rows updated.
8. Recommended next slice.

Keep it concise. Link files instead of pasting their contents.

## C12. Banned claims

Do not write "done", "complete", "fully operational", "production-ready", "A+", "verified" or "live" for anything the evidence does not support:
- "Verified" needs the C6 evidence.
- "Production-ready" needs `production_verified` in the ledger.
- "Live" needs a real provider result in a named environment.

A historical CI run, a staging pass, a mock benchmark, a button, a status badge or a ledger row is not production proof.

---

## Appendix: where each clause came from

This contract merges the four shared-rule variants the prompts used to carry. Nothing was dropped; wording was unified.

| Source batch | Old section names |
|---|---|
| 01–05 | "Required engineering workflow", "Cross-cutting rules", "Output format" |
| 06–10 | "Global acceptance discipline", "Final response expected from Claude" |
| 11–15 | "Universal constraints, tests and deliverables" |
| 16–20 | "Required implementation discipline", "Your output after each implementation slice" |

| Clause | 01–05 | 06–10 | 11–15 | 16–20 |
|---|---|---|---|---|
| C1 session protocol | workflow 1 | — | role/preflight | role/initial inspection, discipline 1 |
| C2 scope | cross-cutting "all pages" | "all pages in scope" | "verify every page… setup-required" | discipline 1 (existence ≠ availability) |
| C3 tenancy | cross-cutting "hybrid access" | "enforce permissions…" | "access rights" | discipline 3 |
| C4 approvals | workflow 3, cross-cutting "approval" | "preserve human approval… idempotency" | "never send/spend/deploy" | discipline 4 |
| C5 privacy | — (in 17) | "PII" in permissions | "respect consent/PII" | discipline 3 |
| C6 truth | cross-cutting "truthfulness" | "distinguish proposal vs…" | "evidence quality" | discipline 5 |
| C7 reuse | cross-cutting "efficiency" | "preserve product tech decisions" | preflight "preserve existing…" | discipline 2 |
| C8 workflow | workflow 2, 5 | "schema/rollout plan" | "implementation plan P0/P1/P2" | discipline 2 |
| C9 testing | workflow 3, 4 | "require tests… p50/p95" | "test units… measure latency" | discipline 4, 5 |
| C10 ledger | "update the master category matrix" | "update the category tracking matrix" | "keep category acceptance ledger updated" | "update the master ALL-PAGES-MATRIX and category backlog" |
| C11 report | output format | final response | "report source-linked baseline…" | output after each slice |
| C12 banned claims | workflow 5 ("never use done or A+") | "never report completion without tests" | "never claim fully operational from a mock" | discipline 5 |
| C3.3, C8.2, C4.4 | — | — | — | — (new: lessons recorded in the repo's history: duplicated RLS predicates, ungranted functions, push = staging deploy) |
| C2.3 (ready needs a probe), C3.6–C3.9, C4.6, C5.4–C5.6, C6 UI wording, C9.4 protocol, C9.5 | added 2026-10-12 | from the category rewrites | each was proposed independently by the 02/10/14, 05–08, 09/10/13, 12, 14–16 and 17–20 reviews | evidence in those prompts' §2 |
