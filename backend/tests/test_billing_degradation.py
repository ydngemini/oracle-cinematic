"""A lapsed subscription blocks new outbound contact and nothing else (T18.05,
AC18.07, F18.07), on fakes. The database half — signed Stripe events moving a
real subscription, the composer, the outbox and the command worker — is
tests/test_billing_degradation_live.py.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import billing
from tenancy import Role, TenantContext

T = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
AGENT = TenantContext(agent_id="agent@lapsed.test", tenant_id=T, role=Role.AGENT)
LAPSED = (402, "An active Neoh subscription is required for this action.")


class _Reached(Exception):
    """The composer got past the gate and opened its transaction."""


def _composer(monkeypatch, *, refusal):
    """crm.send_message with the gate's reading stubbed; any database use
    after the gate raises _Reached, so a test sees how far a message got."""
    import crm

    gate_calls = []

    async def reading(ctx):
        gate_calls.append(ctx.agent_id)
        return refusal

    @asynccontextmanager
    async def tx(ctx):
        raise _Reached()
        yield  # pragma: no cover

    async def postal(_tenant_id):
        raise _Reached()

    import commercial_email

    monkeypatch.setattr(billing, "entitlement_refusal", reading)
    monkeypatch.setattr(commercial_email, "require_postal_address", postal)
    monkeypatch.setattr(crm, "tenant_tx", tx)
    return crm, gate_calls


def test_the_composer_refuses_an_outbound_email_before_anything_is_written(monkeypatch):
    crm, gate_calls = _composer(monkeypatch, refusal=LAPSED)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(crm.send_message(CLIENT, crm.MessageCreate(channel="email", body="Hi"), AGENT))
    assert refused.value.status_code == 402
    assert gate_calls == [AGENT.agent_id]


def test_a_platform_admin_gets_the_refusal_up_front_not_a_queued_email(monkeypatch):
    """email_outbox.deliver refuses for the tenant whoever queued the row, so
    an admin's 'queued' would be blocked for certain and nobody told."""
    crm, gate_calls = _composer(monkeypatch, refusal=LAPSED)
    admin = TenantContext(agent_id="ops@neoh.test", tenant_id=T, role=Role.PLATFORM_ADMIN)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(crm.send_message(CLIENT, crm.MessageCreate(channel="email", body="Hi"), admin))
    assert (refused.value.status_code, refused.value.detail) == LAPSED
    assert gate_calls == [admin.agent_id]


@pytest.mark.parametrize("channel,direction", [
    ("email", "inbound"),   # logging what the client sent
    ("note", "outbound"),   # an internal note has no direction and leaves nothing
    ("sms", "outbound"),    # log-only here: this route has no SMS provider
])
def test_logging_and_notes_stay_open_for_a_lapsed_brokerage(monkeypatch, channel, direction):
    crm, gate_calls = _composer(monkeypatch, refusal=LAPSED)
    with pytest.raises(_Reached):
        asyncio.run(crm.send_message(
            CLIENT, crm.MessageCreate(channel=channel, direction=direction, body="Hi"), AGENT))
    assert gate_calls == []


def test_the_route_gate_and_the_workers_share_one_reading(monkeypatch):
    async def reading(ctx):
        return LAPSED

    monkeypatch.setattr(billing, "entitlement_refusal", reading)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(billing.require_active_subscription(AGENT))
    assert (refused.value.status_code, refused.value.detail) == LAPSED
    # The request gate lets a platform admin through; the workers ask the
    # reading directly, so a send for the tenant is refused whoever runs it.
    admin = TenantContext(agent_id="ops", tenant_id=T, role=Role.PLATFORM_ADMIN)
    assert asyncio.run(billing.require_active_subscription(admin)) is admin


def test_an_unreadable_status_is_not_a_refusal_or_a_pass(monkeypatch):
    """Unknown is not missing: no database with enforcement on is a 503 the
    caller retries, never a silent pass and never a final 'blocked'."""
    from db import connection

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
    with pytest.raises(HTTPException) as unknown:
        asyncio.run(billing.entitlement_refusal(AGENT))
    assert unknown.value.status_code == 503
    monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "0")
    assert asyncio.run(billing.entitlement_refusal(AGENT)) is None


def test_a_refused_send_is_dead_lettered_not_retried():
    """A retry would send the moment the subscription came back."""
    exc = billing.NotEntitled("Not sent: An active Neoh subscription is required for this action.")
    assert exc.job_terminal is True
    assert exc.job_error_code == "NOT_ENTITLED"


def test_reads_and_the_data_export_are_never_behind_the_subscription_gate():
    """AC18.07: a lapsed brokerage can still read and export everything. The
    gate is for spend and contact; no GET and no export route may carry it."""
    import server
    from fastapi.routing import APIRoute

    def gated(route):
        return any(d.call is billing.require_active_subscription for d in route.dependant.dependencies)

    routes = [r for r in server.app.routes if isinstance(r, APIRoute)]
    assert [r.path for r in routes if "GET" in r.methods and gated(r)] == []
    exports = [r for r in routes if r.path.startswith("/api/privacy/exports")]
    assert {r.path for r in exports} == {"/api/privacy/exports", "/api/privacy/exports/{operation_id}/download"}
    assert not any(gated(r) for r in exports)
