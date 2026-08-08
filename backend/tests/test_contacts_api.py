from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import contacts_api
from contacts_api import (
    ContactCreate,
    ContactPatch,
    IntakeSubmission,
    NurtureJobRequest,
    PropertyRelationshipCreate,
)
from tenancy import Role, TenantContext, require_context


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CONTACT_ID = "22222222-2222-2222-2222-222222222222"
CLIENT_ID = "33333333-3333-3333-3333-333333333333"
SESSION_ID = "44444444-4444-4444-4444-444444444444"
TASK_ID = "55555555-5555-5555-5555-555555555555"
JOB_ID = "66666666-6666-6666-6666-666666666666"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)


def _fake_tenant_tx(conn, seen=None):
    @asynccontextmanager
    async def tx(ctx):
        if seen is not None:
            seen.append(ctx)
        yield conn

    return tx


async def _open_contact(_conn, _tenant_id, _ciphertext):
    return {
        "full_name": "YDN G",
        "email": "ydnop@ydnhft.com",
        "phone": "+13024078981",
    }


class _CreateContactConn:
    def __init__(self):
        self.fetchrow_calls = []
        self.execute_calls = []
        self.created_at = NOW

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id,contact_id FROM clients" in query:
            return {"id": CLIENT_ID, "contact_id": None}
        if "INSERT INTO agent_contacts" in query:
            return {
                "id": CONTACT_ID,
                "assigned_agent_id": CTX.agent_id,
                "pii_ciphertext": b"sealed-contact",
                "email_lookup_hash": "e" * 64,
                "phone_lookup_hash": "p" * 64,
                "birthday_month": None,
                "birthday_day": None,
                "timezone": "America/New_York",
                "state_code": "DE",
                "preferred_channel": "sms",
                "consent": '{"sms":{"granted":false}}',
                "suppression": "{}",
                "nurture_enabled": True,
                "source": "website",
                "legacy_client_id": CLIENT_ID,
                "data_state": "sealed",
                "deleted_at": None,
                "created_at": self.created_at,
                "updated_at": self.created_at,
            }
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


class _IntakeConn:
    def __init__(self):
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT contact.id,contact.assigned_agent_id" in query:
            return {
                "id": CONTACT_ID,
                "assigned_agent_id": CTX.agent_id,
                "client_id": CLIENT_ID,
            }
        if "INSERT INTO contact_intake_sessions" in query:
            return {"id": SESSION_ID, "created_at": NOW}
        if "INSERT INTO intake_handoff_tasks" in query:
            return {"id": TASK_ID, "status": "open", "due_at": NOW}
        raise AssertionError(f"unexpected fetchrow: {query}")


class _NurtureConn:
    def __init__(self, *, duplicate=False, consent=True):
        self.duplicate = duplicate
        self.consent = consent
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id,birthday_month,birthday_day" in query:
            return {
                "id": CONTACT_ID,
                "birthday_month": 8,
                "birthday_day": 3,
                "timezone": "America/New_York",
                "consent": {"email": {"granted": self.consent}},
                "suppression": {},
                "nurture_enabled": True,
            }
        if "INSERT INTO contact_nurture_jobs" in query:
            if self.duplicate:
                return None
            return {
                "id": JOB_ID,
                "state": "scheduled",
                "scheduled_for": NOW,
                "calendar_year": 2026,
                "idempotency_key": args[6],
            }
        if "SELECT id,state,scheduled_for,calendar_year,idempotency_key" in query:
            return {
                "id": JOB_ID,
                "state": "scheduled",
                "scheduled_for": NOW,
                "calendar_year": 2026,
                "idempotency_key": (
                    f"nurture:v1:{TENANT_ID}:{CONTACT_ID}:birthday:email:2026"
                ),
            }
        raise AssertionError(f"unexpected fetchrow: {query}")


class _MissingPropertyConn:
    def __init__(self):
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if "SELECT id FROM agent_contacts" in query:
            return {"id": CONTACT_ID}
        if "SELECT id FROM leads" in query:
            return None
        raise AssertionError(f"unexpected fetchrow: {query}")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def test_every_contact_route_uses_tenant_auth_dependency():
    for route in contacts_api.router.routes:
        dependencies = [dependency.call for dependency in route.dependant.dependencies]
        assert require_context in dependencies, route.path


def test_create_contact_encrypts_canonical_pii_and_dual_writes_client(monkeypatch):
    conn = _CreateContactConn()
    seen = []

    async def seal(_conn, tenant_id, payload):
        assert tenant_id == TENANT_ID
        assert payload == {
            "full_name": "YDN G",
            "email": "ydnop@ydnhft.com",
            "phone": "+13024078981",
        }
        return b"sealed-contact"

    monkeypatch.setattr(contacts_api, "tenant_tx", _fake_tenant_tx(conn, seen))
    monkeypatch.setattr(contacts_api, "seal_json", seal)
    monkeypatch.setattr(contacts_api, "open_json", _open_contact)
    monkeypatch.setattr(
        contacts_api,
        "lookup_hash",
        lambda _tenant, field, value: (field[0] * 64) if value else None,
    )
    monkeypatch.setattr(
        contacts_api,
        "name_search_tokens",
        lambda _tenant, _name: ["n" * 64],
    )

    result = asyncio.run(
        contacts_api.create_contact(
            ContactCreate(
                full_name=" YDN   G ",
                email="YDNOP@YDNHFT.COM",
                phone="302-407-8981",
                timezone="America/New_York",
                state_code="de",
                preferred_channel="sms",
                client_id=CLIENT_ID,
                source="website",
            ),
            CTX,
        )
    )

    insert_query, insert_args = next(
        (query, args)
        for query, args in conn.fetchrow_calls
        if "INSERT INTO agent_contacts" in query
    )
    assert insert_args[0] == TENANT_ID
    assert insert_args[2] == b"sealed-contact"
    assert insert_args[3:5] == ("e" * 64, "p" * 64)
    assert insert_args[5] == ["n" * 64]
    assert insert_args[9] == "DE"
    assert "$1::uuid" in insert_query
    dual_write = next(
        (query, args)
        for query, args in conn.execute_calls
        if "UPDATE clients" in query
    )
    assert dual_write[1] == (
        CONTACT_ID,
        "YDN G",
        "ydnop@ydnhft.com",
        "+13024078981",
        CLIENT_ID,
    )
    assert result["contact"]["phone"] == "+13024078981"
    assert result["contact"]["state_code"] == "DE"
    assert seen == [CTX]


def test_contact_state_code_is_normalized_and_rejects_non_letters():
    assert ContactCreate(full_name="YDN G", state_code=" de ").state_code == "DE"
    assert ContactPatch(state_code="pa").state_code == "PA"
    with pytest.raises(ValidationError, match="two letters"):
        ContactCreate(full_name="YDN G", state_code="1!")


def test_exact_intake_persists_all_encrypted_artifacts_and_handoff(monkeypatch):
    conn = _IntakeConn()
    sealed_payloads = []

    async def seal(_conn, tenant_id, payload):
        assert tenant_id == TENANT_ID
        sealed_payloads.append(payload)
        return f"sealed-{len(sealed_payloads)}".encode()

    monkeypatch.setattr(contacts_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(contacts_api, "seal_json", seal)

    result = asyncio.run(
        contacts_api.create_intake(
            CONTACT_ID,
            IntakeSubmission(
                persona="seller",
                answers=[
                    "5639 S Homan Ave, Chicago, IL",
                    "Within 60 days",
                    "A clean sale",
                ],
            ),
            CTX,
        )
    )

    assert len(sealed_payloads) == 3
    assert sealed_payloads[0]["questions"] == list(
        contacts_api.questions_for("seller")
    )
    assert sealed_payloads[1] == {
        "property_address": "5639 S Homan Ave, Chicago, IL",
        "desired_timeline": "Within 60 days",
        "desired_outcome": "A clean sale",
    }
    assert "Q: What is the property address?" in sealed_payloads[2]["transcript"]
    intake_query, intake_args = next(
        (query, args)
        for query, args in conn.fetchrow_calls
        if "INSERT INTO contact_intake_sessions" in query
    )
    assert "ARRAY[]::text[]" in intake_query
    assert intake_args[0:5] == (
        TENANT_ID,
        CONTACT_ID,
        CLIENT_ID,
        "seller",
        "neoh-intake-v1",
    )
    assert result["intake"]["tool_access"] == []
    assert result["intake"]["status"] == "handoff_pending"
    assert result["handoff_task"]["id"] == TASK_ID


def test_intake_models_reject_extra_or_incomplete_data():
    with pytest.raises(ValidationError):
        IntakeSubmission(persona="buyer", answers=["$200K", "3 and 2"])
    with pytest.raises(ValidationError):
        IntakeSubmission(
            persona="buyer",
            answers=["$200K", "3 and 2", "19963"],
            property_search=True,
        )


def test_contact_patch_preserves_omitted_vs_null_semantics():
    patch = ContactPatch(email=None)
    assert patch.model_dump(exclude_unset=True) == {"email": None}
    assert ContactPatch(birthday_month=None, birthday_day=None).model_dump(
        exclude_unset=True
    ) == {"birthday_month": None, "birthday_day": None}

    for field_name in (
        "timezone",
        "preferred_channel",
        "consent",
        "suppression",
        "nurture_enabled",
        "deleted",
    ):
        with pytest.raises(ValidationError):
            ContactPatch.model_validate({field_name: None})


def test_private_property_relationship_rejects_missing_or_foreign_tenant_anchor(
    monkeypatch,
):
    conn = _MissingPropertyConn()
    monkeypatch.setattr(contacts_api, "tenant_tx", _fake_tenant_tx(conn))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            contacts_api.create_property_relationship(
                CONTACT_ID,
                PropertyRelationshipCreate(
                    property_ref_kind="lead",
                    property_ref_id=JOB_ID,
                    relationship_type="seller",
                ),
                CTX,
            )
        )

    assert exc.value.status_code == 404
    assert not any(
        "INSERT INTO contact_property_relationships" in query
        for query, _args in conn.fetchrow_calls
    )


def test_question_endpoint_exposes_no_tools():
    result = asyncio.run(contacts_api.intake_questions("buyer", CTX))
    assert len(result["questions"]) == 3
    assert result["tool_access"] == []


def test_nurture_reservation_is_exact_once_and_parameterized(monkeypatch):
    conn = _NurtureConn(duplicate=True)
    monkeypatch.setattr(contacts_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(contacts_api, "datetime", _FrozenDatetime)

    result = asyncio.run(
        contacts_api.reserve_nurture_job(
            CONTACT_ID,
            NurtureJobRequest(event_type="birthday", channel="email"),
            CTX,
        )
    )

    insert_query, insert_args = next(
        (query, args)
        for query, args in conn.fetchrow_calls
        if "INSERT INTO contact_nurture_jobs" in query
    )
    assert "ON CONFLICT (tenant_id,contact_id,event_type,channel,calendar_year)" in insert_query
    assert insert_args[0:7] == (
        TENANT_ID,
        CONTACT_ID,
        None,
        "birthday",
        "email",
        2026,
        f"nurture:v1:{TENANT_ID}:{CONTACT_ID}:birthday:email:2026",
    )
    assert result["created"] is False
    assert result["job"]["id"] == JOB_ID


def test_nurture_without_consent_does_not_create_a_job(monkeypatch):
    conn = _NurtureConn(consent=False)
    monkeypatch.setattr(contacts_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(contacts_api, "datetime", _FrozenDatetime)

    result = asyncio.run(
        contacts_api.reserve_nurture_job(
            CONTACT_ID,
            NurtureJobRequest(event_type="birthday", channel="email"),
            CTX,
        )
    )

    assert result["decision"]["reason"] == "consent_missing"
    assert not any(
        "INSERT INTO contact_nurture_jobs" in query
        for query, _args in conn.fetchrow_calls
    )
