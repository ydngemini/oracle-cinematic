#!/usr/bin/env python3
"""Onboard an AI persona as a real agent seat — like hiring a new agent.

Creates one `users` row (role='agent', is_active=true) with `password_hash`
left NULL. `auth._verify_pw()` returns False on a null hash unconditionally,
so this identity can never authenticate through /auth/login — it is a real
employee record, not a credential. It can only ever act through the Missions
engine (backend/missions/), constructed directly as a TenantContext by the
scheduler, never as a login.

Also writes the matching `user_profiles` row: a display name (what drafted
messages sign off as, since missions/executor.py's `_stage` now reads
`load_agent_identity(ctx)["name"]` rather than the raw agent_id) and a
public_email. It does NOT configure the SMTP credential that email actually
sends through — that is a secret and belongs in ProviderDeliveryPage.jsx's
"Your email (SMTP)" form (or the provider API directly), submitted by a
broker-owner, never pasted into a script or a chat transcript.

Usage:
    ORACLE_OWNER_AGENT_ID=you@example.com \\
        python3 backend/scripts/onboard_ai_agent.py \\
            --agent-id neoh-ai@neohrs.com \\
            --name "Neoh" \\
            --public-email you@example.com

Run from the repo root or from backend/ — either works.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from db.connection import tenant_tx  # noqa: E402
from tenancy import Role, TenantContext  # noqa: E402


async def _owner_tenant_id(owner_agent_id: str) -> str:
    """Onboard the new agent into the same tenant as an existing user, found
    by their own agent_id — the same "which brokerage is this for" question
    you'd ask when actually hiring someone."""
    platform_ctx = TenantContext(
        agent_id="onboard-ai-agent",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE lower(agent_id) = lower($1)",
            owner_agent_id,
        )
    if row is None:
        raise SystemExit(
            f"No existing user with agent_id={owner_agent_id!r}. Pass "
            "--tenant-id directly, or set ORACLE_OWNER_AGENT_ID to a real "
            "account on this deployment."
        )
    return str(row["tenant_id"])


async def onboard(*, agent_id: str, name: str, public_email: str, brokerage: str, tenant_id: str) -> None:
    ctx = TenantContext(agent_id="onboard-ai-agent", tenant_id=tenant_id, role=Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        user = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, agent_id, role, is_active, password_hash, full_name)
            VALUES ($1::uuid, $2, 'agent', true, NULL, $3)
            ON CONFLICT (agent_id) DO UPDATE SET full_name = EXCLUDED.full_name
            RETURNING id, agent_id
            """,
            tenant_id,
            agent_id,
            name,
        )
        await conn.execute(
            """
            INSERT INTO user_profiles (user_id, tenant_id, display_name, public_email, brokerage)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                public_email = EXCLUDED.public_email,
                brokerage = EXCLUDED.brokerage
            """,
            agent_id,
            tenant_id,
            name,
            public_email,
            brokerage,
        )

    print(f"Onboarded {name!r} as agent_id={agent_id!r} in tenant {tenant_id}.")
    print("  password_hash is NULL — this identity cannot log in.")
    print()
    print("Next:")
    print(f"  1. As broker-owner, submit its SMTP credential (account_label={agent_id!r})")
    print("     via the 'Your email (SMTP)' form in Providers, or POST /api/sales/providers/smtp")
    print(f"     directly — from_email={public_email!r}, using a Gmail app password, never your")
    print("     account password.")
    print(f"  2. python3 backend/scripts/setup_ai_agent_mission.py --agent-id {agent_id}")
    print("     gives it its first caseload as a Mission, in shadow mode — drafts sign off")
    print(f"     as {name!r} (missions/executor.py resolves the drafting identity from the")
    print("     mission's own owner, not a fixed service account).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, help="Unique internal identity, e.g. neoh-ai@neohrs.com")
    parser.add_argument("--name", required=True, help="Display name drafts sign off as, e.g. Neoh")
    parser.add_argument("--public-email", required=True, help="Address replies should go to")
    parser.add_argument("--brokerage", default="", help="Shown alongside the agent's name")
    parser.add_argument("--tenant-id", default="", help="Skip owner lookup and use this tenant directly")
    args = parser.parse_args()

    async def _run() -> None:
        from db.connection import close_pool, init_pool

        await init_pool()
        try:
            tenant_id = args.tenant_id
            if not tenant_id:
                owner = os.environ.get("ORACLE_OWNER_AGENT_ID", "").strip()
                if not owner:
                    raise SystemExit("Set --tenant-id or ORACLE_OWNER_AGENT_ID.")
                tenant_id = await _owner_tenant_id(owner)
            await onboard(
                agent_id=args.agent_id, name=args.name, public_email=args.public_email,
                brokerage=args.brokerage, tenant_id=tenant_id,
            )
        finally:
            await close_pool()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
