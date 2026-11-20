#!/usr/bin/env bash
#
# Neoh — disaster recovery validation drill.
#
# Proves recovery by performing it:
#
#   seed → assert → back up (under live writes, in both scopes) → destroy →
#   restore → validate → isolate → smoke → break the restore on purpose →
#   validate again → restore and validate the other scope's backup
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
# The disposable clusters. Must ship bash (the real scripts run inside them via
# `bash -s`) and pg_dump/pg_restore of the production major version.
PG_IMAGE="${ORACLE_DRILL_PG_IMAGE:-postgres:16}"

SRC=neoh-dr-source
DST=neoh-dr-restored
# No fixed host ports. The drill reaches both clusters through `docker exec`;
# a published port exists only so --keep can say where to connect. A fixed
# port (55432 before) sits inside Linux's ephemeral range (32768-60999), where
# any outbound connection on the runner can already hold it: CI run
# 44803825496 failed "address already in use" before the drill began. Docker
# picks a free loopback port at start, and nothing listens on 0.0.0.0.
host_port() { docker port "$1" 5432/tcp 2>/dev/null | head -n1; }
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
    echo; echo "  --keep: $SRC ($(host_port "$SRC")) and $DST ($(host_port "$DST")) left running."
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

# Ready means the REAL server, over TCP. The image's first start runs initdb
# under a temporary server that listens on the Unix socket only, then restarts
# it — so a socket-only pg_isready says "ready" during init, and the very next
# query lands in the restart gap and comes back empty. That made step 6 report
# "target already has  tables" on a genuinely empty target (found 2026-10-14
# making the drill a CI job, L17-18).
wait_ready() {
  for _ in $(seq 1 45); do
    docker exec "$1" pg_isready -h 127.0.0.1 -U postgres -q 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

# ── 2. Disposable source ───────────────────────────────────────────────────

phase "2. Build the disposable source"
mkdir -p "$BACKUPS"
docker rm -fv "$SRC" "$DST" >/dev/null 2>&1
docker run -d --name "$SRC" -p "127.0.0.1::5432" -v "$BACKUPS:/backups" \
  -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB=neoh_src "$PG_IMAGE" >/dev/null
wait_ready "$SRC"

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

# REC-03: a table a live workload keeps writing to WHILE the backup runs (step
# 4). Drill-only, never in the Neoh schema; the backup counts it like any
# other table, which is the point.
WRITES=dr_drill_concurrent_writes
sq "CREATE TABLE public.$WRITES (id bigserial PRIMARY KEY, written_at timestamptz NOT NULL DEFAULT clock_timestamp());" >/dev/null
[ "$(sq "SELECT to_regclass('public.$WRITES') IS NOT NULL;")" = "t" ] \
  && ok "concurrent-write table created" || bad "could not create $WRITES"

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

phase "4. Back up while a writer is committing"
rm -f "$BACKUPS"/*

# REC-03: the manifest's counts must be the DUMP's counts, not the counts of
# whatever moment psql happened to ask. A writer commits one row every ~20 ms
# for the whole backup; it stops when it sees row -1, which is inserted only
# after the backup has finished (so it is never in the dump). Each INSERT is
# its own transaction (COMMIT inside DO, allowed at top level since PG 11).
docker exec "$SRC" psql -U postgres -d neoh_src -q -v ON_ERROR_STOP=1 -c "
  DO \$\$ BEGIN
    FOR i IN 1..15000 LOOP
      EXIT WHEN EXISTS (SELECT 1 FROM public.$WRITES WHERE id = -1);
      INSERT INTO public.$WRITES DEFAULT VALUES;
      COMMIT;
      PERFORM pg_sleep(0.02);
    END LOOP;
  END \$\$;" > "$WORK/writer.log" 2>&1 &
WRITER_PID=$!
W_BEFORE=0
for _ in $(seq 1 50); do
  W_BEFORE="$(sq "SELECT count(*) FROM public.$WRITES;")"
  [ "${W_BEFORE:-0}" -ge 3 ] && break
  sleep 0.2
done
[ "${W_BEFORE:-0}" -ge 3 ] && ok "writer is committing ($W_BEFORE rows before the backup starts)" \
                           || bad "the concurrent writer never started — see $WORK/writer.log"

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
W_AFTER="$(sq "SELECT count(*) FROM public.$WRITES;")"

# The OTHER scope too, still under the writer, so every run drills both: CI
# runs this drill in `full` scope, and the nightly backup runs `authoritative`
# (data of the rebuildable tables excluded, their human-touched leads carried
# by an extract read in the same snapshot). Restored and verified in step 11.
# A different ORACLE_ENV keeps its file names apart from the first backup's.
if [ "${ORACLE_BACKUP_SCOPE:-full}" = "authoritative" ]; then OTHER_SCOPE=full; else OTHER_SCOPE=authoritative; fi
docker exec -i \
  -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=localhost -e ORACLE_DB_NAME=neoh_src \
  -e ORACLE_DB_USER=postgres -e ORACLE_ENV="drill-$OTHER_SCOPE" \
  -e ORACLE_GIT_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)" \
  -e ORACLE_BACKUP_SCOPE="$OTHER_SCOPE" \
  "$SRC" bash -s /backups < "$HERE/backup-postgres.sh" > "$WORK/backup-other.log" 2>&1
OTHER_RC=$?
W_END="$(sq "SELECT count(*) FROM public.$WRITES;")"
sq "INSERT INTO public.$WRITES (id) VALUES (-1);" >/dev/null
for _ in $(seq 1 50); do kill -0 "$WRITER_PID" 2>/dev/null || break; sleep 0.2; done
kill "$WRITER_PID" 2>/dev/null
wait "$WRITER_PID" 2>/dev/null

[ "$BACKUP_RC" -eq 0 ] && ok "backup command succeeded (${BACKUP_SECONDS}s)" \
                       || bad "backup command exited $BACKUP_RC — see $WORK/backup.log"

MANIFEST="$(ls "$BACKUPS"/neoh-drill-2*.manifest.json 2>/dev/null | head -1)"
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

# REC-03: the manifest must count every table whose data is in the dump, from
# the dump's own snapshot. The writer table is the proof: rows committed after
# the snapshot (before W_AFTER) are in neither the dump nor the manifest, so
# the manifest's number is >= the count taken before the backup and < the count
# taken after it. Step 8 then requires the restored table to hold exactly that
# number.
grep -q '"manifest_version": 2' "$MANIFEST" && ok "manifest_version 2 (per-table counts)" \
                                            || bad "manifest is not version 2 — no per-table counts"
M_TABLES="$(sed -n 's/^[[:space:]]*"table_counts_tables":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$MANIFEST" | head -1)"
M_MISSING=""
for t in tenants users clients property_media brokerage_invitations "$WRITES"; do
  grep -q "^[[:space:]]*\"public\.$t\": [0-9]" "$MANIFEST" || M_MISSING="$M_MISSING $t"
done
if [ "${M_TABLES:-0}" -gt 100 ] && [ -z "$M_MISSING" ]; then
  ok "manifest holds exact counts for $M_TABLES tables, not a four-table sample"
else
  bad "manifest counts ${M_TABLES:-no} tables; missing:${M_MISSING:- none}"
fi
M_WRITES="$(sed -n "s/^[[:space:]]*\"public\.$WRITES\":[[:space:]]*\([0-9][0-9]*\),\{0,1\}[[:space:]]*\$/\1/p" "$MANIFEST" | head -1)"
if [ -n "$M_WRITES" ] && [ "${W_AFTER:-0}" -gt "${W_BEFORE:-0}" ] \
   && [ "$M_WRITES" -ge "${W_BEFORE:-0}" ] && [ "$M_WRITES" -lt "${W_AFTER:-0}" ]; then
  ok "writer committed $((W_AFTER - W_BEFORE)) row(s) during the backup; the manifest counted $M_WRITES (before: $W_BEFORE, after: $W_AFTER) — the snapshot's state, not a moment's"
else
  bad "snapshot consistency unproven: manifest counted '${M_WRITES:-nothing}' for $WRITES, writer saw $W_BEFORE before and $W_AFTER after the backup"
fi

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
docker run -d --name "$DST" -p "127.0.0.1::5432" -v "$BACKUPS:/backups" \
  -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB=neoh_restore "$PG_IMAGE" >/dev/null
wait_ready "$DST"

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
  -e ORACLE_ENCRYPTION_MASTER_KEY=drill-probe-key \
  "$DST" bash -s "/backups/$(basename "$MANIFEST")" < "$HERE/verify-restore.sh" \
  > "$WORK/verify.log" 2>&1
VERIFY_RC=$?
# DR-001/REC-02: the fixture's sealed note is encrypted under drill-probe-key's
# tenant key, so the validator must also REFUSE this restore without a key and
# with the wrong one. A validator that passes either has stopped checking.
for NEG in "" "not-the-drill-key"; do
  NEG_LABEL="no"; [ -n "$NEG" ] && NEG_LABEL="a wrong"
  docker exec -i \
    -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=127.0.0.1 -e ORACLE_DB_NAME=neoh_restore \
    -e ORACLE_DB_USER=postgres -e ORACLE_DB_SSLMODE=disable \
    -e ORACLE_ENCRYPTION_MASTER_KEY="$NEG" \
    "$DST" bash -s "/backups/$(basename "$MANIFEST")" < "$HERE/verify-restore.sh" \
    > "$WORK/verify-neg.log" 2>&1 \
    && bad "validator PASSED with $NEG_LABEL master key — it is not checking decryptability" \
    || ok "validator refuses the restore with $NEG_LABEL master key"
done
VERIFY_PASSES="$(grep "  PASS  " "$WORK/verify.log" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$VERIFY_RC" -eq 0 ]; then
  ok "validator passed ($VERIFY_PASSES checks)"
else
  bad "validator FAILED"
  grep "  FAIL  " "$WORK/verify.log" | head -6 | sed 's/^/        /'
fi
grep -q "Row reconciliation: EXHAUSTIVE — all" "$WORK/verify.log" \
  && ok "validator reconciled every table exhaustively" \
  || bad "validator did not reconcile every table — see $WORK/verify.log"

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
# REC-03: exactly the rows the manifest counted inside the dump's snapshot —
# none of the writes committed after it, none missing.
check "rows written during the backup" "${M_WRITES:-?}" "$(dq "SELECT count(*) FROM public.$WRITES;")"

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

# ── 10. Break the restore on purpose: the validator must notice ────────────
#
# REC-03. The validator used to count tenants, users, clients and leads, so a
# restore that lost rows in any other table passed it. Each run below breaks
# the restored database in one way a real restore can fail silently, and the
# validator must refuse it AND name what broke.

phase "10. A damaged restore must fail validation"
# run_verify <log> [database] [manifest]
run_verify() {
  docker exec -i \
    -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=127.0.0.1 -e ORACLE_DB_NAME="${2:-neoh_restore}" \
    -e ORACLE_DB_USER=postgres -e ORACLE_DB_SSLMODE=disable \
    -e ORACLE_ENCRYPTION_MASTER_KEY=drill-probe-key \
    "$DST" bash -s "/backups/$(basename "${3:-$MANIFEST}")" < "$HERE/verify-restore.sh" > "$1" 2>&1
}

# (a) One row gone from a table outside the old four-table sample. property_media
# is the row class pg_restore once dropped silently (rejected by its FK while
# the restore reported "completed").
dq "DELETE FROM property_media WHERE id = (SELECT id FROM property_media ORDER BY id LIMIT 1);" >/dev/null
if run_verify "$WORK/verify-lost-row.log"; then
  bad "validator PASSED a restore missing one property_media row"
else
  LOST_FAILS="$(grep -c "  FAIL  " "$WORK/verify-lost-row.log" || true)"
  if grep -q "  FAIL  public.property_media: " "$WORK/verify-lost-row.log" && [ "$LOST_FAILS" = "1" ]; then
    ok "validator refuses a restore missing one property_media row, and names that table alone"
  else
    bad "validator refused the restore but did not name public.property_media as the only failure ($LOST_FAILS FAIL line(s))"
    grep "  FAIL  " "$WORK/verify-lost-row.log" | head -4 | sed 's/^/        /'
  fi
fi

# (b) Referential integrity and indexes: one FK re-created NOT VALID, one index
# dropped. Every row count still matches these two.
NV_FK="$(dq "SELECT conname FROM pg_constraint WHERE conrelid = 'public.property_media'::regclass AND contype = 'f' AND convalidated ORDER BY conname LIMIT 1;")"
NV_IDX="$(dq "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = 'public.property_media'::regclass AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid) ORDER BY c.relname LIMIT 1;")"
if [ -n "$NV_FK" ] && [ -n "$NV_IDX" ]; then
  dq "DO \$\$ DECLARE d text; BEGIN
        SELECT pg_get_constraintdef(oid) INTO d FROM pg_constraint
         WHERE conrelid = 'public.property_media'::regclass AND conname = '$NV_FK';
        EXECUTE format('ALTER TABLE public.property_media DROP CONSTRAINT %I', '$NV_FK');
        EXECUTE format('ALTER TABLE public.property_media ADD CONSTRAINT %I %s NOT VALID', '$NV_FK', d);
      END \$\$;" >/dev/null
  dq "DROP INDEX public.\"$NV_IDX\";" >/dev/null
  run_verify "$WORK/verify-integrity.log"
  grep -q "  FAIL  foreign key property_media.$NV_FK is NOT VALIDATED" "$WORK/verify-integrity.log" \
    && ok "validator refuses an unvalidated foreign key ($NV_FK) the source had validated" \
    || bad "validator did not catch foreign key $NV_FK left NOT VALID"
  grep -q "  FAIL  .* indexes in public, but the backup had" "$WORK/verify-integrity.log" \
    && ok "validator refuses a restore missing an index ($NV_IDX)" \
    || bad "validator did not catch the dropped index $NV_IDX"
else
  bad "no validated FK or plain index on property_media to break (fk='$NV_FK' index='$NV_IDX')"
fi

# ── 11. The other scope's backup: restore it and validate it ───────────────
#
# Into a fresh database in the same restored cluster (the roles file is
# idempotent). Same contract as steps 6–8: no pg_restore error, the validator
# passes and reconciles every table exhaustively, and the writer table holds
# exactly the count its manifest took inside its own snapshot. In
# `authoritative` scope the validator must also hold `leads` to the extract.

phase "11. The $OTHER_SCOPE-scope backup: restore and validate"
MANIFEST2="$(ls "$BACKUPS"/neoh-drill-"$OTHER_SCOPE"-*.manifest.json 2>/dev/null | head -1)"
if [ "$OTHER_RC" -eq 0 ] && [ -n "$MANIFEST2" ]; then
  ok "$OTHER_SCOPE-scope backup succeeded under the same writer"
else
  bad "$OTHER_SCOPE-scope backup exited $OTHER_RC — see $WORK/backup-other.log"
fi
M2_WRITES="$(sed -n "s/^[[:space:]]*\"public\.$WRITES\":[[:space:]]*\([0-9][0-9]*\),\{0,1\}[[:space:]]*\$/\1/p" "${MANIFEST2:-/dev/null}" | head -1)"
if [ -n "$M2_WRITES" ] && [ "$M2_WRITES" -ge "${W_AFTER:-0}" ] && [ "$M2_WRITES" -lt "${W_END:-0}" ]; then
  ok "its manifest counted $M2_WRITES writer rows (before: $W_AFTER, after: $W_END)"
else
  bad "its manifest counted '${M2_WRITES:-nothing}' writer rows; before: $W_AFTER, after: $W_END"
fi

dq "CREATE DATABASE neoh_restore_other;" >/dev/null
docker exec -i \
  -e PGPASSWORD="$PGPW" -e ORACLE_DB_HOST=127.0.0.1 -e ORACLE_DB_NAME=neoh_restore_other \
  -e ORACLE_DB_USER=postgres -e ORACLE_DB_SSLMODE=disable \
  "$DST" bash -s "/backups/$(basename "${MANIFEST2:-none}")" < "$HERE/restore-postgres.sh" \
  > "$WORK/restore-other.log" 2>&1
RESTORE2_RC=$?
if [ "$RESTORE2_RC" -eq 0 ] && ! grep -q "pg_restore: error" "$WORK/restore-other.log"; then
  ok "restored into a fresh database with no pg_restore errors"
else
  bad "the $OTHER_SCOPE-scope restore exited $RESTORE2_RC — see $WORK/restore-other.log"
fi

run_verify "$WORK/verify-other.log" neoh_restore_other "$MANIFEST2"
VERIFY2_RC=$?
if [ "$VERIFY2_RC" -eq 0 ] && grep -q "Row reconciliation: EXHAUSTIVE — all" "$WORK/verify-other.log"; then
  ok "validator passed and reconciled every table exhaustively"
else
  bad "validator did not pass the $OTHER_SCOPE-scope restore exhaustively"
  grep "  FAIL  " "$WORK/verify-other.log" | head -4 | sed 's/^/        /'
fi
LEAKS2=0
for secret in "$PGPW" PGPASSWORD api_key secret_key access_key BEGIN_PRIVATE; do
  grep -qi -- "$secret" "${MANIFEST2:-/dev/null}" "$WORK/backup-other.log" 2>/dev/null && LEAKS2=1
done
[ "$LEAKS2" -eq 0 ] && ok "no secret in its manifest or log" || bad "the $OTHER_SCOPE-scope manifest or log leaks a secret"
GOT2_WRITES="$(docker exec "$DST" psql -U postgres -d neoh_restore_other -tAX -c "SELECT count(*) FROM public.$WRITES;" 2>/dev/null)"
check "rows written during the $OTHER_SCOPE backup" "${M2_WRITES:-?}" "$GOT2_WRITES"
if [ "$OTHER_SCOPE" = "authoritative" ]; then
  WANT_LEADS="$(sed -n 's/^[[:space:]]*"expected_leads":[[:space:]]*"\([0-9][0-9]*\)".*/\1/p' "${MANIFEST2:-/dev/null}" | head -1)"
  if [ "${WANT_LEADS:-0}" -ge 1 ] \
     && grep -q "  PASS  public.leads: $WANT_LEADS row(s), the authoritative extract's exact count" "$WORK/verify-other.log"; then
    ok "leads came back through the in-snapshot extract: exactly its $WANT_LEADS row(s)"
  else
    bad "leads did not come back as the extract's ${WANT_LEADS:-unknown} row(s)"
  fi
fi

# ── 12. Summary ────────────────────────────────────────────────────────────

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
