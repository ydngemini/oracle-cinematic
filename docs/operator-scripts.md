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

## `backfill_property_coordinates.py` — geocode public property records

**Why:** 8.59M `public_property_records` carry an address (93.8%) and an owner
(89.3%), but only **4.3%** carry latitude/longitude. Without a coordinate the
map view has nothing to plot, and `list_comparable_sales` / `estimate_arv` —
whose radius search migration 0076 added an index for — can only consider the
4% that happen to have one. The addresses were always there; nobody resolved
them.

The Census batch geocoder is keyless and free, and was already wired
(`data_integrations/census_geocoder.py`). This is a backfill, not a purchase.

```bash
# match rate only, writes nothing — always run this first
python backfill_property_coordinates.py --state DE --batches 1 --dry-run

# one state, then the rest
python backfill_property_coordinates.py --state DE
python backfill_property_coordinates.py
```

**Resumable.** Progress is the id cursor and the filter is `latitude IS NULL`,
so an interrupted run restarts where it stopped and a row that already has a
coordinate is never re-fetched. Safe to re-run.

**Unmatched rows are not failures.** A demolished parcel or a rural route has no
coordinate to find. The script exits non-zero only when a run resolves *nothing*,
which is what an endpoint outage looks like — `geocode_batch` never raises, it
degrades every row to unmatched, so "0 matched" is the only signal available.

**Courtesy.** Default batch is 5,000 against an API limit of 10,000, with a
one-second pause between calls. This is a free public endpoint doing real work;
raise the batch deliberately, not by default.

**Measured match rate:** 4,494 of 5,000 (89.9%) on Delaware, 2026-08-29.

**A preflight probes the geocoder before touching the database.** Without it an
unreachable endpoint is indistinguishable from a genuine zero-match run —
`geocode_batch` never raises, it degrades every row to unmatched, so an operator
would watch `0/5000 matched` scroll past for hours and conclude the addresses
were bad. Exit codes: `0` wrote something, `1` resolved nothing, `2` the
geocoder was unreachable and nothing was read or written.

Census both has outages and refuses some datacenter egress ranges, and the two
look identical from here (TCP connects, then the connection dies). If the
preflight fails, retry first; if it persists, run from a residential connection
or set `HTTPS_PROXY` to one. `--skip-preflight` forces a run anyway, since the
probe hits a different endpoint than the batch API.

**Census is intermittent, and the script is built around that.** The batch
endpoint can fail while the preflight endpoint answers. A page that resolves
NOTHING is retried in place — the cursor does not advance — because real data
matches ~80-90%, so a whole batch resolving nothing is far more likely to be the
endpoint than the addresses. After three attempts the run stops and exits 1,
leaving the remaining rows queued for the next run. That matters at this scale:
without it, one outage would walk the cursor through all 8.2M rows asking
nothing and report 0% as though the addresses were unmatchable.

Long runs should be detached (`docker compose run -d`), not held open by a shell
whose timeout will SIGTERM them. Resuming is free, but eight hours is not.
