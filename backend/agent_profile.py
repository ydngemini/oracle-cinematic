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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.agent_profile")

router = APIRouter(prefix="/api/agents", tags=["Agent Profile"])

_ZIP_RE = re.compile(r"^\d{5}$")

EXPERIENCE_LEVELS = {"scout", "closer", "fund"}
VOLUME_TARGETS = {"1-2", "3-5", "6+"}


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
