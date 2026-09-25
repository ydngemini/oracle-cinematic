#!/usr/bin/env bash
#
# Neoh — prove a restored database is actually usable.
#
# "pg_restore exited 0" is not a successful restore. These are the ways a
# restore of THIS system passes that test and is still broken:
#
#   * The schema came back at a different migration head than the application
#     expects. Everything appears fine until the first query hits a column that
#     is not there.
#   * Row-level security did not come back. Every tenant can read every other
#     tenant's clients, and nothing errors — the data simply flows.
#   * The encryption master key is missing. `oracle_decrypt()` returns NULL
#     rather than raising, so 26 ciphertext columns across 18 tables come back
#     looking EMPTY rather than looking encrypted. This is the quietest failure
#     in the whole system: a restore that reports success and has silently lost
#     every lead payload, contract body, call transcript and chat message.
#   * Functions exist but carry no grants, so the app role cannot execute them.
#     Migration 0003 revokes PUBLIC from every function; a restore that misses
#     the re-grant leaves chat bodies and radius search dead.
#
# Each check below exists because of one of those.
#
# Usage:
#   ORACLE_DB_HOST=... ORACLE_DB_USER=... PGPASSWORD=... \
#     scripts/verify-restore.sh <manifest.json>
#
# Exit 0 = the restore is trustworthy. Non-zero = it is not, and the output
# says which specific guarantee failed.

set -uo pipefail

MANIFEST="${1:-}"
[ -n "$MANIFEST" ] || { echo "usage: verify-restore.sh <manifest.json>" >&2; exit 2; }
[ -f "$MANIFEST" ] || { echo "no such manifest: $MANIFEST" >&2; exit 2; }

: "${ORACLE_DB_HOST:?ORACLE_DB_HOST is required}"
: "${ORACLE_DB_NAME:?ORACLE_DB_NAME is required}"
: "${ORACLE_DB_USER:?ORACLE_DB_USER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

DB_PORT="${ORACLE_DB_PORT:-5432}"
export PGSSLMODE="${ORACLE_DB_SSLMODE:-prefer}"
Q=(psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_USER" -d "$ORACLE_DB_NAME" -tAX)

PASS=0; FAIL=0
ok()   { printf '  PASS  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }

die() { printf '\n  ERROR: %s\n\n' "$*" >&2; exit 2; }

# Read a scalar, possibly nested one level (expected_after_restore.tenants).
# Same reasoning as restore-postgres.sh: a stock postgres image has no python3,
# and a parser that silently returns "" turns every comparison into a
# comparison of two empty strings, which passes.
m() {
  local key="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$key" '.[$k] // empty' "$MANIFEST" 2>/dev/null && return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split('.'):
    d=d.get(k) if isinstance(d,dict) else None
    if d is None: print(''); raise SystemExit
print(d)" "$MANIFEST" "$key" 2>/dev/null && return
  fi
  sed -n "s/^[[:space:]]*\"${key}\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\)\"\{0,1\},\{0,1\}[[:space:]]*$/\1/p" \
    "$MANIFEST" | head -1
}

# The one field every check depends on. If this cannot be read, the manifest is
# unparsed and every comparison below would be "" against "" — green, and
# meaningless.
if [ -z "$(m migration_head)" ]; then
  die "could not read 'migration_head' from $MANIFEST — the manifest is not
  parseable in this environment. Every check below would compare empty strings
  and report success. Install jq or python3 and re-run."
fi

echo
echo "Verifying restore against $(basename "$MANIFEST")"
echo "  target  $ORACLE_DB_HOST:$DB_PORT/$ORACLE_DB_NAME"
echo

# ── 1. The schema is the version the manifest says ─────────────────────────

echo "1. Schema version"
WANT_HEAD="$(m migration_head)"
WANT_COUNT="$(m migration_count)"
GOT_HEAD="$("${Q[@]}" -c "SELECT coalesce(max(filename),'none') FROM schema_migrations;" 2>/dev/null)"
GOT_COUNT="$("${Q[@]}" -c "SELECT count(*)::text FROM schema_migrations;" 2>/dev/null)"

[ "$GOT_HEAD" = "$WANT_HEAD" ] \
  && ok "migration head is $GOT_HEAD" \
  || bad "migration head is $GOT_HEAD, manifest says $WANT_HEAD"
[ "$GOT_COUNT" = "$WANT_COUNT" ] \
  && ok "$GOT_COUNT migrations recorded" \
  || bad "$GOT_COUNT migrations recorded, manifest says $WANT_COUNT"

# ── 2. The rows that define a successful restore ───────────────────────────

echo
echo "2. Row counts"
for table in tenants users clients leads; do
  want="$(m "expected_$table")"
  [ -n "$want" ] || continue
  got="$("${Q[@]}" -c "SELECT count(*)::text FROM $table;" 2>/dev/null)"
  [ "$got" = "$want" ] \
    && ok "$table: $got" \
    || bad "$table: $got, expected $want"
done

# ── 3. Tenant isolation survived ───────────────────────────────────────────
#
# The single most dangerous silent failure. If RLS did not come back, every
# query still works — it just returns everyone's data.

echo
echo "3. Tenant isolation"
RLS_TABLES="$("${Q[@]}" -c "
  SELECT count(*)::text FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity;" 2>/dev/null)"
FORCED="$("${Q[@]}" -c "
  SELECT count(*)::text FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname='public' AND c.relkind='r' AND c.relforcerowsecurity;" 2>/dev/null)"
POLICIES="$("${Q[@]}" -c "SELECT count(*)::text FROM pg_policies WHERE schemaname='public';" 2>/dev/null)"

[ "${RLS_TABLES:-0}" -gt 0 ] \
  && ok "row-level security enabled on $RLS_TABLES tables" \
  || bad "NO table has row-level security — every tenant can read every other tenant"
[ "${FORCED:-0}" -gt 0 ] \
  && ok "FORCE row-level security on $FORCED tables" \
  || bad "no table FORCEs RLS — the owner role bypasses every policy"
[ "${POLICIES:-0}" -gt 0 ] \
  && ok "$POLICIES policies present" \
  || bad "no RLS policies present"

# The functions the policies are written in terms of. A policy referencing a
# missing function does not fail closed — it fails to create, and the table is
# then unprotected.
for fn in app_current_tenant app_is_platform_admin app_current_role; do
  if [ "$("${Q[@]}" -c "SELECT count(*)::text FROM pg_proc WHERE proname='$fn';" 2>/dev/null)" != "0" ]; then
    ok "$fn() exists"
  else
    bad "$fn() is MISSING — the RLS policies cannot evaluate"
  fi
done

# ── 4. Encryption: the quiet one ───────────────────────────────────────────
#
# Without the master key, oracle_decrypt() returns NULL instead of raising.
# The data comes back looking empty, not looking encrypted, and every count
# above still passes.

echo
echo "4. Encryption"
CIPHER_COLS="$("${Q[@]}" -c "
  SELECT count(*)::text FROM information_schema.columns
   WHERE data_type='bytea' AND table_schema='public';" 2>/dev/null)"
WANT_CIPHER="$(m ciphertext_columns)"
[ "$CIPHER_COLS" = "$WANT_CIPHER" ] \
  && ok "$CIPHER_COLS ciphertext columns present" \
  || bad "$CIPHER_COLS ciphertext columns, manifest says $WANT_CIPHER"

if [ "$("${Q[@]}" -c "SELECT count(*)::text FROM pg_extension WHERE extname='pgcrypto';" 2>/dev/null)" != "0" ]; then
  ok "pgcrypto extension present"
else
  bad "pgcrypto is MISSING — nothing encrypted can ever be read back"
fi

# Prove a round trip actually works with the key this environment holds. This
# is the check that distinguishes "restored and readable" from "restored".
if [ -n "${ORACLE_ENCRYPTION_MASTER_KEY:-}" ]; then
  ROUNDTRIP="$("${Q[@]}" -c "
    SELECT oracle_decrypt(oracle_encrypt('neoh-restore-probe', 'k'), 'k');" 2>/dev/null)"
  [ "$ROUNDTRIP" = "neoh-restore-probe" ] \
    && ok "encrypt/decrypt round trip works" \
    || bad "encrypt/decrypt round trip FAILED (got '${ROUNDTRIP:-<null>}') — ciphertext columns are unreadable"
else
  echo "  WARN  ORACLE_ENCRYPTION_MASTER_KEY not set here, so decryptability is UNPROVEN."
  echo "        The dump contains $CIPHER_COLS ciphertext columns. Without the key they"
  echo "        read as NULL, not as an error. Do not call this restore verified"
  echo "        until a round trip has been demonstrated with the real key."
fi

# ── 5. Function grants ─────────────────────────────────────────────────────
#
# 0003 revokes EXECUTE from PUBLIC on every function, including the ones
# pgcrypto and earthdistance install. A restore that misses the re-grant leaves
# chat bodies, OAuth tokens and radius search silently unusable.

echo
echo "5. Function grants"
UNGRANTED="$("${Q[@]}" -c "
  SELECT count(*)::text FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname='public'
     AND p.proname IN ('oracle_encrypt','oracle_decrypt','app_current_tenant')
     AND NOT has_function_privilege('$ORACLE_DB_USER', p.oid, 'EXECUTE');" 2>/dev/null)"
[ "${UNGRANTED:-0}" = "0" ] \
  && ok "the core functions are executable by $ORACLE_DB_USER" \
  || bad "$UNGRANTED core function(s) are not executable by $ORACLE_DB_USER"

# ── 6. The application can actually use this database ──────────────────────
#
# The gap the DR drill found. pg_dump does not dump roles, so a restore into a
# fresh cluster produced correct data, correct RLS, 146 policies — and none of
# the three roles that 566 grants point at. Every check above passed. The
# application could not open a connection.

echo
echo "6. Application access"
WANT_ROLES="$(m roles_count)"
if [ -n "$WANT_ROLES" ]; then
  GOT_ROLES="$("${Q[@]}" -c "
    SELECT count(*)::text FROM pg_roles
     WHERE rolname NOT LIKE 'pg\_%' AND rolname <> 'postgres';" 2>/dev/null)"
  [ "${GOT_ROLES:-0}" -ge "${WANT_ROLES:-0}" ] \
    && ok "$GOT_ROLES role(s) present, manifest expected $WANT_ROLES" \
    || bad "$GOT_ROLES role(s) present, manifest expected $WANT_ROLES — the application cannot connect"

  APP_GRANTS="$("${Q[@]}" -c "
    SELECT count(*)::text FROM information_schema.role_table_grants
     WHERE grantee LIKE 'oracle%';" 2>/dev/null)"
  [ "${APP_GRANTS:-0}" -gt 0 ] \
    && ok "$APP_GRANTS table grant(s) to the application role" \
    || bad "the application role holds NO table grants — it can connect and read nothing"

  # A login role with no password cannot authenticate. That is the intended
  # state straight after a restore, but it must be said out loud rather than
  # discovered when the app fails to start.
  NOPW="$("${Q[@]}" -c "
    SELECT count(*)::text FROM pg_authid
     WHERE rolcanlogin AND rolpassword IS NULL
       AND rolname NOT LIKE 'pg\_%' AND rolname <> 'postgres';" 2>/dev/null)"
  if [ "${NOPW:-0}" -gt 0 ]; then
    echo "  WARN  $NOPW login role(s) have no password, which is how the roles file"
    echo "        restores them on purpose. Set it from the secret store before"
    echo "        starting the application:  ALTER ROLE <role> PASSWORD '<secret>';"
  fi
fi

# ── 7. Nothing is half-restored ────────────────────────────────────────────

echo
echo "7. Completeness"
EMPTY_TABLES="$("${Q[@]}" -c "
  SELECT count(*)::text FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public' AND c.relkind='r';" 2>/dev/null)"
[ "${EMPTY_TABLES:-0}" -gt 50 ] \
  && ok "$EMPTY_TABLES tables present" \
  || bad "only $EMPTY_TABLES tables present — the restore looks truncated"

echo
if [ "$FAIL" -eq 0 ]; then
  echo "  $PASS checks passed. RESTORE VERIFIED."
  echo
  exit 0
fi
echo "  $PASS passed, $FAIL FAILED. DO NOT TRUST THIS RESTORE."
echo
exit 1
