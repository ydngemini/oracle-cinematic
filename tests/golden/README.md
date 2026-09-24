# Golden E2E

Every other suite here tests a feature in isolation, mostly on fakes. This
walks one customer's journey end to end against the running stack — real HTTP
through the real middleware (auth, CSRF, RLS), real PostgreSQL — and answers a
different question: *is this a product, or a collection of parts that each
pass?*

```bash
tests/golden/run.sh
```

## What it covers

| Step | What it proves |
|---|---|
| 1–2 | Signup, setup screen, profile, recommended-next progression |
| 3–4 | Invitations issued; only digests stored; a double click yields one live invite |
| 5–6 | Unauthenticated preview, acceptance into the right tenant, **replay refused** |
| 7–8 | Roster; CRM contacts |
| 9 | MLS: unentitled → invisible, entitled → searchable, coverage honest |
| 10 | Property → people, with evidence and stated unknowns |
| 11 | Capabilities reflect reality; readiness stays BLOCKED honestly |
| 12 | **Tenant isolation** — roster, listings, listing-by-id, invitations |
| 13 | Role enforcement: an agent is not an owner |
| 14 | Durability across a fresh connection |
| 15 | Unauthenticated access refused everywhere |

## Why it exists

It found a bug on its first run that four unit-test suites missed.

`buyer_matching._as_number` handled `int`, `float` and `str`. Postgres returns
`numeric` columns as `Decimal`. So against the real database every budget and
bedroom comparison silently became "unknown" — a strong match degraded to
possible, and the API told the agent *"listing has no price"* about a listing
that plainly had one. Every unit test passed, because they all used Python
ints.

That is the whole argument for this file: the fakes agreed with the code, and
both were wrong about the database.

## Honest limits

- **No Redis/Valkey in the local stack**, so rate-limit and distributed-lock
  behaviour is reported as SKIP rather than silently passing. Set
  `GOLDEN_REDIS_URL` when a Redis is available.
- **No provider is contacted** — no mail, no calls, no texts, no payments. The
  invitation token is minted the way the API mints it, so the accept path is
  real while the email is not.
- Phone and messaging readiness are asserted as *not configured*, which is the
  truth for this stack; they are not exercised against Plivo or Telnyx.
