#!/usr/bin/env bash
# Rehearse new migrations the way staging/production apply them: as a
# NOSUPERUSER, BYPASSRLS table owner (DigitalOcean's doadmin), not as the
# superuser local dev and CI use. Superuser-only SQL (a function `SET app.x`
# clause, ALTER SYSTEM, session_replication_role) passes every local test and
# fails the PRE_DEPLOY job — this is the check that catches it.
#
# Usage: scripts/rehearse-migrations-nosuperuser.sh <first-new-migration-number>
#   e.g. scripts/rehearse-migrations-nosuperuser.sh 0151
# Applies every migration below that number as a throwaway superuser on a
# scratch database, hands ownership to a NOSUPERUSER BYPASSRLS CREATEROLE role,
# then runs run_migrations.py as that role over the full directory. Drops the
# scratch database and both roles on exit. Local dev DB in DinD only.
set -uo pipefail
FIRST="${1:?first new migration number, e.g. 0151}"
export DOCKER_HOST="${DOCKER_HOST:-unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"          # must be under a path DinD can mount
PART="$BACKEND/.rehearsal_migrations"
DB="rehearse_$(date +%s)"
PW="$(head -c 18 /dev/urandom | base64 | tr -dc A-Za-z0-9)"
SU="rh_su_$$"; OW="rh_ow_$$"
IMAGE="${ORACLE_REHEARSAL_IMAGE:-registry.digitalocean.com/neoh-registry/neoh-backend:b810caf}"
psqlc() { docker exec -i oracle-db-1 psql -U postgres -d "${2:-oracle}" -v ON_ERROR_STOP=1 -qAtc "$1"; }
cleanup() {
  psqlc "DROP DATABASE IF EXISTS $DB WITH (FORCE)" >/dev/null 2>&1
  for r in $SU $OW; do psqlc "DROP ROLE IF EXISTS $r" >/dev/null 2>&1; done
  rm -rf "$PART"
}
trap cleanup EXIT
psqlc "CREATE ROLE $SU LOGIN SUPERUSER PASSWORD '$PW'"
psqlc "CREATE ROLE $OW LOGIN NOSUPERUSER BYPASSRLS CREATEROLE CREATEDB PASSWORD '$PW'"
mkdir -p "$PART"
for f in "$BACKEND"/db/migrations/*.sql; do
  n="$(basename "$f")"; n="${n%%_*}"
  if [ "$n" \< "$FIRST" ]; then cp "$f" "$PART/"; fi
done
run() { # user, migrations dir (inside the container)
  docker run --rm --network oracle_default -v "$BACKEND":/app -w /app \
    -e ORACLE_DB_HOST=db -e ORACLE_DB_NAME="$DB" -e ORACLE_DB_SSL=disable -e ORACLE_ENV=dev \
    -e ORACLE_SKIP_DOTENV=1 -e ORACLE_DB_ADMIN_USER="$1" -e ORACLE_DB_ADMIN_PASSWORD="$PW" \
    -e ORACLE_MIGRATIONS_DIR="$2" -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" \
    python run_migrations.py 2>&1 | grep -v "recorded, skip"
}
psqlc "CREATE DATABASE $DB OWNER $SU"   # REASSIGN OWNED hands the database itself to the owner, as on DigitalOcean
echo "== migrations below $FIRST as a superuser"
run "$SU" /app/.rehearsal_migrations | tail -2
psqlc "REASSIGN OWNED BY $SU TO $OW" "$DB"
psqlc "DROP OWNED BY $SU" "$DB"
echo "== owner: $(psqlc "SELECT 'super='||rolsuper||' bypassrls='||rolbypassrls FROM pg_roles WHERE rolname='$OW'")"
echo "== $FIRST and later as the NOSUPERUSER owner"
out="$(run "$OW" /app/db/migrations)"; echo "$out" | tail -12
echo "$out" | grep -qiE "error|failed|permission denied" && { echo "REHEARSAL FAILED"; exit 1; }
echo "REHEARSAL OK"
