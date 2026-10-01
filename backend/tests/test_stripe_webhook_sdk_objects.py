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


class _LedgerConn:
    """Models stripe_webhook_events' primary key: each event id inserts once."""

    def __init__(self):
        self.ids: set[str] = set()
        self.executed: list[tuple] = []

    async def fetchval(self, query, *args):
        assert "INSERT INTO stripe_webhook_events" in query
        if args[0] in self.ids:
            return None
        self.ids.add(args[0])
        return args[0]

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def _fake_tx(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


def _signed(event: dict, secret: str) -> _Req:
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    v1 = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return _Req(body, f"t={ts},v1={v1}")


def test_checkout_completed_reaches_its_handler_as_a_plain_mapping(monkeypatch):
    secret = "whsec_test_secret"
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(billing, "get_pool", lambda: object())
    monkeypatch.setattr(billing, "tenant_tx", _fake_tx(_LedgerConn()))
    seen = {}

    async def handler(conn, obj, created):
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
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "whsec_real_secret_value")
    with pytest.raises(billing.HTTPException) as exc:
        asyncio.run(billing.stripe_webhook(_signed({"type": "x", "data": {"object": {}}}, "whsec_forged")))
    assert exc.value.status_code == 400


def _event(event_id="evt_1", etype="checkout.session.completed", created=None, obj=None):
    return {"id": event_id, "object": "event", "api_version": "2026-05-27", "livemode": False,
            "created": created or int(time.time()), "type": etype,
            "data": {"object": obj or {"id": "cs_1", "object": "checkout.session", "subscription": "sub_1",
                                       "customer": "cus_1", "payment_status": "paid",
                                       "metadata": {"tenant_id": "11111111-1111-1111-1111-111111111111"}}}}


def test_an_empty_webhook_secret_refuses_instead_of_verifying_with_an_empty_key(monkeypatch):
    """BILL-1: stripe-python accepts an HMAC keyed with "" — so an unset secret
    meant anyone could sign a checkout.session.completed for any tenant."""
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "")
    called = []
    monkeypatch.setattr(billing, "_handle_checkout_completed", lambda *a: called.append(a))
    with pytest.raises(billing.HTTPException) as exc:
        asyncio.run(billing.stripe_webhook(_signed(_event(), "")))
    assert exc.value.status_code == 503
    assert called == []


def test_a_replayed_event_changes_billing_state_once(monkeypatch):
    """BILL-3: Stripe re-signs each retry, so the signature cannot stop a replay."""
    secret = "whsec_test_secret"
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(billing, "get_pool", lambda: object())
    conn = _LedgerConn()
    monkeypatch.setattr(billing, "tenant_tx", _fake_tx(conn))
    ev = _event()
    first = asyncio.run(billing.stripe_webhook(_signed(ev, secret)))
    again = asyncio.run(billing.stripe_webhook(_signed(ev, secret)))
    assert first.status_code == again.status_code == 200
    assert b"duplicate" in again.body
    assert len([q for q, _ in conn.executed if "INSERT INTO subscriptions" in q]) == 1


def test_an_older_event_cannot_overwrite_a_newer_state():
    """Every state write is conditioned on the event not being older than the
    last one applied; a late invoice.paid cannot resurrect a cancellation."""
    import inspect

    for handler in (billing._handle_invoice_paid, billing._handle_subscription_updated,
                    billing._handle_checkout_completed):
        assert "last_event_created <=" in inspect.getsource(handler), handler.__name__


def test_unpaid_checkout_does_not_grant_access(monkeypatch):
    conn = _LedgerConn()
    obj = _event()["data"]["object"] | {"payment_status": "unpaid"}
    asyncio.run(billing._handle_checkout_completed(conn, obj, 10))
    (query, args), = conn.executed
    assert args[3] == "incomplete"
