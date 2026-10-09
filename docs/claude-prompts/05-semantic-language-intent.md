# Claude implementation prompt 05: Language understanding beyond keywords, goals and contextual references

> Working repository: `ydngemini/oracle-cinematic` (shipping FastAPI backend and React/Vite browser app).
> Scope: all-pages, fully functional Neoh for **one brokerage + 20 participating agents** (plus explicit owner/admin seats).
> This is an INSTRUCTION and acceptance specification, not a declaration of implemented features, existing provider permissions, tested production, or code already changed.

## Claude: your assignment

Act as principal engineer, security reviewer and implementation owner. Start by reading root `CLAUDE.md`, `docs/neoh-full-platform-source-audit-2026-10-08.md`, the [master prompt index](README.md), and relevant category specifications. Inspect current HEAD and reference actual source lines before assuming older audit findings still apply. Implement in bounded, reviewable slices. **Do not stop at outlining plans** when code/test work is possible. Preserve authentic, accessible product behavior, responsible budgets, explicit user consent, no unauthorized external side effects, reproducible tests and durable provider receipts.

**Initial source paths (verify current reachability):** backend/neoh_intents.py; backend/ai_chat_agent.py; backend/llm_gateway.py; backend/ai_chat_store.py; backend/intent_states.py; backend/search_api.py; oracle-app/src/neoh/NeohConversation.jsx

## Detailed engineering tasks

### Desired intelligence
Neoh should understand purpose, negation, references, corrections, scope and task constraints: “Find Sarah homes”, “What about the one yesterday?”, “Would she qualify?”, “Don’t message her yet”, “Actually the other Sarah”, “Show our team's stuck deals”, “Do it after the manager signs off.” Meaning is not a regex match or an invitation to invent identity.

### Architecture steps
1. Inspect neoh_intents.py regex fast paths and safe fallthrough. Preserve deterministic high-precision shortcuts for exact commands; add a typed semantic interpretation layer for paraphrases, temporal references and compound requests.
2. Propose versioned SemanticFrame: goal(s), intent label(s), subject/entity mentions, candidate canonical IDs, time interpretation/time zone, user instructions/negative constraints, scope (mine/team/shared), required data, freshness criteria, permitted effect classification, unfilled slots, ambiguities and supporting conversational evidence.
3. Use low-cost structured model extraction with Pydantic/schema validation. Validate against current brokerage membership and record permissions before treating any IDs as authorized. Never elevate “call her” or a retrieved note into permission to call an unverified number.
4. Maintain short-lived discourse context: pronoun “her”, deixis “that house”, previous search, corrections, interruptions, cancelled intents, ambiguity resolution and repair. User clarification modifies the plan, rather than stacking contradictory actions.
5. Adopt task-aware routing: deterministic first, semantic parser next, evidence retrieval then planner. Complex questions may require intent decomposition but simple queries must not pay for a large planning model or all tools.
6. Distinguish stated intent from observed and inferred motivation. Real estate sensitive contexts require a clear explanation of assumptions; abstain from protected-class profiling, housing steering or unsupported eligibility assertions.
7. Build a policy-bound action intent contract: no send/call, approval required, scope all assigned clients, draft-only, time-bound reminders; these remain machine-readable throughout queued missions, voice turns and UI confirmations.
8. Prevent prompt injection from transcripts, listings, web pages and CRM notes: these supply evidence only, not executable operator commands. Version and test trusted/untrusted boundaries.
9. Speech recognition adds partial/unstable phrases; only act on final confirmed turns for consequential actions. Recover from ASR homophones in names and addresses with grounded disambiguation.
10. Integrate context-aware clarifications into the UI and voice; ask one precise question with named choices if multiple plausible entities. Never fabricate a record just to avoid asking.
11. Evaluate task understanding separately from execution success and from factual grounding. Use real-estate paraphrase sets and adversarial utterances to find over-eager tool invocation.

### Evaluation suite
Pairs of same-intent/different-wording; same-wording/different-goals; ambiguous two Sarahs; “don't” constraints; time-zone ambiguity; changed/withdrawn instructions; unauthorized manager request; source text attempting instruction override; incomplete voice transcript; long-context conflict. Score intent/extraction/entity accuracy, constraint preservation, clarification rate, abstention and unauthorized action count.

### Done
A governed semantic frame supplies tool planning; faster deterministic shortcuts remain; every consequential action uses server authorization; all acceptance cases pass on holdout utterances; measured latency and cost fit chat/voice budgets.

## Required engineering workflow

1. Map concrete sources and call graph; document code paths, schema, external connections, permissions, failure states, test setup and observed baseline. Keep a table of **existing verified**, **proposed**, **implemented**, **tested** and **blocked**; never merge categories.
2. Implement one vertical slice including frontend, API, storage/RLS, provider/worker, customer-visible error/retry, tests and telemetry where appropriate. Use additive migrations and backwards-compatible API changes; preserve former route aliases and workflows.
3. Add unit and contract tests plus real PostgreSQL/RLS, React component, Playwright, concurrency, permission or live/sandbox provider testing as applicable. Do not contact real customers, send external communications, purchase services, deploy live or execute real financial transactions without explicit authorized approvals.
4. Run and report commands and exit codes, separate simulated and actual provider tests, measure p50/p95 where applicable, inspect logs for PII, and verify no regressed authorized page.
5. Commit focused code changes with changelog, migration notes, rollback/recovery instructions, exact acceptance evidence and remaining issues. Never use “done” or “A+” for untested paths; record external contractual, legal, licensing, operational or device blockers.

## Cross-cutting rules

- **All pages remain in launch scope**; honest setup-required gates are allowed when a customer must grant an external license/credential, but absent advertised functionality remains unfinished.
- **Hybrid brokerage access:** tenant boundary, agent assignments, explicit shares and team privileges must apply to DB, API, search, AI, jobs and browser caches.
- **Truthfulness:** missing facts, timed-out sources, non-verified 3D geometry and provisional model estimates are not verified outcomes.
- **Approval:** model suggestions are never authorization. Legal instruments, outreach, publishing, calendar writes, spend and high-impact changes require their established approvals.
- **Efficiency:** reuse FastAPI/Postgres/React/PlayCanvas and current job/tool architecture. Avoid duplicate frameworks or microservices unless benchmarks prove benefit.
- **Launch:** 20 agents concurrently plus owner/operator, actual iOS/Android browser tests, production-shaped deployment, cost and reliability measurement.

## Output format

Return a concise source-linked baseline, chosen vertical slice, completed files and migrations, tests run with results, measurable before/after where available, next dependent tasks, and blockers requiring human approvals. Update the master category matrix rather than duplicating contradictory backlogs.
