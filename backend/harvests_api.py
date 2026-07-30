"""Harvest scheduling, coverage, freshness, failures, and manual reruns."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from automation_jobs import enqueue_job, list_jobs, register_handler
from db.connection import tenant_tx
from platform_policy import Feature, require_feature
from tenancy import Role, TenantContext, require_context, require_role

router = APIRouter(prefix="/api/harvests", tags=["harvests"])
logger = logging.getLogger("oracle.harvests")

SOURCE_CATALOG = {
    "chicago_building_violations": {
        "display_name": "Chicago Building Violations",
        "jurisdiction": "IL-Chicago",
        "adapter": "ChicagoBuildingViolationsHarvester",
        "default_schedule_seconds": 1_800,
    },
    "nyc_hpd_violations": {
        "display_name": "NYC HPD Violations",
        "jurisdiction": "NY-NYC",
        "adapter": "NYCHPDViolationsHarvester",
        "default_schedule_seconds": 1_800,
    },
    "nyc_pluto": {
        "display_name": "NYC PLUTO",
        "jurisdiction": "NY-NYC",
        "adapter": "NewYorkPlutoHarvester",
        "default_schedule_seconds": 86_400,
    },
    "national_parcels": {
        "display_name": "National Parcel Firehose",
        "jurisdiction": "US",
        "adapter": "MultiStateFirehose",
        "default_schedule_seconds": 86_400,
    },
}


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
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in {"coverage", "metrics", "payload", "result"}:
            result[key] = _json(value)
    return result


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    schedule_seconds: int = Field(ge=300, le=31_536_000)


class ManualRerun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_records: Optional[int] = Field(default=None, ge=1, le=100_000)
    state_codes: list[str] = Field(default_factory=list, max_length=51)
    reason: str = Field(min_length=8, max_length=500)


@router.get("")
async def harvest_status(ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.MUNICIPAL_HARVESTS)
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        sources = await conn.fetch(
            """
            SELECT s.*,
                   r.id AS latest_run_id,r.state AS latest_run_state,
                   r.started_at AS latest_run_started_at,
                   r.completed_at AS latest_run_completed_at,
                   r.fetched AS latest_fetched,r.inserted AS latest_inserted,
                   r.malformed AS latest_malformed,r.retries AS latest_retries,
                   r.error_summary AS latest_error_summary
            FROM harvest_sources s
            LEFT JOIN LATERAL (
                SELECT * FROM harvest_runs h
                WHERE h.source_id=s.id ORDER BY h.started_at DESC LIMIT 1
            ) r ON true
            ORDER BY s.source_key
            """
        )
    by_key = {row["source_key"]: _row(row) for row in sources}
    output = []
    now = time.time()
    for key, definition in SOURCE_CATALOG.items():
        item = by_key.get(key, {"source_key": key, **definition, "enabled": False})
        cursor_at = item.get("cursor_observed_at")
        last_success = item.get("last_succeeded_at")
        item["cursor_age_seconds"] = (
            max(0, now - datetime.fromisoformat(cursor_at).timestamp()) if cursor_at else None
        )
        item["source_freshness_seconds"] = (
            max(0, now - datetime.fromisoformat(last_success).timestamp()) if last_success else None
        )
        hits = int(item.get("cache_hits") or 0)
        misses = int(item.get("cache_misses") or 0)
        item["cache_savings_rate"] = round(hits / (hits + misses), 4) if hits + misses else None
        output.append(item)
    from data_integrations.periodic import scheduler

    return {"sources": output, "scheduler": scheduler.status()}


@router.get("/jobs")
async def harvest_jobs(
    state_filter: Optional[str] = Query(default=None, alias="state"),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_role(ctx, Role.BROKER_OWNER)
    jobs = await list_jobs(ctx, state=state_filter, limit=limit)
    return {"jobs": [job for job in jobs if str(job.get("job_type", "")).startswith(("harvest:", "periodic:"))]}


@router.put("/{source_key}/schedule")
async def update_schedule(
    source_key: str,
    body: ScheduleUpdate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MUNICIPAL_HARVESTS)
    require_role(ctx, Role.BROKER_OWNER)
    definition = SOURCE_CATALOG.get(source_key)
    if not definition:
        raise HTTPException(status_code=404, detail="Harvest source not found.")
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO harvest_sources (
                tenant_id,source_key,display_name,jurisdiction,adapter,
                schedule_seconds,enabled
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                schedule_seconds=EXCLUDED.schedule_seconds,
                enabled=EXCLUDED.enabled,updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            source_key,
            definition["display_name"],
            definition["jurisdiction"],
            definition["adapter"],
            body.schedule_seconds,
            body.enabled,
        )
    return _row(row)


@router.post("/{source_key}/rerun")
async def manual_rerun(
    source_key: str,
    body: ManualRerun,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MUNICIPAL_HARVESTS)
    require_role(ctx, Role.BROKER_OWNER)
    if source_key not in SOURCE_CATALOG:
        raise HTTPException(status_code=404, detail="Harvest source not found.")
    states = sorted({state.upper() for state in body.state_codes if len(state) == 2})
    request_id = str(uuid.uuid4())
    job, _ = await enqueue_job(
        ctx,
        job_type="harvest:source",
        payload={
            "source_key": source_key,
            "max_records": body.max_records,
            "state_codes": states,
            "manual_reason": body.reason,
            "requested_by": ctx.agent_id,
        },
        idempotency_key=f"harvest-manual:{source_key}:{request_id}",
        created_by=ctx.agent_id,
        priority=15,
        max_attempts=5,
    )
    return {"job": job, "controlled_rerun": True}


async def _execute_source_harvest(
    source_key: str,
    tenant_id: str,
    max_records: Optional[int],
    state_codes: list[str],
) -> dict[str, Any]:
    if source_key == "chicago_building_violations":
        from harvesters.il_chicago_violations import ChicagoBuildingViolationsHarvester

        return await ChicagoBuildingViolationsHarvester(
            tenant_id, agent_id="durable-chicago"
        ).harvest(max_records=max_records, persist=True)
    if source_key == "nyc_hpd_violations":
        from harvesters.ny_hpd_violations import NYCHPDViolationsHarvester

        return await NYCHPDViolationsHarvester(
            tenant_id, agent_id="durable-hpd"
        ).harvest(max_records=max_records, persist=True)
    if source_key == "nyc_pluto":
        from harvesters.ny_pluto import NewYorkPlutoHarvester

        return await NewYorkPlutoHarvester(
            tenant_id, agent_id="durable-pluto"
        ).harvest(max_records=max_records, persist=True)

    from harvesters.firehose import MultiStateFirehose

    raw = await MultiStateFirehose(
        tenant_id, states=state_codes or None, agent_id="durable-national"
    ).run(max_records_per_state=max_records, concurrency=2)
    return {**raw.get("totals", {}), "states": raw.get("states", {})}


async def _harvest_job(payload: dict[str, Any], reporter) -> dict[str, Any]:
    source_key = str(payload.get("source_key") or "")
    definition = SOURCE_CATALOG.get(source_key)
    if not definition:
        raise ValueError(f"unknown harvest source {source_key!r}")
    tenant_id = str(reporter.job["tenant_id"])
    ctx = TenantContext(agent_id="harvest-worker", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        source = await conn.fetchrow(
            """
            INSERT INTO harvest_sources (
                tenant_id,source_key,display_name,jurisdiction,adapter,
                schedule_seconds,enabled,last_started_at
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,true,now())
            ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                last_started_at=now(),updated_at=now()
            RETURNING *
            """,
            tenant_id,
            source_key,
            definition["display_name"],
            definition["jurisdiction"],
            definition["adapter"],
            definition["default_schedule_seconds"],
        )
        run = await conn.fetchrow(
            """
            INSERT INTO harvest_runs (tenant_id,source_id,job_id,state,cursor_start)
            VALUES ($1::uuid,$2,$3::uuid,'running',$4) RETURNING id
            """,
            tenant_id,
            source["id"],
            reporter.job["id"],
            source["cursor_value"],
        )
    await reporter.progress(10, f"{source_key}: fetching public records")
    max_records = payload.get("max_records")
    try:
        result = await _execute_source_harvest(
            source_key,
            tenant_id,
            max_records,
            list(payload.get("state_codes") or []),
        )
    except Exception as exc:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                UPDATE harvest_runs
                   SET state='failed',completed_at=now(),error_summary=$2
                 WHERE id=$1
                """,
                run["id"],
                str(exc)[:2_000],
            )
            failed_source = await conn.fetchrow(
                """
                UPDATE harvest_sources
                   SET failure_count=failure_count+1,last_error=$2,
                       circuit_state=CASE WHEN failure_count+1>=5 THEN 'open' ELSE circuit_state END,
                       circuit_open_until=CASE WHEN failure_count+1>=5
                           THEN now()+interval '5 minutes' ELSE circuit_open_until END,
                       updated_at=now()
                 WHERE id=$1
                 RETURNING failure_count,circuit_state
                """,
                source["id"],
                str(exc)[:2_000],
            )
        logger.error(
            "ORACLE_METRIC harvest_source_failure source=%s circuit=%s failures=%d",
            source_key,
            str(failed_source["circuit_state"] if failed_source else "unknown"),
            int(failed_source["failure_count"] if failed_source else 1),
        )
        raise

    await reporter.progress(90, f"{source_key}: recording coverage")
    has_state_failures = int(result.get("errors") or 0) > 0
    run_state = "partial" if has_state_failures else "succeeded"
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            UPDATE harvest_runs
               SET state=$2,requests=$3,fetched=$4,normalized=$5,
                   aggregated=$6,inserted=$7,retries=$8,cache_hits=$9,
                   malformed=$10,completed_at=now(),metrics=$11::jsonb,
                   error_summary=CASE WHEN $2='partial' THEN 'One or more jurisdictions failed.' ELSE NULL END
             WHERE id=$1
            """,
            run["id"],
            run_state,
            int(result.get("requests") or 0),
            int(result.get("fetched") or 0),
            int(result.get("parsed") or 0),
            int(result.get("aggregated") or result.get("parsed") or 0),
            int(result.get("inserted") or 0),
            int(result.get("retries") or 0),
            int(result.get("cache_hits") or 0),
            int(result.get("malformed") or 0),
            json.dumps(result, default=str),
        )
        await conn.execute(
            """
            UPDATE harvest_sources
               SET last_succeeded_at=CASE WHEN $5 THEN last_succeeded_at ELSE now() END,
                   coverage=$2::jsonb,
                   cache_hits=cache_hits+$3,cache_misses=cache_misses+$4,
                   failure_count=CASE WHEN $5 THEN failure_count+1 ELSE 0 END,
                   circuit_state=CASE WHEN $5 AND failure_count+1>=5 THEN 'open'
                                      WHEN NOT $5 THEN 'closed' ELSE circuit_state END,
                   health_status=CASE WHEN $5 THEN 'degraded' ELSE 'fresh' END,
                   health_detail=CASE WHEN $5 THEN 'One or more jurisdictions failed.' ELSE NULL END,
                   last_health_checked_at=now(),last_error=CASE WHEN $5
                       THEN 'One or more jurisdictions failed.' ELSE NULL END,updated_at=now()
             WHERE id=$1
            """,
            source["id"],
            json.dumps(result, default=str),
            int(result.get("cache_hits") or 0),
            int(result.get("cache_misses") or 0),
            has_state_failures,
        )
    if has_state_failures:
        return {**result, "_terminal_state": "partial"}
    return result


register_handler("harvest:source", _harvest_job)
