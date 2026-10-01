#!/bin/sh
# Production-shaped local topology for load tests. Runs INSIDE the DinD host
# (oracle-sypher-docker), on the compose network, against the same database:
#
#   oracle-perf-web-1   ORACLE_PROCESS_ROLE=web      no --reload, like App Platform
#   oracle-perf-web-2   ORACLE_PROCESS_ROLE=web
#   oracle-perf-worker  ORACLE_PROCESS_ROLE=worker   the single background worker
#   oracle-perf-lb      nginx round-robin over both web replicas (+ WebSockets)
#   oracle-perf-valkey  Valkey, as DO Managed Valkey: shared rate-limit state
#                       across replicas. PERF_NO_VALKEY=1 omits it, to measure
#                       the PostgreSQL-window fallback the design specifies.
#
# Every process runs with ORACLE_ENV=loadtest and ORACLE_RECOVERY_MODE=1: no
# provider egress, no SMTP, no Stripe, no scheduler — the DR kill switch. The
# Stripe key is blanked and ORACLE_ALLOW_LIVE_STRIPE dropped, because the dev
# env carries a LIVE key and the override that permits it.
#
# Usage (from the host):
#   docker exec -i -e PERF_GIT_SHA=$(git rev-parse HEAD) oracle-sypher-docker sh -s < performance/topology/up.sh
set -eu

NET=oracle_default
# Built by topology/image (backend image + current requirements.txt).
IMAGE=oracle-backend-perf:latest
SRC=/media/ydn/SYPHER_CORE2/Oracle
ENVF=/tmp/neoh-perf.env
SHA="${PERF_GIT_SHA:-unknown}"

# The dev env is cloned for its CONFIGURATION, never its credentials. Recovery
# mode blocks side effects (SMS, calls, email, billing) but NOT model calls,
# realtime voice sessions or GPU dispatch — and the dev env carries live keys
# for all of them. So every provider credential is dropped here, by pattern,
# and replaced below with perf-only secrets and the local provider mock.
# Kept: the database login, and this environment's own signing keys.
KEEP='^(ORACLE_DB_PASSWORD|ORACLE_DB_APP_PASSWORD|ORACLE_SECRET_KEY|ORACLE_ENCRYPTION_MASTER_KEY)$'
DROP='(KEY|SECRET|TOKEN|PASSWORD|_SID|_AUTH_ID|CONNECTION_STRING|^STRIPE_|^TELNYX_|^PLIVO_|^TWILIO_|^ACS_|^ORACLE_SMTP_|^RUNPOD|^ELEVENLABS|^DASHSCOPE|FIREWORKS|^AZURE_|BEDROCK|^AWS_|^REGRID|^RENTCAST|^WALKSCORE|^FRED_|GEMINI|OPENAI|^VITE_|^ONCOMPUTE|^RECON_|^RECONSTRUCTION_|^ORACLE_LOCAL_LLM|^ORACLE_PLIVO|^ORACLE_PUBLIC_BASE_URL|^ORACLE_S3_|^ORACLE_STORAGE_BACKEND|^REDIS_URL$|^ORACLE_ENV$|^ORACLE_PROCESS_ROLE$|^ORACLE_RECOVERY_MODE$|^ORACLE_ALLOW_LIVE_STRIPE$|^ORACLE_GIT_SHA$|^ORACLE_SCHEDULER_ENABLED$)'
docker inspect oracle-backend-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | awk -v keep="$KEEP" -v drop="$DROP" 'index($0, "=") { n = substr($0, 1, index($0, "=") - 1); if (n ~ keep || n !~ drop) print }' \
  > "$ENVF"

# Perf-only secrets and mock endpoints (fixtures/perf_secrets.py writes them;
# the webhook fixtures are signed with the same values).
SECRETS_ENV=$SRC/performance/out/perf-secrets.env
if [ ! -f "$SECRETS_ENV" ]; then
  echo "missing $SECRETS_ENV — run: python3 performance/fixtures/perf_secrets.py" >&2
  exit 1
fi
cat "$SECRETS_ENV" >> "$ENVF"

docker rm -fv oracle-perf-valkey >/dev/null 2>&1 || true
if [ "${PERF_NO_VALKEY:-0}" != 1 ]; then
  # No persistence, bounded memory: it holds rate windows and cache, never
  # durable state — exactly the role Managed Valkey plays in production.
  docker run -d --name oracle-perf-valkey --network "$NET" --memory 256m \
    valkey/valkey:8.1-alpine valkey-server --save '' --appendonly no \
    --maxmemory 200mb --maxmemory-policy allkeys-lru >/dev/null
  echo "started oracle-perf-valkey"
  echo "REDIS_URL=redis://oracle-perf-valkey:6379/0" >> "$ENVF"
fi

# The markers the guard and every provider path read. Recovery mode is the DR
# kill switch: no SMS, calls, email, billing or scheduler.
cat >> "$ENVF" <<ENV
ORACLE_ENV=loadtest
ORACLE_RECOVERY_MODE=1
ORACLE_CORS_ORIGINS=http://localhost:5173
ORACLE_BILLING_ENFORCED=0
ORACLE_GIT_SHA=$SHA
ENV
# Per-run overrides for capacity experiments, e.g.
#   PERF_EXTRA_ENV="ORACLE_WS_MAX_CONNECTIONS=1000;ORACLE_JOB_WORKERS=8"
# Recorded in the run's environment so a result says what it ran with.
if [ -n "${PERF_EXTRA_ENV:-}" ]; then
  printf '%s\n' "$PERF_EXTRA_ENV" | tr ';' '\n' | grep -E '^[A-Z0-9_]+=' >> "$ENVF"
  echo "extra env: $PERF_EXTRA_ENV"
fi
chmod 600 "$ENVF"

docker volume create oracle_perf_media >/dev/null

start() {
  name=$1 role=$2
  docker rm -fv "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network "$NET" --env-file "$ENVF" \
    -e ORACLE_PROCESS_ROLE="$role" \
    -v "$SRC/backend:/app:ro" \
    -v "$SRC/performance:/perf:ro" \
    -v oracle_perf_media:/var/neoh-media \
    --memory 900m \
    "$IMAGE" \
    uvicorn server:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 30 --ws-max-size 1048576 \
      --workers "$( [ "$role" = web ] && echo "${PERF_WEB_PROCESSES:-1}" || echo 1 )" >/dev/null
  echo "started $name ($role)"
}
start oracle-perf-web-1 web
start oracle-perf-web-2 web
start oracle-perf-worker worker

# Local provider mock: OpenAI-compatible chat completions and a DashScope-shaped
# realtime socket, with configurable latency and failure (mocks/provider_mock.py).
docker rm -fv oracle-perf-mock >/dev/null 2>&1 || true
docker run -d --name oracle-perf-mock --network "$NET" --memory 256m \
  -e MOCK_LLM_LATENCY_MS="${MOCK_LLM_LATENCY_MS:-2500}" -e MOCK_LLM_JITTER_MS="${MOCK_LLM_JITTER_MS:-1500}" \
  -v "$SRC/performance:/perf:ro" -w /perf/mocks \
  "$IMAGE" uvicorn provider_mock:app --host 0.0.0.0 --port 9000 --log-level warning >/dev/null
echo "started oracle-perf-mock"

docker rm -fv oracle-perf-lb >/dev/null 2>&1 || true
docker run -d --name oracle-perf-lb --network "$NET" \
  -v "$SRC/performance/topology/nginx.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.30.4-alpine >/dev/null
echo "started oracle-perf-lb"

for i in $(seq 1 60); do
  ok=0
  for n in oracle-perf-web-1 oracle-perf-web-2; do
    docker exec "$n" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2)" >/dev/null 2>&1 && ok=$((ok+1))
  done
  [ "$ok" = 2 ] && break
  sleep 3
done
echo "healthy web replicas: $ok/2"
