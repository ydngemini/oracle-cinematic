#!/usr/bin/env python3
"""Pre-sign provider webhook deliveries for webhook_burst.js (§42–§44).

Signed with the perf environment's OWN secrets (fixtures/perf_secrets.py), with
each provider's real scheme, so the server's verifiers run exactly as in
production:

  telnyx  Ed25519 over "{timestamp}|{body}"   telnyx-signature-ed25519 / -timestamp
  stripe  HMAC-SHA256 over "{t}.{body}"        Stripe-Signature: t=…,v1=…
  plivo   V3: HMAC-SHA256 over url+params.nonce  X-Plivo-Signature-V3 / -Nonce

Includes deliberate DUPLICATE deliveries (providers retry), so the run proves
idempotency, not just throughput. Telnyx and Stripe reject signatures older
than 300 s, so generate immediately before the run.

Writes performance/out/webhooks.json:
  {"requests": [{kind, method, path, headers, body}], "expect": {…}}

Run inside a backend-image container (it needs the plivo SDK):
  python /perf/fixtures/webhooks.py --telnyx 600 --stripe 100 --calls 100 --dup-rate 0.1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import pathlib
import random
import time
import uuid

OUT = pathlib.Path(os.environ.get("PERF_FIXTURE_OUT", "/out"))
BASE_URL = os.environ.get("PERF_PUBLIC_BASE_URL", "https://oracle-perf-lb:8080")  # = the perf env ORACLE_PUBLIC_BASE_URL


def _telnyx(secrets: dict, payload: dict) -> tuple[dict, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secrets["telnyx_private_key"]))
    body = json.dumps(payload, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = base64.b64encode(key.sign(f"{ts}|{body}".encode())).decode()
    return {"Content-Type": "application/json", "telnyx-signature-ed25519": sig, "telnyx-timestamp": ts}, body


def _stripe(secrets: dict, event: dict) -> tuple[dict, str]:
    body = json.dumps(event, separators=(",", ":"))
    ts = str(int(time.time()))
    v1 = hmac.new(secrets["stripe_webhook_secret"].encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "Stripe-Signature": f"t={ts},v1={v1}"}, body


def _plivo(secrets: dict, path: str, params: dict) -> tuple[dict, str]:
    from urllib.parse import urlencode
    from plivo.utils.signature_v3 import construct_post_url, get_signature_v3
    nonce = uuid.uuid4().hex
    base = construct_post_url(BASE_URL + path, params)
    sig = get_signature_v3(secrets["plivo_auth_token"], base, nonce)
    sig = sig.decode() if isinstance(sig, bytes) else sig
    return ({"Content-Type": "application/x-www-form-urlencoded",
             "X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce}, urlencode(params))


def build(n_telnyx: int, n_stripe: int, n_calls: int, dup_rate: float) -> dict:
    secrets = json.loads((OUT / "perf-secrets.json").read_text())
    routes = json.loads((OUT / "routes.json").read_text())
    tenants = list(routes.values())
    rng = random.Random(7)
    reqs: list[dict] = []
    ids = {"telnyx": set(), "stripe_subs": set(), "calls": set()}

    def add(kind, path, headers, body, dup=False):
        reqs.append({"kind": kind + ("-dup" if dup else ""), "method": "POST", "path": path,
                     "headers": headers, "body": body})

    for i in range(n_telnyx):
        t = rng.choice(tenants)
        mid = str(uuid.uuid4())
        ids["telnyx"].add(mid)
        payload = {"data": {"event_type": "message.received", "id": str(uuid.uuid4()), "payload": {
            "id": mid, "type": "SMS", "text": f"Is the house still available? {t['sentinel']}",
            "from": {"phone_number": f"+1555{9000000 + rng.randrange(999999):07d}"},
            "to": [{"phone_number": t["did"]}], "media": []}}}
        h, b = _telnyx(secrets, payload)
        add("telnyx-inbound", "/api/messaging/webhooks/telnyx", h, b)
        if rng.random() < dup_rate:
            add("telnyx-inbound", "/api/messaging/webhooks/telnyx", h, b, dup=True)
        if i % 3 == 0:  # a delivery receipt for a message this platform never sent: a no-op update
            h, b = _telnyx(secrets, {"data": {"event_type": "message.finalized", "id": str(uuid.uuid4()),
                                              "payload": {"id": str(uuid.uuid4()), "to": [{"status": "delivered"}]}}})
            add("telnyx-dlr", "/api/messaging/webhooks/telnyx", h, b)

    for i in range(n_stripe):
        t = rng.choice(tenants)
        sub = f"sub_perf_{t['tenant_id'][:8]}_{i}"
        ids["stripe_subs"].add(sub)
        # Shaped like a real Stripe event (object/api_version/livemode): the SDK
        # builds its Event type from these, and a bare dict is not one.
        event = {"id": f"evt_Perf{uuid.uuid4().hex[:16]}", "object": "event", "api_version": "2024-06-20",
                 "created": int(time.time()), "livemode": False, "type": "checkout.session.completed",
                 "data": {"object": {"id": f"cs_perf_{i}", "object": "checkout.session", "subscription": sub,
                                     "customer": f"cus_perf_{i}", "metadata": {"tenant_id": t["tenant_id"]}}}}
        h, b = _stripe(secrets, event)
        add("stripe", "/billing/webhook", h, b)
        if rng.random() < dup_rate:
            add("stripe", "/billing/webhook", h, b, dup=True)

    for i in range(n_calls):
        t = rng.choice(tenants)
        call = str(uuid.uuid4())
        ids["calls"].add(call)
        caller = f"+1555{8000000 + rng.randrange(999999):07d}"
        path = f"/api/telephony/webhooks/plivo/inbound/{t['endpoint_key']}"
        params = {"CallUUID": call, "CallStatus": "ringing", "From": caller, "To": t["did"]}
        h, b = _plivo(secrets, path, params)
        add("plivo-answer", path, h, b)
        if rng.random() < dup_rate:
            h, b = _plivo(secrets, path, params)
            add("plivo-answer", path, h, b, dup=True)
        spath = f"/api/telephony/webhooks/plivo/status/{t['endpoint_key']}"
        for st in ("in-progress", "completed"):
            h, b = _plivo(secrets, spath, {"CallUUID": call, "CallStatus": st, "From": caller, "To": t["did"]})
            add("plivo-status", spath, h, b)

    return {"generated_at": int(time.time()), "requests": reqs,
            "expect": {"telnyx_unique_messages": len(ids["telnyx"]),
                       "telnyx_message_ids": sorted(ids["telnyx"]),
                       "stripe_unique_subscriptions": len(ids["stripe_subs"]),
                       "plivo_unique_calls": len(ids["calls"]),
                       "plivo_call_ids": sorted(ids["calls"])}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--telnyx", type=int, default=600)
    ap.add_argument("--stripe", type=int, default=100)
    ap.add_argument("--calls", type=int, default=100)
    ap.add_argument("--dup-rate", type=float, default=0.1)
    a = ap.parse_args()
    data = build(a.telnyx, a.stripe, a.calls, a.dup_rate)
    (OUT / "webhooks.json").write_text(json.dumps(data))
    (OUT / "webhooks.json").chmod(0o644)
    kinds: dict = {}
    for r in data["requests"]:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"  {len(data['requests'])} signed deliveries: {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
