# Runbook: AI model provider down, slow or rate-limited

**You see:**
- the `ai` component DEGRADED or UNAVAILABLE;
- agents seeing "Neoh couldn't complete that response. Your work is saved."

**What still works:** everything except Neoh's answers. CRM, Work, MLS search, calling and texting are unaffected.

**What Neoh does:**
- Each turn tries the provider ladder (`llm_gateway`) and gives up within its bounds (120 s per call, 90 s per Foundry round).
- A failed turn is recorded `failed` and is **not retried later**; the agent asks again.
- Tool actions only run from a complete response.
- Turns stuck `pending` are failed by the reconciliation sweep after 15 minutes, so they never lock a user out.

**Do:**
1. Check the provider's status page.
2. Check `ORACLE_FIREWORKS_API_KEY` validity (401 = rotate the key, see `docs/credential-rotation.md`).
3. For 429s, raise the quota or reduce concurrency (`ORACLE_INTERACTIVE_JOB_WORKERS`).
4. Nothing to clean up afterwards.
