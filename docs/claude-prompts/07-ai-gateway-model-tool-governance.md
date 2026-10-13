# 07 — AI gateway, model routing and governed tools

> **Scope:** Make model use reliable and tool use explicitly governed: the model chooses only among truthful, available capabilities, the backend validates authority, tools return typed receipts, and the UI reports actual outcomes. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03, 05, 06 · **Unblocks:** 08, 09, 10, 14, 20 · **Ledger:** [L07](LEDGER.md#l07) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Gateway: `backend/llm_gateway.py`, `backend/billing_usage.py`, `backend/tests/test_llm_gateway.py`, `backend/tests/test_llm_gateway_structured.py`
- Chat agent and tool offer: `backend/ai_chat_agent.py`, `backend/ai_chat_store.py`, `backend/neoh_persona.py`
- Tool surface:
  - `backend/ai_tool_policy.py`, `backend/platform_policy.py`
  - `backend/ai_tools_read.py`, `backend/ai_tools_gated.py`
  - `backend/tests/test_ai_tool_policy.py`, `backend/tests/test_ai_tools_gated.py`, `backend/tests/test_ai_tools_read.py`
- Approval and execution: `backend/approval_service.py`, `backend/commands_api.py`, `backend/command_providers.py`, `backend/outreach_compliance.py`
- Personalization: `backend/agent_twin.py`
- Sibling prompts: [05](05-semantic-language-intent.md), [06](06-evidence-retrieval-memory.md), [08](08-durable-missions-workflow-orchestration.md), [11](11-property-market-intelligence.md) (T11.01); [audit](../neoh-full-platform-source-audit-2026-10-08.md) A09

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **One gateway, two task ladders.** `llm_gateway.py:174` `_TASK_MODELS` defines `analysis` and `fast` ladders over Fireworks, Foundry, Bedrock and local; `providers_for` (`:206`) returns the configured ones in order.
- **Tool rounds never fall back; text completions may.** `tool_call` (`llm_gateway.py:565`) sends to one provider with no fallback (docstring `:576-583`); `complete` (`:387`) may fall back within one deadline.
- **litellm is told not to log request bodies.** `_litellm` (`llm_gateway.py:299`) sets `suppress_debug_info` and `drop_params`.
- **Call counts are in-process only.** `_Counter` (`llm_gateway.py:221`), read by `admin_ops.py:166` via `ambient_calls_last_minute`.
- **Token usage is metered when the provider reports it.** `billing_usage.py:100-102` calls `token_usage_of` and records nothing for a missing field.
- **The chat catalog has 126 tools; 126 have a risk class.** `TOOLS` (`ai_chat_agent.py:273`) and `TOOL_RISK` (`ai_tool_policy.py:50`), each counted by grep over its dict body.
- **Only an allowlist of 57 names can be offered.** `_AGENT_AVAILABLE_TOOLS` (`ai_chat_store.py:605-643`) unions explicit names with `TOOLS_HANDLED` from `ai_tools_read.py:54` (28 names); `is_agent_tool_available` is at `ai_chat_store.py:646`.
- **The offer depends on the open record.** `offered_tool_names` (`ai_chat_agent.py:558`) combines `_ALWAYS_TOOLS` (`:494`) with `_CONTEXT_WRITE_TOOLS` (`:540`) for the record type.
- **The executor refuses anything not offered.** `execute_safe_tool` (`ai_chat_store.py:1811`) returns "not available for this conversation" (`:1833-1834`).
- **Consent-writing tools are banned by name.** `CONSENT_WRITE_NAMES` (`ai_tool_policy.py:36`).
- **Gated tools stage and never call a provider; facts come from records.** `ai_tools_gated.py:1-20` (module docstring); tested by `test_a_gated_tool_will_not_take_a_phone_number_from_the_model` (`test_ai_tool_policy.py:181`).
- **Approvals are bound to a payload hash.** `create_approval` (`approval_service.py:39`) stores `payload_hash(draft)` (`:53`).
- **A reply that claims staging without a tool gets a note.** `_guard_unbacked_claims` (`ai_chat_agent.py:808`).
- **The persona tells the model to claim only offered capabilities.** `HONESTY` (`neoh_persona.py:56`).
- **Two read tools still query legacy MLS data.** `_search_listings` (`ai_tools_read.py:402`) and `_get_days_on_market` (`:1022`) read `oracle_mls_listings`. Gating them is 11 T11.01.
- **Turn budgets are env-configured.** `_TURN_DEADLINE_SECONDS` 90 (`ai_chat_agent.py:34`), `_LOCAL_TOOL_ROUNDS = 3` (`:53`), `_HOSTED_TOOL_ROUNDS` 6 (`:60`).

## 3. Goal

The model picks from a small set of tools that really exist and are usable for this actor now. The backend, not the model, decides authority. Every effect returns a typed receipt on the C6 ladder (staged, approved, accepted, delivered, responded), and the UI never reports an earlier stage as a later one.

## 4. Engineering tasks

- **T07.01 Trace one request end to end.** Chat and mission requests through routing, prompt context, offered tools, validation, staging, approval, execution and receipts. Inventory tool contracts and policy classes. Keep tool names and public APIs compatible ([C7.3](CONTRACT.md#c7-reuse-the-architecture)).
- **T07.02 Versioned `ToolCapability` registry.** Per tool: purpose, input/output schema version, record types, read / internal edit / external effect, permission and consent prerequisites, confirmation rule, provider state, latency and cost estimate, idempotency class, timeout, retry class, verifier and audit category. Derive it from `TOOL_RISK`; don't restate it.
- **T07.03 The planner proposes; it never grants.** The backend resolves canonical contact IDs and verified destinations. A model-supplied phone or email is never a send destination. Human approval stays for outreach, spend, legal actions, publishing and calendar changes ([C4.1](CONTRACT.md#c4-external-actions-and-approvals)).
- **T07.04 Offer a small, eligible subset.** Detect unavailable tools before planning, and name the missing integration or permission instead of silently skipping (with 08 T08.03).
- **T07.05 Route models by evidence.** Use validated task complexity (05 frame), structured-output reliability, measured benchmark results, cost, privacy and region limits, queue state and SLO. Keep the deterministic path for simple tasks; never default to the most expensive model.
- **T07.06 Typed intermediate artifacts.** Parsed frame (05), evidence bundle (06), plan, proposed operation, staged approval, provider acceptance, delivery and outcome. An HTTP 200 is not business success.
- **T07.07 Loop budgets.** Turns, parallel reads, side-effect order, retries, timeouts, tokens, rate limits, provider spend, tenant and user budgets. A budget hit returns an actionable `degraded` state.
- **T07.08 Correlation ID end to end.** From the user request through plan, tool call, message or appointment, provider callback and outcome. Keep the action ledger and idempotency; revalidate membership and consent at execution ([C3.4](CONTRACT.md#c3-tenancy-roles-and-authorization)).
- **T07.09 Injection hardening.** Notes, emails, transcripts, web pages and property details never change permissions, recipients, model behaviour or policy. Test attempted cross-record disclosure.
- **T07.10 Validation pipeline.** Schema parse, business invariants, RLS checks, record existence, PII controls, destination verification, consent, provider capability, dry-run for high-risk operations, and outcome reconciliation. A model judging a model is never the only verifier.
- **T07.11 Bound the agent twin.** `agent_twin.py` learns verified preferences and past decisions as advice only. It never authorizes sends, reprioritizes private clients or makes financial or legal assertions.
- **T07.12 End-to-end evals.** Tool choice, arguments, abstention, zero unauthorized commands, citation quality, provider failures, interruption, races, replay, grounding, p50/p95 model time and cost per successful action.
- **T07.13 Re-verify and close audit A09.** Compare, per capability: the persona and onboarding capability text (`neoh_persona.py`, `brokerage_onboarding.py`), the UI labels, the API metadata, the 126-entry catalog and the 57-name allowlist. Each capability shows one state: available, approval-gated, `degraded`, setup required or unavailable. Remove or gate any description without a handler, licensed data or a safe effect path.
- **T07.14 Keep legacy MLS tools out of the offer.** Reference 11 T11.01 for `_search_listings` and `_get_days_on_market`; add the registry check here so they cannot be re-offered without a decision.
- **T07.15 Verify whether** `_Counter` and token metering give per-tenant spend, or only per-process counts, and record the gap for T07.07.

## 5. Failure and adversarial cases

- **F07.01 Phantom capability.** The model or UI describes an action no handler can perform (A09).
- **F07.02 Model-chosen destination.** A phone number or email written by the model reaches a provider.
- **F07.03 Fallback replays writes.** A tool round is retried on a second provider and repeats CRM writes.
- **F07.04 Unoffered tool executes.** The model emits a tool name it was not offered, and it runs.
- **F07.05 Injection escalates.** Record or web text changes a recipient, permission or policy.
- **F07.06 Stage reported as success.** A staged or accepted command is shown as delivered or completed.
- **F07.07 Runaway loop.** A turn exceeds its round, token or spend budget without a `degraded` result.
- **F07.08 Stale authority.** Consent or membership is revoked after approval, and execution proceeds.

## 6. Acceptance criteria

- **AC07.01 Registry and policy agree.** Every catalog tool has a capability entry and a risk class, and every offered tool has a handler. Evidence: extended `backend/tests/test_ai_tool_policy.py`, run with counts. Covers: T07.01, T07.02, T07.04.
- **AC07.02 Capability claims match reality (closes A09).** UI, API metadata, persona text and offered tools agree for every capability. Evidence: snapshot test over all four surfaces plus a Playwright check. Covers: T07.13, T07.14, F07.01.
- **AC07.03 Destinations come from records.** Evidence: existing and new gated-tool tests for phone, email and calendar targets. Covers: T07.03, F07.02.
- **AC07.04 Tool rounds are never retried across providers.** Evidence: gateway test with a failing first provider that asserts one write. Covers: T07.07, F07.03.
- **AC07.05 Unoffered and injected calls are refused.** Evidence: adversarial suite (unoffered names, injected notes, cross-record requests), run and scored. Covers: T07.09, F07.04, F07.05.
- **AC07.06 Receipts use the C6 ladder.** Each stage is recorded separately and the UI shows the true stage. Evidence: receipt contract test and component test. Covers: T07.06, T07.08, F07.06.
- **AC07.07 Execution revalidates authority.** Revoked consent or membership between approval and execution blocks the effect. Evidence: real-PostgreSQL test under the application role. Covers: T07.08, T07.10, F07.08.
- **AC07.08 Routing and budgets are measured.** p50/p95 model time, tokens and cost per successful action, with conditions, and budget hits produce `degraded`. Evidence: committed eval command and result. Covers: T07.05, T07.07, T07.12, T07.15, F07.07.
- **AC07.09 The twin advises only.** Evidence: test that a twin preference cannot release or reorder an action. Covers: T07.11.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS is out of scope** (D-2026-10-12-01). No legacy MLS tool may be offered; removal is 11 T11.01.
- **Shared contract with 05, 06 and 08.** Same IDs, the same execution-time consent and permission check, and C6 status words. 07 owns `ToolCapability`, receipts and the correlation ID.
- **Retries and tool calls do not mix.** Keep the `tool_call` no-fallback rule in `llm_gateway.py`.
- **No consent-writing tool,** ever (`CONSENT_WRITE_NAMES`).
- **Owner question:** which model providers and regions are approved for customer data in production?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the `ToolCapability` registry and its tests;
  - the A09 capability-agreement report and fixes;
  - the validation pipeline and receipt contract;
  - routing traces with cost; the evaluation suite.
- **First slice:** T07.13 + AC07.02 (re-verify and close A09), then T07.02 + AC07.01.
- **Ledger:** add L07-01 onward for each slice, and update the A09 row, per [C10](CONTRACT.md#c10-ledger-update-rule).
