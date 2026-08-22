# Migrations

Applied by `backend/run_migrations.py`. Read this before adding or renaming a file.

## How the runner behaves

- **Ordering** is `sorted(glob("*.sql"))` — plain lexicographic filename order.
  The zero-padded prefix is what makes that match numeric order, so keep the
  four digits.
- **The ledger is keyed on the full filename**, in `schema_migrations(filename)`.
  A file that has already been recorded is skipped.
- **Each file runs inside its own transaction**, and the runner takes a Postgres
  advisory lock first, so two instances booting together cannot interleave.
- **Any failure aborts the whole run** — it re-raises rather than continuing past
  a broken file. A duplicate-object error is never treated as "already applied",
  because later statements in the same file may still be missing.

## Numbering

Prefixes are unique but **not required to be contiguous**. `0040` is absent and
that is not a defect: the runner never computes a sequence, looks for gaps, or
derives the next number, so the gap has no effect on anything.

**Do not renumber existing files to close a gap.** The ledger stores filenames,
so a rename makes an already-applied migration look brand new and the runner
will try to apply it again on the next boot — against a database that already
has the objects.

For a new migration, take the next unused number above the current maximum.

## Renaming or editing an applied migration

Don't. Once a file has run anywhere, its name and contents are frozen — edit it
and the change silently never reaches any database that already recorded it.
Write a new migration that alters what the old one created.

## Requirements a migration may depend on

Extensions are created by earlier migrations, not assumed:

| Extension | Created in | Used for |
|---|---|---|
| `pgcrypto` | `0001_init_tenancy.sql` | `gen_random_uuid()` |
| `postgis` | `0013_state_compliance.sql` | flood-zone geometry |
| `earthdistance` (+ `cube`) | `0013_state_compliance.sql` | school-district radius search |
| `pg_trgm` | `0050_public_property_catalog.sql` | fuzzy property search |

`0013` guards both of its extensions, so a deployment without PostGIS still
migrates; the routes that need the spatial columns degrade instead of 500ing.

## Column-level grants

`0063` granted `UPDATE` column-by-column on `video_studio_jobs`. Under that
style of grant a column omitted from the list is unwritable, and the failure
surfaces at runtime rather than at migration time — `0068` exists partly to
repair one such omission. If you add a column to a table with column-level
grants, grant it explicitly.
