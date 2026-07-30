"""Focused regressions for CRM comms and latent client-task contracts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import crm
from client_enterprise import TaskCreate
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
INTERACTION_ID = "33333333-3333-3333-3333-333333333333"
THREAD_ID = "44444444-4444-4444-4444-444444444444"
OUTBOX_ID = "55555555-5555-5555-5555-555555555555"
LEAD_ID = "77777777-7777-7777-7777-777777777777"
LISTING_ID = "88888888-8888-8888-8888-888888888888"
PUBLIC_RECORD_ID = "99999999-9999-9999-9999-999999999999"
CTX = TenantContext(agent_id="agent@example.test", tenant_id=TENANT_ID, role=Role.AGENT)


def _fake_tenant_tx(conn, seen_contexts=None):
    @asynccontextmanager
    async def tx(ctx):
        if seen_contexts is not None:
            seen_contexts.append(ctx)
        yield conn

    return tx


class _MessageConn:
    def __init__(self, *, email="client@example.test", phone="+13055550142"):
        self.client = {
            "id": CLIENT_ID,
            "full_name": "Client One",
            "email": email,
            "phone": phone,
            "client_type": "buyer",
        }
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id, full_name, email, phone, client_type FROM clients" in query:
            return self.client
        if "INSERT INTO interaction_logs" in query:
            return {
                "id": INTERACTION_ID,
                "lead_id": None,
                "client_id": CLIENT_ID,
                "actor_role": args[2],
                "interaction_type": args[3],
                "direction": args[4],
                "subject": args[5],
                "payload": args[6],
                "thread_id": THREAD_ID,
                "created_at": datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            }
        if "INSERT INTO email_outbox" in query:
            return {"id": OUTBOX_ID}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1" if "INSERT INTO client_activities" in query else "UPDATE 1"

    @property
    def interaction_args(self):
        return next(args for query, args in self.fetchrow_calls if "INSERT INTO interaction_logs" in query)


class _ThreadsConn:
    def __init__(self):
        self.query = ""

    async def fetch(self, query, *_args):
        self.query = query
        return [
            {
                "id": CLIENT_ID,
                "full_name": "First Contact",
                "email": "first@example.test",
                "phone": None,
                "client_type": "seller",
                "interaction_type": None,
                "direction": None,
                "subject": None,
                "snippet_body": None,
                "created_at": None,
                "cnt": 0,
            },
            {
                "id": "66666666-6666-6666-6666-666666666666",
                "full_name": "Existing Note",
                "email": None,
                "phone": "+13055550143",
                "client_type": "buyer",
                "interaction_type": "message",
                "direction": None,
                "subject": None,
                "snippet_body": "Private context",
                "created_at": datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc),
                "cnt": 1,
            },
        ]


class _HouseConn:
    def __init__(
        self,
        *,
        lead=True,
        lead_owner=None,
        linked_listing=None,
        existing_manual=None,
    ):
        self.lead = {
            "id": LEAD_ID,
            "address": "15 Public Record Way, Dover, DE 19901",
            "payload": {"address": "15 Public Record Way, Dover, DE 19901"},
            "seller_client_id": lead_owner,
        } if lead else None
        self.linked_listing = linked_listing
        self.existing_manual = existing_manual
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id, full_name FROM clients" in query:
            return {"id": CLIENT_ID, "full_name": "Client One"}
        if "FROM leads" in query:
            return self.lead
        if "FROM listings" in query and "lead_id = $1" in query:
            return self.linked_listing
        if "lower(btrim(address))" in query:
            return self.existing_manual
        if "UPDATE listings" in query and "RETURNING id, address" in query:
            return {
                **self.existing_manual,
                "seller_client_id": CLIENT_ID,
            }
        if "INSERT INTO listings" in query:
            return {
                "id": LISTING_ID,
                "address": args[1],
                "lead_id": None,
                "seller_client_id": CLIENT_ID,
                "status": "draft",
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1" if "INSERT INTO client_activities" in query else "UPDATE 1"


class _PublicCatalogHouseConn(_HouseConn):
    def __init__(self):
        super().__init__(lead=False)
        self.private_lead = {
            "id": LEAD_ID,
            "address": "15 Public Record Way, Dover, DE 19901",
            "payload": {"address": "15 Public Record Way, Dover, DE 19901"},
            "seller_client_id": CLIENT_ID,
        }

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id, full_name FROM clients" in query:
            return {"id": CLIENT_ID, "full_name": "Client One"}
        if "FROM public_property_records" in query:
            return {
                "id": PUBLIC_RECORD_ID,
                "source_key": "firehose:DE",
                "parcel_id": "DE-PARCEL-1",
                "state": "DE",
                "county": "New Castle",
                "city": "Dover",
                "zip_code": "19901",
                "address": "15 Public Record Way, Dover, DE 19901",
                "owner_name": "OWNER OF RECORD",
                "owner_type": "individual",
                "public_record_value": 250000,
                "reported_record_date": None,
                "zoning_district": None,
                "land_use": "Residential",
                "lot_area_sqft": 6000,
                "building_area_sqft": 1400,
                "latitude": 39.1,
                "longitude": -75.5,
                "source_name": "New Castle County parcels + ownership",
                "coverage_scope": "county:New Castle",
                "detail_level": "standard",
                "observed_fields": ["parcel_id", "state", "address"],
                "verification_required": True,
                "record_refreshed_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
                "dataset_version": "2026-07",
            }
        if "FROM leads" in query and "parcel_id=$1" in query:
            return None
        if "INSERT INTO leads" in query:
            return self.private_lead
        if "FROM listings" in query and "lead_id = $1" in query:
            return None
        raise AssertionError(f"Unexpected fetchrow query: {query}")


def test_message_and_task_enums_match_frontend_contract():
    for channel in ("email", "sms", "note", "message", " SMS "):
        assert crm.MessageCreate(channel=channel, body="Hello").channel == channel.strip().lower()

    with pytest.raises(ValidationError):
        crm.MessageCreate(channel="push", body="Hello")

    assert TaskCreate(title="Follow up").priority == "normal"
    with pytest.raises(ValidationError):
        TaskCreate(title="Follow up", priority="medium")


def test_internal_note_is_stored_as_private_message_without_outbound_contact(monkeypatch):
    conn = _MessageConn()
    seen_contexts = []
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn, seen_contexts))

    result = asyncio.run(crm.send_message(CLIENT_ID, crm.MessageCreate(channel="note", body="Private"), CTX))

    interaction_args = conn.interaction_args
    payload = json.loads(interaction_args[6])
    assert interaction_args[2] == "agent"
    assert interaction_args[3] == "message"
    assert interaction_args[4] is None
    assert payload == {"body": "Private", "visibility": "internal"}
    assert result["interaction"]["channel"] == "note"
    assert result["interaction"]["direction"] is None
    assert result["delivery_status"] == "internal"
    assert not any("INSERT INTO email_outbox" in query for query, _args in conn.fetchrow_calls)
    assert not any("UPDATE clients SET last_contacted_at" in query for query, _args in conn.execute_calls)
    assert seen_contexts == [CTX]


def test_sms_is_logged_with_not_sent_metadata_and_never_claims_contact(monkeypatch):
    conn = _MessageConn()
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(crm.send_message(CLIENT_ID, crm.MessageCreate(channel="sms", body="Text"), CTX))

    interaction_args = conn.interaction_args
    payload = json.loads(interaction_args[6])
    assert interaction_args[3] == "sms"
    assert interaction_args[4] == "outbound"
    assert payload["delivery_status"] == "not_sent"
    assert payload["delivery_reason"] == "provider_not_configured"
    assert result["interaction"]["delivery_status"] == "not_sent"
    assert result["delivery_status"] == "not_sent"
    assert not any("INSERT INTO email_outbox" in query for query, _args in conn.fetchrow_calls)
    assert not any("UPDATE clients SET last_contacted_at" in query for query, _args in conn.execute_calls)


def test_sms_requires_a_client_phone_even_for_log_only_entry(monkeypatch):
    conn = _MessageConn(phone=None)
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(crm.send_message(CLIENT_ID, crm.MessageCreate(channel="sms", body="Text"), CTX))

    assert exc.value.status_code == 422
    assert "phone number" in exc.value.detail
    assert not any("INSERT INTO interaction_logs" in query for query, _args in conn.fetchrow_calls)


def test_email_still_uses_compliance_gate_and_durable_outbox(monkeypatch):
    conn = _MessageConn()
    compliance_calls = []

    async def allow_email(*args, **kwargs):
        compliance_calls.append((args, kwargs))

    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(crm, "enforce_outreach", allow_email)

    result = asyncio.run(
        crm.send_message(
            CLIENT_ID,
            crm.MessageCreate(channel="email", subject="Hello", body="Email body"),
            CTX,
        )
    )

    assert compliance_calls
    assert result["queued_email_id"] == OUTBOX_ID
    assert result["delivery_status"] == "queued"
    assert any("INSERT INTO email_outbox" in query for query, _args in conn.fetchrow_calls)
    assert any("UPDATE clients SET last_contacted_at" in query for query, _args in conn.execute_calls)


def test_thread_rollup_includes_tenant_scoped_clients_without_history(monkeypatch):
    conn = _ThreadsConn()
    seen_contexts = []
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn, seen_contexts))

    result = asyncio.run(crm.comms_threads(CTX))

    assert "LEFT JOIN LATERAL" in conn.query
    assert "NULLS LAST" in conn.query
    assert seen_contexts == [CTX]
    assert result["threads"][0] == {
        "client": {
            "id": CLIENT_ID,
            "full_name": "First Contact",
            "email": "first@example.test",
            "phone": None,
            "client_type": "seller",
        },
        "last": None,
        "count": 0,
    }
    assert result["threads"][1]["last"]["channel"] == "note"


def test_house_link_model_requires_exactly_one_source():
    assert crm.ClientHouseLink(lead_id=LEAD_ID).lead_id == LEAD_ID
    assert (
        crm.ClientHouseLink(public_record_id=PUBLIC_RECORD_ID).public_record_id
        == PUBLIC_RECORD_ID
    )
    assert crm.ClientHouseLink(address="  15   Main St  ").address == "15 Main St"

    with pytest.raises(ValidationError):
        crm.ClientHouseLink()
    with pytest.raises(ValidationError):
        crm.ClientHouseLink(lead_id=LEAD_ID, address="15 Main St")
    with pytest.raises(ValidationError):
        crm.ClientHouseLink(lead_id=LEAD_ID, public_record_id=PUBLIC_RECORD_ID)


def test_shared_public_record_is_copied_into_private_crm_only_when_linked(monkeypatch):
    conn = _PublicCatalogHouseConn()
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(
        crm.link_client_house(
            CLIENT_ID,
            crm.ClientHouseLink(public_record_id=PUBLIC_RECORD_ID),
            CTX,
        )
    )

    insert_query, insert_args = next(
        (query, args)
        for query, args in conn.fetchrow_calls
        if "INSERT INTO leads" in query
    )
    payload = json.loads(insert_args[4])
    assert insert_args[0] == TENANT_ID
    assert insert_args[1:3] == ("DE-PARCEL-1", "DE")
    assert payload["public_record_id"] == PUBLIC_RECORD_ID
    assert payload["provenance"]["data_classification"] == "public_property_record"
    assert "tenant_id" not in payload
    assert "contact" not in payload
    assert result["created"] is True
    assert result["house"]["lead_id"] == LEAD_ID
    assert result["house"]["source"] == "public_record"


def test_public_record_house_link_is_tenant_scoped_and_idempotent(monkeypatch):
    conn = _HouseConn()
    seen_contexts = []
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn, seen_contexts))

    result = asyncio.run(
        crm.link_client_house(CLIENT_ID, crm.ClientHouseLink(lead_id=LEAD_ID), CTX)
    )

    assert result["created"] is True
    assert result["house"]["source"] == "public_record"
    assert result["house"]["lead_id"] == LEAD_ID
    assert any("UPDATE leads SET seller_client_id" in query for query, _args in conn.execute_calls)
    assert any("INSERT INTO client_activities" in query for query, _args in conn.execute_calls)
    assert seen_contexts == [CTX]

    already_linked = _HouseConn(lead_owner=CLIENT_ID)
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(already_linked))
    repeated = asyncio.run(
        crm.link_client_house(CLIENT_ID, crm.ClientHouseLink(lead_id=LEAD_ID), CTX)
    )
    assert repeated["created"] is False
    assert not any("UPDATE leads SET seller_client_id" in query for query, _args in already_linked.execute_calls)
    assert not any("INSERT INTO client_activities" in query for query, _args in already_linked.execute_calls)


def test_public_record_house_link_rejects_missing_or_other_client_record(monkeypatch):
    missing = _HouseConn(lead=False)
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(missing))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(crm.link_client_house(
            CLIENT_ID,
            crm.ClientHouseLink(lead_id=LEAD_ID),
            CTX,
        ))
    assert exc.value.status_code == 404

    other_client = "99999999-9999-9999-9999-999999999999"
    conflict = _HouseConn(lead_owner=other_client)
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conflict))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(crm.link_client_house(
            CLIENT_ID,
            crm.ClientHouseLink(lead_id=LEAD_ID),
            CTX,
        ))
    assert exc.value.status_code == 409
    assert not any("UPDATE leads SET seller_client_id" in query for query, _args in conflict.execute_calls)


def test_manual_house_creates_draft_listing_and_reuses_same_client_record(monkeypatch):
    conn = _HouseConn()
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(
        crm.link_client_house(
            CLIENT_ID,
            crm.ClientHouseLink(address="92 Manual House Lane, Dover, DE 19901"),
            CTX,
        )
    )

    assert result == {
        "house": {
            "id": LISTING_ID,
            "kind": "listing",
            "address": "92 Manual House Lane, Dover, DE 19901",
            "lead_id": None,
            "listing_id": LISTING_ID,
            "client_id": CLIENT_ID,
            "source": "crm_manual",
            "status": "draft",
        },
        "created": True,
    }
    insert = next((query, args) for query, args in conn.fetchrow_calls if "INSERT INTO listings" in query)
    assert "'draft'" in insert[0]
    assert insert[1] == (TENANT_ID, "92 Manual House Lane, Dover, DE 19901", CLIENT_ID)

    existing = {
        "id": LISTING_ID,
        "address": "92 Manual House Lane, Dover, DE 19901",
        "lead_id": None,
        "seller_client_id": CLIENT_ID,
        "status": "draft",
    }
    repeated_conn = _HouseConn(existing_manual=existing)
    monkeypatch.setattr(crm, "tenant_tx", _fake_tenant_tx(repeated_conn))
    repeated = asyncio.run(
        crm.link_client_house(
            CLIENT_ID,
            crm.ClientHouseLink(address="92 Manual House Lane, Dover, DE 19901"),
            CTX,
        )
    )
    assert repeated["created"] is False
    assert not any("INSERT INTO listings" in query for query, _args in repeated_conn.fetchrow_calls)
