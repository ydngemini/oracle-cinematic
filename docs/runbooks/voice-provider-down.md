# Runbook: voice provider (Twilio / Plivo / realtime voice model) down

**You see:**
- `outbound_side_effects` DEGRADED;
- call commands `failed` (definite) or `reconciliation_required`;
- agents seeing "Calling is temporarily unavailable" (also shown when Valkey is down: call state lives there);
- callers reporting that they heard a goodbye.

**What Neoh does:**
- **Placing a call.**
  - Twilio has a 20 s HTTP timeout.
  - Plivo makes exactly one attempt; its SDK used to re-POST the call to fallback hosts on a 5xx.
  - A 4xx (other than 408/409/429) is a definite failure, and the agent sees "Calling is temporarily unavailable".
  - A 5xx, a timeout or a lost response is **uncertain**. The command goes to `reconciliation_required` and is checked by call SID every 15 minutes, for up to 6 attempts. It is **never** redialled automatically.
- **Number purchase.** One purchase can be in flight per agent (`provider_purchases`). A timed-out purchase is recorded as `unknown`, and the sweep either adopts the Twilio number it finds or concludes that none was bought. No second number is bought blindly.
- **Realtime voice model.**
  - If it fails before the stream starts, the caller is forwarded to the agent or told to call back.
  - If it fails mid-call on an **inbound** call, the bridge redirects the caller to the agent (reason `ai_unavailable`, gated by the route's `forward_when_ai_unavailable`). With no eligible agent, the caller hears that the agent has their details and will call back.
  - If it fails mid-call on an **outbound** AI call, the client hears a goodbye and the call record is finalized. This is a **known gap**.
- **API restart.** A restart of the API replica that carries an active media stream ends that call. Calls are not migrated between replicas.
- **Status webhooks.** The webhook sets `ended_at` and `outcome` on the call session, and the write is idempotent (`COALESCE`).

**Do:**
1. Check Twilio / Plivo / DashScope status pages and the account balance. Also check `GET /api/admin/health/components` for `outbound_side_effects` and the count of open "needs review" commands.
2. After recovery, review `command_executions` in `reconciliation_required` (the "needs review" reason means 6 checks did not confirm). For example: `SELECT id, tenant_id, reconciliation_reason FROM command_executions WHERE state='reconciliation_required'`. Look the call SID up in the provider console, and confirm with the agent whether the call happened. Redial **only** after confirming that the call did not happen.
3. For any `provider_purchases` row still `unknown` on Plivo: check the Plivo console for numbers bought in that window, and adopt or release them. Plivo purchases are reconciled manually.
4. If the realtime model was down, call back the people whose outbound AI calls ended early, and inbound callers who were not connected to an agent: `live_call_sessions` where `outcome` is set and `ended_at` falls within the outage window.
5. Avoid restarting the API during business hours while calls are live. Drain first: check the active call sessions.

Related: [provider failure matrix](../provider-failure-matrix.md), [dependency map](../dependency-resilience-map.md).
