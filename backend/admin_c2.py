"""
Admin C2 — Chaos engineering control plane.

Provides /admin/simulate-surge for load-testing the Oracle pipeline.
Injects synthetic inbound leads, monitors Kubernetes HPA scaling of
Qwen Voice containers, and streams real-time telemetry to the frontend.
"""

import asyncio
import json
import logging
import os
import random
import time
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from admin_ops import require_platform_admin
from tenancy import TenantContext

logger = logging.getLogger("oracle.admin_c2")

router = APIRouter(prefix="/admin", tags=["chaos"])

K8S_API_HOST = os.environ.get("K8S_API_HOST", "https://kubernetes.default.svc")
K8S_TOKEN_PATH = os.environ.get(
    "K8S_TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token"
)
K8S_CA_PATH = os.environ.get(
    "K8S_CA_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)
QWEN_VOICE_DEPLOYMENT = os.environ.get("QWEN_VOICE_DEPLOYMENT", "qwen-voice-agent")
QWEN_VOICE_NAMESPACE = os.environ.get("QWEN_VOICE_NAMESPACE", "oracle")
HPA_NAME = os.environ.get("QWEN_VOICE_HPA", "qwen-voice-agent-hpa")

SURGE_TOTAL = 500
SURGE_WINDOW_SECONDS = 10.0
BATCH_INTERVAL = SURGE_WINDOW_SECONDS / 50  # 50 batches of 10

_surge_active = False
_surge_subscribers: list[WebSocket] = []

DELAWARE_STREETS = [
    "Silverside Rd", "Concord Pike", "Kirkwood Hwy", "Old Baltimore Pike",
    "Capitol Trail", "Limestone Rd", "Paper Mill Rd", "Centerville Rd",
    "Red Lion Rd", "Christiana Rd", "Marsh Rd", "Foulk Rd",
    "Philadelphia Pike", "Governor Printz Blvd", "Naaman's Rd",
    "Lancaster Pike", "Kennett Pike", "Rockland Rd", "Barley Mill Rd",
]

DELAWARE_CITIES = [
    "Wilmington", "Newark", "Bear", "Hockessin", "Middletown",
    "Dover", "Pike Creek", "Claymont", "Elsmere", "New Castle",
]

LIFE_EVENTS = [
    "DIVORCE_FILING", "PROBATE", "NOTICE_OF_DEFAULT",
    "TAX_LIEN", "PRE_FORECLOSURE", "ESTATE_SALE", "BANKRUPTCY",
    None, None, None,
]

OWNER_NAMES = [
    "Patricia Hawkins", "Robert Simmons", "Margaret Ortiz",
    "William DeLuca", "Barbara Kowalski", "James Fontaine",
    "Linda Marchetti", "Thomas Hendricks", "Dorothy Callahan",
    "Richard Ostrowski", "Sandra Whitfield", "Michael Cavanaugh",
    "Karen Pemberton", "Steven Lockhart", "Angela Rousseau",
]


def _generate_surge_lead() -> dict:
    street_num = random.randint(100, 9999)
    street = random.choice(DELAWARE_STREETS)
    city = random.choice(DELAWARE_CITIES)
    address = f"{street_num} {street}, {city}, DE {random.randint(19700, 19899)}"
    market_value = random.randint(140000, 1200000)
    equity_pct = random.randint(10, 95)
    life_event = random.choice(LIFE_EVENTS)

    return {
        "record_type": random.choice(["TAX_ASSESSMENT", "PROBATE_FILING", "COUNTY_LIEN", "FORECLOSURE_NOTICE"]),
        "owner_name": random.choice(OWNER_NAMES),
        "address": address,
        "assessed_value": int(market_value * 0.72),
        "market_value": market_value,
        "sqft": random.randint(900, 5500),
        "bedrooms": random.randint(2, 6),
        "bathrooms": random.randint(1, 4),
        "county": "New Castle",
        "state": "DE",
        "equity_pct": equity_pct,
        "years_owned": random.randint(1, 35),
        "purchase_year": random.randint(1992, 2024),
        "mortgage_balance": int(market_value * (1 - equity_pct / 100)),
        "life_event": life_event,
        "event_date": f"2026-{random.randint(1, 5):02d}-{random.randint(1, 28):02d}",
        "case_number": f"NC-{random.randint(10000, 99999)}" if life_event else None,
        "surge_synthetic": True,
    }


async def _get_k8s_token() -> Optional[str]:
    try:
        with open(K8S_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("K8S_BEARER_TOKEN")


async def _query_hpa_metrics() -> dict:
    """Query Kubernetes HPA status for the Qwen Voice deployment."""
    import httpx

    token = await _get_k8s_token()
    if not token:
        return {"error": "no_k8s_credentials", "replicas": None}

    hpa_url = (
        f"{K8S_API_HOST}/apis/autoscaling/v2/namespaces/"
        f"{QWEN_VOICE_NAMESPACE}/horizontalpodautoscalers/{HPA_NAME}"
    )

    headers = {"Authorization": f"Bearer {token}"}
    verify = K8S_CA_PATH if os.path.exists(K8S_CA_PATH) else False

    try:
        async with httpx.AsyncClient(verify=verify, timeout=5.0) as client:
            resp = await client.get(hpa_url, headers=headers)

        if resp.status_code != 200:
            return {"error": f"hpa_query_failed_{resp.status_code}", "replicas": None}

        hpa = resp.json()
        status = hpa.get("status", {})
        spec = hpa.get("spec", {})

        return {
            "current_replicas": status.get("currentReplicas", 0),
            "desired_replicas": status.get("desiredReplicas", 0),
            "min_replicas": spec.get("minReplicas", 1),
            "max_replicas": spec.get("maxReplicas", 10),
            "conditions": [
                {"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
                for c in status.get("conditions", [])
            ],
            "current_metrics": [
                {
                    "type": m.get("type"),
                    "current": m.get("resource", {}).get("current", {}),
                }
                for m in status.get("currentMetrics", [])
            ],
        }
    except Exception as e:
        logger.warning(f"K8s HPA query failed: {e}")
        return {"error": str(e), "replicas": None}


async def _query_pod_count() -> dict:
    """Get current running pod count for the Qwen Voice deployment."""
    import httpx

    token = await _get_k8s_token()
    if not token:
        return {"running": 0, "pending": 0, "error": "no_k8s_credentials"}

    pods_url = (
        f"{K8S_API_HOST}/api/v1/namespaces/{QWEN_VOICE_NAMESPACE}/pods"
        f"?labelSelector=app={QWEN_VOICE_DEPLOYMENT}"
    )

    headers = {"Authorization": f"Bearer {token}"}
    verify = K8S_CA_PATH if os.path.exists(K8S_CA_PATH) else False

    try:
        async with httpx.AsyncClient(verify=verify, timeout=5.0) as client:
            resp = await client.get(pods_url, headers=headers)

        if resp.status_code != 200:
            return {"running": 0, "pending": 0, "error": f"status_{resp.status_code}"}

        pods = resp.json().get("items", [])
        running = sum(1 for p in pods if p.get("status", {}).get("phase") == "Running")
        pending = sum(1 for p in pods if p.get("status", {}).get("phase") == "Pending")

        return {"running": running, "pending": pending, "total": len(pods)}
    except Exception as e:
        logger.warning(f"K8s pod query failed: {e}")
        return {"running": 0, "pending": 0, "error": str(e)}


async def _broadcast_telemetry(event: dict):
    payload = json.dumps({"type": "SURGE_TELEMETRY", "data": event})
    dead = []
    for ws in _surge_subscribers:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _surge_subscribers.remove(ws)


async def _run_surge(graph, websocket: Optional[WebSocket] = None):
    """Inject 500 synthetic leads over 10 seconds, polling HPA between batches."""
    global _surge_active

    if _surge_active:
        return {"error": "surge_already_active"}

    _surge_active = True
    start_time = time.time()
    injected = 0
    batch_size = SURGE_TOTAL // 50  # 10 leads per batch

    await _broadcast_telemetry({
        "event": "SURGE_START",
        "total_leads": SURGE_TOTAL,
        "window_seconds": SURGE_WINDOW_SECONDS,
        "timestamp": start_time,
    })

    logger.info(f"CHAOS SURGE — injecting {SURGE_TOTAL} leads over {SURGE_WINDOW_SECONDS}s")

    try:
        for batch_idx in range(50):
            leads = [_generate_surge_lead() for _ in range(batch_size)]

            for lead in leads:
                await graph.ingest_public_record(lead)
                injected += 1

            elapsed = time.time() - start_time
            rate = injected / elapsed if elapsed > 0 else 0

            telemetry = {
                "event": "SURGE_BATCH",
                "batch": batch_idx + 1,
                "injected": injected,
                "rate_per_sec": round(rate, 1),
                "elapsed": round(elapsed, 2),
            }

            if batch_idx % 5 == 0:
                hpa_status = await _query_hpa_metrics()
                pod_status = await _query_pod_count()
                telemetry["hpa"] = hpa_status
                telemetry["pods"] = pod_status
                logger.info(
                    f"SURGE batch {batch_idx + 1}/50 — "
                    f"{injected}/{SURGE_TOTAL} injected — "
                    f"pods: {pod_status.get('running', '?')} running, "
                    f"{pod_status.get('pending', '?')} pending"
                )

            await _broadcast_telemetry(telemetry)

            if websocket:
                await websocket.send_text(json.dumps({
                    "type": "STATUS_UPDATE",
                    "agent": f"CHAOS SURGE — {injected}/{SURGE_TOTAL} leads injected ({rate:.0f}/s)",
                }))

            await asyncio.sleep(BATCH_INTERVAL)

        final_hpa = await _query_hpa_metrics()
        final_pods = await _query_pod_count()
        duration = time.time() - start_time

        result = {
            "event": "SURGE_COMPLETE",
            "total_injected": injected,
            "duration_seconds": round(duration, 2),
            "avg_rate": round(injected / duration, 1),
            "hpa_final": final_hpa,
            "pods_final": final_pods,
        }

        await _broadcast_telemetry(result)
        logger.info(f"CHAOS SURGE COMPLETE — {injected} leads in {duration:.1f}s")
        return result

    finally:
        _surge_active = False


@router.post("/simulate-surge")
async def simulate_surge(ctx: TenantContext = Depends(require_platform_admin)):
    """
    Chaos engineering endpoint: injects 500 synthetic inbound leads into the
    workflow pipeline over 10 seconds. Triggers Kubernetes HPA scaling of
    Qwen Voice containers. Frontend watches via /admin/surge-telemetry WS.
    """
    from graph_engine import PropertyGraph

    if _surge_active:
        return {"status": "rejected", "reason": "surge_already_in_progress"}

    graph = PropertyGraph()
    asyncio.create_task(_run_surge(graph))

    return {
        "status": "initiated",
        "leads": SURGE_TOTAL,
        "window_seconds": SURGE_WINDOW_SECONDS,
        "hpa_target": HPA_NAME,
        "deployment": QWEN_VOICE_DEPLOYMENT,
        "namespace": QWEN_VOICE_NAMESPACE,
        "telemetry_ws": "/admin/surge-telemetry",
    }


@router.get("/surge-status")
async def surge_status(ctx: TenantContext = Depends(require_platform_admin)):
    """Current state of the surge and HPA metrics."""
    hpa = await _query_hpa_metrics()
    pods = await _query_pod_count()
    return {
        "surge_active": _surge_active,
        "hpa": hpa,
        "pods": pods,
    }


@router.websocket("/surge-telemetry")
async def surge_telemetry_ws(websocket: WebSocket):
    """Real-time telemetry stream for the frontend to watch HPA scaling.

    Platform-admin only. WebSockets can't carry an Authorization header, so the
    JWT is passed as the ?token= query param (same convention as /ws) and the
    socket is closed with 1008 (policy violation) for missing/invalid tokens or
    any non-platform_admin caller."""
    raw_token = websocket.query_params.get("token", "")
    try:
        from auth import decode_token

        claims = decode_token(raw_token)
    except Exception:  # noqa: BLE001 — missing/invalid/expired token
        await websocket.close(code=1008)
        return
    if claims.get("role") != "platform_admin":
        await websocket.close(code=1008)
        return

    await websocket.accept()
    _surge_subscribers.append(websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _surge_subscribers:
            _surge_subscribers.remove(websocket)
