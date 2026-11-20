"""National data-coverage reporting and harvest task control."""
from __future__ import annotations


from fastapi import Depends, HTTPException, Query, status

from db.connection import tenant_tx
from admin_ops import require_platform_admin
from tenancy import TenantContext, require_context


from ._common import (
    router, _iso,
)

@router.get(
    "/api/market/coverage",
    summary="National data-coverage map (property + compliance per jurisdiction)",
)
async def get_data_coverage(
    summary_only: bool = Query(
        False, description="Return just the rollup totals, not the per-state matrix."
    ),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Report what data the platform holds for every US jurisdiction.

    Three planes: property/parcel (per-state scrapers), compliance (per-state
    statutory rules), and market/demographic (national APIs — no per-state gap).
    Property and compliance status are tracked per jurisdiction so the coverage
    gap stays visible; this is the source of truth behind the ops-console
    coverage view and the next harvest-batch planning.
    """
    from data_coverage import GEOMETRY_ONLY, report_json, summary as coverage_summary
    from lead_pipeline import scope_class

    # Static source scope says what a jurisdiction can publish; this scoped
    # lookup adds the live Azure worker evidence without inventing freshness
    # for an unprobed market.
    async with tenant_tx(ctx) as conn:
        health_rows = await conn.fetch(
            """
            SELECT jurisdiction,health_status,last_health_checked_at
              FROM harvest_sources
             WHERE tenant_id=$1::uuid AND source_key LIKE 'regional_parcels_%'
            """,
            ctx.tenant_id,
        )
    health_by_state = {str(row["jurisdiction"]): row for row in health_rows}
    if summary_only:
        data = coverage_summary()
        states = [str(row["jurisdiction"]) for row in health_rows]
        data["source_health"] = {
            "observed_jurisdictions": len(states),
            "fresh": sum(str(row["health_status"]) == "fresh" for row in health_rows),
            "non_fresh": sum(str(row["health_status"]) != "fresh" for row in health_rows),
        }
        return data

    data = report_json()
    for jurisdiction in data["jurisdictions"]:
        code = jurisdiction["code"]
        property_data = jurisdiction["property"]
        health = health_by_state.get(code)
        property_data["scope_class"] = scope_class(
            property_data.get("scope"), geometry_only=code in GEOMETRY_ONLY
        )
        property_data["lead_ready"] = (
            property_data["status"] == "live" and code not in GEOMETRY_ONLY
        )
        property_data["health_status"] = str(health["health_status"]) if health else "unknown"
        property_data["last_health_checked_at"] = (
            _iso(health["last_health_checked_at"]) if health else None
        )
    return data


@router.get(
    "/api/market/harvest/status",
    summary="Periodic auto-update scheduler status (per-task cadence + last run)",
)
async def get_harvest_status(
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Report the auto-update heartbeat: whether the periodic scheduler is on,
    its tick interval, and every task's cadence / last-run / failure count."""
    from data_integrations.periodic import scheduler

    return scheduler.status()


@router.post(
    "/api/market/harvest/run/{task_name}",
    summary="Manually trigger a periodic harvest task now (platform admin)",
)
async def run_harvest_task(
    task_name: str,
    ctx: TenantContext = Depends(require_platform_admin),
) -> dict:
    """Force a scheduled task to run immediately, outside its cadence. Useful
    for an on-demand refresh of a state/city dataset. Platform-admin only."""
    from data_integrations.periodic import scheduler

    try:
        result = await scheduler.run_now(task_name)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return {"task": task_name, "result": result}

