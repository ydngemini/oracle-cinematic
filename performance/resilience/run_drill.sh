#!/bin/sh
# One controlled failure drill against the LOCAL perf topology (never staging
# or production). Starts the probe, injects a fault at +INJECT_AT s, restores
# it after HOLD s, keeps probing RECOVER s more, then summarizes.
#
#   NEOH_LOAD_TEST_ALLOWED=1 performance/resilience/run_drill.sh db_pause
#
# Drills: db_pause db_stop db_lock conn_reset valkey_stop llm_timeout llm_429
#         llm_500 web_restart worker_kill
# PERF_ENV=<env file> also samples per-component health (detection per component);
# llm_* drills also send real chat turns (chat_probe.py) while the fault is in.
set -eu
[ "${NEOH_LOAD_TEST_ALLOWED:-}" = 1 ] || { echo "REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required" >&2; exit 1; }
DRILL="$1"
P=/media/ydn/SYPHER_CORE2/Oracle
export DOCKER_HOST=${DOCKER_HOST:-unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock}
INJECT_AT=${INJECT_AT:-15}; HOLD=${HOLD:-60}; RECOVER=${RECOVER:-60}
TOTAL=$((INJECT_AT + HOLD + RECOVER))
OUT=$P/performance/out/drills; mkdir -p "$OUT"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN="$OUT/$DRILL-$STAMP"
mark() { echo "{\"wall\": $(date +%s.%N | cut -c1-14), \"mark\": \"$1\"}" >> "$RUN.marks.jsonl"; }
mock() { docker exec oracle-perf-lb wget -qO- --header 'Content-Type: application/json' --post-data "$1" http://oracle-perf-mock:9000/config >/dev/null; }
psql_db() { docker exec -i oracle-db-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$0"' "$1"; }

docker run -d --rm --name "drill-probe-$DRILL" --network oracle_default \
  -e NEOH_LOAD_TEST_ALLOWED=1 -e PROBE_SECONDS="$TOTAL" \
  -v "$P/performance:/perf:ro" oracle-backend-perf:latest python /perf/resilience/probe.py > /dev/null
( docker logs -f "drill-probe-$DRILL" > "$RUN.jsonl" 2>&1 & )
if [ -n "${PERF_ENV:-}" ]; then
  docker run -d --rm --name "drill-comp-$DRILL" --network oracle_default --env-file "$PERF_ENV" \
    -e PROBE_SECONDS="$TOTAL" -v "$P/backend:/app:ro" -v "$P/performance:/perf:ro" -w /app \
    oracle-backend-perf:latest python /perf/resilience/components_loop.py > /dev/null
  ( docker logs -f "drill-comp-$DRILL" 2>/dev/null | grep --line-buffered '^{' > "$RUN.components.jsonl" & )
fi
mark start
sleep "$INJECT_AT"

mark inject
case "$DRILL" in
  db_pause)       docker pause oracle-db-1 ;;
  db_stop)        docker stop -t 5 oracle-db-1 >/dev/null ;;
  valkey_stop)    docker stop -t 2 oracle-perf-valkey >/dev/null ;;
  llm_timeout)    mock '{"llm_fail":"timeout"}' ;;
  llm_429)        mock '{"faults":[{"match":"/v1/chat","mode":"429","retry_after":20}]}' ;;
  llm_500)        mock '{"llm_fail":"500"}' ;;
  conn_reset)     psql_db "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename IN ('oracle_app_login','oracle_platform_login')" ;;
  db_lock)        ( docker exec -i oracle-db-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q' >/dev/null 2>&1 <<SQL
BEGIN; LOCK TABLE clients IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($HOLD); COMMIT;
SQL
                  ) & ;;
  web_restart)    docker restart -t 5 oracle-perf-web-1 >/dev/null ;;
  worker_kill)    docker kill oracle-perf-worker >/dev/null ;;
  *) echo "unknown drill $DRILL" >&2; exit 2 ;;
esac
case "$DRILL" in
  llm_*) docker run --rm --network oracle_default -e NEOH_LOAD_TEST_ALLOWED=1 -e CHAT_TURNS=2 \
           -e CHAT_TURN_TIMEOUT=$((HOLD - 5)) -v "$P/performance:/perf:ro" oracle-backend-perf:latest \
           python /perf/resilience/chat_probe.py > "$RUN.chat.jsonl" 2>&1 & ;;
esac
sleep "$HOLD"

mark restore
case "$DRILL" in
  db_pause)     docker unpause oracle-db-1 ;;
  db_stop)      docker start oracle-db-1 >/dev/null ;;
  valkey_stop)  docker start oracle-perf-valkey >/dev/null ;;
  llm_*)        mock '{"llm_fail":"","faults":[]}' ;;
  worker_kill)  docker start oracle-perf-worker >/dev/null ;;
  *) : ;;
esac
sleep "$RECOVER"
mark end
sleep 3
python3 "$P/performance/resilience/analyze.py" "$RUN"
