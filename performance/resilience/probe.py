"""Resilience drill probe: sample Neoh once a second through the balancer.

Runs inside the perf network (performance/resilience/run_drill.sh). Each line
of stdout is one JSON sample:

  t        seconds since the probe started
  live     /live status            (process up; must stay 200 in every drill)
  ready    /health status          (readiness; 503 while the DB is down)
  status   /api/status state       (normalized product status for users)
  crm      GET /api/crm/clients?limit=1 status, and crm_ms its latency
           (a real authenticated CRM read: "can people still work?")

Every request has a hard timeout, so a hung dependency shows up as a timeout
sample, never as a probe that stops reporting. Markers can be written to
/out/drill-marks.jsonl by the driver; analyze.py lines them up with samples.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx

TARGET = os.getenv("TARGET", "http://oracle-perf-lb:8080")
SECONDS = float(os.getenv("PROBE_SECONDS", "120"))
TIMEOUT = float(os.getenv("PROBE_TIMEOUT", "12"))


def _session() -> dict:
    data = json.load(open(os.getenv("SESSIONS_FILE", "/perf/out/sessions.json")))
    return next(s for s in data["sessions"] if s["role"] == "broker_owner")


async def _one(client, method, path, **kw) -> tuple[object, float]:
    t0 = time.monotonic()
    try:
        r = await client.request(method, path, timeout=TIMEOUT, **kw)
        return r.status_code, (time.monotonic() - t0) * 1000
    except httpx.TimeoutException:
        return "timeout", (time.monotonic() - t0) * 1000
    except httpx.HTTPError as exc:
        return type(exc).__name__, (time.monotonic() - t0) * 1000


async def main() -> None:
    session = _session()
    cookies = {"oracle_session": session["token"]}
    headers = {"X-Forwarded-For": "203.0.113.250"}
    start = time.monotonic()
    async with httpx.AsyncClient(base_url=TARGET, cookies=cookies, headers=headers) as client:
        while time.monotonic() - start < SECONDS:
            tick = time.monotonic()
            (live, _), (ready, _), (crm, crm_ms) = await asyncio.gather(
                _one(client, "GET", "/live"), _one(client, "GET", "/health"),
                _one(client, "GET", "/api/crm/clients?limit=1"))
            status_state = None
            try:
                r = await client.get("/api/status", timeout=TIMEOUT)
                status_state = r.json().get("state") if r.status_code == 200 else r.status_code
            except Exception as exc:  # noqa: BLE001
                status_state = type(exc).__name__
            sample = {"t": round(tick - start, 1), "wall": round(time.time(), 1), "live": live,
                      "ready": ready, "crm": crm, "crm_ms": round(crm_ms), "status": status_state}
            print(json.dumps(sample), flush=True)
            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))


if __name__ == "__main__":
    if os.getenv("NEOH_LOAD_TEST_ALLOWED") != "1":
        sys.exit("refusing: NEOH_LOAD_TEST_ALLOWED=1 is required (local perf topology only)")
    asyncio.run(main())
