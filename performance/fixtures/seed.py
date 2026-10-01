#!/usr/bin/env python3
"""Seed deterministic synthetic brokerages for load testing.

No real customer data, no real provider identifiers. Every tenant owns a unique
SENTINEL token that appears in every one of its client names and emails; the
load scenarios fail the moment a response carries another tenant's sentinel.
That makes tenant isolation a property the load test checks on EVERY response,
not an assertion somebody remembered to add.

Four brokerage shapes (§7), so load comes from tenants of different sizes and
exercises different RLS/index/cache patterns (§13):

    solo        1 agent      200 clients
    small       5 agents   1,000 clients
    brokerage  25 agents   5,000 clients
    large     100 agents  20,000 clients

Profiles scale the NUMBER of each (--profile tiny|pilot). The existing database
already carries the data-VOLUME dimension — ~10M leads and ~10.6M public
property records — so client/user counts are what is scaled here.

Deterministic: ids are uuid5 of stable names, so a re-seed produces the same
rows and `--cleanup` removes exactly what was seeded (tenants whose slug starts
with "perf-", and everything hanging off them).

Writes performance/fixtures/out/users.json — synthetic credentials the k6
scenarios log in with. Gitignored.

Usage (inside a perf container, which has the backend modules):
  PERF_DB_DSN=postgresql://… python /perf/fixtures/seed.py --profile tiny
  PERF_DB_DSN=postgresql://… python /perf/fixtures/seed.py --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import uuid

sys.path.insert(0, "/app")

NS = uuid.UUID("5e4a1d2c-9f0b-4c3e-8a71-0d6b2f9e4c10")
SLUG_PREFIX = "perf-"
OUT = pathlib.Path(os.environ.get("PERF_FIXTURE_OUT", "/perf-out"))

SHAPES = {
    "solo":      {"agents": 1,   "clients": 200,    "leads": 50},
    "small":     {"agents": 5,   "clients": 1_000,  "leads": 250},
    "brokerage": {"agents": 25,  "clients": 5_000,  "leads": 1_250},
    "large":     {"agents": 100, "clients": 20_000, "leads": 5_000},
}
PROFILES = {
    # 10 tenants, 169 users, 33,800 clients — fits a 4-core laptop.
    "tiny":  {"solo": 4, "small": 3, "brokerage": 2, "large": 1},
    # 50 tenants — the pilot. Meant for staging, not a laptop.
    "pilot": {"solo": 20, "small": 20, "brokerage": 8, "large": 2},
}

FIRST = ["Avery", "Blake", "Casey", "Drew", "Emery", "Finley", "Gray", "Harper",
         "Indy", "Jules", "Kai", "Logan", "Morgan", "Noel", "Oakley", "Parker",
         "Quinn", "Reese", "Sage", "Taylor"]
LAST = ["Abbott", "Barnes", "Chen", "Diaz", "Ellis", "Fox", "Garcia", "Hayes",
        "Ito", "Jensen", "Kim", "Lopez", "Moss", "Nash", "Ortiz", "Patel",
        "Quint", "Reyes", "Shah", "Tran"]
STAGES = ["lead", "active", "nurture", "under_contract", "closed", "lost"]
TYPES = ["buyer", "seller", "both"]


def uid(*parts) -> uuid.UUID:
    return uuid.uuid5(NS, "/".join(str(p) for p in parts))


def sentinel(slug: str) -> str:
    """Unique, unguessable-looking, greppable: SNTL + 10 hex of the slug."""
    return "SNTL" + hashlib.sha256(slug.encode()).hexdigest()[:10].upper()


def password_for(agent_id: str) -> str:
    seed = os.environ.get("PERF_PASSWORD_SEED", "neoh-perf-2026")
    return "Pf!" + hashlib.sha256(f"{seed}:{agent_id}".encode()).hexdigest()[:20]


def plan(profile: str) -> list[dict]:
    tenants = []
    for shape, count in PROFILES[profile].items():
        for i in range(1, count + 1):
            slug = f"{SLUG_PREFIX}{shape}-{i:02d}"
            tenants.append({"slug": slug, "shape": shape, "id": uid("tenant", slug),
                            "sentinel": sentinel(slug), **SHAPES[shape]})
    return tenants


async def seed(dsn: str, profile: str) -> dict:
    import asyncpg
    # config first, exactly as server.py does: it resolves the JWT issuer and
    # audience from the public base URL and publishes them before auth.py reads
    # them at import. Importing auth alone fails closed outside development.
    import config  # noqa: F401
    from auth import _hash_pw
    from policy_contract import PLATFORM_POLICY_VERSION

    conn = await asyncpg.connect(dsn)
    tenants = plan(profile)
    users_out = []
    totals = {"tenants": 0, "users": 0, "clients": 0}
    # Passwords are hashed with the backend's own _hash_pw so login verifies
    # them exactly as it would a real user's. scrypt is deliberately slow, so
    # each hash runs off the event loop.
    loop = asyncio.get_running_loop()
    try:
        for t in tenants:
            await conn.execute(
                "INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3) ON CONFLICT (id) DO NOTHING",
                t["id"], t["slug"], f"Perf {t['shape'].title()} {t['slug'][-2:]} ({t['sentinel']})",
            )
            totals["tenants"] += 1

            agent_rows = []
            for a in range(t["agents"]):
                role = "broker_owner" if a == 0 else "agent"
                agent_id = f"{t['slug']}-a{a:03d}@perf.invalid"
                pw = password_for(agent_id)
                pw_hash = await loop.run_in_executor(None, _hash_pw, pw)
                user_id = uid("user", agent_id)
                agent_rows.append((user_id, t["id"], agent_id, role, pw_hash, agent_id,
                                   f"Perf Agent {a:03d} {t['sentinel']}"))
                users_out.append({"tenant_slug": t["slug"], "tenant_id": str(t["id"]),
                                  "sentinel": t["sentinel"], "shape": t["shape"],
                                  "agent_id": agent_id, "password": pw, "role": role})
            await conn.executemany(
                """INSERT INTO users (id, tenant_id, agent_id, role, password_hash, email, full_name)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (id) DO NOTHING""", agent_rows)
            await conn.executemany(
                """INSERT INTO user_policy_acceptances (tenant_id, user_id, policy_version)
                   SELECT $1, $2, $3 WHERE NOT EXISTS (
                     SELECT 1 FROM user_policy_acceptances WHERE user_id=$2 AND policy_version=$3)""",
                [(t["id"], r[0], PLATFORM_POLICY_VERSION) for r in agent_rows])
            totals["users"] += len(agent_rows)

            client_rows = []
            for c in range(t["clients"]):
                first, last = FIRST[c % 20], LAST[(c // 20) % 20]
                client_rows.append((
                    uid("client", t["slug"], c), t["id"],
                    f"{first} {last} {c:05d} {t['sentinel']}",
                    f"{first.lower()}.{last.lower()}.{c:05d}.{t['sentinel'].lower()}@perf.invalid",
                    f"+1555{(c % 10_000_000):07d}",
                    STAGES[c % len(STAGES)], TYPES[c % len(TYPES)], c % 101,
                    agent_rows[c % len(agent_rows)][2],
                ))
            await conn.executemany(
                """INSERT INTO clients (id, tenant_id, full_name, email, phone, stage, client_type,
                                        lead_score, assignee_id, source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'perf-fixture') ON CONFLICT (id) DO NOTHING""",
                client_rows)
            totals["clients"] += len(client_rows)

            # Leads (properties in the pipeline): the WebSocket fan-out trigger
            # (PATCH /api/leads/{id}/dossier-status) and reconstruction jobs
            # need them. The sentinel rides in the payload's owner name.
            lead_rows = [(
                uid("lead", t["slug"], n), t["id"], f"PERF-{t['slug']}-{n:06d}", "TX",
                (n * 37) % 100,
                json.dumps({"owner_name": f"Owner {n:06d} {t['sentinel']}",
                            "address": f"{100 + n} Perf Way", "city": "Austin"}),
            ) for n in range(t.get("leads", 0))]
            await conn.executemany(
                """INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, payload)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT DO NOTHING""", lead_rows)
            totals["leads"] = totals.get("leads", 0) + len(lead_rows)

            # Telephony + messaging routes for the owner, so the Telnyx and
            # Plivo webhook load tests resolve an inbound event to this tenant
            # exactly as production does. Synthetic +1555 numbers only.
            route = route_for(t)
            owner = agent_rows[0][2]
            await conn.execute(
                """INSERT INTO telephony_routes (tenant_id, agent_id, endpoint_key, inbound_did,
                        voice_caller_id_e164, voice_caller_id_verified, provider, provider_account_id,
                        forward_on_request, forward_when_ai_unavailable, outbound_verification_status)
                   VALUES ($1,$2,$3,$4,$4,true,'plivo',$5,false,false,'verified') ON CONFLICT DO NOTHING""",
                t["id"], owner, route["endpoint_key"], route["did"], plivo_account_id())
            await conn.execute(
                """INSERT INTO messaging_routes (tenant_id, agent_id, provider, hosted_order_status, active)
                   VALUES ($1,$2,'telnyx','active',true) ON CONFLICT DO NOTHING""", t["id"], owner)
            print(f"  {t['slug']:22} {t['agents']:>3} agents {t['clients']:>6} clients  {t['sentinel']}")
    finally:
        await conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "users.json").write_text(json.dumps({"profile": profile, "users": users_out}, indent=1))
    # 0644: k6 runs as its own uid and must read this. It holds only synthetic
    # credentials for users that exist in a loadtest environment, and lives in
    # a gitignored directory.
    (OUT / "users.json").chmod(0o644)
    return totals


def route_for(t: dict) -> dict:
    """A tenant's synthetic inbound number and webhook endpoint key."""
    n = int(hashlib.sha256(t["slug"].encode()).hexdigest()[:6], 16) % 10_000_000
    return {"did": f"+1555{n:07d}", "endpoint_key": uid("route", t["slug"])}


def plivo_account_id() -> str:
    """Must equal the perf env's PLIVO_AUTH_ID (fixtures/perf_secrets.py),
    or inbound Plivo webhooks are refused as the wrong account."""
    try:
        return json.loads((OUT / "perf-secrets.json").read_text())["plivo_auth_id"]
    except (OSError, ValueError, KeyError):
        return "MAPERF0000000000"


def write_lead_ids(profile: str, per_tenant: int = 50) -> None:
    """out/leads.json — ids of each tenant's first seeded leads. Computed, not
    queried: ids are uuid5 of stable names, so this needs no database and is
    exactly what `seed` inserted. Scenarios use them as safe mutation targets
    (dossier status, reconstruction) inside the right tenant."""
    out = {t["slug"]: [str(uid("lead", t["slug"], n)) for n in range(min(per_tenant, t["leads"]))]
           for t in plan(profile)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "leads.json").write_text(json.dumps(out))
    (OUT / "leads.json").chmod(0o644)
    routes = {t["slug"]: {"did": route_for(t)["did"], "endpoint_key": str(route_for(t)["endpoint_key"]),
                          "tenant_id": str(t["id"]), "sentinel": t["sentinel"]} for t in plan(profile)}
    (OUT / "routes.json").write_text(json.dumps(routes))
    (OUT / "routes.json").chmod(0o644)


async def seed_routes(dsn: str, profile: str) -> int:
    """Only the webhook routes, for an already-seeded profile (no password
    hashing, no bulk rows) — cheap enough to run beside a measurement."""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        for t in plan(profile):
            route, owner = route_for(t), f"{t['slug']}-a000@perf.invalid"
            await conn.execute(
                """INSERT INTO telephony_routes (tenant_id, agent_id, endpoint_key, inbound_did,
                        voice_caller_id_e164, voice_caller_id_verified, provider, provider_account_id,
                        forward_on_request, forward_when_ai_unavailable, outbound_verification_status)
                   VALUES ($1,$2,$3,$4,$4,true,'plivo',$5,false,false,'verified') ON CONFLICT DO NOTHING""",
                t["id"], owner, route["endpoint_key"], route["did"], plivo_account_id())
            await conn.execute(
                """INSERT INTO messaging_routes (tenant_id, agent_id, provider, hosted_order_status, active)
                   VALUES ($1,$2,'telnyx','active',true) ON CONFLICT DO NOTHING""", t["id"], owner)
    finally:
        await conn.close()
    write_lead_ids(profile)
    return len(plan(profile))


async def cleanup(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants WHERE slug LIKE $1", SLUG_PREFIX + "%")]
        if not ids:
            print("  nothing to clean up")
            return
        # Every table with a tenant_id column, children before parents. Derived
        # from the catalog so rows created by write scenarios go too.
        tables = [r["table_name"] for r in await conn.fetch("""
            SELECT c.table_name FROM information_schema.columns c
              JOIN information_schema.tables t USING (table_schema, table_name)
             WHERE c.table_schema='public' AND c.column_name='tenant_id'
               AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'""")]
        for attempt in range(6):
            remaining = []
            for tbl in tables:
                try:
                    await conn.execute(f'DELETE FROM "{tbl}" WHERE tenant_id = ANY($1::uuid[])', ids)
                except asyncpg.ForeignKeyViolationError:
                    remaining.append(tbl)
            tables = remaining
            if not tables:
                break
        await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)
        print(f"  removed {len(ids)} perf tenants and their rows"
              + (f" (could not clear: {tables})" if tables else ""))
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=sorted(PROFILES), default="tiny")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--routes-only", action="store_true",
                    help="insert only the webhook routes for an already-seeded profile")
    ap.add_argument("--ids-only", action="store_true",
                    help="write out/leads.json for an already-seeded profile; no database")
    args = ap.parse_args()
    if args.routes_only:
        if not os.environ.get("PERF_DB_DSN"):
            print("PERF_DB_DSN is required", file=sys.stderr)
            return 2
        print(f"  routes seeded for {asyncio.run(seed_routes(os.environ['PERF_DB_DSN'], args.profile))} tenants")
        return 0
    if args.ids_only:
        write_lead_ids(args.profile)
        return 0
    dsn = os.environ.get("PERF_DB_DSN")
    if not dsn:
        print("PERF_DB_DSN is required — seeding never guesses a database", file=sys.stderr)
        return 2
    if args.cleanup:
        asyncio.run(cleanup(dsn))
        return 0
    totals = asyncio.run(seed(dsn, args.profile))
    write_lead_ids(args.profile)
    print(f"\n  seeded: {totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
