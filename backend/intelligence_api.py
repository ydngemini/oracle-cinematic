"""Source-backed property and market intelligence API."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from db.connection import tenant_tx
from graph_engine import PropertyGraph
from intelligence_engine import (
    DISTRESS_MODEL_VERSION,
    DISTRESS_SIGNAL_WEIGHTS,
    FORECAST_MODEL_VERSION,
    HBU_MODEL_VERSION,
    TITLE_MODEL_VERSION,
    UNDERWRITING_MODEL_VERSION,
    IntelligenceInputError,
    analyze_highest_best_use,
    calculate_underwriting,
    detect_public_sourcing_signals,
    forecast_micro_market,
    preliminary_title_summary,
    score_pre_distress,
)
from platform_policy import (
    EvidenceStatus,
    Feature,
    IntelligenceEnvelope,
    PUBLIC_PROPERTY_DATA_POLICY,
    SourceCitation,
    UnderwritingTrace,
    enforce_public_property_data,
    latest_observation,
    require_feature,
)
from tenancy import TenantContext, require_context

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


class EvidenceInput(SourceCitation):
    source_record_id: UUID


class CitableSource(BaseModel):
    """One immutable source record, offered to a caller as citable evidence.

    `cite` is exactly an EvidenceInput and nothing more, because SourceCitation
    sets `extra="forbid"` — a caller that spread the metadata below into a POST
    body would be rejected. Keeping the citable subset in its own object means
    the client passes `row.cite` straight through instead of maintaining a
    field-stripping rule that drifts the moment either model changes.

    The metadata beside it exists so a person can tell WHICH record to cite.
    `payload_purged` matters most: retention wipes `raw_payload` but keeps the
    hash and the provenance, so a purged record still proves an observation
    happened and remains legitimately citable — it just cannot be re-read.
    """

    model_config = ConfigDict(extra="forbid")

    cite: EvidenceInput
    source_key: str
    property_key: Optional[str] = None
    jurisdiction: Optional[str] = None
    property_level_allowed: bool
    outreach_use_allowed: bool
    payload_purged: bool
    expires_at: Optional[datetime] = None


class AnalysisBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_key: str = Field(min_length=1, max_length=240)
    sources: list[EvidenceInput] = Field(min_length=1, max_length=100)


class DistressAnalysis(AnalysisBase):
    signals: dict[str, float]
    calibration: dict[str, Any] = Field(default_factory=dict)


class HBUAnalysis(AnalysisBase):
    zoning_district: str = Field(min_length=1, max_length=80)
    effective_version: str = Field(min_length=1, max_length=120)
    lot_area_sqft: float = Field(gt=0)
    building_area_sqft: float = Field(ge=0)
    max_far: float = Field(ge=0)
    max_lot_coverage: Optional[float] = Field(default=None, ge=0, le=1)
    allowed_uses: list[str] = Field(default_factory=list, max_length=100)
    dimensional_limits: dict[str, Any] = Field(default_factory=dict)
    land_comparables: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class UnderwritingAnalysis(AnalysisBase):
    subject_sqft: float = Field(gt=0)
    comparables: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rehab_items: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    acquisition_ratio: float = Field(default=0.70, gt=0, le=1)
    explicit_arv: Optional[float] = Field(default=None, gt=0)


class TitleFindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_record_id: UUID
    kind: str = Field(min_length=1, max_length=80)
    record_id: Optional[str] = Field(default=None, max_length=240)
    amount: Optional[float] = Field(default=None, ge=0)
    recorded_at: Optional[date] = None
    released_at: Optional[date] = None
    match_status: str = Field(pattern=r"^(matched|possible_match|unresolved|released)$")
    chain_gap: bool = False
    notes: Optional[str] = Field(default=None, max_length=2_000)


class TitleAnalysis(AnalysisBase):
    findings: list[TitleFindingInput] = Field(max_length=500)


class ForecastAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_key: str = Field(min_length=1, max_length=240)
    sources: list[EvidenceInput] = Field(min_length=1, max_length=100)
    observations: list[dict[str, Any]] = Field(min_length=3, max_length=100)
    horizon_years: int = Field(default=5, ge=1, le=5)


class DetectorAnalysis(AnalysisBase):
    signals: dict[str, Any]


class EntityGraphAnalysis(AnalysisBase):
    record: dict[str, Any]


def _source_ids(sources: list[EvidenceInput]) -> list[str]:
    return [str(source.source_record_id) for source in sources]


async def _verified_citations(
    ctx: TenantContext,
    source_ids: list[str],
    *,
    property_level: bool = True,
) -> list[SourceCitation]:
    """Resolve provenance from tenant-visible immutable records, never claims.

    `property_level` gates `source_licenses.property_level_allowed`, which the
    harvesters have always written and nothing has ever read. The column exists
    to answer "may this data be attached to an individual property record?", so
    an analysis whose subject IS a property must not cite a source whose licence
    says no. Every harvester in the repo leaves the default True today, so this
    changes no current behaviour — it closes the gap before the first licensed
    feed arrives, which is the only moment the check is cheap.

    Pass False for market-subject analyses (the forecast), where the licence
    question does not arise because no individual property is being described.
    """
    unique_ids = list(dict.fromkeys(source_ids))
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT r.id,r.record_key,r.observed_at,r.retrieved_at,
                   l.source_name,l.source_url,l.license_name,
                   l.property_level_allowed
              FROM source_records r
              JOIN source_licenses l ON l.id=r.source_license_id
             WHERE r.id=ANY($1::uuid[])
            """,
            unique_ids,
        )
    by_id = {str(row["id"]): row for row in rows}
    missing = [source_id for source_id in unique_ids if source_id not in by_id]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"code": "SOURCE_RECORD_NOT_VISIBLE", "source_record_ids": missing},
        )
    if property_level:
        forbidden = [
            source_id
            for source_id in unique_ids
            if not by_id[source_id]["property_level_allowed"]
        ]
        if forbidden:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SOURCE_LICENSE_FORBIDS_PROPERTY_USE",
                    "source_record_ids": forbidden,
                },
            )
    return [
        SourceCitation(
            source=by_id[source_id]["source_name"],
            record_id=by_id[source_id]["record_key"],
            source_url=by_id[source_id]["source_url"],
            observed_at=by_id[source_id]["observed_at"].date(),
            retrieved_at=by_id[source_id]["retrieved_at"],
            license=by_id[source_id]["license_name"],
            evidence_status=EvidenceStatus.OBSERVED,
        )
        for source_id in unique_ids
    ]


async def _outreach_blocked_sources(
    ctx: TenantContext,
    source_ids: list[str],
) -> list[str]:
    """Names of cited sources whose licence forbids using the data for contact.

    A second read of rows `_verified_citations()` has already fetched. That is
    deliberate: threading the flag through would change four call sites to carry
    a value only this endpoint uses, and detectors is a low-frequency analysis
    route that runs model inference anyway. The cost is one indexed lookup.
    """
    unique_ids = list(dict.fromkeys(source_ids))
    if not unique_ids:
        return []
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT l.source_name
              FROM source_records r
              JOIN source_licenses l ON l.id = r.source_license_id
             WHERE r.id = ANY($1::uuid[])
               AND l.outreach_use_allowed IS NOT TRUE
            """,
            unique_ids,
        )
    return sorted(row["source_name"] for row in rows)


async def _persist(
    ctx: TenantContext,
    envelope: IntelligenceEnvelope,
    source_ids: list[str],
    *,
    professional_review_status: str,
) -> str:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO intelligence_scores (
                tenant_id, property_key, analysis_type, evidence_status,
                observation_date, confidence, model_version, source_record_ids,
                result, trace, professional_review_status
            ) VALUES (
                $1::uuid,$2,$3,$4,$5,$6,$7,$8::uuid[],$9::jsonb,$10::jsonb,$11
            ) RETURNING id
            """,
            ctx.tenant_id,
            envelope.subject_id,
            envelope.analysis_type,
            envelope.evidence_status.value,
            envelope.observation_date,
            envelope.confidence,
            envelope.model_version,
            source_ids,
            json.dumps(envelope.result, default=str),
            json.dumps(envelope.trace.model_dump(), default=str) if envelope.trace else None,
            professional_review_status,
        )
    return str(row["id"])


async def _respond(
    ctx: TenantContext,
    *,
    analysis_type: str,
    subject_id: str,
    model_version: str,
    source_ids: list[str],
    result: dict[str, Any],
    confidence: float,
    warnings: list[str],
    trace: Optional[UnderwritingTrace] = None,
    review_required: bool = False,
    evidence_status: EvidenceStatus = EvidenceStatus.INFERRED,
    property_level: bool = True,
) -> dict[str, Any]:
    # Caller-supplied citation labels are display hints only. Immutable source
    # records are authoritative, which prevents fabricated observation dates or
    # licenses from entering an intelligence response. This used to take the
    # caller's citations as an argument and overwrite them on the next line,
    # which read as though they mattered; they never did.
    citations = await _verified_citations(ctx, source_ids, property_level=property_level)
    envelope = IntelligenceEnvelope(
        analysis_type=analysis_type,
        subject_id=subject_id,
        evidence_status=evidence_status,
        observation_date=latest_observation(citations),
        confidence=max(0.0, min(1.0, confidence)),
        model_version=model_version,
        sources=citations,
        result=result,
        warnings=warnings,
        trace=trace,
    )
    intelligence_id = await _persist(
        ctx,
        envelope,
        source_ids,
        professional_review_status="required" if review_required else "not_required",
    )
    return {"id": intelligence_id, **envelope.model_dump(mode="json")}


@router.get("/policy")
async def intelligence_policy(ctx: TenantContext = Depends(require_context)):
    """Policy, plus the vocabularies a caller must speak to author an analysis.

    `score_pre_distress()` raises on any signal name it does not recognise, and
    the weights decide what a score means. A client that hardcoded either would
    drift out of step with the engine the first time a signal is added — the
    same failure the intake questions avoid by being served rather than copied.
    So the names and weights come from the engine itself.
    """
    return {
        **PUBLIC_PROPERTY_DATA_POLICY,
        "distress_signals": [
            {"signal": name, "weight": weight}
            for name, weight in DISTRESS_SIGNAL_WEIGHTS.items()
        ],
        "distress_model_version": DISTRESS_MODEL_VERSION,
    }


# Declared before GET /{property_key}, which would otherwise match "sources"
# and try to read intelligence for a property of that name.
@router.get("/sources")
async def citable_sources(
    property_key: Optional[str] = Query(default=None, max_length=240),
    jurisdiction: Optional[str] = Query(default=None, max_length=80),
    source_key: Optional[str] = Query(default=None, max_length=240),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    """List the immutable source records this tenant may cite.

    Every POST on this router requires at least one `source_record_id` that
    `_verified_citations()` can resolve, and until now nothing listed that
    table. The practical consequence was not a missing convenience: authoring
    any intelligence at all was impossible through the product, because a person
    had no way to discover the UUIDs an analysis must cite. Thirteen routes were
    unreachable behind one absent SELECT.

    At least one of `property_key` or `jurisdiction` is required. Both are
    indexed; an unfiltered listing would be a sequential scan of every raw
    record this tenant has ever retained, which is the shape of query that has
    already cost this codebase a 13.5s response once.
    """
    if not property_key and not jurisdiction:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "FILTER_REQUIRED",
                "message": "Pass property_key or jurisdiction. Listing every retained "
                           "record for a tenant is a scan, not a query.",
            },
        )
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT r.id, r.source_key, r.record_key, r.property_key, r.jurisdiction,
                   r.observed_at, r.retrieved_at, r.expires_at, r.purged_at,
                   l.source_name, l.source_url, l.license_name,
                   l.property_level_allowed, l.outreach_use_allowed
              FROM source_records r
              JOIN source_licenses l ON l.id = r.source_license_id
             WHERE ($1::text IS NULL OR r.property_key = $1)
               AND ($2::text IS NULL OR r.jurisdiction = $2)
               AND ($3::text IS NULL OR r.source_key = $3)
             ORDER BY r.observed_at DESC, r.created_at DESC
             LIMIT $4
            """,
            property_key,
            jurisdiction,
            source_key,
            limit,
        )
    sources = [
        CitableSource(
            cite=EvidenceInput(
                source_record_id=row["id"],
                source=row["source_name"],
                record_id=row["record_key"],
                source_url=row["source_url"],
                observed_at=row["observed_at"].date(),
                retrieved_at=row["retrieved_at"],
                license=row["license_name"],
                evidence_status=EvidenceStatus.OBSERVED,
            ),
            source_key=row["source_key"],
            property_key=row["property_key"],
            jurisdiction=row["jurisdiction"],
            property_level_allowed=row["property_level_allowed"],
            outreach_use_allowed=row["outreach_use_allowed"],
            payload_purged=row["purged_at"] is not None,
            expires_at=row["expires_at"],
        )
        for row in rows
    ]
    return {
        "property_key": property_key,
        "jurisdiction": jurisdiction,
        "limit": limit,
        "count": len(sources),
        # Distinguishing "this property has no retained observations" from
        # "the harvesters have never run" is the difference between a screen
        # that tells you to go get data and one that looks broken.
        "citable": [source.model_dump(mode="json") for source in sources],
    }


@router.post("/pre-distress")
async def pre_distress(
    body: DistressAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    try:
        result = score_pre_distress(body.signals, calibration=body.calibration)
    except IntelligenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    confidence = result["signal_coverage"] * (0.9 if result["is_probability_validated"] else 0.65)
    return await _respond(
        ctx,
        analysis_type="pre_distress",
        subject_id=body.property_key,
        model_version=DISTRESS_MODEL_VERSION,
        source_ids=_source_ids(body.sources),
        result=result,
        confidence=confidence,
        warnings=list(result["warnings"]),
    )


@router.post("/highest-best-use")
async def highest_best_use(
    body: HBUAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    try:
        result = analyze_highest_best_use(
            lot_area_sqft=body.lot_area_sqft,
            building_area_sqft=body.building_area_sqft,
            max_far=body.max_far,
            max_lot_coverage=body.max_lot_coverage,
            allowed_uses=body.allowed_uses,
            dimensional_limits=body.dimensional_limits,
            land_comparables=body.land_comparables,
        )
    except IntelligenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    confidence = 0.82 if body.allowed_uses and body.land_comparables else 0.62
    source_ids = _source_ids(body.sources)
    citations = await _verified_citations(ctx, source_ids)
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            INSERT INTO zoning_analyses (
                tenant_id,property_key,source_record_ids,zoning_district,
                effective_version,lot_area_sqft,building_area_sqft,current_far,
                max_far,remaining_buildable_sqft,lot_coverage,permitted_uses,
                dimensional_limits,comparable_land_sales,result,model_version
            ) VALUES (
                $1::uuid,$2,$3::uuid[],$4,$5,$6,$7,$8,$9,$10,$11,
                $12::text[],$13::jsonb,$14::jsonb,$15::jsonb,$16
            )
            """,
            ctx.tenant_id,
            body.property_key,
            source_ids,
            body.zoning_district,
            body.effective_version,
            body.lot_area_sqft,
            body.building_area_sqft,
            result["current_far"],
            body.max_far,
            result["remaining_buildable_area_sqft"],
            body.max_lot_coverage,
            result["allowed_uses"],
            json.dumps(body.dimensional_limits, default=str),
            json.dumps(result["land_comparables"], default=str),
            json.dumps(result, default=str),
            HBU_MODEL_VERSION,
        )
    return await _respond(
        ctx,
        analysis_type="highest_best_use",
        subject_id=body.property_key,
        model_version=HBU_MODEL_VERSION,
        citations=citations,
        source_ids=source_ids,
        result=result,
        confidence=confidence,
        warnings=list(result["warnings"]),
        review_required=True,
    )


@router.post("/underwriting")
async def underwriting(
    body: UnderwritingAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    try:
        result = calculate_underwriting(
            subject_sqft=body.subject_sqft,
            comparables=body.comparables,
            rehab_items=body.rehab_items,
            acquisition_ratio=body.acquisition_ratio,
            explicit_arv=body.explicit_arv,
        )
    except IntelligenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    trace = UnderwritingTrace.model_validate(result.pop("trace"))
    return await _respond(
        ctx,
        analysis_type="underwriting",
        subject_id=body.property_key,
        model_version=UNDERWRITING_MODEL_VERSION,
        source_ids=_source_ids(body.sources),
        result=result,
        confidence=0.85 if len(body.comparables) >= 3 and body.rehab_items else 0.62,
        warnings=list(trace.risks),
        trace=trace,
        review_required=True,
    )


@router.post("/title")
async def title_intelligence(
    body: TitleAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    try:
        finding_payloads = [finding.model_dump(mode="json") for finding in body.findings]
        result = preliminary_title_summary(finding_payloads)
    except IntelligenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    unresolved = result["unresolved_matches"] + result["chain_gaps"]
    confidence = max(0.35, 0.9 - min(0.5, unresolved * 0.08))
    source_ids = _source_ids(body.sources)
    await _verified_citations(ctx, source_ids)
    allowed_source_ids = set(source_ids)
    unknown = sorted(
        {str(finding.source_record_id) for finding in body.findings} - allowed_source_ids
    )
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"code": "FINDING_SOURCE_NOT_CITED", "source_record_ids": unknown},
        )
    async with tenant_tx(ctx) as conn:
        for finding in body.findings:
            await conn.execute(
                """
                INSERT INTO title_findings (
                    tenant_id,property_key,finding_type,record_id,amount,
                    recorded_at,released_at,match_status,chain_gap,
                    source_record_id,notes
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10::uuid,$11)
                """,
                ctx.tenant_id,
                body.property_key,
                finding.kind,
                finding.record_id,
                finding.amount,
                finding.recorded_at,
                finding.released_at,
                finding.match_status,
                finding.chain_gap,
                str(finding.source_record_id),
                finding.notes,
            )
    return await _respond(
        ctx,
        analysis_type="preliminary_title",
        subject_id=body.property_key,
        model_version=TITLE_MODEL_VERSION,
        source_ids=source_ids,
        result=result,
        confidence=confidence,
        warnings=list(result["warnings"]),
        review_required=True,
    )


@router.post("/forecast")
async def market_forecast(
    body: ForecastAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    try:
        result = forecast_micro_market(body.observations, horizon_years=body.horizon_years)
    except IntelligenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _respond(
        ctx,
        analysis_type="micro_market_forecast",
        subject_id=body.market_key,
        model_version=FORECAST_MODEL_VERSION,
        source_ids=_source_ids(body.sources),
        # Subject is a market, not a property, so property_level_allowed does
        # not apply — no individual property is being described.
        property_level=False,
        result=result,
        confidence=max(
            0.35,
            min(
                0.88,
                0.40
                + 0.35 * float(result["source_coverage"]["coverage"])
                + 0.02 * len(result["historical_years"]),
            ),
        ),
        warnings=list(result["warnings"]),
        review_required=True,
    )


@router.post("/detectors")
async def sourcing_detectors(
    body: DetectorAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    # source_licenses.outreach_use_allowed answers "may this data be used to
    # contact the owner?" and every harvester leaves it False by default,
    # because open data being public does not make it lawful outreach material.
    # It has been written since 0027 and read by nothing.
    #
    # This endpoint is where public-record evidence becomes a list of people to
    # approach, so it is where the answer belongs. It REPORTS rather than
    # refuses: given the default, refusing would disable detectors outright, and
    # producing the candidate list is legitimate — acting on it is the step that
    # needs the licence. The approver simply could not see this before.
    blocked = await _outreach_blocked_sources(ctx, _source_ids(body.sources))
    result = {
        "candidates": detect_public_sourcing_signals(body.signals),
        "outreach_requires_approval": True,
        "identity_verification_required": True,
        "private_contacts_inferred": False,
        "outreach_licence_permits_contact": not blocked,
        "outreach_licence_blocked_sources": blocked,
    }
    return await _respond(
        ctx,
        analysis_type="public_sourcing_detectors",
        subject_id=body.property_key,
        model_version="public-source-detectors-2026.07",
        source_ids=_source_ids(body.sources),
        result=result,
        confidence=0.7,
        warnings=(
            ["Candidates require source-record review and identity verification before outreach."]
            + (
                [
                    "The licence on " + ", ".join(blocked) + " does not permit using this "
                    "data to contact an owner. Approving outreach from these candidates "
                    "needs a separate lawful basis."
                ]
                if blocked
                else []
            )
        ),
    )


@router.post("/entity-graph")
async def entity_graph(
    body: EntityGraphAnalysis,
    ctx: TenantContext = Depends(require_context),
):
    """Join public-record entities while preserving stated relationship roles."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    source_ids = _source_ids(body.sources)
    citations = await _verified_citations(ctx, source_ids)
    record = dict(body.record)
    record.setdefault("parcel_id", body.property_key)
    record.setdefault("record_id", citations[0].record_id)
    record.setdefault("source", citations[0].source)
    record.setdefault("observed_at", citations[0].observed_at.isoformat())
    graph = PropertyGraph()
    try:
        await graph.ingest_public_record(record)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    exported = graph.export()
    type_map = {
        "Property": "property",
        "PersonOfRecord": "person_of_record",
        "AcquisitionEntity": "acquisition_entity",
        "Address": "address",
        "Officer": "officer",
        "Deed": "deed",
        "PublicFiling": "public_filing",
    }
    primary_source_id = source_ids[0]
    observed_date = max(citation.observed_at for citation in citations)
    observed_at = datetime.combine(observed_date, time.min, tzinfo=timezone.utc)
    db_ids: dict[str, str] = {}
    async with tenant_tx(ctx) as conn:
        for node in exported["nodes"]:
            db_type = type_map[node["type"]]
            label = (
                node["properties"].get("name")
                or node["properties"].get("address")
                or node["properties"].get("record_id")
                or node["canonical_key"]
            )
            row = await conn.fetchrow(
                """
                INSERT INTO entity_nodes (
                    tenant_id,entity_type,canonical_key,display_label,
                    attributes,source_record_id,observed_at
                ) VALUES ($1::uuid,$2,$3,$4,$5::jsonb,$6::uuid,$7)
                ON CONFLICT (tenant_id,entity_type,canonical_key) DO UPDATE SET
                    display_label=EXCLUDED.display_label,
                    attributes=entity_nodes.attributes || EXCLUDED.attributes,
                    source_record_id=EXCLUDED.source_record_id,
                    observed_at=GREATEST(entity_nodes.observed_at,EXCLUDED.observed_at)
                RETURNING id
                """,
                ctx.tenant_id,
                db_type,
                node["canonical_key"],
                str(label)[:500],
                json.dumps(node["properties"], default=str),
                primary_source_id,
                observed_at,
            )
            db_ids[node["id"]] = str(row["id"])
        for edge in exported["edges"]:
            await conn.execute(
                """
                INSERT INTO entity_links (
                    tenant_id,from_node_id,to_node_id,relationship,attributes,
                    source_record_id,confidence,match_status
                ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5::jsonb,$6::uuid,$7,$8)
                ON CONFLICT (
                    tenant_id,from_node_id,to_node_id,relationship,source_record_id
                ) DO UPDATE SET
                    attributes=EXCLUDED.attributes,
                    confidence=EXCLUDED.confidence,
                    match_status=EXCLUDED.match_status
                """,
                ctx.tenant_id,
                db_ids[edge["from"]],
                db_ids[edge["to"]],
                edge["type"],
                json.dumps(edge["properties"], default=str),
                primary_source_id,
                edge["confidence"],
                edge["match_status"],
            )
    result = {
        **exported,
        "persisted_node_ids": db_ids,
        "node_count": len(exported["nodes"]),
        "edge_count": len(exported["edges"]),
    }
    return await _respond(
        ctx,
        analysis_type="public_entity_graph",
        subject_id=body.property_key,
        model_version="public-record-entity-join-2026.07",
        citations=citations,
        source_ids=source_ids,
        result=result,
        confidence=1.0,
        warnings=[
            "Officer roles are public-record relationships, not inferred beneficial ownership.",
            "Unresolved identity matches require human review.",
        ],
        evidence_status=EvidenceStatus.OBSERVED,
    )


@router.get("/{property_key}")
async def property_intelligence(
    property_key: str,
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, property_key, analysis_type, evidence_status,
                   observation_date, confidence, model_version, result, trace,
                   professional_review_status, reviewed_by, reviewed_at, created_at
            FROM intelligence_scores
            WHERE property_key=$1
            ORDER BY observation_date DESC, created_at DESC
            LIMIT $2
            """,
            property_key,
            limit,
        )
    return {"property_key": property_key, "analyses": [dict(row) for row in rows]}
