# Claude implementation prompt 07: AI model gateway, capabilities, tool contracts and action validation

> Repository: `ydngemini/oracle-cinematic` shipping FastAPI + React/Vite. Goal: full-feature browser SaaS, one brokerage and **20 agents**, with a separately accounted owner, not a cut-down MVP.
> This is a specification and task prompt, NOT evidence of completed code, production deployment, provider licensing or tested results.

## Claude: implementation assignment

Act as principal architect and hands-on engineering owner. Read `CLAUDE.md`, `README.md` in this prompt folder, the existing platform audit, and the actual current source. Pin main SHA and inspect path:line, tests, migrations, configurations and provider state before coding. Prioritize verifiable full vertical slices rather than replacing entire systems with unproven frameworks.

**Inspect first:** backend/llm_gateway.py; backend/ai_chat_agent.py; backend/ai_chat_store.py; backend/ai_tool_policy.py; backend/ai_tools_read.py; backend/ai_tools_gated.py; backend/command_providers.py; backend/commands_api.py; backend/agent_twin.py

## Work program

### Goal
Make model reasoning reliable and tool use explicitly governed: model chooses among truthful available capabilities, backend validates authority, tools return typed receipts, and UI reports *actual* outcomes without inventing success.

### Steps
1. Trace chat and mission requests through model routing, prompt context, tools available to model, validation, command staging, user approval, execution and durable receipts. Inventory tool contracts and existing policy classes. Maintain exact tool names and public API compatibility where feasible.
2. Create versioned ToolCapability registry: semantic purpose, input/output JSON Schema or Pydantic version, resource/record types, read/write/external effect, permission/consent prerequisites, confirmation required, connected provider state, estimated latency, cost budget, idempotency class, timeout, retry class, verifier and audit category.
3. Planner may propose but NOT grant permissions. Backend resolves canonical contact IDs and verified phone/email values. Never accept a phone number synthesized by a model as an authorized send destination. Keep human approval for outbound communication, spend, sensitive legal actions, publishing and calendar changes per existing policy.
4. Dynamically expose a small relevant eligible tool subset rather than entire catalog. Detect unavailable tools before planning; explain which missing integration/permission prevents success instead of silently skipping unsupported internal tasks.
5. Extend gateway to route by validated task complexity, provider/model capability, language, structured-output reliability, real benchmark performance, cost, privacy/region constraints, queue availability and SLO. Preserve deterministic shortcut for simple tasks; don't use highest-cost model by default.
6. Enforce typed intermediate artifacts: parsed intent, evidence bundle, plan, proposed operation, staged approval, provider acceptance, delivery/verification and externally observable outcome. Distinguish “tool returned HTTP 200” from business success.
7. Enforce tool loop budgets: total turns, parallel reads, side-effect order, retries, timeout, tokens, rate limits, provider spend, tenant budget, user budget and policy restrictions. Bounded tool errors must return actionable retry/degraded state.
8. Preserve effectful operation ledger and request idempotency; add correlation ID from user request through plan, tool call, message/appointment/provider callback and business result. Revalidate membership and consent at execution after approval.
9. Harden against prompt injection from client notes, emails, transcripts, web pages and retrieved property details. Untrusted source never changes tool permission, recipient, model behavior or policy hierarchy; test attempted cross-record disclosure.
10. Implement validation pipeline: schema parse, business invariants, authoritative RLS checks, record existence, PII controls, destination verification, legal consent, provider capability, simulation/dry-run for high-risk ops, final outcome reconciliation. Avoid “LLM judges LLM” as the sole verifier.
11. Maintain agent-twin personalization as bounded advice: learns explicitly verified work preferences and historical decisions, not authority to send texts, reprioritize private clients or make financial/legal assertions.
12. Build end-to-end evals for correct tool selection, arguments, abstention, no unauthorized commands, response citation quality, provider failures, interruption/race/replay, grounding, p50/p95 model time and cost per successful action.

### Definition of done
A reproducible tool registry, policy and verifier tests, consistent model gateway traces, truthful capability UI, instrumented routing choices, no unauthorized side effects, and an evidence-backed action-completion rate in real workflows.

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
