#!/bin/sh
# Worker crash mid-job against the LOCAL perf topology (worker_crash_drill.py).
#   NEOH_LOAD_TEST_ALLOWED=1 performance/resilience/run_worker_crash_drill.sh <perf env file> [N]
set -eu
[ "${NEOH_LOAD_TEST_ALLOWED:-}" = 1 ] || { echo "REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required" >&2; exit 1; }
P=/media/ydn/SYPHER_CORE2/Oracle
export DOCKER_HOST=${DOCKER_HOST:-unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock}
ENV_FILE="$1"; N=${2:-40}; RUN=crash$(date -u +%H%M%S)
OUT=$P/performance/out/drills/worker-crash-$RUN.jsonl
drill() { docker run --rm --network oracle_default --env-file "$ENV_FILE" -e NEOH_LOAD_TEST_ALLOWED=1 -e DRILL_RUN="$RUN" \
  -v "$P/backend:/app:ro" -v "$P/performance:/perf:ro" -w /app oracle-backend-perf:latest \
  python /perf/resilience/worker_crash_drill.py "$@" 2>/dev/null | grep '^{'; }
t0=$(date +%s)
echo "{\"t\":0,\"event\":\"enqueue\",\"result\":$(drill enqueue "$N")}" | tee "$OUT"
sleep 6
docker kill oracle-perf-worker >/dev/null
echo "{\"t\":$(( $(date +%s) - t0 )),\"event\":\"killed\",\"status\":$(drill status)}" | tee -a "$OUT"
sleep 5
docker start oracle-perf-worker >/dev/null
echo "{\"t\":$(( $(date +%s) - t0 )),\"event\":\"restarted\"}" | tee -a "$OUT"
while [ $(( $(date +%s) - t0 )) -lt 420 ]; do
  s=$(drill status)
  echo "{\"t\":$(( $(date +%s) - t0 )),\"event\":\"poll\",\"status\":$s}" | tee -a "$OUT"
  echo "$s" | python3 -c "import json,sys;d=json.load(sys.stdin)['states'];sys.exit(0 if set(d)<={'succeeded','partial','cancelled','dead_letter'} else 1)" && break
  sleep 15
done
