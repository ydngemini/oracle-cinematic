"""State regulatory reference: profiles, disclosure forms, contracts, advertising, licensing requirements, reciprocity."""
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
    _CE_HOURS_BY_STATE, _RECIPROCITY_MATRIX,
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
    "/api/states",
    response_model=list[StateSummary],
    summary="List all 50 states with summary compliance info",
)
async def list_states(
    ctx: TenantContext = Depends(require_context),
) -> list[StateSummary]:
    """Return a summary record for every US state (plus DC).

    The data is drawn from the ``state_regulatory_profiles`` table.  If the
    table is empty or the DB is unavailable the engine falls back to the
    module-level constants so the endpoint always returns a useful response.
    """
    try:
        rows = await _fetch(
            ctx,
            "SELECT * FROM state_regulatory_profiles ORDER BY state_code",
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            rows = []
        else:
            raise

    if rows:
        return [
            StateSummary(
                state_code=r["state_code"],
                state_name=r.get("state_name", r["state_code"]),
                attorney_review_required=r.get("attorney_review_required", False),
                mandatory_disclosure=r.get("mandatory_disclosure", False),
                has_tds=r.get("has_tds", False),
                license_authority=r.get("license_authority", "State Real Estate Commission"),
                regulatory_url=r.get("regulatory_url"),
            )
            for r in rows
        ]

    # Fallback to module constants.
    return [
        StateSummary(
            state_code=code,
            state_name=code,  # full names stored in DB; code is the safe fallback
            attorney_review_required=code in _ATTORNEY_REVIEW_STATES,
            mandatory_disclosure=code in _MANDATORY_DISCLOSURE_STATES,
            has_tds=code in _TDS_STATES,
            license_authority="State Real Estate Commission",
        )
        for code in sorted(ALL_STATE_CODES)
    ]


@router.get(
    "/api/states/{state_code}",
    response_model=StateProfile,
    summary="Full regulatory profile for a state",
)
async def get_state_profile(
    state_code: str,
    ctx: TenantContext = Depends(require_context),
) -> StateProfile:
    """Return the complete regulatory profile for one state.

    Includes attorney-review requirement, dual agency rules, CE hours, renewal
    cycle, and transfer-tax rate.
    """
    code = _require_state(state_code)

    row = await _fetchrow(
        ctx,
        "SELECT * FROM state_regulatory_profiles WHERE state_code = $1",
        code,
    )

    if row:
        return StateProfile(
            state_code=row["state_code"],
            state_name=row.get("state_name", code),
            attorney_review_required=row.get("attorney_review_required", False),
            mandatory_disclosure=row.get("mandatory_disclosure", False),
            has_tds=row.get("has_tds", False),
            license_authority=row.get("license_authority", "State Real Estate Commission"),
            license_authority_url=row.get("license_authority_url"),
            regulatory_url=row.get("regulatory_url"),
            ce_hours_per_cycle=row.get("ce_hours_per_cycle", _CE_HOURS_BY_STATE.get(code)),
            license_renewal_years=row.get("license_renewal_years", 2),
            buyer_agency_required=row.get("buyer_agency_required", False),
            dual_agency_permitted=row.get("dual_agency_permitted", True),
            designated_agency_permitted=row.get("designated_agency_permitted", True),
            sub_agency_permitted=row.get("sub_agency_permitted", True),
            earnest_money_escrow_days=row.get("earnest_money_escrow_days"),
            closing_attorney_states=code in ATTORNEY_CLOSE_STATES,
            transfer_tax_rate=row.get("transfer_tax_rate"),
            notes=row.get("notes"),
        )

    # Row not yet seeded — return a best-effort profile from module constants.
    return StateProfile(
        state_code=code,
        state_name=code,
        attorney_review_required=code in _ATTORNEY_REVIEW_STATES,
        mandatory_disclosure=code in _MANDATORY_DISCLOSURE_STATES,
        has_tds=code in _TDS_STATES,
        license_authority="State Real Estate Commission",
        closing_attorney_states=code in ATTORNEY_CLOSE_STATES,
        ce_hours_per_cycle=_CE_HOURS_BY_STATE.get(code),
    )


@router.get(
    "/api/states/{state_code}/forms",
    response_model=list[DisclosureForm],
    summary="Required disclosure forms for a state",
)
async def list_state_forms(
    state_code: str,
    form_type: Optional[str] = Query(default=None, description="Filter by form type"),
    ctx: TenantContext = Depends(require_context),
) -> list[DisclosureForm]:
    """Return all required disclosure and transaction forms for the given state.

    Optionally filter by ``form_type`` (e.g. ``seller_disclosure``, ``tds``,
    ``lead_paint``).
    """
    code = _require_state(state_code)
    query = "SELECT * FROM state_disclosure_forms WHERE state_code = $1"
    args: list[Any] = [code]
    if form_type:
        query += " AND form_type = $2"
        args.append(form_type)
    query += " ORDER BY form_type, form_name"

    rows = await _fetch(ctx, query, *args)
    return [
        DisclosureForm(
            form_id=str(r["id"]) if "id" in r else str(uuid.uuid4()),
            state_code=r["state_code"],
            form_name=r["form_name"],
            form_type=r["form_type"],
            required_when=r.get("required_when", ""),
            effective_date=r.get("effective_date"),
            download_url=r.get("download_url"),
            notes=r.get("notes"),
        )
        for r in rows
    ]


@router.get(
    "/api/states/{state_code}/contracts",
    response_model=list[ContractTemplate],
    summary="Contract templates for a state",
)
async def list_state_contracts(
    state_code: str,
    property_type: Optional[str] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
) -> list[ContractTemplate]:
    """Return the contract templates associated with a given state.

    Templates are provided by the state REALTOR® association (e.g. CAR for
    California, TAR for Texas).  Filter by ``property_type`` when provided.
    """
    code = _require_state(state_code)
    query = "SELECT * FROM state_contract_templates WHERE state_code = $1"
    args: list[Any] = [code]
    if property_type:
        query += " AND $2 = ANY(property_types)"
        args.append(property_type)
    query += " ORDER BY template_name"

    rows = await _fetch(ctx, query, *args)
    return [
        ContractTemplate(
            template_id=str(r.get("id", uuid.uuid4())),
            state_code=r["state_code"],
            template_name=r["template_name"],
            association=r.get("association", ""),
            property_types=r.get("property_types") or [],
            version=r.get("version", ""),
            effective_date=r.get("effective_date"),
            download_url=r.get("download_url"),
        )
        for r in rows
    ]


@router.get(
    "/api/states/{state_code}/advertising-rules",
    response_model=list[AdvertisingRule],
    summary="Advertising compliance rules for a state",
)
async def list_advertising_rules(
    state_code: str,
    category: Optional[str] = Query(default=None, description="team_names | internet_ads | ..."),
    ctx: TenantContext = Depends(require_context),
) -> list[AdvertisingRule]:
    """Return advertising and marketing compliance rules for a state.

    Categories include ``team_names``, ``brokerage_name``, ``internet_ads``,
    ``social_media``, and ``solicitation``.
    """
    code = _require_state(state_code)
    query = "SELECT * FROM state_advertising_rules WHERE state_code = $1"
    args: list[Any] = [code]
    if category:
        query += " AND category = $2"
        args.append(category)
    query += " ORDER BY category, id"

    rows = await _fetch(ctx, query, *args)
    return [
        AdvertisingRule(
            rule_id=str(r.get("id", uuid.uuid4())),
            state_code=r["state_code"],
            category=r["category"],
            requirement=r["requirement"],
            enforcement_body=r.get("enforcement_body", ""),
            citations=r.get("citations") or [],
        )
        for r in rows
    ]


# ===========================================================================
# 2. Licensing API
# ===========================================================================

@router.get(
    "/api/licensing/requirements/{state_code}",
    response_model=LicenseRequirements,
    summary="Licensing requirements to practice real estate in a state",
)
async def get_license_requirements(
    state_code: str,
    license_type: str = Query(
        default="salesperson",
        description="salesperson | broker | broker_associate",
    ),
    ctx: TenantContext = Depends(require_context),
) -> LicenseRequirements:
    """Return the full set of requirements needed to obtain a license in the
    specified state.  Includes pre-license education hours, exam requirements,
    CE obligations, and renewal cycle.
    """
    code = _require_state(state_code)
    row = await _fetchrow(
        ctx,
        """
        SELECT * FROM state_licensing_requirements
        WHERE state_code = $1 AND license_type = $2
        """,
        code,
        license_type,
    )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Licensing requirements for {code}/{license_type} not found.",
        )

    return LicenseRequirements(
        state_code=row["state_code"],
        license_type=row["license_type"],
        pre_license_hours=row["pre_license_hours"],
        exam_required=row.get("exam_required", True),
        background_check=row.get("background_check", True),
        errors_omissions_required=row.get("errors_omissions_required", False),
        ce_hours_per_cycle=row.get("ce_hours_per_cycle", _CE_HOURS_BY_STATE.get(code, 0)),
        renewal_cycle_years=row.get("renewal_cycle_years", 2),
        sponsoring_broker_required=row.get("sponsoring_broker_required", True),
        license_authority=row.get("license_authority", "State Real Estate Commission"),
        application_fee_usd=_num(row.get("application_fee_usd")),
        exam_provider=row.get("exam_provider"),
        notes=row.get("notes"),
    )


@router.get(
    "/api/licensing/reciprocity/{from_state}/{to_state}",
    response_model=ReciprocityInfo,
    summary="Check reciprocity between two states",
)
async def get_reciprocity(
    from_state: str,
    to_state: str,
    ctx: TenantContext = Depends(require_context),
) -> ReciprocityInfo:
    """Return reciprocity information for an agent licensed in ``from_state``
    seeking to practice in ``to_state``.

    The ``reciprocity_class`` field will be ``full``, ``partial``, or ``none``.
    For ``full`` reciprocity the agent can apply by endorsement.  For
    ``partial`` an additional state law exam is typically required.
    """
    fc = _require_state(from_state)
    tc = _require_state(to_state)

    if fc == tc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from_state and to_state must differ.",
        )

    row = await _fetchrow(
        ctx,
        """
        SELECT * FROM state_reciprocity_matrix
        WHERE from_state = $1 AND to_state = $2
        """,
        fc,
        tc,
    )

    if row:
        return ReciprocityInfo(
            from_state=row["from_state"],
            to_state=row["to_state"],
            reciprocity_class=row["reciprocity_class"],
            additional_requirements=row.get("additional_requirements") or [],
            notes=row.get("notes"),
        )

    # Fallback to module-level constant matrix.
    rec_class = _RECIPROCITY_MATRIX.get((fc, tc), "none")
    notes = (
        "Reciprocity data not yet seeded for this pair — defaulting to module constant. "
        "Verify with the destination state's real estate commission."
    )
    return ReciprocityInfo(
        from_state=fc,
        to_state=tc,
        reciprocity_class=rec_class,
        additional_requirements=(
            ["Additional state law exam required."] if rec_class == "partial" else []
        ),
        notes=notes,
    )


