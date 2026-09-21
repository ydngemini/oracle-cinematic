#!/usr/bin/env python3
"""Give an onboarded AI agent its first caseload — a Mission, in shadow mode.

Run backend/scripts/onboard_ai_agent.py first to create the identity this
script acts as. That matters: a Mission's `created_by` is who its drafts sign
off as (missions/executor.py's `_stage` reads `load_agent_identity(ctx)`
using that same identity) and who its actions are attributed to — so this
script constructs a TenantContext with agent_id set to the AI'S OWN identity,
not whoever runs this script. That's not a login: password_hash is NULL for
that identity, so it structurally cannot authenticate through /auth/login —
this script talks to the database directly, the same way the mission
scheduler itself does, and calls the exact same create_mission/simulate_mission
functions the HTTP API uses (no duplicated SQL, no second code path).

Nothing here sends anything to anyone: `auto_channels` is always empty (every
send would still need a human's approval) and `simulate_mission` computes
what the mission WOULD do without doing it.

Usage:
    python3 backend/scripts/setup_ai_agent_mission.py --agent-id neoh-ai@neohrs.com
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

from tenancy import Role, TenantContext  # noqa: E402

DEFAULT_OBJECTIVE_TEXT = (
    "Work every open lead and past client like a real, real estate agent would: "
    "follow up on anyone who has gone quiet, re-engage past clients who might be "
    "ready to move again, and get qualified buyers and sellers to a scheduled "
    "conversation."
)


async def _tenant_id_for(agent_id: str) -> str:
    from db.connection import tenant_tx

    platform_ctx = TenantContext(
        agent_id="setup-ai-agent-mission",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE lower(agent_id) = lower($1)", agent_id,
        )
    if row is None:
        raise SystemExit(
            f"No user with agent_id={agent_id!r}. Run onboard_ai_agent.py first."
        )
    return str(row["tenant_id"])


async def main_async(agent_id: str) -> None:
    from db.connection import close_pool, init_pool

    await init_pool()
    try:
        await _create_and_simulate(agent_id)
    finally:
        await close_pool()


async def _create_and_simulate(agent_id: str) -> None:
    import missions_api
    from fastapi import HTTPException

    tenant_id = await _tenant_id_for(agent_id)
    # The AI's own identity, constructed directly — see module docstring for
    # why this is not a login and does not need one.
    ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.BROKER_OWNER)

    body = missions_api.MissionCreate(
        objective_kind="sphere_touched",
        objective_text=DEFAULT_OBJECTIVE_TEXT,
        allowed_channels=["email", "sms", "task"],
        auto_channels=[],
    )
    try:
        created = await missions_api.create_mission(body, ctx)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise SystemExit(
                "ORACLE_FEATURE_MISSIONS is off on this deployment. Set it to 1 "
                "in the backend's environment and restart, then re-run this script."
            ) from None
        raise
    mission = created["mission"]
    mission_id = mission["id"]
    print(f"Created mission {mission_id} ({mission['status']}/{mission['mode']}) "
          f"owned by {agent_id}.")

    simulated = await missions_api.simulate_mission(mission_id, ctx)
    result = simulated["simulation"]
    candidates = result.get("candidates", {})
    actions = result.get("actions", {})
    expected = result.get("expected", {})
    print("Simulation (nothing was sent):")
    print(f"  candidates analysed: {candidates.get('analysed')}  "
          f"(strong: {candidates.get('strong')}, recommended: {candidates.get('recommended')})")
    print(f"  planned actions: {actions.get('planned')} — by channel: {actions.get('by_channel')}")
    print(f"  expected replies: {expected.get('replies_low')}-{expected.get('replies_high')} "
          f"(uncalibrated prior — {result.get('caveat')})")
    print()
    print(f"Review it at GET /api/missions/{mission_id}/progress, or in the app at")
    print("  /work?type=missions")
    print()
    print("When ready to start ticking it (still shadow — still sends nothing):")
    print(f"  POST /api/missions/{mission_id}/launch  {{'mode': 'shadow'}}")
    print("  (needs ORACLE_MISSIONS_ENABLED=1 so the scheduler actually sweeps it)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, help="The onboarded AI agent's identity")
    args = parser.parse_args()
    asyncio.run(main_async(args.agent_id))


if __name__ == "__main__":
    main()
