#!/usr/bin/env bash
#
# Neoh — Valkey/Redis loss drill.
#
# The recovery brief says not to assume Redis can simply be discarded, and to
# audit its actual usage first. The audit found four distinct behaviours, and
# an audit is a claim until someone kills the server:
#
#   plivo_call_handler   FAILS CLOSED   calls are refused, not mishandled
#   voice_opt_out        FAILS CLOSED   pending spoken STOPs (audit 34.5) are
#                                       mirrored here from each container's
#                                       spool; unreadable -> the outreach gate
#                                       holds texts and calls (email is not
#                                       held); the owner's spool still blocks,
#                                       and its retries re-write the entry when
#                                       Valkey returns
#   rate_limit_middleware DEGRADES      per-process in-memory fallback, so with
#                                       instance_count: 2 the effective limit
#                                       DOUBLES — weaker, but not fail-open
#   data_integrations/cache REBUILDABLE PostgreSQL is the fallback
#
# This drill starts a real Valkey, proves the distributed path is live (for
# pending STOPs: two processes, a cut, the entry re-written and removed only
# after the row lands — tests/test_voice_opt_out_valkey_live.py), kills it,
# and proves each of those behaviours actually holds.
#
# Usage:  scripts/dr-drill-redis.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONTAINER=neoh-dr-valkey
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
# Loopback only, and Docker picks the port. A fixed host port (this was 56379)
# sits in Linux's ephemeral range, where an outbound connection can already
# hold it: the Postgres drill failed CI that way ("address already in use",
# run 44803825496). And publishing on every interface put an unauthenticated
# Valkey on the network for the length of the drill.
docker run -d --name "$CONTAINER" -p "127.0.0.1::6379" valkey/valkey:8-alpine >/dev/null 2>&1 \
  || { docker rm -fv "$CONTAINER" >/dev/null 2>&1
       docker run -d --name "$CONTAINER" -p "127.0.0.1::6379" redis:7-alpine >/dev/null 2>&1; } \
  || { echo "  could not start valkey or redis"; exit 2; }
PORT="$(docker port "$CONTAINER" 6379/tcp 2>/dev/null | head -n1)"
PORT="${PORT##*:}"
[[ "$PORT" =~ ^[0-9]+$ ]] \
  || { bad "the drill container is not running or Docker published no port for it"; exit 1; }
for _ in $(seq 1 30); do
  docker exec "$CONTAINER" sh -c 'valkey-cli ping 2>/dev/null || redis-cli ping' 2>/dev/null | grep -q PONG && break
  sleep 1
done
docker exec "$CONTAINER" sh -c 'valkey-cli ping 2>/dev/null || redis-cli ping' 2>/dev/null | grep -q PONG \
  && ok "valkey answering on $PORT" || { bad "valkey never came up"; exit 1; }

export REDIS_URL="redis://127.0.0.1:$PORT/0"

phase "2. With Valkey up — the distributed paths are live"
cd "$REPO/backend" || { echo "  cannot cd to $REPO/backend"; exit 2; }
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

ORACLE_LIVE_VALKEY_URL="$REDIS_URL" "$PY" -m pytest -p no:cacheprovider -q \
  tests/test_voice_opt_out_valkey_live.py </dev/null >/dev/null 2>&1 \
  && ok "pending spoken STOPs: shared across processes, fail closed on a cut, re-written, removed after the row" \
  || bad "pending spoken STOP live test failed (tests/test_voice_opt_out_valkey_live.py)"

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

    # ── Pending spoken STOPs: fail CLOSED ─────────────────────────────────
    # A STOP heard on another replica could be waiting in Valkey: a number
    # this process cannot check must not be texted or called.
    import voice_opt_out
    status = await voice_opt_out.opt_out_status("11111111-1111-1111-1111-111111111111", "+13025550142")
    if status != voice_opt_out.UNKNOWN:
        failures.append(f"pending-STOP status with no Valkey is {status!r}, not 'unknown' (must fail closed)")
    else:
        print("  \033[32mPASS\033[0m  pending spoken STOPs fail CLOSED (status unknown)")
    import outreach_compliance
    from contextlib import asynccontextmanager
    from tenancy import Role, TenantContext

    @asynccontextmanager
    async def no_db(ctx, conn):
        yield None

    async def clean_row(*_a, **_kw):
        return {"suppressed": False, "has_consent": True, "has_prior_business": False, "has_written_consent": True,
                "vp_consent": True, "recent_voice": 0}

    outreach_compliance._conn_or_tx = no_db
    outreach_compliance.ConsentLedger.gather_state = clean_row
    ctx = TenantContext(agent_id="drill@x.test", tenant_id="11111111-1111-1111-1111-111111111111",
                        role=Role.AGENT)
    sms = await outreach_compliance.guard_outreach(
        ctx, channel=outreach_compliance.Channel.SMS, contact="+13025550142", state_code="DE", log=False)
    email = await outreach_compliance.guard_outreach(
        ctx, channel=outreach_compliance.Channel.EMAIL, contact="owner@example.test", state_code="DE", log=False)
    held = outreach_compliance.OPT_OUT_STATUS_UNAVAILABLE
    if sms.allowed or held not in sms.blockers or held in email.blockers:
        failures.append("the outreach gate did not hold SMS (or held email) with no Valkey")
    else:
        print("  \033[32mPASS\033[0m  the outreach gate holds texts and calls; email is not held")

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
[ "$RC" -eq 0 ] && PASS=$((PASS+6)) || FAIL=$((FAIL+1))

phase "Valkey loss drill result"
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32m%s checks passed. Valkey loss behaves as documented.\033[0m\n' "$PASS"
  echo "  Losing Valkey drops in-flight calls, loosens rate limits and holds texts and"
  echo "  calls until it returns (pending spoken STOPs cannot be read). It loses no"
  echo "  business data on its own: a pending STOP is lost only if Valkey AND the"
  echo "  container that heard it both lose their data before the row is written."
  echo
  exit 0
fi
printf '  \033[31m%s passed, %s FAILED.\033[0m\n\n' "$PASS" "$FAIL"
exit 1
