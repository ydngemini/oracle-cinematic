import asyncio
import json
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

import config
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from graph_engine import PropertyGraph
from auth import router as auth_router
from policy_acceptance import router as policy_acceptance_router
from billing import router as billing_router
from audit_ledger import router as audit_router, ledger, AuditCategory
from audit_middleware import AuditMiddleware, audit_action, audit_now, drain_pending
from tenancy import require_context, TenantContext
from admin_c2 import router as admin_c2_router
from rate_limit_middleware import (
    RateLimitMiddleware,
    _init_redis,
    close_redis as close_request_rate_limit_redis,
)
from rate_limiter import close_redis as close_ai_rate_limit_redis
from csrf_middleware import CSRFMiddleware
from client_portal import router as client_portal_router
from voice_intel import (
    comms_router as voice_comms_router,
    router as voice_router,
    start_voice_workers,
    stop_voice_workers,
    voice_session_group,
)
from reconstruction_worker import start_reconstruction_workers, stop_reconstruction_workers
from agent_profile import router as agent_profile_router
from disposition_enforcer import start_disposition_enforcer, stop_disposition_enforcer
from lead_dossier import router as lead_dossier_router
from cma_generator import router as cma_router
from crm import router as crm_router
from client_enterprise import router as client_enterprise_router
from ai_chat_api import router as ai_chat_router, handle_chat_websocket
import ws_hub
from legal_agent import format_for_websocket
from spatial_agent import reconstruct_property, should_trigger_reconstruction
from tour_generator import generate_tour
from workflow_engine import WorkflowEngine
from qwen_voice_agent import QwenVoiceAgent
from outreach_compliance import router as outreach_compliance_router, AI_VOICE_DISCLOSURE
from agent_mind import MindService
from db.connection import init_pool, close_pool, get_pool, pool_stats
from data_integrations.cache import IntegrationCacheUnavailable

logger = logging.getLogger("oracle.server")

# Module-level start time for uptime reporting.
_START_TIME: float = time.monotonic()

# Idle WebSocket timeout — connections that send nothing for this long are
# considered stale and will be closed.  Configurable via env.
_WS_IDLE_TIMEOUT: float = float(os.getenv("ORACLE_WS_IDLE_TIMEOUT", "300"))  # seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Memory Core ignition. Bring the asyncpg pool online BEFORE the server
    starts accepting WebSocket connections, so the first SESSION_RESTORED frame
    can hydrate the operator straight from PostgreSQL.

    A pool failure remains non-fatal only in local development. Production
    cannot provide tenant RLS, durable jobs, mandatory integration caching, or
    cross-replica WebSockets without PostgreSQL, so startup fails closed."""
    config.validate_or_die()  # fail fast in prod if a required secret is missing
    redis_client = await _init_redis()
    if config.flag("ORACLE_REQUIRE_REDIS") and redis_client is None:
        raise RuntimeError("Required distributed Redis service is unavailable")
    await mind_service.start()
    try:
        pool = await init_pool()
        ws_started = await ws_hub.start(pool)
        if not ws_started and not config.IS_DEV:
            raise RuntimeError("cross-replica WebSocket listener did not start")
        logger.info("Memory Core DB pool online — operators will hydrate from PostgreSQL.")
    except Exception as e:  # noqa: BLE001 — boot must survive a DB-less dev run
        if not config.IS_DEV:
            await ws_hub.stop()
            await close_pool()
            raise RuntimeError("Production database initialization failed") from e
        logger.warning("DB pool init failed (%s); running without persistent memory.", e)
    await start_voice_workers()
    await start_reconstruction_workers()
    await start_disposition_enforcer()
    from data_integrations.periodic import start_periodic_scheduler, stop_periodic_scheduler
    from automation_jobs import start_job_workers, stop_job_workers
    # Importing periodic registers every job handler before workers can claim a
    # pre-existing row left by an earlier deployment.
    await start_job_workers()
    await start_periodic_scheduler()
    # AWS observability broadcaster is opt-in: it polls Cost Explorer (billed
    # per call) and the infra APIs. Off unless AWS_OBSERVABILITY_ENABLED is set;
    # even when enabled, the loop only calls AWS while a client is connected.
    metrics_task = None
    if os.environ.get("AWS_OBSERVABILITY_ENABLED", "").lower() in {"1", "true", "yes"}:
        from aws_observability import broadcast_metrics
        metrics_task = asyncio.create_task(broadcast_metrics())
    else:
        logger.info(
            "AWS observability broadcaster NOT started — set AWS_OBSERVABILITY_ENABLED=1 "
            "to enable the Cost-Explorer/infra metrics poll."
        )
    try:
        yield
    finally:
        if metrics_task is not None:
            metrics_task.cancel()
            try:
                await metrics_task
            except asyncio.CancelledError:
                pass
        await stop_periodic_scheduler()
        await stop_job_workers()
        await stop_disposition_enforcer()
        await stop_voice_workers()
        await stop_reconstruction_workers()
        await drain_pending()
        await mind_service.stop()
        await ws_hub.stop()
        await close_ai_rate_limit_redis()
        await close_request_rate_limit_redis()
        await close_pool()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(IntegrationCacheUnavailable)
async def _integration_cache_dependency_error(
    request: Request, exc: IntegrationCacheUnavailable
):
    logger.error("External request blocked because IntegrationCache is unavailable: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "External data cache is unavailable; upstream request was not attempted.",
            "code": "INTEGRATION_CACHE_UNAVAILABLE",
        },
    )

_ALLOWED_ORIGINS = os.getenv(
    "ORACLE_CORS_ORIGINS",
    # 5173 = main Neoh app; 5174 = AWS observability dashboard (its /auth/login
    # is cross-origin in dev). Prod overrides this via ORACLE_CORS_ORIGINS.
    "http://localhost:3000,http://localhost:5173,http://localhost:5174,"
    "http://127.0.0.1:3000,http://127.0.0.1:5173,http://127.0.0.1:5174",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
    allow_credentials=True,
)

app.add_middleware(AuditMiddleware)
app.add_middleware(RateLimitMiddleware, enabled=not os.environ.get("ORACLE_DISABLE_RATE_LIMIT"))
# Cookie authentication is a built-in browser authentication path, so CSRF
# protection is not optional. CSRFMiddleware protects every unsafe method,
# including JSON requests whose content type would otherwise avoid a browser
# preflight.
app.add_middleware(CSRFMiddleware, enabled=True)


# Baseline security headers on every response. setdefault() so a route can still
# override (e.g. a future HTML page needing a looser CSP). HSTS is inert over
# plain HTTP, so it is safe to always emit.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response

app.include_router(auth_router)
app.include_router(policy_acceptance_router)
app.include_router(billing_router)
app.include_router(audit_router)
if os.environ.get("ORACLE_ENV", "").lower() in {"dev", "development", "local"}:
    app.include_router(admin_c2_router)
else:
    logger.info(
        "admin_c2 chaos router NOT mounted — ORACLE_ENV is not a dev value. "
        "Chaos/load-test endpoints are disabled outside development."
    )
app.include_router(client_portal_router)
app.include_router(voice_router)
app.include_router(voice_comms_router)
app.include_router(agent_profile_router)
app.include_router(lead_dossier_router)
app.include_router(cma_router)
app.include_router(crm_router)
app.include_router(client_enterprise_router)
app.include_router(ai_chat_router)
app.include_router(outreach_compliance_router)
from admin_ops import router as admin_ops_router  # noqa: E402 — late import, matches local router convention
app.include_router(admin_ops_router)

from state_compliance import router as state_compliance_router  # noqa: E402
app.include_router(state_compliance_router)

from data_sources_api import router as data_sources_router  # noqa: E402 — keyless free data (geocode/fema/epa)
app.include_router(data_sources_router)

from mls_portal import router as mls_portal_router  # noqa: E402 — direct authorized MLS/RESO browse
app.include_router(mls_portal_router)

from pipeline_api import router as pipeline_api_router  # noqa: E402 — public-property viewport map
app.include_router(pipeline_api_router)

from media_api import router as media_api_router  # noqa: E402 — 2D image upload/serve (agent JWT + portal token; bytes in media_blobs)
app.include_router(media_api_router)

from tour_api import router as tour_api_router  # noqa: E402 — walkable-tour tier resolver (exterior/photos/360/splat)
app.include_router(tour_api_router)

from aws_observability import router as aws_obs_router  # noqa: E402 — AWS infrastructure observability
app.include_router(aws_obs_router)

# Real-estate intelligence platform surfaces.  Import these before the
# application lifespan starts so their durable-job handlers are registered
# before a worker can lease rows left by a previous deployment.
from commands_api import configure_command_mind_service, router as commands_router  # noqa: E402
from contracts_api import router as contracts_router  # noqa: E402
from govinfo_mcp import router as govinfo_mcp_router  # noqa: E402 — official GPO federal-source bridge
from harvests_api import router as harvests_router  # noqa: E402
from intelligence_api import router as intelligence_router  # noqa: E402
from marketplace_api import router as marketplace_router  # noqa: E402
from models_api import router as models_router  # noqa: E402
from portfolio_api import router as portfolio_router  # noqa: E402
from spatial_intelligence_api import router as spatial_intelligence_router  # noqa: E402

app.include_router(commands_router)
app.include_router(contracts_router)
app.include_router(govinfo_mcp_router)
app.include_router(harvests_router)
app.include_router(intelligence_router)
app.include_router(marketplace_router)
app.include_router(models_router)
app.include_router(portfolio_router)
app.include_router(spatial_intelligence_router)

from apis.geocoding import geocode, reverse_geocode
from apis.census import get_demographics_by_zip
from apis.property_data import enrich_property, get_flood_zone
from apis.market_data import get_market_snapshot

from fastapi.staticfiles import StaticFiles
from pathlib import Path

# Serve the same directory spatial_agent writes to — it owns the env override
# and the container-safe fallback (ORACLE_SPLAT_DIR / /tmp/oracle_splats).
from spatial_agent import SPLAT_OUTPUT_DIR
app.mount("/public/splats", StaticFiles(directory=str(SPLAT_OUTPUT_DIR)), name="splats")


@app.get("/health")
async def health() -> JSONResponse:
    """Readiness probe: reports process uptime and verifies PostgreSQL.

    Returns HTTP 200 only after a checked-out connection answers ``SELECT 1``;
    a pool object alone is not proof that its sockets or database are healthy.
    Returns HTTP 503 when the pool is absent or the bounded ping fails. /health is
    intentionally not behind any auth middleware so load-balancers can reach it
    without credentials."""
    uptime_s = time.monotonic() - _START_TIME
    stats = pool_stats()
    pool = get_pool()
    db_ok = False
    db_error = "pool not initialised"
    if pool is not None:
        try:
            async with asyncio.timeout(2.0):
                async with pool.acquire() as conn:
                    db_ok = await conn.fetchval("SELECT 1") == 1
            db_error = "database ping returned an unexpected result"
        except Exception:  # noqa: BLE001 — health body must not expose internals
            logger.warning("Readiness database ping failed", exc_info=True)
            db_error = "database ping failed"

    body = {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(uptime_s, 1),
        "db": {**stats, "reachable": True} if db_ok else {**stats, "reachable": False, "error": db_error},
    }
    status_code = 200 if db_ok else 503
    return JSONResponse(content=body, status_code=status_code)


_tour_gen_timestamps: list[float] = []
_TOUR_GEN_RATE_LIMIT = int(os.getenv("ORACLE_TOUR_RATE_LIMIT", "10"))  # per minute
_TOUR_GEN_WINDOW = 60.0


@app.post("/api/generate-tour")
@audit_action(AuditCategory.GENERATE_TOUR, "AI tour generation: {path}")
async def api_generate_tour(
    request: Request, ctx: TenantContext = Depends(require_context)
) -> JSONResponse:
    """AI-generate a full spatial tour schema from property metadata.

    Accepts: { address, sqft, bedrooms, bathrooms, features[], description, price }
    Returns: Complete tour schema (waypoints, poses, floor plan, narrations)
    """
    now = time.monotonic()
    _tour_gen_timestamps[:] = [t for t in _tour_gen_timestamps if now - t < _TOUR_GEN_WINDOW]
    if len(_tour_gen_timestamps) >= _TOUR_GEN_RATE_LIMIT:
        return JSONResponse({"error": "rate limit exceeded — try again shortly"}, status_code=429)
    _tour_gen_timestamps.append(now)

    body = await request.json()
    if not body.get("address") and not body.get("description"):
        return JSONResponse({"error": "address or description required"}, status_code=400)
    result = await generate_tour(body)
    if result is None:
        return JSONResponse({"error": "tour generation failed"}, status_code=502)
    return JSONResponse(result)


@app.get("/api/geocode")
async def api_geocode(address: str, ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Geocode an address to lat/lng coordinates (free, via OpenStreetMap)."""
    result = await geocode(address)
    if not result:
        return JSONResponse({"error": "address not found"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/reverse-geocode")
async def api_reverse_geocode(lat: float, lng: float, ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Reverse geocode coordinates to an address."""
    result = await reverse_geocode(lat, lng)
    if not result:
        return JSONResponse({"error": "location not found"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/demographics/{zip_code}")
async def api_demographics(zip_code: str, ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Get housing demographics for a ZIP code (US Census ACS 5-year)."""
    if not zip_code.isdigit() or len(zip_code) != 5:
        return JSONResponse({"error": "invalid zip code"}, status_code=400)
    result = await get_demographics_by_zip(zip_code)
    if not result:
        return JSONResponse({"error": "no data for this zip"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/enrich-property")
async def api_enrich_property(address: str, lat: float, lng: float, ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Enrich a property with flood zone, POIs, and walkscore (parallel)."""
    result = await enrich_property(address, lat, lng)
    return JSONResponse(result)


@app.get("/api/flood-zone")
async def api_flood_zone(lat: float, lng: float, ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Check FEMA flood zone for coordinates."""
    result = await get_flood_zone(lat, lng)
    if not result:
        return JSONResponse({"error": "flood data unavailable"}, status_code=502)
    return JSONResponse(result)


@app.get("/api/market-snapshot")
async def api_market_snapshot(ctx: TenantContext = Depends(require_context)) -> JSONResponse:
    """Current mortgage rates and treasury yields."""
    result = await get_market_snapshot()
    return JSONResponse(result)


graph = PropertyGraph()
mind_service = MindService()
configure_command_mind_service(mind_service)


async def monologue_loop(websocket: WebSocket):
    """Continuously streams agent inner thoughts to the Walker speech bubble."""
    agent_cycle = ["SCOUT", "ANALYST", "CLOSER", "LEGAL"]
    idx = 0

    while True:
        await asyncio.sleep(6.0)
        agent_id = agent_cycle[idx % len(agent_cycle)]
        idx += 1

        tokens_sent = 0
        async for token in mind_service.stream_monologue(agent_id):
            if tokens_sent == 0:
                await websocket.send_text(json.dumps({
                    "type": "AGENT_THOUGHT",
                    "agent": agent_id,
                    "mode": "start",
                    "token": token,
                }))
            else:
                await websocket.send_text(json.dumps({
                    "type": "AGENT_THOUGHT",
                    "agent": agent_id,
                    "mode": "stream",
                    "token": token,
                }))
            tokens_sent += 1

        if tokens_sent > 0:
            await websocket.send_text(json.dumps({
                "type": "AGENT_THOUGHT",
                "agent": agent_id,
                "mode": "end",
                "token": "",
            }))


async def restore_session(websocket: WebSocket, user_id: str, tenant_id: str):
    """JIT memory hydration — pull the operator's profile from the Memory Core
    and push a SESSION_RESTORED frame so the C2 UI loads their MAO threshold and
    context summary the instant they connect.

    Degrades gracefully: if no user_id is supplied, or the DB pool isn't wired,
    or the user is unknown, we still emit SESSION_RESTORED with safe defaults and
    restored=False so the frontend can show a fallback rather than hang."""
    payload = {
        "type": "SESSION_RESTORED",
        "user_id": user_id,
        "restored": False,
        "mao_threshold": 0.70,
        "summary": "",
        "markets": [],
        "recent": [],
    }

    if user_id:
        try:
            from memory_core.session_manager import SessionManager
            from tenancy import TenantContext, Role

            ctx = TenantContext(agent_id=user_id, tenant_id=tenant_id, role=Role.AGENT)
            context = await SessionManager(ctx).get_user_context(user_id)
            params = context["parameters"]
            payload.update(
                {
                    # restored reflects whether a real profile row existed — NOT
                    # merely that the lookup didn't throw. An unknown user yields
                    # defaulted params with found=False, so the UI shows the
                    # fallback rather than a false 'MEMORY SYNC: ACTIVE'.
                    "restored": context["found"],
                    "mao_threshold": params["target_mao_pct"],
                    "summary": params["profile_summary"],
                    "markets": params["target_markets"],
                    "recent": context["recent_interactions"],
                }
            )
        except Exception as e:
            # No DB / unknown user / import gap — keep defaults, never crash the WS.
            payload["error"] = str(e)[:160]

    await websocket.send_text(json.dumps(payload))


def _websocket_token(websocket: WebSocket) -> tuple[str, bool]:
    """Return ``(token, used_subprotocol)`` without logging credential material."""
    offered = [
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    # Accept 'oracle.jwt' at any position in the protocol list.
    # The token follows 'oracle.jwt' in the list.
    try:
        oracle_idx = offered.index("oracle.jwt")
        if oracle_idx + 1 < len(offered):
            return offered[oracle_idx + 1], True
    except ValueError:
        pass
    cookie_token = getattr(websocket, "cookies", {}).get("oracle_session", "")
    if cookie_token:
        return cookie_token, False
    # Query-string credentials are permitted only for local compatibility.
    # Production JWTs must never enter browser history or ordinary access logs.
    from config import IS_DEV

    return (websocket.query_params.get("token", ""), False) if IS_DEV else ("", False)


def _resolve_websocket_identity(websocket: WebSocket) -> tuple[TenantContext, bool] | None:
    """Authenticate before upgrade; tokenless identity exists in dev only."""
    from config import IS_DEV
    from tenancy import Role

    raw_token, used_subprotocol = _websocket_token(websocket)
    if raw_token:
        try:
            from tenancy import require_context

            ctx = require_context(f"Bearer {raw_token}")
        except Exception:  # noqa: BLE001 - normalize all token failures to 4401
            logger.warning("Main WebSocket rejected a missing/invalid/expired token.")
            return None
        if ctx.tenant_id == ws_hub.FIREHOSE_TENANT_ID and ctx.role is not Role.PLATFORM_ADMIN:
            logger.warning("Main WebSocket rejected non-admin access to the platform firehose.")
            return None
        return ctx, used_subprotocol

    if not IS_DEV:
        logger.warning("Main WebSocket rejected a tokenless production connection.")
        return None

    claimed = websocket.query_params.get("tenant_id", "")
    if claimed == ws_hub.FIREHOSE_TENANT_ID:
        claimed = ""
    tenant_id = claimed or os.getenv(
        "ORACLE_DEMO_TENANT_ID", "00000000-0000-0000-0000-000000000001"
    )
    user_id = websocket.query_params.get("user_id", "demo-operator")[:128]
    return TenantContext(agent_id=user_id, tenant_id=tenant_id, role=Role.AGENT), False


async def push_deal_pipeline(
    websocket: WebSocket, ctx: TenantContext, request: dict | None = None
):
    """Send one authenticated, cursor-paginated page of acquisition leads."""
    from db.connection import tenant_tx
    from lead_pipeline import (
        encode_cursor,
        freshness_status,
        location_confidence,
        parse_request,
        priority_factors,
        scope_class,
        source_record_refreshed_at,
    )
    from mls_enrichment import MLS_OVERLAY_SELECT, clean_mls_overlay

    options = parse_request(request)
    payload = {
        "type": "DEAL_PIPELINE",
        "states": [],
        "total": 0,
        "next_cursor": None,
        "has_more": False,
        "append": bool(options["cursor"]),
        "filters": {key: value for key, value in options.items() if key != "cursor"},
        "market_coverage": {},
    }
    where = """
        WHERE ($1::text IS NULL OR state=$1)
          AND ($2='all' OR
               ($2='statewide' AND payload->'provenance'->>'coverage_scope'='statewide' AND state<>'VA') OR
               ($2='county' AND payload->'provenance'->>'coverage_scope' LIKE 'county:%') OR
               ($2='city' AND payload->'provenance'->>'coverage_scope' LIKE 'city:%') OR
               ($2='geometry_only' AND state='VA'))
          AND ($3='all' OR COALESCE(payload->'data_quality'->>'detail_level','legacy')=$3)
          AND ($4='all' OR
               ($4='fresh' AND COALESCE(
                    CASE WHEN payload->'provenance'->>'record_refreshed_at' ~
                        '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?(Z|[+-]\\d{2}:\\d{2})$'
                         THEN (payload->'provenance'->>'record_refreshed_at')::timestamptz END,
                    updated_at) >= now()-interval '45 days') OR
               ($4='verify' AND COALESCE(
                    CASE WHEN payload->'provenance'->>'record_refreshed_at' ~
                        '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?(Z|[+-]\\d{2}:\\d{2})$'
                         THEN (payload->'provenance'->>'record_refreshed_at')::timestamptz END,
                    updated_at) < now()-interval '45 days))
          AND ($5='all' OR
               ($5='hot' AND motivation_score>=70) OR
               ($5='contract' AND contract_expires_at IS NOT NULL AND contract_expires_at>now()) OR
               ($5='distress' AND (
                    COALESCE(payload->>'is_absentee_owner','false')='true'
                    OR COALESCE(jsonb_array_length(payload->'distress_flags'),0)>0)))
          AND ($6::text IS NULL OR concat_ws(' ',parcel_id,state,payload->>'address',
               payload->>'city',payload->>'owner_name',payload->>'land_use',
               payload->>'zoning_district',payload->>'distress_flags') ILIKE '%' || $6 || '%')
          AND ($7='all' OR
               ($7='source_coordinate' AND payload->>'latitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                AND payload->>'longitude' ~ '^-?[0-9]+(\\.[0-9]+)?$') OR
               ($7='address_approximation' AND COALESCE(payload->>'address','')<>''
                AND NOT (payload->>'latitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                         AND payload->>'longitude' ~ '^-?[0-9]+(\\.[0-9]+)?$')) OR
               ($7='unmapped' AND COALESCE(payload->>'address','')=''
                AND NOT (payload->>'latitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                         AND payload->>'longitude' ~ '^-?[0-9]+(\\.[0-9]+)?$')))
    """
    cursor = options["cursor"]
    cursor_where = """
          AND ($8::int IS NULL OR motivation_score<$8
               OR (motivation_score=$8 AND updated_at<$9::timestamptz)
               OR (motivation_score=$8 AND updated_at=$9::timestamptz AND id<$10::uuid))
    """
    filter_args = [
        options["state"], options["scope"], options["detail"], options["freshness"],
        options["priority"], options["query"], options["map_confidence"],
    ]

    try:
        async with tenant_tx(ctx) as conn:
            total = await conn.fetchval("SELECT count(*) FROM leads " + where, *filter_args)
            rows = await conn.fetch(
                "SELECT id, parcel_id, state, motivation_score, underwriting, payload, updated_at, "
                "       dossier_status, contract_expires_at, " + MLS_OVERLAY_SELECT + " FROM leads "
                + where + cursor_where
                + " ORDER BY motivation_score DESC, updated_at DESC, id DESC LIMIT $11",
                *filter_args,
                cursor[0] if cursor else None,
                cursor[1] if cursor else None,
                cursor[2] if cursor else None,
                options["limit"] + 1,
            )
            source_rows = await conn.fetch(
                """
                SELECT jurisdiction,health_status,last_health_checked_at
                  FROM harvest_sources
                 WHERE tenant_id=$1::uuid AND source_key LIKE 'regional_parcels_%'
                """,
                ctx.tenant_id,
            )

        from data_coverage import LIVE_PROPERTY, US_JURISDICTIONS, GEOMETRY_ONLY

        health_by_state = {str(row["jurisdiction"]): row for row in source_rows}
        payload["market_coverage"] = {
            code: {
                "scope": source[2] if source else None,
                "scope_class": scope_class(source[2] if source else None, geometry_only=code in GEOMETRY_ONLY),
                "geometry_only": code in GEOMETRY_ONLY,
                "source_name": source[0] if source else None,
                "health": str(health_by_state[code]["health_status"]) if code in health_by_state else "unknown",
            }
            for code in US_JURISDICTIONS
            for source in [LIVE_PROPERTY.get(code)]
        }

        has_more = len(rows) > options["limit"]
        page_rows = rows[:options["limit"]]
        grouped: dict[str, list] = {}
        for r in page_rows:
            prop = _loads(r["payload"])
            under = _loads(r["underwriting"])
            provenance = prop.get("provenance") if isinstance(prop.get("provenance"), dict) else {}
            data_quality = prop.get("data_quality") if isinstance(prop.get("data_quality"), dict) else {}
            market = payload["market_coverage"].get(r["state"], {})
            source_refreshed_at = source_record_refreshed_at(prop, r["updated_at"])
            record_freshness = freshness_status(source_refreshed_at)
            location = location_confidence(prop)
            grouped.setdefault(r["state"], []).append({
                "id": str(r["id"]),  # DB uuid — the PipelineBoard mutation key
                "parcel_id": r["parcel_id"],
                "address": prop.get("address", ""),
                "city": prop.get("city", ""),
                "owner_name": prop.get("owner_name", ""),
                "owner_type": prop.get("owner_type", "individual"),
                "estimated_value": prop.get("estimated_value") or under.get("estimated_value", 0),
                "distress_flags": prop.get("distress_flags", []),
                "is_absentee_owner": prop.get("is_absentee_owner", False),
                # Compact public-record detail for cards/maps.  The full,
                # source-backed fact set is loaded on demand in the dossier.
                "zip_code": prop.get("zip_code") or "",
                "land_use": prop.get("land_use") or None,
                "zoning_district": prop.get("zoning_district") or None,
                "reported_record_date": prop.get("last_sale_date") or None,
                "source_name": provenance.get("source_name") or prop.get("source") or None,
                "source_scope": provenance.get("coverage_scope") or None,
                "scope_class": market.get("scope_class", "unknown"),
                # Card freshness describes this record, not the regional
                # connector's last health heartbeat. Keep the latter separate
                # so a valid listing is never mislabeled "verify source"
                # solely because a coverage monitor has not reported yet.
                "source_health": record_freshness,
                "market_health": market.get("health", "unknown"),
                "detail_level": data_quality.get("detail_level") or "legacy",
                "record_refreshed_at": source_refreshed_at.isoformat() if source_refreshed_at else None,
                "record_freshness": record_freshness,
                "location_confidence": location,
                "priority_factors": priority_factors(prop, r["motivation_score"]),
                "verification_required": data_quality.get("verification_required") is not False,
                "mls_overlay": clean_mls_overlay(r.get("mls_overlay")),
                "motivation_score": r["motivation_score"],
                # Prefer source coordinates on the map; client geocoding is a
                # bounded fallback for records that do not carry them yet.
                "latitude": _payload_coordinate(prop, ("latitude", "lat"), -90, 90),
                "longitude": _payload_coordinate(prop, ("longitude", "lng", "lon"), -180, 180),
                # Contract clock (0007) — drives the CountdownChip on lead cards.
                "dossier_status": r["dossier_status"],
                "contract_expires_at": r["contract_expires_at"].isoformat()
                    if r["contract_expires_at"] else None,
            })

        payload["states"] = [
            {"state": st, "count": len(leads), "leads": leads}
            for st, leads in grouped.items()
        ]
        payload["total"] = int(total or 0)
        payload["has_more"] = has_more
        if has_more and page_rows:
            tail = page_rows[-1]
            payload["next_cursor"] = encode_cursor(
                int(tail["motivation_score"] or 0), tail["updated_at"], str(tail["id"])
            )
    except Exception as e:  # noqa: BLE001 — pipeline is best-effort, never fatal
        payload["error"] = str(e)[:160]

    await websocket.send_text(json.dumps(payload))


def _loads(value):
    """asyncpg can hand back a jsonb column as a raw JSON string OR a decoded
    dict depending on codecs — normalize to a dict either way."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return value or {}


def _payload_coordinate(payload, names, minimum, maximum):
    """Return a valid coordinate from JSON payload aliases, otherwise None."""
    for name in names:
        value = payload.get(name)
        if value in (None, ""):
            continue
        try:
            coordinate = float(value)
        except (TypeError, ValueError):
            continue
        if minimum <= coordinate <= maximum:
            return coordinate
    return None


@app.websocket("/ws/voice-telemetry/{session_id}")
async def voice_telemetry_websocket(websocket: WebSocket, session_id: str):
    """Private session stream; identity and session tenancy are checked pre-upgrade."""
    from config import IS_DEV

    allowed_origins = {origin.strip() for origin in _ALLOWED_ORIGINS if origin.strip()}
    origin = websocket.headers.get("origin", "")
    if not IS_DEV and origin not in allowed_origins:
        await websocket.close(code=4403)
        return
    identity = _resolve_websocket_identity(websocket)
    if identity is None:
        await websocket.close(code=4401)
        return
    ctx, used_subprotocol = identity
    try:
        session_uuid = str(uuid.UUID(session_id))
    except (ValueError, TypeError):
        await websocket.close(code=4404)
        return

    try:
        from db.connection import tenant_tx

        async with tenant_tx(ctx) as conn:
            session = await conn.fetchrow(
                """
                SELECT id,consent_recorded,transcript_status,started_at
                  FROM live_call_sessions
                 WHERE id=$1::uuid
                """,
                session_uuid,
            )
            if session is None:
                await websocket.close(code=4404)
                return
            events = await conn.fetch(
                """
                SELECT id,event_type,transcript_excerpt,counter_offer,mao,
                       threshold,payload,created_at
                  FROM negotiation_events
                 WHERE call_session_id=$1::uuid
                 ORDER BY created_at DESC
                 LIMIT 100
                """,
                session_uuid,
            )
    except Exception:
        logger.exception("Voice telemetry WebSocket admission failed session=%s", session_uuid)
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol="oracle.jwt" if used_subprotocol else None)
    group = voice_session_group(ctx.tenant_id, session_uuid)
    ws_hub.register(group, websocket)
    try:
        await websocket.send_json(
            {
                "type": "VOICE_SESSION_HYDRATED",
                "version": 1,
                "session_id": session_uuid,
                "consent_recorded": session["consent_recorded"],
                "transcript_status": session["transcript_status"],
                "events": [
                    {
                        "id": row["id"],
                        "event_type": row["event_type"],
                        "transcript_excerpt": row["transcript_excerpt"],
                        "counter_offer": float(row["counter_offer"]) if row["counter_offer"] is not None else None,
                        "mao": float(row["mao"]) if row["mao"] is not None else None,
                        "threshold": row["threshold"],
                        "payload": _loads(row["payload"]),
                        "created_at": row["created_at"].isoformat(),
                    }
                    for row in reversed(events)
                ],
            }
        )
        while True:
            message = await websocket.receive_text()
            if message == "PING":
                await websocket.send_text("PONG")
    except WebSocketDisconnect:
        pass
    finally:
        ws_hub.unregister(group, websocket)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    from config import IS_DEV

    allowed_origins = {origin.strip() for origin in _ALLOWED_ORIGINS if origin.strip()}
    origin = websocket.headers.get("origin", "")
    if not IS_DEV and origin not in allowed_origins:
        logger.warning("Main WebSocket rejected untrusted origin %r.", origin[:160])
        await websocket.close(code=4403)
        return
    identity = _resolve_websocket_identity(websocket)
    if identity is None:
        await websocket.close(code=4401)
        return
    ctx, used_subprotocol = identity
    await websocket.accept(subprotocol="oracle.jwt" if used_subprotocol else None)

    # Production identity is claim-derived. Query parameters never override it.
    user_id = ctx.agent_id
    tenant_id = ctx.tenant_id
    client_label = f"user={user_id or 'anon'} tenant={tenant_id}"
    logger.info("WebSocket connected — %s", client_label)

    # Join the tenant's broadcast group so background workers (voice intel,
    # future harvest completions) can reach this dashboard. Mirror unregister
    # lives in the finally block below.
    ws_hub.register(tenant_id, websocket, user_id)

    # A browser can close immediately after the upgrade (route changes, test
    # teardown, sleeping mobile tab).  Initial hydration happens before the
    # long-running session guard below, so handle that race here and release
    # the private hub registration instead of surfacing an ASGI error.
    try:
        await restore_session(websocket, user_id, tenant_id)
        await push_deal_pipeline(websocket, ctx)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected during hydration — %s", client_label)
        ws_hub.unregister(tenant_id, websocket, user_id)
        return

    voice_agent = QwenVoiceAgent(websocket=websocket)
    engine = WorkflowEngine(
        websocket=websocket,
        mind_service=mind_service,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    # Background tasks spawned during this session.  We cancel all of them on
    # disconnect so nothing leaks after the client is gone.
    _bg_tasks: set[asyncio.Task] = set()

    def _spawn(coro) -> asyncio.Task:
        """Create, track, and return a background task."""
        task = asyncio.create_task(coro)
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return task

    # Idle-timeout watchdog: track the most recent message receipt time and
    # close the connection if the client goes silent for _WS_IDLE_TIMEOUT seconds.
    _last_seen: dict[str, float] = {"t": time.monotonic()}

    async def idle_watchdog():
        """Ping every 60 s; close if the client has been idle > _WS_IDLE_TIMEOUT."""
        ping_interval = min(60.0, _WS_IDLE_TIMEOUT / 2)
        while True:
            await asyncio.sleep(ping_interval)
            idle_for = time.monotonic() - _last_seen["t"]
            if idle_for >= _WS_IDLE_TIMEOUT:
                logger.warning(
                    "Idle timeout (%.0fs) — closing WebSocket for %s",
                    idle_for,
                    client_label,
                )
                await websocket.close(code=1001)  # 1001 = Going Away
                return
            try:
                await websocket.send_text(json.dumps({"type": "PING"}))
            except Exception:  # noqa: BLE001 — client already gone
                return

    async def listen_for_client_messages():
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                raise  # bubble up to the gather handler
            except Exception as exc:
                logger.warning("WebSocket receive error for %s: %s", client_label, exc)
                raise

            _last_seen["t"] = time.monotonic()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Malformed JSON from %s — ignored (%.120s)", client_label, raw)
                continue

            msg_type = msg.get("type")
            try:
                if msg_type == "WHISPER_INSTRUCT":
                    await voice_agent.handle_whisper_instruct(msg)
                elif msg_type == "AI_CHAT_SEND":
                    try:
                        await handle_chat_websocket(ctx, websocket, msg)
                    except Exception as chat_exc:  # noqa: BLE001 - preserve the shared socket
                        logger.exception("Private AI chat admission failed for %s: %s", client_label, chat_exc)
                        await websocket.send_json({
                            "type": "AI_CHAT_REJECTED", "version": 1,
                            "request_id": msg.get("request_id"),
                            "code": "AI_CHAT_UNAVAILABLE",
                            "message": "The private assistant is temporarily unavailable.",
                        })
                elif msg_type == "REQUEST_DEAL_PIPELINE":
                    await push_deal_pipeline(websocket, ctx, msg)
                elif msg_type == "REQUEST_RECONSTRUCTION":
                    address = msg.get("address", "")
                    if address:
                        _spawn(reconstruct_property(address, websocket))
                elif msg_type == "OBSERVE":
                    mind_service.observe(
                        msg.get("agent", "SCOUT"),
                        msg.get("content", ""),
                        msg.get("importance", 0.5),
                    )
                elif msg_type == "PONG":
                    pass  # client acknowledged our PING; _last_seen already updated
                else:
                    logger.debug("Unhandled message type %r from %s", msg_type, client_label)
            except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
                logger.error(
                    "Error handling message type=%r from %s: %s",
                    msg_type,
                    client_label,
                    exc,
                    exc_info=True,
                )

    try:
        await asyncio.gather(
            engine.start(),
            listen_for_client_messages(),
            monologue_loop(websocket),
            idle_watchdog(),
        )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected — %s", client_label)
    except Exception as exc:
        logger.error("WebSocket session error for %s: %s", client_label, exc, exc_info=True)
    finally:
        ws_hub.unregister(tenant_id, websocket, user_id)
        # Shut down the workflow engine (cancels harvest/analysis/scout/recon tasks).
        try:
            await engine.stop()
        except Exception as _e:  # noqa: BLE001
            logger.debug("engine.stop() raised during cleanup for %s: %s", client_label, _e)
        # Cancel any remaining server-level background tasks spawned during this session.
        if _bg_tasks:
            logger.debug("Cancelling %d background task(s) for %s", len(_bg_tasks), client_label)
            for task in list(_bg_tasks):
                task.cancel()
            await asyncio.gather(*_bg_tasks, return_exceptions=True)
            _bg_tasks.clear()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
