# Provider failure matrix

One row per dependency failure. **Auto retry** means Neoh retries by itself.
**Reconcile** means an ambiguous outcome is resolved from the provider's own
records by `backend/reconciliation.py`: bounded, never a blind resend.
**Alert** names the `component_health` component whose state change pages
through `ops_alerts`. Drill evidence: `docs/resilience-drill-report.md`.

| Dependency / failure | Customer impact | Data impact | Auto retry | Breaker | Cached fallback | Reconcile | Alert | Runbook | Manual action |
|---|---|---|---|---|---|---|---|---|---|
| Postgres down | "Temporarily unavailable — your work is saved" (503) on every request; `/live` stays 200 | none: in-flight transactions roll back, nothing half-written is reported done | clients retry after `Retry-After`; job loops back off with jitter | readiness pulls replicas; rate limiter answers 503 | none (by design) | — | `database` (DB-down path emails directly) | database-down | wait / fail over per DR plan |
| Postgres slow / pool exhausted | requests fail at 10 s with 503, never hang | none | client | — | — | — | `database` (DEGRADED: pool saturated) | database-down | find the slow query (`pg_stat_statements`) |
| Postgres connection reset / failover | in-flight request 503; next request fine | rolled back, never false success | pool reconnects | — | — | — | `realtime_fanout` RECOVERING while LISTEN reconnects | database-down | none |
| Valkey down | CRM works; **calling unavailable**; limits on Postgres | none (no durable data in Valkey) | reconnect after 10 s breaker | yes | Postgres rate limits, Postgres di_cache | — | `valkey` | — | restart Valkey |
| AI model down / timeout | Neoh chat: "couldn't complete that response — your work is saved"; everything else works | user turn kept; assistant turn `failed`; **no retry behind the user** | provider ladder within the turn | — | — | stuck turns failed after 15 min | `ai` | ai-provider-down | check provider status / key |
| AI model 429 | same as above, sooner | same | ladder moves to the next provider; one turn is not retried 20 times | — | — | — | `ai` | ai-provider-down | raise quota |
| Voice realtime fails before the stream | caller forwarded to the agent or told to call back | call record finalized | — | — | — | — | — | voice-provider-down | — |
| Voice realtime fails mid-call | **inbound:** the caller is handed to the agent (`forward_when_ai_unavailable`) or told the agent will call back; **outbound AI call:** the client hears a goodbye (known gap) | call record finalized by the bridge; hand-off recorded | — | — | — | — | — | voice-provider-down | outbound: call the person back |
| Twilio/Plivo API down | "Calling is temporarily unavailable"; the command fails cleanly (definite) or waits for reconciliation | durable command row | definite 4xx: no; 5xx/timeout: **no — reconcile** | — | — | by call SID | `outbound_side_effects` | voice-provider-down | review "needs review" commands |
| Twilio accepts, response lost | none visible; the call happened | `reconciliation_required`, then `succeeded` once Twilio confirms | **never** | — | — | yes (call SID) | `outbound_side_effects` | voice-provider-down | — |
| Plivo 5xx | same | same | SDK fallback re-POSTs **disabled** | — | — | — | — | voice-provider-down | — |
| Number purchase timeout | "We could not confirm the number purchase" | `provider_purchases.unknown`; no second purchase | no | one purchase in flight per agent | — | adopts the bought number or concludes none | — | voice-provider-down | Plivo: check the account |
| Status/receipt callback delayed | status shows pending/sent | state only moves forward | provider redelivers | — | — | — | — | messaging-provider-down | — |
| Status/receipt callback duplicated or out of order | none | monotonic: a late "sent" cannot undo "delivered"; the first final call outcome wins | — | — | — | — | — | — | — |
| Telnyx API down | "Text not sent" (definite) or pending reconciliation | durable command row | 4xx: no; 5xx/timeout: reconcile; SDK retries **off** | — | — | by message id | `outbound_side_effects` | messaging-provider-down | — |
| Telnyx inbound STOP while our DB is failing | none | STOP not lost: webhook answers 503, Telnyx redelivers | provider | — | — | — | — | messaging-provider-down | — |
| SMTP down / refused | email waits in the outbox; agents see it queued | `email_outbox.queued` | yes (backoff) | — | — | sweep re-enqueues | `email_outbox` | email-provider-down | fix the relay |
| SMTP lost after DATA | the email may have arrived | outbox `sending` + `delivery_unknown`; command `reconciliation_required` | **never** | — | — | human (no provider lookup) | `email_outbox` / `outbound_side_effects` | email-provider-down | ask the recipient; resend deliberately |
| Invite email fails | owner sees "Email not delivered" + Resend | invitation exists | owner resends | — | — | — | — | email-provider-down | resend |
| Password reset email fails | requester sees the same neutral message (no enumeration) | token row exists, unused | user retries | — | — | — | log `oracle.auth` | email-provider-down | — |
| Google token revoked | calendar actions need reconnect | none | no | — | — | — | — | google-integration-down | agent reconnects |
| Google 429 | calendar action fails or waits | event id deterministic → no duplicate | job backoff (bounded) | — | — | — | — | google-integration-down | — |
| Stripe API down | **existing customers unaffected**; new checkout/portal "temporarily unavailable" | none | SDK 2 retries, stable idempotency key | — | local `subscriptions` | — | — | stripe-down | — |
| Stripe webhook late / early | none | an early `invoice.paid` is redelivered (503) until its subscription exists | Stripe redelivers (3 days) | — | — | — | — | stripe-down | — |
| Stripe event reordered / duplicated | none | older events cannot overwrite newer (`last_event_created`); duplicates are no-ops | — | — | — | — | — | stripe-down | same-second ties: see runbook |
| MLS provider down / 5xx | listings shown from the last sync, marked stale | cursor not advanced | 5 attempts with jitter | per-board guard | database rows | — | `mls` | mls-provider-down | — |
| MLS auth failure | listings stale; feed AUTH_FAILED | none | **no** | — | database rows | — | `mls` | mls-provider-down | rotate the token |
| MLS 429 | slower sync | cursor not advanced | Retry-After ≤ 120 s | — | — | — | `mls` RATE_LIMITED | mls-provider-down | — |
| MLS down while its cursor stands still | sync **fails** and the feed shows DEGRADED (it used to replay the cached page for up to 7 days and report READY) | cursor not advanced | 5 attempts | — | database rows only, never a cached page | — | `mls` | mls-provider-down | — |
| MLS malformed record | that listing is rejected and counted; the rest are kept | none | — | — | — | — | — | mls-provider-down | report to the board |
| Spaces down | media uploads/downloads fail with a bounded error | no row claims an object that does not exist | job retries | — | — | orphan audit | — | object-storage-down | — |
| RunPod down | 3D job waits or fails; original media kept | none | no auto-resubmit (no duplicate spend) | — | — | pods: name+age reaper | — | gpu-provider-down | resubmit deliberately |
| GPU job never finishes | after 6 h: "timed out — needs attention, your originals are kept" | none | no | — | — | stall watchdog | — | gpu-provider-down | inspect the pod logs |
| Geocoding / public data down | enrichment missing; creation never blocked | none | yes (bounded) | — | di_cache serves stale data | empty answers expire in 1 h | — | — | — |
| Worker crashed mid-job | jobs pause, then resume | lease expires (120 s, renewed while alive) → reclaimed | yes (bounded attempts) | — | — | stale `executing` commands → reconciliation | `workers`, `job_queue` | database-down | restart the worker |
| Job lane saturated by higher-priority work | lower-priority jobs (exports, erasures, sends) wait **≤ ORACLE_JOB_STARVATION_SECONDS + one job**, never indefinitely (strict priority starved them 5+ min in the first-10 drill) | none | — | aging: a job ready > 60 s is claimed oldest-first | — | — | `job_queue` (oldest ready) | — | raise `ORACLE_JOB_WORKERS` if waits persist |
| Scheduler stopped | periodic work pauses | none | next tick | — | — | — | `scheduler` STALE | database-down | restart the worker |
| Alert email fails | none | `ops_alerts.notify_error` recorded; log line written | next incident | — | — | — | log `oracle.ops.alert` | — | fix SMTP |
