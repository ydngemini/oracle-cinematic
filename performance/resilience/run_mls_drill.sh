#!/bin/sh
# MLS failure drill against the local perf topology's mock feed.
#   NEOH_LOAD_TEST_ALLOWED=1 performance/resilience/run_mls_drill.sh <perf env file>
set -eu
[ "${NEOH_LOAD_TEST_ALLOWED:-}" = 1 ] || { echo "REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required" >&2; exit 1; }
P=/media/ydn/SYPHER_CORE2/Oracle
export DOCKER_HOST=${DOCKER_HOST:-unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock}
mkdir -p "$P/performance/out/drills"
docker run --rm --network oracle_default --env-file "$1" -e NEOH_LOAD_TEST_ALLOWED=1 \
  -e ORACLE_ENV=test -e ORACLE_RECOVERY_MODE=0 -e ORACLE_INGEST_TENANT_ID=00000000-0000-0000-0000-000000000000 \
  -v "$P/backend:/app:ro" -v "$P/performance:/perf:ro" -w /app \
  oracle-backend-perf:latest python /perf/resilience/mls_drill.py | tee /dev/stderr \
  | sed -n 's/^SUMMARY //p' | python3 -m json.tool > "$P/performance/out/drills/mls-drill.summary.json"
