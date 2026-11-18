"""Business texts reach the carrier only when Neoh can show the sender is
registered for them (external audit 25.1). The SQL itself is proven against
PostgreSQL in test_sms_send_readiness_live."""
from __future__ import annotations

import asyncio

import pytest

import command_providers
import messaging_data as md

READY = {"agent_id": "a", "active": True, "provider": "telnyx", "hosted_order_status": "active",
         "campaign_id": "c1", "sender": "+13025550100", "sender_verified": True,
         "campaign_status": "approved", "campaign_provider": "telnyx", "provider_campaign_id": "TC1",
         "brand_status": "verified", "sender_assigned": True}


@pytest.mark.parametrize("change,blocker", [
    ({}, None),
    ({"hosted_order_status": "pending"}, "hosting_not_active"),      # hosted alone was the old rule
    ({"campaign_id": None}, "no_campaign_assigned"),                  # active hosting + no campaign
    ({"campaign_status": "pending"}, "campaign_not_approved"),
    ({"campaign_status": "rejected"}, "campaign_not_approved"),       # rejected, or revoked later
    ({"campaign_status": "failed"}, "campaign_not_approved"),
    ({"sender_assigned": False}, "number_not_assigned"),              # approved, no assignment / another number
    ({"brand_status": "failed"}, "brand_not_registered"),
    ({"campaign_provider": "twilio"}, "wrong_provider"),
    ({"provider": "twilio"}, "wrong_provider"),
    ({"active": False}, "disconnected"),
    ({"sender_verified": False}, "no_verified_number"),
    ({"sender": "+18885550100"}, "toll_free_not_supported"),
])
def test_each_registration_gap_blocks_the_send(change, blocker):
    blockers = md.send_blockers({**READY, **change}, "telnyx")
    if blocker is None:
        assert blockers == []
    else:
        assert blocker in blockers


def test_no_route_is_not_connected():
    assert md.send_blockers(None, "telnyx") == ["not_connected"]
    assert "connect text messages" in md.describe_blockers(["not_connected"])


def test_readiness_picks_a_ready_route_and_reports_its_sender(monkeypatch):
    from contextlib import asynccontextmanager

    class _Conn:
        async def fetch(self, sql, *args):
            assert "p.detail->>'phone_number' = t.voice_caller_id_e164" in sql
            assert args == ("t1", "agent@a.test")
            return [{**READY, "campaign_status": "pending"}, READY]

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    from tenancy import Role, TenantContext

    monkeypatch.setattr(md, "tenant_tx", tx)
    ctx = TenantContext(agent_id="agent@a.test", tenant_id="t1", role=Role.AGENT)
    out = asyncio.run(md.messaging_send_readiness(ctx, provider="telnyx"))
    assert out == {"ready": True, "blockers": [], "sender": "+13025550100"}


def test_an_undeclared_twilio_sender_is_refused(monkeypatch):
    """Legacy Twilio: an env-only sender used to pass with no registration at
    all (the type check ran only when a type was given)."""
    monkeypatch.setenv("TWILIO_SMS_FROM_NUMBER", "+13025550100")
    monkeypatch.delenv("TWILIO_SMS_SENDER_TYPE", raising=False)
    creds = {"account_sid": "AC" + "1" * 32, "auth_token": "t" * 32}
    draft = {"target": {"phone": "+13025550142"}, "body": "hi"}
    with pytest.raises(command_providers.ProviderConfigurationError, match="registration"):
        asyncio.run(command_providers.send_twilio_sms(draft, credentials=creds))
    with pytest.raises(command_providers.ProviderConfigurationError, match="registration"):
        asyncio.run(command_providers.send_twilio_sms(
            draft, credentials={**creds, "sms_sender": "+13025550100", "sms_sender_type": "unregistered"}))
