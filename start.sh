#!/usr/bin/env bash
# Oracle — unified local dev auto-start
#
# Brings up: PostgreSQL (Docker), Backend (uvicorn), Frontend (Vite), Stripe CLI
# All services auto-connect. Ctrl+C kills everything cleanly.
#
# Usage:
#   ./start.sh          # start all
#   ./start.sh --reset  # nuke DB volume, start fresh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDS=()

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

cleanup() {
  echo
  yellow "Shutting down..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  green "All processes stopped."
}
trap cleanup EXIT INT TERM

# ── Load .env into this shell ────────────────────────────────────────────────
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

# ── Defaults for local dev ───────────────────────────────────────────────────
export ORACLE_DB_HOST="${ORACLE_DB_HOST:-localhost}"
export ORACLE_DB_PORT="${ORACLE_DB_PORT:-5432}"
export ORACLE_DB_NAME="${ORACLE_DB_NAME:-oracle}"
export ORACLE_DB_USER="${ORACLE_DB_USER:-postgres}"
export ORACLE_DB_PASSWORD="${ORACLE_DB_PASSWORD:-postgres}"
export ORACLE_DB_SSLMODE="${ORACLE_DB_SSLMODE:-disable}"
export ORACLE_DEMO_TENANT_ID="${ORACLE_DEMO_TENANT_ID:-a1b2c3d4-0000-4000-8000-000000000001}"
export ORACLE_BASE_URL="${ORACLE_BASE_URL:-http://localhost:5173}"

# ── 1. PostgreSQL (Docker) ───────────────────────────────────────────────────
if [[ "${1:-}" == "--reset" ]]; then
  yellow "Resetting DB volume..."
  docker rm -f oracle_db 2>/dev/null || true
  docker volume rm oracle_pg_data 2>/dev/null || true
fi

if ! docker ps --format '{{.Names}}' | grep -q '^oracle_db$'; then
  bold "Starting PostgreSQL..."
  docker run -d \
    --name oracle_db \
    -e POSTGRES_DB=oracle \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD="${ORACLE_DB_PASSWORD}" \
    -v oracle_pg_data:/var/lib/postgresql/data \
    -v "$ROOT/backend/db/migrations:/docker-entrypoint-initdb.d:ro" \
    -p 5432:5432 \
    --health-cmd "pg_isready -U postgres -d oracle" \
    --health-interval 3s \
    --health-timeout 2s \
    --health-retries 20 \
    postgres:16-alpine > /dev/null

  # Wait for healthy
  printf "  Waiting for DB"
  for i in $(seq 1 30); do
    if docker exec oracle_db pg_isready -U postgres -d oracle > /dev/null 2>&1; then
      break
    fi
    printf "."
    sleep 1
  done
  echo
  green "  DB ready."

  # Apply migrations (idempotent)
  for sql in "$ROOT"/backend/db/migrations/*.sql; do
    docker exec -i oracle_db psql -U postgres -d oracle < "$sql" > /dev/null 2>&1 || true
  done
  green "  Migrations applied."
else
  green "DB already running."
fi

# ── 2. Backend (uvicorn) ─────────────────────────────────────────────────────
VENV="$ROOT/venv"
if [[ ! -f "$VENV/bin/uvicorn" ]]; then
  yellow "Installing backend deps..."
  python3 -m venv "$VENV" 2>/dev/null || true
  "$VENV/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi

bold "Starting backend on :8000..."
cd "$ROOT/backend"
"$VENV/bin/uvicorn" server:app --host 0.0.0.0 --port 8000 --reload --reload-dir "$ROOT/backend" &
PIDS+=($!)
cd "$ROOT"

# Wait for backend to respond
for i in $(seq 1 20); do
  if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
green "  Backend live."

# ── 3. Frontend (Vite) ───────────────────────────────────────────────────────
if [[ ! -d "$ROOT/oracle-app/node_modules" ]]; then
  yellow "Installing frontend deps..."
  (cd "$ROOT/oracle-app" && npm install --silent)
fi

bold "Starting frontend on :5173..."
(cd "$ROOT/oracle-app" && npm run dev -- --host 0.0.0.0) &
PIDS+=($!)
green "  Frontend live."

# ── 4. Stripe CLI webhook listener ──────────────────────────────────────────
STRIPE_BIN="${HOME}/bin/stripe"
if [[ -x "$STRIPE_BIN" ]] && [[ -n "${STRIPE_SECRET_KEY:-}" ]]; then
  bold "Starting Stripe webhook listener..."
  "$STRIPE_BIN" listen \
    --forward-to localhost:8000/billing/webhook \
    --events checkout.session.completed,invoice.paid,customer.subscription.updated,customer.subscription.deleted &
  PIDS+=($!)
  green "  Stripe webhooks forwarding."
else
  yellow "  Stripe CLI not found or STRIPE_SECRET_KEY not set — skipping webhook listener."
fi

# ── Ready ────────────────────────────────────────────────────────────────────
echo
bold "═══════════════════════════════════════════════════════"
bold " Oracle — all systems auto-connected"
bold "═══════════════════════════════════════════════════════"
green "  Frontend   →  http://localhost:5173"
green "  Backend    →  http://localhost:8000"
green "  WebSocket  →  ws://localhost:8000/ws"
green "  DB         →  localhost:5432 (oracle/postgres)"
green "  Stripe     →  webhooks forwarding to /billing/webhook"
echo
yellow "  Press Ctrl+C to stop everything."
echo

# Keep alive — wait for any child to exit
wait
