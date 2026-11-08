"""channel_setup says a channel needs setup only when the approved command
would fail closed, and names the same setup the executor names (T07.04,
L07-03). Every seam here is the executor's own read."""

from __future__ import annotations

import asyncio

import pytest

import channel_setup
import commands_api
import inbound_voice
import messaging_data
from tenancy import Role, TenantContext

AGENT = TenantContext(agent_id="avery@tenant.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)
OWNER = TenantContext(agent_id="owner@tenant.test", tenant_id=AGENT.tenant_id, role=Role.BROKER_OWNER)


@pytest.fixture
def world(monkeypatch):
    """A brokerage with nothing set up; tests switch pieces on."""
    state = {"credentials": set(), "google": set(), "messaging_route": None, "business_number": None,
             "verified_caller_id": None, "telephony_route": None, "calls": []}
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "TWILIO_API_SECRET",
                 "PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "ORACLE_SMTP_FROM_EMAIL", "ORACLE_SMTP_USERNAME",
                 "ORACLE_CUSTOM_CALL_API_URL", "ORACLE_MESSAGING_PROVIDER", "ORACLE_EMAIL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)

    async def has_credential(ctx, provider, *, account_label=None):
        state["calls"].append((provider, account_label))
        return provider in state["credentials"]

    async def has_google(ctx, account_label):
        state["calls"].append(("google", account_label))
        return account_label in state["google"]

    async def messaging_route(_ctx):
        return state["messaging_route"]

    async def business_number(_ctx):
        return state["business_number"]

    async def verified(_ctx):
        return state["verified_caller_id"]

    async def telephony_route(_ctx):
        return state["telephony_route"]

    monkeypatch.setattr(commands_api, "has_provider_credential", has_credential)
    monkeypatch.setattr(commands_api, "has_google_calendar", has_google)
    monkeypatch.setattr(messaging_data, "get_messaging_route", messaging_route)
    monkeypatch.setattr(messaging_data, "get_public_business_number", business_number)
    monkeypatch.setattr(inbound_voice, "get_verified_caller_id", verified)
    monkeypatch.setattr(inbound_voice, "get_telephony_route", telephony_route)
    return state


def gaps(ctx=AGENT):
    return asyncio.run(channel_setup.gaps(ctx))


def test_with_nothing_set_up_every_channel_names_what_it_needs(world):
    out = gaps()
    assert set(out) == set(channel_setup.CHANNELS)
    assert "mail server" in out["email"] and "Providers" in out["email"]
    assert "business number" in out["sms"] and "text messages" in out["sms"]
    assert out["voice"] == "Neoh calls aren't set up: connect and verify your business number before placing calls."
    assert "Google Calendar" in out["calendar"]
    # The agent's own rows are looked for first, as the executor does.
    assert ("smtp", AGENT.agent_id) in world["calls"] and ("google", AGENT.agent_id) in world["calls"]


# ── sms ─────────────────────────────────────────────────────────────────────

def test_texting_on_the_telnyx_rail_needs_an_active_hosted_order_on_a_verified_number(world):
    world["business_number"] = {"voice_caller_id_e164": "+13025550100"}
    world["messaging_route"] = {"hosted_order_status": "processing"}
    assert "still being set up (processing)" in gaps()["sms"]
    world["messaging_route"] = {"hosted_order_status": "active"}
    assert "sms" not in gaps()
    # Twilio-era route fields never decide this rail.
    world["business_number"] = None
    assert "sms" in gaps()


def test_texting_on_the_twilio_rail_needs_an_account_somewhere(world, monkeypatch):
    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "twilio")
    assert "Twilio" in gaps()["sms"]
    world["credentials"].add("twilio")
    assert "sms" not in gaps()
    world["credentials"].clear()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t" * 32)
    assert "sms" not in gaps()


# ── voice ───────────────────────────────────────────────────────────────────

def test_calls_need_a_verified_caller_id_then_the_routes_own_rail(world, monkeypatch):
    world["verified_caller_id"] = "+13025550100"
    world["telephony_route"] = {"provider": "plivo"}
    assert "Plivo" in gaps()["voice"]
    monkeypatch.setenv("PLIVO_AUTH_ID", "MA" + "0" * 18)
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "p" * 32)
    assert "voice" not in gaps()
    monkeypatch.delenv("PLIVO_AUTH_ID")
    world["credentials"].add("plivo")
    assert "voice" not in gaps()

    world["credentials"].clear()
    world["telephony_route"] = {"provider": "twilio"}
    assert "Twilio" in gaps()["voice"]
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "https://calls.example.test")
    assert "voice" not in gaps(), "the Twilio path falls back to the custom call URL"


# ── email ───────────────────────────────────────────────────────────────────

def test_email_needs_a_credential_this_agent_can_use_or_the_platform_relay(world, monkeypatch):
    world["credentials"].add("smtp")
    assert "email" not in gaps()
    world["credentials"].clear()
    monkeypatch.setenv("ORACLE_SMTP_FROM_EMAIL", "relay@neoh.test")
    assert "email" not in gaps()


# ── calendar ────────────────────────────────────────────────────────────────

def test_the_calendar_gap_follows_the_row_the_command_loads_whoever_asks(world):
    world["google"].add(OWNER.agent_id)
    assert "calendar" not in gaps(OWNER)
    assert "calendar" in gaps(AGENT)


# ── unknown ─────────────────────────────────────────────────────────────────

def test_a_read_that_fails_leaves_that_channel_out(world, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("telephony_routes unreadable")

    monkeypatch.setattr(inbound_voice, "get_verified_caller_id", boom)
    out = gaps()
    assert "voice" not in out and {"email", "sms", "calendar"} <= set(out)


def test_the_credential_readers_share_the_executors_selection():
    """has_provider_credential / has_google_calendar read the very rows the
    loaders decrypt — one SQL each, so they cannot drift apart."""
    import inspect

    load = inspect.getsource(commands_api._load_provider_credential)
    assert "_PROVIDER_ROW_SQL" in load and "SELECT" not in load
    token = inspect.getsource(commands_api._load_google_access_token)
    assert "_GOOGLE_ROW_SQL" in token and "SELECT" not in token
    assert "account_label=$2" in commands_api._GOOGLE_ROW_SQL
