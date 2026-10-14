"""GET /billing/plan reports what checkout bills, or 'unavailable' (prompt 18 T18.03)."""

from __future__ import annotations

import asyncio

import billing


def _fresh(monkeypatch, price_id="price_123", key="sk_test_x"):
    billing._PLAN_CACHE.clear()
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", price_id)
    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", key)


def test_the_plan_price_is_the_stripe_price_checkout_bills(monkeypatch):
    _fresh(monkeypatch)
    calls = []

    def retrieve(pid):
        calls.append(pid)
        return {"unit_amount": 199900, "currency": "USD", "recurring": {"interval": "month", "interval_count": 1}}

    monkeypatch.setattr(billing.stripe.Price, "retrieve", retrieve)
    monkeypatch.setenv("ORACLE_PLAN_NAME", "Neoh Brokerage")
    info = asyncio.run(billing._stripe_plan())
    assert (info.name, info.amount_cents, info.currency, info.interval, info.source) == (
        "Neoh Brokerage", 199900, "usd", "month", "stripe")
    asyncio.run(billing._stripe_plan())
    assert calls == ["price_123"], "cached, not fetched on every page load"


def test_a_misconfigured_price_is_unavailable_not_guessed(monkeypatch):
    _fresh(monkeypatch, price_id="price_REPLACE_ME")
    info = asyncio.run(billing._stripe_plan())
    assert info.source == "unavailable" and info.amount_cents is None
    assert info.reason == "not_configured"


def test_no_secret_key_is_not_configured(monkeypatch):
    _fresh(monkeypatch, key="")
    info = asyncio.run(billing._stripe_plan())
    assert (info.source, info.reason) == ("unavailable", "not_configured")


def test_a_stripe_failure_is_unavailable_not_guessed(monkeypatch):
    _fresh(monkeypatch)

    def boom(_pid):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(billing.stripe.Price, "retrieve", boom)
    info = asyncio.run(billing._stripe_plan())
    assert info.source == "unavailable" and info.amount_cents is None
    assert info.reason == "lookup_failed", "an outage is not 'not set up'"
