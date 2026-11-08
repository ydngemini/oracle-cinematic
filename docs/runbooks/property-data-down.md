# Runbook: property data (valuations, public records, market statistics) failing

Replaces the MLS runbook for live use: MLS is out of scope (D-2026-10-12-01). Ledger L19-03, L19-04, L19-05, L19-06.

**You see** one of these health components (`GET /api/admin/health/components`, operator dashboard) not HEALTHY:

| Component | Built from | States you can see |
|---|---|---|
| `valuation_providers` | RentCast / ATTOM calls (`backend/avm_client.py`), last outcome per provider in `provider_health` (0160) | NOT_CONFIGURED (no key), UNKNOWN (key set, no call recorded yet), AUTH_FAILED, RATE_LIMITED, DEGRADED (3+ failures in a row), HEALTHY |
| `property_harvesters` | `harvest_sources` (public-record harvesters) | NOT_CONFIGURED, DEGRADED (a source's circuit is open), STALE (overdue), HEALTHY |
| `market_data` | `state_market_stats` (market_research task) | NOT_CONFIGURED, STALE (3 missed refreshes), HEALTHY |

All three are `optional`: UNKNOWN does not degrade overall health; any other non-HEALTHY state does. In recovery mode, overdue syncs read NOT_CONFIGURED ("paused") instead of paging.

**What Neoh does:**
- A failed valuation or lookup is reported per source as `unavailable` / `timed_out` with a reason code (`backend/source_status.py`), never as "no value" or "no records" (audit A02).
- A rejected key (401/403) stops calls to that provider for 5 minutes (`_CREDENTIAL_COOLDOWN_SECONDS`, avm_client.py) so one dead key does not cause a storm of calls; the cooldown lifts itself.
- 429 and 5xx are not cooled down; the next request retries.
- Only successes are cached; a failure is never cached as a property fact.
- `provider_health` stores a provider name, timestamps, a fixed failure kind and a counter, never an address or error text.

**Do:**
1. **AUTH_FAILED:** rotate `RENTCAST_API_KEY` / `ATTOM_API_KEY` in the app spec secrets, redeploy. The next successful call resets `consecutive_failures` to 0.
2. **RATE_LIMITED:** check the provider plan's quota; nothing to restart. If it persists, lower call volume (bulk refreshes) before raising spend. Spend needs owner approval (CONTRACT C4).
3. **DEGRADED valuations:** check the provider status page; `SELECT provider, last_ok_at, last_failure_at, last_failure_kind, consecutive_failures FROM provider_health`.
4. **property_harvesters DEGRADED/STALE:** `SELECT source_key, circuit_state, circuit_open_until, failure_count, last_succeeded_at, health_status FROM harvest_sources WHERE enabled ORDER BY last_succeeded_at NULLS FIRST`. A source's portal changed shape or is down; the circuit re-closes after its cooldown. Fix the adapter in `backend/harvesters/` if the shape changed.
5. **market_data STALE:** check that the `market_research` periodic task runs (`scheduler` and `workers` HEALTHY, [background-work-stalled](background-work-stalled.md)).

**Done when:** the component reads HEALTHY. For valuations that means a recorded successful call: setting a key alone leaves it UNKNOWN by design.
