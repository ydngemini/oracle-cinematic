"""The Stripe webhook must work with the REAL SDK's event objects.

stripe-python 15 made StripeObject stop being a dict; the handlers' `.get()`
raised KeyError('get'), so every checkout.session.completed returned 500 and
the subscription never activated. No test caught it because none ran the
real construct_event — this one does (Mission 8 webhook load test).
"""

import asyncio
import hashlib
import hmac
import json
import time

import pytest

import billing


class _Req:
    def __init__(self, body: bytes, sig: str):
        self._body, self.headers = body, {"stripe-signature": sig}

    async def body(self):
        return self._body


def _signed(event: dict, secret: str) -> _Req:
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    v1 = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return _Req(body, f"t={ts},v1={v1}")


def test_checkout_completed_reaches_its_handler_as_a_plain_mapping(monkeypatch):
    secret = "whsec_test_secret"
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(billing, "get_pool", lambda: object())
    seen = {}

    async def handler(pool, obj):
        seen["tenant"] = obj.get("metadata", {}).get("tenant_id")   # the call that broke
        seen["sub"] = obj.get("subscription")

    monkeypatch.setattr(billing, "_handle_checkout_completed", handler)
    event = {"id": "evt_1", "object": "event", "api_version": "2024-06-20", "created": int(time.time()),
             "livemode": False, "type": "checkout.session.completed",
             "data": {"object": {"id": "cs_1", "object": "checkout.session", "subscription": "sub_1",
                                 "customer": "cus_1", "metadata": {"tenant_id": "t-1"}}}}
    resp = asyncio.run(billing.stripe_webhook(_signed(event, secret)))
    assert resp.status_code == 200
    assert seen == {"tenant": "t-1", "sub": "sub_1"}


def test_a_bad_signature_is_still_refused(monkeypatch):
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "whsec_real")
    with pytest.raises(billing.HTTPException) as exc:
        asyncio.run(billing.stripe_webhook(_signed({"type": "x", "data": {"object": {}}}, "whsec_forged")))
    assert exc.value.status_code == 400
