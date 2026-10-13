# 09 — Real-time voice, speech and response latency

> **Scope:** Make Neoh's phone voice natural, fast, truthful and compliant on every carrier path, with interruption control, consent-gated call notes and CRM-grounded read-only help, for 20 agents in one brokerage. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 06, 07 · **Unblocks:** 10, 20 · **Ledger:** [L09](LEDGER.md#l09) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Engines: `backend/elevenlabs_realtime.py`, `backend/qwen_omni_realtime.py`, `backend/qwen_voice_agent.py`, `backend/QWEN_OMNI_REALTIME.md`, `scripts/elevenlabs-agent.py`
- Carriers and webhooks: `backend/voice_provider.py`, `backend/plivo_call_handler.py`, `backend/twilio_call_handler.py`, `backend/acs_call_handler.py`, `backend/telephony_api.py`, `backend/commands_api.py` (`plivo_qwen_media`, `record_call_outcome`, `deliver_call_notes`)
- Inbound and rules: `backend/inbound_voice.py`, `backend/outreach_compliance.py`, `backend/voice_intel.py`, `backend/voice_tts.py`, `backend/ws_hub.py`
- Frontend: `oracle-app/src/neoh/useSpeechInput.js`, `oracle-app/src/neoh/callPresence.js`, `oracle-app/src/state/useOracleWebSocket.js`, `oracle-app/src/components/ClientNotes.jsx`
- Tests: `backend/tests/test_elevenlabs_realtime.py`, `backend/tests/test_call_notes_live.py`, `backend/tests/test_plivo_outbound_webhooks.py`, `backend/tests/test_voice_opt_out.py`
- Docs: `docs/capacity-plan.md`, `docs/performance-targets.md`, `docs/provider-failure-matrix.md`; audit finding [A01](../neoh-full-platform-source-audit-2026-10-08.md)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **ElevenLabs Agents carries the AI voice on staging Plivo calls (L09-01).** Commit `7662171` added `PlivoElevenLabsBridge` (`elevenlabs_realtime.py:165`). Real call #5 on 2026-10-07 held a 128 s conversation, and its outcome reached the status handler through Plivo's `hangup_url` (commit `4cef36d`; `voice_provider.py:574` in `PlivoVoiceProvider.place_call`).
- **Qwen is still the default engine.** `elevenlabs_realtime.py:87` `realtime_provider()` returns `qwen` unless `ORACLE_PLIVO_REALTIME_PROVIDER=elevenlabs`; `plivo_qwen_enabled` (`plivo_call_handler.py:42`) covers both engines.
- **The disclosure is spoken by the carrier before any AI audio.** `plivo_outbound_answer` (`telephony_api.py:1646`) returns `speak_and_stream_markup` with `AI_VOICE_DISCLOSURE` (`telephony_api.py:1717`), which puts `<Speak>` before `<Stream>` (`voice_provider.py:886`). The text says "automated AI assistant on a recorded line" (`outreach_compliance.py:105`).
- **The prompt forbids denying automation.** `_system_instructions` (`qwen_omni_realtime.py:185`) says "Never deny or obscure that you are automated" (`:189`); the ElevenLabs bridge reuses it for outbound calls in `_instructions` (`elevenlabs_realtime.py:213`).
- **A caller-turn limit applies to both engines.** `QWEN_REALTIME_MAX_TURNS` (default 20, capped at 80) sets `_max_turns` in `elevenlabs_realtime.py:204` and `qwen_omni_realtime.py:219`; `_on_transcript` ends the session at the limit (`elevenlabs_realtime.py:392`). The provisioned agent also caps a conversation at 300 s (`elevenlabs-agent.py:111`).
- **Outbound calls keep no transcript.** `_on_transcript` (`elevenlabs_realtime.py:382`) keeps lines only when the direction is inbound (`:385`). Inbound transcripts are encrypted with `encrypt_pii` in `finalize_inbound_voice_call` (`inbound_voice.py:698`, `:749`).
- **Call notes need the callee's spoken consent (L09-02).** Commits `aec14c4` and `9c3463a`. `_on_tool_call` (`elevenlabs_realtime.py:337`) records consent through `record_call_note_consent` (`commands_api.py:2270`), and `save_note` refuses until consent is true (`elevenlabs_realtime.py:360`).
- **Notes are stored as plaintext client notes (audit A01).** `deliver_call_notes` (`commands_api.py:2301`) inserts the body into `client_notes` (`:2335`) and a "Call notes from Neoh (n)" timeline entry (`:2343`).
- **The CALL_NOTES frame goes to the whole brokerage.** `deliver_call_notes` calls `ws_hub.broadcast` (`commands_api.py:2354`), which delivers to every socket of the tenant (`ws_hub.py:386`); the private `broadcast_user` exists at `ws_hub.py:393`. The frame carries the note text.
- **Notes on a lead-only call are dropped.** `deliver_call_notes` logs and returns at `commands_api.py:2326` when the call has no client.
- **The provisioned agent keeps no call audio.** `elevenlabs-agent.py:117` sets `record_voice: False` and `retention_days: 1`; `:114` enables signed-URL auth.
- **Barge-in clears queued carrier audio on ElevenLabs calls.** `_agent_to_plivo` sends Plivo `clearAudio` on an `interruption` event (`elevenlabs_realtime.py:301`).
- **The browser has speech input only, no AI audio.** `useSpeechInput.js:18` says there is no TTS or audio channel in the browser.
- **Every voice capacity number is from a mock.** `capacity-plan.md:62` reports `voice_sim.py` runs; `:131` says chat and voice numbers used the mock. The Neoh-share target is in `performance-targets.md:50`.

## 3. Goal

A caller always hears the disclosure first and is never told the AI is a person. The voice answers quickly, stops when interrupted, and says "I can't confirm that" rather than guessing. Notes exist only with consent, reach only the agents allowed to see that client, and are protected like other customer content. Every latency or capacity claim comes from a measured real or sandbox call.

## 4. Engineering tasks

- **T09.01 Map the real call path.** For each carrier (Plivo, Twilio, ACS) and direction, trace: signed webhook → media WebSocket → codec → endpointing → engine → outbound audio → provider acknowledgement → CRM receipts. Mark every path that is not connected. Browser speech input is STT only and is not evidence of synthesized playback.
- **T09.02 Measure every boundary.** On real test calls, record p50/p95 for carrier ingress, jitter buffer, resampling, speech end, model first token, first audio packet, playback acknowledgement, barge-in, CRM context fetch, hangup and transfer. Mock benchmarks are not carrier capacity proof.
- **T09.03 Build a low-latency voice lane.** Bounded buffering, adaptive endpointing, streaming output, accurate barge-in cancellation, never two overlapping assistant responses, playback state from real frames, and per-locale voices. No synchronous database or SDK work on the event loop.
- **T09.04 Add a read-only knowledge sidecar.** After caller identity is verified, prefetch permitted client facts, property status and route capabilities, and return typed, source-backed tool results within a fixed deadline. Never reveal private record content on caller ID alone.
- **T09.05 Persist operational call states.** Connecting, disclosure, caller speaking, paused or uncertain speech, preparing, speaking, tool retrieval, transfer requested, transfer in progress, provider dropped, goodbye. Store the current stage with timestamps and a user-cancelled flag.
- **T09.06 Evaluate endpointing.** Test noise, accents, fast and slow speech, overlapping speakers, long pauses and ambiguous recognition. Adaptive trailing silence of 300–700 ms is a target, not measured, and not a rule for every dialect or room.
- **T09.07 Handle tool-heavy questions honestly.** Acknowledge at once, fetch the source, answer only when verified or say it cannot be confirmed. Never fill silence with ungrounded answers; offer a callback when a source is unavailable.
- **T09.08 Harden transfer and escalation.** Warm summary to an authorized human only, timeouts, no answered-inbound loops, reconciliation of dropped calls, voicemail and unavailable-agent handling.
- **T09.09 Keep adapters provider-neutral.** Deterministic test audio, adapters that fit each provider's real endpoint, region, recording terms and cost, and no voice cloning or imitation.
- **T09.10 Bound spend and concurrency.** Active calls per agent and per brokerage, model tokens and audio seconds, queue and carrier rate, reconnection cap, circuit breaker, per-brokerage kill switch, and alerts for dropped or stalled media. Verify whether any concurrent-call cap exists today; none was found in the bridges or `plivo_qwen_media`.
- **T09.11 Keep the mascot off the audio path.** Speech-reactive visuals follow playback frames or verified events, never text generation time.
- **T09.12 Send call notes only to authorized agents.** Replace the tenant-wide CALL_NOTES broadcast with delivery to the agents who may see that client (C3), and keep the note text out of frames other agents receive.
- **T09.13 Protect call notes as customer content.** Store AI call notes through the encrypted path, matching inbound transcripts (A01, owned by 17), and classify them in the privacy data map.
- **T09.14 Put notes up for agent approval if the owner confirms it.** Shared notes (2026-10-11) ask that AI notes be proposed for approval, not saved directly. Build the proposal flow once the owner decides (§7); keep the current behaviour until then.
- **T09.15 Verify whether a spoken opt-out on an outbound call is recorded.** The disclosure promises "won't contact you again", but outbound bridges keep no transcript and contain no opt-out handling. Record the opt-out without storing the transcript.
- **T09.16 Verify whether "recorded line" is true for each engine.** The disclosure says the line is recorded while the ElevenLabs agent keeps no audio. Bring the wording to the owner and counsel; do not change it alone.
- **T09.17 Keep notes from lead-only calls.** Today they are logged and dropped. Attach them to the lead or tell the agent nothing was kept.
- **T09.18 Prove notes on one real call.** With owner approval in the session, place one consented call to the operator's own number and confirm the note reaches the right client, agent and timeline (L09-02 next task).
- **T09.19 Prove the voice rules on every engine.** Regression tests that each carrier path and engine plays the disclosure before AI audio, never denies automation, enforces the turn limit, and stores no outbound transcript without consent.

## 5. Failure and adversarial cases

- **F09.01 AI audio before the disclosure.** A fallback, retry or new carrier path opens the stream before the disclosure is spoken.
- **F09.02 The AI claims to be human,** or evades "am I talking to a robot?".
- **F09.03 Runaway call.** The turn limit, duration cap or spend cap is skipped and a call or reconnection loop keeps billing.
- **F09.04 Kept without consent.** An outbound transcript or note is stored without a yes, or the AI says it noted something that was not saved.
- **F09.05 Spoken opt-out lost.** "Stop", "remove me" or "don't send that" is heard but not recorded, and the person is contacted again.
- **F09.06 Provider failure misreported.** A 401/429/500, model failover or dropped media leaves dead air or records a false "completed" call.
- **F09.07 Overlapping or late speech.** Audio loss, jitter or event lag makes two responses overlap, or barge-in fails to stop queued audio; an interrupted command is still executed.
- **F09.08 Caller-ID trust.** A spoofed number, a cross-brokerage caller or an ambiguous buyer name unlocks another client's private facts.
- **F09.09 Bad transfer.** A transfer reaches the wrong or unavailable person, loops back to the AI, or leaves voicemail with private content.
- **F09.10 Notes leak inside the brokerage.** CALL_NOTES text reaches an agent with no right to the client, or permissions change mid-call and are not re-checked.
- **F09.11 Twenty agents at once.** Simultaneous calls from 20 agents, with mixed phone and desktop use, cause late frames or starve other work.

## 6. Acceptance criteria

- **AC09.01 Disclosure first and no denial on every path.** Every carrier path and engine plays the disclosure before AI audio, and the prompt forbids denying automation. Evidence: markup and bridge tests per path in `backend/tests/test_voice_rules.py` (proposed), plus `backend/tests/test_plivo_outbound_webhooks.py`. Covers: T09.19, T09.09, F09.01, F09.02.
- **AC09.02 Calls stay bounded.** Turn limit, duration cap, per-agent and per-brokerage concurrency caps and the kill switch end or refuse a call, with a recorded reason. Evidence: `backend/tests/test_elevenlabs_realtime.py` turn-limit test plus a concurrency-cap test (proposed). Covers: T09.10, F09.03.
- **AC09.03 Nothing is kept without consent, and kept notes are protected.** No outbound transcript is stored; notes exist only after a yes and a confirmed `save_note`; stored notes use the encrypted path; lead-only calls are handled. Evidence: unit tests plus a real-PostgreSQL test reading the stored column. Covers: T09.13, T09.17, F09.04.
- **AC09.04 Notes reach only authorized agents.** A second agent in the same brokerage without access to the client receives no CALL_NOTES text; approval mode follows the owner's decision. Evidence: WebSocket fan-out test with two agents plus the cross-brokerage test. Covers: T09.12, T09.14, F09.10.
- **AC09.05 Spoken opt-outs are honored.** An outbound or inbound "stop" creates an opt-out before the next contact, without storing an outbound transcript. Evidence: bridge tests built on `backend/tests/test_voice_opt_out.py` cases. Covers: T09.15, F09.05.
- **AC09.06 Latency is measured, not assumed.** Per-boundary p50/p95 is recorded with carrier, engine, region and call count. Targets, not measured: first audio p50 < 1 s and p95 < 2 s for routine dialogue, cached context p95 < 300 ms, barge-in stops queued speech promptly, endpointing 300–700 ms. Evidence: committed measurement protocol run on approved synthetic destinations. Covers: T09.01, T09.02, T09.03, T09.06, F09.07.
- **AC09.07 Failures end honestly.** Provider errors, failover and dropped media produce a true outcome (never a false "completed"), an inbound hand-off, or a callback task. Evidence: failure-injection tests for 401/429/500, media loss, and transfer to an unavailable agent. Covers: T09.05, T09.08, F09.06, F09.09.
- **AC09.08 Private facts need verified identity.** The sidecar returns nothing private on caller ID alone, and an ambiguous name or cross-brokerage caller gets no record. Evidence: sidecar contract tests with spoofed and cross-brokerage callers. Covers: T09.04, T09.07, F09.08.
- **AC09.09 Twenty simultaneous agents are measured.** 20 concurrent calls on staging record late frames, CPU and cost with conditions. Evidence: committed load protocol and result (staging, not production). Covers: F09.11.
- **AC09.10 The mascot follows playback, not text.** Evidence: component test driving visual state from playback events only. Covers: T09.11.
- **AC09.11 One real consented call proves notes, and the disclosure wording is settled.** Evidence: provider call ID, the client note and timeline row, and the owner or counsel decision on "recorded line" recorded in L09. Covers: T09.18, T09.16.

## 7. Out of scope, owner decisions, category-specific rules

- **Voice rules (not in CONTRACT):**
  - the disclosure always plays before any AI audio;
  - the AI never denies or hides that it is automated;
  - a turn limit applies to every realtime engine;
  - outbound calls store no transcript unless the callee consents;
  - no binding offers or terms on a call, and no requests for full SSNs, card numbers or passwords;
  - no voice cloning or imitation of a real person.
- **Owner questions:** should AI call notes be proposed for agent approval instead of saved directly (L09-02)? Is "recorded line" the right wording when no audio is kept (T09.16)? Jurisdiction-specific disclosure needs counsel review.
- **Real calls** go only to the operator's own allow-listed number, with owner approval in that session (C4.3).
- **Out of scope here:** SMS, email and calendars (10); mascot animation (15); encryption design for A01 (17).

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the call path map (`docs/voice-call-path.md`, proposed);
  - the voice-rule regression suite;
  - the CALL_NOTES fan-out fix and encrypted note storage;
  - the latency measurement protocol and its results.
- **First slice:** T09.12 + T09.19 with AC09.04 and AC09.01, which close an in-brokerage leak and lock the rules. Then the owner decision, T09.14, and the real call in T09.18.
- **Ledger:** update L09-02, then add L09-03 onward per slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
