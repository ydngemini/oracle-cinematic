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
# the dump, its roles file, its extract (authoritative scope) and its manifest
# — none contains a credential — plus two FIFOs in a private temp directory
# (TMPDIR) that carry the snapshot session and are removed on exit.

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

# Every number the manifest records about the DATA — the migration head, the
# row counts, the RLS and constraint inventory — is read further down, inside
# the snapshot the dump itself is taken from (REC-03). Reading them here, in
# separate sessions before pg_dump starts, is what this script used to do, and
# on a live database it described a different moment from the dump.

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

# EXCLUDED_TABLES is built in the SAME loop as the pg_dump flags, so the
# tables the manifest declares "not counted" can never drift from the tables
# whose data pg_dump actually leaves out.
SCOPE_ARGS=()
EXCLUDED_TABLES=""
EXCLUDED_SQL="ARRAY[]::text[]"
if [ "$SCOPE" = "authoritative" ]; then
  for t in $REBUILDABLE_TABLES; do
    SCOPE_ARGS+=(--exclude-table-data="public.$t")
    EXCLUDED_TABLES="${EXCLUDED_TABLES:+$EXCLUDED_TABLES }public.$t"
  done
  EXCLUDED_SQL="string_to_array('$EXCLUDED_TABLES', ' ')"
fi

mkdir -p "$OUT_DIR"
DUMP_NAME="neoh-${ENVIRONMENT}-${BACKUP_ID}.dump"
DUMP_PATH="$OUT_DIR/$DUMP_NAME"
MANIFEST_PATH="$OUT_DIR/neoh-${ENVIRONMENT}-${BACKUP_ID}.manifest.json"

# State for the snapshot session and the background pg_dump (both further
# down), and the one exit handler that cleans up after them. Installed here,
# before the first artifact is written, so every failure from now on leaves
# nothing behind that looks like a backup.
SNAP_DIR=""; SNAP_PID=""; DUMP_PID=""; SNAPSHOT_ID=""; BACKUP_DONE=0
SNAP_EOR="__neoh_snapshot_end_of_reply__"
# Per-line read timeout on the session: a guard against a hung connection, not
# a budget. One line arrives per counted table, so it bounds the slowest
# single table count, which on the 29 GB leads table is minutes, not hours.
SNAP_TIMEOUT="${ORACLE_BACKUP_SNAPSHOT_TIMEOUT:-21600}"
TAB="$(printf '\t')"

# Every exit path, including die and Ctrl-C: stop a running pg_dump, end the
# snapshot transaction, remove the FIFOs, and remove every artifact of a backup
# that did not finish — a dump without its manifest is a file that looks like
# a backup and cannot be verified.
on_exit() {
  local rc=$? f
  trap - EXIT
  set +e   # cleanup must run to the end; a failed kill of an exited pid is fine
  if [ -n "$DUMP_PID" ] && kill -0 "$DUMP_PID" 2>/dev/null; then
    kill "$DUMP_PID" 2>/dev/null
    wait "$DUMP_PID" 2>/dev/null
  fi
  if [ -n "$SNAP_PID" ]; then
    trap '' PIPE
    printf 'ROLLBACK;\n\\q\n' 2>/dev/null >&8
    exec 8>&-
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$SNAP_PID" 2>/dev/null || break
      sleep 0.2
    done
    kill "$SNAP_PID" 2>/dev/null
    wait "$SNAP_PID" 2>/dev/null
    exec 9<&-
  fi
  [ -n "$SNAP_DIR" ] && rm -rf "$SNAP_DIR"
  if [ "$BACKUP_DONE" != 1 ]; then
    for f in "${DUMP_PATH:-}" "${EXTRACT_PATH:-}" "${ROLES_PATH:-}" "${MANIFEST_PATH:-}" \
             "$OUT_DIR/.leads.tsv"; do
      [ -n "$f" ] && rm -f "$f"
    done
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

# ── One snapshot for the dump and for every number in the manifest ─────────
#
# REC-03 (2026-11-20). The manifest used to count four tables (tenants, users,
# clients, leads), each in its own psql session, BEFORE pg_dump started. On a
# live database those numbers described a different moment from the dump, and
# every other table was not counted at all, so a restore that lost rows
# anywhere else could still verify.
#
# Now one session opens a REPEATABLE READ, READ ONLY transaction and exports
# its snapshot with pg_export_snapshot(). pg_dump runs with --snapshot=<id>,
# and the same session, while the dump runs, counts every table whose data the
# dump carries. Both read exactly the same committed state, so the counts are
# what a correct restore of THIS file contains, whatever was being written
# meanwhile. pg_export_snapshot() needs no superuser (pg_dump -j relies on the
# same mechanism), so DigitalOcean's doadmin can run it.
#
# An exported snapshot can be imported only while the transaction that
# exported it is still open, so the session is held until pg_dump and the
# authoritative-row extract have both finished, and closed on every exit path
# (on_exit, above). The session talks over two FIFOs in a private temp dir —
# plain bash, no coproc, so it behaves the same on bash 3.2 and 5.
#
# row_security=off, as pg_dump sets it: if the backup role were subject to
# RLS, a count would error instead of silently counting only what the policy
# lets it see. statement/lock/idle timeouts are off for the same reason
# pg_dump turns them off: a count that is cut short is no count.

# Send SQL (and psql meta-commands) to the snapshot session and print its
# reply. `\echo` marks the end of the reply; psql runs with ON_ERROR_STOP, so
# any SQL error ends the session, the read sees EOF, and this returns non-zero.
snap_query() {
  local line rc
  trap '' PIPE
  printf '%s\n\\echo %s\n' "$1" "$SNAP_EOR" >&8
  rc=$?
  trap - PIPE
  [ "$rc" -eq 0 ] || return 1
  while IFS= read -r -t "$SNAP_TIMEOUT" line <&9; do
    if [ "$line" = "$SNAP_EOR" ]; then return 0; fi
    printf '%s\n' "$line"
  done
  return 1
}

snap_fail() {
  if [ -n "$SNAP_DIR" ] && [ -s "$SNAP_DIR/err" ]; then
    echo "--- snapshot session (psql stderr, last 20 lines) ---" >&2
    tail -20 "$SNAP_DIR/err" >&2
  fi
  die "$1"
}

# `key=value` lines from a reply.
reply_value() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }

# Run SQL from stdin in a NEW session that imports the backup's snapshot, so it
# reads exactly what the dump and the counts read.
in_snapshot() {
  {
    printf 'SET statement_timeout = 0;\nSET row_security = off;\n'
    printf 'BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;\n'
    printf "SET TRANSACTION SNAPSHOT '%s';\n" "$SNAPSHOT_ID"
    cat
    printf 'COMMIT;\n'
  } | "${PSQL[@]}" -q -v ON_ERROR_STOP=1 -f - 8>&- 9<&-
}

# What a backup has to agree with a restore about. verify-restore.sh carries
# byte-identical copies of these three definitions, and
# test_backup_restore_scripts.py fails if they ever differ: a backup and a
# verifier that count different things will disagree on a perfect restore, and
# the fix someone reaches for then is loosening the check.
#
# Data tables: every table whose rows pg_dump writes. Ordinary tables (a leaf
# partition is one) in every non-system schema, except temporary tables and
# tables an extension owns: pg_dump writes neither as table data, and an
# extension's configuration table (PostGIS spatial_ref_sys) may be dumped
# through a filter that count(*) cannot mirror. Counted with ONLY, because
# pg_dump's COPY reads a table's own rows and never its inheritance children.
DATA_TABLES_SQL="SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND c.relpersistence <> 't' AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
# Table constraints in public, NOT NULL excluded (PostgreSQL 18 records those
# in pg_constraint; 16 does not, so counting them would fail a cross-version
# restore that lost nothing). Extension-owned tables excluded, as above.
CONSTRAINTS_SQL="FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND k.contype <> 'n' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
# Indexes in public that pg_dump writes: it skips an invalid index (a failed
# CREATE INDEX CONCURRENTLY) unless its table is partitioned, and one that is
# not ready, so counting those would fail every restore of such a source.
INDEXES_SQL="FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = ic.relnamespace WHERE n.nspname = 'public' AND (i.indisvalid OR c.relkind = 'p') AND i.indisready AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"

SNAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/neoh-backup-snapshot.XXXXXX")" \
  || die "could not create a private temp directory for the snapshot session"
mkfifo "$SNAP_DIR/in" "$SNAP_DIR/out" \
  || die "could not create the snapshot session's FIFOs in $SNAP_DIR"
"${PSQL[@]}" -q -v ON_ERROR_STOP=1 < "$SNAP_DIR/in" > "$SNAP_DIR/out" 2> "$SNAP_DIR/err" &
SNAP_PID=$!
# Opening the write end unblocks psql's read of `in`; opening the read end
# unblocks its write of `out`. Same order on both sides, so no deadlock.
exec 8> "$SNAP_DIR/in"
exec 9< "$SNAP_DIR/out"

REPLY_TEXT="$(snap_query "SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET row_security = off;
BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SELECT 'snapshot=' || pg_export_snapshot();")" \
  || snap_fail "could not open a REPEATABLE READ transaction and export its snapshot."
SNAPSHOT_ID="$(reply_value "$REPLY_TEXT" snapshot)"
case "$SNAPSHOT_ID" in
  ''|*[!0-9A-Fa-f-]*) snap_fail "pg_export_snapshot() returned '$SNAPSHOT_ID', which is not a snapshot id." ;;
esac
echo "  snapshot  $SNAPSHOT_ID (the dump and every count below read this one state)"

# The migration head is the single most important number in the manifest: a
# dump restored against application code expecting a different schema head is
# the failure mode that looks like it worked.
# `filename`, not `version` — this ledger keys on the migration file name
# (0111_mls_drop_unfed_columns.sql), and there is no version column. Getting
# that wrong does not error: the failure would turn into "unknown" in the
# manifest, and the backup would look fine — hence the refusal below.
REPLY_TEXT="$(snap_query "SELECT 'migration_head=' || coalesce(max(filename), 'none') FROM schema_migrations;
SELECT 'migration_count=' || count(*) FROM schema_migrations;")" || REPLY_TEXT=""
MIGRATION_HEAD="$(reply_value "$REPLY_TEXT" migration_head)"
MIGRATION_COUNT="$(reply_value "$REPLY_TEXT" migration_count)"
MIGRATION_HEAD="${MIGRATION_HEAD:-unknown}"

# A manifest whose migration head is "unknown" cannot be verified against
# anything later, which defeats the point of having one.
if [ "$MIGRATION_HEAD" = "unknown" ] || [ "$MIGRATION_HEAD" = "none" ]; then
  [ -s "$SNAP_DIR/err" ] && tail -5 "$SNAP_DIR/err" >&2
  die "could not read the migration head from schema_migrations.
  Without it this backup cannot be matched to a schema at restore time."
fi

# The rest of the schema facts, from the same snapshot:
#   ciphertext_columns   how much of the data needs the master key to read
#   rls_*                REC-04: exact tenant-isolation counts, so the verifier
#                        can require ALL of them back, not "at least one"
#   constraints/indexes  a restore that silently lost a constraint or an
#                        index is not the database that was backed up
#   not_validated        FKs the SOURCE never validated (0122 adds
#                        lead_response_events_command_fk NOT VALID, and
#                        pg_dump preserves that). The verifier allows exactly
#                        these and fails on any other.
REPLY_TEXT="$(snap_query "SELECT 'ciphertext_columns=' || count(*) FROM information_schema.columns WHERE data_type='bytea' AND table_schema='public';
SELECT 'rls_tables=' || count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity;
SELECT 'rls_forced_tables=' || count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relforcerowsecurity;
SELECT 'rls_policies=' || count(*) FROM pg_policies WHERE schemaname='public';
SELECT 'constraints_public=' || count(*) $CONSTRAINTS_SQL;
SELECT 'foreign_keys_public=' || count(*) $CONSTRAINTS_SQL AND k.contype = 'f';
SELECT 'foreign_keys_not_validated=' || coalesce(string_agg(c.relname || '.' || k.conname, ' ' ORDER BY c.relname, k.conname), '') $CONSTRAINTS_SQL AND k.contype = 'f' AND NOT k.convalidated;
SELECT 'indexes_public=' || count(*) $INDEXES_SQL;")" \
  || snap_fail "could not read the schema inventory inside the backup's snapshot."
CIPHERTEXT_COLUMNS="$(reply_value "$REPLY_TEXT" ciphertext_columns)"
RLS_TABLES="$(reply_value "$REPLY_TEXT" rls_tables)"
RLS_FORCED="$(reply_value "$REPLY_TEXT" rls_forced_tables)"
RLS_POLICIES="$(reply_value "$REPLY_TEXT" rls_policies)"
CONSTRAINTS_PUBLIC="$(reply_value "$REPLY_TEXT" constraints_public)"
FOREIGN_KEYS_PUBLIC="$(reply_value "$REPLY_TEXT" foreign_keys_public)"
FOREIGN_KEYS_NOT_VALIDATED="$(reply_value "$REPLY_TEXT" foreign_keys_not_validated)"
INDEXES_PUBLIC="$(reply_value "$REPLY_TEXT" indexes_public)"
for v in "$CIPHERTEXT_COLUMNS" "$RLS_TABLES" "$RLS_FORCED" "$RLS_POLICIES" \
         "$CONSTRAINTS_PUBLIC" "$FOREIGN_KEYS_PUBLIC" "$INDEXES_PUBLIC"; do
  case "$v" in
    ''|*[!0-9]*) snap_fail "the schema inventory came back incomplete ('$v'). Refusing to write a manifest the verifier would compare against blanks." ;;
  esac
done

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
#
# --snapshot: the dump reads the session's exported snapshot instead of taking
# its own, so it and the counts below describe one and the same state. It runs
# in the background while that session counts; the session stays open until
# it has finished. `8>&- 9<&-`: pg_dump does not inherit the session's FIFOs,
# so nothing but this script can hold the session open.

echo "Dumping ..."
pg_dump \
  -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
  --snapshot="$SNAPSHOT_ID" \
  --format=custom \
  --compress=6 \
  --no-owner \
  ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} \
  --verbose \
  --file="$DUMP_PATH" 2> "$OUT_DIR/.dump.log" 8>&- 9<&- &
DUMP_PID=$!

# ── Count every table the dump carries — same snapshot, while it runs ───────
#
# Exact count(*), not pg_class.reltuples (an estimate) and not
# pg_stat_user_tables (eventually consistent): the verifier compares these
# for equality. One line per table, `"schema.table"<TAB>n`, the name already
# JSON-encoded by PostgreSQL so it goes into the manifest unaltered.
# The tables whose data this scope excludes are not counted and are listed in
# the manifest as excluded instead (EXCLUDED_SQL mirrors --exclude-table-data).
echo "Counting every table in the snapshot ..."
COUNT_LINES="$(snap_query "SELECT format('SELECT %L || E''\\t'' || count(*) FROM ONLY %I.%I', to_json(t.nspname || '.' || t.relname)::text, t.nspname, t.relname)
  FROM ($DATA_TABLES_SQL) t
 WHERE (t.nspname || '.' || t.relname) <> ALL ($EXCLUDED_SQL)
 ORDER BY t.nspname, t.relname
\\gexec")" \
  || snap_fail "could not count every table inside the backup's snapshot. A manifest
  without exact per-table counts cannot prove a restore complete."

TABLE_COUNTS_JSON=""
TABLE_COUNT_TABLES=0
TABLE_COUNT_ROWS=0
TENANT_COUNT=unknown; USER_COUNT=unknown; CLIENT_COUNT=unknown; LEAD_COUNT=unknown
while IFS= read -r line; do
  [ -n "$line" ] || continue
  key="${line%%"$TAB"*}"
  n="${line#*"$TAB"}"
  case "$key" in
    \"*\") ;;
    *) snap_fail "unexpected line from the table count: '$line'" ;;
  esac
  case "$n" in
    ''|*[!0-9]*) snap_fail "unexpected count for $key: '$n'" ;;
  esac
  TABLE_COUNTS_JSON="${TABLE_COUNTS_JSON:+$TABLE_COUNTS_JSON,
}    $key: $n"
  TABLE_COUNT_TABLES=$((TABLE_COUNT_TABLES + 1))
  TABLE_COUNT_ROWS=$((TABLE_COUNT_ROWS + n))
  case "$key" in
    '"public.tenants"') TENANT_COUNT="$n" ;;
    '"public.users"')   USER_COUNT="$n" ;;
    '"public.clients"') CLIENT_COUNT="$n" ;;
    '"public.leads"')   LEAD_COUNT="$n" ;;
  esac
done <<EOF
$COUNT_LINES
EOF
[ "$TABLE_COUNT_TABLES" -gt 0 ] || snap_fail "the snapshot holds no data table at all."

# `source_rows_leads` keeps its old meaning — the SOURCE's lead count — even
# when this scope excludes the leads data and so leaves it out of table_counts.
if [ "$LEAD_COUNT" = unknown ] && [ "$SCOPE" = "authoritative" ]; then
  REPLY_TEXT="$(snap_query "SELECT 'source_leads=' || count(*) FROM leads;")" \
    || snap_fail "could not count the source's leads inside the snapshot."
  LEAD_COUNT="$(reply_value "$REPLY_TEXT" source_leads)"
fi
echo "  counted   $TABLE_COUNT_TABLES table(s), $TABLE_COUNT_ROWS row(s)${EXCLUDED_TABLES:+; data excluded by scope: $EXCLUDED_TABLES}"

set +e
wait "$DUMP_PID"
DUMP_RC=$?
set -e
DUMP_PID=""

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
  #
  # Every query here runs inside the backup's snapshot (REC-03): a child row in
  # the dump points at a lead as of the snapshot, so the parent has to be read
  # as of the snapshot too. Read later, a lead deleted in between would be
  # missing from the extract while its child sat in the dump.
  REPLY_TEXT="$(snap_query "SELECT 'referenced_sql=' || coalesce(string_agg(
               format('SELECT %I FROM %s WHERE %I IS NOT NULL',
                      a.attname, c.conrelid::regclass, a.attname),
               ' UNION '), '')
        FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
       WHERE c.contype='f' AND c.confrelid = 'public.leads'::regclass;")" \
    || snap_fail "could not read which tables reference leads."
  REFERENCED_SQL="$(reply_value "$REPLY_TEXT" referenced_sql)"

  if [ -z "$REFERENCED_SQL" ]; then
    TOUCHED="$HUMAN_TOUCHED"
    echo "  note      no table references leads; extract is human-touched rows only"
  else
    TOUCHED="($HUMAN_TOUCHED) OR id IN ($REFERENCED_SQL)"
  fi

  REPLY_TEXT="$(snap_query "SELECT 'extract_rows=' || count(*) FROM leads WHERE $TOUCHED;")" \
    || snap_fail "could not count the authoritative lead rows inside the snapshot."
  EXTRACT_ROWS="$(reply_value "$REPLY_TEXT" extract_rows)"

  {
    echo "-- Neoh authoritative-row extract for backup $BACKUP_ID"
    echo "-- Rows inside rebuildable tables that re-harvesting would NOT restore."
    echo "-- Apply AFTER pg_restore of the matching dump."
    echo "BEGIN;"
  } > "$EXTRACT_PATH"

  # A second session that imports the same snapshot. Server-side COPY TO
  # STDOUT needs no privilege beyond SELECT, and unlike \copy it may span lines.
  if ! printf 'COPY (SELECT * FROM leads WHERE %s) TO STDOUT;\n' "$TOUCHED" | in_snapshot \
        > "$OUT_DIR/.leads.tsv" 2>"$OUT_DIR/.extract.err"; then
    cat "$OUT_DIR/.extract.err" >&2
    rm -f "$DUMP_PATH" "$EXTRACT_PATH" "$OUT_DIR/.leads.tsv" "$OUT_DIR/.extract.err"
    die "could not extract the authoritative lead rows. Refusing to record a
  backup that silently drops real deals."
  fi
  # COPY's text format is one line per row (an embedded newline is escaped),
  # so the extract must hold exactly the rows the snapshot counted.
  EXTRACTED="$(wc -l < "$OUT_DIR/.leads.tsv" | tr -d ' ')"
  if [ "$EXTRACTED" != "$EXTRACT_ROWS" ]; then
    rm -f "$DUMP_PATH" "$EXTRACT_PATH" "$OUT_DIR/.leads.tsv" "$OUT_DIR/.extract.err"
    die "the authoritative extract holds $EXTRACTED row(s), but the snapshot
  counted $EXTRACT_ROWS. Refusing to record a backup whose extract and manifest disagree."
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

# ── Release the snapshot ────────────────────────────────────────────────────
#
# pg_dump and the extract are done with it. Holding a snapshot open stops
# VACUUM from removing rows that died after it, so it is released as soon as
# nothing needs it, and before anything slow (the checksum above is the last
# read of the dump).
snap_query "COMMIT;" >/dev/null || snap_fail "the snapshot transaction did not end cleanly."
printf '\\q\n' >&8
exec 8>&-
set +e
wait "$SNAP_PID"
SNAP_RC=$?
set -e
exec 9<&-
SNAP_PID=""
[ "$SNAP_RC" -eq 0 ] || die "the snapshot session exited $SNAP_RC after COMMIT."

# ── The manifest ────────────────────────────────────────────────────────────
#
# No passwords, no tokens, no PII, no access keys. Only the facts a restore
# needs in order to prove it worked.
#
# manifest_version 2 (REC-03, 2026-11-20) adds, all read inside the dump's own
# snapshot: table_counts (exact rows of every table whose data is in the dump),
# excluded_tables, and the constraint/index inventory. Every version-1 field
# is still written with its old meaning, so older tooling keeps working. A
# manifest without table_counts is version 1, and verify-restore.sh says
# plainly that it could not reconcile such a restore exhaustively.

cat > "$MANIFEST_PATH" <<JSON
{
  "manifest_version": 2,
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
  "__key_shape_note": "Every scalar key here is FLAT and unique. Nested objects were tried and withdrawn: a restore runs wherever you can get a shell, a stock postgres image has neither jq nor python3, and the sed fallback cannot tell source_rows.leads from expected_after_restore.leads — it matched the first 'leads' line in the file and returned 10273553 where 9 was correct. It failed loudly that time. The same bug in the other direction passes silently. The one exception is table_counts, last in the file: its keys are schema-qualified table names, which always contain a dot and so can never match a scalar key (none has one); it is written one entry per line; nothing reads it with the scalar reader; and verify-restore.sh fails unless the entries it read equal table_counts_tables.",
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
  "rls_tables": "$RLS_TABLES",
  "rls_forced_tables": "$RLS_FORCED",
  "rls_policies": "$RLS_POLICIES",
  "requires_master_key": true,
  "master_key_note": "ORACLE_ENCRYPTION_MASTER_KEY is NOT in this dump by design. Without it, oracle_decrypt() returns NULL and the restored data reads as empty rather than as encrypted. Escrow the key separately.",
  "snapshot_id": "$SNAPSHOT_ID",
  "__snapshot_note": "pg_dump ran with --snapshot=snapshot_id, exported by the session that read every count below, so the counts and the dump describe one committed state even while the source was being written to. The id is meaningless once that transaction ended; it is recorded only as evidence of which snapshot was shared.",
  "constraints_public": "$CONSTRAINTS_PUBLIC",
  "foreign_keys_public": "$FOREIGN_KEYS_PUBLIC",
  "foreign_keys_not_validated": "$FOREIGN_KEYS_NOT_VALIDATED",
  "indexes_public": "$INDEXES_PUBLIC",
  "excluded_tables": "$EXCLUDED_TABLES",
  "__table_counts_note": "Exact count(*) of every table whose data is in this dump, as of the snapshot. A correct restore must reproduce every one exactly. Tables in excluded_tables had their data left out by this scope and are deliberately not counted here (leads comes back through the authoritative extract and is checked against expected_leads).",
  "table_counts_tables": $TABLE_COUNT_TABLES,
  "table_counts_rows": $TABLE_COUNT_ROWS,
  "table_counts": {
$TABLE_COUNTS_JSON
  }
}
JSON
BACKUP_DONE=1

echo
echo "  dump      $DUMP_PATH"
echo "  bytes     $DUMP_BYTES"
echo "  sha256    $DUMP_SHA256"
echo "  manifest  $MANIFEST_PATH"
echo "  head      migration $MIGRATION_HEAD on postgres $PG_VERSION"
echo "  counts    $TABLE_COUNT_TABLES table(s), exact, from snapshot $SNAPSHOT_ID"
echo
echo "  Verify with:  scripts/verify-restore.sh $MANIFEST_PATH"
echo
