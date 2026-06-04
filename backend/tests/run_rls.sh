#!/usr/bin/env bash
# Runtime-test the Postgres RLS layer (migrations 0001 + 0002) in a throwaway
# postgres:16 container. Applies the migrations, seeds cross-tenant data, then
# runs tests/rls_test.sql whose assertions execute as the non-superuser
# `oracle_app` role so RLS actually binds.
#
# 0003_hardening.sql is intentionally NOT applied: it depends on the pgaudit
# extension and the `rds_iam` grant, which exist only on AWS Aurora. The RLS
# correctness proof lives entirely in 0001 + 0002.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIG="$HERE/../db/migrations"
IMG="postgres:16-alpine"
CONTAINER="oracle_rls_test_$$"
DB="oracle"

# The docker socket is root-owned; if the current user isn't in the `docker`
# group, fall back to sudo (will prompt when run interactively).
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1; then
        echo "note: docker socket needs root — using 'sudo docker' (may prompt)"
        DOCKER="sudo docker"
    fi
fi
if ! $DOCKER info >/dev/null 2>&1; then
    echo "FAIL: cannot reach the docker daemon. Either add yourself to the" >&2
    echo "      docker group ('sudo usermod -aG docker \$USER' then re-login)" >&2
    echo "      or run this script with sudo." >&2
    exit 2
fi

cleanup() { $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> starting $IMG as $CONTAINER"
$DOCKER run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB="$DB" \
    "$IMG" >/dev/null

echo -n "==> waiting for postgres"
for _ in $(seq 1 30); do
    if $DOCKER exec "$CONTAINER" pg_isready -U postgres -d "$DB" >/dev/null 2>&1; then
        echo " ready"; break
    fi
    echo -n "."; sleep 1
done

psql() { $DOCKER exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DB" "$@"; }

echo "==> applying 0001_init_tenancy.sql"
psql -q < "$MIG/0001_init_tenancy.sql"
echo "==> applying 0002_listing_grants.sql"
psql -q < "$MIG/0002_listing_grants.sql"

echo "==> running RLS assertions (as role oracle_app)"
psql < "$HERE/rls_test.sql"

echo "==> RLS layer: PASS"
