import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from graph_engine import PropertyGraph
from auth import router as auth_router
from billing import router as billing_router
from audit_ledger import router as audit_router, ledger, AuditCategory
from audit_middleware import AuditMiddleware, audit_action, audit_now, drain_pending
from tenancy import require_context, TenantContext
from admin_c2 import router as admin_c2_router
from client_portal import router as client_portal_router
from voice_intel import router as voice_router, start_voice_workers, stop_voice_workers
from reconstruction_worker import start_reconstruction_workers, stop_reconstruction_workers
from agent_profile import router as agent_profile_router
from disposition_enforcer import start_disposition_enforcer, stop_disposition_enforcer
from lead_dossier import router as lead_dossier_router
from cma_generator import router as cma_router
from crm import router as crm_router
from client_enterprise import router as client_enterprise_router
import ws_hub
from legal_agent import format_for_websocket
from spatial_agent import reconstruct_property, should_trigger_reconstruction
from tour_generator import generate_tour
from workflow_engine import WorkflowEngine
from qwen_voice_agent import QwenVoiceAgent
from outreach_compliance import router as outreach_compliance_router, AI_VOICE_DISCLOSURE
from agent_mind import MindService
from db.connection import init_pool, close_pool, pool_stats

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

    A pool failure (e.g. no DB reachable in a frontend-only dev run, or missing
    AWS creds for the Aurora IAM path) is logged but non-fatal: restore_session()
    already degrades to safe defaults, so the server still boots and the UI shows
    'MEMORY SYNC: —' rather than refusing every connection."""
    import config
    config.validate_or_die()  # fail fast in prod if a required secret is missing
    try:
        await init_pool()
        logger.info("Memory Core DB pool online — operators will hydrate from PostgreSQL.")
    except Exception as e:  # noqa: BLE001 — boot must survive a DB-less dev run
        logger.warning("DB pool init failed (%s); running without persistent memory.", e)
    await start_voice_workers()
    await start_reconstruction_workers()
    await start_disposition_enforcer()
    from data_integrations.periodic import start_periodic_scheduler, stop_periodic_scheduler
    await start_periodic_scheduler()
    try:
        yield
    finally:
        await stop_periodic_scheduler()
        await stop_disposition_enforcer()
        await stop_voice_workers()
        await stop_reconstruction_workers()
        # Flush in-flight audit writes BEFORE the pool closes — they need it.
        await drain_pending()
        await close_pool()


app = FastAPI(lifespan=lifespan)

_ALLOWED_ORIGINS = os.getenv("ORACLE_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

app.add_middleware(AuditMiddleware)


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
app.include_router(agent_profile_router)
app.include_router(lead_dossier_router)
app.include_router(cma_router)
app.include_router(crm_router)
app.include_router(client_enterprise_router)
app.include_router(outreach_compliance_router)
from admin_ops import router as admin_ops_router  # noqa: E402 — late import, matches local router convention
app.include_router(admin_ops_router)

from state_compliance import router as state_compliance_router  # noqa: E402
app.include_router(state_compliance_router)

from data_sources_api import router as data_sources_router  # noqa: E402 — keyless free data (geocode/fema/epa)
app.include_router(data_sources_router)

from mls_portal import router as mls_portal_router  # noqa: E402 — retail MLS browse (quota-safe, RentCast→oracle_mls_listings)
app.include_router(mls_portal_router)

from media_api import router as media_api_router  # noqa: E402 — 2D image upload/serve (agent JWT + portal token; bytes in media_blobs)
app.include_router(media_api_router)

from tour_api import router as tour_api_router  # noqa: E402 — walkable-tour tier resolver (exterior/photos/360/splat)
app.include_router(tour_api_router)

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
    """Shallow health probe: reports process uptime and DB pool metrics.

    Returns HTTP 200 when the pool is alive, HTTP 503 when the pool is absent
    (e.g. DB-less dev run or a mid-shutdown state).  The /health path is
    intentionally not behind any auth middleware so load-balancers can reach it
    without credentials."""
    uptime_s = time.monotonic() - _START_TIME
    stats = pool_stats()
    db_ok = bool(stats)

    body = {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(uptime_s, 1),
        "db": stats if db_ok else {"error": "pool not initialised"},
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
async def api_geocode(address: str) -> JSONResponse:
    """Geocode an address to lat/lng coordinates (free, via OpenStreetMap)."""
    result = await geocode(address)
    if not result:
        return JSONResponse({"error": "address not found"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/reverse-geocode")
async def api_reverse_geocode(lat: float, lng: float) -> JSONResponse:
    """Reverse geocode coordinates to an address."""
    result = await reverse_geocode(lat, lng)
    if not result:
        return JSONResponse({"error": "location not found"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/demographics/{zip_code}")
async def api_demographics(zip_code: str) -> JSONResponse:
    """Get housing demographics for a ZIP code (US Census ACS 5-year)."""
    if not zip_code.isdigit() or len(zip_code) != 5:
        return JSONResponse({"error": "invalid zip code"}, status_code=400)
    result = await get_demographics_by_zip(zip_code)
    if not result:
        return JSONResponse({"error": "no data for this zip"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/enrich-property")
async def api_enrich_property(address: str, lat: float, lng: float) -> JSONResponse:
    """Enrich a property with flood zone, POIs, and walkscore (parallel)."""
    result = await enrich_property(address, lat, lng)
    return JSONResponse(result)


@app.get("/api/flood-zone")
async def api_flood_zone(lat: float, lng: float) -> JSONResponse:
    """Check FEMA flood zone for coordinates."""
    result = await get_flood_zone(lat, lng)
    if not result:
        return JSONResponse({"error": "flood data unavailable"}, status_code=502)
    return JSONResponse(result)


@app.get("/api/market-snapshot")
async def api_market_snapshot() -> JSONResponse:
    """Current mortgage rates and treasury yields."""
    result = await get_market_snapshot()
    return JSONResponse(result)


graph = PropertyGraph()
mind_service = MindService()


async def monologue_loop(websocket: WebSocket):
    """Continuously streams agent inner thoughts to the Walker speech bubble."""
    await mind_service.start()

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


def _resolve_tenant(websocket: WebSocket) -> str:
    """Tenant the operator's leads are scoped to.

    Resolution order:
      1. A `token` query param carrying a valid JWT — tenant_id comes from the
         verified claims. This is the ONLY path that can land in the platform
         firehose group (role must be platform_admin).
      2. The legacy `tenant_id` query param (tokenless dev clients), explicitly
         barred from claiming the platform tenant — it is client-asserted.
      3. ORACLE_DEMO_TENANT_ID (the tenant the harvesters ingest under), then
         the harmless 'default' sentinel.
    """
    raw_token = websocket.query_params.get("token", "")
    if raw_token:
        try:
            from auth import decode_token

            claims = decode_token(raw_token)
            claimed = claims.get("tenant_id") or "default"
            if claimed == ws_hub.FIREHOSE_TENANT_ID and claims.get("role") != "platform_admin":
                logger.warning("WS token valid but not platform_admin — demoting from firehose group.")
                return os.getenv("ORACLE_DEMO_TENANT_ID") or "default"
            return claimed
        except Exception:  # noqa: BLE001 — invalid/expired token → dev fallback
            logger.warning("WS token rejected — falling back to dev tenant resolution.")

    claimed = websocket.query_params.get("tenant_id", "")
    if claimed == ws_hub.FIREHOSE_TENANT_ID:
        # Spoof guard: the all-tenant firehose is JWT-gated, never query-param.
        logger.warning("WS client asserted the platform tenant without a token — demoted.")
        claimed = ""
    return claimed or os.getenv("ORACLE_DEMO_TENANT_ID") or "default"


async def push_deal_pipeline(websocket: WebSocket, user_id: str, tenant_id: str):
    """Stream the operator's motivated-seller leads (the 4-State firehose output)
    to the DealPipeline panel, grouped by state and ranked by motivation_score.

    Reads through tenant_tx so RLS scopes the rows to this operator's tenant.
    Degrades to an empty pipeline on any failure (no DB, non-UUID dev tenant,
    import gap) — the panel shows 'Awaiting leads' rather than hanging."""
    payload = {"type": "DEAL_PIPELINE", "states": [], "total": 0}

    try:
        from tenancy import TenantContext, Role
        from db.connection import tenant_tx

        ctx = TenantContext(agent_id=user_id or "demo-operator", tenant_id=tenant_id, role=Role.AGENT)
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                "SELECT id, parcel_id, state, motivation_score, underwriting, payload, "
                "       dossier_status, contract_expires_at "
                "FROM leads ORDER BY state ASC, motivation_score DESC LIMIT 500"
            )

        grouped: dict[str, list] = {}
        for r in rows:
            prop = _loads(r["payload"])
            under = _loads(r["underwriting"])
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
                "motivation_score": r["motivation_score"],
                # Contract clock (0007) — drives the CountdownChip on lead cards.
                "dossier_status": r["dossier_status"],
                "contract_expires_at": r["contract_expires_at"].isoformat()
                    if r["contract_expires_at"] else None,
            })

        payload["states"] = [
            {"state": st, "count": len(leads), "leads": leads}
            for st, leads in sorted(grouped.items())
        ]
        payload["total"] = sum(len(v) for v in grouped.values())
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Operator identity for JIT memory hydration (query params; tenant defaults
    # so a tokenless dev client still connects).
    user_id = websocket.query_params.get("user_id", "")
    tenant_id = _resolve_tenant(websocket)
    client_label = f"user={user_id or 'anon'} tenant={tenant_id}"
    logger.info("WebSocket connected — %s", client_label)

    # Join the tenant's broadcast group so background workers (voice intel,
    # future harvest completions) can reach this dashboard. Mirror unregister
    # lives in the finally block below.
    ws_hub.register(tenant_id, websocket)

    await restore_session(websocket, user_id, tenant_id)
    await push_deal_pipeline(websocket, user_id, tenant_id)

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
                elif msg_type == "REQUEST_DEAL_PIPELINE":
                    await push_deal_pipeline(websocket, user_id, tenant_id)
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
        ws_hub.unregister(tenant_id, websocket)
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
