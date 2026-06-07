#!/usr/bin/env python3
"""
Oracle — Memory Core ignition.

Builds the Memory Core's physical tables (user_profiles, user_interactions) from
MEMORY_SCHEMA_SQL and seeds the 'demo-operator' profile, so the C2 UI shows a
live, stateful connection the instant the React app mounts: the WebSocket fires,
restore_session() reads this exact row from PostgreSQL, and SESSION_RESTORED
lights the 'MEMORY SYNC: ACTIVE' indicator.

Idempotent — CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT DO UPDATE — so
re-running just refreshes the seed.

Run from the repo root:
    python3 backend/scripts/ignite_memory.py

Connects with plain password auth (local dev), honoring env vars:
    ORACLE_DB_HOST        (localhost)
    ORACLE_DB_PORT        (5432)
    ORACLE_DB_NAME        (oracle)
    ORACLE_DB_ADMIN_USER  (postgres)   — needs CREATE TABLE; use the superuser,
                                          NOT the runtime oracle_app_login role.
    ORACLE_DB_PASSWORD    (postgres)
  or a single DATABASE_URL that overrides all of the above.

Need a database first? A throwaway one matching these defaults:
    docker run -d --name oracle_db -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=oracle -p 5432:5432 postgres:16-alpine
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make `memory_core` importable when invoked as `python3 backend/scripts/...`
# from the repo root (sys.path[0] would otherwise be backend/scripts).
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from memory_core.session_manager import MEMORY_SCHEMA_SQL  # noqa: E402


# --- the mock operator ----------------------------------------------------- #
DEMO = {
    "user_id": "demo-operator",
    "tenant_id": "nexaswarm-alpha",
    # The brief said "70.0", but target_mao_pct is the 70%-rule *fraction*
    # (column numeric(4,3); the UI renders it ×100 → "70%"). 70.0 would overflow
    # the column and display as 7000%, so the correct stored value is 0.70.
    "target_mao_pct": 0.70,
    "target_markets": ["New Castle", "Kent"],
    "profile_summary": "Aggressive buyer focusing on high-equity pre-foreclosures in Delaware.",
}

SEED_SQL = """
INSERT INTO user_profiles
       (user_id, tenant_id, target_mao_pct, target_markets,
        profile_summary, summary_updated_at)
VALUES ($1, $2, $3, $4::jsonb, $5, now())
ON CONFLICT (user_id) DO UPDATE
   SET tenant_id          = EXCLUDED.tenant_id,
       target_mao_pct     = EXCLUDED.target_mao_pct,
       target_markets     = EXCLUDED.target_markets,
       profile_summary    = EXCLUDED.profile_summary,
       summary_updated_at = now();
"""


def _conn_kwargs() -> dict:
    """Resolve connection params from DATABASE_URL or the ORACLE_DB_* env vars,
    defaulting to the local postgres:16 dev-container convention."""
    url = os.getenv("DATABASE_URL")
    if url:
        return {"dsn": url}
    return {
        "host": os.getenv("ORACLE_DB_HOST", "localhost"),
        "port": int(os.getenv("ORACLE_DB_PORT", "5432")),
        "database": os.getenv("ORACLE_DB_NAME", "oracle"),
        "user": os.getenv("ORACLE_DB_ADMIN_USER", "postgres"),
        "password": os.getenv("ORACLE_DB_PASSWORD", "postgres"),
    }


async def main() -> int:
    import asyncpg  # lazy: importing this module never requires the driver

    kwargs = _conn_kwargs()
    where = kwargs.get("dsn") or (
        f"{kwargs['user']}@{kwargs['host']}:{kwargs['port']}/{kwargs['database']}"
    )
    print(f"==> connecting to {where}")
    try:
        conn = await asyncpg.connect(**kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not connect ({e}).", file=sys.stderr)
        print("      Start a local Postgres, e.g.:", file=sys.stderr)
        print("        docker run -d --name oracle_db -e POSTGRES_PASSWORD=postgres \\",
              file=sys.stderr)
        print("          -e POSTGRES_DB=oracle -p 5432:5432 postgres:16-alpine",
              file=sys.stderr)
        return 2

    try:
        print("==> creating Memory Core tables (MEMORY_SCHEMA_SQL)")
        await conn.execute(MEMORY_SCHEMA_SQL)

        print("==> seeding demo-operator")
        await conn.execute(
            SEED_SQL,
            DEMO["user_id"],
            DEMO["tenant_id"],
            DEMO["target_mao_pct"],
            json.dumps(DEMO["target_markets"]),
            DEMO["profile_summary"],
        )

        row = await conn.fetchrow(
            "SELECT user_id, tenant_id, target_mao_pct, target_markets, profile_summary "
            "FROM user_profiles WHERE user_id = $1",
            DEMO["user_id"],
        )
        mao = float(row["target_mao_pct"])
        print("==> Memory Core ONLINE. Seeded profile:")
        print(f"    user_id        : {row['user_id']}")
        print(f"    tenant_id      : {row['tenant_id']}")
        print(f"    target_mao_pct : {mao}  (UI renders {round(mao * 100)}%)")
        print(f"    target_markets : {row['target_markets']}")
        print(f"    profile_summary: {row['profile_summary']}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
