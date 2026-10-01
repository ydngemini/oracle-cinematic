# performance/ — Neoh's load-test harness

Framework: **k6** (HTTP + WebSocket, arrival-rate executors, thresholds that
PASS/FAIL, CSV output) for request/socket load; small Python tools for what k6
cannot do natively (Ed25519/Plivo signing, the Plivo media stream, direct
queue/ingest drivers). How to run everything: `docs/performance-capacity-runbook.md`.
Results and decisions: `docs/capacity-plan.md`. Targets: `docs/performance-targets.md`.

| Path | What |
|---|---|
| `guard.py` | Fail-closed production refusal (opt-in + host + the target's own `/version`) |
| `run.sh` | Guard → upstream preflight → k6 → analyze → result artifact |
| `pyrun.sh` | Run a Python tool inside the perf network with the perf env |
| `monitor.py` | Container CPU/memory + Postgres connections/locks/job backlog every 5 s |
| `analyze.py` | Per-endpoint p50/p95/p99, error rate, payload size, replica split |
| `result.py` | The per-run artifact (§70), credentials scrubbed |
| `timeline.py` | Latency over time for one endpoint |
| `fanout_analyze.py` | Joins WS fan-out sends to receipts: latency, missed, duplicates |
| `jobs_load.py` | Durable-queue throughput + exactly-once (via Valkey) |
| `mls_ingest.py` | Synthetic listings through the real MLS sink |
| `voice_sim.py` | Simulated calls on the production Plivo media WebSocket |
| `ai_real_sample.py` | The ONLY real-model caller: two opt-ins, hard caps |
| `first10.sh` | The first-10-brokerages mixed workload (launch gate) |
| `lib/neoh.js` | Sessions, CSRF, per-user IPs, and the tenant-sentinel check on EVERY response |
| `scenarios/` | `baseline`, `read_load`, `write_load`, `ws_load`, `ws_fanout`, `ai_chat`, `auth_storm`, `rate_limit`, `webhook_burst`, `mls_search`, `recon_queue`, `mixed` |
| `fixtures/` | `seed.py` (synthetic brokerages + sentinels), `sessions.py`/`mint.sh`, `perf_secrets.py`, `webhooks.py` |
| `mocks/provider_mock.py` | OpenAI-compatible LLM + DashScope-shaped realtime, instrumented |
| `topology/` | `up.sh`/`down.sh`, `nginx.conf`, `image/` (backend image + current requirements) |
| `out/` | gitignored: synthetic credentials, fixtures, results |

Harness self-tests run in CI: `backend/tests/test_perf_harness.py`.
