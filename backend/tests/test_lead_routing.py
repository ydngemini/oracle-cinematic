from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import lead_routing_api
from lead_routing_api import (
    LeadIntakePayload,
    RoutingRuleInput,
    _choose_agent,
    _decode_event_cursor,
    _encode_event_cursor,
    _verify_webhook_signature,
    _webhook_signature,
)


TENANT_ID = "11111111-1111-4111-8111-111111111111"


def test_webhook_signature_is_body_bound_and_replay_limited(monkeypatch):
    monkeypatch.setattr(lead_routing_api.time, "time", lambda: 1_800_000_000)
    body = b'{"external_event_id":"evt-1"}'
    timestamp = "1800000000"
    signature = _webhook_signature("secret-value", timestamp, body)
    _verify_webhook_signature("secret-value", timestamp, body, signature)

    with pytest.raises(HTTPException, match="signature"):
        _verify_webhook_signature("secret-value", timestamp, body + b" ", signature)
    with pytest.raises(HTTPException, match="replay"):
        _verify_webhook_signature("secret-value", "1799999600", body, signature)


def test_lead_payload_normalizes_and_requires_a_contact_method():
    payload = LeadIntakePayload(
        external_event_id="zillow-1",
        full_name="  Sam   Seller ",
        email="SAM@EXAMPLE.TEST",
        intent="seller",
        state_code="de",
        zip_code="19801",
        source_url="https://example.test/lead/1",
    )
    assert payload.full_name == "Sam Seller"
    assert payload.email == "sam@example.test"
    assert payload.state_code == "DE"

    with pytest.raises(ValidationError, match="email or phone"):
        LeadIntakePayload(
            external_event_id="missing-route",
            full_name="No Destination",
            intent="buyer",
        )
    with pytest.raises(ValidationError, match="HTTPS"):
        LeadIntakePayload(
            external_event_id="bad-url",
            full_name="URL Test",
            email="url@example.test",
            intent="buyer",
            source_url="http://example.test/lead",
        )


def test_routing_rule_validates_geography_and_fixed_assignment():
    with pytest.raises(ValidationError, match="fixed_agent"):
        RoutingRuleInput(name="Fixed", assignment_mode="fixed_agent")
    with pytest.raises(ValidationError, match="five-digit"):
        RoutingRuleInput(name="ZIP", zip_codes=["1980"])
    rule = RoutingRuleInput(
        name="Delaware sellers",
        source_key="ZILLOW",
        state_codes=["de", "DE"],
        intent="seller",
        agent_ids=["agent@example.test", "agent@example.test"],
    )
    assert rule.source_key == "zillow"
    assert rule.state_codes == ["DE"]
    assert rule.agent_ids == ["agent@example.test"]


def test_event_cursor_round_trips_and_rejects_tampering():
    received_at = datetime.now(timezone.utc).replace(microsecond=0)
    event_id = "22222222-2222-4222-8222-222222222222"
    cursor = _encode_event_cursor(received_at, event_id)
    assert _decode_event_cursor(cursor) == (received_at, event_id)
    with pytest.raises(HTTPException, match="cursor"):
        _decode_event_cursor(cursor[:-2] + "xx")


def test_round_robin_uses_only_tenant_agents_with_capacity():
    now = datetime.now(timezone.utc)

    class FakeConn:
        async def fetchrow(self, query, *args):
            assert "tenant_id=$5::uuid" in query
            assert args[-1] == TENANT_ID
            return {
                "id": "33333333-3333-4333-8333-333333333333",
                "agent_ids": [],
                "assignment_mode": "round_robin",
            }

        async def fetch(self, query, *args):
            assert "u.tenant_id=$1::uuid" in query
            return [
                {
                    "agent_id": "full@example.test",
                    "accepting_leads": True,
                    "capacity": 1,
                    "open_contacts": 1,
                    "last_assigned_at": None,
                },
                {
                    "agent_id": "recent@example.test",
                    "accepting_leads": True,
                    "capacity": 10,
                    "open_contacts": 1,
                    "last_assigned_at": now,
                },
                {
                    "agent_id": "next@example.test",
                    "accepting_leads": True,
                    "capacity": 10,
                    "open_contacts": 2,
                    "last_assigned_at": now - timedelta(days=1),
                },
            ]

    selected, reason = asyncio.run(
        _choose_agent(
            FakeConn(),
            TENANT_ID,
            "zillow",
            LeadIntakePayload(
                external_event_id="evt-2",
                full_name="Buyer Example",
                email="buyer@example.test",
                intent="buyer",
            ),
        )
    )
    assert selected == "next@example.test"
    assert reason.startswith("rule:")


def test_routing_migration_forces_rls_and_keeps_webhook_secrets_encrypted():
    migration = (
        Path(__file__).parents[1] / "db" / "migrations" / "0061_lead_intake_and_routing.sql"
    ).read_text(encoding="utf-8")
    for table in (
        "lead_source_connectors",
        "lead_routing_rules",
        "agent_routing_state",
        "lead_intake_events",
    ):
        assert f"'{table}'" in migration
    assert "webhook_secret_ciphertext bytea NOT NULL" in migration
    assert "payload_ciphertext       bytea NOT NULL" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "REVOKE DELETE,TRUNCATE" in migration
    assert "webhook_secret text" not in migration

