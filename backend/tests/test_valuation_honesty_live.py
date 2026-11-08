"""The valuation promotion gate against real PostgreSQL (audit A08, L11-05):
real jsonb model cards and evaluation metrics, FORCE RLS, the application
login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Two DE underwriters, both validated, both with a passed, reviewed evaluation
that publishes per-region error: the one whose card says its targets are
heuristic is refused; the one trained on observed transactions activates.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

PER_REGION = {"per_region": {"New Castle": {"n": 140, "error": 0.08, "uncertainty": 0.12}}}


def test_only_an_underwriter_on_observed_targets_activates(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_LOCAL_MODELS", "1")
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg
    from fastapi import HTTPException

    import models_api
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = uuid.uuid4()
    tag = str(tenant)[:8]
    owner = TenantContext(agent_id=f"owner-{tag}@models.test", tenant_id=str(tenant), role=Role.BROKER_OWNER)

    async def model(name, provenance):
        mid = uuid.uuid4()
        card = {"dataset": {"targets": {"provenance": provenance}}}
        await admin.execute(
            """INSERT INTO model_registry (id, tenant_id, name, version, model_kind, scope_type, scope_key,
                   base_model, artifact_uri, artifact_sha256, model_card, status, registered_by)
               VALUES ($1,$2,$3,'1','state_lora','state','DE','base','s3://b/a',$4,$5::jsonb,'validated','t')""",
            mid, tenant, name, "a" * 64, json.dumps(card))
        await admin.execute(
            """INSERT INTO model_evaluations (tenant_id, model_id, evaluation_set, metrics,
                   leakage_reviewed, geographic_bias_reviewed, passed, evaluator)
               VALUES ($1,$2,'holdout-2026',$3::jsonb,true,true,true,'t')""",
            tenant, mid, json.dumps(PER_REGION))
        return mid

    decision = models_api.Decision(reason="promote after evaluation")
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Models')", tenant, f"models-{tag}")
        heuristic = await model("de-heuristic", "heuristic")
        observed = await model("de-observed", "observed_transactions")

        with pytest.raises(HTTPException) as refused:
            await models_api.activate_model(model_id=heuristic, body=decision, ctx=owner)
        assert refused.value.status_code == 409 and "heuristic or unrecorded targets" in refused.value.detail
        assert await admin.fetchval("SELECT status FROM model_registry WHERE id=$1", heuristic) == "validated"

        out = await models_api.activate_model(model_id=observed, body=decision, ctx=owner)
        assert out["model"]["status"] == "active"
        assert await admin.fetchval("SELECT status FROM model_registry WHERE id=$1", observed) == "active"

        # Re-registering the active version with a heuristic artifact is
        # refused; the active row keeps its artifact and card (L11-06).
        swap = models_api.ModelRegister(
            name="de-observed", version="1", model_kind="state_lora", scope_type="state", scope_key="DE",
            base_model="base", artifact_uri="s3://b/heuristic", artifact_sha256="b" * 64,
            model_card={"dataset": {"targets": {"provenance": "heuristic"}}})
        with pytest.raises(HTTPException) as swapped:
            await models_api.register_model(body=swap, ctx=owner)
        assert swapped.value.status_code == 409 and "active" in swapped.value.detail
        row = await admin.fetchrow("SELECT status, artifact_uri, model_card FROM model_registry WHERE id=$1", observed)
        assert (row["status"], row["artifact_uri"]) == ("active", "s3://b/a")
        assert json.loads(row["model_card"])["dataset"]["targets"]["provenance"] == "observed_transactions"

        # A per-region report on an evaluation nobody reviewed for leakage and
        # geographic bias does not count, even beside a reviewed pass.
        unreviewed = uuid.uuid4()
        await admin.execute(
            """INSERT INTO model_registry (id, tenant_id, name, version, model_kind, scope_type, scope_key,
                   base_model, artifact_uri, artifact_sha256, model_card, status, registered_by)
               VALUES ($1,$2,'de-unreviewed','1','state_lora','state','DE','base','s3://b/c',$3,$4::jsonb,'validated','t')""",
            unreviewed, tenant, "c" * 64, json.dumps({"dataset": {"targets": {"provenance": "observed_transactions"}}}))
        await admin.execute(
            """INSERT INTO model_evaluations (tenant_id, model_id, evaluation_set, metrics,
                   leakage_reviewed, geographic_bias_reviewed, passed, evaluator)
               VALUES ($1,$2,'holdout-a','{"accuracy": 0.9}'::jsonb,true,true,true,'t'),
                      ($1,$2,'holdout-b',$3::jsonb,false,false,true,'t')""",
            tenant, unreviewed, json.dumps(PER_REGION))
        with pytest.raises(HTTPException) as unseen:
            await models_api.activate_model(model_id=unreviewed, body=decision, ctx=owner)
        assert unseen.value.status_code == 409 and "per-region error and uncertainty" in unseen.value.detail
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("model_evaluations", "model_registry"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
