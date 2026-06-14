"""
State Compliance, Licensing, MLS Integration, and Market Data Router.

Five endpoint groups covering the full regulatory surface for a multi-state
real-estate operation:

  * /api/states        — State regulatory profiles: disclosure forms, contract
                         templates, advertising rules.
  * /api/licensing     — Agent license requirements per state, reciprocity
                         matrix, per-agent status, CE credit logging.
  * /api/mls           — MLS board registry, sync health, normalized property
                         search, and listing detail.
  * /api/market        — State/county aggregate stats, flood zone (FEMA),
                         school district, and zoning lookups.
  * /api/compliance    — Transactional compliance engine: form checklist
                         generation, disclosure tracking, form validation.

Auth pattern: every route calls require_context (Bearer JWT → TenantContext).
Role-differentiated gates use require_role() inline.  All DB reads go through
tenant_tx(ctx) so RLS + SET LOCAL GUCs apply automatically.

Tenant scoping: compliance state data (forms, contracts) is shared/public but
agent licensing rows and transaction checklists are per-tenant.  The engine
class encapsulates the state-specific business rules; the routes are thin.
"""

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

logger = logging.getLogger("oracle.state_compliance")

router = APIRouter(tags=["State Compliance"])

# ---------------------------------------------------------------------------
# Input validation constants
# ---------------------------------------------------------------------------

_STATE_RE = re.compile(r"^[A-Z]{2}$")
_FIPS_RE = re.compile(r"^\d{5}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

ALL_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# Disclosure attorney-review states require an attorney at closing.
_ATTORNEY_REVIEW_STATES = {"CT", "DE", "GA", "MA", "NY", "SC", "WV"}

# States with mandatory seller disclosure forms (≠ "as-is" caveat emptor).
_MANDATORY_DISCLOSURE_STATES = ALL_STATE_CODES - {"AR", "NH", "NM", "ND", "WY"}

# States that operate a Transfer Disclosure Statement (TDS) or equivalent.
_TDS_STATES = {"CA", "CO", "FL", "IL", "MN", "OH", "OR", "TX", "WA"}

# Lead-paint disclosure required nationwide on pre-1978 housing (federal).
_FEDERAL_LEAD_PAINT_THRESHOLD_YEAR = 1978

# CE hours required per renewal cycle — a representative sample; full data is
# stored in the DB table state_licensing_requirements.
_CE_HOURS_BY_STATE: dict[str, int] = {
    "CA": 45, "TX": 18, "FL": 14, "NY": 22, "IL": 12,
    "GA": 36, "CO": 24, "WA": 30, "OR": 30, "AZ": 24,
}

# Reciprocity adjacency — value is the reciprocity class:
#   "full"    — no additional exam required
#   "partial" — additional state law exam required
#   "none"    — must re-apply from scratch
# Only a representative sample is stored here; the full matrix is in
# state_licensing_requirements rows with type = 'reciprocity'.
_RECIPROCITY_MATRIX: dict[tuple[str, str], str] = {
    ("GA", "AL"): "full",   ("GA", "FL"): "partial",
    ("TX", "OK"): "full",   ("TX", "NM"): "partial",
    ("CA", "NV"): "partial",("OR", "WA"): "full",
    ("CO", "UT"): "partial",("FL", "GA"): "partial",
    ("NY", "NJ"): "partial",("VA", "MD"): "full",
    ("VA", "DC"): "partial",("MD", "DC"): "partial",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


def _require_state(state_code: str) -> str:
    code = state_code.upper()
    if code not in ALL_STATE_CODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown state code: {state_code!r}.",
        )
    return code


def _require_uuid(value: str, name: str) -> str:
    if not value or not _UUID_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{name} must be a valid UUID.",
        )
    return value.lower()


async def _fetch(ctx: TenantContext, query: str, *args) -> list[dict]:
    """Single-read helper; maps connection failures to 503."""
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("State compliance DB query failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Core offline.",
        )


async def _fetchrow(ctx: TenantContext, query: str, *args) -> Optional[dict]:
    rows = await _fetch(ctx, query, *args)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Pydantic schemas — States
# ---------------------------------------------------------------------------

class StateSummary(BaseModel):
    """Abbreviated state record for list endpoints."""
    state_code: str
    state_name: str
    attorney_review_required: bool
    mandatory_disclosure: bool
    has_tds: bool
    license_authority: str
    regulatory_url: Optional[str] = None


class DisclosureForm(BaseModel):
    form_id: str
    state_code: str
    form_name: str
    form_type: str          # e.g. "seller_disclosure", "lead_paint", "tds", "flood"
    required_when: str      # human-readable trigger condition
    effective_date: Optional[date] = None
    download_url: Optional[str] = None
    notes: Optional[str] = None


class ContractTemplate(BaseModel):
    template_id: str
    state_code: str
    template_name: str
    association: str        # e.g. "CAR", "TAR", "GCAAR"
    property_types: list[str]
    version: str
    effective_date: Optional[date] = None
    download_url: Optional[str] = None


class AdvertisingRule(BaseModel):
    rule_id: str
    state_code: str
    category: str           # e.g. "team_names", "brokerage_name", "internet_ads"
    requirement: str
    enforcement_body: str
    citations: list[str] = Field(default_factory=list)


class StateProfile(BaseModel):
    state_code: str
    state_name: str
    attorney_review_required: bool
    mandatory_disclosure: bool
    has_tds: bool
    license_authority: str
    license_authority_url: Optional[str] = None
    regulatory_url: Optional[str] = None
    ce_hours_per_cycle: Optional[int] = None
    license_renewal_years: int = 2
    buyer_agency_required: bool = False
    dual_agency_permitted: bool = True
    designated_agency_permitted: bool = True
    sub_agency_permitted: bool = True
    earnest_money_escrow_days: Optional[int] = None
    closing_attorney_states: bool = False
    transfer_tax_rate: Optional[float] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Pydantic schemas — Licensing
# ---------------------------------------------------------------------------

class LicenseRequirements(BaseModel):
    state_code: str
    license_type: str       # "salesperson" | "broker" | "broker_associate"
    pre_license_hours: int
    exam_required: bool
    background_check: bool
    errors_omissions_required: bool
    ce_hours_per_cycle: int
    renewal_cycle_years: int
    sponsoring_broker_required: bool
    license_authority: str
    application_fee_usd: Optional[float] = None
    exam_provider: Optional[str] = None
    notes: Optional[str] = None


class ReciprocityInfo(BaseModel):
    from_state: str
    to_state: str
    reciprocity_class: str  # "full" | "partial" | "none"
    additional_requirements: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class AgentLicense(BaseModel):
    license_id: str
    agent_id: str
    state_code: str
    license_type: str
    license_number: str
    status: str             # "active" | "expired" | "suspended" | "inactive"
    issued_date: Optional[date] = None
    expiry_date: Optional[date] = None
    days_until_expiry: Optional[int] = None
    expiry_warning: bool = False   # True if expiring within 90 days
    ce_hours_completed: int = 0
    ce_hours_required: int = 0
    ce_deficit: int = 0


class AgentLicenseStatus(BaseModel):
    agent_id: str
    licenses: list[AgentLicense]
    total_active: int
    expiring_soon: int      # within 90 days
    expired: int


class CECreditBody(BaseModel):
    state_code: str = Field(..., min_length=2, max_length=2)
    provider: str = Field(..., min_length=1, max_length=256)
    course_name: str = Field(..., min_length=1, max_length=512)
    hours: float = Field(..., gt=0, le=100)
    completion_date: date
    certificate_number: Optional[str] = None

    @field_validator("state_code")
    @classmethod
    def upper_state(cls, v: str) -> str:
        return v.upper()


class CECreditResponse(BaseModel):
    ce_log_id: str
    agent_id: str
    state_code: str
    hours_logged: float
    total_hours_this_cycle: float
    hours_required: int
    deficit: float


# ---------------------------------------------------------------------------
# Pydantic schemas — MLS
# ---------------------------------------------------------------------------

class MLSRegion(BaseModel):
    mls_id: str
    mls_name: str
    states: list[str]
    counties: list[str]
    member_count: Optional[int] = None
    listing_count: Optional[int] = None
    feed_type: str          # "RETS" | "RESO_Web_API" | "IDX"
    data_sharing: str       # "full" | "IDX_only" | "VOW_only"
    website: Optional[str] = None


class MLSSyncStatus(BaseModel):
    mls_id: str
    mls_name: str
    feed_type: str
    last_sync_at: Optional[datetime] = None
    listings_synced: int = 0
    errors_last_24h: int = 0
    sync_lag_minutes: Optional[int] = None
    health: str             # "healthy" | "degraded" | "offline"
    notes: Optional[str] = None


class MLSSearchBody(BaseModel):
    mls_ids: list[str] = Field(default_factory=list)
    state_codes: list[str] = Field(default_factory=list)
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_beds: Optional[int] = None
    min_baths: Optional[float] = None
    min_sqft: Optional[int] = None
    max_sqft: Optional[int] = None
    property_types: list[str] = Field(default_factory=list)
    status: str = "active"  # "active" | "pending" | "sold"
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius_miles: Optional[float] = None
    limit: int = Field(default=25, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class NormalizedListing(BaseModel):
    listing_id: str
    mls_id: str
    mls_number: str
    address: str
    city: str
    state_code: str
    zip_code: str
    county: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    list_price: float
    orig_list_price: Optional[float] = None
    status: str
    property_type: str
    beds: Optional[int] = None
    baths_full: Optional[int] = None
    baths_half: Optional[int] = None
    sqft: Optional[int] = None
    lot_sqft: Optional[int] = None
    year_built: Optional[int] = None
    hoa_monthly: Optional[float] = None
    days_on_market: Optional[int] = None
    list_date: Optional[date] = None
    close_date: Optional[date] = None
    close_price: Optional[float] = None
    description: Optional[str] = None
    photos: list[str] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    last_updated: Optional[datetime] = None


class MLSSearchResponse(BaseModel):
    total_count: int
    offset: int
    limit: int
    listings: list[NormalizedListing]


# ---------------------------------------------------------------------------
# Pydantic schemas — Market Data
# ---------------------------------------------------------------------------

class StateMarketOverview(BaseModel):
    state_code: str
    state_name: str
    median_list_price: Optional[float] = None
    median_sale_price: Optional[float] = None
    median_days_on_market: Optional[float] = None
    months_of_supply: Optional[float] = None
    yoy_price_change_pct: Optional[float] = None
    active_listings: Optional[int] = None
    closed_sales_last_30d: Optional[int] = None
    list_to_sale_ratio: Optional[float] = None
    avg_price_per_sqft: Optional[float] = None
    as_of_date: Optional[date] = None


class CountyMarketData(BaseModel):
    fips_code: str
    county_name: str
    state_code: str
    median_sale_price: Optional[float] = None
    median_list_price: Optional[float] = None
    median_days_on_market: Optional[float] = None
    property_tax_rate_pct: Optional[float] = None
    effective_tax_rate_pct: Optional[float] = None
    median_annual_tax: Optional[float] = None
    population: Optional[int] = None
    households: Optional[int] = None
    homeownership_rate_pct: Optional[float] = None
    as_of_date: Optional[date] = None


class FloodZoneResult(BaseModel):
    latitude: float
    longitude: float
    fema_zone: str          # e.g. "AE", "X", "VE", "0.2PCT"
    zone_description: str
    flood_insurance_required: bool
    firm_panel: Optional[str] = None
    firm_date: Optional[date] = None
    community_name: Optional[str] = None
    community_number: Optional[str] = None


class SchoolDistrict(BaseModel):
    district_id: str
    district_name: str
    district_type: str      # "unified" | "elementary" | "high"
    state_code: str
    county: str
    nces_id: Optional[str] = None
    rating: Optional[float] = None      # 1-10 scale if available
    enrollment: Optional[int] = None
    student_teacher_ratio: Optional[float] = None
    distance_miles: Optional[float] = None
    website: Optional[str] = None


class SchoolsResponse(BaseModel):
    latitude: float
    longitude: float
    radius_miles: float
    districts: list[SchoolDistrict]


class ZoningResult(BaseModel):
    parcel_id: str
    zone_code: str
    zone_description: str
    zone_category: str      # "residential" | "commercial" | "industrial" | "agricultural" | "mixed"
    overlays: list[str] = Field(default_factory=list)
    permitted_uses: list[str] = Field(default_factory=list)
    conditional_uses: list[str] = Field(default_factory=list)
    max_height_ft: Optional[float] = None
    max_density_units_per_acre: Optional[float] = None
    min_lot_sqft: Optional[int] = None
    setback_front_ft: Optional[float] = None
    setback_rear_ft: Optional[float] = None
    setback_side_ft: Optional[float] = None
    jurisdiction: Optional[str] = None


# ---------------------------------------------------------------------------
# Pydantic schemas — Compliance Engine
# ---------------------------------------------------------------------------

class TransactionContext(BaseModel):
    """Input to the compliance check — describes the transaction being formed."""
    state_code: str = Field(..., min_length=2, max_length=2)
    property_type: str = Field(
        ...,
        description="residential_1_4 | condo | commercial | land | multi_family",
    )
    year_built: Optional[int] = Field(
        default=None,
        description="Construction year; drives lead-paint disclosure trigger.",
    )
    buyer_represented: bool = True
    dual_agency: bool = False
    is_new_construction: bool = False
    has_hoa: bool = False
    in_flood_zone: bool = False
    septic_system: bool = False
    well_water: bool = False
    seller_known_defects: bool = False
    transaction_id: Optional[str] = None

    @field_validator("state_code")
    @classmethod
    def upper_state(cls, v: str) -> str:
        return v.upper()

    @field_validator("property_type")
    @classmethod
    def valid_prop_type(cls, v: str) -> str:
        allowed = {
            "residential_1_4", "condo", "commercial",
            "land", "multi_family", "mobile_home",
        }
        if v not in allowed:
            raise ValueError(f"property_type must be one of {sorted(allowed)}")
        return v


class RequiredDisclosure(BaseModel):
    disclosure_id: str
    form_name: str
    form_type: str
    trigger_reason: str
    is_federal: bool = False
    is_mandatory: bool = True
    notes: Optional[str] = None


class ComplianceCheckResponse(BaseModel):
    state_code: str
    property_type: str
    required_disclosures: list[RequiredDisclosure]
    required_forms: list[str]
    attorney_review_required: bool
    dual_agency_permitted: bool
    total_required: int
    compliance_notes: list[str]


class DisclosureChecklistItem(BaseModel):
    item_id: str
    transaction_id: str
    disclosure_id: str
    form_name: str
    status: str             # "pending" | "delivered" | "signed" | "waived"
    due_date: Optional[date] = None
    delivered_at: Optional[datetime] = None
    signed_at: Optional[datetime] = None
    signed_by: Optional[str] = None
    notes: Optional[str] = None


class ComplianceChecklist(BaseModel):
    transaction_id: str
    state_code: str
    total_items: int
    completed: int
    pending: int
    overdue: int
    items: list[DisclosureChecklistItem]


class FormValidationBody(BaseModel):
    state_code: str = Field(..., min_length=2, max_length=2)
    form_type: str
    form_data: dict[str, Any]
    transaction_id: Optional[str] = None

    @field_validator("state_code")
    @classmethod
    def upper_state(cls, v: str) -> str:
        return v.upper()


class ValidationError(BaseModel):
    field: str
    code: str
    message: str
    severity: str = "error"  # "error" | "warning"


class FormValidationResponse(BaseModel):
    state_code: str
    form_type: str
    is_valid: bool
    errors: list[ValidationError]
    warnings: list[ValidationError]
    validated_at: datetime


# ---------------------------------------------------------------------------
# Compliance Engine
# ---------------------------------------------------------------------------

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
            lambda c: (
                c.property_type in ("residential_1_4", "condo", "multi_family")
                and c.year_built is not None
                and c.year_built < _FEDERAL_LEAD_PAINT_THRESHOLD_YEAR
            ),
            "Lead-Based Paint Disclosure",
            "lead_paint",
            True,
            "Required by 42 U.S.C. § 4852d on pre-1978 residential housing.",
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
            closing_attorney_states=code in _ATTORNEY_REVIEW_STATES,
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
        closing_attorney_states=code in _ATTORNEY_REVIEW_STATES,
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
    query += " ORDER BY category, rule_id"

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
    aid = _require_uuid(agent_id, "agent_id")

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
    aid = _require_uuid(agent_id, "agent_id")
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

@router.get(
    "/api/mls/regions",
    response_model=list[MLSRegion],
    summary="All MLS boards with state and county coverage",
)
async def list_mls_regions(
    state_code: Optional[str] = Query(default=None, description="Filter by state"),
    ctx: TenantContext = Depends(require_context),
) -> list[MLSRegion]:
    """Return all known MLS boards.  Optionally filter by state code."""
    query = "SELECT * FROM mls_boards"
    args: list[Any] = []
    if state_code:
        code = _require_state(state_code)
        query += " WHERE $1 = ANY(states)"
        args.append(code)
    query += " ORDER BY mls_name"

    rows = await _fetch(ctx, query, *args)
    return [
        MLSRegion(
            mls_id=str(r.get("id", uuid.uuid4())),
            mls_name=r["mls_name"],
            states=r.get("states") or [],
            counties=r.get("counties") or [],
            member_count=r.get("member_count"),
            listing_count=r.get("listing_count"),
            feed_type=r.get("feed_type", "RESO_Web_API"),
            data_sharing=r.get("data_sharing", "IDX_only"),
            website=r.get("website"),
        )
        for r in rows
    ]


@router.get(
    "/api/mls/regions/{mls_id}/status",
    response_model=MLSSyncStatus,
    summary="Sync health for an MLS feed",
)
async def get_mls_sync_status(
    mls_id: str,
    ctx: TenantContext = Depends(require_context),
) -> MLSSyncStatus:
    """Return feed synchronisation health for the specified MLS board.

    The ``health`` field summarises: ``healthy`` (lag < 60 min, no errors),
    ``degraded`` (lag 60–240 min or minor errors), ``offline`` (no sync > 4h).
    """
    row = await _fetchrow(
        ctx,
        "SELECT * FROM mls_sync_status WHERE mls_id = $1",
        mls_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MLS region {mls_id!r} not found.",
        )

    lag = row.get("sync_lag_minutes")
    errors = row.get("errors_last_24h", 0)
    if lag is None or lag > 240 or errors > 50:
        health = "offline"
    elif lag > 60 or errors > 5:
        health = "degraded"
    else:
        health = "healthy"

    return MLSSyncStatus(
        mls_id=mls_id,
        mls_name=row.get("mls_name", ""),
        feed_type=row.get("feed_type", "RESO_Web_API"),
        last_sync_at=row.get("last_sync_at"),
        listings_synced=row.get("listings_synced", 0),
        errors_last_24h=errors,
        sync_lag_minutes=lag,
        health=health,
        notes=row.get("notes"),
    )


@router.post(
    "/api/mls/search",
    response_model=MLSSearchResponse,
    summary="Normalized property search across one or more MLSs",
)
async def mls_search(
    body: MLSSearchBody,
    ctx: TenantContext = Depends(require_context),
) -> MLSSearchResponse:
    """Execute a normalized property search against the oracle_mls_listings view.

    The view unions listings from all configured MLS feeds into a single
    schema.  Filters include price range, beds/baths, sqft, property type,
    status, and optional radius search when ``lat``/``lng`` are provided.
    """
    conditions: list[str] = ["1 = 1"]
    args: list[Any] = []
    idx = 0

    def _arg(v: Any) -> str:
        nonlocal idx
        args.append(v)
        idx += 1
        return f"${idx}"

    if body.mls_ids:
        conditions.append(f"mls_id = ANY({_arg(body.mls_ids)})")
    if body.state_codes:
        conditions.append(f"state_code = ANY({_arg(body.state_codes)})")
    if body.min_price is not None:
        conditions.append(f"list_price >= {_arg(body.min_price)}")
    if body.max_price is not None:
        conditions.append(f"list_price <= {_arg(body.max_price)}")
    if body.min_beds is not None:
        conditions.append(f"beds >= {_arg(body.min_beds)}")
    if body.min_baths is not None:
        conditions.append(f"(baths_full + baths_half * 0.5) >= {_arg(body.min_baths)}")
    if body.min_sqft is not None:
        conditions.append(f"sqft >= {_arg(body.min_sqft)}")
    if body.max_sqft is not None:
        conditions.append(f"sqft <= {_arg(body.max_sqft)}")
    if body.property_types:
        conditions.append(f"property_type = ANY({_arg(body.property_types)})")
    if body.status:
        conditions.append(f"status = {_arg(body.status)}")
    if body.lat is not None and body.lng is not None and body.radius_miles is not None:
        # PostGIS: earth_distance via the earthdistance extension (miles).
        conditions.append(
            f"earth_distance(ll_to_earth(latitude, longitude), "
            f"ll_to_earth({_arg(body.lat)}, {_arg(body.lng)})) "
            f"<= {_arg(body.radius_miles * 1609.34)}"
        )

    where = " AND ".join(conditions)
    count_q = f"SELECT COUNT(*) FROM oracle_mls_listings WHERE {where}"
    data_q = (
        f"SELECT * FROM oracle_mls_listings WHERE {where} "
        f"ORDER BY list_date DESC NULLS LAST "
        f"LIMIT {_arg(body.limit)} OFFSET {_arg(body.offset)}"
    )

    try:
        async with tenant_tx(ctx) as conn:
            count_row = await conn.fetchrow(count_q, *args[: idx - 2])
            total = int(count_row["count"]) if count_row else 0
            rows = [dict(r) for r in await conn.fetch(data_q, *args)]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("MLS search failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Core offline.",
        )

    listings = [
        NormalizedListing(
            listing_id=str(r.get("id", uuid.uuid4())),
            mls_id=r.get("mls_id", ""),
            mls_number=r.get("mls_number", ""),
            address=r.get("address", ""),
            city=r.get("city", ""),
            state_code=r.get("state_code", ""),
            zip_code=r.get("zip_code", ""),
            county=r.get("county", ""),
            latitude=_num(r.get("latitude")),
            longitude=_num(r.get("longitude")),
            list_price=float(r.get("list_price", 0)),
            orig_list_price=_num(r.get("orig_list_price")),
            status=r.get("status", "active"),
            property_type=r.get("property_type", "residential_1_4"),
            beds=r.get("beds"),
            baths_full=r.get("baths_full"),
            baths_half=r.get("baths_half"),
            sqft=r.get("sqft"),
            lot_sqft=r.get("lot_sqft"),
            year_built=r.get("year_built"),
            hoa_monthly=_num(r.get("hoa_monthly")),
            days_on_market=r.get("days_on_market"),
            list_date=r.get("list_date"),
            close_date=r.get("close_date"),
            close_price=_num(r.get("close_price")),
            description=r.get("description"),
            photos=r.get("photos") or [],
            features=r.get("features") or {},
            last_updated=r.get("last_updated"),
        )
        for r in rows
    ]

    return MLSSearchResponse(
        total_count=total,
        offset=body.offset,
        limit=body.limit,
        listings=listings,
    )


@router.get(
    "/api/mls/listing/{listing_id}",
    response_model=NormalizedListing,
    summary="Normalized listing detail",
)
async def get_mls_listing(
    listing_id: str,
    ctx: TenantContext = Depends(require_context),
) -> NormalizedListing:
    """Return the full normalized listing record for the given ID."""
    row = await _fetchrow(
        ctx,
        "SELECT * FROM oracle_mls_listings WHERE id = $1::uuid",
        listing_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id!r} not found.",
        )
    return NormalizedListing(
        listing_id=str(row.get("id", listing_id)),
        mls_id=row.get("mls_id", ""),
        mls_number=row.get("mls_number", ""),
        address=row.get("address", ""),
        city=row.get("city", ""),
        state_code=row.get("state_code", ""),
        zip_code=row.get("zip_code", ""),
        county=row.get("county", ""),
        latitude=_num(row.get("latitude")),
        longitude=_num(row.get("longitude")),
        list_price=float(row.get("list_price", 0)),
        orig_list_price=_num(row.get("orig_list_price")),
        status=row.get("status", "active"),
        property_type=row.get("property_type", "residential_1_4"),
        beds=row.get("beds"),
        baths_full=row.get("baths_full"),
        baths_half=row.get("baths_half"),
        sqft=row.get("sqft"),
        lot_sqft=row.get("lot_sqft"),
        year_built=row.get("year_built"),
        hoa_monthly=_num(row.get("hoa_monthly")),
        days_on_market=row.get("days_on_market"),
        list_date=row.get("list_date"),
        close_date=row.get("close_date"),
        close_price=_num(row.get("close_price")),
        description=row.get("description"),
        photos=row.get("photos") or [],
        features=row.get("features") or {},
        last_updated=row.get("last_updated"),
    )


# ===========================================================================
# 4. Market Data API
# ===========================================================================

@router.get(
    "/api/market/{state_code}/overview",
    response_model=StateMarketOverview,
    summary="Aggregate market statistics for a state",
)
async def get_state_market_overview(
    state_code: str,
    ctx: TenantContext = Depends(require_context),
) -> StateMarketOverview:
    """Return aggregate market data for the given state.

    Data is sourced from the ``state_market_stats`` materialised view, which
    the harvester pipeline refreshes nightly from public data feeds.
    """
    code = _require_state(state_code)
    row = await _fetchrow(
        ctx,
        "SELECT * FROM state_market_stats WHERE state_code = $1",
        code,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Market data not available for {code}.",
        )
    return StateMarketOverview(
        state_code=row["state_code"],
        state_name=row.get("state_name", code),
        median_list_price=_num(row.get("median_list_price")),
        median_sale_price=_num(row.get("median_sale_price")),
        median_days_on_market=_num(row.get("median_days_on_market")),
        months_of_supply=_num(row.get("months_of_supply")),
        yoy_price_change_pct=_num(row.get("yoy_price_change_pct")),
        active_listings=row.get("active_listings"),
        closed_sales_last_30d=row.get("closed_sales_last_30d"),
        list_to_sale_ratio=_num(row.get("list_to_sale_ratio")),
        avg_price_per_sqft=_num(row.get("avg_price_per_sqft")),
        as_of_date=row.get("as_of_date"),
    )


@router.get(
    "/api/market/county/{fips_code}",
    response_model=CountyMarketData,
    summary="County-level market and tax data",
)
async def get_county_market_data(
    fips_code: str,
    ctx: TenantContext = Depends(require_context),
) -> CountyMarketData:
    """Return market statistics and property tax rate data for the specified
    county FIPS code (5-digit, e.g. ``13121`` for Fulton County, GA).
    """
    if not _FIPS_RE.match(fips_code):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="fips_code must be a 5-digit string.",
        )
    row = await _fetchrow(
        ctx,
        "SELECT * FROM county_market_stats WHERE fips_code = $1",
        fips_code,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"County data not available for FIPS {fips_code!r}.",
        )
    return CountyMarketData(
        fips_code=row["fips_code"],
        county_name=row.get("county_name", ""),
        state_code=row.get("state_code", ""),
        median_sale_price=_num(row.get("median_sale_price")),
        median_list_price=_num(row.get("median_list_price")),
        median_days_on_market=_num(row.get("median_days_on_market")),
        property_tax_rate_pct=_num(row.get("property_tax_rate_pct")),
        effective_tax_rate_pct=_num(row.get("effective_tax_rate_pct")),
        median_annual_tax=_num(row.get("median_annual_tax")),
        population=row.get("population"),
        households=row.get("households"),
        homeownership_rate_pct=_num(row.get("homeownership_rate_pct")),
        as_of_date=row.get("as_of_date"),
    )


@router.get(
    "/api/market/flood-zone",
    response_model=FloodZoneResult,
    summary="FEMA flood zone lookup by lat/lng",
)
async def get_flood_zone(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lng: float = Query(..., ge=-180.0, le=180.0),
    ctx: TenantContext = Depends(require_context),
) -> FloodZoneResult:
    """Return the FEMA flood zone designation for the given coordinate.

    The lookup queries the ``fema_flood_zones`` spatial table (PostGIS).  Data
    is sourced from the FEMA National Flood Hazard Layer (NFHL).

    Flood insurance is required by federally-backed lenders for parcels in
    Special Flood Hazard Areas (SFHA): zones A, AE, AH, AO, AR, A99, V, VE.
    """
    row = await _fetchrow(
        ctx,
        """
        SELECT fz.*, c.community_name, c.community_number
        FROM fema_flood_zones fz
        LEFT JOIN fema_communities c ON c.community_number = fz.community_number
        WHERE ST_Contains(
            fz.geom,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)
        )
        ORDER BY fz.firm_date DESC
        LIMIT 1
        """,
        lng,  # PostGIS: X=longitude, Y=latitude
        lat,
    )

    if not row:
        # Coordinate not found in NFHL — return unknown zone X (outside SFHA).
        return FloodZoneResult(
            latitude=lat,
            longitude=lng,
            fema_zone="X",
            zone_description="Area of minimal flood hazard (outside SFHA). NFHL data not available for this location.",
            flood_insurance_required=False,
        )

    zone = row.get("flood_zone", "X")
    sfha_zones = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
    insurance_required = any(zone.startswith(z) for z in sfha_zones)

    zone_descriptions: dict[str, str] = {
        "AE": "Special Flood Hazard Area — 1% annual chance flood (detailed study).",
        "AO": "Special Flood Hazard Area — 1% annual chance shallow flooding.",
        "AH": "Special Flood Hazard Area — 1% annual chance shallow flooding (ponds).",
        "AR": "Special Flood Hazard Area — temporarily increased flood risk.",
        "A99": "Special Flood Hazard Area — protected by federal flood control project.",
        "VE": "Coastal Special Flood Hazard Area with wave action (detailed study).",
        "V": "Coastal Special Flood Hazard Area with wave action.",
        "X": "Area of minimal flood hazard (outside 0.2% annual chance floodplain).",
        "0.2PCT": "Moderate flood hazard — 0.2% annual chance (500-year) floodplain.",
    }
    description = zone_descriptions.get(zone, f"Flood zone {zone} — consult FEMA FIRM panel.")

    return FloodZoneResult(
        latitude=lat,
        longitude=lng,
        fema_zone=zone,
        zone_description=description,
        flood_insurance_required=insurance_required,
        firm_panel=row.get("firm_panel"),
        firm_date=row.get("firm_date"),
        community_name=row.get("community_name"),
        community_number=row.get("community_number"),
    )


@router.get(
    "/api/market/schools",
    response_model=SchoolsResponse,
    summary="Nearby school districts within a radius",
)
async def get_nearby_schools(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lng: float = Query(..., ge=-180.0, le=180.0),
    radius: float = Query(default=5.0, ge=0.1, le=50.0, description="Radius in miles"),
    ctx: TenantContext = Depends(require_context),
) -> SchoolsResponse:
    """Return school districts whose centroid falls within ``radius`` miles of
    the given coordinate.  Results are ordered nearest-first.
    """
    meters = radius * 1609.34
    rows = await _fetch(
        ctx,
        """
        SELECT sd.*,
               earth_distance(
                   ll_to_earth(sd.centroid_lat, sd.centroid_lng),
                   ll_to_earth($1, $2)
               ) / 1609.34 AS distance_miles
        FROM school_districts sd
        WHERE earth_distance(
            ll_to_earth(sd.centroid_lat, sd.centroid_lng),
            ll_to_earth($1, $2)
        ) <= $3
        ORDER BY distance_miles ASC
        LIMIT 20
        """,
        lat,
        lng,
        meters,
    )
    districts = [
        SchoolDistrict(
            district_id=str(r.get("id", uuid.uuid4())),
            district_name=r["district_name"],
            district_type=r.get("district_type", "unified"),
            state_code=r.get("state_code", ""),
            county=r.get("county", ""),
            nces_id=r.get("nces_id"),
            rating=_num(r.get("rating")),
            enrollment=r.get("enrollment"),
            student_teacher_ratio=_num(r.get("student_teacher_ratio")),
            distance_miles=_num(r.get("distance_miles")),
            website=r.get("website"),
        )
        for r in rows
    ]
    return SchoolsResponse(latitude=lat, longitude=lng, radius_miles=radius, districts=districts)


@router.get(
    "/api/market/zoning",
    response_model=ZoningResult,
    summary="Zoning classification for a parcel",
)
async def get_zoning(
    parcel_id: str = Query(..., description="Assessor parcel number (APN)"),
    ctx: TenantContext = Depends(require_context),
) -> ZoningResult:
    """Return the zoning classification and development standards for the
    specified parcel.  Data is sourced from county assessor feeds and
    municipal GIS layers ingested by the harvester pipeline.
    """
    if not parcel_id or len(parcel_id) > 64:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="parcel_id must be 1–64 characters.",
        )
    row = await _fetchrow(
        ctx,
        "SELECT * FROM parcel_zoning WHERE parcel_id = $1",
        parcel_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Zoning data not found for parcel {parcel_id!r}.",
        )
    return ZoningResult(
        parcel_id=parcel_id,
        zone_code=row["zone_code"],
        zone_description=row.get("zone_description", ""),
        zone_category=row.get("zone_category", "residential"),
        overlays=row.get("overlays") or [],
        permitted_uses=row.get("permitted_uses") or [],
        conditional_uses=row.get("conditional_uses") or [],
        max_height_ft=_num(row.get("max_height_ft")),
        max_density_units_per_acre=_num(row.get("max_density_units_per_acre")),
        min_lot_sqft=row.get("min_lot_sqft"),
        setback_front_ft=_num(row.get("setback_front_ft")),
        setback_rear_ft=_num(row.get("setback_rear_ft")),
        setback_side_ft=_num(row.get("setback_side_ft")),
        jurisdiction=row.get("jurisdiction"),
    )


# ===========================================================================
# 5. Compliance Engine API
# ===========================================================================

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
