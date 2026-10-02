#!/bin/sh
# Run a performance/ Python tool INSIDE the perf network, with the perf env
# (stripped credentials, mock providers) and the backend modules on the path.
#   NEOH_LOAD_TEST_ALLOWED=1 performance/pyrun.sh voice_sim.py --calls 25
set -eu
P=/media/ydn/SYPHER_CORE2/Oracle
[ "${NEOH_LOAD_TEST_ALLOWED:-}" = 1 ] || { echo "REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required" >&2; exit 1; }
docker exec -i oracle-sypher-docker docker run --rm --network oracle_default \
  --env-file "${PERF_ENV_FILE:-/tmp/neoh-perf.env}" \
  -e NEOH_LOAD_TEST_ALLOWED=1 -e PERF_FIXTURE_OUT=/out -e PGPASSWORD="${PGPASSWORD:-postgres}" \
  -e PERF_DB_DSN=postgresql://postgres@db:5432/oracle \
  -v "$P/backend:/app:ro" -v "$P/performance:/perf:ro" -v "$P/performance/out:/out" \
  -w /app oracle-backend-perf:latest python "/perf/$@"
