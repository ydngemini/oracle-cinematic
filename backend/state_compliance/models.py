"""Pydantic request/response models for the state_compliance package."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, Role, require_context, require_role

# Authoritative attorney-at-closing list — single source of truth shared with
# the compliance engine so the public state-profile API and ComplianceEngine
# never disagree about whether a state requires an attorney at closing.
from compliance_engine.closing import ATTORNEY_CLOSE_STATES

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


class StateDocumentLibraryItem(BaseModel):
    """A selectable state-specific contract or document reference.

    The item intentionally carries metadata and source provenance only. It is
    not an assertion that the platform may reproduce an association form.
    """

    item_id: str
    state_code: str
    kind: Literal["contract", "document"]
    title: str
    subtitle: str = ""
    source_name: str
    source_status: str
    selection_status: str
    version: Optional[str] = None
    effective_date: Optional[date] = None
    download_url: Optional[str] = None
    citations: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    attorney_review_required: bool = True


class StateDocumentLibrary(BaseModel):
    """State selector payload for the contract and document library."""

    state_code: str
    state_name: str
    regulatory_url: Optional[str] = None
    attorney_review_required: bool
    items: list[StateDocumentLibraryItem]
    total_contracts: int
    total_documents: int
    source_note: str


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
