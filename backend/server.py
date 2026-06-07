import asyncio
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

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
from db.connection import init_pool, close_pool

logger = logging.getLogger("oracle.server")


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
                asyncio.create_task(
                    reconstruct_property(hit["address"], websocket)
                )

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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Operator identity for JIT memory hydration (query params; tenant defaults
    # so a tokenless dev client still connects).
    user_id = websocket.query_params.get("user_id", "")
    tenant_id = websocket.query_params.get("tenant_id", "default")
    await restore_session(websocket, user_id, tenant_id)

    voice_agent = QwenVoiceAgent(websocket=websocket)
    engine = WorkflowEngine(websocket=websocket, mind_service=mind_service)

    async def listen_for_client_messages():
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "WHISPER_INSTRUCT":
                await voice_agent.handle_whisper_instruct(msg)
            elif msg.get("type") == "OBSERVE":
                mind_service.observe(
                    msg.get("agent", "SCOUT"),
                    msg.get("content", ""),
                    msg.get("importance", 0.5),
                )

    try:
        await asyncio.gather(
            engine.start(),
            listen_for_client_messages(),
            monologue_loop(websocket),
        )
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
