"""/api/missions — create, simulate, launch, watch.

Two endpoints carry the weight and both refuse rather than assume.

`launch` will not put a mission live without a simulation, and will not put it
live without naming every credential it lacks. The database enforces the first
(`missions_live_requires_simulation`); this refuses earlier and with a sentence
a person can act on, because a CHECK violation surfaced to a UI is a 500 with
no advice in it.

`create` takes the consent sentence VERBATIM. It is not generated here from a
template and it is not a boolean: the row records the words the agent actually
agreed to, and an audit a year from now reads those words rather than a
reconstruction of them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.connection import tenant_tx
from platform_policy import Feature, require_feature
from tenancy import TenantContext, require_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/missions", tags=["missions"])

CHANNELS = ("email", "sms", "voice", "task")
OBJECTIVES = ("listings_won", "buyers_converted", "appointments_set",
              "database_reactivated", "sphere_touched", "deals_saved")


class MissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective_kind: Literal[OBJECTIVES]  # type: ignore[valid-type]
    objective_text: str = Field(min_length=1, max_length=1000)
    target_count: Optional[int] = Field(default=None, gt=0, le=10_000)
    deadline: Optional[str] = None
    budget_cents: int = Field(default=0, ge=0, le=1_000_000)
    allowed_channels: list[str] = Field(default_factory=list, max_length=4)
    auto_channels: list[str] = Field(default_factory=list, max_length=4)
    #: The sentence the agent agreed to, word for word. Never templated here.
    consent_text: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("allowed_channels", "auto_channels")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        bad = [c for c in value if c not in CHANNELS]
        if bad:
            raise ValueError(f"unknown channel(s): {', '.join(bad)}")
        return sorted(set(value))


class LaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["shadow", "live"] = "shadow"


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_mission(
    body: MissionCreate, ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MISSIONS)

    if not set(body.auto_channels) <= set(body.allowed_channels):
        raise HTTPException(
            status_code=400,
            detail="A channel cannot be on autopilot unless the mission is "
                   "allowed to use it at all.",
        )
    if body.auto_channels and not (body.consent_text or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Autopilot needs the sentence you agreed to, recorded "
                   "verbatim. A mission that can send on your behalf without "
                   "one has no record of what you authorised.",
        )

    snapshot = await _autonomy_snapshot(ctx)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """INSERT INTO missions
                   (tenant_id, objective_kind, objective_text, target_count,
                    deadline, budget_cents, allowed_channels, auto_channels,
                    consent_at, consent_by, consent_text, autonomy_snapshot,
                    created_by)
               VALUES ($1::uuid, $2, $3, $4, $5::timestamptz, $6, $7::text[],
                       $8::text[],
                       CASE WHEN $8::text[] = '{}'::text[] THEN NULL ELSE now() END,
                       CASE WHEN $8::text[] = '{}'::text[] THEN NULL ELSE $9 END,
                       CASE WHEN $8::text[] = '{}'::text[] THEN NULL ELSE $10 END,
                       $11::jsonb, $9)
               RETURNING *""",
            ctx.tenant_id, body.objective_kind, body.objective_text.strip(),
            body.target_count, body.deadline, body.budget_cents,
            body.allowed_channels, body.auto_channels, ctx.agent_id,
            (body.consent_text or "").strip() or None, json.dumps(snapshot),
        )
    await _journal(ctx, row["id"], "created", {"objective": body.objective_text})
    return {"mission": _mission_json(row)}


@router.get("")
async def list_missions(ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.MISSIONS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT * FROM missions ORDER BY updated_at DESC LIMIT 100")
    return {"missions": [_mission_json(r) for r in rows]}


@router.get("/{mission_id}")
async def get_mission(mission_id: str, ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.MISSIONS)
    row = await _load(ctx, mission_id)
    async with tenant_tx(ctx) as conn:
        events = await conn.fetch(
            """SELECT kind, detail, occurred_at FROM mission_events
                WHERE mission_id = $1::uuid ORDER BY occurred_at DESC LIMIT 50""",
            mission_id,
        )
    return {
        "mission": _mission_json(row),
        "events": [
            {"kind": e["kind"], "detail": _decode(e["detail"]),
             "occurred_at": e["occurred_at"].isoformat()}
            for e in events
        ],
    }


@router.post("/{mission_id}/simulate")
async def simulate_mission(
    mission_id: str, ctx: TenantContext = Depends(require_context),
):
    """What this mission would do, before it does anything."""
    require_feature(Feature.MISSIONS)
    from missions import simulator

    row = await _load(ctx, mission_id)
    mission = dict(row)

    async with tenant_tx(ctx) as conn:
        candidates = await conn.fetch(
            "SELECT * FROM mission_candidates WHERE mission_id = $1::uuid", mission_id)
        actions = await conn.fetch(
            "SELECT * FROM mission_actions WHERE mission_id = $1::uuid", mission_id)
        outcomes = await conn.fetchval(
            "SELECT count(*)::int FROM outcome_events WHERE attributed_at IS NOT NULL")

    result = simulator.simulate(
        mission, [dict(c) for c in candidates], [dict(a) for a in actions],
        outcomes_observed=int(outcomes or 0),
    )
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """UPDATE missions
                  SET simulated_at = now(),
                      status = CASE WHEN status = 'draft' THEN 'simulated' ELSE status END,
                      updated_at = now()
                WHERE id = $1::uuid""",
            mission_id,
        )
    await _journal(ctx, mission_id, "simulated", {"actions": result["actions"]["planned"]})
    return {"simulation": result}


@router.post("/{mission_id}/launch")
async def launch_mission(
    mission_id: str, body: LaunchRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Refuses live without a simulation, and names what is missing."""
    require_feature(Feature.MISSIONS)
    from missions import policy

    row = await _load(ctx, mission_id)
    if row["status"] in ("completed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"This mission is {row['status']}.")

    if body.mode == "live":
        if row["simulated_at"] is None:
            raise HTTPException(
                status_code=409,
                detail="Simulate this mission before putting it live. Nobody "
                       "should point it at their database without first seeing "
                       "what it would do.",
            )
        ready, missing = await policy.outbound_ready(
            ctx, list(row["allowed_channels"] or []))
        if not ready:
            # Named, not "not configured" — the agent has to know WHICH.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This mission cannot send on every channel it is allowed "
                    "to use: " + "; ".join(missing)
                ),
            )

    status_value = "active" if body.mode == "live" else "shadow"
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchrow(
            """UPDATE missions
                  SET mode = $2, status = $3, launched_at = coalesce(launched_at, now()),
                      updated_at = now()
                WHERE id = $1::uuid
               RETURNING *""",
            mission_id, body.mode, status_value,
        )
    await _journal(ctx, mission_id, "launched", {"mode": body.mode})
    return {"mission": _mission_json(updated)}


@router.post("/{mission_id}/pause")
async def pause_mission(
    mission_id: str, ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MISSIONS)
    await _load(ctx, mission_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """UPDATE missions SET status = 'paused', updated_at = now()
                WHERE id = $1::uuid RETURNING *""",
            mission_id,
        )
    await _journal(ctx, mission_id, "paused", {"by": ctx.agent_id})
    return {"mission": _mission_json(row)}


@router.get("/{mission_id}/progress")
async def mission_progress(
    mission_id: str, ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MISSIONS)
    from missions import evaluator, learning

    await _load(ctx, mission_id)
    await evaluator.attach_outcomes(ctx, mission_id)
    report = await evaluator.progress(ctx, mission_id)
    return {
        "progress": report,
        "recommendations": learning.recommendations(report),
        # Shown, not buried: the UI states which models are not fitted and
        # what volume would justify them.
        "learning": learning.what_is_not_fitted(),
    }


# ---------------------------------------------------------------------------

async def _load(ctx: TenantContext, mission_id: str) -> Any:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM missions WHERE id = $1::uuid", mission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Mission not found.")
    return row


async def _autonomy_snapshot(ctx: TenantContext) -> dict[str, Any]:
    """What the dial said when this mission was created.

    Recorded so a later audit can tell a mission that was granted autopilot
    from one that merely ran while the dial happened to be permissive.
    """
    import autonomy

    try:
        return await autonomy.get_settings(ctx)
    except Exception:  # noqa: BLE001 — a missing snapshot must not block creation
        logger.exception("mission: autonomy snapshot failed")
        return {}


async def _journal(ctx: TenantContext, mission_id: Any, kind: str, detail: dict) -> None:
    try:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """INSERT INTO mission_events (tenant_id, mission_id, kind, detail)
                   VALUES ($1::uuid, $2::uuid, $3, $4::jsonb)""",
                ctx.tenant_id, mission_id, kind, json.dumps(detail, default=str),
            )
    except Exception:  # noqa: BLE001
        logger.exception("mission %s: journalling %s failed", mission_id, kind)


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _mission_json(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    data = dict(row)
    return {
        "id": str(data["id"]),
        "objective_kind": data["objective_kind"],
        "objective_text": data["objective_text"],
        "target_count": data.get("target_count"),
        "deadline": _iso(data.get("deadline")),
        "budget_cents": data.get("budget_cents"),
        "allowed_channels": list(data.get("allowed_channels") or []),
        "auto_channels": list(data.get("auto_channels") or []),
        # The sentence travels with the mission. A UI that shows "autopilot: on"
        # without it is showing a switch and hiding the agreement.
        "consent_text": data.get("consent_text"),
        "consent_at": _iso(data.get("consent_at")),
        "consent_by": data.get("consent_by"),
        "status": data["status"],
        "mode": data["mode"],
        "simulated_at": _iso(data.get("simulated_at")),
        "launched_at": _iso(data.get("launched_at")),
        "created_at": _iso(data.get("created_at")),
    }


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value
