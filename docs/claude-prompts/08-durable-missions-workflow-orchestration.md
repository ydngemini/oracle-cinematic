# Claude implementation prompt 08: Durable AI missions, task graphs, autonomy, events and recovery

> Repository: `ydngemini/oracle-cinematic` shipping FastAPI + React/Vite. Goal: full-feature browser SaaS, one brokerage and **20 agents**, with a separately accounted owner, not a cut-down MVP.
> This is a specification and task prompt, NOT evidence of completed code, production deployment, provider licensing or tested results.

## Claude: implementation assignment

Act as principal architect and hands-on engineering owner. Read `CLAUDE.md`, `README.md` in this prompt folder, the existing platform audit, and the actual current source. Pin main SHA and inspect path:line, tests, migrations, configurations and provider state before coding. Prioritize verifiable full vertical slices rather than replacing entire systems with unproven frameworks.

**Inspect first:** backend/missions/planner.py; backend/missions/executor.py; backend/missions/evaluator.py; backend/missions/simulator.py; backend/missions/policy.py; backend/missions/learning.py; backend/automation_jobs.py; backend/commands_api.py; backend/outcome_memory.py; oracle-app/src/neoh/MissionBuilder.jsx

## Work program

### Mission
Neoh coordinates multi-step work across CRM, property research, drafts, communications, showings and approvals while ensuring durable execution, recoverable partial failures and accurate progress. No unsafe fully autonomous contact or purchase.

### Program
1. Inspect mission request model, planner, simulator, policy evaluator, executor, user-facing builder, command ledger and background jobs. Map transitions, lease claims, retry/backoff, job priority, callback races, cancellation, timeouts, cost and user-visible status. Reuse existing job infrastructure; don't add another workflow engine blindly.
2. Define typed TaskGraph: graph ID, initiating actor/tenant, intent constraints, immutable step IDs, dependency edges, read/write/external effect, required evidence, permission and consent snapshot references, approval gate, deadline, retry policy, idempotency identity, verifier, compensator, time/financial/token budget.
3. Model plan feasibility before approval: every proposed step must map to a registered currently available tool with supported schema and permissions; inspect noted mission executor path that skips unsupported internal task actions. Do not produce plans that claim tools exist when they do not.
4. Parallelize independent safe reads only; serialize dependent writes and protect provider rate limits. Batch/compress context where possible; cancellation and approval changes invalidate later decisions, not persisted facts.
5. Model states DRAFT, PLANNED, AWAITING_APPROVAL, QUEUED, LEASED, EXECUTING, VERIFYING, COMPLETED, DEGRADED, FAILED, COMPENSATING, CANCELED, EXPIRED, and UNKNOWN_EXTERNAL_RESULT with explicit allowed transitions. Do not count a staged command as sent, or API acceptance as delivered.
6. Ensure at-least-once worker delivery can produce effectively once externally by durable idempotency keys when providers support them; otherwise reconcile provider receipts and tolerate unknown outcomes rather than falsely promising universal exactly-once side effects.
7. Design safe recovery after crashes at each boundary: before approval, after DB commit before outbox publish, after carrier accepted call/text, before callback, before verification, after user's access revoked. Store receipts and next reconciliation step.
8. Use policy/authorization check at plan and at execution, including current membership, private record grants, provider credentials, no-contact consent, credit/card budget and legal/financial approval. Do not let model edit the plan to weaken policy after human approval.
9. For irreversible external effects (email, SMS, call), offer compensation only when actually possible; otherwise visible incident and operator reconciliation. Retry only transient errors; permission failure is terminal/reapproval.
10. Expose a clear agent/owner mission timeline: task graph, source evidence, estimated time/cost with uncertainty, pending human steps, actual external status, cancellation, progress when measured and audit receipts. Disallow fabricated completion percentages.
11. Integrate actor-visible shared brokerage missions vs personal missions through explicit ACL, and include delegation and role transfer with audited scope. A team leader cannot see private mission contents by being on same tenant.
12. Collect learning data without claiming causal lift: completed task types, durable receipts, time saved with manual baseline, human corrections, false positives and failed steps. Use offline scenario tests to evaluate policy improvements before shipping.

### Test cases
20 agents schedule overlapping missions; duplicate webhook; multi-worker claim; dropped Redis; DB restart; LLM timeout; provider 429; provider success but local timeout; owner revokes agent; reassignment halfway through mission; partial approvals; quota exhaustion; external API returns ambiguous state; GDPR/DPDPA erasure with queued tasks.
### Done
Task graph real, demonstrated durability, bounded cost, truthful status, replay protection, clear owner/agent authority, tested failure recoveries, and instrumented meaningful business outcomes.

## Global acceptance discipline

- **All pages in scope**, including nested Work, Deals, Properties, Sales, AI, settings and public capability routes. Available page does not imply working external integration; implement or truthfully surface setup required, no false “live.”
- Enforce tenant/agent/team/owner permissions at database, API, AI context, jobs, media, search and caches. Do not treat the model as authorization, or legal/financial/property inference as verified evidence.
- Preserve current human approval before external sends, calls, publishing, calendar writes, financial and legal decisions; verify final provider events; use durable idempotency for effectful actions.
- Preserve product tech decisions: PostgreSQL RLS, FastAPI, React/Vite, PlayCanvas/gsplat for tours, browser-only spatial capture, existing workflows and endpoint names unless planned backwards-compatible migration is tested.
- Require tests: unit/contracts, real Postgres/RLS, UI/Playwright, load/failure-injection, provider sandbox/live *only with approved synthetic destinations*, cross-brokerage access, 20-account end-to-end. No unapproved production deployment, charges, sending or real customer calls.
- Distinguish proposal vs code change, mocked vs real benchmark, verified vs inferred, staged vs delivered, deployment staging vs production. Add p50/p95 and cost evidence where relevant.
- Emit source-linked baseline, concrete implementation sequence, tested commits, schema/rollout plan, role/privacy matrix, outcome receipts, blockers and verification reports. Never report completion without executing the relevant tests.

## Final response expected from Claude

Files changed with exact references; tests and their outputs; measured performance where possible; residual known failures; release gate status; recommended next vertical slice. Update the category tracking matrix and existing docs, avoiding parallel contradictory sources of truth.
