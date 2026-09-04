"""One tick of one mission: refresh, plan, evaluate, and — sometimes — stage.

The shape that matters is that every quarter of this loop runs on every tick,
whatever the mission's mode and whatever the deployment can send. Candidates
are refreshed, a plan is made, each due action is put through the full policy
gate, and the verdict is written to the action row. Only the last step differs:

    shadow, or blocked, or no credential  →  state='would_have_done'
    live and permitted                    →  stage_command(...) → 'staged'
    ... and a consented grant             →  release_command(...) → 'approved'

That is why a shadow run is worth reading. It is not a shorter code path
wearing the same name; it is the same path with the final step withheld, and
the reason it was withheld is recorded on the row the schema requires it on.

There is no new send path here. Staging goes through `stage_command`, which
every other command in this product uses, and releasing goes through
`release_command`, extracted from the body of the endpoint a person's Approve
click already calls. A mission that releases its own approval therefore leaves
exactly the audit trail a human approval leaves: one decision record, one job,
one state transition.

**Dormancy is Feature.MISSIONS, default off.** Not the absence of credentials:
that assumption was tested against the running stack and found false.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from platform_policy import Feature, feature_enabled
from tenancy import TenantContext

from . import costs, planner, policy, simulator

logger = logging.getLogger(__name__)

#: A tick will not evaluate more than this many actions, so one runaway
#: mission cannot monopolise the worker.
MAX_ACTIONS_PER_TICK = 25


def enabled() -> bool:
    """Default OFF. See Feature.MISSIONS for why this is the off switch."""
    return feature_enabled(Feature.MISSIONS, default=False)


async def tick(ctx: TenantContext, mission_id: str) -> dict[str, Any]:
    """Advance one mission by one step. Returns what happened, for the journal."""
    # First line, before even an import: a disabled deployment does not read a
    # mission and decide against it, it does not look.
    if not enabled():
        return {"skipped": "missions are not enabled on this deployment"}

    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        mission = await conn.fetchrow(
            "SELECT * FROM missions WHERE id = $1::uuid", mission_id)
    if mission is None:
        return {"skipped": "mission not found"}
    mission = dict(mission)
    if mission["status"] not in ("shadow", "active"):
        return {"skipped": f"mission is {mission['status']}"}

    result: dict[str, Any] = {
        "mission_id": mission_id, "planned": 0, "staged": 0,
        "released": 0, "withheld": 0, "blocked": 0,
    }

    # 1. Plan, but only when nothing is already pending. Planning costs a model
    #    call, and re-planning over a queue that has not been worked yet would
    #    pay for it repeatedly to produce the same sequence.
    pending = await _count_planned(ctx, mission_id)
    if pending == 0:
        result["planned"] = await _plan(ctx, mission)

    # 2. Work what is due.
    due = await _due_actions(ctx, mission_id)
    for action in due[:MAX_ACTIONS_PER_TICK]:
        outcome = await _work_one(ctx, mission, action)
        result[outcome] = result.get(outcome, 0) + 1

    return result


async def _plan(ctx: TenantContext, mission: dict[str, Any]) -> int:
    """Refresh candidates, ask for a sequence, write the actions."""
    from db.connection import tenant_tx

    candidates = await _refresh_candidates(ctx, mission)
    if not candidates:
        await _journal(ctx, mission["id"], "planned",
                       {"steps": 0, "reason": "no candidates matched"})
        return 0

    try:
        steps, dropped, reasoning = await planner.propose_plan(ctx, mission, candidates)
    except planner.PlanUnavailable as exc:
        # The mission stays where it is. No fabricated fallback plan.
        await _journal(ctx, mission["id"], "plan_failed", {"error": str(exc)[:500]})
        logger.warning("mission %s: planning failed: %s", mission["id"], exc)
        return 0

    launched = mission.get("launched_at") or datetime.now(timezone.utc)
    async with tenant_tx(ctx) as conn:
        for step in steps:
            await conn.execute(
                """INSERT INTO mission_actions
                       (tenant_id, mission_id, candidate_id, step_index, channel,
                        due_at, cost_cents, state)
                   VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, 'planned')""",
                ctx.tenant_id, mission["id"], step["candidate_id"],
                step["step_index"], step["channel"],
                launched + timedelta(days=int(step["day_offset"])),
                costs.unit_cost_cents(step["channel"]),
            )

    await _journal(ctx, mission["id"], "planned", {
        "steps": len(steps),
        "dropped": [d["reason"] for d in dropped],
        "summary": planner.plan_summary(steps, dropped),
        "reasoning": reasoning[:500],
    })
    return len(steps)


async def _work_one(
    ctx: TenantContext, mission: dict[str, Any], action: dict[str, Any],
) -> str:
    """Evaluate one action and record what happened. Returns a result key."""
    contact, state_code = await _contact_for(ctx, action)
    verdict = await policy.evaluate_action(
        ctx, mission, action, contact=contact, state_code=state_code)

    if not verdict.may_stage:
        # Enriched in place — never a second row. The count of actions stays
        # the count of intentions whether or not anything was sent.
        await _set_state(
            ctx, action["id"], "would_have_done",
            blocked_reason=verdict.blocked_reason or verdict.reason,
        )
        await _journal(ctx, mission["id"], "action_withheld", {
            "action_id": str(action["id"]),
            "channel": action["channel"],
            "reason": verdict.blocked_reason,
            "dial": verdict.dial_reason,
        })
        return "withheld"

    if action["channel"] == "task":
        # Internal work with no send behind it. The command surface is EMAIL,
        # SMS, CALL and CALENDAR — all outbound — so there is nothing here to
        # stage. Recorded as skipped with the reason rather than dressed up as
        # a failure or, worse, promoted into an outbound command.
        await _set_state(
            ctx, action["id"], "skipped",
            blocked_reason="task actions are not executed yet — no internal task surface",
        )
        return "withheld"

    staged, error = await _stage(ctx, mission, action, verdict)
    if staged is None:
        # 'blocked', not 'failed': the usual cause is a command this mission
        # cannot yet complete (no message body, no state_code), which is a
        # missing capability rather than a fault. The reason names which.
        await _set_state(
            ctx, action["id"], "blocked",
            blocked_reason=f"cannot stage: {error}" if error else "cannot stage",
        )
        await _journal(ctx, mission["id"], "action_blocked", {
            "action_id": str(action["id"]), "reason": error,
        })
        return "blocked"

    await _set_state(ctx, action["id"], "staged", command_id=staged["id"])
    await _journal(ctx, mission["id"], "action_staged", {
        "action_id": str(action["id"]), "command_id": str(staged["id"]),
    })

    if not verdict.may_release:
        return "staged"

    released = await _release(ctx, staged)
    if not released:
        return "staged"
    await _set_state(ctx, action["id"], "approved", command_id=staged["id"])
    await _journal(ctx, mission["id"], "action_released", {
        "action_id": str(action["id"]),
        "authority": verdict.release_authority,
    })
    return "released"


async def _stage(
    ctx: TenantContext, mission: dict[str, Any], action: dict[str, Any],
    verdict: policy.Verdict,
) -> tuple[Optional[dict[str, Any]], str]:
    """The one staging path in this product. Not a second one.

    Returns (command, error). The error is carried rather than swallowed
    because it is usually not a fault — it is the command layer saying the
    command is incomplete, and the agent needs to read which part.

    **What a mission cannot yet produce.** `stage_command` validates a whole
    command, and two of its requirements have no source in a mission today:

      * `draft.body` — the message itself. The planner sequences contacts and
        writes a one-line INTENT for the agent; it does not write the text that
        goes to the client. Drafting is a separate job from sequencing and is
        not built here, so a staged mission command currently has no body.
      * `target.state_code` — a two-letter state, required for SMS. The
        `clients` table has no state column (see `_contact_for`), so there is
        nothing to fill it from.

    Both are recorded on the action as blocked_reason rather than hidden, so a
    mission that cannot act says which piece is missing.
    """
    from commands_api import stage_command

    try:
        command = await stage_command(
            ctx,
            command_type=_command_type(action["channel"]),
            target=await _target_for(ctx, action),
            draft={},
            context={
                "mission_id": str(mission["id"]),
                "objective": mission.get("objective_text"),
                # Carried so the worker delivers them: guard_outreach returns
                # obligations, and a disclosure computed at planning time and
                # dropped before sending is a compliance failure with a paper
                # trail saying it was handled.
                "required_disclosures": list(verdict.disclosures),
            },
            idempotency_key=f"mission:{mission['id']}:action:{action['id']}",
            created_by=f"mission:{mission['id']}",
        )
        return command, ""
    except ValueError as exc:
        # The command layer refusing an incomplete command. Expected today.
        logger.info("mission %s: action %s cannot be staged: %s",
                    mission["id"], action["id"], exc)
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — one bad action must not stop the tick
        logger.exception("mission %s: staging action %s failed",
                         mission["id"], action["id"])
        return None, str(exc)[:200]


async def _release(ctx: TenantContext, staged: dict[str, Any]) -> bool:
    """Release through the same function a person's Approve click calls."""
    from commands_api import _get_command, release_command

    try:
        row = await _get_command(ctx, str(staged["id"]))
        if row is None or row["state"] != "awaiting_approval":
            return False
        await release_command(ctx, row, reason="released by mission grant")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("mission: releasing command %s failed", staged.get("id"))
        return False


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

async def _count_planned(ctx: TenantContext, mission_id: str) -> int:
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        # Literal predicate: matches idx_mission_actions_planned.
        return int(await conn.fetchval(
            """SELECT count(*)::int FROM mission_actions
                WHERE mission_id = $1::uuid AND state = 'planned'""",
            mission_id,
        ) or 0)


async def _due_actions(ctx: TenantContext, mission_id: str) -> list[dict[str, Any]]:
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """SELECT * FROM mission_actions
                WHERE mission_id = $1::uuid AND state = 'planned'
                  AND (due_at IS NULL OR due_at <= now())
                ORDER BY due_at NULLS FIRST, step_index
                LIMIT $2""",
            mission_id, MAX_ACTIONS_PER_TICK,
        )
    return [dict(r) for r in rows]


async def _set_state(
    ctx: TenantContext, action_id: Any, state: str, *,
    blocked_reason: Optional[str] = None, command_id: Any = None,
) -> None:
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """UPDATE mission_actions
                  SET state = $2, blocked_reason = coalesce($3, blocked_reason),
                      command_id = coalesce($4::uuid, command_id), updated_at = now()
                WHERE id = $1::uuid""",
            action_id, state, blocked_reason, command_id,
        )


async def _journal(ctx: TenantContext, mission_id: Any, kind: str, detail: dict) -> None:
    import json

    from db.connection import tenant_tx

    try:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """INSERT INTO mission_events (tenant_id, mission_id, kind, detail)
                   VALUES ($1::uuid, $2::uuid, $3, $4::jsonb)""",
                ctx.tenant_id, mission_id, kind, json.dumps(detail, default=str),
            )
    except Exception:  # noqa: BLE001 — the journal must never break the run
        logger.exception("mission %s: journalling %s failed", mission_id, kind)


async def _refresh_candidates(
    ctx: TenantContext, mission: dict[str, Any],
) -> list[dict[str, Any]]:
    """Candidates come from the deterministic side — never from the model."""
    import opportunity_engine
    from db.connection import tenant_tx

    scan = await opportunity_engine.scan(ctx)
    found = scan.get("opportunities", [])[:planner.MAX_CANDIDATES]

    rows: list[dict[str, Any]] = []
    async with tenant_tx(ctx) as conn:
        for opportunity in found:
            subject_id = opportunity.get("subject_id")
            if not subject_id:
                continue
            row = await conn.fetchrow(
                """INSERT INTO mission_candidates
                       (tenant_id, mission_id, subject_type, subject_id, score, evidence, state)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, 'selected')
                   ON CONFLICT (mission_id, subject_type, subject_id) DO UPDATE
                       SET score = EXCLUDED.score, updated_at = now()
                   RETURNING *""",
                ctx.tenant_id, mission["id"],
                opportunity.get("subject_type") or "client", str(subject_id),
                opportunity.get("confidence"),
                _json(opportunity),
            )
            if row:
                rows.append({
                    **dict(row),
                    "label": opportunity.get("subject"),
                    "why": opportunity.get("why"),
                })
    return rows


def _json(value: Any) -> str:
    import json
    return json.dumps(value, default=str)


async def _contact_for(
    ctx: TenantContext, action: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """The address this action would reach, and the state whose rules apply."""
    from db.connection import tenant_tx

    if not action.get("candidate_id"):
        return None, None
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """SELECT c.subject_type, c.subject_id, cl.email, cl.phone
                 FROM mission_candidates c
                 LEFT JOIN clients cl ON cl.id::text = c.subject_id
                WHERE c.id = $1::uuid""",
            action["candidate_id"],
        )
    if row is None:
        return None, None
    contact = row["email"] if action["channel"] == "email" else row["phone"]
    # state_code is None, and that is a real limitation rather than an
    # oversight: `clients` carries no state column at all, so the
    # state-specific half of the outreach rules (call windows, per-state
    # disclosure) cannot be applied to a client. crm.py's email path already
    # passes None for the same reason. Everything that does not depend on the
    # state still applies — consent, suppression, frequency, and the federal
    # rules that make AI voice the strictest channel.
    return contact, None


async def _target_for(ctx: TenantContext, action: dict[str, Any]) -> dict[str, Any]:
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT subject_type, subject_id FROM mission_candidates WHERE id = $1::uuid",
            action.get("candidate_id"),
        )
    if row is None:
        return {}
    key = {"client": "client_id", "lead": "lead_id", "contact": "contact_id"}.get(
        row["subject_type"], "client_id")
    return {key: row["subject_id"]}


def _command_type(channel: str):
    """The command this channel becomes.

    There is no TASK command type — the command surface is EMAIL, SMS, CALL and
    CALENDAR, all of which reach a person. A mission's 'task' channel is
    internal work with no send behind it, so it is refused here rather than
    quietly promoted into an outbound command.
    """
    from commands_api import CommandType

    mapping = {
        "email": CommandType.EMAIL,
        "sms": CommandType.SMS,
        "voice": CommandType.CALL,
    }
    if channel not in mapping:
        raise ValueError(f"{channel!r} is not an outbound command channel")
    return mapping[channel]


async def handle_tick(ctx: TenantContext, payload: dict[str, Any]) -> dict[str, Any]:
    """`mission:tick` job handler."""
    return await tick(ctx, str(payload.get("mission_id")))


async def sweep_all_tenants(*, tenant_limit: int = 200) -> dict[str, Any]:
    """Tick every running mission across every tenant. Scheduler entry point.

    Same posture as outcome_memory.sweep_all_tenants: one cross-tenant read to
    find who has work, then a FRESH single-tenant context for each, so nothing
    in `tick` ever runs as an admin or needs a tenant predicate of its own.
    """
    import os

    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    if not enabled():
        return {"skipped": "missions are not enabled on this deployment"}

    platform_ctx = TenantContext(
        agent_id="mission-tick",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID",
                            "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        # Business scope, deliberately cross-tenant: this is the scheduler
        # asking which tenants have work, exactly as the attribution sweep
        # does. Every tick below runs in that tenant's own context.
        rows = await conn.fetch(
            """SELECT id, tenant_id FROM missions
                WHERE status IN ('shadow', 'active')
                ORDER BY updated_at
                LIMIT $1""",
            tenant_limit,
        )

    results: dict[str, Any] = {"missions": 0, "errors": 0}
    for row in rows:
        ctx = TenantContext(
            agent_id="mission-tick", tenant_id=str(row["tenant_id"]), role=Role.AGENT,
        )
        try:
            await tick(ctx, str(row["id"]))
            results["missions"] += 1
        except Exception:  # noqa: BLE001 — one tenant must not stop the sweep
            logger.exception("mission tick failed for %s", row["id"])
            results["errors"] += 1
    return results


# Registered on import, the same way speed_to_lead does it, so a queued
# `mission:tick` job is never an unknown type. Registration is not execution:
# `tick` returns immediately unless Feature.MISSIONS is on.
def _register() -> None:
    from automation_jobs import register_handler

    async def _handler(ctx, payload):
        return await handle_tick(ctx, payload)

    register_handler("mission:tick", _handler)


_register()
