# Performance targets (internal)

**These are engineering targets, not customer SLAs.** Nothing here may be
quoted to a customer as a commitment. They exist so a load test can PASS or
FAIL (§69), and so a regression is visible before a customer feels it.

Each target is enforced as a k6 threshold in the scenario named in the last
column. Change a target here and in the scenario together.

## Why these numbers

A real-estate agent uses Neoh between calls, on a phone as often as a laptop.
The Work list and a client record are opened dozens of times a day; they must
feel instant (<300 ms perceived) or agents drift back to a spreadsheet.
Searches can take a beat longer because the person expects work to happen. A
Neoh reply is judged by *when it starts* (first token), not when it finishes.
A voice turn is judged against human conversational pause (~700–1,000 ms before
silence feels broken).

The measured single-user baseline (docs/capacity-plan.md §Baseline) is
40–220 ms on every interactive path, so these targets leave room for load
without being arbitrary.

## Interactive HTTP (p95, per request, at the tested concurrency)

| Path class | Examples | p95 target | Scenario |
|---|---|---|---|
| Item read | `GET /api/crm/clients/{id}` | < 300 ms | `read_load.js` `kind:item` |
| List read | Home command center, contacts | < 500 ms | `read_load.js` `kind:list` |
| Work list | `GET /api/crm/clients` (the heaviest list) | < 800 ms | `read_load.js` `kind:worklist` |
| Search | CRM `?q=`, MLS search page 1 | < 800 ms | `read_load.js`/`mls_search.js` `kind:search` |
| Deep page | MLS page 10/50 | < 1,200 ms | `mls_search.js` `kind:deep_page` |
| Listing detail + buyer match | `GET /api/mls/listings/{id}` | < 1,500 ms | `mls_search.js` `kind:detail` |
| Write | create/update client, note, task, stage | < 800 ms | `write_load.js` `kind:write` |
| Read-back after write | same | < 500 ms | `write_load.js` `kind:readback` |
| Login | `POST /auth/login` (scrypt) | < 1,000 ms | `auth_storm.js` |

Error rate: `http_req_failed < 1%` in every scenario. A 429 for a *legitimate*
user counts as a failure (`rate_limit.js` colleague, `auth_storm.js MODE=nat`).

## Real-time

| Measure | Target | Scenario |
|---|---|---|
| WebSocket connect → first server frame | p95 < 2,000 ms | `ws_load.js` |
| Cross-replica event delivery (NOTIFY) | p95 < 500 ms, **0 missed, 0 duplicated** | `ws_fanout.js` + `fanout_analyze.py` |
| Neoh chat admission (SEND → ACCEPTED) | p95 < 1,000 ms | `ai_chat.js` |
| Neoh first token (SEND → first delta) | p95 < 10 s *with the mock's 2.5–4 s model latency* | `ai_chat.js` |
| Neoh turn timeouts | 0 at the tested concurrency | `ai_chat.js` |
| Voice: caller audio end → first Neoh audio | Neoh's own share p95 < 150 ms (on top of the model's) | `voice_sim.py` |

First token and voice turn time are dominated by the model provider. The
target for Neoh is its **own** overhead, measured against the provider mock
with a fixed latency; the real provider's latency is recorded separately
(`ai_real_sample.py`) and is not something Neoh's infrastructure can buy down.

## Background

| Measure | Target |
|---|---|
| Durable job duplicate executions | **0** (hard) |
| Oldest queued job, first-10 workload | < 60 s |
| MLS ingestion while agents search | search thresholds still met |
| Provider webhooks (Telnyx/Plivo/Stripe) | p95 < 500 ms (answer webhook < 1 s), 0 rejected, 0 duplicate rows |

## Correctness (not negotiable at any load)

- `tenant_leaks == 0` — every scenario, every response.
- `write_mismatch == 0` — a 2xx write must read back.
- Duplicate job execution, duplicate SMS/call rows, duplicate subscriptions: 0.

Slow is acceptable; wrong is not (§77).
