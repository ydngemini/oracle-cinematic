"""State regulatory reference: profiles, disclosure forms, contracts, advertising, licensing requirements, reciprocity."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, Role, require_context, require_role

# Authoritative attorney-at-closing list — single source of truth shared with
# the compliance engine so the public state-profile API and ComplianceEngine
# never disagree about whether a state requires an attorney at closing.
from compliance_engine.closing import ATTORNEY_CLOSE_STATES
from compliance_engine import ComplianceEngine as SeedComplianceEngine

from ._common import (
    router, logger,
    _STATE_RE, _FIPS_RE, _UUID_RE,
    ALL_STATE_CODES, _ATTORNEY_REVIEW_STATES, _MANDATORY_DISCLOSURE_STATES,
    _TDS_STATES, _FEDERAL_LEAD_PAINT_THRESHOLD_YEAR,
    _CE_HOURS_BY_STATE, _RECIPROCITY_MATRIX,
    _iso, _num, _require_state, _require_uuid, _fetch, _fetchrow,
    _require_dataset_loaded,
)
from .models import (  # noqa: F401  (re-exported for route handlers)
    StateSummary,
    DisclosureForm,
    ContractTemplate,
    StateDocumentLibrary,
    StateDocumentLibraryItem,
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


_EPA_LEAD_DISCLOSURE_URL = (
    "https://www.epa.gov/lead/lead-based-paint-disclosure-rule-section-1018-title-x"
)


@lru_cache(maxsize=1)
def _seed_compliance_catalog() -> SeedComplianceEngine:
    """Load the versioned, cited 50-state rule catalog once per process."""
    return SeedComplianceEngine.from_seed_directory()


def _rule_needs_document_reference(rule: Any) -> bool:
    """Keep only rule entries that identify a form, notice, or document."""
    for action in rule.required_actions:
        action_name = str(action.get("action", "")).lower()
        if action.get("form_id") or action.get("document"):
            return True
        if any(token in action_name for token in ("form", "document", "disclosure", "notice")):
            return True
    return False


def _safe_external_url(value: Any) -> Optional[str]:
    """Only return absolute HTTPS source URLs safe for a client-side link."""
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme == "https" and parsed.netloc else None


def _compliance_reference_items(
    state_code: str,
    *,
    attorney_review_required: bool,
) -> list[StateDocumentLibraryItem]:
    """Expose cited requirements as references, never executable legal forms."""
    items: list[StateDocumentLibraryItem] = []
    for rule in _seed_compliance_catalog().get_rules_for_state(state_code):
        if not _rule_needs_document_reference(rule):
            continue
        form_ids = [
            str(action["form_id"])
            for action in rule.required_actions
            if action.get("form_id")
        ]
        download_url = (
            _EPA_LEAD_DISCLOSURE_URL
            if rule.rule_id == "FEDERAL-LEAD-PAINT-001"
            else None
        )
        items.append(
            StateDocumentLibraryItem(
                item_id=f"compliance:{rule.rule_id}",
                state_code=state_code,
                kind="document",
                title=rule.title,
                subtitle=" · ".join(form_ids) or "Compliance reference",
                source_name=str(rule.metadata.get("federal_agency") or "State compliance reference"),
                source_status="official_reference" if download_url else "citation_reference",
                selection_status="source_linked" if download_url else "review_required",
                version=str(rule.version),
                effective_date=rule.effective_date,
                download_url=download_url,
                citations=list(rule.citations),
                notes=rule.description,
                attorney_review_required=attorney_review_required,
            )
        )
    return items


def _document_type(rule: Any, form_id: str) -> str:
    value = f"{rule.category} {rule.title} {form_id}".lower()
    if "assign" in value:
        return "ASSIGNMENT"
    if "purchase" in value or "contract" in value:
        return "PURCHASE_AGREEMENT"
    if "title" in value or "lien" in value:
        return "TITLE"
    if "condition" in value or "disclos" in value or "lead" in value:
        return "DISCLOSURE"
    return "DOCUMENT"


def _autofill_fields(document_type: str) -> list[str]:
    fields = {
        "ASSIGNMENT": [
            "assignor_name",
            "assignee_name",
            "assignment_fee",
            "property_address",
        ],
        "PURCHASE_AGREEMENT": [
            "buyer_name",
            "seller_name",
            "property_address",
            "purchase_price",
        ],
        "TITLE": ["property_address", "owner_name", "parcel_id"],
        "DISCLOSURE": ["property_address", "seller_name"],
        "DOCUMENT": ["property_address"],
    }
    return fields[document_type]



# Known issuer prefixes, so a derived label reads as a document rather than as a
# database key. Nothing here renames a document — it only expands the acronym the
# form_id already carries.
_FORM_ID_ISSUERS = {
    "EPA": "EPA",
    "HUD": "HUD",
    "CFPB": "CFPB",
    "IRS": "IRS",
    "FHA": "FHA",
    "VA": "VA",
}


def _name_from_form_id(form_id: str) -> str:
    """Turn `CFPB-LOAN-ESTIMATE` into `CFPB Loan Estimate`.

    Used only when a rule requires several documents and the seed author named
    none of them — FEDERAL-RESPA-001 requires both a Loan Estimate and a Closing
    Disclosure, and calling both by the rule's title made them indistinguishable
    on screen. The form_id is an identifier the seed already asserts, so
    expanding it invents no fact; it just stops two different obligations from
    rendering as one repeated row.
    """
    parts = [p for p in form_id.replace("_", "-").split("-") if p]
    if not parts:
        return ""
    words = []
    for part in parts:
        upper = part.upper()
        words.append(_FORM_ID_ISSUERS.get(upper, upper if len(part) <= 3 and part.isupper() else part.capitalize()))
    return " ".join(words)

@router.get(
    "/api/compliance/documents/{state_code}",
    summary="State document pre-list from the cited compliance catalog",
)
async def compliance_document_prelist(
    state_code: str,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Return obligations without presenting unapproved forms as executable."""
    code = _require_state(state_code)
    rules = _seed_compliance_catalog().get_rules_for_state(code)
    _profile, _forms, _contracts, template_rows = await _state_library_rows(ctx, code)
    executable_types = {
        str(row.get("document_type") or "").lower(): row
        for row in template_rows
        if (
            str(row.get("source_status") or "").lower() == "approved"
            and str(row.get("source_ref") or "").lower() == "tenant-managed template"
        )
    }

    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in rules:
        # A rule requiring ONE document should keep its official title —
        # "Seller's Disclosure of Real Property Condition Report" is the name of
        # the form, and degrading it to a form_id would be a regression. Only
        # when a rule bundles several distinct documents does the title stop
        # identifying any one of them, and only then is a derived label better.
        contributing = sum(
            1
            for a in rule.required_actions
            if str(a.get("form_id") or "").strip() or str(a.get("document") or "").strip()
        )
        for action in rule.required_actions:
            form_id = str(action.get("form_id") or "").strip()
            document_name = str(action.get("document") or "").strip()
            if not form_id and not document_name:
                continue
            doc_id = form_id or f"{code}-{rule.rule_id}-DOCUMENT"
            if doc_id in seen:
                continue
            seen.add(doc_id)
            document_type = _document_type(rule, doc_id)
            # One rule can require several genuinely different documents —
            # FEDERAL-RESPA-001 requires BOTH a Loan Estimate and a Closing
            # Disclosure; FEDERAL-LEAD-PAINT-001 requires the disclosure AND the
            # EPA pamphlet. Naming every one of them `rule.title` rendered them
            # as identical rows, so an agent could tick one believing the other
            # was covered. The action's own name wins when the seed author gave
            # one; otherwise the form_id is carried alongside so two rows under
            # the same obligation are still distinguishable. Nothing is invented:
            # if neither exists the title stands as before.
            documents.append(
                {
                    "doc_id": doc_id,
                    "type": document_type,
                    "name": (
                        document_name
                        or (_name_from_form_id(form_id) if contributing > 1 else "")
                        or rule.title
                    ),
                    "form_id": form_id or None,
                    "obligation": rule.title,
                    "mandatory": str(rule.severity).lower() == "required",
                    "ai_autofill_fields": _autofill_fields(document_type),
                    "generation_available": False,
                    "template_key": None,
                    "status": "NOT_STARTED",
                    "citations": list(rule.citations),
                    "rule_id": rule.rule_id,
                    "version": rule.version,
                    "professional_review_required": True,
                }
            )

    template_names = {
        "assignment": ("ASSIGNMENT", "Wholesale Assignment Agreement"),
        "seller_purchase": ("PURCHASE_AGREEMENT", "Seller Purchase Agreement"),
        "buyer_purchase": ("PURCHASE_AGREEMENT", "Buyer Purchase Agreement"),
    }
    for template_type, (document_type, title) in template_names.items():
        row = executable_types.get(template_type)
        if row is None:
            continue
        doc_id = str(row.get("template_key") or row.get("id"))
        if doc_id in seen:
            continue
        documents.append(
            {
                "doc_id": doc_id,
                "type": document_type,
                "name": f"{code} {title}",
                "mandatory": False,
                "ai_autofill_fields": _autofill_fields(document_type),
                "generation_available": True,
                "template_key": row.get("template_key"),
                "status": "AUTOFILL_READY",
                "citations": [],
                "rule_id": None,
                "version": row.get("version"),
                "professional_review_required": True,
            }
        )
    return {
        "state": code,
        "required_documents": documents,
        "source": "versioned_state_seed_rules",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _state_library_rows(ctx: TenantContext, state_code: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read public state entries plus tenant-scoped source-controlled templates."""
    try:
        async with tenant_tx(ctx) as conn:
            profile = await conn.fetchrow(
                """
                SELECT state_name, regulatory_url, attorney_review_required
                FROM state_regulatory_profiles
                WHERE state_code = $1
                """,
                state_code,
            )
            forms = await conn.fetch(
                """
                SELECT id, form_name, form_type, required_when, effective_date, download_url, notes
                FROM state_disclosure_forms
                WHERE state_code = $1
                ORDER BY form_type, form_name
                """,
                state_code,
            )
            contracts = await conn.fetch(
                """
                SELECT id, template_name, association, property_types, version, effective_date, download_url
                FROM state_contract_templates
                WHERE state_code = $1
                ORDER BY template_name
                """,
                state_code,
            )
            templates = await conn.fetch(
                """
                WITH registered_sources AS (
                    SELECT
                        source.id,
                        source.template_key,
                        source.document_type,
                        source.jurisdiction,
                        source.version,
                        source.source_status,
                        source.source_ref
                    FROM tenant_contract_template_registrations AS registration
                    JOIN contract_template_sources AS source ON source.id = registration.source_id
                    WHERE registration.tenant_id = $1::uuid
                      AND registration.status = 'registered'
                      AND source.jurisdiction IN ($2, 'US-GENERIC')
                ),
                tenant_only AS (
                    SELECT
                        template.id,
                        template.template_key,
                        template.document_type,
                        template.jurisdiction,
                        template.version,
                        template.status AS source_status,
                        'tenant-managed template' AS source_ref
                    FROM contract_templates AS template
                    WHERE template.tenant_id = $1::uuid
                      AND template.jurisdiction IN ($2, 'US-GENERIC')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM tenant_contract_template_registrations AS registration
                          JOIN contract_template_sources AS source ON source.id = registration.source_id
                          WHERE registration.tenant_id = template.tenant_id
                            AND source.template_key = template.template_key
                            AND source.version = template.version
                      )
                )
                SELECT * FROM registered_sources
                UNION ALL
                SELECT * FROM tenant_only
                ORDER BY template_key, version
                """,
                ctx.tenant_id,
                state_code,
            )
    except Exception as exc:  # Catalog references remain useful if optional DB tables are offline.
        logger.warning("State document library source lookup failed for %s: %s", state_code, exc)
        return {}, [], [], []

    return (
        dict(profile) if profile else {},
        [dict(row) for row in forms],
        [dict(row) for row in contracts],
        [dict(row) for row in templates],
    )


@router.get(
    "/api/states/{state_code}/document-library",
    response_model=StateDocumentLibrary,
    summary="Selectable contract and document references for a state",
)
async def get_state_document_library(
    state_code: str,
    ctx: TenantContext = Depends(require_context),
) -> StateDocumentLibrary:
    """Combine licensed sources, tenant templates, and cited state references.

    This is deliberately a library of source metadata. It never returns an
    association form body and never upgrades source control into legal approval.
    """
    code = _require_state(state_code)
    profile, form_rows, contract_rows, template_rows = await _state_library_rows(ctx, code)
    attorney_review_required = bool(
        profile.get("attorney_review_required", code in _ATTORNEY_REVIEW_STATES)
    )
    items: list[StateDocumentLibraryItem] = []

    for row in template_rows:
        source_status = str(row.get("source_status") or "tenant_managed")
        items.append(
            StateDocumentLibraryItem(
                item_id=f"tenant-template:{row['id']}",
                state_code=code,
                kind="contract",
                title=str(row["template_key"]),
                subtitle=f"{row.get('document_type', 'contract')} · {row.get('jurisdiction', code)}",
                source_name="Tenant source control",
                source_status=source_status,
                selection_status="approved_source" if source_status == "approved" else "review_required",
                version=str(row.get("version") or ""),
                notes=str(row.get("source_ref") or "Source-controlled template."),
                attorney_review_required=True,
            )
        )

    for row in contract_rows:
        download_url = _safe_external_url(row.get("download_url"))
        items.append(
            StateDocumentLibraryItem(
                item_id=f"state-contract:{row['id']}",
                state_code=code,
                kind="contract",
                title=str(row["template_name"]),
                subtitle=" · ".join(row.get("property_types") or []) or "State contract reference",
                source_name=str(row.get("association") or "State association"),
                source_status="source_linked" if download_url else "reference_only",
                selection_status="source_linked" if download_url else "review_required",
                version=str(row.get("version") or "") or None,
                effective_date=row.get("effective_date"),
                download_url=download_url,
                attorney_review_required=True,
            )
        )

    for row in form_rows:
        download_url = _safe_external_url(row.get("download_url"))
        items.append(
            StateDocumentLibraryItem(
                item_id=f"state-form:{row['id']}",
                state_code=code,
                kind="document",
                title=str(row["form_name"]),
                subtitle=str(row.get("form_type") or "State document"),
                source_name="State regulatory source",
                source_status="source_linked" if download_url else "reference_only",
                selection_status="source_linked" if download_url else "review_required",
                effective_date=row.get("effective_date"),
                download_url=download_url,
                notes=row.get("required_when") or row.get("notes"),
                attorney_review_required=attorney_review_required,
            )
        )

    known_item_ids = {item.item_id for item in items}
    items.extend(
        item
        for item in _compliance_reference_items(
            code,
            attorney_review_required=attorney_review_required,
        )
        if item.item_id not in known_item_ids
    )
    items.sort(key=lambda item: (item.kind != "contract", item.title.casefold()))

    return StateDocumentLibrary(
        state_code=code,
        state_name=str(profile.get("state_name") or code),
        regulatory_url=_safe_external_url(profile.get("regulatory_url")),
        attorney_review_required=attorney_review_required,
        items=items,
        total_contracts=sum(item.kind == "contract" for item in items),
        total_documents=sum(item.kind == "document" for item in items),
        source_note=(
            "Select a source-controlled template or cited reference. Association forms "
            "must be obtained through an authorized source; encrypted draft work stays in Personal AI."
        ),
    )

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
            # No `, True` fallbacks: an absent or NULL agency column means the
            # question was never researched for this state, and coalescing it
            # to "permitted" is a licensing claim the data does not support.
            # See migration 0069.
            dual_agency_permitted=row.get("dual_agency_permitted"),
            designated_agency_permitted=row.get("designated_agency_permitted"),
            sub_agency_permitted=row.get("sub_agency_permitted"),
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
    if not rows:
        # Every US state mandates *some* disclosure paperwork, so an empty list
        # here is never a true statement about the state — it means the table
        # was not seeded.
        await _require_dataset_loaded(ctx, "state_disclosure_forms")
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
    if not rows:
        await _require_dataset_loaded(ctx, "state_contract_templates")
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
    if not rows:
        # Advertising rules are a compliance surface — silently returning "no
        # rules apply" is the most dangerous empty list in this module.
        await _require_dataset_loaded(ctx, "state_advertising_rules")
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

    # Fallback to module-level constant matrix. A pair the constant does not
    # cover is "unknown", never "none" — reporting "no reciprocity" for a pair
    # nobody researched would tell an agent they cannot practise somewhere on
    # the strength of missing data.
    rec_class = _RECIPROCITY_MATRIX.get((fc, tc), "unknown")
    notes = (
        "Reciprocity data not yet seeded for this pair — defaulting to module constant. "
        "Verify with the destination state's real estate commission."
        if rec_class != "unknown"
        else (
            "No reciprocity data is held for this pair. This is not a finding that "
            "reciprocity is unavailable — confirm with the destination state's real "
            "estate commission."
        )
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
