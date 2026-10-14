# Admin AI Neoh: an operations agent (scope for owner approval)

> **Status: proposal. Nothing in this document is built.** It scopes the owner's request (2026-10-14) for an "admin AI Neoh". The owner asked to scope it first and to approve it before anything is built. Each claim about existing code cites `path:line` at base `5618600` (2026-10-14). Everything else is a design target (C6). The cost figures are **estimates**, not quotes or measurements.
>
> Binding: [CONTRACT.md](claude-prompts/CONTRACT.md) C4 (approvals), C5 (privacy), C6 (truth vocabulary) and C12 (banned claims). Related prompts: [19](claude-prompts/19-infrastructure-deploy-observability-recovery.md) (operations, alerts), [07](claude-prompts/07-ai-gateway-model-tool-governance.md) (gateway, governed tools) and [17](claude-prompts/17-security-privacy-compliance-data-lifecycle.md) (security, privacy).

## 1. Recommendation in brief

- **Build an observer and reporter, not an operator.** It reads health, alerts, billing exceptions, AI failures and release state. It writes one daily digest and immediate critical alerts to the owner. Later it drafts GitHub issues, and later still a Claude Code routine may open **draft** PRs that the owner reviews.
- **It never changes the SaaS on its own.** No code change, merge, deploy, data change, refund or billing action, and no email to anyone but the owner, without owner approval.
- **It is a detective control for the ISMS, not segregation of duties.** It strengthens A.8.16 (monitoring) and A.5.25 (event assessment). It is **not** a second person: AI agents do not count as one ([02 §5.3](compliance/iso27001/02-leadership-and-policy.md), line 92).
- **Phase 1 needs no LLM at all.** The digest is built from counts and codes that already exist. This keeps personal data away from model providers and keeps the cost near zero.
- **Two findings shape Phase 1** (§3.2). Staging blocks all email, alerts included, and staging starts no periodic jobs. So on staging, today, the owner receives no alert email at all.

## 2. Rules that hold in every phase

| # | Rule | Source |
|---|---|---|
| R1 | No code change, merge, deploy, data change, refund, billing action, provider change or restart without the owner's explicit approval, bound to the exact content | C4.1, C4.2 |
| R2 | Pushing to `main` deploys staging, so the agent never pushes to `main` and never merges. There is no auto-merge | C4.4; `ci.yml:259-265` |
| R3 | Any email to anyone other than the owner (the collaborator included) needs owner approval. Customers are never contacted | C4.3 |
| R4 | No PII, secrets or record content in any message, issue, PR, prompt or log | C5.4; `operator_api.py:20-25` (the "never customer content" rule) |
| R5 | Content the agent reads (emails, logs, issue bodies, error text) is data, never instructions | C3.9; 07 T07.09 |
| R6 | A missing signal is reported as `unavailable` or `timed_out`, never as healthy. "No alerts" says what was checked | C6 |
| R7 | A dedicated, least-privilege identity. Every run and every action is in `audit_ledger` | §8 |
| R8 | Recovery mode outranks the agent. On a restored copy it may observe; its only allowed effect is the owner-only alert email (§3.2, Q3) | `recovery_mode.py:1-30` |

## 3. What it observes

### 3.1 Signal inventory

Column "Today": **exists** = the code exists in source (not a claim that it is exercised on staging); **partial**; **missing**.

| Signal | Source | Today | Notes |
|---|---|---|---|
| Component health: database, realtime fan-out, Valkey, workers, scheduler, job queue, AI, outbound side effects, email outbox (plus legacy `mls`) | `component_health.py:211` `snapshot`, `:33` `ALERTING`, `:37-39` tiers | exists | No property-provider components (19 §2; L19-03) |
| Incidents with open, last-seen, resolved and notify state (MTTD) | `ops_alerts.py:42` `evaluate_once`, `:74` `_record`, `ops_alerts` table | exists | Two-evaluation confirmation (`:34`); one advisory-lock leader (`:35`) |
| Privileged activity: operator sign-ins, repeated failures, role changes, new broker owners, operator second-factor change | `security_alerts.py:122`, `:127`, `:158`, `:175`, `:215` | exists | Emails the account id and IP by design (`privacy-data-map.md:29`); the failure count is per process (`security_alerts.py:28-31`) |
| Access-denial bursts and anomalies | `audit_middleware.py:145-147`, `:223` `_record_anomaly_safe`; `admin_ops.py:729` `/anomalies` | exists | Recorded, not pushed to a person (annex A.5.25 gap #6) |
| Audit chain integrity and audit loss | `audit_ledger.py:746` `verify_chain_segment`; `:159` `_alert_audit_loss`; task `periodic.py:1104-1110` | partial | The verify task is periodic, so it does not run on staging (§3.2) |
| Platform-admin reads of brokerage data | `audit_middleware.py:151-221` (L17-17) | exists | Feeds the SoD evidence |
| Fleet counts, sessions, users online | `admin_ops.py:234` `/overview`, `:395` `/users`, `:752` `/system` | exists | `/users` returns identities, so the digest uses counts only |
| Per-replica load: sockets, pool, asyncio tasks, ambient LLM calls | `admin_ops.py:98` `/runtime-load` | partial | In-process, one replica per answer; nothing aggregates across replicas |
| Per-brokerage state and needs-action | `operator_api.py:510` `/brokerages`, `:630` `/diagnostics` | exists | Content-free by construction (`operator_api.py:20-25`) |
| Billing exceptions: no subscription, payment issue, cancelled, webhook refusals, metering backlog | `operator_api.py:1136` `/billing/exceptions`; `admin_ops.py:349` `/billing-summary` | exists | Read only. A refund or plan change is always R1 |
| Voice and texting state per brokerage | `operator_api.py:1333` `/comms` | exists | |
| AI provider calls, failures, rate limits; chat p50/p95; tool failures (24 h) | `operator_api.py:1481` `_gateway_view`, `:1511` `/ai`; `llm_gateway.py:221` `_Counter` | partial | The gateway counter is in-process, per replica (`llm_gateway.py:221-240`). Latency is chat turns only |
| Release: API build, worker builds, migration head | `operator_api.py:1050` `_release`, `:1118` `/release` | exists | |
| Email outbox status rollup | `admin_ops.py:506` `/outbox` | exists | The digest gives counts by status, never addresses or subjects |
| Mission activity | `missions/digest.py:221` `send_digest` (counts only, `:1-13`) | exists, off | `ORACLE_MISSION_DIGEST_ENABLED` defaults to `0` (`:41-42`). A periodic task |
| Pilot product metrics | `operator_api.py:1607` `/pilot-metrics` | exists | |
| **Request latency and error rate per route (API p50/p95, 5xx)** | none | **missing** | No APM or metrics library in the backend. Only chat latency exists |
| **Runtime log forwarding** | `infra/digitalocean/app.yaml` has no `log_destinations` | **missing** | Annex A.8.15 gap; 02 §5.3 "off-platform copy" gap. Owner chooses the sink |
| **External uptime check** (the whole app down) | none | **missing** | When the app is down, nothing in-app can alert (annex A.8.16) |
| **Database CPU, locks, connections; app CPU and memory; deploy events** | DigitalOcean metrics (outside the app) | **missing** | Needs a read-only DigitalOcean token (Q14) |
| **CI on `main`, staging deploy result, Dependabot alerts** | GitHub API | **missing** | Needs a read-only GitHub token |
| **Email from brokerages and agents** | none | **missing** | No inbound mail reader exists. The support address is a placeholder (`support-model.md:11`). See §6 |
| Property-provider health (RentCast, ATTOM, harvesters, market data) | `avm_client.py:94-98` (cooldown only) | missing | 19 T19.07 owns it; the digest consumes it once it exists |

### 3.2 Two staging facts that change the design

1. **Staging cannot send any email, alerts included.** Staging runs with `ORACLE_RECOVERY_MODE=1` (`render-app-spec.py:18`, `:220`, checked at `:320-328`). `smtp_mailer.send` calls `recovery_mode.guard` first (`smtp_mailer.py:291`). The only exception is a demo allowlist for messages and calls, which explicitly excludes email (`recovery_mode.py:145-150`: "email, calendar, any other recipient — stays blocked"). `ops_alerts.send_email` (`ops_alerts.py:133-148`) and `security_alerts.deliver` (`security_alerts.py:79-99`) both go through `smtp_mailer.send`. Inferred from source, not observed on staging: each send fails with `RecoveryModeBlocked`, which is stored in `ops_alerts.notify_error` (`ops_alerts.py:87-89`) and retried on every evaluation. **On staging the owner receives no alert email today**, whether or not `ORACLE_ALERT_EMAIL` is set (`app.yaml:222-228`).
2. **Staging starts no periodic jobs.** `PeriodicScheduler.start` returns early in recovery mode (`periodic.py:174-185`). So the mission digest, `audit_chain_verify` and every other periodic task never run there. The alert evaluator is the exception: it starts in the API lifespan, not in the scheduler (`server.py:170-181`).

**Design consequence.** The agent's evaluation loop follows the `ops_alerts` pattern (API lifespan, advisory-lock leader). It does not depend on the periodic scheduler. Owner-only alert delivery on a non-production instance needs a narrow recovery-mode exception: an `operator_alert` kind, an exact match against the configured alert addresses, and non-production `ORACLE_ENV` only. This copies the demo-allowlist pattern (`recovery_mode.py:150-194`). Changing the disaster-recovery kill switch is an owner decision (Q3). The alternative is that staging stays log-only until production exists.

## 4. What it may do, and what it may never do

| Action | Phase | On its own? | Notes |
|---|---|---|---|
| Read the signals in §3.1 (aggregates and codes) | 1 | **yes** | Read-only transactions, §8 |
| Compose the daily digest and the immediate alerts | 1 | **yes** | To the owner's alert addresses only |
| Write audit entries for its own runs | 1 | **yes** | §8 |
| Re-run read-only diagnostics (a fresh health snapshot, a chain-segment verify) | 1 | **yes** | Bounded by the existing timeouts |
| Draft a GitHub issue (title, codes, counts, runbook link) | 2 | **yes**, as a draft | Opening it is Q6, because an opened issue notifies everyone with repo access, the collaborator included |
| Open a GitHub issue | 2 | only if the owner approves auto-open (Q6) | Same no-content rule as the email |
| Start a Claude Code routine on an issue | 3 | **no**: only on issues the owner labels | The label is the approval, recorded in the audit trail |
| Push a branch `ops-agent/*` and open a **draft** PR | 3 | only inside an owner-labelled run | CI runs. The owner reviews. Never a merge |
| Merge, push to `main`, deploy, promote, roll back | — | **never** | R1, R2 |
| Data change, migration, erasure, export, role or MFA change (e.g. `operator_api.py:1103` MFA reset) | — | **never** | R1 |
| Refund, credit, plan or subscription change, Stripe write | — | **never** | R1 |
| Restart, scale, change env or secrets, flip a feature flag | — | **never** (Q12) | A runbook link in the alert instead |
| Email anyone other than the owner (the collaborator included) | — | **never without approval** | R3, Q5 |
| Reply to a brokerage or an agent | — | **never** | C4.3. It may draft a reply into the owner's digest (Phase 2+, Q10) |
| Read agents' connected mailboxes | — | **never** | §6 |

## 5. How "connects to Claude Code" works safely

| Option | How it works | Risk | Cost (estimate) | Verdict |
|---|---|---|---|---|
| **A. Issue → owner hands it to Claude Code** | The agent drafts or opens an issue with codes, counts and the runbook link. The owner starts a Claude Code session on it when back | Lowest. A human starts every change. Nothing new holds write access | ~$0 extra: the owner's existing Claude plan. The cost is latency (fixes wait for the owner) | **Phase 2 default** |
| **B. Scheduled Claude Code routine, draft PR only** | A routine runs on a schedule, picks up issues the owner has labelled (e.g. `ops-agent:draft-fix`), works in a fresh clone, pushes `ops-agent/<issue>` and opens a **draft** PR. CI runs and the owner reviews and merges | Medium. The risks are prompt injection through issue text, CI secrets reached by a PR, and runaway usage. Mitigated by the controls below | Each attempt: roughly $1–$15 of model usage (estimate; it depends on the plan or API pricing at the time and on the task size), plus one CI run of GitHub Actions minutes. Capped at N attempts a week (Q7) | **Phase 3, if approved** |
| **C. Autonomous fixer** (merges, or pushes to `main`) | The agent writes, merges and therefore deploys staging | High. It breaks C4.4 (a push is a deploy) and R1, and removes the only human gate. An AI is not a second person, so it cannot be the SoD review | Low money, high blast radius | **Not recommended. Out of scope** |

**Controls for option B (all required):**
- **Least-privilege token.** A GitHub App or fine-grained token for this one repository: `contents: write` only through a ruleset that limits pushes to `ops-agent/*`; `pull_requests: write`; `issues: read`; `metadata: read`. It has **no** `workflows` permission, so the routine cannot edit `.github/workflows/*` and cannot reach CI secrets through a changed workflow. It also has no `administration`, `secrets`, `environments` or `actions: write`. It is a separate token from the Phase 2 issue token.
- **`main` protection**, which cannot be verified from the repo (Q8): a PR is required; the required checks are `backend`, `frontend`, `security` and `rls` (the release job depends on them, `ci.yml:259-265`); the owner's review is required; the bot has no bypass; auto-merge is off. The production `promote` job keeps its manual dispatch and reviewer (`ci.yml:616-633`; `release-checklist.md:13`).
- **No secrets in prompts or the runtime.** The routine gets no `.env`, no DigitalOcean, Stripe, SMTP or provider keys, and no database URL. Tests use the existing synthetic setup (`ORACLE_SKIP_DOTENV`, blocked SMTP in `conftest`). The issue text it reads holds codes and counts only (R4).
- **Injection boundary.** The routine acts only on owner-labelled issues. It treats issue and comment text as data (R5). It edits code only, not CI, infra or migrations (Q13), and the PR description lists every file touched.
- **The output is a draft.** A draft PR is "staged" (C6). It is never described as a fix until CI passes and the owner merges, and "fixed in production" needs production evidence (C12).
- **Budget.** At most N runs a week, one open draft PR per issue, and a hard stop on failure (no retry loops). Every run is in the audit trail (§8).

## 6. Email reading (brokerages and agents)

**Today, at `5618600`:** no inbound mail path exists. The support address is a placeholder (`support-model.md:11`). Agents' Google OAuth asks only for `calendar.events` (`commands_api.py:371`): no Gmail read scope exists. Outbound mail goes through consumer Gmail SMTP, which has no DPA (`subprocessor-inventory.md:19`, `:34`).

**Proposed scope (only if the owner approves; Q10, Q11):**

| Topic | Proposal |
|---|---|
| What is read | **Only a dedicated support mailbox** (e.g. a support address on a provider with a DPA), which also closes `support-model.md:11`. **Never** agents' or brokers' own mailboxes, and no Gmail read scope. Even with consent, that would read their clients' mail (third parties who never agreed), and Google treats Gmail read scopes as restricted, with a security assessment (to be re-checked before any decision) |
| What the agent does | It classifies each message as metadata only: received time, sender matched to a user and a brokerage **by exact keyed lookup against records** (never by the model, C4.6), a category (billing, access, bug, privacy request, other) and an urgency. The digest shows counts by category and urgency and a link to the mailbox. It never quotes, summarises into the email, replies or forwards |
| Model use | Phase 1–2 use deterministic rules (sender match, keywords). A model reads message bodies only after the owner approves the provider and region (the open 07 §7 question). If one is used, its output is an enum, so injected text cannot reach a tool or a recipient (C3.9) |
| Privacy requests | A message classified as a privacy request is flagged as urgent to the owner, because statutory clocks run from receipt. The agent never acts on it |
| What is stored | Neoh stores no bodies. A proposed table `ops_support_messages` holds the message-id hash, received time, `tenant_id` and `user_id` (nullable), category, urgency and status. Bodies stay in the mailbox under the mailbox's own retention |
| Retention (proposal) | Metadata kept 90 days, then deleted. Mailbox retention is set by the owner (e.g. 24 months; Q10). Erasure of a user or brokerage deletes their metadata rows |
| Privacy data map (C5.1) | New rows in `docs/privacy-data-map.md` and `backend/privacy_data_map.py`: the support mailbox (a system; Neoh is controller for the brokerage user's own message), and `ops_support_messages` (`C.COMMUNICATION_METADATA`, disposition ERASE). New rows in `docs/subprocessor-inventory.md` for the mailbox provider and any model provider. Add it to the erasure order (`privacy_requests.py:214-216`) and to DSAR export |
| Policy (C5.7) | Platform Use Policy wording, drafted for owner approval, plus a `PLATFORM_POLICY_VERSION` bump (`policy_contract.py:9`) and a `POLICY_COVERAGE` row (`:104`). Draft: *"Messages you send to Neoh support are read by the Neoh operator. Software, which may include an AI model, sorts them by topic and urgency so urgent problems are seen first. This sorting never replies to you, never changes your account and never contacts anyone. Neoh keeps only the topic, the time and your brokerage, for 90 days; the message itself stays in the support mailbox for [N] months."* Counsel reviews the privacy-notice version (17 T17.07) |

## 7. Notification design

**Recipients.** The owner only, at `ORACLE_ALERT_EMAIL` (operations) and `ORACLE_SECURITY_ALERT_EMAIL` (security, `security_alerts.py:63-65`). Use a mailbox separate from the operator login account, as 02 §5.3 requires for the compensating control. **The GitHub collaborator** gets nothing by default. If the owner approves (Q5), they get engineering-class items only (CI, release, issue links), never billing, security events or brokerage state. The owner supplies their address; this document records none.

| Class | Delivery | Examples (source) |
|---|---|---|
| **P1 critical** | Immediate email; one per incident; a recovery email when resolved | database unavailable (`ops_alerts.py:110`); a core component confirmed bad (`component_health.py:37`); AI fully `UNAVAILABLE`; job queue dead-letter spike; outbound side effects stuck; audit chain broken or audit rows lost (`audit_ledger.py:159`); all privileged-activity alerts (`security_alerts.py`; never suppressed); billing webhook refusals; staging deploy or `main` CI failed (Phase 1b); external uptime check down |
| **P2 attention** | The next daily digest | `DEGRADED` on an important component; a brokerage needs action; billing exceptions; tool failure rate up; anomaly bursts; support messages by category |
| **P3 information** | Daily digest | counts, release state, migration head, mission activity, what was checked |

**Daily digest.** One plain-text email at a fixed time (Q4). It has a fixed section order, with a per-source status line on each section (`checked`, `unavailable`, `timed_out`, `not_applicable`). If any required source is not `checked`, the digest headline says `degraded` (C6). It covers open and resolved incidents with time to detect, P2 items, the C6-labelled counts, and an "unavailable signals" list (the missing rows of §3.1). It is sent even when everything is quiet, so that its absence is itself a signal. A missed digest is caught by the external uptime monitor or by a heartbeat check (Q9).

**Rate limits (proposal).**
- One open incident per component, as today (`ops_alerts.py:79-86`).
- At most 6 immediate emails an hour and 20 a day across P1. After that, one roll-up email ("N more; see the operator console") and silence until the next hour.
- Privileged-activity alerts are exempt from the cap: they are the SoD control. Repeated failures are already thresholded (`security_alerts.py:68-72`).
- Recovery emails within 10 minutes of each other are batched into one.
- No quiet hours for P1.
- An undeliverable email is recorded and retried by the existing evaluator (`ops_alerts.py:87-89`). It never blocks evaluation.

**A message never contains:** a name, email address, phone number or postal address of a client, lead, agent or broker; any message body, note, transcript, document, property address or deal figure; a brokerage name (it gives a short tenant reference instead, which the operator console resolves); a secret, token, key, environment value, connection string, stack trace or free-text provider error (it gives error type names and codes, as `ops_alerts.py:146-148` does). The one existing exception is the privileged-activity alert, which carries the account id and IP by design (`privacy-data-map.md:29`); it is unchanged. A sentinel test seeds secrets and PII into fake rows and asserts that none reaches a message, an issue or a PR body, as `test_operator_api` does (`operator_api.py:24-25`).

## 8. Identity, kill switch and audit

**Identity (least privilege).** Today there is no read-only platform role. `Role` has only `platform_admin` (god mode), `broker_owner` and `agent` (`tenancy.py:72-75`). The agent must not hold `platform_admin`. Proposal:
- A dedicated service principal, `ops-agent`. It has no password, no session and no login route, so it cannot sign in through the UI or the API.
- It reads through a small set of new aggregate-only database functions that return counts, states and codes and never rows. Each function has an explicit `GRANT` (C8.2). Its queries run in `READ ONLY` transactions with `statement_timeout`.
- These functions are proved as the real non-superuser application role. A DEFINER function under FORCE RLS can silently count zero (a lead from earlier work, to re-verify).
- The section builders in `operator_api.py` (`_Sections` at `:169`, one SAVEPOINT per section, `:34-36`) are reused rather than copied. Where they need a context, the agent runs under a distinct actor id, so its reads are attributable (L17-17).
- Separate tokens per phase: a GitHub issue token (Phase 2), a GitHub draft-PR token (Phase 3, §5) and an optional read-only DigitalOcean token (Q14). None is ever in a prompt.

**Kill switch (layered; any one stops it):**
1. `ORACLE_OPS_AGENT_ENABLED` (default `0`), plus one flag per phase: `…_DIGEST`, `…_ISSUES`, `…_ROUTINE`.
2. A `paused` row the owner can toggle from the operator console, audited. It takes effect on the next evaluation, with no deploy.
3. Revoking the GitHub tokens (one click on a phone) ends Phases 2–3. Disabling the routine ends Phase 3.
4. Recovery mode keeps blocking every external effect except the owner-only alert email (R8).

**Audit.** Every run, every message, every issue or PR and every refusal writes one `audit_ledger` entry (`audit_ledger.py:370` `record`). The category is `ADMIN_ACTION` (`:213`) or a new `OPS_AGENT` value (proposed; `category` is free text in `0005_audit_ledger.sql:60`). Metadata holds the run id, phase, each source with its status, counts, the recipient as `actor_ref` (never the address), the message's SHA-256, and the issue or PR number. Content is never stored. The chain is hash-linked and the app role may only INSERT and SELECT (02 §5.3, `0119`). The digest reports the agent's own activity, so a silent agent is visible.

## 9. Phased plan

Effort: **S** ≤ 1 day, **M** 2–5 days, **L** more than a week, for one engineering session including tests. All costs are **estimates** at list prices, unverified and to be re-checked before approval.

| Phase | Builds on | Scope | Effort | Cost (estimate) | Owner decisions |
|---|---|---|---|---|---|
| **0. Prerequisites** (owner actions, no code) | — | Set the alert addresses; choose an external uptime monitor; choose a log sink | — | Uptime monitor: $0 on common free tiers. Log sink: $0–$20 a month, depending on provider and volume | Q2, Q9, Q13 |
| **1. Read-only digest and critical alerts** | `ops_alerts` (evaluator, leader lock, incident table), `component_health`, `operator_api` sections, `security_alerts`, `audit_ledger` | An evaluation loop in the API lifespan; the digest composer; P1 and P2 classes; rate limiter; the owner-only recovery-mode exception (if Q3 = yes); aggregate-only DB functions and grants; the `ops-agent` actor; kill-switch flags and console toggle; audit entries; tests (no-PII sentinel, rate limits, `unavailable` and `degraded` rendering, recovery-mode exception limited to non-production and exact match); ledger row L19-xx; privacy-map row for the digest | **M** | No LLM. Compute about $0 extra on the current instances. Email through the existing SMTP relay (consumer Gmail, no DPA; acceptable only for owner-only, content-free mail, Q13) | Q1, Q3, Q4, Q12 |
| **1b. External signals** (optional) | Phase 1 | A read-only GitHub token (CI on `main`, staging deploy, Dependabot); an optional read-only DigitalOcean token (DB CPU, connections, app metrics) | **S** | $0 (API reads) | Q14 |
| **2. Draft GitHub issues** | Phase 1 incidents | A fingerprint per incident (component plus error code) to deduplicate; an issue template (codes, counts, first and last seen, runbook link under `docs/runbooks/`); draft-in-digest or auto-open (Q6); an optional one-paragraph summary from codes only through `llm_gateway.complete` (`llm_gateway.py:387`, the text path that may fall back); support-inbox classification metadata (if Q10 = yes) with its policy, privacy-map and subprocessor changes | **M** | Without the LLM, $0. With the LLM summary, under $5 a month at about 30 short calls a day (estimate). A support mailbox on a DPA provider costs about $6–$8 per user a month on common business plans (estimate) | Q5, Q6, Q10, Q11 |
| **3. Claude Code routine drafts PRs** | Phase 2 issues and owner labels | The routine definition; the draft-PR token and branch ruleset; `main` protection confirmed; budget caps; an audit hook; a written runbook for revoking it | **L** (mostly configuration, review and rehearsal: one supervised dry run on a synthetic issue) | About $1–$15 of model usage per attempt (estimate), capped at N a week (Q7); plus CI minutes per draft PR | Q7, Q8, Q13 |

**Exit evidence per phase (C9):** tests run with command and counts. For Phase 1, one P1 test alert fired on staging and **received** by the owner (19 AC19.06). For Phase 3, one dry-run draft PR on a synthetic issue, with CI results and the owner's review. Nothing is reported as live before its provider result (C12).

## 10. ISMS position

- It supports A.8.16 (monitoring: pushes events to a person), A.5.25 (event triage table, gap #6) and A.8.15 (off-platform evidence, together with the log sink). The SoA rows stay `partial` until evidence exists.
- It does **not** close A.5.3 (segregation of duties). The compensating controls remain those listed in 02 §5.3. The agent's alerts reach a mailbox the operator account does not control, which supports the existing alert control. They are not an independent review.
- New risk-register entries are needed: R-new-1, "an agent with repo write access is abused or misled" (option B), and R-new-2, "support mailbox content handled by automation". Each has treatment and an owner sign-off (03 register).

## 11. Open owner questions

| # | Question |
|---|---|
| Q1 | Approve this scope and the phase order? Phase 1 only for now? |
| Q2 | Which mailbox gets operations alerts and which gets security alerts? Should they be separate from the account the operator signs in with (recommended by 02 §5.3)? |
| Q3 | Allow a narrow recovery-mode exception, so that owner-only alert email is sent from non-production instances (exact address match, non-production env only)? Or does staging stay log-only until production exists? |
| Q4 | Digest time and time zone? Daily only, or daily plus weekly? |
| Q5 | Should the GitHub collaborator receive anything? If so, which classes (engineering only is recommended)? Their address, and is a confidentiality agreement in place (A.6.6)? |
| Q6 | Should Phase 2 issues open automatically (visible to and notifying everyone with repo access), or be drafted in the digest for you to open? |
| Q7 | For Phase 3, use a Claude subscription routine or API billing? What monthly cap, and how many attempts a week? |
| Q8 | Is `main` protected (PR required, required checks, your review, no bypass, auto-merge off)? This cannot be verified from the repository. |
| Q9 | Which external uptime monitor and which log sink? Each is a new subprocessor and needs a DPA review. |
| Q10 | Which support address and provider? May the agent classify that mailbox? For how many months is mail retained? May a model read bodies, and which provider and region (the 07 §7 question)? |
| Q11 | Confirm that agents' and brokers' own mailboxes are **never** read. (Recommended.) |
| Q12 | Confirm that the agent never restarts, scales or flips flags, and only alerts with a runbook link. (Recommended.) |
| Q13 | Is consumer Gmail SMTP acceptable for owner-only, content-free operations mail until Workspace or a transactional provider replaces it (`subprocessor-inventory.md:34`)? May a Phase 3 PR touch migrations or infrastructure, or only application code and tests (recommended)? |
| Q14 | Issue a read-only DigitalOcean token for platform metrics? |

## 12. On approval

A ledger row (proposed **L19-xx, "operations agent"**) is added in the first build commit (C10). Then, in order: the Phase 1 slice; the privacy-map row; the policy wording (only if support-mail classification is approved); and the risk-register entries. This document stays the design reference. Status lives in the ledger only.
