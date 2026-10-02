# Neoh — Provider Resilience Drill Report

**2026-10-02.** Every failure here was injected and observed. None were assumed.

| | |
|---|---|
| Code under test | `67ef19b` plus the uncommitted resilience changes (committed with this report) |
| Environment | local perf topology in DinD: 2 API replicas behind nginx, 1 worker, Valkey, the shared dev PostgreSQL, the provider mock |
| Providers contacted | **none**. Every provider was the mock (`performance/mocks/provider_mock.py`), and egress was blocked by `ORACLE_RECOVERY_MODE=1` |
| Customer data touched | none. The probes read; the MLS drill used its own feed id `drillmock` and deleted its rows |
| Harness | `performance/resilience/` (`run_drill.sh`, `probe.py`, `analyze.py`, `chat_probe.py`, `mls_drill.py`, `worker_crash_drill.py`, `integrity_check.py`) |
| Raw evidence | `performance/out/drills/*.jsonl` and `*.summary.json` |

## How a drill is measured

A probe samples about once a second through the load balancer:
- `/live` (process up);
- `/health` (ready);
- `GET /api/crm/clients?limit=1` as a real agent session (what a user sees);
- `/api/status` (the product status banner).

A second sampler records every `component_health` component, so detection is measured **per component**, not just for the whole app. For LLM drills a third probe sends real Neoh chat turns over `/ws`.

Each drill runs 20 s of baseline, then injects the fault, holds it, restores it, and watches recovery.

- **Detected** = first sample after injection whose state differs from the baseline.
- **Recovered** = first sample after restore that is back to baseline.
- Baseline `/api/status` in this topology is `DEGRADED`, not HEALTHY. Recovery mode leaves the scheduler UNKNOWN and blocks the email outbox; that is expected here and is not a failure.

## Results: final code (round 3, plus round 1/2 where the code did not change)

`/live` answered 200 in **every sample of every drill**, so no replica was ever killed for a dependency outage.

| Drill | What users saw while the fault was in | Detected | Recovered | Verdict |
|---|---|---|---|---|
| PostgreSQL stopped | CRM 503 "temporarily unavailable", every answer ≤ 193 ms, `Retry-After` | 1.2 s | 2.9 s | PASS |
| PostgreSQL frozen (paused) | CRM 503 or the client gives up at 12 s (bounded by the 30 s command timeout); nothing reports success | 14.8 s | 14.9 s (includes the 15 s status cache) | PASS, with a known bound (see gaps) |
| All app DB connections killed | CRM 100%, max 342 ms; the realtime listener logged "lost; reconnecting" → "reconnected" | — (self-healed before the 15 s probe) | 0.3 s | PASS |
| Table lock held 60 s (`ACCESS EXCLUSIVE` on `clients`) | **before the fix:** every CRM request hung to the 12 s client timeout. **after:** 503 within ≤ 5.1 s (`lock_timeout=5s`) | — | 0.7 s | PASS (fixed) |
| Valkey stopped | CRM 100%, max 313 ms; calling unavailable (by design) | `valkey` 1.0 s | 1.8 s | PASS |
| Worker killed (held 210 s) | CRM 100%, max 452 ms; queued work waits (durable) | `workers` UNAVAILABLE at 111.6 s | 9.4 s after restart | PASS: detection is bounded by 4 missed 30 s heartbeats (120 s) |
| API replica restarted | CRM 100%, max 388 ms (nginx routed to the other replica) | — | 0.1 s | PASS |
| AI model hangs (timeout) | **before the fix:** both chat turns spun with no answer for more than 70 s. **after:** both turns ended at **90.2 s** with `AI_CHAT_ERROR` and the product message (`AI_RESPONSE_UNAVAILABLE`); CRM 100%, max 371 ms | the turn itself at 90 s (the `ai` component was already red from the earlier drills) | next turn | PASS (fixed) |
| AI model 429 | chat: `AI_CHAT_ERROR` "Neoh couldn't complete that response. Your work is saved — try again in a moment." in 0.3–0.5 s; CRM 100%, max 399 ms | `ai` UNAVAILABLE at 1.1 s | HEALTHY on the next successful turn (healthy baseline afterwards: 3/3 turns complete in 3.1–4.5 s) | PASS |
| AI model 500 | CRM 100%, max 319 ms | — | 0.5 s | PASS |

### MLS feed drill (real RESO sync code against the mock board)

| Phase | Result |
|---|---|
| 1. healthy | 120 rows, feed READY, cursor advanced |
| 2. 401 | sync **failed in 0.0 s** (no retry storm); feed `AUTH_ERROR`; 120 rows kept; cursor unchanged |
| 3. 503 outage | retried with backoff, failed after 5.2 s; feed `DEGRADED`; rows and cursor unchanged |
| 4. every 10th record corrupt | 126 upserted, **14 refused and counted** (`rejected_on_write`); the rest of each page persisted (one bad row used to lose the whole page) |
| 5. recovered | 140 rows, READY |

**The first run of this drill found a defect.** Phases 2–5 all reported "succeeded", with no fault reaching the board. Sync pages were read through the integration cache, which has a 7-day stale window. Whenever a board's cursor stood still (no new changes), a re-sync during an outage replayed the cached page, recorded success, and the feed showed READY with the board down. Both feed clients now read the board directly (`_fetch_page`), with a regression test (`test_sync_page_is_a_live_read_never_a_cached_replay`). The table above is the rerun.

### Stripe webhooks against real PostgreSQL (`tests/test_stripe_webhook_live.py`)

The events are signed exactly as Stripe signs them. All 7 scenarios pass:
- an `invoice.paid` that arrives before checkout is answered 503 (Stripe redelivers it) and applied once the subscription exists;
- an older update arriving late does not revert a newer one;
- the same event delivered 12× concurrently has one effect, with 11 answered `duplicate`;
- deletion is terminal;
- a forged signature gets 400.

### Worker crash mid-job

`run_worker_crash_drill.sh`: 40 durable jobs (3 s each) enqueued; the worker is `docker kill`ed 9 s in, with 4 jobs mid-run, and restarted 8 s later.

| t (s) | queued | running | succeeded | attempts | distinct executions | duplicates |
|---|---|---|---|---|---|---|
| 9 (killed) | 29 | 4 | 7 | 11 | 7 | 0 |
| 52 | 0 | 4 (orphaned leases) | 36 | 40 | 36 | 0 |
| 138 | 0 | 0 | **40** | 44 | **40** | **0** |

Every job completed exactly once. The 4 interrupted jobs were re-claimed when their 120 s lease lapsed, with no operator action. The cost of a crash is therefore up to 120 s of delay for in-flight jobs, never a lost or doubled job. Delivery is at-least-once by design: a handler killed in the instant between finishing and recording completion would run twice. That is why every side-effecting handler records intent first and is reconciled from the provider's records, never blindly rerun.

### Data integrity after the drills (`integrity_check.py`)

Run after all drills, on the request pool (`oracle_app_login`, FORCE RLS). Result: **all invariants hold** (`performance/out/drills/integrity-check.json`).

| Invariant | Result |
|---|---|
| tenant sees only itself | 21,059 own rows, **0 foreign** |
| no tenant context sees nothing | 0 rows |
| orphaned job leases (> 5 min past expiry) | 0 |
| command executions stuck `executing` > 15 min | 0 |
| chat turns stuck pending > 15 min | 0 |
| email outbox: `sent` without a provider id / duplicate provider ids | 0 / 0 (2 rows `queued`: blocked by recovery mode, provably never sent) |
| audit hash chain | verifies end to end |
| MLS drill rows left behind | 0 |
| open alerts | `mls` STALE (one real feed is stale in dev), `email_outbox` DEGRADED (the 2 queued rows). Both are true conditions of this topology, not drill debris |

### Queue backlog (5,000 jobs at once, `jobs_load.py`)

5,000 durable jobs (50 ms each) were seeded at once onto the default queue with 1 worker (4 slots).

- **Result:** all 5,000 succeeded, with **0 duplicates, 0 dead letters, 0 retries**. Drained in 168.6 s (29.7 jobs/s); peak age of the oldest queued job 167 s.
- **Compared with Mission 8:** the same shape drained at 49.3 jobs/s on 2026-09-26.
- **The new claim is not the cause:**
  - an A/B of the old and new claim SQL against a 5,000-job backlog (inside a rolled-back transaction) measured the **new claim at about 2.6 ms vs about 8.5 ms**, roughly 3× faster;
  - `pg_stat_statements` shows every per-job statement at or below 1.3 ms.
- **Not isolated:** the cause of the throughput gap. This dev DB now carries 34k job rows and 300k completion updates from today's drills. It is listed under known gaps, to re-measure on staging.

### Webhook storm (60/s, `webhook_burst`, freshly signed with each provider's real scheme)

| Kind | Sent | Result |
|---|---|---|
| Telnyx inbound SMS (+10 % duplicate redeliveries) | 1,788 + 194 | 0 % errors; duplicates accepted and ignored |
| Stripe events (+ duplicates) | 199 + 19 | 0 % errors; **200 distinct events in the ledger**, every duplicate absorbed |
| Plivo answer (+ duplicates) | 149 + 15 | 0 % errors |
| Telnyx delivery receipts for messages that do not exist | 596 | **503 by design.** A receipt that races ahead of its message row is redelivered by Telnyx instead of being dropped (this mission's change). The k6 "rejected" threshold counts these |

Tail latency under the burst on this host: p99 6–7 s (Mission 8: 3.5 s), and the arrival-rate executor dropped about 10 % of iterations. Throughput behaviour is unchanged by this mission; the host had been under drills all day.

**The first run found a defect.** A correctly signed Stripe event whose id the ledger's CHECK refuses answered **500**, which Stripe redelivers for days. It is now a permanent 400 (`test_stripe_webhook_live.py` step 8). (The trigger was the harness's own fixture ids, which were also fixed.)

### Realtime voice failure (mock DashScope `rt_fail`, 2 calls × 10 s per mode)

| Mode | Media socket | Audio heard | What the caller gets now |
|---|---|---|---|
| refuse (provider closes before the session) | closed 1011 | none | inbound: handed to the agent, or told the agent will call back. Outbound: goodbye |
| after_audio (drops after the caller speaks) | closed 1011 | none | same |
| mid_reply (drops halfway through Neoh's reply) | closed 1011 | one partial reply | same |

All three close the media stream (no hung bridge). Healthy baseline: 30/30 calls complete, turn p95 ≈ 700 ms.

### First-10-brokerage combined run under faults

`first10.sh`, 6 min: the k6 mixed workload (browsing, CRM writes, open tabs, Neoh chat across solo/small/brokerage/large tenants) **plus** 5 concurrent simulated calls back to back, a paced MLS delta sync, and 60 background jobs/min. During the run:
- the AI model answered 429 for 60 s (at +2 min);
- the worker was `docker kill`ed for 30 s (at +4 min).

| Signal | Result (final code) |
|---|---|
| CRM / MLS search requests | **4,517 requests, 0 errors**, evenly split across the 2 replicas |
| Voice | **30/30 calls completed**, 0 failed; turn p95 659–717 ms |
| MLS sync | 14,400 records upserted at 36 rec/s; batch p95 830 ms |
| Background jobs | **360/360 succeeded**; lower-priority wait ≤ 125 s with the worker kill (was 337 s and unbounded) |
| Neoh chat | k6 `ai_errored` / `ai_turn_timeout` thresholds crossed, **as injected**. Turns in the 429 window got the product error; the turn the worker kill interrupted got `AI_RESPONSE_INTERRUPTED` |

The harness's `crm:client_reconcile` waits (avg 58 s) reflect this topology's 4 default workers. Production runs `ORACLE_JOB_WORKERS=8`, the Mission 8 fix for exactly this load.

## Defects found by injecting failures, and fixed

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | conn_reset | The cross-replica realtime listener never reconnected after its connection died: live updates silently stopped on that replica | Supervisor with a probe, termination listener and jittered reconnect (`ws_hub.py`) |
| 2 | db_pause | Pool acquire was unbounded, so requests hung for as long as the DB did | 10 s acquire bound gives a 503 with `Retry-After` |
| 3 | db_stop | The rate limiter answered 429 "slow down" when its stores were down | 503 (still fail-closed) |
| 4 | db_lock | A held lock froze every request touching the table until the 30 s command timeout | `lock_timeout=5s` on every pool, with explicit longer waits only for the audit chain (30 s) and the MLS feed lock (300 s) |
| 5 | llm_timeout | A hung model left the chat spinning forever | 90 s per-turn deadline and an honest product error |
| 6 | llm_429 | `ai` health stayed red for 15 min after recovery | recovers on the latest successful turn |
| 7 | MLS drill | Feed outages masked by a 7-day page cache (above) | live reads |
| 8 | outbox drill | An email blocked by recovery mode was misfiled as "delivery unknown" (and would never be sent) | returns to `queued` |
| 9 | alert evaluator in drills | Alerts opened and closed every minute during restarts | resolution hysteresis (2.5 evaluation intervals, HEALTHY or NOT_CONFIGURED only) |
| 10 | valkey drill | The Valkey health probe read the rate limiter's cached client state, so an outage was never seen | its own 1 s probe |
| 11 | first-10 combined run | **Strict-priority job claims starved lower-priority work** for as long as higher-priority jobs kept arriving: no-op jobs waited up to 337 s and all started only when the load stopped. Privacy exports (60) and erasures (80) sit below `crm:client_reconcile` (45) on the same queue | claim aging: a job ready > `ORACLE_JOB_STARVATION_SECONDS` (60) is claimed oldest-first (migration 0124 index; `test_job_claim_aging_live.py`). Rerun: max wait 125 s *including a worker kill*, 60–67 s otherwise |
| 12 | voice drill (`rt_fail`) | A realtime-model failure mid-call dropped an **inbound** caller with "Goodbye" | the bridge redirects the caller to the agent (`ai_unavailable` gate). Otherwise the caller hears that the agent will call back |
| 13 | worker kill during chat | A chat turn interrupted by a worker crash was **re-run from scratch** when its lease lapsed. That could repeat CRM tool writes, and the user had long since given up | a re-claimed `streaming` turn ends with `AI_RESPONSE_INTERRUPTED`, "please ask again". Proven live: error at 120 s, no regeneration, next turn completes in 8.5 s |
| 14 | webhook storm | A signed Stripe event with an id the ledger refuses answered 500 (Stripe redelivers 5xx for days) | 400 before the insert |

Defects found by code audit and proven by tests are listed in `docs/provider-failure-matrix.md`. They include:
- Plivo re-POSTing a call 3× (proven with the real SDK);
- Telnyx's hidden POST retries;
- Twilio having no timeout;
- SMTP ambiguity after DATA;
- Stripe calls blocking the event loop;
- duplicate number purchases;
- job leases expiring under long handlers.

## Known gaps (honest)

- **A frozen (not stopped) database** is bounded by the 30 s command timeout, not the 10 s acquire bound. An already-acquired connection waits on the server.
- **Mid-call voice-model failure on an outbound AI call**: the client hears a goodbye. Inbound callers are now handed to the agent.
- **A chat turn interrupted by a worker crash** reports "interrupted" only when its 120 s lease lapses. It is never re-run, but the user waits up to 2 minutes for that answer.
- **An API restart ends any live call** carried by that replica. Drain before deploying during business hours.
- **Plivo number purchases** are reconciled manually; Twilio's are automatic.
- **Stripe events created in the same second** apply in arrival order.
- **10DLC brand/campaign** registration repeated by PUT can create duplicates at the provider.
- **RunPod SSH** runs with `known_hosts=None` (host key not pinned).
- **Scheduler health is UNKNOWN in this topology** (recovery mode never starts it), so its detection was proven by unit test, not by drill.
- **Backlog drain throughput** measured 29.7 jobs/s here vs 49.3 jobs/s in Mission 8 (same shape). The claim query is exonerated (3× faster in A/B); the cause is not isolated. Re-measure on staging.
- **Not drilled against real providers or on DigitalOcean staging.** All provider behaviour here is the mock's. The DO-specific paths (`/live` liveness, the smoke test's Spaces WARN) are configured but were not exercised on App Platform.
