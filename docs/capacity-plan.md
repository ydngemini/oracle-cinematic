# Capacity plan

What Neoh can carry, measured — and what is still a guess. Every number here
comes from a result file under `performance/out/results/` (named in each
table) produced by the harness in `performance/` (runbook:
`docs/performance-capacity-runbook.md`; pass/fail targets:
`docs/performance-targets.md`; database connections:
`docs/database-connection-budget.md`).

## Where these numbers come from

- **Topology:** local, production-shaped — 2 web replicas (1 uvicorn process
  each) + 1 worker + nginx round-robin + PostgreSQL 16 + Valkey 8 + the
  provider mock, on **one 4-core host** that also runs the load generator.
  **Not DigitalOcean.** Read every figure as a relative measurement and a
  bottleneck discovery; absolute DO capacity is re-measured on staging.
- **Data:** the `tiny` seed profile — 10 tenants (4 solo, 3 small,
  2 brokerage, 1 large), 169 users, 33.8k clients, 8.4k leads; MLS search
  against 52.6k ACTRIS rows plus the perf ingest feed.
- **Providers:** all mocked. The LLM mock answers in 2.5 s ± jitter; no run
  has used a real model (see *Still unknown*).
- **Code:** git `3835414`, migration head `0116_mls_search_order_index.sql`.

## Baseline

One user per tenant shape, no contention (`baseline-post-reboot`): every
endpoint p95 < 225 ms; the slowest is `GET /api/mls/search` at
p95 189–221 ms (p99 ≤ 432 ms). Everything else in the browse path is under
100 ms p95. These are the floors the load targets are set against.

## The launch gate: first 10 brokerages

`performance/first10.sh` runs `scenarios/mixed.js` — 25 people browsing,
3 writing, 50 open tabs on the WebSocket, 10 chatting with Neoh — alongside
5 simulated voice calls and an MLS ingest, for 15 minutes.

| Run | Requests | Errors | Worst p95 (excl. CSRF prime) | DB CPU mean / peak | Job queue oldest wait (peak) |
|---|---|---|---|---|---|
| `mixed-first10` (4 job workers) | 11,676 | 0 | client search [large] 248 ms | 0.36 / 2.07 cores | **403 s** |
| `mixed-soak30` (8 job workers, 30 min) | 23,226 | 0 | client search [large] 243 ms | 0.39 / 1.71 cores | 6 s |

Voice during first10: 5/5 calls completed, turn p95 624–642 ms, 0 late frames.
Database: 0 lock waits, peak 36 connections, ≤ 6 idle-in-transaction.

**Verdict: PASS.** The 15-minute gate exposed one capacity defect — jobs
waited up to 405 s to *start* while running in 0.22 s on average, because
1,215 `crm:client_reconcile` and 543 `ai_chat:response` jobs held all 4
default slots. Raising `ORACLE_JOB_WORKERS` to 8 took the peak wait to 6 s in
the 30-minute soak.

## Per-subsystem ceilings

| Subsystem | Result file | Measured | Bottleneck / note |
|---|---|---|---|
| CRM / command-center reads | `read_load-25vu`, `-75vu` | 0 errors at 75 VUs | DB CPU (1.38 cores mean at 75 VUs) |
| MLS search | `read_load-75vu`, `mls_search-*-v2` | p95 983 ms at 75 VUs before 0116; 83–144 ms p95 after, *during* ingest | was a sort over the overlay — fixed by migration 0116 |
| MLS ingest | `mls_search-during-ingest-v2` | 100k records alongside search, search p95 144 ms | DB CPU peaks 2.08 cores |
| CRM writes | `write_load-20vu` | create p95 82 ms, patch p95 47 ms | none found |
| WebSocket sessions | `ws_load-1000-cap2000` | 1,000 concurrent, connect p95 7 ms, first frame p95 9 ms, 0 abnormal closes | `ORACLE_WS_MAX_CONNECTIONS` cap, not CPU |
| Cross-replica fanout | `ws_fanout-400c-50rps-v2` | 400 clients × 50 updates/s, 120,040/120,040 delivered, p95 20 ms | none found |
| Neoh chat | `ai_chat-*` | 50 conversations at first-token p95 4.7 s (16 slots); ceiling 4.45 turns/s | interactive job slots (slots ÷ 3.5 s) |
| Voice | `voice_sim.py` 1→100 calls | 100 calls, 0 failed, turn p95 687 ms, 0 late frames | web CPU: 91–93 % peak per replica at 100 calls |
| Webhooks | `webhook_burst-60rps-v3` | 2,727 at 60/s, 0 errors; telnyx p95 168 ms / p99 3.5 s | tail latency under burst |
| Job queue | `jobs_load-10000-2workers` | 10,000 jobs drained at 117/s, 0 duplicates, 0 dead letters | worker slot count |
| Auth storm (shared NAT) | `auth_storm-nat-fixed3` | login p95 73 ms | rate-limit key, not CPU |
| Rate limiting | `rate_limit-25rps-lua` | hammer throttled (≈55 % 429, intended), colleague on the same tenant 0 % | — |
| Valkey loss | `read_load-valkey-stop-v2`, `-pause-v2` | 0 errors with Valkey stopped or paused | falls back to the PostgreSQL window |

## Chat

The turn is: SEND → ACCEPTED (admission) → job on the **interactive** queue →
model → first delta. Admission was never the problem once fixed (p95 ≤ 28 ms
at every level). First-token time is governed by Little's law: each turn holds
an interactive slot for ~3.5 s (2.5 s mock model + tool/DB work), so
**8 slots ≈ 2.3 turns/s**. At 50 VUs with 5–15 s think time the offered load
is ~3.7 turns/s, so turns queue — the v3 50-VU run completed exactly 2.3
turns/s and first-token p95 rose to 13 s.

Think time 5–15 s, 2-minute runs, mock model 2.5 s ± jitter. Gate: first-token
p95 < 10 s (`ai_chat.js`, enforced since the Mission 8 close-out — before it,
only admission was gated and the 13 s run passed).

| Run | Concurrent chats | Interactive slots | Turns/s | First token p50 / p95 / p99 | Admission p95 | Errors / timeouts | DB conns peak | Verdict |
|---|---|---|---|---|---|---|---|---|
| `ai_chat-10vu-v3` | 10 | 8 | — | 3.6 / 4.6 / 4.9 s | 28 ms | 0 / 0 | 24 | PASS |
| `ai_chat-25vu-v3` | 25 | 8 | — | 3.7 / 4.7 / 5.6 s | 22 ms | 0 / 0 | 34 | PASS |
| `ai_chat-50vu-int8` | 50 | 8 | 2.25 | 11.7 / **13.6** / 14.2 s | 61 ms | 0 / 0 | 30 | **FAIL** |
| `ai_chat-50vu-int16` | 50 | **16** | 3.55 | 3.8 / **4.7** / 5.3 s | 79 ms | 0 / 0 | 34 | PASS |
| `ai_chat-100vu-int16` | 100 | 16 | 4.45 | 12.1 / 13.3 / 13.7 s | 71 ms | 0 / 0 | 35 | FAIL (ceiling) |

Both failures sit exactly on the formula: 8 ÷ 3.5 s ≈ 2.3 turns/s and
16 ÷ 3.6 s ≈ 4.45 turns/s. Overload degrades gracefully — turns wait, none
error or time out — but a person waiting 13 s for the first word is a failure.

**Sizing rule:** interactive slots ≥ peak turns/s × seconds a turn holds a
slot. With the mock, 16 slots carry ~50 simultaneous conversations. The
production default is now 16 (`infra/digitalocean/app.yaml`,
`automation_jobs.py`); doubling slots cost 4 database connections, because a
handler does not hold one across the model call. **A real model that takes
6 s instead of 3.5 halves the conversations per slot** — re-derive from
`ai_real_sample.py` before launch (see *Still unknown*).

## Voice

Turn latency is flat (≈ 622 ms p95, dominated by the mock's fixed latency) up
to 50 calls and rises to 687 ms p95 at 100; web CPU is ≈ 1.1 % per call per
replica and is the only resource that moves (database < 8 %, Valkey < 1 %).

**Scale voice with replicas, never `--workers`.** Two uvicorn processes per
replica at 100 calls produced 1,628 late audio frames (0 with one process),
doubled memory per replica (255 → 561 MiB) and did not relieve CPU.

## Decisions taken from these measurements

1. **PostgreSQL 2 vCPU / 4 GiB on DO**, not the 1 GiB tier — its 22
   connections are fewer than the pools can open, and the database CPU is the
   first resource to saturate (`docs/database-connection-budget.md`).
2. **`ORACLE_JOB_WORKERS=8`** (was 4) — the first10 queue wait.
3. **Chat runs on the interactive queue** (`ai_chat_api.py`,
   `INTERACTIVE_QUEUE`) — a person is waiting; never behind an MLS backfill —
   with **16 slots** (`ORACLE_INTERACTIVE_JOB_WORKERS`, was 8).
4. **One uvicorn process per replica**; scale web with replicas.
5. **Autoscaling max ≤ the connection budget's ceiling** — each web replica
   and worker opens its own pool.
6. **Migration 0116** — MLS search order index.

## Still unknown

- **Everything on DigitalOcean.** No figure above was measured there.
- **Pilot data scale** — the 50-tenant `pilot` profile has not been run.
- **Real model latency.** Every chat and voice number used the mock; real
  first-token time and its variance change the slot arithmetic directly.
- **PostgreSQL CPU on the DO 2 vCPU plan.** The first-10 mix already peaked
  at 1.7–2.1 cores locally; the DO plan has little headroom above that.
