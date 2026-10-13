# 08 — Durable missions, task graphs and recovery

> **Scope:** Let Neoh coordinate multi-step work (CRM, property research, drafts, communications, showings, approvals) as a typed task graph with durable execution, recoverable partial failure and truthful progress, with no unsafe autonomous contact or purchase. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03, 05, 06, 07 · **Unblocks:** 10, 13, 14, 20 · **Ledger:** [L08](LEDGER.md#l08) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Missions: `backend/missions/planner.py`, `backend/missions/executor.py`, `backend/missions/policy.py`, `backend/missions/simulator.py`, `backend/missions/evaluator.py`, `backend/missions/learning.py`, `backend/missions/costs.py`, `backend/missions/drafter.py`, `backend/missions/digest.py`
- API and UI: `backend/missions_api.py`, `oracle-app/src/neoh/MissionBuilder.jsx`, `oracle-app/src/neoh/missionModel.js`
- Schema: `backend/db/migrations/0100_missions.sql`, `backend/db/migrations/0027_real_estate_intelligence_platform.sql` (`command_executions`)
- Execution path: `backend/automation_jobs.py`, `backend/commands_api.py` (`stage_command`, `release_command`), `backend/approval_service.py`, `backend/autonomy.py`, `backend/outreach_compliance.py`, `backend/outcome_memory.py`
- Switches: `.env.example` (Missions block), `infra/digitalocean/app.yaml`
- Tests: `backend/tests/test_missions_executor.py`, `backend/tests/test_missions_planner.py`, `backend/tests/test_missions_policy.py`, `backend/tests/test_missions_schema.py`
- Open work: PR #11, branch `feat/mission-internal-tasks-executable` (read-only: `git show 55ac99c --stat`)
- Sibling prompts: [05](05-semantic-language-intent.md), [06](06-evidence-retrieval-memory.md), [07](07-ai-gateway-model-tool-governance.md)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Missions are off by two switches.** `enabled` (`backend/missions/executor.py:45`) defaults `Feature.MISSIONS` off; `.env.example:316-322` documents `ORACLE_FEATURE_MISSIONS` and `ORACLE_MISSIONS_ENABLED`; `infra/digitalocean/app.yaml:412-413` sets `ORACLE_MISSIONS_ENABLED` to "0".
- **The API has create, list, get, simulate, launch, pause and progress.** `missions_api.py:32` (prefix `/api/missions`), routes at `:66`, `:110`, `:119`, `:139`, `:175`, `:222`, `:238`.
- **Nothing in mission code writes `cancelled`.** The status is allowed (`0100_missions.sql:77-79`) but only read (`missions_api.py:185`, `backend/missions/digest.py:123`).
- **Action states are fixed by a CHECK.** `0100_missions.sql:180-182`: `planned`, `would_have_done`, `staged`, `approved`, `executed`, `blocked`, `skipped`, `failed`.
- **The model chooses candidates by index only.** `backend/missions/planner.py:1-21` (module docstring); `CHANNELS` (`:47`) includes `task`; an unparseable plan raises `PlanUnavailable` (`:60`).
- **The executor skips every `task` action.** `_work_one` (`backend/missions/executor.py:142`) sets `skipped` with "task actions are not executed yet — no internal task surface" (`:165-174`). Open PR #11 (not merged) addresses this.
- **A mission may release its own approval under a consented grant.** `evaluate_action` (`backend/missions/policy.py:209`) sets `may_release` when the channel is in `auto_channels` and `consent_at` is set (`:257-261`), or when the autonomy dial allows it (`:262-264`). `_release` (`backend/missions/executor.py:297`) then calls `release_command` (`commands_api.py:1806`).
- **Commands have their own state ladder.** `command_executions.state` allows `draft` … `succeeded`, `failed`, `cancelled`, `reconciliation_required` (`0027_real_estate_intelligence_platform.sql:291-294`).
- **Jobs are leased with SKIP LOCKED and a lease token.** `automation_jobs.py:1-7` (module docstring), `claim_next_job` (`:230`), `_reap_exhausted_leases` (`:314`), `_LEASE_SECONDS` (`:36`), `max_attempts` default 5 (`:147`).
- **The builder UI calls create, simulate and launch.** `MissionBuilder.jsx:85`, `:101`, `:116`.

## 3. Goal

A mission is a typed task graph. Each step maps to a tool that exists and is permitted now. Effects are staged, approved, accepted, delivered and responded to as separate, recorded stages ([C6](CONTRACT.md#c6-truth-vocabulary-use-these-words-exactly)). Crashes, duplicates and revocations never repeat an effect or hide one. Agents and the owner see true status, never invented progress.

## 4. Engineering tasks

- **T08.01 Review open PR #11, then merge or rework it.** Branch `feat/mission-internal-tasks-executable`, commits `55ac99c` and `0b52fbf`. Check:
  - it rebases onto current `main`, and migration 0129 still follows the latest migration;
  - the `client_tasks` link is tenant-scoped under RLS, with grants;
  - created tasks are recorded as executed internal work, never as an outbound command;
  - `0b52fbf`'s direct-assignment rule matches 03's sharing model;
  - its tests run, with command and result.

  Pushing to `main` deploys staging; get approval first ([C4.4](CONTRACT.md#c4-external-actions-and-approvals)).
- **T08.02 Map the mission system.** Request model, planner, simulator, policy, executor, builder, command ledger and jobs: transitions, lease claims, retry and backoff, priority, callback races, cancellation, timeouts, cost and user-visible status. Reuse the job queue; add no workflow engine ([C7.2](CONTRACT.md#c7-reuse-the-architecture)).
- **T08.03 Define a typed `TaskGraph`.** Graph ID, initiating actor and tenant, 05 frame constraints, immutable step IDs, dependency edges, read / write / external effect, required evidence (06), permission and consent snapshot references, approval gate, deadline, retry policy, idempotency key, verifier, compensator, and time, money and token budgets.
- **T08.04 Check plan feasibility before approval.** Every step maps to a registered, available tool in 07's `ToolCapability` registry with its schema and permissions. The planner offers `task` today while the executor skips it; no plan claims a tool that does not exist.
- **T08.05 Parallelize only safe reads.** Serialize dependent writes and respect provider rate limits. Batch context. Cancellation or approval changes invalidate later decisions, not persisted facts.
- **T08.06 One state machine with allowed transitions.** Cover the old target states: draft, planned, awaiting approval, queued, leased, executing, verifying, completed, `degraded`, failed, compensating, cancelled, expired, unknown external result. Map them onto the existing mission, action, command and job states and onto C6; add missing states only by additive migration. A staged command is never "sent"; provider acceptance is never "delivered".
- **T08.07 Effectively once, not "exactly once".** Use durable idempotency keys where providers support them; otherwise reconcile receipts and hold an unknown outcome (`reconciliation_required`) instead of guessing ([C4.5](CONTRACT.md#c4-external-actions-and-approvals)).
- **T08.08 Recover at every boundary.** Before approval; after DB commit, before outbox publish; after a carrier accepts a call or text; before the callback; before verification; after the actor's access is revoked. Store receipts and the next reconciliation step.
- **T08.09 Check policy at plan time and at execution.** Membership, private record grants, provider credentials, do-not-contact and consent, spend budget and legal or financial approval ([C3.4](CONTRACT.md#c3-tenancy-roles-and-authorization)). The model cannot edit an approved plan to weaken policy.
- **T08.10 Handle irreversible effects honestly.** Offer compensation only where it is real; otherwise raise a visible incident for operator reconciliation. Retry only transient errors. A permission failure is terminal and needs re-approval.
- **T08.11 Show a truthful mission timeline.** Task graph, evidence, time and cost estimates with uncertainty, pending human steps, real external status, cancellation, measured progress only, and receipts. No invented completion percentages. Works at phone width.
- **T08.12 Personal vs shared missions.** Explicit access lists, audited delegation and role transfer. A team leader does not see a private mission by being in the same tenant.
- **T08.13 Collect learning data without causal claims.** Task types, receipts, time saved against a manual baseline, human corrections, false positives and failed steps (`backend/missions/learning.py`, `outcome_memory.py`). Test policy changes on offline scenarios before shipping.
- **T08.14 Run the failure scenarios.** 20 agents with overlapping missions; duplicate webhook; multi-worker claim; dropped Redis; DB restart; LLM timeout; provider 429; provider success with local timeout; owner revokes an agent; reassignment mid-mission; partial approvals; quota exhaustion; ambiguous provider state; erasure with queued tasks.
- **T08.15 Verify whether** any path cancels a mission or expires its actions, and add one if not (§2: nothing writes `cancelled`).

## 5. Failure and adversarial cases

- **F08.01 Silent skip.** A plan is presented as feasible, and its steps are then recorded as `skipped`.
- **F08.02 Duplicate effect.** A duplicate webhook, a second worker claim or a crash repeats a send.
- **F08.03 Stage reported as outcome.** A staged or accepted command is counted as sent or delivered.
- **F08.04 Unknown result guessed.** A provider timeout is retried as a failure (sending twice) or shown as success.
- **F08.05 Stale authority.** An agent is revoked or a client reassigned, and the mission keeps acting.
- **F08.06 Policy weakened after approval.** A re-plan changes a step's recipient, channel or content after a human approved it.
- **F08.07 Private mission exposed.** A team leader or another agent reads a private mission's contents.
- **F08.08 Invented progress.** The timeline shows a percentage nobody measured.
- **F08.09 Erasure ignored.** Queued tasks still contact or process an erased person.
- **F08.10 Runaway cost.** A mission exceeds its money, token or provider quota.

## 6. Acceptance criteria

- **AC08.01 PR #11 is resolved.** Merged with its tests run (command, exit code, counts), or a rework is recorded in L08. Evidence: PR review and test output. Covers: T08.01.
- **AC08.02 Plans are feasible or refused.** A step without an available tool is rejected at plan time with a named reason; no feasible-looking plan is later `skipped`. Evidence: planner and executor tests against the 07 registry. Covers: T08.02, T08.04, F08.01.
- **AC08.03 Graph and states are typed.** `TaskGraph` has a schema version; illegal transitions are refused; a mapping table links every state to C6. Evidence: schema and transition tests. Covers: T08.03, T08.06, F08.03.
- **AC08.04 Effects happen at most once, and unknowns are held.** Evidence: failure-injection tests for each T08.08 boundary, duplicate webhooks and two-worker claims, asserting one provider call and `reconciliation_required` on ambiguity. Covers: T08.07, T08.08, F08.02, F08.04.
- **AC08.05 Authority is rechecked at execution.** Revocation, reassignment, withdrawn consent, erasure and post-approval edits each block the effect. Evidence: real-PostgreSQL tests under the application role, across two agents and two brokerages. Covers: T08.09, T08.12, F08.05, F08.06, F08.07, F08.09.
- **AC08.06 Irreversible effects are honest.** Evidence: tests that a permission failure is terminal, a transient error retries within budget, and a non-compensable effect raises an incident. Covers: T08.10.
- **AC08.07 The timeline tells the truth.** No percentage without a measurement; every external status comes from a receipt. Evidence: component test and Playwright at desktop and phone width. Covers: T08.11, F08.08.
- **AC08.08 The scenario suite passes with bounded cost.** The T08.14 scenarios run with 20 agents, with p50/p95 and cost recorded. Evidence: committed scenario command and result with conditions. Covers: T08.05, T08.14, F08.10.
- **AC08.09 Learning makes no causal claim.** Evidence: test that learning output carries sample sizes and intervals, plus an offline policy-evaluation run. Covers: T08.13.
- **AC08.10 Missions can be cancelled.** Evidence: route and state test showing pending actions stop and no new effect starts. Covers: T08.15.

## 7. Out of scope, owner decisions, category-specific rules

- **Missions stay off by default.** Do not flip `ORACLE_FEATURE_MISSIONS` or `ORACLE_MISSIONS_ENABLED` on any shared environment without owner approval.
- **No autonomous purchase, charge or contact of real people** in any session ([C4.3](CONTRACT.md#c4-external-actions-and-approvals)).
- **Shared contract with 05, 06 and 07.** Same IDs, the same execution-time consent and permission check, and C6 words. 08 owns `TaskGraph` and the state mapping.
- **No new workflow engine or broker;** extend `automation_jobs.py`.
- **Owner question:** does a mission's consented grant (`policy.py:257-261`) count as "explicit, current human approval" under C4.1 and C4.2, given it releases content the human never saw?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the PR #11 review and outcome;
  - the `TaskGraph` schema and state-mapping table;
  - feasibility checks against the 07 registry;
  - recovery and idempotency tests; the cancel path;
  - the mission timeline UI; the scenario-suite results.
- **First slice:** T08.01 + AC08.01 (resolve PR #11), then T08.04 + AC08.02.
- **Ledger:** update L08 with the PR #11 outcome, then add L08-01 onward, per [C10](CONTRACT.md#c10-ledger-update-rule).
