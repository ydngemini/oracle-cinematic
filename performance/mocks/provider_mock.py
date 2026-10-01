"""Local stand-in for Neoh's model providers during load tests.

Two surfaces, both instrumented so the harness can see what Neoh did to them:

  POST /v1/chat/completions   OpenAI-compatible (the local-LLM tier Neoh calls
                              via ORACLE_LOCAL_LLM_URL). Latency, jitter and a
                              failure mode are configurable at runtime.
  WS   /realtime              DashScope/Qwen-Omni-realtime-shaped socket: it
                              answers session.update, counts appended audio,
                              and after each UTTERANCE_MS of caller audio waits
                              RT_LATENCY_MS then streams one assistant reply.

  GET  /stats                 counters: requests, in-flight now, PEAK in-flight
                              (the number that shows whether Neoh bounds its
                              model concurrency, §28), statuses, open sessions
  POST /config                change latency / failure mode mid-test (§55/§56)
  POST /reset                 zero the counters

Nothing here reaches the internet. It costs nothing, which is the point: the
infrastructure tests must never discover a database limit by spending money.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI()

CFG = {
    "llm_latency_ms": int(os.getenv("MOCK_LLM_LATENCY_MS", "2500")),
    "llm_jitter_ms": int(os.getenv("MOCK_LLM_JITTER_MS", "1500")),
    # "" = healthy; "429" / "500" = always that status; "timeout" = never answer
    "llm_fail": os.getenv("MOCK_LLM_FAIL", ""),
    "llm_fail_rate": float(os.getenv("MOCK_LLM_FAIL_RATE", "0")),
    "rt_latency_ms": int(os.getenv("MOCK_RT_LATENCY_MS", "600")),
    "rt_reply_ms": int(os.getenv("MOCK_RT_REPLY_MS", "1200")),
    "utterance_ms": int(os.getenv("MOCK_RT_UTTERANCE_MS", "2000")),
}
STATS: dict = {}


def _reset() -> None:
    STATS.clear()
    STATS.update({"llm_requests": 0, "llm_inflight": 0, "llm_peak_inflight": 0, "llm_status": {},
                  "rt_sessions_open": 0, "rt_sessions_peak": 0, "rt_sessions_total": 0,
                  "rt_replies": 0, "rt_audio_bytes_in": 0, "started_at": time.time()})


_reset()


@app.get("/stats")
async def stats():
    return {**STATS, "config": CFG}


@app.post("/config")
async def config(request: Request):
    CFG.update({k: v for k, v in (await request.json()).items() if k in CFG})
    return CFG


@app.post("/reset")
async def reset():
    _reset()
    return STATS


def _count(status: int) -> None:
    STATS["llm_status"][str(status)] = STATS["llm_status"].get(str(status), 0) + 1


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATS["llm_requests"] += 1
    STATS["llm_inflight"] += 1
    STATS["llm_peak_inflight"] = max(STATS["llm_peak_inflight"], STATS["llm_inflight"])
    try:
        fail = CFG["llm_fail"] or ("500" if random.random() < CFG["llm_fail_rate"] else "")
        if fail == "timeout":
            await asyncio.sleep(3600)
        delay = CFG["llm_latency_ms"] + random.uniform(0, CFG["llm_jitter_ms"])
        await asyncio.sleep(delay / 1000)
        if fail in ("429", "500", "503"):
            _count(int(fail))
            return JSONResponse({"error": {"message": f"mock {fail}", "type": "mock"}}, status_code=int(fail))
        _count(200)
        last = next((m.get("content") for m in reversed(body.get("messages") or [])
                     if m.get("role") == "user"), "") or ""
        text = ("Here is what I found for your request. This is a load-test reply from the "
                f"provider mock, so no model was called. You asked about: {str(last)[:80]}")
        return {
            "id": f"mock-{STATS['llm_requests']}", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model") or "perf-mock",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 60, "total_tokens": 460},
        }
    finally:
        STATS["llm_inflight"] -= 1


# 16 kHz 16-bit mono in (what the bridge appends), 24 kHz 16-bit mono out.
_IN_BYTES_PER_MS = 32
_OUT_CHUNK_MS = 20
_OUT_CHUNK = base64.b64encode(b"\x00\x01" * (24 * _OUT_CHUNK_MS)).decode()


@app.websocket("/realtime")
async def realtime(ws: WebSocket):
    await ws.accept()
    STATS["rt_sessions_open"] += 1
    STATS["rt_sessions_total"] += 1
    STATS["rt_sessions_peak"] = max(STATS["rt_sessions_peak"], STATS["rt_sessions_open"])
    heard = 0
    replying: list[asyncio.Task] = []

    async def reply() -> None:
        await asyncio.sleep(CFG["rt_latency_ms"] / 1000)
        await ws.send_text(json.dumps({"type": "response.created"}))
        for _ in range(max(1, CFG["rt_reply_ms"] // _OUT_CHUNK_MS)):
            await ws.send_text(json.dumps({"type": "response.audio.delta", "delta": _OUT_CHUNK}))
            await asyncio.sleep(_OUT_CHUNK_MS / 1000)
        await ws.send_text(json.dumps({"type": "response.done"}))
        STATS["rt_replies"] += 1

    try:
        while True:
            event = json.loads(await ws.receive_text())
            kind = event.get("type")
            if kind == "session.update":
                await ws.send_text(json.dumps({"type": "session.updated"}))
            elif kind == "input_audio_buffer.append":
                n = len(base64.b64decode(event.get("audio") or ""))
                heard += n
                STATS["rt_audio_bytes_in"] += n
                if heard >= CFG["utterance_ms"] * _IN_BYTES_PER_MS:
                    heard = 0
                    replying.append(asyncio.create_task(reply()))
            elif kind == "session.finish":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for t in replying:
            t.cancel()
        STATS["rt_sessions_open"] -= 1
