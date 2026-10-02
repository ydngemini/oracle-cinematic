# Runbook: MLS feed (Bridge / RESO) failing

**You see:** the `mls` component STALE, AUTH_FAILED or RATE_LIMITED, and search showing "Listing data may be out of date."

**What Neoh does:**
- Listings are served from the last good sync and marked stale; nothing claims to be fresh.
- Each RESO board is tracked separately, so a revoked token shows AUTH_ERROR for that board immediately.
- 429, 500, 502, 503 and 504 are retried 5 times with jitter. Retry-After is honoured up to 120 s.
- The cursor only advances after a committed batch, so the next run resumes without a full resync.
- A record the database refuses is skipped, counted (`notes.rejected_on_write`) and logged; the rest of the page is kept.

**Do:**
- **AUTH_FAILED:** rotate the token (`docs/mls-production-runbook.md` §Q). Do not loop retries.
- **RATE_LIMITED:** wait; lower page size or frequency if it persists.
- **Persistent rejections:** report the record ids to the board.
