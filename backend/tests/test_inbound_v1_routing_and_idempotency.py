"""V1 inbound call path ("use my existing business number") — the two-number
model from migration 0106 under test.

Three properties matter enough to be worth their own file:

1. **Routing never trusts the originally-dialed public number.** Carrier
   forwarding can rewrite or drop it; only Twilio's signed `To` (the hidden
   inbound_did) + AccountSid + the endpoint_key in the URL path are ever
   used, and each combination resolves to exactly one tenant+agent — never
   across tenants, never across agents in the same tenant.
2. **Unknown/unprovisioned destinations and bad signatures fail closed** —
   no route, no call record, no crash.
3. **Duplicate Twilio callbacks for the same CallSid are idempotent** — no
   duplicate CRM task, no duplicate intake session, no duplicate transcript
   write — and a successful forwarded call flips inbound_forwarding_status
   to 'active' exactly once.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from types import SimpleNamespace

import pytest
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

import inbound_voice
import telephony_api
from inbound_voice import InboundCallBinding
from tenancy import Role, TenantContext

ACCOUNT_SID = "AC" + "a" * 32

TENANT_A = "11111111-1111-4111-8111-111111111111"
AGENT_A = "agent-a@example.test"
ROUTE_A = "22222222-2222-4222-8222-222222222222"
ENDPOINT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DID_A = "+18662805386"

TENANT_B = "99999999-9999-4999-8999-999999999999"
AGENT_B = "agent-b@example.test"
ROUTE_B = "88888888-8888-4888-8888-888888888888"
ENDPOINT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DID_B = "+13024078981"

CALLER = "+13025550100"


def _route_row(*, tenant_id, agent_id, route_id, endpoint_key, did, status="active"):
    return {
        "id": route_id,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "endpoint_key": endpoint_key,
        "inbound_did": did,
        "twilio_account_sid": ACCOUNT_SID,
        "intake_mode": "buyer",
        "active": True,
        "agent_forward_e164": None,
        "forward_on_request": False,
        "forward_when_ai_unavailable": False,
        "forward_timeout_seconds": 25,
        "voice_caller_id_e164": did,
        "inbound_forwarding_status": status,
        "provider": "twilio",
        "provider_account_id": ACCOUNT_SID,
    }


class FakeRoutesConn:
    """Stands in for the telephony_routes table across two tenants and
    reproduces the exact WHERE-clause semantics resolve_inbound_route and
    resolve_inbound_call_route depend on: endpoint_key AND inbound_did AND
    twilio_account_sid AND active must ALL match one row."""

    def __init__(self, routes: list[dict[str, Any]]):
        self.routes = routes
        self.set_updates: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM telephony_routes" in q and "WHERE endpoint_key=" in q:
            endpoint_uuid, did, provider_account_id, provider = args
            for row in self.routes:
                if (
                    str(row["endpoint_key"]) == str(endpoint_uuid)
                    and row["inbound_did"] == did
                    and row["provider_account_id"] == provider_account_id
                    and row["provider"] == provider
                    and row["active"]
                ):
                    return dict(row)
            return None
        if q.startswith("UPDATE telephony_routes"):
            set_clause = q.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            names = re.findall(r"(\w+)\s*=\s*\$\d+", set_clause)
            tenant_id, agent_id, *values = args
            row = next(
                (
                    r
                    for r in self.routes
                    if r["tenant_id"] == tenant_id and r["agent_id"] == agent_id
                ),
                None,
            )
            if row is None:
                return None
            for name, value in zip(names, values):
                row[name] = value
            self.set_updates.append(dict(row))
            return dict(row)
        raise AssertionError(f"unexpected query: {q}")


def _patch_tx(monkeypatch, conn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)


# ── resolve_inbound_route: cross-tenant isolation ───────────────────────────


def test_hidden_did_a_resolves_only_to_tenant_a(monkeypatch):
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, route_id=ROUTE_B, endpoint_key=ENDPOINT_B, did=DID_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    route = asyncio.run(inbound_voice.resolve_inbound_route(ENDPOINT_A, DID_A, ACCOUNT_SID))
    assert route is not None
    assert route["tenant_id"] == TENANT_A
    assert route["agent_id"] == AGENT_A


def test_hidden_did_b_resolves_only_to_tenant_b(monkeypatch):
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, route_id=ROUTE_B, endpoint_key=ENDPOINT_B, did=DID_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    route = asyncio.run(inbound_voice.resolve_inbound_route(ENDPOINT_B, DID_B, ACCOUNT_SID))
    assert route is not None
    assert route["tenant_id"] == TENANT_B
    assert route["agent_id"] == AGENT_B


def test_did_a_never_resolves_under_tenant_bs_endpoint_key(monkeypatch):
    """Even if a caller (or a forged request) pairs tenant A's hidden DID with
    tenant B's endpoint_key, no route may resolve — every signed field must
    agree on the SAME route."""
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, route_id=ROUTE_B, endpoint_key=ENDPOINT_B, did=DID_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(inbound_voice.resolve_inbound_route(ENDPOINT_B, DID_A, ACCOUNT_SID)) is None
    assert asyncio.run(inbound_voice.resolve_inbound_route(ENDPOINT_A, DID_B, ACCOUNT_SID)) is None


def test_unknown_forwarding_destination_fails_closed(monkeypatch):
    conn = FakeRoutesConn(
        [_route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A)]
    )
    _patch_tx(monkeypatch, conn)

    assert (
        asyncio.run(
            inbound_voice.resolve_inbound_route(ENDPOINT_A, "+19995551234", ACCOUNT_SID)
        )
        is None
    )


def test_inactive_route_does_not_resolve(monkeypatch):
    inactive = _route_row(
        tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A
    )
    inactive["active"] = False
    conn = FakeRoutesConn([inactive])
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(inbound_voice.resolve_inbound_route(ENDPOINT_A, DID_A, ACCOUNT_SID)) is None


# ── Full webhook: unknown DID and bad signature fail closed, no side effects ─


class FakeRequest:
    def __init__(self, form, signature, query_params=None):
        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": signature, "host": "internal.invalid"}
        self.query_params = dict(query_params or {})
        self.url = SimpleNamespace(
            netloc="internal.invalid", scheme="http", query=urlencode(self.query_params)
        )

    async def form(self):
        return self._form


def test_webhook_hangs_up_and_creates_nothing_for_an_unrecognized_did(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-auth-token")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    async def resolve_none(*_args, **_kwargs):
        return None

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("no call binding work may happen for an unmatched route")

    monkeypatch.setattr(telephony_api, "resolve_inbound_route", resolve_none)
    monkeypatch.setattr(telephony_api, "prepare_inbound_call", should_not_run)
    monkeypatch.setattr(telephony_api, "mark_inbound_forwarding_ready", should_not_run)

    form = {
        "CallSid": "CA" + "a" * 32,
        "AccountSid": ACCOUNT_SID,
        "From": CALLER,
        "To": "+19995551234",
    }
    response = asyncio.run(
        telephony_api.twilio_inbound_webhook(ENDPOINT_A, FakeRequest(form, "irrelevant"))
    )
    body = response.body.decode("utf-8")
    assert "<Hangup/>" in body


# ── prepare_inbound_call: duplicate CallSid is idempotent ───────────────────


class FakeCallsConn:
    """One inbound_voice_calls table with a real UNIQUE(provider_call_sid)
    constraint, matching migration behaviour: a second INSERT for the same
    CallSid is a no-op (ON CONFLICT DO NOTHING) and the caller falls back to
    the SELECT branch."""

    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.insert_attempts = 0

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO inbound_voice_calls"):
            self.insert_attempts += 1
            call_sid = args[4]
            if call_sid in self.rows:
                return None  # ON CONFLICT DO NOTHING
            row = {
                "id": f"call-{self._next_id}",
                "tenant_id": args[0],
                "route_id": args[1],
                "contact_id": args[2],
                "client_id": args[3],
                "intake_mode": args[5],
                "callback_task_id": None,
                "contact_intake_session_id": None,
                "intake_handoff_task_id": None,
            }
            self._next_id += 1
            self.rows[call_sid] = row
            return dict(row)
        if q.startswith("SELECT id,tenant_id,route_id,contact_id,client_id,intake_mode"):
            call_sid, tenant_id, route_id = args
            row = self.rows.get(call_sid)
            if row and row["tenant_id"] == tenant_id and row["route_id"] == route_id:
                return dict(row)
            return None
        if "FROM agent_contacts ac" in q:
            return None
        if "FROM clients" in q and "regexp_replace" in q:
            return None
        raise AssertionError(f"unexpected query: {q}")


def test_duplicate_callsid_reuses_the_same_call_binding_not_a_second_row(monkeypatch):
    conn = FakeCallsConn()
    _patch_tx(monkeypatch, conn)

    async def fake_encrypt(_conn, _plaintext, _key):
        return b"ciphertext"

    monkeypatch.setattr(inbound_voice, "encrypt_pii", fake_encrypt)
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-master-key")

    route = _route_row(
        tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A
    )
    call_sid = "CA" + "b" * 32

    first = asyncio.run(
        inbound_voice.prepare_inbound_call(route, call_sid=call_sid, caller_phone=CALLER)
    )
    second = asyncio.run(
        inbound_voice.prepare_inbound_call(route, call_sid=call_sid, caller_phone=CALLER)
    )

    assert first.call_id == second.call_id
    assert conn.insert_attempts == 2
    assert len(conn.rows) == 1


# ── finalize_inbound_voice_call: duplicate finalize creates one CRM task ────


class FakeFinalizeConn:
    def __init__(self, call_row: dict[str, Any]):
        self.call_row = dict(call_row)
        self.task_inserts = 0
        self.executes: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT id,client_id,contact_id,intake_mode,callback_task_id"):
            return dict(self.call_row)
        if q.startswith("INSERT INTO client_tasks"):
            self.task_inserts += 1
            return {"id": f"task-{self.task_inserts}"}
        if q.startswith("INSERT INTO contact_intake_sessions"):
            return {"id": "intake-session-1"}
        if q.startswith("INSERT INTO intake_handoff_tasks"):
            return {"id": "handoff-task-1"}
        raise AssertionError(f"unexpected query: {q}")

    async def execute(self, query: str, *args: Any):
        q = " ".join(query.split())
        self.executes.append((q, args))
        if q.startswith("UPDATE inbound_voice_calls"):
            # Mirror the write back so a second finalize call in the same
            # test observes the task as already created, exactly like a real
            # transaction committed between two webhook deliveries would.
            self.call_row["callback_task_id"] = args[7]
        return "UPDATE 1"


def test_duplicate_finalize_for_the_same_callsid_creates_only_one_crm_task(monkeypatch):
    call_sid = "CA" + "c" * 32
    conn = FakeFinalizeConn(
        {
            "id": "call-1",
            "client_id": None,
            "contact_id": None,
            "intake_mode": "buyer",
            "callback_task_id": None,
            "contact_intake_session_id": None,
            "intake_handoff_task_id": None,
        }
    )

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)

    async def fake_encrypt(_conn, _plaintext, _key):
        return b"ciphertext"

    monkeypatch.setattr(inbound_voice, "encrypt_pii", fake_encrypt)
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-master-key")

    transcript = [{"role": "caller", "text": "I am looking to buy a house."}]
    state = {"direction": "inbound", "tenant_id": TENANT_A, "agent_id": AGENT_A}

    asyncio.run(inbound_voice.finalize_inbound_voice_call(call_sid, transcript, state))
    asyncio.run(inbound_voice.finalize_inbound_voice_call(call_sid, transcript, state))

    assert conn.task_inserts == 1


# ── mark_inbound_forwarding_ready: the test-call mechanism ─────────────────


def test_mark_inbound_forwarding_ready_flips_pending_to_active(monkeypatch):
    conn = FakeRoutesConn(
        [_route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, status="pending")]
    )
    _patch_tx(monkeypatch, conn)

    route = conn.routes[0]
    asyncio.run(inbound_voice.mark_inbound_forwarding_ready(route))

    assert conn.set_updates, "expected an UPDATE to persist the ready state"
    updated = conn.set_updates[-1]
    assert updated["inbound_forwarding_status"] == "active"
    assert updated["inbound_forwarding_last_tested_at"] is not None
    assert updated["inbound_forwarding_failure_reason"] is None


def test_mark_inbound_forwarding_ready_is_a_no_op_once_already_active(monkeypatch):
    conn = FakeRoutesConn(
        [_route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, status="active")]
    )
    _patch_tx(monkeypatch, conn)

    route = conn.routes[0]
    asyncio.run(inbound_voice.mark_inbound_forwarding_ready(route))

    assert conn.set_updates == []


def test_signed_inbound_webhook_marks_forwarding_ready_on_first_successful_call(monkeypatch):
    """End-to-end: a genuine signed inbound call through a route that has
    never received one flips inbound_forwarding_status to 'active'."""
    public_base = "https://api.example.test"
    suffix = f"/api/telephony/webhooks/twilio/inbound/{ENDPOINT_A}"
    auth_token = "twilio-auth-token"
    form = {
        "CallSid": "CA" + "d" * 32,
        "AccountSid": ACCOUNT_SID,
        "From": CALLER,
        "To": DID_A,
    }
    signature = RequestValidator(auth_token).compute_signature(public_base + suffix, form)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", public_base)

    route = _route_row(
        tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, status="pending"
    )
    marked = []

    async def resolve(_endpoint, _did, _account):
        return dict(route)

    async def route_tokens(_route):
        return [auth_token]

    async def mark_ready(resolved_route):
        marked.append(dict(resolved_route))

    async def prepare(*_args, **_kwargs):
        return InboundCallBinding(
            call_id="call-1", tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A,
            contact_id=None, client_id=None, intake_mode="buyer",
        )

    async def initialize(*_args, **_kwargs):
        return {"direction": "inbound", "tenant_id": TENANT_A, "agent_id": AGENT_A, "intake_mode": "buyer"}

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(telephony_api, "resolve_inbound_route", resolve)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "mark_inbound_forwarding_ready", mark_ready)
    monkeypatch.setattr(telephony_api, "prepare_inbound_call", prepare)
    monkeypatch.setattr(telephony_api, "initialize_inbound_twilio_call_state", initialize)
    monkeypatch.setattr(telephony_api, "finalize_inbound_voice_call", noop)
    monkeypatch.setattr(telephony_api, "update_inbound_call_status", noop)
    monkeypatch.setattr(telephony_api, "twilio_qwen_enabled", lambda _state: False)

    asyncio.run(telephony_api.twilio_inbound_webhook(ENDPOINT_A, FakeRequest(form, signature)))

    assert len(marked) == 1
    assert marked[0]["tenant_id"] == TENANT_A


def test_bad_signature_never_marks_forwarding_ready(monkeypatch):
    """A route match with an invalid signature must not be treated as proof
    the forwarding setup works — that would let an attacker who merely knows
    the (unsecret) hidden DID and endpoint_key fake a successful test call."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-auth-token")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    route = _route_row(
        tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, status="pending"
    )

    async def resolve(*_args, **_kwargs):
        return dict(route)

    async def route_tokens(_route):
        return ["twilio-auth-token"]

    async def should_not_mark(*_args, **_kwargs):
        raise AssertionError("forwarding must not be marked ready before signature validation")

    monkeypatch.setattr(telephony_api, "resolve_inbound_route", resolve)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "mark_inbound_forwarding_ready", should_not_mark)

    form = {"CallSid": "CA" + "e" * 32, "AccountSid": ACCOUNT_SID, "From": CALLER, "To": DID_A}
    with pytest.raises(telephony_api.HTTPException) as exc_info:
        asyncio.run(
            telephony_api.twilio_inbound_webhook(ENDPOINT_A, FakeRequest(form, "invalid-signature"))
        )
    assert exc_info.value.status_code == 400
