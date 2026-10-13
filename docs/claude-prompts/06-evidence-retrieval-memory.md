# 06 — Evidence retrieval, hybrid search and memory

> **Scope:** Give Neoh and its dashboards only relevant, authorized, fresh and cited evidence, through one evidence contract, one context compiler, named retrieval legs with honest per-source status, and memory tiers that can be corrected and erased. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03, 04, 05 · **Unblocks:** 07, 08, 09, 11, 20 · **Ledger:** [L06](LEDGER.md#l06) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Search: `backend/search_api.py`, `backend/contacts_api.py` (`search_contact_rows`, blind index), `backend/tests/test_search_api.py`
- Living graph and memory:
  - `backend/belief_store.py`, `backend/db/migrations/0095_perception_and_living_graph.sql`
  - `backend/intent_states.py`, `backend/outcome_memory.py`, `backend/agent_twin.py`
  - `backend/memory_core/session_manager.py`, `backend/db/migrations/0009_agent_onboarding.sql`
- Consumers: `backend/ai_chat_agent.py` (`_generate`), `backend/client_ai_automation.py`, `backend/intelligence_os_api.py`, `backend/neoh_intents.py`
- Coverage and privacy: `backend/data_coverage.py`, `backend/privacy_data_map.py`, `docs/privacy-data-map.md`
- Sibling prompts: [05](05-semantic-language-intent.md) (frame), [07](07-ai-gateway-model-tool-governance.md) (tools), [08](08-durable-missions-workflow-orchestration.md) (missions), [11](11-property-market-intelligence.md) (property sources); [audit](../neoh-full-platform-source-audit-2026-10-08.md) A01, A02

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **One search fans out over five named legs.** `search_api.py:342` `LEGS` maps `people`, `properties`, `deals`, `conversations` and `records`; `DEFAULT_KINDS` (`:52`) omits `records` unless the query looks like an address (`:358-359`).
- **Each leg has a statement budget.** `LEG_TIMEOUT_MS = 2000` (`search_api.py:73`), applied by `_budget` (`:76`) with `SET LOCAL statement_timeout`.
- **A failed leg is named, but not typed.** `search` (`search_api.py:351`) puts any leg that raised into `degraded` and sets its count to 0 (`:370-374`). There is no `checked` / `timed_out` / `unavailable` value per leg.
- **People search uses plaintext clients plus blind-indexed contacts.** `_people` (`search_api.py:128`) runs `ILIKE` over `clients` and calls `search_contact_rows` for encrypted contacts.
- **The property leg reads Neoh's own tables.** `_properties` (`search_api.py:184`) selects from `leads` and `listings`.
- **Beliefs carry provenance, decay at read time and surface disputes.** `belief_store.py:161` `assert_belief`, `:88` `effective_confidence`, `:58` `HALF_LIFE_DAYS`; `revision_state` allows `active`, `superseded`, `retracted`, `disputed` (`0095_perception_and_living_graph.sql:138`).
- **Model extractions become beliefs with source kind `model`.** `client_ai_automation.py:480` (`_assert_client_beliefs`, `:465`).
- **Intent readings say when nothing was observed.** `intent_states.py:353` `read_intent`; `evidence_state` is `observed`, `weak` or `unobserved` (`:156`).
- **Outcomes are recorded and attributed by a simple last-touch rule.** `outcome_memory.py:133` `record_outcome`, `:349` `attribute_pending`.
- **Agent-twin rates use Wilson intervals.** `agent_twin.py:86` `wilson_interval`, `:261` `policy`.
- **Every chat turn is stored as plaintext.** `SessionManager.record_interaction` (`session_manager.py:273`) inserts into `user_interactions`, called from `ai_chat_agent.py:1378-1379`. `privacy_data_map.py:135` notes "raw chat turns plaintext" (audit A01).
- **A failed property lookup in client automation returns an empty list.** `client_ai_automation.py:700` inside `_property_candidates_bounded` (`:680`). This is audit A02, owned by 11 (T11.10).
- **No embedding or vector search exists in tracked code.** `git grep -i "embedding\|pgvector\|fastembed" -- backend oracle-app/src` returns nothing.
- **No permission-revision identifier exists for cache keys.** The nearest is the per-user `session_epoch` (`auth.py:348`).

## 3. Goal

For every agent and customer journey, Neoh finds the right records, joins them across systems and separates facts from claims and suggestions. It sends a model only the evidence the actor may see, with sources, freshness and explicit unknowns. A failed source is never a verified zero.

## 4. Engineering tasks

- **T06.01 Build a source catalog.** Cover customer, lead, property (see 11), transaction, communication and event sources. Record schema, identifiers, tenant and agent access, staleness, licence, retention and legal limits. Use C6 classes: fact, inferred, predicted, provisional, plus the existing `disputed` marker. No single undifferentiated "confidence" number.
- **T06.02 Define a versioned `EvidenceFact` and `EvidenceBundle`.** Fields:
  - immutable fact ID, canonical subject (04 IDs), predicate, typed value and unit;
  - source IDs, observation time, valid time, expiry or refresh policy;
  - C6 class, conflicts, sensitivity, authorized audience, schema revision;
  - source spans or citations readable by authorized roles.
- **T06.03 Centralize a context compiler.** Input: authenticated actor, the 05 `SemanticFrame`, allowed actions, time and tool budgets. It resolves identities, runs minimal reads in parallel, reconciles conflicts and returns a bounded, privacy-filtered bundle with explicit unknowns. No dumping of whole CRM histories into prompts.
- **T06.04 Adapt, don't duplicate.** Keep `belief_store.py` provenance, decay and dispute behaviour and the `intent_states.py` declared/observed split. Add adapters, not a second living graph.
- **T06.05 Search tiers.** Exact IDs and tenant-keyed blind indexes for protected data; full-text and structured queries where allowed; embeddings only for licensed, permitted data (none exist today); relationship traversal over indexed foreign keys. Apply permissions before returning, exporting or sending content to a third-party model.
- **T06.06 Named retrieval legs with C6 per-source status.** Legs: clients, property sources (see 11), deal timeline, calls, files, conversations. Each returns `checked`, `unavailable`, `timed_out` or `not_applicable`, with elapsed time, freshness and a short reason (for example "not configured" or "no entitlement"). A required leg that is not `checked` makes the bundle `degraded` ([C6](CONTRACT.md#c6-truth-vocabulary-use-these-words-exactly)). Replace the bare `degraded` list in `search_api.search`. The client-automation case is 11 T11.10; reuse this contract there.
- **T06.07 Memory tiers, each with retention and erasure.** Ephemeral working context; episodic events with receipts; source-backed semantic facts; authorized agent preferences and procedural settings; outcome memory without causal claims. Move `user_interactions` content off plaintext or justify it in the privacy map (A01, with 17).
- **T06.08 Source-version-aware caches.** Invalidate on record edit, assignment, share or revocation, provider sync, permission change, retention deletion and source retraction. Model summaries are derived artifacts, never authoritative. Every cache key includes tenant, user and a permission revision (define one; see §2).
- **T06.09 Provenance-aware answers.** Cite sources the customer may see. Label missing or contradictory data. Abstain rather than invent listing status, financial eligibility, deadlines or room scale. Derived summaries keep each source's licence restrictions.
- **T06.10 Instrument retrieval.** Latency, hit/miss, authorization denials, stale-fact rate, source conflicts, unsupported-claim rate, context tokens, summary drift, vendor cost and index amplification.
- **T06.11 Offline regression suite.** Pronouns; conflicting client notes; two properties on the same street; time-zone mismatches; a provider outage; RLS share and revoke; stale caches; unsupported owner data; an uploaded CSV that contains a prompt injection.
- **T06.12 Live-voice retrieval.** Pre-authorized context snapshots, bounded deadlines, freshness checks and an asynchronous read-only sidecar (with 09). No shared cache key without tenant, user and permission revision.
- **T06.13 Verify whether** `user_interactions` rows are reached by export, correction and erasure (`backend/privacy_data_map.py` classes them `AI_CHAT`), and record the gap with 17.

## 5. Failure and adversarial cases

- **F06.01 Failure read as zero.** A leg that raised or timed out is reported as "no results".
- **F06.02 Leak into model context.** A record the actor cannot read reaches a prompt, an embedding or a cached summary.
- **F06.03 Stale cache after revocation.** A share is revoked, but a cached bundle still serves the record.
- **F06.04 Contradiction hidden.** Two disagreeing sources are merged into one confident value.
- **F06.05 Summary treated as fact.** A model-written summary or a `model` belief is cited as a source.
- **F06.06 Wrong record.** Two properties on the same street or two people with one name are joined.
- **F06.07 Injected upload.** A CSV or note instructs the model and changes retrieval or actions.
- **F06.08 Erasure misses a copy.** Erased data survives in chat turns, beliefs, caches or summaries.

## 6. Acceptance criteria

- **AC06.01 Per-source status everywhere.** Search and the context compiler return C6 status per leg, and a timeout makes the result `degraded`. Evidence: tests for a verified zero, a timeout, a raised leg and `not_applicable`. Covers: T06.06, F06.01.
- **AC06.02 The evidence contract is typed.** `EvidenceBundle` has a schema version, and every fact has a source and a C6 class. Evidence: contract test (proposed `backend/tests/test_evidence_bundle.py`). Covers: T06.01, T06.02, T06.04, F06.05.
- **AC06.03 No inaccessible document reaches a model.** Zero forbidden records in prompts, embeddings or caches across two agents and two brokerages. Evidence: real-PostgreSQL RLS test under the application role, capturing compiled context. Covers: T06.03, T06.05, F06.02.
- **AC06.04 Revocation invalidates caches.** After share revoke or permission change, the next read misses the cache. Evidence: integration test per invalidation trigger. Covers: T06.08, T06.12, F06.03.
- **AC06.05 Conflicts and unknowns are visible.** Disputed facts appear as a pair with sources; unknowns are stated. Evidence: answer-composition tests on the T06.11 suite. Covers: T06.09, F06.04, F06.06.
- **AC06.06 Retrieval quality is measured.** Exact-record precision and recall, source faithfulness, correct "unknown" answers, p50/p95 bundle assembly and token cost per task family. Evidence: committed eval command and result with conditions. Covers: T06.10, T06.11, F06.07.
- **AC06.07 Memory lifecycle reaches every tier.** Export, correction and erasure reach chat turns, beliefs, caches and summaries. Evidence: erasure test listing each store, plus the privacy-map update. Covers: T06.07, T06.13, F06.08.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS is out of scope** (D-2026-10-12-01). The property leg uses property sources (see 11) only. Do not add a legacy MLS retrieval leg.
- **Shared contract with 05, 07 and 08.** Same IDs, the same execution-time consent and permission check ([C3.4](CONTRACT.md#c3-tenancy-roles-and-authorization)) and C6 words. 06 owns `EvidenceBundle`, per-source status and the permission revision used in cache keys.
- **Never duplicate an RLS predicate** to filter evidence ([C3.3](CONTRACT.md#c3-tenancy-roles-and-authorization)).
- **No vector store** until a benchmark shows a need and licences permit embedding the data ([C7.2](CONTRACT.md#c7-reuse-the-architecture), [C7.4](CONTRACT.md#c7-reuse-the-architecture)).
- **Owner question:** may customer communications be sent to a third-party model at all, and under which DPA (with 17)?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the source catalog (proposed `docs/evidence-source-catalog.md`);
  - the `EvidenceBundle` schema and per-source status contract;
  - the context compiler wired into `_generate`;
  - the memory lifecycle change and privacy-map update;
  - the permission-safe cache; the evaluation set; retrieval traces.
- **First slice:** T06.06 + AC06.01 (typed per-source status in `search_api.search`, shared with 05 T05.11 and 11 T11.10). Then T06.02 + AC06.02.
- **Ledger:** add L06-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
