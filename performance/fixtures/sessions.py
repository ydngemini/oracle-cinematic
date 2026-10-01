#!/usr/bin/env python3
"""Mint one session per synthetic user, the way /auth/login does, once.

Why not just log in: the load scenarios measure the APPLICATION. /auth/login is
limited to 10 requests per minute per client IP, and every simulated user comes
from the load generator's single IP, so logging 169 users in through the
endpoint would take 17 minutes and measure the login limiter rather than Neoh.
§15 asks for exactly this separation: an authentication storm is its own
scenario (auth_storm.js), and normal load reuses established sessions, as real
users do.

The token is issued with auth._issue_jwt using the same arguments login passes
— subject, the user's real tenant and role from the database — and signed with
the target environment's own ORACLE_SECRET_KEY. It is a real session, not a
shortcut around authorization: every request still goes through the same
verification, RLS and rate limiting.

Writes performance/out/sessions.json (gitignored).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, "/app")

OUT = pathlib.Path(os.environ.get("PERF_FIXTURE_OUT", "/perf-out"))


async def main() -> int:
    import asyncpg
    import config  # noqa: F401 — publishes JWT issuer/audience before auth imports
    from auth import _issue_jwt

    users = json.loads((OUT / "users.json").read_text())["users"]
    dsn = os.environ.get("PERF_DB_DSN")
    if dsn:
        # Preferred: take tenant and role from the database, so a session can
        # never claim more than the user actually holds.
        conn = await asyncpg.connect(dsn)
        try:
            rows = {r["agent_id"]: r for r in await conn.fetch(
                "SELECT agent_id, tenant_id::text AS tenant_id, role, id::text AS id, session_epoch "
                "FROM users WHERE agent_id = ANY($1)",
                [u["agent_id"] for u in users])}
        finally:
            await conn.close()
    else:
        # No privileged DSN: use what the seed recorded when it created the
        # users. A stale fixture only yields tokens the server then rejects or
        # scopes by its own RLS — it cannot widen access.
        print("  PERF_DB_DSN unset — using tenant/role recorded by the seed", file=sys.stderr)
        rows = {u["agent_id"]: {"tenant_id": u["tenant_id"], "role": u["role"]} for u in users}

    sessions = []
    for u in users:
        row = rows.get(u["agent_id"])
        if row is None:
            print(f"  {u['agent_id']} is not in the database — re-run the seed", file=sys.stderr)
            return 1
        sessions.append({
            "agent_id": u["agent_id"],
            "tenant_slug": u["tenant_slug"],
            "shape": u["shape"],
            "sentinel": u["sentinel"],
            "role": row["role"],
            # Bound to the account row and its epoch exactly as login binds
            # them (0117), so the load runs the production session check.
            "token": _issue_jwt(u["agent_id"], row["tenant_id"], row["role"],
                                user_id=row.get("id"), session_epoch=int(row.get("session_epoch") or 0)),
        })
    (OUT / "sessions.json").write_text(json.dumps({"sessions": sessions}))
    (OUT / "sessions.json").chmod(0o644)
    print(f"  minted {len(sessions)} sessions (24 h)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
