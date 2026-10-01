# Performance & capacity runbook

How to run Neoh's load tests without tribal knowledge. The harness lives in
`performance/` (see `performance/README.md` for the file map). The results
and the capacity decisions they support are in `docs/capacity-plan.md`;
the targets each test enforces are in `docs/performance-targets.md`.

## 0. Never production

Every entry point refuses to run unless **all** of these hold:

1. `NEOH_LOAD_TEST_ALLOWED=1` is set for this run;
2. the target is not a production hostname (`neohrs.com`, `neoh.app`, and
   anything in `NEOH_PRODUCTION_HOSTS` — add the production App Platform
   `*.ondigitalocean.app` name there);
3. the **target itself** answers `GET /version` with `environment` =
   `staging` or `loadtest` (`performance/guard.py`).

`run.sh` additionally refuses if the balancer routes to anything that is not
an API replica. Real providers are unreachable by construction (§2 below).

## 1. Test tiers (§68)

| Tier | When | What | Cost |
|---|---|---|---|
| **PR** | every commit (CI) | `backend/tests/test_perf_harness.py` + the query-shape/index tests (`test_client_list_query_shape.py`, `test_mls_overlay_indexable.py`, `test_ws_session_shape.py`, `test_interactive_job_queue.py`, `test_reconstruction_durable_queue.py`, …) — seconds, no load | $0 |
| **Nightly / manual** | before merging perf-sensitive work | `baseline`, `read_load` 25/75, `ws_load` 250, `ws_fanout`, `ai_chat` 10/25, `jobs_load.py --jobs 1000`, `mls_search` | $0 (mocks) |
| **Pre-production** | before a launch or plan change, on staging | `first10.sh` (15 min), soak `DURATION=30m`+, `voice_sim.py` 1→100, `webhook_burst`, breakpoint (`read_load` stepped), Valkey loss | $0 (mocks) + staging hours |
| **Real provider** | rarely, deliberately | `ai_real_sample.py` (≤30 requests, ≤$1 estimate, two opt-ins) | cents |

## 2. The environment

### Local production-shaped topology (what Mission 8 measured)

Two API replicas (`ORACLE_PROCESS_ROLE=web`, no `--reload`), one worker,
nginx round-robin (+ WebSockets), Valkey, the provider mock, against the dev
Postgres — inside the DinD host:

```sh
python3 performance/fixtures/perf_secrets.py                 # once: perf-only webhook keys
docker exec oracle-sypher-docker docker build -q -t oracle-backend-perf:latest \
  /media/ydn/SYPHER_CORE2/Oracle/performance/topology/image  # backend image + current requirements
docker exec -i -e PERF_GIT_SHA=$(git rev-parse HEAD) oracle-sypher-docker sh -s < performance/topology/up.sh
```

`up.sh` clones the dev backend's **configuration** but drops every provider
credential by pattern (model, voice, SMS, email, Stripe, GPU, AWS, maps …)
and injects perf-only secrets and the mock endpoints. It sets
`ORACLE_ENV=loadtest` and `ORACLE_RECOVERY_MODE=1` (no SMS/calls/email/
billing/scheduler). Options: `PERF_NO_VALKEY=1`, `PERF_WEB_PROCESSES=2`,
`PERF_EXTRA_ENV="KEY=v;KEY2=v"`, `MOCK_LLM_LATENCY_MS`/`MOCK_LLM_JITTER_MS`.

Stop the dev backend (`docker stop oracle-backend-1`) while measuring: it is a
second job worker on the same database and would skew worker results.

**This host is one 4-core machine**: API, worker, Postgres, load generator and
simulators share it. Results are valid *relative* measurements and bottleneck
discoveries; absolute DigitalOcean capacity must be re-measured on staging.

### DigitalOcean staging

The staging app (Mission 6) runs the release images. Set its
`ORACLE_ENV=staging`, point `TARGET=https://<staging>.ondigitalocean.app`,
put the production app's hostname in `NEOH_PRODUCTION_HOSTS`, and use staging's
own database. Provider credentials on staging must be sandbox/test ones or
absent; LLM/realtime must point at a mock (deploy `performance/mocks/
provider_mock.py` as a staging-only service) — **recovery mode does not block
model or realtime egress**.

## 3. Seed data

```sh
export NEOH_LOAD_TEST_ALLOWED=1
performance/pyrun.sh fixtures/seed.py --profile tiny     # 10 tenants, 169 users, 33.8k clients, 8.4k leads
performance/pyrun.sh fixtures/seed.py --profile pilot    # 50 tenants — staging-sized
performance/pyrun.sh fixtures/seed.py --routes-only      # webhook routes only (cheap)
docker exec -i oracle-sypher-docker sh -s < performance/fixtures/mint.sh   # 24 h sessions
```

Re-mint after 24 h. Expired sessions fail at the socket, so no work happens —
and a scenario gated only on percentiles and `count==0` would PASS with zero
samples. `lib/neoh.js` therefore refuses to start when any token expires within
`SESSION_MARGIN_S` (default 3600 s). If `up.sh` ran from the host against the
DinD socket rather than inside `oracle-sypher-docker`, `/tmp/neoh-perf.env` is
on the host: run `mint.sh` the same way
(`DOCKER_HOST=unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock sh performance/fixtures/mint.sh`).

Synthetic tenants are `perf-*`; every row carries the tenant's sentinel
(`SNTL…`). The shared data volume (≈10.5M leads, ≈10.6M property records) is
the dev database's own.

## 4. Run a scenario

```sh
export NEOH_LOAD_TEST_ALLOWED=1
sh performance/run.sh baseline
VUS=75 DURATION=3m LABEL=75vu sh performance/run.sh read_load
VUS=20 sh performance/run.sh write_load
CONNS=500 HOLD=90 RAMP=60 sh performance/run.sh ws_load        # STORM=1 for a reconnect storm
CONNS=400 RATE=50 DURATION=60s sh performance/run.sh ws_fanout && \
  python3 performance/fanout_analyze.py performance/out/results/ws_fanout-*.csv.gz
VUS=25 DURATION=2m THINK_MIN=5 THINK_MAX=15 sh performance/run.sh ai_chat
MODE=nat VUS=25 sh performance/run.sh auth_storm             # MODE=distinct too
RATE=25 DURATION=130s sh performance/run.sh rate_limit
performance/pyrun.sh fixtures/webhooks.py --telnyx 1500 --stripe 200 --calls 150 && \
  RATE=60 sh performance/run.sh webhook_burst                # within 300 s of signing
VUS=25 sh performance/run.sh mls_search
python3 performance/jobs_load.py --jobs 10000 --ms 50
performance/pyrun.sh mls_ingest.py --records 100000 --batch 500 [--update]
performance/pyrun.sh voice_sim.py --calls 25 --seconds 30
VUS=6 sh performance/run.sh recon_queue
DURATION=15m sh performance/first10.sh                      # the launch gate workload
```

Real model sample (deliberate, capped):
`NEOH_LOAD_TEST_ALLOWED=1 NEOH_REAL_AI_ALLOWED=1 FIREWORKS_API_KEY=… python3 performance/ai_real_sample.py --requests 12 --concurrency 2`

## 5. Monitor while it runs

`run.sh` samples every 5 s into `<run>.resources.jsonl`: per-container CPU and
memory, Postgres connections (total/active/idle-in-tx), lock waits, job backlog
and oldest queued age. Watch live with
`docker exec oracle-sypher-docker docker stats` and
`SELECT state, count(*) FROM pg_stat_activity GROUP BY 1`. Query hot spots:
`pg_stat_statements` (enabled on the perf database; `SELECT
pg_stat_statements_reset()` before a run). On DigitalOcean use App Platform
Insights and the Managed Postgres/Valkey dashboards — the same signals.

## 6. Stop safely

Ctrl-C a k6 run (it stops within `gracefulStop`). Kill a background tool with
its PID. Then `docker exec -i oracle-sypher-docker sh -s <
performance/topology/down.sh` removes every perf container and volume.

## 7. Read the results

Each run leaves `<run>.result.json` (verdict, target identity from the guard,
data scale, per-endpoint latency, replica split, resource peaks),
`<run>.analysis.json`, `<run>.csv.gz` (every sample) and the resources file.

- `verdict` is k6's threshold verdict — PASS/FAIL, not a judgement call.
- `tenant_leaks > 0` anywhere → stop everything; it is a security incident.
- Latency rising while DB CPU saturates and web CPU does not → the database
  is the bottleneck (see capacity plan); more API replicas will not help.
- `timeline.py <csv> <endpoint> 15` shows WHEN latency rose (start-up burst
  vs steady queueing need opposite fixes).

## 8. Clean up

```sh
performance/pyrun.sh fixtures/seed.py --cleanup     # every perf-* tenant and its rows
performance/pyrun.sh mls_ingest.py --cleanup        # the perf-mls feed
docker exec -i oracle-sypher-docker sh -s < performance/topology/down.sh
```
`performance/out/` is gitignored (synthetic credentials and raw results).
