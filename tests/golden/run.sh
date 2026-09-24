#!/usr/bin/env bash
# One command: bring the stack up if needed, then walk the customer journey.
#
#   tests/golden/run.sh
#   tests/golden/run.sh --keep          # leave seeded data for inspection
#
# Requires the local stack (docker compose). Contacts no provider, sends no
# mail, places no calls, charges nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${DOCKER_HOST:=unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock}"
export DOCKER_HOST
BASE="${GOLDEN_BASE_URL:-http://localhost:8000}"

echo "==> checking the stack"
if ! curl -fsS -o /dev/null "$BASE/health" 2>/dev/null; then
  echo "    API not answering at $BASE — starting the stack"
  (cd "$ROOT" && ORACLE_BIND_HOST=0.0.0.0 ./scripts/dev-start.sh)
  for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$BASE/health" 2>/dev/null && break
    sleep 2
  done
fi
curl -fsS -o /dev/null "$BASE/health" || { echo "    API never came up"; exit 1; }
echo "    API is up"

echo "==> migrations at head"
HEAD_FILE="$(ls "$ROOT"/backend/db/migrations/*.sql | sort | tail -1 | xargs basename)"
APPLIED="$(docker exec oracle-db-1 psql -U postgres -d oracle -t -A \
  -c "SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1" 2>/dev/null || echo "?")"
echo "    repo head:    $HEAD_FILE"
echo "    applied head: $APPLIED"

echo "==> golden customer journey"
exec python3 "$ROOT/tests/golden/golden_e2e.py" --base-url "$BASE" "$@"
