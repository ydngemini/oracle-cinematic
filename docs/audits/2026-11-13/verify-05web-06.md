# Re-verification: CHUNK-05 (WEB-001..003, UX-001) and CHUNK-06 (RUN-01..06)

- Checkout: `/media/ydn/SYPHER_CORE2/Oracle`, branch `main`, `git rev-parse --short HEAD` = **54740ef**, which matches the audit snapshot. Working tree clean.
- Method: read-only. Grep, then line-range reads. No tests run, no external calls. Both audit files were treated as claims to check.
- Tracking sources checked: `docs/claude-prompts/LEDGER.md` (3534 lines, including the § Audit cross-reference at :3519) and `docs/neoh-full-platform-source-audit-2026-10-08.md` (A01–A10).
- **Result: none of the 10 items has a ledger row or an A-finding that covers it.** Adjacent rows are named per item.

---

## Summary table

| id | verdict | key file:line | category | already tracked? | P | one-line fix |
|---|---|---|---|---|---|---|
| WEB-001 | PARTIAL | `oracle-app/src/lib/clearPrivateCaches.js:31`, `src/sw-oracle.js:34-80,178`, `lib/capture/captureStore.js:41-48` | 17 (16 co-owner) | No. Adjacent: L12-03 (LEDGER:2036), L16-05 (LEDGER:2438) | P2 | Add `db.onversionchange = () => db.close()` and close per-op handles in the SW and captureStore; make `clearPrivateCaches` return `{blocked:[…]}` instead of treating `onblocked` as done |
| WEB-002 | CONFIRMED (narrow environment) | `oracle-app/src/App.jsx:51,72,82`; `components/LoginVault.jsx:51-53` | 02 (17 for the sign-out cleanup) | No | P2 | Add a guarded storage helper; wrap sign-out and expiry in try/finally so `clearPrivateCaches()` and `setAuthed(false)` always run |
| WEB-003 | DESIGN-GAP (verification item, not a defect) | `observability-platform/src/hooks/useAwsWebSocket.jsx:40`; `backend/aws_observability.py:1216-1233`; `backend/server.py:519-521` | 19 | No (LEDGER:3092 notes `/api/aws` is unmounted unless enabled) | P2 | Keep the route unmounted on DO. Before enabling it: a short-lived WS ticket, plus `verify_session_current` before `accept()` |
| UX-001 | DESIGN-GAP (deliberate, pinned by a test) | `oracle-app/src/components/ServiceStatusBanner.jsx:15-23`; test `ServiceStatusBanner.test.jsx:32-37` | 02 | No | P2 | After N consecutive `/api/status` failures, show "Service status unavailable" |
| RUN-01 | CONFIRMED | `backend/ai_chat_api.py:330-332,334-360`; `ai_chat_store.py:154-328`; `reconciliation.py:78-84` | 07 (08 for the outbox pattern) | No | P1 | On a duplicate with `status='pending'`, re-call the idempotent `enqueue_job`; sweep re-enqueues pending turns that have no job |
| RUN-02 | CONFIRMED | `backend/inbound_voice.py:754,909-915,716-718`; `elevenlabs_realtime.py:267-273`; `qwen_omni_realtime.py:545-557,787-799` | 09 (17 for consent) | No (L09-05 at LEDGER:1308 is outbound only) | **P0** (opt-out) | Write the inbound suppression per utterance, the way outbound already does; sweep and alert on rows stuck at `transcript_status='active'` |
| RUN-03 | CONFIRMED | `backend/elevenlabs_realtime.py:388-392,265-266,395-403`; `commands_api.py:2712-2776` | 09 | No (L09-02 at LEDGER:1251 built the feature) | P1 | `save_note` persists before returning "Saved"; on failure it says the note was not saved |
| RUN-04 | CONFIRMED (more paths than the audit names) | `backend/email_outbox.py:71-77,110,155-158`; `reconciliation.py:109-112`; `component_health.py:156-159,203-205` | 10 (19 for health) | No | P1 | Sweep marks stale unmarked `sending` rows as `delivery_unknown` and never resends; health then counts them |
| RUN-05 | CONFIRMED | `backend/telephony_api.py:1770-1788,1755-1756` | 10 (09 co-owner; touches A05) | No (A05 is about over-counting at staging; this is under-recording) | P1 | If `record_call_outcome` raises, return 503 and skip cleanup so a retry can still resolve |
| RUN-06 | PARTIAL (the missing await is real; the bigger consequence is different and affects both bridges) | `backend/elevenlabs_realtime.py:249-251,285-286,418`; `qwen_omni_realtime.py:316-321` | 09 | No | P1 if tied to opt-out, otherwise P2 | Bounded drain of the agent leg after the carrier hangs up, then `gather(*pending)`; wrap the suppression write in `asyncio.shield` |

---

## CHUNK-05: frontend

### WEB-001: IndexedDB `onblocked` treated as successful deletion

**Verdict: PARTIAL.** The control-flow claim holds. The stated consequence, that a new user can read the prior user's cache, does not hold as written. The real cause is a different and narrower bug.

**Confirmed:**
- `oracle-app/src/lib/clearPrivateCaches.js:29-32` calls `req.onsuccess = req.onerror = req.onblocked = () => resolve();`. Nothing is reported back, and the function returns `undefined`.
- The service worker's own clear handler does the same thing (`src/sw-oracle.js:177-178`).
- `App.jsx:73` (expiry) calls `clearPrivateCaches()` without awaiting it. `App.jsx:83` (sign-out) does await it.
- The test `clearPrivateCaches.test.js:20-32` only fires `onsuccess`.

**Not supported:**
- **A blocked delete is not cancelled.** Under the IndexedDB spec, a `deleteDatabase` request that fires `blocked` stays queued and runs once the blocking connections close. Later `open()` calls for the same name queue behind it.
- So a new user's fresh connection cannot open the old database. Old bytes stay reachable only through a connection that was already open. Neither the SW nor CaptureStudio keeps a reference that would serve them.
- Online splat reads are also checked by the server first. `lib/splatCacheGate.js:21-35` sends a conditional GET; 401/403/404/410 purge the copy, and the bytes are served only on a 304.
- The comment at `clearPrivateCaches.js:3-5` still says the SW serves `/api/media/{id}` "with no auth check". That has been stale since L16-05 / T16.14.

**What is actually wrong:**
- **The SW leaks IndexedDB connections.** `openDB()` (`sw-oracle.js:34-52`) opens a new connection for every `idbPut/idbGet/idbDelete` call (`:54-80`). None of them is ever closed, and none has an `onversionchange` handler. As a result, "blocked" is the *normal* outcome while the SW is alive.
- The CLEAR handler's `openDB(); db.close()` (`:172-175`) closes only the connection it just opened, not the leaked ones.
- `captureStore.indexedDbAdapter` (`lib/capture/captureStore.js:41-48`) caches one connection (`opened`) with no `versionchange` handling.
- `signOut` awaits the cache clear *before* `setAuthed(false)` (`App.jsx:83-84`). CaptureStudio is therefore still mounted and holds the `neoh-capture` connection, so that delete is also blocked.
- **Consequences:**
  - (a) Prior-user capture originals and splats stay on disk after sign-out until GC or SW termination. CaptureStudio tells the user "Signing out discards anything not uploaded" (`CaptureStudio.jsx:259`), which is not yet true at that moment.
  - (b) *Inferred, not reproduced:* the next session's IndexedDB opens queue behind the pending delete. Every `/api/media/*` request goes through `serveSplat` → `idbGet` (`sw-oracle.js:249-262`), so media fetches could stall until the leaked connections close.
- **Residual exposure:** the offline branch serves the cached copy with no check (`splatCacheGate.js:28-30,36-43`). That only matters if the delete failed outright, through `onerror`, which is also swallowed.

**Tracking:** no row. L12-03 (LEDGER:2036, "Sign-out now deletes the staged-capture database", `:2055`) and L16-05 (LEDGER:2438) added the code but did not cover the blocked case. Not covered by any A-finding.

**Category:** 17 (privacy and data lifecycle). 16 co-owns the SW cache.

**Smallest fix:**
1. In `sw-oracle.js` `openDB()` and in `captureStore` `db()`, set `db.onversionchange = () => db.close()` (and reset `opened = null` in captureStore). In the SW, also close the connection after each transaction completes.
2. In `clearPrivateCaches`, resolve `onsuccess`/`onerror` as `done`. On `onblocked`, record the name and keep waiting for `onsuccess` up to a bounded timeout (for example 3 s). Return `{blocked: [...]}` so `signOut` can log it or tell the user.

**Failing-first tests:**
- `clearPrivateCaches.test.js`: stub `deleteDatabase` to fire `onblocked` and never `onsuccess`. Expect the result to equal `{blocked: ['oracle-predictive-cache','neoh-capture']}`. This fails today because the function returns `undefined`.
- `capture.test.js`: give the adapter a fake IDB and fire `versionchange` on the opened db. Expect `close()` to be called. This fails today.

**Severity:** P2. Prior-user captures persist on a shared device past sign-out, but are not readable by the next user under spec semantics. Raise to P1 if a browser repro shows media stalls after re-login.

### WEB-002: storage exceptions break login, restore and sign-out

**Verdict: CONFIRMED in code.** The environment that triggers it is narrow.

**Unguarded storage calls:**
- `App.jsx:72` (`expireSession`) and `App.jsx:82` (`signOut`) call `sessionStorage.removeItem` before `clearPrivateCaches()` / `setAuthed(false)`. If it throws, cleanup and the UI transition are both skipped, even though the server session was already revoked at `:81`.
- `App.jsx:51` (`rememberIdentity`): `sessionStorage.setItem` is unguarded, while the `localStorage` writes at `:56-60` are guarded.
- `LoginVault.jsx:51-53`: all three writes are unguarded.

**Two consequences the audit missed:**
- **Restore path:** a throw inside `onIdentity` (`useSessionRestore.js:37`) lands in `.catch` (`:40-47`). `isSignedOutAnswer` is false, so the app retries and finally shows "Neoh can't be reached right now. You are still signed in." (`App.jsx:93-94`). That message is wrong.
- **Login path:** a throw in `finishAuth` from `:120` is caught at `:121` and shown as a login failure, even though the server has already set the session cookie.

**Environment caveat:** reading `window.sessionStorage` throws `SecurityError` mainly when site data is blocked. That setting usually also blocks the session cookie, which breaks the app anyway. Realistic triggers are partial policies: sandboxed embeds, enterprise policy, older Safari private mode quota errors.

**Tracking:** none. **Category:** 02, with 17 owning the sign-out cleanup part.

**Smallest fix:**
1. Add `lib/safeStorage.js` with `safeSet/safeRemove`, each wrapped in try/catch.
2. In `expireSession` and `signOut`, use `try { safeRemove(...) } finally { await clearPrivateCaches(); setAuthed(false); }`.
3. Use the helper in `rememberIdentity` and `finishAuth`.

**Failing-first test:** new `src/App.session.test.jsx`.
- Render `NeohApp` with `/auth/session` mocked as authenticated.
- `vi.spyOn(Storage.prototype,'removeItem').mockImplementation(() => { throw new DOMException('x','SecurityError') })`.
- Dispatch `auth:expired`.
- Expect the LoginVault to render and `clearPrivateCaches` to have been called. This fails today: the handler throws at `:72`.

**Severity:** P2. Rare browsers can keep showing CRM data after a revoked session or expiry.

### WEB-003: observability JWT in `Sec-WebSocket-Protocol`

**Verdict: DESIGN-GAP / verification item.** The audit itself does not claim a leak, and none was found.

**What the code does:**
- `useAwsWebSocket.jsx:40` opens `new WebSocket(..., ['oracle.jwt', tok])`.
- The server reads the header at `aws_observability.py:1216-1222` and echoes only `oracle.jwt` at `:1233`.
- The token also lives in `sessionStorage` (`observability-platform/src/lib/auth.js:22,48`), so XSS exposure is the larger token risk.

**Exposure today:**
- The router is mounted only when `AWS_OBSERVABILITY_ENABLED` is set (`server.py:519-521`).
- That variable does not appear in `infra/digitalocean/app.yaml` (grep is empty).
- The dashboard image is deployed only by the retired AWS Terraform (`infra/terraform/observability.tf`).
- Its nginx is static-only (`observability-platform/nginx.conf:1-3`). Default nginx and uvicorn access logs do not record request headers.

**Adjacent finding, outside this chunk:** the three platform-admin sockets never call `verify_session_current` (`tenancy.py:483-504`) before `accept()`. They are `aws_observability.py:1223-1233`, `admin_c2.py:344-352` and `audit_ledger.py:989-999`.
- `require_context` (`tenancy.py:471-480`) only decodes the token.
- `record_platform_websocket` (`audit_middleware.py:224-240`) is synchronous and opens no transaction.
- So an unexpired token belonging to a revoked or demoted admin can open these streams. This matches the "Chunk 1 / PR #45" item the audit refers to.

**Tracking:** none for the header. LEDGER:3092 notes "/api/aws is not mounted unless AWS observability is enabled". **Category:** 19 (17 for the session check).

**Smallest fix:** add `await verify_session_current(ctx)` before each `accept()`. Before enabling the dashboard anywhere, swap the JWT for a single-use, 60-second WS ticket.

**Failing-first test:** in `test_platform_admin_read_audit.py` or a new WS test, monkeypatch `verify_session_current` to raise a 401 and connect with a valid admin JWT. Expect close code 4401. This fails today: the socket is accepted.

**Severity:** P2. Not deployed on DO, so no customer impact today.

### UX-001: status banner hides a failed `/api/status`

**Verdict: DESIGN-GAP, deliberate.**
- `ServiceStatusBanner.jsx:17` swallows errors with the comment "the status endpoint failing is itself shown by request errors".
- This is pinned by `ServiceStatusBanner.test.jsx:32-37`, "stays out of the way if the status call itself fails".
- Nuance the audit missed: after a degraded answer, later failures do *not* clear `messages`, so a stale degraded message stays on screen.
- Mitigations: offline detection in `context/NetworkContext.jsx:9-20`, the composer connection notice in `neoh/NeohComposer.jsx:139-151`, and session-unreachable handling in `App.jsx:92-96`.

**Tracking:** none. **Category:** 02.

**Fix:** count consecutive failures. At 3 or more (about 3 minutes at the 60 s poll), show "Service status is unavailable", and mark any existing messages as possibly out of date.

**Failing-first test:** in `ServiceStatusBanner.test.jsx`, use fake timers with `crmGet` rejected three times. Expect the text `/status is unavailable/`. This fails today. The existing `:32-37` test will need its intent updated.

**Severity:** P2. During an outage the agent may see no banner until individual requests fail.

---

## CHUNK-06: runtime, AI, voice

### RUN-01: accepted AI chat with no durable job

**Verdict: CONFIRMED.**

**The accept-then-enqueue gap:**
- `create_chat_request` commits the user row and the `pending` assistant row in one `tenant_tx` (`ai_chat_store.py:154`, inserts at `:306` and `:314`).
- `ai_chat_api.py:330` then broadcasts `AI_CHAT_ACCEPTED`, and only afterwards enqueues (`:334-351`).
- A caught exception is handled at `:352-360`. A process death between `:313` and `:334` is not.

**The duplicate path:**
- A duplicate request returns at `:331-332` without enqueueing.
- `enqueue_job` is idempotent by key: "Insert one job or return the existing idempotent job" (`automation_jobs.py:152`), with key `ai-chat:{agent}:{request_id}` (`ai_chat_api.py:345`).
- So re-enqueueing on a duplicate would be safe. It is simply not done.

**Recovery:**
- The sweep fails turns that are `pending`/`streaming` for 15 or more minutes (`reconciliation.py:78-84`, `CHAT_STUCK_MINUTES=15` at `:42`). It runs every 900 s (`periodic.py:1092-1096` → `run_sweep` at `:770-774`). The worst case is about 30 minutes.
- The sweep broadcasts nothing (no `ws_hub` in `reconciliation.py`), so the open UI stays "pending" until it reloads.
- The concurrency slot also leaks until a 1-hour TTL (`rate_limiter.py:145-147`, `max_active=2` at `ai_chat_store.py:200-201`). Two crashes lock the agent out of chat for up to an hour.

**Replaying a `pending` turn is safe:** the worker sets `streaming` only at `ai_chat_agent.py:1756`, and refuses to re-run a `streaming` turn (`:1735-1754`). A `pending` turn has run no tools.

**Tracking:** none. Not covered by A02 (property lookup) or A09. **Category:** 07 (chat turn). 08 if a general transactional outbox is adopted.

**Smallest fix:**
1. In `ai_chat_api.py:331`, change to `if created["duplicate"] and created.get("status") != "pending": return`, so a duplicate `pending` turn falls through to the idempotent `enqueue_job`.
2. Add a sweep step: for assistant rows that are `pending` for more than 2 minutes with no `automation_jobs` row for the key, enqueue (safe), or fail with an `AI_CHAT_ERROR` broadcast.
3. Optionally move `AI_CHAT_ACCEPTED` after the enqueue.

**Failing-first test:** in `tests/test_ai_chat.py`, make the first frame's `enqueue_job` raise `asyncio.CancelledError` to simulate dying after commit. Resend the same `request_id` and assert `enqueue_job` is called with `idempotency_key == f"ai-chat:{agent}:{rid}"`. This fails today because the duplicate path returns. No test today covers `AI_QUEUE_UNAVAILABLE` or the duplicate path (grep is empty).

**Severity:** P1. The user sees "accepted", then a spinner that never answers for up to about 30 minutes, and can be blocked from chatting for up to an hour.

### RUN-02: inbound call finalization has no retry, including opt-outs

**Verdict: CONFIRMED.**

**Detection and swallowed failures:**
- An inbound opt-out is detected only at finalization: `opted_out = _requested_opt_out(cleaned)` (`inbound_voice.py:754`).
- It is written inside the same transaction (`:909-915`).
- All three bridges log and swallow finalization exceptions: `elevenlabs_realtime.py:267-273`, `qwen_omni_realtime.py:545-557` and `:787-799`.
- The docstring of `_record_voice_opt_out` says an unrecorded opt-out "must fail the finalize **(and its retry)**" (`inbound_voice.py:716-718`). **No retry exists.**

**No sweeper:**
- Rows stay at `transcript_status='active'` (set at `:704`) forever.
- Grep across `reconciliation.py`, `component_health.py` and `operator_api.py` finds no reader of stuck inbound rows. `operator_api.py:792-797` counts only `provider_status`.
- SIGKILL or a redeploy skips the `finally` blocks entirely.

**Inbound vs outbound:** outbound bridges already persist the opt-out *per utterance* (`elevenlabs_realtime.py:415-419`, `qwen_omni_realtime.py:360-363`, via `voice_opt_out.honour`). Inbound has no equivalent.

**What does work:** the status callback (`telephony_api.py:1321-1347`) records only the carrier status. The `_ai_unavailable` paths finalize with an empty transcript (`:1184`, `:1463`).

**Tracking:** none. L09-05 (LEDGER:1308) covers outbound only. **Category:** 09, with 17 for consent and TCPA.

**Smallest fix:**
1. In both bridges' transcript handlers, when `direction=='inbound'` and `voice_opt_out.spoken_opt_out(text)` is true, call a new `inbound_voice.record_inbound_opt_out(call_sid, state)`. It reuses `_record_voice_opt_out` inside its own `tenant_tx`; the ON CONFLICT DO NOTHING (`:727-729`) makes it idempotent. Do not end the call.
2. On a finalize exception, best-effort set `transcript_status='failed'`.
3. Add a sweep step plus a `component_health` count for rows with `transcript_status='active' AND started_at < now()-30 min`, raising DEGRADED and opening a compliance task.

**Failing-first test:** in `tests/test_elevenlabs_realtime.py`, build an inbound bridge with fake sockets. Feed the caller transcript "Please don't call me again." and monkeypatch `finalize_inbound_voice_call` to raise. Assert the suppression recorder was called. This fails today: the only write lives inside the failing finalize. Also extend `test_inbound_voice.py:333` with a stuck-row sweep assertion.

**Severity:** **P0 for the opt-out sub-path.** A spoken do-not-contact request can be lost on a database blip or pod kill at hangup, after which the outreach gate (which reads only `outreach_suppression`) allows later AI calls. That is TCPA exposure per call. Losing the transcript and callback task alone would be P1.

### RUN-03: ElevenLabs says "Saved" before saving

**Verdict: CONFIRMED.**
- `save_note` appends to `self.notes` (`elevenlabs_realtime.py:388`) and returns "Saved for their agent." (`:392`).
- Persistence happens only in `finally` at the end of the call (`:265-266`) through `_deliver_notes` (`:395-403`), which catches the exception and logs "Call notes NOT delivered".
- Consent is durable (`record_call_note_consent` writes to the database). The notes are not.
- `deliver_call_notes` is idempotent per call by *skipping* when a note already exists (`commands_api.py:2712-2776`). Per-note incremental delivery therefore needs an update path, not a second insert.
- If the handler is cancelled at shutdown, the `finally` await can be cancelled too.

**Tracking:** none. L09-02 (LEDGER:1251) shipped the feature; L09-06 (`:1332`) covers lead-only calls. **Category:** 09.

**Smallest fix:**
1. In the `TOOL_SAVE_NOTE` branch, `await` a durable write before returning success. Either call `deliver_call_notes(session_ref, self.notes + [note], conversation_id)` with its existing-note branch changed to re-seal the body with the full list, or add a `stage_call_note` that appends to the session's sealed note.
2. On an exception, return `("The note could not be saved; suggest they tell their agent directly.", True)` and do not append.
3. Keep the end-of-call delivery as an idempotent finaliser.

**Failing-first test:** in `tests/test_elevenlabs_realtime.py`, with an outbound bridge and `notes_consent=True`, monkeypatch `commands_api.deliver_call_notes` (or the new stager) to raise. Call `await bridge._on_tool_call(TOOL_SAVE_NOTE, {"note": "wants a 3-bed"})` and assert `is_error is True` and `"Saved" not in result`. This fails today because it returns `("Saved for their agent.", False)`.

**Severity:** P1. A consenting callee is told something was saved that a pod restart or database blip at hangup silently loses. That is a false product claim (CONTRACT truth vocabulary).

### RUN-04: email stranded in `sending`

**Verdict: CONFIRMED, with more paths than the audit names.**

**How a row gets stranded:**
- The claim sets `status='sending'` (`email_outbox.py:71-77`).
- Process death after the claim leaves an unmarked `sending` row.
- **Two in-process paths the audit missed:**
  - (a) If the pre-send check fails and `_finish(..., "queued")` itself fails (`:110`, database down), the row stays `sending` although nothing was sent.
  - (b) The provider accepts but the `sent` UPDATE (`:155-158`) fails, so the row stays unmarked `sending`. The job retry then re-claims only `queued` rows (`:73`) and returns `not_queued`.

**Why nobody notices:**
- The sweep re-enqueues only `queued` rows (`reconciliation.py:109-112`).
- Health counts `sending` only when `error LIKE 'delivery_unknown%'`, and counts `stuck` only for `queued` (`component_health.py:156-159`).
- With no other signal, health reports "email outbox draining" (`:203-205`).

**What works:** the policy of never blindly resending is correct and tested (`test_email_outbox.py:97`).

**Tracking:** none. L04-02 (LEDGER:~620-637) notes that SMTP has no receipt but not this stranding. **Category:** 10, with 19 for health and alerting.

**Smallest fix:** add one sweep step in `run_sweep`:

```sql
UPDATE email_outbox
   SET error = 'delivery_unknown: worker stopped after claim', updated_at = now()
 WHERE status = 'sending'
   AND (error IS NULL OR error NOT LIKE 'delivery_unknown%')
   AND updated_at < now() - interval '15 minutes'
```

It never resends. The existing health query then reports the row as `unknown` / DEGRADED, and an operator decides.

**Failing-first test:** in `tests/test_reconciliation_live.py`, seed a row with `status='sending', error=NULL, updated_at=now()-'1h'`, run `run_sweep(only_tenant=…)`, and assert `error LIKE 'delivery_unknown%'` with status still `sending` and no job enqueued. This fails today. Add a unit test in `test_component_health.py` that such a row yields DEGRADED.

**Severity:** P1. An email can be neither sent nor flagged, and the agent's UI shows "sending" forever.

### RUN-05: Plivo outbound callback acknowledged despite failure to store the outcome

**Verdict: CONFIRMED.**
- `record_call_outcome` exceptions are swallowed (`telephony_api.py:1770-1778`).
- Terminal cleanup runs regardless (`:1779-1786`), and the handler returns 204 (`:1788`).
- Because cleanup removes the call state, even a provider retry would hit `state is None` and get a 204 with no action (`:1755-1757`).
- Tests cover only the success path (`test_plivo_outbound_webhooks.py:331`). Grep finds no failure test.
- **What gets lost:**
  - the `live_call_sessions` outcome, and the client timeline entry;
  - `_record_call_answered` (`commands_api.py:2613-2620`), the *only* point a speed-to-lead call meters `lead_engaged`. So this under-records a delivered or billable event, which is the A05 lifecycle.
- Plivo's retry policy on non-2xx was not verified (no external access).

**Tracking:** none. A05 covers over-counting at the staged step, not a lost delivery event. A07 is unrelated (route initialisation race). **Category:** 10, with 09 co-owning.

**Smallest fix:** on an exception from `record_call_outcome`, log and `return Response(status_code=503)` *before* cleanup, so state survives for a retry; the state TTL still bounds it. Confirm the timeline insert is idempotent per (call, status) before relying on retries. `_record_call_answered` already is (`:2617-2619`).

**Failing-first test:** in `test_plivo_outbound_webhooks.py`, monkeypatch `commands_api.record_call_outcome` to raise and spy on `cleanup_plivo_call`. Post a correctly signed `CallStatus=completed`. Assert `status_code >= 500` and that cleanup was not called. This fails today: 204 and cleanup called.

**Severity:** P1. A database blip leaves a placed call stuck at "Call placed", with no answered or not-answered result and no speed-to-lead engagement record.

### RUN-06: ElevenLabs cancels the losing task without awaiting

**Verdict: PARTIAL.** The missing await is real, but its direct effect is minor. The more material consequence, which the audit did not state, applies to **both** bridges.

**Confirmed:**
- `elevenlabs_realtime.py:249-251` cancels without `gather`. Qwen awaits at `qwen_omni_realtime.py:316-321`.
- The effect: the `async with` exits and closes the agent socket while the cancelled task is still unwinding, and exceptions from the pending task are never retrieved.

**The bigger consequence (both bridges cancel on `FIRST_COMPLETED`):**
- When the carrier leg ends first (Plivo `stop` at `elevenlabs_realtime.py:285-286`, or a disconnect), the agent-to-carrier leg is cancelled immediately.
- Any final `user_transcript` or `client_tool_call` still in flight from ElevenLabs is dropped.
- Example: a caller says "I'm not interested, stop" and hangs up within the transcription latency.
  - Outbound: the per-utterance opt-out (`:415-419`) never runs.
  - Inbound: finalization gets a transcript without the last utterance.
- If cancellation lands while `voice_opt_out.honour` is awaiting its database write (`:418`), that transaction is aborted. `honour` catches only `OptOutNotRecorded` (`voice_opt_out.py:79-83`).
- Awaiting with `gather` alone does not fix either case.
- This is inferred from control flow and was not reproduced.

**Tracking:** none. **Category:** 09.

**Smallest fix:**
1. When the carrier leg finishes first, wait for the agent leg with a bound: `await asyncio.wait({to_caller}, timeout=2.0)`.
2. Then `cancel()` it and `await asyncio.gather(*pending, return_exceptions=True)`.
3. Inside `honour`, wrap `record_outbound_opt_out` in `asyncio.shield(...)`.
4. Make the same `shield` change in Qwen.

**Failing-first test:** in `tests/test_elevenlabs_realtime.py`, the fake Plivo socket sends `{"event":"stop"}` while the fake agent socket yields `user_transcript: "I'm not interested, stop."` after a 50 ms delay. Assert `record_outbound_opt_out` was called. This fails today because `to_caller` is cancelled before the transcript arrives.

**Severity:** P1 for the opt-out case (TCPA; the same class as RUN-02), P2 for resource cleanup.

---

## Cross-cutting notes
- **Truth claims:** RUN-02, RUN-03 and RUN-06 share one pattern. Compliance- or customer-relevant state lives only in bridge memory until hangup. Per-utterance or per-tool durable writes, as outbound opt-out already does under L09-05, are the consistent fix.
- **Missing sweeps:** RUN-01, RUN-02 and RUN-04 are each closed by one more step in `reconciliation.run_sweep`, which already runs every 15 minutes, plus one `component_health` count. Each new sweep step needs a test in `test_reconciliation_live.py` (real PostgreSQL).
- **Stale comment:** `clearPrivateCaches.js:3-5` says the SW serves media with no auth check; L16-05 has since added the check (see WEB-001).
