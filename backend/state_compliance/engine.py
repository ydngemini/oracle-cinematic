"""State-specific compliance business rules (ComplianceEngine)."""
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

class ComplianceEngine:
    """Encapsulates state-specific compliance logic.

    The engine is stateless: all methods are pure functions over the provided
    context.  State-specific rules are expressed as data (dicts/sets) at module
    level so they can be updated without changing call sites, or overridden with
    DB-sourced rows in a future migration.

    Usage::

        engine = ComplianceEngine()
        result = engine.check(ctx_body)
        checklist = engine.build_checklist(transaction_id, result)
    """

    # Disclosure rules table: (trigger_fn, form_name, form_type, is_federal, notes)
    # Each trigger_fn receives a TransactionContext and returns bool.
    _DISCLOSURE_RULES: list[tuple] = [
        # Federal rules — apply in all states
        (
            # TSCA 1018 (42 U.S.C. § 4852d) covers "target housing" — residential
            # built before 1978. FAIL CLOSED on an unknown build year: an unverified
            # year_built must REQUIRE the disclosure, never silently skip it. Only a
            # KNOWN year >= 1978 exempts the property.
            lambda c: (
                c.property_type in ("residential_1_4", "condo", "multi_family")
                and (
                    c.year_built is None
                    or c.year_built < _FEDERAL_LEAD_PAINT_THRESHOLD_YEAR
                )
            ),
            "Lead-Based Paint Disclosure",
            "lead_paint",
            True,
            "Required by 42 U.S.C. § 4852d on pre-1978 residential housing "
            "(an unknown build year is treated as pre-1978 and requires disclosure).",
        ),
        # Seller disclosure — mandatory states
        (
            lambda c: c.state_code in _MANDATORY_DISCLOSURE_STATES
                and c.property_type != "land",
            "Seller's Property Disclosure",
            "seller_disclosure",
            False,
            "State-mandated disclosure of known material defects.",
        ),
        # TDS
        (
            lambda c: c.state_code in _TDS_STATES
                and c.property_type in ("residential_1_4", "condo"),
            "Transfer Disclosure Statement",
            "tds",
            False,
            "Required TDS or equivalent in this state.",
        ),
        # Buyer agency
        (
            lambda c: c.buyer_represented,
            "Buyer Representation Agreement",
            "buyer_agency",
            False,
            "Required whenever a buyer is represented.",
        ),
        # Dual agency
        (
            lambda c: c.dual_agency,
            "Dual Agency Disclosure and Consent",
            "dual_agency",
            False,
            "Informed consent required before dual agency commences.",
        ),
        # New construction
        (
            lambda c: c.is_new_construction,
            "New Construction Disclosure",
            "new_construction",
            False,
            "Builder warranty and defect disclosure for new builds.",
        ),
        # HOA
        (
            lambda c: c.has_hoa,
            "HOA Documents and Resale Certificate",
            "hoa_docs",
            False,
            "Governing docs, financials, and resale certificate required.",
        ),
        # Flood zone
        (
            lambda c: c.in_flood_zone,
            "Flood Zone Disclosure",
            "flood_zone",
            False,
            "FEMA flood zone designation and insurance requirement.",
        ),
        # Septic
        (
            lambda c: c.septic_system,
            "Septic System Disclosure",
            "septic",
            False,
            "Last inspection date, capacity, and known deficiencies.",
        ),
        # Well water
        (
            lambda c: c.well_water,
            "Well Water Disclosure",
            "well_water",
            False,
            "Water quality test results and system condition.",
        ),
    ]

    # Form-field validators by (state_code, form_type) — returns list[ValidationError].
    # Extend with DB-sourced rule rows for production.
    _FIELD_VALIDATORS: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {
        # (state, form_type): [(field_path, code, message, severity)]
        ("CA", "seller_disclosure"): [
            ("seller_name", "required", "Seller name is required.", "error"),
            ("property_address", "required", "Property address is required.", "error"),
            ("known_defects", "required", "Known defects field is required.", "error"),
        ],
        ("TX", "seller_disclosure"): [
            ("seller_name", "required", "Seller name is required.", "error"),
            ("property_address", "required", "Property address is required.", "error"),
            ("occupancy_status", "required", "Current occupancy status required.", "error"),
        ],
        ("FL", "tds"): [
            ("seller_name", "required", "Seller name required.", "error"),
            ("property_address", "required", "Property address required.", "error"),
            ("hoa_exists", "required", "HOA existence flag required.", "error"),
        ],
    }

    def check(self, ctx: TransactionContext) -> ComplianceCheckResponse:
        """Evaluate all disclosure rules against the provided transaction context.

        Returns a ComplianceCheckResponse listing every required form and
        disclosure, sorted with mandatory federal items first.
        """
        disclosures: list[RequiredDisclosure] = []
        compliance_notes: list[str] = []
        idx = 0

        for trigger_fn, form_name, form_type, is_federal, notes in self._DISCLOSURE_RULES:
            try:
                triggered = trigger_fn(ctx)
            except Exception:
                # Never let a bad rule crash the engine — skip with a log.
                logger.warning(
                    "Compliance rule eval error for form_type=%r state=%r.",
                    form_type, ctx.state_code,
                )
                continue

            if triggered:
                idx += 1
                disclosures.append(
                    RequiredDisclosure(
                        disclosure_id=f"{ctx.state_code}-{form_type}-{idx:03d}",
                        form_name=form_name,
                        form_type=form_type,
                        trigger_reason=notes,
                        is_federal=is_federal,
                        is_mandatory=True,
                        notes=notes,
                    )
                )

        # State-specific compliance notes
        if ctx.state_code in _ATTORNEY_REVIEW_STATES:
            compliance_notes.append(
                f"{ctx.state_code} requires an attorney to review and/or close the transaction."
            )

        if ctx.dual_agency and ctx.state_code in {"FL", "CO", "MD", "WI"}:
            compliance_notes.append(
                f"Dual agency in {ctx.state_code} requires written consent from all parties "
                f"before any negotiation begins."
            )

        if ctx.property_type == "condo" and ctx.state_code in {"FL", "NY", "IL", "VA"}:
            compliance_notes.append(
                f"{ctx.state_code} condominiums require a public offering statement or "
                f"equivalent resale package."
            )

        # Sort: federal first, then alphabetical by form_type.
        disclosures.sort(key=lambda d: (not d.is_federal, d.form_type))

        return ComplianceCheckResponse(
            state_code=ctx.state_code,
            property_type=ctx.property_type,
            required_disclosures=disclosures,
            required_forms=[d.form_name for d in disclosures],
            attorney_review_required=ctx.state_code in _ATTORNEY_REVIEW_STATES,
            dual_agency_permitted=ctx.state_code
                not in {"AK", "CO", "FL", "MD", "TX", "WI"},
            total_required=len(disclosures),
            compliance_notes=compliance_notes,
        )

    def build_checklist(
        self,
        transaction_id: str,
        check_result: ComplianceCheckResponse,
    ) -> list[dict]:
        """Convert a ComplianceCheckResponse into checklist row dicts for INSERT."""
        rows = []
        for d in check_result.required_disclosures:
            rows.append(
                {
                    "item_id": str(uuid.uuid4()),
                    "transaction_id": transaction_id,
                    "disclosure_id": d.disclosure_id,
                    "form_name": d.form_name,
                    "status": "pending",
                }
            )
        return rows

    def validate_form(
        self,
        state_code: str,
        form_type: str,
        form_data: dict[str, Any],
    ) -> tuple[list[ValidationError], list[ValidationError]]:
        """Validate form_data against state rules; return (errors, warnings)."""
        errors: list[ValidationError] = []
        warnings: list[ValidationError] = []

        rules = self._FIELD_VALIDATORS.get((state_code, form_type), [])
        # Also apply generic rules that have no state qualifier.
        rules = rules + self._FIELD_VALIDATORS.get(("*", form_type), [])

        for field, code, message, severity in rules:
            value = form_data.get(field)
            missing = value is None or (isinstance(value, str) and not value.strip())
            if missing:
                ve = ValidationError(field=field, code=code, message=message, severity=severity)
                if severity == "error":
                    errors.append(ve)
                else:
                    warnings.append(ve)

        return errors, warnings


# Module-level singleton — all routes share one engine instance.
_engine = ComplianceEngine()


# ===========================================================================
# 1. State Regulations API
# ===========================================================================

