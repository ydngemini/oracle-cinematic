"""Business texts reach the carrier only when Neoh can show the sender is
registered for them (external audit 25.1). The SQL itself is proven against
PostgreSQL in test_sms_send_readiness_live."""
from __future__ import annotations

import asyncio

import pytest

import command_providers
import messaging_data as md

ACCOUNT = md.account_fingerprint({"api_key": "KEY-A"})
READY = {"agent_id": "a", "active": True, "provider": "telnyx", "provider_account_id": ACCOUNT,
         "hosted_order_status": "active", "campaign_id": "c1", "sender": "+13025550100",
         "sender_verified": True, "campaign_status": "approved", "campaign_provider": "telnyx",
         "provider_campaign_id": "TC1", "brand_status": "verified", "brand_provider": "telnyx",
         "campaign_fresh": True, "sender_assigned": True}


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
    ({"brand_provider": "twilio"}, "brand_not_registered"),
    ({"provider_account_id": "plivo"}, "wrong_account"),           # a route hosted before 25.1
    ({"provider_account_id": md.account_fingerprint({"api_key": "KEY-B"})}, "wrong_account"),
    # approval not confirmed by a successful provider read in 14 days (a
    # cancelled account answers 401s: the status must not stay sendable)
    ({"campaign_fresh": False}, "registration_unverified"),
])

def test_each_registration_gap_blocks_the_send(change, blocker):
    blockers = md.send_blockers({**READY, **change}, "telnyx", ACCOUNT)
    if blocker is None:
        assert blockers == []
    else:
        assert blocker in blockers


def test_without_a_credential_the_route_must_carry_a_recorded_account():
    assert md.send_blockers(READY, "telnyx", None) == []
    assert md.send_blockers({**READY, "provider_account_id": "plivo"}, "telnyx", None) == ["wrong_account"]
    assert md.send_blockers({**READY, "provider_account_id": None}, "telnyx", None) == ["wrong_account"]


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

        async def fetchval(self, sql, *args):
            return "approved"

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    from tenancy import Role, TenantContext

    monkeypatch.setattr(md, "tenant_tx", tx)
    ctx = TenantContext(agent_id="agent@a.test", tenant_id="t1", role=Role.AGENT)
    out = asyncio.run(md.messaging_send_readiness(ctx, provider="telnyx", account=ACCOUNT))
    assert out == {"ready": True, "blockers": [], "sender": "+13025550100"}


@pytest.mark.parametrize("tenant_campaign,blocker", [
    (None, "no_campaign"), ("pending", "campaign_not_approved"), ("approved", "no_campaign_assigned")])
def test_a_route_without_a_campaign_says_what_is_missing(monkeypatch, tenant_campaign, blocker):
    from contextlib import asynccontextmanager

    from tenancy import Role, TenantContext

    class _Conn:
        async def fetch(self, sql, *args):
            return [{**READY, "campaign_id": None}]

        async def fetchval(self, sql, *args):
            return tenant_campaign

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(md, "tenant_tx", tx)
    ctx = TenantContext(agent_id="agent@a.test", tenant_id="t1", role=Role.AGENT)
    assert asyncio.run(md.messaging_send_readiness(ctx, provider="telnyx", account=ACCOUNT))["blockers"] == [blocker]


def test_the_fingerprint_never_carries_the_key(monkeypatch):
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    fp = md.account_fingerprint({"api_key": "KEY0123456789abcdef"})
    assert fp.startswith("tk:") and "KEY" not in fp and len(fp) == 19
    assert md.account_fingerprint({}) is None
    monkeypatch.setenv("TELNYX_API_KEY", "PLATFORM")
    assert md.account_fingerprint(None) == md.account_fingerprint({"api_key": "PLATFORM"})


def test_the_status_api_reports_the_gate_not_hosting(monkeypatch):
    import messaging_api

    async def creds(_ctx):
        return {"api_key": "KEY-A"}

    seen = {}

    async def readiness(_ctx, *, provider, account=None):
        seen["account"] = account
        return {"ready": False, "blockers": ["number_not_assigned"], "sender": None}

    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "telnyx")
    monkeypatch.setattr(messaging_api, "_telnyx_credentials", creds)
    monkeypatch.setattr(md, "messaging_send_readiness", readiness)
    out = asyncio.run(messaging_api._with_readiness(object(), {"hosted_order_status": "active"}))
    assert out["ready"] is False and out["send_blockers"] == ["number_not_assigned"]
    assert "not confirmed on your approved texting campaign" in out["send_blocker_messages"][0]
    assert seen["account"] == ACCOUNT
    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "carrier-pigeon")
    assert asyncio.run(messaging_api._with_readiness(object(), {}))["send_blockers"] == ["wrong_provider"]


def test_the_platform_never_vouches_for_a_tenant_twilio_sender(monkeypatch):
    monkeypatch.setenv("TWILIO_SMS_SENDER_TYPE", "twilio_registered")
    creds = {"account_sid": "AC" + "1" * 32, "auth_token": "t" * 32, "sms_sender": "+13025550100"}
    with pytest.raises(command_providers.ProviderConfigurationError, match="registration"):
        asyncio.run(command_providers.send_twilio_sms(
            {"target": {"phone": "+13025550142"}, "body": "hi"}, credentials=creds))


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
