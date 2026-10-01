#!/usr/bin/env python3
"""A SMALL, capped sample against the real configured model provider (§27).

Every load scenario uses the local provider mock. This is the one place a real
model is called, and it is fenced three ways:

  1. Two opt-ins: NEOH_LOAD_TEST_ALLOWED=1 AND NEOH_REAL_AI_ALLOWED=1.
  2. Hard caps no argument can raise: HARD_MAX_REQUESTS, HARD_MAX_CONCURRENCY,
     and HARD_MAX_USD against a deliberately pessimistic price estimate —
     the request count is cut until the estimate fits.
  3. It calls the provider directly with a short prompt and max_tokens, not
     through Neoh's tool loop, so one "request" is one bounded completion.

Measures: time to first token (streamed), full-response latency, HTTP status
mix (429s = the provider's own limit), and errors. The API key is read from the
environment and never printed or written.

    NEOH_LOAD_TEST_ALLOWED=1 NEOH_REAL_AI_ALLOWED=1 \\
    FIREWORKS_API_KEY=… python3 performance/ai_real_sample.py --requests 12 --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import pathlib
import sys
import time

HARD_MAX_REQUESTS = 30
HARD_MAX_CONCURRENCY = 4
HARD_MAX_USD = 1.00
MAX_TOKENS = 200
PROMPT_TOKENS_EST = 400
# Pessimistic: well above what the configured Fireworks models charge, so the
# real bill can only be smaller than the estimate.
USD_PER_MILLION_TOKENS = 3.00

DEFAULT_URL = "https://api.fireworks.ai/inference/v1/chat/completions"


class CostGuardRefused(Exception):
    pass


def plan(env, requests: int, concurrency: int) -> dict:
    if env.get("NEOH_LOAD_TEST_ALLOWED") != "1" or env.get("NEOH_REAL_AI_ALLOWED") != "1":
        raise CostGuardRefused("real-provider sampling needs NEOH_LOAD_TEST_ALLOWED=1 AND NEOH_REAL_AI_ALLOWED=1")
    requests = max(1, min(int(requests), HARD_MAX_REQUESTS))
    concurrency = max(1, min(int(concurrency), HARD_MAX_CONCURRENCY, requests))
    per_request = (PROMPT_TOKENS_EST + MAX_TOKENS) * USD_PER_MILLION_TOKENS / 1_000_000
    while requests > 1 and requests * per_request > HARD_MAX_USD:
        requests -= 1
    return {"requests": requests, "concurrency": concurrency,
            "estimated_cost_usd": round(requests * per_request, 4), "max_tokens": MAX_TOKENS}


def _pct(v, p):
    if not v:
        return None
    s = sorted(v)
    return round(s[max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))], 1)


async def _one(client, url, key, model, sem, out):
    body = {"model": model, "stream": True, "max_tokens": MAX_TOKENS, "temperature": 0.2,
            "messages": [{"role": "system", "content": "You are Neoh, a concise real-estate assistant."},
                         {"role": "user", "content": "In two sentences, what should an agent check before listing a home?"}]}
    async with sem:
        t0 = time.perf_counter()
        first = None
        status = None
        try:
            async with client.stream("POST", url, json=body,
                                     headers={"Authorization": f"Bearer {key}"}, timeout=60) as resp:
                status = resp.status_code
                async for line in resp.aiter_lines():
                    if first is None and line.startswith("data:") and '"content"' in line:
                        first = time.perf_counter()
            out.append({"status": status, "ttft_ms": (first - t0) * 1000 if first else None,
                        "total_ms": (time.perf_counter() - t0) * 1000})
        except Exception as exc:  # noqa: BLE001 — every failure is a data point
            out.append({"status": status or "error", "error": type(exc).__name__,
                        "total_ms": (time.perf_counter() - t0) * 1000})


async def run(p: dict, url: str, key: str, model: str) -> dict:
    import httpx
    out: list = []
    sem = asyncio.Semaphore(p["concurrency"])
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[_one(client, url, key, model, sem, out) for _ in range(p["requests"])])
    ok = [r for r in out if r.get("status") == 200]
    statuses: dict = {}
    for r in out:
        statuses[str(r.get("status"))] = statuses.get(str(r.get("status")), 0) + 1
    return {
        "plan": p, "provider_url": url, "model": model, "statuses": statuses,
        "ttft_ms": {"p50": _pct([r["ttft_ms"] for r in ok if r.get("ttft_ms")], 50),
                    "p95": _pct([r["ttft_ms"] for r in ok if r.get("ttft_ms")], 95)},
        "total_ms": {"p50": _pct([r["total_ms"] for r in ok], 50), "p95": _pct([r["total_ms"] for r in ok], 95),
                     "max": _pct([r["total_ms"] for r in ok], 100)},
        "errors": [r for r in out if r.get("status") != 200],
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=12)
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args(argv[1:])
    try:
        p = plan(os.environ, args.requests, args.concurrency)
    except CostGuardRefused as exc:
        print(f"\n  REFUSING: {exc}\n", file=sys.stderr)
        return 1
    key = os.environ.get("ORACLE_FIREWORKS_API_KEY") or os.environ.get("FIREWORKS_API_KEY") or ""
    if not key:
        print("  no provider key in the environment", file=sys.stderr)
        return 2
    url = os.environ.get("ORACLE_FIREWORKS_URL") or DEFAULT_URL
    model = os.environ.get("ORACLE_FIREWORKS_MODEL") or "accounts/fireworks/models/qwen3-30b-a3b"
    print(f"  plan: {p}")
    res = asyncio.run(run(p, url, key, model))
    res["recorded_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    out = pathlib.Path(__file__).resolve().parent / "out" / "results" / f"ai_real_sample-{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("statuses", "ttft_ms", "total_ms")}, indent=1))
    print(f"  → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
