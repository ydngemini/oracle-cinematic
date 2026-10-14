#!/usr/bin/env bash
# Run live DB tests against the local dev DB with throwaway roles (dropped on exit).
# Usage: livetest.sh migrate | livetest.sh <cmd...>   (BACKEND_DIR overrides the mounted backend)
set -uo pipefail
export DOCKER_HOST=unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock
B=${BACKEND_DIR:-/media/ydn/SYPHER_CORE2/Oracle/backend}
PW=$(head -c 18 /dev/urandom | base64 | tr -dc A-Za-z0-9)
S=$(date +%s%N | cut -c1-13); R=lt_admin_$S; A=lt_app_$S; P=lt_plat_$S
psqlc(){ docker exec -i oracle-db-1 psql -U postgres -d oracle -v ON_ERROR_STOP=1 -qc "$1" >/dev/null; }
psqlc "CREATE ROLE $R LOGIN SUPERUSER PASSWORD '$PW'; CREATE ROLE $A LOGIN NOSUPERUSER NOBYPASSRLS INHERIT PASSWORD '$PW' IN ROLE oracle_app; CREATE ROLE $P LOGIN NOSUPERUSER NOBYPASSRLS INHERIT PASSWORD '$PW' IN ROLE oracle_app, platform_admin_role"
cleanup(){ for x in $R $A $P; do psqlc "REASSIGN OWNED BY $x TO postgres; DROP OWNED BY $x; DROP ROLE $x"; done; }
trap cleanup EXIT
ENVS=(-e ORACLE_LIVE_DB_ADMIN_DSN="postgresql://$R:$PW@db:5432/oracle"
  -e ORACLE_DB_HOST=db -e ORACLE_DB_NAME=oracle -e ORACLE_DB_SSLMODE=disable -e ORACLE_DB_SSL=disable -e ORACLE_ENV=dev
  -e ORACLE_SKIP_DOTENV=1 -e ORACLE_DB_USER=$A -e ORACLE_DB_PLATFORM_USER=$P -e ORACLE_DB_PASSWORD=$PW
  -e ORACLE_DB_ADMIN_USER=$R -e ORACLE_DB_ADMIN_PASSWORD=$PW -e ORACLE_MIGRATIONS_DIR=/app/db/migrations
  -e PYTHONDONTWRITEBYTECODE=1 -e ORACLE_AUDIT_SQLITE=/tmp/audit_logs.db)
if [ "${1:-}" = migrate ]; then set -- python run_migrations.py; fi
docker run --rm --network oracle_default -v "$B":/app -w /app "${ENVS[@]}" \
  registry.digitalocean.com/neoh-registry/neoh-backend:b810caf "$@"
