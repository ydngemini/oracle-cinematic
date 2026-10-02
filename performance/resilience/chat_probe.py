"""Send real Neoh chat turns through /ws and record how each one ends.

The question under an AI-provider fault: does the agent get a clear,
product-language answer in bounded time ("Neoh couldn't complete that
response. Your work is saved"), or an endless spinner / a leaked provider
error? One JSON line per turn: outcome, seconds, message.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

import websockets

TARGET = os.getenv("WS_TARGET", "ws://oracle-perf-lb:8080/ws")
TURNS = int(os.getenv("CHAT_TURNS", "3"))
TURN_TIMEOUT = float(os.getenv("CHAT_TURN_TIMEOUT", "240"))


def _session() -> dict:
    data = json.load(open("/perf/out/sessions.json"))
    agents = [s for s in data["sessions"] if s["role"] == "agent"]
    return agents[int(os.getenv("CHAT_AGENT_INDEX", "7")) % len(agents)]


async def one_turn(ws, prompt: str) -> dict:
    rid = str(uuid.uuid4())
    t0 = time.monotonic()
    await ws.send(json.dumps({"type": "AI_CHAT_SEND", "version": 1, "request_id": rid,
                              "content": prompt, "context": None, "attachment_ids": []}))
    accepted = None
    while time.monotonic() - t0 < TURN_TIMEOUT:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=TURN_TIMEOUT)
        except asyncio.TimeoutError:
            break
        if '"PING"' in raw:
            await ws.send('{"type":"PONG"}')
            continue
        if "AI_CHAT_" not in raw:
            continue
        m = json.loads(raw)
        if m.get("request_id") not in (None, rid):
            continue
        kind = m.get("type")
        if kind == "AI_CHAT_ACCEPTED":
            accepted = round(time.monotonic() - t0, 2)
        elif kind in ("AI_CHAT_COMPLETE", "AI_CHAT_ERROR", "AI_CHAT_REJECTED"):
            return {"outcome": kind, "accepted_s": accepted, "seconds": round(time.monotonic() - t0, 1),
                    "code": m.get("code"), "message": (m.get("message") or "")[:160]}
    return {"outcome": "NO_ANSWER", "accepted_s": accepted, "seconds": round(time.monotonic() - t0, 1)}


async def main() -> None:
    s = _session()
    headers = {"Origin": os.getenv("ORIGIN", "http://localhost:5173"),
               "Cookie": f"oracle_session={s['token']}", "X-Forwarded-For": "203.0.113.251"}
    async with websockets.connect(TARGET, additional_headers=headers, open_timeout=10) as ws:
        for i in range(TURNS):
            print(json.dumps(await one_turn(ws, "Summarize my pipeline and what needs attention today.")),
                  flush=True)


if __name__ == "__main__":
    if os.getenv("NEOH_LOAD_TEST_ALLOWED") != "1":
        sys.exit("refusing: local perf topology only")
    asyncio.run(main())
