# Dependency resilience map

Every external dependency Neoh has, what it is for, what happens to customers
when it fails, and what bounds the failure. As of 2026-10-02 (resilience
mission). The failure-by-failure table is `docs/provider-failure-matrix.md`;
the drills that back it are `performance/resilience/` (results in
`docs/resilience-drill-report.md`).

**The blast-radius rule.** Only the database stops the service. Everything else
degrades one feature, uses a cache, or queues work, and the CRM keeps working.
Health is reported per component in one normalized model
(`backend/component_health.py`). Alerts come from `backend/ops_alerts.py`.

## Classes

| Class | Meaning | Members |
|---|---|---|
| CRITICAL CORE | Without it Neoh cannot serve | DigitalOcean App Platform, Managed PostgreSQL |
| IMPORTANT FEATURE | One capability stops; the CRM works | Valkey (calling), AI model (Neoh chat), voice realtime (AI calls), Plivo/Twilio (calls), Telnyx (texts), SMTP (email), Stripe (new subscriptions), Google (calendar, mailbox), MLS feeds (fresh listings), Spaces (media), RunPod (3D) |
| OPTIONAL ENRICHMENT | Data is missing or stale; nothing breaks | geocoding, census/ACS, FEMA, AVM (RentCast/ATTOM), Regrid, public-records harvesters, web research |
| BUILD/DEPLOY ONLY | Affects releases, never running traffic | GitHub, GitHub Container Registry, DO build pipeline |

## Dependencies

| Dependency | Class | Unavailable → | Timeout | Retries | Breaker / guard | Health component |
|---|---|---|---|---|---|---|
| **PostgreSQL** | core | STOP SERVICE honestly: 503 "temporarily unavailable, your work is saved". `/live` stays 200 so replicas are not restarted into a crash loop | connect 60 s; command 30 s; **pool acquire 10 s** (`ORACLE_DB_ACQUIRE_TIMEOUT`, was unbounded) | pool reconnects; job loops back off exponentially with jitter (was a fixed 4 s); requests are not retried | readiness `/health` fails; rate limiter answers **503** rather than a misleading 429 | `database` |
| **Realtime fan-out** (Postgres LISTEN) | core | live updates and worker→API chat replies pause on that replica | probe 5 s every 15 s | **reconnects with jittered backoff to 30 s** (it never reconnected before) | — | `realtime_fanout` |
| **Job workers** | core | jobs, sends, syncs wait (durable) | lease 120 s, **renewed automatically** while running | job backoff 5·2ⁿ s, bounded attempts | lease expiry; dead letter | `workers`, `job_queue` |
| **Scheduler** | core | periodic work waits | — | a failed enqueue retries on the next tick (it skipped a whole interval before) | heartbeat row `role='scheduler'` | `scheduler` |
| **Valkey** | important | rate limits fall back to PostgreSQL, AI chat admission uses the database, **calling is unavailable** (call state needs it), caches fall to Postgres | 0.5–2 s socket | reconnect after a 10 s breaker | breaker (rate limiter) | `valkey` |
| **AI model** (Fireworks / local / Foundry / Bedrock via `llm_gateway`) | important | "Neoh couldn't complete that response. Your work is saved." CRM unaffected. The turn is recorded `failed`, never retried behind the user's back | 120 s per call; Foundry round 90 s (was unbounded) | provider ladder in `complete()`; tool rounds never retried | stuck turns failed by the sweep after 15 min | `ai` |
| **Voice realtime** (DashScope Qwen) | important | inbound: forwards to the agent or plays a message if it fails **before** streaming; **after** streaming starts an inbound caller is handed to the agent (or told the agent will call back); an outbound AI call ends with a goodbye (known gap) | open 10 s, session 10 s, ping 20 s | none | — | (calls) |
| **Twilio / Plivo** (calls, numbers) | important | calling unavailable; commands fail or wait for reconciliation, **never duplicate** | Twilio HTTP **20 s** (was none); Plivo 5 s per operation | **one attempt per command attempt**: Plivo's hidden fallback re-POSTs are disabled; 5xx/timeouts mean **reconcile**, not retry | purchases recorded in `provider_purchases` before buying; one in flight | `outbound_side_effects` |
| **Telnyx** (SMS, hosted numbers, 10DLC) | important | texting unavailable; STOP is still recorded (503 → Telnyx redelivers) | **20 s** (was 60 s with 2 hidden retries) | SDK retries **off**; 5xx/timeouts reconcile | monotonic receipts; early receipts redelivered | `outbound_side_effects` |
| **SMTP** | important | emails wait in the outbox (`email_outbox`) or command reconciliation; nothing claims "sent" early | 20 s per operation, 40 s total | definite refusals retried (job backoff); **lost-connection-after-DATA is unknown, never resent** | — | `email_outbox` |
| **Stripe** | important | new subscriptions and the portal unavailable; **existing access unaffected** (read from local `subscriptions`) | 45 s checkout / 30 s portal, off the event loop (they blocked it before) | SDK 2 retries with a **stable idempotency key** per brokerage per 10 min | one plan per brokerage; webhook dedupe + ordering; early events redelivered | (billing) |
| **Google** (OAuth, Calendar, revoke) | important | calendar actions fail; the event id is deterministic, so retries cannot duplicate | 15 s total (aiohttp) | none at app level | — | (commands) |
| **MLS feeds** (Bridge, RESO) | important | listings served from the database **marked stale**; search says so instead of returning a silent empty result | 30 s per operation | 5 attempts on 429/500/502/503/504 (was 429/503 only), Retry-After honoured **up to 120 s** (was unbounded) | per-board guard records AUTH_ERROR / RATE_LIMITED; **one bad row no longer stalls a feed** (savepoint per row); **sync pages are live reads** (a 7-day page cache let an outage replay the last page and report READY) | `mls` |
| **Spaces / object storage** | important | uploads and media fail with a bounded error; CRM records unaffected | boto3 60/60 s | SDK | rows record keys only after a successful put | (media) |
| **RunPod** (GPU reconstruction) | important | 3D jobs wait or fail honestly; **source media always kept** | REST 60 s; job phases bounded | none for submission (no duplicate spend) | stuck `running` jobs time out after `ORACLE_RECON_STALL_HOURS` (6 h) | (spatial) |
| Geocoding / census / FEMA / AVM / Regrid | optional | enrichment missing; contact and client creation never wait on it | 10–45 s per operation | 5 attempts on transient failures | di_cache serves stale data; **an empty answer is believed for 1 h only** (it was the full TTL, up to 90 days) | — |
| DNS | core (indirect) | as the dependency it resolves | bounded by each client's timeout | as above | — | — |
| GitHub / GHCR | deploy | releases blocked, traffic unaffected | — | — | smoke checks separate core failures from provider degradation (`docs/runbooks/` §release) | — |

## TLS

Provider TLS fails closed everywhere:
- The one `verify=False` fallback (admin K8s calls) is removed.
- SMTP requires a valid certificate and STARTTLS.
- Production Postgres is `sslmode=require`.

Known: the RunPod pod SSH session does not pin a host key (`known_hosts=None`), because pods are ephemeral and RunPod does not publish keys. This is documented in `docs/runpod-pods-runbook.md`.

## Production configuration

Boot fails only for genuinely mandatory settings: secrets, JWT issuer and audience, the database, CORS, operator MFA when an operator account exists, and webhook secrets when their provider is enabled. A missing optional provider makes that feature unavailable, not the app. See `backend/config.py` and the H section of the audit in this mission's report.
