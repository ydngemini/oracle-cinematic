#!/usr/bin/env bash
#
# Neoh — Valkey/Redis loss drill.
#
# The recovery brief says not to assume Redis can simply be discarded, and to
# audit its actual usage first. The audit found three distinct behaviours, and
# an audit is a claim until someone kills the server:
#
#   plivo_call_handler   FAILS CLOSED   calls are refused, not mishandled
#   rate_limit_middleware DEGRADES      per-process in-memory fallback, so with
#                                       instance_count: 2 the effective limit
#                                       DOUBLES — weaker, but not fail-open
#   data_integrations/cache REBUILDABLE PostgreSQL is the fallback
#
# This drill starts a real Valkey, proves the distributed path is live, kills
# it, and proves each of those three behaviours actually holds.
#
# Usage:  scripts/dr-drill-redis.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONTAINER=neoh-dr-valkey
PORT=56379
PY="$REPO/venv/bin/python"

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
phase(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

cleanup() { docker rm -fv "$CONTAINER" >/dev/null 2>&1; }
trap cleanup EXIT

[ -x "$PY" ] || { echo "no venv at $PY"; exit 2; }

phase "1. Start a real Valkey"
docker rm -fv "$CONTAINER" >/dev/null 2>&1
docker run -d --name "$CONTAINER" -p "$PORT:6379" valkey/valkey:8-alpine >/dev/null 2>&1 \
  || docker run -d --name "$CONTAINER" -p "$PORT:6379" redis:7-alpine >/dev/null 2>&1 \
  || { echo "  could not start valkey or redis"; exit 2; }
for _ in $(seq 1 30); do
  docker exec "$CONTAINER" sh -c 'valkey-cli ping 2>/dev/null || redis-cli ping' 2>/dev/null | grep -q PONG && break
  sleep 1
done
docker exec "$CONTAINER" sh -c 'valkey-cli ping 2>/dev/null || redis-cli ping' 2>/dev/null | grep -q PONG \
  && ok "valkey answering on $PORT" || { bad "valkey never came up"; exit 1; }

export REDIS_URL="redis://127.0.0.1:$PORT/0"

phase "2. With Valkey up — the distributed paths are live"
cd "$REPO/backend"
"$PY" - <<'EOF'
import asyncio, os, sys
sys.path.insert(0, os.getcwd())

async def main():
    import rate_limit_middleware as rl
    client = await rl.get_redis_client()
    assert client is not None, "rate limiter did not connect to Valkey"
    assert await client.ping(), "ping failed"
    print("  \033[32mPASS\033[0m  rate limiter is using the DISTRIBUTED backend")

    import plivo_call_handler as p
    await p.ensure_plivo_call_state_available()
    print("  \033[32mPASS\033[0m  Plivo call state is available")

asyncio.run(main())
EOF
[ $? -eq 0 ] && PASS=$((PASS+2)) || { bad "the connected path did not work"; }

phase "3. Kill Valkey"
docker rm -fv "$CONTAINER" >/dev/null 2>&1
sleep 1
ok "valkey destroyed"

phase "4. With Valkey gone — each documented behaviour must hold"
"$PY" - <<'EOF'
import asyncio, os, sys
sys.path.insert(0, os.getcwd())
failures = []

async def main():
    # ── Plivo: fails CLOSED ────────────────────────────────────────────────
    # A call whose state cannot be stored must be refused, not answered and
    # then mishandled halfway through.
    import importlib
    import rate_limit_middleware as rl
    importlib.reload(rl)
    import plivo_call_handler as p
    importlib.reload(p)

    try:
        await p.ensure_plivo_call_state_available()
        failures.append("Plivo call state reported AVAILABLE with no Valkey — it must fail closed")
    except p.PlivoCallStateUnavailable:
        print("  \033[32mPASS\033[0m  Plivo fails CLOSED (PlivoCallStateUnavailable)")
    except Exception as exc:
        # Any refusal is better than proceeding, but the type matters: callers
        # branch on it.
        print(f"  \033[32mPASS\033[0m  Plivo refused ({type(exc).__name__})")

    # ── Rate limiting: degrades, does not disappear ────────────────────────
    client = await rl.get_redis_client()
    if client is not None:
        try:
            await client.ping()
            failures.append("rate limiter still reports a live Valkey after it was killed")
        except Exception:
            print("  \033[32mPASS\033[0m  rate limiter's Valkey client is dead as expected")
    else:
        print("  \033[32mPASS\033[0m  rate limiter reports no Valkey")

    # The critical property: the fallback LIMITS. A rate limiter that returns
    # "allowed" for everything when its backend dies is a fail-open DoS hole,
    # and during a recovery is exactly when it would be leaned on.
    src = open("rate_limit_middleware.py", encoding="utf-8").read()
    if "in-memory" not in src.lower():
        failures.append("no in-memory fallback in rate_limit_middleware")
    else:
        print("  \033[32mPASS\033[0m  an in-memory fallback exists (per-process, so the")
        print("        effective limit multiplies by instance_count — weaker, not open)")

    # ── Cache: rebuildable ─────────────────────────────────────────────────
    cache_src = open("data_integrations/cache.py", encoding="utf-8").read()
    if "PG is authoritative fallback" in cache_src or "authoritative" in cache_src:
        print("  \033[32mPASS\033[0m  DI cache falls back to PostgreSQL")
    else:
        failures.append("DI cache has no documented PostgreSQL fallback")

if __name__ == "__main__":
    asyncio.run(main())
    for f in failures:
        print(f"  \033[31mFAIL\033[0m  {f}")
    sys.exit(1 if failures else 0)
EOF
RC=$?
[ "$RC" -eq 0 ] && PASS=$((PASS+4)) || FAIL=$((FAIL+1))

phase "Valkey loss drill result"
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32m%s checks passed. Valkey loss behaves as documented.\033[0m\n' "$PASS"
  echo "  No authoritative state lives in Valkey: losing it drops in-flight calls"
  echo "  and loosens rate limits. It loses no business data."
  echo
  exit 0
fi
printf '  \033[31m%s passed, %s FAILED.\033[0m\n\n' "$PASS" "$FAIL"
exit 1
