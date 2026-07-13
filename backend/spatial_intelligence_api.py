"""Separate provenance-aware property inference and spatial concept routes."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from automation_jobs import enqueue_job, register_handler
from db.connection import tenant_tx
from intelligence_api import AnalysisBase, _respond, _source_ids, _verified_citations
from platform_policy import EvidenceStatus, Feature, enforce_public_property_data, require_feature
from property_inference import (
    CHARACTERISTIC_MODEL_VERSION,
    GEOSPATIAL_MODEL_VERSION,
    PHOTO_REHAB_MODEL_VERSION,
    TOUR_DISCLOSURE,
    TOUR_VARIANT_MODEL_VERSION,
    InferenceInputError,
    analyze_topography,
    estimate_photo_rehab,
    impute_characteristics,
    tour_variant_manifest,
)
from tenancy import Role, TenantContext, require_context

router = APIRouter(prefix="/api/intelligence", tags=["spatial-intelligence"])


class CharacteristicInference(AnalysisBase):
    missing_fields: list[str] = Field(min_length=1, max_length=50)
    comparable_records: list[dict[str, Any]] = Field(min_length=3, max_length=200)


class PhotoRehabInference(AnalysisBase):
    findings: list[dict[str, Any]] = Field(min_length=1, max_length=200)


class TopographyInference(AnalysisBase):
    samples: list[dict[str, Any]] = Field(min_length=3, max_length=2_000)


class TourVariantRequest(AnalysisBase):
    model_config = ConfigDict(extra="forbid")
    lead_id: Optional[uuid.UUID] = None
    listing_id: Optional[uuid.UUID] = None
    source_media_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    variant_name: str = Field(min_length=2, max_length=120)
    style: str = Field(min_length=2, max_length=40)
    rehab_scope: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    source_media_rights_attested: bool

    @model_validator(mode="after")
    def validate_anchor_and_rights(self) -> "TourVariantRequest":
        if not self.lead_id and not self.listing_id:
            raise ValueError("lead_id or listing_id is required")
        if not self.source_media_rights_attested:
            raise ValueError("rights to use every source image must be attested")
        enforce_public_property_data(self.model_dump(exclude={"sources"}))
        return self


async def _persist_inference(
    ctx: TenantContext,
    *,
    property_key: str,
    characteristic: str,
    inferred_value: Any,
    confidence: float,
    model_version: str,
    source_ids: list[str],
    method: str,
) -> None:
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            INSERT INTO property_characteristic_inferences (
                tenant_id,property_key,characteristic,inferred_value,
                confidence,model_version,source_record_ids,method
            ) VALUES ($1::uuid,$2,$3,$4::jsonb,$5,$6,$7::uuid[],$8)
            """,
            ctx.tenant_id,
            property_key,
            characteristic,
            json.dumps(inferred_value, default=str),
            max(0.0, min(confidence, 1.0)),
            model_version,
            source_ids,
            method,
        )


@router.post("/characteristics/impute")
async def characteristic_imputation(
    body: CharacteristicInference,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    source_ids = _source_ids(body.sources)
    citations = await _verified_citations(ctx, source_ids)
    try:
        result = impute_characteristics(body.missing_fields, body.comparable_records)
    except InferenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for inference in result["inferences"]:
        if inference["status"] == "inferred":
            await _persist_inference(
                ctx,
                property_key=body.property_key,
                characteristic=inference["characteristic"],
                inferred_value=inference["inferred_value"],
                confidence=inference["confidence"],
                model_version=CHARACTERISTIC_MODEL_VERSION,
                source_ids=source_ids,
                method="statistical",
            )
    confidences = [item["confidence"] for item in result["inferences"] if item["confidence"] > 0]
    return await _respond(
        ctx,
        analysis_type="property_characteristic_imputation",
        subject_id=body.property_key,
        model_version=CHARACTERISTIC_MODEL_VERSION,
        citations=citations,
        source_ids=source_ids,
        result=result,
        confidence=sum(confidences) / len(confidences) if confidences else 0.0,
        warnings=result["warnings"],
        evidence_status=EvidenceStatus.INFERRED,
    )


@router.post("/rehab/photo-estimate")
async def photo_rehab_estimate(
    body: PhotoRehabInference,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    enforce_public_property_data(body.model_dump())
    source_ids = _source_ids(body.sources)
    citations = await _verified_citations(ctx, source_ids)
    try:
        result = estimate_photo_rehab(body.findings)
    except InferenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _persist_inference(
        ctx,
        property_key=body.property_key,
        characteristic="rehab_cost_band",
        inferred_value=result,
        confidence=result["confidence"],
        model_version=PHOTO_REHAB_MODEL_VERSION,
        source_ids=source_ids,
        method="photo_estimate",
    )
    return await _respond(
        ctx,
        analysis_type="photo_rehab_estimate",
        subject_id=body.property_key,
        model_version=PHOTO_REHAB_MODEL_VERSION,
        citations=citations,
        source_ids=source_ids,
        result=result,
        confidence=result["confidence"],
        warnings=result["warnings"],
        review_required=True,
    )


@router.post("/spatial/topography")
async def topography_intelligence(
    body: TopographyInference,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.SPATIAL_TOURS)
    enforce_public_property_data(body.model_dump())
    source_ids = _source_ids(body.sources)
    citations = await _verified_citations(ctx, source_ids)
    try:
        result = analyze_topography(body.samples)
    except InferenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    confidence = min(0.9, 0.45 + len(body.samples) / 100)
    await _persist_inference(
        ctx,
        property_key=body.property_key,
        characteristic="topography_viewshed_context",
        inferred_value=result,
        confidence=confidence,
        model_version=GEOSPATIAL_MODEL_VERSION,
        source_ids=source_ids,
        method="geospatial",
    )
    return await _respond(
        ctx,
        analysis_type="source_backed_topography",
        subject_id=body.property_key,
        model_version=GEOSPATIAL_MODEL_VERSION,
        citations=citations,
        source_ids=source_ids,
        result=result,
        confidence=confidence,
        warnings=result["warnings"],
        review_required=True,
    )


@router.post("/spatial/tour-variants", status_code=202)
async def create_tour_variant(
    body: TourVariantRequest,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.SPATIAL_TOURS)
    source_ids = _source_ids(body.sources)
    citations = await _verified_citations(ctx, source_ids)
    try:
        # Validate the render request before persisting/queueing it.
        tour_variant_manifest(
            variant_name=body.variant_name,
            style=body.style,
            rehab_scope=body.rehab_scope,
            source_media_ids=[str(value) for value in body.source_media_ids],
        )
    except InferenceInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with tenant_tx(ctx) as conn:
        media_count = await conn.fetchval(
            """
            SELECT count(*) FROM property_media
             WHERE id=ANY($1::uuid[])
               AND (($2::uuid IS NOT NULL AND lead_id=$2)
                 OR ($3::uuid IS NOT NULL AND listing_id=$3))
            """,
            [str(value) for value in body.source_media_ids],
            str(body.lead_id) if body.lead_id else None,
            str(body.listing_id) if body.listing_id else None,
        )
        if media_count != len(set(body.source_media_ids)):
            raise HTTPException(status_code=422, detail="Source media are not visible on the anchored property.")
        row = await conn.fetchrow(
            """
            INSERT INTO spatial_tour_variants (
                tenant_id,lead_id,listing_id,property_key,source_record_ids,
                source_media_ids,variant_name,style,rehab_scope,model_version,
                disclosure,created_by
            ) VALUES (
                $1::uuid,$2::uuid,$3::uuid,$4,$5::uuid[],$6::uuid[],$7,$8,
                $9::jsonb,$10,$11,$12
            ) RETURNING id
            """,
            ctx.tenant_id,
            str(body.lead_id) if body.lead_id else None,
            str(body.listing_id) if body.listing_id else None,
            body.property_key,
            source_ids,
            [str(value) for value in body.source_media_ids],
            body.variant_name,
            body.style,
            json.dumps(body.rehab_scope, default=str),
            TOUR_VARIANT_MODEL_VERSION,
            TOUR_DISCLOSURE,
            ctx.agent_id,
        )
    payload = {"variant_id": str(row["id"])}
    job, _ = await enqueue_job(
        ctx,
        job_type="spatial:tour_variant",
        payload=payload,
        idempotency_key=f"tour-variant:{row['id']}",
        created_by=ctx.agent_id,
        max_attempts=3,
    )
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE spatial_tour_variants SET job_id=$2::uuid WHERE id=$1",
            row["id"],
            job["id"],
        )
    result = {
        "variant_id": str(row["id"]),
        "job": job,
        "state": "queued",
        "disclosure": TOUR_DISCLOSURE,
    }
    return await _respond(
        ctx,
        analysis_type="post_rehab_tour_variant",
        subject_id=body.property_key,
        model_version=TOUR_VARIANT_MODEL_VERSION,
        citations=citations,
        source_ids=source_ids,
        result=result,
        confidence=0.7,
        warnings=[TOUR_DISCLOSURE],
        evidence_status=EvidenceStatus.MIXED,
    )


async def _tour_variant_job(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
    tenant_id = str(reporter.job["tenant_id"])
    ctx = TenantContext(
        agent_id="tour-variant-worker",
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )
    variant_id = str(payload["variant_id"])
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "UPDATE spatial_tour_variants SET state='running' WHERE id=$1::uuid RETURNING *",
            variant_id,
        )
    if row is None:
        raise ValueError("tour variant not found")
    await reporter.progress(35, "Building disclosed post-rehab concept manifest")
    try:
        rehab_scope = row["rehab_scope"]
        if isinstance(rehab_scope, str):
            rehab_scope = json.loads(rehab_scope)
        manifest = tour_variant_manifest(
            variant_name=row["variant_name"],
            style=row["style"],
            rehab_scope=rehab_scope,
            source_media_ids=[str(value) for value in row["source_media_ids"]],
        )
        await reporter.progress(85, "Recording AI disclosure and provenance")
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                UPDATE spatial_tour_variants
                   SET state='succeeded',manifest=$2::jsonb WHERE id=$1::uuid
                """,
                variant_id,
                json.dumps(manifest, default=str),
            )
        return manifest
    except Exception:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                "UPDATE spatial_tour_variants SET state='failed' WHERE id=$1::uuid",
                variant_id,
            )
        raise


register_handler("spatial:tour_variant", _tour_variant_job)
