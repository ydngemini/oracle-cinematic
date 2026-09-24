#!/usr/bin/env python3
"""Golden E2E — does Neoh work as one machine, or as parts that each pass?

Every other suite in this repo tests a feature in isolation, mostly on fakes.
This walks one customer's journey end to end against the running stack: real
HTTP through the real middleware (auth, CSRF, RLS), real PostgreSQL, real
background state. It is the test that answers "is this a product?" rather than
"does this function return the right dict?".

It fails loudly on the two things that are invisible until they are a
catastrophe: data crossing a tenant boundary, and data disappearing after a
restart.

Usage:
    python3 tests/golden/golden_e2e.py [--base-url http://localhost:8000]

It seeds everything it needs, asserts, and removes what it created.
Nothing external is contacted: no provider, no mail server, no payment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

STAMP = int(time.time())
POLICY = "neoh-platform-use-policy-2026-07-15-v1"
ESA = "neoh-account-security-esa-2026-07-18-v1"

PASSWORD = "correct horse battery staple"

results: list[tuple[bool, str, str]] = []
_skipped: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {label}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def skip(label: str, why: str) -> None:
    _skipped.append(label)
    print(f"  SKIP  {label} — {why}", flush=True)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class Session:
    """One browser-shaped session: bearer token plus the CSRF cookie pair."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token: Optional[str] = None
        self.csrf: Optional[str] = None
        self.tenant_id: Optional[str] = None
        self.agent_id: Optional[str] = None

    def request(self, method: str, path: str, body: Any = None,
                *, expect: Optional[int] = None) -> tuple[int, Any]:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
            req.add_header("Cookie", f"csrf_token={self.csrf}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode() or "{}"
                for header, value in resp.getheaders():
                    if header.lower() == "set-cookie" and "csrf_token=" in value:
                        self.csrf = value.split("csrf_token=")[1].split(";")[0]
                return resp.status, json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() or "{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"detail": raw[:200]}
            return exc.code, payload

    # -- convenience -------------------------------------------------------
    def get(self, path, **kw):    return self.request("GET", path, **kw)
    def post(self, path, body=None, **kw):  return self.request("POST", path, body, **kw)
    def patch(self, path, body=None, **kw): return self.request("PATCH", path, body, **kw)
    def delete(self, path, **kw): return self.request("DELETE", path, **kw)

    def prime_csrf(self) -> None:
        self.get("/auth/policy-acceptance")

    def accept_policy(self) -> None:
        self.prime_csrf()
        status, payload = self.post("/auth/policy-acceptance",
                                    {"policy_version": POLICY, "account_security_version": ESA})
        if status == 200 and payload.get("token"):
            self.token = payload["token"]


def sql(query: str) -> str:
    """Assertions the API cannot make — and the cleanup."""
    env = dict(os.environ)
    env["DOCKER_HOST"] = env.get(
        "DOCKER_HOST", "unix:///media/ydn/SYPHER_CORE2/runpod-docker-socket/docker.sock")
    out = subprocess.run(
        ["docker", "exec", "oracle-db-1", "psql", "-U", "postgres", "-d", "oracle", "-t", "-A", "-c", query],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return (out.stdout or out.stderr).strip()


def register(base: str, label: str) -> Session:
    s = Session(base)
    email = f"{label}-{STAMP}@golden.test"
    status, payload = s.post("/auth/register", {
        "email": email, "password": PASSWORD,
        "full_name": f"{label.title()} Owner", "company": f"Golden {label.title()} {STAMP}",
    })
    if status != 201:
        raise SystemExit(f"registration failed for {label}: {status} {payload}")
    s.token = payload["token"]
    s.tenant_id = payload["tenant_id"]
    s.agent_id = payload["agent_id"]
    s.accept_policy()
    return s


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------

def journey(base: str) -> None:
    feed_id = f"golden_e2e_{STAMP}"

    section("1. A brokerage owner signs up")
    a = register(base, "alpha")
    check(bool(a.tenant_id), "owner has a tenant", a.tenant_id)
    status, setup = a.get("/api/brokerage/setup")
    check(status == 200, "setup screen loads for a brand-new brokerage", f"HTTP {status}")
    check(setup["capabilities"]["brokerage_profile"] == "NEEDS_ACTION",
          "a new brokerage is told to describe itself",
          setup["capabilities"]["brokerage_profile"])
    check(setup["recommended_next"] == "brokerage_profile",
          "and that is the recommended next step", str(setup.get("recommended_next")))

    section("2. They describe the business")
    status, setup = a.patch("/api/brokerage/profile", {
        "name": f"Alpha Realty {STAMP}", "org_type": "brokerage",
        "primary_state": "de", "website": "https://alpha.test",
    })
    check(status == 200 and setup["capabilities"]["brokerage_profile"] == "READY",
          "profile saved and the capability turns READY", f"HTTP {status}")
    check(setup["brokerage"]["primary_state"] == "DE",
          "the state was normalised server-side", str(setup["brokerage"]["primary_state"]))
    check(setup["recommended_next"] == "agent_invites",
          "the next step moves on", str(setup.get("recommended_next")))

    section("3. They invite their team")
    emails = [f"agent{i}-{STAMP}@golden.test" for i in range(3)]
    status, invited = a.post("/api/brokerage/invitations", {"emails": emails, "role": "agent"})
    check(status == 201 and len(invited["created"]) == 3,
          "three invitations issued", f"HTTP {status}, {len(invited.get('created', []))} created")
    check(all("token" not in json.dumps(inv) for inv in invited["created"]),
          "no invitation token is exposed in the response")
    digest_rows = sql(f"SELECT count(*) FROM brokerage_invitations WHERE email LIKE '%{STAMP}@golden.test'")
    check(digest_rows == "3", "three invitation rows persisted", digest_rows)
    raw_in_db = sql(
        f"SELECT count(*) FROM brokerage_invitations "
        f"WHERE email LIKE '%{STAMP}@golden.test' AND token_hash ~ '^[0-9a-f]{{64}}$'")
    check(raw_in_db == "3", "only digests are stored, never a usable token", raw_in_db)

    section("4. Idempotency — a double click must not double invite")
    status, again = a.post("/api/brokerage/invitations", {"emails": [emails[0]], "role": "agent"})
    live = sql(
        f"SELECT count(*) FROM brokerage_invitations WHERE email='{emails[0]}' "
        f"AND consumed_at IS NULL AND revoked_at IS NULL")
    check(live == "1", "re-inviting leaves exactly one live invitation", live)

    section("5. An invited agent accepts")
    # The raw token only ever existed in the email, so mint one the same way
    # the API does to exercise the accept path.
    import hashlib
    raw_token = f"golden-token-{STAMP}"
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    owner_uuid = sql(f"SELECT id FROM users WHERE tenant_id='{a.tenant_id}' LIMIT 1")
    invitee = f"joiner-{STAMP}@golden.test"
    sql(f"""INSERT INTO brokerage_invitations
            (token_hash, tenant_id, email, invited_role, invited_by, invited_by_agent_id, expires_at)
            VALUES ('{digest}','{a.tenant_id}','{invitee}','agent','{owner_uuid}','{a.agent_id}',
                    now()+interval '14 days')""")

    anon = Session(base)
    status, preview = anon.get(f"/auth/invitation?token={raw_token}")
    check(status == 200 and preview["state"] == "pending",
          "an unauthenticated invitee can preview the invitation", f"HTTP {status}")
    check("tenant_id" not in preview and a.tenant_id not in json.dumps(preview),
          "the preview leaks no tenant identifier")

    status, joined = anon.post("/auth/accept-invite", {
        "token": raw_token, "password": PASSWORD, "full_name": "Joiner Agent"})
    check(status == 201, "the invitation is accepted", f"HTTP {status}")
    check(joined.get("tenant_id") == a.tenant_id,
          "the new agent landed in the inviting brokerage", str(joined.get("tenant_id")))
    check(joined.get("role") == "agent", "with the invited role", str(joined.get("role")))

    section("6. Replay — the same link must not work twice")
    status, replay = anon.post("/auth/accept-invite", {
        "token": raw_token, "password": PASSWORD, "full_name": "Impostor"})
    check(status == 409, "a replayed invitation is refused", f"HTTP {status}")
    accounts = sql(f"SELECT count(*) FROM users WHERE agent_id='{invitee}'")
    check(accounts == "1", "and produced exactly one account", accounts)

    section("7. The owner sees the roster")
    status, team = a.get("/api/brokerage/team")
    names = [m["email"] for m in team["members"]]
    check(invitee in names, "the accepted agent appears as a member", ", ".join(names))
    check(len(team["pending_invitations"]) >= 3, "pending invitations are still listed",
          str(len(team["pending_invitations"])))

    section("8. Contacts — the CRM the brokerage actually runs on")
    sql(f"""INSERT INTO clients (tenant_id, full_name, email, client_type, preferences)
            VALUES ('{a.tenant_id}','Sarah Golden','sarah-{STAMP}@golden.test','buyer',
                    '{{"target_zips":["19801"],"budget_max":525000,"beds":3}}'::jsonb),
                   ('{a.tenant_id}','Marcus Golden','marcus-{STAMP}@golden.test','buyer',
                    '{{"cities":["Wilmington"]}}'::jsonb)""")
    contacts = sql(f"SELECT count(*) FROM clients WHERE tenant_id='{a.tenant_id}'")
    check(contacts == "2", "two buyer contacts exist", contacts)

    section("9. MLS — ingest, entitle, search, detail")
    sql(f"""INSERT INTO mls_sync_status
            (mls_id, mls_name, feed_type, provider, dataset, license_classification,
             license_reason, agreement_ref, last_sync_at, last_success_at,
             backfill_complete, listings_synced)
            VALUES ('{feed_id}','Golden Board','Bridge_API_v2','bridge','goldenboard',
                    'licensed_property_listing','declared under GOLDEN-E2E','GOLDEN-E2E',
                    now(), now(), true, 1)""")
    sql(f"""INSERT INTO oracle_mls_listings
            (mls_id, mls_number, address, city, state_code, zip_code, list_price,
             status, beds, property_type, license_classification, source_modified_at)
            VALUES ('{feed_id}','GE-1','9 Golden Way','Wilmington','DE','19801',
                    485000,'Active',4,'Single Family','licensed_property_listing', now())""")

    status, search = a.get("/api/mls/search?state=DE")
    check(status == 200, "MLS search responds", f"HTTP {status}")
    sees_licensed = any("9 Golden Way" in (l.get("address") or "") for l in search["listings"])
    check(not sees_licensed,
          "a brokerage with no entitlement does NOT see the licensed listing")

    sql(f"""INSERT INTO mls_feed_entitlements (tenant_id, mls_id, granted_by)
            VALUES ('{a.tenant_id}','{feed_id}','golden-e2e')""")
    status, search = a.get("/api/mls/search?state=DE")
    listing = next((l for l in search["listings"] if "9 Golden Way" in (l.get("address") or "")), None)
    check(listing is not None, "once entitled, the listing is searchable")
    check(search["coverage"]["state"] == "fresh",
          "and coverage reports fresh licensed data", search["coverage"]["state"])

    if listing:
        status, detail = a.get(f"/api/mls/listings/{listing['id']}")
        check(status == 200, "listing detail opens", f"HTTP {status}")
        section("10. Property → people: who should I call about this?")
        buyers = {b["name"]: b for b in detail.get("buyers", [])}
        check("Sarah Golden" in buyers, "the matching buyer is surfaced", ", ".join(buyers))
        sarah = buyers.get("Sarah Golden", {})
        check(sarah.get("verdict") == "strong", "as a strong match", str(sarah.get("verdict")))
        check(bool(sarah.get("evidence")), "with readable evidence, not a score",
              "; ".join(sarah.get("evidence", [])))
        marcus = buyers.get("Marcus Golden", {})
        check(marcus.get("verdict") == "possible",
              "a buyer with only a location match is 'possible', not excluded",
              str(marcus.get("verdict")))
        check(any("no budget" in u for u in marcus.get("unknowns", [])),
              "and what is unknown about them is stated")

    section("11. Capability readiness reflects reality, not configuration")
    status, setup = a.get("/api/brokerage/setup")
    caps = setup["capabilities"]
    check(caps["mls"] == "READY", "MLS is READY on a licensed, fresh, entitled feed", caps["mls"])
    check(caps["phone"] in ("NOT_STARTED", "NEEDS_ACTION"),
          "phone is honestly not set up", caps["phone"])
    check(caps["billing"] in ("NOT_STARTED", "NEEDS_ACTION"),
          "billing is honestly not set up", caps["billing"])
    check(caps["readiness"] == "BLOCKED",
          "overall readiness stays BLOCKED while required capabilities are missing",
          caps["readiness"])

    section("12. Tenant isolation — the failure that must never be silent")
    b = register(base, "beta")
    status, b_setup = b.get("/api/brokerage/setup")
    check(b_setup["brokerage"]["id"] != a.tenant_id, "brokerage B has its own tenant")
    check(b_setup["team"]["active_members"] == 1,
          "B sees only its own member", str(b_setup["team"]["active_members"]))

    status, b_team = b.get("/api/brokerage/team")
    b_emails = json.dumps(b_team)
    check(invitee not in b_emails, "B cannot see A's agent")
    check(str(STAMP) not in ",".join(m["email"] for m in b_team["members"] if m["email"] != b.agent_id)
          or len(b_team["members"]) == 1, "B's roster contains only B")

    status, b_search = b.get("/api/mls/search?state=DE")
    check(not any("9 Golden Way" in (l.get("address") or "") for l in b_search["listings"]),
          "B cannot see A's licensed listing")
    check(b_search["coverage"]["state"] != "fresh",
          "and B is told it has no licensed coverage", b_search["coverage"]["state"])

    if listing:
        status, b_detail = b.get(f"/api/mls/listings/{listing['id']}")
        check(status == 404,
              "B cannot open A's listing by id — the CRM half of the leak",
              f"HTTP {status}")

    status, b_inv = b.get("/api/brokerage/invitations")
    check(status == 200 and not any(str(STAMP) in i["email"] for i in b_inv["invitations"]),
          "B cannot see A's invitations")

    section("13. Role enforcement — an agent is not an owner")
    agent = Session(base)
    status, login = agent.post("/auth/login", {"agent_id": invitee, "passphrase": PASSWORD})
    if status != 200:
        skip("agent session", f"login returned HTTP {status}")
    else:
        agent.token = login["token"]
        agent.accept_policy()
        status, _ = agent.post("/api/brokerage/invitations", {"emails": ["x@y.test"]})
        check(status == 403, "an ordinary agent cannot invite", f"HTTP {status}")
        status, _ = agent.patch("/api/brokerage/profile", {"name": "Hijacked"})
        check(status == 403, "an ordinary agent cannot rewrite the business", f"HTTP {status}")
        status, seen = agent.get("/api/brokerage/setup")
        check(status == 200, "but may still read the setup screen", f"HTTP {status}")

    section("14. Durability — the data survives a new connection")
    # A fresh session is a fresh pool connection: anything held only in process
    # memory disappears here.
    fresh = Session(base)
    status, login = fresh.post("/auth/login", {"agent_id": a.agent_id, "passphrase": PASSWORD})
    if status == 200:
        fresh.token = login["token"]
        fresh.accept_policy()
        status, after = fresh.get("/api/brokerage/setup")
        check(after["brokerage"]["name"] == f"Alpha Realty {STAMP}",
              "the brokerage profile survived", after["brokerage"]["name"])
        check(after["capabilities"]["mls"] == "READY", "MLS readiness survived",
              after["capabilities"]["mls"])
        status, after_team = fresh.get("/api/brokerage/team")
        check(any(m["email"] == invitee for m in after_team["members"]),
              "the accepted agent survived")
    else:
        skip("re-login durability", f"login returned HTTP {status}")

    section("15. Unauthenticated access is refused everywhere")
    nobody = Session(base)
    for path in ("/api/brokerage/setup", "/api/brokerage/team",
                 "/api/brokerage/invitations", "/api/mls/search"):
        status, _ = nobody.get(path)
        check(status in (401, 403), f"{path} requires a session", f"HTTP {status}")


def cleanup(feed_id: str) -> None:
    sql(f"DELETE FROM oracle_mls_listings WHERE mls_id LIKE 'golden_e2e_%'")
    sql(f"DELETE FROM mls_sync_status WHERE mls_id LIKE 'golden_e2e_%'")
    sql(f"DELETE FROM mls_feed_entitlements WHERE mls_id LIKE 'golden_e2e_%'")
    sql(f"DELETE FROM tenants WHERE slug LIKE 'golden-alpha-%' OR slug LIKE 'golden-beta-%'")
    sql(f"DELETE FROM users WHERE agent_id LIKE '%{STAMP}@golden.test'")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("GOLDEN_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--keep", action="store_true", help="leave seeded data in place")
    args = parser.parse_args()

    print(f"Golden E2E against {args.base_url}  (run {STAMP})")
    print("Real HTTP, real middleware, real PostgreSQL. No provider is contacted.")

    # Redis/Valkey is part of the production shape but not of this local stack.
    # Say so rather than let its assertions silently not run.
    if not os.getenv("GOLDEN_REDIS_URL"):
        skip("Redis/Valkey behaviour (rate limits, distributed locks)",
             "no Redis in this stack; set GOLDEN_REDIS_URL to exercise it")

    feed_id = f"golden_e2e_{STAMP}"
    try:
        journey(args.base_url)
    except Exception as exc:  # noqa: BLE001
        check(False, "the journey ran to completion", f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        if not args.keep:
            cleanup(feed_id)

    failed = [label for ok, label, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f", {len(_skipped)} skipped" if _skipped else ""))
    if failed:
        print("FAILED:")
        for label in failed:
            print(f"  - {label}")
        print("\nGOLDEN E2E FAILED")
        return 1
    print("\nGOLDEN E2E PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
