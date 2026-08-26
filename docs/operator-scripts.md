# Operator scripts — one-shot jobs that are not part of any deploy

Scripts here are run by hand, deliberately, usually once. Nothing imports them
and no deploy invokes them, which means they are invisible unless written down —
this file is that record.

Migrations are **not** in this category: they run through
`backend/run_migrations.py`, which `scripts/dev-start.sh` invokes automatically.
See `docs/production-blockers.md` for operator actions that need vendor or
billing access rather than a command.

---

## `backend/backfill_contact_search.py` — make existing contacts searchable by name

### When this is needed

Migration `0059_contact_search_and_assignment_integrity.sql` added
`agent_contacts.name_search_tokens` with a `DEFAULT ARRAY[]::text[]`. Every row
that existed before that migration therefore has **zero tokens**.

Name search (`contacts_api.py`) matches on `name_search_tokens @> $4`, so a
contact with no tokens is **permanently invisible to name search**. It is not a
degraded result — the contact simply never appears.

The live write paths tokenize on create and update only. Nothing else in the
codebase repairs an untokenized row. **This script is the only remedy.**

This particularly affects contacts seeded by
`0054_agent_contacts_and_intake.sql`, which populates `agent_contacts` from
`clients` *metadata only* — no plaintext PII is copied — leaving
`pii_ciphertext = NULL`.

### Check whether you need it

```sql
SELECT count(*) FROM agent_contacts
 WHERE deleted_at IS NULL AND cardinality(name_search_tokens) = 0;
```

`0` means the cutover is complete for that database. Run this against
**production**, not a dev database — a dev database with a handful of seeded
contacts tells you nothing about production.

### Running it

Requires `ORACLE_ENCRYPTION_MASTER_KEY` (it decrypts `pii_ciphertext` to derive
the tenant-keyed blind index) plus the usual `ORACLE_DB_*` connection vars.

```bash
docker compose run --rm --no-deps \
  -e ORACLE_ENCRYPTION_MASTER_KEY="$ORACLE_ENCRYPTION_MASTER_KEY" \
  backend python /app/backfill_contact_search.py
```

Optional: `ORACLE_CONTACT_SEARCH_BACKFILL_BATCH` (default `250`, range 1–2000).

### What to expect

Restart-safe — it selects only rows with zero tokens and takes
`FOR UPDATE ... SKIP LOCKED`, so it can be re-run and can run beside live
traffic. It logs counts only, never names.

| Exit | Meaning |
|---|---|
| `0` | Every contact that could be indexed was indexed. |
| `1` | Indexing completed, but some contacts have **no canonical name** and were skipped. They stay invisible to name search until a name is supplied. The count is on stderr. |
| `2` | Refused to start — missing `ORACLE_ENCRYPTION_MASTER_KEY`, or an out-of-range batch size. |

Exit `1` is a report, not a failure: the work that could be done has been
committed. A contact reaches that state by having neither encrypted PII nor a
legacy `clients.full_name`, which is what the 0054 seed can produce.

> Until 2026-08-26 the first such contact raised and aborted the entire run —
> across every remaining tenant — so a single nameless row could prevent the
> whole estate from ever becoming searchable. Skipped rows are now excluded from
> subsequent batches, which is also what keeps the loop terminating: a skipped
> row still matches `cardinality(...) = 0` and would otherwise be re-selected
> forever.
