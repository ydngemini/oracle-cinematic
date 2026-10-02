# Runbook: Google (OAuth / Calendar) failing

**Symptoms:** calendar commands failing, or "reconnect Google" for one agent.

**What Neoh does:**
- A revoked or expired grant makes that agent's calendar and mailbox actions fail clearly. The rest of Neoh is unaffected.
- Calendar events use a deterministic id per command, so a retry after a lost response conflicts instead of duplicating.
- 429s are retried by the job system with bounded backoff, never indefinitely.

**Do:**
1. Ask the agent to reconnect Google in Settings.
2. For tenant-wide failures, check the OAuth client in Google Cloud (consent screen and quota).
