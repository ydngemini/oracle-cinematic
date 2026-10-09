# Claude implementation prompt 09: Speech, realtime voice AI, VAD, streaming, interruptions and response latency

> Repository: `ydngemini/oracle-cinematic` shipping FastAPI + React/Vite. Goal: full-feature browser SaaS, one brokerage and **20 agents**, with a separately accounted owner, not a cut-down MVP.
> This is a specification and task prompt, NOT evidence of completed code, production deployment, provider licensing or tested results.

## Claude: implementation assignment

Act as principal architect and hands-on engineering owner. Read `CLAUDE.md`, `README.md` in this prompt folder, the existing platform audit, and the actual current source. Pin main SHA and inspect path:line, tests, migrations, configurations and provider state before coding. Prioritize verifiable full vertical slices rather than replacing entire systems with unproven frameworks.

**Inspect first:** backend/qwen_omni_realtime.py; backend/qwen_voice_agent.py; backend/inbound_voice.py; backend/voice_intel.py; backend/voice_provider.py; backend/telephony_api.py; oracle-app/src/neoh/useSpeechInput.js; oracle-app/src/neoh/callPresence.js; docs/capacity-plan.md

## Work program

### Mission
A natural, low-latency, truthful, compliant Neoh phone/voice experience with interruption control and CRM-grounded read-only assistance, supporting 20 agents in the first brokerage.

### Analyze first
1. Map actual carrier path inbound/outbound (Twilio/Plivo/Telnyx if enabled) -> signed webhook -> media WebSocket -> codec conversion -> VAD/endpointing -> audio model or STT/LLM/TTS -> outbound audio -> acknowledgement/CRM receipts. Mark paths that are not connected; browser useSpeechInput.js does STT and is not evidence of synthesized audio playback.
2. Measure p50/p95 at every boundary on real test calls: carrier ingress, jitter/buffer, resampling, speech-end, model first token, first audio packet, provider playback acknowledgement, barge-in interruption, CRM context retrieval, hangup and transfer. Local voice mock benchmarks are not real-carrier capacity proof.
3. Create a real-time voice lane optimized for minimal turn response: bounded buffering, adaptive VAD, streaming output, accurate barge-in cancellation, no two overlapping assistant responses, audio playback actual-state tracking and configurable language/locale voices. Avoid event-loop blocking synchronous database and SDK work.
4. Introduce asynchronous knowledge sidecar: after verified caller identity/context, prefetch permitted common client facts, property status and route capabilities; deliver typed, source-backed read-only tool results within a tight deadline. Don't leak private record content based only on caller ID.
5. Split operational voice states: call connecting, disclosure, caller speaking, paused/uncertain speech, Neoh preparing, Neoh speaking, tool retrieval, transfer request, transfer in progress, dropped provider, goodbye. Persist current stage with timestamps and user-cancelled status.
6. Evaluate speech-end settings under noise, accents, fast and slow speech, overlapping speakers, long pauses and speech recognition ambiguity. Present adaptive VAD proposed 300–700 ms trailing silence as a target, not a requirement for every dialect or environment.
7. For tool-heavy queries, acknowledge immediately, gather source, respond only when verified or state cannot confirm. Do not pad silence with ungrounded answers; allow callback/review if source unavailable.
8. Keep transfer/escalation robust: warm summaries to an authorized human, no disclosure to wrong recipient, timeouts, no answered-inbound loops, disconnected calls reconciled, voicemail and unavailable agents handled.
9. Define provider-neutral adapters and deterministic test audio inputs; adapt architecture to actual Qwen endpoint/region/recording agreements and cost. Disclose AI and recording appropriately based on jurisdiction and counsel review. Avoid speech imitation/voice-clone policy violations.
10. Add spend/concurrency limits: active calls/agent, model tokens/audio seconds, queue and carrier rate, reconnection cap, circuit breaker, kill switch per brokerage, monitoring for dropped/stalled media.
11. Keep audio path independent of animated mascot; frontend speech-reactive visual state must be based on actual playback frames or verified events, not text generation time.

### Targets and proof
Example engineering objectives (not established benchmarks): response first audio p50 <1s, p95 <2s for routine dialogue where compatible carriers/providers permit; common cached context retrieval p95 <300ms; barge-in promptly cancels queued speech; no false completed call. Tune only against measured live/sandbox results.
### Test matrix
Inbound/outbound, 20 agents active with simultaneous calls, provider 401/429/500, audio loss/jitter, call transfers, opt-out, ASR “don't send”, ambiguous buyer names, interrupted commands, changed permissions, cross-brokerage caller injection, identity verification, mixed phone/desktop use, event lag, model failover and cost caps.

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
