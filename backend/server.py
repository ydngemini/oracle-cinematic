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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from graph_engine import PropertyGraph
from auth import router as auth_router
from billing import router as billing_router
from audit_ledger import router as audit_router, ledger, AuditCategory
from admin_c2 import router as admin_c2_router
from legal_agent import format_for_websocket
from spatial_agent import reconstruct_property, should_trigger_reconstruction
from workflow_engine import WorkflowEngine
from qwen_voice_agent import QwenVoiceAgent
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
    try:
        await init_pool()
        logger.info("Memory Core DB pool online — operators will hydrate from PostgreSQL.")
    except Exception as e:  # noqa: BLE001 — boot must survive a DB-less dev run
        logger.warning("DB pool init failed (%s); running without persistent memory.", e)
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(audit_router)
app.include_router(admin_c2_router)

from fastapi.staticfiles import StaticFiles
from pathlib import Path

splats_dir = Path(__file__).parent.parent / "oracle-app" / "public" / "splats"
splats_dir.mkdir(parents=True, exist_ok=True)
app.mount("/public/splats", StaticFiles(directory=str(splats_dir)), name="splats")


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


graph = PropertyGraph()
mind_service = MindService()

DELAWARE_STREETS = [
    "Silverside Rd", "Concord Pike", "Kirkwood Hwy", "Old Baltimore Pike",
    "Capitol Trail", "Limestone Rd", "Paper Mill Rd", "Centerville Rd",
    "Red Lion Rd", "Christiana Rd", "Marsh Rd", "Foulk Rd",
    "Philadelphia Pike", "Governor Printz Blvd", "Naaman's Rd",
]

DELAWARE_CITIES = [
    "Wilmington", "Newark", "Bear", "Hockessin", "Middletown",
    "Dover", "Pike Creek", "Claymont", "Elsmere", "New Castle",
]

LIFE_EVENTS = [
    "DIVORCE_FILING", "PROBATE", "NOTICE_OF_DEFAULT",
    "TAX_LIEN", "PRE_FORECLOSURE", None, None, None,
]

OWNER_NAMES = [
    "Patricia Hawkins", "Robert Simmons", "Margaret Ortiz",
    "William DeLuca", "Barbara Kowalski", "James Fontaine",
    "Linda Marchetti", "Thomas Hendricks", "Dorothy Callahan",
    "Richard Ostrowski", "Sandra Whitfield", "Michael Cavanaugh",
]


def generate_mock_record() -> dict:
    street_num = random.randint(100, 9999)
    street = random.choice(DELAWARE_STREETS)
    city = random.choice(DELAWARE_CITIES)
    address = f"{street_num} {street}, {city}, DE {random.randint(19700, 19899)}"

    market_value = random.randint(180000, 850000)
    equity_pct = random.randint(15, 92)
    life_event = random.choice(LIFE_EVENTS)

    return {
        "record_type": random.choice(["TAX_ASSESSMENT", "PROBATE_FILING", "COUNTY_LIEN"]),
        "owner_name": random.choice(OWNER_NAMES),
        "address": address,
        "assessed_value": int(market_value * 0.72),
        "market_value": market_value,
        "sqft": random.randint(1100, 4200),
        "bedrooms": random.randint(2, 6),
        "bathrooms": random.randint(1, 4),
        "county": "New Castle",
        "state": "DE",
        "equity_pct": equity_pct,
        "years_owned": random.randint(2, 28),
        "purchase_year": random.randint(1997, 2022),
        "mortgage_balance": int(market_value * (1 - equity_pct / 100)),
        "life_event": life_event,
        "event_date": f"2026-{random.randint(1,5):02d}-{random.randint(1,28):02d}",
        "case_number": f"NC-{random.randint(10000, 99999)}" if life_event else None,
    }


async def data_ingestion_loop(websocket: WebSocket):
    batch_num = 0
    _loop_tasks: set[asyncio.Task] = set()

    def _spawn_loop_task(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        _loop_tasks.add(task)
        task.add_done_callback(_loop_tasks.discard)
        return task

    while True:
        batch_num += 1
        batch_size = random.randint(3, 8)

        await websocket.send_text(json.dumps({
            "type": "STATUS_UPDATE",
            "agent": f"AGENT SCOUT — ingesting batch #{batch_num} ({batch_size} records)",
        }))

        for _ in range(batch_size):
            record = generate_mock_record()
            await graph.ingest_public_record(record)

        await asyncio.sleep(0.4)

        await websocket.send_text(json.dumps({
            "type": "STATUS_UPDATE",
            "agent": "AGENT ANALYST — scoring novelty across graph",
        }))

        async for hit in graph.calculate_novelty_score():
            await websocket.send_text(json.dumps({
                "type": "STATUS_UPDATE",
                "agent": f"HIGH-PROBABILITY SELLER DETECTED — novelty {hit['novelty_score']}",
            }))

            await asyncio.sleep(0.3)

            await websocket.send_text(json.dumps({
                "type": "DATA_PULLED",
                "data": {
                    "address": hit["address"],
                    "squareFootage": hit["sqft"],
                    "price": hit["market_value"],
                    "bedrooms": hit["bedrooms"],
                    "bathrooms": hit["bathrooms"],
                    "novelty": hit["novelty_score"],
                },
            }))

            await asyncio.sleep(0.5)

            if should_trigger_reconstruction(hit["novelty_score"]):
                _spawn_loop_task(reconstruct_property(hit["address"], websocket))

            await websocket.send_text(json.dumps({
                "type": "STAGE_PROPERTY",
            }))

            await asyncio.sleep(1.0)

            dialogue = [
                {"agent": "AI CLOSER", "text": f"Initiating contact with {hit['owner_name']}..."},
                {"agent": "AI CLOSER", "text": f"Signal: {hit['life_event'].replace('_', ' ').title()} + {hit['equity_pct']}% equity"},
                {"agent": "AI CLOSER", "text": "Call connected. Voice synthesis active."},
            ]

            for line in dialogue:
                await asyncio.sleep(0.8)
                await websocket.send_text(json.dumps({
                    "type": "TRANSCRIPT_LINE",
                    "agent": line["agent"],
                    "text": line["text"],
                }))

            ledger.record(
                AuditCategory.AI_PHONE_CALL,
                actor="AI CLOSER",
                action="outbound_call_transcript",
                payload={"owner": hit["owner_name"], "address": hit["address"], "transcript": dialogue},
            )

            await asyncio.sleep(1.2)
            await websocket.send_text(json.dumps({
                "type": "STATUS_UPDATE",
                "agent": "AGENT LEGAL — generating contract package",
            }))
            await asyncio.sleep(0.6)

            property_data = {
                "address": hit["address"],
                "price": hit["market_value"],
                "owner_name": hit["owner_name"],
            }
            legal_payload = format_for_websocket(property_data, strategy="wholesale")
            await websocket.send_text(legal_payload)

            ledger.record(
                AuditCategory.LEGAL_CONTRACT,
                actor="AGENT LEGAL",
                action="contract_generated",
                payload={"address": hit["address"], "owner": hit["owner_name"], "strategy": "wholesale"},
            )

            break

        interval = random.uniform(4.0, 8.0)
        await websocket.send_text(json.dumps({
            "type": "STATUS_UPDATE",
            "agent": f"AGENT SCOUT — next scan in {interval:.0f}s",
        }))
        await asyncio.sleep(interval)


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
    """Tenant the operator's leads are scoped to. Real deployments pass tenant_id
    from the verified JWT; for a tokenless dev client we fall back to
    ORACLE_DEMO_TENANT_ID (the same tenant the harvesters ingest under) so the
    pipeline actually shows data, then to the harmless 'default' sentinel."""
    return (
        websocket.query_params.get("tenant_id")
        or os.getenv("ORACLE_DEMO_TENANT_ID")
        or "default"
    )


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
                "SELECT parcel_id, state, motivation_score, underwriting, payload "
                "FROM leads ORDER BY state ASC, motivation_score DESC LIMIT 500"
            )

        grouped: dict[str, list] = {}
        for r in rows:
            prop = _loads(r["payload"])
            under = _loads(r["underwriting"])
            grouped.setdefault(r["state"], []).append({
                "parcel_id": r["parcel_id"],
                "address": prop.get("address", ""),
                "city": prop.get("city", ""),
                "owner_name": prop.get("owner_name", ""),
                "owner_type": prop.get("owner_type", "individual"),
                "estimated_value": prop.get("estimated_value") or under.get("estimated_value", 0),
                "distress_flags": prop.get("distress_flags", []),
                "is_absentee_owner": prop.get("is_absentee_owner", False),
                "motivation_score": r["motivation_score"],
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

    await restore_session(websocket, user_id, tenant_id)
    await push_deal_pipeline(websocket, user_id, tenant_id)

    voice_agent = QwenVoiceAgent(websocket=websocket)
    engine = WorkflowEngine(websocket=websocket, mind_service=mind_service)

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
        # Cancel all background tasks spawned during this session.
        if _bg_tasks:
            logger.debug("Cancelling %d background task(s) for %s", len(_bg_tasks), client_label)
            for task in list(_bg_tasks):
                task.cancel()
            await asyncio.gather(*_bg_tasks, return_exceptions=True)
            _bg_tasks.clear()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
