#!/usr/bin/env bash
#
# Neoh — restore a logical backup into a CLEAN database, and never into a
# populated one by accident.
#
# The safety rule this script enforces above all others: it refuses to run
# against a database that already has Neoh tables in it, unless the operator
# says so explicitly. A restore is the one operation where "oops, wrong host"
# is unrecoverable, and the wrong host during an incident is the likeliest
# mistake there is — you are tired, there are two terminals open, and both
# prompts look the same.
#
# Usage:
#   ORACLE_DB_HOST=... ORACLE_DB_USER=... PGPASSWORD=... \
#     scripts/restore-postgres.sh <manifest.json> [--i-know-this-database-has-data]
#
# After it finishes, run scripts/verify-restore.sh with the same manifest.
# A restore is not done when pg_restore exits 0; it is done when the verifier
# passes.

set -uo pipefail

die() { printf '\n  ERROR: %s\n\n' "$*" >&2; exit 1; }

MANIFEST="${1:-}"
FORCE="${2:-}"
[ -n "$MANIFEST" ] || die "usage: restore-postgres.sh <manifest.json> [--i-know-this-database-has-data]"
[ -f "$MANIFEST" ] || die "no such manifest: $MANIFEST"

: "${ORACLE_DB_HOST:?ORACLE_DB_HOST is required — this script never guesses a target}"
: "${ORACLE_DB_NAME:?ORACLE_DB_NAME is required}"
: "${ORACLE_DB_USER:?ORACLE_DB_USER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

DB_PORT="${ORACLE_DB_PORT:-5432}"
export PGSSLMODE="${ORACLE_DB_SSLMODE:-prefer}"
DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
Q=(psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" -tAX)

# Read one scalar from the manifest.
#
# Deliberately dependency-light with a hard failure at the end. A DR script runs
# wherever you can get a shell during an incident — a stock postgres image has
# no python3, and `jq` is not guaranteed either. The first version of this used
# python3 unconditionally and, when it was absent, returned an EMPTY STRING for
# every field. The restore then reported "the manifest names  but it is not next
# to the manifest" and stopped, which at least stopped — but an empty checksum
# compared against an empty checksum would have MATCHED.
#
# So: try jq, try python3, fall back to sed over this manifest's flat one-key-
# per-line shape, and if a key that must exist comes back empty, abort.
_read_manifest_key() {
  local key="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$key" '.[$k] // empty' "$MANIFEST" 2>/dev/null && return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "
import json,sys
print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$MANIFEST" "$key" 2>/dev/null && return
  fi
  sed -n "s/^[[:space:]]*\"$key\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\)\"\{0,1\},\{0,1\}[[:space:]]*$/\1/p" \
    "$MANIFEST" | head -1
}

m() { _read_manifest_key "$1"; }

require() {
  local value
  value="$(_read_manifest_key "$1")"
  if [ -z "$value" ]; then
    die "could not read '$1' from $MANIFEST.

  This is a parse failure, not a missing field — and an unparsed manifest is
  dangerous rather than merely unhelpful, because an empty checksum would
  compare equal to another empty checksum and the integrity check would pass.
  Install jq or python3 in this environment and re-run."
  fi
  printf '%s' "$value"
}

DUMP_FILE="$(require dump_filename)"
DUMP_SHA="$(require dump_sha256)"
ROLES_FILE="$(m roles_filename)"
ROLES_SHA="$(m roles_sha256)"
EXTRACT_FILE="$(m authoritative_extract_filename)"
EXTRACT_SHA="$(m authoritative_extract_sha256)"
SCOPE="$(m scope)"
HEAD="$(m migration_head)"

DUMP_PATH="$DIR/$DUMP_FILE"
[ -f "$DUMP_PATH" ] || die "the manifest names $DUMP_FILE but it is not next to the manifest"

# ── The artifact is the one the manifest describes ─────────────────────────
#
# Checked before anything is touched. A corrupted or swapped dump discovered
# halfway through a restore has already destroyed the target.

echo "Checking artifact integrity ..."
ACTUAL_SHA="$(sha256sum "$DUMP_PATH" | cut -d' ' -f1)"
[ "$ACTUAL_SHA" = "$DUMP_SHA" ] \
  || die "checksum mismatch for $DUMP_FILE
  manifest: $DUMP_SHA
  actual:   $ACTUAL_SHA
  This is not the file the manifest describes. Do not restore it."
echo "  dump checksum matches"

if [ -n "$EXTRACT_FILE" ]; then
  EXTRACT_PATH="$DIR/$EXTRACT_FILE"
  [ -f "$EXTRACT_PATH" ] || die "the manifest names the authoritative-row extract
  $EXTRACT_FILE but it is not next to the manifest. Restoring without it would
  silently drop the rows that re-harvesting cannot reproduce — real deals."
  ACTUAL_EXTRACT_SHA="$(sha256sum "$EXTRACT_PATH" | cut -d' ' -f1)"
  [ "$ACTUAL_EXTRACT_SHA" = "$EXTRACT_SHA" ] \
    || die "checksum mismatch for the authoritative-row extract"
  echo "  extract checksum matches"
fi

if [ -n "$ROLES_FILE" ]; then
  ROLES_PATH="$DIR/$ROLES_FILE"
  [ -f "$ROLES_PATH" ] || die "the manifest names the roles file $ROLES_FILE but it
  is not next to the manifest. Restoring without it produces a database with
  correct data, correct RLS, and no role the application can connect as."
  [ "$(sha256sum "$ROLES_PATH" | cut -d' ' -f1)" = "$ROLES_SHA" ] \
    || die "checksum mismatch for the roles file"
  echo "  roles checksum matches"
fi

# ── Refuse to overwrite a populated database ───────────────────────────────

EXISTING="$("${Q[@]}" -c "
  SELECT count(*)::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public' AND c.relkind='r';" 2>/dev/null)" \
  || die "cannot reach $ORACLE_DB_HOST:$DB_PORT/$ORACLE_DB_NAME"

if [ "${EXISTING:-0}" -gt 0 ] && [ "$FORCE" != "--i-know-this-database-has-data" ]; then
  ROWS="$("${Q[@]}" -c "SELECT count(*)::text FROM tenants;" 2>/dev/null || echo '?')"
  die "$ORACLE_DB_NAME on $ORACLE_DB_HOST already contains $EXISTING tables (tenants: $ROWS).

  Refusing to restore over it. A restore into the wrong database is the one
  mistake with no undo, and during an incident the wrong prompt looks exactly
  like the right one.

  If this really is the throwaway target, re-run with:
      scripts/restore-postgres.sh '$MANIFEST' --i-know-this-database-has-data"
fi

# ── Restore ────────────────────────────────────────────────────────────────
#
# -j for parallel data load. Not --clean/--if-exists: this is meant for an
# empty target, and a flag that drops objects on the way in is exactly the
# thing that turns a mis-targeted restore into a destroyed database.

JOBS="${ORACLE_RESTORE_JOBS:-4}"
echo
echo "Restoring $DUMP_FILE (scope=$SCOPE, head=$HEAD) with $JOBS jobs ..."
START="$(date +%s)"

# NOT --no-privileges. The dump carries 350 ACL entries and the roles file has
# already created the roles they name. Passing --no-privileges here silently
# dropped all 566 grants: the drill got a database with every row, every
# policy, both roles — and an application role that could read nothing. The
# backup side had already been fixed; this side had not, which is the whole
# reason a drill exists rather than a review.
_pg_restore_section() {
  pg_restore \
    -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
    --no-owner \
    --section="$1" \
    ${2:+--jobs="$2"} \
    "$DUMP_PATH" 2>> "$DIR/.restore.log"
}

: > "$DIR/.restore.log"

# Roles first, always. Every GRANT in the dump names a role, and a GRANT to a
# role that does not exist is an error — 566 of them, in Neoh's case. The drill
# found a restored database that had every row and every policy and refused the
# application every read.
if [ -n "$ROLES_FILE" ]; then
  echo "  creating roles (no passwords — set them from the secret store after) ..."
  if ! psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
        -v ON_ERROR_STOP=1 -q -f "$DIR/$ROLES_FILE"; then
    die "could not create the roles. Restoring the dump now would fail every
  GRANT in it and leave a database the application cannot use."
  fi
fi

if [ -n "$EXTRACT_FILE" ]; then
  # A scoped restore has to interleave, because the extract holds PARENT rows.
  #
  # Found by drill: restoring the whole dump and then applying the extract
  # fails, because the child tables load during the dump while `leads` is still
  # empty, and 580 rows across property_media, reconstruction_jobs,
  # interaction_logs, client_portals and live_call_sessions are rejected by
  # their foreign keys. pg_restore reports those as errors and carries on, so
  # the restore "completes" having dropped them.
  #
  #   pre-data   tables and constraints
  #   extract    the leads those children point at
  #   data       everything else, whose FKs now resolve
  #   post-data  indexes, triggers
  echo "  scoped restore: pre-data -> authoritative rows -> data -> post-data"
  set +e
  _pg_restore_section pre-data
  RC=$?
  set -e

  echo "  applying the authoritative-row extract before the child tables ..."
  if ! psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
        -v ON_ERROR_STOP=1 -q -f "$DIR/$EXTRACT_FILE"; then
    die "the authoritative-row extract failed to apply. Every table that
  references a lead would now lose its rows on the data load. Stopping."
  fi

  set +e
  _pg_restore_section data "$JOBS"
  RC=$((RC + $?))
  _pg_restore_section post-data "$JOBS"
  RC=$((RC + $?))
  set -e
  EXTRACT_FILE=""   # already applied, in the only order that works
else
  set +e
  pg_restore \
    -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
    --no-owner \
    --jobs="$JOBS" \
    "$DUMP_PATH" 2> "$DIR/.restore.log"
  RC=$?
  set -e
fi

# pg_restore exits non-zero for warnings that are expected against a clean
# target (extensions the role may not create, comments on them). Those are
# reported, not swallowed — but they do not by themselves mean failure. The
# verifier is what decides, which is why it is not optional.
if [ "$RC" -ne 0 ]; then
  echo
  echo "  pg_restore exited $RC. Diagnostics:"
  grep -E "^pg_restore: error" "$DIR/.restore.log" | head -20 | sed 's/^/    /'
  echo
  echo "  This is not automatically a failure — some errors are expected against"
  echo "  a clean target. scripts/verify-restore.sh decides. Continuing."
fi

if [ -n "$EXTRACT_FILE" ]; then
  echo
  echo "Applying the authoritative-row extract ..."
  if ! psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" \
        -v ON_ERROR_STOP=1 -q -f "$DIR/$EXTRACT_FILE"; then
    die "the authoritative-row extract failed to apply. The schema is restored
  but the rows that re-harvesting cannot reproduce are NOT in it. Do not treat
  this database as recovered."
  fi
  echo "  applied"
fi

END="$(date +%s)"
echo
echo "  restore completed in $((END - START))s"
echo
if [ -n "$ROLES_FILE" ]; then
  echo "  The login role has NO PASSWORD. Before pointing the application here:"
  echo "      ALTER ROLE oracle_app_login PASSWORD '<from your secret store>';"
  echo
fi
echo "  NOT DONE YET. Run:"
echo "      scripts/verify-restore.sh $MANIFEST"
echo "  A restore is finished when the verifier passes, not when pg_restore exits."
echo
rm -f "$DIR/.restore.log"
