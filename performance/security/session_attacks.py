"""Live session-revocation and token-forgery attacks against a Neoh test topology.

Each check prints PASS/FAIL; exit status is the number of failures. Runs inside
the topology network with the backend's own environment (so it can mint tokens
with the test topology's signing key — exactly what an attacker who stole one
token could replay, nothing more):

  docker exec -i -e NEOH_SECURITY_TEST_ALLOWED=1 oracle-perf-worker \
      python /perf/security/session_attacks.py

Account state it changes (password, role, is_active) is restored before exit.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import asyncpg
import httpx
import jwt

sys.path.insert(0, "/app")

BASE = os.getenv("TARGET", "http://oracle-perf-lb:8080")
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


def mint(claims: dict) -> str:
    import config  # noqa: F401 — derives the JWT issuer/audience defaults auth needs
    import auth  # the topology's own signing configuration

    now = time.time()
    payload = {"policy_version": auth.PLATFORM_POLICY_VERSION, "iat": now, "exp": now + 3600}
    if auth._JWT_ISSUER:
        payload["iss"] = auth._JWT_ISSUER
    if auth._JWT_AUDIENCE:
        payload["aud"] = auth._JWT_AUDIENCE
    payload.update(claims)
    return jwt.encode(payload, auth.SECRET_KEY, algorithm="HS256")


def get(token: str, path: str = "/api/crm/clients?limit=1") -> int:
    return httpx.get(f"{BASE}{path}", cookies={"oracle_session": token}, timeout=30,
                     headers={"X-Forwarded-For": "203.0.113.77"}).status_code


def login(user: dict, password: str | None = None) -> httpx.Response:
    return httpx.post(f"{BASE}/auth/login", timeout=30, headers={"X-Forwarded-For": "203.0.113.78"},
                      json={"agent_id": user["agent_id"], "passphrase": password or user["password"]})


def csrf_for(token: str) -> str:
    r = httpx.get(f"{BASE}/auth/csrf", cookies={"oracle_session": token}, timeout=30)
    return r.cookies.get("csrf_token") or r.json().get("csrf_token", "")


async def main() -> None:
    guard()
    users = json.load(open(USERS))["users"]
    agent = next(u for u in users if u["tenant_slug"] == "perf-brokerage-01" and u["role"] == "agent")
    owner = next(u for u in users if u["tenant_slug"] == "perf-brokerage-01" and u["role"] == "broker_owner")
    other_tenant = next(u for u in users if u["tenant_slug"] == "perf-brokerage-02")["tenant_id"]
    db = await asyncpg.connect(host="db", user="postgres", password=os.getenv("ORACLE_DB_ADMIN_PASSWORD", "postgres"),
                               database=os.getenv("ORACLE_DB_NAME", "oracle"))
    row = await db.fetchrow("SELECT id, role, session_epoch, password_hash FROM users WHERE lower(agent_id)=lower($1)",
                            agent["agent_id"])
    uid, epoch0, hash0 = str(row["id"]), row["session_epoch"], row["password_hash"]
    try:
        # --- baseline: a fresh login works and is bound to the account -----------
        r = login(agent)
        t1 = r.cookies.get("oracle_session")
        claims = jwt.decode(t1, options={"verify_signature": False})
        check("login token carries uid + epoch", claims.get("uid") == uid and claims.get("sep") == epoch0)
        check("fresh session reads tenant data", get(t1) == 200)

        # --- password change ends every other session ---------------------------
        t_other = login(agent).cookies.get("oracle_session")
        csrf = csrf_for(t1)
        new_pw = agent["password"] + "x"
        r = httpx.post(f"{BASE}/auth/change-password", timeout=30,
                       cookies={"oracle_session": t1, "csrf_token": csrf}, headers={"X-CSRF-Token": csrf},
                       json={"current_password": agent["password"], "new_password": new_pw})
        check("change-password succeeds", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        t2 = r.cookies.get("oracle_session")
        check("other device's token dies after password change", get(t_other) == 401, str(get(t_other)))
        check("the changing device's old token dies too", get(t1) == 401)
        check("the re-issued token on the changing device works", bool(t2) and get(t2) == 200)
        check("old token cannot renew via policy-acceptance",
              get(t_other, "/auth/policy-acceptance") == 401)

        # --- legacy token shape (no uid) is still epoch-checked -------------------
        legacy = mint({"sub": agent["agent_id"], "tenant_id": agent["tenant_id"], "role": "agent"})
        check("pre-0117 token (no uid, epoch 0) rejected once epoch moved", get(legacy) == 401)

        # --- demotion / role forgery ----------------------------------------------
        cur = await db.fetchval("SELECT session_epoch FROM users WHERE id=$1::uuid", uid)
        forged_role = mint({"sub": agent["agent_id"], "tenant_id": agent["tenant_id"], "role": "broker_owner",
                            "uid": uid, "sep": cur})
        check("signed token claiming a role the account lacks is refused", get(forged_role) == 401)
        forged_tenant = mint({"sub": agent["agent_id"], "tenant_id": other_tenant, "role": "agent",
                              "uid": uid, "sep": cur})
        check("signed token moving the account to another tenant is refused", get(forged_tenant) == 401)

        # --- deactivation ---------------------------------------------------------
        live = mint({"sub": agent["agent_id"], "tenant_id": agent["tenant_id"], "role": "agent", "uid": uid, "sep": cur})
        check("current token works before deactivation", get(live) == 200)
        await db.execute("UPDATE users SET is_active=false WHERE id=$1::uuid", uid)
        check("deactivated account's live token refused", get(live) == 401)
        check("deactivated account cannot renew", get(live, "/auth/policy-acceptance") == 401)
        await db.execute("UPDATE users SET is_active=true WHERE id=$1::uuid", uid)

        # --- absolute session age -------------------------------------------------
        old = mint({"sub": agent["agent_id"], "tenant_id": agent["tenant_id"], "role": "agent", "uid": uid,
                    "sep": cur, "auth_time": int(time.time()) - 8 * 86400})
        check("token whose password proof is >7 days old refused", get(old) == 401)

        # --- forgery basics ---------------------------------------------------------
        none_tok = jwt.encode({"sub": owner["agent_id"], "tenant_id": owner["tenant_id"], "role": "platform_admin",
                               "exp": time.time() + 600}, None, algorithm="none")
        check("alg=none token refused", get(none_tok) == 401)
        bad_sig = live[:-4] + ("AAAA" if not live.endswith("AAAA") else "BBBB")
        check("tampered signature refused", get(bad_sig) == 401)
            # nginx answers an oversized cookie header itself (400) — refused either way.
        check("oversized token refused", get("a." + "b" * 9000 + ".c") in (400, 401, 431))
        admin_forge = mint({"sub": agent["agent_id"], "tenant_id": "00000000-0000-0000-0000-000000000000",
                            "role": "platform_admin", "uid": uid, "sep": cur})
        # /api/admin/system answers from process memory and never opens a tenant
        # transaction — the gate itself must prove the account (verify_session_current).
        for admin_path in ("/api/admin/system", "/api/admin/runtime-load", "/api/admin/users"):
            check(f"signed platform_admin claim for a non-admin account refused at {admin_path}",
                  get(admin_forge, admin_path) in (401, 403), str(get(admin_forge, admin_path)))
    finally:
        await db.execute("UPDATE users SET is_active=true, password_hash=$2 WHERE id=$1::uuid", uid, hash0)
        await db.close()
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(len(FAILS))


if __name__ == "__main__":
    asyncio.run(main())
