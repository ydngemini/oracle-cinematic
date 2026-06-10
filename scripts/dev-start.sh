#!/usr/bin/env bash
# Oracle — one-command local dev startup
#
# Idempotent: safe to run multiple times. Already-running services are left
# alone; migrations and ignite_memory are idempotent by design (IF NOT EXISTS /
# ON CONFLICT DO UPDATE).
#
# Usage:
#   ./scripts/dev-start.sh          # normal start
#   ./scripts/dev-start.sh --reset  # tear down volumes, start fresh
#
# Prerequisites: Docker (with Compose plugin), Python 3.12 in PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colour helpers ────────────────────────────────────────────────────────────
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

# ── Guard: Docker must be running ─────────────────────────────────────────────
if ! docker info > /dev/null 2>&1; then
  red "Docker is not running. Start Docker and try again."
  exit 1
fi

if ! docker compose version > /dev/null 2>&1; then
  red "Docker Compose plugin not found. Install it with your Docker distribution."
  exit 1
fi

cd "$PROJECT_ROOT"

# ── Optional --reset: nuke volumes so the DB starts clean ─────────────────────
if [[ "${1:-}" == "--reset" ]]; then
  yellow "Resetting: stopping containers and removing volumes..."
  docker compose down --volumes --remove-orphans 2>/dev/null || true
  green "Reset complete."
fi

# ── .env bootstrap ────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  yellow ".env not found — copying from .env.example (review and fill in Stripe / AWS keys)"
  cp .env.example .env
fi

# ── Start compose services (detached) ─────────────────────────────────────────
bold "Starting Oracle services..."
docker compose up -d --build

# ── Wait for DB healthy ───────────────────────────────────────────────────────
bold "Waiting for DB to become healthy..."
MAX_WAIT=60
elapsed=0
until docker compose exec -T db pg_isready -U postgres -d oracle > /dev/null 2>&1; do
  if (( elapsed >= MAX_WAIT )); then
    red "Timed out waiting for DB after ${MAX_WAIT}s."
    docker compose logs db | tail -20
    exit 1
  fi
  printf '.'
  sleep 2
  (( elapsed += 2 ))
done
echo  # newline after dots
green "DB is healthy."

# ── Apply migrations ──────────────────────────────────────────────────────────
# Migrations live in backend/db/migrations/ and are also mounted into the
# postgres container's /docker-entrypoint-initdb.d/ — they run automatically
# on a fresh volume. This step re-applies them for an already-initialised DB
# (psql errors on duplicate objects are silenced; all migrations are idempotent).
bold "Applying schema migrations..."
for sql_file in "$PROJECT_ROOT"/backend/db/migrations/*.sql; do
  filename="$(basename "$sql_file")"
  printf '  applying %s... ' "$filename"
  docker compose exec -T db psql -U postgres -d oracle \
    < "$sql_file" > /dev/null 2>&1 && echo "ok" || echo "(already applied / no-op)"
done
green "Migrations done."

# ── Seed demo tenants ─────────────────────────────────────────────────────────
# The auth demo tenancy map (backend/auth.py DEMO_TENANCY) stamps these tenant
# ids into JWTs; lead inserts FK against tenants, so the rows must exist.
bold "Seeding demo tenants..."
docker compose exec -T db psql -U postgres -d oracle -q -c \
  "INSERT INTO tenants (id, name, slug) VALUES
     ('00000000-0000-0000-0000-000000000000', 'Oracle Platform', 'platform'),
     ('11111111-1111-1111-1111-111111111111', 'Apex Brokerage',  'apex')
   ON CONFLICT (id) DO NOTHING;"
green "Tenants seeded."

# ── Seed Memory Core (ignite_memory) ─────────────────────────────────────────
# Creates user_profiles / user_interactions tables and upserts the demo-operator
# row. Idempotent — ON CONFLICT DO UPDATE. Runs inside the backend container so
# it uses the compose DB hostname, not localhost.
bold "Seeding Memory Core (ignite_memory)..."
docker compose exec -T backend \
  python3 scripts/ignite_memory.py 2>&1 \
  | while IFS= read -r line; do printf '  %s\n' "$line"; done
green "Memory Core seeded."

# ── Print service URLs ────────────────────────────────────────────────────────
echo
bold "═══════════════════════════════════════════"
bold " Oracle is running"
bold "═══════════════════════════════════════════"
green "  Frontend  →  http://localhost:5173"
green "  Backend   →  http://localhost:8000"
green "  API docs  →  http://localhost:8000/docs"
green "  DB        →  localhost:5432 (oracle / postgres)"
echo
yellow "  Logs:  docker compose logs -f"
yellow "  Stop:  docker compose down"
yellow "  Reset: ./scripts/dev-start.sh --reset"
echo
