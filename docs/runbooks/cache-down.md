# Runbook: cache down (Managed Valkey)

**You see:**
- alert `[Neoh] valkey UNAVAILABLE` (a direct 1 s ping fails);
- customers: **"Calling is temporarily unavailable."** Everything else works.

**What Neoh does by itself:**
- Rate limits fall back to Postgres; they still fail **closed** (503) if both stores are down.
- Neoh chat admission uses the database.
- Integration caches fall back to Postgres (`di_cache`).
- Live call state needs Valkey, so new calls are refused cleanly rather than half-placed.
- The client reconnects after a 10 s breaker once Valkey returns.

**Do:**
1. DigitalOcean → Databases → the Valkey cluster: status, memory and connection count, and whether maintenance is running.
2. If it is out of memory, look at eviction and key growth; the `di:*` integration cache is the usual bulk. Resize the cluster if needed.
3. Do **not** restart API replicas. They recover by themselves when Valkey answers.

**Verify:** `valkey` is HEALTHY, a test call connects, and the alert closes.
