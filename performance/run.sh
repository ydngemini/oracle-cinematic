#!/bin/sh
# Run one k6 scenario against the local perf topology, then analyze it.
#
#   NEOH_LOAD_TEST_ALLOWED=1 performance/run.sh <scenario> [extra k6 args...]
#   e.g.  VUS=75 LABEL=75vu NEOH_LOAD_TEST_ALLOWED=1 performance/run.sh read_load
#
# The guard runs FIRST and fails closed: it refuses unless NEOH_LOAD_TEST_ALLOWED
# is set, the target is not a known production host, and the target's own
# /version reports a staging or loadtest environment.
#
# Scenario tuning is passed as k6 env vars: any variable in PASS_ENV below that
# is set in the caller's environment is forwarded to k6.
#
# Leaves, per run, in performance/out/results/:
#   <run>.csv.gz          every k6 sample
#   <run>.analysis.json   per-endpoint latency + replica distribution
#   <run>.resources.jsonl container CPU/memory + DB connections, every 5 s
#   <run>.result.json     the combined artifact (§70) with the PASS/FAIL verdict
set -eu
SCEN="$1"; shift
P=/media/ydn/SYPHER_CORE2/Oracle
R="$P/performance/out/results"
DIND=oracle-sypher-docker
TARGET="${TARGET:-http://oracle-perf-lb:8080}"
PASS_ENV="BROWSE WRITE SOCKETS CHAT MODE TURN_TIMEOUT STORM RAMP ORIGIN VUS DURATION RATE THINK_MIN THINK_MAX CONNS HOLD MSGS STAGES BURST AI_PROVIDER AI_REAL_MAX_REQUESTS NEOH_REAL_AI_ALLOWED LIMIT_PROBE"
rc=0
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN="${SCEN}${LABEL:+-$LABEL}-${STAMP}"
mkdir -p "$R"

# The guard runs where the target is reachable: inside the perf network.
docker exec -i "$DIND" docker exec -e NEOH_LOAD_TEST_ALLOWED="${NEOH_LOAD_TEST_ALLOWED:-}" \
  -e NEOH_PRODUCTION_HOSTS="${NEOH_PRODUCTION_HOSTS:-}" \
  oracle-perf-worker python /perf/guard.py "$TARGET" > "$R/${RUN}.guard.json"

# Preflight: every address the balancer routes to must BE an API replica.
# (A stale upstream once sent half of all traffic to the worker process.)
WEB_IPS=$(docker exec "$DIND" sh -c 'for c in $(docker ps --format "{{.Names}}" | grep oracle-perf-web); do docker inspect "$c" --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"; done' | sort | tr '\n' ' ')
SEEN=$(docker exec -i "$DIND" docker run -i --rm --network oracle_default python:3-alpine python -c "
import urllib.request
print(' '.join(sorted({urllib.request.urlopen('$TARGET/version').headers.get('X-Perf-Upstream','?').split(':')[0] for _ in range(24)})))")
for ip in $SEEN; do
  case " $WEB_IPS" in *" $ip "*) ;; *) echo "REFUSING: balancer routes to $ip, which is not an API replica ($WEB_IPS)" >&2; exit 1;; esac
done

K6ENV=""
for v in $PASS_ENV; do
  eval "val=\${$v:-}"
  [ -n "$val" ] && K6ENV="$K6ENV -e $v=$val"
done

python3 "$P/performance/monitor.py" "$R/${RUN}.resources.jsonl" 5 &
MON=$!
trap 'kill $MON 2>/dev/null || true' EXIT INT TERM

# shellcheck disable=SC2086 # K6ENV is a deliberately word-split list of -e flags
docker exec -i "$DIND" docker run --rm --network oracle_default \
  -v "$P/performance:/perf:ro" -v "$P/performance/out:/out" \
  -e GIT_SHA="$(git -C "$P" rev-parse --short HEAD)" -e TARGET="$TARGET" $K6ENV \
  grafana/k6:0.57.0 run --quiet --out "csv=/out/results/${RUN}.csv.gz" "$@" "/perf/scenarios/${SCEN}.js" || rc=$?

kill $MON 2>/dev/null || true
python3 "$P/performance/monitor.py" --summarize "$R/${RUN}.resources.jsonl" > "$R/${RUN}.resources.json" || true
python3 "$P/performance/analyze.py" "$R/${RUN}.csv.gz" --json "$R/${RUN}.analysis.json" || true
python3 "$P/performance/result.py" "$RUN" "$R/${RUN}.guard.json" "$R/${RUN}.analysis.json" \
  "$R/${RUN}.resources.json" "$rc" $K6ENV "$@"
# A failed run is the one that most needs analyzing: report, THEN fail.
exit $rc
