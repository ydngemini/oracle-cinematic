"""Checkout — the revenue endpoint — had no tests at all.

Two failures it used to have, both landing on the buyer:

 1. STRIPE_PRICE_ID defaults to the placeholder "price_REPLACE_ME", and nothing
    checked it. A misconfigured deploy reached Stripe, got "No such price", and
    the handler returned Stripe's text verbatim as a 400 — so the customer read
    an operator's configuration error at the moment of payment, phrased as
    though the request were theirs.
 2. Every InvalidRequestError did the same, leaking price ids, tax-registration
    state and account details to whoever clicked Subscribe.
"""

from __future__ import annotations

import asyncio

import pytest
import stripe
from fastapi import HTTPException

import billing
from tenancy import Role, TenantContext


class _Session:
    """Stripe returns an object, not a dict — the handler reads .id and .url."""

    def __init__(self, id_, url):
        self.id = id_
        self.url = url

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)


def _request(tenant_id=TENANT_ID):
    return billing.CheckoutRequest(tenant_id=tenant_id)


def _checkout(ctx=CTX, tenant_id=TENANT_ID):
    return asyncio.run(billing.create_checkout_session(_request(tenant_id), ctx=ctx))


# ── the price guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "price",
    ["price_REPLACE_ME", "", "   ", "prod_SomeProductNotAPrice"],
)
def test_an_unusable_price_refuses_before_stripe_is_called(monkeypatch, price):
    called = False

    def _never(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Stripe must not be called with an unusable price")

    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", price)
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_never))

    with pytest.raises(HTTPException) as excinfo:
        _checkout()

    assert excinfo.value.status_code == 503
    assert called is False
    # The buyer is told the plan is not configured — not shown a Stripe id.
    assert "price_" not in excinfo.value.detail
    assert "not configured" in excinfo.value.detail


def test_a_real_price_reaches_stripe_and_returns_the_url(monkeypatch):
    seen = {}

    def _create(**kwargs):
        seen.update(kwargs)
        return _Session("cs_test_123", "https://checkout.stripe.com/c/pay/cs_test_123")

    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_1TftozEDjW1NbBU5FaAQGPiH")
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_create))

    result = _checkout()

    assert result.url.startswith("https://checkout.stripe.com/")
    assert seen["line_items"][0]["price"] == "price_1TftozEDjW1NbBU5FaAQGPiH"
    assert seen["metadata"] == {"tenant_id": TENANT_ID}


# ── what the buyer is allowed to read ────────────────────────────────────────

def test_a_stripe_rejection_is_never_echoed_to_the_buyer(monkeypatch):
    leak = "No such price: 'price_1TftozEDjW1NbBU5FaAQGPiH'; a similar object exists in test mode"

    def _raise(**_kwargs):
        raise stripe.error.InvalidRequestError(leak, param="line_items[0][price]")

    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_live_looking_id")
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_raise))

    with pytest.raises(HTTPException) as excinfo:
        _checkout()

    # 502, not 400: the request body is only a tenant_id, so this is never the
    # caller's fault and must not be reported as though it were.
    assert excinfo.value.status_code == 502
    assert leak not in excinfo.value.detail
    assert "price_" not in excinfo.value.detail
    assert "test mode" not in excinfo.value.detail


# ── the IDOR guard, which did exist and must keep existing ───────────────────

def test_an_agent_cannot_open_checkout_for_another_tenant(monkeypatch):
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_ok")
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        staticmethod(lambda **_k: (_ for _ in ()).throw(AssertionError("must not reach Stripe"))),
    )

    with pytest.raises(HTTPException) as excinfo:
        _checkout(tenant_id=OTHER_TENANT)

    assert excinfo.value.status_code == 403


def test_a_platform_admin_may_act_for_another_tenant(monkeypatch):
    admin = TenantContext(
        agent_id="ops@platform.test",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_ok")
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        staticmethod(lambda **_k: _Session("cs_1", "https://checkout.stripe.com/c/pay/cs_1")),
    )

    result = _checkout(ctx=admin, tenant_id=OTHER_TENANT)

    assert result.url.endswith("cs_1")


# ── the misconfiguration predicate itself ────────────────────────────────────

@pytest.mark.parametrize(
    "price,expected",
    [
        ("price_1TftozEDjW1NbBU5FaAQGPiH", None),
        ("price_REPLACE_ME", "unset"),
        ("", "unset"),
        ("prod_ABC123", "not a Stripe price id"),
    ],
)
def test_the_predicate_names_the_variable_and_the_expectation(monkeypatch, price, expected):
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", price)
    reason = billing._price_misconfigured()
    if expected is None:
        assert reason is None
    else:
        assert reason is not None
        assert "STRIPE_PRICE_ID" in reason
        assert expected in reason


def test_a_generic_stripe_error_is_not_echoed_either(monkeypatch):
    """The catch-all branch leaked `f"Stripe error: {exc}"` for the same reason."""
    leak = "Your account cannot currently make live charges: verification required"

    def _raise(**_kwargs):
        raise stripe.error.APIConnectionError(leak)

    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_ok")
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_raise))

    with pytest.raises(HTTPException) as excinfo:
        _checkout()

    assert excinfo.value.status_code == 502
    assert leak not in excinfo.value.detail
    assert "verification" not in excinfo.value.detail


# ── the cancellation path ────────────────────────────────────────────────────

def _portal(ctx=CTX, tenant_id=TENANT_ID):
    return asyncio.run(
        billing.create_portal_session(billing.PortalRequest(tenant_id=tenant_id), ctx=ctx)
    )


class _Row(dict):
    pass


def _with_subscription(monkeypatch, customer_id):
    from contextlib import asynccontextmanager

    class _Conn:
        async def fetchrow(self, _query, *_args):
            return None if customer_id is _MISSING else _Row(stripe_customer_id=customer_id)

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(billing, "get_pool", lambda: object())
    monkeypatch.setattr(billing, "tenant_tx", tx)


_MISSING = object()


def test_a_subscription_with_no_customer_id_is_not_sent_to_stripe(monkeypatch):
    """A row with a NULL customer id is a broken subscription, not a missing one.

    customer=None reached Stripe and came back as a 502 carrying Stripe's
    wording. Both cases now read the same to the customer, because from their
    side both mean the portal is not there.
    """
    _with_subscription(monkeypatch, None)
    monkeypatch.setattr(
        stripe.billing_portal.Session,
        "create",
        staticmethod(lambda **_k: (_ for _ in ()).throw(AssertionError("must not reach Stripe"))),
    )

    with pytest.raises(HTTPException) as excinfo:
        _portal()

    assert excinfo.value.status_code == 404


def test_the_portal_never_echoes_stripe_to_the_customer(monkeypatch):
    leak = "No such customer: 'cus_123'; a similar object exists in test mode"
    _with_subscription(monkeypatch, "cus_123")

    def _raise(**_kwargs):
        raise stripe.error.InvalidRequestError(leak, param="customer")

    monkeypatch.setattr(stripe.billing_portal.Session, "create", staticmethod(_raise))

    with pytest.raises(HTTPException) as excinfo:
        _portal()

    assert excinfo.value.status_code == 502
    assert leak not in excinfo.value.detail
    assert "cus_123" not in excinfo.value.detail


def test_an_agent_cannot_open_another_tenants_portal(monkeypatch):
    _with_subscription(monkeypatch, "cus_123")

    with pytest.raises(HTTPException) as excinfo:
        _portal(tenant_id=OTHER_TENANT)

    assert excinfo.value.status_code == 403
