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


def _citations(sources: list[EvidenceInput]) -> list[SourceCitation]:
    return [
        SourceCitation.model_validate(source.model_dump(exclude={"source_record_id"}))
        for source in sources
    ]


def _source_ids(sources: list[EvidenceInput]) -> list[str]:
    return [str(source.source_record_id) for source in sources]


async def _verified_citations(
    ctx: TenantContext,
    source_ids: list[str],
) -> list[SourceCitation]:
    """Resolve provenance from tenant-visible immutable records, never claims."""
    unique_ids = list(dict.fromkeys(source_ids))
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT r.id,r.record_key,r.observed_at,r.retrieved_at,
                   l.source_name,l.source_url,l.license_name
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
    citations: list[SourceCitation],
    source_ids: list[str],
    result: dict[str, Any],
    confidence: float,
    warnings: list[str],
    trace: Optional[UnderwritingTrace] = None,
    review_required: bool = False,
    evidence_status: EvidenceStatus = EvidenceStatus.INFERRED,
) -> dict[str, Any]:
    # Caller-supplied citation labels are display hints only. Immutable source
    # records are authoritative, which prevents fabricated observation dates or
    # licenses from entering an intelligence response.
    citations = await _verified_citations(ctx, source_ids)
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
    return PUBLIC_PROPERTY_DATA_POLICY


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
        citations=_citations(body.sources),
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
        citations=_citations(body.sources),
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
        citations=_citations(body.sources),
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
        citations=_citations(body.sources),
        source_ids=_source_ids(body.sources),
        result=result,
        confidence=max(0.45, min(0.85, len(body.observations) / 12)),
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
    result = {
        "candidates": detect_public_sourcing_signals(body.signals),
        "outreach_requires_approval": True,
        "identity_verification_required": True,
        "private_contacts_inferred": False,
    }
    return await _respond(
        ctx,
        analysis_type="public_sourcing_detectors",
        subject_id=body.property_key,
        model_version="public-source-detectors-2026.07",
        citations=_citations(body.sources),
        source_ids=_source_ids(body.sources),
        result=result,
        confidence=0.7,
        warnings=["Candidates require source-record review and identity verification before outreach."],
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
