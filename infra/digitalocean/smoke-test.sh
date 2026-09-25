#!/usr/bin/env bash
#
# smoke-test.sh — Post-deploy smoke test for Neoh on DigitalOcean App Platform.
#
# Mirrors infra/scripts/prod-smoke.sh's AWS checks, adapted to this stack:
#   1. Public health endpoint (APP_URL/health) — HTTP 200 means the pool is up
#      and a live "SELECT 1" answered.
#   2. Release identity (APP_URL/version) — confirms the SHA that's actually
#      live matches EXPECTED_GIT_SHA (when given), so a deploy can be verified
#      rather than assumed. Also prints the live migration_head.
#   3. The API router is mounted: an anonymous call to an auth-gated route
#      must return 401/403/422, not 404 (not mounted) or 5xx (broken).
#   4. Spaces bucket reachable and NOT publicly listable — Spaces is
#      S3-API-compatible, so the AWS CLI works against it with --endpoint-url;
#      no DO-specific tooling required for this check.
#
# Usage:
#   APP_URL=https://neohrs.com EXPECTED_GIT_SHA=$(git rev-parse HEAD) \
#     ./smoke-test.sh
#
# Env overrides:
#   APP_URL           (required)
#   EXPECTED_GIT_SHA  (optional — skip to only check that /version answers)
#   SPACES_ENDPOINT   (default https://nyc3.digitaloceanspaces.com)
#   SPACES_BUCKET     (default neoh-media)
#
# Requires: curl, and (only for check 4) the aws CLI with Spaces keys set as
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — Spaces accepts the same CLI,
# just pointed at a different endpoint.
#
# Exit code: 0 if all hard checks pass, 1 if any hard check fails.

set -uo pipefail

APP_URL="${APP_URL:?Set APP_URL, e.g. https://neohrs.com}"
SPACES_ENDPOINT="${SPACES_ENDPOINT:-https://nyc3.digitaloceanspaces.com}"
SPACES_BUCKET="${SPACES_BUCKET:-neoh-media}"

FAIL=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=1; }
info() { printf '  \033[2m%s\033[0m\n' "$1"; }

echo "== 1. Health =="
health_code=$(curl -s -o /tmp/smoke-health.json -w '%{http_code}' "$APP_URL/health" || echo "000")
if [ "$health_code" = "200" ]; then
  pass "GET /health -> 200"
  info "$(cat /tmp/smoke-health.json)"
else
  fail "GET /health -> $health_code (expected 200)"
fi

echo "== 2. Release identity =="
version_body=$(curl -s "$APP_URL/version" || echo "{}")
info "$version_body"
live_sha=$(printf '%s' "$version_body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("git_sha","unknown"))' 2>/dev/null || echo "unknown")
migration_head=$(printf '%s' "$version_body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("migration_head","unknown"))' 2>/dev/null || echo "unknown")
if [ "$live_sha" = "unknown" ]; then
  fail "GET /version returned no git_sha — release was built without --build-arg GIT_SHA"
elif [ -n "${EXPECTED_GIT_SHA:-}" ] && [ "$live_sha" != "$EXPECTED_GIT_SHA" ]; then
  fail "live git_sha ($live_sha) != EXPECTED_GIT_SHA ($EXPECTED_GIT_SHA) — the deploy did not land, or landed partially"
else
  pass "live release: git_sha=$live_sha migration_head=$migration_head"
fi

echo "== 2b. Background worker is alive, on THIS release =="
# The API passing says nothing about the worker, which serves no route. A
# release whose worker crashed on boot used to pass this script while every
# background job stopped. GET /health/workers reads the heartbeats workers
# write every 30 s (backend/process_heartbeat.py).
#
# Waits, because the worker rolls over on its own schedule: it may still be
# draining the previous release's jobs (grace_period_seconds: 300) when the API
# is already serving. The check is for a live worker whose git_sha is THIS
# release — a previous-release worker still running jobs against a schema the
# new API has changed is a failed release, not a healthy one.
WORKER_WAIT="${WORKER_WAIT_SECONDS:-420}"
waited=0
worker_ok=0
worker_body="{}"
while [ "$waited" -le "$WORKER_WAIT" ]; do
  worker_body=$(curl -s -m 10 "$APP_URL/health/workers" || echo "{}")
  verdict=$(printf '%s' "$worker_body" | EXPECTED="${EXPECTED_GIT_SHA:-}" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable"); raise SystemExit
want = os.environ.get("EXPECTED", "")
live = d.get("live_git_shas") or []
if not d.get("healthy"):
    print("dead")
elif want and want not in live:
    print("stale:" + ",".join(live))
elif want and len(live) > 1:
    print("mixed:" + ",".join(live))
else:
    print("ok")
' 2>/dev/null || echo "unreadable")
  case "$verdict" in
    ok) worker_ok=1; break ;;
  esac
  sleep 15
  waited=$((waited + 15))
done
info "$worker_body"
if [ "$worker_ok" = "1" ]; then
  pass "a live worker is running ${EXPECTED_GIT_SHA:-the current release}"
else
  case "$verdict" in
    dead)       fail "no live worker after ${WORKER_WAIT}s — background jobs are not running" ;;
    stale:*)    fail "live worker(s) are on ${verdict#stale:}, not ${EXPECTED_GIT_SHA} — the worker did not roll over" ;;
    mixed:*)    fail "workers on more than one release (${verdict#mixed:}) after ${WORKER_WAIT}s — a rollout is stuck" ;;
    *)          fail "GET /health/workers unreadable — cannot tell whether background jobs run" ;;
  esac
fi

echo "== 3. API router is mounted =="
api_code=$(curl -s -o /dev/null -w '%{http_code}' "$APP_URL/api/commands" || echo "000")
case "$api_code" in
  401|403|422) pass "GET /api/commands -> $api_code (auth gate reached, router mounted)" ;;
  404) fail "GET /api/commands -> 404 (router not mounted)" ;;
  000) fail "GET /api/commands -> no response (app unreachable)" ;;
  *) fail "GET /api/commands -> $api_code (unexpected — check for a 5xx boot error)" ;;
esac

echo "== 4. Spaces bucket =="
if command -v aws >/dev/null 2>&1; then
  if aws --endpoint-url "$SPACES_ENDPOINT" s3api head-bucket --bucket "$SPACES_BUCKET" >/dev/null 2>&1; then
    pass "Spaces bucket $SPACES_BUCKET is reachable"
    acl=$(aws --endpoint-url "$SPACES_ENDPOINT" s3api get-bucket-acl --bucket "$SPACES_BUCKET" 2>/dev/null || echo "")
    if printf '%s' "$acl" | grep -q "AllUsers"; then
      fail "Spaces bucket $SPACES_BUCKET grants access to AllUsers — private media must not be public"
    else
      pass "Spaces bucket $SPACES_BUCKET has no AllUsers grant"
    fi
  else
    fail "Spaces bucket $SPACES_BUCKET is not reachable at $SPACES_ENDPOINT (check ORACLE_S3_ACCESS_KEY_ID/SECRET)"
  fi
else
  info "aws CLI not installed — skipping Spaces bucket check"
fi

echo
if [ "$FAIL" = "0" ]; then
  echo "All checks passed."
else
  echo "One or more checks FAILED — see above."
fi
exit "$FAIL"
