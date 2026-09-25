#!/usr/bin/env bash
#
# Neoh — refuse to migrate a database that is not the one this release expects.
#
# Runs BEFORE production migrations, and aborts on drift rather than reporting
# it. A migration applied to an unexpected schema is not a failed deploy; it is
# a corrupted database plus a failed deploy, and the second is much easier to
# recover from than the first.
#
# It checks the things that are cheap to check and expensive to get wrong:
#
#   * the ledger is readable at all
#   * every migration recorded as applied still exists in this release
#   * every checksummed migration still hashes to what was recorded
#   * the target head is reachable from where the database actually is
#   * the admin credential works, before anything needs it
#
# The second check is not hypothetical. This repository's own local database
# records `0106_brokerage_setup.sql` as applied, and that file has never
# existed in git — it was renumbered to `0109_brokerage_onboarding.sql` before
# being committed, and the ledger row was left behind. Harmless there. On a
# production database the same signal means something applied a migration that
# this release does not contain, and continuing would stack a new migration on
# a schema nobody can reproduce.
#
# Usage:
#   ORACLE_DB_HOST=... ORACLE_DB_ADMIN_USER=... PGPASSWORD=... \
#     scripts/migration-precheck.sh [release-manifest.json]
#
# Exit 0 = safe to migrate. Non-zero = do not migrate; the output says why.

set -uo pipefail

MANIFEST="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
MIGRATIONS="$REPO/backend/db/migrations"

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }

: "${ORACLE_DB_HOST:?ORACLE_DB_HOST is required — never guess a migration target}"
: "${ORACLE_DB_NAME:?ORACLE_DB_NAME is required}"
: "${ORACLE_DB_ADMIN_USER:?ORACLE_DB_ADMIN_USER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

DB_PORT="${ORACLE_DB_PORT:-5432}"
export PGSSLMODE="${ORACLE_DB_SSLMODE:-require}"
Q=(psql -h "$ORACLE_DB_HOST" -p "$DB_PORT" -U "$ORACLE_DB_ADMIN_USER" -d "$ORACLE_DB_NAME" -tAX)

echo
echo "Migration precheck against $ORACLE_DB_HOST:$DB_PORT/$ORACLE_DB_NAME"
echo

# ── 1. The credential works, before anything depends on it ─────────────────

if "${Q[@]}" -c "SELECT 1;" >/dev/null 2>&1; then
  ok "admin credential connects"
else
  bad "cannot connect as $ORACLE_DB_ADMIN_USER — check host, credential and TLS"
  echo; echo "  Aborting: nothing below can be checked."; exit 1
fi

# ── 2. The ledger exists and is readable ───────────────────────────────────

LEDGER_ROWS="$("${Q[@]}" -c "SELECT count(*) FROM schema_migrations;" 2>/dev/null)"
if [ -z "$LEDGER_ROWS" ]; then
  bad "no readable schema_migrations table — this database has never been
        migrated by the runner, or is not a Neoh database at all"
  echo; exit 1
fi
ok "ledger readable — $LEDGER_ROWS migration(s) recorded"

CURRENT_HEAD="$("${Q[@]}" -c "SELECT coalesce(max(filename),'(none)') FROM schema_migrations;" 2>/dev/null)"
RELEASE_HEAD="$(ls "$MIGRATIONS"/*.sql 2>/dev/null | xargs -n1 basename | sort | tail -1)"
RELEASE_COUNT="$(ls "$MIGRATIONS"/*.sql 2>/dev/null | wc -l | tr -d ' ')"

echo "        database head: $CURRENT_HEAD"
echo "        release head:  $RELEASE_HEAD  ($RELEASE_COUNT files)"

# ── 3. Nothing is recorded that this release does not contain ──────────────
#
# The check that catches "something else migrated this database". A row naming
# a file this release has never heard of means the schema was shaped by code
# outside this repository, and the next migration would stack on a state
# nobody can reproduce.

ls "$MIGRATIONS"/*.sql 2>/dev/null | xargs -n1 basename | sort > /tmp/.precheck_files.$$
"${Q[@]}" -c "SELECT filename FROM schema_migrations ORDER BY filename;" 2>/dev/null \
  | sed '/^$/d' > /tmp/.precheck_ledger.$$

UNKNOWN="$(comm -13 /tmp/.precheck_files.$$ /tmp/.precheck_ledger.$$)"
if [ -n "$UNKNOWN" ]; then
  bad "the database records migration(s) this release does not contain:"
  printf '        %s\n' $UNKNOWN
  echo "        Something outside this repository has migrated this database."
else
  ok "every recorded migration exists in this release"
fi

# ── 4. The target head is ahead of where the database is ───────────────────
#
# Not merely different — AHEAD. Deploying a release whose migration head is
# BEHIND the database means rolling the schema backwards, which migrations
# cannot do, and is the signature of deploying an old artifact by mistake.

PENDING="$(comm -23 /tmp/.precheck_files.$$ /tmp/.precheck_ledger.$$ | wc -l | tr -d ' ')"
if [ "$CURRENT_HEAD" = "(none)" ]; then
  warn "the database has no migrations at all — this is a first deploy, or the
        wrong database. $PENDING migration(s) would be applied."
elif [ "$RELEASE_HEAD" \< "$CURRENT_HEAD" ]; then
  bad "the release head ($RELEASE_HEAD) is BEHIND the database head
        ($CURRENT_HEAD). This artifact is older than the schema it would run
        against — almost certainly the wrong release."
elif [ "$RELEASE_HEAD" = "$CURRENT_HEAD" ]; then
  ok "schema is already at the release head — $PENDING migration(s) pending"
else
  ok "release head is ahead — $PENDING migration(s) to apply"
fi

# ── 5. No historical migration has been edited ─────────────────────────────
#
# The ledger stores a sha256 per migration. An applied migration whose file now
# hashes differently has been edited after the fact, which means the database
# and the repository disagree about what was run — and every environment
# migrated later gets the new version while this one kept the old.

DRIFTED=0
while IFS='|' read -r fname recorded_sha; do
  [ -n "$fname" ] || continue
  [ -n "$recorded_sha" ] || continue
  path="$MIGRATIONS/$fname"
  [ -f "$path" ] || continue
  actual="$(sha256sum "$path" | cut -d' ' -f1)"
  if [ "$actual" != "$recorded_sha" ]; then
    bad "$fname was EDITED after it was applied"
    echo "          recorded $recorded_sha"
    echo "          on disk  $actual"
    DRIFTED=$((DRIFTED+1))
  fi
done < <("${Q[@]}" -F'|' -c "SELECT filename, sha256 FROM schema_migrations WHERE sha256 IS NOT NULL;" 2>/dev/null)
[ "$DRIFTED" -eq 0 ] && ok "no applied migration has been edited"

# ── 6. Recovery readiness ──────────────────────────────────────────────────
#
# Advisory rather than blocking: this cannot see the operator's bucket. It
# refuses to stay silent, because "we had backups" is discovered to be false
# only ever at the worst moment.

if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
  ok "release manifest supplied: $(basename "$MANIFEST")"
else
  warn "no release manifest supplied — the deploy cannot later prove which
        artifact this migration ran for"
fi
if [ "$PENDING" -gt 0 ]; then
  warn "$PENDING migration(s) will be applied. Confirm a current backup exists
        (scripts/backup-postgres.sh) — DigitalOcean's own PITR window is 7 days
        and its backups cannot be downloaded."
fi

rm -f /tmp/.precheck_files.$$ /tmp/.precheck_ledger.$$

echo
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32m%s checks passed. Safe to migrate.\033[0m\n\n' "$PASS"
  exit 0
fi
printf '  \033[31m%s passed, %s FAILED. DO NOT MIGRATE.\033[0m\n\n' "$PASS" "$FAIL"
exit 1
