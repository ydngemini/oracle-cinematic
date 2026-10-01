#!/bin/sh
# Re-mint the load-test sessions (24 h JWTs) against the perf environment's own
# secret. Runs a throwaway container from the backend image with the perf env.
#   docker exec -i oracle-sypher-docker sh -s < performance/fixtures/mint.sh
set -eu
SRC=/media/ydn/SYPHER_CORE2/Oracle
# The password travels as PGPASSWORD (asyncpg reads it), never inside the DSN,
# so no character in it needs URL-escaping and it never appears in `ps`.
DSN="postgresql://postgres@db:5432/oracle"
docker run --rm --network oracle_default --env-file /tmp/neoh-perf.env \
  ${PGPASSWORD:+-e PERF_DB_DSN="$DSN" -e PGPASSWORD} -e PERF_FIXTURE_OUT=/out \
  -v "$SRC/backend:/app:ro" -v "$SRC/performance:/perf:ro" -v "$SRC/performance/out:/out" \
  oracle-backend-perf:latest python /perf/fixtures/sessions.py
