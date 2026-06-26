"""Per-agent license status and continuing-education credit logging."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, Role, require_context, require_role

# Authoritative attorney-at-closing list — single source of truth shared with
# the compliance engine so the public state-profile API and ComplianceEngine
# never disagree about whether a state requires an attorney at closing.
from compliance_engine.closing import ATTORNEY_CLOSE_STATES

from ._common import (
    router, logger,
    _STATE_RE, _FIPS_RE, _UUID_RE,
    ALL_STATE_CODES, _ATTORNEY_REVIEW_STATES, _MANDATORY_DISCLOSURE_STATES,
    _TDS_STATES, _FEDERAL_LEAD_PAINT_THRESHOLD_YEAR,
    _CE_HOURS_BY_STATE,
    _iso, _num, _require_state, _require_uuid, _require_agent_id, _fetch, _fetchrow,
)
from .models import (  # noqa: F401  (re-exported for route handlers)
    StateSummary,
    DisclosureForm,
    ContractTemplate,
    AdvertisingRule,
    StateProfile,
    LicenseRequirements,
    ReciprocityInfo,
    AgentLicense,
    AgentLicenseStatus,
    CECreditBody,
    CECreditResponse,
    MLSRegion,
    MLSSyncStatus,
    MLSSearchBody,
    NormalizedListing,
    MLSSearchResponse,
    StateMarketOverview,
    CountyMarketData,
    FloodZoneResult,
    SchoolDistrict,
    SchoolsResponse,
    ZoningResult,
    TransactionContext,
    RequiredDisclosure,
    ComplianceCheckResponse,
    DisclosureChecklistItem,
    ComplianceChecklist,
    FormValidationBody,
    ValidationError,
    FormValidationResponse,
)
from .engine import _engine  # noqa: F401

@router.get(
    "/api/licensing/agent/{agent_id}/status",
    response_model=AgentLicenseStatus,
    summary="All licenses for an agent with expiry warnings",
)
async def get_agent_license_status(
    agent_id: str,
    ctx: TenantContext = Depends(require_context),
) -> AgentLicenseStatus:
    """Return all active and historical licenses for the specified agent.

    Licenses expiring within 90 days are flagged.  CE deficit is computed as
    ``hours_required − hours_completed`` for the current renewal cycle.
    """
    aid = _require_agent_id(agent_id)

    # Agents may only read their own licenses unless broker_owner / platform_admin.
    if (
        ctx.agent_id != aid
        and not ctx.is_platform_admin
        and not ctx.is_broker_owner
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    rows = await _fetch(
        ctx,
        """
        SELECT l.*, u.tenant_id
        FROM agent_licenses l
        JOIN users u ON u.id::text = l.agent_id::text
        WHERE l.agent_id = $1
        ORDER BY l.state_code, l.expiry_date
        """,
        aid,
    )

    today = date.today()
    licenses: list[AgentLicense] = []
    for r in rows:
        expiry = r.get("expiry_date")
        days = (expiry - today).days if isinstance(expiry, date) else None
        ce_req = r.get("ce_hours_required", _CE_HOURS_BY_STATE.get(r["state_code"], 0))
        ce_done = r.get("ce_hours_completed", 0)
        licenses.append(
            AgentLicense(
                license_id=str(r.get("id", uuid.uuid4())),
                agent_id=aid,
                state_code=r["state_code"],
                license_type=r.get("license_type", "salesperson"),
                license_number=r.get("license_number", ""),
                status=r.get("status", "active"),
                issued_date=r.get("issued_date"),
                expiry_date=expiry,
                days_until_expiry=days,
                expiry_warning=days is not None and 0 <= days <= 90,
                ce_hours_completed=ce_done,
                ce_hours_required=ce_req,
                ce_deficit=max(0, ce_req - ce_done),
            )
        )

    active = [l for l in licenses if l.status == "active"]
    expiring = [l for l in active if l.expiry_warning]
    expired = [l for l in licenses if l.status == "expired"]

    return AgentLicenseStatus(
        agent_id=aid,
        licenses=licenses,
        total_active=len(active),
        expiring_soon=len(expiring),
        expired=len(expired),
    )


@router.post(
    "/api/licensing/agent/{agent_id}/ce",
    response_model=CECreditResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log continuing education credits for an agent",
)
async def log_ce_credits(
    agent_id: str,
    body: CECreditBody,
    ctx: TenantContext = Depends(require_context),
) -> CECreditResponse:
    """Record a CE course completion for the specified agent.

    Agents may only log CE for themselves; broker owners may log for any agent
    in their tenant; platform admins may log for anyone.
    """
    aid = _require_agent_id(agent_id)
    code = _require_state(body.state_code)

    if (
        ctx.agent_id != aid
        and not ctx.is_platform_admin
        and not ctx.is_broker_owner
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied.",
        )

    log_id = str(uuid.uuid4())
    try:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                INSERT INTO agent_ce_log
                    (id, tenant_id, agent_id, state_code, provider, course_name,
                     hours, completion_date, certificate_number, created_at)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                """,
                log_id,
                ctx.tenant_id,
                aid,
                code,
                body.provider,
                body.course_name,
                body.hours,
                body.completion_date,
                body.certificate_number,
            )

            # Aggregate hours completed this cycle.
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(hours), 0) AS total
                FROM agent_ce_log
                WHERE agent_id = $1 AND state_code = $2
                  AND completion_date >= now() - interval '2 years'
                """,
                aid,
                code,
            )
            total_hours = float(row["total"]) if row else body.hours
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("CE log insert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Core offline.",
        )

    required = _CE_HOURS_BY_STATE.get(code, 0)
    return CECreditResponse(
        ce_log_id=log_id,
        agent_id=aid,
        state_code=code,
        hours_logged=body.hours,
        total_hours_this_cycle=total_hours,
        hours_required=required,
        deficit=max(0.0, required - total_hours),
    )


# ===========================================================================
# 3. MLS Integration API
# ===========================================================================

