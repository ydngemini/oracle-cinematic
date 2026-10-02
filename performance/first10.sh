#!/bin/sh
# §65 THE FIRST-10-BROKERAGES RUN — the launch capacity gate's workload.
#
# k6 scenarios/mixed.js (browsing, writes, open tabs, Neoh chat) PLUS, at the
# same time, in the same environment:
#   voice   VOICE_CALLS simultaneous simulated calls, back to back
#   mls     a paced MLS delta sync (MLS_RATE records per batch, every 5 s)
#   jobs    background durable jobs (JOBS_PER_MIN no-op jobs per minute)
#
#   NEOH_LOAD_TEST_ALLOWED=1 DURATION=15m performance/first10.sh
# Soak: DURATION=30m (or longer). The k6 verdict, voice and job results land
# in performance/out/results/ next to each other under one label.
set -eu
P=/media/ydn/SYPHER_CORE2/Oracle
DURATION="${DURATION:-15m}"
VOICE_CALLS="${VOICE_CALLS:-5}"
MLS_RATE="${MLS_RATE:-200}"
JOBS_PER_MIN="${JOBS_PER_MIN:-60}"
LABEL="${LABEL:-first10}"
[ "${NEOH_LOAD_TEST_ALLOWED:-}" = 1 ] || { echo "REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required" >&2; exit 1; }
SECS=$(python3 -c "d='$DURATION';print(int(d[:-1])*(60 if d.endswith('m') else 1))")
END=$(( $(date +%s) + SECS ))
R="$P/performance/out/results"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
START=$(date +%s)

# voice: back-to-back 60 s calls, VOICE_CALLS at a time, until the end
( while [ "$(date +%s)" -lt "$END" ]; do
    sh "$P/performance/pyrun.sh" voice_sim.py --calls "$VOICE_CALLS" --seconds 60 --ramp 5 2>/dev/null | tail -1
  done > "$R/$LABEL-voice-$STAMP.jsonl" ) &
VOICE=$!

# mls: a delta sync paced at MLS_RATE records per 5 s batch, for the duration
( N=$(( SECS / 5 * MLS_RATE )); sh "$P/performance/pyrun.sh" mls_ingest.py --records "$N" --batch "$MLS_RATE" --update --pause 5 \
    > "$R/$LABEL-mls-$STAMP.json" 2>&1 ) &
MLS=$!

# jobs: JOBS_PER_MIN no-op jobs enqueued each minute
( while [ "$(date +%s)" -lt "$END" ]; do
    docker exec oracle-sypher-docker docker exec oracle-db-1 psql -U postgres -d oracle -Atqc "
      INSERT INTO automation_jobs (tenant_id, job_type, state, payload, idempotency_key, created_by, max_attempts)
      SELECT (SELECT id FROM tenants WHERE slug='perf-small-01'), 'loadtest:noop', 'queued',
             jsonb_build_object('key', 'mix-' || extract(epoch from now())::bigint || '-' || g, 'ms', 200),
             'mix-' || extract(epoch from now())::bigint || '-' || g, 'perf-first10', 3
        FROM generate_series(1, $JOBS_PER_MIN) g" >/dev/null
    sleep 60
  done ) &
JOBS=$!

rc=0
DURATION="$DURATION" LABEL="$LABEL" sh "$P/performance/run.sh" mixed || rc=$?
kill "$JOBS" 2>/dev/null || true
wait "$VOICE" 2>/dev/null || true
kill "$MLS" 2>/dev/null || true
echo "voice:"; cat "$R/$LABEL-voice-$STAMP.jsonl"
echo "mls:"; tail -1 "$R/$LABEL-mls-$STAMP.json" || true
docker exec oracle-sypher-docker docker exec oracle-db-1 psql -U postgres -d oracle -Atc "
  SELECT 'jobs', state, count(*), round(avg(extract(epoch from completed_at - created_at))::numeric,1) AS avg_s
    FROM automation_jobs WHERE created_by='perf-first10' AND created_at >= to_timestamp($START) GROUP BY state"
exit $rc
