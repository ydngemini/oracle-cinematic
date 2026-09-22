"""Plivo inbound routing + webhook signature validation.

Mirrors test_inbound_v1_routing_and_idempotency.py's properties for the
Plivo path, where the key difference from Twilio is that resolve_inbound_route
has no confirmed account-identity field to match on for Plivo (see that
function's docstring) — so isolation here rests on endpoint_key + inbound_did
alone, PLUS the per-tenant V3 signature check the webhook performs right
after. Both layers are tested: the DB-level match, and the signature gate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import plivo.utils as plivo_utils
import pytest
from contextlib import asynccontextmanager

import inbound_voice
import plivo_call_handler
import telephony_api

TENANT_A = "11111111-1111-4111-8111-111111111111"
AGENT_A = "agent-a@example.test"
ROUTE_A = "22222222-2222-4222-8222-222222222222"
ENDPOINT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DID_A = "+18662805386"
AUTH_ID_A = "MA" + "a" * 18
AUTH_TOKEN_A = "token-a-" + "x" * 20

TENANT_B = "99999999-9999-4999-8999-999999999999"
AGENT_B = "agent-b@example.test"
ROUTE_B = "88888888-8888-4888-8888-888888888888"
ENDPOINT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DID_B = "+13024078981"
AUTH_ID_B = "MA" + "b" * 18
AUTH_TOKEN_B = "token-b-" + "y" * 20


def _route_row(*, tenant_id, agent_id, route_id, endpoint_key, did, auth_id):
    return {
        "id": route_id,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "endpoint_key": endpoint_key,
        "inbound_did": did,
        "provider": "plivo",
        "provider_account_id": auth_id,
        "intake_mode": "buyer",
        "active": True,
        "agent_forward_e164": None,
        "forward_on_request": False,
        "forward_when_ai_unavailable": False,
        "forward_timeout_seconds": 25,
        "voice_caller_id_e164": did,
    }


class FakeRoutesConn:
    def __init__(self, routes: list[dict[str, Any]]):
        self.routes = routes

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM telephony_routes" in q and "WHERE endpoint_key=" in q:
            # resolve_inbound_route's no-account-id branch: endpoint_key, did, provider
            endpoint_uuid, did, provider = args
            for row in self.routes:
                if (
                    str(row["endpoint_key"]) == str(endpoint_uuid)
                    and row["inbound_did"] == did
                    and row["provider"] == provider
                    and row["active"]
                ):
                    return dict(row)
            return None
        raise AssertionError(f"unexpected query: {q}")


def _patch_tx(monkeypatch, conn):
    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)


# ── resolve_inbound_route: cross-tenant isolation without an account field ─


def test_hidden_did_a_resolves_only_to_tenant_a(monkeypatch):
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, auth_id=AUTH_ID_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, route_id=ROUTE_B, endpoint_key=ENDPOINT_B, did=DID_B, auth_id=AUTH_ID_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    route = asyncio.run(
        inbound_voice.resolve_inbound_route(ENDPOINT_A, DID_A, None, provider="plivo")
    )
    assert route is not None
    assert route["tenant_id"] == TENANT_A


def test_did_a_never_resolves_under_tenant_bs_endpoint_key(monkeypatch):
    conn = FakeRoutesConn(
        [
            _route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, auth_id=AUTH_ID_A),
            _route_row(tenant_id=TENANT_B, agent_id=AGENT_B, route_id=ROUTE_B, endpoint_key=ENDPOINT_B, did=DID_B, auth_id=AUTH_ID_B),
        ]
    )
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(
        inbound_voice.resolve_inbound_route(ENDPOINT_B, DID_A, None, provider="plivo")
    ) is None
    assert asyncio.run(
        inbound_voice.resolve_inbound_route(ENDPOINT_A, DID_B, None, provider="plivo")
    ) is None


def test_unknown_destination_fails_closed(monkeypatch):
    conn = FakeRoutesConn(
        [_route_row(tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A, endpoint_key=ENDPOINT_A, did=DID_A, auth_id=AUTH_ID_A)]
    )
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(
        inbound_voice.resolve_inbound_route(ENDPOINT_A, "+19995551234", None, provider="plivo")
    ) is None


def test_twilio_routes_never_resolve_under_plivo_provider(monkeypatch):
    """A route created under Twilio must not accidentally satisfy a Plivo
    webhook just because the endpoint_key/DID happen to match — the
    provider column is part of the match."""
    twilio_row = _route_row(
        tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A,
        endpoint_key=ENDPOINT_A, did=DID_A, auth_id=AUTH_ID_A,
    )
    twilio_row["provider"] = "twilio"
    conn = FakeRoutesConn([twilio_row])
    _patch_tx(monkeypatch, conn)

    assert asyncio.run(
        inbound_voice.resolve_inbound_route(ENDPOINT_A, DID_A, None, provider="plivo")
    ) is None


# ── validate_plivo_signature: the cryptographic confirmation layer ──────


class FakePlivoRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers
        self.url = type("U", (), {"query": ""})()
        import types

        self.url = types.SimpleNamespace(scheme="https", netloc="neoh.example", query="")


def _sign(url: str, nonce: str, auth_token: str, params: dict[str, str]) -> str:
    return plivo_utils.signature_v3.get_signature_v3(
        auth_token,
        plivo_utils.signature_v3.construct_post_url(url, dict(params)).decode("utf-8"),
        nonce,
    ).decode("utf-8")


def test_valid_v3_signature_is_accepted(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.example")
    monkeypatch.setenv("ORACLE_ENV", "production")
    suffix = "/api/telephony/webhooks/plivo/status/route1"
    url = f"https://neoh.example{suffix}"
    params = {"CallUUID": "uuid-1", "CallStatus": "completed"}
    nonce = "nonce-123"
    signature = _sign(url, nonce, AUTH_TOKEN_A, params)

    request = FakePlivoRequest(
        {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}
    )
    from starlette.datastructures import FormData

    form = FormData(params)
    telephony_api.validate_plivo_signature(request, form, suffix, tokens=[AUTH_TOKEN_A])


def test_wrong_tenant_token_is_rejected(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.example")
    monkeypatch.setenv("ORACLE_ENV", "production")
    suffix = "/api/telephony/webhooks/plivo/status/route1"
    url = f"https://neoh.example{suffix}"
    params = {"CallUUID": "uuid-1", "CallStatus": "completed"}
    nonce = "nonce-123"
    # Signed with tenant A's token, but validated against tenant B's — the
    # exact scenario resolve_inbound_route's "resolve broadly, confirm with
    # a per-tenant secret" design must catch.
    signature = _sign(url, nonce, AUTH_TOKEN_A, params)

    request = FakePlivoRequest(
        {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}
    )
    from fastapi import HTTPException
    from starlette.datastructures import FormData

    form = FormData(params)
    with pytest.raises(HTTPException) as exc_info:
        telephony_api.validate_plivo_signature(request, form, suffix, tokens=[AUTH_TOKEN_B])
    assert exc_info.value.status_code == 400


def test_missing_signature_header_is_rejected(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.example")
    monkeypatch.setenv("ORACLE_ENV", "production")
    suffix = "/api/telephony/webhooks/plivo/status/route1"
    request = FakePlivoRequest({})
    from fastapi import HTTPException
    from starlette.datastructures import FormData

    form = FormData({})
    with pytest.raises(HTTPException) as exc_info:
        telephony_api.validate_plivo_signature(request, form, suffix, tokens=[AUTH_TOKEN_A])
    assert exc_info.value.status_code == 400


def test_altered_payload_invalidates_the_signature(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.example")
    monkeypatch.setenv("ORACLE_ENV", "production")
    suffix = "/api/telephony/webhooks/plivo/status/route1"
    url = f"https://neoh.example{suffix}"
    original_params = {"CallUUID": "uuid-1", "CallStatus": "completed"}
    nonce = "nonce-123"
    signature = _sign(url, nonce, AUTH_TOKEN_A, original_params)

    tampered_params = {"CallUUID": "uuid-1", "CallStatus": "failed"}
    request = FakePlivoRequest(
        {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}
    )
    from fastapi import HTTPException
    from starlette.datastructures import FormData

    form = FormData(tampered_params)
    with pytest.raises(HTTPException) as exc_info:
        telephony_api.validate_plivo_signature(request, form, suffix, tokens=[AUTH_TOKEN_A])
    assert exc_info.value.status_code == 400


# ── prepare_inbound_call: Plivo call UUIDs are accepted, Twilio format is not ─


def test_prepare_inbound_call_accepts_a_plivo_call_uuid():
    route = _route_row(
        tenant_id=TENANT_A, agent_id=AGENT_A, route_id=ROUTE_A,
        endpoint_key=ENDPOINT_A, did=DID_A, auth_id=AUTH_ID_A,
    )
    # Just validate the format gate directly — the DB path is exercised by
    # test_inbound_v1_routing_and_idempotency.py's FakeCallsConn pattern for
    # Twilio; here the property under test is the format acceptance itself.
    inbound_voice._validate_provider_call_id(
        "612ec2a1-d33c-11e8-816c-0630a5643bb6", "plivo"
    )  # must not raise


def test_prepare_inbound_call_rejects_a_twilio_shaped_id_for_plivo():
    with pytest.raises(ValueError):
        inbound_voice._validate_provider_call_id("CA" + "a" * 32, "plivo")


def test_prepare_inbound_call_rejects_a_plivo_shaped_id_for_twilio():
    with pytest.raises(ValueError):
        inbound_voice._validate_provider_call_id(
            "612ec2a1-d33c-11e8-816c-0630a5643bb6", "twilio"
        )
