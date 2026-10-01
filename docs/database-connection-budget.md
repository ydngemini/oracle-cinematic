# Database connection budget

**This budget, not CPU, decides how many API replicas Neoh may run.** Scale the
API past it and every process competes for connections Postgres will refuse —
autoscaling on CPU would turn a busy hour into an outage.

## What DigitalOcean gives (provider hard limit)

DigitalOcean Managed PostgreSQL allows **25 connections per GiB of RAM, minus 3
reserved** for maintenance (live DO docs, verified 2026-09-25):

| Plan (RAM) | Usable backend connections |
|---|---|
| 1 GiB | **22** |
| 2 GiB | 47 |
| 4 GiB | 97 |
| 8 GiB | 197 |

Managed PgBouncer pools are carved *out of* that number, not added to it, and
their default **transaction** mode breaks `LISTEN/NOTIFY` (which Neoh's
cross-replica WebSocket fan-out depends on), prepared statements and advisory
locks. Neoh connects directly; PgBouncer is not a free multiplier here.

## What Neoh can open (from the code)

`db/connection.py`: each process opens up to `ORACLE_DB_POOL_MAX` (default
**10**) connections for requests **plus 1** held permanently by the `ws_hub`
LISTEN connection. One uvicorn process per instance (`backend/Dockerfile`).

| Consumer | Count | Max connections |
|---|---|---|
| API replicas (`api`, `instance_count: 2`) | 2 × (10 + 1) | 22 |
| Worker (`worker`, 1 instance) | 1 × (10 + 1) | 11 |
| Migration job (release) | 1 | 1–2 |
| Operators / psql / DR tooling | reserve | 3 |
| **Total at the current spec** | | **~38** |

## The finding

`docs/deploy-digitalocean.md` sizes Postgres at the **smallest tier (1 vCPU /
1 GiB) → 22 connections**. The current spec can open **~38**. On that plan the
pools exceed the server's limit as soon as both API replicas and the worker
are busy at once, and new connections fail with `too many connections`.

The measured load (docs/capacity-plan.md) also puts the database CPU, not the
API, first to saturate — on a 4-core local Postgres. A 1-vCPU database would
saturate far earlier. **Minimum for launch: the 4 GiB plan (2 vCPU / 4 GiB,
97 connections).**

## Safe API replica count

```
max_api_replicas = floor( (usable − worker − migration − operator_reserve)
                          / (ORACLE_DB_POOL_MAX + 1) )
```

Keep **30% headroom** (deploy overlap: during a rolling deploy old and new API
instances are both connected; a stuck release holds connections too):

| Plan | Usable | − worker 16 (see below) − 2 − 3 | × 0.7 | ÷ 11 per API replica | **Max API replicas** |
|---|---|---|---|---|---|
| 1 GiB | 22 | 1 | 0.7 | — | **0 — cannot run the current spec** |
| 2 GiB | 47 | 26 | 18 | 1.6 | **1** (not HA) |
| 4 GiB | 97 | 76 | 53 | 4.8 | **4** |
| 8 GiB | 197 | 176 | 123 | 11.2 | **11** |

Worker pool: the worker now runs 8 default + 16 interactive job workers, 2
voice workers and a reconstruction worker; `infra/digitalocean/app.yaml` gives
it `ORACLE_DB_POOL_MAX=15` (16 with the listener). Job slots are not
connections: handlers do not hold one across a model call. Measured at 50
concurrent chats, going from 8 to 16 interactive slots raised the database's
peak connections from 30 to 34 with 0 lock waits, so 15 per worker is still
headroom, not a guess at a need.

## Rules

1. **Autoscaling max ≤ the "Max API replicas" row for the current plan.** Never
   set App Platform `max_instance_count` above it.
2. Raising `ORACLE_DB_POOL_MAX` lowers the replica ceiling — recompute.
3. Adding a second worker (for voice capacity) costs 16 — recompute.
4. Alert at **70%** of usable connections (`SELECT count(*) FROM
   pg_stat_activity`); act at 80%.
5. The `/health` endpoint reports each process's pool (`db.size`, `db.idle`,
   `usable_for_requests`) — sum across instances to check the budget live.
