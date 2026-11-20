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

TAB="$(printf '\t')"

# What a backup and a verifier must agree on. BYTE-IDENTICAL to the copies in
# backup-postgres.sh (test_backup_restore_scripts.py fails if they differ): if
# the two counted different things, a perfect restore would fail, and the fix
# someone reaches for then is loosening the check. backup-postgres.sh explains
# each predicate.
DATA_TABLES_SQL="SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND c.relpersistence <> 't' AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
CONSTRAINTS_SQL="FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND k.contype <> 'n' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
INDEXES_SQL="FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = ic.relnamespace WHERE n.nspname = 'public' AND (i.indisvalid OR c.relkind = 'p') AND i.indisready AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"

# The manifest's table_counts, one `"schema.table"<TAB>n` line per entry, the
# name JSON-encoded exactly as PostgreSQL's to_json() writes it — the same form
# the restored side is counted in, so the two compare byte for byte.
#
# It is the manifest's one object, so it is never read with m(). jq, then
# python3, then plain bash over the one-entry-per-line shape backup-postgres.sh
# writes (a stock postgres image has neither jq nor python3). Whatever reads it,
# section 2 requires the number of entries to equal table_counts_tables, so a
# reader that stops early, or reads nothing, cannot pass for a match.
manifest_table_counts() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '(.table_counts // {}) | to_entries[] | "\(.key | tojson)\t\(.value)"' "$MANIFEST" 2>/dev/null && return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
d = json.load(open(sys.argv[1])).get("table_counts") or {}
for k, v in d.items():
    print(json.dumps(k, ensure_ascii=False) + "\t" + str(v))' "$MANIFEST" 2>/dev/null && return
  fi
  local inside=0 line key n
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    if [ "$inside" = 0 ]; then
      case "$line" in
        '"table_counts"'*'{'*'}'*) return ;;     # an empty object on one line
        '"table_counts"'*'{'*) inside=1 ;;
      esac
      continue
    fi
    case "$line" in
      '}'*) return ;;
      '') continue ;;
    esac
    line="${line%,}"
    n="${line##*:}"
    key="${line%:*}"
    n="${n#"${n%%[![:space:]]*}"}"
    n="${n%"${n##*[![:space:]]}"}"
    key="${key%"${key##*[![:space:]]}"}"
    printf '%s\t%s\n' "$key" "$n"
  done < "$MANIFEST"
}

# Exact count(*) of every data table in the restored database, in the same
# `"schema.table"<TAB>n` form. $1: SQL text[] of tables not to count.
restored_table_counts() {
  printf '%s\n' "SET row_security = off;" \
    "SELECT format('SELECT %L || E''\\t'' || count(*) FROM ONLY %I.%I', to_json(t.nspname || '.' || t.relname)::text, t.nspname, t.relname) FROM ($DATA_TABLES_SQL) t WHERE (t.nspname || '.' || t.relname) <> ALL ($1) ORDER BY t.nspname, t.relname" \
    '\gexec' | "${Q[@]}" -q -v ON_ERROR_STOP=1 -f - 2>&1
}

# "public.leads" -> public.leads, for messages.
unq() { local k="${1#\"}"; printf '%s' "${k%\"}"; }

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

# ── 2. Every row the backup held (REC-03) ──────────────────────────────────
#
# A manifest from backup-postgres.sh since 2026-11-20 (manifest_version 2)
# carries the exact row count of EVERY table whose data is in the dump, read
# inside the dump's own snapshot. Every one must come back exactly; a table in
# the manifest that is missing fails; a table present after the restore that
# the manifest does not list fails unless the manifest lists it as excluded.
#
# An older manifest counted four tables only, outside the dump's snapshot.
# For one of those this keeps the old four-table check — and says, here and in
# the summary, that the reconciliation was NOT exhaustive: a restore that lost
# rows in any other table would pass it.

echo
echo "2. Row counts"
WANT_TABLES="$(m table_counts_tables)"
MANIFEST_VERSION="$(m manifest_version)"
EXHAUSTIVE=0
RECONCILED="NOT EXHAUSTIVE"
if [ -n "$WANT_TABLES" ]; then
  EXHAUSTIVE=1
  RECONCILED="EXHAUSTIVE — but it FAILED, see section 2"
  EXCLUDED="$(m excluded_tables)"
  case "$EXCLUDED" in
    *[!A-Za-z0-9_.\ ]*) bad "excluded_tables in the manifest is not a list of table names: '$EXCLUDED'"; EXCLUDED="" ;;
  esac
  if [ -n "$EXCLUDED" ]; then
    EXCLUDED_SQL="string_to_array('$EXCLUDED', ' ')"
  else
    EXCLUDED_SQL="ARRAY[]::text[]"
  fi

  WANT_COUNTS="$(manifest_table_counts)"
  READ_RC=$?
  PARSED="$(printf '%s\n' "$WANT_COUNTS" | grep -c . || true)"
  GOT_RAW="$(restored_table_counts "$EXCLUDED_SQL")"
  GOT_RC=$?
  GOT_COUNTS="$(printf '%s\n' "$GOT_RAW" | grep "^\".*\"$TAB[0-9][0-9]*\$" || true)"

  if [ "$READ_RC" -ne 0 ]; then
    bad "the manifest's table_counts could not be read (reader exited $READ_RC) — no table can be called reconciled"
  elif [ "$PARSED" != "$WANT_TABLES" ]; then
    bad "read $PARSED of the $WANT_TABLES table counts this manifest declares — it cannot be parsed in full here, so no table can be called reconciled (install jq or python3)"
  elif [ "$GOT_RC" -ne 0 ]; then
    bad "could not count every restored table: $(printf '%s\n' "$GOT_RAW" | grep -v "^\".*\"$TAB" | head -1)"
  else
    # One pass over both lists. Counts compare as strings: exact, no rounding.
    RESULT="$( { printf '%s\n' "$WANT_COUNTS"; echo "@@restored@@"; printf '%s\n' "$GOT_COUNTS"; } | awk -F "$TAB" '
      $0 == "@@restored@@" { side = 1; next }
      $0 == ""             { next }
      side == 0 { want[$1] = $2; order[++n] = $1; next }
      { got[$1] = $2; if (!($1 in want)) extra[++e] = $1 }
      END {
        for (i = 1; i <= n; i++) {
          k = order[i]
          if (!(k in got))               print "MISSING\t" k "\t" want[k]
          else if (got[k] "" != want[k] "") print "DIFF\t" k "\t" got[k] "\t" want[k]
          else                           matched++
        }
        for (i = 1; i <= e; i++) print "EXTRA\t" extra[i] "\t" got[extra[i]]
        print "MATCHED\t" (matched + 0)
      }')"
    CMP_RC=$?
    MATCHED=""; MISMATCHED=0
    while IFS="$TAB" read -r kind key a b; do
      case "$kind" in
        MISSING) bad "$(unq "$key"): the backup had $a row(s); the table is MISSING after the restore"; MISMATCHED=$((MISMATCHED+1)) ;;
        DIFF)    bad "$(unq "$key"): $a row(s) after the restore, the backup's snapshot had $b"; MISMATCHED=$((MISMATCHED+1)) ;;
        EXTRA)   bad "$(unq "$key"): present after the restore ($a rows) but not in the backup's table_counts — nothing to reconcile it against"; MISMATCHED=$((MISMATCHED+1)) ;;
        MATCHED) MATCHED="$key" ;;
      esac
    done <<EOF
$RESULT
EOF
    # Fail closed. Only a comparison that ran to the end, found no mismatch
    # AND confirmed every listed table passes; anything short of that — awk
    # failing, its output cut off, a count that does not add up — is a FAIL,
    # never a silent gap between PASS and FAIL.
    if [ "$CMP_RC" -ne 0 ]; then
      bad "the comparison of backup and restored counts did not run (awk exited $CMP_RC) — nothing is reconciled"
    elif [ "$MISMATCHED" -eq 0 ] && [ "$MATCHED" = "$WANT_TABLES" ]; then
      ok "every one of the $WANT_TABLES tables in the backup's snapshot came back with exactly its row count ($(m table_counts_rows) rows)"
      RECONCILED="EXHAUSTIVE — all $WANT_TABLES tables in the backup's snapshot matched exactly"
    elif [ "$MISMATCHED" -eq 0 ]; then
      bad "only ${MATCHED:-none} of the $WANT_TABLES tables could be confirmed as matching, and no mismatch was reported — the comparison did not complete, so nothing is reconciled"
    fi
  fi

  # Tables whose data the backup's scope left out: not in table_counts, by
  # design. Their schema must still be back. leads returns through the
  # authoritative extract, whose row count the manifest records.
  for t in $EXCLUDED; do
    exists="$("${Q[@]}" -c "SELECT (to_regclass('$t') IS NOT NULL)::text;" 2>/dev/null)"
    if [ "$exists" != "true" ]; then
      bad "$t: the table is MISSING after the restore (its data was excluded by scope, but its schema is in the dump)"
      continue
    fi
    want="$(m "expected_${t#public.}")"
    # -q: without it psql 16 prints the SET's command tag ahead of the count.
    got="$("${Q[@]}" -q -c "SET row_security = off; SELECT count(*)::text FROM ONLY $t;" 2>/dev/null)"
    if [ -n "$want" ] && [ "$t" = "public.${t#public.}" ]; then
      [ "$got" = "$want" ] \
        && ok "$t: $got row(s), the authoritative extract's exact count (data otherwise excluded by scope)" \
        || bad "$t: ${got:-none} row(s), the authoritative extract held $want"
    else
      echo "  NOTE  $t: ${got:-?} row(s); data excluded by the backup's scope (rebuildable), not reconciled"
    fi
  done
elif [ "${MANIFEST_VERSION:-1}" != "1" ]; then
  bad "manifest_version is '$MANIFEST_VERSION' but there is no table_counts_tables — the manifest is damaged or was not parsed; row reconciliation is impossible"
  RECONCILED="IMPOSSIBLE — damaged manifest"
else
  echo "  WARN  This manifest predates per-table counts (manifest_version 1), so only"
  echo "        tenants, users, clients and leads can be compared — and their counts"
  echo "        were taken outside the dump's snapshot. Row reconciliation is NOT"
  echo "        EXHAUSTIVE: a restore that lost rows in any other table passes here."
  for table in tenants users clients leads; do
    want="$(m "expected_$table")"
    [ -n "$want" ] || continue
    got="$("${Q[@]}" -c "SELECT count(*)::text FROM $table;" 2>/dev/null)"
    [ "$got" = "$want" ] \
      && ok "$table: $got" \
      || bad "$table: $got, expected $want"
  done
  RECONCILED="NOT EXHAUSTIVE — a manifest_version 1 manifest names only tenants, users, clients and leads; take a new backup with the current backup-postgres.sh to reconcile every table"
fi

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

# ── 8. Constraints, indexes and referential integrity (REC-03) ─────────────
#
# pg_restore creates every foreign key after the data and VALIDATES it on the
# way in, so a restored FK that is not validated is one the source never
# validated either (0122 adds lead_response_events_command_fk NOT VALID, and
# pg_dump preserves NOT VALID). The manifest names those; any other unvalidated
# FK fails. A constraint or index that did not come back fails on its count —
# its table's rows can all be there while the database no longer refuses what
# the source refused.

echo
echo "8. Constraints and indexes"
GOT_NV="$("${Q[@]}" -c "SELECT coalesce(string_agg(c.relname || '.' || k.conname, ' ' ORDER BY c.relname, k.conname), '') $CONSTRAINTS_SQL AND k.contype = 'f' AND NOT k.convalidated;" 2>/dev/null)"
if [ "$EXHAUSTIVE" = 1 ]; then
  for pair in "constraints_public:constraints:$CONSTRAINTS_SQL" \
              "foreign_keys_public:foreign keys:$CONSTRAINTS_SQL AND k.contype = 'f'" \
              "indexes_public:indexes:$INDEXES_SQL"; do
    key="${pair%%:*}"; rest="${pair#*:}"; label="${rest%%:*}"; from="${rest#*:}"
    want="$(m "$key")"
    got="$("${Q[@]}" -c "SELECT count(*)::text $from;" 2>/dev/null)"
    if [ -z "$want" ]; then
      bad "the manifest has no $key — cannot compare $label"
    elif [ "$got" = "$want" ]; then
      ok "$got $label in public, as backed up"
    else
      bad "${got:-?} $label in public, but the backup had $want — the restore did not bring the schema back whole"
    fi
  done
  WANT_NV="$(m foreign_keys_not_validated)"
  if [ "$GOT_NV" = "$WANT_NV" ]; then
    if [ -z "$GOT_NV" ]; then
      ok "every foreign key is validated: referential integrity was enforced as the data came back"
    else
      ok "every foreign key the source validated is validated; NOT VALID exactly as in the source: $GOT_NV"
    fi
  else
    for fk in $GOT_NV; do
      case " $WANT_NV " in
        *" $fk "*) ;;
        *) bad "foreign key $fk is NOT VALIDATED after the restore, but the source had validated it — referential integrity of its rows is unproven" ;;
      esac
    done
    for fk in $WANT_NV; do
      case " $GOT_NV " in
        *" $fk "*) ;;
        *) bad "foreign key $fk was NOT VALID in the source; after the restore it is validated or gone" ;;
      esac
    done
  fi
else
  echo "  WARN  This manifest predates constraint and index counts; not compared."
  [ -n "$GOT_NV" ] && echo "  NOTE  foreign keys not validated after the restore: $GOT_NV"
fi

echo
echo "  Row reconciliation: $RECONCILED."
if [ "$FAIL" -eq 0 ]; then
  if [ "$EXHAUSTIVE" = 1 ]; then
    echo "  $PASS checks passed. RESTORE VERIFIED."
  else
    echo "  $PASS checks passed. RESTORE VERIFIED, BUT NOT EXHAUSTIVELY — see 'Row reconciliation' above."
  fi
  echo
  exit 0
fi
echo "  $PASS passed, $FAIL FAILED. DO NOT TRUST THIS RESTORE."
echo
exit 1
