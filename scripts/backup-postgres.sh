#!/usr/bin/env bash
#
# Neoh — logical PostgreSQL backup with a verifiable manifest.
#
# WHY THIS EXISTS, given DigitalOcean already backs the cluster up daily:
#
#   1. DigitalOcean backups CANNOT BE DOWNLOADED. DO's own support doc says so
#      plainly. They restore only into a new DO cluster, in the same DO account.
#      They are not a copy of your data that you possess.
#   2. They are retained for SEVEN DAYS. Any incident discovered on day eight —
#      a slow data corruption, a bad migration nobody noticed, a deletion found
#      at month-end close — is outside the window, permanently.
#   3. They cannot restore into a laptop, a staging cluster, or another cloud.
#
# So DO's backups cover "the cluster died" and nothing else. This covers the
# rest. It is the only artifact Neoh actually holds.
#
# WHAT THIS DOES NOT COVER — read this before relying on it:
#
#   This dump contains CIPHERTEXT for 26 columns across 18 tables (leads,
#   clients, contracts, chat, calls, intake). Those are encrypted with a key
#   derived from ORACLE_ENCRYPTION_MASTER_KEY, which is an environment variable
#   and is DELIBERATELY NOT IN THE DATABASE and therefore NOT IN THIS BACKUP.
#
#   A restore without that master key does not fail. `oracle_decrypt()` returns
#   NULL when the key is absent, so the data comes back looking EMPTY rather
#   than looking encrypted. Backing this file up without separately escrowing
#   the master key produces a backup that restores cleanly and is worthless.
#
#   See docs/disaster-recovery-state-map.md, "Encryption key custody".
#
# Usage:
#   ORACLE_DB_HOST=... ORACLE_DB_USER=... PGPASSWORD=... \
#     scripts/backup-postgres.sh [output-dir]
#
# Credentials come from the environment only. Nothing is written to disk but
# the dump and its manifest, and neither contains a credential.

set -euo pipefail

OUT_DIR="${1:-./backups}"

# ── Refuse unsafe or missing configuration, loudly and before doing work ────

die() { printf '\n  ERROR: %s\n\n' "$*" >&2; exit 1; }

: "${ORACLE_DB_HOST:?ORACLE_DB_HOST is required — this script never guesses a target}"
: "${ORACLE_DB_NAME:?ORACLE_DB_NAME is required}"
: "${ORACLE_DB_USER:?ORACLE_DB_USER is required}"
: "${PGPASSWORD:?PGPASSWORD is required (export it; it is never echoed or stored)}"

DB_PORT="${ORACLE_DB_PORT:-5432}"

# TLS is required unless the target is unambiguously local. A managed database
# reached over the public internet without TLS would ship every encrypted
# column, and every unencrypted one, in clear text across the wire.
case "$ORACLE_DB_HOST" in
  localhost|127.0.0.1|::1|oracle-db-1|db) IS_LOCAL=1 ;;   # loopback / compose service
  *)                                      IS_LOCAL=0 ;;
esac

# The DEFAULT has to depend on the host, not just the requirement. Defaulting
# everything to `require` and then merely exempting localhost from the CHECK
# still sends sslmode=require to a local container that does not speak TLS, and
# the backup fails with a confusing error. Remote defaults to require; local
# defaults to prefer; an explicit setting always wins.
if [ -n "${ORACLE_DB_SSLMODE:-}" ]; then
  SSLMODE="$ORACLE_DB_SSLMODE"
elif [ "$IS_LOCAL" = "1" ]; then
  SSLMODE="prefer"
else
  SSLMODE="require"
fi

# Remote is remote regardless of what was asked for: a managed database reached
# over the public internet without TLS ships every column, encrypted or not, in
# clear text across the wire.
if [ "$IS_LOCAL" = "0" ]; then
  case "$SSLMODE" in
    disable|allow|prefer)
      die "ORACLE_DB_SSLMODE=$SSLMODE against remote host $ORACLE_DB_HOST.
  A remote backup must use TLS. Set ORACLE_DB_SSLMODE=require (or verify-full)."
      ;;
  esac
fi

command -v pg_dump >/dev/null || die "pg_dump not found on PATH"
command -v psql    >/dev/null || die "psql not found on PATH"
command -v sha256sum >/dev/null || die "sha256sum not found on PATH"

export PGSSLMODE="$SSLMODE"
PSQL=(psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" -tAX)

# ── Identify what we are about to capture ───────────────────────────────────

BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ENVIRONMENT="${ORACLE_ENV:-unknown}"

# ORACLE_GIT_SHA lets a caller that is not in the checkout — a container, a CI
# job, a cron host — still record WHICH CODE this schema belongs to. That field
# is how a restore later knows which application version matches the dump, so
# "unknown" is a real loss, not a cosmetic one.
if [ -n "${ORACLE_GIT_SHA:-}" ]; then
  GIT_SHA="$ORACLE_GIT_SHA"
  GIT_DIRTY="${ORACLE_GIT_DIRTY:-unknown}"
else
  GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  GIT_DIRTY="false"
  if ! git diff --quiet HEAD 2>/dev/null; then GIT_DIRTY="true"; fi
fi

echo "Connecting to $ORACLE_DB_HOST:$DB_PORT/$ORACLE_DB_NAME (sslmode=$SSLMODE) ..."
PG_VERSION="$("${PSQL[@]}" -c 'SHOW server_version;')" \
  || die "cannot reach the database — check host, credentials and TLS settings"

# The migration head is the single most important number in the manifest: a
# dump restored against application code expecting a different schema head is
# the failure mode that looks like it worked.
# `filename`, not `version` — this ledger keys on the migration file name
# (0111_mls_drop_unfed_columns.sql), and there is no version column. Getting
# that wrong does not error: the `|| echo unknown` below would swallow it and
# write "unknown" into the manifest, and the backup would look fine.
MIGRATION_HEAD="$("${PSQL[@]}" -c \
  "SELECT coalesce(max(filename), 'none') FROM schema_migrations;" 2>/dev/null || echo unknown)"
MIGRATION_COUNT="$("${PSQL[@]}" -c \
  "SELECT count(*)::text FROM schema_migrations;" 2>/dev/null || echo unknown)"

# Row counts for the tables whose survival defines a successful restore. These
# are the numbers verify-restore.sh compares against, which is what turns "the
# dump ran" into "the data is there".
count_of() { "${PSQL[@]}" -c "SELECT count(*)::text FROM $1;" 2>/dev/null || echo unknown; }
TENANT_COUNT="$(count_of tenants)"
USER_COUNT="$(count_of users)"
CLIENT_COUNT="$(count_of clients)"
LEAD_COUNT="$(count_of leads)"

# How much of the data is ciphertext we cannot read back without the master key.
CIPHERTEXT_COLUMNS="$("${PSQL[@]}" -c \
  "SELECT count(*)::text FROM information_schema.columns
    WHERE data_type='bytea' AND table_schema='public';" 2>/dev/null || echo unknown)"

# A manifest whose migration head is "unknown" cannot be verified against
# anything later, which defeats the point of having one.
if [ "$MIGRATION_HEAD" = "unknown" ] || [ "$MIGRATION_HEAD" = "none" ]; then
  die "could not read the migration head from schema_migrations.
  Without it this backup cannot be matched to a schema at restore time."
fi

# ── Scope ───────────────────────────────────────────────────────────────────
#
# ORACLE_BACKUP_SCOPE=full          everything (the default)
#                    =authoritative skip the DATA of tables that can be rebuilt
#
# This is not a size optimisation, it is a statement about what Neoh's data
# actually IS. Measured on the live database:
#
#   leads                     10,273,553 rows / 29 GB
#   public_property_records   10,651,309 rows / 17 GB
#   di_cache                                    426 MB
#   ...everything else                        ~600 MB
#
# Of those 10.27M leads, TWO are under contract and THREE have a client
# attached. The rest are `draft` rows produced by the public-records harvester,
# carrying machine-generated underwriting. public_property_records is harvested
# county/state open data. di_cache is a provider-response cache.
#
# So ~98% of this database is re-derivable, and protecting it with the same RPO
# as a signed contract buys nothing while making every restore hours long.
# `authoritative` scope skips their bulk data AND separately extracts the rows
# in them that a human or a tenant actually touched — because "mostly
# rebuildable" is not "entirely rebuildable", and losing those five rows would
# be losing real deals.
#
# Rebuilding the excluded data is a HARVEST, not a restore: it costs provider
# quota and hours. See docs/disaster-recovery-state-map.md for the RTO that
# implies. Never read "rebuildable" as "free".

SCOPE="${ORACLE_BACKUP_SCOPE:-full}"
case "$SCOPE" in
  full|authoritative) ;;
  *) die "ORACLE_BACKUP_SCOPE=$SCOPE is not valid; use 'full' or 'authoritative'" ;;
esac

REBUILDABLE_TABLES="public_property_records di_cache leads"

SCOPE_ARGS=()
if [ "$SCOPE" = "authoritative" ]; then
  for t in $REBUILDABLE_TABLES; do
    SCOPE_ARGS+=(--exclude-table-data="public.$t")
  done
fi

mkdir -p "$OUT_DIR"
DUMP_NAME="neoh-${ENVIRONMENT}-${BACKUP_ID}.dump"
DUMP_PATH="$OUT_DIR/$DUMP_NAME"
MANIFEST_PATH="$OUT_DIR/neoh-${ENVIRONMENT}-${BACKUP_ID}.manifest.json"

# ── Roles ───────────────────────────────────────────────────────────────────
#
# pg_dump does NOT dump roles. They are cluster-level objects, so a database
# restored into a fresh cluster has correct data, correct RLS — and no role the
# application can connect as. Found by the DR drill: the restored database had
# every row and every policy, 566 table grants pointing at `oracle_app`, and
# not one of the three roles those grants name. The application could not open
# a connection, let alone read a table.
#
# Captured WITHOUT passwords, deliberately. `pg_dumpall --roles-only` embeds the
# SCRAM verifier for every login role, which would put a credential in an
# artifact that gets copied into buckets and incident channels. The operator
# re-sets the login password from the secret store after restoring; the runbook
# says so and verify-restore.sh checks for it.

ROLES_NAME="neoh-${ENVIRONMENT}-${BACKUP_ID}.roles.sql"
ROLES_PATH="$OUT_DIR/$ROLES_NAME"

{
  echo "-- Neoh role definitions for backup $BACKUP_ID"
  echo "-- NO PASSWORDS. Set the login password from the secret store after applying:"
  echo "--   ALTER ROLE oracle_app_login PASSWORD '<from secret store>';"
  echo "-- Apply BEFORE restoring the dump, or every GRANT in it fails."
} > "$ROLES_PATH"

if ! "${PSQL[@]}" -c "
    SELECT format('DO \$\$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%L) THEN CREATE ROLE %I%s; END IF; END\$\$;',
                  r.rolname, r.rolname,
                  CASE WHEN r.rolcanlogin   THEN ' LOGIN'      ELSE '' END ||
                  CASE WHEN r.rolcreatedb   THEN ' CREATEDB'   ELSE '' END ||
                  CASE WHEN r.rolcreaterole THEN ' CREATEROLE' ELSE '' END)
      FROM pg_roles r
     WHERE r.rolname NOT LIKE 'pg\_%' AND r.rolname <> 'postgres'
     ORDER BY r.rolcanlogin, r.rolname;" >> "$ROLES_PATH" 2>/dev/null; then
  die "could not read the cluster's roles. A restore without them produces a
  database the application cannot connect to."
fi

"${PSQL[@]}" -c "
    SELECT format('GRANT %I TO %I;', g.rolname, m.rolname)
      FROM pg_auth_members am
      JOIN pg_roles g ON g.oid = am.roleid
      JOIN pg_roles m ON m.oid = am.member
     WHERE g.rolname NOT LIKE 'pg\_%' AND m.rolname NOT LIKE 'pg\_%';" >> "$ROLES_PATH" 2>/dev/null

ROLE_COUNT="$(grep -c 'CREATE ROLE' "$ROLES_PATH" || true)"
if [ "${ROLE_COUNT:-0}" -eq 0 ]; then
  die "captured zero roles. Every GRANT in the dump would fail on restore."
fi
ROLES_SHA256="$(sha256sum "$ROLES_PATH" | cut -d' ' -f1)"
echo "  roles     $ROLE_COUNT role(s) -> $ROLES_NAME (no passwords)"

# ── The dump ────────────────────────────────────────────────────────────────
#
# Custom format (-Fc), not plain SQL: it is compressed, and pg_restore can
# restore it selectively and in parallel (-j), which is the difference between
# a two-hour and a twenty-minute RTO on a large database.
#
# --no-owner, but privileges ARE included.
#
# This used to pass --no-privileges, reasoning that migration 0003 is the
# authority on grants. That reasoning was wrong in exactly one way, and the
# drill found it: migrations do not re-run on a restore. Dropping the ACLs
# meant dropping all 566 of them, and the restored database refused the
# application every read. The roles file above is applied first so the GRANTs
# in here have something to grant to.
#
# --no-owner stays: object ownership is cluster-specific, and the restoring
# superuser owning them is both harmless and what a fresh cluster can express.

echo "Dumping ..."
set +e
pg_dump \
  -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
  --format=custom \
  --compress=6 \
  --no-owner \
  ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} \
  --verbose \
  --file="$DUMP_PATH" 2> "$OUT_DIR/.dump.log"
DUMP_RC=$?
set -e

if [ "$DUMP_RC" -ne 0 ]; then
  echo "--- pg_dump output (last 20 lines) ---" >&2
  tail -20 "$OUT_DIR/.dump.log" >&2
  rm -f "$DUMP_PATH"
  die "pg_dump failed (exit $DUMP_RC). The partial dump has been deleted — a
  truncated backup that sits in the bucket looking like a backup is worse than
  no backup, because it is the one you reach for."
fi

# "Exited 0" is not the same as "complete". pg_restore --list reads the dump's
# own table of contents; if the archive is truncated it fails here rather than
# at 3 a.m. during the restore.
if ! pg_restore --list "$DUMP_PATH" > "$OUT_DIR/.toc.txt" 2>/dev/null; then
  rm -f "$DUMP_PATH"
  die "the dump is not a readable archive — pg_restore cannot list its contents"
fi

TOC_ENTRIES="$(wc -l < "$OUT_DIR/.toc.txt" | tr -d ' ')"
if [ "$TOC_ENTRIES" -lt 50 ]; then
  die "the dump contains only $TOC_ENTRIES archive entries, which is far too
  few for this schema. Refusing to record it as a valid backup."
fi

# ── The rows inside the rebuildable tables that are NOT rebuildable ─────────
#
# A lead the harvester produced can be produced again. A lead someone signed a
# contract on, or attached a client to, cannot — re-harvesting the parcel gives
# back the address, not the deal. Excluding the table's data wholesale would
# quietly drop those, and the restore would look complete.
#
# So `authoritative` scope pairs the dump with a plain-SQL extract of exactly
# those rows. It is small by construction: if it ever is not, that is a signal
# the classification above has drifted from reality and should be re-measured.

EXTRACT_NAME=""
EXTRACT_SHA256=""
EXTRACT_ROWS="0"
if [ "$SCOPE" = "authoritative" ]; then
  EXTRACT_NAME="neoh-${ENVIRONMENT}-${BACKUP_ID}.authoritative-rows.sql"
  EXTRACT_PATH="$OUT_DIR/$EXTRACT_NAME"

  # The predicate that defines "a human or a tenant touched this".
  # `dossier_status <> 'draft'` and not `IS NOT NULL`: the column is NOT NULL
  # with a 'draft' default, so the obvious null-check matches all 10.27M rows.
  # That mistake was made once already while measuring this.
  HUMAN_TOUCHED="seller_client_id IS NOT NULL
        OR contract_execution_date IS NOT NULL
        OR marketing_generated_at IS NOT NULL
        OR coalesce(dossier_status, 'draft') <> 'draft'"

  # ...plus every lead any RETAINED table still points at.
  #
  # Found by running this drill, which is the only reason it is here. Excluding
  # the leads data while keeping the thirteen tables that carry a lead_id
  # foreign key is incoherent: pg_restore loaded the children, their FKs had no
  # parent, and 567 property_media rows, 7 reconstruction jobs, 3 interaction
  # logs, 2 client portals and 1 live call session were REJECTED. The restore
  # reported "completed" and had silently dropped them.
  #
  # A scoped backup must be closed under its own foreign keys. Derived from
  # pg_constraint rather than a hand-written list of table names, so a child
  # table added next year is covered without anyone remembering to come back.
  REFERENCED_SQL="$("${PSQL[@]}" -c "
      SELECT string_agg(
               format('SELECT %I FROM %s WHERE %I IS NOT NULL',
                      a.attname, c.conrelid::regclass, a.attname),
               ' UNION ')
        FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
       WHERE c.contype='f' AND c.confrelid = 'public.leads'::regclass;")"

  if [ -z "$REFERENCED_SQL" ]; then
    TOUCHED="$HUMAN_TOUCHED"
    echo "  note      no table references leads; extract is human-touched rows only"
  else
    TOUCHED="($HUMAN_TOUCHED) OR id IN ($REFERENCED_SQL)"
  fi

  EXTRACT_ROWS="$("${PSQL[@]}" -c "SELECT count(*)::text FROM leads WHERE $TOUCHED;")"

  {
    echo "-- Neoh authoritative-row extract for backup $BACKUP_ID"
    echo "-- Rows inside rebuildable tables that re-harvesting would NOT restore."
    echo "-- Apply AFTER pg_restore of the matching dump."
    echo "BEGIN;"
  } > "$EXTRACT_PATH"

  if ! "${PSQL[@]}" -c "\\copy (SELECT * FROM leads WHERE $TOUCHED) TO STDOUT" \
        > "$OUT_DIR/.leads.tsv" 2>"$OUT_DIR/.extract.err"; then
    cat "$OUT_DIR/.extract.err" >&2
    rm -f "$DUMP_PATH" "$EXTRACT_PATH" "$OUT_DIR/.leads.tsv" "$OUT_DIR/.extract.err"
    die "could not extract the authoritative lead rows. Refusing to record a
  backup that silently drops real deals."
  fi

  {
    echo "COPY public.leads FROM STDIN;"
    cat "$OUT_DIR/.leads.tsv"
    echo "\\."
    echo "COMMIT;"
  } >> "$EXTRACT_PATH"
  rm -f "$OUT_DIR/.leads.tsv" "$OUT_DIR/.extract.err"

  EXTRACT_SHA256="$(sha256sum "$EXTRACT_PATH" | cut -d' ' -f1)"
  echo "  extract   $EXTRACT_ROWS authoritative lead row(s) -> $EXTRACT_NAME"
fi

# In `full` scope a restore must reproduce every lead. In `authoritative` scope
# the dump carries none of them and the companion extract carries exactly the
# rows re-harvesting could not reproduce — so that is the number to expect.
if [ "$SCOPE" = "authoritative" ]; then
  EXPECTED_LEADS="$EXTRACT_ROWS"
else
  EXPECTED_LEADS="$LEAD_COUNT"
fi

DUMP_BYTES="$(wc -c < "$DUMP_PATH" | tr -d ' ')"
DUMP_SHA256="$(sha256sum "$DUMP_PATH" | cut -d' ' -f1)"
rm -f "$OUT_DIR/.dump.log" "$OUT_DIR/.toc.txt"

# ── The manifest ────────────────────────────────────────────────────────────
#
# No passwords, no tokens, no PII, no access keys. Only the facts a restore
# needs in order to prove it worked.

cat > "$MANIFEST_PATH" <<JSON
{
  "backup_id": "$BACKUP_ID",
  "created_at": "$CREATED_AT",
  "environment": "$ENVIRONMENT",
  "git_sha": "$GIT_SHA",
  "git_dirty": "$GIT_DIRTY",
  "migration_head": "$MIGRATION_HEAD",
  "migration_count": "$MIGRATION_COUNT",
  "postgres_server_version": "$PG_VERSION",
  "database_name": "$ORACLE_DB_NAME",
  "dump_format": "custom",
  "dump_filename": "$DUMP_NAME",
  "dump_bytes": $DUMP_BYTES,
  "dump_sha256": "$DUMP_SHA256",
  "toc_entries": $TOC_ENTRIES,
  "__key_shape_note": "Every key here is FLAT and unique. Nested objects were tried and withdrawn: a restore runs wherever you can get a shell, a stock postgres image has neither jq nor python3, and the sed fallback cannot tell source_rows.leads from expected_after_restore.leads — it matched the first 'leads' line in the file and returned 10273553 where 9 was correct. It failed loudly that time. The same bug in the other direction passes silently.",
  "source_rows_tenants": "$TENANT_COUNT",
  "source_rows_users": "$USER_COUNT",
  "source_rows_clients": "$CLIENT_COUNT",
  "source_rows_leads": "$LEAD_COUNT",
  "source_rows_migrations": "$MIGRATION_COUNT",
  "__expected_note": "What a correct restore of THIS dump must contain. For a table whose data this scope excluded, that is not the source count — recording the source count would make every verification of a scoped backup fail, and the fix for that would have been to loosen the check until it stopped noticing anything.",
  "expected_tenants": "$TENANT_COUNT",
  "expected_users": "$USER_COUNT",
  "expected_clients": "$CLIENT_COUNT",
  "expected_leads": "$EXPECTED_LEADS",
  "expected_migrations": "$MIGRATION_COUNT",
  "scope": "$SCOPE",
  "excluded_table_data": "$( [ "$SCOPE" = "authoritative" ] && echo "$REBUILDABLE_TABLES" || echo "" )",
  "authoritative_extract_filename": "$EXTRACT_NAME",
  "authoritative_extract_sha256": "$EXTRACT_SHA256",
  "authoritative_extract_rows": "$EXTRACT_ROWS",
  "roles_filename": "$ROLES_NAME",
  "roles_sha256": "$ROLES_SHA256",
  "roles_count": "$ROLE_COUNT",
  "roles_note": "Roles carry NO passwords. Apply the roles file BEFORE the dump, then set the login password from the secret store.",
  "ciphertext_columns": "$CIPHERTEXT_COLUMNS",
  "requires_master_key": true,
  "master_key_note": "ORACLE_ENCRYPTION_MASTER_KEY is NOT in this dump by design. Without it, oracle_decrypt() returns NULL and the restored data reads as empty rather than as encrypted. Escrow the key separately."
}
JSON

echo
echo "  dump      $DUMP_PATH"
echo "  bytes     $DUMP_BYTES"
echo "  sha256    $DUMP_SHA256"
echo "  manifest  $MANIFEST_PATH"
echo "  head      migration $MIGRATION_HEAD on postgres $PG_VERSION"
echo
echo "  Verify with:  scripts/verify-restore.sh $MANIFEST_PATH"
echo
