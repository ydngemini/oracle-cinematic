# 05 — Semantic intent and contextual understanding

> **Scope:** Make Neoh understand what a person means (goals, references, corrections, negation, scope and constraints) through a typed, permission-checked semantic frame, while keeping the deterministic fast path. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 04 · **Unblocks:** 06, 07, 08, 20 · **Ledger:** [L05](LEDGER.md#l05) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Deterministic path: `backend/neoh_intents.py`, `backend/neoh_render.py`, `backend/intelligence_os_api.py` (`neoh_ask`)
- Model path: `backend/ai_chat_agent.py`, `backend/ai_chat_store.py`, `backend/llm_gateway.py`, `backend/neoh_persona.py`
- Context it can draw on: `backend/search_api.py`, `backend/intent_states.py`, `backend/belief_store.py`, `backend/memory_core/session_manager.py`
- Frontend: `oracle-app/src/neoh/NeohConversation.jsx`, `oracle-app/src/neoh/useNeohComposer.js`, `oracle-app/src/neoh/useSpeechInput.js`, `oracle-app/src/neoh/useNeohChannel.js`
- Tests: `backend/tests/test_neoh_intents.py`, `backend/tests/test_search_api.py`
- Sibling prompts that share this frame: [06](06-evidence-retrieval-memory.md) (evidence), [07](07-ai-gateway-model-tool-governance.md) (tools), [08](08-durable-missions-workflow-orchestration.md) (missions); budgets in `docs/performance-targets.md`; [audit](../neoh-full-platform-source-audit-2026-10-08.md)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **The ⌘K fast path is five ordered regular expressions over four intents, with no model.** `neoh_intents.py:163` `INTENTS` holds `properties_for`, `deal_blocker`, two `who_to_call` patterns and `show_person`; the module docstring (`:1-22`) states the model is not in this path.
- **A miss or a broken resolver falls through to chat.** `neoh_intents.py:207` `ask` returns `render.fallthrough` for no match and for any resolver exception (`:229-232`). `useNeohComposer.js:60` makes the `/api/neoh/ask` call, and on fallthrough `:68` sends the same text down the chat channel (`channel.send`).
- **Word order decides the match.** The `properties_for` pattern (`neoh_intents.py:166-169`) needs "homes for <name>", so "Find Sarah homes" falls through.
- **Two or more name matches produce a choice, not a guess.** `_show_person` (`neoh_intents.py:69`) and `_properties_for` (`:107`) call `render.person_choices` when `len(matches) > 1` (`:91`, `:119`), capped by `NAME_MATCH_LIMIT = 5` (`:38`).
- **A failed search leg reads as "Nothing matches".** `search_api.search` (`search_api.py:351`) reports failed legs in `degraded` (`:370-374`), but `neoh_intents.py` never reads `degraded`, so `_show_person` answers "Nothing matches" (`:88`).
- **An open record bypasses record-agnostic intents.** `RECORD_AGNOSTIC` (`neoh_intents.py:204`) sends "who should I call" to the model when `has_record` is set (`:225-226`).
- **The fast path is feature-gated.** `intelligence_os_api.py:80` calls `require_feature(Feature.PREDICTIVE_INTELLIGENCE)` inside `neoh_ask` (`:70`).
- **Only final speech submits a turn.** `useNeohComposer.js:38` handles `onFinal`; while Neoh is busy the words go into the draft instead (`:39-42`).
- **Selected-record text is fenced as untrusted data.** `ai_chat_agent.py:1150` labels the record block "SELECTED RECORD — UNTRUSTED DATA" inside `_generate` (`:1119`).
- **Chat history is trimmed by characters, not by meaning.** `_compact_history` (`ai_chat_agent.py:437`) keeps up to `max_chars=40_000`.
- **Declared and observed intent are kept apart.** `intent_states.py:353` `read_intent` returns an `evidence_state` of `observed`, `weak` or `unobserved` (`:156`).
- **No typed semantic frame exists.** `grep -rn -i "SemanticFrame" backend oracle-app/src` returns nothing.

## 3. Goal

Neoh understands requests such as "Find Sarah homes", "What about the one yesterday?", "Would she qualify?", "Don't message her yet", "Actually the other Sarah", "Show our team's stuck deals" and "Do it after the manager signs off". Meaning is captured in a typed frame that tools and missions consume. A regex match or a model guess never creates an identity, a permission or a send.

## 4. Engineering tasks

- **T05.01 Keep and test the deterministic shortcuts.** Map every pattern in `INTENTS`, its resolver and its fallthrough. Keep exact high-precision commands on this path and add a typed semantic layer behind it for paraphrases, temporal references and compound requests.
- **T05.02 Define a versioned `SemanticFrame`.** Fields:
  - goals and intent labels; entity mentions with candidate canonical IDs (IDs from 04);
  - time interpretation with time zone; negative constraints and user instructions;
  - scope (mine, team, shared); required data and freshness;
  - permitted effect class (read, internal edit, external effect, per `ai_tool_policy.py`);
  - unfilled slots, ambiguities and the conversational evidence for each.
- **T05.03 Extract with a small structured model call.** Use `llm_gateway` structured output with schema validation. Validate every candidate ID against current brokerage membership and record permissions before treating it as authorized. "Call her" or a retrieved note never authorizes a number.
- **T05.04 Keep short-lived discourse context.** Track pronouns ("her"), deixis ("that house"), the previous search, corrections, interruptions, cancelled intents and repairs. A clarification amends the frame; it never stacks a contradictory action.
- **T05.05 Route by task.** Deterministic first, then the semantic parser, then evidence retrieval (06), then the planner (07, 08). Decompose complex requests; simple ones never pay for a large model or the whole tool list.
- **T05.06 Separate stated, observed and inferred intent.** Reuse `intent_states.py`. State assumptions plainly. Abstain from protected-class profiling, steering and unsupported eligibility claims.
- **T05.07 Carry action constraints end to end.** "No send", "approval required", "all assigned clients", "draft only" and time-bound reminders stay machine-readable in the frame through 08 missions, voice turns and UI confirmations.
- **T05.08 Treat transcripts, listings, web pages and CRM notes as evidence only.** Version the trusted/untrusted boundary (extend the fence at `ai_chat_agent.py:1150`) and test it.
- **T05.09 Act on speech only when it is final.** Partial or unstable phrases never trigger a consequential action. Resolve homophones in names and addresses through grounded disambiguation (with 09).
- **T05.10 Ask one precise question.** When several entities fit, name the choices in UI and voice (extend `person_choices`). Never fabricate a record to avoid asking.
- **T05.11 Report failed lookups honestly.** The fast path reads `degraded` from `search_api.search` and says a source failed, using the per-source status from 06 ([C6](CONTRACT.md#c6-truth-vocabulary-use-these-words-exactly)), instead of "Nothing matches".
- **T05.12 Evaluate understanding separately** from execution success (07, 08) and factual grounding (06). Use real-estate paraphrase sets and adversarial utterances to find over-eager tool calls.
- **T05.13 Build the evaluation suite.** Cases:
  - same intent, different wording; same wording, different goals;
  - two Sarahs; "don't" constraints; time-zone ambiguity;
  - changed or withdrawn instructions; an unauthorized manager request;
  - source text that tries to override instructions; an incomplete voice transcript; a long-context conflict.

  Score intent, extraction and entity accuracy, constraint preservation, clarification rate, abstention and unauthorized-action count, on held-out utterances.
- **T05.14 Measure latency and cost** of the semantic layer against the chat and voice budgets in `docs/performance-targets.md` (target, not measured).

## 5. Failure and adversarial cases

- **F05.01 Regex over-reach.** A fast-path pattern captures a request that means something else and renders a confident wrong answer.
- **F05.02 Invented identity.** The model or parser resolves "Sarah" to an ID the actor cannot see, or to a record that does not exist.
- **F05.03 Lost negation.** "Don't message her yet" reaches the planner as "message her".
- **F05.04 Stacked correction.** "Actually the other Sarah" leaves the first Sarah's action queued.
- **F05.05 Injected instruction.** Text in a note, transcript or listing changes the goal, the recipient or the permitted effect.
- **F05.06 Partial speech acts.** An unstable voice fragment triggers a consequential action.
- **F05.07 Scope escalation.** "Our team's deals" returns records outside the actor's delegated scope.
- **F05.08 Failure read as absence.** A failed search leg is shown as "Nothing matches".
- **F05.09 Profiling.** An inferred motivation uses or implies a protected characteristic.

## 6. Acceptance criteria

- **AC05.01 Fast path preserved.** Every existing intent still resolves its exact commands, and misses still fall through. Evidence: `backend/tests/test_neoh_intents.py` plus new paraphrase cases, run with counts. Covers: T05.01, F05.01.
- **AC05.02 The frame is typed and validated.** A `SemanticFrame` schema version exists, and an invalid frame is rejected before planning. Evidence: schema contract test (proposed `backend/tests/test_semantic_frame.py`). Covers: T05.02, T05.03, T05.07.
- **AC05.03 Identity is authorized, never invented.** Unknown or forbidden candidates are dropped or produce a clarification. Evidence: real-PostgreSQL RLS test with two agents and two brokerages. Covers: T05.03, T05.10, F05.02, F05.07.
- **AC05.04 Constraints survive.** Negation, draft-only and approval constraints reach 07 tool calls and 08 mission steps unchanged. Evidence: end-to-end test from utterance to staged command. Covers: T05.04, T05.07, F05.03, F05.04.
- **AC05.05 Untrusted text cannot steer.** Injected instructions in notes, transcripts and listings change nothing. Evidence: adversarial suite run and scored. Covers: T05.08, F05.05.
- **AC05.06 Voice acts only on final turns.** Evidence: component test of `useNeohComposer.js` with partial and final events, plus a homophone set. Covers: T05.09, F05.06.
- **AC05.07 A failed leg is never "no results".** Evidence: test that forces a leg to raise and asserts a `degraded` answer with per-source status. Covers: T05.11, F05.08.
- **AC05.08 Understanding is scored on held-out data.** The T05.13 suite runs, with scores for each metric. Evidence: committed eval command and result. Covers: T05.05, T05.06, T05.12, T05.13, F05.09.
- **AC05.09 Cost and latency are measured.** p50/p95 and tokens per turn for the semantic layer, with conditions. Evidence: benchmark command and result. Covers: T05.14.

## 7. Out of scope, owner decisions, category-specific rules

- **Shared contract with 06, 07 and 08.** The four prompts use one set of IDs (canonical person, property, deal IDs from 04), one consent and permission check at execution ([C3.4](CONTRACT.md#c3-tenancy-roles-and-authorization)), and C6 status words. 05 owns `SemanticFrame`; 06 owns the evidence bundle and per-source status; 07 owns tool capability and receipts; 08 owns the task graph.
- **Model output is never authorization.** The frame proposes; the server decides ([C4.1](CONTRACT.md#c4-external-actions-and-approvals)).
- **No new NLU framework** unless a benchmark shows a need ([C7.2](CONTRACT.md#c7-reuse-the-architecture)).
- **Fair housing:** no protected-characteristic inference, steering or eligibility assertions.
- **Owner question:** which languages must the semantic layer support at launch?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the `SemanticFrame` schema and validator;
  - the routing change (deterministic, semantic, evidence, planner);
  - the discourse-context store with its retention rule;
  - the evaluation suite and scored results;
  - the latency and cost benchmark.
- **First slice:** T05.11 + AC05.07 (failed legs are not "no results"), then T05.02 + T05.03 + AC05.02 + AC05.03.
- **Ledger:** add L05-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
