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

Fault simulator (resilience drills, docs/provider-failure-matrix.md):
  CFG["faults"] = [{"match": "/reso", "mode": "...", "rate": 1.0,
                    "retry_after": 30, "delay_ms": 0}, ...]
  modes: timeout | 429 | 500 | 502 | 503 | 401 | 403 | malformed | slow | empty
  Rules apply to every route below (first match by path prefix). "Connection
  refused" needs no rule: point the provider URL at a closed port.
  CFG["rt_fail"]: "" | "refuse" (close before session) | "after_audio"
  (close once caller audio arrives) | "mid_reply" (close half-way through a
  reply) — the realtime voice failure points.
  GET /reso/Property          a RESO OData feed (paged with @odata.nextLink)
                              for MLS drills: MOCK_RESO_LISTINGS rows,
                              "corrupt_every" injects bad records.

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
    # Security testing: a FULLY HIJACKED model. When set to a list of
    # {"name", "arguments"} tool calls, the first round of every chat turn
    # answers with exactly those calls ("{record_id}" is replaced with the
    # selected record's id from the prompt's <record> fence), as a model taken
    # over by injected record text would. The tool layer must refuse what the
    # signed-in user could not do. performance/security/ai_chain_attack.py.
    "llm_hijack_tools": None,
    "faults": [],
    "rt_fail": "",
    "reso_listings": int(os.getenv("MOCK_RESO_LISTINGS", "120")),
    "reso_page": 25,
    "corrupt_every": 0,
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


def _fault_for(path: str):
    for rule in CFG.get("faults") or []:
        if path.startswith(rule.get("match", "/")) and random.random() < float(rule.get("rate", 1.0)):
            return rule
    return None


async def _apply_fault(path: str):
    """Return a Response for an injected fault, or None to proceed."""
    rule = _fault_for(path)
    if rule is None:
        return None
    mode = str(rule.get("mode"))
    STATS.setdefault("faults_injected", {})
    STATS["faults_injected"][mode] = STATS["faults_injected"].get(mode, 0) + 1
    if mode == "timeout":
        await asyncio.sleep(3600)
    if mode == "slow":
        await asyncio.sleep(float(rule.get("delay_ms", 15000)) / 1000)
        return None
    if mode in ("429", "500", "502", "503", "401", "403"):
        headers = {"Retry-After": str(rule["retry_after"])} if rule.get("retry_after") else {}
        return JSONResponse({"error": {"message": f"mock {mode}"}}, status_code=int(mode), headers=headers)
    if mode == "malformed":
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("<html>upstream proxy error</html>{not json", status_code=200)
    if mode == "empty":
        return JSONResponse({}, status_code=200)
    return None


@app.get("/reso/Property")
async def reso_property(request: Request):
    """Minimal RESO Web API Property feed: paged, ordered, optionally corrupt."""
    fault = await _apply_fault("/reso")
    if fault is not None:
        return fault
    skip = int(request.query_params.get("$skip", "0") or 0)
    page = int(CFG["reso_page"])
    total = int(CFG["reso_listings"])
    rows = []
    for i in range(skip, min(total, skip + page)):
        row = {"ListingKey": f"MOCK{i:06d}", "ListingId": f"MOCK{i:06d}", "StandardStatus": "Active",
               "UnparsedAddress": f"{100 + i} Mock St", "City": "Austin", "StateOrProvince": "TX",
               "PostalCode": "78701", "ListPrice": 300000 + i, "BedroomsTotal": 3,
               "BathroomsFull": 2, "LivingArea": 1500, "Latitude": 30.26, "Longitude": -97.74,
               "ModificationTimestamp": f"2026-10-02T00:{(i // 60) % 60:02d}:{i % 60:02d}Z"}
        if CFG["corrupt_every"] and i % int(CFG["corrupt_every"]) == 0:
            row["ListPrice"] = 10 ** 15   # overflows numeric(14,2): the database refuses it
        rows.append(row)
    body = {"value": rows}
    if skip + page < total:
        body["@odata.nextLink"] = f"{request.url.scheme}://{request.url.netloc}/reso/Property?$skip={skip + page}"
    return body


def _count(status: int) -> None:
    STATS["llm_status"][str(status)] = STATS["llm_status"].get(str(status), 0) + 1


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATS["llm_requests"] += 1
    STATS["llm_inflight"] += 1
    STATS["llm_peak_inflight"] = max(STATS["llm_peak_inflight"], STATS["llm_inflight"])
    try:
        fault = await _apply_fault("/v1/chat/completions")
        if fault is not None:
            _count(fault.status_code)
            return fault
        fail = CFG["llm_fail"] or ("500" if random.random() < CFG["llm_fail_rate"] else "")
        if fail == "timeout":
            await asyncio.sleep(3600)
        delay = CFG["llm_latency_ms"] + random.uniform(0, CFG["llm_jitter_ms"])
        await asyncio.sleep(delay / 1000)
        if fail in ("429", "500", "503"):
            _count(int(fail))
            return JSONResponse({"error": {"message": f"mock {fail}", "type": "mock"}}, status_code=int(fail))
        _count(200)
        messages = body.get("messages") or []
        hijack = CFG.get("llm_hijack_tools")
        if hijack and body.get("tools") and not any(m.get("role") == "tool" for m in messages):
            import json as _json
            import re as _re

            prompt = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
            found = _re.search(r'<record>\{.*?"id":\s*"([0-9a-f-]{36})"', prompt)
            record_id = found.group(1) if found else ""
            STATS.setdefault("hijack_rounds", 0)
            STATS["hijack_rounds"] += 1
            calls = [{
                "id": f"call_{i}", "type": "function",
                "function": {"name": c["name"],
                             "arguments": _json.dumps(c.get("arguments") or {}).replace("{record_id}", record_id)},
            } for i, c in enumerate(hijack)]
            return {
                "id": f"mock-{STATS['llm_requests']}", "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model") or "perf-mock",
                "choices": [{"index": 0, "finish_reason": "tool_calls",
                             "message": {"role": "assistant", "content": None, "tool_calls": calls}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 60, "total_tokens": 460},
            }
        last = next((m.get("content") for m in reversed(messages)
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
    if CFG.get("rt_fail") == "refuse":
        await ws.close(code=1011)
        STATS["rt_refused"] = STATS.get("rt_refused", 0) + 1
        return
    await ws.accept()
    STATS["rt_sessions_open"] += 1
    STATS["rt_sessions_total"] += 1
    STATS["rt_sessions_peak"] = max(STATS["rt_sessions_peak"], STATS["rt_sessions_open"])
    heard = 0
    replying: list[asyncio.Task] = []

    async def reply() -> None:
        await asyncio.sleep(CFG["rt_latency_ms"] / 1000)
        await ws.send_text(json.dumps({"type": "response.created"}))
        chunks = max(1, CFG["rt_reply_ms"] // _OUT_CHUNK_MS)
        for n in range(chunks):
            if CFG.get("rt_fail") == "mid_reply" and n == chunks // 2:
                STATS["rt_dropped"] = STATS.get("rt_dropped", 0) + 1
                await ws.close(code=1011)
                return
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
                if CFG.get("rt_fail") == "after_audio":
                    STATS["rt_dropped"] = STATS.get("rt_dropped", 0) + 1
                    await ws.close(code=1011)
                    return
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
