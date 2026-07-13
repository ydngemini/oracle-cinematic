"""Validated model registry, opt-in style training, activation, and rollback."""

from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from approval_service import create_approval, decide_approval
from automation_jobs import enqueue_job, register_handler
from db.connection import tenant_tx
from model_training import runpod_train
from ml_forge.edge_forge.train_lora import redact_pii
from platform_policy import ActionRisk, Feature, require_feature
from tenancy import Role, TenantContext, require_context, require_role

router = APIRouter(prefix="/api/models", tags=["models"])

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in {"model_card", "compatibility", "metrics", "calibration", "dataset_manifest"}:
            result[key] = _json(value)
    return result


class ModelRegister(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=160)
    version: str = Field(min_length=1, max_length=80)
    model_kind: str = Field(pattern=r"^(base|state_lora|agent_style_lora|scoring|forecast|vision)$")
    scope_type: str = Field(pattern=r"^(tenant|state|agent)$")
    scope_key: str = Field(min_length=1, max_length=128)
    base_model: str = Field(min_length=2, max_length=300)
    artifact_uri: str = Field(min_length=3, max_length=2_048)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_card: dict[str, Any]
    compatibility: dict[str, Any] = Field(default_factory=dict)
    minimum_gpu_mb: Optional[int] = Field(default=None, gt=0, le=262_144)

    @model_validator(mode="after")
    def validate_scope(self) -> "ModelRegister":
        if self.model_kind == "state_lora" and self.scope_type != "state":
            raise ValueError("state_lora requires state scope")
        if self.model_kind == "agent_style_lora" and self.scope_type != "agent":
            raise ValueError("agent_style_lora requires agent scope")
        if self.scope_type == "state" and len(self.scope_key) != 2:
            raise ValueError("state scope_key must be a two-letter code")
        if not self.artifact_uri.startswith(("s3://", "https://")):
            raise ValueError("artifact_uri must be a private S3 or HTTPS artifact URI")
        return self


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    evaluation_set: str = Field(min_length=2, max_length=240)
    metrics: dict[str, Any]
    calibration: dict[str, Any] = Field(default_factory=dict)
    leakage_reviewed: bool
    geographic_bias_reviewed: bool
    passed: bool


class TrainingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    state_code: Optional[str] = Field(default=None, min_length=2, max_length=2)
    training_kind: str = Field(default="agent_style", pattern=r"^(agent_style|state)$")
    base_model: str = Field(min_length=2, max_length=300)
    example_ids: list[UUID] = Field(min_length=5, max_length=20_000)
    dataset_uri: str = Field(pattern=r"^s3://.+")
    consent_version: str = Field(min_length=1, max_length=120)
    max_steps: int = Field(default=60, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_training_scope(self) -> "TrainingCreate":
        if self.training_kind == "state" and not self.state_code:
            raise ValueError("state training requires state_code")
        return self


class StyleExampleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    consented: bool
    consent_version: str = Field(min_length=1, max_length=120)
    input: str = Field(min_length=1, max_length=20_000)
    output: str = Field(min_length=1, max_length=20_000)
    dataset_split: str = Field(pattern=r"^(train|evaluation)$")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=8, max_length=500)


@router.get("")
async def list_models(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.LOCAL_MODELS)
    async with tenant_tx(ctx) as conn:
        if status_filter:
            rows = await conn.fetch(
                "SELECT * FROM model_registry WHERE status=$1 ORDER BY created_at DESC LIMIT $2",
                status_filter,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM model_registry ORDER BY created_at DESC LIMIT $1", limit
            )
    return {"models": [_row(row) for row in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_model(
    body: ModelRegister,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.LOCAL_MODELS)
    require_role(ctx, Role.BROKER_OWNER)
    if body.model_card.get("artifact_sha256") not in {None, body.artifact_sha256}:
        raise HTTPException(status_code=422, detail="Model card checksum does not match artifact.")
    card = {**body.model_card, "artifact_sha256": body.artifact_sha256}
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO model_registry (
                tenant_id,name,version,model_kind,scope_type,scope_key,
                base_model,artifact_uri,artifact_sha256,model_card,
                compatibility,minimum_gpu_mb,status,registered_by
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,'candidate',$13)
            ON CONFLICT (tenant_id,name,version,scope_type,scope_key) DO UPDATE SET
                artifact_uri=EXCLUDED.artifact_uri,
                artifact_sha256=EXCLUDED.artifact_sha256,
                model_card=EXCLUDED.model_card,
                compatibility=EXCLUDED.compatibility,
                minimum_gpu_mb=EXCLUDED.minimum_gpu_mb,
                updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            body.name,
            body.version,
            body.model_kind,
            body.scope_type,
            body.scope_key.upper() if body.scope_type == "state" else body.scope_key,
            body.base_model,
            body.artifact_uri,
            body.artifact_sha256,
            json.dumps(card),
            json.dumps(body.compatibility),
            body.minimum_gpu_mb,
            ctx.agent_id,
        )
    return _row(row)


@router.post("/{model_id}/evaluations", status_code=status.HTTP_201_CREATED)
async def add_evaluation(
    model_id: UUID,
    body: EvaluationCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.LOCAL_MODELS)
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        model = await conn.fetchval("SELECT 1 FROM model_registry WHERE id=$1", model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found.")
        row = await conn.fetchrow(
            """
            INSERT INTO model_evaluations (
                tenant_id,model_id,evaluation_set,metrics,calibration,
                leakage_reviewed,geographic_bias_reviewed,passed,evaluator
            ) VALUES ($1::uuid,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8,$9)
            RETURNING *
            """,
            ctx.tenant_id,
            model_id,
            body.evaluation_set,
            json.dumps(body.metrics),
            json.dumps(body.calibration),
            body.leakage_reviewed,
            body.geographic_bias_reviewed,
            body.passed,
            ctx.agent_id,
        )
        if body.passed and body.leakage_reviewed and body.geographic_bias_reviewed:
            await conn.execute(
                "UPDATE model_registry SET status='validated' WHERE id=$1 AND status='candidate'",
                model_id,
            )
            await conn.execute(
                """
                UPDATE team_memberships m SET training_status='validated'
                  FROM users u,model_registry mr
                 WHERE mr.id=$1 AND mr.model_kind='agent_style_lora'
                   AND mr.scope_type='agent' AND u.agent_id=mr.scope_key
                   AND m.user_id=u.id
                """,
                model_id,
            )
    return _row(row)


@router.post("/{model_id}/activate")
async def activate_model(
    model_id: UUID,
    body: Decision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.LOCAL_MODELS)
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        model = await conn.fetchrow("SELECT * FROM model_registry WHERE id=$1 FOR UPDATE", model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found.")
        if model["status"] not in {"validated", "canary"}:
            raise HTTPException(status_code=409, detail="Only validated/canary models can activate.")
        passed = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM model_evaluations
                WHERE model_id=$1 AND passed=true AND leakage_reviewed=true
                  AND geographic_bias_reviewed=true
            )
            """,
            model_id,
        )
        if not passed:
            raise HTTPException(status_code=409, detail="Required evaluation gate has not passed.")
        old = await conn.fetchrow(
            """
            SELECT id FROM model_registry
            WHERE model_kind=$1 AND scope_type=$2 AND scope_key=$3 AND status='active'
            FOR UPDATE
            """,
            model["model_kind"],
            model["scope_type"],
            model["scope_key"],
        )
        if old:
            await conn.execute("UPDATE model_registry SET status='fallback' WHERE id=$1", old["id"])
        row = await conn.fetchrow(
            """
            UPDATE model_registry
               SET status='active', rollback_model_id=$2, activated_by=$3,
                   activated_at=now(), updated_at=now()
             WHERE id=$1 RETURNING *
            """,
            model_id,
            old["id"] if old else None,
            ctx.agent_id,
        )
        if model["model_kind"] == "agent_style_lora" and model["scope_type"] == "agent":
            await conn.execute(
                """
                UPDATE team_memberships m SET training_status='active'
                  FROM users u WHERE u.id=m.user_id AND u.agent_id=$1
                """,
                model["scope_key"],
            )
    return {"model": _row(row), "activation_reason": body.reason}


@router.post("/{model_id}/rollback")
async def rollback_model(
    model_id: UUID,
    body: Decision,
    ctx: TenantContext = Depends(require_context),
):
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        model = await conn.fetchrow("SELECT * FROM model_registry WHERE id=$1 FOR UPDATE", model_id)
        if not model or model["status"] != "active":
            raise HTTPException(status_code=409, detail="Model is not active.")
        rollback_id = model["rollback_model_id"]
        if not rollback_id:
            raise HTTPException(status_code=409, detail="No rollback model is registered.")
        await conn.execute("UPDATE model_registry SET status='retired' WHERE id=$1", model_id)
        row = await conn.fetchrow(
            """
            UPDATE model_registry
               SET status='active',activated_by=$2,activated_at=now(),updated_at=now()
             WHERE id=$1 RETURNING *
            """,
            rollback_id,
            ctx.agent_id,
        )
    return {"model": _row(row), "rollback_reason": body.reason, "rolled_back_from": str(model_id)}


@router.post("/training/examples", status_code=status.HTTP_201_CREATED)
async def add_style_example(
    body: StyleExampleCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Redact a specifically consented example before any persistence."""
    require_feature(Feature.LOCAL_MODELS)
    if body.consented is not True:
        raise HTTPException(status_code=422, detail="Explicit opt-in consent is required.")
    agent_id = body.agent_id or ctx.agent_id
    if agent_id != ctx.agent_id:
        require_role(ctx, Role.BROKER_OWNER)
    redacted_input, input_scan = redact_pii(body.input)
    redacted_output, output_scan = redact_pii(body.output)
    scan = {
        key: input_scan.get(key, 0) + output_scan.get(key, 0)
        for key in set(input_scan) | set(output_scan)
    }
    digest = hashlib.sha256(
        json.dumps(
            {"agent_id": agent_id, "input": redacted_input, "output": redacted_output},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO style_training_examples (
                tenant_id,agent_id,consented_at,consent_version,
                redacted_input,redacted_output,pii_scan,dataset_split,example_sha256
            ) VALUES ($1::uuid,$2,now(),$3,$4,$5,$6::jsonb,$7,$8)
            ON CONFLICT (tenant_id,agent_id,example_sha256) DO UPDATE SET
                consented_at=EXCLUDED.consented_at,
                consent_version=EXCLUDED.consent_version,
                dataset_split=EXCLUDED.dataset_split,
                revoked_at=NULL
            RETURNING id,agent_id,consented_at,consent_version,pii_scan,
                      dataset_split,example_sha256,revoked_at,created_at
            """,
            ctx.tenant_id,
            agent_id,
            body.consent_version,
            redacted_input,
            redacted_output,
            json.dumps(scan),
            body.dataset_split,
            digest,
        )
    return _row(row)


@router.delete("/training/examples/{example_id}")
async def revoke_style_example(
    example_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Withdraw consent without erasing the immutable training provenance row."""
    require_feature(Feature.LOCAL_MODELS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM style_training_examples WHERE id=$1", example_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Training example not found.")
        if row["agent_id"] != ctx.agent_id:
            require_role(ctx, Role.BROKER_OWNER)
        row = await conn.fetchrow(
            """
            UPDATE style_training_examples SET revoked_at=COALESCE(revoked_at,now())
             WHERE id=$1 RETURNING id,agent_id,revoked_at,example_sha256
            """,
            example_id,
        )
    return _row(row)


@router.post("/training", status_code=status.HTTP_201_CREATED)
async def create_training_run(
    body: TrainingCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.LOCAL_MODELS)
    example_agent_id = body.agent_id or ctx.agent_id
    if body.training_kind == "state":
        require_role(ctx, Role.BROKER_OWNER)
    elif example_agent_id != ctx.agent_id:
        require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,dataset_split,consent_version,example_sha256
            FROM style_training_examples
            WHERE id=ANY($1::uuid[]) AND agent_id=$2 AND revoked_at IS NULL
            """,
            list(body.example_ids),
            example_agent_id,
        )
    if len(rows) != len(set(body.example_ids)):
        raise HTTPException(status_code=422, detail="One or more examples are missing, revoked, or belong to another agent.")
    if any(row["consent_version"] != body.consent_version for row in rows):
        raise HTTPException(status_code=422, detail="Consent version mismatch in training examples.")
    splits = {row["dataset_split"] for row in rows}
    if not {"train", "evaluation"}.issubset(splits):
        raise HTTPException(status_code=422, detail="Separate train and evaluation examples are required.")

    run_id = str(uuid4())
    manifest = {
        "training_run_id": run_id,
        "tenant_id": ctx.tenant_id,
        "agent_id": example_agent_id if body.training_kind == "agent_style" else None,
        "example_agent_id": example_agent_id,
        "state_code": body.state_code.upper() if body.state_code else None,
        "scope_type": "agent" if body.training_kind == "agent_style" else "state",
        "scope_key": (
            example_agent_id
            if body.training_kind == "agent_style"
            else str(body.state_code).upper()
        ),
        "base_model": body.base_model,
        "dataset_uri": body.dataset_uri,
        "example_sha256": [row["example_sha256"] for row in rows],
        "train_count": sum(row["dataset_split"] == "train" for row in rows),
        "evaluation_count": sum(row["dataset_split"] == "evaluation" for row in rows),
        "consent_version": body.consent_version,
        "max_steps": body.max_steps,
        "pii_redacted": True,
    }
    approval_payload = {"training_run_id": run_id, "manifest": manifest}
    approval = await create_approval(
        ctx,
        action_type="model:runpod_training",
        risk=ActionRisk.FINANCIAL,
        target_type="model_training_run",
        target_id=run_id,
        draft_payload=approval_payload,
        expires_in_minutes=1_440,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO model_training_runs (
                id,tenant_id,agent_id,state_code,provider,base_model,
                dataset_manifest,status,created_by
            ) VALUES ($1::uuid,$2::uuid,$3,$4,'runpod',$5,$6::jsonb,'queued',$7)
            RETURNING *
            """,
            run_id,
            ctx.tenant_id,
            example_agent_id if body.training_kind == "agent_style" else None,
            body.state_code.upper() if body.state_code else None,
            body.base_model,
            json.dumps({**manifest, "approval_id": str(approval["id"])}),
            ctx.agent_id,
        )
    return {"training_run": _row(row), "approval": approval}


@router.post("/training/{run_id}/approve")
async def approve_training_run(
    run_id: UUID,
    body: Decision,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        run = await conn.fetchrow("SELECT * FROM model_training_runs WHERE id=$1", run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Training run not found.")
    manifest = _json(run["dataset_manifest"])
    approval_id = manifest.pop("approval_id")
    approval_payload = {"training_run_id": str(run_id), "manifest": manifest}
    approval = await decide_approval(
        ctx, approval_id, decision="approved", reason=body.reason
    )
    job, _ = await enqueue_job(
        ctx,
        job_type="model:train",
        payload=approval_payload,
        idempotency_key=f"model-training:{run_id}",
        created_by=ctx.agent_id,
        priority=30,
        max_attempts=2,
        risk=ActionRisk.FINANCIAL,
        approval_id=approval_id,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "UPDATE model_training_runs SET job_id=$2 WHERE id=$1 RETURNING *",
            run_id,
            job["id"],
        )
    return {"training_run": _row(row), "approval": approval, "job": job}


async def _training_job(payload: dict[str, Any], reporter) -> dict[str, Any]:
    tenant_id = str(reporter.job["tenant_id"])
    ctx = TenantContext(agent_id="model-training-worker", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    run_id = payload["training_run_id"]
    manifest = dict(payload["manifest"])
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE model_training_runs SET status='running',started_at=now() WHERE id=$1::uuid",
            run_id,
        )
        await conn.execute(
            """
            UPDATE team_memberships m SET training_status='training'
              FROM users u WHERE u.id=m.user_id AND u.agent_id=$1
            """,
            manifest.get("agent_id"),
        )
    try:
        result = await runpod_train(manifest, reporter)
        card = dict(result["model_card"])
        version = str(card.get("version") or f"run-{run_id}")[:80]
        artifact_uri = str(result["artifact_uri"])
        checksum = str(result["artifact_sha256"]).lower()
        if manifest.get("scope_type") == "agent":
            model_kind = "agent_style_lora"
            scope_type = "agent"
            scope_key = str(manifest["scope_key"])
            name = f"agent-style-{scope_key}"[:160]
        else:
            model_kind = "state_lora"
            scope_type = "state"
            scope_key = str(manifest["scope_key"] or "").upper()
            name = f"state-underwriter-{scope_key}"[:160]
        if not scope_key:
            raise ValueError("training manifest has no model scope")
        card.update(
            {
                "artifact_sha256": checksum,
                "training_run_id": run_id,
                "consent_version": manifest.get("consent_version"),
                "pii_redacted": True,
            }
        )
    except Exception as exc:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                UPDATE model_training_runs
                   SET status='failed',completed_at=now(),error=$2 WHERE id=$1::uuid
                """,
                run_id,
                str(exc)[:2_000],
            )
            await conn.execute(
                """
                UPDATE team_memberships m SET training_status='failed'
                  FROM users u WHERE u.id=m.user_id AND u.agent_id=$1
                """,
                manifest.get("agent_id"),
            )
        raise
    async with tenant_tx(ctx) as conn:
        model = await conn.fetchrow(
            """
            INSERT INTO model_registry (
                tenant_id,name,version,model_kind,scope_type,scope_key,
                base_model,artifact_uri,artifact_sha256,model_card,
                compatibility,status,registered_by
            ) VALUES (
                $1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,
                'candidate','model-training-worker'
            )
            ON CONFLICT (tenant_id,name,version,scope_type,scope_key) DO UPDATE SET
                artifact_uri=EXCLUDED.artifact_uri,
                artifact_sha256=EXCLUDED.artifact_sha256,
                model_card=EXCLUDED.model_card,
                compatibility=EXCLUDED.compatibility,updated_at=now()
            RETURNING id
            """,
            tenant_id,
            name,
            version,
            model_kind,
            scope_type,
            scope_key,
            manifest["base_model"],
            artifact_uri,
            checksum,
            json.dumps(card),
            json.dumps(
                {
                    "base_model": manifest["base_model"],
                    "training_provider": "runpod",
                    "runpod_job_id": result.get("runpod_job_id"),
                }
            ),
        )
        await conn.execute(
            """
            UPDATE model_training_runs
               SET status='succeeded',completed_at=now(),artifact_sha256=$2,
                   model_card=$3::jsonb,error=NULL
             WHERE id=$1::uuid
            """,
            run_id,
            checksum,
            json.dumps(card),
        )
        await conn.execute(
            """
            UPDATE team_memberships m SET training_status='awaiting_validation'
              FROM users u WHERE u.id=m.user_id AND u.agent_id=$1
            """,
            manifest.get("agent_id"),
        )
    return {**result, "model_registry_id": str(model["id"]), "model_version": version}


register_handler("model:train", _training_job)
