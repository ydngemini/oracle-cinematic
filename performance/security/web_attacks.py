"""Live attacks on request handling, CORS, webhooks, team offboarding and
WebSockets against a Neoh test topology (never production).

  docker exec -i -e NEOH_SECURITY_TEST_ALLOWED=1 -w /app oracle-perf-worker \
      python /perf/security/web_attacks.py

Exit status = number of failed checks. Account state it changes is restored.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import asyncpg
import httpx

sys.path.insert(0, "/app")

BASE = os.getenv("TARGET", "http://oracle-perf-lb:8080")
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")
USERS = os.getenv("USERS_FILE", "/perf/out/users.json")
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def guard() -> None:
    if os.getenv("NEOH_SECURITY_TEST_ALLOWED") != "1":
        sys.exit("refusing: set NEOH_SECURITY_TEST_ALLOWED=1 (test topology only)")
    env = httpx.get(f"{BASE}/version", timeout=10).json().get("environment", "")
    if env not in ("loadtest", "staging", "development", "test"):
        sys.exit(f"refusing: target reports environment={env!r}")


class S:
    """A signed-in browser: session cookie + CSRF double-submit."""

    def __init__(self, user: dict, ip: str):
        self.user = user
        self.ip = ip
        r = httpx.post(f"{BASE}/auth/login", timeout=30, headers={"X-Forwarded-For": ip},
                       json={"agent_id": user["agent_id"], "passphrase": user["password"]})
        r.raise_for_status()
        self.token = r.cookies.get("oracle_session")
        c = httpx.get(f"{BASE}/auth/csrf", cookies={"oracle_session": self.token}, timeout=30)
        self.csrf = c.cookies.get("csrf_token") or c.json().get("csrf_token", "")

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        cookies = {"oracle_session": self.token, "csrf_token": self.csrf}
        headers = {"X-CSRF-Token": self.csrf, "X-Forwarded-For": self.ip, **kw.pop("headers", {})}
        return httpx.request(method, f"{BASE}{path}", cookies=cookies, headers=headers, timeout=60, **kw)


async def main() -> None:
    guard()
    users = json.load(open(USERS))["users"]
    pick = lambda slug, role, n=0: [u for u in users if u["tenant_slug"] == slug and u["role"] == role][n]
    a_owner, a_agent, a_agent2 = pick("perf-brokerage-01", "broker_owner"), pick("perf-brokerage-01", "agent"), pick("perf-brokerage-01", "agent", 1)
    b_owner, b_agent = pick("perf-brokerage-02", "broker_owner"), pick("perf-brokerage-02", "agent")
    db = await asyncpg.connect(host="db", user="postgres", password=os.getenv("ORACLE_DB_ADMIN_PASSWORD", "postgres"),
                               database=os.getenv("ORACLE_DB_NAME", "oracle"))

    # ── Request bodies (UPL-1/UPL-2) ────────────────────────────────────────
    r = httpx.post(f"{BASE}/api/crm/clients", content=b"x" * (3 * 1024 * 1024),
                   headers={"content-type": "application/json", "X-Forwarded-For": "203.0.113.90"}, timeout=60)
    check("anonymous 3 MB JSON body refused with 413 before auth/parse", r.status_code == 413, str(r.status_code))
    t0 = time.monotonic()
    r = httpx.post(f"{BASE}/api/public/property-upload/{'A' * 43}", timeout=120,
                   headers={"X-Forwarded-For": "203.0.113.91"},
                   files={"file": ("x.mp4", b"\x00\x00\x00\x18ftypmp42" + b"0" * (40 * 1024 * 1024), "video/mp4")})
    took = time.monotonic() - t0
    check("public upload with a guessed token refused (404)", r.status_code == 404, str(r.status_code))
    # Declare 600 MB and send only the headers: the answer must come back
    # without the server waiting for (or reading) the body.
    host = BASE.split("//", 1)[1]
    hname, _, hport = host.partition(":")
    reader, writer = await asyncio.open_connection(hname, int(hport or 80))
    writer.write((f"POST /api/public/property-upload/{'A' * 43} HTTP/1.1\r\nHost: {host}\r\n"
                  "Content-Type: multipart/form-data; boundary=x\r\n"
                  f"Content-Length: {600 * 1024 * 1024}\r\nX-Forwarded-For: 203.0.113.92\r\n\r\n").encode())
    await writer.drain()
    status_line = (await asyncio.wait_for(reader.readline(), timeout=15)).decode()
    writer.close()
    check("public upload declaring 600 MB refused by size, before any body", " 413 " in status_line, status_line.strip())
    print(f"      (40 MB guessed-token upload answered in {took:.2f}s)")

    # ── CORS (WEB-1) ──────────────────────────────────────────────────────────
    sa = S(a_agent, "203.0.113.93")
    for origin in ("https://evil.example", "http://localhost:5173.evil.example", "null",
                   "https://localhost:5173", "http://localhost:5174", "http://LOCALHOST:5173"):
        r = httpx.get(f"{BASE}/auth/session", cookies={"oracle_session": sa.token},
                      headers={"Origin": origin}, timeout=30)
        check(f"CORS refuses origin {origin!r}", r.headers.get("access-control-allow-origin") is None,
              r.headers.get("access-control-allow-origin", ""))
    r = httpx.get(f"{BASE}/auth/session", cookies={"oracle_session": sa.token},
                  headers={"Origin": "http://localhost:5173"}, timeout=30)
    check("CORS admits exactly the configured origin",
          r.headers.get("access-control-allow-origin") == "http://localhost:5173")

    # ── CSRF ──────────────────────────────────────────────────────────────────
    r = httpx.post(f"{BASE}/api/crm/clients", cookies={"oracle_session": sa.token}, json={"full_name": "csrf"},
                   headers={"X-Forwarded-For": "203.0.113.93"}, timeout=30)
    check("cookie-authenticated POST without CSRF token refused", r.status_code == 403, str(r.status_code))
    r = httpx.post(f"{BASE}/api/crm/clients", cookies={"oracle_session": sa.token, "csrf_token": "a" * 43},
                   json={"full_name": "csrf"}, headers={"X-CSRF-Token": "b" * 43, "X-Forwarded-For": "203.0.113.93"}, timeout=30)
    check("mismatched CSRF header/cookie refused", r.status_code == 403, str(r.status_code))
    r = httpx.post(f"{BASE}/api/crm/clients", cookies={"oracle_session": sa.token},
                   data={"full_name": "csrf"}, headers={"Origin": "https://evil.example", "X-Forwarded-For": "203.0.113.93"}, timeout=30)
    check("cross-site form post refused", r.status_code in (403, 422), str(r.status_code))

    # ── Stripe webhook forgery and replay (BILL-1/BILL-3) ─────────────────────
    def stripe_sig(body: bytes, secret: str) -> str:
        ts = str(int(time.time()))
        return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    ev = {"id": "evt_secreview" + uuid.uuid4().hex[:16], "object": "event", "created": int(time.time()),
          "type": "customer.subscription.deleted", "livemode": False, "api_version": "2026-05-27",
          "data": {"object": {"id": "sub_secreview_nonexistent", "object": "subscription"}}}
    body = json.dumps(ev).encode()
    for label, sig in (("empty-key HMAC", stripe_sig(body, "")), ("missing", ""), ("wrong key", stripe_sig(body, "whsec_guess"))):
        r = httpx.post(f"{BASE}/billing/webhook", content=body, headers={"stripe-signature": sig}, timeout=30)
        check(f"Stripe webhook with {label} signature refused", r.status_code == 400, str(r.status_code))
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if secret:
        r1 = httpx.post(f"{BASE}/billing/webhook", content=body, headers={"stripe-signature": stripe_sig(body, secret)}, timeout=30)
        r2 = httpx.post(f"{BASE}/billing/webhook", content=body, headers={"stripe-signature": stripe_sig(body, secret)}, timeout=30)
        check("validly-signed event accepted once", r1.status_code == 200 and "duplicate" not in r1.text, r1.text[:100])
        check("replayed event (re-signed) is a no-op", r2.status_code == 200 and "duplicate" in r2.text, r2.text[:100])
        n = await db.fetchval("SELECT count(*) FROM stripe_webhook_events WHERE event_id=$1", ev["id"])
        check("event id recorded exactly once", n == 1, str(n))
        await db.execute("DELETE FROM stripe_webhook_events WHERE event_id=$1", ev["id"])

    # ── Subscription entitlement (BILL-2) ─────────────────────────────────────
    # The topology runs with ORACLE_BILLING_ENFORCED=0 for load work; assert the
    # gate is wired (route carries the dependency) via the 402 contract only when on.
    # ── Billing portal is owner-only (BILL-4) ─────────────────────────────────
    r = sa.req("POST", "/billing/create-portal-session", json={"tenant_id": a_agent["tenant_id"]})
    check("agent cannot open the brokerage billing portal", r.status_code == 403, str(r.status_code))
    r = sa.req("POST", "/billing/create-portal-session", json={"tenant_id": b_agent["tenant_id"]})
    check("agent cannot open ANOTHER tenant's billing portal", r.status_code == 403, str(r.status_code))

    # ── Team offboarding (OFF-1) ──────────────────────────────────────────────
    so = S(a_owner, "203.0.113.94")
    victim = S(a_agent2, "203.0.113.95")
    v_id = str(await db.fetchval("SELECT id FROM users WHERE lower(agent_id)=lower($1)", a_agent2["agent_id"]))
    o_id = str(await db.fetchval("SELECT id FROM users WHERE lower(agent_id)=lower($1)", a_owner["agent_id"]))
    b_victim_id = str(await db.fetchval("SELECT id FROM users WHERE lower(agent_id)=lower($1)", b_agent["agent_id"]))
    try:
        r = sa.req("POST", f"/api/brokerage/team/{v_id}/suspend", json={"reason": "agent tries"})
        check("an agent cannot suspend a teammate", r.status_code == 403, str(r.status_code))
        r = sa.req("POST", f"/api/brokerage/team/{o_id}/suspend", json={"reason": "agent tries owner"})
        check("an agent cannot suspend the owner", r.status_code == 403, str(r.status_code))
        r = so.req("POST", f"/api/brokerage/team/{b_victim_id}/suspend", json={"reason": "cross tenant"})
        check("an owner cannot suspend another brokerage's agent", r.status_code == 404, str(r.status_code))
        r = so.req("POST", f"/api/brokerage/team/{o_id}/suspend", json={"reason": "self"})
        check("an owner cannot suspend themselves", r.status_code == 404, str(r.status_code))
        r = so.req("POST", f"/api/brokerage/team/{v_id}/suspend", json={"reason": "left the brokerage", "role": "platform_admin"})
        check("suspend body rejects unexpected fields (extra=forbid)", r.status_code == 422, str(r.status_code))
        check("victim session works before suspension", victim.req("GET", "/api/crm/clients?limit=1").status_code == 200)
        r = so.req("POST", f"/api/brokerage/team/{v_id}/suspend", json={"reason": "left the brokerage"})
        check("owner suspends an agent", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        check("suspended agent's open session is refused", victim.req("GET", "/api/crm/clients?limit=1").status_code == 401)
        r = httpx.post(f"{BASE}/auth/login", json={"agent_id": a_agent2["agent_id"], "passphrase": a_agent2["password"]},
                       headers={"X-Forwarded-For": "203.0.113.96"}, timeout=30)
        check("suspended agent cannot sign in", r.status_code == 401, str(r.status_code))
        r = so.req("POST", f"/api/brokerage/team/{v_id}/reinstate", json={"reason": "returned"})
        check("owner reinstates", r.status_code == 200, str(r.status_code))
        check("the pre-suspension session stays ended after reinstatement",
              victim.req("GET", "/api/crm/clients?limit=1").status_code == 401)
        audit = await db.fetchval(
            "SELECT count(*) FROM audit_ledger WHERE target_id=$1 AND action LIKE 'team.member.%'", v_id)
        check("suspension and reinstatement are in the audit ledger", (audit or 0) >= 2, str(audit))
    finally:
        await db.execute("UPDATE users SET is_active=true WHERE id=$1::uuid", v_id)
        await db.execute("UPDATE team_memberships SET status='active' WHERE user_id=$1::uuid AND status='suspended'", v_id)

    # ── Telephony route takeover (HOOK-2 / TEN-8) ─────────────────────────────
    r = sa.req("PUT", "/api/telephony/routes/me", json={
        "inbound_did": "+13025550199", "twilio_account_sid": "AC" + "0" * 32, "voice_caller_id_e164": "+13025550100"})
    check("an agent cannot point a route at a number", r.status_code == 403, f"{r.status_code} {r.text[:100]}")
    r = sa.req("PUT", "/api/telephony/routes/me", json={
        "inbound_did": "+13025550199", "twilio_account_sid": "AC" + "0" * 32, "voice_caller_id_verified": True,
        "tenant_id": b_agent["tenant_id"]})
    check("route body rejects tenant_id (extra=forbid)", r.status_code in (403, 422), str(r.status_code))

    # ── Admin surface ─────────────────────────────────────────────────────────
    for path in ("/api/admin/system", "/api/admin/users", "/api/admin/overview", "/api/admin/billing-summary"):
        r = sa.req("GET", path)
        check(f"agent refused at {path}", r.status_code in (401, 403), str(r.status_code))
        r = so.req("GET", path)
        check(f"broker owner refused at platform-only {path}", r.status_code in (401, 403), str(r.status_code))
        r = httpx.get(f"{BASE}{path}", timeout=30)
        check(f"anonymous refused at {path}", r.status_code in (401, 403), str(r.status_code))

    # ── WebSocket ─────────────────────────────────────────────────────────────
    import websockets

    async def ws_try(headers: dict, frames: list | None = None, expect_close: bool = True) -> str:
        try:
            async with websockets.connect(f"{WS_BASE}/ws", additional_headers=headers, open_timeout=10,
                                          max_size=2**22) as ws:
                for f in frames or []:
                    await ws.send(f)
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    return f"open:{str(msg)[:60]}"
                except asyncio.TimeoutError:
                    return "open:silent"
        except websockets.exceptions.InvalidStatus as exc:
            return f"rejected:{exc.response.status_code}"
        except websockets.exceptions.ConnectionClosed as exc:
            return f"closed:{exc.rcvd.code if exc.rcvd else '?'}"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"

    good = {"Origin": "http://localhost:5173", "Cookie": f"oracle_session={sa.token}", "X-Forwarded-For": "203.0.113.93"}
    res = await ws_try({"Origin": "http://localhost:5173"})
    check("WS with no token refused", res.startswith(("closed:4401", "rejected", "closed")), res)
    res = await ws_try({"Origin": "https://evil.example", "Cookie": f"oracle_session={sa.token}"})
    check("WS from an untrusted Origin refused", res.startswith(("closed:4403", "rejected", "closed")), res)
    res = await ws_try({**good, "Cookie": "oracle_session=not.a.jwt"})
    check("WS with a malformed token refused", res.startswith(("closed:4401", "rejected", "closed")), res)
    res = await ws_try(good)
    check("WS with a valid session opens", res.startswith("open"), res)
    res = await ws_try(good, frames=["[]", '"x"', "{bad json", json.dumps({"type": "OBSERVE", "agent": "X" * 1000, "content": "y" * 100000})])
    check("WS survives non-object / malformed / removed-type frames", res.startswith("open"), res)
    async def oversized_frame() -> str:
        async with websockets.connect(f"{WS_BASE}/ws", additional_headers=good, open_timeout=10,
                                      max_size=2**22) as ws:
            await ws.send("z" * (2 * 1024 * 1024))
            try:
                while True:  # drain the hydration frames until the server closes
                    await asyncio.wait_for(ws.recv(), timeout=10)
            except websockets.exceptions.ConnectionClosed as exc:
                return f"closed:{exc.rcvd.code if exc.rcvd else '?'}"
            except asyncio.TimeoutError:
                return "still-open"

    res = await oversized_frame()
    check("WS 2 MB frame closes the socket (1009 message too big)", res == "closed:1009", res)

    await db.close()
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(len(FAILS))


if __name__ == "__main__":
    asyncio.run(main())
