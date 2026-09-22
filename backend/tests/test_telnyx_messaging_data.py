"""messaging_data.py — business-number reuse, eligibility gate, hosted-order
connect/OTP flow, inbound tenant isolation, and message idempotency.

Mirrors test_plivo_business_number_connect.py / test_plivo_inbound_routing.py
for the messaging rail.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

import messaging_data
import messaging_provider
from command_providers import ProviderResult
from tenancy import Role, TenantContext

TENANT_A = "33333333-3333-4333-8333-333333333333"
AGENT_A = "agent@example.test"
CTX = TenantContext(agent_id=AGENT_A, tenant_id=TENANT_A, role=Role.BROKER_OWNER)
NUMBER_A = "+13024078981"

TENANT_B = "44444444-4444-4444-8444-444444444444"
AGENT_B = "agent-b@example.test"

_SET_COL_RE = re.compile(r"(\w+)\s*=\s*\$(\d+)")


class FakeTelephonyConn:
    """Simulates the telephony_routes lookup get_public_business_number does."""

    def __init__(self, row: dict[str, Any] | None):
        self.row = row

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        assert "FROM telephony_routes" in q
        return dict(self.row) if self.row else None


class FakeMessagingRouteConn:
    def __init__(self, initial: dict[str, Any] | None = None):
        self.row: dict[str, Any] | None = dict(initial) if initial else None
        self.inserts = 0

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT") and "FROM messaging_routes" in q:
            return dict(self.row) if self.row else None
        if q.startswith("INSERT INTO messaging_routes"):
            self.inserts += 1
            if self.row is None:
                self.row = {
                    "id": "aaaaaaaa-1111-4111-8111-111111111111",
                    "tenant_id": TENANT_A,
                    "agent_id": AGENT_A,
                    "provider": args[2],
                    "provider_account_id": args[3],
                    "messaging_profile_id": None,
                    "eligibility_status": "unknown",
                    "eligibility_checked_at": None,
                    "eligibility_detail": None,
                    "hosted_order_id": None,
                    "hosted_order_status": "not_started",
                    "hosted_order_failure_reason": None,
                    "verification_method": None,
                    "loa_document_state": "not_required",
                    "invoice_document_state": "not_required",
                    "campaign_id": None,
                    "active": True,
                    "disconnected_at": None,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            return dict(self.row)
        if q.startswith("UPDATE messaging_routes"):
            set_clause = q.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            row = dict(self.row) if self.row else {}
            for name, idx in _SET_COL_RE.findall(set_clause):
                row[name] = args[int(idx) - 1]
            self.row = row
            return dict(row)
        raise AssertionError(f"unexpected query: {q}")


def _patch_tx_sequence(monkeypatch, conns: list[Any]):
    """messaging_data functions open a fresh tenant_tx per call — supply one
    fake connection per call in order."""
    remaining = list(conns)

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield remaining.pop(0) if remaining else conns[-1]

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)


class FakeTelnyxAdapter:
    def __init__(self, *, eligible=True, verified=False):
        self._eligible = eligible
        self._verified = verified
        self.eligibility_calls = 0
        self.order_calls = 0
        self.verify_start_calls: list[dict[str, Any]] = []
        self.verify_complete_calls: list[dict[str, Any]] = []

    async def check_number_eligibility(self, phone_numbers, *, credentials=None):
        self.eligibility_calls += 1
        status = messaging_provider.ELIGIBILITY_ELIGIBLE if self._eligible else messaging_provider.ELIGIBILITY_INELIGIBLE_WIRELESS
        return [
            messaging_provider.EligibilityResult(phone_number=n, status=status, detail="test")
            for n in phone_numbers
        ]

    async def begin_hosted_messaging(self, phone_number, *, messaging_profile_id=None, credentials=None):
        self.order_calls += 1
        return ProviderResult("telnyx_hosted_order", "order-uuid-1", "pending", {"phone_number": phone_number})

    async def start_ownership_verification(self, order_id, phone_number, *, method="sms", credentials=None):
        self.verify_start_calls.append({"order_id": order_id, "phone_number": phone_number, "method": method})

    async def complete_ownership_verification(self, order_id, phone_number, code, *, credentials=None):
        self.verify_complete_calls.append({"order_id": order_id, "code": code})
        return self._verified

    async def get_hosted_order_status(self, order_id, *, credentials=None):
        return {"status": "successful", "phone_numbers": [{"phone_number": NUMBER_A, "status": "successful"}]}

    async def disconnect_hosted_number(self, order_id, phone_number, *, credentials=None):
        pass


def _voice_route_row(*, verified=True):
    return {"voice_caller_id_e164": NUMBER_A, "voice_caller_id_verified": verified, "voice_provider": "plivo"}


# ── get_public_business_number: reuse, never a second number field ──────


def test_get_public_business_number_requires_voice_verification(monkeypatch):
    conn = FakeTelephonyConn(_voice_route_row(verified=False))

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    result = asyncio.run(messaging_data.get_public_business_number(CTX))
    assert result is None


def test_get_public_business_number_returns_verified_number(monkeypatch):
    conn = FakeTelephonyConn(_voice_route_row(verified=True))

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    result = asyncio.run(messaging_data.get_public_business_number(CTX))
    assert result["voice_caller_id_e164"] == NUMBER_A


# ── eligibility check ─────────────────────────────────────────────────────


def test_check_eligibility_fails_closed_with_no_voice_number(monkeypatch):
    conn = FakeTelephonyConn(None)

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    with pytest.raises(messaging_data.MessagingDataError):
        asyncio.run(messaging_data.check_eligibility(CTX, credentials={}))


def test_check_eligibility_records_eligible_status(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn()

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        # get_public_business_number, then get_messaging_route (inside
        # ensure_messaging_route), then the INSERT, then the final UPDATE.
        if call_count["n"] == 1:
            yield telephony_conn
        else:
            yield route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    adapter = FakeTelnyxAdapter(eligible=True)
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)

    route = asyncio.run(messaging_data.check_eligibility(CTX, credentials={}))
    assert route["eligibility_status"] == messaging_provider.ELIGIBILITY_ELIGIBLE
    assert adapter.eligibility_calls == 1


def test_check_eligibility_records_ineligible_wireless(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn()

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield telephony_conn
        else:
            yield route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    adapter = FakeTelnyxAdapter(eligible=False)
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)

    route = asyncio.run(messaging_data.check_eligibility(CTX, credentials={}))
    assert route["eligibility_status"] == messaging_provider.ELIGIBILITY_INELIGIBLE_WIRELESS


# ── connect + OTP verification ───────────────────────────────────────────


def test_connect_messaging_requires_eligible_status_first(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn({
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "tenant_id": TENANT_A,
        "agent_id": AGENT_A,
        "provider": "telnyx",
        "eligibility_status": "unknown",
        "hosted_order_id": None,
        "hosted_order_status": "not_started",
    })

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        yield telephony_conn if call_count["n"] == 1 else route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    with pytest.raises(messaging_data.MessagingDataError):
        asyncio.run(messaging_data.connect_messaging(CTX, provider_account_id="acct", credentials={}))


def test_connect_messaging_submits_order_and_starts_sms_verification(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn({
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "tenant_id": TENANT_A,
        "agent_id": AGENT_A,
        "provider": "telnyx",
        "eligibility_status": messaging_provider.ELIGIBILITY_ELIGIBLE,
        "hosted_order_id": None,
        "hosted_order_status": "not_started",
        "messaging_profile_id": None,
    })

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        yield telephony_conn if call_count["n"] == 1 else route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    adapter = FakeTelnyxAdapter()
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)

    route = asyncio.run(messaging_data.connect_messaging(CTX, provider_account_id="acct", credentials={}))
    assert route["hosted_order_id"] == "order-uuid-1"
    assert route["hosted_order_status"] == "pending_verification"
    assert adapter.order_calls == 1
    assert len(adapter.verify_start_calls) == 1
    assert adapter.verify_start_calls[0]["method"] == "sms"


def test_connect_messaging_is_idempotent_on_existing_order(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn({
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "tenant_id": TENANT_A,
        "agent_id": AGENT_A,
        "provider": "telnyx",
        "eligibility_status": messaging_provider.ELIGIBILITY_ELIGIBLE,
        "hosted_order_id": "order-uuid-1",
        "hosted_order_status": "pending_verification",
    })

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        yield telephony_conn if call_count["n"] == 1 else route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    adapter = FakeTelnyxAdapter()
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)

    route = asyncio.run(messaging_data.connect_messaging(CTX, provider_account_id="acct", credentials={}))
    assert adapter.order_calls == 0, "must not submit a second hosted order for the same route"
    assert route["hosted_order_id"] == "order-uuid-1"


def test_complete_verification_true_marks_processing(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn({
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "tenant_id": TENANT_A,
        "agent_id": AGENT_A,
        "provider": "telnyx",
        "hosted_order_id": "order-uuid-1",
        "hosted_order_status": "pending_verification",
    })

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        # get_messaging_route, get_public_business_number, then the final UPDATE.
        if call_count["n"] == 2:
            yield telephony_conn
        else:
            yield route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    adapter = FakeTelnyxAdapter(verified=True)
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)

    route = asyncio.run(messaging_data.complete_messaging_verification(CTX, "123456", credentials={}))
    assert route["hosted_order_status"] == "processing"


def test_complete_verification_false_raises(monkeypatch):
    telephony_conn = FakeTelephonyConn(_voice_route_row())
    route_conn = FakeMessagingRouteConn({
        "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "tenant_id": TENANT_A,
        "agent_id": AGENT_A,
        "provider": "telnyx",
        "hosted_order_id": "order-uuid-1",
        "hosted_order_status": "pending_verification",
    })

    call_count = {"n": 0}

    @asynccontextmanager
    async def fake_tx(_ctx):
        call_count["n"] += 1
        yield telephony_conn if call_count["n"] == 2 else route_conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    adapter = FakeTelnyxAdapter(verified=False)
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)

    with pytest.raises(messaging_data.MessagingDataError):
        asyncio.run(messaging_data.complete_messaging_verification(CTX, "000000", credentials={}))


# ── inbound routing: tenant isolation, fail closed ──────────────────────


class FakeInboundResolveConn:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    async def fetchrow(self, query: str, *args: Any):
        (to_number,) = args
        for row in self.rows:
            if row["voice_caller_id_e164"] == to_number:
                return dict(row)
        return None


def _route_pair_row(*, tenant_id, agent_id, number, active=True, hosted_status="active"):
    return {
        "messaging_route_id": f"route-{tenant_id}",
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "provider": "telnyx",
        "hosted_order_status": hosted_status,
        "voice_caller_id_e164": number,
    }


def test_resolve_messaging_route_isolates_tenants(monkeypatch):
    conn = FakeInboundResolveConn(
        [
            _route_pair_row(tenant_id=TENANT_A, agent_id=AGENT_A, number="+15551110000"),
            _route_pair_row(tenant_id=TENANT_B, agent_id=AGENT_B, number="+15552220000"),
        ]
    )

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    route_a = asyncio.run(messaging_data.resolve_messaging_route_by_number("+15551110000"))
    assert route_a["tenant_id"] == TENANT_A
    route_b = asyncio.run(messaging_data.resolve_messaging_route_by_number("+15552220000"))
    assert route_b["tenant_id"] == TENANT_B


def test_resolve_messaging_route_fails_closed_for_unknown_number(monkeypatch):
    conn = FakeInboundResolveConn([_route_pair_row(tenant_id=TENANT_A, agent_id=AGENT_A, number="+15551110000")])

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    assert asyncio.run(messaging_data.resolve_messaging_route_by_number("+19995551234")) is None


def test_resolve_messaging_route_rejects_malformed_number(monkeypatch):
    conn = FakeInboundResolveConn([])

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(messaging_data, "tenant_tx", fake_tx)
    assert asyncio.run(messaging_data.resolve_messaging_route_by_number("not-a-number")) is None
