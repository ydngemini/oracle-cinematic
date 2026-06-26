"""Transactional compliance engine: checklist generation, disclosure tracking, form validation."""
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

@router.post(
    "/api/compliance/check",
    response_model=ComplianceCheckResponse,
    summary="Generate a required-disclosure list for a transaction context",
)
async def compliance_check(
    body: TransactionContext,
    ctx: TenantContext = Depends(require_context),
) -> ComplianceCheckResponse:
    """Run the compliance engine against the given transaction context.

    Returns every required form and disclosure for the combination of state,
    property type, and transaction characteristics.  Federal disclosures (e.g.
    lead-paint) are flagged separately from state-mandated items.

    This endpoint is purely computational — it does not persist anything.  Call
    ``POST /api/compliance/checklist`` (via the transaction router) to
    materialise the result as a tracked checklist row.
    """
    result = _engine.check(body)
    logger.info(
        "Compliance check: state=%s type=%s required=%d tenant=%s",
        body.state_code,
        body.property_type,
        result.total_required,
        ctx.tenant_id,
    )
    return result


@router.get(
    "/api/compliance/checklist/{transaction_id}",
    response_model=ComplianceChecklist,
    summary="Track disclosure completion for a transaction",
)
async def get_compliance_checklist(
    transaction_id: str,
    ctx: TenantContext = Depends(require_context),
) -> ComplianceChecklist:
    """Return the disclosure checklist for the given transaction.

    Each item tracks one required form through its lifecycle: ``pending`` →
    ``delivered`` → ``signed`` (or ``waived`` with documented reason).
    """
    txn_id = _require_uuid(transaction_id, "transaction_id")

    # Verify the transaction belongs to this tenant.
    txn_row = await _fetchrow(
        ctx,
        "SELECT state_code, tenant_id FROM transactions WHERE id = $1",
        txn_id,
    )
    if not txn_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {txn_id!r} not found.",
        )

    items_rows = await _fetch(
        ctx,
        """
        SELECT * FROM compliance_checklist_items
        WHERE transaction_id = $1
        ORDER BY form_name
        """,
        txn_id,
    )

    today = date.today()
    items: list[DisclosureChecklistItem] = []
    for r in items_rows:
        due = r.get("due_date")
        items.append(
            DisclosureChecklistItem(
                item_id=str(r.get("id", uuid.uuid4())),
                transaction_id=txn_id,
                disclosure_id=r.get("disclosure_id", ""),
                form_name=r.get("form_name", ""),
                status=r.get("status", "pending"),
                due_date=due,
                delivered_at=r.get("delivered_at"),
                signed_at=r.get("signed_at"),
                signed_by=r.get("signed_by"),
                notes=r.get("notes"),
            )
        )

    completed = sum(1 for i in items if i.status in ("delivered", "signed", "waived"))
    pending = sum(1 for i in items if i.status == "pending")
    overdue = sum(
        1 for i in items
        if i.status == "pending" and isinstance(i.due_date, date) and i.due_date < today
    )

    return ComplianceChecklist(
        transaction_id=txn_id,
        state_code=txn_row.get("state_code", ""),
        total_items=len(items),
        completed=completed,
        pending=pending,
        overdue=overdue,
        items=items,
    )


@router.post(
    "/api/compliance/validate",
    response_model=FormValidationResponse,
    summary="Validate a filled form against state rules",
)
async def validate_form(
    body: FormValidationBody,
    ctx: TenantContext = Depends(require_context),
) -> FormValidationResponse:
    """Validate a completed disclosure or contract form against the compliance
    rules for the specified state.

    The ``form_data`` object should carry all fields present on the physical
    form.  The engine checks required fields, format constraints, and
    state-specific business rules.

    Returns ``is_valid: true`` only when ``errors`` is empty (warnings are
    informational and do not block submission).
    """
    code = _require_state(body.state_code)
    errors, warnings = _engine.validate_form(code, body.form_type, body.form_data)

    logger.info(
        "Form validation: state=%s form=%s errors=%d warnings=%d tenant=%s",
        code,
        body.form_type,
        len(errors),
        len(warnings),
        ctx.tenant_id,
    )

    return FormValidationResponse(
        state_code=code,
        form_type=body.form_type,
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        validated_at=datetime.now(timezone.utc),
    )
