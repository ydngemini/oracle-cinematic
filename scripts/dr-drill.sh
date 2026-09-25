#!/usr/bin/env bash
#
# Neoh — disaster recovery validation drill.
#
# Proves recovery by performing it:
#
#   seed → assert → back up → destroy → restore → validate → isolate → smoke
#
# Everything happens in disposable containers created and destroyed by this
# script. It never touches production, never uses real customer data, and never
# contacts a provider — the fixture's phone numbers are in the reserved +1555
# fictional range and the Stripe ids are literals.
#
# This is deliberately a DRILL rather than a test: it runs the real
# backup-postgres.sh, the real restore-postgres.sh and the real
# verify-restore.sh, because the point is to find out whether those work, not
# whether a re-implementation of them works.
#
# Usage:  scripts/dr-drill.sh [--keep]
#         --keep   leave the containers up afterwards for inspection

set -uo pipefail

KEEP="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="${ORACLE_DRILL_DIR:-/tmp/neoh-drill}"
BACKUPS="$WORK/backups"

SRC=neoh-dr-source
DST=neoh-dr-restored
SRC_PORT=55432
DST_PORT=55433
# Distinctive on purpose. The secret-leak check below greps the manifest and
# the log for this value, and a password that is also an ordinary word ("drill"
# appears in the environment name, the filenames and the fixture) makes that
# check fire on every run and prove nothing. A leak check that always trips is
# a leak check nobody reads.
PGPW=drillpw-7c3f9a2e5b

PASS=0; FAIL=0
FAILURES=()

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); FAILURES+=("$*"); }
note() { printf '        %s\n' "$*"; }
phase(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

sq()  { docker exec "$SRC" psql -U postgres -d neoh_src     -tAX -c "$1" 2>/dev/null; }
dq()  { docker exec "$DST" psql -U postgres -d neoh_restore -tAX -c "$1" 2>/dev/null; }

# `-v` on every removal below is load-bearing, not tidiness. `docker rm -f`
# leaves a container's ANONYMOUS volume behind, and the postgres image declares
# its data directory as one — so each drill run stranded roughly 300 MB. Twenty
# runs took the host from 2.2 GB free to 35 MB, at which point no container
# could start at all and the drill began failing with every assertion empty.
# A drill that cannot be run repeatedly is not a drill.
cleanup() {
  if [ "$KEEP" = "--keep" ]; then
    echo; echo "  --keep: $SRC (port $SRC_PORT) and $DST (port $DST_PORT) left running."
    return
  fi
  docker rm -fv "$SRC" "$DST" >/dev/null 2>&1
}
trap cleanup EXIT

# ── 1. Prerequisites ────────────────────────────────────────────────────────
#
# The drill refuses to invent a missing tool. If the recovery task did not
# land, that is the finding, and a drill that quietly worked around it would be
# proving the wrong thing.

phase "1. Prerequisites"
for tool in backup-postgres.sh restore-postgres.sh verify-restore.sh dr-drill-fixture.sql; do
  if [ -f "$HERE/$tool" ]; then ok "$tool present"; else bad "$tool MISSING"; fi
done
[ -f "$REPO/backend/recovery_mode.py" ] && ok "recovery_mode.py present" || bad "recovery_mode.py MISSING"
[ -f "$REPO/docs/disaster-recovery-state-map.md" ] && ok "state map present" || bad "state map MISSING"
if [ "$FAIL" -gt 0 ]; then
  echo; echo "  Prerequisites missing. Stopping rather than drilling a fiction."; exit 1
fi

# ── 2. Disposable source ───────────────────────────────────────────────────

phase "2. Build the disposable source"
mkdir -p "$BACKUPS"
docker rm -fv "$SRC" "$DST" >/dev/null 2>&1
docker run -d --name "$SRC" -p "$SRC_PORT:5432" -v "$BACKUPS:/backups" \
  -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB=neoh_src postgres:16 >/dev/null
for _ in $(seq 1 45); do docker exec "$SRC" pg_isready -U postgres -q 2>/dev/null && break; sleep 2; done

if [ ! -f "$WORK/schema.sql" ]; then
  bad "no schema at $WORK/schema.sql — export one from a Neoh database first:
        pg_dump --schema-only --no-owner --no-privileges  > $WORK/schema.sql
        pg_dump --data-only --table=schema_migrations     > $WORK/ledger.sql"
  exit 1
fi
# Roles FIRST: a real Neoh cluster has oracle_app / oracle_app_login /
# platform_admin_role, and the schema dump's GRANTs name them. A drill source
# without them is not representative — and the first version of this drill was
# exactly that, which is why it could not have caught the missing-roles gap by
# itself.
if [ -f "$WORK/roles.sql" ]; then
  docker exec -i "$SRC" psql -U postgres -d neoh_src -q < "$WORK/roles.sql" >/dev/null 2>&1
  SRC_ROLES="$(sq "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'oracle%';")"
  [ "${SRC_ROLES:-0}" -gt 0 ] && ok "source has $SRC_ROLES application role(s)" \
                             || bad "source has no application roles — not representative"
else
  bad "no $WORK/roles.sql — the drill source would not represent a real cluster"
fi

docker exec -i "$SRC" psql -U postgres -d neoh_src -q -v ON_ERROR_STOP=1 < "$WORK/schema.sql" >/dev/null 2>&1
docker exec -i "$SRC" psql -U postgres -d neoh_src -q < "$WORK/ledger.sql" >/dev/null 2>&1

TABLES="$(sq "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r';")"
HEAD="$(sq "SELECT max(filename) FROM schema_migrations;")"
[ "${TABLES:-0}" -gt 100 ] && ok "schema loaded — $TABLES tables, head $HEAD" \
                           || bad "schema load looks wrong — only $TABLES tables"

docker exec -i "$SRC" psql -U postgres -d neoh_src -q -v ON_ERROR_STOP=1 < "$HERE/dr-drill-fixture.sql" >/dev/null 2>&1 \
  && ok "fixture loaded — two isolated brokerages" \
  || bad "fixture failed to load"

# ── 3. Pre-failure assertions ──────────────────────────────────────────────
#
# Written down BEFORE the disaster. Comparing the restore against numbers
# derived after the fact would prove only that the restore is self-consistent.

phase "3. Record pre-failure assertions"
A=aaaaaaaa-0000-4000-8000-00000000000a
B=bbbbbbbb-0000-4000-8000-00000000000b

EXP_TENANTS="$(sq "SELECT count(*) FROM tenants;")"
EXP_USERS="$(sq "SELECT count(*) FROM users;")"
EXP_CLIENTS_A="$(sq "SELECT count(*) FROM clients WHERE tenant_id='$A';")"
EXP_CLIENTS_B="$(sq "SELECT count(*) FROM clients WHERE tenant_id='$B';")"
EXP_PENDING="$(sq "SELECT count(*) FROM brokerage_invitations WHERE consumed_at IS NULL;")"
EXP_ACCEPTED="$(sq "SELECT count(*) FROM brokerage_invitations WHERE consumed_at IS NOT NULL;")"
EXP_SUB="$(sq "SELECT status FROM subscriptions WHERE tenant_id='$A';")"
EXP_MEDIA="$(sq "SELECT count(*) FROM property_media;")"
EXP_ENTITLE="$(sq "SELECT count(*) FROM mls_feed_entitlements;")"
EXP_DID="$(sq "SELECT inbound_did FROM telephony_routes WHERE tenant_id='$A';")"
EXP_MLS="$(sq "SELECT mls_number FROM oracle_mls_listings LIMIT 1;")"
# A checksum over the object fixture, so "the media row is back" means the same
# row rather than merely a row.
EXP_OBJECT_SHA="$(sq "SELECT encode(sha256(convert_to(url,'UTF8')),'hex') FROM property_media LIMIT 1;")"

note "tenants=$EXP_TENANTS users=$EXP_USERS clients A/B=$EXP_CLIENTS_A/$EXP_CLIENTS_B"
note "invites accepted/pending=$EXP_ACCEPTED/$EXP_PENDING subscription=$EXP_SUB"
note "media=$EXP_MEDIA entitlements=$EXP_ENTITLE did=$EXP_DID mls=$EXP_MLS"
note "object sha256=${EXP_OBJECT_SHA:0:16}…"
ok "pre-failure state recorded"

# ── 4. Backup, using the real tooling ──────────────────────────────────────

phase "4. Back up"
rm -f "$BACKUPS"/*
B0="$(date +%s)"
docker exec -i \
  -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=localhost -e ORACLE_DB_NAME=neoh_src \
  -e ORACLE_DB_USER=postgres -e ORACLE_ENV=drill \
  -e ORACLE_GIT_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)" \
  -e ORACLE_BACKUP_SCOPE="${ORACLE_BACKUP_SCOPE:-full}" \
  "$SRC" bash -s /backups < "$HERE/backup-postgres.sh" > "$WORK/backup.log" 2>&1
BACKUP_RC=$?
B1="$(date +%s)"
BACKUP_SECONDS=$((B1 - B0))

[ "$BACKUP_RC" -eq 0 ] && ok "backup command succeeded (${BACKUP_SECONDS}s)" \
                       || bad "backup command exited $BACKUP_RC — see $WORK/backup.log"

MANIFEST="$(ls "$BACKUPS"/*.manifest.json 2>/dev/null | head -1)"
[ -n "$MANIFEST" ] && ok "manifest exists" || { bad "no manifest produced"; exit 1; }

DUMP="$BACKUPS/$(grep -o '"dump_filename": "[^"]*"' "$MANIFEST" | cut -d'"' -f4)"
WANT_SHA="$(grep -o '"dump_sha256": "[^"]*"' "$MANIFEST" | cut -d'"' -f4)"
GOT_SHA="$(sha256sum "$DUMP" | cut -d' ' -f1)"
BACKUP_BYTES="$(wc -c < "$DUMP" | tr -d ' ')"

[ -n "$WANT_SHA" ] && ok "checksum recorded" || bad "no checksum in manifest"
[ "$WANT_SHA" = "$GOT_SHA" ] && ok "checksum validates" || bad "checksum MISMATCH"
[ "${BACKUP_BYTES:-0}" -gt 0 ] && ok "dump is non-zero ($BACKUP_BYTES bytes)" || bad "dump is empty"
grep -q '"git_sha": "[0-9a-f]\{7,\}"' "$MANIFEST" && ok "git SHA recorded" || bad "git SHA missing"
grep -q "\"migration_head\": \"$HEAD\"" "$MANIFEST" && ok "migration head recorded ($HEAD)" \
                                                    || bad "migration head wrong or missing"

# No secret may appear in the manifest or the log. Checked rather than assumed:
# a backup artifact is copied into runbooks and incident channels.
LEAKS=0
for secret in "$PGPW" PGPASSWORD api_key secret_key access_key BEGIN_PRIVATE; do
  if grep -qi -- "$secret" "$MANIFEST" 2>/dev/null; then bad "manifest leaks '$secret'"; LEAKS=1; fi
  if grep -qi -- "$secret" "$WORK/backup.log" 2>/dev/null; then bad "backup log leaks '$secret'"; LEAKS=1; fi
done
[ "$LEAKS" -eq 0 ] && ok "no secret in manifest or log"

# ── 5. The disaster ────────────────────────────────────────────────────────
#
# Against the disposable source only. Deliberately a MIXTURE: rows deleted,
# a value corrupted, and a child row orphaned — because "restore the whole
# database" hides a scoped restore's real weakness.

phase "5. Simulate the disaster"
sq "DELETE FROM clients WHERE tenant_id='$A';" >/dev/null
sq "UPDATE subscriptions SET status='canceled' WHERE tenant_id='$A';" >/dev/null
sq "DELETE FROM property_media;" >/dev/null
sq "DELETE FROM brokerage_invitations WHERE consumed_at IS NULL;" >/dev/null

DMG_CLIENTS="$(sq "SELECT count(*) FROM clients WHERE tenant_id='$A';")"
DMG_SUB="$(sq "SELECT status FROM subscriptions WHERE tenant_id='$A';")"
[ "$DMG_CLIENTS" = "0" ] && ok "brokerage A's contacts destroyed ($EXP_CLIENTS_A → 0)" \
                         || bad "the disaster did not take effect"
[ "$DMG_SUB" = "canceled" ] && ok "subscription corrupted (active → canceled)" \
                            || bad "subscription corruption did not take effect"
note "the source is now damaged; it is never restored over"

# ── 6. Restore into a NEW database ─────────────────────────────────────────

phase "6. Restore into a clean database"
docker run -d --name "$DST" -p "$DST_PORT:5432" -v "$BACKUPS:/backups" \
  -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB=neoh_restore postgres:16 >/dev/null
for _ in $(seq 1 45); do docker exec "$DST" pg_isready -U postgres -q 2>/dev/null && break; sleep 2; done

EMPTY="$(dq "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r';")"
[ "${EMPTY:-1}" = "0" ] && ok "target is genuinely empty" || bad "target already has $EMPTY tables"

R0="$(date +%s)"
docker exec -i \
  -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=127.0.0.1 -e ORACLE_DB_NAME=neoh_restore \
  -e ORACLE_DB_USER=postgres -e ORACLE_DB_SSLMODE=disable \
  "$DST" bash -s "/backups/$(basename "$MANIFEST")" < "$HERE/restore-postgres.sh" \
  > "$WORK/restore.log" 2>&1
R1="$(date +%s)"
RESTORE_SECONDS=$((R1 - R0))
ok "restore finished in ${RESTORE_SECONDS}s"

# Any unexpected restore error is a failed drill until explained.
# `grep -c` prints 0 and ALSO exits 1 when there is no match, so the obvious
# `|| echo 0` appends a second zero and the result is the two-line string
# "0\n0" — which `[` then rejects as not an integer, and the drill reports a
# failure whose message is literally "0". Count lines instead.
UNEXPECTED="$(grep "pg_restore: error" "$WORK/restore.log" 2>/dev/null | wc -l | tr -d ' ')"
if [ "${UNEXPECTED:-0}" -eq 0 ]; then
  ok "no pg_restore errors"
else
  bad "$UNEXPECTED pg_restore error(s) — unexplained errors fail the drill"
  grep "pg_restore: error" "$WORK/restore.log" | head -5 | sed 's/^/        /'
fi

# ── 7. Recovery validator ──────────────────────────────────────────────────

phase "7. Run the recovery validator"
docker exec -i \
  -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=127.0.0.1 -e ORACLE_DB_NAME=neoh_restore \
  -e ORACLE_DB_USER=postgres -e ORACLE_DB_SSLMODE=disable \
  -e ORACLE_ENCRYPTION_MASTER_KEY="${ORACLE_ENCRYPTION_MASTER_KEY:-drill-probe-key}" \
  "$DST" bash -s "/backups/$(basename "$MANIFEST")" < "$HERE/verify-restore.sh" \
  > "$WORK/verify.log" 2>&1
VERIFY_RC=$?
VERIFY_PASSES="$(grep "  PASS  " "$WORK/verify.log" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$VERIFY_RC" -eq 0 ]; then
  ok "validator passed ($VERIFY_PASSES checks)"
else
  bad "validator FAILED"
  grep "  FAIL  " "$WORK/verify.log" | head -6 | sed 's/^/        /'
fi

# ── 8. The data itself is back ─────────────────────────────────────────────

phase "8. Compare against the pre-failure assertions"
check() {
  local label="$1" want="$2" got="$3"
  [ "$want" = "$got" ] && ok "$label: $got" || bad "$label: $got, expected $want"
}
check "tenants"             "$EXP_TENANTS"   "$(dq "SELECT count(*) FROM tenants;")"
check "users"               "$EXP_USERS"     "$(dq "SELECT count(*) FROM users;")"
check "brokerage A contacts" "$EXP_CLIENTS_A" "$(dq "SELECT count(*) FROM clients WHERE tenant_id='$A';")"
check "brokerage B contacts" "$EXP_CLIENTS_B" "$(dq "SELECT count(*) FROM clients WHERE tenant_id='$B';")"
check "accepted invitations" "$EXP_ACCEPTED"  "$(dq "SELECT count(*) FROM brokerage_invitations WHERE consumed_at IS NOT NULL;")"
check "pending invitations"  "$EXP_PENDING"   "$(dq "SELECT count(*) FROM brokerage_invitations WHERE consumed_at IS NULL;")"
check "subscription status"  "$EXP_SUB"       "$(dq "SELECT status FROM subscriptions WHERE tenant_id='$A';")"
check "property media rows"  "$EXP_MEDIA"     "$(dq "SELECT count(*) FROM property_media;")"
check "MLS entitlements"     "$EXP_ENTITLE"   "$(dq "SELECT count(*) FROM mls_feed_entitlements;")"
check "inbound DID"          "$EXP_DID"       "$(dq "SELECT inbound_did FROM telephony_routes WHERE tenant_id='$A';")"
check "MLS listing"          "$EXP_MLS"       "$(dq "SELECT mls_number FROM oracle_mls_listings LIMIT 1;")"
check "object checksum"      "$EXP_OBJECT_SHA" "$(dq "SELECT encode(sha256(convert_to(url,'UTF8')),'hex') FROM property_media LIMIT 1;")"

# The FK that a scoped backup broke once already.
ORPHANS="$(dq "SELECT count(*) FROM property_media m LEFT JOIN leads l ON l.id = m.lead_id WHERE m.lead_id IS NOT NULL AND l.id IS NULL;")"
[ "${ORPHANS:-1}" = "0" ] && ok "no orphaned child rows" || bad "$ORPHANS orphaned property_media rows"

# ── 9. Tenant isolation, under a real tenant session ───────────────────────
#
# The check that matters most and is easiest to skip. A restore that brings
# back every row and loses RLS looks perfect from a row count.

phase "9. Prove tenant isolation"
# Tested as the REAL application role, not an invented one.
#
# The first version created its own `drill_app` with SELECT on every table, and
# both isolation checks failed in a way that looked like a leak. The cause was
# neither RLS nor the restore: migration 0003 revokes EXECUTE from PUBLIC on
# every function, so `drill_app` could not call `app_current_tenant()`, the
# policy could not evaluate, and the query errored — leaving the previous
# statement's output as the "count".
#
# Using oracle_app_login is also the more honest test: isolation matters for
# the role the application actually connects as.
dq "ALTER ROLE oracle_app_login LOGIN PASSWORD '$PGPW';" >/dev/null 2>&1

isolated() {
  docker exec -e PGPASSWORD="$PGPW" "$DST" psql -U oracle_app_login -d neoh_restore -tAX -c "
    SELECT set_config('app.current_tenant', '$1', false);
    SELECT count(*) FROM clients;" 2>/dev/null | tail -1
}
SEEN_AS_A="$(isolated "$A")"
SEEN_AS_B="$(isolated "$B")"

[ "$SEEN_AS_A" = "$EXP_CLIENTS_A" ] && ok "as brokerage A: sees its own $SEEN_AS_A contacts" \
  || bad "as brokerage A: sees $SEEN_AS_A contacts, expected $EXP_CLIENTS_A"
[ "$SEEN_AS_B" = "$EXP_CLIENTS_B" ] && ok "as brokerage B: sees its own $SEEN_AS_B contacts" \
  || bad "as brokerage B: sees $SEEN_AS_B contacts, expected $EXP_CLIENTS_B"

TOTAL=$((EXP_CLIENTS_A + EXP_CLIENTS_B))
if [ "$SEEN_AS_A" = "$TOTAL" ] || [ "$SEEN_AS_B" = "$TOTAL" ]; then
  bad "CROSS-TENANT LEAK: a tenant session can see all $TOTAL contacts"
else
  ok "neither tenant can see the other's data"
fi

# ── 9b. The application can actually connect and read ──────────────────────
#
# The check the first run of this drill did not have, and the gap it found:
# every row was back, RLS was intact, 146 policies were present — and the
# restored cluster had none of the three roles that 566 grants point at, so the
# application could not open a connection. Row counts cannot see that.

phase "9b. Prove the application can use the restored database"
APP_ROLES="$(dq "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'oracle%';")"
[ "${APP_ROLES:-0}" -gt 0 ] && ok "$APP_ROLES application role(s) restored" \
                            || bad "NO application role — the app cannot connect at all"

APP_GRANTS="$(dq "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee LIKE 'oracle%';")"
[ "${APP_GRANTS:-0}" -gt 0 ] && ok "$APP_GRANTS table grant(s) to the application role" \
                             || bad "the application role holds no grants — it can read nothing"

# Connect AS the application role and read a tenant's own data through RLS.
# This is the end-to-end claim: restored, permissioned, and isolated.
APP_READ="$(docker exec -e PGPASSWORD="$PGPW" "$DST" psql -U oracle_app_login -d neoh_restore -tAX -c "
  SELECT set_config('app.current_tenant', '$A', false);
  SELECT count(*) FROM clients;" 2>/dev/null | tail -1)"
[ "$APP_READ" = "$EXP_CLIENTS_A" ] \
  && ok "the application role reads brokerage A's $APP_READ contacts through RLS" \
  || bad "the application role read '$APP_READ' contacts, expected $EXP_CLIENTS_A"

# ── 10. Summary ────────────────────────────────────────────────────────────

phase "Drill result"
echo "  backup    ${BACKUP_SECONDS}s, $BACKUP_BYTES bytes"
echo "  restore   ${RESTORE_SECONDS}s"
echo "  RTO       $((BACKUP_SECONDS + RESTORE_SECONDS))s measured end to end (backup + restore)"
echo
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32m%s checks passed. DRILL PASSED.\033[0m\n\n' "$PASS"
  exit 0
fi
printf '  \033[31m%s passed, %s FAILED. DRILL FAILED.\033[0m\n' "$PASS" "$FAIL"
for f in "${FAILURES[@]}"; do echo "    - $f"; done
echo
exit 1
