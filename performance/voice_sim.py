#!/usr/bin/env python3
"""§31/§32 VOICE SESSION SIMULATOR — the real media path, no real call.

Each simulated call does what Plivo does for a live call, against the
PRODUCTION endpoint (/api/commands/media/plivo on the API replicas):

  1. call state is initialized in Valkey and a bridge token minted — with the
     same functions the outbound-call path uses (plivo_call_handler), under
     the perf environment's own keys;
  2. the media WebSocket opens and sends Plivo's `start` frame;
  3. 8 kHz μ-law frames stream every 20 ms for CALL_SECONDS;
  4. the server bridges each call to its own realtime session — here the
     provider mock (DASHSCOPE_REALTIME_URL), which answers each 2 s of caller
     audio after RT_LATENCY with ~1.2 s of assistant audio;
  5. Neoh's audio comes back as `playAudio` frames.

Measured per call: setup (socket open → bridge ready = first frame accepted),
turn latency (caller audio complete → first playAudio; subtract the mock's
configured think time to get Neoh's own share), frames sent on schedule, and
failures (close codes). Concurrency levels: --calls 1 5 10 25 50 100.

Runs inside a backend-image container on the perf network, with the perf env:
  python /perf/voice_sim.py --calls 25 --seconds 30
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, "/app")

TARGET = os.environ.get("PERF_WS_TARGET", "ws://oracle-perf-lb:8080")
FRAME = base64.b64encode(b"\xff" * 160).decode()  # 20 ms of μ-law silence
UTTERANCE_FRAMES = 100                               # mock replies per 2 s of audio


def pct(v, p):
    if not v:
        return None
    s = sorted(v)
    return round(s[max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))], 1)


async def one_call(n: int, seconds: float, tenant_id: str, stats: dict) -> None:
    import websockets
    from plivo_call_handler import create_plivo_bridge_token, initialize_outbound_plivo_call_state

    call_uuid = str(uuid.uuid4())
    stream_id = str(uuid.uuid4())
    await initialize_outbound_plivo_call_state(call_uuid, "+15550000000", tenant_id=tenant_id,
                                               account_id=os.environ.get("PLIVO_AUTH_ID", "MAPERF"))
    token = create_plivo_bridge_token(call_uuid)
    t_open = time.perf_counter()
    marks: list[float] = []      # when each utterance's audio finished
    firsts: list[float] = []     # first playAudio after each mark
    waiting = [False]
    late_frames = 0
    try:
        async with websockets.connect(f"{TARGET}/api/commands/media/plivo?bridge_token={token}",
                                      open_timeout=15, max_size=4 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"event": "start", "start": {
                "callId": call_uuid, "streamId": stream_id,
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}))
            stats["setup_ms"].append((time.perf_counter() - t_open) * 1000)

            async def reader():
                async for raw in ws:
                    if '"playAudio"' in raw:
                        stats["play_frames"] += 1
                        if waiting[0]:
                            waiting[0] = False
                            firsts.append(time.perf_counter())

            rtask = asyncio.create_task(reader())
            start = time.perf_counter()
            frames = int(seconds * 50)
            for i in range(frames):
                due = start + i * 0.02
                now = time.perf_counter()
                if now < due:
                    await asyncio.sleep(due - now)
                elif now - due > 0.1:
                    late_frames += 1
                await ws.send(json.dumps({"event": "media", "streamId": stream_id, "media": {"payload": FRAME}}))
                if (i + 1) % UTTERANCE_FRAMES == 0:
                    marks.append(time.perf_counter())
                    waiting[0] = True
            await asyncio.sleep(3)  # let the last reply land
            await ws.send(json.dumps({"event": "stop", "streamId": stream_id}))
            rtask.cancel()
        stats["ok"] += 1
    except websockets.exceptions.ConnectionClosed as exc:
        stats["closed"].append(getattr(exc, "code", None) or str(exc)[:60])
    except Exception as exc:  # noqa: BLE001 — a failed call is a data point
        stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:80]}")
    # Pair each utterance end with the first reply after it.
    for m in marks:
        after = [f for f in firsts if f >= m]
        if after:
            stats["turn_ms"].append((after[0] - m) * 1000)
            firsts.remove(after[0])
    stats["late_frames"] += late_frames
    stats["utterances"] += len(marks)


async def main(a) -> int:
    import asyncpg
    conn = await asyncpg.connect(os.environ["PERF_DB_DSN"])
    tid = str(await conn.fetchval("SELECT id FROM tenants WHERE slug = 'perf-brokerage-01'"))
    await conn.close()
    stats = {"setup_ms": [], "turn_ms": [], "ok": 0, "closed": [], "errors": [], "play_frames": 0,
             "late_frames": 0, "utterances": 0}
    t0 = time.perf_counter()
    # Calls arrive over RAMP seconds, not in one instant (calls do not all
    # connect in the same millisecond); --ramp 0 makes it a burst.
    tasks = []
    for n in range(a.calls):
        tasks.append(asyncio.create_task(one_call(n, a.seconds, tid, stats)))
        if a.ramp:
            await asyncio.sleep(a.ramp / a.calls)
    await asyncio.gather(*tasks)
    out = {
        "calls": a.calls, "call_seconds": a.seconds, "wall_s": round(time.perf_counter() - t0, 1),
        "completed": stats["ok"], "failed": a.calls - stats["ok"],
        "close_codes": stats["closed"][:10], "errors": stats["errors"][:10],
        "setup_ms": {"p50": pct(stats["setup_ms"], 50), "p95": pct(stats["setup_ms"], 95)},
        "turn_ms": {"n": len(stats["turn_ms"]), "p50": pct(stats["turn_ms"], 50),
                    "p95": pct(stats["turn_ms"], 95), "max": pct(stats["turn_ms"], 100)},
        "utterances": stats["utterances"], "replies_heard": len(stats["turn_ms"]),
        "play_frames": stats["play_frames"], "simulator_late_frames": stats["late_frames"],
    }
    print(json.dumps(out))
    return 0 if out["failed"] == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=5)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--ramp", type=float, default=10)
    if os.environ.get("NEOH_LOAD_TEST_ALLOWED") != "1":
        print("REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("DASHSCOPE_REALTIME_URL", "").startswith("ws://oracle-perf-mock"):
        print("REFUSING: the realtime provider is not the local mock", file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(main(ap.parse_args())))
