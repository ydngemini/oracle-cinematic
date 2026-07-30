"""
Agent Profile — the 1-minute mobile onboarding write path.

POST /api/agents/profile captures the three calibration data points the
onboarding gate collects (experience tier, target ZIP strike zones, monthly
volume target) and upserts them into the Memory Core's user_profiles row for
the authenticated agent. Target ZIPs land in the existing target_markets jsonb,
so the very next SESSION_RESTORED frame hydrates them into the dashboard with
zero extra plumbing.
"""

import json
import logging
import re
import uuid
from datetime import date
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from approval_service import create_approval, decide_approval
from db.connection import tenant_tx
from platform_policy import ActionRisk, communication_preferences
from tenancy import Role, TenantContext, require_context, require_role

logger = logging.getLogger("oracle.agent_profile")

router = APIRouter(prefix="/api/agents", tags=["Agent Profile"])

_ZIP_RE = re.compile(r"^\d{5}$")

EXPERIENCE_LEVELS = {"scout", "closer", "fund"}
VOLUME_TARGETS = {"1-2", "3-5", "6+"}


async def load_agent_identity(ctx: TenantContext) -> dict[str, Any]:
    """Load the authenticated agent's approved drafting identity under RLS."""
    async with tenant_tx(ctx) as conn:
        profile = await conn.fetchrow(
            """
            SELECT display_name,public_email,phone,brokerage,email_signature
              FROM user_profiles
             WHERE user_id=$1
            """,
            ctx.agent_id,
        )
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE lower(agent_id)=lower($1)",
            ctx.agent_id,
        )
        membership = None
        settings = None
        if user is not None:
            membership = await conn.fetchrow(
                "SELECT team_name,title,status FROM team_memberships WHERE user_id=$1",
                user["id"],
            )
            settings = await conn.fetchrow(
                "SELECT approved_tone,preferences FROM agent_ai_settings WHERE user_id=$1",
                user["id"],
            )

    profile_data = dict(profile) if profile else {}
    membership_data = dict(membership) if membership else {}
    settings_data = dict(settings) if settings else {}
    preferences = settings_data.get("preferences") or {}
    if isinstance(preferences, str):
        try:
            preferences = json.loads(preferences)
        except ValueError:
            preferences = {}
    return {
        "name": profile_data.get("display_name") or ctx.agent_id,
        "brokerage": profile_data.get("brokerage") or membership_data.get("team_name") or "",
        "communication_tone": settings_data.get("approved_tone") or preferences.get("tone") or "neutral",
        "signature": profile_data.get("email_signature") or "",
        "phone_number": profile_data.get("phone") or "",
        "public_email": profile_data.get("public_email") or "",
        "membership_status": membership_data.get("status") or "",
    }


class OnboardingProfile(BaseModel):
    experience_level: str = Field(..., description="scout | closer | fund")
    target_zips: list[str] = Field(..., min_length=1, max_length=20)
    monthly_deal_target: str = Field(..., description="1-2 | 3-5 | 6+")

    @field_validator("experience_level")
    @classmethod
    def _check_experience(cls, v: str) -> str:
        if v not in EXPERIENCE_LEVELS:
            raise ValueError(f"must be one of {sorted(EXPERIENCE_LEVELS)}")
        return v

    @field_validator("monthly_deal_target")
    @classmethod
    def _check_volume(cls, v: str) -> str:
        if v not in VOLUME_TARGETS:
            raise ValueError(f"must be one of {sorted(VOLUME_TARGETS)}")
        return v

    @field_validator("target_zips")
    @classmethod
    def _check_zips(cls, v: list[str]) -> list[str]:
        cleaned = [z.strip() for z in v]
        bad = [z for z in cleaned if not _ZIP_RE.match(z)]
        if bad:
            raise ValueError(f"not 5-digit ZIP codes: {bad}")
        # Dedupe, preserve order
        return list(dict.fromkeys(cleaned))


class LicenseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    state_code: str = Field(pattern=r"^[A-Za-z]{2}$")
    license_number: str = Field(min_length=2, max_length=120)
    license_type: str = Field(default="salesperson", min_length=2, max_length=80)
    expires_on: Optional[date] = None

    @field_validator("state_code")
    @classmethod
    def normalize_state(cls, value: str) -> str:
        return value.upper()


class AISettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_tone: Literal["concise", "warm", "formal", "neutral", "direct"] = "neutral"
    autonomous_research: bool = True
    autonomous_drafting: bool = True
    style_training_opt_in: bool = False
    preferences: dict[str, Any] = Field(default_factory=dict)


class BrokerageOnboarding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    team_name: str = Field(min_length=2, max_length=160)
    title: Optional[str] = Field(default=None, max_length=120)
    licenses: list[LicenseInput] = Field(min_length=1, max_length=20)
    ai_settings: AISettingsInput = Field(default_factory=AISettingsInput)


class BrokerOnboardingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=8, max_length=500)


@router.post("/profile", status_code=status.HTTP_200_OK)
async def save_onboarding_profile(
    body: OnboardingProfile,
    ctx: TenantContext = Depends(require_context),
):
    """Upsert the authenticated agent's onboarding profile. Keyed on the JWT's
    agent_id — a client can never write another agent's row, and the ON
    CONFLICT tenant guard means a recycled user_id in another tenant is a
    no-op rather than a cross-tenant overwrite (RLS would also refuse it)."""
    try:
        async with tenant_tx(ctx) as conn:
            result = await conn.execute(
                """
                INSERT INTO user_profiles
                    (user_id, tenant_id, target_markets, experience_level, monthly_deal_target)
                VALUES ($1, $2, $3::jsonb, $4, $5)
                ON CONFLICT (user_id) DO UPDATE
                   SET target_markets      = EXCLUDED.target_markets,
                       experience_level    = EXCLUDED.experience_level,
                       monthly_deal_target = EXCLUDED.monthly_deal_target
                 WHERE user_profiles.tenant_id = EXCLUDED.tenant_id
                """,
                ctx.agent_id,
                ctx.tenant_id,
                json.dumps(body.target_zips),
                body.experience_level,
                body.monthly_deal_target,
            )
    except RuntimeError as exc:
        # tenant_tx raises when the pool never initialized (DB-less dev run).
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — profile not persisted ({exc})",
        )

    if result == "INSERT 0 0":
        # ON CONFLICT fired but the WHERE tenant guard rejected the update.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "profile id exists under a different tenant",
        )

    logger.info(
        "Onboarding profile saved: agent=%s tenant=%s zips=%d level=%s volume=%s",
        ctx.agent_id, ctx.tenant_id,
        len(body.target_zips), body.experience_level, body.monthly_deal_target,
    )
    return {
        "status": "saved",
        "experience_level": body.experience_level,
        "target_markets": body.target_zips,
        "monthly_deal_target": body.monthly_deal_target,
    }


@router.put("/brokerage-onboarding", status_code=status.HTTP_202_ACCEPTED)
async def submit_brokerage_onboarding(
    body: BrokerageOnboarding,
    ctx: TenantContext = Depends(require_context),
):
    """Submit team, license, and explicit AI settings for broker approval."""
    preferences = communication_preferences(
        body.ai_settings.preferences,
        approved_tone=body.ai_settings.approved_tone,
    )
    async with tenant_tx(ctx) as conn:
        user = await conn.fetchrow(
            "SELECT id,role FROM users WHERE lower(agent_id)=lower($1)", ctx.agent_id
        )
        if user is None:
            raise HTTPException(status_code=404, detail="Authenticated user record not found.")
        existing = await conn.fetchrow(
            "SELECT * FROM team_memberships WHERE user_id=$1 FOR UPDATE", user["id"]
        )
        if existing and existing["approval_id"]:
            await conn.execute(
                """
                UPDATE action_approvals
                   SET status='revoked',decided_by=$2,decided_at=now(),
                       reason='Brokerage onboarding resubmitted.'
                 WHERE id=$1 AND status='pending'
                """,
                existing["approval_id"],
                ctx.agent_id,
            )
        membership = await conn.fetchrow(
            """
            INSERT INTO team_memberships (
                tenant_id,user_id,team_name,title,member_role,status,training_status
            ) VALUES ($1::uuid,$2,$3,$4,$5,'pending_broker_approval',$6)
            ON CONFLICT (tenant_id,user_id) DO UPDATE SET
                team_name=EXCLUDED.team_name,title=EXCLUDED.title,
                member_role=EXCLUDED.member_role,status='pending_broker_approval',
                training_status=EXCLUDED.training_status,approval_id=NULL,
                approved_by=NULL,approved_at=NULL,updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            user["id"],
            body.team_name,
            body.title,
            "broker" if user["role"] == "broker_owner" else "agent",
            "examples_ready" if body.ai_settings.style_training_opt_in else "not_started",
        )
        await conn.execute("DELETE FROM agent_licenses WHERE user_id=$1", user["id"])
        license_ids: list[str] = []
        for license_input in body.licenses:
            license_row = await conn.fetchrow(
                """
                INSERT INTO agent_licenses (
                    tenant_id,user_id,agent_id,state_code,license_number,license_type,
                    expires_on,expiry_date
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$7) RETURNING id
                """,
                ctx.tenant_id,
                user["id"],
                ctx.agent_id,
                license_input.state_code,
                license_input.license_number,
                license_input.license_type,
                license_input.expires_on,
            )
            license_ids.append(str(license_row["id"]))
        await conn.execute(
            """
            INSERT INTO agent_ai_settings (
                tenant_id,user_id,approved_tone,autonomous_research,
                autonomous_drafting,style_training_opt_in,preferences
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (tenant_id,user_id) DO UPDATE SET
                approved_tone=EXCLUDED.approved_tone,
                autonomous_research=EXCLUDED.autonomous_research,
                autonomous_drafting=EXCLUDED.autonomous_drafting,
                style_training_opt_in=EXCLUDED.style_training_opt_in,
                preferences=EXCLUDED.preferences,updated_at=now()
            """,
            ctx.tenant_id,
            user["id"],
            body.ai_settings.approved_tone,
            body.ai_settings.autonomous_research,
            body.ai_settings.autonomous_drafting,
            body.ai_settings.style_training_opt_in,
            json.dumps(preferences, separators=(",", ":")),
        )

    approval = await create_approval(
        ctx,
        action_type="brokerage.onboarding",
        risk=ActionRisk.ROLE_OVERRIDE,
        target_type="team_membership",
        target_id=str(membership["id"]),
        draft_payload={
            "membership_id": str(membership["id"]),
            "user_id": str(user["id"]),
            "team_name": body.team_name,
            "license_ids": license_ids,
            "ai_settings_reviewed": True,
        },
        expires_in_minutes=7 * 24 * 60,
    )
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE team_memberships SET approval_id=$2::uuid WHERE id=$1",
            membership["id"],
            approval["id"],
        )
    return {
        "status": "pending_broker_approval",
        "membership_id": str(membership["id"]),
        "approval": approval,
    }


@router.post("/brokerage-onboarding/{approval_id}/decision")
async def decide_brokerage_onboarding(
    approval_id: uuid.UUID,
    body: BrokerOnboardingDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM action_approvals WHERE id=$1::uuid", str(approval_id)
        )
    if row is None or row["action_type"] != "brokerage.onboarding":
        raise HTTPException(status_code=404, detail="Onboarding approval not found.")
    try:
        approval = await decide_approval(
            ctx,
            str(approval_id),
            decision=body.decision,
            reason=body.reason,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    draft = approval["draft_payload"]
    accepted = approval["status"] == "approved" and body.decision == "approved"
    async with tenant_tx(ctx) as conn:
        membership = await conn.fetchrow(
            """
            UPDATE team_memberships
               SET status=$2,approved_by=$3,approved_at=CASE WHEN $2='active' THEN now() ELSE NULL END
             WHERE id=$1::uuid AND approval_id=$4::uuid
            RETURNING *
            """,
            draft["membership_id"],
            "active" if accepted else "rejected",
            ctx.agent_id,
            str(approval_id),
        )
        await conn.execute(
            """
            UPDATE agent_licenses
               SET verification_status=$2,verified_by=$3,
                   verified_at=CASE WHEN $2='verified' THEN now() ELSE NULL END
             WHERE user_id=$1::uuid
            """,
            draft["user_id"],
            "verified" if accepted else "rejected",
            ctx.agent_id,
        )
    if membership is None:
        raise HTTPException(status_code=409, detail="Onboarding request changed before decision.")
    return {
        "status": membership["status"],
        "membership_id": str(membership["id"]),
        "approval": approval,
    }


@router.get("/me/onboarding")
async def my_brokerage_onboarding(ctx: TenantContext = Depends(require_context)):
    async with tenant_tx(ctx) as conn:
        user = await conn.fetchrow(
            "SELECT id,role FROM users WHERE lower(agent_id)=lower($1)", ctx.agent_id
        )
        if user is None:
            # Environment-injected operator accounts are valid authenticated
            # principals but intentionally do not require a mutable users row.
            # Return an empty onboarding profile so Personal AI remains usable
            # instead of surfacing a noisy 404 for those accounts.
            return {
                "user_role": ctx.role.value,
                "membership": None,
                "licenses": [],
                "ai_settings": None,
                "google_connected": False,
                "style_training_examples": 0,
            }
        membership = await conn.fetchrow(
            "SELECT * FROM team_memberships WHERE user_id=$1", user["id"]
        )
        licenses = await conn.fetch(
            "SELECT * FROM agent_licenses WHERE user_id=$1 ORDER BY state_code", user["id"]
        )
        settings = await conn.fetchrow(
            "SELECT * FROM agent_ai_settings WHERE user_id=$1", user["id"]
        )
        google_connected = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM provider_credentials
                 WHERE provider='google' AND disabled_at IS NULL
                   AND account_label IN ($1,'default')
            )
            """,
            ctx.agent_id,
        )
        examples = await conn.fetchval(
            """
            SELECT count(*)::int FROM style_training_examples
             WHERE agent_id=$1 AND revoked_at IS NULL
            """,
            ctx.agent_id,
        )

    def serialize(row):
        if row is None:
            return None
        result = dict(row)
        for key, value in list(result.items()):
            if isinstance(value, uuid.UUID):
                result[key] = str(value)
            elif hasattr(value, "isoformat"):
                result[key] = value.isoformat()
            elif isinstance(value, str) and key == "preferences":
                try:
                    result[key] = json.loads(value)
                except ValueError:
                    pass
        return result

    return {
        "user_role": user["role"],
        "membership": serialize(membership),
        "licenses": [serialize(row) for row in licenses],
        "ai_settings": serialize(settings),
        "google_connected": bool(google_connected),
        "style_training_examples": examples,
    }


@router.get("/team")
async def brokerage_team(ctx: TenantContext = Depends(require_context)):
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT m.id,m.team_name,m.title,m.member_role,m.status,
                   m.training_status,m.approved_by,m.approved_at,
                   u.id AS user_id,u.agent_id,u.role,u.is_active,
                   count(l.id)::int AS license_count,
                   count(l.id) FILTER (WHERE l.verification_status='verified')::int AS verified_licenses
              FROM team_memberships m
              JOIN users u ON u.id=m.user_id
              LEFT JOIN agent_licenses l ON l.user_id=u.id
             GROUP BY m.id,u.id ORDER BY m.team_name,u.agent_id
            """
        )
    return {
        "members": [
            {
                **dict(row),
                "id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
            }
            for row in rows
        ]
    }
