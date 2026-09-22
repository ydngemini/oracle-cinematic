"""Plivo 'use my existing business number' — provisioning, OTP verification,
and rate limiting. Mirrors test_business_number_connect.py's structure for
the Twilio path; this file is the Plivo-specific counterpart of properties
1-3 there, plus the OTP-submission flow Twilio's flow doesn't have.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import inbound_voice
import voice_provider
from command_providers import ProviderResult
from tenancy import Role, TenantContext

TENANT_ID = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "agent@example.test"
CTX = TenantContext(agent_id=AGENT_ID, tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
AUTH_ID = "MA" + "a" * 18
NUMBER_A = "+13024078981"
NUMBER_B = "+18662805386"
HIDDEN_DID = "+15551230000"

_INSERT_COLUMNS = (
    "tenant_id", "agent_id", "inbound_did", "twilio_account_sid", "intake_mode",
    "forwarding_mode", "forwarding_source_e164", "sip_domain", "voice_caller_id_e164",
    "voice_caller_id_verified", "sms_sender_e164", "sms_sender_type", "active",
    "agent_forward_e164", "forward_on_request", "forward_when_ai_unavailable",
    "forward_timeout_seconds", "provider", "provider_account_id",
)
_SET_COL_RE = re.compile(r"(\w+)\s*=\s*\$(\d+)")


class FakeRouteConn:
    """Same shape as test_business_number_connect.py's fixture — see there
    for the rationale (reads column names out of the SQL text)."""

    def __init__(self, initial: dict[str, Any] | None = None):
        self.row: dict[str, Any] | None = dict(initial) if initial else None
        self.inserts = 0
        self.updates: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT") and "FROM telephony_routes" in q:
            return dict(self.row) if self.row else None
        if q.startswith("INSERT INTO telephony_routes"):
            self.inserts += 1
            values = dict(zip(_INSERT_COLUMNS, args))
            row = dict(self.row) if self.row else {
                "id": "aaaaaaaa-0000-4000-8000-000000000000",
                "endpoint_key": "bbbbbbbb-0000-4000-8000-000000000000",
                "created_at": datetime.now(timezone.utc),
                "provider": "twilio",
                "outbound_verification_status": "unverified",
                "outbound_verification_sid": None,
                "outbound_verification_requested_at": None,
                "outbound_verification_last_tested_at": None,
                "outbound_verification_failure_reason": None,
                "inbound_forwarding_status": "not_configured",
                "inbound_forwarding_provider_sid": None,
                "inbound_forwarding_last_tested_at": None,
                "inbound_forwarding_failure_reason": None,
                "provider_account_id": None,
                "provider_app_id": None,
                "outbound_verification_channel": None,
                "outbound_verification_attempt_count": 0,
                "outbound_verification_locked_until": None,
            }
            row.update(values)
            row["updated_at"] = datetime.now(timezone.utc)
            self.row = row
            return dict(row)
        if q.startswith("UPDATE telephony_routes"):
            set_clause = q.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            row = dict(self.row) if self.row else {}
            for name, idx in _SET_COL_RE.findall(set_clause):
                row[name] = args[int(idx) - 1]
            self.row = row
            self.updates.append(dict(row))
            return dict(row)
        raise AssertionError(f"unexpected query: {q}")


def _patch_tx(monkeypatch, conn: FakeRouteConn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)


class FakePlivoAdapter:
    """Records calls instead of touching the network."""

    def __init__(self, *, purchased_number=HIDDEN_DID, verified=False):
        self.purchased_number = purchased_number
        self.provisioned = 0
        self.configured: list[dict[str, Any]] = []
        self.verify_started: list[dict[str, Any]] = []
        self.verify_completed: list[dict[str, Any]] = []
        self._verified = verified

    async def provision_forwarding_number(self, *, credentials=None, area_code=None):
        self.provisioned += 1
        return ProviderResult(
            "plivo_number", self.purchased_number, "purchased",
            {"phone_number": self.purchased_number},
        )

    async def configure_number_webhook(self, sid, *, voice_url, status_callback_url=None, credentials=None):
        self.configured.append({"sid": sid, "voice_url": voice_url})

    async def verify_caller_id_start(self, number, *, channel="sms", credentials=None):
        self.verify_started.append({"number": number, "channel": channel})
        return ProviderResult("plivo_caller_id", "verify-uuid-1", "pending", {})

    async def verify_caller_id_complete(self, verification_id, otp, *, phone_number, credentials=None):
        self.verify_completed.append({"verification_id": verification_id, "otp": otp})
        return self._verified


# ── connect_business_number_generic: idempotent provisioning ────────────


def test_connect_provisions_hidden_number_and_starts_verification(monkeypatch):
    conn = FakeRouteConn()
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter()
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.example")

    row = asyncio.run(
        inbound_voice.connect_business_number_generic(
            CTX, NUMBER_A, provider="plivo", provider_account_id=AUTH_ID, credentials={}
        )
    )

    assert adapter.provisioned == 1
    assert row["provider"] == "plivo"
    assert row["provider_account_id"] == AUTH_ID
    assert row["inbound_did"] == HIDDEN_DID
    assert row["inbound_forwarding_status"] == "active"
    assert row["outbound_verification_status"] == "pending"
    assert row["outbound_verification_channel"] == "sms"
    assert len(adapter.verify_started) == 1


def test_connect_reuses_existing_hidden_number_for_same_provider(monkeypatch):
    existing = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "endpoint_key": "bbbbbbbb-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "provider_account_id": AUTH_ID,
        "inbound_did": HIDDEN_DID,
        "inbound_forwarding_provider_sid": HIDDEN_DID,
        "inbound_forwarding_status": "active",
        "voice_caller_id_e164": NUMBER_A,
        "voice_caller_id_verified": True,
        "outbound_verification_status": "verified",
        "intake_mode": "auto",
        "forward_on_request": False,
        "forward_when_ai_unavailable": False,
        "forward_timeout_seconds": 25,
    }
    conn = FakeRouteConn(existing)
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter()
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)

    row = asyncio.run(
        inbound_voice.connect_business_number_generic(
            CTX, NUMBER_A, provider="plivo", provider_account_id=AUTH_ID, credentials={}
        )
    )

    assert adapter.provisioned == 0, "must not re-purchase an existing hidden number"
    assert len(adapter.verify_started) == 0, "must not re-verify an already-verified number"
    assert row["voice_caller_id_verified"] is True


def test_connect_with_a_new_number_re_arms_verification_but_keeps_hidden_number(monkeypatch):
    existing = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "endpoint_key": "bbbbbbbb-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "provider_account_id": AUTH_ID,
        "inbound_did": HIDDEN_DID,
        "inbound_forwarding_provider_sid": HIDDEN_DID,
        "inbound_forwarding_status": "active",
        "voice_caller_id_e164": NUMBER_A,
        "voice_caller_id_verified": True,
        "outbound_verification_status": "verified",
        "intake_mode": "auto",
        "forward_on_request": False,
        "forward_when_ai_unavailable": False,
        "forward_timeout_seconds": 25,
    }
    conn = FakeRouteConn(existing)
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter()
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)

    row = asyncio.run(
        inbound_voice.connect_business_number_generic(
            CTX, NUMBER_B, provider="plivo", provider_account_id=AUTH_ID, credentials={}
        )
    )

    assert adapter.provisioned == 0, "the hidden number is infrastructure, unrelated to which business number is connected"
    assert len(adapter.verify_started) == 1
    assert row["voice_caller_id_verified"] is False


# ── complete_business_number_verification: OTP + rate limiting ─────────


def test_complete_verification_marks_verified_on_correct_otp(monkeypatch):
    route = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "voice_caller_id_e164": NUMBER_A,
        "outbound_verification_status": "pending",
        "outbound_verification_sid": "verify-uuid-1",
        "outbound_verification_attempt_count": 0,
        "outbound_verification_locked_until": None,
    }
    conn = FakeRouteConn(route)
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter(verified=True)
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)

    row = asyncio.run(
        inbound_voice.complete_business_number_verification(CTX, "123456", credentials={})
    )
    assert row["voice_caller_id_verified"] is True
    assert row["outbound_verification_status"] == "verified"
    assert row["outbound_verification_attempt_count"] == 0


def test_complete_verification_wrong_otp_increments_attempts(monkeypatch):
    route = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "voice_caller_id_e164": NUMBER_A,
        "outbound_verification_status": "pending",
        "outbound_verification_sid": "verify-uuid-1",
        "outbound_verification_attempt_count": 0,
        "outbound_verification_locked_until": None,
    }
    conn = FakeRouteConn(route)
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter(verified=False)
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)

    row = asyncio.run(
        inbound_voice.complete_business_number_verification(CTX, "000000", credentials={})
    )
    assert row["outbound_verification_attempt_count"] == 1
    assert row.get("outbound_verification_locked_until") is None


def test_complete_verification_locks_out_after_max_attempts(monkeypatch):
    route = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "voice_caller_id_e164": NUMBER_A,
        "outbound_verification_status": "pending",
        "outbound_verification_sid": "verify-uuid-1",
        "outbound_verification_attempt_count": inbound_voice._MAX_VERIFICATION_ATTEMPTS - 1,
        "outbound_verification_locked_until": None,
    }
    conn = FakeRouteConn(route)
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter(verified=False)
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)

    row = asyncio.run(
        inbound_voice.complete_business_number_verification(CTX, "000000", credentials={})
    )
    assert row["outbound_verification_attempt_count"] == inbound_voice._MAX_VERIFICATION_ATTEMPTS
    assert row["outbound_verification_locked_until"] is not None


def test_complete_verification_refuses_while_locked_out(monkeypatch):
    route = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "voice_caller_id_e164": NUMBER_A,
        "outbound_verification_status": "pending",
        "outbound_verification_sid": "verify-uuid-1",
        "outbound_verification_attempt_count": inbound_voice._MAX_VERIFICATION_ATTEMPTS,
        "outbound_verification_locked_until": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    conn = FakeRouteConn(route)
    _patch_tx(monkeypatch, conn)
    adapter = FakePlivoAdapter(verified=True)
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: adapter)

    with pytest.raises(inbound_voice.InboundVoiceError):
        asyncio.run(inbound_voice.complete_business_number_verification(CTX, "123456", credentials={}))
    assert len(adapter.verify_completed) == 0, "must not even contact Plivo while locked out"


def test_complete_verification_rejects_twilio_routes(monkeypatch):
    """A Twilio route has no Plivo verification session to complete against."""
    route = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000",
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "twilio",
        "voice_caller_id_e164": NUMBER_A,
    }
    conn = FakeRouteConn(route)
    _patch_tx(monkeypatch, conn)

    with pytest.raises(inbound_voice.InboundVoiceError):
        asyncio.run(
            inbound_voice.complete_business_number_verification(CTX, "123456", credentials={})
        )


# ── get_verified_caller_id: provider-agnostic, no change needed ─────────


def test_get_verified_caller_id_works_identically_for_plivo_routes(monkeypatch):
    route = {
        "tenant_id": TENANT_ID,
        "agent_id": AGENT_ID,
        "provider": "plivo",
        "voice_caller_id_e164": NUMBER_A,
        "voice_caller_id_verified": True,
        "outbound_verification_status": "verified",
    }
    conn = FakeRouteConn(route)
    _patch_tx(monkeypatch, conn)

    result = asyncio.run(inbound_voice.get_verified_caller_id(CTX))
    assert result == NUMBER_A
