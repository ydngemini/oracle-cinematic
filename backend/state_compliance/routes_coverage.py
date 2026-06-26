"""National data-coverage reporting and harvest task control."""
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
    _iso, _num, _require_state, _require_uuid, _fetch, _fetchrow,
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
    "/api/market/coverage",
    summary="National data-coverage map (property + compliance per jurisdiction)",
)
async def get_data_coverage(
    summary_only: bool = Query(
        False, description="Return just the rollup totals, not the per-state matrix."
    ),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Report what data the platform holds for every US jurisdiction.

    Three planes: property/parcel (per-state scrapers), compliance (per-state
    statutory rules), and market/demographic (national APIs — no per-state gap).
    Property and compliance status are tracked per jurisdiction so the coverage
    gap stays visible; this is the source of truth behind the ops-console
    coverage view and the next harvest-batch planning.
    """
    from data_coverage import report_json, summary as coverage_summary

    return coverage_summary() if summary_only else report_json()


@router.get(
    "/api/market/harvest/status",
    summary="Periodic auto-update scheduler status (per-task cadence + last run)",
)
async def get_harvest_status(
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Report the auto-update heartbeat: whether the periodic scheduler is on,
    its tick interval, and every task's cadence / last-run / failure count."""
    from data_integrations.periodic import scheduler

    return scheduler.status()


@router.post(
    "/api/market/harvest/run/{task_name}",
    summary="Manually trigger a periodic harvest task now (platform admin)",
)
async def run_harvest_task(
    task_name: str,
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Force a scheduled task to run immediately, outside its cadence. Useful
    for an on-demand refresh of a state/city dataset. Platform-admin only."""
    require_role(ctx, Role.PLATFORM_ADMIN)
    from data_integrations.periodic import scheduler

    try:
        result = await scheduler.run_now(task_name)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return {"task": task_name, "result": result}


