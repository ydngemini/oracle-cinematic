"""Versioned, reviewed legal drafts stored only as encrypted content and PDFs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from approval_service import create_approval, decide_approval
from automation_jobs import canonical_json, enqueue_job, register_handler
from crypto import CryptoError, decrypt_pii, derive_tenant_key, encrypt_pii
from db.connection import tenant_tx
from ml_forge.synthetic_lawyer import (
    BUILTIN_CONTRACT_TEMPLATES,
    defensive_redline,
    render_approved_contract_template,
    template_sha256,
    validate_contract_template,
    write_contract_pdf,
)
from platform_policy import (
    ActionRisk,
    Feature,
    enforce_public_property_data,
    require_feature,
    validate_approval_reason,
)
from tenancy import Role, TenantContext, require_context, require_role

router = APIRouter(prefix="/api/contracts", tags=["contracts"])

DocumentType = Literal[
    "assignment", "seller_purchase", "buyer_purchase", "joint_venture", "redline"
]


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _public_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("content_ciphertext", None)
    result.pop("body_template", None)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in {"metadata", "required_fields"}:
            result[key] = _json(value)
    return result


def _tenant_key(ctx: TenantContext) -> str:
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Legal draft encryption is not configured.",
        )
    return derive_tenant_key(ctx.tenant_id, master)


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
    document_type: DocumentType
    jurisdiction: str = Field(min_length=2, max_length=80)
    body_template: str = Field(min_length=20, max_length=100_000)
    required_fields: list[str] = Field(min_length=1, max_length=50)


class TemplateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approved", "rejected"]
    attorney_reviewed_by: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=8, max_length=500)


class DocumentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_id: uuid.UUID
    lead_id: Optional[uuid.UUID] = None
    transaction_id: Optional[uuid.UUID] = None
    vault_client_id: uuid.UUID
    attorney_reviewer: str = Field(min_length=3, max_length=200)
    inputs: dict[str, Any]

    @model_validator(mode="after")
    def anchor_required(self) -> "DocumentDraft":
        if not self.lead_id and not self.transaction_id:
            raise ValueError("lead_id or transaction_id is required")
        enforce_public_property_data(self.inputs)
        return self


class DocumentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revised_text: str = Field(min_length=20, max_length=200_000)
    attorney_reviewer: str = Field(min_length=3, max_length=200)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=8, max_length=500)


class SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    signature_reference: str = Field(min_length=4, max_length=500)
    reason: str = Field(min_length=8, max_length=500)


async def _get_template(conn: Any, template_id: str, *, approved: bool = False) -> Any:
    clause = " AND status='approved'" if approved else ""
    row = await conn.fetchrow(
        f"SELECT * FROM contract_templates WHERE id=$1::uuid{clause}", template_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Contract template not found.")
    if template_sha256(row["body_template"]) != row["template_sha256"]:
        raise HTTPException(status_code=409, detail="Contract template checksum mismatch.")
    return row


@router.get("/policy")
async def contract_policy(ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.CONTRACTS)
    return {
        "approved_templates_only": True,
        "attorney_review_required": True,
        "storage": "private-s3-aes256",
        "draft_storage": "tenant-key-encrypted",
        "legal_advice": False,
    }


@router.get("/templates")
async def list_templates(ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT * FROM contract_templates ORDER BY template_key,created_at DESC"
        )
    return {"templates": [_public_row(row) for row in rows]}


@router.post("/templates/bootstrap", status_code=201)
async def bootstrap_templates(ctx: TenantContext = Depends(require_context)):
    """Install checksum-pinned draft candidates; never auto-approve them."""
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    created: list[dict[str, Any]] = []
    async with tenant_tx(ctx) as conn:
        for key, candidate in BUILTIN_CONTRACT_TEMPLATES.items():
            row = await conn.fetchrow(
                """
                INSERT INTO contract_templates (
                    tenant_id,template_key,version,document_type,jurisdiction,
                    body_template,required_fields,template_sha256,created_by
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::text[],$8,$9)
                ON CONFLICT (tenant_id,template_key,version) DO NOTHING
                RETURNING *
                """,
                ctx.tenant_id,
                key,
                candidate["version"],
                candidate["document_type"],
                candidate["jurisdiction"],
                candidate["body_template"],
                candidate["required_fields"],
                template_sha256(candidate["body_template"]),
                ctx.agent_id,
            )
            if row:
                created.append(_public_row(row))
    return {"templates": created, "status": "draft", "attorney_review_required": True}


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    validation = validate_contract_template(
        body.document_type, body.body_template, body.required_fields
    )
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"issues": validation["issues"]})
    async with tenant_tx(ctx) as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO contract_templates (
                    tenant_id,template_key,version,document_type,jurisdiction,
                    body_template,required_fields,template_sha256,created_by
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::text[],$8,$9)
                RETURNING *
                """,
                ctx.tenant_id,
                body.template_key,
                body.version,
                body.document_type,
                body.jurisdiction,
                body.body_template,
                validation["required_fields"],
                validation["template_sha256"],
                ctx.agent_id,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Template version already exists.") from exc
            raise
    return _public_row(row)


@router.post("/templates/{template_id}/decision")
async def decide_template(
    template_id: uuid.UUID,
    body: TemplateDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    reason = validate_approval_reason(body.reason)
    async with tenant_tx(ctx) as conn:
        await _get_template(conn, str(template_id))
        row = await conn.fetchrow(
            """
            UPDATE contract_templates
               SET status=$2,attorney_reviewed_by=$3,
                   attorney_reviewed_at=now(),approval_notes=$4
             WHERE id=$1::uuid AND status='draft'
            RETURNING *
            """,
            str(template_id),
            body.decision,
            body.attorney_reviewed_by,
            reason,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="Only draft templates can be decided.")
    return _public_row(row)


def _render_document(template: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    if template["document_type"] == "redline":
        return defensive_redline(
            str(inputs.get("original_text") or ""),
            str(inputs.get("proposed_text") or ""),
        )
    return render_approved_contract_template(
        document_type=template["document_type"],
        body_template=template["body_template"],
        required_fields=list(template["required_fields"]),
        transaction_data=inputs,
    )


async def _approval_for_document(
    ctx: TenantContext,
    *,
    document_id: str,
    content_sha256: str,
    template: Any,
    vault_client_id: str,
    attorney_reviewer: str,
) -> dict[str, Any]:
    payload = {
        "document_id": document_id,
        "content_sha256": content_sha256,
        "template_key": template["template_key"],
        "template_version": template["version"],
        "vault_client_id": vault_client_id,
        "attorney_reviewer": attorney_reviewer,
    }
    return await create_approval(
        ctx,
        action_type="contract.vault_and_approve",
        risk=ActionRisk.LEGAL_DOCUMENT,
        target_type="contract_document",
        target_id=document_id,
        draft_payload=payload,
        expires_in_minutes=7 * 24 * 60,
    )


@router.post("/documents", status_code=201)
async def draft_document(
    body: DocumentDraft,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        template = await _get_template(conn, str(body.template_id), approved=True)
    result = _render_document(template, body.inputs)
    if result.get("status") != "SUCCESS":
        raise HTTPException(status_code=422, detail=result)

    content = result["final_contract_text"]
    content_sha = result["content_sha256"]
    key = _tenant_key(ctx)
    metadata = {
        "content_sha256": content_sha,
        "template_sha256": template["template_sha256"],
        "warnings": result.get("warnings", []),
        "vault_client_id": str(body.vault_client_id),
        "revision": 1,
        "redline_changes": result.get("changes", []),
    }
    async with tenant_tx(ctx) as conn:
        ciphertext = await encrypt_pii(conn, content, key)
        row = await conn.fetchrow(
            """
            INSERT INTO contract_documents (
                tenant_id,transaction_id,lead_id,document_type,template_key,
                template_version,input_hash,content_ciphertext,status,
                attorney_review_required,metadata,created_by
            ) VALUES (
                $1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,'review_required',
                true,$9::jsonb,$10
            ) RETURNING *
            """,
            ctx.tenant_id,
            str(body.transaction_id) if body.transaction_id else None,
            str(body.lead_id) if body.lead_id else None,
            template["document_type"],
            template["template_key"],
            template["version"],
            result.get("input_sha256")
            or hashlib.sha256(canonical_json(body.inputs).encode("utf-8")).hexdigest(),
            ciphertext,
            canonical_json(metadata),
            ctx.agent_id,
        )

    approval = await _approval_for_document(
        ctx,
        document_id=str(row["id"]),
        content_sha256=content_sha,
        template=template,
        vault_client_id=str(body.vault_client_id),
        attorney_reviewer=body.attorney_reviewer,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "UPDATE contract_documents SET approval_id=$2::uuid WHERE id=$1 RETURNING *",
            row["id"],
            approval["id"],
        )
    return {
        "document": _public_row(row),
        "approval": approval,
        "editable_draft": content,
        "professional_review_required": True,
    }


@router.get("/documents")
async def list_documents(
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT * FROM contract_documents ORDER BY created_at DESC LIMIT $1", limit
        )
    return {"documents": [_public_row(row) for row in rows]}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    include_draft: bool = Query(default=False),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow("SELECT * FROM contract_documents WHERE id=$1", document_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Contract document not found.")
        draft = None
        if include_draft:
            try:
                draft = await decrypt_pii(conn, row["content_ciphertext"], _tenant_key(ctx))
            except CryptoError as exc:
                raise HTTPException(status_code=500, detail="Contract draft could not be decrypted.") from exc
    return {"document": _public_row(row), "editable_draft": draft}


@router.put("/documents/{document_id}/draft")
async def edit_document(
    document_id: uuid.UUID,
    body: DocumentEdit,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    if "\x00" in body.revised_text:
        raise HTTPException(status_code=422, detail="Draft contains unsupported control data.")
    try:
        body.revised_text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=422, detail="Draft contains unsupported PDF characters.") from exc
    content_sha = hashlib.sha256(body.revised_text.encode("utf-8")).hexdigest()
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM contract_documents WHERE id=$1::uuid FOR UPDATE", str(document_id)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract document not found.")
        if row["status"] not in {"draft", "review_required"}:
            raise HTTPException(status_code=409, detail="Approved or signed documents cannot be edited.")
        if row["approval_id"]:
            await conn.execute(
                """
                UPDATE action_approvals
                   SET status='revoked',decided_by=$2,decided_at=now(),
                       reason='Draft revised; prior content approval revoked.'
                 WHERE id=$1 AND status='pending'
                """,
                row["approval_id"],
                ctx.agent_id,
            )
        ciphertext = await encrypt_pii(conn, body.revised_text, _tenant_key(ctx))
        metadata = dict(_json(row["metadata"]) or {})
        metadata.update(
            {
                "content_sha256": content_sha,
                "revision": int(metadata.get("revision") or 1) + 1,
            }
        )
        row = await conn.fetchrow(
            """
            UPDATE contract_documents
               SET content_ciphertext=$2,status='review_required',approval_id=NULL,
                   metadata=$3::jsonb,reviewed_by=NULL,reviewed_at=NULL
             WHERE id=$1 RETURNING *
            """,
            row["id"],
            ciphertext,
            canonical_json(metadata),
        )
        template = await conn.fetchrow(
            """
            SELECT * FROM contract_templates
             WHERE template_key=$1 AND version=$2 AND status='approved'
            """,
            row["template_key"],
            row["template_version"],
        )
        if template is None:
            raise HTTPException(status_code=409, detail="Approved template version is unavailable.")

    approval = await _approval_for_document(
        ctx,
        document_id=str(row["id"]),
        content_sha256=content_sha,
        template=template,
        vault_client_id=str(metadata["vault_client_id"]),
        attorney_reviewer=body.attorney_reviewer,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "UPDATE contract_documents SET approval_id=$2::uuid WHERE id=$1 RETURNING *",
            row["id"],
            approval["id"],
        )
    return {"document": _public_row(row), "approval": approval}


@router.post("/documents/{document_id}/decision", status_code=202)
async def review_document(
    document_id: uuid.UUID,
    body: ReviewDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT d.*,a.draft_payload,a.payload_hash,a.status AS approval_status
              FROM contract_documents d
              JOIN action_approvals a ON a.id=d.approval_id
             WHERE d.id=$1::uuid
            """,
            str(document_id),
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Contract document not found.")
    payload = dict(_json(row["draft_payload"]) or {})
    metadata = dict(_json(row["metadata"]) or {})
    if payload.get("content_sha256") != metadata.get("content_sha256"):
        raise HTTPException(status_code=409, detail="Draft changed after approval was requested.")
    try:
        approval = await decide_approval(
            ctx,
            str(row["approval_id"]),
            decision=body.decision,
            reason=body.reason,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if body.decision == "rejected" or approval["status"] != "approved":
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                UPDATE contract_documents
                   SET status='draft',metadata=metadata || jsonb_build_object(
                       'last_rejection_reason',$2,'last_rejected_at',now()
                   ) WHERE id=$1::uuid
                """,
                str(document_id),
                body.reason,
            )
        return {"approval": approval, "queued": False}

    job, _ = await enqueue_job(
        ctx,
        job_type="contract:vault",
        payload=payload,
        idempotency_key=f"contract-vault:{document_id}:{payload['content_sha256']}",
        created_by=ctx.agent_id,
        risk=ActionRisk.LEGAL_DOCUMENT,
        approval_id=str(row["approval_id"]),
        priority=10,
        max_attempts=5,
    )
    return {"approval": approval, "job": job, "queued": True}


def _vault_sync(content: str, document_id: str, client_id: str) -> dict[str, Any]:
    from contract_vault import SovereignVault

    with tempfile.TemporaryDirectory(prefix="oracle_legal_") as tmp:
        path = Path(tmp) / f"{document_id}.pdf"
        write_contract_pdf(content, path)
        artifact_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        vaulted = SovereignVault().vault_pdf(
            path,
            client_id=client_id,
            document_id=document_id,
            expiration_seconds=300,
        )
    data = vaulted.to_dict()
    data.pop("presigned_url", None)
    return {**data, "artifact_sha256": artifact_sha}


async def _vault_document_job(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
    tenant_id = str(reporter.job["tenant_id"])
    ctx = TenantContext(
        agent_id="contract-vault-worker",
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )
    document_id = str(payload["document_id"])
    await reporter.progress(10, "Validating approved legal draft")
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM contract_documents WHERE id=$1::uuid FOR UPDATE", document_id
        )
        if row is None:
            raise ValueError("contract document not found")
        metadata = dict(_json(row["metadata"]) or {})
        if metadata.get("content_sha256") != payload.get("content_sha256"):
            raise ValueError("approved content checksum no longer matches")
        key = derive_tenant_key(tenant_id, os.environ["ORACLE_ENCRYPTION_MASTER_KEY"])
        content = await decrypt_pii(conn, row["content_ciphertext"], key)
        if not content:
            raise ValueError("approved contract content could not be decrypted")
    await reporter.progress(45, "Rendering private PDF")
    result = await asyncio.to_thread(
        _vault_sync,
        content,
        document_id,
        str(payload["vault_client_id"]),
    )
    await reporter.progress(90, "Recording vault provenance")
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            UPDATE contract_documents
               SET status='approved',reviewed_by=$2,reviewed_at=now(),
                   s3_bucket=$3,s3_key=$4,artifact_sha256=$5,
                   encryption='AES256',metadata=metadata || jsonb_build_object(
                       'vaulted_at',now(),'vault_client_id',$6
                   )
             WHERE id=$1::uuid
            """,
            document_id,
            str(payload["attorney_reviewer"]),
            result["bucket"],
            result["s3_key"],
            result["artifact_sha256"],
            str(payload["vault_client_id"]),
        )
    return result


register_handler("contract:vault", _vault_document_job)


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    expires_in: int = Query(default=300, ge=30, le=3600),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow("SELECT * FROM contract_documents WHERE id=$1", document_id)
    if row is None or row["status"] not in {"approved", "signed"} or not row["s3_key"]:
        raise HTTPException(status_code=404, detail="Approved contract PDF not found.")
    metadata = dict(_json(row["metadata"]) or {})
    from contract_vault import SovereignVault

    url = await asyncio.to_thread(
        SovereignVault(bucket_name=row["s3_bucket"]).generate_expiring_link,
        str(metadata["vault_client_id"]),
        str(document_id),
        expires_in,
    )
    if not url:
        raise HTTPException(status_code=502, detail="Contract link could not be generated.")
    return {"url": url, "expires_in": expires_in, "watermark": "PRIVATE LEGAL DOCUMENT"}


@router.post("/documents/{document_id}/signed")
async def record_signature(
    document_id: uuid.UUID,
    body: SignatureRecord,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    reason = validate_approval_reason(body.reason)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE contract_documents
               SET status='signed',metadata=metadata || jsonb_build_object(
                   'signature_reference',$2,'signature_recorded_by',$3,
                   'signature_reason',$4,'signature_recorded_at',now()
               )
             WHERE id=$1::uuid AND status='approved'
            RETURNING *
            """,
            str(document_id),
            body.signature_reference,
            ctx.agent_id,
            reason,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="Only approved documents can be marked signed.")
    return _public_row(row)
