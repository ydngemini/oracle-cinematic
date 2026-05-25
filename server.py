import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

async def simulate_ai_swarm(websocket: WebSocket):
    """Simulates the multi-agent swarm analyzing a property and calling a lead."""
    
    # --- AGENT 1: THE SCOUT ---
    await asyncio.sleep(1)
    await websocket.send_text(json.dumps({
        "type": "STATUS_UPDATE",
        "agent": "AGENT SCOUT"
    }))
    
    await asyncio.sleep(2)
    await websocket.send_text(json.dumps({
        "type": "DATA_PULLED",
        "data": {
            "address": "124 Matrix Blvd, Bear, DE 19701",
            "squareFootage": 2850,
            "price": 425000,
            "bedrooms": 4,
            "bathrooms": 3,
            "novelty": 94
        }
    }))

    # --- AGENT 2: THE DESIGNER ---
    await asyncio.sleep(2)
    await websocket.send_text(json.dumps({
        "type": "STATUS_UPDATE",
        "agent": "AGENT DESIGNER"
    }))
    
    await asyncio.sleep(2)
    await websocket.send_text(json.dumps({
        "type": "STAGE_PROPERTY"
    }))

    # --- AGENT 3: THE CLOSER ---
    await asyncio.sleep(2)
    await websocket.send_text(json.dumps({
        "type": "STATUS_UPDATE",
        "agent": "AGENT CLOSER"
    }))

    dialogue = [
        {"agent": "AI CLOSER", "text": "Connecting to homeowner via encrypted VOIP..."},
        {"agent": "AI CLOSER", "text": "Call connected. Initializing voice synthesis..."},
        {"agent": "HOMEOWNER", "text": "Hello? Who is this?"},
        {"agent": "AI CLOSER", "text": "Hi there, I'm calling on behalf of Oracle Real Estate. We noticed your property in Bear has seen a massive equity spike. Have you considered fielding an off-market offer?"},
        {"agent": "HOMEOWNER", "text": "Actually... yeah. My husband and I were just talking about downsizing."},
        {"agent": "AI CLOSER", "text": "Understood. Updating lead motivation to 'High'. I can have a senior partner view the property tomorrow at 2 PM. Does that work?"},
        {"agent": "HOMEOWNER", "text": "Yes, 2 PM works perfectly."},
        {"agent": "AI CLOSER", "text": "Appointment locked. Disconnecting."}
    ]

    for line in dialogue:
        await asyncio.sleep(1.8) # Simulate conversational delay
        await websocket.send_text(json.dumps({
            "type": "TRANSCRIPT_LINE",
            "agent": line["agent"],
            "text": line["text"]
        }))

    await asyncio.sleep(1)
    await websocket.send_text(json.dumps({
        "type": "STATUS_UPDATE",
        "agent": "ALL AGENTS IDLE - LEAD SECURED"
    }))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        # Trigger the autonomous simulation as soon as the React frontend connects
        await simulate_ai_swarm(websocket)
        # Keep connection open
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        print("Client disconnected")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
