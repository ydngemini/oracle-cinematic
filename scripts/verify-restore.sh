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

# REC-04: "> 0" passed a restore that lost RLS on all but one table. The
# manifest now records the exact counts at backup time; all must come back.
rls_check() {
  local label="$1" got="$2" want="$3" none="$4"
  if [ -n "$want" ]; then
    [ "${got:-x}" = "$want" ] \
      && ok "$label: $got, as backed up" \
      || bad "$label: ${got:-none}, but the backup had $want — tenant isolation did not fully come back"
  else
    [ "${got:-0}" -gt 0 ] \
      && ok "$label: $got (this manifest predates exact RLS counts, so only '> 0' is checked)" \
      || bad "$none"
  fi
}
rls_check "tables with row-level security" "$RLS_TABLES" "$(m rls_tables)" \
  "NO table has row-level security — every tenant can read every other tenant"
rls_check "tables that FORCE row-level security" "$FORCED" "$(m rls_forced_tables)" \
  "no table FORCEs RLS — the owner role bypasses every policy"
rls_check "RLS policies" "$POLICIES" "$(m rls_policies)" "no RLS policies present"

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

# Prove the RESTORED ciphertext opens with the key this environment holds
# (DR-001/REC-02, 2026-11-13). This used to encrypt and decrypt the literal
# 'k' — proving only that pgcrypto works — and with no key it warned and still
# reported VERIFIED. Now, for every *_ciphertext column on a table with a
# tenant_id, up to 3 restored values are decrypted with the tenant's real key:
# crypto.derive_tenant_key = HKDF-SHA256(salt = 32 zero bytes, ikm = master,
# info = 'oracle-pii-v1:' || tenant_id), 32 bytes, i.e. two HMACs, which
# pgcrypto's hmac() computes in the restored database itself. The master key
# reaches psql through \getenv (psql >= 15), never the command line; an older
# psql leaves :'mk' unset, the query fails, and that counts as unreadable.
ZERO_SALT="\\x$(printf '0%.0s' $(seq 1 64))"
CIPHER_PAIRS="$("${Q[@]}" -c "
  SELECT c.table_name || ' ' || c.column_name
    FROM information_schema.columns c
    JOIN information_schema.columns t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
     AND t.column_name = 'tenant_id' AND t.data_type = 'uuid'
   WHERE c.table_schema = 'public' AND c.data_type = 'bytea'
     AND c.column_name LIKE '%\_ciphertext' ESCAPE '\\'
   ORDER BY 1;" 2>/dev/null)"
WITH_DATA=0; SAMPLED=0; UNREADABLE=0; QUERY_FAILED=0
while read -r CT_TABLE CT_COL; do
  [ -n "${CT_TABLE:-}" ] || continue
  SAMPLE="SELECT tenant_id, \"$CT_COL\" AS v FROM \"$CT_TABLE\"
           WHERE \"$CT_COL\" IS NOT NULL AND tenant_id IS NOT NULL LIMIT 3"
  if [ -n "${ORACLE_ENCRYPTION_MASTER_KEY:-}" ]; then
    R="$(printf '%s\n' '\getenv mk ORACLE_ENCRYPTION_MASTER_KEY' "
      SELECT count(*) || ' ' || count(*) FILTER (WHERE oracle_decrypt(s.v, encode(hmac(
               convert_to('oracle-pii-v1:' || s.tenant_id::text, 'UTF8') || '\\x01'::bytea,
               hmac(convert_to(:'mk', 'UTF8'), '$ZERO_SALT'::bytea, 'sha256'),
               'sha256'), 'hex')) IS NULL)
        FROM ($SAMPLE) s;" | "${Q[@]}" -f - 2>/dev/null | tail -1)"
  else
    R="$("${Q[@]}" -c "SELECT count(*) || ' 0' FROM ($SAMPLE) s;" 2>/dev/null)"
  fi
  case "$R" in
    *[0-9]\ *[0-9]) ;;
    *) QUERY_FAILED=$((QUERY_FAILED+1)); continue ;;
  esac
  N="${R%% *}"; BADN="${R##* }"
  [ "$N" -gt 0 ] && WITH_DATA=$((WITH_DATA+1))
  SAMPLED=$((SAMPLED+N)); UNREADABLE=$((UNREADABLE+BADN))
done <<< "$CIPHER_PAIRS"

if [ "$QUERY_FAILED" -gt 0 ]; then
  bad "$QUERY_FAILED ciphertext column(s) could not be sampled (psql < 15 has no \\getenv, or the query failed) — decryptability unproven"
elif [ "$WITH_DATA" -eq 0 ]; then
  ok "no restored ciphertext values to decrypt (nothing encrypted in this dump)"
elif [ -z "${ORACLE_ENCRYPTION_MASTER_KEY:-}" ]; then
  bad "ORACLE_ENCRYPTION_MASTER_KEY is not set: $WITH_DATA column(s) hold ciphertext that would read as NULL. Not verified until it decrypts with the real key"
elif [ "$UNREADABLE" -gt 0 ]; then
  bad "$UNREADABLE of $SAMPLED restored ciphertext value(s) do NOT decrypt with this key (wrong or rotated key?) — they would read as empty"
else
  ok "$SAMPLED restored ciphertext value(s) across $WITH_DATA column(s) decrypt with the per-tenant key"
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
