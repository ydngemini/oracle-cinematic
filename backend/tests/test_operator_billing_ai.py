"""The two small additions the operator console needed outside its own module:
an invoice.payment_failed handler that fits the webhook ledger, and a
rate-limit count in the LLM gateway's call counter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import time

import pytest

import billing
import llm_gateway


class _Conn:
    def __init__(self, *, update_result="UPDATE 1", known=True):
        self.update_result, self.known = update_result, known
        self.executed: list[tuple] = []
        self.ids: set[str] = set()

    async def fetchval(self, query, *args):
        if "INSERT INTO stripe_webhook_events" in query:
            if args[0] in self.ids:
                return None
            self.ids.add(args[0])
            return args[0]
        return 1 if self.known else None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return self.update_result


def _tx(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tx(_ctx):
        yield conn
    return tx


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


def _failed_invoice(created=100, event_id="evt_pf1"):
    return {"id": event_id, "object": "event", "api_version": "2026-05-27", "livemode": False,
            "created": created, "type": "invoice.payment_failed",
            "data": {"object": {"id": "in_1", "object": "invoice", "subscription": "sub_1"}}}


def test_payment_failed_moves_only_an_active_subscription_and_respects_order():
    conn = _Conn()
    asyncio.run(billing._handle_invoice_payment_failed(conn, {"subscription": "sub_1"}, 100))
    (query, args), = conn.executed
    assert "status = 'past_due'" in query
    assert "AND status = 'active'" in query          # incomplete/canceled stay as they are
    assert "last_event_created <= $2" in query       # an older failure cannot undo a newer recovery
    assert args == ("sub_1", 100)


def test_payment_failed_for_an_unknown_subscription_asks_stripe_to_retry():
    conn = _Conn(update_result="UPDATE 0", known=False)
    with pytest.raises(billing._SubscriptionNotYetKnown):
        asyncio.run(billing._handle_invoice_payment_failed(conn, {"subscription": "sub_new"}, 5))


def test_payment_failed_on_a_known_non_active_subscription_is_a_no_op():
    conn = _Conn(update_result="UPDATE 0", known=True)
    asyncio.run(billing._handle_invoice_payment_failed(conn, {"subscription": "sub_1"}, 5))


def test_payment_failed_is_routed_through_the_idempotent_ledger(monkeypatch):
    secret = "whsec_test_secret_value"
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(billing, "get_pool", lambda: object())
    conn = _Conn()
    monkeypatch.setattr(billing, "tenant_tx", _tx(conn))
    ev = _failed_invoice()
    first = asyncio.run(billing.stripe_webhook(_signed(ev, secret)))
    again = asyncio.run(billing.stripe_webhook(_signed(ev, secret)))
    assert first.status_code == again.status_code == 200
    assert b"duplicate" in again.body
    assert len([q for q, _ in conn.executed if "past_due" in q]) == 1


def test_refusals_are_counted_and_diagnostics_hold_no_secrets(monkeypatch):
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "whsec_real_secret_value_123")
    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_SENTINELKEY")
    monkeypatch.setattr(billing, "_WEBHOOK_REFUSALS", {})
    with pytest.raises(billing.HTTPException):
        asyncio.run(billing.stripe_webhook(_signed(_failed_invoice(), "whsec_forged_secret")))
    diag = billing.webhook_diagnostics()
    assert diag["refused_since_start"] == {"invalid_signature": 1}
    assert diag["stripe_mode"] == "test" and diag["webhook_secret_configured"] is True
    blob = json.dumps(diag)
    assert "SENTINEL" not in blob and "whsec_real" not in blob


def test_every_state_handler_still_orders_on_event_time():
    for handler in (billing._handle_invoice_paid, billing._handle_subscription_updated,
                    billing._handle_checkout_completed, billing._handle_invoice_payment_failed):
        assert "last_event_created <=" in inspect.getsource(handler), handler.__name__


def test_gateway_counter_separates_rate_limits_from_other_failures():
    counter = llm_gateway._Counter()

    class RateLimitError(Exception):
        pass

    class Throttled(Exception):
        status_code = 429

    counter.record("fast", "foundry", ok=False, exc=RateLimitError("slow down"))
    counter.record("fast", "foundry", ok=False, exc=Throttled())
    counter.record("fast", "foundry", ok=False, exc=TimeoutError())
    counter.record("fast", "foundry", ok=False)
    counter.record("fast", "foundry", ok=True)
    snap = counter.snapshot()
    assert snap["failures"] == {"fast:foundry": 4}
    assert snap["rate_limited"] == {"fast:foundry": 2}
    assert snap["calls"] == {"fast:foundry": 1}
